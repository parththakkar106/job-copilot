"""services.py — the domain layer the routers call.

Pure-ish functions over the shared on-disk state. The pile view + transitions mirror the
bridge's dashboard logic (bridge.py `_dashboard_rows` / `_apply_transition`, spec in
dashboard_spec.md) so the website and the extension popup agree on every bucket; they're
re-implemented here (not imported) because bridge.py pulls in websockets/requests and
boots a server on import. Everything routes through pending_mod / triage_mod / bank_mod,
which are the same modules the live pipeline uses.
"""
from __future__ import annotations

import json
import re
import socket
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from .paths import (
    ANSWER_BANK_PATH,
    COMPANIES_PATH,
    DREAM_PATH,
    FLUSH_TRIGGER,
    PROFILE_PATH,
    QUEUE_PATH,
    RUN_TRIGGER,
    bank_mod,
    pending_mod,
    triage_mod,
)

# ── Companies (dream vs pipeline) ────────────────────────────────────────────────────

def load_companies() -> list[dict]:
    """Every company from companies.yaml with its tier (pipeline | dream)."""
    if not COMPANIES_PATH.exists():
        return []
    data = yaml.safe_load(COMPANIES_PATH.read_text(encoding="utf-8")) or {}
    out = []
    for c in data.get("companies", []) or []:
        out.append({
            "name": c.get("name", ""),
            "ats": c.get("ats", ""),
            "slug": c.get("slug", ""),
            "tier": c.get("tier", "pipeline"),
        })
    return out


def _tier_map() -> dict[str, str]:
    """company-name (casefold) -> tier, for tagging roles that only carry a company name."""
    return {c["name"].casefold(): c["tier"] for c in load_companies() if c["name"]}


def set_company_tier(name: str, tier: str) -> bool:
    """Flip one company's tier by editing its line in place, so the file's comments and the
    commented-out off-ATS block at the bottom survive (yaml.safe_dump would wipe both).

    companies.yaml stores each company as an inline flow map on its own line, e.g.
      - {name: Abridge, ats: ashby, slug: abridge, tier: pipeline}
    We find the line whose name matches and swap only its `tier:` value.
    """
    if tier not in ("pipeline", "dream"):
        raise ValueError("tier must be 'pipeline' or 'dream'")
    if not COMPANIES_PATH.exists():
        return False
    lines = COMPANIES_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    name_pat = re.compile(r"name:\s*" + re.escape(name) + r"\s*[,}]")
    tier_pat = re.compile(r"(tier:\s*)(pipeline|dream)")
    changed = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue  # never touch the commented-out off-ATS targets
        if name_pat.search(line) and tier_pat.search(line):
            lines[i] = tier_pat.sub(lambda m: m.group(1) + tier, line, count=1)
            changed = True
            break
    if changed:
        COMPANIES_PATH.write_text("".join(lines), encoding="utf-8")
    return changed


# ── Dashboard (merged pile view) ─────────────────────────────────────────────────────

def _apply_url(rec: dict) -> str:
    """Where to actually open the application. Greenhouse forms are often embedded on the
    company's own careers site where the form is a cross-origin iframe — the extension panel
    can't float and its dropdown clicks misfire. The legacy embed endpoint serves the same
    form standalone, so route Greenhouse roles there; others keep their stored URL."""
    if rec.get("ats") == "greenhouse" and rec.get("ats_job_id"):
        return f"https://boards.greenhouse.io/embed/job_app?token={rec['ats_job_id']}"
    return rec.get("url", "")


def _pending_row(rec: dict, tiers: dict[str, str]) -> dict:
    st = rec.get("status")
    n = len(pending_mod.unanswered(rec))
    if st == pending_mod.APPROVED:
        bucket, tag = "filled", "ready to submit"
    elif st == pending_mod.HELD:
        bucket, tag = "filled", rec.get("hold_reason") or "needs you"
    elif st == pending_mod.SKIPPED:
        bucket, tag = "in_queue", "skipped"
    else:  # awaiting
        bucket = "in_queue"
        tag = f"{n} question{'' if n == 1 else 's'}" if n else "ready to approve"
    return {
        "dedup_key": rec.get("dedup_key"),
        "company": rec.get("company", ""),
        "title": rec.get("title", ""),
        "location": rec.get("location", ""),
        "url": rec.get("url", ""),
        "apply_url": _apply_url(rec),
        "bucket": bucket,
        "tag": tag,
        "status": st,
        "tier": tiers.get((rec.get("company") or "").casefold(), "pipeline"),
        "date": (rec.get("created") or "")[:10],
        "unanswered": n,
        "resume": rec.get("resume", ""),
    }


def _queue_rows(seen_keys: set[str]) -> list[dict]:
    """Scraped roles the extension hasn't collected yet, as a 'discovered' pile.

    These are genuinely still-to-apply but had no pile of their own, so the dashboard used
    to hide them entirely: in_queue rows come from pending records, which only exist once a
    P1 collection run has opened and filled the form. Excludes anything another pile already
    surfaced (a collected role shows as in_queue, a decided one as submitted/blocked)."""
    out = []
    for j in load_queue():
        key = j.get("dedup_key")
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        out.append({
            "dedup_key": key,
            "company": j.get("company", ""),
            "title": j.get("title", ""),
            "location": j.get("location", ""),
            "url": j.get("url", ""),
            "apply_url": _apply_url(j),
            "bucket": "discovered",
            "tag": "not collected yet",
            "status": "discovered",
            "tier": j.get("tier") or "pipeline",
            "date": (j.get("posted") or "")[:10],
            "unanswered": 0,
            "resume": "",
        })
    return out


def _dream_rows(seen_keys: set[str]) -> list[dict]:
    """Discovered dream roles not yet acted on, as a manual-only 'dream' pile. Excludes any
    dedup_key already surfaced by another pile (pending/applied) so a manually-applied or
    collected dream role shows in that pile instead, never twice."""
    out = []
    for j in load_dream():
        key = j.get("dedup_key")
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        out.append({
            "dedup_key": key,
            "company": j.get("company", ""),
            "title": j.get("title", ""),
            "location": j.get("location", ""),
            "url": j.get("url", ""),
            "bucket": "dream",
            "tag": "apply by hand",
            "status": "dream",
            "tier": "dream",
            "date": (j.get("posted") or "")[:10],
            "unanswered": 0,
            "resume": "",
        })
    return out


def dashboard_rows(since_days: int | None = 60) -> list[dict]:
    """Every pile merged into dated, bucketed rows, newest first. Pending shown in full;
    applied/blocked (only grows) is windowed to since_days (None = all). Scraped-but-not-yet
    collected roles ride in as 'discovered' and discovered dream roles as a manual-only
    'dream' pile, both minus any key another pile already surfaced."""
    tiers = _tier_map()
    rows = [_pending_row(r, tiers) for r in pending_mod.all_jobs()]
    cutoff = (date.today() - timedelta(days=since_days)).isoformat() if since_days else None
    for r in triage_mod.load_applied(triage_mod.APPLIED_PATH):
        when = (r.get("applied") or "")[:10]
        if cutoff and when and when < cutoff:
            continue
        blocked = (r.get("status") == "rejected")
        rows.append({
            "dedup_key": r.get("dedup_key"),
            "company": r.get("company", ""),
            "title": r.get("title", ""),
            "location": r.get("location", ""),
            "url": r.get("url", ""),
            "bucket": "blocked" if blocked else "submitted",
            "tag": "blocked" if blocked else "applied",
            "status": r.get("status") or "applied",
            "tier": tiers.get((r.get("company") or "").casefold(), "pipeline"),
            "date": when,
            "unanswered": 0,
            "resume": "",
        })
    seen = {r.get("dedup_key") for r in rows if r.get("dedup_key")}
    rows.extend(_queue_rows(seen))
    rows.extend(_dream_rows(seen))
    rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    return rows


# Buckets whose roles are still waiting on Parth to apply — the console's default view.
TO_APPLY = ("discovered", "in_queue", "filled", "dream")


def count_rows(rows: list[dict]) -> dict:
    """Bucket + tier counts over a given row set. Tier counts live under tier_* keys so the
    'dream' bucket count and the 'dream' tier count don't collide on one key.

    Takes rows rather than re-deriving them so the chip counts always describe exactly what
    the view is showing — computing counts over all history while the rows were windowed to
    60 days made the numbers disagree with the list underneath them."""
    out = {"all": len(rows), "discovered": 0, "in_queue": 0, "filled": 0, "submitted": 0,
           "blocked": 0, "dream": 0, "tier_pipeline": 0, "tier_dream": 0}
    for r in rows:
        out[r["bucket"]] = out.get(r["bucket"], 0) + 1
        out["tier_" + r["tier"]] = out.get("tier_" + r["tier"], 0) + 1
    out["to_apply"] = sum(out.get(b, 0) for b in TO_APPLY)
    out["pending"] = pending_mod.counts()
    return out


def counts(since_days: int | None = None) -> dict:
    """Counts over the whole history by default (what the topbar wants)."""
    return count_rows(dashboard_rows(since_days=since_days))


# ── Transitions (move a job between piles) ───────────────────────────────────────────

def apply_transition(key: str, target: str, reason: str | None = None,
                     meta: dict | None = None) -> dict:
    """Move one job to the target bucket. Mirrors bridge.py `_apply_transition`
    (dashboard_spec.md §2). append_applied + archive are idempotent, so a race with an
    in-flight flush degrades safely.

    Every move is reversible, because the console lets Parth make them with one keypress:
    leaving the queue archives the record instead of deleting it, and coming back revives
    that archive. So each queue-side target below first undoes whatever the role's last
    decision was."""
    meta = meta or {}
    if not key:
        return {"ok": False, "error": "no dedup_key"}
    pend = pending_mod.load(key)
    in_applied = key in triage_mod.applied_keys(triage_mod.APPLIED_PATH)

    def revive() -> dict | None:
        """Undo a prior submitted/blocked decision so a queue-side target can take effect:
        drop the applied-log row and pull the archived record back into the active batch."""
        nonlocal pend
        if in_applied:
            triage_mod.remove_applied(key, triage_mod.APPLIED_PATH)
        if pend is None:
            pend = pending_mod.restore(key)
        return pend

    if target in ("submitted", "blocked"):
        status = "applied" if target == "submitted" else "rejected"
        if in_applied:
            # Already decided — this is a CORRECTION (Submitted <-> Blocked), so edit the
            # row in place. append_applied would no-op here and report success, which is
            # exactly the "the move did nothing but said it worked" bug.
            triage_mod.set_applied_status(key, status, triage_mod.APPLIED_PATH)
        else:
            rec = {**meta, **(pend or {}), "dedup_key": key, "status": status}
            triage_mod.append_applied(rec, triage_mod.APPLIED_PATH)
        if pend:
            # archive, not delete: the record holds every answer Parth typed, and these
            # moves are one keypress away in the console. Deleting made Block a permanent
            # data loss on a mis-tap and left nothing for un-block to restore.
            pending_mod.archive(key)
        return {"ok": True, "bucket": target}

    if target == "filled":
        revive()
        r = pending_mod.approve(key)  # guarded: refuses if incomplete / no record
        if r:
            return {"ok": True, "bucket": "filled"}
        return {"ok": False, "error": "answer its questions first (or no filled record exists)"}

    if target == "in_queue":
        moved = bool(revive())
        if pend:
            moved = bool(pending_mod.reopen(key)) or moved
        elif in_applied:
            moved = True  # the applied-log row was dropped even though no record came back
        if not moved:
            return {"ok": False, "error": "nothing to move back for this role"}
        if not pend:
            # No record to reopen and nothing archived: all we did was drop it from
            # applied.json so it stops being filtered out. If it's still in the scraped
            # queue it reappears immediately as 'discovered'; otherwise there's nothing for
            # the console to show until discovery finds it again — say which, rather than
            # leaving Parth staring at a row that just disappeared.
            still_listed = any(j.get("dedup_key") == key
                               for j in (load_queue() + load_dream()))
            if still_listed:
                return {"ok": True, "bucket": "discovered"}
            return {"ok": True, "bucket": "in_queue", "vanishes": True,
                    "note": "cleared — it comes back on the next discovery run"}
        return {"ok": True, "bucket": "in_queue"}

    if target == "skipped":
        if revive() and pending_mod.skip(key):
            return {"ok": True, "bucket": "in_queue"}
        return {"ok": False, "error": "only a collected role can be skipped"}

    if target == "held":
        revive()
        r = pending_mod.hold(key, reason=reason)
        if r:
            return {"ok": True, "bucket": "filled"}
        return {"ok": False, "error": "no record to hold"}

    return {"ok": False, "error": f"unknown target: {target}"}


# ── Run triggers (reach the extension via sentinel files, never a WebSocket) ─────────

def drop_run_trigger() -> None:
    """Ask the bridge to start a P1 collection run (fill, never submit)."""
    RUN_TRIGGER.parent.mkdir(parents=True, exist_ok=True)
    RUN_TRIGGER.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")


def drop_flush_trigger() -> None:
    """Ask the bridge to flush (submit) the approved pile."""
    FLUSH_TRIGGER.parent.mkdir(parents=True, exist_ok=True)
    FLUSH_TRIGGER.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")


def bridge_alive(timeout: float = 0.4) -> bool:
    """True if something is LISTENING on the bridge's WS port. A bare TCP connect that we
    close immediately does NOT complete a WebSocket handshake, so it never invokes the
    bridge's ws_handler and can't supersede Parth's live extension session."""
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=timeout):
            return True
    except OSError:
        return False


# ── Profile ──────────────────────────────────────────────────────────────────────────

def load_profile() -> dict:
    if not PROFILE_PATH.exists():
        return {}
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def save_profile(obj: dict) -> None:
    """Write profile.json back, pretty-printed to match the hand-edited file. Caller has
    already validated it's a JSON object."""
    PROFILE_PATH.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")


# ── Answer bank ──────────────────────────────────────────────────────────────────────

_VALID_STATUS = ("active", "draft", "stale")


def bank_list() -> list[dict]:
    return bank_mod.load()


def bank_upsert(row: dict) -> dict:
    """Add or update one answer. If `id` matches an existing row we update it; otherwise
    we add a new one (deriving an id from key+company like bank.cmd_add does)."""
    rows = bank_mod.load()
    today = date.today().isoformat()
    key = (row.get("key") or "").strip()
    if not key:
        raise ValueError("key is required")
    scope = row.get("scope") or "common"
    if scope not in ("common", "company"):
        raise ValueError("scope must be 'common' or 'company'")
    company = (row.get("company") or "").strip() or None
    if scope == "company" and not company:
        raise ValueError("company scope requires a company")
    status = row.get("status") or "active"
    if status not in _VALID_STATUS:
        raise ValueError(f"status must be one of {_VALID_STATUS}")
    answer = row.get("answer") or ""
    tags = row.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    _id = (row.get("id") or "").strip() or bank_mod.slug(key, company if scope == "company" else "")
    existing = bank_mod.find(rows, _id)
    if existing:
        existing.update({"key": key, "scope": scope,
                         "company": company if scope == "company" else None,
                         "answer": answer, "tags": tags, "status": status, "updated": today})
        result = existing
    else:
        result = {"id": _id, "key": key, "scope": scope,
                  "company": company if scope == "company" else None,
                  "answer": answer, "tags": tags, "status": status,
                  "created": today, "updated": today, "last_used": None, "uses": 0}
        rows.append(result)
    bank_mod.save(rows)
    return result


def bank_delete(_id: str) -> bool:
    rows = bank_mod.load()
    r = bank_mod.find(rows, _id)
    if not r:
        return False
    rows.remove(r)
    bank_mod.save(rows)
    return True


# ── Scraped queue ────────────────────────────────────────────────────────────────────

def load_queue() -> list[dict]:
    if not QUEUE_PATH.exists():
        return []
    try:
        return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def load_dream() -> list[dict]:
    """Discovered dream roles (out/dream.json). Same shape as queue.json; the extension
    never touches these — the console surfaces them as a manual-only pile."""
    if not DREAM_PATH.exists():
        return []
    try:
        return json.loads(DREAM_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
