#!/usr/bin/env python3
"""Checks for the console's link queue and the human check on pasted links.

    python bridge/test_link_queue.py
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as B  # noqa: E402

checks = 0


def ok(cond, msg=""):
    global checks
    assert cond, msg
    checks += 1


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def put(link_id, **kw):
    row = {"id": link_id, "url": "https://x/" + link_id, "form_url": "https://f/" + link_id,
           "status": "waiting", "attempts": 0, **kw}
    B._link_path(link_id).write_text(json.dumps(row), encoding="utf-8")


def row(link_id):
    return json.loads(B._link_path(link_id).read_text(encoding="utf-8"))


async def run():
    ws = FakeWS()
    await B.serve_link()                 # nothing connected, nothing queued
    put("1-aaaaaa")
    await B.serve_link()
    ok(row("1-aaaaaa")["status"] == "waiting", "pushed with no client")

    B.CLIENTS.add(ws)
    put("2-bbbbbb")
    await B.serve_link()
    ok(ws.sent[-1] == {"type": "apply_url", "url": "https://f/1-aaaaaa", "link_id": "1-aaaaaa"})
    ok(row("1-aaaaaa")["status"] == "running" and row("1-aaaaaa")["attempts"] == 1)
    await B.serve_link()
    ok(len([m for m in ws.sent if m["type"] == "apply_url"]) == 1, "two fills at once")

    # Busy puts it back, with a wait and no attempt spent.
    await B.handle_link_done({"link_id": "1-aaaaaa", "busy": True, "reqId": "r"}, ws)
    ok(row("1-aaaaaa")["status"] == "waiting" and row("1-aaaaaa")["attempts"] == 0)
    await B.serve_link()
    ok(ws.sent[-1].get("link_id") == "2-bbbbbb", "the busy link did not wait its turn")

    # Success removes it, and a failure stays visible with its reason.
    await B.handle_link_done({"link_id": "2-bbbbbb", "ok": True, "reqId": "r"}, ws)
    ok(not B._link_path("2-bbbbbb").exists())
    put("3-cccccc")
    B._link_path("1-aaaaaa").unlink()
    await B.serve_link()
    await B.handle_link_done({"link_id": "3-cccccc", "note": "no form", "reqId": "r"}, ws)
    ok(row("3-cccccc")["status"] == "failed" and row("3-cccccc")["note"] == "no form")

    # An answer that never comes retries, then gives up.
    late = time.time() - B.LINK_ANSWER_SECS - 1
    put("4-dddddd", status="running", sent_at=late, attempts=1)
    ok(B._next_link(time.time())["id"] == "4-dddddd")
    put("4-dddddd", status="running", sent_at=late, attempts=B.LINK_MAX_ATTEMPTS)
    ok(B._next_link(time.time()) is None)
    ok(row("4-dddddd")["status"] == "failed")

    # A hostile id in the answer touches nothing outside the queue folder.
    outside = Path(B.LINK_QUEUE_DIR).parent / "evil.json"
    outside.write_text("{}")
    await B.handle_link_done({"link_id": "../evil", "ok": True, "reqId": "r"}, ws)
    ok(outside.exists())

    # Only a first attempt restarts the idle clock, so retries cannot hold back a send.
    put("7-abcdef", attempts=1)
    B.LAST_ACTIVITY = 1.0
    await B.serve_link()
    ok(B.LAST_ACTIVITY == 1.0, "a retry restarted the idle clock")
    B._link_path("7-abcdef").unlink()
    put("8-abcdef")
    await B.serve_link()
    ok(B.LAST_ACTIVITY != 1.0, "a new link left the idle clock alone")
    B._link_path("8-abcdef").unlink()
    B.CLIENTS.discard(ws)

    # A link removed while its fill runs stays removed.
    put("5-eeeeee")
    stale = row("5-eeeeee")
    B._link_path("5-eeeeee").unlink()
    B._save_link(stale)
    ok(not B._link_path("5-eeeeee").exists())

    # A pasted link never auto-approves. A queue role still can.
    approved = []

    # A confident fill clears the lower bar for a console link, and an essay never does.
    rec = {"questions": [], "ai_decisions": [{"action": "fill", "label": "City", "value": "Ohio",
                                              "confidence": 0.85}]}
    ok(B.pending_mod.auto_block(rec, B.pending_mod.LINK_MIN_CONF) is None)
    ok(B.pending_mod.auto_block(rec) is not None, "the default bar moved")
    essay = {"questions": [], "ai_decisions": [{"action": "fill", "label": "Why", "value": "x" * 200,
                                                "confidence": 0.99}]}
    ok("essay" in B.pending_mod.auto_block(essay, B.pending_mod.LINK_MIN_CONF))

    async def fake_auto(rec, min_conf=B.pending_mod.AUTO_MIN_CONF):
        approved.append((rec["dedup_key"], min_conf))

    real_auto = B._auto_approve
    B._auto_approve = fake_auto
    B.drafter_mod.available = lambda: False
    B.resolve_dedup_key = lambda job: "pasted1"
    B.posted_from_queue = lambda key: ""
    job = {"company": "Acme", "title": "SWE", "url": "https://x/1"}
    await B.handle_collect({"reqId": "a", "job": {**job, "dedup_key": ""}, "questions": []}, ws)
    ok(approved == [], "a Telegram link was auto-approved")
    await B.handle_collect({"reqId": "b", "job": {**job, "dedup_key": "queued1"},
                            "questions": []}, ws)
    ok(approved == [("queued1", B.pending_mod.AUTO_MIN_CONF)], approved)

    # A console link binds to its record and uses the lower bar.
    approved.clear()
    put("6-ffffff", status="running")
    B.resolve_dedup_key = lambda job: "linked1"
    await B.handle_collect({"reqId": "c", "job": {**job, "dedup_key": "", "link_id": "6-ffffff"},
                            "questions": []}, ws)
    ok(approved == [("linked1", B.pending_mod.LINK_MIN_CONF)], approved)
    ok(row("6-ffffff")["status"] == "filled" and row("6-ffffff")["dedup_key"] == "linked1")

    # A filled link stays listed. Only an auto-approved role triggers a send.
    sends = []

    async def fake_flush(source):
        sends.append(source)

    B.fire_flush = fake_flush
    await B.handle_link_done({"link_id": "6-ffffff", "ok": True, "reqId": "d"}, ws)
    ok(B._link_path("6-ffffff").exists() and sends == [], sends)
    rec = B.pending_mod.load("linked1")
    rec["auto_approved"] = True
    B.pending_mod.save(rec)
    await B.handle_link_done({"link_id": "6-ffffff", "ok": True, "reqId": "e"}, ws)
    ok(sends == ["link"], sends)
    B._auto_approve = real_auto


def main():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        B.LINK_QUEUE_DIR = tmp / "links"
        B.LINK_QUEUE_DIR.mkdir()
        B.pending_mod.PENDING_DIR = tmp / "pending"
        B.pending_mod.PENDING_DIR.mkdir()
        B.pending_mod.REFERRAL_WAIT_PATH = tmp / "referral_wait.json"
        B.tg_send = lambda *a, **k: 1
        asyncio.run(run())
    print(f"link_queue: {checks}/{checks} checks passed")


if __name__ == "__main__":
    main()
