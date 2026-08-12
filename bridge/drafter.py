"""Tier-2 answer drafting via the Claude Code CLI (subscription, not the API).

The answer bank is still the source of truth (a saved answer always wins). This is the
fallback for the essays the bank has never seen — why_company, behavioral, anything_else —
so Parth gets a 👍-able draft instead of a cold "reply with your answer". It shells out to
the `claude` binary in print mode (`claude -p`), which bills the Claude Code subscription,
so there is no ANTHROPIC_API_KEY and no per-token cost. Deliberately best-effort: any
failure (no binary, timeout, empty output) returns None and the caller degrades to asking
cold, which is exactly the pre-drafting behaviour.

Drafts must read as genuinely written by Parth — see feedback-answer-writing-style: human
and natural, 3-4 lines, one small subtle typo, and NEVER a dash. That style lives in the
prompt; `sanitize()` is the belt-and-suspenders that strips any dash the model leaves in.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import prefs

# The `claude` binary. Override with CLAUDE_BIN; else whatever is on PATH.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
# Per-draft wall-clock cap. A hung CLI must never stall a Telegram push or a panel fill.
DRAFT_TIMEOUT = float(os.environ.get("DRAFT_TIMEOUT", "90"))
# Model for the draft call, pinned via `claude --model`. Default Sonnet — better prose than
# Haiku for the essays, and the per-call cost gap between tiers is small (the CLI's fixed
# system-prompt reload dominates, not the model). Set DRAFT_MODEL="" or "default" to let the
# CLI pick (Parth's Opus pin); "haiku" for the cheapest calls.
DRAFT_MODEL = os.environ.get("DRAFT_MODEL", "sonnet").strip()
# In-memory override for the enabled flag. None = defer to the persisted preference (the
# normal case). Tests set this to a bool to force a state without touching prefs.json.
_ENABLED = None
# Preference key + env seed. The flag is ON by default (Parth's choice) — drafts are
# offered automatically and he can flip it OFF anytime from Telegram (/draft off) or the
# web console. A saved bank answer still always wins over a Claude draft.
_PREF_KEY = "drafts_enabled"


def is_enabled() -> bool:
    """The on/off flag Parth controls. In-memory override wins (tests); otherwise the
    persisted preference, seeded once by the DRAFTS_ENABLED env var, defaulting to OFF.
    Read fresh each call so a toggle in the web process is seen by the bridge live."""
    if _ENABLED is not None:
        return bool(_ENABLED)
    val = prefs.get(_PREF_KEY)
    if val is None:
        return os.environ.get("DRAFTS_ENABLED", "1").strip().lower() not in ("0", "false", "no", "")
    return bool(val)


def set_enabled(on: bool) -> bool:
    """Persist the flag (survives restarts, shared with the web console). Returns the new
    state. The bridge and web both read is_enabled() fresh, so no restart is needed."""
    prefs.set(_PREF_KEY, bool(on))
    return bool(on)


def available() -> bool:
    """True when drafting can actually run — the flag is on AND a `claude` binary exists."""
    return bool(is_enabled() and CLAUDE_BIN)


def _load_identity() -> tuple[str, str]:
    """(name, bio) used to ground every draft, built entirely from profile.json.

    Nothing about a person is hardcoded here, deliberately. An earlier version carried a
    literal fallback bio naming one specific applicant, and it sat on BOTH paths: a profile
    that failed to load grounded every draft in a stranger's employer and school, and even
    the success path prefixed the wrong name. An ungrounded draft is recoverable; a fluent,
    confident one describing someone else's career is not — it goes to a real employer.
    So a profile we can't read yields ("the applicant", "") and the prompt drops the
    About line rather than inventing a person.
    """
    try:
        root = pathlib.Path(__file__).resolve().parent.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from core import paths
        p = json.loads(paths.PROFILE.read_text(encoding="utf-8"))
    except Exception:
        return ("the applicant", "")

    # profile.json marks a value the user still owes as the literal FILL_ME rather than
    # omitting the key, so a bare .get() returns a placeholder that reads as real text.
    # Unfiltered, a fresh install grounds every draft in "currently FILL_ME at FILL_ME".
    def val(d: dict, k: str) -> str:
        v = d.get(k)
        v = v.strip() if isinstance(v, str) else ""
        return "" if v == "FILL_ME" else v

    ident = p.get("identity") or {}
    emp = p.get("employment") or {}
    edu = p.get("education") or {}

    name = (val(ident, "full_name")
            or " ".join(x for x in (val(ident, "first_name"), val(ident, "last_name")) if x)).strip()

    bits = []
    title, employer = val(emp, "current_title"), val(emp, "current_employer")
    if title and employer:
        bits.append(f"currently {title} at {employer}")
    elif title or employer:
        bits.append(f"currently {title or employer}")
    school = val(edu, "school")
    if school:
        deg = f"{val(edu, 'degree')} {val(edu, 'major')}".strip()
        grad = val(edu, "graduation_date")
        bits.append((f"{deg} from {school}" if deg else school) + (f" ({grad})" if grad else ""))

    if not bits:
        return (name or "the applicant", "")

    bio = ((name + ", ") if name else "") + ". ".join(bits) + "."
    # Optional free-text line ("quant, data science, and AI/ML roles in the US") — it says
    # what the applicant is aiming FOR, which the structured fields above can't express.
    target = val(p.get("preferences") or {}, "target_roles")
    if target:
        bio += f" Targeting {target}."
    return (name or "the applicant", bio)


def _load_facts() -> list[str]:
    """Answers the DETERMINISTIC pass already puts on the same form, restated for the model.

    THE FAILURE THIS FIXES, live on a real Robinhood application: the deterministic
    pass correctly filled "Will you now (or in the future) require visa sponsorship? -> No",
    and the drafter, handed only the label "If you answered Yes to the above question, please
    provide additional information here:", wrote "Yes I will need visa sponsorship down the
    line, right now I'm on my OPT ... will eventually need H1B support." Fluent, confident,
    and the opposite of what the same form said two fields earlier. A second draft invented a
    non-compete with the current employer.

    The bio alone could not prevent that: it says where someone works and studied, and says
    nothing about work authorization, so the model filled the gap. These are the facts most
    often asked about in free text right after being asked as a checkbox, so they are the
    ones worth spending prompt space on. Built from profile.json at runtime and held nowhere.
    """
    try:
        root = pathlib.Path(__file__).resolve().parent.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from core import paths
        p = json.loads(paths.PROFILE.read_text(encoding="utf-8"))
    except Exception:
        return []

    def yn(block: str, key: str) -> str | None:
        v = (p.get(block) or {}).get(key)
        return None if v is None or v == "FILL_ME" else ("Yes" if v is True else "No" if v is False else None)

    def txt(block: str, key: str) -> str:
        v = (p.get(block) or {}).get(key)
        v = v.strip() if isinstance(v, str) else ""
        return "" if v == "FILL_ME" else v

    out = []
    pairs = [
        ("work_authorization", "authorized_us", "Legally authorized to work in the US"),
        ("work_authorization", "require_sponsorship", "Requires visa sponsorship now or in the future"),
        ("work_authorization", "relocation", "Willing to relocate"),
        ("attestations", "non_compete", "Bound by a non-compete or restrictive covenant"),
        ("attestations", "criminal_history", "Has a criminal record"),
        ("attestations", "security_clearance", "Holds an active security clearance"),
    ]
    for block, key, phrasing in pairs:
        a = yn(block, key)
        if a:
            out.append(f"{phrasing}: {a}")
    status = txt("work_authorization", "status")
    if status:
        out.append(f"Work-authorization status: {status}")
    return out


NAME, BIO = _load_identity()
# Omitted entirely rather than left as a dangling "About: " when the profile is unreadable.
ABOUT = f"About {NAME}: {BIO}\n\n" if BIO else ""
FACTS = _load_facts()
FACTS_BLOCK = (
    "Facts already stated elsewhere on this same application. An answer that contradicts\n"
    "any of them is a false statement to a real employer, so never write against one:\n"
    + "\n".join(f"- {f}" for f in FACTS) + "\n\n"
) if FACTS else ""


# A label whose whole meaning is "expand on the PREVIOUS question" — the model is handed a
# pointer and no target. Live example, Robinhood: "If you answered "Yes" to the
# above question, please provide additional information here:". There is nothing in that
# string to ground on, so the model chose what the previous answer must have been and wrote
# a confident paragraph on it, contradicting the deterministic pass on the same form.
_REFERS_ELSEWHERE = re.compile(
    r"""^\s*\(?\s*if\s+(you\s+)?(answered|selected|checked|chose|responded|said)?\s*["'“”]?\s*yes\b"""
    r"""|^\s*\(?\s*if\s+(yes|so|applicable|any|selected)\b"""
    r"""|\bto\s+the\s+(above|previous|preceding)\s+question\b"""
    r"""|^\s*if\s+you\s+(answered|checked|selected)\b""",
    re.IGNORECASE,
)


def refers_to_another_question(item: dict) -> bool:
    """Is this label meaningless on its own — a follow-up to a question we are not showing?"""
    return bool(_REFERS_ELSEWHERE.search(str(item.get("label") or "")))


def is_draftable(item: dict) -> bool:
    """Only free-text essay questions the taxonomy routed to a draftable answer_key. A
    multiple-choice item (options present) is a pick, not an essay, so never draft it.

    A conditional follow-up is refused here too: AUTOMATIC drafting is the dangerous path,
    because nobody reads the result before it is stored on the record. The explicit
    "Draft it" tap (drafts_on_request) still allows it — Parth asked, he can see the
    question above it on screen, and the draft lands in an editable box in front of him.
    """
    return (bool(item.get("answer_key")) and not item.get("options")
            and not refers_to_another_question(item))


def drafts_on_request(item: dict) -> bool:
    """May we draft this because Parth explicitly ASKED (the panel's "Draft it" button)?

    Waives the answer_key requirement — taxonomy.yaml only tags question shapes it has seen
    before, so a one-off prompt ("our purpose is economic empowerment, what does that mean to
    you?") arrives untagged and would otherwise never be draftable no matter how much he
    wants a draft. Keeps the essay shape: a multiple-choice item is a pick, and prose is the
    wrong answer for it. A label is required too — an unlabeled control gives the model
    nothing to write about."""
    return bool(str(item.get("label") or "").strip()) and not item.get("options")


def build_prompt(item: dict, context: dict | None) -> str:
    """The full self-contained instruction handed to `claude -p`. Everything the draft
    needs is inline (no tools, no file reads) so the CLI answers from this text alone."""
    ctx = context or {}
    company = (ctx.get("company") or "").strip()
    role = (ctx.get("role") or ctx.get("title") or "").strip()
    label = str(item.get("label") or "").strip()
    key = str(item.get("answer_key") or "").strip()

    where = []
    if company:
        where.append(f"Company: {company}")
    if role:
        where.append(f"Role: {role}")
    where_block = ("\n".join(where) + "\n") if where else ""

    return (
        f"You are drafting a job-application answer that {NAME} will submit as their own. "
        f"Write it in the first person, as {NAME}.\n\n"
        f"{ABOUT}"
        f"{FACTS_BLOCK}"
        f"{where_block}"
        f"Question to answer: {label}\n"
        f"(internal category: {key or 'general'})\n\n"
        "Rules for the answer, follow them exactly:\n"
        "- Sound like a real person wrote it in a hurry, not polished or AI-generated.\n"
        "- Keep it short: 3 to 4 lines maximum.\n"
        "- Include ONE small, subtle typo (a missing apostrophe or a dropped letter). Just one, and keep it easy to miss.\n"
        "- NEVER use a dash of any kind. No em dash, no en dash, no hyphen used as punctuation. "
        "Rewrite any phrase that would need one (say 'end to end', not 'end-to-end').\n"
        f"- Be specific and grounded in the facts about {NAME} above. Do not invent employers, dates, or numbers.\n"
        "- If the question turns on a fact you were not given, do NOT decide what it is. Say you are "
        "happy to go into detail, and leave the specifics out.\n"
        "- Do not use any tools. Write only from what is given here.\n\n"
        "Output ONLY the answer text itself. No preamble, no quotes, no explanation, no sign-off."
    )


def build_batch_prompt(items: list[dict], context: dict | None) -> str:
    """One prompt covering ALL of a job's essay questions, so a single `claude -p` call
    drafts them together. The CLI reloads its whole system prompt on every call (that fixed
    overhead dominates the cost, not the essays), so N questions in 1 call is ~N times
    cheaper than N calls. The model returns a JSON object keyed by question number."""
    ctx = context or {}
    company = (ctx.get("company") or "").strip()
    role = (ctx.get("role") or ctx.get("title") or "").strip()

    where = []
    if company:
        where.append(f"Company: {company}")
    if role:
        where.append(f"Role: {role}")
    where_block = ("\n".join(where) + "\n\n") if where else ""

    numbered = "\n".join(f"{i}. {str(it.get('label') or '').strip()}"
                         for i, it in enumerate(items, 1))

    return (
        f"You are drafting several job-application answers that {NAME} will submit as their own. "
        f"Write each in the first person, as {NAME}.\n\n"
        f"{ABOUT}"
        f"{FACTS_BLOCK}"
        f"{where_block}"
        "Answer EVERY question in the numbered list below.\n\n"
        f"Questions:\n{numbered}\n\n"
        "Rules for EACH answer, follow them exactly:\n"
        "- Sound like a real person wrote it in a hurry, not polished or AI-generated.\n"
        "- Keep it short: 3 to 4 lines maximum.\n"
        "- Include ONE small, subtle typo (a missing apostrophe or a dropped letter). Just one per answer, easy to miss.\n"
        "- NEVER use a dash of any kind. No em dash, no en dash, no hyphen used as punctuation. "
        "Rewrite any phrase that would need one (say 'end to end', not 'end-to-end').\n"
        f"- Be specific and grounded in the facts about {NAME} above. Do not invent employers, dates, or numbers.\n"
        "- If the question turns on a fact you were not given, do NOT decide what it is. Say you are "
        "happy to go into detail, and leave the specifics out.\n"
        "- Do not use any tools. Write only from what is given here.\n\n"
        "Output ONLY a JSON object mapping each question number (as a string) to that "
        'answer\'s text, e.g. {"1": "...", "2": "..."}. No preamble, no code fences, no '
        "explanation — just the JSON object."
    )


def _parse_batch(raw: str, items: list[dict]) -> dict:
    """Map the model's JSON reply back to item ids. Lenient: pulls the outermost {...} out
    of any surrounding chatter/code fences before parsing. Returns {item_id: sanitized}
    for every question it answered; a missing or empty entry is simply left out (the
    caller cold-asks it)."""
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
    for i, it in enumerate(items, 1):
        val = obj.get(str(i))
        if isinstance(val, str):
            clean = sanitize(val)
            if clean:
                out[it.get("id")] = clean
    return out


def draft_answers(items: list[dict], context: dict | None = None) -> dict:
    """Draft every draftable essay in `items` in ONE Claude call (see build_batch_prompt).
    Returns {item_id: text} for those it answered; ids it couldn't cover are omitted so the
    caller falls back to asking cold. One draftable question skips the batch (no benefit).
    On a batch parse failure it falls back to per-item calls so drafts still appear — costlier
    only in that rare case. Never raises: any failure yields {} and the caller cold-asks."""
    if not available():
        return {}
    draftable = [it for it in items if is_draftable(it) and it.get("id")]
    if not draftable:
        return {}
    if len(draftable) == 1:
        one = draft_answer(draftable[0], context)
        return {draftable[0]["id"]: one} if one else {}
    raw = _run_claude(build_batch_prompt(draftable, context))
    parsed = _parse_batch(raw, draftable) if raw else {}
    if parsed:
        return parsed
    # Batch produced nothing usable — degrade to per-item so the drafts aren't simply lost.
    out: dict = {}
    for it in draftable:
        one = draft_answer(it, context)
        if one:
            out[it["id"]] = one
    return out


def sanitize(text: str) -> str:
    """Clean the raw CLI output into something submittable. Strips wrapping quotes and any
    dash the model slipped past the prompt (the no-dash rule is load-bearing for Parth), and
    caps the length so a runaway response can't dump an essay into a Telegram card."""
    t = (text or "").strip()
    # Strip a single layer of wrapping quotes the model sometimes adds.
    if len(t) >= 2 and t[0] in "\"'" and t[-1] == t[0]:
        t = t[1:-1].strip()
    # Neutralize dashes: em/en dashes and a spaced hyphen used as punctuation become commas.
    # Hyphenated words (no surrounding spaces) are left alone — the prompt already avoids them.
    t = t.replace("—", ", ").replace("–", ", ").replace(" - ", ", ")
    # Collapse the double spaces/commas those swaps can create.
    while "  " in t:
        t = t.replace("  ", " ")
    t = t.replace(", ,", ",").replace(" ,", ",")
    # Hard length guard (well past 4 lines) so a misbehaving draft can't flood the card.
    if len(t) > 700:
        t = t[:700].rstrip()
    return t.strip()


def _run_claude(prompt: str) -> str | None:
    """The subprocess boundary — isolated so tests can monkeypatch it. Runs the CLI in a
    throwaway cwd so it never loads the project's CLAUDE.md or explores the repo, and always
    returns a string or None (never raises)."""
    if not CLAUDE_BIN:
        return None
    cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "text"]
    # Pin the model unless explicitly told to use the CLI default. Keeping this a --model
    # flag (not the ANTHROPIC_MODEL env) means it can't leak into any other claude call.
    if DRAFT_MODEL and DRAFT_MODEL.lower() != "default":
        cmd += ["--model", DRAFT_MODEL]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                cmd,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=DRAFT_TIMEOUT, cwd=tmp,
            )
        if proc.returncode != 0:
            print(f"[drafter] claude exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}",
                  file=sys.stderr)
            return None
        return proc.stdout
    except subprocess.TimeoutExpired:
        print(f"[drafter] claude timed out after {DRAFT_TIMEOUT}s", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[drafter] claude call failed: {e}", file=sys.stderr)
        return None


def draft_answer(item: dict, context: dict | None = None, force: bool = False) -> str | None:
    """Draft one answer, or None if drafting is off/unavailable/failed or the item isn't a
    draftable essay. The caller treats None as 'no draft' and asks Parth cold. `force` = he
    asked for this one by hand; see drafts_on_request."""
    if not available() or not (is_draftable(item) or (force and drafts_on_request(item))):
        return None
    raw = _run_claude(build_prompt(item, context))
    if raw is None:
        return None
    out = sanitize(raw)
    return out or None
