#!/usr/bin/env python3
"""Regression test for the reload-mid-ask deadlock.

Before the fix, a handler blocked on a Telegram reply held LOCK forever; a reloaded
extension's questions could never acquire it. The ws_handler now cancels the stuck handler
on reconnect. This verifies the core invariant: cancelling a blocked handle_ask releases
LOCK, and a fresh ask then completes.
"""
import asyncio
import json

import bridge as B


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


async def scenario():
    B.INBOX = asyncio.Queue()
    B.LOCK = asyncio.Lock()
    B.tg_send = lambda text, buttons=None: 1
    B.tg_answer_callback = lambda *a, **k: None
    checks = 0

    # Session 1: an ask blocks awaiting a Telegram reply, holding LOCK.
    ws1 = FakeWS()
    t1 = asyncio.create_task(B.handle_ask({"reqId": "r1", "items": [{"id": "q1", "label": "Q1"}]}, ws1))
    await asyncio.sleep(0.05)
    assert B.LOCK.locked(), "handler should hold the lock while waiting"; checks += 1

    # Reload/reconnect supersede: cancel the stuck handler (what ws_handler now does).
    t1.cancel()
    try:
        await t1
    except BaseException:
        pass
    assert not B.LOCK.locked(), "lock must be free after the stuck handler is cancelled"; checks += 1

    # Session 2: the new ask must now go through (no deadlock).
    ws2 = FakeWS()
    t2 = asyncio.create_task(B.handle_ask({"reqId": "r2", "items": [{"id": "q2", "label": "Q2"}]}, ws2))
    await asyncio.sleep(0.05)
    await B.INBOX.put({"kind": "text", "text": "myanswer"})
    await asyncio.wait_for(t2, timeout=2)
    ans = [m for m in ws2.sent if m.get("type") == "answers"]
    assert ans and ans[0]["answers"].get("q2") == "myanswer", ws2.sent; checks += 1

    print(f"supersede: {checks}/{checks} checks passed")


if __name__ == "__main__":
    asyncio.run(scenario())
