"""Unit tests for drafter.py. Stubs the CLI boundary (_run_claude) so nothing is spent
and no `claude` binary is required — we test prompt building, sanitization, gating, and
the draft_answer wiring, which is where the bugs would be."""
import tempfile
import types
from pathlib import Path

import drafter
import prefs


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    return bool(cond)


def main():
    ok = True

    # --- is_draftable: only free-text essays with an answer_key ---
    ok &= check("draftable: essay with answer_key",
                drafter.is_draftable({"answer_key": "why_company", "label": "Why us?"}))
    ok &= check("not draftable: no answer_key",
                not drafter.is_draftable({"label": "Phone"}))
    ok &= check("not draftable: has options (multiple choice)",
                not drafter.is_draftable({"answer_key": "x", "options": ["Yes", "No"]}))

    # --- build_prompt: carries the style rules + company/role + question ---
    p = drafter.build_prompt(
        {"answer_key": "why_company", "label": "Why do you want to work here?"},
        {"company": "Affirm", "role": "Data Scientist"})
    ok &= check("prompt names the company", "Affirm" in p)
    ok &= check("prompt names the role", "Data Scientist" in p)
    ok &= check("prompt carries the question", "Why do you want to work here?" in p)
    ok &= check("prompt forbids dashes", "NEVER use a dash" in p)
    ok &= check("prompt asks for one typo", "ONE small" in p)
    ok &= check("prompt caps length", "3 to 4 lines" in p)
    # Whoever the loaded profile describes, the prompt must address them by name. Asserting
    # a literal name here would only pass for one person's profile.json — and did, until
    # the example template (every field FILL_ME) made this fail on a fresh clone.
    ok &= check("prompt grounds in bio", drafter.NAME in p)

    # --- sanitize: strips wrapping quotes, neutralizes dashes, caps length ---
    ok &= check("sanitize strips wrapping quotes",
                drafter.sanitize('"hello there"') == "hello there")
    ok &= check("sanitize kills em dash",
                "—" not in drafter.sanitize("I build end—to—end systems"))
    ok &= check("sanitize kills spaced hyphen",
                " - " not in drafter.sanitize("I do data science - mostly"))
    ok &= check("sanitize keeps hyphenated words",
                "data-science" in drafter.sanitize("I love data-science work"))
    ok &= check("sanitize caps very long output",
                len(drafter.sanitize("x " * 1000)) <= 700)
    ok &= check("sanitize empty -> empty", drafter.sanitize("   ") == "")

    # --- draft_answer: stub the CLI so no real call happens ---
    orig_run, orig_enabled = drafter._run_claude, drafter._ENABLED
    orig_bin = drafter.CLAUDE_BIN
    try:
        drafter._ENABLED = True
        drafter.CLAUDE_BIN = "/fake/claude"  # make available() true under the stub
        drafter._run_claude = lambda prompt: '"I really admire the mission—it fits me."'
        got = drafter.draft_answer(
            {"answer_key": "why_company", "label": "Why us?"}, {"company": "Neon"})
        ok &= check("draft_answer returns sanitized text",
                    got == "I really admire the mission, it fits me.")

        drafter._run_claude = lambda prompt: None  # CLI failure
        ok &= check("draft_answer: CLI failure -> None",
                    drafter.draft_answer({"answer_key": "x", "label": "Q"}, None) is None)

        drafter._run_claude = lambda prompt: "should not be called"
        ok &= check("draft_answer: non-draftable item -> None (no CLI call)",
                    drafter.draft_answer({"label": "Phone"}, None) is None)

        # --- force: the panel's "Draft it" on a question taxonomy never tagged ---
        drafter._run_claude = lambda prompt: "drafted anyway"
        ok &= check("force: untagged essay CAN be drafted on request",
                    drafter.draft_answer({"label": "What does our purpose mean to you?"},
                                         None, force=True) == "drafted anyway")
        ok &= check("force keeps the essay shape: multiple choice still refused",
                    drafter.draft_answer({"label": "Veteran status?", "options": ["Yes", "No"]},
                                         None, force=True) is None)
        ok &= check("force still needs a label to write about",
                    drafter.draft_answer({"label": "  "}, None, force=True) is None)
        ok &= check("without force an untagged question is still refused",
                    drafter.draft_answer({"label": "What does our purpose mean to you?"},
                                         None) is None)
        ok &= check("drafts_on_request agrees with the force gate",
                    drafter.drafts_on_request({"label": "Q"})
                    and not drafter.drafts_on_request({"label": "Q", "options": ["a"]}))

        drafter._ENABLED = False
        ok &= check("draft_answer: kill switch -> None",
                    drafter.draft_answer({"answer_key": "x", "label": "Q"}, None) is None)
        ok &= check("kill switch beats force too",
                    drafter.draft_answer({"label": "Q"}, None, force=True) is None)
    finally:
        drafter._run_claude, drafter._ENABLED = orig_run, orig_enabled
        drafter.CLAUDE_BIN = orig_bin

    # --- the on/off flag persists through prefs (temp file, real applied.json untouched) ---
    orig_pref_path, orig_override = prefs.PREFS_PATH, drafter._ENABLED
    with tempfile.TemporaryDirectory() as tmp:
        prefs.PREFS_PATH = Path(tmp) / "prefs.json"
        try:
            drafter._ENABLED = None  # defer to prefs (production path)
            drafter.set_enabled(True)
            ok &= check("set_enabled(True) -> is_enabled True", drafter.is_enabled() is True)
            ok &= check("flag persisted to prefs file", prefs.get("drafts_enabled") is True)
            drafter.set_enabled(False)
            ok &= check("set_enabled(False) -> is_enabled False", drafter.is_enabled() is False)
            ok &= check("in-memory override still wins over prefs",
                        (setattr(drafter, "_ENABLED", True) or drafter.is_enabled()) is True)
        finally:
            prefs.PREFS_PATH, drafter._ENABLED = orig_pref_path, orig_override

    # --- model pinning: _run_claude passes --model, unless DRAFT_MODEL is default ---
    orig_model, orig_bin2, orig_subrun = drafter.DRAFT_MODEL, drafter.CLAUDE_BIN, drafter.subprocess.run
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="a draft", stderr="")
    try:
        drafter.CLAUDE_BIN = "/fake/claude"
        drafter.subprocess.run = fake_run
        drafter.DRAFT_MODEL = "haiku"
        drafter._run_claude("hi")
        ok &= check("_run_claude pins --model haiku",
                    "--model" in captured["cmd"] and "haiku" in captured["cmd"])
        drafter.DRAFT_MODEL = "default"
        drafter._run_claude("hi")
        ok &= check("DRAFT_MODEL=default omits --model", "--model" not in captured["cmd"])
    finally:
        drafter.DRAFT_MODEL, drafter.CLAUDE_BIN = orig_model, orig_bin2
        drafter.subprocess.run = orig_subrun

    # --- batch drafting: one call for a whole application's essays ---
    items3 = [{"id": "a", "answer_key": "why_company", "label": "Why us?"},
              {"id": "b", "answer_key": "behavioral", "label": "Tell us about a project."},
              {"id": "c", "label": "Phone"}]  # not draftable (no answer_key)

    bp = drafter.build_batch_prompt(items3[:2], {"company": "Neon", "role": "DS"})
    ok &= check("batch prompt lists both questions",
                "Why us?" in bp and "Tell us about a project." in bp)
    ok &= check("batch prompt names the company", "Neon" in bp)
    ok &= check("batch prompt asks for a JSON object", "JSON object" in bp)

    parsed = drafter._parse_batch(
        'sure! {"1": "I admire the mission", "2": "I built end—to—end tools"} done',
        items3[:2])
    ok &= check("parse_batch maps numbers to item ids",
                parsed.get("a") == "I admire the mission")
    ok &= check("parse_batch sanitizes each answer", "—" not in parsed.get("b", "x"))
    ok &= check("parse_batch on junk -> empty", drafter._parse_batch("no json here", items3[:2]) == {})

    orig_run2, orig_enabled2, orig_bin3 = drafter._run_claude, drafter._ENABLED, drafter.CLAUDE_BIN
    try:
        drafter._ENABLED = True
        drafter.CLAUDE_BIN = "/fake/claude"
        calls = {"n": 0}

        def one_call(prompt):
            calls["n"] += 1
            return '{"1": "ans one", "2": "ans two"}'
        drafter._run_claude = one_call
        got = drafter.draft_answers(items3, {"company": "Neon"})
        ok &= check("draft_answers drafts every draftable essay",
                    got.get("a") == "ans one" and got.get("b") == "ans two")
        ok &= check("draft_answers skips the non-draftable item", "c" not in got)
        ok &= check("draft_answers uses ONE call for the batch", calls["n"] == 1)

        # A single draftable item takes the non-batch path (draft_answer), not the batch.
        drafter._run_claude = lambda prompt: "just one answer"
        solo = drafter.draft_answers([items3[0]], None)
        ok &= check("draft_answers: single item -> single-draft path",
                    solo.get("a") == "just one answer")

        # Unparseable batch reply falls back to per-item so drafts aren't lost.
        drafter._run_claude = lambda prompt: "no json at all"
        fb = drafter.draft_answers(items3[:2], None)
        ok &= check("draft_answers: unparseable batch -> per-item fallback",
                    fb.get("a") == "no json at all" and fb.get("b") == "no json at all")
    finally:
        drafter._run_claude, drafter._ENABLED, drafter.CLAUDE_BIN = orig_run2, orig_enabled2, orig_bin3

    print(f"\ndrafter units: {'all passed' if ok else 'FAILURES ABOVE'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
