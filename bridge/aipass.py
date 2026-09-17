"""aipass.py — the optional AI fill + verify pass: ONE `claude -p` call per form.

WHAT IT IS FOR
--------------
The deterministic pass (extension/fill.js + core/profile.py's taxonomy) is fast, free and
right about the fields it knows. It is wrong in two ways that only a reader of the whole
page can catch:

  1. A question it never learned stays empty and becomes another thing Parth types.
     Live: "Do you have any projects (GitHub/links) you would like to share?" matched the
     github-link rule, so the form got a bare URL where a couple of honest lines plus the
     links was the answer the question asked for.
  2. A rule can match the wrong field. Nothing downstream re-reads the form and asks
     "does this value actually answer that label?".

So this module hands the WHOLE serialized page to Claude once — every control, its label,
its options, whether it is required, what the deterministic pass put in it and where that
value came from — and takes back, in one reply, the blanks filled AND a verdict on the
values already there. Drafting, filling and verifying in a single call is deliberate: the
CLI reloads its entire system prompt on every invocation and that fixed overhead dominates
the cost, which is the same reason drafter.build_batch_prompt batches essays.

WHAT IT MAY AND MAY NOT DO (Parth's rules, 2026-09-17)
-----------------------------------------------------
- always_ask (salary, notice period, YOE gates) is NEVER written. The model may PROPOSE an
  answer; it rides to Parth as a draft he taps to accept, exactly like a bank draft.
- A field the deterministic pass left EMPTY is filled, whatever the confidence. A
  low-confidence fill is tagged and sorted to the top of the review rather than withheld,
  because a complete form he corrects beats a blocked one he has to finish by hand.
- A field that ALREADY holds a value is overwritten only at REPLACE_MIN confidence or
  above, and every overwrite carries the old value so the review reads "was X, now Y".
  Below that bar the model's disagreement is recorded as a flag and nothing is written.
- A saved answer-bank reply is used VERBATIM unless the question asks something that
  answer does not address. That is a rewrite the model must justify, and it goes through
  the same REPLACE_MIN gate as any other overwrite.

Identity and contact facts are NOT locked (Parth's call, asked and reaffirmed): the model
may overwrite a name, phone or graduation date at high confidence like anything else. The
mitigation is visibility, not prohibition — `decide()` marks those replacements `hot`, and
the review surfaces them at the very top next to the low-confidence fills.

Nothing here is trusted blindly and nothing here submits. Every decision lands in the same
end-of-form review gate the deterministic fills already pass through, and the submit is
still Parth's own tap.
"""
from __future__ import annotations

import json
import os
import re
import sys

import drafter
import prefs

# The pass is OFF by default and shares drafter's binary/timeout plumbing — one `claude`
# resolution, one subprocess boundary, one place to point at a different CLI.
_PREF_KEY = "ai_fill_enabled"
_ENABLED = None  # in-memory override for tests; None = defer to the persisted preference

# Model for the pass. Bigger prompt than a draft (the whole form), and the job is
# reasoning about field/label fit rather than prose, so it gets its own knob instead of
# riding DRAFT_MODEL — Parth may well want cheap drafts and a stronger verifier, or both.
AI_MODEL = os.environ.get("AI_PASS_MODEL", "sonnet").strip()
# Wall clock for the one call. Longer than a draft: it is reading a whole form.
AI_TIMEOUT = float(os.environ.get("AI_PASS_TIMEOUT", "180"))
# Confidence at or above which the model may OVERWRITE a value that is already on the form.
REPLACE_MIN = float(os.environ.get("AI_REPLACE_MIN", "0.8"))
# Below this, a fill is still written but tagged `low` and sorted to the top of the review.
LOW_CONF = float(os.environ.get("AI_LOW_CONF", "0.6"))
# Hard ceiling on how many controls we describe in one prompt. A pathological page (a
# 400-row table of inputs) must not turn one call into an enormous one.
MAX_FIELDS = int(os.environ.get("AI_PASS_MAX_FIELDS", "120"))
# Per-answer length cap. The review card has to stay readable on a phone.
MAX_ANSWER = 1500

# Buckets whose values the model may never write. `always_ask` is Parth's own line: those
# answers are decisions, not facts, and no confidence score earns the right to guess one.
NEVER_WRITE = ("always_ask",)


def is_enabled() -> bool:
    """The on/off flag. In-memory override wins (tests); else the persisted preference,
    seeded once from AI_FILL_ENABLED, defaulting to OFF. Read fresh on every call so a
    toggle in the web console is seen by the running bridge with no restart."""
    if _ENABLED is not None:
        return bool(_ENABLED)
    val = prefs.get(_PREF_KEY)
    if val is None:
        return os.environ.get("AI_FILL_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


def set_enabled(on: bool) -> bool:
    """Persist the flag (shared with the web console) and return the new state."""
    prefs.set(_PREF_KEY, bool(on))
    return bool(on)


def available() -> bool:
    """True when the pass can actually run — flag on AND a `claude` binary exists."""
    return bool(is_enabled() and drafter.CLAUDE_BIN)


def _conf(raw) -> float:
    """The model's confidence as a 0-1 float. Accepts 0.82, "0.82", 82 and "82%", because
    all four come back in practice and a parse failure that reads as 0.0 would silently
    demote every overwrite to a flag."""
    try:
        v = float(str(raw).strip().rstrip("%"))
    except (TypeError, ValueError):
        return 0.0
    if v > 1.0:
        v = v / 100.0
    return max(0.0, min(1.0, v))


def _clean(text: str) -> str:
    """Strip wrapping quotes and cap the length. The no-dash rule belongs to PROSE answers
    only, so it is applied in `decide()` to free-text fields — running drafter.sanitize
    over a dropdown value would turn "Full-time" into "Full, time" and stop it matching
    any option on the page."""
    t = (text or "").strip()
    if len(t) >= 2 and t[0] in "\"'" and t[-1] == t[0]:
        t = t[1:-1].strip()
    return t[:MAX_ANSWER].strip()


def _is_prose(field: dict) -> bool:
    """Free-text the model is WRITING rather than picking. Only these get the dash pass
    and the house style; a select/radio/yes-no answer must stay matchable."""
    return not (field.get("options") or []) and str(field.get("kind") or "") in ("text", "textarea", "")


def _describe(field: dict, n: int) -> str:
    """One field, as the prompt sees it. Everything that decides an answer is on the line:
    what it asks, what shape it is, whether it blocks submit, what is in it now and where
    that came from — the model cannot see the page, so an omission here is a guess there."""
    bits = [f"{n}. {str(field.get('label') or '(unlabeled)').strip()}"]
    kind = str(field.get("kind") or "text")
    opts = [str(o).strip() for o in (field.get("options") or []) if str(o).strip()]
    if opts:
        bits.append("   choose exactly one of: " + " | ".join(opts[:25]))
    else:
        bits.append(f"   free text ({kind})")
    if field.get("required"):
        bits.append("   REQUIRED — the form will not submit without it")
    cur = str(field.get("value") or "").strip()
    src = str(field.get("source") or "")
    if cur:
        where = {
            "deterministic": "filled by the rule engine from the profile",
            "bank": "Parth's own saved answer from his answer bank",
            "already": "already on the form when we arrived (the ATS or its resume parser put it there)",
            "ai": "written by an earlier pass",
        }.get(src, "already present")
        bits.append(f"   CURRENT VALUE ({where}): {cur[:400]}")
    else:
        bits.append("   CURRENT VALUE: (empty)")
    if field.get("bucket") in NEVER_WRITE:
        bits.append("   PARTH ANSWERS THIS ONE HIMSELF. Propose an answer if you have a "
                    "grounded one, and say so; it will be shown to him, never typed in.")
    return "\n".join(bits)


def build_prompt(fields: list[dict], context: dict | None) -> str:
    """The whole instruction for the one call. Self-contained on purpose: `_run_claude`
    runs in a throwaway directory with no tools, so anything not written here does not
    exist as far as the model is concerned."""
    ctx = context or {}
    company = str(ctx.get("company") or "").strip()
    role = str(ctx.get("role") or ctx.get("title") or "").strip()

    where = []
    if company:
        where.append(f"Company: {company}")
    if role:
        where.append(f"Role: {role}")
    where_block = ("\n".join(where) + "\n\n") if where else ""

    listing = "\n\n".join(_describe(f, i) for i, f in enumerate(fields, 1))

    return (
        f"You are helping {drafter.NAME} finish a real job application form. Everything you "
        f"write will be typed into that form and submitted to a real employer, so an answer "
        f"you are not sure of costs him the job, not a retry.\n\n"
        f"{drafter.ABOUT}"
        f"{drafter.FACTS_BLOCK}"
        f"{where_block}"
        "Below is EVERY field on the form, with what is in it right now. Most values were "
        "filled by a rule engine that matches question wording to his profile. It is right "
        "about the fields it knows and it is occasionally wrong: it can match a rule to the "
        "wrong question, and it answers narrowly (dropping a bare URL into a question that "
        "asked him to talk about his projects).\n\n"
        "Do TWO jobs in this one reply:\n"
        "A. FILL every field that is empty and that you can answer from the facts above.\n"
        "B. VERIFY every field that already has a value. If the value genuinely answers its "
        "label, say nothing about it. Only tell me about the ones you would change.\n\n"
        f"The form:\n\n{listing}\n\n"
        "Rules, follow them exactly:\n"
        "- NEVER invent a fact. No employers, dates, numbers, degrees, titles or links that "
        "are not in the facts above. If a field needs a fact you were not given, leave it "
        "out of your reply entirely rather than guessing one.\n"
        "- NEVER contradict a fact above or another field on this same form. Work "
        "authorization, sponsorship and relocation are stated; an answer that fights them is "
        "a false statement on a real application.\n"
        "- A field that says 'choose exactly one of' must be answered with one of those "
        "options, copied exactly. Never invent an option.\n"
        "- A CURRENT VALUE that is Parth's own saved answer is his confirmed wording: keep "
        "it as it is. Rewrite it ONLY if the question asks something that answer does not "
        "address, and say that in your reason.\n"
        "- A question that only makes sense as a follow-up to another one ('if you answered "
        "yes above...') must be read against the answer actually given above it. If that "
        "answer was no, the honest reply is that it does not apply.\n"
        "- For free-text answers: write in the first person as him, sound like a real person "
        "in a hurry, 3 to 4 lines maximum, and NEVER use a dash of any kind (write 'end to "
        "end', not 'end-to-end').\n"
        "- Confidence is how sure you are that this exact text is the right answer to that "
        "exact question. Be honest and use the whole range. Anything you would not defend to "
        "him belongs below 0.5.\n\n"
        "Output ONLY a JSON object mapping the field's number (as a string) to "
        '{"value": "...", "confidence": 0.0 to 1.0, "why": "one short clause"}. Include a '
        "field ONLY if you are filling it or would change it — leave out every field you "
        "agree with. No preamble, no code fences, no explanation outside the JSON."
    )


def parse(raw: str, fields: list[dict]) -> dict:
    """The model's JSON reply, mapped back onto field ids. Lenient (pulls the outermost
    {...} out of any chatter) and never raises: a reply we cannot read means no AI
    decisions, which degrades exactly to the deterministic-only behaviour."""
    text = (raw or "").strip()
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b <= a:
        return {}
    try:
        obj = json.loads(text[a:b + 1])
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(obj, dict):
        return {}
    out: dict = {}
    for i, f in enumerate(fields, 1):
        got = obj.get(str(i))
        if isinstance(got, str):          # a bare string reply, no confidence given
            got = {"value": got, "confidence": 0.5}
        if not isinstance(got, dict):
            continue
        value = _clean(str(got.get("value") or ""))
        if not value or not f.get("id"):
            continue
        out[f["id"]] = {"value": value, "confidence": _conf(got.get("confidence")),
                        "why": _clean(str(got.get("why") or ""))[:200]}
    return out


# A replacement worth putting at the very top of the review. These are the fields where a
# confident-but-wrong overwrite is unrecoverable: a wrong phone number or a wrong
# graduation date goes out looking exactly as deliberate as a right one. Parth chose not to
# lock them, so they are marked instead — see the module docstring.
_HOT = re.compile(
    r"\b(name|e-?mail|phone|mobile|address|street|city|zip|postal|"
    r"school|university|college|degree|major|discipline|graduat|"
    r"employer|company you|linkedin|github|website|portfolio|"
    r"sponsor|visa|authoriz|work authorization)\b", re.IGNORECASE)


def decide(fields: list[dict], answers: dict) -> list[dict]:
    """Turn the model's raw answers into the four things the extension may do with them.

    action:
      "fill"    — the field was empty, write it. (Low confidence writes too, and is tagged.)
      "replace" — the field had a value and the model is confident enough to overwrite it.
      "flag"    — the model disagrees but not confidently: write NOTHING, show the pair.
      "propose" — always_ask. Never written; offered to Parth as a draft he taps to accept.

    Returns one record per decision, carrying everything the review needs to be honest
    about what happened: the old value, the new one, the confidence and the model's reason.
    """
    by_id = {f.get("id"): f for f in fields if f.get("id")}
    out = []
    for fid, got in answers.items():
        f = by_id.get(fid)
        if not f:
            continue
        value = got["value"]
        conf = got["confidence"]
        # The house style is for prose only. A dropdown value must survive intact or it
        # stops matching any option on the page.
        if _is_prose(f):
            value = drafter.sanitize(value)
        if not value:
            continue
        current = str(f.get("value") or "").strip()
        if f.get("bucket") in NEVER_WRITE:
            action = "propose"
        elif not current:
            action = "fill"
        elif value.strip() == current:
            continue                       # agreeing with what is already there is not news
        elif conf >= REPLACE_MIN:
            action = "replace"
        else:
            action = "flag"
        out.append({
            "id": fid,
            "label": f.get("label") or "",
            "action": action,
            "value": value,
            "was": current,
            "confidence": round(conf, 2),
            "why": got.get("why") or "",
            "low": conf < LOW_CONF,
            # A high-confidence overwrite of a hard fact — top of the review, always.
            "hot": action == "replace" and bool(_HOT.search(str(f.get("label") or ""))),
            **({"answer_key": f["answer_key"]} if f.get("answer_key") else {}),
        })
    # Review order IS this order: the things most likely to be wrong first, so the pass
    # Parth actually does starts where his attention is worth the most.
    rank = {"replace": 0, "flag": 1, "propose": 2, "fill": 3}
    out.sort(key=lambda d: (not d["hot"], not d["low"], rank.get(d["action"], 9)))
    return out


def run(fields: list[dict], context: dict | None = None) -> list[dict]:
    """The whole pass: one call, decisions out. [] when the flag is off, the binary is
    missing, there is nothing to ask about, or anything at all goes wrong — every one of
    those degrades to the deterministic-only fill, which is the behaviour without this
    module. Never raises."""
    if not available():
        return []
    usable = [f for f in (fields or []) if f.get("id") and str(f.get("label") or "").strip()]
    if not usable:
        return []
    if len(usable) > MAX_FIELDS:
        # Keep the ones that matter: required and still-empty first, then the rest.
        usable.sort(key=lambda f: (not f.get("required"), bool(str(f.get("value") or "").strip())))
        usable = usable[:MAX_FIELDS]
    try:
        raw = drafter._run_claude(build_prompt(usable, context), model=AI_MODEL, timeout=AI_TIMEOUT)
        if not raw:
            return []
        return decide(usable, parse(raw, usable))
    except Exception as e:
        # The caller's own guard would catch this, but the contract is what callers are
        # written against: a pass that can raise is one every future caller has to
        # remember to wrap, and the one that forgets kills a fill mid-form.
        print(f"[aipass] pass failed ({e}) — deterministic fill stands", file=sys.stderr)
        return []
