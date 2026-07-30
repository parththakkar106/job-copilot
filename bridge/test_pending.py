#!/usr/bin/env python3
"""Tests for the disk-backed pending store.

Everything runs against a temp dir — never bridge/pending/, which holds a live batch.
"""
import json
import tempfile
from pathlib import Path

import pending as P

JOB = {
    "dedup_key": "abc123def456aaaa",
    "company": "Ramp",
    "title": "Software Engineer",
    "location": "New York",
    "url": "https://jobs.ashbyhq.com/ramp/x",
    "compensation": "$150k-$200k",
}
QS = [
    {"id": "ask_0", "label": "Why Ramp?", "required": True, "answer_key": "why_company"},
    {"id": "ask_1", "label": "Portfolio", "required": False},
]
FILLED = [{"label": "Email", "value": "jordan.lee@example.com"}]


def tmp() -> Path:
    return Path(tempfile.mkdtemp())


def test_roundtrip():
    d, n = tmp(), 0

    rec = P.put(JOB, QS, FILLED, base=d)
    assert rec["status"] == P.AWAITING and rec["company"] == "Ramp", rec
    n += 1

    # questions arrive unresolved, not blank — the distinction drives everything else
    assert all(q["answer"] is None for q in rec["questions"]), rec["questions"]
    n += 1

    back = P.load(JOB["dedup_key"], base=d)
    assert back["url"] == JOB["url"] and len(back["questions"]) == 2, back
    n += 1

    assert P.load("nope", base=d) is None
    n += 1

    # written as real utf-8 JSON (Windows defaults to cp1252 and would mangle this)
    raw = (d / f"{JOB['dedup_key']}.json").read_text(encoding="utf-8")
    assert json.loads(raw)["company"] == "Ramp"
    n += 1
    return n


def test_no_clobber():
    """A phase 1 re-run must not wipe answers Parth already gave."""
    d, n = tmp(), 0
    P.put(JOB, QS, FILLED, base=d)
    P.set_answer(JOB["dedup_key"], "ask_0", "Because I like Ramp.", base=d)

    assert P.put(JOB, QS, FILLED, base=d) is None, "re-put should refuse, not overwrite"
    n += 1
    rec = P.load(JOB["dedup_key"], base=d)
    assert rec["questions"][0]["answer"] == "Because I like Ramp.", rec
    n += 1
    return n


def test_answers_and_completion():
    d, n = tmp(), 0
    P.put(JOB, QS, FILLED, base=d)
    rec = P.load(JOB["dedup_key"], base=d)

    assert len(P.unanswered(rec)) == 2 and not P.is_complete(rec)
    n += 1
    assert P.next_question(rec)["id"] == "ask_0", "questions served in order"
    n += 1

    rec = P.set_answer(JOB["dedup_key"], "ask_0", "Because I like Ramp.", base=d)
    assert P.next_question(rec)["id"] == "ask_1"
    n += 1

    # an optional left deliberately blank IS resolved — "" is an answer, None is not
    rec = P.set_answer(JOB["dedup_key"], "ask_1", "", base=d)
    assert P.is_complete(rec) and P.next_question(rec) is None, rec
    n += 1

    # a stale tap on a question/job that no longer exists is a no-op, not a crash
    assert P.set_answer(JOB["dedup_key"], "ask_99", "x", base=d) is None
    n += 1
    assert P.set_answer("gone", "ask_0", "x", base=d) is None
    n += 1
    return n


def test_approve_gate():
    d, n = tmp(), 0
    P.put(JOB, QS, FILLED, base=d)

    # the safety property: cannot approve while a question is outstanding
    assert P.approve(JOB["dedup_key"], base=d) is None, "half-answered must not approve"
    n += 1
    assert P.load(JOB["dedup_key"], base=d)["status"] == P.AWAITING
    n += 1

    P.set_answer(JOB["dedup_key"], "ask_0", "a", base=d)
    P.set_answer(JOB["dedup_key"], "ask_1", "", base=d)
    rec = P.approve(JOB["dedup_key"], base=d)
    assert rec["status"] == P.APPROVED, rec
    n += 1

    # a zero-question job is complete on arrival but still needs an explicit ✅
    z = {**JOB, "dedup_key": "zero000000000000"}
    rec = P.put(z, [], FILLED, base=d)
    assert P.is_complete(rec) and rec["status"] == P.AWAITING, rec
    n += 1
    assert P.approve(z["dedup_key"], base=d)["status"] == P.APPROVED
    n += 1
    return n


def test_listing_and_counts():
    d, n = tmp(), 0
    P.put(JOB, QS, FILLED, base=d)
    P.put({**JOB, "dedup_key": "bbb", "company": "Notion"}, QS[:1], FILLED, base=d)
    P.put({**JOB, "dedup_key": "ccc", "company": "Vercel"}, [], FILLED, base=d)
    P.approve("ccc", base=d)

    assert len(P.all_jobs(base=d)) == 3
    n += 1
    assert [r["dedup_key"] for r in P.all_jobs(P.APPROVED, base=d)] == ["ccc"]
    n += 1

    c = P.counts(base=d)
    assert c == {"jobs": 3, "awaiting": 2, "approved": 1, "held": 0, "skipped": 0, "ready": 0, "questions_left": 3}, c
    n += 1

    # next_job skips the approved one — it's done needing him
    assert P.next_job(base=d)["dedup_key"] == JOB["dedup_key"]
    n += 1

    assert P.delete("bbb", base=d) and not P.delete("bbb", base=d)
    n += 1
    assert len(P.all_jobs(base=d)) == 2
    n += 1

    # archive: retires a submitted record from the batch but KEEPS its answers on disk.
    assert P.archive(JOB["dedup_key"], base=d), "archive moves an existing record"
    n += 1
    assert len(P.all_jobs(base=d)) == 1, "archived record leaves the active batch"
    n += 1
    assert P.load(JOB["dedup_key"], base=d) is None, "and is no longer a live pending record"
    n += 1
    kept = P.load_archived(JOB["dedup_key"], base=d)
    assert kept and kept["dedup_key"] == JOB["dedup_key"], "but its full record survives"
    n += 1
    assert not P.archive("nope", base=d), "archiving a missing key is a no-op"
    n += 1
    return n


def test_restore_from_archive():
    """restore() is the inverse of archive(): the web console blocks and un-blocks roles on
    a single keypress, so a retired record has to come back with its answers intact."""
    d, n = tmp(), 0
    P.put(JOB, QS, FILLED, base=d)
    key = JOB["dedup_key"]
    P.set_answer(key, QS[0]["id"], "kept text", base=d)
    P.approve(key, base=d) if P.is_complete(P.load(key, base=d)) else None
    P.archive(key, base=d)

    assert P.load(key, base=d) is None, "archived record is out of the active batch"
    n += 1
    back = P.restore(key, base=d)
    assert back and back["status"] == P.AWAITING, back
    n += 1
    assert P.load(key, base=d) is not None, "and is active again"
    n += 1
    assert back["questions"][0]["answer"] == "kept text", "answers survive the round trip"
    n += 1
    assert P.load_archived(key, base=d) is None, "the archive copy is moved, not copied"
    n += 1
    assert P.restore("nope", base=d) is None, "restoring a missing key is a no-op"
    n += 1
    return n


def test_corrupt_file_is_survivable():
    """A corrupt record must not take the whole batch down with it."""
    d, n = tmp(), 0
    P.put(JOB, QS, FILLED, base=d)
    (d / "garbage.json").write_text("{{{ not json", encoding="utf-8")

    assert len(P.all_jobs(base=d)) == 1, "the good record still lists"
    n += 1
    assert P.load("garbage", base=d) is None
    n += 1
    return n


if __name__ == "__main__":
    n = (test_roundtrip() + test_no_clobber() + test_answers_and_completion()
         + test_approve_gate() + test_listing_and_counts() + test_restore_from_archive()
         + test_corrupt_file_is_survivable())
    print(f"pending: {n}/{n} checks passed")
