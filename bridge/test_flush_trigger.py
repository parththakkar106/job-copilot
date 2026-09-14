#!/usr/bin/env python3
"""Tests for the web console's flush-trigger path.

Covers fire_flush / flush_trigger_watcher — the WS-free sentinel-file handoff the web app
(web/) uses to ask the bridge to send the approved pile, without opening a WebSocket that
would supersede Parth's live extension session. No Telegram, no browser, no sockets:
tg_send is stubbed, CLIENTS holds fakes, and pending.counts is monkeypatched.
"""
import asyncio
import json
import pathlib
import tempfile

import bridge as B


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _stub_tg():
    msgs = []
    B.tg_send = lambda text, buttons=None: msgs.append(text) or 1
    return msgs


def _stub_counts(approved):
    B.pending_mod.counts = lambda base=None: {
        "jobs": approved, "awaiting": 0, "approved": approved,
        "held": 0, "skipped": 0, "ready": 0, "questions_left": 0,
    }


async def scenario():
    checks = 0

    # 1. fire_flush with a client + approved pile pushes a flush to every client.
    _stub_tg(); _stub_counts(3)
    a, b = FakeWS(), FakeWS()
    B.CLIENTS = {a, b}
    await B.fire_flush("test")
    assert [m for m in a.sent if m.get("type") == "flush"], a.sent; checks += 1
    assert [m for m in b.sent if m.get("type") == "flush"], b.sent; checks += 1

    # 2. Nothing approved -> no flush (don't wake the extension for an empty pile).
    _stub_tg(); _stub_counts(0)
    ws = FakeWS(); B.CLIENTS = {ws}
    await B.fire_flush("test")
    assert not [m for m in ws.sent if m.get("type") == "flush"], "empty pile must not flush"; checks += 1

    # 3. No client connected but work approved -> notify, never a silent miss.
    msgs = _stub_tg(); _stub_counts(2)
    B.CLIENTS = set()
    await B.fire_flush("test")
    assert any("isn't connected" in m for m in msgs), msgs; checks += 1

    # 4. flush_trigger_watcher: stale sentinel cleared at boot, fresh drop fires one flush.
    _stub_tg(); _stub_counts(1)
    with tempfile.TemporaryDirectory() as d:
        B.FLUSH_TRIGGER_FILE = pathlib.Path(d) / ".flush_trigger"
        B.FLUSH_TRIGGER_FILE.write_text("stale")  # left from a prior crash
        ws2 = FakeWS(); B.CLIENTS = {ws2}
        orig_poll = B.TRIGGER_POLL_SECS
        B.TRIGGER_POLL_SECS = 0.01
        task = asyncio.create_task(B.flush_trigger_watcher())
        try:
            await asyncio.sleep(0.05)
            assert not B.FLUSH_TRIGGER_FILE.exists(), "stale sentinel should be cleared at startup"; checks += 1
            assert not [m for m in ws2.sent if m.get("type") == "flush"], "stale file must not fire a flush"; checks += 1
            B.FLUSH_TRIGGER_FILE.write_text("go")
            await asyncio.sleep(0.1)
            assert not B.FLUSH_TRIGGER_FILE.exists(), "sentinel should be consumed"; checks += 1
            assert [m for m in ws2.sent if m.get("type") == "flush"], ws2.sent; checks += 1
        finally:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            B.TRIGGER_POLL_SECS = orig_poll

    # 5. activity_watcher: a web-console ping restarts the idle countdown.
    with tempfile.TemporaryDirectory() as d:
        B.ACTIVITY_PING_FILE = pathlib.Path(d) / ".activity"
        B.ACTIVITY_PING_FILE.write_text("stale")  # left from a prior crash
        B.LAST_ACTIVITY = 0.0
        orig_poll = B.TRIGGER_POLL_SECS
        B.TRIGGER_POLL_SECS = 0.01
        task = asyncio.create_task(B.activity_watcher())
        try:
            await asyncio.sleep(0.05)
            assert not B.ACTIVITY_PING_FILE.exists(), "stale ping should be cleared at startup"; checks += 1
            assert B.LAST_ACTIVITY == 0.0, "a stale ping must not count as activity"; checks += 1
            B.ACTIVITY_PING_FILE.write_text("edit")
            await asyncio.sleep(0.1)
            assert not B.ACTIVITY_PING_FILE.exists(), "ping should be consumed"; checks += 1
            assert B.LAST_ACTIVITY > 0.0, "a fresh ping must restart the idle countdown"; checks += 1
        finally:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            B.TRIGGER_POLL_SECS = orig_poll

    print(f"flush_trigger: {checks}/{checks} checks passed")


if __name__ == "__main__":
    asyncio.run(scenario())
