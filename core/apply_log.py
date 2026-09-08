#!/usr/bin/env python3
"""
apply_log — record applications you've submitted so the scraper stops re-surfacing them.

Writes to applied.json (git-ignored), the same file scraper.py dedups against. Until
tracker-sync exists, this is the manual feedback loop: mark a role applied here and it
won't show up in tomorrow's queue.

  python apply_log.py add --company Ramp --title "Software Engineer" --location "New York, NY" [--url ...]
  python apply_log.py add --from-queue 3          # mark queue.json entry #3 (0-based) as applied
  python apply_log.py list
  python apply_log.py remove <dedup_key>

The dedup key is computed by scraper.dedup_key(company, title, location) so it matches
exactly what the scraper produces — mark it with the same company/title/location the
scraper showed you (or use --from-queue to copy them straight from the queue).
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import date
from pathlib import Path

from core import paths
from core.scraper import dedup_key  # reuse the exact same normalization
from server.store import get_store

APPLIED = paths.APPLIED
QUEUE = paths.QUEUE


def load() -> list[dict]:
    # A legacy log could hold bare dedup_key strings, so keep tolerating them: the store
    # holds whatever the import found, and the import does not rewrite old rows.
    return [e if isinstance(e, dict) else {"dedup_key": e} for e in get_store().get_applied()]


def save(rows: list[dict]):
    get_store().set_applied(rows)


# --------------------------------------------------------- Workday tenants
# Workday's apply modal offers "Use My Last Application", which clones a previous
# submission to the SAME TENANT and skips most of the 6-step wizard. That is the whole
# economics of Workday for us: 143 dream-tier jobs sit across only 11 tenants, and step 2
# alone wants full work history + education + resume. Applying manually once per tenant and
# cloning the rest is far cheaper than filling 143 wizards.
#
# The route therefore depends on a fact we already record: has this tenant been applied to
# before? applied.json has no `ats` field (46 rows, all None), so the tenant is recovered
# from the URL host instead. Adding a field would only help rows written from here on.

WORKDAY_HOST = re.compile(
    r"//(?P<tenant>[a-z0-9][a-z0-9-]*)\.(?P<pod>wd\d+)\.myworkdayjobs\.com/", re.I)


def workday_tenant(url: str | None) -> str | None:
    """'https://adobe.wd5.myworkdayjobs.com/external_experienced/job/...' -> 'adobe'.

    Returns None for any non-Workday URL. The tenant, not the company name, is the unit
    of account: a Workday login and its saved applications are per-tenant, and company
    names drift between the tracker and the board ("Bank of America" vs "BofA").
    """
    if not url:
        return None
    m = WORKDAY_HOST.search(url)
    return m.group("tenant").lower() if m else None


def applied_workday_tenants(rows: list[dict] | None = None) -> set[str]:
    """Tenants with at least one recorded application — i.e. where cloning is available."""
    rows = load() if rows is None else rows
    return {t for t in (workday_tenant(r.get("url")) for r in rows) if t}


def workday_route(url: str, rows: list[dict] | None = None) -> dict:
    """Which entry route to take on a Workday posting.

    Returns {tenant, route, reason}. `route` is a key of the adapter's `entry.routes`.

    Never `manual` for a real Workday URL. Both automated routes get Workday itself to do
    the bulk of the typing — a clone replays a past application, and the autofill route
    parses the PDF we were going to attach anyway — and the driver then reconciles the
    result against the chosen resume track, overwriting only what is missing or disagrees.
    Filling six steps from an empty form is strictly more work for a worse result, so it
    is the answer only when there is no Workday tenant to reason about at all.

    A clone copies the PREVIOUS answers including its resume, so it can land the quant PDF
    on an AI role. That used to be grounds for refusing to clone; it is now the reconcile
    pass's job, which is a better place for it — the refusal made the clone path
    unreachable, since nothing recorded which track a past application used.
    """
    tenant = workday_tenant(url)
    if not tenant:
        return {"tenant": None, "route": "manual", "reason": "not a Workday URL"}
    seen = applied_workday_tenants(rows)
    if tenant in seen:
        return {"tenant": tenant, "route": "useMyLastApplication",
                "reason": f"prior application recorded for tenant '{tenant}'"}
    return {"tenant": tenant, "route": "autofillWithResume",
            "reason": f"no prior application for tenant '{tenant}' — seed it from the resume"}


def cmd_add(a, rows):
    if a.from_queue is not None:
        if not QUEUE.exists():
            sys.exit(f"no queue at {QUEUE} — run the scraper first")
        q = get_store().get_queue("queue")
        if a.from_queue < 0:  # negative would silently index from the end — wrong entry logged
            sys.exit(f"queue index must be >= 0 (0..{len(q)-1})")
        try:
            j = q[a.from_queue]
        except IndexError:
            sys.exit(f"queue index {a.from_queue} out of range (0..{len(q)-1})")
        company, title, location, url = j["company"], j["title"], j["location"], j.get("url", "")
    else:
        if not (a.company and a.title):
            sys.exit("need --company and --title (and ideally --location), or --from-queue N")
        company, title, location, url = a.company, a.title, a.location or "", a.url or ""

    key = dedup_key(company, title, location)
    if any(r.get("dedup_key") == key for r in rows):
        print(f"already logged: {key} ({company} — {title})")
        return
    rows.append({"dedup_key": key, "company": company, "title": title,
                 "location": location, "url": url, "applied": date.today().isoformat()})
    save(rows)
    print(f"logged {key}  {company} — {title}")


def cmd_list(a, rows):
    for r in rows:
        print(f'{r.get("dedup_key"):<18} {r.get("applied","?"):<12} '
              f'{r.get("company","?")} — {r.get("title","?")} ({r.get("location","")})')
    print(f"\n{len(rows)} applications logged")


def cmd_remove(a, rows):
    before = len(rows)
    rows = [r for r in rows if r.get("dedup_key") != a.dedup_key]
    if len(rows) == before:
        sys.exit(f"no entry with dedup_key {a.dedup_key}")
    save(rows)
    print(f"removed {a.dedup_key}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("--company"); p.add_argument("--title"); p.add_argument("--location")
    p.add_argument("--url"); p.add_argument("--from-queue", type=int)
    sub.add_parser("list")
    p = sub.add_parser("remove"); p.add_argument("dedup_key")
    a = ap.parse_args()
    rows = load()
    {"add": cmd_add, "list": cmd_list, "remove": cmd_remove}[a.cmd](a, rows)
