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
                       "greenhouse", slug, str(j.get("id", "")), posted, None, tier))
    return out


def parse_lever(company, slug, tier, raw) -> list[Job]:
    out = []
    for j in raw:  # lever returns a bare list
        cat = j.get("categories") or {}
        posted = iso_from_ms(j.get("createdAt"))
        out.append(Job(company, j.get("text", ""), cat.get("location", ""),
                       j.get("hostedUrl", ""), "lever", slug, str(j.get("id", "")),
                       posted, None, tier))
    return out


def parse_ashby(company, slug, tier, raw) -> list[Job]:
    out = []
    for j in raw.get("jobs", []):
        if j.get("isListed") is False:
            continue
        posted = iso_date(j.get("publishedAt"))
        url = j.get("jobUrl") or j.get("applyUrl") or ""
        out.append(Job(company, j.get("title", ""), j.get("location", ""), url,
                       "ashby", slug, str(j.get("id", "")), posted, None, tier))
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


def passes_location(location: str, cfg: dict) -> bool:
    if not cfg.get("us_only"):
        return True
    loc = norm(location)
    if not loc:
        return True  # unknown -> keep, liveness/manual will catch
    # Word-bounded (has_word), NOT substring: "india" was dropping Indianapolis/Indiana,
    # "uk" was dropping Milwaukee, "dublin" Dublin CA — real US roles silently lost.
    return not any(has_word(loc, bad) for bad in cfg.get("exclude_locations", []))


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


def fetch_company(c: dict, fields_cfg: dict) -> list[Job]:
    ats, slug = c["ats"], c["slug"]
    tier = c.get("tier", "pipeline")
    name = c.get("name", slug)
    urls = {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
        "lever":      f"https://api.lever.co/v0/postings/{slug}?mode=json",
        "ashby":      f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false",
    }
    if ats not in urls:  # a typo'd ats: must cost one company, not the whole daily run
        print(f"  ! {name}: unknown ats '{ats}' (want greenhouse/lever/ashby) — skipped", file=sys.stderr)
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
            all_jobs.extend(fetch_company(c, fields_cfg))
        except Exception as e:  # one malformed entry must not kill the whole daily run
            print(f"  ! {c.get('name', c.get('slug', '?'))}: {e}", file=sys.stderr)
        time.sleep(0.5)

    # filter
    kept, seen = [], set()
    for j in all_jobs:
        if j.field is None:                                   continue
        if not passes_seniority(j.title, filt["seniority"]):  continue
        if not passes_location(j.location, filt["location"]): continue
        if not is_fresh(j.posted, filt.get("freshness_days", 14)): continue
        if j.dedup_key in applied:                            continue
        if j.dedup_key in seen:                               continue
        seen.add(j.dedup_key)
        kept.append(j)

    kept.sort(key=lambda x: x.posted or "", reverse=True)  # freshest first

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
    gh = {"jobs": [{"id": 1, "title": "Software Engineer, Backend",
                    "location": {"name": "New York, NY"},
                    "absolute_url": "http://x/1", "first_published": "2026-07-08T00:00:00Z"},
                   {"id": 2, "title": "Senior Data Scientist",
                    "location": {"name": "London, UK"},
                    "absolute_url": "http://x/2", "updated_at": "2026-07-01T00:00:00-04:00"}]}
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
    assert jobs[0].dedup_key == dedup_key("Acme", "Software Engineer, Backend", "New York, NY")
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
