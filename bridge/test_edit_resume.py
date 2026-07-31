#!/usr/bin/env python3
"""Tests for /edit and /resume — changing an answer or the resume BEFORE the ✅.

Same harness as test_batch.py: point the modules at throwaway state, capture Telegram
sends and callback-answer toasts, and drive the handlers the way the poller does.
"""
import asyncio
import tempfile
from pathlib import Path

import bridge as B
import pending as P
import triage as T

SENT: list = []
TOASTS: list = []


def setup(tmp: Path):
    P.PENDING_DIR = tmp / "pending"
    T.APPLIED_PATH = tmp / "applied.json"
    B.LOCK = asyncio.Lock()
    B.INBOX = asyncio.Queue()
    SENT.clear()
    TOASTS.clear()
    B.tg_send = lambda text, buttons=None: SENT.append((text, buttons)) or 1
    B.tg_answer_callback = lambda cb_id, text="": TOASTS.append(text)
    B.bank_draft = lambda item, company: (item.get("draft"), None)


def job(key, company="Ramp", title="Software Engineer"):
    return {"dedup_key": key, "company": company, "title": title,
            "location": "New York", "url": f"https://jobs.example/{key}",
            "compensation": "$100k-$150k", "resume": "Parth_Resume_tech_v4.pdf (tech track)"}


QS = [
    {"id": "ask_0", "label": "Why Ramp?", "required": True},
    {"id": "ask_1", "label": "Pronouns", "required": False,
     "options": ["He/Him", "She/Her", "They/Them"]},
]
FILLED = [{"label": "Email", "value": "jordan.lee@example.com"}]


def buttons_flat(bs):
    return [b["callback_data"] for row in (bs or []) for b in row]


async def test_edit_lists_answers():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), QS, FILLED)
    P.set_answer("aaa", "ask_0", "Because I love payments infra")
    P.set_answer("aaa", "ask_1", "They/Them")

    await B.handle_edit("/edit")
    text, _ = SENT[-1]
    assert "1. <b>Why Ramp?</b>: Because I love payments infra" in text, text
    n += 1
    assert "2. <b>Pronouns</b>: They/Them" in text, text
    n += 1
    return n


async def test_edit_one_shot():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), QS, FILLED)
    P.set_answer("aaa", "ask_0", "old answer")
    P.set_answer("aaa", "ask_1", "They/Them")

    await B.handle_edit("/edit 1 Because the team is elite")
    assert P.load("aaa")["questions"][0]["answer"] == "Because the team is elite"
    n += 1
    # complete + unapproved -> the summary is re-rendered so he can ✅
    assert any("ready to submit" in t for t, _ in SENT), [t for t, _ in SENT]
    n += 1
    return n


async def test_edit_reopen_re_asks():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), QS, FILLED)
    P.set_answer("aaa", "ask_0", "keep me")
    P.set_answer("aaa", "ask_1", "They/Them")

    await B.handle_edit("/edit 2")          # clear ask_1 and re-serve it
    assert P.load("aaa")["questions"][1]["answer"] is None, "re-open must clear the answer"
    n += 1
    text, _ = SENT[-1]
    assert "Pronouns" in text, text          # the question got served again
    n += 1
    assert P.load("aaa")["questions"][0]["answer"] == "keep me", "must not touch other answers"
    n += 1
    return n


async def test_edit_rejects_bad_number():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), QS, FILLED)
    await B.handle_edit("/edit 9")
    assert "1–2" in SENT[-1][0], SENT[-1][0]
    n += 1
    await B.handle_edit("/edit notanumber")
    assert "1–2" in SENT[-1][0], SENT[-1][0]
    n += 1
    return n


async def test_edit_no_questions_points_to_resume():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), [], FILLED)            # all-deterministic job (e.g. Chime)
    await B.handle_edit("/edit")
    assert "/resume" in SENT[-1][0] and "No free-text" in SENT[-1][0], SENT[-1][0]
    n += 1
    return n


async def test_edit_nothing_pending():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    await B.handle_edit("/edit")
    assert "Nothing in front of you" in SENT[-1][0], SENT[-1][0]
    n += 1
    return n


async def test_resume_shows_track_buttons():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), QS, FILLED)
    await B.handle_resume()
    text, bs = SENT[-1]
    cbs = buttons_flat(bs)
    tags = [t["tag"] for t in B.profile_mod.load_profile()["resume_tracks"]]
    for tag in tags:
        assert f"{B.CB}:r:aaa:{tag}" in cbs, (tag, cbs)
    n += 1
    assert "tech track" in text, text        # shows the current resume
    n += 1
    return n


async def test_resume_override_callback_sets_tag():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), [], FILLED)            # zero-question job so it's complete
    await B.batch_callback(f"{B.CB}:r:aaa:quant", "cb")
    rec = P.load("aaa")
    assert rec["resume_override"] == "quant", rec.get("resume_override")
    n += 1
    assert "quant track" in rec["resume"], rec["resume"]     # display string updated
    n += 1
    assert any("resume set to" in t for t, _ in SENT), [t for t, _ in SENT]
    n += 1
    return n


async def test_resume_override_unknown_track_noop():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), [], FILLED)
    await B.batch_callback(f"{B.CB}:r:aaa:bogus", "cb")
    assert P.load("aaa").get("resume_override") is None, "unknown track must not be stored"
    n += 1
    assert "Unknown track" in TOASTS[-1], TOASTS
    n += 1
    return n


async def test_resume_override_refused_after_approve():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    P.put(job("aaa"), [], FILLED)
    P.approve("aaa")                         # he already ✅'d it
    await B.batch_callback(f"{B.CB}:r:aaa:quant", "cb")
    assert P.load("aaa").get("resume_override") is None, "must not change an approved job"
    n += 1
    assert "Already approved" in TOASTS[-1], TOASTS
    n += 1
    return n


async def test_resume_override_stale_button_no_crash():
    setup(Path(tempfile.mkdtemp()))
    n = 0
    await B.batch_callback(f"{B.CB}:r:gone:quant", "cb")   # job already deleted
    assert "gone" in TOASTS[-1].lower(), TOASTS
    n += 1
    return n


async def main():
    n = 0
    for t in (test_edit_lists_answers, test_edit_one_shot, test_edit_reopen_re_asks,
              test_edit_rejects_bad_number, test_edit_no_questions_points_to_resume,
              test_edit_nothing_pending, test_resume_shows_track_buttons,
              test_resume_override_callback_sets_tag, test_resume_override_unknown_track_noop,
              test_resume_override_refused_after_approve, test_resume_override_stale_button_no_crash):
        n += await t()
    print(f"edit/resume: {n}/{n} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
