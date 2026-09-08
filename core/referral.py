#!/usr/bin/env python3
"""
Referral flow, phase 1 — paste a person, get the reqs worth asking them about.

`docs/referral_flow_plan.md` is the spec. The short version: `core/contacts.py` went
job -> people (take a queue row, search for contacts). This goes the other way. Parth finds
the person himself, through his own network, and pastes the profile in; this module answers
"which of my open reqs at that company should I put in front of them, and when do I stop
waiting for their referral and just apply".

WHY THE INVERSION MATTERS TO THE CODE
-------------------------------------
Nothing here searches. There is no provider, no query ladder, no API key, no title filter at
request time — the person is already known, so every one of those exists to solve a problem
this path does not have. What IS reused from phase 1 is the part that was never about
searching: `normalize_profile_url` (dedupe identity), `employer_is` (exact company matching,
which is what stops "Writer" matching a freelance writer), `parse_profile_card`, and the
`outreach.json` store under HOME.

THE FOUR TIERS ARE THE WHOLE FEATURE
------------------------------------
A paste is only useful if it can name a real req. Company resolution runs against LOCAL
FILES ONLY (Parth's call 2026-08-18 — no live re-scrape of 100+ boards because somebody
pasted a profile), in this order:

    1  out/queue.json, out/dream.json   open reqs       -> the good case, draft the ask
    2  applied.json                     already applied -> a different ask entirely
    3  companies.yaml                   tracked, nothing open -> offer ONE live pull
    4  nothing                          unknown         -> offer to add the company

Tier 2 is the COMMON case, not the fallback: most companies reachable from these files are
ones he has already applied to, and "please add a referral to my existing application" is a
smaller favour than "please refer me", so it is a better ask anyway.

`resolve_paste()` records the tier on every paste. That is not telemetry for its own sake:
coverage measured 2026-08-18 was roughly two fifths of the tracked companies, so if tiers 3
and 4 turn out to dominate real use, the local-only constraint is the thing to revisit, and
without the log there is no way to know.

PII
---
Everything in this module is SOMEBODY ELSE'S personal data — the only such category in the
project. The contacts live in the store, never in the checkout,
and every fixture below is invented (`example-` profile slugs, per the reservation
`scripts/pii_guard.py` documents). A first draft of `core/contacts.py`'s selftest was
written with real people lifted from live search results; that is precisely the leak this
feature creates.

Pure functions with `--selftest`; nothing here touches the network except the opt-in tier-3
pull, which is injected.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone

from core import paths
from server.store import get_store, set_store, use_temp_store
from core.contacts import (
    _norm,
    country_from_location,
    country_of,
    employer_is,
    normalize_profile_url,
    parse_profile_card,
)

# Tier names, used as ints in the record and as words in every surface. Kept as constants
# because the number IS the logged coverage measurement, and a stray literal in a card
# renderer is how that quietly stops matching what the resolver meant.
TIER_QUEUE, TIER_APPLIED, TIER_TRACKED, TIER_UNKNOWN = 1, 2, 3, 4
TIER_NAMES = {TIER_QUEUE: "open reqs", TIER_APPLIED: "already applied",
              TIER_TRACKED: "tracked, nothing open", TIER_UNKNOWN: "unknown company"}

# How Parth knows the person. Drives the opening line of the draft far more than anything
# else does, so it is stored rather than inferred again at draft time.
TIES = ("alum", "ex-colleague", "mutual", "cold")

# Show two, keep four. A referral ask naming five reqs reads as a mass send, but the extra
# rows cost nothing to carry and are what a "next req" button pages through.
REQ_CAP = 4
REQ_SHOW = 2

# Workday's referral link CREATES the application, so self-applying first can downgrade or
# block it. Every other board attaches a referral to an existing application cleanly.
WAIT_ATS = {"workday"}
WAIT_DAYS = 4
# A posting this old is being decided now; being applicant #400 instead of #40 costs more
# than the referral gains, so the wait is capped regardless of who replied.
STALE_DAYS = 10

# Lines a LinkedIn page paste carries that are chrome, not profile. Matched against the
# whole line, case-insensitively — these are the ones that otherwise land in `headline` or
# `location` and get quoted back at the person in a draft.
_CHROME = re.compile(
    r"(?i)^\s*(contact info|show all.*|see (more|all).*|message|connect|follow(ing)?|more"
    r"|pending|open to work|\d[\d,+]*\s*(connections?|followers?|mutual connections?)"
    r"|talks about.*|\W*)\s*$")

# Section headings in a pasted profile. Everything before the first one is the head block
# (name / headline / location); after it, the prose that names employers and schools.
_SECTION = re.compile(
    r"(?i)^\s*#{0,3}\s*(experience|education|about|licenses|skills|activity|interests"
    r"|recommendations|honors|projects|publications|volunteering)\s*:?\s*$")

_PROFILE_URL = re.compile(
    r"(?i)\b((?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[^\s\"'<>)\],]+)")

# "Quant Analyst at Northwind", "Data Scientist @ Northwind".
_HEADLINE_EMPLOYER = re.compile(r"(?i)\s(?:at|@)\s+([^|·•,\n]{2,60})$")

# Words that make a title token useless for measuring overlap with a headline. Same idea as
# contacts._STOP, kept separate because this list is about TITLES vs HEADLINES, not queries.
_TITLE_STOP = {"senior", "sr", "staff", "principal", "lead", "junior", "jr", "new", "grad",
               "graduate", "engineer", "engineering", "developer", "software", "the", "and",
               "for", "with", "of", "a", "an", "i", "ii", "iii", "iv", "l2", "l3", "l4",
               "l5", "us", "usa", "remote", "hybrid", "onsite", "full", "time", "role",
               "position", "team", "intern", "early", "career", "analyst", "associate"}


@dataclass
class Person:
    """One pasted profile, parsed. Deliberately NOT `contacts.Contact`: that dataclass is
    shaped by what a search result can tell you (signals, a score), and a pasted profile has
    neither, while carrying things a search result never does — a school, Parth's own notes,
    how he knows them."""
    name: str
    headline: str = ""
    employer: str = ""
    school: str = ""
    location: str = ""
    profile_url: str = ""
    country: str = ""
    about: str = ""
    source: str = "paste"

    def __post_init__(self):
        if not self.country:
            self.country = country_from_location(self.location) or country_of(self.profile_url)

    def key(self) -> str:
        """Dedupe identity, same rule as `contacts.Contact.key`. Inviting one person twice is
        the failure that costs a relationship and, repeated, an account."""
        return normalize_profile_url(self.profile_url)


# ------------------------------------------------------------------ parsing ---
def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in str(text or "").replace("\r", "").split("\n")]


def extract_profile_url(text: str) -> str:
    """The first LinkedIn profile URL anywhere in the paste, as a normalised https URL.

    Parth pastes whatever the page gave him, so the URL arrives in the body, on its own
    line, or wrapped in tracking parameters. It is the ONE field that must be recovered
    reliably: it is the dedupe key, and a record without it cannot answer "have I already
    approached this person?", which is the question the store exists to answer.
    """
    m = _PROFILE_URL.search(str(text or ""))
    if not m:
        return ""
    return "https://" + normalize_profile_url(m.group(1))


def _looks_like_name(line: str) -> bool:
    """A plausible human name line: two to five words, no sentence punctuation, not chrome.

    Deliberately loose. Getting this wrong costs a mislabelled draft that Parth reads before
    sending; being strict enough to reject "Wei Chen, Ph.D." or "Ana de la Cruz" costs the
    whole paste.
    """
    s = line.strip()
    if not s or _CHROME.match(s) or _SECTION.match(s):
        return False
    low = s.lower()
    if "linkedin.com" in low or "@" in s or "http" in low:
        return False
    words = s.split()
    if not (1 < len(words) <= 5):
        return False
    if re.search(r"[.!?]$", s) and not re.search(r"(?i)(ph\.?d\.?|jr\.|sr\.|m\.d\.)$", s):
        return False
    return " at " not in low and "|" not in s


def _section_body(lines: list[str], name: str, limit: int = 12) -> list[str]:
    """The lines under one section heading, up to the next heading. [] when absent."""
    out: list[str] = []
    taking = False
    for ln in lines:
        if _SECTION.match(ln):
            if taking:
                break
            taking = bool(re.match(rf"(?i)^\s*#{{0,3}}\s*{name}\s*:?\s*$", ln))
            continue
        if taking and ln and not _CHROME.match(ln):
            out.append(ln)
            if len(out) >= limit:
                break
    return out


def _first_entity(lines: list[str], skip: re.Pattern, cap: int) -> str:
    """The first line in a section that names an organisation rather than a date or a degree.

    LinkedIn renders the most recent entry first and puts the organisation on its own line,
    often suffixed with a type: `Northwind · Full-time`. Taking the FIRST such line is right
    for the same reason the headline is: it is the current one.
    """
    for ln in lines:
        if skip.search(ln):
            continue
        cand = re.split(r"\s*[·•|]\s*", ln)[0].strip()
        if cand and len(cand) < cap:
            return cand
    return ""


_EXP_SKIP = re.compile(r"(?i)\b(present|\d{4}\s*[-–]|\d+\s*(yrs?|mos?)\b"
                       r"|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b")
_EDU_SKIP = re.compile(r"(?i)\b(\d{4}|bachelor|master|b\.?e\b|b\.?s\b|m\.?s\b|ph\.?d"
                       r"|degree|major|honou?rs)\b")


def employer_from_headline(headline: str) -> str:
    """`"Quant Analyst at Northwind"` -> `"Northwind"`. "" when the headline names no company.

    A headline is the most reliable statement of a current employer on a profile — it is
    what `contacts.title_has_company` turns on — but only when written in the `X at Y` form.
    Plenty are not ("Building things", "Northwind | Payments"), which is why the Experience
    section is consulted too, and why a paste with neither still resolves from what Parth
    typed in his notes.
    """
    m = _HEADLINE_EMPLOYER.search(str(headline or "").strip())
    if not m:
        return ""
    emp = m.group(1).strip().rstrip(".")
    # "at scale", "at the intersection of X and Y" — prepositional prose, not an employer.
    if not emp or emp.lower().split()[0] in {"scale", "the", "a", "an", "my", "our", "work",
                                             "heart", "speed", "night", "least"}:
        return ""
    return emp


def parse_pasted_profile(text: str, source: str = "paste") -> Person | None:
    """A blob of pasted profile text -> a `Person`. None when there is no person in it.

    Three input shapes, all of which really arrive:

      * CARD    — `# Name` / employer / location, what a scraping search provider returns.
                  Handed straight to `contacts.parse_profile_card`; that shape is already
                  parsed correctly in phase 1, so it is not re-implemented here.
      * PAGE    — a select-all-and-copy of the LinkedIn profile page: name, headline,
                  location, chrome, then Experience / Education sections.
      * MINIMAL — a URL, possibly with a name beside it. What arrives from a phone.

    Never raises and never guesses a company: an unresolvable employer yields a Person with
    `employer=""`, which lands the paste in tier 4 with an honest message. That is a far
    better outcome than a confident draft naming the wrong firm to a real person.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    url = extract_profile_url(raw)

    card = parse_profile_card(raw)
    if card:
        return Person(name=card["name"], headline=card["about"] or card["employer"],
                      employer=card["employer"], location=card["location"],
                      school=card["education"], profile_url=url, about=card["about"],
                      source=source)

    lines = _lines(raw)
    # The head block: everything before the first section heading, minus chrome and URLs.
    head: list[str] = []
    for ln in lines:
        if _SECTION.match(ln):
            break
        if not ln or _CHROME.match(ln) or "linkedin.com" in ln.lower():
            continue
        head.append(ln)

    name = head[0] if head and _looks_like_name(head[0]) else ""
    rest = head[1:] if name else head
    headline = rest[0] if rest else ""
    # A location line is the one with commas and no verb: "Fairhaven, Vermont, United States".
    # Checked against the SECOND line too, because a profile with no headline renders the
    # location there, and mistaking one for the other quotes a city back at somebody as
    # though it were their job.
    location = ""
    for i, cand in enumerate(rest[:3]):
        if not re.match(r"^[A-Za-z .'-]+(,\s*[A-Za-z .'-]+){1,3}$", cand):
            continue
        if " at " in cand.lower():
            continue
        location = cand
        if i == 0:
            headline = ""
        break

    if not name:
        # MINIMAL: no name line, but a URL is enough to record the person. The slug is a
        # readable stand-in until Parth types the real name, and is never used in a draft.
        if not url:
            return None
        slug = url.rsplit("/in/", 1)[-1].strip("/")
        name = re.sub(r"-[a-z0-9]*\d[a-z0-9]*$", "", slug).replace("-", " ").title()

    employer = (employer_from_headline(headline)
                or _first_entity(_section_body(lines, "experience"), _EXP_SKIP, 60))
    school = _first_entity(_section_body(lines, "education"), _EDU_SKIP, 80)
    about = " ".join(_section_body(lines, "about", limit=6))[:400]
    return Person(name=name.strip(), headline=headline.strip(), employer=employer.strip(),
                  school=school.strip(), location=location.strip(), profile_url=url,
                  about=about, source=source)


# ------------------------------------------------------- local job sources ---
def _row_dicts(rows) -> list[dict]:
    """The dict rows out of a stored list; [] for anything malformed.

    Every caller here is answering "what do I know locally about this company", and the
    honest answer when nothing is stored is "nothing" — the tier ladder then falls through
    to the offer to pull or to add the company, which is a working outcome. Raising instead
    would turn a missed overnight scrape into a dead paste.
    """
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def open_reqs() -> list[dict]:
    """Every open role from the overnight scrape: the auto-apply queue AND the dream pile.

    The dream pile is included on purpose, and it is most of the point. `core/scraper.py` writes
    "the auto-apply loop skips these — apply by hand (referral first)" into the digest and
    then abandons those roles at exactly the moment a referral is the thing that matters. A
    dream row is the single best thing to put in front of an alum.
    """
    st = get_store()
    return _row_dicts(st.get_queue("queue")) + _row_dicts(st.get_queue("dream"))


def applied_rows() -> list[dict]:
    return _row_dicts(get_store().get_applied())


def tracked_companies() -> list[dict]:
    """The tracked company rows: {name, ats, slug, tier}. [] when nothing is stored."""
    try:
        return _row_dicts(get_store().get_companies())
    except Exception as e:                       # an unreadable store is "nothing tracked"
        print(f"[referral] could not read the companies: {e}", file=sys.stderr)
        return []


def company_matches(employer: str, rows: list[dict], key: str = "company") -> list[dict]:
    """Rows whose `key` names this employer, matched EXACTLY (with a legal-suffix allowance).

    `employer_is` from phase 1, reused rather than re-implemented, and the reason is
    measured: a substring test makes single-word company names unusable. "Writer" matched a
    freelance content writer, "Harvey" a surname, "Sierra" a role at Cisco (2026-08-04). The
    trap is worse here than it was there, because a false match does not merely rank
    somebody badly — it puts another company's req into a message to a real person.

    Matched in BOTH directions: the pasted employer may carry the legal suffix the board
    omits ("Northwind Markets, Inc." vs "Northwind"), or the other way round.
    """
    emp = str(employer or "").strip()
    if not emp:
        return []
    return [r for r in rows
            if employer_is(str(r.get(key) or ""), emp) or employer_is(emp, str(r.get(key) or ""))]


# ------------------------------------------------------------ req ranking ---
def field_order() -> list[str]:
    """Resume tracks in preference order, strongest first, read from profile.json.

    `default_resume_tag` is the strongest field by definition — it is what gets sent when
    nothing matches — and `resume_tracks` supplies the rest in the order the user wrote them.
    Read at runtime rather than hardcoded, for the same reason `default_affinities()` is:
    these are the user's own choices, so a list in this file would be both personal data in a
    shareable repo and wrong for anybody else.
    """
    try:
        prof = get_store().get_profile()
    except (OSError, json.JSONDecodeError):
        return []
    tags = [str(t.get("tag") or "") for t in (prof.get("resume_tracks") or []) if t.get("tag")]
    top = str(prof.get("default_resume_tag") or "").strip()
    if top and top in tags:
        tags.remove(top)
        tags.insert(0, top)
    return tags


def _posted_date(req: dict) -> date | None:
    raw = str(req.get("posted") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def title_overlap(title: str, headline: str) -> int:
    """How many meaningful words the req title and the contact's headline share.

    The lever the plan asks for: an alum in payments should be pointed at the payments req,
    not at whichever one was scraped first. Stop words are stripped from both sides, so
    "Senior Software Engineer" against "Software Engineer at Northwind" scores 0 rather than
    2 — otherwise every req ties on those words and the ranking is noise.
    """
    def toks(s: str) -> set[str]:
        return {t for t in _norm(s).split() if len(t) > 2 and t not in _TITLE_STOP}
    return len(toks(title) & toks(headline))


def rank_reqs(reqs: list[dict], person: Person | None = None, limit: int = REQ_CAP,
              order: list[str] | None = None, today: date | None = None) -> list[dict]:
    """The best reqs to put in front of this person, best first, capped.

    Cheap and entirely local — no model call, because there is nothing here a model knows
    that the queue row does not already state. Ordered by field preference, then headline
    overlap, then freshness. Rows the scraper's own filters rejected cannot appear (they
    never reach queue.json), so the only filter left is a row with no field at all, which
    means the classifier could not place it.
    """
    order = field_order() if order is None else order
    today = today or date.today()
    rank_of = {tag: i for i, tag in enumerate(order)}
    headline = f"{person.headline} {person.about}" if person else ""

    def key(r: dict):
        # An unknown or absent field sorts last, never first: an unplaceable row is the one
        # least worth spending somebody's goodwill on.
        fr = rank_of.get(str(r.get("field") or ""), len(order) + 1)
        overlap = title_overlap(str(r.get("title") or ""), headline)
        posted = _posted_date(r)
        age = (today - posted).days if posted else 9999
        return (fr, -overlap, age, str(r.get("title") or ""))

    keep = [r for r in reqs if r.get("field") or not order]
    return sorted(keep, key=key)[:max(0, limit)]


REQ_ID_FROM_URL = [
    # Greenhouse: .../jobs/4567890  |  ?gh_jid=4567890
    re.compile(r"(?i)(?:/jobs/|[?&]gh_jid=)(\d{4,})"),
    # Lever and Ashby both end in a UUID.
    re.compile(r"(?i)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"),
    # Workday: .../job/<location>/<title>_R-12345
    re.compile(r"(?i)_(R-?\d{4,})\b"),
]


def req_id(row: dict) -> str:
    """The requisition id a referral form will ask for.

    `ats_job_id` when the scraper recorded one — that is the authoritative value. Rows from
    `applied.json` have no such field (that ledger predates the scraper's schema), so it is
    recovered from the URL, which is the only thing those rows carry. "" when neither works,
    and an empty req id must simply be omitted from the draft rather than guessed: a wrong
    id sends the person hunting for a role that does not exist.
    """
    jid = str(row.get("ats_job_id") or "").strip()
    if jid:
        return jid
    url = str(row.get("url") or "")
    for pat in REQ_ID_FROM_URL:
        m = pat.search(url)
        if m:
            return m.group(1)
    return ""


def ats_of(row: dict) -> str:
    """The board a row is on, from the field or, for older rows, from the URL host."""
    ats = str(row.get("ats") or "").strip().lower()
    if ats:
        return ats
    host = str(row.get("url") or "").lower()
    for name, needle in (("greenhouse", "greenhouse.io"), ("lever", "lever.co"),
                         ("ashby", "ashbyhq.com"), ("workday", "myworkdayjobs.com")):
        if needle in host:
            return name
    return ""


def apply_after(row: dict, replied: bool = False, sent: date | None = None,
                today: date | None = None) -> str:
    """The ISO date to stop waiting for a referral and apply anyway. Today = "apply now".

    Settled 2026-08-18: the two tracks run in PARALLEL. A referral arriving after the
    application keeps nearly all of its value at almost every company, while being applicant
    #400 instead of #40 does not, so waiting is close to always wrong. The single exception
    is a Workday req where a contact has already replied, because there the referral link
    itself creates the application, and self-applying first can downgrade or block it.

    Three rules, and their ordering is what makes them consistent:

      * no reply yet      -> today. An accepted invite is not a pending referral, and this is
                             what stops a silent contact freezing an application forever.
      * not Workday       -> today. The referral attaches after the fact.
      * Workday + replied -> `sent` + 4 days, never past `posted` + 10 days, never in the
                             past. The plan writes this as `min(sent + 4d, now)`; the 10-day
                             cap is its "posting older than ~10 days -> now regardless" rule
                             doing the same job more precisely, since a wait that runs past
                             the point where the req is being decided is not a wait, it is a
                             missed application.
    """
    today = today or date.today()
    if not replied or ats_of(row) not in WAIT_ATS:
        return today.isoformat()
    cand = (sent or today) + timedelta(days=WAIT_DAYS)
    posted = _posted_date(row)
    if posted:
        cand = min(cand, posted + timedelta(days=STALE_DAYS))
    return max(cand, today).isoformat()


def resume_for(field: str) -> dict:
    """{tag, path} — the resume track that matches a req's field. {} when unresolvable.

    The `yes_pack` message attaches a resume, and which of the four it should be is a
    property of the REQ, not of the contact: an alum asking for a quant req needs the quant
    PDF regardless of how Parth knows them. Resolved here rather than at draft time so the
    Telegram card can name the file even when drafting is switched off.

    Falls back to `default_resume_tag`, which is what the whole track system does when
    nothing matches (`core/profile.py:pick_resume`), so this cannot return a track the user
    has not configured.
    """
    try:
        prof = get_store().get_profile()
    except (OSError, json.JSONDecodeError):
        return {}
    tracks = [t for t in (prof.get("resume_tracks") or []) if isinstance(t, dict)]
    tag = str(field or "").strip().lower()
    hit = next((t for t in tracks if str(t.get("tag") or "").lower() == tag), None)
    if hit is None:
        default = str(prof.get("default_resume_tag") or "").lower()
        hit = next((t for t in tracks if str(t.get("tag") or "").lower() == default), None)
    if hit is None:
        return {}
    return {"tag": str(hit.get("tag") or ""), "path": str(hit.get("local_path") or "")}


def as_req(row: dict, replied: bool = False, sent: date | None = None,
           today: date | None = None) -> dict:
    """One local job row -> the compact req block stored on the contact record."""
    return {
        "dedup_key": str(row.get("dedup_key") or ""),
        "ats_job_id": req_id(row),
        "url": str(row.get("url") or ""),
        "title": str(row.get("title") or ""),
        "location": str(row.get("location") or ""),
        "field": str(row.get("field") or ""),
        "ats": ats_of(row),
        "posted": str(row.get("posted") or ""),
        "apply_after": apply_after(row, replied=replied, sent=sent, today=today),
        "resume": resume_for(str(row.get("field") or "")),
    }


# ----------------------------------------------------------- the resolver ---
def default_affinities() -> tuple[str, str]:
    """(school, employer) from profile.json — re-exported from phase 1 so callers do not
    have to know which module owns it."""
    from core.contacts import default_affinities as _da
    return _da()


def infer_tie(person: Person, school: str = "", employer: str = "") -> str:
    """How Parth knows this person, guessed from what the profile itself says.

    Only ever a DEFAULT: he types the real answer in his notes, and `resolve_paste` lets him
    override it. Worth guessing anyway, because it is right most of the time and it is the
    single biggest influence on the opening line of the draft — a note to an ex-colleague
    that opens on the role instead of the shared team reads like a template.
    """
    if not school or not employer:
        s, e = default_affinities()
        school, employer = school or s, employer or e
    blob = _norm(f"{person.headline} {person.school} {person.about} {person.employer}")
    if employer and _norm(employer).strip() and _norm(employer) in blob:
        return "ex-colleague"
    if school and _norm(school).strip() and _norm(school) in blob:
        return "alum"
    return "cold"


def pull_company(name: str, timeout: float = 180.0) -> list[dict]:
    """Tier 3's opt-in live pull: ONE company's board, right now, via the scraper.

    Deliberately a subprocess rather than an import. `core.scraper` pulls in yaml, requests
    and a whole filtering pipeline at import time, and this module has to stay importable
    (and selftestable) inside the bridge without any of that. It also means the scraper's
    own `--only` guard — which refuses to overwrite the shared out/ files — is what enforces
    that a paste can never clobber the overnight queue.

    Never raises: a failed pull is reported as "nothing found", which lands the paste in the
    same place it would have been without the pull.
    """
    cmd = [sys.executable, "-m", "core.scraper", "--only", name, "--json"]
    try:
        proc = subprocess.run(cmd, cwd=str(paths.REPO_ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"[referral] live pull for {name!r} failed: {e}", file=sys.stderr)
        return []
    if proc.returncode != 0:
        print(f"[referral] scraper exited {proc.returncode}: {(proc.stderr or '')[-300:]}",
              file=sys.stderr)
        return []
    text = (proc.stdout or "").strip()
    a, b = text.find("["), text.rfind("]")
    if a == -1 or b <= a:
        return []
    try:
        rows = json.loads(text[a:b + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict)]


def resolve_company(person: Person, live: bool = False, puller=pull_company) -> dict:
    """The four-tier ladder. Returns {tier, company, reqs, applied, ats, slug}.

    Tiers 1 and 2 are not exclusive, and the overlap is the interesting case: a company with
    an open req AND a past application means the ask is "refer me to this new one" while the
    draft still has to acknowledge the earlier application rather than pretend it did not
    happen. So `applied` rows are returned whatever the tier.

    `live` is the opt-in tier-3 pull and defaults OFF. It is per-paste and never automatic:
    the local-only rule exists so that pasting a profile cannot fire a hundred board
    requests, and an "always try harder" default is how that rule dies quietly.
    """
    emp = person.employer
    applied = company_matches(emp, applied_rows())
    reqs = company_matches(emp, open_reqs())
    tracked = company_matches(emp, tracked_companies(), key="name")
    entry = tracked[0] if tracked else {}
    base = {"company": emp, "applied": applied,
            "ats": str(entry.get("ats") or ""), "slug": str(entry.get("slug") or "")}
    if reqs:
        return {**base, "tier": TIER_QUEUE, "reqs": reqs}
    if applied:
        return {**base, "tier": TIER_APPLIED, "reqs": []}
    if tracked:
        pulled = company_matches(emp, puller(str(entry.get("name") or emp))) if live else []
        if pulled:
            # A live pull that finds something IS tier 1 — the only difference from the
            # overnight path is when the fetch happened, and recording it as tier 3 would
            # make the coverage measurement read as a miss when it was a hit.
            return {**base, "tier": TIER_QUEUE, "reqs": pulled, "pulled": True}
        return {**base, "tier": TIER_TRACKED, "reqs": [], "pulled": live}
    return {**base, "tier": TIER_UNKNOWN, "reqs": []}


# --------------------------------------------------------------- the store ---
def load_store() -> dict:
    """The whole outreach store, keyed by normalised profile URL. {} when absent.

    Shared with `core/contacts.py` — same rows, same key rule — so a person found by the
    phase-1 search and the same person pasted in here are one row, not two. That is the
    entire point of keying on the profile URL.
    """
    return get_store().get_outreach()


def save_store(store: dict) -> None:
    get_store().set_outreach(store)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def upsert(record: dict) -> tuple[dict, bool]:
    """Write a paste into the store. Returns (stored_record, is_new).

    An EXISTING row is updated, never replaced, and the fields it defends are the ones that
    took a human to produce: `state`, `history`, and any draft already edited. Re-pasting
    somebody invited last week must refresh their reqs without resetting them to `found` —
    that would re-offer an invite to a person already invited, which is the one mistake here
    that costs a real relationship and, repeated, an account.
    """
    key = str(record.get("id") or "")
    if not key:
        return record, False
    store = load_store()
    old = store.get(key)
    if old is None:
        row = {**record, "state": record.get("state") or "found",
               "history": [{"state": "found", "ts": _now()}]}
        store[key] = row
        save_store(store)
        return row, True
    merged = dict(old)
    for k, v in record.items():
        if k in ("state", "history"):
            continue
        if k == "drafts":
            # Keep what is already there: a re-paste must not silently discard a draft Parth
            # has edited. New variants are added, existing ones left alone.
            merged["drafts"] = {**(v or {}), **(old.get("drafts") or {})}
            continue
        if v not in ("", [], {}, None) or k not in merged:
            merged[k] = v
    merged["seen"] = _now()
    store[key] = merged
    save_store(store)
    return merged, False


def set_state(key: str, state: str) -> dict | None:
    """Move a contact along the state machine, appending to its history. None if unknown.

    `accepted` is the only transition Parth reports by hand — the pipeline cannot see his
    LinkedIn — which is why every surface needs this, and why the history is kept: the bump
    sweep asks "invited, and how long ago", and that question has no answer without it.
    """
    store = load_store()
    row = store.get(key)
    if row is None:
        return None
    row["state"] = state
    row.setdefault("history", []).append({"state": state, "ts": _now()})
    save_store(store)
    return row



# ------------------------------------------------------- surfaces (P3+) ---
# A Telegram callback_data field is capped at 64 BYTES, and the store's key is a whole
# profile URL — `linkedin.com/in/firstname-lastname-a1b2c3` is already 40 of them before the
# namespace and the action are prefixed. So buttons carry a short token instead, and the
# store is searched for it. The store key stays the URL because `core/contacts.py` uses the
# same file with the same rule, and two modules keying one file differently is how a person
# ends up in it twice.
def short_id(key: str) -> str:
    """A stable 8-hex token for one contact, for anywhere the full key will not fit.

    FNV-1a, the same hash `docs/referral_flow_plan.md` names in the data model, chosen there
    for the same reason it is right here: it is stable across runs, unlike a sequence
    number, so a button pressed tomorrow still means the person it meant today.
    """
    h = 0x811C9DC5
    for b in normalize_profile_url(key).encode("utf-8"):
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"


def find_by_short(token: str, store: dict | None = None) -> dict | None:
    """The contact a button token refers to, or None if it has since been removed."""
    tok = str(token or "").strip().lower()
    if not tok:
        return None
    for key, row in (load_store() if store is None else store).items():
        if short_id(key) == tok:
            return row
    return None


# How long to wait before ASKING whether an invite was accepted, and before offering the
# bump. `accepted` is the only transition Parth reports by hand — the pipeline cannot see
# his LinkedIn — so without a sweep the whole state machine stalls at `invited` forever and
# the follow-up that makes outreach work never happens.
#
# The bump window is the plan's 5-7 days. Six rather than five because the ask itself is
# often sent a day after the invite is accepted, and a bump that arrives before the person
# has had a working day to answer reads as pressure.
NUDGE_ACCEPT_DAYS = 3
NUDGE_BUMP_DAYS = 6
# States a nudge would be pointless or rude in. `replied` has done its job, and the plan is
# explicit that after ONE bump the contact is dropped forever rather than chased.
NUDGE_DONE_STATES = {"replied", "no_reply", "skipped", "bumped"}


def _state_ts(row: dict, state: str) -> datetime | None:
    """When this contact most recently entered `state`, from its history. None if never."""
    best = None
    for h in reversed(row.get("history") or []):
        if h.get("state") != state:
            continue
        try:
            ts = datetime.fromisoformat(str(h.get("ts") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        best = ts if best is None or ts > best else best
        break
    return best


def due_nudges(store: dict | None = None, now: datetime | None = None) -> list[tuple[str, str]]:
    """[(contact key, kind)] for every follow-up that is due. Kinds: `accepted`, `bump`.

    Pure, so the sweep that sends these can be tested without a clock or a network. Each
    kind fires at most ONCE per contact: `nudged` records what has already been asked, and a
    follow-up loop that re-asks every hour is a loop Parth mutes, which costs him the
    feature entirely.
    """
    now = now or datetime.now(timezone.utc)
    out: list[tuple[str, str]] = []
    for key, row in (load_store() if store is None else store).items():
        if not isinstance(row, dict):
            continue
        state = str(row.get("state") or "")
        if state in NUDGE_DONE_STATES:
            continue
        asked = row.get("nudged") or {}
        if state == "invited" and "accepted" not in asked:
            ts = _state_ts(row, "invited")
            if ts and (now - ts) >= timedelta(days=NUDGE_ACCEPT_DAYS):
                out.append((key, "accepted"))
        elif state in ("accepted", "messaged") and "bump" not in asked:
            ts = _state_ts(row, state)
            if ts and (now - ts) >= timedelta(days=NUDGE_BUMP_DAYS):
                out.append((key, "bump"))
    return out


def mark_nudged(key: str, kind: str) -> dict | None:
    """Record that a follow-up has been asked, so the sweep never asks it twice."""
    store = load_store()
    row = store.get(key)
    if row is None:
        return None
    row.setdefault("nudged", {})[kind] = _now()
    save_store(store)
    return row


# -------------------------------------------------------------- the paste ---
def resolve_paste(text: str, notes: str = "", tie: str = "", live: bool = False,
                  source: str = "paste", puller=pull_company, today: date | None = None,
                  store: bool = True) -> dict | None:
    """The whole of phase 1: pasted text in, a stored contact record out. None if unparseable.

    The record is the contract every later phase reads — P2 drafts from it, P3 renders a
    Telegram card from it, P5 tables it — so its shape is fixed here and documented in
    `docs/referral_flow_plan.md`. `store=False` is for a dry run that must not touch
    somebody's outreach history.
    """
    person = parse_pasted_profile(text, source=source)
    if not person:
        return None
    res = resolve_company(person, live=live, puller=puller)
    ranked = rank_reqs(res["reqs"], person, today=today)
    record = {
        "id": person.key(),
        **{k: v for k, v in asdict(person).items() if k != "source"},
        "source": source,
        "notes": str(notes or "").strip(),
        "tie": tie if tie in TIES else infer_tie(person),
        "tier": res["tier"],
        "company": res["company"],
        "ats": res["ats"], "slug": res["slug"],
        "reqs": [as_req(r, today=today) for r in ranked],
        "applied": [{"title": str(r.get("title") or ""), "url": str(r.get("url") or ""),
                     "applied": str(r.get("applied") or ""), "ats_job_id": req_id(r)}
                    for r in res["applied"]],
        "show": REQ_SHOW,
        "pulled": bool(res.get("pulled")),
        "drafts": {},
    }
    if not store or not record["id"]:
        # No profile URL means no dedupe key, so it cannot be stored without risking a
        # second row for the same person on the next paste. Returned anyway: the resolved
        # reqs are still useful, and the surface can ask for the link.
        return record
    row, _new = upsert(record)
    return row


def summarize(record: dict) -> str:
    """One human-readable block for a terminal or a Telegram card."""
    if not record:
        return "nothing parsed"
    tier = record.get("tier")
    out = [f"{record.get('name')}  ({record.get('tie')})",
           f"  {record.get('headline') or '?'}",
           f"  {record.get('profile_url') or 'NO PROFILE URL - paste the link too'}",
           f"  company: {record.get('company') or '?'}   "
           f"tier {tier}: {TIER_NAMES.get(tier, '?')}"]
    for r in record.get("reqs") or []:
        out.append(f"  - {r.get('title')}  [{r.get('field') or '?'}]  {r.get('location') or ''}"
                   f"  req {r.get('ats_job_id') or '?'}  apply {r.get('apply_after')}")
        out.append(f"    {r.get('url')}")
    for a in record.get("applied") or []:
        out.append(f"  * applied {a.get('applied') or '?'}: {a.get('title')}")
    if tier == TIER_TRACKED:
        out.append("  tracked, but nothing open matched last night. Re-run with --live for a "
                   "single-company pull.")
    if tier == TIER_UNKNOWN:
        out.append("  not in companies.yaml. Add it and tonight's scrape will find its roles.")
    return "\n".join(out)


# --------------------------------------------------------------- selftest ---
# EVERY person below is invented, and every profile slug is `example-` prefixed, which is
# the reservation scripts/pii_guard.py documents. The first draft of core/contacts.py's
# selftest was written with real people lifted from live search results; that is exactly the
# leak this feature creates, and it is worth restating here rather than assuming.
PAGE_PASTE = """
Dagny Halloran
Senior Data Scientist at Northwind
Fairhaven, Vermont, United States
Contact info
500+ connections

About
Builds forecasting systems. Previously payments risk.

Experience
Northwind · Full-time
Senior Data Scientist
Jan 2024 - Present

Education
Kestrel Institute
M.S., Computer Science
https://www.linkedin.com/in/example-dagny-halloran-77
"""

CARD_PASTE = """# Wei Ashford
Northwind
Toronto, Ontario, Canada, CA
500 connections, 1120 followers
## About
Payments infrastructure.
## Education
Kestrel Institute
"""


def selftest():
    import tempfile
    checks = 0
    NORTH = "Northwind"

    # --- extract_profile_url: the dedupe key, recovered from any shape ----------
    assert extract_profile_url("see https://www.linkedin.com/in/example-a-b-12/?trk=x here") \
        == "https://linkedin.com/in/example-a-b-12"; checks += 1
    assert extract_profile_url("in.linkedin.com/in/example-c-d") \
        == "https://linkedin.com/in/example-c-d"; checks += 1
    assert extract_profile_url("no link at all") == ""; checks += 1

    # --- parse_pasted_profile: PAGE ---------------------------------------------
    p = parse_pasted_profile(PAGE_PASTE)
    assert p and p.name == "Dagny Halloran", p; checks += 1
    assert p.employer == NORTH, p.employer; checks += 1
    assert p.location == "Fairhaven, Vermont, United States", p.location; checks += 1
    assert p.school == "Kestrel Institute", p.school; checks += 1
    assert p.key() == "linkedin.com/in/example-dagny-halloran-77", p.key(); checks += 1
    # "Contact info" and the connection count are chrome and must never reach a draft.
    assert "connections" not in p.headline and "Contact" not in p.headline; checks += 1

    # --- parse_pasted_profile: CARD (phase 1's shape, not re-implemented) --------
    c = parse_pasted_profile(CARD_PASTE)
    assert c and c.name == "Wei Ashford" and c.employer == NORTH, c; checks += 1
    # The stated location beats the URL subdomain, exactly as in contacts.parse_result.
    assert c.country == "ca", c.country; checks += 1

    # --- parse_pasted_profile: MINIMAL and empty ---------------------------------
    m = parse_pasted_profile("https://www.linkedin.com/in/example-rui-oyelaran-4b2")
    assert m and m.key() == "linkedin.com/in/example-rui-oyelaran-4b2", m; checks += 1
    # The slug stands in for the name until Parth types the real one, and the trailing hash
    # is dropped. The leading "Example" is this fixture's own reserved prefix showing
    # through, which is the correct behaviour: the derivation must not special-case it, or
    # a real person whose slug happens to start that way would lose part of their name.
    assert m.name == "Example Rui Oyelaran", m.name; checks += 1
    assert m.employer == "", "a bare URL must NOT invent an employer"; checks += 1
    assert parse_pasted_profile("") is None; checks += 1
    assert parse_pasted_profile("   \n  ") is None; checks += 1

    # A headline with no employer, and one whose "at" is prose rather than a company.
    assert employer_from_headline("Senior Data Scientist at Northwind") == NORTH; checks += 1
    assert employer_from_headline("Building things at scale") == ""; checks += 1
    assert employer_from_headline("Northwind | Payments") == ""; checks += 1

    # --- company_matches: the exact-match rule that phase 1 measured -------------
    rows = [{"company": "Northwind"}, {"company": "Northwinds Ltd"}, {"company": "Writer"}]
    assert len(company_matches(NORTH, rows)) == 1, company_matches(NORTH, rows); checks += 1
    # The measured trap: a single-word company name must not substring-match a headline.
    assert company_matches("Writer", [{"company": "Freelance Writer"}]) == []; checks += 1
    # Suffix tolerance, in both directions.
    assert company_matches("Northwind Markets, Inc.", [{"company": NORTH}]); checks += 1
    assert company_matches(NORTH, [{"company": "Northwind Markets, Inc."}]); checks += 1
    assert company_matches("", rows) == []; checks += 1

    # --- req_id: the field when present, the URL when the ledger is old ----------
    assert req_id({"ats_job_id": "4567890"}) == "4567890"; checks += 1
    assert req_id({"url": "https://job-boards.greenhouse.io/northwind/jobs/4567890"}) \
        == "4567890"; checks += 1
    assert req_id({"url": "https://jobs.lever.co/northwind/"
                          "0a1b2c3d-4e5f-6789-abcd-ef0123456789"}) \
        == "0a1b2c3d-4e5f-6789-abcd-ef0123456789"; checks += 1
    assert req_id({"url": "https://northwind.wd5.myworkdayjobs.com/ext/job/Atlanta/"
                          "Data-Scientist_R-12345"}) == "R-12345"; checks += 1
    # No id anywhere: "" so the draft OMITS it. A guessed req id is worse than none.
    assert req_id({"url": "https://northwind.com/careers"}) == ""; checks += 1

    assert ats_of({"ats": "Greenhouse"}) == "greenhouse"; checks += 1
    assert ats_of({"url": "https://northwind.wd5.myworkdayjobs.com/x"}) == "workday"; checks += 1
    assert ats_of({}) == ""; checks += 1

    # --- apply_after: the parallel-tracks rule -----------------------------------
    T = date(2026, 8, 20)
    gh = {"ats": "greenhouse", "posted": "2026-08-19T00:00:00Z"}
    wd = {"ats": "workday", "posted": "2026-08-19T00:00:00Z"}
    assert apply_after(gh, replied=True, today=T) == "2026-08-20"; checks += 1
    # Not replied: nothing is ever waited on, Workday included. A silent contact must not
    # freeze an application.
    assert apply_after(wd, replied=False, today=T) == "2026-08-20"; checks += 1
    # Workday + replied: wait the 4 days for the referral link.
    assert apply_after(wd, replied=True, today=T) == "2026-08-24"; checks += 1
    # ...but never past the 10-day staleness cap: posted 2026-08-19 caps the wait at 08-29,
    # and a contact who replies on 08-28 gets 08-29, not 09-01.
    late = date(2026, 8, 28)
    assert apply_after(wd, replied=True, sent=late, today=late) == "2026-08-29", \
        apply_after(wd, replied=True, sent=late, today=late); checks += 1
    # An old posting collapses the wait entirely.
    old = {"ats": "workday", "posted": "2026-07-01T00:00:00Z"}
    assert apply_after(old, replied=True, today=T) == "2026-08-20"; checks += 1
    # No posted date: the 4-day wait stands on its own.
    assert apply_after({"ats": "workday"}, replied=True, today=T) == "2026-08-24"; checks += 1

    # --- title_overlap: generic words must NOT be what ranks a req ---------------
    assert title_overlap("Senior Software Engineer", "Software Engineer at Northwind") == 0; checks += 1
    assert title_overlap("Data Scientist, Payments Risk",
                         "Senior Data Scientist at Northwind, payments risk") >= 2; checks += 1

    # --- rank_reqs: field order, then overlap, then freshness -------------------
    order = ["quant", "ai", "ds", "tech"]
    person = Person(name="X", headline="Payments risk data scientist at Northwind")
    reqs = [
        {"title": "Software Engineer", "field": "tech", "posted": "2026-08-19T00:00:00Z"},
        {"title": "Data Scientist, Payments", "field": "ds", "posted": "2026-08-01T00:00:00Z"},
        {"title": "Data Scientist, Ads", "field": "ds", "posted": "2026-08-19T00:00:00Z"},
        {"title": "Quant Researcher", "field": "quant", "posted": "2026-06-01T00:00:00Z"},
        {"title": "Unplaceable", "field": "", "posted": "2026-08-19T00:00:00Z"},
    ]
    got = rank_reqs(reqs, person, order=order, today=T)
    assert [r["title"] for r in got][0] == "Quant Researcher", got; checks += 1
    # Within one field, the headline overlap beats the fresher posting. This is the whole
    # point of the lever: an alum in payments is pointed at the payments req.
    ds = [r["title"] for r in got if r["field"] == "ds"]
    assert ds == ["Data Scientist, Payments", "Data Scientist, Ads"], ds; checks += 1
    # The cap holds and the unplaceable row is dropped, not merely demoted.
    assert len(got) == REQ_CAP and "Unplaceable" not in [r["title"] for r in got], got; checks += 1
    # With no field order configured (an unfilled profile) nothing is dropped for its field.
    assert len(rank_reqs(reqs, person, order=[], today=T)) == REQ_CAP; checks += 1

    # --- infer_tie ---------------------------------------------------------------
    alum = Person(name="A", headline="SWE at Northwind", school="Kestrel Institute")
    assert infer_tie(alum, school="Kestrel Institute", employer="Ardent Capital") == "alum"; checks += 1
    ex = Person(name="B", headline="SWE at Northwind", about="Previously Ardent Capital")
    assert infer_tie(ex, school="Kestrel Institute", employer="Ardent Capital") == "ex-colleague"; checks += 1
    cold = Person(name="C", headline="SWE at Northwind")
    assert infer_tie(cold, school="Kestrel Institute", employer="Ardent Capital") == "cold"; checks += 1

    # --- the four tiers, against a temp store so no real data is touched --------
    orig_store = get_store()
    with tempfile.TemporaryDirectory() as tmp:
        import pathlib
        d = pathlib.Path(tmp)
        try:
            st = use_temp_store(d)
            st.set_profile({
                "resume_tracks": [{"tag": "quant"}, {"tag": "ds"}, {"tag": "tech"}],
                "default_resume_tag": "ds"})
            # field_order puts the default tag first and keeps the rest in file order.
            assert field_order() == ["ds", "quant", "tech"], field_order(); checks += 1
            # resume_for: the req's own field decides the PDF, not the contact.
            assert resume_for("quant")["tag"] == "quant"; checks += 1
            # An unknown or absent field falls back to the configured default, exactly as
            # core/profile.py:pick_resume does, rather than naming a track nobody has.
            assert resume_for("")["tag"] == "ds" and resume_for("nope")["tag"] == "ds"; checks += 1

            # Nothing stored at all: a paste still resolves, as tier 4, rather than raising.
            rec = resolve_paste(PAGE_PASTE, notes="met at a meetup", store=False)
            assert rec and rec["tier"] == TIER_UNKNOWN, rec; checks += 1
            assert rec["notes"] == "met at a meetup"; checks += 1
            assert rec["reqs"] == [] and rec["applied"] == []; checks += 1

            # Tier 3: a tracked company, nothing open.
            st.set_companies([{"name": "Northwind", "ats": "greenhouse",
                               "slug": "northwind", "tier": "pipeline"}])
            rec = resolve_paste(PAGE_PASTE, store=False)
            assert rec["tier"] == TIER_TRACKED, rec["tier"]; checks += 1
            assert rec["ats"] == "greenhouse" and rec["slug"] == "northwind", rec; checks += 1
            # ...and the live pull is OPT-IN: the default must not fire a network call.
            def _boom(_name):
                raise AssertionError("the live pull must never run unless asked for")
            assert resolve_paste(PAGE_PASTE, puller=_boom, store=False)["tier"] == TIER_TRACKED
            checks += 1
            # With --live and a pull that finds something, it is tier 1, not tier 3: the
            # only difference from the overnight path is when the fetch happened.
            pulled = [{"company": "Northwind", "title": "Data Scientist", "field": "ds",
                       "ats": "greenhouse", "ats_job_id": "999",
                       "url": "https://job-boards.greenhouse.io/northwind/jobs/999",
                       "posted": "2026-08-19T00:00:00Z"}]
            rec = resolve_paste(PAGE_PASTE, live=True, puller=lambda n: pulled, today=T,
                                store=False)
            assert rec["tier"] == TIER_QUEUE and rec["pulled"] is True, rec; checks += 1
            assert rec["reqs"][0]["ats_job_id"] == "999"; checks += 1

            # Tier 2: applied, nothing open.
            st.set_applied([
                {"dedup_key": "abc", "company": "Northwind", "title": "Data Scientist",
                 "url": "https://job-boards.greenhouse.io/northwind/jobs/4567890",
                 "applied": "2026-08-01"}])
            rec = resolve_paste(PAGE_PASTE, today=T, store=False)
            assert rec["tier"] == TIER_APPLIED, rec["tier"]; checks += 1
            # The req id is recovered from the URL, because an applied row has no ats_job_id.
            assert rec["applied"][0]["ats_job_id"] == "4567890", rec["applied"]; checks += 1

            # Tier 1 wins over tier 2 when both are true, and the earlier application is
            # still carried so the draft can acknowledge it rather than pretend otherwise.
            st.set_queue("queue", [
                {"company": "Northwind", "title": "Data Scientist, Payments", "field": "ds",
                 "ats": "greenhouse", "ats_job_id": "111", "dedup_key": "k1",
                 "url": "https://job-boards.greenhouse.io/northwind/jobs/111",
                 "posted": "2026-08-19T00:00:00Z", "location": "New York, NY"}])
            st.set_queue("dream", [
                {"company": "Northwind", "title": "Quant Researcher", "field": "quant",
                 "ats": "workday", "ats_job_id": "R-222", "dedup_key": "k2",
                 "url": "https://northwind.wd5.myworkdayjobs.com/x/job/NY/Q_R-222",
                 "posted": "2026-08-19T00:00:00Z", "location": "New York, NY"}])
            rec = resolve_paste(PAGE_PASTE, today=T, store=False)
            assert rec["tier"] == TIER_QUEUE, rec["tier"]; checks += 1
            assert len(rec["applied"]) == 1, "the past application must survive tier 1"; checks += 1
            # The dream row is in the pool: that is the gap this whole feature closes.
            titles = [r["title"] for r in rec["reqs"]]
            assert "Quant Researcher" in titles, titles; checks += 1
            # Nothing is waited on at paste time, Workday included, because nobody replied.
            assert all(r["apply_after"] == "2026-08-20" for r in rec["reqs"]), rec["reqs"]; checks += 1

            # --- the store: dedupe, and what a re-paste must never reset --------
            rec = resolve_paste(PAGE_PASTE, today=T)
            assert rec["state"] == "found" and len(load_store()) == 1; checks += 1
            key = rec["id"]
            set_state(key, "invited")
            rec2 = resolve_paste(PAGE_PASTE, today=T)
            assert len(load_store()) == 1, "a re-paste must not create a second row"; checks += 1
            assert rec2["state"] == "invited", "a re-paste must NOT reset an invited contact"
            checks += 1
            assert len(rec2["history"]) == 2, rec2["history"]; checks += 1
            # An edited draft survives a re-paste; the record's empty drafts must not win.
            store = load_store()
            store[key]["drafts"] = {"invite_note": "hand edited"}
            save_store(store)
            rec3 = resolve_paste(PAGE_PASTE, today=T)
            assert rec3["drafts"]["invite_note"] == "hand edited", rec3["drafts"]; checks += 1
            # The same person via a country subdomain is the SAME row.
            assert normalize_profile_url("https://in.linkedin.com/in/example-dagny-halloran-77/") \
                == key; checks += 1
            # An unknown key is not a crash.
            assert set_state("linkedin.com/in/example-nobody", "invited") is None; checks += 1
            # A paste with no URL is returned but never stored: no key means a duplicate row
            # on the next paste, which is the one thing the store exists to prevent.
            nourl = resolve_paste("Dagny Halloran\nSenior Data Scientist at Northwind\n",
                                  today=T)
            assert nourl and nourl["id"] == "" and len(load_store()) == 1; checks += 1
        finally:
            set_store(orig_store)

    # --- short_id + find_by_short: a button field is 64 BYTES ------------------
    K = "linkedin.com/in/example-dagny-halloran-77"
    assert len(short_id(K)) == 8 and short_id(K) == short_id("https://in.linkedin.com/in/"
                                                             "example-dagny-halloran-77/")
    checks += 1
    # The whole reason the token exists: the key alone nearly fills the callback field.
    assert len(f"b:x:{short_id(K)}:invited".encode()) < 64; checks += 1
    assert short_id(K) != short_id("linkedin.com/in/example-rui-oyelaran-4b2"); checks += 1
    fake = {K: {"name": "Dagny Halloran"}}
    assert find_by_short(short_id(K), fake)["name"] == "Dagny Halloran"; checks += 1
    assert find_by_short("deadbeef", fake) is None; checks += 1
    assert find_by_short("", fake) is None; checks += 1

    # --- due_nudges: the sweep that keeps the state machine moving --------------
    NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)

    def _row(state, days_ago, **extra):
        ts = (NOW - timedelta(days=days_ago)).isoformat()
        return {"state": state, "history": [{"state": state, "ts": ts}], **extra}

    store = {
        "a": _row("invited", NUDGE_ACCEPT_DAYS + 1),      # due: did they accept?
        "b": _row("invited", 1),                          # too soon
        "c": _row("accepted", NUDGE_BUMP_DAYS + 1),       # due: bump
        "d": _row("accepted", 1),                         # too soon
        "e": _row("replied", 30),                         # done, never chase
        "f": _row("invited", 30, nudged={"accepted": "x"}),   # already asked once
        "g": {"state": "invited"},                        # no history: cannot age it
    }
    due = dict(due_nudges(store, NOW))
    assert due.get("a") == "accepted", due; checks += 1
    assert due.get("c") == "bump", due; checks += 1
    assert set(due) == {"a", "c"}, sorted(due); checks += 1
    # One bump and then the contact is dropped forever, per the plan. A follow-up loop that
    # re-asks every hour is one Parth mutes, which costs him the whole feature.
    assert due_nudges({"h": _row("bumped", 30)}, NOW) == []; checks += 1

    # --- the tier constants are the coverage measurement; pin them --------------
    assert (TIER_QUEUE, TIER_APPLIED, TIER_TRACKED, TIER_UNKNOWN) == (1, 2, 3, 4); checks += 1
    assert set(TIER_NAMES) == {1, 2, 3, 4}; checks += 1
    assert REQ_SHOW <= REQ_CAP; checks += 1

    print(f"referral: {checks}/{checks} checks passed")


def main():
    ap = argparse.ArgumentParser(
        description="Paste a LinkedIn profile, get the reqs worth asking them about.")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--paste", help="profile text (or use --file, or pipe it on stdin)")
    ap.add_argument("--file", help="read the pasted profile from a file")
    ap.add_argument("--notes", default="", help="how you know them, in your own words")
    ap.add_argument("--tie", default="", choices=("", *TIES))
    ap.add_argument("--live", action="store_true",
                    help="tier 3 only: pull this ONE company's board now")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve and print, but do not write outreach.json")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return
    text = a.paste or ""
    if a.file:
        text = open(a.file, encoding="utf-8").read()
    elif not text and not sys.stdin.isatty():
        text = sys.stdin.read()
    if not text.strip():
        ap.error("nothing to parse: pass --paste, --file, or pipe the profile on stdin")
    rec = resolve_paste(text, notes=a.notes, tie=a.tie, live=a.live, store=not a.dry_run)
    if rec is None:
        print("could not find a person in that paste", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(rec, indent=2, ensure_ascii=False) if a.json else summarize(rec))


if __name__ == "__main__":
    main()
