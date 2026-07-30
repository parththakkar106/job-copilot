#!/usr/bin/env python3
"""
answer bank manager — keep open-ended answers fresh without hand-editing dates.

  python bank.py list [--key why_company] [--company Ramp] [--status active]
  python bank.py add  --key why_company --scope company --company Ramp --answer "..."
  python bank.py add  --key biggest_strength --scope common --answer "..." --status active
  python bank.py update <id> --answer "..." [--status active]
  python bank.py remove <id>
  python bank.py touch  <id>                 # bump last_used + uses (called by apply engine)
  python bank.py stale  --days 90            # list active answers unused/unrevised in N days
  python bank.py prune  --days 90 [--delete] # mark those stale (or --delete to remove)
  python bank.py get    --key why_company --company Ramp   # what apply-time would serve

The YAML stays fully hand-editable; the CLI is just convenience + date hygiene.
"""
from __future__ import annotations
import argparse, re, sys
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
import yaml

from core import paths

BANK = paths.ANSWER_BANK              # read: may be the shipped template on a fresh install
BANK_WRITE = paths.ANSWER_BANK_WRITE  # write: always the real, gitignored filename
TODAY = date.today().isoformat()


def load() -> list[dict]:
    if not BANK.exists():
        return []
    return (yaml.safe_load(BANK.read_text(encoding="utf-8")) or {}).get("answers", []) or []


def save(rows: list[dict]):
    # Deliberately NOT BANK — that can point at answer_bank.example.yaml before the user
    # has their own file, and answers are personal prose that must not land in a tracked
    # template. The first save promotes the template into the user's own file.
    BANK_WRITE.parent.mkdir(parents=True, exist_ok=True)
    BANK_WRITE.write_text(yaml.safe_dump({"answers": rows}, sort_keys=False, allow_unicode=True, width=100),
                          encoding="utf-8")


def slug(*parts) -> str:
    s = "_".join(p for p in parts if p)
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def find(rows, _id):
    return next((r for r in rows if r["id"] == _id), None)


# --- apply-time API (used by the CLI below AND by bridge.py to serve drafts) ---------

def resolve(key: str, company: str | None = None, rows: list[dict] | None = None) -> dict | None:
    """The answer apply-time serves for `key`: a company-scoped active answer beats a
    common active one; no active answer -> None (the caller asks Parth instead).
    Returns the live row, so callers can read its id for touch()."""
    if rows is None:
        rows = load()
    for want_company in (company, None):
        for r in rows:
            if r.get("key") != key or r.get("status") != "active":
                continue
            # Case-insensitive: fill.js derives company from the URL slug title-cased
            # ("Janestreet"), bank rows are hand-typed — an exact compare silently missed.
            if want_company and (r.get("company") or "").casefold() != want_company.casefold():
                continue
            if not want_company and r.get("scope") != "common":
                continue
            return r
    return None


def touch(_id: str, rows: list[dict] | None = None) -> bool:
    """Record that an answer was actually used. False if the id is unknown.
    Dates are read fresh rather than from TODAY: the bridge is long-running, so a
    module-level date would freeze at whenever it booted."""
    own = rows is None
    if own:
        rows = load()
    r = find(rows, _id)
    if not r:
        return False
    r["last_used"] = date.today().isoformat()
    r["uses"] = r.get("uses", 0) + 1
    save(rows)
    return True


def cmd_list(a, rows):
    for r in rows:
        if a.key and r["key"] != a.key:            continue
        if a.company and r.get("company") != a.company: continue
        if a.status and r.get("status") != a.status:    continue
        who = r.get("company") or "common"
        print(f'{r["id"]:<28} {r["status"]:<7} {r["key"]:<20} {who:<14} uses={r.get("uses",0)} last={r.get("last_used")}')


def cmd_get(a, rows):
    r = resolve(a.key, a.company, rows)
    if r is None:
        print("(no active answer — apply-time would ask Parth)")
        return
    print(f'[{r["id"]}] ({r.get("company") or "common"})\n{r["answer"].strip()}')


def cmd_add(a, rows):
    if a.scope == "company" and not a.company:
        sys.exit("company scope requires --company")
    _id = a.id or slug(a.key, a.company)
    if find(rows, _id):
        sys.exit(f"id '{_id}' exists — use update")
    rows.append({
        "id": _id, "key": a.key, "scope": a.scope,
        "company": a.company if a.scope == "company" else None,
        "answer": a.answer, "tags": a.tags.split(",") if a.tags else [],
        "status": a.status, "created": TODAY, "updated": TODAY,
        "last_used": None, "uses": 0,
    })
    save(rows); print(f"added {_id}")


def cmd_update(a, rows):
    r = find(rows, a.id) or sys.exit(f"no id '{a.id}'")
    if a.answer is not None: r["answer"] = a.answer
    if a.status is not None: r["status"] = a.status
    r["updated"] = TODAY
    save(rows); print(f"updated {a.id}")


def cmd_remove(a, rows):
    r = find(rows, a.id) or sys.exit(f"no id '{a.id}'")
    rows.remove(r); save(rows); print(f"removed {a.id}")


def cmd_touch(a, rows):
    if not touch(a.id, rows):
        sys.exit(f"no id '{a.id}'")
    print(f"touched {a.id} (uses={find(rows, a.id)['uses']})")


def _stale_rows(rows, days):
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    out = []
    for r in rows:
        if r.get("status") != "active":
            continue
        ref = r.get("last_used") or r.get("updated") or r.get("created")
        try:
            if date.fromisoformat(ref) < cutoff:
                out.append(r)
        except (TypeError, ValueError):
            out.append(r)
    return out


def cmd_stale(a, rows):
    for r in _stale_rows(rows, a.days):
        print(f'{r["id"]:<28} last_used={r.get("last_used")} updated={r.get("updated")}')


def cmd_prune(a, rows):
    stale = _stale_rows(rows, a.days)
    if a.delete:
        for r in stale: rows.remove(r)
        print(f"deleted {len(stale)} stale")
    else:
        for r in stale: r["status"] = "stale"
        print(f"marked {len(stale)} stale (status=stale; won't be served)")
    save(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list");  p.add_argument("--key"); p.add_argument("--company"); p.add_argument("--status")
    p = sub.add_parser("get");   p.add_argument("--key", required=True); p.add_argument("--company")
    p = sub.add_parser("add")
    p.add_argument("--key", required=True); p.add_argument("--scope", choices=["common", "company"], required=True)
    p.add_argument("--company"); p.add_argument("--answer", required=True); p.add_argument("--id")
    p.add_argument("--tags"); p.add_argument("--status", default="active", choices=["active", "draft", "stale"])
    p = sub.add_parser("update"); p.add_argument("id"); p.add_argument("--answer"); p.add_argument("--status", choices=["active", "draft", "stale"])
    p = sub.add_parser("remove"); p.add_argument("id")
    p = sub.add_parser("touch");  p.add_argument("id")
    p = sub.add_parser("stale");  p.add_argument("--days", type=int, default=90)
    p = sub.add_parser("prune");  p.add_argument("--days", type=int, default=90); p.add_argument("--delete", action="store_true")

    a = ap.parse_args()
    rows = load()
    {"list": cmd_list, "get": cmd_get, "add": cmd_add, "update": cmd_update,
     "remove": cmd_remove, "touch": cmd_touch, "stale": cmd_stale, "prune": cmd_prune}[a.cmd](a, rows)
