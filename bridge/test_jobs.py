#!/usr/bin/env python3
"""Offline tests for the three Telegram batch additions:

  1. the role card that leads a job's questions
  2. ✋ skipping a job WHILE answering it (defers, keeps the answers)
  3. /jobs — the whole batch as a numbered, pickable list

Telegram is mocked (tg_send / tg_answer_callback record instead of calling), and every
record lives in a temp dir, so nothing here touches the live batch or a real bot.
"""
import asyncio
import tempfile
from pathlib import Path

import bridge as B
import pending as P

QS = [
    {"id": "ask_0", "label": "Why us?", "required": True},
    {"id": "ask_1", "label": "Portfolio", "required": False},
]


def _job(key, company, title="Software Engineer", created=None):
    job = {"dedup_key": key, "company": company, "title": title,
           "location": "New York", "url": f"https://boards.example/{key}"}
    return job


def _put(base, key, company, created, questions=QS, **extra):
    rec = P.put(_job(key, company), [dict(q) for q in questions], [], base=base)
    rec["created"] = created           # explicit, so ordering never depends on clock ties
    rec.update(extra)
    P.save(rec, base)
    return rec


def run():
    checks = 0
    sent, answered = [], []
    B.tg_send = lambda text, buttons=None: sent.append((text, buttons)) or 1
    B.tg_answer_callback = lambda cb_id, text="": answered.append(text)

    d = Path(tempfile.mkdtemp())
    P.PENDING_DIR = d                  # every pending_mod call in bridge lands here

    _put(d, "k_ramp", "Ramp", "2026-08-24T10:00:00+00:00")
    _put(d, "k_notion", "Notion", "2026-08-25T10:00:00+00:00")
    _put(d, "k_vercel", "Vercel", "2026-08-26T10:00:00+00:00", questions=[])
    P.approve("k_vercel", base=d)      # zero-question job, already ✅'d

    # --- 1) the card leads the questions, once per job ------------------------------
    # With no posting date, serving_order takes the newest collect day first, so Notion
    # (25 Aug) leads Ramp.
    B._LAST_CARD_KEY = None
    sent.clear()
    asyncio.run(B.serve_next())
    card, question = sent[0][0], sent[1][0]
    assert card.startswith("📋") and "Notion" in card, card; checks += 1
    assert "found 25 Aug" in card and "0/2 answered" in card, card; checks += 1
    assert "boards.example/k_notion" in card, card; checks += 1
    assert "Why us?" in question, question; checks += 1

    # the second question of the SAME job doesn't repeat the card
    sent.clear()
    asyncio.run(B.answer_current("because reasons"))
    assert len(sent) == 1 and "Portfolio" in sent[0][0], sent; checks += 1

    # ...but moving to a different job shows its own card
    sent.clear()
    asyncio.run(B.answer_current("github.com/parth"))
    assert any(t.startswith("✅") for t, _ in sent), sent    # Notion is complete -> summary
    checks += 1
    sent.clear()
    asyncio.run(B.serve_next())          # nothing changed -> still Notion, still no card
    assert not any(t.startswith("📋") for t, _ in sent), sent; checks += 1
    P.approve("k_notion", base=d)
    sent.clear()
    asyncio.run(B.serve_next())          # on to the next job -> its own card
    assert sent[0][0].startswith("📋") and "Ramp" in sent[0][0], sent[0][0]; checks += 1

    # --- 2) ✋ mid-question defers, keeping the answers --------------------------------
    P.reopen("k_notion", base=d)
    P.set_answer("k_notion", "ask_1", None, base=d)
    P.set_answer("k_notion", "ask_0", "half an answer", base=d)
    q = P.next_question(P.load("k_notion", base=d))
    sent.clear()
    B.send_question(P.load("k_notion", base=d), q)
    flat = [b["callback_data"] for row in sent[0][1] for b in row]
    assert f"{B.CB}:j:k_notion:defer" in flat, flat; checks += 1

    sent.clear(); answered.clear()
    asyncio.run(B.batch_callback(f"{B.CB}:j:k_notion:defer", "cb1"))
    rec = P.load("k_notion", base=d)
    assert rec["status"] == P.SKIPPED, rec["status"]; checks += 1
    kept = {q["id"]: q["answer"] for q in rec["questions"]}
    assert kept["ask_0"] == "half an answer", kept; checks += 1   # the whole point
    assert any("kept" in t for t, _ in sent), sent; checks += 1
    # and it is out of the way: /next moves on instead of re-serving it
    assert P.next_job(base=d)["dedup_key"] == "k_ramp"; checks += 1

    # --- 3) /jobs lists everything, with counts, dates and a tap per row -------------
    sent.clear()
    asyncio.run(B.handle_jobs("/jobs"))
    text, buttons = sent[0]
    assert "1–3 of 3" in text, text; checks += 1
    assert "Notion" in text and "Ramp" in text and "Vercel" in text, text; checks += 1
    assert "❓ 1/2" in text and "✋ skipped" in text, text          # Notion's row
    checks += 1
    assert "❓ no questions" in text, text                          # Vercel has none
    checks += 1
    assert "📅 24 Aug" in text, text; checks += 1
    cbs = [b["callback_data"] for row in buttons for b in row]
    assert f"{B.CB}:f:k_notion" in cbs, cbs; checks += 1
    # approved jobs list but can't be tapped — they're mid-flight to an employer
    assert f"{B.CB}:f:k_vercel" not in cbs, cbs; checks += 1
    answered.clear()
    asyncio.run(B.batch_callback(f"{B.CB}:f:k_vercel", "cb2"))
    assert "Already approved" in answered[0], answered; checks += 1
    assert P.load("k_vercel", base=d)["status"] == P.APPROVED; checks += 1

    # --- picking a row pins it: it comes back, and /next serves IT ------------------
    sent.clear(); answered.clear()
    asyncio.run(B.batch_callback(f"{B.CB}:f:k_notion", "cb3"))
    rec = P.load("k_notion", base=d)
    assert rec["status"] == P.AWAITING and rec.get("focus_at"), rec; checks += 1
    assert P.next_job(base=d)["dedup_key"] == "k_notion"; checks += 1
    assert sent[0][0].startswith("📋") and "Notion" in sent[0][0], sent[0][0]  # card again
    checks += 1

    # a pinned job outranks a fresher, already-started one
    _put(d, "k_stripe", "Stripe", "2026-09-01T10:00:00+00:00")
    P.set_answer("k_stripe", "ask_0", "started this one", base=d)
    assert P.next_job(base=d)["dedup_key"] == "k_notion"; checks += 1
    # ...and approving the pinned job releases the pin
    P.set_answer("k_notion", "ask_1", "", base=d)
    P.approve("k_notion", base=d)
    assert "focus_at" not in P.load("k_notion", base=d); checks += 1
    assert P.next_job(base=d)["dedup_key"] == "k_stripe"; checks += 1

    # --- paging: 9 rows split 8 + 1, numbered across the whole list -----------------
    rows = [{"dedup_key": f"k{i}", "company": f"Co{i}", "title": "SWE",
             "status": P.AWAITING, "created": "2026-08-24T00:00:00+00:00",
             "questions": [dict(q) for q in QS]} for i in range(9)]
    text, buttons = B.jobs_page(rows, 0)
    assert "1–8 of 9" in text, text; checks += 1
    assert [b["callback_data"] for b in buttons[-1]] == [f"{B.CB}:p:1"], buttons[-1]; checks += 1
    text, buttons = B.jobs_page(rows, 1)
    assert "9–9 of 9" in text and "<b>9.</b>" in text, text; checks += 1
    assert [b["callback_data"] for b in buttons[-1]] == [f"{B.CB}:p:0"], buttons[-1]; checks += 1
    # an out-of-range page clamps rather than showing an empty list
    assert "9–9 of 9" in B.jobs_page(rows, 47)[0]; checks += 1

    print(f"jobs: {checks}/{checks} checks passed")


if __name__ == "__main__":
    run()
