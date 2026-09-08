#!/usr/bin/env python3
"""Offline tests for the dashboard endpoints: list_all (merged pile view) and set_state
(bucket transitions). See dashboard_spec.md. Mocks the WebSocket + Telegram and isolates
every pile in a temp dir, so no bot and no real pending/applied files are touched."""
import asyncio
import json
import tempfile
from pathlib import Path

import bridge as B
import pending
import triage

from server.store import get_store, use_temp_store


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _job(company, title, key):
    return {"company": company, "title": title, "location": "US",
            "url": f"https://x/{key}", "dedup_key": key}


def _list_all(since=60):
    ws = FakeWS()
    asyncio.run(B.handle_list_all({"reqId": "L", "type": "list_all", "since_days": since}, ws))
    return ws.sent[0]["answers"]


def _set_state(key, target, meta=None):
    ws = FakeWS()
    asyncio.run(B.handle_set_state(
        {"reqId": "S", "type": "set_state", "dedup_key": key, "target": target, "meta": meta or {}}, ws))
    return ws.sent[0]["answers"]


def run():
    checks = 0
    B.tg_send = lambda text, buttons=None: 1  # silence Telegram

    with tempfile.TemporaryDirectory() as tmp:
        # Isolate every pile: one store holds all four.
        use_temp_store(tmp)

        # Seed the pending pile: one complete (answered) awaiting, one incomplete.
        pending.put(_job("Acme", "SWE", "k1"),
                    [{"id": "q1", "label": "Why", "answer": "because"}], [])
        pending.put(_job("Beta", "DS", "k2"),
                    [{"id": "q1", "label": "Salary", "answer": None}], [])
        # Seed an already-applied role and a blocked one (dated older so sort is testable).
        # Dated within the default 60-day window, but older than the (today) pending rows.
        triage.append_applied({"dedup_key": "k3", "company": "Gamma", "title": "MLE",
                               "url": "https://x/k3", "status": "applied", "applied": "2026-07-10"})
        triage.append_applied({"dedup_key": "k4", "company": "Delta", "title": "SRE",
                               "url": "https://x/k4", "status": "rejected", "applied": "2026-07-11"})

        # 1) list_all merges all four, buckets + tags them, newest first.
        ans = _list_all()
        assert ans["ok"] and len(ans["rows"]) == 4, ans; checks += 1
        by_key = {r["dedup_key"]: r for r in ans["rows"]}
        assert by_key["k1"]["bucket"] == "in_queue" and by_key["k1"]["tag"] == "ready to approve", by_key["k1"]; checks += 1
        assert by_key["k2"]["bucket"] == "in_queue" and by_key["k2"]["tag"] == "1 question", by_key["k2"]; checks += 1
        assert by_key["k3"]["bucket"] == "submitted", by_key["k3"]; checks += 1
        assert by_key["k4"]["bucket"] == "blocked", by_key["k4"]; checks += 1
        # newest first: today's pending (k1/k2) precede the Jan applied rows.
        assert ans["rows"][0]["dedup_key"] in ("k1", "k2"), ans["rows"][0]; checks += 1

        # 1b) since_days windows the applied pile only (pending always shown).
        ans_recent = _list_all(since=5)  # cutoff ~2026-07-15, so the 07-10/11 rows drop
        keys = {r["dedup_key"] for r in ans_recent["rows"]}
        assert keys == {"k1", "k2"}, keys; checks += 1

        # 2) → filled: succeeds on the complete job, refuses the incomplete one.
        assert _set_state("k1", "filled")["ok"] is True; checks += 1
        assert pending.load("k1")["status"] == pending.APPROVED; checks += 1
        r = _set_state("k2", "filled")
        assert r["ok"] is False and "question" in r["error"], r; checks += 1
        assert pending.load("k2")["status"] == pending.AWAITING; checks += 1

        # 3) → skipped then back → in_queue (reopen keeps the record).
        assert _set_state("k2", "skipped")["ok"] is True; checks += 1
        assert pending.load("k2")["status"] == pending.SKIPPED; checks += 1
        # skipped shows under in_queue with the skipped tag.
        assert _list_all()["rows"] and next(x for x in _list_all()["rows"] if x["dedup_key"] == "k2")["tag"] == "skipped"; checks += 1
        assert _set_state("k2", "in_queue")["ok"] is True; checks += 1
        assert pending.load("k2")["status"] == pending.AWAITING; checks += 1

        # 4) → submitted: writes applied.json and retires the pending record.
        assert _set_state("k1", "submitted")["ok"] is True; checks += 1
        assert "k1" in triage.applied_keys(); checks += 1
        assert pending.load("k1") is None, "pending must be deleted after submit"; checks += 1

        # 5) → blocked from a pending record: applied.json rejected + pending gone.
        assert _set_state("k2", "blocked")["ok"] is True; checks += 1
        rows = triage.load_applied()
        assert any(r["dedup_key"] == "k2" and r["status"] == "rejected" for r in rows), rows; checks += 1
        assert pending.load("k2") is None; checks += 1

        # 6) un-submit: → in_queue on an applied-only row removes it from applied.json
        #    (re-enters the scrape pool). k3 has no pending file.
        assert _set_state("k3", "in_queue")["ok"] is True; checks += 1
        assert "k3" not in triage.applied_keys(), "un-submit must drop it from applied"; checks += 1

        # 7) skipped is invisible to the flush (only APPROVED is pulled) and to re-serving.
        pending.put(_job("Echo", "SWE", "k5"), [], [])   # zero-question, complete
        pending.skip("k5")
        jobs, held = B._approved_not_yet_applied()
        assert all(j["dedup_key"] != "k5" for j in jobs), "skipped must not flush"; checks += 1
        # handle_queue excludes every pending key, so a skipped role isn't re-served either.
        get_store().set_queue("queue", [_job("Echo", "SWE", "k5")])
        ws = FakeWS()
        asyncio.run(B.handle_queue({"reqId": "q", "type": "queue"}, ws))
        served = [j["dedup_key"] for j in ws.sent[0]["answers"]["jobs"]]
        assert "k5" not in served, served; checks += 1

    print(f"dashboard: {checks}/{checks} checks passed")


if __name__ == "__main__":
    run()
