"""routes.py — the JSON API. Everything the extension popup and Telegram can do, plus
answer/profile/company editing, exposed under /api.

Run-triggering endpoints reach the extension by dropping a sentinel file the bridge
watches — never by opening a WebSocket (that would supersede Parth's live session).
"""
from __future__ import annotations

import asyncio
import sys

from fastapi import APIRouter, HTTPException

from . import services as svc
from .models import (
    AnswersUpdate,
    BankRow,
    CompanyTier,
    DraftPref,
    ProfileUpdate,
    ResumeOverride,
    StateChange,
)
from .paths import EXTENSION_BUILD, REPO_ROOT, SCRAPER_MODULE, drafter_mod, pending_mod

router = APIRouter()


# ── Dashboard / piles ────────────────────────────────────────────────────────────────

@router.get("/dashboard")
def get_dashboard(since_days: int | None = 60):
    # Counts come from the rows we're returning, so every chip number matches the list.
    rows = svc.dashboard_rows(since_days)
    return {"rows": rows, "counts": svc.count_rows(rows)}


@router.get("/status")
def get_status():
    return {"bridge_alive": svc.bridge_alive(), "counts": svc.counts()}


# ── One job (pending record) ─────────────────────────────────────────────────────────

def _for_display(rec: dict) -> dict:
    """Decorate a pending record for the console drawer. Read-only: nothing is saved.

    Two things the stored record cannot say for itself. `warnings` is a one-shot DOM
    snapshot from the phase-1 fill that nothing ever revisits, so most of its entries are
    disproved by the record's own `filled` list. And a question's label can be the raw
    control id when Greenhouse had not wired its <label for> yet, which asks Parth to
    answer something called "end-month--0".

    The question `id` is deliberately untouched: it is what phase 3 matches a stored
    answer against, so relabelling must never reach it.
    """
    rec["_complete"] = pending_mod.is_complete(rec)
    rec["warnings"] = pending_mod.open_warnings(rec)
    # Also carries a draft for any question the form has already answered — see
    # draft_from_form. It relabels too, so this is the one pass over the questions.
    rec["questions"] = pending_mod.draft_from_form(rec)
    return rec


@router.get("/jobs/{dedup_key}")
def get_job(dedup_key: str):
    rec = pending_mod.load(dedup_key)
    if rec is None:
        raise HTTPException(404, "no pending record for that job")
    return _for_display(rec)


@router.put("/jobs/{dedup_key}/answers")
def update_answers(dedup_key: str, body: AnswersUpdate):
    if pending_mod.load(dedup_key) is None:
        raise HTTPException(404, "no pending record for that job")
    updated = None
    for a in body.answers:
        updated = pending_mod.set_answer(dedup_key, a.id, a.value) or updated
    rec = pending_mod.load(dedup_key)
    if rec is None:
        raise HTTPException(404, "record vanished")
    # Recomputed here too, not just on GET: answering a required question is exactly the
    # event that should retire its warning, and the drawer re-renders from this response.
    return _for_display(rec)


def _resume_display(tag: str) -> str | None:
    """`<outgoing filename> (<tag> track)` — the format the Telegram summary uses.

    None for a tag the profile does not define: pending's override then leaves the old
    line alone, and fill.js refuses the unknown tag and holds the job for review.
    """
    from core import profile as profile_mod

    profile = profile_mod.load_profile() or {}
    track = next((t for t in profile.get("resume_tracks", []) if t.get("tag") == tag), None)
    if track is None:
        return None
    return f"{profile_mod.resume_filename(track, profile)} ({tag} track)"


@router.put("/jobs/{dedup_key}/resume")
def update_resume(dedup_key: str, body: ResumeOverride):
    # Build the display string here when the caller sends none. The drawer sends only the
    # tag, and without this the record kept the previously shown resume line while the
    # extension attached the new track — the same name/file mismatch the Telegram path had.
    display = body.display or _resume_display(body.tag)
    rec = pending_mod.set_resume_override(dedup_key, body.tag, display)
    if rec is None:
        raise HTTPException(404, "no pending record for that job")
    return rec


@router.post("/jobs/{dedup_key}/state")
def change_state(dedup_key: str, body: StateChange):
    res = svc.apply_transition(dedup_key, body.target, body.reason, body.meta)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "transition failed"))
    return res


@router.post("/jobs/{dedup_key}/submit")
def submit_job(dedup_key: str):
    """Per-job submit: approve this job, then drop the flush sentinel so the bridge tells
    the extension to fill + submit the approved pile. The website never submits a form
    itself — only the extension can, on Parth's machine with his live session."""
    rec = pending_mod.load(dedup_key)
    if rec is None:
        raise HTTPException(404, "no pending record for that job")
    if not pending_mod.is_complete(rec):
        raise HTTPException(400, "answer its questions first")
    if not svc.bridge_alive():
        raise HTTPException(409, "bridge isn't running — start bridge.py, then Submit")
    pending_mod.approve(dedup_key)
    svc.drop_flush_trigger()
    approved = pending_mod.counts().get("approved", 0)
    return {"ok": True, "approved_now": approved,
            "note": "flush requested; the extension will send the whole approved pile"}


# ── Run controls ─────────────────────────────────────────────────────────────────────

@router.post("/run/collect")
def run_collect():
    """Start a P1 collection run (fills forms, never submits)."""
    if not svc.bridge_alive():
        raise HTTPException(409, "bridge isn't running — start bridge.py first")
    svc.drop_run_trigger()
    return {"ok": True, "note": "collection run requested"}


@router.post("/run/flush")
def run_flush():
    """Send the approved pile (P3 flush -> the extension submits each approved job)."""
    if not svc.bridge_alive():
        raise HTTPException(409, "bridge isn't running — start bridge.py first")
    approved = pending_mod.counts().get("approved", 0)
    if not approved:
        raise HTTPException(400, "nothing approved to send")
    svc.drop_flush_trigger()
    return {"ok": True, "approved_now": approved, "note": "flush requested"}


@router.post("/run/scraper")
async def run_scraper():
    """Refresh discovery: run the scraper to rebuild out/queue.json. Needs network."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", SCRAPER_MODULE,
        cwd=str(REPO_ROOT),   # -m resolves core.scraper off the repo root
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=240)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, "scraper timed out after 240s")
    tail = (out or b"").decode("utf-8", "replace").splitlines()[-25:]
    return {"ok": proc.returncode == 0, "returncode": proc.returncode,
            "output": "\n".join(tail), "queue_size": len(svc.load_queue())}


@router.post("/run/build-extension")
async def run_build_extension():
    """Restage taxonomy.json / profile.json into the extension after a profile edit."""
    if not EXTENSION_BUILD.exists():
        raise HTTPException(404, "extension/build.py not found")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(EXTENSION_BUILD),
        cwd=str(EXTENSION_BUILD.parent),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    tail = (out or b"").decode("utf-8", "replace").splitlines()[-25:]
    return {"ok": proc.returncode == 0, "returncode": proc.returncode,
            "output": "\n".join(tail),
            "note": "reload the unpacked extension in chrome://extensions to activate"}


# ── Scraped queue ────────────────────────────────────────────────────────────────────

@router.get("/queue")
def get_queue():
    return {"jobs": svc.load_queue()}


# ── Answer bank ──────────────────────────────────────────────────────────────────────

@router.get("/bank")
def get_bank():
    return {"answers": svc.bank_list()}


@router.put("/bank")
def upsert_bank(row: BankRow):
    try:
        return svc.bank_upsert(row.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/bank/{answer_id}")
def delete_bank(answer_id: str):
    if not svc.bank_delete(answer_id):
        raise HTTPException(404, "no answer with that id")
    return {"ok": True}


# ── First-run setup ───────────────────────────────────────────────

@router.get("/setup/status")
def get_setup_status():
    """Checklist for a fresh install: what is done, what is left, what only a human can do.
    Recomputed on every call so the page's "check again" button actually re-checks."""
    return svc.setup_status()


# ── Profile ──────────────────────────────────────────────────────────────────────────

@router.get("/profile")
def get_profile():
    return svc.load_profile()


@router.put("/profile")
def put_profile(body: ProfileUpdate):
    if not isinstance(body.profile, dict) or not body.profile:
        raise HTTPException(400, "profile must be a non-empty object")
    svc.save_profile(body.profile)
    return {"ok": True, "note": "run build-extension to restage into the extension"}


# ── Preferences (Tier-2 Claude drafting toggle) ──────────────────────────────────────

@router.get("/prefs/drafts")
def get_drafts_pref():
    """The Claude-drafting flag, plus whether a `claude` binary is actually available and
    which model draft calls use — so the UI can warn if the flag is on but unusable."""
    return {"enabled": drafter_mod.is_enabled(),
            "available": bool(drafter_mod.CLAUDE_BIN),
            "model": drafter_mod.DRAFT_MODEL or "default"}


@router.put("/prefs/drafts")
def set_drafts_pref(body: DraftPref):
    """Flip the flag. Persisted to the shared prefs file, so the running bridge picks it
    up on its next read — no restart. Submitting is unaffected; this only governs whether
    Claude drafts essay answers the bank hasn't seen."""
    drafter_mod.set_enabled(body.enabled)
    return {"ok": True, "enabled": drafter_mod.is_enabled(),
            "available": bool(drafter_mod.CLAUDE_BIN),
            "model": drafter_mod.DRAFT_MODEL or "default"}


# ── Companies (dream vs pipeline) ────────────────────────────────────────────────────

@router.get("/companies")
def get_companies():
    cos = svc.load_companies()
    return {"companies": cos,
            "counts": {"total": len(cos),
                       "pipeline": sum(1 for c in cos if c["tier"] == "pipeline"),
                       "dream": sum(1 for c in cos if c["tier"] == "dream")}}


@router.put("/companies/tier")
def put_company_tier(body: CompanyTier):
    try:
        ok = svc.set_company_tier(body.name, body.tier)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, f"couldn't find '{body.name}' to update")
    return {"ok": True}
