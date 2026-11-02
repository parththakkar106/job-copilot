#!/usr/bin/env python3
"""Checks for the /refer flow, on temp files and with Telegram captured.

    python bridge/test_refer.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as B  # noqa: E402

SENT: list = []
PLAIN: list = []
checks = 0


def ok(cond, msg=""):
    global checks
    assert cond, msg
    checks += 1


def callbacks(buttons):
    return [b["callback_data"] for row in buttons or [] for b in row]


async def run():
    await B.handle_refer("/refer")
    ok("/refer Acme" in SENT[-1][0], SENT[-1])
    await B.handle_refer("/refer Nobody")
    ok("no applied roles" in SENT[-1][0], SENT[-1])

    # Newest first, and a role passed on is hidden.
    await B.handle_refer("/refer acme")
    text, buttons = SENT[-1]
    ok(callbacks(buttons) == ["b:x:k2", "b:x:k1"], buttons)
    ok(buttons[0][0]["text"].startswith("2026-03-01 · AI Engineer"), buttons)

    # A role shows only the levels that have a template.
    await B.referral_callback("k2", [], "cb")
    ok(callbacks(SENT[-1][1]) == ["b:x:k2:hm", "b:x:k2:bitsian"], SENT[-1])

    # A role type with no templates says so.
    await B.referral_callback("k1", [], "cb")
    ok("no Tech templates" in SENT[-1][0], SENT[-1])

    # A level with no template, and a stale role, change nothing.
    await B.referral_callback("k2", ["recruiter"], "cb")
    await B.referral_callback("gone", ["hm"], "cb")
    ok(B.REFER_WAITING is None)

    # A level button asks for the name, and the reply is the rendered message in plain text.
    await B.referral_callback("k2", ["hm"], "cb")
    ok(B.REFER_WAITING == ("k2", "hm") and "name" in SENT[-1][0], SENT[-1])
    await B.send_referral_message(B.REFER_WAITING, "Ivo")
    ok(PLAIN[-1] == "Hey Ivo, AI Engineer at Acme & Co", PLAIN)

    # The name must never reach the answer path, or it goes to a real employer. The poller
    # needs a network, so this reads the source.
    src = Path(B.__file__).read_text(encoding="utf-8")
    ok(src.index("if REFER_WAITING:") < src.index("if await answer_current(text)"))

    # Starting over forgets a name that was never sent.
    await B.handle_refer("/refer acme")
    ok(B.REFER_WAITING is None)


def main():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        B.triage_mod.APPLIED_PATH = tmp / "applied.json"
        B.triage_mod.APPLIED_PATH.write_text(json.dumps([
            {"dedup_key": "k1", "company": "Acme & Co", "title": "Backend Engineer",
             "applied": "2026-01-01"},
            {"dedup_key": "k2", "company": "Acme & Co", "title": "AI Engineer",
             "applied": "2026-03-01"},
            {"dedup_key": "k3", "company": "Acme & Co", "title": "Quant", "status": "rejected",
             "applied": "2026-04-01"}]), encoding="utf-8")
        B.refer_mod.paths.REFERRAL_TEMPLATES = tmp / "templates.yaml"
        B.refer_mod.paths.REFERRAL_TEMPLATES.write_text(
            "ai:\n  hm: 'Hey {name}, {role} at {company}'\n  bitsian: 'Hi {name}'\n"
            "  recruiter: ''\n", encoding="utf-8")
        B.LAST_ACTIVITY = 1.0
        B.tg_send = lambda text, buttons=None: SENT.append((text, buttons)) or 1
        B.tg_send_plain = lambda text: PLAIN.append(text) or 1
        B.tg_answer_callback = lambda *a, **k: None
        asyncio.run(run())
    print(f"refer: {checks}/{checks} checks passed")


if __name__ == "__main__":
    main()
