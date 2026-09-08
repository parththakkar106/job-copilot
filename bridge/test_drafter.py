"""Unit tests for drafter.py. Stubs the CLI boundary (_run_claude) so nothing is spent
and no `claude` binary is required — we test prompt building, sanitization, gating, and
the draft_answer wiring, which is where the bugs would be."""
import tempfile
import types
from pathlib import Path

import drafter
import prefs

from server.store import get_store, set_store, use_temp_store


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

    # --- the on/off flag persists through prefs (temp store, real data untouched) ---
    orig_store, orig_override = get_store(), drafter._ENABLED
    with tempfile.TemporaryDirectory() as tmp:
        use_temp_store(tmp)
        try:
            drafter._ENABLED = None  # defer to prefs (production path)
            drafter.set_enabled(True)
            ok &= check("set_enabled(True) -> is_enabled True", drafter.is_enabled() is True)
            ok &= check("flag persisted to prefs", prefs.get("drafts_enabled") is True)
            drafter.set_enabled(False)
            ok &= check("set_enabled(False) -> is_enabled False", drafter.is_enabled() is False)
            ok &= check("in-memory override still wins over prefs",
                        (setattr(drafter, "_ENABLED", True) or drafter.is_enabled()) is True)
        finally:
            set_store(orig_store)
            drafter._ENABLED = orig_override

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

    # --- conditional follow-ups (added) ------------------------------------
    # THE FAILURE: on a live Robinhood form the deterministic pass answered "require visa
    # sponsorship? -> No", and the follow-up under it reached the drafter carrying nothing
    # but "If you answered "Yes" to the above question, please provide additional
    # information here:". With no target to ground on, the model picked what the answer
    # above must have been and wrote a fluent paragraph about being on OPT and needing H1B
    # support. A second invented a non-compete. Both contradicted the same application.
    followups = [
        'If you answered "Yes" to the above question, please provide additional information here:',
        "If yes, please explain",
        "If so, tell us more",
        "If you selected yes, describe the arrangement",
        "Please provide additional information to the above question",
    ]
    for label in followups:
        ok &= check(f"refuses to auto-draft a pointer-only label: {label[:44]}",
                    not drafter.is_draftable({"label": label, "answer_key": "anything_else"}))
    # An explicit "Draft it" tap is still allowed: Parth asked for it, the question above is
    # on screen in front of him, and the draft lands in an editable box he reads before use.
    # AUTOMATIC drafting is the dangerous path, because nothing is read before it is stored.
    ok &= check("an explicit Draft-it tap on a follow-up is still permitted",
                drafter.drafts_on_request({"label": followups[0]}))
    # Precision: a real essay that merely opens with "If" must be unaffected.
    for label in ["If you could have any superpower, what would it be?",
                  "What if anything would you change about our product?",
                  "If you were starting this company today, what would you do differently?",
                  "Why do you want to work here?"]:
        ok &= check(f"still drafts a real essay beginning with If: {label[:40]}",
                    drafter.is_draftable({"label": label, "answer_key": "why_company"}))

    # The other half of the cure: the model is TOLD the answers the deterministic pass put
    # on the same form, so it cannot contradict them even when it does draft.
    facts = "\n".join(drafter.FACTS).lower()
    ok &= check("the facts block names work authorization", "authorized to work" in facts)
    ok &= check("the facts block names sponsorship", "sponsorship" in facts)
    ok &= check("the facts block reaches the single prompt",
                drafter.FACTS_BLOCK[:30] in drafter.build_prompt({"label": "Why us?", "answer_key": "why_company"}, None))
    ok &= check("the facts block reaches the batch prompt",
                drafter.FACTS_BLOCK[:30] in drafter.build_batch_prompt(
                    [{"label": "Why us?", "answer_key": "why_company"},
                     {"label": "Why this role?", "answer_key": "why_role"}], None))
    ok &= check("the prompt forbids inventing a fact it was not given",
                "do NOT decide what it is" in drafter.build_prompt({"label": "X", "answer_key": "y"}, None))
    # It must hold no personal data of its own — same rule as the bio.
    ok &= check("the facts block is built from the profile, not hardcoded",
                "FACTS = _load_facts()" in Path(drafter.__file__).read_text(encoding="utf-8"))

    ok &= outreach_units(check)

    print(f"\ndrafter units: {'all passed' if ok else 'FAILURES ABOVE'}")
    return 0 if ok else 1


def outreach_units(check) -> bool:
    """Phase 2 of the referral flow: the prompt, the validators, and the persistence rule.

    The CLI boundary is stubbed throughout, so nothing is spent and no `claude` binary is
    needed. Every person here is invented; the profile slug carries the `example-` prefix
    that scripts/pii_guard.py reserves for fixtures.
    """
    import json as _json
    ok = True

    RECORD = {
        "id": "linkedin.com/in/example-ivo-brennan-31",
        "name": "Ivo Brennan",
        "headline": "Data Scientist at Northwind",
        "company": "Northwind",
        "tie": "alum",
        "notes": "same lab, two years above me",
        "show": 2,
        "reqs": [
            {"title": "Data Scientist, Payments", "url": "https://example.com/jobs/111",
             "ats_job_id": "111", "location": "New York, NY", "field": "ds",
             "apply_after": "2026-08-20", "resume": {"tag": "ds", "path": "resumes/ds.pdf"}},
            {"title": "Quant Researcher", "url": "https://example.com/jobs/222",
             "ats_job_id": "222", "location": "New York, NY", "field": "quant",
             "apply_after": "2026-08-24"},
            {"title": "Third Role", "url": "https://example.com/jobs/333",
             "ats_job_id": "333", "field": "tech"},
        ],
        "applied": [],
        "drafts": {},
    }

    p = drafter.build_outreach_prompt(RECORD)
    ok &= check("outreach prompt names the contact", "Ivo Brennan" in p)
    ok &= check("outreach prompt names the company", "Northwind" in p)
    ok &= check("outreach prompt carries Parth's own notes verbatim",
                "same lab, two years above me" in p)
    ok &= check("outreach prompt states the tie", "same school" in p)
    ok &= check("outreach prompt names the req id", "req id 111" in p)
    ok &= check("outreach prompt carries the link", "https://example.com/jobs/111" in p)
    # `show` is what stops a referral ask reading as a mass send. The third req is carried
    # on the record for a "next req" button, but must not reach the prompt.
    ok &= check("outreach prompt shows only `show` reqs", "Third Role" not in p)
    ok &= check("outreach prompt forbids dashes", "NEVER use a dash" in p)
    # The one rule that INVERTS the application-answer style. If this ever silently flips
    # back, every referral ask goes out with a deliberate typo in it.
    ok &= check("outreach prompt forbids a typo", "Do NOT include a typo" in p)
    ok &= check("outreach prompt forbids claiming they are hiring",
                "NEVER say or imply that this person is hiring" in p)
    ok &= check("outreach prompt states the 300 char cap", str(drafter.INVITE_CAP) in p)
    ok &= check("outreach prompt asks for every variant",
                all(v in p for v in drafter.OUTREACH_VARIANTS))
    # No earlier application: the variant must be suppressed rather than invented.
    ok &= check("no application means already_applied is left empty",
                "leave\n`already_applied` as an empty string" in p.replace("\n", "\n")
                or "leave `already_applied` as an empty string" in p.replace("\n", " "))

    applied_rec = {**RECORD, "applied": [{"title": "Data Scientist", "ats_job_id": "999",
                                          "applied": "2026-08-01"}]}
    pa = drafter.build_outreach_prompt(applied_rec)
    ok &= check("an earlier application is named with its req id", "req id 999" in pa)
    ok &= check("an earlier application is dated", "2026-08-01" in pa)
    ok &= check("the ask becomes add-a-referral, not refer-me-fresh",
                "EXISTING application" in pa)

    norequ = drafter.build_outreach_prompt({**RECORD, "reqs": []})
    ok &= check("with no open req, only the invite and bump are asked for",
                "no open roles to name" in norequ)

    # --- sanitize_outreach ---------------------------------------------------------
    ok &= check("outreach sanitize kills every dash form",
                "-" not in drafter.sanitize_outreach("a — b – c - d"))
    # yes_pack IS a bulleted list, so line structure has to survive. `sanitize()` collapsing
    # everything into one paragraph is right for an essay answer and wrong here.
    multi = drafter.sanitize_outreach("line one\n- bullet a\n- bullet b", 500)
    ok &= check("outreach sanitize keeps line structure", multi.count("\n") == 2)
    long = drafter.sanitize_outreach("word " * 200, drafter.INVITE_CAP)
    ok &= check("an over-long note is truncated to the cap", len(long) <= drafter.INVITE_CAP)
    ok &= check("truncation lands on a word boundary", not long.rstrip(".").endswith("wor"))

    # --- _parse_outreach -----------------------------------------------------------
    good = _json.dumps({"invite_note": "hi there", "ask_dm": "one role", "yes_pack": "",
                        "bump": "still keen", "already_applied": ""})
    parsed = drafter._parse_outreach("here you go:\n```json\n" + good + "\n```")
    ok &= check("parses the batch out of surrounding chatter", parsed.get("ask_dm") == "one role")
    ok &= check("an empty variant is dropped, not stored blank",
                "yes_pack" not in parsed and "already_applied" not in parsed)
    ok &= check("garbage parses to nothing rather than raising",
                drafter._parse_outreach("not json at all") == {})
    ok &= check("a null reply is not fatal", drafter._parse_outreach(None) == {})

    # --- draft_outreach: gating, the retry, and what must never be overwritten -------
    saved_run, saved_enabled = drafter._run_claude, drafter._ENABLED
    saved_bin = drafter.CLAUDE_BIN
    try:
        drafter._ENABLED, drafter.CLAUDE_BIN = True, "stub"
        calls = []

        def fake(prompt):
            calls.append(prompt)
            return _json.dumps({"invite_note": "short note", "ask_dm": "the ask",
                                "yes_pack": "the pack", "bump": "the bump",
                                "already_applied": ""})
        drafter._run_claude = fake
        got = drafter.draft_outreach(RECORD, persist=False)
        ok &= check("one contact costs ONE call", len(calls) == 1)
        ok &= check("every applicable variant comes back", got.get("bump") == "the bump")

        # An existing draft may have been edited by hand. Replacing it silently is the one
        # failure Parth would not notice until after he sent it.
        calls.clear()
        edited = {**RECORD, "drafts": {"invite_note": "hand edited"}}
        got = drafter.draft_outreach(edited, persist=False)
        ok &= check("existing drafts win and cost no call",
                    got["invite_note"] == "hand edited" and not calls)
        got = drafter.draft_outreach(edited, force=True, persist=False)
        ok &= check("--force re-drafts", got["invite_note"] == "short note" and len(calls) == 1)

        # The 300-char cap is LinkedIn's own, so an over-long note is a REJECTED request.
        calls.clear()

        def over(prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return _json.dumps({"invite_note": "x " * 300, "ask_dm": "a", "yes_pack": "",
                                    "bump": "b", "already_applied": ""})
            return "a much shorter note"
        drafter._run_claude = over
        got = drafter.draft_outreach(RECORD, persist=False)
        ok &= check("an over-long invite triggers exactly one rewrite", len(calls) == 2)
        ok &= check("the rewrite is what gets stored", got["invite_note"] == "a much shorter note")
        ok &= check("the rewrite prompt states the limit", str(drafter.INVITE_CAP) in calls[1])

        # Drafting off must cost nothing, same kill switch as every other draft path.
        calls.clear()
        drafter._ENABLED = False
        ok &= check("the kill switch stops outreach drafting too",
                    drafter.draft_outreach(RECORD, persist=False) == {} and not calls)
    finally:
        drafter._run_claude, drafter._ENABLED, drafter.CLAUDE_BIN = (
            saved_run, saved_enabled, saved_bin)
    return ok


if __name__ == "__main__":
    raise SystemExit(main())
