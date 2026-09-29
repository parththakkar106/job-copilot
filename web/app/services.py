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
    ACTIVITY_PING,
    ANSWER_BANK_PATH,
    COMPANIES_PATH,
    COMPANIES_WRITE,
    DREAM_PATH,
    FILTERS_PATH,
    FLUSH_TRIGGER,
    MANUAL_PATH,
    PROFILE_PATH,
    PROFILE_WRITE,
    QUEUE_PATH,
    RUN_TRIGGER,
    SEARCH_PATH,
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
        # Read may have come from companies.example.yaml on a fresh install; the write
        # always lands on the real, gitignored filename. First tier toggle therefore
        # promotes the template into the user's own file rather than editing the template.
        COMPANIES_WRITE.write_text("".join(lines), encoding="utf-8")
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


# Dashboard order, lowest rank first. The awaiting tiers match the bridge's
# pending.serving_order, so the console and Telegram agree. Telegram leaves approved jobs
# out of that order, because the flush submits them and a phone cannot act on them.
RANK_READY_TO_SUBMIT = 0
RANK_IN_PROGRESS = 1
RANK_READY_TO_APPROVE = 2
RANK_UNTOUCHED = 3        # also discovered, dream, and manual roles
RANK_HELD = 4
RANK_SKIPPED = 5
RANK_DONE = 6             # submitted and blocked
_AWAITING_RANK = {
    pending_mod.TIER_IN_PROGRESS: RANK_IN_PROGRESS,
    pending_mod.TIER_READY: RANK_READY_TO_APPROVE,
    pending_mod.TIER_FRESH: RANK_UNTOUCHED,
}


def sort_rows(rows: list[dict]) -> None:
    """Sort dashboard rows in place: by rank, then newest date, then fewest questions.

    Only pending rows know their question count. A discovered, dream, or manual role has
    not been opened yet, so on the same date it sorts after roles with a known count."""
    rows.sort(key=lambda r: r["unanswered"] if "rank" in r else float("inf"))
    rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    rows.sort(key=lambda r: r.get("rank", RANK_DONE if r.get("bucket") in ("submitted", "blocked")
                                  else RANK_UNTOUCHED))


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
    if st == pending_mod.APPROVED:
        rank = RANK_READY_TO_SUBMIT
    elif st == pending_mod.AWAITING:
        rank = _AWAITING_RANK[pending_mod.next_tier(rec)]
    else:
        rank = RANK_HELD if st == pending_mod.HELD else RANK_SKIPPED
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
        "date": pending_mod.job_date(rec),
        "unanswered": n,
        "resume": rec.get("resume", ""),
        "rank": rank,
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


def _byhand_rows(roles: list[dict], bucket: str, tag: str, tier: str,
                 seen_keys: set[str]) -> list[dict]:
    """Discovered roles the extension will never touch, as a manual-only pile.

    Two piles share this shape — 'dream' (too good to spend an auto-apply on) and 'manual'
    (no adapter for the board) — because from the console's side they behave identically:
    derived from a scraped file, no pending record, no transition target, apply by hand.
    Excludes any dedup_key another pile already surfaced, so a role that HAS been collected
    or applied to shows in that pile instead, never twice.
    """
    out = []
    for j in roles:
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
            "bucket": bucket,
            "tag": tag,
            "status": bucket,
            "tier": tier,
            "date": (j.get("posted") or "")[:10],
            "unanswered": 0,
            "resume": "",
        })
    return out


def _dream_rows(seen_keys: set[str]) -> list[dict]:
    return _byhand_rows(load_dream(), "dream", "apply by hand", "dream", seen_keys)


def _manual_rows(seen_keys: set[str], tiers: dict) -> list[dict]:
    """out/manual.json as its own pile. Tier comes from companies.yaml where the company is
    on it at all — most of these arrive from a feed, so most are plain pipeline."""
    rows = _byhand_rows(load_manual(), "manual", "no adapter", "pipeline", seen_keys)
    for r in rows:
        r["tier"] = tiers.get((r["company"] or "").casefold(), "pipeline")
    return rows


def dashboard_rows(since_days: int | None = 60) -> list[dict]:
    """Every pile merged into dated, bucketed rows, in `sort_rows` order. Pending shown in full;
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
    rows.extend(_manual_rows(seen, tiers))
    sort_rows(rows)
    return rows


# Buckets whose roles are still waiting on Parth to apply — the console's default view.
TO_APPLY = ("discovered", "in_queue", "filled", "dream", "manual")


def count_rows(rows: list[dict]) -> dict:
    """Bucket + tier counts over a given row set. Tier counts live under tier_* keys so the
    'dream' bucket count and the 'dream' tier count don't collide on one key.

    Takes rows rather than re-deriving them so the chip counts always describe exactly what
    the view is showing — computing counts over all history while the rows were windowed to
    60 days made the numbers disagree with the list underneath them."""
    out = {"all": len(rows), "discovered": 0, "in_queue": 0, "filled": 0, "submitted": 0,
           "blocked": 0, "dream": 0, "manual": 0, "tier_pipeline": 0, "tier_dream": 0}
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
                               for j in (load_queue() + load_dream() + load_manual()))
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


def ping_activity() -> None:
    """Tell the bridge Parth is still working, so its idle-watcher restarts the countdown.

    The console is a separate process, so the bridge cannot see these edits. Without this
    ping, an approval made after a quiet spell was flushed on the next idle poll, seconds
    later. Call it from every route that represents Parth acting on a job: approving,
    answering, changing a resume. Never from a read, and never from the Send-approved
    path, which is meant to release the pile now."""
    try:
        ACTIVITY_PING.parent.mkdir(parents=True, exist_ok=True)
        ACTIVITY_PING.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    except OSError:
        # A missed ping only costs an early flush, so never fail a real edit over it.
        pass


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
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    # Before the first save, this is the shipped template with every resume track. Trim it
    # to your search picks, so the Setup page and the first save both match them.
    if not PROFILE_WRITE.exists() and SEARCH_PATH.exists():
        profile = _search_tracks(profile)[0]
    return profile


def save_profile(obj: dict) -> None:
    """Write profile.json back, pretty-printed to match the hand-edited file. Caller has
    already validated it's a JSON object."""
    # PROFILE_WRITE, never PROFILE: on a fresh install the read path is the shipped
    # template, and saving through it would put real personal details into a tracked file.
    PROFILE_WRITE.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_WRITE.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")


# ── Search topics and levels ─────────────────────────────────────────────────────────

def search_options() -> dict:
    """The topic and level catalog from filters.yaml, marked with your current picks."""
    from core import search
    filt = yaml.safe_load(FILTERS_PATH.read_text(encoding="utf-8")) or {}
    sel = search.load(filt, SEARCH_PATH)
    return {
        "topics": [{"key": k, "label": v.get("label", k), "field": v.get("field"),
                    "selected": k in sel["topics"]}
                   for k, v in (filt.get("topics") or {}).items()],
        "levels": [{"key": k, "label": v.get("label", k), "selected": k in sel["levels"]}
                   for k, v in (filt.get("levels") or {}).items()],
        "tracks": search.tracks(filt, sel),
    }


def save_search(topics: list[str], levels: list[str]) -> list[str]:
    """Save your picks and match the profile's resume tracks to them.

    Returns the resume track tags. Raises ValueError for an empty or unknown pick.
    """
    from core import search
    filt = yaml.safe_load(FILTERS_PATH.read_text(encoding="utf-8")) or {}
    for kind, picked in (("topics", topics), ("levels", levels)):
        unknown = set(picked) - set(filt.get(kind) or {})
        if not picked or unknown:
            raise ValueError(f"pick at least one of the known {kind}"
                             + (f"; unknown: {', '.join(sorted(unknown))}" if unknown else ""))
    search.save({"topics": list(dict.fromkeys(topics)), "levels": list(dict.fromkeys(levels))},
                SEARCH_PATH)
    profile, tags = _search_tracks(load_profile())
    # Without a profile.json, load_profile trims the template instead. Writing one here
    # would count your details as started before you typed any.
    if PROFILE_WRITE.exists():
        save_profile(profile)
    return tags


def _search_tracks(profile: dict) -> tuple[dict, list[str]]:
    """The profile with its resume tracks matched to your search picks, and the track tags."""
    from core import paths as core_paths
    from core import search
    filt = yaml.safe_load(FILTERS_PATH.read_text(encoding="utf-8")) or {}
    tags = search.tracks(filt, search.load(filt, SEARCH_PATH))
    template = json.loads((core_paths.REPO_ROOT / "data" / "profile.example.json")
                          .read_text(encoding="utf-8"))
    return search.apply_tracks(profile, tags, template), tags


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

def _load_roles(path: Path) -> list[dict]:
    """One of the scraper's role files, or [] if it isn't there / isn't readable yet.

    Absent is the NORMAL state for these, not an error: out/ is gitignored, so a fresh
    checkout has none of them until the first scrape runs.
    """
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def load_queue() -> list[dict]:
    return _load_roles(QUEUE_PATH)


def load_dream() -> list[dict]:
    """Discovered dream roles (out/dream.json). Same shape as queue.json; the extension
    never touches these — the console surfaces them as a manual-only pile."""
    return _load_roles(DREAM_PATH)


def load_manual() -> list[dict]:
    """Roles on a board with no adapter (out/manual.json). Same shape and same manual-only
    handling as dream, but for a different reason — see core/scraper.py."""
    return _load_roles(MANUAL_PATH)


# ── First-run setup ──────────────────────────────────────────────────────────────────

def _configured(names: tuple[str, ...]) -> list[str]:
    """Which of `names` are set, counting bridge/.env as well as the environment.

    The bridge loads bridge/.env itself on import, so a value living only in that file is
    genuinely configured even though this process never sees it in os.environ. Checking
    os.environ alone - which this replaced - reported "not configured" for exactly the
    setup the READMEs tell you to do, which is the worst way for a checklist to be wrong.

    Names only. The values are secrets; they are read here and never returned or logged.
    """
    import os
    from core import paths as core_paths

    found = {n for n in names if str(os.environ.get(n) or "").strip()}
    env_file = core_paths.REPO_ROOT / "bridge" / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                key, sep, val = line.strip().partition("=")
                if not sep or key.startswith("#"):
                    continue
                if key.strip() in names and val.strip().strip('"').strip("'"):
                    found.add(key.strip())
        except OSError:
            pass
    return [n for n in names if n in found]


# The two credential shapes bridge/mailcode.py accepts for reading an emailed verification
# code (its OAUTH_VARS / IMAP_VARS). Duplicated rather than imported, for the reason at the
# top of this file: bridge/ is not a package and importing it boots a server.
_OAUTH_VARS = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
_IMAP_VARS = ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")


# Profile fields that only the local-folder resume sync reads. A PDF uploaded on the Setup
# page fills the cache without them, so they do not count as unset details.
_RESUME_SOURCE_FIELD = re.compile(
    r"^(_local_resume_source\.|resume_tracks\[\d+\]\.(current_file|local_source)$)")


def resume_status() -> list[dict]:
    """Each resume track and whether its PDF is in the cache the extension attaches from."""
    from core import resumes as resumes_mod
    manifest = resumes_mod.load_manifest()
    return [{"tag": t["tag"], "cached": resumes_mod.cached_path(t).is_file(),
             "synced": manifest.get(t["tag"], {}).get("synced")}
            for t in load_profile().get("resume_tracks") or []]


def upload_resume(tag: str, raw: bytes) -> bool:
    """Install an uploaded PDF as this track's cached resume. True if the bytes changed.

    Raises KeyError for an unknown track and ValueError for bytes that are not a PDF.
    """
    from core import resumes as resumes_mod
    track = next((t for t in load_profile().get("resume_tracks") or []
                  if t.get("tag") == tag), None)
    if track is None:
        raise KeyError(tag)
    try:
        return resumes_mod._write_cache(track, raw, "console upload")
    except resumes_mod.SyncError as e:
        raise ValueError(str(e)) from e


def setup_status() -> dict:
    """What a new install still needs, as a checklist the Setup page renders.

    Every step reports `done` plus enough detail to act on it. Steps the machine cannot do
    for the user (loading an unpacked extension, talking to BotFather, asking Google for a
    mail credential) are still listed - with `manual: True` - because hiding them is what
    made the README feel like a wall of instructions in the first place. `blocking` marks
    the ones without which nothing works.

    ORDER IS THE MESSAGE. The extension is the product: it is what fills a form, and the
    steps run details -> extension -> resumes -> companies because that is the shortest
    path to a filled application. `optional: True` marks the two control-plane add-ons
    (Telegram, emailed verification codes) that the extension never needs - they are
    grouped last and counted separately, so a checklist that reads "4 of 6" cannot make a
    complete install look unfinished for declining an extra.
    """
    from core import paths as core_paths
    from core import profile as profile_mod

    steps: list[dict] = []

    # Read through the MODULE-level names, not core_paths directly: test_api rebinds these
    # onto a temp directory, and reaching past them made this function report on the real
    # install regardless of isolation - so it passed on a configured machine and failed on
    # a fresh clone, which is exactly backwards for a first-run check.
    configured = PROFILE_WRITE.exists()
    holes: list[str] = []
    if configured:
        try:
            holes = [h for h in profile_mod.find_fillme(
                json.loads(PROFILE_WRITE.read_text(encoding="utf-8")))
                if not _RESUME_SOURCE_FIELD.match(h)]
        except Exception:
            holes = []
    steps.append({
        "id": "profile", "title": "Your details",
        "done": bool(configured and not holes),
        # A leftover FILL_ME is not a broken install - the extension just asks you that
        # question instead of autofilling it, which is a deliberate design choice. So the
        # step shows incomplete while the install still counts as usable.
        "minimum_met": configured,
        "blocking": True, "manual": False, "optional": False,
        "detail": ("Not created yet - saving the Profile page once creates it."
                   if not configured else
                   (f"{len(holes)} field(s) still unset." if holes else "Complete.")),
        # The whole list: the page scrolls it, and a shorter list contradicts the count.
        "unfilled": holes,
        "link": "/profile",
    })

    # What to search. The defaults work, so this is never blocking. It comes before the
    # resumes because the chosen topics decide which resume tracks the page asks for.
    steps.append({
        "id": "search", "title": "What to search",
        "done": SEARCH_PATH.exists(), "blocking": False, "manual": False, "optional": False,
        "detail": ("Saved. Change it any time on the Search page." if SEARCH_PATH.exists()
                   else "Pick your topics and experience levels. Until you do, the default "
                        "tech topics at entry and mid level are searched."),
        "link": "/search",
    })

    # 2. The extension, straight after the details that feed it. Chrome deliberately
    # requires a human for an unpacked extension, so this is `manual` - but it is the main
    # event, not an extra, and it is listed here rather than after the optional add-ons.
    staged = (core_paths.REPO_ROOT / "extension" / "profile.json").exists()
    steps.append({
        "id": "extension", "title": "Chrome extension",
        "done": staged, "blocking": False, "manual": True, "optional": False,
        "detail": ("Built. Load it once in Chrome, then rebuild from the Profile page "
                   "after you edit your details."
                   if staged else
                   "Not staged yet - use Rebuild extension on the Profile page, then load "
                   "it at chrome://extensions."),
        "link": "/profile",
        # The page offers this as a copy button. Chrome blocks links to chrome:// pages,
        # so copying the address and the folder is as close as a web page can get.
        "path": str(core_paths.REPO_ROOT / "extension"),
    })

    # 3. Companies: usable immediately thanks to the shipped example, so "done" means the
    # user has their OWN list rather than merely having something to scrape.
    own_companies = COMPANIES_WRITE.exists()
    try:
        n_companies = len(load_companies())
    except Exception:
        n_companies = 0
    steps.append({
        "id": "companies", "title": "Companies to watch",
        "done": own_companies, "blocking": False, "manual": False, "optional": False,
        "detail": (f"Using your own list ({n_companies} companies)." if own_companies
                   else f"Using the {n_companies} shipped examples - edit any tier to make it yours."),
        "link": "/companies",
    })

    # 4. Resumes: only the apply half needs them. The page renders an upload button per
    # track from `tracks`, so this step needs no command.
    tracks = resume_status()
    n_resumes = sum(1 for t in tracks if t["cached"])
    steps.append({
        "id": "resumes", "title": "Resume PDFs",
        "done": n_resumes > 0, "blocking": False, "manual": False, "optional": False,
        "detail": (f"{n_resumes} of {len(tracks)} tracks have a PDF." if n_resumes else
                   "Upload a PDF for each resume track you use."),
        "tracks": tracks,
        "link": None,
    })

    # ── Optional from here down. Both are control-plane conveniences: the extension fills
    # and submits forms without either one, and declining them costs nothing but a little
    # typing. Neither is ever `blocking`.

    # 5. Telegram: answer questions from your phone instead of the console.
    tg_set = _configured(("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"))
    tg = len(tg_set) == 2
    steps.append({
        "id": "telegram", "title": "Telegram control",
        "done": tg, "blocking": False, "manual": True, "optional": True,
        # A half-finished pair is the common state here and naming the missing half saves a
        # hunt: "not configured" when one of the two is already set reads as a lie.
        "detail": ("Connected." if tg else
                   (f"Half configured - {tg_set[0]} is set, the other is not. See "
                    "bridge/README.md." if tg_set else
                    "Not configured. Skip it and use this console instead, or see "
                    "bridge/README.md - python bridge/get_chat_id.py does the fiddly part.")),
        "link": None,
    })

    # 6. Verification codes. Some Greenhouse boards mail an 8-character code on submit; with
    # a mailbox credential the extension reads it back and fills it, and without one it
    # parks the application for you to finish by hand - which is the behaviour this project
    # shipped with for months. Manual for the same reason Telegram is: generating an App
    # Password or an OAuth client is a conversation with Google that no script here can
    # have on your behalf.
    oauth_set, imap_set = _configured(_OAUTH_VARS), _configured(_IMAP_VARS)
    mail_ready = len(oauth_set) == len(_OAUTH_VARS) or len(imap_set) == len(_IMAP_VARS)
    if mail_ready:
        # Which one is live matters: OAuth wins when both are set (make_reader prefers it),
        # so a leftover App Password is not what is being used.
        mail_detail = ("Read-only OAuth token configured."
                       if len(oauth_set) == len(_OAUTH_VARS) else
                       "App Password configured (IMAP, opened read-only).")
    elif oauth_set or imap_set:
        mail_detail = ("Partly configured - " + ", ".join(oauth_set + imap_set) +
                       " set. Finish one of the two shapes; see bridge/README.md.")
    else:
        mail_detail = ("Not configured. Codes get pasted by hand, which works fine. To "
                       "automate it, set GMAIL_ADDRESS + GMAIL_APP_PASSWORD, or run "
                       "python bridge/gmail_auth.py for a read-only token.")
    steps.append({
        "id": "verify_code", "title": "Emailed verification codes",
        "done": mail_ready, "blocking": False, "manual": True, "optional": True,
        "detail": mail_detail,
        "link": None,
    })

    blocking_left = [s["id"] for s in steps
                     if s["blocking"] and not s.get("minimum_met", s["done"])]
    core = [s for s in steps if not s["optional"]]
    return {
        "ready": not blocking_left,
        "blocking_left": blocking_left,
        # Progress is measured over the core steps only. Counting the optional extras in
        # the denominator would put a permanent unfinished remainder on the page of anyone
        # who reasonably never wants Telegram or a mail credential.
        "core_done": sum(1 for s in core if s["done"]),
        "core_total": len(core),
        "done_count": sum(1 for s in steps if s["done"]),
        "total": len(steps),
        "steps": steps,
    }
