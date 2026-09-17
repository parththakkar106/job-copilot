#!/usr/bin/env python3
"""Tests for the optional AI fill + verify pass (bridge/aipass.py + its bridge wiring).

Nothing here talks to Claude: `drafter._run_claude` is the one subprocess boundary and it
is monkeypatched throughout, so the suite is offline and deterministic. What is actually
being pinned down is the POLICY, because that is the part with teeth — which fields the
model may write to, which it may only argue about, and which it may never touch at all:

  fill    a field the deterministic pass left empty        -> written, low confidence too
  replace a field that already had a value                 -> written ONLY at REPLACE_MIN+
  flag    the same, below that bar                         -> nothing written
  propose an always_ask question (salary, notice, YOE)     -> never written, ever

Every one of those is a decision that ends up on a real application, so each has a check.
The bank-learning path writes to a TEMP answer bank — never the real one, whose answers
and use counters are live data.
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import bank as bank_mod
import aipass
import drafter
import pending as pending_mod
import bridge as B


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def field(fid, label, **kw):
    """One serialized control, in the shape fill.js's poolAdd emits."""
    return {"id": fid, "label": label, "kind": kw.get("kind", "text"),
            "bucket": kw.get("bucket"), "required": kw.get("required", False),
            "options": kw.get("options", []), "value": kw.get("value", ""),
            "source": kw.get("source", ""),
            **({"answer_key": kw["answer_key"]} if kw.get("answer_key") else {})}


def use_temp_bank(rows):
    tmp = Path(tempfile.mkdtemp()) / "answer_bank.yaml"
    tmp.write_text(yaml.safe_dump({"answers": rows}, sort_keys=False), encoding="utf-8")
    bank_mod.BANK = tmp
    bank_mod.BANK_WRITE = tmp
    return tmp


def use_temp_prefs():
    """Point the shared prefs file at a throwaway, so flipping the flag in a test never
    touches the toggle state Parth is actually running with."""
    import prefs
    tmp = Path(tempfile.mkdtemp()) / "prefs.json"
    prefs.PREFS_PATH = tmp
    return tmp


# --------------------------------------------------------------------- the flag ---

def test_flag():
    use_temp_prefs()
    checks = 0
    aipass._ENABLED = None

    # OFF unless someone says otherwise: the pass spends a Claude call on every form.
    assert aipass.is_enabled() is False; checks += 1
    assert aipass.set_enabled(True) is True; checks += 1
    assert aipass.is_enabled() is True; checks += 1
    # The persisted value is what the OTHER process (the web console) reads.
    import prefs
    assert prefs.get("ai_fill_enabled") is True; checks += 1
    aipass.set_enabled(False)
    assert aipass.is_enabled() is False; checks += 1

    # available() is the flag AND a binary — on with nothing to run is not available.
    aipass._ENABLED = True
    real = drafter.CLAUDE_BIN
    drafter.CLAUDE_BIN = None
    assert aipass.available() is False; checks += 1
    drafter.CLAUDE_BIN = "/fake/claude"
    assert aipass.available() is True; checks += 1
    drafter.CLAUDE_BIN = real
    aipass._ENABLED = None
    return checks


# ------------------------------------------------------------------- the prompt ---

def test_prompt():
    checks = 0
    fields = [
        field("q1", "Email", value="p@example.com", source="deterministic", required=True),
        field("q2", "Preferred pronouns", options=["He/him", "She/her"], kind="select"),
        field("q3", "Desired salary", bucket="always_ask", required=True),
        field("q4", "Why this company?", value="Saved wording.", source="bank",
              answer_key="why_company"),
    ]
    p = aipass.build_prompt(fields, {"company": "Ramp", "role": "Quant"})

    assert "Ramp" in p and "Quant" in p; checks += 1
    # Every field is described, numbered, with its shape.
    for lab in ("Email", "Preferred pronouns", "Desired salary", "Why this company?"):
        assert lab in p; checks += 1
    assert "He/him | She/her" in p; checks += 1
    assert "REQUIRED" in p; checks += 1
    # Where a value came from is stated, because that is what decides whether it may be
    # rewritten. A bank answer described as a rule-engine guess is how his own confirmed
    # wording quietly stops being used.
    assert "answer bank" in p; checks += 1
    assert "rule engine" in p; checks += 1
    assert "p@example.com" in p; checks += 1
    # always_ask carries its own instruction ON the field, not only in the rules block.
    assert "PARTH ANSWERS THIS ONE HIMSELF" in p; checks += 1
    # The standing guardrails.
    assert "NEVER invent a fact" in p; checks += 1
    assert "never" in p.lower() and "dash" in p.lower(); checks += 1
    assert "(empty)" in p; checks += 1
    return checks


# -------------------------------------------------------------------- the parse ---

def test_parse():
    checks = 0
    fields = [field("a", "One"), field("b", "Two"), field("c", "Three")]

    raw = ('Sure, here you go:\n```json\n'
           '{"1": {"value": "first", "confidence": 0.9, "why": "obvious"},'
           ' "2": "bare string", "3": {"value": "  ", "confidence": 1}}\n```')
    got = aipass.parse(raw, fields)
    assert got["a"]["value"] == "first" and got["a"]["confidence"] == 0.9; checks += 1
    assert got["a"]["why"] == "obvious"; checks += 1
    # A bare string is still an answer; it just carries no stated confidence.
    assert got["b"]["value"] == "bare string" and got["b"]["confidence"] == 0.5; checks += 1
    # An empty value is not an answer.
    assert "c" not in got; checks += 1

    # Percent-style confidence, which the CLI does return.
    got = aipass.parse('{"1": {"value": "x", "confidence": "85%"}}', fields)
    assert got["a"]["confidence"] == 0.85; checks += 1
    # Garbage never raises, it just means no AI decisions.
    assert aipass.parse("no json at all", fields) == {}; checks += 1
    assert aipass.parse("", fields) == {}; checks += 1
    assert aipass.parse('{"9": {"value": "nowhere"}}', fields) == {}; checks += 1
    return checks


# ------------------------------------------------------------------ the policy ----

def test_decide():
    checks = 0
    fields = [
        field("empty", "Do you have any projects (GitHub/links) to share?"),
        field("held", "Cover letter", value="Old text.", source="deterministic"),
        field("shaky", "How did you hear about us?", value="Website", source="deterministic"),
        field("salary", "Desired base salary", bucket="always_ask", required=True),
        field("agree", "Full name", value="Parth Thakkar", source="deterministic"),
        field("phone", "Phone number", value="555 000 1111", source="deterministic"),
        field("pick", "Employment type", options=["Full-time", "Part-time"], kind="select"),
    ]
    answers = {
        "empty": {"value": "A few, github.com/x and a site.", "confidence": 0.9, "why": "asked for links"},
        "held": {"value": "New and better text.", "confidence": 0.95, "why": "old one ignored the question"},
        "shaky": {"value": "LinkedIn", "confidence": 0.4, "why": "not sure"},
        "salary": {"value": "150000", "confidence": 0.99, "why": "market rate"},
        "agree": {"value": "Parth Thakkar", "confidence": 1.0, "why": "same"},
        "phone": {"value": "555 999 8888", "confidence": 0.97, "why": "looks mis-mapped"},
        "pick": {"value": "Full-time", "confidence": 0.9, "why": "the posting says so"},
    }
    out = {d["id"]: d for d in aipass.decide(fields, answers)}

    # An empty field is filled.
    assert out["empty"]["action"] == "fill"; checks += 1
    assert out["empty"]["was"] == ""; checks += 1
    # A held value is overwritten only when confident, and the old value comes along.
    assert out["held"]["action"] == "replace"; checks += 1
    assert out["held"]["was"] == "Old text."; checks += 1
    # Below the bar: nothing is written, the disagreement is recorded.
    assert out["shaky"]["action"] == "flag"; checks += 1
    assert out["shaky"]["was"] == "Website"; checks += 1
    assert out["shaky"]["low"] is True; checks += 1
    # always_ask is a proposal whatever the confidence. This is the line that must not move:
    # a salary number typed into a real form is not recoverable by reviewing it afterwards.
    assert out["salary"]["action"] == "propose"; checks += 1
    assert out["salary"]["confidence"] == 0.99; checks += 1
    # Agreeing with what is already there is not news.
    assert "agree" not in out; checks += 1
    # A confident overwrite of a hard fact is marked for the top of the review.
    assert out["phone"]["action"] == "replace" and out["phone"]["hot"] is True; checks += 1
    assert out["held"]["hot"] is False; checks += 1

    # Worst-first ordering: the hot overwrite leads, the plain fill trails.
    order = [d["id"] for d in aipass.decide(fields, answers)]
    assert order[0] == "phone", order; checks += 1
    assert order.index("shaky") < order.index("empty"), order; checks += 1

    # Prose gets the house dash pass; an option value must survive intact or it stops
    # matching anything on the page.
    got = aipass.decide(
        [field("p", "Tell us about yourself"), field("o", "Type", options=["Full-time"], kind="select")],
        {"p": {"value": "I build end—to—end systems", "confidence": 0.9},
         "o": {"value": "Full-time", "confidence": 0.9}})
    by = {d["id"]: d for d in got}
    assert "—" not in by["p"]["value"]; checks += 1
    assert by["o"]["value"] == "Full-time"; checks += 1

    # An answer for a field we never sent is ignored rather than guessed at.
    assert aipass.decide(fields, {"ghost": {"value": "x", "confidence": 1}}) == []; checks += 1
    return checks


# ---------------------------------------------------------------------- the run ---

def test_run():
    checks = 0
    calls = []

    def fake(prompt, model=None, timeout=None):
        calls.append({"prompt": prompt, "model": model, "timeout": timeout})
        return '{"1": {"value": "written", "confidence": 0.9, "why": "because"}}'

    real, real_bin = drafter._run_claude, drafter.CLAUDE_BIN
    drafter._run_claude, drafter.CLAUDE_BIN = fake, "/fake/claude"
    try:
        fields = [field("q1", "Anything else?")]

        # OFF: not a single call. The toggle has to be load-bearing or it isn't a toggle.
        aipass._ENABLED = False
        assert aipass.run(fields, {}) == []; checks += 1
        assert calls == []; checks += 1

        aipass._ENABLED = True
        out = aipass.run(fields, {"company": "Ramp"})
        assert len(calls) == 1; checks += 1
        assert out and out[0]["value"] == "written" and out[0]["action"] == "fill"; checks += 1
        # Its own model + clock, not the drafter's: this call reads a whole form.
        assert calls[0]["model"] == aipass.AI_MODEL; checks += 1
        assert calls[0]["timeout"] == aipass.AI_TIMEOUT; checks += 1

        # ONE call for the whole form, however many fields there are — the cost rule.
        calls.clear()
        aipass.run([field(f"q{i}", f"Question {i}") for i in range(12)], {})
        assert len(calls) == 1; checks += 1

        # Nothing to ask about: no call.
        calls.clear()
        assert aipass.run([], {}) == []; checks += 1
        assert aipass.run([field("x", "   ")], {}) == []; checks += 1
        assert calls == []; checks += 1

        # A CLI that fails degrades to the deterministic-only fill, never an exception.
        drafter._run_claude = lambda *a, **k: None
        assert aipass.run(fields, {}) == []; checks += 1
        # ...and so does one that blows up. A pass that can raise is one every future
        # caller has to remember to wrap, and the one that forgets kills a fill mid-form.
        drafter._run_claude = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        assert aipass.run(fields, {}) == []; checks += 1

        # Too many controls: the prompt keeps the ones that matter (required, still empty).
        drafter._run_claude = fake
        calls.clear()
        many = ([field(f"opt{i}", f"Optional {i}", value="filled", source="deterministic")
                 for i in range(aipass.MAX_FIELDS + 20)]
                + [field("must", "A required empty one", required=True)])
        aipass.run(many, {})
        assert "A required empty one" in calls[0]["prompt"]; checks += 1
    finally:
        drafter._run_claude, drafter.CLAUDE_BIN = real, real_bin
        aipass._ENABLED = None
    return checks


# ----------------------------------------------------------------- the wiring -----

async def test_handler():
    checks = 0
    use_temp_bank([
        {"id": "why_company_ramp", "key": "why_company", "scope": "company", "company": "Ramp",
         "answer": "My own saved wording about Ramp.", "status": "active", "uses": 0,
         "last_used": None},
    ])
    fields = [field("q1", "Why Ramp?", answer_key="why_company"),
              field("q2", "Anything else?")]

    # OFF: the extension is told so and no decisions come back, which is how fill.js knows
    # to leave the deterministic result alone.
    aipass._ENABLED = False
    ws = FakeWS()
    await B.handle_ai_pass({"reqId": "a1", "fields": fields, "context": {"company": "Ramp"}}, ws)
    assert ws.sent[0]["answers"] == {"enabled": False, "decisions": []}; checks += 1

    seen = {}

    def fake(prompt, model=None, timeout=None):
        seen["prompt"] = prompt
        return '{"2": {"value": "Yes, a bit more.", "confidence": 0.9, "why": "blank"}}'

    real, real_bin = drafter._run_claude, drafter.CLAUDE_BIN
    drafter._run_claude, drafter.CLAUDE_BIN = fake, "/fake/claude"
    aipass._ENABLED = True
    try:
        ws = FakeWS()
        await B.handle_ai_pass({"reqId": "a2", "fields": fields, "context": {"company": "Ramp"}}, ws)
        ans = ws.sent[0]["answers"]
        assert ans["enabled"] is True; checks += 1
        assert len(ans["decisions"]) == 1 and ans["decisions"][0]["id"] == "q2"; checks += 1
        # The bank answer was seeded IN before the model saw the form, so it is presented as
        # his confirmed wording rather than as an empty box to write fresh prose into.
        assert "My own saved wording about Ramp." in seen["prompt"]; checks += 1
        assert "answer bank" in seen["prompt"]; checks += 1

        # A blow-up inside the pass answers cleanly instead of killing the fill.
        drafter._run_claude = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        ws = FakeWS()
        await B.handle_ai_pass({"reqId": "a3", "fields": fields, "context": {}}, ws)
        assert ws.sent[0]["answers"]["decisions"] == []; checks += 1
    finally:
        drafter._run_claude, drafter.CLAUDE_BIN = real, real_bin
        aipass._ENABLED = None
    return checks


def test_seed_bank():
    checks = 0
    use_temp_bank([
        {"id": "why_company_common", "key": "why_company", "scope": "common", "company": None,
         "answer": "Generic saved answer.", "status": "active", "uses": 0, "last_used": None},
        {"id": "why_role_stale", "key": "why_role", "scope": "common", "company": None,
         "answer": "Never served.", "status": "stale", "uses": 0, "last_used": None},
    ])
    out = {f["id"]: f for f in B._seed_bank_values([
        field("a", "Why us?", answer_key="why_company"),
        field("b", "Why this role?", answer_key="why_role"),
        field("c", "Why us, again?", answer_key="why_company", value="Already typed",
              source="deterministic"),
        field("d", "No key at all"),
    ], "Ramp")}

    assert out["a"]["value"] == "Generic saved answer." and out["a"]["source"] == "bank"; checks += 1
    # Only status:active is served — the same rule the rest of the bank follows.
    assert out["b"]["value"] == ""; checks += 1
    # A field that already holds something is not overwritten by the seeding step.
    assert out["c"]["value"] == "Already typed" and out["c"]["source"] == "deterministic"; checks += 1
    assert out["d"]["value"] == ""; checks += 1
    return checks


def test_learning():
    checks = 0
    tmp = use_temp_bank([])
    long_answer = "A properly long answer about why this company, well past the length bar."
    rec = {"dedup_key": "k1", "company": "Ramp", "ai_decisions": [
        {"id": "q1", "action": "fill", "value": long_answer, "answer_key": "why_company",
         "label": "Why us?"},
        {"id": "q2", "action": "flag", "value": long_answer, "answer_key": "why_role"},
        {"id": "q3", "action": "fill", "value": "Yes", "answer_key": "relocation"},
        {"id": "q4", "action": "fill", "value": long_answer},
    ]}
    written = B.learn_from_ai(rec)
    rows = {r["id"]: r for r in yaml.safe_load(tmp.read_text(encoding="utf-8"))["answers"]}

    assert len(written) == 1, written; checks += 1
    row = rows[written[0]]
    assert row["answer"] == long_answer; checks += 1
    # DRAFT, never active. Approving an answer for one application is not the same as
    # blessing it as his standard wording, and `bank.py list --status draft` is the queue.
    assert row["status"] == "draft"; checks += 1
    assert row["scope"] == "company" and row["company"] == "Ramp"; checks += 1
    assert "ai-drafted" in row["tags"]; checks += 1
    # A flagged value was never written to the form, so it is not his answer to bank.
    assert not any(r.get("key") == "why_role" for r in rows.values()); checks += 1
    # A one-word pick and an untagged answer are worth nothing on the next company's form.
    assert not any(r.get("key") == "relocation" for r in rows.values()); checks += 1
    assert len(rows) == 1; checks += 1

    # Approving a second application for the same company does not duplicate the row.
    assert B.learn_from_ai(rec) == []; checks += 1
    assert len(yaml.safe_load(tmp.read_text(encoding="utf-8"))["answers"]) == 1; checks += 1
    # Nothing to learn is not an error.
    assert B.learn_from_ai({"dedup_key": "k2", "company": "X"}) == []; checks += 1
    return checks


def test_review_lines():
    checks = 0
    assert B.ai_review_lines([]) == []; checks += 1
    text = "\n".join(B.ai_review_lines([
        {"action": "replace", "label": "Phone", "value": "555 999 8888", "was": "555 000 1111",
         "confidence": 0.97, "why": "looked mis-mapped", "hot": True, "applied": True},
        {"action": "flag", "label": "How did you hear about us?", "value": "LinkedIn",
         "was": "Website", "low": True, "applied": False},
        {"action": "propose", "label": "Desired salary", "value": "150000", "applied": False},
        {"action": "fill", "label": "Never landed", "value": "x", "applied": False},
    ]))
    # An overwrite has to show the value it destroyed; that is the whole point of the line.
    assert "555 000 1111" in text and "555 999 8888" in text; checks += 1
    assert "REPLACED" in text; checks += 1
    assert "🔴" in text; checks += 1
    # A flag says plainly that nothing was written.
    assert "left as" in text and "Website" in text; checks += 1
    # A proposal must never read as something already on the form.
    assert "not filled" in text; checks += 1
    # A fill whose setter refused is not on the form, so it must not be vouched for.
    assert "Never landed" not in text; checks += 1
    return checks


def test_record_carries_decisions():
    """Phase 1 stores the decisions; phase 3 replays them instead of paying for a second
    opinion that could differ from the summary Parth approved."""
    checks = 0
    base = Path(tempfile.mkdtemp())
    decisions = [{"id": "q1", "action": "fill", "value": "written", "confidence": 0.9}]
    rec = pending_mod.put({"dedup_key": "k1", "company": "Ramp", "title": "Quant"},
                          [], [{"label": "Email", "value": "p@example.com"}],
                          base, [], decisions)
    assert rec["ai_decisions"] == decisions; checks += 1
    assert pending_mod.load("k1", base)["ai_decisions"] == decisions; checks += 1
    # An older record written before this feature existed reads as "no AI pass", not a crash.
    plain = pending_mod.put({"dedup_key": "k2", "company": "X", "title": "Y"}, [], [], base)
    assert plain["ai_decisions"] == []; checks += 1
    return checks


if __name__ == "__main__":
    n = (test_flag() + test_prompt() + test_parse() + test_decide() + test_run()
         + asyncio.run(test_handler()) + test_seed_bank() + test_learning()
         + test_review_lines() + test_record_carries_decisions())
    print(f"ai-pass tests: {n}/{n} green")
