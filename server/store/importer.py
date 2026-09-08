"""Reads today's files into the store.

This is how an existing install crosses over. It is idempotent: importing twice leaves the
same rows, because every config load replaces its table rather than appending to it.

Config files are stored twice, as parsed rows and as their original text. `companies.yaml`
carries 82 off-board targets as comments and the answer bank's header documents every
field, so a parse-and-render round trip would silently delete the more useful half of both
files. See `ConfigDoc` in `models.py`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core import paths  # noqa: E402

from .store import Store, rows_hash  # noqa: E402

#: Config files kept verbatim as well as parsed, keyed by the name `export` writes back.
PROFILE_DOC = "profile.json"
COMPANIES_DOC = "companies.yaml"
ANSWER_BANK_DOC = "answer_bank.yaml"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def import_profile(store: Store, path: Path | None = None) -> int:
    """The profile, plus each resume track's PDF bytes if the cache holds them."""
    p = path or paths.PROFILE
    data = _read_json(p)
    if data is None:
        return 0
    store.set_profile(data)
    store.set_config_doc(PROFILE_DOC, p.read_text(encoding="utf-8"), rows_hash(data))
    return 1 + import_resumes(store, data)


def import_resumes(store: Store, profile: dict | None = None) -> int:
    """Copy the cached resume PDFs into the store.

    A track with no cached file is still recorded, so `all_resumes()` lists the tracks the
    profile declares rather than only the ones that happen to be synced.
    """
    profile = profile if profile is not None else store.get_profile()
    n = 0
    for track in (profile.get("resume_tracks") or []):
        tag = str(track.get("tag") or "")
        if not tag:
            continue
        pdf = None
        src = paths.RESUMES / Path(str(track.get("local_path") or "")).name
        if src.name and src.exists():
            pdf = src.read_bytes()
        store.put_resume(tag, pdf, label=str(track.get("title_prefix") or ""),
                         filename=src.name, meta=track)
        n += 1
    return n


def import_companies(store: Store, path: Path | None = None) -> int:
    p = path or paths.COMPANIES
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return 0
    rows = doc.get("companies") or []
    store.set_companies(rows)
    store.set_config_doc(COMPANIES_DOC, p.read_text(encoding="utf-8"), rows_hash(rows))
    return len(rows)


def import_answers(store: Store, path: Path | None = None) -> int:
    p = path or paths.ANSWER_BANK
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return 0
    rows = doc.get("answers") or []
    store.set_answers(rows)
    store.set_config_doc(ANSWER_BANK_DOC, p.read_text(encoding="utf-8"), rows_hash(rows))
    return len(rows)


def import_applied(store: Store, path: Path | None = None) -> int:
    data = _read_json(path or paths.APPLIED)
    rows = data.get("applied") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return 0
    store.set_applied(rows)
    return len(rows)


def import_outreach(store: Store, path: Path | None = None) -> int:
    data = _read_json(path or paths.OUTREACH)
    if not isinstance(data, dict):
        return 0
    store.set_outreach(data)
    return len(data)


def import_prefs(store: Store, path: Path | None = None) -> int:
    data = _read_json(path or paths.PREFS)
    if not isinstance(data, dict):
        return 0
    store.set_prefs(data)
    return len(data)


def import_pending(store: Store, directory: Path | None = None) -> int:
    """Active records from `pending/`, and submitted ones from `pending/submitted/`."""
    d = directory or paths.PENDING
    if not d.exists():
        return 0
    n = 0
    for archived, folder in ((False, d), (True, d / "submitted")):
        if not folder.exists():
            continue
        for f in sorted(folder.glob("*.json")):
            rec = _read_json(f)
            if isinstance(rec, dict) and rec.get("dedup_key"):
                store.put_pending(rec, archived=archived)
                n += 1
    return n


def import_queues(store: Store, out_dir: Path | None = None) -> int:
    d = out_dir or paths.OUT
    n = 0
    for kind in ("queue", "dream", "manual"):
        jobs = _read_json(d / f"{kind}.json")
        if isinstance(jobs, list):
            store.set_queue(kind, jobs)
            n += 1
    return n


def import_logs(store: Store, logs_dir: Path | None = None) -> int:
    """Fill reports and audit dumps.

    A malformed JSONL line is skipped rather than failing the import. The file is appended
    to by a live process, so a truncated last line is a normal thing to find.
    """
    d = logs_dir or paths.LOGS
    n = 0
    reports = d / "fill_reports.jsonl"
    if reports.exists():
        for line in reports.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                store.append_fill_report(json.loads(line))
                n += 1
            except json.JSONDecodeError:
                continue
    index = d / "audit_dumps.jsonl"
    summaries: dict[str, dict] = {}
    if index.exists():
        for line in index.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                summaries[str(rec.get("name") or rec.get("file") or "")] = rec
    audit = d / "audit"
    if audit.exists():
        for png in sorted(audit.glob("*.png")):
            store.add_audit(png.stem, png.read_bytes(), summaries.pop(png.stem, {}))
            n += 1
    for name, rec in summaries.items():
        store.add_audit(name, None, rec)
        n += 1
    return n


def import_all(store: Store) -> dict[str, int]:
    """Ingest the whole `JOBCOPILOT_HOME` layout. Returns a count per source."""
    return {
        "profile": import_profile(store),
        "companies": import_companies(store),
        "answers": import_answers(store),
        "applied": import_applied(store),
        "outreach": import_outreach(store),
        "prefs": import_prefs(store),
        "pending": import_pending(store),
        "queues": import_queues(store),
        "logs": import_logs(store),
    }
