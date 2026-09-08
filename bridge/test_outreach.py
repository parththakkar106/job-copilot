#!/usr/bin/env python3
"""Tests for the referral surface: /refer, the contact card, its buttons, and the sweep.

Drives the handlers the way the poller does and asserts on what would have been sent.
The outreach store is a temp file every time — never the real one, which holds other
people's names. Every person here is invented and every profile slug carries the
`example-` prefix that scripts/pii_guard.py reserves for fixtures.
"""
import asyncio
import json
import tempfile
from pathlib import Path

import bridge as B
import drafter as drafter_mod

from core import referral as R
from server.store import get_store, use_temp_store
from server.store.importer import import_profile

SENT: list = []
PLAIN: list = []


def setup(tmp: Path):
    """Point the store at throwaway state and capture every Telegram send."""
    store = use_temp_store(tmp)
    # The pack names a resume file, so the profile's tracks have to be there.
    import_profile(store)
    SENT.clear()
    PLAIN.clear()
    B.REFER_WAITING = False
    B.tg_send = lambda text, buttons=None: SENT.append((text, buttons)) or 1
    B.tg_send_plain = lambda text: PLAIN.append(text) or 1
    B.tg_answer_callback = lambda *a, **k: None


PASTE = """Ivo Brennan
Senior Data Scientist at Northwind
Fairhaven, Vermont, United States
500+ connections
https://www.linkedin.com/in/example-ivo-brennan-31
"""

QUEUE_ROW = {"company": "Northwind", "title": "Data Scientist, Payments", "field": "ds",
             "ats": "greenhouse", "ats_job_id": "111", "dedup_key": "k1",
             "url": "https://job-boards.greenhouse.io/northwind/jobs/111",
             "posted": "2026-08-25T00:00:00Z", "location": "New York, NY"}


def buttons_flat(bs):
    return [b["callback_data"] for row in (bs or []) for b in row]


def button_labels(bs):
    return [b["text"] for row in (bs or []) for b in row]


async def test_paste_to_card():
    """The whole point: paste a profile, get a card naming a real req."""
    tmp = Path(tempfile.mkdtemp())
    setup(tmp)
    n = 0
    get_store().set_queue("queue", [QUEUE_ROW])

    await B.handle_refer("/refer " + PASTE)
    text, bs = SENT[-1]
    assert "Ivo Brennan" in text, text
    n += 1
    assert "Northwind" in text and "tier 1" in text, text
    n += 1
    assert "Data Scientist, Payments" in text and "req 111" in text, text
    n += 1
    # Nothing is drafted until asked: drafting costs a Claude call, and most pastes get
    # looked at and dropped.
    assert any(c.endswith(":draft") for c in buttons_flat(bs)), bs
    n += 1
    # The state machine is on the card, because `accepted` is the one transition the
    # pipeline can never observe for itself.
    assert any(c.endswith(":s:invited") for c in buttons_flat(bs)), bs
    n += 1
    # A Telegram callback_data field is 64 BYTES. The whole reason short_id exists.
    assert all(len(c.encode()) <= 64 for c in buttons_flat(bs)), buttons_flat(bs)
    n += 1
    # It was stored, so the same person pasted again is one row, not two.
    assert len(R.load_store()) == 1
    n += 1
    return n


async def test_paste_mode_beats_the_answer_path():
    """A bare /refer arms exactly one message, and a paste must never become an answer."""
    setup(Path(tempfile.mkdtemp()))
    n = 0
    await B.handle_refer("/refer")
    assert B.REFER_WAITING is True
    n += 1
    assert "paste" in SENT[-1][0].lower(), SENT[-1][0]
    n += 1
    await B.resolve_referral(PASTE)
    B.REFER_WAITING = False
    # The card, not necessarily the LAST send: an unknown company gets a follow-up line
    # after it saying how to fix that.
    assert any("Ivo Brennan" in t for t, _ in SENT), [t for t, _ in SENT]
    n += 1
    # The ordering that matters is in the poller: REFER_WAITING is consumed BEFORE
    # answer_current, or a pasted profile is stored as somebody's answer to "Why us?" and
    # submitted to a real employer. Asserted on the source, since the poller needs a network.
    src = Path(B.__file__).read_text(encoding="utf-8")
    assert src.index("if REFER_WAITING:") < src.index("if await answer_current(text)"), \
        "the armed paste must be consumed before the answer path"
    n += 1
    return n


async def test_tiers_and_the_live_offer():
    """Tier 3 offers the one-company pull; tier 4 says so plainly and offers nothing."""
    tmp = Path(tempfile.mkdtemp())
    setup(tmp)
    n = 0
    get_store().set_companies([{"name": "Northwind", "ats": "greenhouse",
                                "slug": "northwind", "tier": "pipeline"}])
    await B.resolve_referral(PASTE)
    text, bs = SENT[-1]
    assert "tier 3" in text, text
    n += 1
    assert any(c.endswith(":live") for c in buttons_flat(bs)), bs
    n += 1

    setup(tmp := Path(tempfile.mkdtemp()))
    await B.resolve_referral(PASTE)
    assert "tier 4" in SENT[-2][0], SENT[-2][0]
    n += 1
    # Tier 4 must say what to do about it rather than dead-ending.
    assert "companies.yaml" in SENT[-1][0], SENT[-1][0]
    n += 1
    assert not any(c.endswith(":live") for c in buttons_flat(SENT[-2][1])), SENT[-2][1]
    n += 1
    return n


async def test_no_profile_url_is_flagged():
    """A paste with no link resolves, but says it cannot remember the person."""
    setup(Path(tempfile.mkdtemp()))
    n = 0
    await B.resolve_referral("Ivo Brennan\nSenior Data Scientist at Northwind\n")
    assert any("can't remember" in t for t, _ in SENT), [t for t, _ in SENT]
    n += 1
    assert R.load_store() == {}, "a keyless contact must not be stored"
    n += 1
    return n


async def test_drafting_and_copying():
    """The draft button, and what a tapped message looks like when it is copied."""
    tmp = Path(tempfile.mkdtemp())
    setup(tmp)
    n = 0
    get_store().set_queue("queue", [QUEUE_ROW])
    await B.resolve_referral(PASTE)
    ref = R.short_id(list(R.load_store())[0])

    saved = (drafter_mod._run_claude, drafter_mod._ENABLED, drafter_mod.CLAUDE_BIN)
    try:
        drafter_mod._ENABLED, drafter_mod.CLAUDE_BIN = True, "stub"
        drafter_mod._run_claude = lambda prompt: json.dumps({
            "invite_note": "hi Ivo, we overlapped at the lab",
            "ask_dm": "one role, here's the link",
            "yes_pack": "the pack", "bump": "still keen", "already_applied": ""})
        await B.referral_callback(ref, ["draft"], "cb")
        text, bs = SENT[-1]
        assert any(c.endswith(":m:invite_note") for c in buttons_flat(bs)), bs
        n += 1
        # already_applied REPLACES the ask, so with no earlier application only ask_dm shows.
        assert any(c.endswith(":m:ask_dm") for c in buttons_flat(bs)), bs
        n += 1
        assert not any(c.endswith(":m:already_applied") for c in buttons_flat(bs)), bs
        n += 1
        # The drafts persisted, so a second look costs no Claude call.
        assert R.load_store()[list(R.load_store())[0]]["drafts"]["bump"] == "still keen"
        n += 1

        await B.referral_callback(ref, ["m", "invite_note"], "cb")
        # PLAIN, not SENT: the message is meant to be long-pressed and copied, and an
        # escaped apostrophe is what gets pasted into LinkedIn if this goes out as HTML.
        assert PLAIN and "hi Ivo, we overlapped at the lab" in PLAIN[-1], PLAIN
        n += 1
        assert "/300 characters" in PLAIN[-1], PLAIN[-1]
        n += 1

        # The pack names the resume file: the bridge has no sendDocument, and attaching the
        # wrong track's PDF is worse than fetching it by hand.
        await B.referral_callback(ref, ["m", "yes_pack"], "cb")
        assert "attach:" in PLAIN[-1], PLAIN[-1]
        n += 1

        # A variant that was not drafted must not send an empty message.
        before = len(PLAIN)
        await B.referral_callback(ref, ["m", "already_applied"], "cb")
        assert len(PLAIN) == before, PLAIN[-1]
        n += 1

        # Drafting off: no call, and it says why rather than failing silently.
        drafter_mod._ENABLED = False
        await B.referral_callback(ref, ["draft"], "cb")
        assert "off" in SENT[-1][0].lower(), SENT[-1][0]
        n += 1
    finally:
        (drafter_mod._run_claude, drafter_mod._ENABLED, drafter_mod.CLAUDE_BIN) = saved
    return n


async def test_states_and_stale_buttons():
    """State transitions write through, and a button for a vanished contact is not a crash."""
    tmp = Path(tempfile.mkdtemp())
    setup(tmp)
    n = 0
    await B.resolve_referral(PASTE)
    key = list(R.load_store())[0]
    ref = R.short_id(key)

    await B.referral_callback(ref, ["s", "invited"], "cb")
    assert R.load_store()[key]["state"] == "invited"
    n += 1
    assert "invited" in SENT[-1][0], SENT[-1][0]
    n += 1
    # The history is what the sweep ages, so it has to grow with each transition.
    await B.referral_callback(ref, ["s", "accepted"], "cb")
    assert len(R.load_store()[key]["history"]) == 3, R.load_store()[key]["history"]
    n += 1

    before = len(SENT)
    await B.referral_callback("deadbeef", ["s", "invited"], "cb")
    assert len(SENT) == before, "a stale button must not send anything"
    n += 1
    return n


async def test_the_follow_up_sweep():
    """The sweep asks about a quiet invite, once, and offers exactly one bump."""
    tmp = Path(tempfile.mkdtemp())
    setup(tmp)
    n = 0
    await B.resolve_referral(PASTE)
    key = list(R.load_store())[0]
    R.set_state(key, "invited")
    # Age the invite past the window.
    store = R.load_store()
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=R.NUDGE_ACCEPT_DAYS + 1)).isoformat()
    store[key]["history"][-1]["ts"] = old
    R.save_store(store)

    due = R.due_nudges()
    assert due == [(key, "accepted")], due
    n += 1
    # One sweep tick, driven directly rather than by waiting an hour.
    for k, kind in due:
        rec = R.load_store()[k]
        B.tg_send(f"did {rec['name']} accept?", [[{"text": "Yes", "callback_data":
                                                   f"{B.CB}:{B.CB_REF}:{R.short_id(k)}:s:accepted"}]])
        R.mark_nudged(k, kind)
    assert R.due_nudges() == [], "a nudge must fire once, not every hour"
    n += 1
    # The bump path opens only after the accept, and closes for good once bumped.
    R.set_state(key, "accepted")
    store = R.load_store()
    store[key]["history"][-1]["ts"] = (
        datetime.now(timezone.utc) - timedelta(days=R.NUDGE_BUMP_DAYS + 1)).isoformat()
    R.save_store(store)
    assert R.due_nudges() == [(key, "bump")], R.due_nudges()
    n += 1
    R.set_state(key, "bumped")
    assert R.due_nudges() == [], "after one bump the contact is dropped, not chased"
    n += 1
    return n


async def test_the_list():
    """/refers shows who is in flight and never claims an empty store is a problem."""
    tmp = Path(tempfile.mkdtemp())
    setup(tmp)
    n = 0
    await B.handle_outreach_list()
    assert "No contacts yet" in SENT[-1][0], SENT[-1][0]
    n += 1
    await B.resolve_referral(PASTE)
    await B.handle_outreach_list()
    assert "Ivo Brennan" in SENT[-1][0], SENT[-1][0]
    n += 1
    key = list(R.load_store())[0]
    R.set_state(key, "skipped")
    await B.handle_outreach_list()
    assert "0 in flight" in SENT[-1][0], SENT[-1][0]
    n += 1
    return n


def test_note_splitting():
    """Parth's own words are the best context the drafter gets, so they must survive."""
    n = 0
    body, notes = B._split_paste(PASTE + "\nnotes: same lab, two years above me")
    assert notes == "same lab, two years above me", notes
    n += 1
    assert "Ivo Brennan" in body
    n += 1
    body, notes = B._split_paste(PASTE + "\nmet him at a meetup")
    assert notes == "met him at a meetup", notes
    n += 1
    # A profile with no note attached must not lose its last section to this.
    body, notes = B._split_paste(PASTE)
    assert notes == "" and "linkedin.com" in body, (notes, body)
    n += 1
    return n


async def main():
    checks = test_note_splitting()
    for t in (test_paste_to_card, test_paste_mode_beats_the_answer_path,
              test_tiers_and_the_live_offer, test_no_profile_url_is_flagged,
              test_drafting_and_copying, test_states_and_stale_buttons,
              test_the_follow_up_sweep, test_the_list):
        checks += await t()
    print(f"outreach: {checks}/{checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
