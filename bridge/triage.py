#!/usr/bin/env python3
"""
triage.py — the orchestrator's brain (pure, dependency-light, testable offline).

The extension can drive tabs and fill forms, but it can't read the day's queue or the
applied-log off disk. The bridge already lives on Parth's laptop with filesystem access
and an open WebSocket to the extension, so it serves the work list: read the (already
pipeline-only, freshest-first) queue.json, drop anything already applied, apply the
daily / per-company caps, and hand back the roles to work.

Kept separate from bridge.py on purpose: no websockets/requests import here, so the
triage logic unit-tests standalone (test_triage.py) without a running bridge.

Design notes baked in from the plan + Fable's review:
  * Dream roles are ALREADY excluded — scraper.py writes only tier==pipeline to
    queue.json (dream goes to the digest, flagged "not auto-applied"). No filter needed.
  * Dedup vs applied.json uses the SAME dedup_key the scraper/apply_log compute, so a
    role applied via any path (bridge, apply_log CLI) won't be re-surfaced.
  * Per-company cap (default 2/day) guards against N applications to one company in one
    morning — the one cross-application flag a recruiter actually sees.
  * Daily cap (default 15) keeps the ✅-tap load and bot-detection surface sane.
"""
from __future__ import annotations

import os
import sys
import threading
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import paths  # noqa: E402
from server.store import resolve  # noqa: E402

# Imported, never re-derived. This module used to compute its own repo root and its own
# applied.json path; core/paths.py holds the same two, so pointing one at a temp directory
# left the other aimed at the real file, and a test wrote live records through it.
REPO_ROOT = paths.REPO_ROOT


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# Caps are env-overridable so Parth can dial volume without a code change.
MAX_PER_DAY = _int_env("APPLY_MAX_PER_DAY", 15)
MAX_PER_COMPANY = _int_env("APPLY_MAX_PER_COMPANY", 2)


def load_applied(store=None) -> list[dict]:
    """Applied records, tolerating a legacy bare list of dedup_key strings (matches
    apply_log.load exactly)."""
    return [e if isinstance(e, dict) else {"dedup_key": e}
            for e in resolve(store).get_applied()]


def applied_keys(store=None) -> set[str]:
    return {r.get("dedup_key") for r in load_applied(store) if r.get("dedup_key")}


def load_queue(store=None) -> list[dict]:
    """Today's scraped roles, or [] before the first run of the day."""
    return resolve(store).get_queue("queue")


# Serialize the read-modify-write: two near-simultaneous confirms (flush + a panel log)
# both run via asyncio.to_thread and could interleave, losing a row. In-process only —
# a concurrently-run apply_log CLI is still unguarded (rare, manual, accepted).
_APPLIED_WRITE_LOCK = threading.Lock()


def append_applied(record: dict, store=None) -> bool:
    """Append one decided role. Idempotent on dedup_key. Returns True if it was
    newly added (False if already present). Record shape matches apply_log.py so the two
    paths stay interchangeable.

    `status` records WHY the role is off the list: "applied" (default) or "rejected"
    (Parth said never show me this again). Both are excluded from future queues, since
    applied_keys() keys on dedup_key alone — the field only exists so a later count of
    "how many did I actually apply to" doesn't quietly include the rejects."""
    with _APPLIED_WRITE_LOCK:
        st = resolve(store)
        rows = load_applied(st)
        key = record.get("dedup_key")
        if key and any(r.get("dedup_key") == key for r in rows):
            return False
        row = {
            "dedup_key": key,
            "company": record.get("company", ""),
            "title": record.get("title", ""),
            "location": record.get("location", ""),
            "url": record.get("url", ""),
            "status": record.get("status") or "applied",
            "applied": record.get("applied") or date.today().isoformat(),
        }
        # Which resume track this application went out with. OPTIONAL and omitted when
        # unknown, so the rows written before this existed stay valid and a reader can tell
        # "filled with the quant CV" apart from "nobody recorded it" — a distinction the
        # Workday clone guard depends on, since it must fall back to reconciling rather
        # than trust a default. Never invent one: a wrong track here staples the wrong PDF
        # to every future clone of this application.
        if record.get("resume_track"):
            row["resume_track"] = record["resume_track"]
        st.add_applied(row)
        return True


def set_applied_status(dedup_key: str, status: str, store=None) -> bool:
    """Flip an ALREADY-decided role between "applied" and "rejected" in place.

    append_applied is idempotent on dedup_key: if the role is already logged it returns
    False and changes nothing. That's right for the collect/submit paths (never double-log
    a submission) but wrong for the dashboard, where Submitted -> Blocked is a correction
    of a role that is by definition already in the log. Routing that through append_applied
    made the move a silent no-op. This is the corrective path; it keeps the original
    `applied` date, since when it left the queue didn't change — only why.

    Returns True if a row was found and its status actually changed."""
    with _APPLIED_WRITE_LOCK:
        st = resolve(store)
        for r in load_applied(st):
            if r.get("dedup_key") == dedup_key:
                if (r.get("status") or "applied") == status:
                    return False
                r["status"] = status
                st.add_applied(r)
                return True
        return False


def remove_applied(dedup_key: str, store=None) -> bool:
    """Drop a decided role from applied.json so it re-enters the scrape pool. Backs the
    dashboard's un-submit / un-block — the only way to correct a mis-marked role, since
    applied_keys() otherwise hides it from every future queue forever. Returns True if a
    row was removed. Same in-process lock as append_applied so the two can't interleave."""
    with _APPLIED_WRITE_LOCK:
        st = resolve(store)
        rows = load_applied(st)
        kept = [r for r in rows if r.get("dedup_key") != dedup_key]
        if len(kept) == len(rows):
            return False
        st.set_applied(kept)
        return True


def triage(store=None,
           max_per_day: int | None = None,
           max_per_company: int | None = None,
           exclude_keys: set[str] | None = None) -> dict:
    """Return the roles to work today, plus a small stats block for the Telegram summary.

    Input queue is pipeline-only and freshest-first (scraper guarantees both), so we just
    walk it in order, skipping already-applied and anything over a cap. Order-preserving:
    the first N eligible roles win, so the freshest roles are worked first.

    exclude_keys = jobs already in flight (the pending table): they're being answered on
    Telegram, and re-collecting them is a no-op (pending.put never clobbers) that still
    burned a daily slot and a tab visit — every day, until answered. Skip them here.
    """
    day = MAX_PER_DAY if max_per_day is None else max_per_day
    per_co = MAX_PER_COMPANY if max_per_company is None else max_per_company
    exclude = set(exclude_keys or ())

    st = resolve(store)
    queue = load_queue(st)
    done = applied_keys(st)

    jobs: list[dict] = []
    counts: dict[str, int] = {}
    skipped_applied = skipped_company_cap = in_flight = 0
    for j in queue:
        if j.get("dedup_key") in done:
            skipped_applied += 1
            continue
        if j.get("dedup_key") in exclude:
            in_flight += 1
            continue
        co = (j.get("company") or "").strip().lower()
        if counts.get(co, 0) >= per_co:
            skipped_company_cap += 1
            continue
        counts[co] = counts.get(co, 0) + 1
        jobs.append(j)
        if len(jobs) >= day:
            break

    return {
        "jobs": jobs,
        "stats": {
            "queue_total": len(queue),
            "selected": len(jobs),
            "already_applied": skipped_applied,
            "in_flight": in_flight,
            "over_company_cap": skipped_company_cap,
            "hit_daily_cap": len(jobs) >= day,
            "max_per_day": day,
            "max_per_company": per_co,
        },
    }


if __name__ == "__main__":  # quick manual peek
    res = triage()
    s = res["stats"]
    print(f"{s['selected']} to work (of {s['queue_total']} in queue; "
          f"{s['already_applied']} already applied, {s['over_company_cap']} over company cap)")
    for j in res["jobs"]:
        print(f"  {j.get('company'):<20} {j.get('title')}")
