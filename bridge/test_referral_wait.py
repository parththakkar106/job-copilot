#!/usr/bin/env python3
"""Offline checks for the Awaiting referral pile: the 2-day ask, the 2-day timeout, and
what Yes and No do. Every path points at a temp dir, so real records are never touched."""
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bridge as B
import pending


def run():
    checks = 0
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        pending.PENDING_DIR = d / "pending"
        pending.REFERRAL_WAIT_PATH = d / "referral_wait.json"
        B.triage_mod.APPLIED_PATH = d / "applied.json"

        def put(key, answer):
            pending.save({"dedup_key": key, "status": pending.AWAITING, "company": "Co",
                          "title": "Quant", "questions": [{"id": "q1", "answer": answer}]})
            pending.park_referral(key, {"company": "Co", "title": "Quant", "bucket": "in_queue"})

        put("done", "Yes")
        put("open", None)
        pending.park_referral("disc", {"company": "Co", "title": "DS", "bucket": "discovered"})
        assert pending.load("done")["status"] == pending.REFERRAL; checks += 1

        now = datetime.now(timezone.utc)
        assert pending.due_referrals(now) == []; checks += 1
        later = now + timedelta(days=2, minutes=1)
        assert {k for k, kind in pending.due_referrals(later) if kind == "ask"} == \
            {"done", "open", "disc"}; checks += 1
        pending.mark_referral_asked("done")
        assert ("done", "timeout") in pending.due_referrals(later); checks += 1

        # No on a fully answered record approves it, so the next flush sends it.
        assert "Approved" in B._resolve_referral("done", got=False); checks += 1
        assert pending.load("done")["status"] == pending.APPROVED; checks += 1
        # No with an open question cannot send, so it goes back to the queue.
        assert "1 open question" in B._resolve_referral("open", got=False); checks += 1
        assert pending.load("open")["status"] == pending.AWAITING; checks += 1
        # A Discovered role has no record. It returns to Discovered for the next collect.
        assert "Discovered" in B._resolve_referral("disc", got=False); checks += 1
        assert pending.referral_waits() == {}; checks += 1

        # Yes logs it as Sent with the referred tag and retires the record.
        put("yes", None)
        assert "Sent" in B._resolve_referral("yes", got=True); checks += 1
        row = json.loads((d / "applied.json").read_text())[0]
        assert row["dedup_key"] == "yes" and row["referred"] is True; checks += 1
        assert pending.load("yes") is None; checks += 1
        # A second tap on a stale button changes nothing.
        assert "no longer waiting" in B._resolve_referral("yes", got=True); checks += 1
    print(f"referral wait: {checks}/{checks} checks passed")


if __name__ == "__main__":
    run()
