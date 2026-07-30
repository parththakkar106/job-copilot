#!/usr/bin/env python3
"""Tests for Layer 1: answer_bank.yaml answers served as Telegram drafts.

Covers bank_draft() resolution and the touch-on-accept path in handle_ask. Runs
against a TEMP bank file — never the real answer_bank.yaml, which holds Parth's
live answers and whose uses/last_used counters are real data.
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

import yaml

# core/bank.py lives under the repo root. bridge.py adds the root to sys.path when it
# imports, but don't rely on import order to make this file work — put it there explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import bank as bank_mod
import bridge as B


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


ROWS = [
    {"id": "why_company_ramp", "key": "why_company", "scope": "company", "company": "Ramp",
     "answer": "Ramp-specific answer.", "status": "active", "uses": 0, "last_used": None},
    {"id": "why_company_common", "key": "why_company", "scope": "common", "company": None,
     "answer": "Generic answer.", "status": "active", "uses": 0, "last_used": None},
    {"id": "why_role_stale", "key": "why_role", "scope": "common", "company": None,
     "answer": "Should never be served.", "status": "stale", "uses": 0, "last_used": None},
]


def use_temp_bank(rows):
    """Point bank.py at a throwaway file seeded with `rows`. Returns its Path."""
    tmp = Path(tempfile.mkdtemp()) / "answer_bank.yaml"
    tmp.write_text(yaml.safe_dump({"answers": rows}, sort_keys=False), encoding="utf-8")
    # BOTH, always. bank.py reads BANK and writes BANK_WRITE (they differ on a fresh
    # install, where the read falls back to the shipped template). Rebinding only the read
    # sent bank.save() -- which the use-counter calls on every hit -- straight at the real
    # data/answer_bank.yaml and clobbered it with these fixtures.
    bank_mod.BANK = tmp
    bank_mod.BANK_WRITE = tmp
    return tmp


def test_resolution():
    use_temp_bank(ROWS)
    checks = 0

    # company-scoped answer beats the common one
    draft, bid = B.bank_draft({"answer_key": "why_company"}, "Ramp")
    assert draft == "Ramp-specific answer." and bid == "why_company_ramp", (draft, bid)
    checks += 1

    # unknown company -> falls back to the common answer
    draft, bid = B.bank_draft({"answer_key": "why_company"}, "Notion")
    assert draft == "Generic answer." and bid == "why_company_common", (draft, bid)
    checks += 1

    # no company at all -> common answer
    draft, bid = B.bank_draft({"answer_key": "why_company"}, None)
    assert draft == "Generic answer.", draft
    checks += 1

    # a non-active row is never served (status gate)
    assert B.bank_draft({"answer_key": "why_role"}, None) == (None, None)
    checks += 1

    # key with no bank entry at all -> ask cold
    assert B.bank_draft({"answer_key": "cover_letter"}, None) == (None, None)
    checks += 1

    # always_ask / unrecognized items carry no answer_key -> no lookup, no draft
    assert B.bank_draft({"label": "Desired salary"}, "Ramp") == (None, None)
    checks += 1

    # a draft the extension already supplied wins and is NOT attributed to the bank
    draft, bid = B.bank_draft({"answer_key": "why_company", "draft": "preset"}, "Ramp")
    assert draft == "preset" and bid is None, (draft, bid)
    checks += 1

    # a broken bank degrades to asking cold rather than taking the fill down with it
    bad = Path(tempfile.mkdtemp()) / "answer_bank.yaml"
    bad.write_text("{{{ not yaml", encoding="utf-8")
    bank_mod.BANK = bad
    bank_mod.BANK_WRITE = bad
    assert B.bank_draft({"answer_key": "why_company"}, "Ramp") == (None, None)
    checks += 1

    return checks


async def scenario():
    """handle_ask: a bank-backed draft renders with a 👍 button, and accepting it
    touches the row while typing over it does not."""
    checks = 0
    B.INBOX = asyncio.Queue()
    B.LOCK = asyncio.Lock()
    sent = []
    B.tg_send = lambda text, buttons=None: sent.append((text, buttons)) or 1
    B.tg_answer_callback = lambda *a, **k: None

    # --- accepting the draft ---
    path = use_temp_bank([dict(r) for r in ROWS])
    ws = FakeWS()
    t = asyncio.create_task(B.handle_ask(
        {"reqId": "r1", "context": {"company": "Ramp"},
         "items": [{"id": "q1", "label": "Why Ramp?", "answer_key": "why_company",
                    "required": True}]}, ws))
    await asyncio.sleep(0.05)

    text, buttons = sent[-1]
    assert "Ramp-specific answer." in text, text
    assert "your saved answer" in text, "bank drafts should be labelled as Parth's own"
    assert any(b[0]["callback_data"] == "ok" for b in buttons), buttons
    checks += 1
    # required -> no "leave blank" escape hatch
    assert not any(b[0]["callback_data"] == "skip" for b in buttons), buttons
    checks += 1

    await B.INBOX.put({"kind": "callback", "data": "ok", "cb_id": "c1"})
    await asyncio.wait_for(t, timeout=2)
    assert ws.sent[0]["answers"] == {"q1": "Ramp-specific answer."}, ws.sent
    checks += 1

    rows = yaml.safe_load(path.read_text(encoding="utf-8"))["answers"]
    used = next(r for r in rows if r["id"] == "why_company_ramp")
    assert used["uses"] == 1 and used["last_used"], used
    checks += 1
    untouched = next(r for r in rows if r["id"] == "why_company_common")
    assert untouched["uses"] == 0, "only the row actually served should be touched"
    checks += 1

    # --- typing over the draft ---
    path = use_temp_bank([dict(r) for r in ROWS])
    ws = FakeWS()
    t = asyncio.create_task(B.handle_ask(
        {"reqId": "r2", "context": {"company": "Ramp"},
         "items": [{"id": "q1", "label": "Why Ramp?", "answer_key": "why_company"}]}, ws))
    await asyncio.sleep(0.05)
    await B.INBOX.put({"kind": "text", "text": "my own words"})
    await asyncio.wait_for(t, timeout=2)
    assert ws.sent[0]["answers"] == {"q1": "my own words"}, ws.sent
    checks += 1

    rows = yaml.safe_load(path.read_text(encoding="utf-8"))["answers"]
    rejected = next(r for r in rows if r["id"] == "why_company_ramp")
    assert rejected["uses"] == 0 and rejected["last_used"] is None, (
        "a draft Parth overrode is not a 'use' — it should go stale so he rewrites it")
    checks += 1

    return checks


async def scenario_panel():
    """handle_drafts / handle_bank_touch: the on-page panel gets the same bank answers
    without Telegram, and the touch-on-accept rule survives the move."""
    checks = 0
    sent = []
    B.tg_send = lambda text, buttons=None: sent.append((text, buttons)) or 1

    path = use_temp_bank([dict(r) for r in ROWS])
    ws = FakeWS()
    await B.handle_drafts({"reqId": "d1", "context": {"company": "Ramp"}, "items": [
        {"id": "q1", "answer_key": "why_company"},   # bank hit -> draft + id to touch
        {"id": "q2", "answer_key": "why_role"},      # stale row -> not served
        {"id": "q3", "label": "Desired salary"},     # always_ask -> no key, no draft
    ]}, ws)
    answers = ws.sent[0]["answers"]
    assert answers["q1"] == {"draft": "Ramp-specific answer.", "bank_id": "why_company_ramp"}, answers
    checks += 1
    assert "q2" not in answers and "q3" not in answers, "only real drafts should come back"
    checks += 1
    assert sent == [], "the panel path must not message Telegram — he's at the keyboard"
    checks += 1

    # Offering a draft is not using it: nothing is touched until he accepts.
    rows = yaml.safe_load(path.read_text(encoding="utf-8"))["answers"]
    assert next(r for r in rows if r["id"] == "why_company_ramp")["uses"] == 0, rows
    checks += 1

    # ...and accepting it in the panel touches the row, exactly as a 👍 does on Telegram.
    ws = FakeWS()
    await B.handle_bank_touch({"reqId": "t1", "bank_id": "why_company_ramp"}, ws)
    assert ws.sent[0]["answers"] == {"ok": True}, ws.sent
    checks += 1
    rows = yaml.safe_load(path.read_text(encoding="utf-8"))["answers"]
    used = next(r for r in rows if r["id"] == "why_company_ramp")
    assert used["uses"] == 1 and used["last_used"], used
    checks += 1

    # A bad id is reported, not raised: a failed touch must never break the panel's fill.
    ws = FakeWS()
    await B.handle_bank_touch({"reqId": "t2", "bank_id": "nope"}, ws)
    assert ws.sent[0]["answers"] == {"ok": False}, ws.sent
    checks += 1

    # force=True: the panel's "✨ Draft it" on a question the taxonomy never tagged. The bank
    # still wins when it has one; only a bank MISS reaches Claude. Stub the CLI boundary so
    # this spends nothing.
    orig_run, orig_enabled = B.drafter_mod._run_claude, B.drafter_mod._ENABLED
    orig_bin = B.drafter_mod.CLAUDE_BIN
    try:
        B.drafter_mod._ENABLED = True
        B.drafter_mod.CLAUDE_BIN = "/fake/claude"
        B.drafter_mod._run_claude = lambda prompt: "a drafted answer"
        ws = FakeWS()
        await B.handle_drafts({"reqId": "d3", "context": {"company": "Block"}, "force": True,
                               "items": [{"id": "qx", "label": "What does our purpose mean to you?"}]}, ws)
        assert ws.sent[0]["answers"]["qx"] == {"draft": "a drafted answer", "bank_id": None}, ws.sent[0]
        checks += 1
        # Same untagged question WITHOUT the tap must still come back empty — drafting on
        # request is the whole point of the flag; automatic drafting of anything unlabeled
        # would spend quota on every fill.
        ws = FakeWS()
        await B.handle_drafts({"reqId": "d4", "context": {"company": "Block"},
                               "items": [{"id": "qx", "label": "What does our purpose mean to you?"}]}, ws)
        assert ws.sent[0]["answers"] == {}, ws.sent[0]
        checks += 1
        # A bank hit still beats Claude even when forced — his own wording wins.
        ws = FakeWS()
        await B.handle_drafts({"reqId": "d5", "context": {"company": "Ramp"}, "force": True,
                               "items": [{"id": "q1", "answer_key": "why_company", "label": "Why us?"}]}, ws)
        assert ws.sent[0]["answers"]["q1"]["bank_id"] == "why_company_ramp", ws.sent[0]
        checks += 1
    finally:
        B.drafter_mod._run_claude, B.drafter_mod._ENABLED = orig_run, orig_enabled
        B.drafter_mod.CLAUDE_BIN = orig_bin

    # A broken bank degrades to "no drafts" rather than taking the panel down.
    bad = Path(tempfile.mkdtemp()) / "answer_bank.yaml"
    bad.write_text("{{{ not yaml", encoding="utf-8")
    bank_mod.BANK = bad
    bank_mod.BANK_WRITE = bad
    ws = FakeWS()
    await B.handle_drafts({"reqId": "d2", "context": {"company": "Ramp"},
                           "items": [{"id": "q1", "answer_key": "why_company"}]}, ws)
    assert ws.sent[0]["answers"] == {}, ws.sent
    checks += 1

    return checks


if __name__ == "__main__":
    # These tests cover the answer-bank layer in isolation. The Claude-drafting fallback
    # (bank miss -> generate a draft) is a separate feature with its own suite
    # (test_drafter.py); turn it off here so a bank miss stays "no draft", the behaviour
    # these assertions describe.
    B.drafter_mod._ENABLED = False
    n = test_resolution() + asyncio.run(scenario()) + asyncio.run(scenario_panel())
    print(f"bank-draft tests: {n}/{n} green")
