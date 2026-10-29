#!/usr/bin/env python3
"""Tests for the autonomous daily-run trigger path.

Covers push_run_queue / fire_run_queue / trigger_watcher — the WS-free sentinel-file
handoff the daily launcher uses to start a P1 collection pass (see run_daily.ps1).
No Telegram, no browser, no sockets: tg_send is stubbed and CLIENTS holds fakes.
"""
import asyncio
import json
import pathlib
import tempfile

import bridge as B

# fire_run_queue runs the expiry sweep, which rewrites pending records. Point it at an empty
# temp dir so this test can never expire Parth's real batch.
B.pending_mod.PENDING_DIR = pathlib.Path(tempfile.mkdtemp())


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _stub_tg():
    msgs = []
    B.tg_send = lambda text, buttons=None: msgs.append(text) or 1
    return msgs


async def scenario():
    checks = 0

    # 1. push_run_queue reaches every connected client.
    a, b = FakeWS(), FakeWS()
    B.CLIENTS = {a, b}
    await B.push_run_queue()
    assert [m for m in a.sent if m.get("type") == "run_queue"], a.sent; checks += 1
    assert [m for m in b.sent if m.get("type") == "run_queue"], b.sent; checks += 1

    # 2. fire_run_queue with a client connected pushes the run and arms the cooldown.
    _stub_tg()
    B._LAST_RUN_KICK = 0.0
    ws = FakeWS()
    B.CLIENTS = {ws}
    await B.fire_run_queue("test")
    assert [m for m in ws.sent if m.get("type") == "run_queue"], ws.sent; checks += 1
    assert B._LAST_RUN_KICK > 0, "cooldown timestamp should be set after a real kick"; checks += 1

    # 3. A second immediate kick is swallowed by the cooldown (no double collection run).
    ws.sent.clear()
    await B.fire_run_queue("test")
    assert not [m for m in ws.sent if m.get("type") == "run_queue"], "cooldown must block the double-fire"; checks += 1

    # 4. No client connected -> wait out the window, then notify (never a silent miss).
    msgs = _stub_tg()
    B._LAST_RUN_KICK = 0.0
    B.CLIENTS = set()
    orig_wait, orig_poll = B.RUN_CLIENT_WAIT_SECS, B.TRIGGER_POLL_SECS
    B.RUN_CLIENT_WAIT_SECS, B.TRIGGER_POLL_SECS = 0.05, 0.01
    try:
        await B.fire_run_queue("test")
    finally:
        B.RUN_CLIENT_WAIT_SECS, B.TRIGGER_POLL_SECS = orig_wait, orig_poll
    assert any("never connected" in m for m in msgs), msgs; checks += 1
    assert B._LAST_RUN_KICK == 0.0, "a run that never started must not arm the cooldown"; checks += 1

    # 5. trigger_watcher: a boot-time stale sentinel is cleared, then a fresh drop fires once.
    _stub_tg()
    B._LAST_RUN_KICK = 0.0
    with tempfile.TemporaryDirectory() as d:
        B.TRIGGER_FILE = pathlib.Path(d) / ".run_trigger"
        B.TRIGGER_FILE.write_text("stale")  # left from a prior crash
        ws2 = FakeWS()
        B.CLIENTS = {ws2}
        orig_poll = B.TRIGGER_POLL_SECS
        B.TRIGGER_POLL_SECS = 0.01
        task = asyncio.create_task(B.trigger_watcher())
        try:
            await asyncio.sleep(0.05)
            # Boot-clear removed the stale file and it did NOT fire a run.
            assert not B.TRIGGER_FILE.exists(), "stale sentinel should be cleared at startup"; checks += 1
            assert not [m for m in ws2.sent if m.get("type") == "run_queue"], "stale file must not fire a run"; checks += 1
            # A genuine drop is picked up, consumed, and fires exactly one run.
            B.TRIGGER_FILE.write_text("go")
            await asyncio.sleep(0.1)
            assert not B.TRIGGER_FILE.exists(), "sentinel should be consumed"; checks += 1
            assert [m for m in ws2.sent if m.get("type") == "run_queue"], ws2.sent; checks += 1
        finally:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            B.TRIGGER_POLL_SECS = orig_poll

    print(f"run_trigger: {checks}/{checks} checks passed")


if __name__ == "__main__":
    asyncio.run(scenario())
