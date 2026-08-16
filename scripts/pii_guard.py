"""Refuse to commit personal details into the shareable code repo.

    python scripts/pii_guard.py --staged    # what a commit is about to add (the hook)
    python scripts/pii_guard.py --all       # every tracked file, for an audit
    python scripts/pii_guard.py --patterns  # show what is being hunted, match nothing

WHY THIS EXISTS
---------------
The code and the person using it live in two repos now. The code repo is the one that can
be made public; nothing in it should identify anybody. But the leaky details are not data
files — those are easy to keep out — they are *comments and test fixtures*. A home city in
a note explaining a location picker, an employer in a taxonomy rule, a phone number in a
fill fixture. Those get written as legitimate engineering notes, faster than anyone
re-audits them, and one session of work on a dropdown added about fifteen.

The old design caught them at publish time, in a script that stripped files on the way out.
That script cannot survive the split: it only ran in the private repo, and the public tree
is now the working copy, so there is no "way out" left to check. The check has to move to
the moment the line is written.

WHY IT DOES NOT HARDCODE WHAT IT HUNTS FOR
------------------------------------------
A leak scanner has to name the thing it is looking for, so the obvious implementation ships
a file listing your home city, employer and school — publishing the detector publishes the
secret. The predecessor solved that by excluding itself from the public tree, which is not
available here: this file has to live in the code repo to run on its commits.

So it derives the patterns from your own profile.json at runtime and holds none of them.
That is what makes this file publishable, and it has the pleasant side effect of working
for a stranger out of the box, hunting their details instead of somebody else's.

Anything that is not in profile.json — a private target-list statistic, an EEO answer
shape — goes in `pii_extra.txt` next to it, which is gitignored for the same reason.

WHAT IT DELIBERATELY DOES NOT FLAG
----------------------------------
The author's NAME. A named author with a private phone number is the normal open-source
posture; attribution is the point of a portfolio piece. Sensitive *attributes* are what get
scrubbed, not authorship.

Only lines the commit ADDS are scanned, never whole files. Scanning whole files would mean
that one unscrubbed comment in fill.js blocks every future commit to fill.js until somebody
cleans the entire file, which is how a hook earns itself a permanent --no-verify.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from core import paths  # noqa: E402  (needs REPO on sys.path first)

# Files that never reach the shareable repo, so a real personal value in one is expected
# and correct. Deliberately the same list the old publish step withheld: the config files
# that hold the details on purpose, the working documents that record findings against a
# live job search, and the scrubbing machinery itself, which has to name what it hunts.
# In the split layout none of these live in the code repo at all.
#
# Keep this list SHORT. Every entry is a file nobody is checking, so an entry added for
# convenience rather than because the file is genuinely private is a hole.
ALLOW = re.compile(
    r"^(data/(profile|answer_bank|companies)\.(json|yaml)"
    r"|data/pii_extra\.txt"
    r"|CLAUDE\.md"
    r"|docs/[^/]*audit[^/]*\.md"
    # This guard's own test, and the ONE entry here that is not private. It is exempt for
    # the opposite reason to the rest: its whole job is to contain the shapes the guard
    # hunts, so scanning it means either mangling the fixtures until they stop testing
    # anything, or a standing --no-verify on the file that proves the guard works. Its
    # fixtures are invented — a made-up employer, school, country and set of EEO answers,
    # and a 555 phone — so there is nothing real in it to protect. Keep it that way.
    r"|scripts/test_pii_guard\.py)$"
)
# NOTE this file is NOT in that list, on purpose. It is the one scrubbing tool that holds
# none of what it hunts — the patterns are built from your profile at runtime and exist
# only in memory — which is what lets it live in the shareable repo at all. Exempting it
# would remove the check that keeps it that way, so it is scanned like everything else.

# Shape-only patterns. These name nobody, so they are safe to hardcode, and they catch a
# detail that belongs to someone who is not you — a recruiter's number pasted into a note.
#
# PRECISION MATTERS MORE THAN BREADTH. A scan that cries wolf on every ten-digit constant
# gets bypassed, and a bypassed scan protects nothing. A phone needs real separators so an
# FNV prime and INT_MAX do not match; 555 is reserved fiction; example.com/.org are the
# documentation domains and users.noreply.github.com is a commit identity, not a person.
SHAPE_PATTERNS: list[tuple[str, str]] = [
    (r"(?<![\d-])(?!555)\d{3}[-.\s]\d{3}[-.\s]\d{4}(?![\d-])", "phone-shaped number"),
    (r"\((?!555)\d{3}\)\s*\d{3}[-.\s]?\d{4}", "phone-shaped number"),
    # Both ends need three characters, which drops the one-letter-either-side class of
    # placeholder, and the reserved TLDs (RFC 2606/6761) are as fictional as 555. Writing
    # such a placeholder out here would itself be an email-shaped string, which this very
    # check flags — describe the shape, do not spell one. Your OWN address is
    # matched literally by the derived set, so tightening here cannot lose it — this
    # pattern is only here to catch somebody else's address pasted into a note.
    (r"[A-Za-z0-9._%+-]{3,}@(?!example\.(?:com|org)\b|users\.noreply\.github\.com\b)"
     r"[A-Za-z0-9.-]{3,}\.(?!test\b|invalid\b|example\b|localhost\b)[A-Za-z]{2,}\b",
     "email address"),

    # --- the classes a DERIVED set structurally cannot catch ---------------------------
    # Everything else this guard hunts is either a shape or a literal lifted from your own
    # profile, so it only ever recognises the answers you happen to hold today. These two
    # hunt the disclosure itself, whoever it belongs to. They came from a second, hardcoded
    # scanner that has since been retired; it caught things the derived patterns could not,
    # which is why they were kept rather than deleted with it.
    #
    # EEO self-identification is special-category data (GDPR Art.9) and the class a
    # value-substitution pass misses entirely, because the values look innocuous on their
    # own. Matching the QUESTION beside ANY standard answer also catches a fixture
    # transcribed from somebody else's run, or from an answer you have since changed —
    # neither of which appears in your profile to be derived from. Option lists and
    # mapping tables are suppressed by lists_alternatives, exactly as for the derived
    # EEO checks, so a canonicalisation fixture does not fire.
    (r"(?i)\b(sexual orientation|gender identity|pronouns|gender)\b[^\n]{0,40}"
     r"\b(straight|heterosexual|gay|lesbian|bisexual|male|female|man|woman|non.?binary)\b",
     "EEO self-identification"),
    # A named company beside a DATE or a live record is a partial history of where somebody
    # applied and when, which is the same category as the data files this repo keeps out.
    #
    # The date requirement is the whole boundary, and it is deliberately strict. "Verified
    # live on <Company>: the first synthesized click is often swallowed" is an ATS
    # BEHAVIOUR note — the most valuable content in the repo, and the round-2 audit
    # explicitly kept that class while scrubbing the dated ones. Widening this to any
    # company name beside any outcome verb immediately flagged that comment, plus an
    # assertion about a message being "rejected by Telegram" — a bad trade, because a
    # check that fires on ordinary prose gets bypassed on the line that matters.
    (r"(?i:live on |the real )[A-Z][A-Za-z]+[^\n]{0,20}\d{4}-\d{2}-\d{2}|"
     r"(?i:skipped a live )[A-Z][A-Za-z]+ record|"
     r"(?i:\bthe real )[A-Z][A-Za-z]+ (?i:run|failure)\b",
     "application outcome"),

    # A THIRD PARTY's LinkedIn profile. Everything else this guard protects is Parth's own
    # data; outreach contacts are the one category that belongs to somebody else, which
    # makes them the most important thing to keep out of a public repo and the only category
    # nobody would think to check. A real slug reads `/in/firstname-lastname-a1b2c3`, so the
    # shape is a hyphenated multi-part slug — not the bare `/in/foo` that appears in prose
    # explaining the URL format.
    #
    # `example-` is reserved for invented fixtures, exactly as 555 and example.com are
    # reserved above: core/contacts.py's selftest needs realistic profile URLs to test
    # against, and mangling them until they stop looking real would stop them testing
    # anything. Anything NOT so prefixed is presumed to be a person.
    (r"linkedin\.com/in/(?!example-)[a-z0-9]+(?:-[a-z0-9]+){1,}",
     "someone else's LinkedIn profile"),
]

# Mutually exclusive answers to the same question. Nobody is both Male and Female, so a
# line holding two members of one group is a mapping table, an option list or a comparison
# — never a disclosure. This is what separates
#
#     ["Gender", "Male", "Man"],                     <- canonicalisation fixture
#     // "Male" matches an ATS that only offers "Man"   <- a comment explaining it
#
# from the thing actually worth stopping:
#
#     { label: "Gender", value: <your answer> }      <- a disclosure
#
# Only applied to the EEO checks, and only after a match has already been found, so it can
# suppress a false positive but never widen what is hunted. Members are matched
# case-insensitively; \bmale\b does not fire inside "female", which is why the pairs can
# sit on one line without collapsing into a single hit.
EEO_ALTERNATIVES: list[list[str]] = [
    [r"\bmale\b", r"\bmen\b", r"\bman\b", r"\bfemale\b", r"\bwomen\b", r"\bwoman\b",
     r"\bnon.?binary\b", r"\bgenderqueer\b", r"\bagender\b"],
    [r"\bstraight\b", r"\bheterosexual\b", r"\bgay\b", r"\blesbian\b", r"\bbisexual\b",
     r"\bqueer\b", r"\basexual\b", r"\bpansexual\b"],
    # The paired forms only. A bare he/she/they is far too common in prose to treat as an
    # answer, and treating it as one would suppress a real disclosure sitting next to it.
    [r"\bhe[\s/]+him\b", r"\bshe[\s/]+her\b", r"\bthey[\s/]+them\b"],
]

# The same rule, applied to the home state. Nobody lives in three states at once, so a
# line naming several is a lookup table — `core/scraper.py`'s US_STATE_NAMES / _ABBR are
# exactly that, and without this the guard blocks every future edit to the file that
# holds them. Threshold is 3, not 2: "moved from Vermont to Oregon" is a real disclosure,
# a 50-entry table never has that shape. (This comment named a real home state on its
# first draft and the guard flagged itself. It is deliberately not in its own ALLOW list.)
STATE_ALTERNATIVES: list[list[str]] = [
    [rf"\b{s}\b" for s in (
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
        "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho", "illinois",
        "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland",
        "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana",
        "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york",
        "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
        "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah",
        "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming")],
    # Quoted abbreviations, which is the only form the state check hunts.
    [rf"[\"']{s}[\"']" for s in (
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
        "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
        "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
        "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC")],
]

# Words too generic to identify anyone on their own. Without this, a school called
# "... Institute of Technology" would make every occurrence of "technology" a finding.
GENERIC = {
    "institute", "institution", "technology", "technologies", "science", "sciences",
    "university", "college", "school", "academy", "polytechnic", "state", "national",
    "international", "group", "company", "corporation", "holdings", "partners",
    "capital", "global", "systems", "solutions", "services", "the", "and", "for", "of",
}


def _lit(value: str) -> str:
    """A regex matching `value` literally, tolerant of how whitespace was typed."""
    return re.sub(r"\\?\s+", r"\\s+", re.escape(value.strip()))


def _tokens(value: str) -> list[str]:
    """The distinctive words in a name: 'Northgate Institute of Technology' -> northgate.

    Partial matches are the dangerous case: a fixture once kept the real area code and
    exchange with only the last four digits changed, so searching for the whole number
    found nothing. The same applies to names — an abbreviated school or an employer's
    surname alone still identifies you, so the pieces are hunted as well as the whole.
    """
    words = re.findall(r"[A-Za-z]{4,}", value)
    return sorted({w.lower() for w in words if w.lower() not in GENERIC})


def _add(out: list[tuple[str, str]], pattern: str, label: str) -> None:
    if pattern and (pattern, label) not in out:
        out.append((pattern, label))


def _add_ci(out: list[tuple[str, str]], value: str, label: str, word: bool = False) -> None:
    """Add a case-insensitive literal for `value`, if there is one.

    The empty check is load-bearing: an unset field would otherwise produce the pattern
    "(?i)", which matches every line in the repo.
    """
    if not value.strip():
        return
    body = _lit(value)
    _add(out, rf"(?i)\b{body}\b" if word else rf"(?i){body}", label)


def _near(question: str, answer: str, window: int = 40) -> str:
    """Match `answer` only when `question` sits within `window` characters, either order.

    For values that are ordinary words on their own. An EEO answer sitting in prose is
    nothing; the same word immediately after its question is a disclosure. Returns "" when
    there is no answer to hunt, so an unset field contributes no pattern rather than one
    that matches every line.

    (This docstring spelled out a question-and-answer pair as its example until the
    shape-based EEO check started flagging it. Name the CLASS of value here, never an
    instance of one — the same rule the work-authorization comment below records.)
    """
    if not answer.strip():
        return ""
    a = _lit(answer)
    gap = rf"[^\n]{{0,{window}}}"
    return rf"(?i)(\b(?:{question})\b{gap}\b{a}\b|\b{a}\b{gap}\b(?:{question})\b)"


def derive_patterns(profile: dict) -> list[tuple[str, str]]:
    """Build the hunt list from a filled-in profile. Unset values contribute nothing.

    profile.json uses the literal FILL_ME rather than omitting keys, so an unfilled
    template would otherwise turn the string FILL_ME into a pattern and flag every
    template in the repo.
    """
    pats: list[tuple[str, str]] = []

    def val(*keys: str) -> str:
        node: object = profile
        for k in keys:
            if not isinstance(node, dict):
                return ""
            node = node.get(k, "")
        s = str(node or "").strip()
        return "" if s in ("FILL_ME", "None") else s

    # --- phone: match however it was punctuated, plus the leading two groups on their own
    digits = re.sub(r"\D", "", val("identity", "phone"))
    if len(digits) >= 10:
        a, b, c = digits[-10:-7], digits[-7:-4], digits[-4:]
        sep = r"[-.\s]?\s*"
        _add(pats, rf"\(?{a}\)?{sep}{b}{sep}{c}", "your phone number")
        _add(pats, rf"\(?{a}\)?{sep}{b}\b", "your area code + exchange")

    _add_ci(pats, val("identity", "email"), "your email address")

    # --- address. A state abbreviation only counts when quoted: two bare capitals are far
    # too common in code to flag, but the same two in quotes, as a value, are the answer.
    for key, label in (("line1", "your street address"), ("city", "your home city"),
                       ("zip", "your postal code"), ("state_full", "your home state")):
        _add_ci(pats, val("identity", "address", key), label, word=True)
    st = val("identity", "address", "state")
    if len(st) == 2:
        _add(pats, rf"[\"']{re.escape(st)}[\"']", "your home state")
    _add_ci(pats, val("identity", "location_display"), "your location")

    # --- handles. Bare last path segment of a profile URL, which is what tends to get
    # pasted into a comment.
    for k in ("linkedin", "github", "twitter"):
        u = val("links", k)
        if not u:
            continue
        _add_ci(pats, u, f"your {k} link")
        tail = u.rstrip("/").rsplit("/", 1)[-1]
        if len(tail) >= 5 and tail.lower() not in GENERIC:
            _add_ci(pats, tail, f"your {k} handle", word=True)

    # --- your credential. Not hunted at all until 2026-07-31, which let a comment naming
    # it as its worked example pass the guard and reach the shareable repo. The LEVEL
    # ("Bachelors") and the MAJOR ("Computer Science") are deliberately not hunted: they
    # are shared by millions and identify nobody, so hunting them is pure noise. The exact
    # credential string is the distinctive part.
    _add_ci(pats, val("education", "degree"), "your degree")
    # A GPA is only worth hunting when its WRITTEN FORM is distinctive. On a 4.0 scale it is
    # a bare number shared by millions, and belongs with the degree level and the major
    # above: it identifies nobody. Worse, as a substring it is everywhere a repo already is —
    # npm version ranges in a lockfile, a line-height in a stylesheet — so hunting it reports
    # those and no actual leaks. A scaled form (a value carrying its own denominator) is
    # nobody's version number and stays hunted.
    gpa = val("education", "gpa")
    if gpa and not re.fullmatch(r"\d+(?:\.\d+)?", gpa):
        _add_ci(pats, gpa, "your GPA")

    # --- employer and school, whole and in distinctive pieces
    for keys, label in ((("employment", "current_employer"), "your employer"),
                        (("education", "school"), "your school")):
        full = val(*keys)
        if not full:
            continue
        _add_ci(pats, full, label)
        for t in _tokens(full):
            _add(pats, rf"(?i)\b{re.escape(t)}\b", label)

    # --- work authorization. A residency status is a distinctive enough phrase to hunt on
    # its own; a country name is not, so it only counts beside a word that makes it a claim
    # about a person. Bare-matching a country flags every timezone note and location filter
    # in the repo, and a check that fires on those gets bypassed on the one that matters.
    # (This comment spelled out a real status as its example until the publish scan caught
    # it. Name the CLASS of value in comments here, never an instance of it.)
    _add_ci(pats, val("work_authorization", "status"), "your work-authorization status")
    _add(pats, _near(r"citizen(ship)?|nationality|passport|country of (origin|birth)",
                     val("work_authorization", "citizenship_country")), "your citizenship")

    # --- EEO self-identification: special-category data under GDPR Art.9, and the class a
    # value-substitution pass misses entirely because the values look innocuous.
    #
    # Context is mandatory here for the same reason. These answers are ordinary English —
    # "straight", "woman", "no" — and hunting them bare matches prose like "goes straight
    # into the prompt". The pairing is what makes it a disclosure, so the question has to
    # sit within a line of the answer. Values under four characters are skipped outright.
    eeo = profile.get("eeo") if isinstance(profile.get("eeo"), dict) else {}
    for k, v in (eeo or {}).items():
        v = str(v or "").strip()
        if k.startswith("_") or v in ("", "FILL_ME") or len(v) < 4:
            continue
        if v.lower() in ("true", "false"):
            continue
        # "sexual_orientation" also has to match "sexual orientation" and "sexual-orientation"
        question = re.sub(r"_+", r"[\\s_-]*", re.escape(k))
        _add(pats, _near(question, v), f"your EEO answer ({k})")

    return pats


def derive_company_stats() -> list[tuple[str, str]]:
    """Hunt the SIZE and TIERING of the private target list.

    companies.yaml is withheld from the shareable repo, but its statistics leak in prose
    anyway: a design doc that states the total and the per-tier split gives away the very
    ranking the file is withheld for, and no pattern derived from profile.json can know
    those numbers. Reading them from the real companies file keeps this module free of
    hardcoded facts and makes it work on a stranger's own list.

    (Written with the real totals as its worked example; the check caught them on its
    first run, which is the same lesson the _near docstring above records.)

    Bare numbers would be far too noisy, so each one only counts beside a word that turns
    it into a claim about the list. Never fatal: no companies file, or an unreadable one,
    simply contributes nothing.
    """
    # COMPANIES_WRITE, never COMPANIES: the read path falls back to the example template,
    # whose counts are fiction and would hunt numbers nobody is protecting.
    path = paths.COMPANIES_WRITE
    if not path.exists():
        return []
    try:
        import yaml
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        rows = doc.get("companies") if isinstance(doc, dict) else doc
        rows = [r for r in (rows or []) if isinstance(r, dict)]
    except Exception as e:                      # a broken config must not block a commit
        print(f"[pii-guard] could not read {path.name} ({e}); target-list stats not hunted",
              file=sys.stderr)
        return []
    if not rows:
        return []

    out: list[tuple[str, str]] = []
    tiers: dict[str, int] = {}
    for r in rows:
        t = str(r.get("tier") or "").strip().lower()
        if t:
            tiers[t] = tiers.get(t, 0) + 1
    # A count of 1 or 2 is not a statistic, it is every small number in the codebase.
    if len(rows) > 2:
        _add(out, rf"\b{len(rows)}\b[^\n]{{0,30}}(?i:compan|target|employer)",
             "your target-list size")
    for tier, n in tiers.items():
        if n > 2:
            _add(out, rf"\b{n}\b[^\n]{{0,20}}(?i:{re.escape(tier)})",
                 "your target-list tiering")
    return out


def derive_contact_patterns() -> list[tuple[str, str]]:
    """Hunt the NAMES of real outreach contacts, read from outreach.json.

    The shape check above catches a profile URL. It cannot catch a contact's NAME written
    into a comment or a test fixture ("verified against <a real recruiter>"), because a name
    has no shape — and unlike every other pattern here, these names are not in profile.json
    to be derived from. They come from the one file under HOME that holds somebody else's
    data rather than Parth's.

    Full names only. A first name alone is far too common to hunt without flagging ordinary
    prose, and this guard is bypassed the moment it cries wolf. Never fatal: no outreach
    file, or an unreadable one, contributes nothing.
    """
    path = paths.OUTREACH
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[pii-guard] could not read {path.name} ({e}); contact names not hunted",
              file=sys.stderr)
        return []
    if isinstance(rows, dict):
        rows = list(rows.values())
    out: list[tuple[str, str]] = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "").strip()
        # Two real parts, neither generic — the same bar derive_patterns applies to a school.
        parts = [p for p in re.split(r"\s+", name) if len(p) > 2 and p.lower() not in GENERIC]
        if len(parts) >= 2:
            _add_ci(out, name, "an outreach contact's name", word=True)
        url = str(r.get("profile_url") or "").strip()
        if "/in/" in url:
            slug = url.split("/in/", 1)[1].strip("/").split("?")[0]
            if len(slug) > 3:
                _add_ci(out, slug, "an outreach contact's profile")
    return out


def load_extra() -> list[tuple[str, str]]:
    """Extra regexes from `pii_extra.txt` in the data dir: one per line, # comments.

    For everything the derived patterns cannot know — a phrase that only reads as
    sensitive in context. Lives beside the config it protects, and is gitignored for
    exactly the reason this whole file exists.
    """
    path = paths.DATA / "pii_extra.txt"
    if not path.exists():
        return []
    out: list[tuple[str, str]] = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pat, _, label = line.partition("\t")
        pat, label = pat.strip(), (label.strip() or "private detail")
        try:
            re.compile(pat)
        except re.error as e:
            print(f"[pii-guard] {path.name}:{n} is not a valid regex, ignored ({e})",
                  file=sys.stderr)
            continue
        _add(out, pat, label)
    return out


def build_patterns() -> tuple[list[tuple[str, str]], bool]:
    """(patterns, personalised). personalised=False means no filled-in profile was found."""
    pats = list(SHAPE_PATTERNS)
    personal = False
    # PROFILE_WRITE, never PROFILE: the read path falls back to the .example template on a
    # fresh clone, and every value in it is FILL_ME.
    if paths.PROFILE_WRITE.exists():
        try:
            pats += derive_patterns(json.loads(paths.PROFILE_WRITE.read_text("utf-8")))
            personal = True
        except (json.JSONDecodeError, OSError) as e:
            print(f"[pii-guard] could not read profile.json ({e}); shape checks only",
                  file=sys.stderr)
    pats += derive_company_stats()
    pats += derive_contact_patterns()
    pats += load_extra()
    return pats, personal


# ---------------------------------------------------------------------------
# What to scan
# ---------------------------------------------------------------------------
def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True,
                          text=True, errors="replace").stdout


def _parse_diff(diff: str) -> list[tuple[str, int, str]]:
    """(path, new line number, text) for every ADDED line in a unified diff.

    Additions only, and numbered in the post-image, so a pre-existing match elsewhere in
    the same file does not block the commit. Split out from the git call so it can be
    tested without a repository.
    """
    out: list[tuple[str, int, str]] = []
    path, lineno = "", 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            path = "" if p == "/dev/null" else p[2:] if p.startswith("b/") else p
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            lineno = int(m.group(1)) if m else 0
        elif line.startswith("+"):
            if path and not ALLOW.match(path):
                out.append((path, lineno, line[1:]))
            lineno += 1
    return out


def staged_additions() -> list[tuple[str, int, str]]:
    """Every line the pending commit would add."""
    return _parse_diff(_git("diff", "--cached", "--unified=0", "--no-color",
                            "--diff-filter=ACM", "-M"))


def all_tracked_lines() -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for f in [ln.strip() for ln in _git("ls-files").splitlines() if ln.strip()]:
        if ALLOW.match(f):
            continue
        try:
            text = (REPO / f).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue           # binary or gone: nothing to match against
        out += [(f, n, ln) for n, ln in enumerate(text.splitlines(), 1)]
    return out


def lists_alternatives(line: str) -> set[str]:
    """Which checks this line is an option-list for, as label prefixes to suppress.

    A line holding mutually exclusive answers to one question is a mapping table, an
    option list or a comparison — never a disclosure. Suppression is applied only AFTER
    a match, and only to the check it belongs to, so it can kill a false positive but
    never widen what is hunted.
    """
    hit: set[str] = set()
    for group in EEO_ALTERNATIVES:
        if sum(1 for p in group if re.search(p, line, re.I)) >= 2:
            hit.add("your EEO answer")
            # The shape-based EEO check hunts the same disclosure from the other side, so
            # it needs the same escape hatch — otherwise every canon() mapping table in
            # the extension becomes a permanent finding.
            hit.add("EEO self-identification")
            break
    for group in STATE_ALTERNATIVES:
        if sum(1 for p in group if re.search(p, line, re.I)) >= 3:
            hit.add("your home state")
            break
    return hit


def scan(lines: list[tuple[str, int, str]],
         patterns: list[tuple[str, str]]) -> list[str]:
    compiled = [(re.compile(p), lbl) for p, lbl in patterns]
    findings: list[str] = []
    for path, n, text in lines:
        option_list = lists_alternatives(text)
        for rx, label in compiled:
            if any(label.startswith(pfx) for pfx in option_list):
                continue
            m = rx.search(text)
            if m:
                findings.append(f"{path}:{n}  {label}: {m.group(0)[:60]}")
                break          # one finding per line is enough to stop the commit
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--staged", action="store_true", help="scan what a commit would add")
    g.add_argument("--all", action="store_true", help="scan every tracked file")
    g.add_argument("--patterns", action="store_true", help="list the patterns and exit")
    args = ap.parse_args()

    patterns, personalised = build_patterns()

    if args.patterns:
        print(f"{len(patterns)} pattern(s), "
              f"{'derived from your profile' if personalised else 'shape checks only'}:")
        for p, label in patterns:
            print(f"  {label:38s} {p}")
        return 0

    if not personalised:
        # Not an error. A fresh clone has no profile yet, and blocking commits on a repo
        # somebody just cloned is how a hook gets deleted in week one.
        print("[pii-guard] no profile.json yet, running shape checks only.", file=sys.stderr)

    lines = staged_additions() if args.staged else all_tracked_lines()
    findings = scan(lines, patterns)

    if not findings:
        return 0

    where = "this commit" if args.staged else "the tracked tree"
    print(f"\n  pii-guard: {len(findings)} personal detail(s) in {where}\n", file=sys.stderr)
    for f in findings:
        print(f"    ! {f}", file=sys.stderr)
    print("\n  This repo is the shareable one. Reword the line to keep the technical point"
          "\n  and drop the personal example — a generic school teaches the same lesson."
          "\n"
          "\n  Genuinely a false positive?  git commit --no-verify"
          "\n  Seeing the patterns:         python scripts/pii_guard.py --patterns\n",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
