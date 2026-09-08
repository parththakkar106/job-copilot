"""Writes the store back out as the files it replaced.

Two reasons this exists. Editing `companies.yaml` in a text editor stays a supported
workflow, so the round trip has to be explicit and lossless. And an export is the backup: a
database you cannot get your job history out of is worse than the files it replaced.

Verbatim rule for config: `import` keeps the original text of each config file along with a
hash of the rows it parsed. If the rows still hash the same, the file has not been edited
through the store and the original text is written back byte for byte, comments included.
Once the rows differ, the file is rendered from them and the caller is told, because
comments that documented rows that no longer exist would be worse than none.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core import paths  # noqa: E402

from .importer import ANSWER_BANK_DOC, COMPANIES_DOC, PROFILE_DOC  # noqa: E402
from .store import Store, rows_hash  # noqa: E402


def _verbatim(store: Store, name: str, rows) -> str | None:
    """The stored source text, if the rows have not changed since it was imported."""
    doc = store.get_config_doc(name)
    if doc and doc.get("rows_hash") == rows_hash(rows):
        return doc.get("raw_text") or None
    return None


def _write(path: Path | None, text: str) -> str:
    if path is None:
        return text
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


def export_profile(store: Store, path: Path | None = None) -> str:
    data = store.get_profile()
    text = _verbatim(store, PROFILE_DOC, data) or json.dumps(data, indent=2,
                                                             ensure_ascii=False)
    return _write(path, text)


def export_companies(store: Store, path: Path | None = None) -> str:
    rows = store.get_companies()
    text = _verbatim(store, COMPANIES_DOC, rows) or yaml.safe_dump(
        {"companies": rows}, sort_keys=False, allow_unicode=True, width=100)
    return _write(path, text)


def export_answers(store: Store, path: Path | None = None) -> str:
    rows = store.get_answers()
    text = _verbatim(store, ANSWER_BANK_DOC, rows) or yaml.safe_dump(
        {"answers": rows}, sort_keys=False, allow_unicode=True, width=100)
    return _write(path, text)


# The JSON state files below are written without a trailing newline, matching the writers
# they replace (`core/apply_log.py`, `core/contacts.py`, `bridge/prefs.py`). One extra byte
# is enough to fail the byte-identical round trip these exports are checked against.
def export_applied(store: Store, path: Path | None = None) -> str:
    return _write(path, json.dumps(store.get_applied(), indent=2))


def export_outreach(store: Store, path: Path | None = None) -> str:
    return _write(path, json.dumps(store.get_outreach(), indent=2, ensure_ascii=False))


def export_prefs(store: Store, path: Path | None = None) -> str:
    return _write(path, json.dumps(store.get_prefs(), indent=2))


def export_pending(store: Store, directory: Path) -> int:
    """Rewrite `pending/` and `pending/submitted/` from the store."""
    n = 0
    for archived, folder in ((False, directory), (True, directory / "submitted")):
        folder.mkdir(parents=True, exist_ok=True)
        for rec in store.all_pending(archived=archived):
            (folder / f"{rec['dedup_key']}.json").write_text(
                json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
            n += 1
    return n


def export_queues(store: Store, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for kind in ("queue", "dream", "manual"):
        jobs = store.get_queue(kind)
        if jobs:
            (out_dir / f"{kind}.json").write_text(json.dumps(jobs, indent=2), encoding="utf-8")
            n += 1
    return n


def export_resumes(store: Store, directory: Path) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    n = 0
    for row in store.all_resumes():
        blob = store.get_resume(row["tag"])
        if blob and blob.get("pdf"):
            (directory / (row["filename"] or f"{row['tag']}.pdf")).write_bytes(blob["pdf"])
            n += 1
    return n


def export_logs(store: Store, logs_dir: Path) -> int:
    logs_dir.mkdir(parents=True, exist_ok=True)
    reports = store.fill_reports()
    if reports:
        (logs_dir / "fill_reports.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in reports),
            encoding="utf-8")
    audits = list(reversed(store.audits()))
    if audits:
        (logs_dir / "audit_dumps.jsonl").write_text(
            "".join(json.dumps(a["summary"], ensure_ascii=False) + "\n"
                    for a in audits if a["summary"]), encoding="utf-8")
        d = logs_dir / "audit"
        d.mkdir(parents=True, exist_ok=True)
        for a in audits:
            png = store.audit_png(a["name"])
            if png:
                (d / f"{a['name']}.png").write_bytes(png)
    return len(reports) + len(audits)


def export_all(store: Store, directory: Path | None = None) -> dict[str, int]:
    """Write the whole layout into `directory`, defaulting to the live one.

    Everything lands in one directory, which is what makes an export a portable backup:
    copy it and you have moved the job search, exactly as `JOBCOPILOT_HOME` promises.
    """
    d = Path(directory) if directory else paths.STATE
    d.mkdir(parents=True, exist_ok=True)
    export_profile(store, d / "profile.json")
    export_companies(store, d / "companies.yaml")
    export_answers(store, d / "answer_bank.yaml")
    export_applied(store, d / "applied.json")
    export_outreach(store, d / "outreach.json")
    export_prefs(store, d / "prefs.json")
    return {
        "config": 6,
        "pending": export_pending(store, d / "pending"),
        "queues": export_queues(store, d / "out"),
        "resumes": export_resumes(store, d / "resumes"),
        "logs": export_logs(store, d / "logs"),
    }
