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
import argparse, json, sys
from datetime import date
from pathlib import Path

from core import paths
from core.scraper import dedup_key  # reuse the exact same normalization

APPLIED = paths.APPLIED
QUEUE = paths.QUEUE


def load() -> list[dict]:
    if not APPLIED.exists():
        return []
    data = json.loads(APPLIED.read_text(encoding="utf-8"))
    # tolerate a legacy bare list of dedup_key strings
    return [e if isinstance(e, dict) else {"dedup_key": e} for e in data]


def save(rows: list[dict]):
    APPLIED.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def cmd_add(a, rows):
    if a.from_queue is not None:
        if not QUEUE.exists():
            sys.exit(f"no queue at {QUEUE} — run the scraper first")
        q = json.loads(QUEUE.read_text(encoding="utf-8"))
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
