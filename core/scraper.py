#!/usr/bin/env python3
"""
Job scraper v2 — Greenhouse / Lever / Ashby.

Design goals (fixing the v1 pain):
  * FRESH-TRUTH: every run re-reads the live board API and surfaces ONLY what is
    currently open, so expired/closed postings never leak through. There is no
    accumulation of a "seen" pile that goes stale.
  * BROADER: Greenhouse + Lever + Ashby, driven by a hand-editable companies.yaml.
  * TAGGED: each role is classified into a field (ai/tech/quant/ds) at scrape time.
  * DEDUPED: normalized (company + title + location) key, checked against applied.json.
  * FILTERED: relevance (field match), seniority, US-location, freshness.
  * LIVENESS: optional per-job re-check on the shortlist to close the scrape->surface gap.

Outputs:
  queue.json   -> pipeline roles, live, fresh, not-yet-applied, freshest first (apply feeds off this)
  dream.json   -> dream roles, same shape as queue.json; manual-only (extension skips these)
  digest.md    -> human-readable, grouped by field, dream roles flagged separately
  raw_YYYYMMDD.json -> everything fetched this run (audit)

Note: the job-board APIs are not reachable from every sandbox; run this in your
GitHub Actions environment. The pure functions (parse/classify/filter/dedup) are
unit-testable offline via --selftest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import requests
except ImportError:  # allow --selftest without network deps
    requests = None
import yaml

from core import paths

UA = "job-copilot/2.0 (personal job search)"
FIELD_PRIORITY = ("quant", "ai", "ds", "tech")  # order matters for classification

# --- what counts as a US location -------------------------------------------
# State NAMES are matched against the normalized (lowercased) string; the two-letter
# ABBREVIATIONS are matched against the RAW string with their real casing, on purpose.
# Lowercasing turns IN / OR / ME / OK / DE / LA / HI into ordinary English words, and
# "Remote in Europe" would then read as Indiana.
US_STATE_NAMES = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia",
)
US_STATE_ABBR = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
    "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN",
    "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
)
US_MARKERS = (
    "united states", "usa", "us", "u s", "u s a", "america", "nationwide",
    "remote us", "us remote",
)
# Cities that commonly appear WITHOUT a state ("San Francisco", "NYC"). Deliberately
# excludes names that are also major non-US cities (Cambridge, Birmingham, Manchester,
# Portland is fine, London is not) -- Cambridge MA still passes on the "MA" abbreviation.
US_CITIES = (
    "new york", "nyc", "brooklyn", "manhattan", "san francisco", "sf", "bay area",
    "silicon valley", "palo alto", "mountain view", "menlo park", "sunnyvale",
    "san jose", "santa clara", "san mateo", "redwood city", "oakland", "berkeley",
    "los angeles", "santa monica", "irvine", "san diego", "sacramento", "seattle",
    "bellevue", "portland", "austin", "dallas", "houston", "san antonio", "denver",
    "boulder", "chicago", "boston", "somerville", "atlanta", "miami", "orlando",
    "tampa", "philadelphia", "pittsburgh", "phoenix", "scottsdale", "salt lake city",
    "minneapolis", "detroit", "ann arbor", "nashville", "charlotte", "raleigh",
    "durham", "columbus", "kansas city", "st louis", "las vegas", "jersey city",
    "hoboken", "princeton", "stamford", "madison", "indianapolis", "milwaukee",
    "cincinnati", "cleveland", "richmond", "baltimore", "honolulu", "anchorage",
)


# ------------------------------------------------------------------ model ---
@dataclass
class Job:
    company: str
    title: str
    location: str
    url: str
    ats: str
    ats_slug: str
    ats_job_id: str
    posted: str | None      # ISO8601 UTC, or None if unknown
    field: str | None       # ai / tech / quant / ds / None(=irrelevant)
    tier: str               # dream / pipeline
    # Secondary location evidence, used ONLY when `location` itself can't be placed.
    # Boards routinely put "Hybrid" / "In-Office" / "Remote" / "N/A" in the location field
    # and keep the real geography somewhere else: Greenhouse in offices[].name and the
    # "Job Posting Location" metadata, Ashby in address.postalAddress.addressCountry,
    # Workday in country.descriptor. Free text on purpose — it is fed back through
    # location_verdict(), so the same allowlist and exclude_locations judge it.
    location_hint: str = ""
    dedup_key: str = ""

    def keyed(self) -> "Job":
        self.dedup_key = dedup_key(self.company, self.title, self.location)
        return self


# -------------------------------------------------------------- utilities ---
def norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def has_word(text: str, phrase: str) -> bool:
    p = norm(phrase)
    if not p:
        return False
    return re.search(rf"\b{re.escape(p)}\b", norm(text)) is not None


def dedup_key(company: str, title: str, location: str) -> str:
    base = f"{norm(company)}|{norm(title)}|{norm(location)}"
    return hashlib.sha1(base.encode()).hexdigest()[:16]


def iso_date(s) -> str | None:
    if not s:
        return None
    s = str(s).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def iso_from_ms(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- parsers ---
def parse_greenhouse(company, slug, tier, raw) -> list[Job]:
    out = []
    for j in raw.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        posted = iso_date(j.get("first_published") or j.get("updated_at"))
        out.append(Job(company, j.get("title", ""), loc, j.get("absolute_url", ""),
                       "greenhouse", slug, str(j.get("id", "")), posted, None, tier,
                       greenhouse_location_hint(j)))
    return out


def greenhouse_location_hint(j: dict) -> str:
    """Where Greenhouse hides the real geography when `location.name` is useless.

    Two places, both seen in production: offices[].name ("Remote India", "Canada
    Locations") and a metadata field literally called "Job Posting Location"
    (["Bengaluru, India"]). Cloudflare files real India roles as location "In-Office",
    so this is what stops `keep_unknown_if_us` from re-admitting them.
    """
    parts = []
    for o in j.get("offices") or []:
        n = (o or {}).get("name")
        if n:
            parts.append(str(n))
    for m in j.get("metadata") or []:
        if not isinstance(m, dict):
            continue
        if "location" not in str(m.get("name", "")).lower():
            continue
        v = m.get("value")
        if isinstance(v, list):
            parts.extend(str(x) for x in v if x)
        elif v:
            parts.append(str(v))
    return "; ".join(parts)


def parse_lever(company, slug, tier, raw) -> list[Job]:
    out = []
    for j in raw:  # lever returns a bare list
        cat = j.get("categories") or {}
        posted = iso_from_ms(j.get("createdAt"))
        hint = "; ".join(str(x) for x in (cat.get("country"), j.get("country"),
                                          j.get("workplaceType")) if x)
        out.append(Job(company, j.get("text", ""), cat.get("location", ""),
                       j.get("hostedUrl", ""), "lever", slug, str(j.get("id", "")),
                       posted, None, tier, hint))
    return out


def parse_workday_relative(posted_on: str | None) -> str | None:
    """'Posted 3 Days Ago' -> ISO date. Workday's LIST endpoint has no real date.

    Only a fallback: the detail endpoint returns a true `startDate`, and that is what we
    use whenever the detail fetch succeeds. '30+ Days Ago' is Workday's terminal bucket —
    it means "at least 31", so it reads as exactly 31 and such roles fall out of any
    freshness window <= 31. That is the correct bias: better to drop a stale-looking role
    than to date it optimistically and surface something closed.
    """
    if not posted_on:
        return None
    t = norm(posted_on)
    if "today" in t or "just posted" in t:
        days = 0
    elif "yesterday" in t:
        days = 1
    else:
        m = re.search(r"(\d+)\s*(day|week|month)", t)
        if not m:
            return None
        n = int(m.group(1))
        days = n * {"day": 1, "week": 7, "month": 30}[m.group(2)]
        if "30" in t and "+" in (posted_on or ""):
            days = 31
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def parse_ashby(company, slug, tier, raw) -> list[Job]:
    out = []
    for j in raw.get("jobs", []):
        if j.get("isListed") is False:
            continue
        posted = iso_date(j.get("publishedAt"))
        url = j.get("jobUrl") or j.get("applyUrl") or ""
        # Ashby is the tidy one: a real ISO-ish country on the posting itself.
        country = (((j.get("address") or {}).get("postalAddress") or {})
                   .get("addressCountry") or "")
        hint = "; ".join(x for x in ([country] + list(j.get("secondaryLocations") or [])
                                     ) if isinstance(x, str) and x)
        out.append(Job(company, j.get("title", ""), j.get("location", ""), url,
                       "ashby", slug, str(j.get("id", "")), posted, None, tier, hint))
    return out


# ------------------------------------------------------------- classify -----
def classify_field(title: str, fields_cfg: dict) -> str | None:
    # Word-bounded with plural tolerance ("llm" matches "LLMs"), NOT a raw substring:
    # bare `in` made "quant" match "Quantum Computing" and "ai" hide inside other words.
    t = norm(title)
    for fld in FIELD_PRIORITY:
        for kw in fields_cfg.get(fld, []):
            p = norm(kw)
            if p and re.search(rf"\b{re.escape(p)}s?\b", t):
                return fld
    return None


def passes_seniority(title: str, cfg: dict) -> bool:
    return not any(has_word(title, bad) for bad in cfg.get("exclude", []))


def location_verdict(location: str, cfg: dict) -> str:
    """'us' | 'excluded' | 'unknown' — why a location passed or failed us_only.

    exclude_locations alone was never enough: it is a DENYLIST, so every location that
    simply wasn't on it passed. That is how "Reykjavik" and bare "Hybrid" reached the
    queue. The denylist still runs first (it is precise and Parth curates it), but a
    location now has to show a POSITIVE US signal to be kept.
    """
    if not cfg.get("us_only"):
        return "us"
    loc = norm(location)
    if not loc:
        return "us"  # no location field at all -> keep, liveness/manual will catch

    # Word-bounded (has_word), NOT substring: "india" was dropping Indianapolis/Indiana,
    # "uk" was dropping Milwaukee, "dublin" Dublin CA — real US roles silently lost.
    if any(has_word(loc, bad) for bad in cfg.get("exclude_locations", [])):
        return "excluded"

    allow = (US_MARKERS + US_STATE_NAMES + US_CITIES
             + tuple(cfg.get("include_locations", []) or ()))
    if any(has_word(loc, good) for good in allow):
        return "us"
    # Abbreviations are checked against the RAW string so their casing still means
    # something — see the US_STATE_ABBR note.
    if any(re.search(rf"\b{ab}\b", location or "") for ab in US_STATE_ABBR):
        return "us"
    return "unknown"


def passes_location(location: str, cfg: dict) -> bool:
    v = location_verdict(location, cfg)
    if v == "unknown":
        # Default drop: a location we can't place is usually foreign or not a location
        # at all ("Hybrid"). Set `unknown_locations: keep` in filters.yaml to invert.
        return str(cfg.get("unknown_locations", "drop")).lower() == "keep"
    return v == "us"


def resolved_verdict(location: str, hint: str, cfg: dict) -> str:
    """location_verdict, with a second look at the hint when the location won't place.

    The primary location always wins when it is conclusive — a hint never overrides a
    real answer. The hint is consulted ONLY for the "unknown" bucket ("Hybrid",
    "In-Office", "Remote", "N/A"), and it is judged by the same rules, so a hint of
    "Remote India" comes back `excluded` and one of "United States" comes back `us`.
    Gated on `keep_unknown_if_us` so the old drop-everything behaviour stays available.
    """
    v = location_verdict(location, cfg)
    if v != "unknown" or not hint:
        return v
    if not cfg.get("keep_unknown_if_us", True):
        return v
    # location_verdict treats an empty string as "keep"; we already know hint is non-empty.
    return location_verdict(hint, cfg)


def is_fresh(posted: str | None, days: int) -> bool:
    if not posted:
        return True  # unknown date -> don't drop
    try:
        d = datetime.fromisoformat(posted)
    except ValueError:
        return True
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d >= datetime.now(timezone.utc) - timedelta(days=days)


# ----------------------------------------------------------------- fetch ----
def _get(url, **kw):
    for attempt in range(3):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=20, **kw)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return r
        except requests.RequestException:
            pass
        time.sleep(1.5 * (attempt + 1))
    return None


def _post(url, payload):
    for attempt in range(3):
        try:
            r = requests.post(url, headers={"User-Agent": UA, "Accept": "application/json",
                                            "Content-Type": "application/json"},
                              json=payload, timeout=25)
            if r.status_code in (200, 404):
                return r
        except requests.RequestException:
            pass
        time.sleep(1.5 * (attempt + 1))
    return None


# --- Workday -----------------------------------------------------------------
# Workday's public "CXS" JSON API. No auth, no key. Two endpoints:
#   POST /wday/cxs/<tenant>/<board>/jobs           -> a page of postings (limit caps at 20)
#   GET  /wday/cxs/<tenant>/<board><externalPath>  -> one posting, in full
# The list page is deliberately thin: `locationsText` is often just "3 Locations" and
# `postedOn` is prose ("Posted 3 Days Ago"), so neither the US filter nor the freshness
# filter can run on it honestly. The detail call returns a real `location`, an
# `additionalLocations` array, a `country`, and a true ISO `startDate`.
#
# So this is TWO-PHASE on purpose: page the whole board cheaply, drop everything whose
# TITLE already fails field/seniority (that is ~90% of a 2000-posting bank board), and
# only then spend one detail request per survivor. Filtering on the title first is what
# keeps Citi from costing 2000 extra round-trips.
WORKDAY_PAGE = 20          # hard server-side cap; limit=50 returns HTTP 400
WORKDAY_MAX = 1500         # per-company posting ceiling, override with `max_postings`


def _workday_list(host: str, board: str, tenant: str, cap: int) -> list[dict]:
    url = f"https://{host}/wday/cxs/{tenant}/{board}/jobs"
    out, offset, total = [], 0, None
    while offset < cap:
        r = _post(url, {"appliedFacets": {}, "limit": WORKDAY_PAGE,
                        "offset": offset, "searchText": ""})
        if not r or r.status_code != 200:
            break
        try:
            data = r.json()
        except ValueError:
            break
        page = data.get("jobPostings") or []
        out.extend(page)
        # `total` is only populated on the FIRST page — every later page reports 0. Reading
        # it each time made `offset >= total` true on page 2 and silently capped every
        # board at 40 postings. Latch it once; a short page is the real terminator.
        if total is None:
            total = data.get("total") or 0
        offset += WORKDAY_PAGE
        if len(page) < WORKDAY_PAGE or (total and offset >= total):
            break
        time.sleep(0.25)
    return out


def _workday_detail(host: str, board: str, tenant: str, path: str) -> dict | None:
    r = _get(f"https://{host}/wday/cxs/{tenant}/{board}{path}")
    if not r or r.status_code != 200:
        return None
    try:
        return r.json().get("jobPostingInfo") or {}
    except ValueError:
        return None


def fetch_workday(c: dict, fields_cfg: dict, filt: dict | None) -> list[Job]:
    """`host` + `board` in companies.yaml, e.g.
       {name: Adobe, ats: workday, host: adobe.wd5.myworkdayjobs.com,
        board: external_experienced, tier: pipeline}
    """
    name = c.get("name", "?")
    host, board = c.get("host"), c.get("board")
    tier = c.get("tier", "pipeline")
    if not host or not board:
        print(f"  ! {name}: workday entry needs both `host` and `board` — skipped", file=sys.stderr)
        return []
    tenant = c.get("tenant") or host.split(".")[0]
    cap = int(c.get("max_postings") or WORKDAY_MAX)

    listings = _workday_list(host, board, tenant, cap)
    if not listings:
        print(f"  ! {name} (workday/{tenant}/{board}) returned no postings", file=sys.stderr)
        return []

    sen_cfg = (filt or {}).get("seniority", {})
    # Title-only prefilter. Same predicates run again in run() against the real record;
    # this pass exists purely to decide which postings are worth a detail request.
    candidates = []
    for p in listings:
        title = p.get("title") or ""
        if classify_field(title, fields_cfg) is None:
            continue
        if sen_cfg and not passes_seniority(title, sen_cfg):
            continue
        candidates.append(p)

    print(f"    workday: {len(listings)} postings -> {len(candidates)} title-matches "
          f"-> fetching detail", file=sys.stderr)

    out = []
    for p in candidates:
        path = p.get("externalPath") or ""
        bullets = p.get("bulletFields") or []
        job_id = str(bullets[0]) if bullets else path.rsplit("_", 1)[-1]
        info = _workday_detail(host, board, tenant, path) if path else None
        hint = ""
        if info:
            locs = [info.get("location") or ""] + list(info.get("additionalLocations") or [])
            location = "; ".join(x for x in locs if x)
            country = ((info.get("country") or {}).get("descriptor") or "")
            if country and "united states" not in country.lower():
                # Explicit country beats string-sniffing: a role stamped India is India
                # however its city reads. Only skip when the API actually told us.
                continue
            hint = country
            posted = iso_date(info.get("startDate")) or parse_workday_relative(info.get("postedOn"))
            url = info.get("externalUrl") or f"https://{host}/{board}{path}"
            title = info.get("title") or p.get("title") or ""
            job_id = str(info.get("jobReqId") or job_id)
        else:
            # detail unavailable -> fall back to the thin list record rather than drop
            location = p.get("locationsText") or ""
            posted = parse_workday_relative(p.get("postedOn"))
            url = f"https://{host}/{board}{path}"
            title = p.get("title") or ""
        out.append(Job(name, title, location, url, "workday", f"{tenant}/{board}",
                       job_id, posted, None, tier, hint))
        time.sleep(0.2)

    for j in out:
        j.field = classify_field(j.title, fields_cfg)
        j.keyed()
    return out


def fetch_company(c: dict, fields_cfg: dict, filt: dict | None = None) -> list[Job]:
    ats = c["ats"]
    tier = c.get("tier", "pipeline")
    name = c.get("name", c.get("slug", "?"))
    if ats == "workday":
        return fetch_workday(c, fields_cfg, filt)
    slug = c["slug"]
    urls = {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
        "lever":      f"https://api.lever.co/v0/postings/{slug}?mode=json",
        "ashby":      f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false",
    }
    if ats not in urls:  # a typo'd ats: must cost one company, not the whole daily run
        print(f"  ! {name}: unknown ats '{ats}' (want greenhouse/lever/ashby/workday) — skipped",
              file=sys.stderr)
        return []
    r = _get(urls[ats])
    if not r or r.status_code != 200:
        print(f"  ! {name} ({ats}/{slug}) fetch failed", file=sys.stderr)
        return []
    data = r.json()
    parser = {"greenhouse": parse_greenhouse, "lever": parse_lever, "ashby": parse_ashby}[ats]
    jobs = parser(name, slug, tier, data)
    for j in jobs:
        j.field = classify_field(j.title, fields_cfg)
        j.keyed()
    return jobs


# Ashby has no per-job liveness endpoint, and its jobUrl is an SPA shell that returns 200
# whether or not the posting is open — fetching it proves nothing. Re-fetch the board's
# JSON once per slug and check the job id is still listed. None = board fetch failed ->
# unknown -> keep the job (same "don't drop on a network blip" spirit as is_fresh).
_ASHBY_LIVE_IDS: dict[str, set[str] | None] = {}


def _ashby_live_ids(slug: str) -> set[str] | None:
    if slug not in _ASHBY_LIVE_IDS:
        r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false")
        ids: set[str] | None = None
        if r and r.status_code == 200:
            try:
                ids = {str(j.get("id")) for j in r.json().get("jobs", [])
                       if j.get("isListed") is not False}
            except (ValueError, AttributeError):
                ids = None
        _ASHBY_LIVE_IDS[slug] = ids
    return _ASHBY_LIVE_IDS[slug]


def verify_live(j: Job) -> bool:
    if j.ats == "ashby":
        ids = _ashby_live_ids(j.ats_slug)
        return True if ids is None else j.ats_job_id in ids
    if j.ats == "workday":
        # The detail endpoint 404s once a requisition closes, so the job url is a real
        # liveness probe here (unlike Ashby's SPA shell). Network failure -> keep.
        r = _get(j.url)
        return True if r is None else r.status_code == 200
    urls = {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{j.ats_slug}/jobs/{j.ats_job_id}",
        "lever":      f"https://api.lever.co/v0/postings/{j.ats_slug}/{j.ats_job_id}?mode=json",
    }
    r = _get(urls[j.ats])
    return bool(r and r.status_code == 200)


# ------------------------------------------------------------------ main ----
def load_yaml(p: Path):
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def run(args):
    companies = load_yaml(Path(args.companies))["companies"]
    filt = load_yaml(Path(args.filters))
    fields_cfg = filt["fields"]

    applied_path = Path(args.applied)
    applied = set()
    if applied_path.exists():
        applied = {e if isinstance(e, str) else e.get("dedup_key")
                   for e in json.loads(applied_path.read_text(encoding="utf-8"))}

    all_jobs: list[Job] = []
    for c in companies:
        print(f"- {c.get('name', c.get('slug', '?'))} [{c.get('ats', '?')}]")
        try:
            all_jobs.extend(fetch_company(c, fields_cfg, filt))
        except Exception as e:  # one malformed entry must not kill the whole daily run
            print(f"  ! {c.get('name', c.get('slug', '?'))}: {e}", file=sys.stderr)
        time.sleep(0.5)

    # filter
    kept, seen, rescued = [], set(), []
    unplaceable: dict[str, int] = {}   # location -> count, so the lists can be extended
    for j in all_jobs:
        if j.field is None:                                   continue
        if not passes_seniority(j.title, filt["seniority"]):  continue
        verdict = resolved_verdict(j.location, j.location_hint, filt["location"])
        if verdict == "excluded":
            continue
        if verdict == "unknown":
            # Still unplaceable even after the hint — these are the ones worth eyeballing.
            unplaceable[j.location] = unplaceable.get(j.location, 0) + 1
            if str(filt["location"].get("unknown_locations", "drop")).lower() != "keep":
                continue
        if verdict == "us" and location_verdict(j.location, filt["location"]) == "unknown":
            rescued.append(j)
        if not is_fresh(j.posted, filt.get("freshness_days", 14)): continue
        if j.dedup_key in applied:                            continue
        if j.dedup_key in seen:                               continue
        seen.add(j.dedup_key)
        kept.append(j)

    kept.sort(key=lambda x: x.posted or "", reverse=True)  # freshest first

    if rescued:
        # These were kept on secondary evidence, not on their own location string, so
        # print what convinced us. A wrong hint should be obvious here, not invisible.
        print(f"\nkept {len(rescued)} role(s) whose location was unplaceable but whose "
              f"board data says US:", file=sys.stderr)
        for j in rescued[:15]:
            print(f"  {j.company}: {j.location!r} -> {j.location_hint!r}", file=sys.stderr)
        if len(rescued) > 15:
            print(f"  ... and {len(rescued) - 15} more", file=sys.stderr)

    if unplaceable:
        # A US role landing here is a gap in US_CITIES / include_locations, not noise.
        print(f"\ncouldn't place {sum(unplaceable.values())} location(s) as US "
              f"(add real ones to location.include_locations in filters.yaml):",
              file=sys.stderr)
        for loc, n in sorted(unplaceable.items(), key=lambda kv: -kv[1])[:15]:
            print(f"  {n:>3}x {loc!r}", file=sys.stderr)

    if args.verify_liveness:
        checked = []
        for j in kept[: args.limit or len(kept)]:
            if verify_live(j):
                checked.append(j)
            time.sleep(0.3)
        kept = checked

    pipeline = [j for j in kept if j.tier == "pipeline"]
    dream = [j for j in kept if j.tier == "dream"]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "queue.json").write_text(
        json.dumps([asdict(j) for j in pipeline], indent=2), encoding="utf-8")
    # dream.json — discovered dream roles the auto-apply loop skips. Same shape as
    # queue.json so the web console can surface them as a manual-only pile (apply by
    # hand / referral first); the extension never reads this file.
    (out / "dream.json").write_text(
        json.dumps([asdict(j) for j in dream], indent=2), encoding="utf-8")
    (out / f"raw_{datetime.now():%Y%m%d}.json").write_text(
        json.dumps([asdict(j) for j in all_jobs], indent=2), encoding="utf-8")
    (out / "digest.md").write_text(render_digest(pipeline, dream), encoding="utf-8")
    (out / "digest.html").write_text(render_digest_html(pipeline, dream), encoding="utf-8")
    print(f"\n{len(pipeline)} pipeline + {len(dream)} dream roles -> {out}/queue.json")


def render_digest(pipeline: list[Job], dream: list[Job]) -> str:
    lines = [f"# Job queue — {datetime.now():%Y-%m-%d}", ""]
    for fld in FIELD_PRIORITY:
        group = [j for j in pipeline if j.field == fld]
        if not group:
            continue
        lines.append(f"## {fld.upper()} ({len(group)})")
        for j in group:
            date = (j.posted or "")[:10]
            lines.append(f"- [{j.title}]({j.url}) — {j.company} · {j.location} · {date}")
        lines.append("")
    if dream:
        lines.append("## ⭐ DREAM")
        lines.append("> ⚠️ The auto-apply loop skips these — apply by hand (referral first).")
        for j in dream:
            lines.append(f"- [{j.title}]({j.url}) — {j.company} · {j.field}")
    return "\n".join(lines)


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_digest_html(pipeline: list[Job], dream: list[Job]) -> str:
    """Email-safe HTML (inline styles) so the digest renders formatted, not as raw markdown."""
    P = "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
    parts = [
        f'<div style="{P}max-width:720px;margin:0 auto;color:#1a1a1a;line-height:1.5;">',
        f'<h1 style="font-size:22px;margin:0 0 4px;">Job queue</h1>',
        f'<div style="color:#888;font-size:13px;margin-bottom:20px;">{datetime.now():%A, %d %b %Y} · '
        f'{len(pipeline)} pipeline · {len(dream)} dream</div>',
    ]

    def item(j: "Job", show_field: bool) -> str:
        date = (j.posted or "")[:10]
        meta = f"{_esc(j.company)} · {_esc(j.location)}" if not show_field else \
               f"{_esc(j.company)} · {(j.field or '').upper()}"
        tail = f" · {date}" if date and not show_field else ""
        return (f'<li style="margin:0 0 10px;">'
                f'<a href="{_esc(j.url)}" style="color:#2563eb;text-decoration:none;font-weight:600;">'
                f'{_esc(j.title)}</a><br>'
                f'<span style="color:#666;font-size:13px;">{meta}{tail}</span></li>')

    for fld in FIELD_PRIORITY:
        group = [j for j in pipeline if j.field == fld]
        if not group:
            continue
        parts.append(f'<h2 style="font-size:15px;border-bottom:1px solid #eee;padding-bottom:6px;'
                     f'margin:24px 0 12px;">{fld.upper()} <span style="color:#aaa;font-weight:400;">'
                     f'({len(group)})</span></h2>')
        parts.append('<ul style="list-style:none;padding:0;margin:0;">')
        parts += [item(j, False) for j in group]
        parts.append('</ul>')

    if dream:
        parts.append(f'<h2 style="font-size:15px;border-bottom:1px solid #eee;padding-bottom:6px;'
                     f'margin:28px 0 12px;">⭐ DREAM <span style="color:#aaa;font-weight:400;">'
                     f'({len(dream)})</span></h2>')
        parts.append('<div style="background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;'
                     'border-radius:6px;padding:10px 12px;font-size:13px;margin:0 0 12px;">'
                     '⚠️ <b>The auto-apply loop skips these.</b> Apply to them by hand '
                     '(referral first) — they are not in the queue the extension works.</div>')
        parts.append('<ul style="list-style:none;padding:0;margin:0;">')
        parts += [item(j, True) for j in dream]
        parts.append('</ul>')

    parts.append('</div>')
    return "\n".join(parts)


# --------------------------------------------------------------- selftest ---
def selftest():
    fields = {"quant": ["quant", "quantitative"],
              "ai": ["machine learning", "ml engineer", "applied scientist"],
              "ds": ["data scientist", "data science"],
              "tech": ["software engineer", "backend"]}
    # Dates are computed, never hardcoded: a literal fixture date goes stale and the
    # freshness assert below then aborts the selftest, masking every check after it.
    _recent = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh = {"jobs": [{"id": 1, "title": "Software Engineer, Backend",
                    "location": {"name": "New York, NY"},
                    "absolute_url": "http://x/1", "first_published": _recent},
                   {"id": 2, "title": "Senior Data Scientist",
                    "location": {"name": "London, UK"},
                    "absolute_url": "http://x/2", "updated_at": _recent}]}
    jobs = parse_greenhouse("Acme", "acme", "pipeline", gh)
    for j in jobs:
        j.field = classify_field(j.title, fields); j.keyed()
    seniority = {"exclude": ["senior", "staff", "principal", "manager", "director"]}
    location = {"us_only": True, "exclude_locations": ["london", "uk", "india"]}
    assert jobs[0].field == "tech", jobs[0].field
    assert jobs[1].field == "ds"
    assert passes_seniority(jobs[0].title, seniority) is True
    assert passes_seniority(jobs[1].title, seniority) is False       # "Senior"
    assert passes_location(jobs[0].location, location) is True
    assert passes_location(jobs[1].location, location) is False      # London
    assert is_fresh(jobs[0].posted, 14) is True
    assert is_fresh("2026-01-01T00:00:00+00:00", 14) is False
    assert classify_field("Quantitative Researcher", fields) == "quant"
    assert classify_field("Machine Learning Engineer", fields) == "ai"
    assert classify_field("Recruiter", fields) is None
    # word-bounded classification: no substring leaks, plural still matches
    assert classify_field("Quantum Computing Engineer", fields) is None      # quant ⊄ quantum
    fields_llm = {**fields, "ai": fields["ai"] + ["llm"]}
    assert classify_field("Software Engineer, LLMs", fields_llm) == "ai"     # plural tolerance
    # word-bounded location exclusion: US cities that CONTAIN an excluded word must survive
    assert passes_location("Indianapolis, IN", location) is True             # "india" ⊄
    assert passes_location("Milwaukee, WI", location) is True                # "uk" ⊄
    assert passes_location("Bengaluru, India", location) is False
    assert passes_location("Remote - UK", {"us_only": True, "exclude_locations": ["uk"]}) is False
    # us_only is an ALLOWLIST: a location off the denylist is no longer automatically US.
    # These two shapes are what actually leaked into a live queue.
    assert location_verdict("Reykjavik, Iceland", location) == "unknown"
    assert passes_location("Reykjavik, Iceland", location) is False
    assert passes_location("Hybrid", location) is False
    assert location_verdict("Zurich", location) == "unknown"
    assert passes_location("Remote", location) is False       # ambiguous, not a US signal
    # ...but every real US shape still passes
    for good in ("New York, NY", "San Francisco", "Remote - US", "United States",
                 "Austin, Texas", "Cambridge, MA", "NYC", "Remote (USA)",
                 "Boston, Massachusetts", "Seattle, WA or Remote"):
        assert passes_location(good, location) is True, good
    # abbreviations only count with their real casing, else "Remote in Europe" == Indiana
    assert passes_location("Remote in Europe", location) is False
    assert passes_location("Portland, OR", location) is True
    # the denylist still wins over an allow signal (a US-named company office abroad)
    assert passes_location("London, UK", {**location, "include_locations": ["london"]}) is False
    # escape hatches
    assert passes_location("Reykjavik", {**location, "unknown_locations": "keep"}) is True
    assert passes_location("Zurich", {**location, "include_locations": ["zurich"]}) is True
    assert passes_location("Anywhere", {"us_only": False}) is True
    assert passes_location("", location) is True              # no location field -> keep
    assert jobs[0].dedup_key == dedup_key("Acme", "Software Engineer, Backend", "New York, NY")

    # --- location hints: rescue ambiguous locations, but only on US evidence --
    assert resolved_verdict("Hybrid", "United States", location) == "us"
    assert resolved_verdict("N/A", "San Francisco, CA", location) == "us"
    # the real Cloudflare case: location says "In-Office", offices say Remote India
    assert resolved_verdict("In-Office", "Remote India", location) == "excluded"
    assert resolved_verdict("N/A", "Canada Locations", {**location,
                                                        "exclude_locations": ["canada"]}) == "excluded"
    assert resolved_verdict("Remote", "", location) == "unknown"        # no hint -> unchanged
    assert resolved_verdict("Remote", "Zurich", location) == "unknown"  # hint no help
    # a conclusive primary location is never overridden by a hint
    assert resolved_verdict("London, UK", "United States", location) == "excluded"
    assert resolved_verdict("Austin, TX", "Remote India", location) == "us"
    # opt-out restores the old drop-everything behaviour
    assert resolved_verdict("Hybrid", "United States",
                            {**location, "keep_unknown_if_us": False}) == "unknown"
    # hint extraction from the two real Greenhouse shapes
    assert greenhouse_location_hint(
        {"offices": [{"name": "Remote India"}]}) == "Remote India"
    assert greenhouse_location_hint(
        {"metadata": [{"name": "Job Posting Location", "value": ["Bengaluru, India"]}]}
    ) == "Bengaluru, India"
    assert greenhouse_location_hint({"offices": [], "metadata": None}) == ""
    assert greenhouse_location_hint(
        {"metadata": [{"name": "Cost Center", "value": "6300 - Accounting"}]}) == ""
    _ash = parse_ashby("Acme", "acme", "pipeline", {"jobs": [
        {"id": 9, "title": "Software Engineer", "location": "Remote",
         "address": {"postalAddress": {"addressCountry": "United States"}}}]})
    assert _ash[0].location_hint == "United States"
    assert resolved_verdict(_ash[0].location, _ash[0].location_hint, location) == "us"

    # --- workday relative-date parsing ---------------------------------------
    def _age(s):
        iso = parse_workday_relative(s)
        return None if iso is None else (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).days
    assert _age("Posted Today") == 0
    assert _age("Posted Yesterday") == 1
    assert _age("Posted 3 Days Ago") == 3
    assert _age("Posted 2 Weeks Ago") == 14
    assert _age("Posted 30+ Days Ago") == 31          # terminal bucket -> "at least 31"
    assert _age(None) is None and _age("Posted recently") is None
    # a "30+ Days Ago" role must fall out of a 30-day window, never sneak in as fresh
    assert is_fresh(parse_workday_relative("Posted 30+ Days Ago"), 30) is False
    assert is_fresh(parse_workday_relative("Posted 3 Days Ago"), 30) is True
    print("selftest OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--companies", default=str(paths.COMPANIES))
    ap.add_argument("--filters", default=str(paths.FILTERS))
    ap.add_argument("--applied", default=str(paths.APPLIED))
    ap.add_argument("--out", default=str(paths.OUT))
    ap.add_argument("--verify-liveness", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    else:
        run(a)
