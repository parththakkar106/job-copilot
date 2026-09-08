#!/usr/bin/env python3
"""pending.py — the batch's memory.

Why this exists: Parth answers questions whenever he has 5-10 minutes free, then walks
away for an hour. The original ask flow held a WebSocket open, took a lock, and blocked
on an in-memory queue with a 30-minute timeout — none of which survives an hour-long
gap, a bridge restart, or an extension reload. So the batch is stored instead, and
Telegram becomes a stateless view over it. Nothing blocks; nothing times out.

Records live in the store's `pending` table, one row per dedup_key. Pass `base` to work
against a different store; tests pass a temp directory and get a SQLite file in it.

Status flow:
  awaiting   phase 1 wrote it; some questions may still need answers
  approved   Parth tapped ✅; phase 3 fills + submits it unattended
  held       filled but couldn't submit — needs Parth (out of the auto-flush pile)
  skipped    deferred (dashboard, or ✋ mid-question on Telegram) — kept visible with its
             answers, but out of flush + re-scrape until it's picked again
  (deleted)  submitted / rejected / (Telegram summary ✋) skipped — see below

Terminal states delete the record, because the applied log is the permanent one and a
pending record is only ever work-in-flight:
  submitted -> caller appends to the applied log (status "applied"), then delete()
  rejected  -> caller appends to the applied log (status "rejected"), then delete()
  skipped   -> delete() and write nothing, so tomorrow's queue serves it again

Writing the applied record is deliberately the CALLER's job, not ours: this module stays
pure storage so it can be tested without touching the real applied log.

`answer` is tri-state and the distinction matters:
  None  not yet resolved — still needs Parth
  ""    resolved as deliberately blank (he tapped "leave blank" on an optional)
  str   his answer
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.store import resolve  # noqa: E402

AWAITING = "awaiting"
APPROVED = "approved"
# Approved, but phase 3 tried to submit it MAX_FLUSH_ATTEMPTS times without a
# confirmation (a swallowed submit, a verification gate, a form that needs Parth's
# hand). Held jobs drop out of the auto-flush pile so the idle-watcher stops re-firing
# them every 5 minutes forever; Parth finishes them with the panel or /retry re-queues.
HELD = "held"
# Deferred from the dashboard, or by ✋ on a question in Telegram: Parth doesn't want this
# one now, but wants it kept VISIBLE with whatever he has already answered (unlike the ✋ on
# the Telegram SUMMARY, which deletes the record so tomorrow's scrape re-serves it — fine
# there, because by then there is a full set of answers and nothing half-done to lose).
# A skipped record drops out of the auto-flush pile (that pile only pulls APPROVED) and is
# not re-served by the daily queue (handle_queue excludes every pending key). He un-skips
# it from the dashboard, or by picking it out of /jobs, when he's ready.
SKIPPED = "skipped"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save(rec: dict, base=None) -> dict:
    """Write one pending record. Returns the record as stored.

    The atomic temp-file-and-rename this replaced existed because a half-written JSON file
    would strand a job with no way to notice. A row write is atomic already.
    """
    rec["updated"] = _now()
    resolve(base).put_pending(rec)
    return rec


def load(dedup_key: str, base=None) -> dict | None:
    return resolve(base).get_pending(dedup_key)


def all_jobs(status: str | None = None, base=None) -> list[dict]:
    """Every pending record, oldest first. Oldest-first preserves the queue's
    freshest-first ordering: phase 1 writes them in the order triage served them."""
    return resolve(base).all_pending(status)


def delete(dedup_key: str, base=None) -> bool:
    return resolve(base).delete_pending(dedup_key)


def archive(dedup_key: str, base=None) -> bool:
    """Retire a submitted job's record but KEEP it.

    The full record — every answer given, the resume pick, the approved summary — stays
    stored for later reuse and the future answer-bank sync, out of the active batch and out
    of the /status counts. Returns True if a record was retired.
    """
    return resolve(base).archive_pending(dedup_key)


def restore(dedup_key: str, base=None) -> dict | None:
    """Return an archived record to the active batch, awaiting review again.

    The inverse of archive(). Backs the dashboard's un-submit and un-block: without it the
    role's answers stayed archived while the active batch had nothing, so the console showed
    no row at all until the next discovery run rediscovered the job from scratch. Returns
    the restored record, or None if nothing was archived under that key.
    """
    if not resolve(base).restore_pending(dedup_key):
        return None
    return reopen(dedup_key, base)


def load_archived(dedup_key: str, base=None) -> dict | None:
    """Read back a submitted job's kept record (answers included), or None if absent."""
    return resolve(base).get_pending(dedup_key, archived=True)


def put(job: dict, questions: list[dict], filled: list[dict],
        base=None, warnings: list | None = None) -> dict | None:
    """Record what phase 1 found for one job. Returns the new record, or None if this
    job is already pending — never clobbers a record Parth has partly answered, which
    is what a re-run of phase 1 would otherwise do.

    warnings = required fields the DOM said were still empty after the phase-1 fill
    (beyond the questions themselves) — shown on the ✅ summary so Parth isn't
    approving blind. ats/ats_slug/ats_job_id ride along so phase 3 can rebuild a
    fillable URL for jobs whose queue URL points at a custom career domain."""
    key = job.get("dedup_key")
    if not key:
        raise ValueError("job needs a dedup_key")
    if load(key, base) is not None:
        return None
    rec = {
        "dedup_key": key,
        "company": job.get("company", ""),
        "title": job.get("title", ""),
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "ats": job.get("ats", ""),
        "ats_slug": job.get("ats_slug", ""),
        "ats_job_id": job.get("ats_job_id", ""),
        "compensation": job.get("compensation", ""),
        "employment_type": job.get("employment_type", ""),
        "resume": job.get("resume", ""),
        "filled": filled,
        "questions": [{**q, "answer": q.get("answer")} for q in questions],
        "warnings": list(warnings or []),
        "status": AWAITING,
        "created": _now(),
    }
    save(rec, base)
    return rec


def set_answer(dedup_key: str, qid: str, value: str | None,
               base=None) -> dict | None:
    """Store one answer. Returns the updated record, or None if the job or question
    is unknown (e.g. a stale Telegram button tapped after the job was skipped).

    Passing None resets the question to unresolved — used by /edit to re-open an
    already-answered question so it gets served again for a fresh reply."""
    rec = load(dedup_key, base)
    if rec is None:
        return None
    for q in rec["questions"]:
        if q.get("id") == qid:
            q["answer"] = value
            save(rec, base)
            return rec
    return None


def set_resume_override(dedup_key: str, tag: str, display: str | None = None,
                        base=None) -> dict | None:
    """Pin a specific resume track for this one job, overriding fill.js's best-of-4 pick.

    `tag` is the track id (quant/ds/tech/ai) fill.js will attach; `display` is the
    human string shown on the Telegram summary (rec["resume"]). Returns the updated
    record, or None if the job is gone. Resume never participates in the drift guard
    (it lives only here, not in `filled`), so overriding it can't wrongly block a submit.
    """
    rec = load(dedup_key, base)
    if rec is None:
        return None
    rec["resume_override"] = tag
    if display is not None:
        rec["resume"] = display
    save(rec, base)
    return rec


def unanswered(rec: dict) -> list[dict]:
    return [q for q in rec.get("questions", []) if q.get("answer") is None]


def _warn_key(label) -> str:
    """A label reduced to what identifies the FIELD, for comparing across sources.

    The asterisk matters: warnings are the union of two lists that disagree about it.
    `emptyRequiredLabels` strips the required marker as it collects, the escalated
    labels on report.needsInput/flagged keep it, and the `new Set()` that unions them
    compares raw strings — so every escalated required field was listed twice, once as
    "What is your military status?*" and once without. Normalising here collapses the
    twins for records already on disk; fill.js stops making them going forward.
    """
    return _warn_text(label).casefold()


EDU_DATE_ID = re.compile(r"^(start|end)-(month|year)--(\d+)$", re.I)


def readable_label(label) -> str:
    """A label that is really just the control's id, turned into something answerable.

    The mirror of readableLabel() in extension/fill.js -- keep the two in step. That one
    fixes new fills at the moment a field escalates; this one fixes the 29 questions across
    16 records that were already written to disk before it existed, at the moment the
    console reads them. Both are purely presentational: the question's `id` is what phase 3
    matches an answer against, and neither touches it.

    The cause is Greenhouse's education block being scanned before the page has wired its
    <label for> elements, so labelFor() falls through to its last resort of `name || id`.
    Every id seen on disk is from that block -- school--0, degree--0, discipline--0,
    end-month--0.
    """
    raw = str(label if label is not None else "").strip()
    # Only machine ids qualify: one token, no spaces, and a separator. A real question has
    # spaces, so no human-written label can reach the rewrites below.
    if not raw or re.search(r"\s", raw) or not re.search(r"[-_]", raw):
        return raw

    m = EDU_DATE_ID.match(raw)
    if m:
        which, part, row = m.group(1), m.group(2), m.group(3)
        when = "Start" if which.lower() == "start" else "End"
        unit = "year" if part.lower() == "year" else "month"
        if row == "0":
            return f"{when} date {unit}"
        return f"{when} date {unit} (education {int(row) + 1})"

    m = re.match(r"^(.*?)--(\d+)$", raw)
    stem = re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", m.group(1) if m else raw)).strip()
    if not stem:
        return raw
    nice = stem[0].upper() + stem[1:]
    if m and m.group(2) != "0":
        return f"{nice} (row {int(m.group(2)) + 1})"
    return nice


def _warn_text(label) -> str:
    """The label as it should READ: no required marker, no doubled whitespace.

    The marker is a DOM artifact of how the ATS renders "this one is mandatory", not part
    of the question, and the warning box already says every entry is required in its own
    heading. Stripping it here also means the surviving twin renders the same whichever
    of the two source lists happened to be seen first.
    """
    stripped = re.sub(r"\s+", " ", str(label or "").replace("✱", "").replace("*", "")).strip()
    return readable_label(stripped)


def open_warnings(rec: dict) -> list[str]:
    """The record's warnings, minus the ones its own contents disprove.

    `warnings` is a DOM snapshot taken once, during the phase-1 fill: the required
    fields that still read empty at that moment. It is never revisited, so it goes stale
    two ways. A field the fill did commit can be read back as empty on a custom widget
    (Greenhouse's React comboboxes report an empty `el.value` even when a value is
    showing) and stays on the list for good; and answering a question later never
    removes anything either.

    Measured on a live record 2026-08-24: 16 warnings, 15 of which the SAME record
    listed under `filled` with a real value. A warning box where 15 of 16 entries are
    false is worse than no warning box - it trains you to close it without reading, on
    exactly the screen whose job is to stop you approving something half-filled.

    So a warning is dropped when the record itself shows that field carrying a
    non-empty value. Deliberately not dropped when the value is blank: an answered-blank
    optional really does leave the form field empty, which is the case the warning is
    for. Nothing is written back - the raw snapshot stays on disk as the audit trail of
    what phase 1 actually saw.
    """
    have = {_warn_key(f.get("label")) for f in (rec.get("filled") or [])
            if str(f.get("value") or "").strip()}
    have |= {_warn_key(q.get("label")) for q in (rec.get("questions") or [])
             if str(q.get("answer") or "").strip()}
    out, seen = [], set()
    for w in (rec.get("warnings") or []):
        text = _warn_text(w)
        k = _warn_key(w)
        if not k or k in have or k in seen:
            continue
        seen.add(k)
        out.append(text)
    return out


def form_values(rec: dict) -> dict:
    """What the record says is already sitting in each field, keyed for comparison.

    Same key as the warnings use, so a field recorded under its raw control id matches the
    same field recorded under its hydrated label.
    """
    out = {}
    for f in (rec.get("filled") or []):
        val = str(f.get("value") or "").strip()
        k = _warn_key(f.get("label"))
        if k and val and k not in out:
            out[k] = val
    return out


def draft_from_form(rec: dict) -> list[dict]:
    """The record's questions, with a draft filled in from the form where one exists.

    Greenhouse's resume parser answers the education block before the extension ever scans
    it, and none of those fields match a taxonomy rule, so they escalated as "unrecognized
    required field" - and Parth was asked to type an answer into a box that visibly already
    had one. Measured on disk 2026-08-24: 115 open questions across 31 records that the
    SAME record lists under `filled` with a value.

    Deliberately a draft and not an answer. Resolving them outright would be a one-line
    change and it would also decide, for 31 records at once, that they may now be approved
    and submitted unattended - is_complete() gates approve(), and a value nobody has looked
    at is not the same as a value Parth accepted. So the drawer offers it on the "use draft"
    chip he already has, one click, with the gate left where it is. fill.js no longer
    escalates these at all, so this is for the backlog rather than for new records.
    """
    out = []
    have = form_values(rec)
    for q in (rec.get("questions") or []):
        q = {**q, "label": readable_label(q.get("label"))}
        if q.get("answer") is None and not q.get("draft"):
            v = have.get(_warn_key(q.get("label")))
            if v:
                q["draft"] = v
                q["_on_form"] = True
        out.append(q)
    return out


def is_complete(rec: dict) -> bool:
    """True when every question has been resolved. Optional questions still need an
    explicit "leave blank" — silence is not consent, and an unanswered optional is
    indistinguishable from one Parth never saw."""
    return not unanswered(rec)


def approve(dedup_key: str, base=None) -> dict | None:
    """Mark a job ready for phase 3. Refuses while questions are outstanding, so a
    stale ✅ tap can't send a half-answered form to a real employer."""
    rec = load(dedup_key, base)
    if rec is None or not is_complete(rec):
        return None
    rec["status"] = APPROVED
    rec.pop("focus_at", None)   # it has left the answering pile; the pin has nothing to hold
    save(rec, base)
    return rec


def hold(dedup_key: str, base=None,
         reason: str | None = None) -> dict | None:
    """Park an approved job that phase 3 filled but couldn't submit — a form that came out
    different from what Parth approved, a required field only he can fill, an email
    verification gate, or a click that never confirmed. Keeps the record (and his answers)
    on disk but out of the auto-flush pile, so the idle-watcher stops re-driving a form
    that's already been filled. `reason` is the short human note shown in the held pile.
    Returns the updated record, or None if the job is gone."""
    rec = load(dedup_key, base)
    if rec is None:
        return None
    rec["status"] = HELD
    rec.pop("focus_at", None)
    if reason:
        rec["hold_reason"] = reason
    save(rec, base)
    return rec


def unhold_all(base=None) -> list[dict]:
    """Re-queue every held job: back to APPROVED, attempt counter reset. Drives /retry.
    Returns the list of jobs that were re-queued."""
    out = []
    for rec in all_jobs(HELD, base):
        rec["status"] = APPROVED
        rec["flush_attempts"] = 0
        rec.pop("hold_reason", None)
        save(rec, base)
        out.append(rec)
    return out


def skip(dedup_key: str, base=None) -> dict | None:
    """Defer a job from the dashboard: keep the record (and any answers) on disk but out of
    the auto-flush pile and out of re-scrape serving, while staying visible in the 'in queue'
    bucket. Returns the updated record, or None if the job is gone."""
    rec = load(dedup_key, base)
    if rec is None:
        return None
    rec["status"] = SKIPPED
    rec.pop("hold_reason", None)
    rec.pop("focus_at", None)   # deferring it is the opposite of pinning it
    save(rec, base)
    return rec


def reopen(dedup_key: str, base=None) -> dict | None:
    """Send a job (skipped/approved/held) back to the awaiting pile so it shows up for
    review again — its answers are kept. Clears the flush-attempt counter and hold reason.
    Returns the updated record, or None if the job is gone."""
    rec = load(dedup_key, base)
    if rec is None:
        return None
    rec["status"] = AWAITING
    rec["flush_attempts"] = 0
    rec.pop("hold_reason", None)
    save(rec, base)
    return rec


def clear_focus(base=None) -> None:
    """Drop the /jobs pin from every record. Only one job is ever pinned: the pin means
    "serve THIS one next", and two of those is a contradiction."""
    for rec in all_jobs(None, base):
        if rec.pop("focus_at", None) is not None:
            save(rec, base)


def focus(dedup_key: str, base=None) -> dict | None:
    """Pin one job to the front of the /next queue — what tapping a row in /jobs does.

    The batch has deliberately no cursor: "the current job" is derived from disk every
    time, which is what makes an hour-long gap and a bridge restart survivable. A pin has
    to survive the same gaps, so it is not a variable in this process — it is a `focus_at`
    stamp ON the record, read back by serving_order(). Picking a job from the phone and
    then answering it an hour later after a restart lands on the same job.

    Picking a deferred or held job implies wanting to work on it, so it comes back to the
    awaiting pile first (reopen keeps its answers). An APPROVED job is not pinnable: it is
    queued for an unattended submit, and re-opening it under a flush in flight is the one
    race this store exists to avoid — the caller checks status and says so.

    Returns the pinned record, or None if the job is gone.
    """
    rec = load(dedup_key, base)
    if rec is None:
        return None
    clear_focus(base)
    if rec.get("status") != AWAITING:
        rec = reopen(dedup_key, base) or rec
    rec["focus_at"] = _now()
    save(rec, base)
    return rec


def answered_count(rec: dict) -> int:
    """How many of this job's questions are resolved — the 2 in "2/4"."""
    return len([q for q in (rec.get("questions") or []) if q.get("answer") is not None])


def progress(rec: dict) -> str:
    """The at-a-glance "2/4" for the /jobs list. "no questions" reads better than 0/0:
    a job phase 1 found nothing to ask about is ready to approve, not unstarted."""
    total = len(rec.get("questions") or [])
    if not total:
        return "no questions"
    return f"{answered_count(rec)}/{total}"


# /next serving tiers, lowest served first.
TIER_IN_PROGRESS = 0    # he started answering this one and did not finish
TIER_READY = 1          # nothing left to answer, one ✅ from approved
TIER_FRESH = 2          # never touched


def next_tier(rec: dict) -> int:
    """Which serving tier a record is in. See serving_order."""
    qs = rec.get("questions") or []
    answered = [q for q in qs if q.get("answer") is not None]
    if answered and len(answered) < len(qs):
        return TIER_IN_PROGRESS
    # Covers both "every question answered" and "phase 1 found no questions at all".
    if not unanswered(rec):
        return TIER_READY
    return TIER_FRESH


def serving_order(base=None) -> list[dict]:
    """Every awaiting job in the order /next should hand them over.

    Was: oldest `created` first, full stop. That was written when the batch was one
    collect run deep, where oldest-first IS freshest-first (phase 1 writes records in the
    order triage served them, and triage serves freshest postings first). It does not
    survive a backlog. Measured 2026-08-24: 178 awaiting records spanning 24 collect days,
    so /next opened on a role collected on 2026-07-22 — 33 days stale, and very likely
    closed — and would have kept doing so for another hundred taps.

    A job picked by hand from /jobs is pinned and comes before all of it. Then three tiers:
      1. Started and unfinished. Half-answered is the worst state for a record to sit in,
         and he already paid the cost of reading the role.
      2. Complete but unapproved. One ✅ turns it into a real application; leaving 22 of
         them parked behind 155 unread roles is the batch's biggest cheap win.
      3. Everything untouched, newest collect run first.

    Within tier 3 the order is by RUN, not by record. Reversing the records outright would
    put the last-written record of the newest run first, and within a run last-written is
    the OLDEST posting — triage serves freshest first, so a plain reverse quietly inverts
    the ordering that already exists inside each run. Bucketing by collect day and
    reversing the buckets keeps both: newest run first, freshest posting first inside it.
    """
    awaiting = all_jobs(AWAITING, base)          # oldest first

    # Tier 0: the one job Parth picked from /jobs, if any. An explicit pick outranks every
    # heuristic below — the tiers are a guess at what he'd want next, and this is him
    # saying it. Only ever one (focus() clears the others), but sort defensively so a
    # hand-edited record can't produce an order that depends on the glob.
    pinned = [r for r in awaiting if r.get("focus_at")]
    pinned.sort(key=lambda r: r.get("focus_at") or "", reverse=True)

    tiers = {TIER_IN_PROGRESS: [], TIER_READY: [], TIER_FRESH: []}
    for rec in awaiting:
        if rec.get("focus_at"):
            continue
        tiers[next_tier(rec)].append(rec)

    by_run: dict[str, list[dict]] = {}
    for rec in tiers[TIER_FRESH]:
        by_run.setdefault((rec.get("created") or "")[:10], []).append(rec)
    fresh = [rec for day in sorted(by_run, reverse=True) for rec in by_run[day]]

    return pinned + tiers[TIER_IN_PROGRESS] + tiers[TIER_READY] + fresh


def next_job(base=None) -> dict | None:
    """The next job wanting Parth's attention — see serving_order for the ordering.
    A zero-question job still counts: it's complete but unapproved, so it needs his ✅."""
    order = serving_order(base)
    return order[0] if order else None


def next_question(rec: dict) -> dict | None:
    qs = unanswered(rec)
    return qs[0] if qs else None


def counts(base=None) -> dict:
    """Batch state for /status."""
    jobs = all_jobs(None, base)
    awaiting = [r for r in jobs if r.get("status") == AWAITING]
    return {
        "jobs": len(jobs),
        "awaiting": len(awaiting),
        "approved": len([r for r in jobs if r.get("status") == APPROVED]),
        "held": len([r for r in jobs if r.get("status") == HELD]),
        "skipped": len([r for r in jobs if r.get("status") == SKIPPED]),
        "ready": len([r for r in awaiting if is_complete(r)]),
        "questions_left": sum(len(unanswered(r)) for r in awaiting),
    }
