"""mailcode.py — find the emailed verification code for a submit that Parth started.

Some Greenhouse boards (Cloudflare, Affirm) gate submission behind a code mailed to the
applicant: after the submit click the page asks for an 8-character code "to confirm you're
a human". The extension has always DETECTED that gate and parked the application; this
module is the other half, added 2026-08-14 at Parth's request — read the code out of his
own inbox so he does not have to go digging for it.

THE RULE THAT DEFINES THIS MODULE: a fetch only ever follows a human action. Parth clicks
Submit, taps the panel's code button, or taps approve in Telegram; nothing here runs on a
timer or on the queue's own initiative. The code proves he controls his own mailbox, which
is true, and he is the one deciding to submit. Nothing in here decides to submit anything.

Two safety properties, both load-bearing:

  * ONLY MAIL THAT ARRIVED AFTER THAT SUBMIT. He applies to several roles a day, so an
    inbox can hold three of these at once. Binding to the click time is what stops an old
    code being typed into a new form (where it fails) or, worse, a code being reused.
  * NEVER A CODE FROM AN UNRELATED MESSAGE. `looks_like_verification` is a HARD gate, not
    a score: without it "any 8 characters in recent mail" would happily lift a bank or
    2FA code out of his inbox and type it into a job application. A missed code costs one
    manual paste. A wrong code taken from the wrong email is a different kind of mistake.

MEASURED against a real Greenhouse code mail 2026-08-14, which corrected two guesses the
first draft had made from the on-page wording alone: the code is ALL LETTERS, MIXED CASE
(so requiring a digit would have rejected it, and upper-casing it would have typed a
different string), and it arrives from a `greenhouse-mail` sender rather than
`greenhouse.io`. Sender and subject stay RANKING signals only — an ALLOW-list is the first
thing to go stale when an ATS changes provider, and it fails closed and silently.

Setting up a mail credential is documented in bridge/README.md; `make_reader` below takes
either shape and says which values are missing when neither is complete.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


# --- what a mail source has to provide ----------------------------------------------

@dataclass(frozen=True)
class MailMessage:
    """One message, reduced to the four things the matcher looks at."""
    uid: str
    received: datetime          # timezone-aware, UTC
    subject: str = ""
    sender: str = ""
    body: str = ""

    def text(self) -> str:
        return f"{self.subject}\n{self.body}"


class MailReader:
    """A source of recent messages.

    One method, so the credential decision (App Password + IMAP vs an OAuth client) is
    confined to one implementation and cannot leak into the matching logic — which is the
    part with the actual rules in it, and the part the tests exercise.
    """

    def search(self, since: datetime) -> list[MailMessage]:  # pragma: no cover - interface
        raise NotImplementedError


class FakeReader(MailReader):
    """A fixed list of messages. Used by the tests and by `--selftest`."""

    def __init__(self, messages: list[MailMessage]):
        self._messages = list(messages)

    def search(self, since: datetime) -> list[MailMessage]:
        return [m for m in self._messages if m.received >= since]


def _flatten_markup(text: str) -> str:
    """HTML -> plain text, enough for a regex to find a code in it.

    Two things matter and neither is cosmetic: tags are stripped so a code wrapped in
    <b></b> is still one token, and entities are unescaped because ATS templates split
    codes with zero-width joiners (&#8203;) that would otherwise break the match.
    """
    import html as _html
    return _html.unescape(re.sub(r"<[^>]+>", " ", text or ""))


class ImapReader(MailReader):
    """Read recent mail over IMAP with a Google App Password.

    THE SIMPLE OPTION, and the honest caveat that comes with it. Setup is one 16-character
    code and nothing else — no Cloud Console project, no consent screen, no refresh token.
    What it costs: an App Password CANNOT BE SCOPED. The credential grants full mailbox
    access including delete, and no amount of care in this file changes that.

    What this file can do, and does: the mailbox is opened with `readonly=True`, which
    issues IMAP EXAMINE rather than SELECT, so the server itself refuses writes on this
    connection and messages are not even marked read. That is read-only BY THIS CODE.
    A `gmail.readonly` OAuth token (see GmailReader) is read-only BY THE CREDENTIAL, which
    is the stronger property and the reason both exist.
    """

    def __init__(self, address: str, app_password: str,
                 host: str = "imap.gmail.com", connect=None):
        self.address = address
        self.app_password = app_password
        self.host = host
        self._connect = connect          # tests inject a fake IMAP4_SSL

    def _open(self):
        if self._connect is not None:
            return self._connect()
        import imaplib
        return imaplib.IMAP4_SSL(self.host)

    def search(self, since: datetime) -> list[MailMessage]:
        import email
        from email.utils import parsedate_to_datetime

        conn = self._open()
        try:
            conn.login(self.address, self.app_password)
            # EXAMINE, not SELECT: read-only at the protocol level, and it leaves the
            # unread state alone — reading a code must not mark his mail as read.
            conn.select("INBOX", readonly=True)
            # IMAP SINCE is date-granular, so this is a coarse pre-filter; find_code
            # re-checks the real timestamp. A day back covers a submit near midnight UTC.
            day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
            typ, data = conn.search(None, "SINCE", day)
            if typ != "OK":
                return []
            ids = (data[0] or b"").split()

            # Narrow SERVER-SIDE where we can. Measured on the real mailbox: a day's SINCE
            # returns 17 messages and fetching them all costs 11.8s, which is most of the
            # time the panel spends waiting; X-GM-RAW cuts it to 3 ids and 1.6s total.
            # That is what makes polling for a code that has not arrived yet affordable.
            #
            # The terms are deliberately a SUPERSET of VERIFY_HINTS — this is a speed
            # filter, never the decision, and looks_like_verification still rules. If it
            # matches nothing we fall back to the unnarrowed list rather than concluding
            # there is no code: a wrong narrowing must cost time, not correctness.
            narrowed = self._narrow(conn, ids)
            if narrowed:
                ids = narrowed
            out = []
            for mid in ids[-25:]:            # newest 25; a code is always among the newest
                typ, payload = conn.fetch(mid, "(RFC822)")
                if typ != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                msg = email.message_from_bytes(payload[0][1])
                try:
                    received = parsedate_to_datetime(msg.get("Date", ""))
                except Exception:
                    continue
                if received is None:
                    continue
                if received.tzinfo is None:
                    received = received.replace(tzinfo=timezone.utc)
                out.append(MailMessage(
                    uid=mid.decode() if isinstance(mid, bytes) else str(mid),
                    received=received.astimezone(timezone.utc),
                    subject=str(msg.get("Subject", "")),
                    sender=str(msg.get("From", "")),
                    body=_flatten_markup(self._body(msg))))
            return out
        finally:
            for close in ("close", "logout"):
                try:
                    getattr(conn, close)()
                except Exception:
                    pass

    # Gmail-only, and optional by design: any other IMAP server rejects X-GM-RAW and we
    # simply keep the unnarrowed list.
    GMAIL_QUERY = ('"newer_than:2d (\\"security code\\" OR \\"verification code\\" '
                   'OR \\"verify your email\\" OR \\"confirm your email\\" '
                   'OR \\"8-character code\\" OR \\"confirm you\\")"')

    @classmethod
    def _narrow(cls, conn, ids):
        """Gmail-side prefilter. Returns a shorter id list, or None to keep them all."""
        try:
            typ, data = conn.search(None, "X-GM-RAW", cls.GMAIL_QUERY)
        except Exception:
            return None                   # not Gmail, or the extension is unavailable
        if typ != "OK":
            return None
        found = (data[0] or b"").split()
        if not found:
            return None                   # narrowed to nothing: distrust it, use the full list
        keep = set(ids)
        subset = [i for i in found if i in keep]
        return subset or None

    @staticmethod
    def _body(msg) -> str:
        """Every text part of a MIME message, concatenated.

        Same reasoning as GmailReader._body: these mails are usually HTML-only, so taking
        'the first text/plain part' would come back empty on exactly the messages that
        matter.
        """
        parts = []
        for part in (msg.walk() if msg.is_multipart() else [msg]):
            if not str(part.get_content_type() or "").startswith("text/"):
                continue
            try:
                raw = part.get_payload(decode=True)
            except Exception:
                continue
            if raw:
                parts.append(raw.decode(part.get_content_charset() or "utf-8", "replace"))
        return "\n".join(parts)


class GmailReader(MailReader):
    """Read recent mail through the Gmail REST API with a `gmail.readonly` token.

    READ-ONLY BY CONSTRUCTION, which is Parth's requirement and the reason this is OAuth
    rather than the simpler App Password over IMAP: an App Password cannot be scoped, so
    it would hand this process full mailbox access — including delete — to read an
    8-character code. The `gmail.readonly` scope cannot modify anything, and he can revoke
    it alone from his Google account without touching any other access.

    No new dependency: `requests` is already here for Telegram, and the two calls this
    needs (messages.list, messages.get) are plain REST. The google-api-python-client would
    add two packages to own an OAuth refresh that is fifteen lines.
    """

    TOKEN_URL = "https://oauth2.googleapis.com/token"
    API = "https://gmail.googleapis.com/gmail/v1/users/me"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, session=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._session = session
        self._access_token = ""
        self._expires_at = 0.0

    # -- auth --------------------------------------------------------------------------
    def _http(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def _token(self) -> str:
        """A live access token, refreshed when the cached one is close to expiring.

        The 60s margin matters: a token that passes the check and then expires mid-flight
        turns into a 401 on the call that was supposed to unblock a submit.
        """
        import time
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        r = self._http().post(self.TOKEN_URL, timeout=20, data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token"})
        r.raise_for_status()
        payload = r.json()
        self._access_token = payload["access_token"]
        self._expires_at = time.time() + float(payload.get("expires_in", 3600))
        return self._access_token

    def _get(self, path: str, **params):
        r = self._http().get(f"{self.API}{path}", timeout=20, params=params,
                             headers={"Authorization": f"Bearer {self._token()}"})
        r.raise_for_status()
        return r.json()

    # -- reading -----------------------------------------------------------------------
    @staticmethod
    def _decode(data: str) -> str:
        import base64
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")

    @staticmethod
    def _flatten(text: str) -> str:
        return _flatten_markup(text)

    @classmethod
    def _body(cls, payload: dict) -> str:
        """Flatten a MIME tree to text.

        Gmail nests the parts (multipart/alternative inside multipart/related, and so on)
        and these mails are usually HTML-only, so walk the whole tree and keep every text
        part rather than hoping for a text/plain sibling.
        """
        out = []
        stack = [payload or {}]
        while stack:
            part = stack.pop()
            data = ((part.get("body") or {}).get("data")) or ""
            if data and str(part.get("mimeType", "")).startswith("text/"):
                out.append(cls._decode(data))
            stack.extend(part.get("parts") or [])
        return _flatten_markup("\n".join(out))

    def search(self, since: datetime) -> list[MailMessage]:
        # Gmail's `after:` is date-granular, so it is a coarse pre-filter only — find_code
        # re-checks every message against the real timestamp. Ask for one day back so a
        # submit just after midnight UTC still sees its own mail.
        day = (since - timedelta(days=1)).strftime("%Y/%m/%d")
        listing = self._get("/messages", q=f"after:{day}", maxResults=25)
        out = []
        for ref in listing.get("messages") or []:
            msg = self._get(f"/messages/{ref['id']}", format="full")
            headers = {h.get("name", "").lower(): h.get("value", "")
                       for h in ((msg.get("payload") or {}).get("headers") or [])}
            received = datetime.fromtimestamp(int(msg.get("internalDate", 0)) / 1000, timezone.utc)
            out.append(MailMessage(
                uid=str(msg.get("id")), received=received,
                subject=headers.get("subject", ""), sender=headers.get("from", ""),
                body=self._body(msg.get("payload") or {})))
        return out


# The two supported credential shapes. OAuth first when both are present: it is the
# read-only one, so if Parth has gone to the trouble of setting it up, an App Password
# left over from trying the easy path first should not silently outrank it.
OAUTH_VARS = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
IMAP_VARS = ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")
ENV_VARS = OAUTH_VARS  # kept: the OAuth set is what the setup script produces


def make_reader(env: dict | None = None) -> MailReader:
    """Build whichever reader is configured, or explain both ways to configure one.

    The dashboard repo does NOT help here: its daily data.json is pushed by a scheduled
    Claude agent using the Gmail connector, which is a once-a-day batch with no way to
    reach a browser tab — wrong shape for a code that expires in minutes.
    """
    import os
    env = os.environ if env is None else env
    get = lambda k: str(env.get(k) or "").strip()

    oauth = [get(k) for k in OAUTH_VARS]
    if all(oauth):
        return GmailReader(*oauth)

    imap = [get(k) for k in IMAP_VARS]
    if all(imap):
        # App Passwords are shown space-grouped ("abcd efgh ijkl mnop") and pasting them
        # that way is the obvious thing to do; Google's IMAP wants them unspaced.
        return ImapReader(imap[0], imap[1].replace(" ", ""))

    # Partial configuration is worth naming precisely — "not configured" when three of
    # four values are set sends you looking in the wrong place.
    started = [k for k in OAUTH_VARS + IMAP_VARS if get(k)]
    detail = f" (set so far: {', '.join(started)})" if started else ""
    raise NotImplementedError(
        "no mail reader configured" + detail + " — either set GMAIL_ADDRESS + "
        "GMAIL_APP_PASSWORD (one code, full-mailbox scope), or run "
        "`python bridge/gmail_auth.py` for a read-only OAuth token")


# --- reading a code out of a message -------------------------------------------------

# Greenhouse asks for "the 8-character code".
#
# CASE IS PRESERVED EXACTLY. A real code (measured 2026-08-14) reads `LsdkUopE` — mixed
# case — and the mail says "Copy and paste this code", so upper-casing it, as the first
# draft did, would type a different string than the one that was issued.
_CODE = r"[A-Za-z0-9]{8}"

# Labelled forms first — "your code is ABCD1234" is unambiguous in a way a bare token is
# not. Ordered most specific to least; the first hit wins.
#
# The gap between label and code is `.{0,n}?`, NOT a negated character class: under re.I a
# class like [^A-Z0-9] excludes lowercase as well, so it could not cross the " is " in
# "your verification code is AB12CD34" and every labelled pattern silently failed. Bounded
# and non-greedy so the label still has to be near the code; re.S so a code on the next
# line is reachable, which is the common layout.
CODE_PATTERNS = [
    re.compile(rf"\b(?:verification|security|confirmation)\s+code\b.{{0,40}}?\b({_CODE})\b", re.I | re.S),
    re.compile(rf"\byour\s+code\s+is\b.{{0,20}}?\b({_CODE})\b", re.I | re.S),
    re.compile(rf"\bcode:\s*({_CODE})\b", re.I),
    re.compile(rf"\benter\s+(?:this|the following)\s+code\b.{{0,40}}?\b({_CODE})\b", re.I | re.S),
    # Last resort: a code alone on its own line, which is how most of these are laid out.
    re.compile(rf"^\s*({_CODE})\s*$", re.I | re.M),
]

# A bare 8-character token is a weak shape — an English word, a hex fragment and a tracking
# id all fit it. So require the token to look GENERATED rather than written.
#
# The first version of this demanded a digit, and a real Greenhouse code turned out to be
# `LsdkUopE` — eight letters, no digit — so it would have rejected the one string it
# existed to find. Two shapes qualify instead, and both mean "not a word someone typed":
#   * a digit mixed with letters  (AB12CD34)
#   * internal case-switching     (LsdkUopE — capitals somewhere other than the first
#     letter, alongside lowercase). "Security" and "PASSWORD" have neither.
def _plausible(code: str) -> bool:
    if not re.search(r"[A-Za-z]", code):
        return False                      # all digits: a date, an amount, an order number
    if re.search(r"\d", code):
        return True
    return bool(re.search(r"[a-z]", code) and re.search(r"(?<=.)[A-Z]", code))


def extract_code(text: str) -> str | None:
    """The verification code in `text`, or None. Returned exactly as it appears."""
    for pat in CODE_PATTERNS:
        for m in pat.finditer(text or ""):
            code = m.group(1)
            if _plausible(code):
                return code
    return None


# --- is this message even about an application? --------------------------------------

# The HARD gate. Phrasing taken from the on-page text the extension already matches
# (fill.js VERIFY_PHRASES), on the assumption the email echoes it.
#
# MEASURED against a real inbox 2026-08-14, and the first draft was badly wrong: it also
# listed a bare "application", which by itself matched 19 of 25 recent messages — every
# rejection, confirmation and job alert. The gate exists to be NARROW, and one loose term
# had quietly turned it into a pass-through; a rejection email carrying an 8-character
# tracking id on its own line would have been read as a verification code. Removing that
# one term took the gate from 20/25 to 4/25, and the 4 are genuine.
#
# So: every term here must name the ACT OF VERIFYING. Words that merely co-occur with job
# mail do not belong, however natural they look when you are writing the list from memory.
# DELIBERATELY ABSENT: "one-time code"/"one-time passcode". Adding it looked harmless and
# the test caught it immediately — that is the standard wording on BANK and 2FA mail, so it
# reopens the one hole this gate exists to close, and it buys nothing: the real Greenhouse
# subject is "Security code for your application to <Company>". Do not add it back.
VERIFY_HINTS = re.compile(
    r"verification code|verify your email|confirm your email|security code"
    r"|confirm you.?re a human|8-character code",
    re.I)

# Ranking only, never a filter: a sender list is the first thing to go stale when an ATS
# changes provider, and a stale ALLOW-list fails closed and silently.
ATS_SENDERS = re.compile(r"greenhouse\.io|greenhouse-mail|myworkday|workday\.com|lever\.co|ashbyhq\.com", re.I)


def looks_like_verification(msg: MailMessage) -> bool:
    return bool(VERIFY_HINTS.search(msg.text()))


# --- the matcher ---------------------------------------------------------------------

@dataclass
class CodeHit:
    code: str
    uid: str
    received: datetime
    sender: str = ""
    subject: str = ""
    score: int = 0
    why: str = ""


def _company_tokens(company: str) -> list[str]:
    """Distinctive words in a company name.

    Drops the suffixes and the one-letter noise, so "Cloudflare, Inc." matches an email
    that just says "Cloudflare". Common-word company names (Ramp, Sierra, Block) still
    collide with ordinary prose — which is why a company match only ADDS score and never
    gates, the same conclusion outreach_plan.md reached about alumni queries.
    """
    words = re.findall(r"[A-Za-z0-9]+", company or "")
    drop = {"inc", "llc", "ltd", "corp", "co", "the", "labs", "technologies", "group"}
    return [w.lower() for w in words if len(w) > 2 and w.lower() not in drop]


def score_message(msg: MailMessage, company: str = "", role: str = "") -> int:
    score = 0
    if ATS_SENDERS.search(msg.sender or ""):
        score += 3
    if VERIFY_HINTS.search(msg.subject or ""):
        score += 2
    body = msg.text().lower()
    toks = _company_tokens(company)
    if toks and any(t in body for t in toks):
        score += 3
    role_toks = [t for t in _company_tokens(role) if len(t) > 3]
    if role_toks and any(t in body for t in role_toks):
        score += 1
    return score


# Codes already handed out, as uid -> the dedup_key that consumed them. Held in memory on
# purpose: these expire in minutes, so a file would be a PII-shaped artifact on disk that
# is stale by the time anything could read it. A bridge restart mid-gate loses the ledger,
# and the cost of that is one code offered twice for the same application, which the form
# itself rejects.
_CONSUMED: dict[str, str] = {}


def reset_consumed() -> None:
    _CONSUMED.clear()


def find_code(reader: MailReader,
              since: datetime,
              company: str = "",
              role: str = "",
              key: str = "",
              skew: timedelta = timedelta(seconds=90),
              now: datetime | None = None) -> CodeHit | None:
    """The best verification code to type into `company`'s form, or None.

    `since` is when Parth clicked Submit. Nothing older than that is eligible, less a
    small `skew` allowance because the mail server's clock and this machine's are not the
    same clock and a code that arrives "one second before" the click is still this click's
    code.
    """
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    window = since - skew

    best: CodeHit | None = None
    for msg in reader.search(window):
        if msg.received < window:
            continue
        if not looks_like_verification(msg):
            continue                      # the hard gate — never a stray OTP
        owner = _CONSUMED.get(msg.uid)
        if owner is not None and owner != key:
            continue                      # already spent on a different application
        code = extract_code(msg.text())
        if not code:
            continue
        hit = CodeHit(code=code, uid=msg.uid, received=msg.received,
                      sender=msg.sender, subject=msg.subject,
                      score=score_message(msg, company, role))
        # Highest score wins; ties go to the newest, because the freshest code is the one
        # the form is currently waiting on.
        if best is None or (hit.score, hit.received) > (best.score, best.received):
            best = hit

    if best is not None:
        age = (now or datetime.now(timezone.utc)) - best.received
        best.why = f"code from a message {int(age.total_seconds())}s old (score {best.score})"
        if key:
            _CONSUMED[best.uid] = key
    return best
