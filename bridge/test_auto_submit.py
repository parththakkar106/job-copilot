#!/usr/bin/env python3
"""Offline checks for unattended approval: pending.auto_block and bridge._auto_approve.
Every record lives in a temp dir and Telegram is stubbed, so nothing real is touched."""
import asyncio
import json
import tempfile

import bridge as B
import pending


def _ai(conf, action="fill", **kw):
    return {"id": "f1", "label": "Why us?", "action": action, "confidence": conf, **kw}


def _rec(questions=(), decisions=(), warnings=()):
    return {"dedup_key": "k1", "company": "Acme", "title": "SWE", "status": pending.AWAITING,
            "questions": list(questions), "ai_decisions": list(decisions),
            "warnings": list(warnings), "filled": []}


def run():
    checks = 0
    ok = _rec(decisions=[_ai(0.95), _ai(0.9, action="replace")])
    assert pending.auto_block(ok) is None; checks += 1
    cases = {
        "open question": _rec(questions=[{"id": "q", "label": "Salary", "answer": None}],
                              decisions=[_ai(0.95)]),
        "required fields": _rec(decisions=[_ai(0.95)], warnings=["School*"]),
        "no AI pass": _rec(),
        "AI 89%": _rec(decisions=[_ai(0.95), _ai(0.89)]),
        "AI flag": _rec(decisions=[_ai(0.95, action="flag")]),
        "AI propose": _rec(decisions=[_ai(0.95, action="propose")]),
        "hard fact": _rec(decisions=[_ai(0.99, action="replace", hot=True)]),
        "not written": _rec(decisions=[_ai(0.99, applied=False)]),
    }
    for want, rec in cases.items():
        got = pending.auto_block(rec)
        assert got and (want.lower().split()[0] in got.lower()), (want, got); checks += 1
    # An answered-blank optional counts as answered.
    assert pending.auto_block(_rec(questions=[{"id": "q", "label": "x", "answer": ""}],
                                   decisions=[_ai(0.95)])) is None; checks += 1

    sent = []
    B.tg_send = lambda text, *a, **k: sent.append(text)
    with tempfile.TemporaryDirectory() as d:
        pending.PENDING_DIR = d
        prefs_path = B.prefs_mod.PREFS_PATH
        B.prefs_mod.PREFS_PATH = __import__("pathlib").Path(d) / "prefs.json"
        try:
            pending.save(dict(ok))
            asyncio.run(B._auto_approve(pending.load("k1")))
            rec = pending.load("k1")
            assert rec["status"] == pending.APPROVED and rec["auto_approved"]; checks += 1
            assert len(sent) == 1 and "Auto-approved" in sent[0]; checks += 1

            bad = {**cases["AI 89%"], "dedup_key": "k2"}
            pending.save(bad)
            asyncio.run(B._auto_approve(pending.load("k2")))
            rec = pending.load("k2")
            assert rec["status"] == pending.AWAITING and "89%" in rec["auto_block"]; checks += 1

            B.prefs_mod.set("auto_submit", False)
            pending.save({**ok, "dedup_key": "k3"})
            asyncio.run(B._auto_approve(pending.load("k3")))
            assert pending.load("k3")["status"] == pending.AWAITING; checks += 1
            assert len(sent) == 1; checks += 1
        finally:
            B.prefs_mod.PREFS_PATH = prefs_path
    print(f"auto_submit: {checks}/{checks} checks passed")


if __name__ == "__main__":
    run()
