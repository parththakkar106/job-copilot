#!/usr/bin/env python3
"""Tests for per-question required flag + the optional "Leave blank" skip button."""
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
    sent_msgs = []
    B.tg_send = lambda text, buttons=None: sent_msgs.append((text, buttons)) or 1
    B.tg_answer_callback = lambda *a, **k: None
    checks = 0

    # Optional item -> a "Leave blank" (skip) button is offered; tapping it => empty answer.
    ws = FakeWS()
    t = asyncio.create_task(B.handle_ask(
        {"reqId": "r1", "items": [{"id": "q1", "label": "Portfolio URL", "required": False}]}, ws))
    await asyncio.sleep(0.05)
    # the question message should advertise Optional + a skip button
    qtext, qbtns = sent_msgs[-1]
    assert "Optional" in qtext, qtext; checks += 1
    flat = [b["callback_data"] for row in (qbtns or []) for b in row]
    assert "skip" in flat, qbtns; checks += 1
    await B.INBOX.put({"kind": "callback", "data": "skip", "cb_id": "c1"})
    await asyncio.wait_for(t, timeout=2)
    ans = [m for m in ws.sent if m.get("type") == "answers"][0]["answers"]
    assert ans["q1"] == "", ans; checks += 1

    # Required item with options -> shows Required, NO skip button, option tap returns text.
    ws = FakeWS()
    t = asyncio.create_task(B.handle_ask(
        {"reqId": "r2", "items": [{"id": "q2", "label": "Authorized?", "required": True,
                                    "options": ["Yes", "No"]}]}, ws))
    await asyncio.sleep(0.05)
    qtext, qbtns = sent_msgs[-1]
    assert "Required" in qtext, qtext; checks += 1
    flat = [b["callback_data"] for row in (qbtns or []) for b in row]
    assert "skip" not in flat, qbtns; checks += 1
    await B.INBOX.put({"kind": "callback", "data": "opt:0", "cb_id": "c2"})
    await asyncio.wait_for(t, timeout=2)
    ans = [m for m in ws.sent if m.get("type") == "answers"][0]["answers"]
    assert ans["q2"] == "Yes", ans; checks += 1

    print(f"ask-skip: {checks}/{checks} checks passed")


if __name__ == "__main__":
    asyncio.run(scenario())
