#!/usr/bin/env python3
"""One-time reconcile for applications that submitted but were never logged.

WHAT WENT WRONG. A submit that navigates destroys the content script while it is still
polling for the outcome, so `sendResponse` never runs and Chrome closes the message
channel. `submitViaRetry` read that as `outcome: "error"` and parked the job with the
reason "couldn't confirm the submit (error)". The application had already reached the
ATS. The fix in extension/background.js stops this happening again, but it cannot repair
the records already on disk, which is what this script is for.

WHY THE HOLD REASON IS NOT ENOUGH ON ITS OWN. `holdJob` stores only the outcome word, so
a channel that died after a successful submit and a genuine exception inside
`submitWithRetry` both land on the same reason string. Some of the parked jobs really did
fail: two Twilio applications stopped at a Greenhouse security-code gate and never went
through. So this script does not decide anything. It applies a decision already recorded
in an evidence file, where every confirmed job is backed by the confirmation email the
company sent. Read `scripts/reconcile_2026-08-27.json` before running it.

WHAT IT WRITES. For each confirmed job: one row appended to applied.json with status
"applied" and the date the submit actually happened, then the pending record moved to
pending/submitted/. Both steps are the same ones the dashboard's "mark submitted" runs.
`append_applied` is idempotent on dedup_key and `archive` moves the file rather than
deleting it, so running this twice changes nothing and `pending.restore` undoes a move.

The script refuses to touch a job whose status or hold reason no longer matches the
evidence, because that means something changed after the evidence was gathered.

Usage:
    python scripts/reconcile_channel_loss.py                 # dry run, prints the plan
    python scripts/reconcile_channel_loss.py --apply         # write it
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bridge import pending as pending_mod  # noqa: E402
from bridge import triage as triage_mod  # noqa: E402

DEFAULT_EVIDENCE = Path(__file__).resolve().parent / "reconcile_2026-08-27.json"


def check(entry: dict, expect_reason: str) -> tuple[dict | None, str]:
    """The pending record for one evidence entry, plus why it is not usable if it is not.

    Returns `(record, "")` when the job is still held for the reason the evidence recorded,
    and `(None, reason)` otherwise.
    """
    key = entry.get("dedup_key") or ""
    rec = pending_mod.load(key)
    if rec is None:
        return None, "no pending record (already reconciled?)"
    if rec.get("status") != pending_mod.HELD:
        return None, f"status is {rec.get('status')!r}, not held"
    if rec.get("hold_reason") != expect_reason:
        return None, f"hold reason is {rec.get('hold_reason')!r}"
    return rec, ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE,
                    help="the evidence file to apply (default: %(default)s)")
    ap.add_argument("--apply", action="store_true",
                    help="write the rows. Without it the script only prints the plan.")
    args = ap.parse_args()

    ev = json.loads(args.evidence.read_text(encoding="utf-8"))
    expect_reason = ev["expect_hold_reason"]
    confirmed = ev.get("confirmed") or []
    skipped = ev.get("not_confirmed") or []

    print(f"evidence: {args.evidence}")
    print(f"applied log: {triage_mod.APPLIED_PATH}")
    print(f"pending dir: {pending_mod.PENDING_DIR}")
    print(f"mode: {'APPLY' if args.apply else 'dry run'}\n")

    ready, blocked = [], []
    for entry in confirmed:
        rec, why = check(entry, expect_reason)
        (ready if rec else blocked).append((entry, rec, why))

    print(f"CONFIRMED BY EMAIL — mark applied and archive ({len(ready)}):")
    for entry, _rec, _why in ready:
        print(f"  {entry['dedup_key']}  {entry['company']:14} {entry['title'][:52]}")
        print(f"      {entry['email']}")

    if blocked:
        print(f"\nSKIPPED — the record no longer matches the evidence ({len(blocked)}):")
        for entry, _rec, why in blocked:
            print(f"  {entry['dedup_key']}  {entry['company']:14} {entry['title'][:40]}  — {why}")

    print(f"\nLEFT HELD — no confirmation email, so these did not submit ({len(skipped)}):")
    for entry in skipped:
        print(f"  {entry['dedup_key']}  {entry['company']:14} {entry['title'][:40]}")
        print(f"      {entry['why']}")

    if not args.apply:
        print(f"\nDry run. Re-run with --apply to write {len(ready)} row(s).")
        return 0

    added = moved = 0
    for entry, rec, _why in ready:
        key = entry["dedup_key"]
        # Date the row by the submit itself, not by today. Re-running the reconcile later
        # must not restate when the application went out.
        row = {**rec, "dedup_key": key, "status": "applied",
               "applied": str(entry.get("held") or rec.get("updated") or "")[:10] or None}
        if triage_mod.append_applied(row, triage_mod.APPLIED_PATH):
            added += 1
        if pending_mod.archive(key):
            moved += 1
        print(f"  applied  {entry['company']:14} {entry['title'][:52]}")

    print(f"\nWrote {added} new applied row(s), archived {moved} pending record(s).")
    print(f"{len(skipped)} job(s) stay held for you to finish by hand.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
