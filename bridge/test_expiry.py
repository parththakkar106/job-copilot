#!/usr/bin/env python3
"""Offline checks for pending.expire_stale and the way back out of Expired. Temp dir only."""
import tempfile
from datetime import date

import pending


def run():
    checks = 0
    today = date(2026, 10, 30)
    with tempfile.TemporaryDirectory() as d:
        def put(key, status, posted="", created="2026-10-29T00:00:00+00:00"):
            pending.save({"dedup_key": key, "status": status, "posted": posted,
                          "created": created, "questions": []}, d)

        put("old", pending.AWAITING, posted="2026-10-22")       # 8 days: expires
        put("edge", pending.AWAITING, posted="2026-10-23")      # exactly 7 days: stays
        put("held", pending.HELD, posted="2026-09-01")
        put("skip", pending.SKIPPED, posted="2026-09-01")
        put("sending", pending.APPROVED, posted="2026-09-01")   # the flush is about to send it
        put("nopost", pending.AWAITING, created="2026-10-01T00:00:00+00:00")  # collect date

        gone = {r["dedup_key"] for r in pending.expire_stale(base=d, today=today)}
        assert gone == {"old", "held", "skip", "nopost"}, gone; checks += 1
        assert pending.load("sending", d)["status"] == pending.APPROVED; checks += 1
        assert pending.load("edge", d)["status"] == pending.AWAITING; checks += 1

        # Brought back by hand: awaiting again, and the next sweep leaves it alone.
        rec = pending.reopen("old", d)
        assert rec["status"] == pending.AWAITING and rec["keep"]; checks += 1
        assert not pending.expire_stale(base=d, today=today); checks += 1
        assert pending.load("old", d)["status"] == pending.AWAITING; checks += 1
    print(f"expiry: {checks}/{checks} checks passed")


if __name__ == "__main__":
    run()
