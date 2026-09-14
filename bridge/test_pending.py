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

    # next_job skips the approved one. Both others are untouched and share a date, so the
    # one with fewer questions left comes first.
    assert P.next_job(base=d)["dedup_key"] == "bbb"
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


def test_open_warnings():
    """The warning box only earns attention if its entries are true.

    `warnings` is a one-time DOM snapshot from the phase-1 fill and nothing ever revisits
    it, so it goes stale in both directions. Measured across the live batch 2026-08-24:
    1785 warnings on 165 records, of which 70% were either a field the same record listed
    as filled, or a duplicate of another warning differing only by its required marker.
    """
    n = 0
    rec = {
        "filled": [{"label": "Country", "value": "United States"},
                   {"label": "School", "value": "Other"},
                   {"label": "Cover letter", "value": ""}],
        "questions": [{"label": "End date year", "answer": None},
                      {"label": "Why us?", "answer": "Because."},
                      {"label": "Portfolio", "answer": ""}],
        "warnings": ["Country", "School", "Cover letter", "End date year", "Why us?",
                     "Portfolio", "Military status?*", "Military status?", "  ", ""],
    }
    got = P.open_warnings(rec)

    # the false ones: the record itself shows a value for these
    assert "Country" not in got and "School" not in got, got
    n += 1
    assert "Why us?" not in got, "an answered question is not an empty required field"
    n += 1

    # the true ones survive, including both blank-is-an-answer cases: a field whose value
    # is "" really IS empty on the form, which is exactly what the warning is for
    assert "Cover letter" in got, "filled-with-blank leaves the form field empty"
    n += 1
    assert "Portfolio" in got, "answered-blank leaves the form field empty"
    n += 1
    assert "End date year" in got, "an unanswered question stays a warning"
    n += 1

    # the asterisk twins collapse to exactly one entry, and the marker is dropped
    assert [w for w in got if "Military" in w] == ["Military status?"], got
    n += 1
    assert not [w for w in got if not w.strip()], "blank entries never render"
    n += 1

    # nothing is written back: the raw snapshot is the audit trail of what phase 1 saw
    assert len(rec["warnings"]) == 10, "open_warnings must not mutate the record"
    n += 1

    # a record with no warnings at all, and one with no filled/questions keys
    assert P.open_warnings({}) == []
    n += 1
    assert P.open_warnings({"warnings": ["Country"]}) == ["Country"],         "with nothing to disprove it, a warning stands"
    n += 1
    return n


def test_readable_label():
    """A question Parth cannot read is a question he cannot answer.

    Greenhouse's education block is scanned before its <label for> elements are wired, so
    labelFor() falls through to `name || id` and those fields carry ids as labels. Found on
    disk 2026-08-24: 29 questions across 16 records, every one of them from that block.

    CASES MIRRORED IN extension/test_fill_units.mjs — the same rewrites live in fill.js as
    readableLabel(), for new fills. If one side changes, change both.
    """
    n = 0
    assert P.readable_label("end-month--0") == "End date month"
    n += 1
    assert P.readable_label("end-year--0") == "End date year"
    n += 1
    assert P.readable_label("start-month--0") == "Start date month"
    n += 1
    assert P.readable_label("END-MONTH--0") == "End date month", "case-insensitive"
    n += 1
    # Row 0 reads exactly as Greenhouse labels it once hydrated, so an escalated question
    # matches its filled sibling; a later row has to say which entry it is.
    assert P.readable_label("end-month--1") == "End date month (education 2)"
    n += 1
    # the other ids actually seen on disk
    for raw, want in (("school--0", "School"), ("degree--0", "Degree"),
                      ("discipline--0", "Discipline")):
        assert P.readable_label(raw) == want, raw
        n += 1
    assert P.readable_label("work_authorization--2") == "Work authorization (row 3)"
    n += 1

    # THE safety property: a real question is never rewritten. Every rule is gated on the
    # label being one token with a separator, and human-written labels have spaces.
    for real in ("Why Ramp?", "What is your gender identity?", "End date month",
                 "Are you willing to work from the office(s) listed?",
                 "E-mail address", "Full-time or part-time?", "Ramp"):
        assert P.readable_label(real) == real, real
        n += 1
    assert P.readable_label("") == "" and P.readable_label(None) == ""
    n += 1

    # It reaches the warning box too, which is how a warning naming a field by its id gets
    # matched against the same field named properly in `filled`.
    rec = {"filled": [{"label": "End date month", "value": "June"}],
           "questions": [], "warnings": ["end-month--0"]}
    assert P.open_warnings(rec) == [],         "an id-named warning is recognised as the filled field it refers to"
    n += 1
    return n


def test_draft_from_form():
    """A question the form has already answered is a confirmation, not a typing task.

    Greenhouse's resume parser fills the education block before the extension scans it and
    none of those fields match a taxonomy rule, so they escalated as questions anyway.
    Found on disk 2026-08-24: 115 open questions across 31 records that the SAME record
    lists under `filled` with a value.
    """
    n = 0
    rec = {
        "filled": [{"label": "End date month", "value": "June"},
                   {"label": "school--0", "value": "Other"},
                   {"label": "Country", "value": ""}],
        "questions": [
            {"id": "a", "label": "end-month--0", "answer": None},
            {"id": "b", "label": "School", "answer": None},
            {"id": "c", "label": "Country", "answer": None},
            {"id": "d", "label": "Why us?", "answer": None},
            {"id": "e", "label": "End date month", "answer": "May"},
            {"id": "f", "label": "End date month", "answer": None, "draft": "Mine"},
        ],
    }
    qs = {q["id"]: q for q in P.draft_from_form(rec)}

    # matched across the id/label divide, in both directions
    assert qs["a"]["draft"] == "June" and qs["a"]["_on_form"] is True, qs["a"]
    n += 1
    assert qs["b"]["draft"] == "Other", "a question labelled properly finds a field stored by id"
    n += 1

    # a field recorded with a BLANK value proves nothing — there is nothing to confirm
    assert "draft" not in qs["c"] and not qs["c"].get("_on_form"), qs["c"]
    n += 1
    assert "draft" not in qs["d"], "a question with no matching field is untouched"
    n += 1

    # never overwrite what is already there
    assert qs["e"]["answer"] == "May" and "draft" not in qs["e"], "an answered question is left alone"
    n += 1
    assert qs["f"]["draft"] == "Mine", "an existing draft (the bank's) outranks the form's value"
    n += 1

    # THE gate property: offering a draft must not decide the record is complete. Approving
    # is what sends an application to a real employer, and a value nobody has looked at is
    # not a value Parth accepted.
    assert not P.is_complete({**rec, "questions": P.draft_from_form(rec)}),         "a drafted question is still unanswered"
    n += 1

    # it relabels on the same pass, so the drawer never shows a raw id
    assert qs["a"]["label"] == "End date month", qs["a"]["label"]
    n += 1
    # and it does not mutate the record it was handed
    assert rec["questions"][0]["label"] == "end-month--0", "draft_from_form must not mutate"
    n += 1
    return n


def test_serving_order():
    """/next serves half-answered, then ready-to-approve, then untouched jobs. Each tier
    is ordered newest posting date first, and fewest questions first on the same date."""
    d, n = tmp(), 0

    def put(key, created, qs, answers=(), posted=""):
        rec = P.put({**JOB, "dedup_key": key, "posted": posted}, qs, FILLED, base=d)
        rec["created"] = created
        for qid, val in answers:
            for q in rec["questions"]:
                if q["id"] == qid:
                    q["answer"] = val
        P.save(rec, base=d)
        return rec

    two = [{"id": "ask_0", "label": "A"}, {"id": "ask_1", "label": "B"}]
    # two collect runs; within a run, written freshest-posting first
    put("aaaaaaaaaaaaaaa1", "2026-07-01T09:00:00+00:00", list(two))          # old, untouched
    put("aaaaaaaaaaaaaaa2", "2026-07-01T09:00:01+00:00", list(two))          # old, untouched
    put("bbbbbbbbbbbbbbb1", "2026-08-01T09:00:00+00:00", list(two))          # new, freshest
    put("bbbbbbbbbbbbbbb2", "2026-08-01T09:00:01+00:00", list(two))          # new, next
    put("ccccccccccccccc1", "2026-07-02T09:00:00+00:00", list(two),
        answers=[("ask_0", "done")])                                          # started
    put("ddddddddddddddd1", "2026-07-03T09:00:00+00:00", [])                  # complete, no qs

    order = [r["dedup_key"] for r in P.serving_order(base=d)]

    assert order[0] == "ccccccccccccccc1", f"a half-answered record is served first: {order}"
    n += 1
    assert order[1] == "ddddddddddddddd1", f"then the one that only needs a ✅: {order}"
    n += 1
    # the untouched tier: newest date first, falling back to the collect date
    assert order[2:4] == ["bbbbbbbbbbbbbbb1", "bbbbbbbbbbbbbbb2"], order
    n += 1
    assert order[4:] == ["aaaaaaaaaaaaaaa1", "aaaaaaaaaaaaaaa2"],         f"the month-old run goes last, not first: {order}"
    n += 1
    assert P.next_job(base=d)["dedup_key"] == "ccccccccccccccc1", "next_job agrees"
    n += 1

    # the posting date outranks the collect date, and a tie goes to fewer questions
    d2 = tmp()
    three = [{"id": f"ask_{i}", "label": str(i)} for i in range(3)]
    for key, created, qs, posted in [
        ("eeeeeeeeeeeeeee1", "2026-09-10T09:00:00+00:00", three, "2026-09-01T00:00:00Z"),
        ("eeeeeeeeeeeeeee2", "2026-09-01T09:00:00+00:00", three, "2026-09-05T00:00:00Z"),
        ("eeeeeeeeeeeeeee3", "2026-09-01T09:00:00+00:00", list(two), "2026-09-05T00:00:00Z"),
    ]:
        rec = P.put({**JOB, "dedup_key": key, "posted": posted}, qs, FILLED, base=d2)
        rec["created"] = created
        P.save(rec, base=d2)
    order = [r["dedup_key"] for r in P.serving_order(base=d2)]
    assert order == ["eeeeeeeeeeeeeee3", "eeeeeeeeeeeeeee2", "eeeeeeeeeeeeeee1"], order
    n += 1

    # tier classification, including the two ways a record reads as READY
    assert P.next_tier({"questions": [{"answer": "x"}, {"answer": None}]}) == P.TIER_IN_PROGRESS
    n += 1
    assert P.next_tier({"questions": [{"answer": "x"}]}) == P.TIER_READY
    n += 1
    assert P.next_tier({"questions": []}) == P.TIER_READY, "no questions at all is ready, not fresh"
    n += 1
    assert P.next_tier({"questions": [{"answer": None}]}) == P.TIER_FRESH
    n += 1
    # a deliberately-blank answer is an answer, so it counts as progress
    assert P.next_tier({"questions": [{"answer": ""}, {"answer": None}]}) == P.TIER_IN_PROGRESS
    n += 1

    # only AWAITING is served: approving must remove a record from the order entirely
    P.approve("ddddddddddddddd1", base=d)
    assert "ddddddddddddddd1" not in [r["dedup_key"] for r in P.serving_order(base=d)]
    n += 1
    assert P.serving_order(base=tmp()) == [] and P.next_job(base=tmp()) is None,         "an empty batch serves nothing rather than raising"
    n += 1
    return n


if __name__ == "__main__":
    n = (test_roundtrip() + test_no_clobber() + test_answers_and_completion()
         + test_approve_gate() + test_listing_and_counts() + test_restore_from_archive()
         + test_corrupt_file_is_survivable() + test_open_warnings()
         + test_readable_label()
         + test_draft_from_form()
         + test_serving_order())
    print(f"pending: {n}/{n} checks passed")
