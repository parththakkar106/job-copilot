#!/usr/bin/env python3
"""Tests for the batch flow: Telegram as a stateless view over bridge/pending/.

Drives the handlers the way the poller does (callback strings + typed text) and asserts
on what would have been sent. Uses temp dirs for both the pending store and applied.json
— never the live ones.
"""
import asyncio
import json
import tempfile
from pathlib import Path

import bridge as B
import pending as P
import triage as T
from server.store import use_temp_store

SENT: list = []


def setup(tmp: Path):
    """Point every module at throwaway state and capture Telegram sends."""
    use_temp_store(tmp)
    B.LOCK = asyncio.Lock()
    B.INBOX = asyncio.Queue()
    SENT.clear()
    B.tg_send = lambda text, buttons=None: SENT.append((text, buttons)) or 1
    B.tg_answer_callback = lambda *a, **k: None
    # Layer 1 would otherwise read the real answer_bank off disk.
    B.bank_draft = lambda item, company: (item.get("draft"), None)


def job(key, company="Ramp", title="Software Engineer"):
    return {"dedup_key": key, "company": company, "title": title,
            "location": "New York", "url": f"https://jobs.example/{key}",
            "compensation": "$150k-$200k"}


QS = [
    {"id": "ask_0", "label": "Why Ramp?", "required": True},
    {"id": "ask_1", "label": "Pronouns", "required": False,
     "options": ["He/Him", "She/Her", "They/Them"]},
]
FILLED = [{"label": "Email", "value": "jordan.lee@example.com"}]


def buttons_flat(bs):
    return [b["callback_data"] for row in (bs or []) for b in row]


async def test_answer_cycle():
    """The core loop: question -> answer -> next question -> summary -> approve."""
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), QS, FILLED)

    await B.serve_next()
    text, bs = SENT[-1]
    assert "Why Ramp?" in text and "Q1 of 2" in text, text
    assert "🔴" in text, "required questions must say so"
    n += 1
    # required -> no leave-blank escape hatch, but always an explicit way to walk away
    assert not any("blank" in c for c in buttons_flat(bs)), bs
    assert f"{B.CB}:later" in buttons_flat(bs), bs
    n += 1

    # a typed reply answers the current question and rolls straight on to the next
    assert await B.answer_current("Because I like Ramp.") is True
    text, bs = SENT[-1]
    assert "Pronouns" in text and "Q2 of 2" in text, text
    n += 1
    # options render as taps; optional -> leave-blank offered
    assert f"{B.CB}:o:aaa:ask_1:0" in buttons_flat(bs), bs
    assert f"{B.CB}:a:aaa:ask_1:blank" in buttons_flat(bs), bs
    n += 1

    await B.batch_callback(f"{B.CB}:o:aaa:ask_1:0", "cb")
    assert P.load("aaa")["questions"][1]["answer"] == "He/Him"
    n += 1

    # everything answered -> the summary gate, showing fills AND answers
    text, bs = SENT[-1]
    assert "ready to submit" in text, text
    assert "jordan.lee@example.com" in text and "Because I like Ramp." in text, text
    n += 1
    assert buttons_flat(bs) == [f"{B.CB}:j:aaa:approve", f"{B.CB}:j:aaa:skip",
                                f"{B.CB}:j:aaa:reject"], bs
    n += 1

    await B.batch_callback(f"{B.CB}:j:aaa:approve", "cb")
    assert P.load("aaa")["status"] == P.APPROVED
    n += 1
    assert "Nothing left" in SENT[-1][0], SENT[-1][0]
    n += 1
    return n


async def test_walk_away_and_return():
    """The whole point: answer one, vanish, come back. State is on disk, so a bridge
    restart mid-batch changes nothing."""
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), QS, FILLED)
    await B.serve_next()
    await B.answer_current("Because I like Ramp.")

    await B.batch_callback(f"{B.CB}:later", "cb")
    assert "Paused" in SENT[-1][0], SENT[-1][0]
    n += 1

    # simulate a restart: nothing in memory survives, only the stored records
    B.LOCK, B.INBOX = asyncio.Lock(), asyncio.Queue()

    await B.serve_next()
    assert "Pronouns" in SENT[-1][0], "must resume at the exact question he left on"
    n += 1
    assert P.load("aaa")["questions"][0]["answer"] == "Because I like Ramp."
    n += 1
    return n


async def test_skip_vs_reject():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa", "Ramp"), [], FILLED)
    P.put(job("bbb", "Notion"), [], FILLED)

    # skip -> no record written, so tomorrow's queue serves it again
    await B.batch_callback(f"{B.CB}:j:aaa:skip", "cb")
    assert P.load("aaa") is None
    n += 1
    assert T.applied_keys() == set(), "skip must leave no permanent trace"
    n += 1
    assert "come back in tomorrow" in SENT[-2][0], SENT[-2][0]
    n += 1

    # reject -> permanent record, excluded from every future queue
    await B.batch_callback(f"{B.CB}:j:bbb:reject", "cb")
    assert P.load("bbb") is None
    n += 1
    rows = T.load_applied()
    assert rows[0]["dedup_key"] == "bbb" and rows[0]["status"] == "rejected", rows
    n += 1
    assert "won't see this role again" in SENT[-2][0], SENT[-2][0]
    n += 1
    return n


async def test_safety_gates():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), QS, FILLED)

    # a stale ✅ on a half-answered job must not approve it
    await B.batch_callback(f"{B.CB}:j:aaa:approve", "cb")
    assert P.load("aaa")["status"] == P.AWAITING, "half-answered must never approve"
    n += 1

    # a button from a message about a job that's since gone is a no-op, not a crash
    await B.batch_callback(f"{B.CB}:j:zzz:approve", "cb")
    await B.batch_callback(f"{B.CB}:a:zzz:ask_0:ok", "cb")
    await B.batch_callback(f"{B.CB}:a:aaa:ask_99:ok", "cb")
    n += 1

    # typed text with nothing pending falls through to the legacy ask flow
    setup(Path(tempfile.mkdtemp()))
    assert await B.answer_current("hello?") is False
    n += 1
    return n


async def test_summary_stays_under_telegram_limit():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    big = [{"label": f"Field {i}", "value": "x" * 300} for i in range(60)]
    P.put(job("aaa"), [], big)
    await B.serve_next()
    text, _ = SENT[-1]
    assert len(text) < 4096, f"would be rejected by Telegram: {len(text)}"
    n += 1
    assert "more fields" in text, "truncation should say what it hid"
    n += 1
    return n


async def test_status():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    await B.send_status()
    assert "Nothing pending" in SENT[-1][0]
    n += 1

    P.put(job("aaa"), QS, FILLED)
    P.put(job("bbb", "Notion"), [], FILLED)
    P.approve("bbb")
    await B.send_status()
    text = SENT[-1][0]
    assert "1 waiting on you (2 question(s) left)" in text, text
    assert "1 approved" in text, text
    n += 1
    return n


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


async def test_idle_flush():
    """The debounce releases ONLY the approved pile, and only after real quiet."""
    setup(Path(tempfile.mkdtemp()))
    n = 0
    ws = FakeWS()
    B.CLIENTS.clear()
    B.CLIENTS.add(ws)
    B.IDLE_FLUSH_SECS = 300
    B.IDLE_POLL_SECS = 0.02
    B._LAST_FLUSH = 0.0

    # one job he approved, one still half-answered
    P.put(job("aaa", "Ramp"), [], FILLED)
    P.approve("aaa")
    P.put(job("bbb", "Notion"), QS, FILLED)

    # still active -> nothing goes out
    B.touch_activity()
    task = asyncio.create_task(B.idle_watcher())
    await asyncio.sleep(0.05)
    assert ws.sent == [], "must not flush while he's still answering"
    n += 1

    # gone quiet past the threshold -> flush
    B.LAST_ACTIVITY = B.time.monotonic() - 400
    await asyncio.sleep(0.15)
    assert any(m.get("type") == "flush" for m in ws.sent), ws.sent
    n += 1

    text = SENT[-1][0]
    assert "1 approved" in text, text
    assert "1 still waiting on you" in text, "should report what's left"
    n += 1

    # the half-answered job is untouched — the debounce cannot reach it
    assert P.load("bbb")["status"] == P.AWAITING
    n += 1

    # and it doesn't re-fire on the next tick
    before = len(ws.sent)
    await asyncio.sleep(0.15)
    assert len(ws.sent) == before, "flush must not repeat every tick"
    n += 1

    task.cancel()
    B.CLIENTS.clear()
    return n


async def test_no_client_nudges_instead_of_stalling_silently():
    """Chrome gone + approved work waiting must NOT fail silently.

    Found live on 2026-07-15: the bridge restarted, MV3 evicted the worker, nothing
    reconnected, and an approved application sat there indefinitely with no signal at
    all. Parth would have assumed it went out. Staying quiet is the bug.
    """
    setup(Path(tempfile.mkdtemp()))
    n = 0
    B.CLIENTS.clear()
    B.IDLE_POLL_SECS = 0.02
    B._LAST_FLUSH = 0.0
    B._NUDGED = False
    P.put(job("aaa"), [], FILLED)
    P.approve("aaa")
    B.LAST_ACTIVITY = B.time.monotonic() - 400

    task = asyncio.create_task(B.idle_watcher())
    await asyncio.sleep(0.15)

    assert SENT, "silence is the bug — he must be told Chrome is disconnected"
    n += 1
    assert "isn't running" in SENT[-1][0], SENT[-1][0]
    n += 1
    assert P.load("aaa")["status"] == P.APPROVED, "still approved, waiting for a client"
    n += 1

    # ...exactly ONCE. Nothing changes until Chrome is back, so repeating it is pure
    # phone-buzzing — it woke Parth at 2am on 2026-07-16 before this was tightened.
    before = len(SENT)
    await asyncio.sleep(0.2)
    assert len(SENT) == before, "must nudge once per disconnection, not on a timer"
    n += 1
    task.cancel()

    # Chrome comes back and drops again -> that IS new, so one more nudge is right.
    ws = FakeWS()
    B.CLIENTS.add(ws)
    task = asyncio.create_task(B.idle_watcher())
    await asyncio.sleep(0.1)      # a tick with a client clears the latch
    B.CLIENTS.clear()
    await asyncio.sleep(0.1)
    assert len(SENT) > before, "a fresh disconnection deserves a fresh nudge"
    n += 1
    task.cancel()

    # No approved work + no client -> genuinely nothing to say.
    setup(Path(tempfile.mkdtemp()))
    B.CLIENTS.clear()
    B.IDLE_POLL_SECS = 0.02
    B._NUDGED = False
    P.put(job("bbb"), QS, FILLED)  # awaiting, not approved
    B.LAST_ACTIVITY = B.time.monotonic() - 400
    task = asyncio.create_task(B.idle_watcher())
    await asyncio.sleep(0.15)
    assert SENT == [], "nothing approved -> nothing to nudge about"
    n += 1
    task.cancel()
    return n


async def test_no_double_apply():
    """Parth panel-applies to a role by hand that's also approved in the batch. Phase 3
    must not open it and apply again — Ramp's server rejects duplicates and it burns a
    real application slot."""
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa", "Ramp"), [], FILLED)
    P.approve("aaa")
    P.put(job("bbb", "Notion"), [], FILLED)
    P.approve("bbb")

    # he applied to Ramp manually since approving it
    T.append_applied({"dedup_key": "aaa", "company": "Ramp", "title": "Software Engineer"})

    jobs, held = B._approved_not_yet_applied()
    assert [r["dedup_key"] for r in jobs] == ["bbb"], jobs
    assert held == []
    n += 1
    # and the stale record is retired rather than lingering in /status forever
    assert P.load("aaa") is None
    n += 1

    ws = FakeWS()
    await B.handle_approved({"reqId": "r1"}, ws)
    assert [r["dedup_key"] for r in ws.sent[0]["answers"]["jobs"]] == ["bbb"], ws.sent
    n += 1
    return n


async def test_applied_retires_pending():
    """A confirmed submit must clear the pending file, or /status counts it forever."""
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), [], FILLED)
    P.approve("aaa")

    ws = FakeWS()
    await B.handle_applied({"reqId": "r1", "record": {
        "dedup_key": "aaa", "company": "Ramp", "title": "Software Engineer"}}, ws)
    assert ws.sent[0]["answers"]["ok"] is True, ws.sent
    n += 1
    assert P.load("aaa") is None, "pending record should be retired once applied"
    n += 1
    assert "aaa" in T.applied_keys()
    n += 1
    return n


async def test_flush_attempts_cap_then_hold():
    """A job phase 3 can never confirm must stop being re-flushed. After MAX_FLUSH_ATTEMPTS
    serves without landing in applied.json it's HELD (out of the auto pile) and Parth is
    told once — that's the fix for the every-5-minutes retry nag."""
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa", "Neon"), [], FILLED)
    P.approve("aaa")

    # Each flush serves the job once (simulating the extension asking for the approved pile
    # and failing to confirm — so it never gets logged applied).
    for i in range(B.MAX_FLUSH_ATTEMPTS):
        jobs, held = B._approved_not_yet_applied()
        assert [r["dedup_key"] for r in jobs] == ["aaa"], (i, jobs)
        assert held == []
        n += 1
    # The next flush is over the cap. Drive it through handle_approved so we also verify
    # the extension gets an empty pile AND Parth is pinged exactly once about the held job.
    SENT.clear()
    ws = FakeWS()
    await B.handle_approved({"reqId": "r"}, ws)
    assert ws.sent[0]["answers"]["jobs"] == [], ws.sent
    n += 1
    assert P.load("aaa")["status"] == P.HELD
    n += 1
    # Held jobs drop out of the approved count so idle_watcher stops firing.
    assert P.counts()["approved"] == 0 and P.counts()["held"] == 1
    n += 1
    assert any("Neon" in s[0] for s in SENT), SENT
    n += 1

    # /retry puts it back in play with a clean counter.
    SENT.clear()
    await B.handle_retry()
    rec = P.load("aaa")
    assert rec["status"] == P.APPROVED and rec.get("flush_attempts", 0) == 0, rec
    n += 1
    jobs, held = B._approved_not_yet_applied()
    assert [r["dedup_key"] for r in jobs] == ["aaa"] and held == []
    n += 1
    return n


async def main():
    n = 0
    for t in (test_answer_cycle, test_walk_away_and_return, test_skip_vs_reject,
              test_safety_gates, test_summary_stays_under_telegram_limit, test_status,
              test_idle_flush, test_no_client_nudges_instead_of_stalling_silently, test_no_double_apply,
              test_applied_retires_pending, test_flush_attempts_cap_then_hold):
        n += await t()
    print(f"batch: {n}/{n} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
