#!/usr/bin/env python3
"""
Outreach contact discovery — find people worth messaging about a role.

Phase 1 of `docs/outreach_plan.md`: given a job from the queue, return 2-3 named people to
reach out to on LinkedIn. Drafting the message and the Telegram/console surfaces are later
phases; this module only finds and ranks.

WHY GOOGLE AND NOT LINKEDIN
---------------------------
LinkedIn has no API that can search for people — profile lookup and people search are
partner-gated and are not granted to individuals (verified 2026-08-04, treat as permanent).
Scraping LinkedIn while logged in risks the account, which is the same reasoning that made
the scraper API-only. So every query here runs against a general web search index over
*public* profile pages. This module never authenticates to LinkedIn, never opens a profile,
and never sends anything. Parth reads the shortlist and messages people himself.

THE TITLE FILTER (the rule the whole module turns on)
-----------------------------------------------------
A search result renders as `Name - Headline | LinkedIn`, and the headline is the person's
CURRENT employer. Measured 2026-08-04: an alum who has left renders as `Marco Ferreira - Google
| LinkedIn` with "ex-Northwind" only in the snippet body. So a raw "<company> <school>"
query returns ~80% former employees, who cannot refer anyone and are therefore worthless.

    Require the company in the result TITLE, not anywhere on the page.

It is applied as a post-filter rather than only as an `intitle:` operator because operators
are honoured inconsistently between engines, and a post-filter is deterministic.

The pure functions (parsing, filtering, scoring, query building) are unit-testable offline
via --selftest; only `search()` needs the network or an API key.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass, field, asdict

try:
    import requests
except ImportError:            # allow --selftest without network deps
    requests = None

from core import paths
from server.store import get_store

UA = "job-copilot/2.0 (personal job search)"

# Google Programmable Search JSON API: 100 queries/day free, which covers ~20 new roles a
# day at 3 queries each. Both values come from the environment — the CX identifies a search
# engine configured to search the whole web, and neither belongs in a public repo.
PSE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
PSE_KEY_ENV = "GOOGLE_PSE_KEY"
PSE_CX_ENV = "GOOGLE_PSE_CX"

# The signals a contact can match. A person is scored on ALL of them, not sorted into one:
# someone who is both a former colleague and on the hiring team is a categorically better contact
# than either signal alone, and a bucket-per-person model cannot express that person.
SIGNAL_WEIGHTS = {
    "alumni_school":   1.00,   # highest reply rate; the reason to build this at all
    "alumni_employer": 0.90,   # a former colleague: same firm, same work, real shared ground
    "team":            0.80,   # the hiring manager or someone who would work with him
    "recruiter":       0.55,   # lowest reply rate, but the only one who can move an application
}

# How much a country mismatch costs, per signal. Recruiters are region-scoped by job
# function, so one in the wrong country is close to useless. Alumni are NOT filtered on
# location at all: a referral travels inside a company regardless of office, so an alum in
# Bangalore can still refer a US req, and dropping them would discard some of the best
# contacts available.
# Measured 2026-08-04: the school signal returns overwhelmingly India-based people (7 of 8
# on one company), while team and recruiter for the SAME company came back US-heavy. That is
# base rate, not a geolocation bias in the search API — the school is an Indian institution,
# so that is where its alumni are.
#
# The alumni penalty is small ON PURPOSE and the size is load-bearing. Parth's call
# (2026-08-04): an alum is worth more regardless of where they sit, because the referral
# itself is the high-value action and a company's internal referral system does not care
# which office anyone is in. So it must only ever break ties BETWEEN alumni:
#
#     distant alum   1.00 - 0.10 = 0.90   still ahead of
#     same-country team contact   0.80
#
# Raising it past ~0.20 would invert that and demote alumni below team contacts, which is
# the opposite of the intent. `test: an alum abroad still beats a same-country recruiter`
# and its sibling in the selftest pin both halves.
LOCATION_PENALTY = {
    "recruiter":       0.45,
    "team":            0.20,
    "alumni_school":   0.10,
    "alumni_employer": 0.10,
}

# Titles that mention a company without working for it, or people who cannot help.
EXCLUDE_HEADLINE = re.compile(
    r"(?i)\b(ex[- ]|former(ly)?\b|staffing|recruitment agency|talent partner at "
    r"|headhunt|account executive|sales development|sdr\b|business development)")

# Seniority words that suggest the person owns a team (and so, possibly, the req).
SENIOR = re.compile(r"(?i)\b(head|director|vp|vice president|manager|lead|principal|chief)\b")

# Generic words that make a query worse rather than more specific.
_STOP = {"senior", "staff", "principal", "software", "engineer", "engineering", "developer",
         "the", "and", "for", "with", "you", "our", "team", "teams", "role", "job", "jr",
         "sr", "i", "ii", "iii", "iv", "l2", "l3", "l4", "l5", "new", "grad"}


@dataclass
class Contact:
    name: str
    headline: str
    profile_url: str
    country: str = ""                       # from the URL subdomain; "" = unknown/global
    signals: list[str] = field(default_factory=list)
    score: float = 0.0

    def __post_init__(self):
        # Derive the country from the URL rather than trusting every caller to remember.
        # It is free evidence sitting in a field we already have, and a Contact built
        # without it scores as location-unknown — which silently disables the whole
        # location weighting instead of failing loudly.
        if not self.country:
            self.country = country_of(self.profile_url)

    def key(self) -> str:
        """Identity for dedupe. The profile URL, normalised — the same person surfaces from
        several queries and for several roles, and inviting them twice is the one mistake
        that costs a real relationship."""
        return normalize_profile_url(self.profile_url)


# ------------------------------------------------------------------ parsing ---
def normalize_profile_url(url: str) -> str:
    """`https://in.linkedin.com/in/example-foo/?trk=x` -> `linkedin.com/in/example-foo`.

    Strips scheme, the country subdomain, query and trailing slash, so the same person found
    via `www.` and via `in.` dedupes to one row.
    """
    u = str(url or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^[a-z]{2,3}\.linkedin\.com", "linkedin.com", u)
    u = re.sub(r"^www\.linkedin\.com", "linkedin.com", u)
    u = u.split("?")[0].split("#")[0].rstrip("/")
    return u


def country_of(url: str) -> str:
    """The country hint LinkedIn puts in the profile URL's subdomain, or "".

    `in.linkedin.com/in/...` is India, `ca.linkedin.com/...` Canada, plain `www.` is the
    default (usually US, but genuinely unmarked — so it is scored as "no evidence", never as
    "confirmed US"). Free with every result, no extra query. It reflects the profile's
    locale rather than strict residence, so it is good-not-certain evidence and is only ever
    used to WEIGHT a contact, never to drop one.
    """
    u = str(url or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    m = re.match(r"^([a-z]{2,3})\.linkedin\.com", u)
    if not m or m.group(1) == "www":
        return ""
    return m.group(1)


def split_title(title: str) -> tuple[str, str]:
    """`"Priya N. - Technical Recruiting at Northwind | LinkedIn"` -> (name, headline).

    Google's separator is an en dash on some results and a hyphen on others, and the
    `| LinkedIn` suffix is sometimes absent, so all three are optional.
    """
    t = re.sub(r"\s*\|\s*LinkedIn\s*$", "", str(title or "").strip(), flags=re.I)
    parts = re.split(r"\s+[-–—]\s+", t, maxsplit=1)
    name = parts[0].strip()
    headline = parts[1].strip() if len(parts) > 1 else ""
    return name, headline


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower())


def title_has_company(headline: str, company: str) -> bool:
    """THE title filter: does this person CURRENTLY work at the company?

    True only if the company name appears in the headline (the part of the result title
    after the person's name), which is where the current employer lives. A match anywhere
    else on the page means they mentioned it, used to work there, or wrote a post about it.

    Matched on word boundaries over a normalised string so `Northwind` does not match
    `Robin Ellery` — a real false positive observed in the 2026-08-04 run.
    """
    co = _norm(company).strip()
    if not co:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(co)}(?![a-z0-9])", _norm(headline)) is not None


def parse_profile_card(content: str) -> dict | None:
    """Parse the structured profile card some providers return instead of a snippet.

    Tavily (and any provider that scrapes the page rather than quoting the SERP) returns a
    rendered profile, not a search excerpt:

        # Helenja Wolfaardt
        Robinhood                          <- current employer
        Toronto, Ontario, Canada, CA       <- location, ending in an ISO country code
        500 connections, 11245 followers
        ## About / ## Education / ...

    This is strictly better evidence than Google's `Name - Headline | LinkedIn` string: the
    employer is a FIELD rather than a substring of a title, the location is city-level
    instead of a URL-subdomain guess, and education is stated instead of hoped for. So it is
    used when present, and `split_title` remains the fallback for SERP-shaped providers.

    Returns None when the content is not card-shaped, which is what routes those providers
    to the fallback. Never raises.
    """
    text = str(content or "").strip()
    if not text.startswith("# "):
        return None
    # Only the head matters; everything from the first section onward is prose and activity.
    head = text.split("\n##", 1)[0].splitlines()
    lines = [ln.strip() for ln in head if ln.strip()]
    if len(lines) < 2:
        return None
    name = lines[0].lstrip("# ").strip()
    employer = lines[1] if len(lines) > 1 else ""
    location = ""
    for ln in lines[2:]:
        if re.match(r"(?i)^\d[\d,]* connections", ln):
            break
        if not location:
            location = ln
    # "N/A" is how these cards spell an empty section; treated as text it becomes a headline
    # reading "N/A", which is worse than having none.
    def _section(header: str, limit: int) -> str:
        m = re.search(rf"(?is)^##\s*{header}\s*\n(.+?)(?:\n##|\Z)", text, re.M)
        if not m:
            return ""
        v = " ".join(m.group(1).replace("#", " ").split())[:limit]
        return "" if v.strip().upper() in {"N/A", "NA", "NONE", "-"} else v

    about = _section("About", 400)
    education = _section("Education", 300)
    if not name:
        return None
    return {"name": name, "employer": employer, "location": location,
            "about": about, "education": education}


def country_from_location(location: str) -> str:
    """The ISO country code a profile-card location line ends with, lowercased, or "".

    "Toronto, Ontario, Canada, CA" -> "ca"; "Greater Seattle Area, US" -> "us". Far better
    evidence than the URL subdomain, which only reflects the profile's locale setting — this
    is the location the person states. Returns "" when the line has no trailing code, which
    keeps it "no evidence" rather than a guess.
    """
    parts = [p.strip() for p in str(location or "").split(",") if p.strip()]
    if not parts:
        return ""
    last = parts[-1]
    return last.lower() if re.fullmatch(r"[A-Z]{2}", last) else ""


def employer_is(employer_line: str, company: str) -> bool:
    """Does this profile-card employer line name the target company AS the employer?

    Equality after normalisation, plus a small allowance for the legal-suffix forms boards
    use ("Robinhood Markets, Inc."). Deliberately strict: on the card shape the line holds
    the employer and nothing else, and a substring test here is what let "Writer" match a
    freelance writer and "Harvey" match a surname (measured 2026-08-04).

    The allowance is capped at a short line so it cannot drift back into substring matching:
    "Sierra West Select at Cisco" names a different employer and must fail.
    """
    line, co = _norm(employer_line).strip(), _norm(company).strip()
    if not line or not co:
        return False
    if line == co:
        return True
    toks = line.split()
    if len(toks) > 4 or not toks:
        return False
    # "<company> markets inc" / "<company> technologies" — must START with the company and
    # continue only into corporate-form words, never into a different name.
    if not line.startswith(co + " "):
        return False
    tail = set(line[len(co):].split())
    return tail <= {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
                    "markets", "technologies", "technology", "labs", "group", "holdings",
                    "software", "systems", "the", "and"}


def parse_result(item: dict, company: str) -> Contact | None:
    """One search-API result row -> a Contact, or None if it is not a usable person.

    Two input shapes, because providers differ (see `parse_profile_card`):

      * CARD  — the snippet is a scraped profile. The employer is a field, so the "does this
                person currently work there" test is a field comparison, and location comes
                from what they state rather than a URL subdomain.
      * SERP  — the snippet is a search excerpt and the title carries `Name - Headline`.
                The employer test falls back to substring-matching the headline.

    Rejects, in order: non-profile URLs (company pages, Wikipedia — a `/in/` path is what
    makes it a person), anything whose current employer is not the target company, and
    headlines on the exclusion list. All offline, which is why it is all testable.
    """
    url = str(item.get("link") or item.get("url") or "").strip()
    if not url or "/in/" not in normalize_profile_url(url):
        return None
    card = parse_profile_card(item.get("snippet") or item.get("content") or "")
    if card:
        # The employer line IS the current-employer claim, so test it EXACTLY rather than by
        # substring. Measured 2026-08-04: a substring test wrecks single-word company names,
        # because the word also occurs in ordinary headlines and surnames — "Writer" matched
        # a freelance content writer, "Harvey" matched somebody's surname, "Sierra" matched
        # a role at a different company entirely. On this shape the line is the employer and
        # nothing else ("Robinhood", "MongoDB"), so equality is both correct and strict.
        if not employer_is(card["employer"], company):
            return None
        headline = card["about"] or card["employer"]
        if EXCLUDE_HEADLINE.search(headline):
            return None
        return Contact(name=card["name"], headline=headline, profile_url=url,
                       country=country_from_location(card["location"]) or country_of(url))
    name, headline = split_title(item.get("title") or "")
    if not name or not headline:
        return None
    if not title_has_company(headline, company):
        return None
    if EXCLUDE_HEADLINE.search(headline):
        return None
    return Contact(name=name, headline=headline, profile_url=url, country=country_of(url))


# ------------------------------------------------------------- query ladder ---
def role_keywords(title: str, limit: int = 3) -> list[str]:
    """The distinctive words in a job title, longest first, generic ones dropped.

    "Senior Software Engineer, Data Lake" -> ["data", "lake"]. Searching the full title
    finds nobody (no one's headline reads like a req), and searching "software engineer"
    finds everybody, so the middle is what is wanted.
    """
    words = [w for w in _norm(title).split() if w not in _STOP and len(w) > 2]
    seen, out = set(), []
    for w in sorted(words, key=len, reverse=True):
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:limit]


def team_hints(description: str, limit: int = 2) -> list[str]:
    """Team names mined out of a job description.

    The single biggest relevance lever: postings routinely name the team ("you'll join the
    Payments Risk team") or the manager ("reporting to the Director of Quantitative
    Research"). Those strings make a `team` query specific enough to find actual teammates
    instead of anyone with a similar job title.

    Deliberately conservative — a wrong team name is worse than none, because it produces a
    confident query for a team that does not exist. Only fires on explicit phrasings.

    NOT YET EXERCISED IN ANGER: `find_contacts` takes the description from its caller and
    nothing supplies one yet, so the 2026-08-04 measurement ran on role keywords alone. See
    phase 2 in `docs/outreach_plan.md` — wiring `fetch_description()` in is what will first
    test these patterns against live postings.

    A model reading the posting would beat these four regexes at any unusual phrasing, and
    that is phase 5 in the plan. This stays as the fallback either way: it costs nothing,
    needs no bridge, and must keep working with drafting disabled.
    """
    text = re.sub(r"<[^>]+>", " ", str(description or ""))
    text = re.sub(r"\s+", " ", text)
    out: list[str] = []
    pats = (
        r"(?i)\bjoin(?:ing)? (?:the|our) ([A-Z][\w&/-]*(?: [A-Z][\w&/-]*){0,3}) team\b",
        r"(?i)\bthe ([A-Z][\w&/-]*(?: [A-Z][\w&/-]*){0,3}) team\b",
        r"(?i)\breport(?:ing|s)? to the ((?:Head|Director|VP|Manager|Lead)[\w ,&/-]{0,40}?)"
        r"(?:\.|,|;| who| and )",
        r"(?i)\bpart of (?:the|our) ([A-Z][\w&/-]*(?: [A-Z][\w&/-]*){0,3}) (?:team|org|group)\b",
    )
    for p in pats:
        for m in re.finditer(p, text):
            v = " ".join(m.group(1).split()).strip(" ,.")
            # One capitalised word like "Engineering" is the generic case, not a team name.
            if len(v) < 4 or v.lower() in _STOP or len(v.split()) < 2:
                continue
            if v.lower() not in {o.lower() for o in out}:
                out.append(v)
            if len(out) >= limit:
                return out
    return out


def build_queries(job: dict, hints: list[str] | None = None,
                  school: str = "", employer: str = "") -> list[tuple[str, str]]:
    """The query LADDER, as (signal, query) pairs in the order they should be tried.

    Same shape as fill.js's `searchCandidates()` for the school dropdown: most specific
    first, fall back until something returns rows. Specific queries match better but often
    return nothing; generic ones always return something but it is mush.

    `intitle:` is included where it helps even though the post-filter is what actually
    guarantees correctness — it improves the result set the engine chooses to return.
    """
    company = str(job.get("company") or "").strip()
    title = str(job.get("title") or "")
    out: list[tuple[str, str]] = []
    if not company:
        return out
    site = 'site:linkedin.com/in'
    co_q = f'"{company}"'
    intitle = f'intitle:"{company}"'

    # team: the strongest signal. Mined team names first, then role keywords, then bare
    # seniority at the company.
    seniority = '(Manager OR Lead OR Head OR Director)'
    for h in (hints or []):
        out.append(("team", f'{site} {intitle} "{h}"'))
    kws = role_keywords(title)
    if kws:
        out.append(("team", f'{site} {intitle} {" ".join(f0 for f0 in kws)} {seniority}'))
    out.append(("team", f'{site} {intitle} {seniority}'))

    # recruiter: works well as a single query, no ladder needed.
    out.append(("recruiter",
                f'{site} {intitle} ("technical recruiter" OR "talent acquisition" OR recruiting)'))

    # alumni: NOT intitle — the school/employer lives in the profile body, and forcing both
    # into the title returns nothing (measured). The title post-filter does the work here.
    if school:
        out.append(("alumni_school", f'{site} {co_q} "{school}"'))
    if employer:
        out.append(("alumni_employer", f'{site} {co_q} "{employer}"'))
    return out


# ----------------------------------------------------------------- scoring ---
def boost_terms(contact: dict | Contact, snippet: str, school: str, employer: str) -> list[str]:
    """Shared-background signals found in a contact's snippet, as extra signal names.

    This is how the shared-employer term earns its place. As a QUERY it failed — measured 2026-08-04,
    `"<company>" "Ardent Capital"` mostly matched people *discussing* the two companies, one
    genuine hit across two searches. Inverted it works: find people via the discovery
    queries, then check whether the term appears in what came back and boost them. Free, and
    it is exactly the overlap ranking the plan calls for.
    """
    text = _norm(f"{getattr(contact, 'headline', '')} {snippet}")
    found = []
    if school and _norm(school).strip() and _norm(school) in text:
        found.append("alumni_school")
    if employer and _norm(employer).strip() and _norm(employer) in text:
        found.append("alumni_employer")
    return found


def score(contact: Contact, job: dict) -> float:
    """Rank a contact for one role. Higher is better; roughly 0..1.5.

    Built from: the best signal it matched, a bonus for matching SEVERAL (the overlap rule),
    a seniority nudge for the team signal, and a country-mismatch penalty weighted per
    signal. Deliberately a small readable formula rather than anything learned — there are
    no labels to learn from, and Parth eyeballs the top 3 anyway.
    """
    if not contact.signals:
        return 0.0
    base = max(SIGNAL_WEIGHTS.get(s, 0.0) for s in contact.signals)
    # Overlap: each ADDITIONAL signal adds a fraction of its own weight. Someone who is both
    # a former colleague AND on the hiring team must outrank either signal alone.
    extra = sorted((SIGNAL_WEIGHTS.get(s, 0.0) for s in contact.signals), reverse=True)[1:]
    base += sum(w * 0.5 for w in extra)
    if "team" in contact.signals and SENIOR.search(contact.headline):
        base += 0.15
    # Location. "" (plain www.linkedin.com) is unmarked, not confirmed-US, so it is never
    # penalised — penalising unknowns would punish the majority of US profiles.
    want = _job_country(job)
    if contact.country and want and contact.country != want:
        worst = max(LOCATION_PENALTY.get(s, 0.0) for s in contact.signals)
        base -= worst
    return round(max(base, 0.0), 4)


def _job_country(job: dict) -> str:
    """The job's country as a LinkedIn-style code, or "" if it cannot be read.

    Only "us" is ever asserted, because that is the only market being targeted and the
    location strings ("Remote", "In-Office", "New York, NY") do not support anything finer.
    """
    blob = _norm(f"{job.get('location') or ''} {job.get('location_hint') or ''}")
    if re.search(r"\b(usa|united states|us|remote us)\b", blob):
        return "us"
    return ""


def rank(contacts: list[Contact], job: dict, top: int = 3) -> list[Contact]:
    """Dedupe by profile URL (merging signals), score, and keep the best few.

    Merging rather than dropping is the point of the dedupe: the same person arriving from
    the team query AND the alumni query is the strongest contact available, and keeping only
    the first-seen copy would silently discard that.
    """
    merged: dict[str, Contact] = {}
    for c in contacts:
        k = c.key()
        if not k:
            continue
        if k in merged:
            for s in c.signals:
                if s not in merged[k].signals:
                    merged[k].signals.append(s)
            # Keep whichever copy carries a country; "" is absence of evidence.
            if not merged[k].country and c.country:
                merged[k].country = c.country
        else:
            merged[k] = c
    out = list(merged.values())
    for c in out:
        c.score = score(c, job)
    out.sort(key=lambda c: (-c.score, c.name))
    return [c for c in out if c.score > 0][:top]


# --------------------------------------------------------------- searching ---
# WHY THIS IS PLUGGABLE
# ---------------------
# The web-search API market has broken three times in eighteen months: Bing Search retired
# outright (2025-08-11), Brave withdrew its free tier (2026-02), and Google's Custom Search
# JSON API closed to NEW customers with existing ones cut off 2027-01-01. The first draft of
# this module hardcoded Google PSE and was unusable on the day it was written.
#
# So the provider is one env var and one small adapter. Every adapter returns the same rows
# — {title, link, snippet} — because that is the shape `parse_result` needs, and it is the
# common denominator of every engine here. Adding the next one when this list rots again
# should be a ten-line function.
SEARCH_PROVIDER_ENV = "CONTACTS_SEARCH_PROVIDER"     # tavily | brave | exa | google
DEFAULT_PROVIDER = "tavily"

PROVIDERS: dict[str, dict] = {
    # Free tier ~1000 credits/month at time of writing — enough for the phase-1 measurement
    # (about 60 queries) and probably for real use at ~20 roles/day.
    "tavily": {"key_env": "TAVILY_API_KEY",
               "url": "https://api.tavily.com/search"},
    # No free tier for new accounts since 2026-02: metered, card required, no spend cap.
    "brave":  {"key_env": "BRAVE_API_KEY",
               "url": "https://api.search.brave.com/res/v1/web/search"},
    "exa":    {"key_env": "EXA_API_KEY",
               "url": "https://api.exa.ai/search"},
    # CLOSED TO NEW CUSTOMERS (verified 2026-08-04) and shut off entirely on 2027-01-01.
    # Kept only because an account that already had access still works until then.
    "google": {"key_env": PSE_KEY_ENV, "cx_env": PSE_CX_ENV, "url": PSE_ENDPOINT},
}


def strip_operators(query: str) -> str:
    """Drop Google-only search operators for engines that treat them as literal words.

    `site:` and `intitle:` are Google syntax. An engine that does not implement them does
    not ignore them either — it matches on the text, so the operator becomes noise that
    competes with the real terms. Providers with a native equivalent (Tavily's
    `include_domains`) get that instead.
    """
    q = str(query or "")
    # `site:` and its operand both go — the domain restriction is expressed natively.
    q = re.sub(r"\bsite:\S+", " ", q)
    # `intitle:`/`inurl:` lose only the PREFIX: the operand is the company name, and
    # dropping it too would remove the single most important term in the query.
    q = re.sub(r"\b(?:intitle|inurl):", "", q)
    return " ".join(q.split())


def _rows_google(data: dict) -> list[dict]:
    return list(data.get("items") or [])


def _rows_tavily(data: dict) -> list[dict]:
    return [{"title": r.get("title") or "", "link": r.get("url") or "",
             "snippet": r.get("content") or ""} for r in (data.get("results") or [])]


def _rows_brave(data: dict) -> list[dict]:
    return [{"title": r.get("title") or "", "link": r.get("url") or "",
             "snippet": r.get("description") or ""}
            for r in ((data.get("web") or {}).get("results") or [])]


def _rows_exa(data: dict) -> list[dict]:
    return [{"title": r.get("title") or "", "link": r.get("url") or "",
             "snippet": r.get("text") or r.get("summary") or ""}
            for r in (data.get("results") or [])]


def search(query: str, num: int = 10, provider: str = "") -> list[dict]:
    """Run one query against the configured search provider.

    Returns raw result rows ({title, link, snippet}); [] on any failure. Never raises: a
    contact lookup is a nice-to-have beside an application, and a search outage must not
    take down whatever called it. That is also why a missing key prints and returns [] — the
    caller gets an empty shortlist, not a traceback.
    """
    name = (provider or os.environ.get(SEARCH_PROVIDER_ENV, "") or DEFAULT_PROVIDER).lower()
    cfg = PROVIDERS.get(name)
    if not cfg:
        print(f"[contacts] unknown search provider {name!r} — expected one of "
              f"{', '.join(sorted(PROVIDERS))}", file=sys.stderr)
        return []
    key = os.environ.get(cfg["key_env"], "").strip()
    if not key:
        print(f"[contacts] {cfg['key_env']} not set — no search performed", file=sys.stderr)
        return []
    if requests is None:
        print("[contacts] requests not installed — no search performed", file=sys.stderr)
        return []
    n = max(1, min(num, 10))
    try:
        if name == "google":
            cx = os.environ.get(cfg["cx_env"], "").strip()
            if not cx:
                print(f"[contacts] {cfg['cx_env']} not set — no search performed",
                      file=sys.stderr)
                return []
            r = requests.get(cfg["url"], headers={"User-Agent": UA}, timeout=20,
                             params={"key": key, "cx": cx, "q": query, "num": n})
        elif name == "brave":
            r = requests.get(cfg["url"], timeout=20,
                             headers={"User-Agent": UA, "Accept": "application/json",
                                      "X-Subscription-Token": key},
                             params={"q": query, "count": n})
        elif name == "tavily":
            # Tavily does not honour Google's `site:`/`intitle:` operators — it has a native
            # domain filter instead, and leaving the operators in the query text only adds
            # noise words for it to match on. Strip them and use `include_domains`.
            r = requests.post(cfg["url"], timeout=30,
                              headers={"User-Agent": UA, "Content-Type": "application/json",
                                       "Authorization": f"Bearer {key}"},
                              json={"query": strip_operators(query), "max_results": n,
                                    "include_domains": ["linkedin.com"]})
        else:  # exa
            r = requests.post(cfg["url"], timeout=30,
                              headers={"User-Agent": UA, "Content-Type": "application/json",
                                       "x-api-key": key},
                              json={"query": query, "numResults": n, "type": "keyword"})
        if r.status_code != 200:
            print(f"[contacts] {name} HTTP {r.status_code} for {query!r}", file=sys.stderr)
            return []
        data = r.json()
    except Exception as e:
        print(f"[contacts] {name} search failed for {query!r}: {e}", file=sys.stderr)
        return []
    return {"google": _rows_google, "tavily": _rows_tavily,
            "brave": _rows_brave, "exa": _rows_exa}[name](data)


def fetch_description(job: dict) -> str:
    """The job's description text, fetched lazily from the board API. "" on any failure.

    The scraper already requests Greenhouse with `content=true` and Lever/Ashby return their
    descriptions too — and all three parsers throw them away, because `Job` keeps seven
    fields and none of them is the body. That is the right call for the nightly run: 200+
    roles of description HTML in a git-committed queue.json would be megabytes of churn
    every night for text used on a handful of roles.

    So it is re-fetched here instead, for the one role being worked, at ~20 lookups a day.
    Only `team_hints()` consumes it, and a miss simply costs specificity in the team query
    rather than breaking anything — which is why every failure path returns "".
    """
    ats = str(job.get("ats") or "").lower()
    slug, jid = str(job.get("ats_slug") or ""), str(job.get("ats_job_id") or "")
    if requests is None or not slug or not jid:
        return ""
    urls = {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}",
        "lever":      f"https://api.lever.co/v0/postings/{slug}/{jid}",
        "ashby":      f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
    }
    if ats not in urls:
        return ""
    try:
        r = requests.get(urls[ats], headers={"User-Agent": UA}, timeout=20)
        if r.status_code != 200:
            return ""
        data = r.json()
    except Exception as e:
        print(f"[contacts] description fetch failed for {slug}/{jid}: {e}", file=sys.stderr)
        return ""
    if ats == "ashby":
        # Ashby has no per-job endpoint; the board JSON carries every posting, so pick ours.
        for j in (data.get("jobs") or []):
            if str(j.get("id")) == jid:
                return str(j.get("descriptionHtml") or j.get("descriptionPlain") or "")
        return ""
    return str(data.get("content") or data.get("description") or "")


def load_outreach() -> dict:
    """The whole outreach store, keyed by normalised profile URL. {} when absent.

    Held in the store rather than a file because it holds OTHER PEOPLE's names and profile
    links. That is the only data in this project which is not the user's own, and therefore
    the data it would be worst to leave in a checkout.
    """
    return get_store().get_outreach()


def known_keys() -> set[str]:
    """Profile keys already recorded, for cross-job dedupe.

    The same recruiter covers several roles, so without this the shortlist for the third
    Northwind job re-offers somebody already contacted about the first two. Inviting one
    person twice is the single mistake here that costs a real relationship, so it is checked
    against the store rather than only within one role's results.
    """
    return set(load_outreach().keys())


def save_contacts(job: dict, found: list[Contact]) -> int:
    """Record newly-found contacts against a job. Returns how many rows were ADDED.

    Never overwrites an existing row: once a contact carries state (invited, accepted) a
    later scrape must not reset it. New roles for a known person are appended to `jobs`, so
    the store answers "who have I already approached, and about what?".
    """
    store = load_outreach()
    key = str(job.get("dedup_key") or job.get("url") or "")
    added = 0
    for c in found:
        k = c.key()
        if not k:
            continue
        row = store.get(k)
        if row is None:
            store[k] = {**asdict(c), "state": "found", "jobs": [key] if key else []}
            added += 1
        elif key and key not in (row.get("jobs") or []):
            row.setdefault("jobs", []).append(key)
    get_store().set_outreach(store)
    return added


def default_affinities() -> tuple[str, str]:
    """(school, employer) read from profile.json — the shared-background terms to hunt.

    Read at runtime rather than written into this module, because they are the user's real
    school and employer: hardcoding them would put personal data in the shareable repo,
    which the PII guard flags (it flagged exactly that in this file's first draft). It also
    means this works for a stranger with their own profile, which is the same reasoning
    behind every derived pattern in `scripts/pii_guard.py`.

    Returns ("", "") when the profile is the shipped template or unreadable — callers then
    simply run the two discovery queries that do not need them.
    """
    try:
        prof = get_store().get_profile()
    except (OSError, json.JSONDecodeError):
        return "", ""
    school = str((prof.get("education") or {}).get("school") or "").strip()
    employer = str((prof.get("employment") or {}).get("current_employer") or "").strip()
    # FILL_ME is the template's placeholder; hunting it would match nothing and waste a query.
    clean = lambda v: "" if (not v or "FILL_ME" in v) else v
    return clean(school), clean(employer)


def find_contacts(job: dict, description: str = "", school: str = "", employer: str = "",
                  top: int = 3, searcher=search, exclude: set[str] | None = None
                  ) -> list[Contact]:
    """The whole phase-1 pipeline for one role: queries -> parse -> filter -> rank.

    `searcher` is injected so the selftest and any future caller can run the full path
    against fixtures without a key or a network. One query per signal is skipped once that
    signal has produced anything — that is the ladder, and it is what keeps a role to ~3
    API calls against the 100/day free quota.

    `exclude` is the set of profile keys already in the outreach store (pass
    `known_keys()`). Dropped BEFORE ranking, so a person already approached about another
    role does not occupy one of the three slots — the shortlist is small on purpose, and a
    row he cannot act on is worse than a shorter list.
    """
    hints = team_hints(description) if description else []
    company = str(job.get("company") or "")
    skip = exclude or set()
    found: list[Contact] = []
    satisfied: set[str] = set()
    for signal, query in build_queries(job, hints, school, employer):
        if signal in satisfied:
            continue
        rows = searcher(query) or []
        got = 0
        for item in rows:
            c = parse_result(item, company)
            if not c:
                continue
            # Count it as a hit for the ladder even when excluded: the query DID find the
            # right kind of person, so falling further down the ladder would only re-find
            # them more expensively.
            got += 1
            if c.key() in skip:
                continue
            snippet = item.get("snippet") or item.get("content") or ""
            corroborated = boost_terms(c, snippet, school, employer)
            # An alumni signal is only real if the shared background is VISIBLE on the
            # profile. Assigning it from the query that found the person credits whatever
            # the engine decided was relevant — and a semantic engine matches loosely, so
            # the alumni query returns plenty of people who simply work there. team and
            # recruiter stay query-assigned: those are claims about the ROLE, which the
            # employer check and the headline already evidence.
            sigs = corroborated if signal.startswith("alumni_") else [signal]
            for b in corroborated:
                if b not in sigs:
                    sigs.append(b)
            if not sigs:
                # Found by an alumni query but nothing on the profile backs it up. Still a
                # current employee at the target company, which is worth something — keep
                # them at the weakest weight rather than discarding a real person.
                sigs = ["recruiter"] if "recruit" in _norm(c.headline) else ["team"]
            c.signals = sigs
            found.append(c)
        if got:
            satisfied.add(signal)
    return rank(found, job, top)


# --------------------------------------------------------------- selftest ---
def selftest():
    checks = 0
    NORTH = "Northwind"

    # --- country_of: the free location signal in the URL subdomain -------------
    assert country_of("https://in.linkedin.com/in/example-in-profile") == "in"; checks += 1
    assert country_of("https://ca.linkedin.com/in/example-ca-profile") == "ca"; checks += 1
    # plain www is UNMARKED, not confirmed-US: scoring must not treat it as evidence.
    assert country_of("https://www.linkedin.com/in/example-us-profile") == ""; checks += 1
    assert country_of("") == ""; checks += 1

    # --- split_title: both dash forms, and a missing headline -----------------
    n, h = split_title("Priya N. - Technical Recruiting at Northwind | LinkedIn")
    assert (n, h) == ("Priya N.", "Technical Recruiting at Northwind"), (n, h); checks += 1
    n, h = split_title("Wei Chen, Ph.D. – Data Science Manager at Northwind")
    assert n == "Wei Chen, Ph.D." and "Data Science Manager" in h, (n, h); checks += 1
    assert split_title("Northwind | LinkedIn") == ("Northwind", ""); checks += 1

    # --- title_has_company: THE filter ----------------------------------------
    assert title_has_company("Head of Data and Experimentation Platform at Northwind", NORTH); checks += 1
    # The measured failure mode: an alum who LEFT renders with their new employer.
    assert not title_has_company("Google", NORTH); checks += 1
    assert not title_has_company("Engineering @ DXC Technology", NORTH); checks += 1
    # The measured false positive: "Robin Ellery" must not match "Northwind".
    assert not title_has_company("Robin Ellery - Ardent Capital", NORTH); checks += 1
    # a longer company that merely starts with the name is a different employer
    assert not title_has_company("Data Science Manager at Northwinds Ltd", NORTH); checks += 1

    # --- parse_result ---------------------------------------------------------
    ok = parse_result({"title": "Dana Whitfield - Head of Data and Experimentation Platform "
                                "at Northwind | LinkedIn",
                       "link": "https://www.linkedin.com/in/example-dana-whitfield/"}, NORTH)
    assert ok and ok.name == "Dana Whitfield" and ok.country == ""; checks += 1
    # a company page, not a person
    assert parse_result({"title": "Northwind | LinkedIn",
                         "link": "https://www.linkedin.com/company/robinhood"}, NORTH) is None; checks += 1
    # ex-employee: rejected by the title filter, not by the word "ex"
    assert parse_result({"title": "Marco Ferreira - Google | LinkedIn",
                         "link": "https://www.linkedin.com/in/example-marco-ferreira/"}, NORTH) is None; checks += 1
    # agency recruiter naming the company in their headline
    assert parse_result({"title": "Someone - Staffing partner to Northwind | LinkedIn",
                         "link": "https://www.linkedin.com/in/example-x/"}, NORTH) is None; checks += 1

    # --- role_keywords / team_hints -------------------------------------------
    assert role_keywords("Senior Software Engineer, Data Lake") == ["data", "lake"], \
        role_keywords("Senior Software Engineer, Data Lake"); checks += 1
    assert role_keywords("Software Engineer (L2)") == []; checks += 1
    assert team_hints("You will join the Payments Risk team and ship fast.") == ["Payments Risk"], \
        team_hints("You will join the Payments Risk team and ship fast."); checks += 1
    assert team_hints("<p>Part of the Data Infrastructure org</p>") == ["Data Infrastructure"]; checks += 1
    # a single generic capitalised word is not a team name
    assert team_hints("You will join the Engineering team.") == []; checks += 1
    assert team_hints("") == []; checks += 1

    # --- build_queries: ladder order and shape --------------------------------
    job = {"company": NORTH, "title": "Senior Software Engineer, Data Lake",
           "location": "Menlo Park, CA, USA"}
    qs = build_queries(job, ["Data Platform"], school="Kestrel Institute", employer="Ardent Capital")
    sigs = [s for s, _ in qs]
    assert sigs[0] == "team" and "Data Platform" in qs[0][1]; checks += 1
    assert sigs.count("team") == 3, sigs; checks += 1
    assert "recruiter" in sigs and "alumni_school" in sigs and "alumni_employer" in sigs; checks += 1
    # alumni queries must NOT use intitle (measured: forcing both into the title returns
    # nothing, because the school lives in the profile body).
    alum = [q for s, q in qs if s.startswith("alumni")]
    assert all("intitle" not in q for q in alum), alum; checks += 1
    assert all(q.startswith("site:linkedin.com/in") for _, q in qs); checks += 1
    assert build_queries({"company": ""}) == []; checks += 1

    # --- scoring ---------------------------------------------------------------
    us_job = {"company": NORTH, "title": "x", "location": "New York, NY, USA"}
    solo = Contact("A", "Data Science Manager at Northwind",
                   "https://www.linkedin.com/in/example-a", signals=["team"])
    both = Contact("B", "Data Science Manager at Northwind",
                   "https://www.linkedin.com/in/example-b", signals=["team", "alumni_employer"])
    assert score(both, us_job) > score(solo, us_job), "overlap must outrank a single signal"; checks += 1
    # seniority nudge inside the team signal
    junior = Contact("C", "Software Engineer at Northwind",
                     "https://www.linkedin.com/in/example-c", signals=["team"])
    assert score(solo, us_job) > score(junior, us_job); checks += 1
    # location: heavy for a recruiter, near-free for an alum
    rec_in = Contact("D", "Recruiting at Northwind", "https://in.linkedin.com/in/example-d",
                     signals=["recruiter"])
    rec_us = Contact("E", "Recruiting at Northwind", "https://www.linkedin.com/in/example-e",
                     signals=["recruiter"])
    assert score(rec_us, us_job) - score(rec_in, us_job) > 0.3; checks += 1
    alum_in = Contact("F", "SWE at Northwind", "https://in.linkedin.com/in/example-f",
                      signals=["alumni_school"])
    alum_us = Contact("G", "SWE at Northwind", "https://www.linkedin.com/in/example-g",
                      signals=["alumni_school"])
    # A nearby alum outranks a distant one — but only just. The penalty breaks ties WITHIN
    # the alumni signal; it must never be large enough to reorder signals.
    assert 0 < score(alum_us, us_job) - score(alum_in, us_job) <= 0.15, \
        "the alumni location penalty must be a tie-breaker, not a demotion"; checks += 1
    # THE INVARIANT (Parth's call 2026-08-04): an alum is worth more wherever they sit, so a
    # distant alum still beats an ordinary same-country team contact and any recruiter.
    # This is what caps LOCATION_PENALTY["alumni_*"] below ~0.20.
    assert score(alum_in, us_job) > score(junior, us_job), \
        "a distant alum must still outrank an ordinary same-country team contact"; checks += 1
    assert score(alum_in, us_job) > score(rec_us, us_job); checks += 1
    # The ONE thing that outranks a distant alum: a SENIOR same-country team contact, via the
    # seniority nudge (0.80 + 0.15 = 0.95 vs 0.90). Deliberate — a Head/Director/Manager on
    # the hiring team is plausibly the person who owns the req, and that beats a referral
    # from someone three time zones from the work. Asserted so the ordering cannot drift
    # unnoticed if either constant is retuned.
    assert score(solo, us_job) > score(alum_in, us_job) > score(junior, us_job), \
        (score(solo, us_job), score(alum_in, us_job), score(junior, us_job)); checks += 1
    assert score(Contact("H", "x", "https://www.linkedin.com/in/example-h"), us_job) == 0.0; checks += 1

    # --- boost_terms ------------------------------------------------------------
    c = Contact("I", "SWE at Northwind", "https://www.linkedin.com/in/example-i")
    assert boost_terms(c, "Previously Ardent Capital. Kestrel Institute grad.", "Kestrel Institute",
                       "Ardent Capital") == ["alumni_school", "alumni_employer"]; checks += 1
    assert boost_terms(c, "nothing relevant", "Kestrel Institute", "Ardent Capital") == []; checks += 1

    # --- rank: dedupe MERGES signals -------------------------------------------
    dup_a = Contact("J", "EM at Northwind", "https://www.linkedin.com/in/example-j/", signals=["team"])
    dup_b = Contact("J", "EM at Northwind", "https://in.linkedin.com/in/example-j?trk=x",
                    country="in", signals=["alumni_school"])
    r = rank([dup_a, dup_b], us_job)
    assert len(r) == 1, r; checks += 1
    assert set(r[0].signals) == {"team", "alumni_school"}, r[0].signals; checks += 1
    assert r[0].country == "in", "a country reading must survive the merge"; checks += 1
    assert len(rank([dup_a, dup_b, solo, both, junior], us_job, top=2)) == 2; checks += 1

    # --- find_contacts end to end, against fixtures (no key, no network) --------
    FIXTURES = {
        "team": [
            {"title": "Dana Whitfield - Head of Data and Experimentation Platform at "
                      "Northwind | LinkedIn",
             "link": "https://www.linkedin.com/in/example-dana-whitfield/",
             "snippet": "Leading data platform. Previously Ardent Capital."},
            {"title": "Marco Ferreira - Google | LinkedIn",           # ex: must be dropped
             "link": "https://www.linkedin.com/in/example-marco-ferreira/",
             "snippet": "ex-SWE @ Northwind, Kestrel Institute"},
        ],
        "recruiter": [
            {"title": "Priya N. - Technical Recruiting at Northwind | LinkedIn",
             "link": "https://www.linkedin.com/in/example-priya-n/", "snippet": "Hiring across teams"},
        ],
        "alumni": [
            {"title": "Ade Balogun - Software Engineer at Northwind | LinkedIn",
             "link": "https://in.linkedin.com/in/example-ade-balogun/",
             "snippet": "Kestrel Institute CS'24"},
        ],
    }
    calls: list[str] = []

    def fake_search(q: str, num: int = 10) -> list[dict]:
        calls.append(q)
        if "recruit" in q:
            return FIXTURES["recruiter"]
        if "Kestrel" in q:
            return FIXTURES["alumni"]
        if "Ardent" in q:
            return []
        return FIXTURES["team"]

    got = find_contacts(job, description="You will join the Data Platform team.",
                        school="Kestrel Institute", employer="Ardent Capital", searcher=fake_search)
    names = [c.name for c in got]
    assert "Marco Ferreira" not in names, "the ex-employee must never reach the shortlist"; checks += 1
    assert names[0] == "Dana Whitfield", names; checks += 1   # team + GS boost wins
    assert "alumni_employer" in got[0].signals, got[0].signals; checks += 1
    assert len(got) == 3, names; checks += 1
    # the ladder stops asking once a signal has produced rows: 3 team queries are available
    # but only the first should have run.
    assert sum(1 for q in calls if "Data Platform" in q) == 1; checks += 1
    assert len([q for q in calls if "Manager OR Lead" in q]) == 0, \
        "a satisfied signal must not fall further down its ladder"; checks += 1

    # --- cross-job dedupe: an already-known person must not take a slot ---------
    got2 = find_contacts(job, description="You will join the Data Platform team.",
                         school="Kestrel Institute", employer="Ardent Capital",
                         searcher=fake_search,
                         exclude={"linkedin.com/in/example-dana-whitfield"})
    assert "Dana Whitfield" not in [c.name for c in got2], [c.name for c in got2]; checks += 1
    assert len(got2) == 2, [c.name for c in got2]; checks += 1
    # ...but the ladder must still treat that query as satisfied, or it pays for more
    # searches to re-find the person it just discarded.
    calls.clear()
    find_contacts(job, description="You will join the Data Platform team.",
                  searcher=fake_search, exclude={"linkedin.com/in/example-dana-whitfield"})
    assert not [q for q in calls if "Manager OR Lead" in q], calls; checks += 1

    # --- the store: append-only, never resets state -----------------------------
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        real = paths.OUTREACH
        try:
            paths.OUTREACH = pathlib.Path(td) / "outreach.json"
            assert load_outreach() == {} and known_keys() == set(); checks += 1
            j1 = {"company": "Northwind", "dedup_key": "k1"}
            c1 = Contact("Dana Whitfield", "Head of Data at Northwind",
                         "https://www.linkedin.com/in/example-dana-whitfield",
                         signals=["team"])
            assert save_contacts(j1, [c1]) == 1; checks += 1
            assert save_contacts(j1, [c1]) == 0, "re-finding must not duplicate a row"; checks += 1
            assert known_keys() == {"linkedin.com/in/example-dana-whitfield"}; checks += 1
            # a state set by a later phase must survive the next scrape
            store = load_outreach()
            store["linkedin.com/in/example-dana-whitfield"]["state"] = "invited"
            paths.OUTREACH.write_text(json.dumps(store), encoding="utf-8")
            save_contacts({"company": "Northwind", "dedup_key": "k2"}, [c1])
            row = load_outreach()["linkedin.com/in/example-dana-whitfield"]
            assert row["state"] == "invited", row["state"]; checks += 1
            assert row["jobs"] == ["k1", "k2"], row["jobs"]; checks += 1
        finally:
            paths.OUTREACH = real

    # --- fetch_description: every failure path returns "" rather than raising ----
    assert fetch_description({}) == ""; checks += 1
    assert fetch_description({"ats": "workday", "ats_slug": "s", "ats_job_id": "1"}) == ""; checks += 1

    # --- profile cards: the richer shape some providers return -------------------
    CARD = ("# Ada Nkemelu\nNorthwind\nToronto, Ontario, Canada, CA  \n"
            "500 connections, 1124 followers\n\n## About\nSenior technical recruiter.\n\n"
            "## Experience\nN/A\n\n## Education\n### Kestrel Institute\n2009 - 2013\n")
    card = parse_profile_card(CARD)
    assert card["name"] == "Ada Nkemelu", card; checks += 1
    assert card["employer"] == "Northwind", card; checks += 1
    assert card["location"] == "Toronto, Ontario, Canada, CA", card; checks += 1
    assert card["about"] == "Senior technical recruiter.", card; checks += 1
    assert "Kestrel Institute" in card["education"], card; checks += 1
    # "N/A" is how these cards spell an empty section — it must not become a headline.
    assert parse_profile_card("# X\nY\n\n## About\nN/A\n")["about"] == ""; checks += 1
    # A SERP-shaped snippet is not a card, which is what routes it to the fallback.
    assert parse_profile_card("Technical Recruiting at Northwind - LinkedIn") is None; checks += 1
    assert parse_profile_card("") is None; checks += 1

    # --- country_from_location: better evidence than the URL subdomain -----------
    assert country_from_location("Toronto, Ontario, Canada, CA") == "ca"; checks += 1
    assert country_from_location("Greater Seattle Area, US") == "us"; checks += 1
    assert country_from_location("San Francisco Bay Area") == ""; checks += 1

    # --- employer_is: strict, because the card's line IS the employer -------------
    assert employer_is("Northwind", "Northwind"); checks += 1
    assert employer_is("Northwind Markets, Inc.", "Northwind"); checks += 1
    # The measured failure class: a single-word company name that is also a common word or
    # a surname. A substring test passed all three of these; equality does not.
    assert not employer_is("Freelance Content Writer", "Writer"); checks += 1
    assert not employer_is("Sierra West Select at Cisco", "Sierra"); checks += 1
    assert not employer_is("Brandon Harvey", "Harvey"); checks += 1
    assert not employer_is("", "Northwind") and not employer_is("Northwind", ""); checks += 1

    # --- parse_result over a card, including the location upgrade ----------------
    c = parse_result({"link": "https://www.linkedin.com/in/example-ada", "snippet": CARD}, "Northwind")
    assert c and c.name == "Ada Nkemelu" and c.headline == "Senior technical recruiter."; checks += 1
    assert c.country == "ca", "the stated location must beat the URL subdomain"; checks += 1
    assert parse_result({"link": "https://www.linkedin.com/in/example-ada",
                         "snippet": CARD}, "Southwind") is None; checks += 1

    # --- strip_operators: keep the operand, drop only Google-only syntax ---------
    q = strip_operators('site:linkedin.com/in intitle:"Northwind" ("technical recruiter")')
    assert "site:" not in q and "intitle:" not in q, q; checks += 1
    assert "Northwind" in q, "stripping intitle must NOT take the company name with it"; checks += 1
    assert "technical recruiter" in q, q; checks += 1

    # --- provider adapters: every engine normalises to {title, link, snippet} ----
    # The row shape is the contract parse_result depends on, so each adapter is pinned to a
    # real payload shape rather than trusted. Adding a provider means adding a case here.
    assert _rows_google({"items": [{"title": "T", "link": "L", "snippet": "S"}]}) == \
        [{"title": "T", "link": "L", "snippet": "S"}]; checks += 1
    assert _rows_tavily({"results": [{"title": "T", "url": "L", "content": "S"}]}) == \
        [{"title": "T", "link": "L", "snippet": "S"}]; checks += 1
    assert _rows_brave({"web": {"results": [{"title": "T", "url": "L", "description": "S"}]}}) == \
        [{"title": "T", "link": "L", "snippet": "S"}]; checks += 1
    assert _rows_exa({"results": [{"title": "T", "url": "L", "text": "S"}]}) == \
        [{"title": "T", "link": "L", "snippet": "S"}]; checks += 1
    # An empty or unexpected payload yields no rows rather than raising.
    for fn in (_rows_google, _rows_tavily, _rows_brave, _rows_exa):
        assert fn({}) == []; checks += 1
    # An unknown provider, and a known one with no key, both return [] rather than raising.
    assert search("q", provider="nope") == []; checks += 1
    # The key is cleared for the duration: with one set this would fire a REAL query, and a
    # selftest that quietly spends someone's search quota is a selftest nobody runs.
    _saved = os.environ.pop(PROVIDERS["tavily"]["key_env"], None)
    try:
        assert search("q", provider="tavily") == []; checks += 1
    finally:
        if _saved is not None:
            os.environ[PROVIDERS["tavily"]["key_env"]] = _saved
    assert set(PROVIDERS) == {"tavily", "brave", "exa", "google"}, sorted(PROVIDERS); checks += 1

    print(f"contacts: {checks}/{checks} checks passed")


def main():
    ap = argparse.ArgumentParser(description="Find outreach contacts for a role.")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--company", help="company name (with --title, does one live lookup)")
    ap.add_argument("--title", default="", help="role title")
    ap.add_argument("--school", default="", help="e.g. 'Kestrel Institute'")
    ap.add_argument("--employer", default="", help="e.g. 'Ardent Capital'")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return
    if not a.company:
        ap.error("--company is required (or use --selftest)")
    job = {"company": a.company, "title": a.title, "location": ""}
    got = find_contacts(job, school=a.school, employer=a.employer)
    if a.json:
        print(json.dumps([asdict(c) for c in got], indent=2))
        return
    if not got:
        prov = (os.environ.get(SEARCH_PROVIDER_ENV, "") or DEFAULT_PROVIDER).lower()
        want = (PROVIDERS.get(prov) or {}).get("key_env", "?")
        print(f"no contacts found (provider={prov}; is {want} set?)")
        return
    for c in got:
        print(f"{c.score:5.2f}  {c.name}  —  {c.headline}")
        print(f"        {','.join(c.signals)}  {c.country or '--'}  {c.profile_url}")


if __name__ == "__main__":
    main()
