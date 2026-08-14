#!/usr/bin/env python
"""Unit tests for mailcode.py — the emailed verification-code matcher.

Run: python bridge/test_mailcode.py

Every fixture here is INVENTED. Codes are made up, addresses use example.com, and the
company names are the ones already public in the repo's own comments. Nothing in this file
is a real message out of a real inbox — the pii_guard has caught this exact class (real
data used as a worked example) five times now.
"""
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, __file__.rsplit("test_mailcode.py", 1)[0])
import mailcode as mc

CHECKS = 0
FAILED = []


def check(cond, label):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"PASS {label}")
    else:
        FAILED.append(label)
        print(f"FAIL {label}")


T0 = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)


def msg(uid, mins, subject="", sender="", body=""):
    return mc.MailMessage(uid=uid, received=T0 + timedelta(minutes=mins),
                          subject=subject, sender=sender, body=body)


# --- extract_code --------------------------------------------------------------------
check(mc.extract_code("Your verification code is AB12CD34") == "AB12CD34", "labelled code")
check(mc.extract_code("code: 9XY8ZW7Q") == "9XY8ZW7Q", "code: form")
check(mc.extract_code("Enter this code to continue:\n\nQ7RT2M9P\n") == "Q7RT2M9P", "code alone on a line")
check(mc.extract_code("security code\n\n4KD8LS2W") == "4KD8LS2W", "security code, next line")
check(mc.extract_code("lower case is upper-cased: ab12cd34") is None,
      "an unlabelled inline token is NOT taken")
check(mc.extract_code("Your verification code is ab12cd34") == "ab12cd34",
      "case is preserved exactly — the mail says copy-and-paste, so it must not be reshaped")

# The plausibility filter: 8 chars alone is far too weak a shape.
check(mc.extract_code("Your verification code is PASSWORD") is None, "all-letters rejected")
check(mc.extract_code("Your verification code is 12345678") is None, "all-digits rejected")
check(mc.extract_code("no code here at all") is None, "no code -> None")
check(mc.extract_code("") is None, "empty text -> None")

# --- looks_like_verification (the hard gate) -----------------------------------------
check(mc.looks_like_verification(msg("a", 1, subject="Your verification code")), "verification subject passes")
check(mc.looks_like_verification(msg("b", 1, body="confirm your email to continue")), "confirm-email body passes")
check(not mc.looks_like_verification(msg("c", 1, subject="Lunch?", body="see you at 1")),
      "ordinary mail is gated out")

# THE SAFETY PROPERTY: a bank OTP must never be eligible, even though it is recent and
# carries a perfectly well-formed code.
bank = msg("bank", 1, subject="Your one-time passcode", sender="alerts.example.invalid",
           body="Your passcode is 88QT4RM2. Do not share it.")
check(not mc.looks_like_verification(bank), "a bank OTP does not pass the gate")
hit = mc.find_code(mc.FakeReader([bank]), since=T0, company="Cloudflare")
check(hit is None, "find_code refuses a bank OTP outright")

# --- find_code: the since-boundary ---------------------------------------------------
mc.reset_consumed()
old = msg("old", -30, subject="verification code", sender="mailer.greenhouse.io",
          body="Cloudflare application. Your verification code is OLD11111")
new = msg("new", 2, subject="verification code", sender="mailer.greenhouse.io",
          body="Cloudflare application. Your verification code is NEW22222")
hit = mc.find_code(mc.FakeReader([old, new]), since=T0, company="Cloudflare")
check(hit is not None and hit.code == "NEW22222", "only mail after the submit click counts")

mc.reset_consumed()
hit = mc.find_code(mc.FakeReader([old]), since=T0, company="Cloudflare")
check(hit is None, "a pre-submit code is not offered at all")

# Clock skew: a message a minute 'before' the click is still this click's code.
mc.reset_consumed()
skewed = msg("skew", -1, subject="verification code", sender="mailer.greenhouse.io",
             body="Your verification code is SKW33333")
hit = mc.find_code(mc.FakeReader([skewed]), since=T0)
check(hit is not None and hit.code == "SKW33333", "90s skew allowance is honoured")

mc.reset_consumed()
hit = mc.find_code(mc.FakeReader([skewed]), since=T0, skew=timedelta(0))
check(hit is None, "with no skew allowance the same message is excluded")

# --- find_code: two applications in flight -------------------------------------------
# The case Parth called out: several roles submitted the same afternoon.
mc.reset_consumed()
cf = msg("cf", 1, subject="verification code", sender="mailer.greenhouse.io",
         body="Verify your email for your Cloudflare application. Code: CFA11111")
af = msg("af", 3, subject="verification code", sender="mailer.greenhouse.io",
         body="Verify your email for your Affirm application. Code: AFF22222")
reader = mc.FakeReader([cf, af])
check(mc.find_code(reader, since=T0, company="Cloudflare", key="k1").code == "CFA11111",
      "company name picks its own code over a NEWER one")
check(mc.find_code(reader, since=T0, company="Affirm", key="k2").code == "AFF22222",
      "the other application gets the other code")

# --- find_code: a code is never reused across records --------------------------------
mc.reset_consumed()
only = msg("only", 1, subject="verification code", sender="mailer.greenhouse.io",
           body="Your verification code is ONE44444")
reader = mc.FakeReader([only])
first = mc.find_code(reader, since=T0, company="Cloudflare", key="job-1")
check(first is not None and first.code == "ONE44444", "first application consumes the code")
second = mc.find_code(reader, since=T0, company="Affirm", key="job-2")
check(second is None, "a consumed code is not handed to a DIFFERENT application")
again = mc.find_code(reader, since=T0, company="Cloudflare", key="job-1")
check(again is not None and again.code == "ONE44444",
      "but the SAME application may re-read its own code (a retry is not a reuse)")

# --- scoring -------------------------------------------------------------------------
mc.reset_consumed()
generic = msg("g", 5, subject="verification code", sender="mail.example.invalid",
              body="Your verification code is GEN55555")
targeted = msg("t", 1, subject="Your verification code", sender="mailer.greenhouse.io",
               body="Cloudflare needs to verify you. Your verification code is TGT66666")
hit = mc.find_code(mc.FakeReader([generic, targeted]), since=T0, company="Cloudflare")
check(hit.code == "TGT66666", "an older, better-matching message beats a newer generic one")

check(mc.score_message(targeted, "Cloudflare") > mc.score_message(generic, "Cloudflare"),
      "ATS sender + company mention scores higher")
check(mc._company_tokens("Cloudflare, Inc.") == ["cloudflare"], "company suffixes dropped")
check(mc._company_tokens("DV Trading LLC") == ["trading"], "short tokens and LLC dropped")

# Ties break to the newest.
mc.reset_consumed()
a = msg("a", 1, subject="verification code", body="Your verification code is AAA11111")
b = msg("b", 4, subject="verification code", body="Your verification code is BBB22222")
check(mc.find_code(mc.FakeReader([a, b]), since=T0).code == "BBB22222", "equal scores -> newest wins")

# --- make_reader against the real environment ----------------------------------------
# Passes whether or not Parth has configured his credentials yet: either it builds a
# reader, or it explains what is missing. What it must never do is raise something else.
try:
    check(isinstance(mc.make_reader(), mc.MailReader), "make_reader() builds a real reader")
except NotImplementedError as e:
    check("GMAIL_" in str(e), "unconfigured -> NotImplementedError naming the GMAIL_ vars")

# --- make_reader: two supported credential shapes ------------------------------------
try:
    mc.make_reader(env={})
    check(False, "make_reader() raises when nothing is configured")
except NotImplementedError as e:
    check("GMAIL_APP_PASSWORD" in str(e) and "gmail_auth.py" in str(e),
          "an unconfigured bridge is told about BOTH options, not just one")

# One code is enough.
r = mc.make_reader(env={"GMAIL_ADDRESS": "someone@example.invalid",
                        "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop"})
check(isinstance(r, mc.ImapReader), "address + app password builds an ImapReader")
check(r.app_password == "abcdefghijklmnop",
      "the spaces Google displays in an App Password are stripped, since IMAP rejects them")

# OAuth wins when both are present: it is the read-only one.
both = {"GMAIL_ADDRESS": "someone@example.invalid", "GMAIL_APP_PASSWORD": "pw",
        "GMAIL_CLIENT_ID": "id", "GMAIL_CLIENT_SECRET": "sec", "GMAIL_REFRESH_TOKEN": "tok"}
check(isinstance(mc.make_reader(env=both), mc.GmailReader),
      "with both configured the READ-ONLY credential is preferred")

check(isinstance(mc.make_reader(env={k: "x" for k in mc.OAUTH_VARS}), mc.GmailReader),
      "the OAuth trio alone builds a GmailReader")

# A half-finished setup must say so, not read as "nothing configured".
try:
    mc.make_reader(env={"GMAIL_CLIENT_ID": "id", "GMAIL_CLIENT_SECRET": "sec"})
    check(False, "a partial configuration still raises")
except NotImplementedError as e:
    check("set so far" in str(e) and "GMAIL_CLIENT_ID" in str(e),
          "a partial setup names what IS set, so the gap is obvious")


# --- ImapReader: read-only, and parsing ----------------------------------------------
class FakeIMAP:
    """Stands in for imaplib.IMAP4_SSL. Records the calls that matter."""

    def __init__(self, raw_messages):
        self.raw = raw_messages
        self.readonly = None
        self.searched = None
        self.closed = False
        self.logged_out = False

    def login(self, *_a):
        return ("OK", [b""])

    def select(self, box, readonly=False):
        self.readonly = readonly
        return ("OK", [b"1"])

    def search(self, _charset, *criteria):
        self.searched = criteria
        return ("OK", [b" ".join(str(i + 1).encode() for i in range(len(self.raw)))])

    def fetch(self, mid, _spec):
        return ("OK", [(b"1 (RFC822 {n}", self.raw[int(mid) - 1])])

    def close(self):
        self.closed = True

    def logout(self):
        self.logged_out = True


def raw_message(headers, body):
    """Assemble an RFC822 message the way a server hands one to imaplib.

    CRLF is built with chr() rather than an escape so this fixture survives being edited
    through shells and heredocs that eat backslashes — which is exactly how it broke once.
    """
    crlf = chr(13) + chr(10)
    return (crlf.join(headers) + crlf + crlf + body + crlf).encode()


RAW_HTML = raw_message([
    "Subject: Your verification code",
    "From: Greenhouse <mailer.greenhouse.io>",
    "Date: Fri, 14 Aug 2026 12:01:00 +0000",
    "Content-Type: text/html; charset=utf-8",
], "<p>Your verification code is <b>IMP77665</b></p>")

fake = FakeIMAP([RAW_HTML])
reader = mc.ImapReader("someone@example.invalid", "pw", connect=lambda: fake)
msgs = reader.search(T0)
check(fake.readonly is True,
      "THE PROPERTY: the mailbox is opened readonly (EXAMINE), so nothing is written or marked read")
check(fake.searched[0] == "SINCE", "the server-side prefilter is a SINCE search")
check(len(msgs) == 1 and msgs[0].subject == "Your verification code", "headers are parsed")
check(mc.extract_code(msgs[0].text()) == "IMP77665", "a code inside HTML is found over IMAP too")
check(msgs[0].received.tzinfo is not None, "the Date header becomes an aware datetime")
check(fake.closed and fake.logged_out, "the connection is closed and logged out")

# A message with an unparseable Date must be skipped, not crash the whole search.
BAD_DATE = raw_message(["Subject: code", "Date: not a date"],
                       "Your verification code is BAD11111")
msgs = mc.ImapReader("a", "b", connect=lambda: FakeIMAP([BAD_DATE, RAW_HTML])).search(T0)
check(len(msgs) == 1 and msgs[0].subject == "Your verification code",
      "an undateable message is skipped and the good one still comes back")


# --- GmailReader: parsing, with the HTTP layer stubbed -------------------------------
import base64


def b64(s):
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


class FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.ok = True
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class FakeHTTP:
    """Stands in for requests.Session. Records what was asked for."""

    def __init__(self, messages):
        self.messages = messages
        self.posts = 0
        self.gets = []

    def post(self, url, **kw):
        self.posts += 1
        return FakeResp({"access_token": f"at{self.posts}", "expires_in": 3600})

    def get(self, url, **kw):
        self.gets.append((url, kw))
        if url.endswith("/messages"):
            return FakeResp({"messages": [{"id": m["id"]} for m in self.messages]})
        mid = url.rsplit("/", 1)[-1]
        return FakeResp(next(m for m in self.messages if m["id"] == mid))


HTML_MSG = {
    "id": "m1",
    "internalDate": str(int(T0.timestamp() * 1000)),
    "payload": {
        "headers": [{"name": "Subject", "value": "Your verification code"},
                    {"name": "From", "value": "Greenhouse <mailer.greenhouse.io>"}],
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html",
             "body": {"data": b64("<p>Your verification code is <b>HTM12345</b></p>")}},
        ],
    },
}

http = FakeHTTP([HTML_MSG])
reader = mc.GmailReader("id", "sec", "tok", session=http)
msgs = reader.search(T0)
check(len(msgs) == 1, "search returns the listed message")
check(msgs[0].subject == "Your verification code", "subject header is read")
check("greenhouse.io" in msgs[0].sender, "from header is read")
check(msgs[0].received == T0, "internalDate becomes an aware UTC datetime")
check(mc.extract_code(msgs[0].text()) == "HTM12345",
      "a code inside HTML markup is still found (tags are stripped)")

# The token is fetched once and reused — a refresh per message would be three round trips
# for one code.
check(http.posts == 1, "one token refresh for the whole search")
reader.search(T0)
check(http.posts == 1, "a cached token is reused across searches")

# Entity-escaped markup: ATS templates split codes with zero-width entities.
ENT = {"id": "m2", "internalDate": str(int(T0.timestamp() * 1000)),
       "payload": {"headers": [{"name": "Subject", "value": "code"}],
                   "mimeType": "text/html",
                   "body": {"data": b64("verification code: <span>ENT99887</span>&#8203;")}}}
msgs = mc.GmailReader("i", "s", "t", session=FakeHTTP([ENT])).search(T0)
check(mc.extract_code(msgs[0].text()) == "ENT99887", "entities are unescaped before matching")

# A nested MIME tree (multipart/related wrapping multipart/alternative).
NESTED = {"id": "m3", "internalDate": str(int(T0.timestamp() * 1000)),
          "payload": {"headers": [{"name": "Subject", "value": "code"}],
                      "mimeType": "multipart/related",
                      "parts": [{"mimeType": "multipart/alternative",
                                 "parts": [{"mimeType": "text/plain",
                                            "body": {"data": b64("Your code is NST55443")}}]}]}}
msgs = mc.GmailReader("i", "s", "t", session=FakeHTTP([NESTED])).search(T0)
check(mc.extract_code(msgs[0].text()) == "NST55443", "a code nested two levels deep is found")

# An empty mailbox must not explode on the missing `messages` key.
check(mc.GmailReader("i", "s", "t", session=FakeHTTP([])).search(T0) == [],
      "an empty listing returns [] rather than raising")

# --- THE REAL EMAIL SHAPE (measured against a live message) --------------------------
# Wording and shape taken from a live ATS message; the code is invented, but it keeps
# the property that broke the first implementation: eight LETTERS, no digit, mixed case.
REAL_SUBJECT = "Security code for your application to Freeform"
REAL_SENDER = "Greenhouse <no-reply.us.greenhouse-mail.io>"
REAL_BODY = (
    "Greenhouse Recruiting\n"
    "Hi Parth,\n"
    "Copy and paste this code into the security code field on your application:\n"
    "\n"
    "QrtbVwxM\n"
    "After you enter the code, resubmit your application\n"
)

check(mc.extract_code(REAL_BODY) == "QrtbVwxM",
      "THE REGRESSION: an all-letters mixed-case code is found (the first _plausible "
      "required a digit and would have rejected the real one)")
check(mc._plausible("QrtbVwxM"), "internal case-switching is enough to look generated")
check(not mc._plausible("Security"), "a capitalised English word is NOT a code")
check(not mc._plausible("SECURITY"), "nor an all-caps one")
check(not mc._plausible("12345678"), "nor all digits")

real = mc.MailMessage(uid="real", received=T0 + timedelta(minutes=1),
                      subject=REAL_SUBJECT, sender=REAL_SENDER, body=REAL_BODY)
check(mc.looks_like_verification(real), "the real subject passes the hard gate")
check(mc.ATS_SENDERS.search(real.sender) is not None,
      "us.greenhouse-mail.io is recognised as an ATS sender")

mc.reset_consumed()
hit = mc.find_code(mc.FakeReader([real]), since=T0, company="Freeform", key="freeform-1")
check(hit is not None and hit.code == "QrtbVwxM", "end to end: the real message yields its code")
check(hit.score >= 6, "ATS sender + company name in the subject both score")

# The gate must still refuse the job mail that surrounds it. 'application' appears in this
# very email, which is exactly why it cannot be a hint on its own.
mc.reset_consumed()
rejection = mc.MailMessage(uid="rej", received=T0 + timedelta(minutes=2),
                           subject="Update on your application",
                           sender="no-reply.us.greenhouse-mail.io",
                           body="Thank you for your application. Reference: XK92MTQ4")
check(not mc.looks_like_verification(rejection),
      "MEASURED: a rejection carrying a code-shaped reference does NOT pass the gate")
check(mc.find_code(mc.FakeReader([rejection]), since=T0, company="Freeform") is None,
      "and so its reference number is never offered as a code")

print()
print(f"mailcode: {CHECKS - len(FAILED)}/{CHECKS} checks passed")
if FAILED:
    for f in FAILED:
        print("  FAILED:", f)
    sys.exit(1)
