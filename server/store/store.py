"""The one interface the rest of the code calls instead of touching files.

Every module used to open its own file. That worked while the bridge was a process on one
laptop and stops working the moment the same code has to run against Postgres on a server.
So the file I/O is gone and this object is what remains.

The methods are deliberately flat and document-shaped. `get_applied()` returns the same
list of dicts that `json.loads(applied.json)` returned, and `set_applied()` takes it back.
That is what makes the rewire in `core/` and `bridge/` a body change rather than a
redesign, and it keeps this phase honest: the seam moves, the behavior does not.

Sessions are per call. A method opens one, does its work, commits and closes. The bridge
holds no long transaction, every write is small, and a session that outlived a call would
have to be handed through the same globals that phase 2 exists to delete.
"""
from __future__ import annotations

import hashlib
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete as sql_delete
from sqlalchemy import select

from . import engine as engine_mod
from .models import (Applied, AnswerBankEntry, AuditDump, Company, ConfigDoc, Contact,
                     Device, FillReport, Pending, Pref, Profile, QueueRun, ResumeTrack, User)

# Local mode has one user and no signup flow, so the id is fixed rather than looked up by
# email. Hosted mode gets the id from the session, never from a request body.
LOCAL_USER_ID = 1


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    """Reads and writes one user's data.

    Build it with `Store.at()` for a directory, or `Store.from_url()` for an explicit
    database. `get_store()` returns the process-wide default and is what production code
    uses.
    """

    def __init__(self, sessionmaker_, user_id: int = LOCAL_USER_ID):
        self._sm = sessionmaker_
        self.user_id = user_id

    # ---- construction ------------------------------------------------------------

    @classmethod
    def from_url(cls, url: str, user_id: int = LOCAL_USER_ID, create: bool = True) -> "Store":
        eng = engine_mod.make_engine(url)
        if create:
            engine_mod.create_all(eng)
        st = cls(engine_mod.make_sessionmaker(eng), user_id)
        st.ensure_user()
        return st

    @classmethod
    def at(cls, directory: Path | str, user_id: int = LOCAL_USER_ID) -> "Store":
        """A SQLite store in `directory`. Tests point this at a temp directory."""
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        return cls.from_url(engine_mod.sqlite_url(d / engine_mod.DB_FILENAME), user_id)

    def adopt_files(self) -> dict | None:
        """Ingest the file layout, once, if this store is empty and files are there.

        Returns the import counts, or None when there was nothing to adopt. Importing is a
        replace per table, so a second call on a populated store would overwrite newer rows
        with stale files. The emptiness check is what stops that.
        """
        if self.has_profile() or self.get_applied() or self.all_pending():
            return None
        from . import importer  # imported here: importer reads this module
        from core import paths
        if not (paths.PROFILE.exists() or paths.APPLIED.exists()):
            return None
        counts = importer.import_all(self)
        print(f"[store] adopted the file layout under {paths.STATE}: "
              + ", ".join(f"{k} {v}" for k, v in counts.items() if v), file=sys.stderr)
        return counts

    def ensure_user(self) -> None:
        with self._sm() as s:
            if s.get(User, self.user_id) is None:
                s.add(User(id=self.user_id))
                s.commit()

    # ---- profile -----------------------------------------------------------------

    def get_profile(self) -> dict:
        with self._sm() as s:
            row = s.get(Profile, self.user_id)
            return dict(row.data) if row and row.data else {}

    def set_profile(self, data: dict) -> None:
        with self._sm() as s:
            row = s.get(Profile, self.user_id)
            if row is None:
                s.add(Profile(user_id=self.user_id, data=data))
            else:
                row.data = data
            s.commit()

    def has_profile(self) -> bool:
        """True once a real profile is stored, replacing `paths.is_configured()`."""
        with self._sm() as s:
            row = s.get(Profile, self.user_id)
            return bool(row and row.data)

    # ---- companies ---------------------------------------------------------------

    def get_companies(self) -> list[dict]:
        with self._sm() as s:
            rows = s.scalars(select(Company).where(Company.user_id == self.user_id)
                             .order_by(Company.position, Company.id)).all()
        return [dict(r.data or {}) for r in rows]

    def set_companies(self, rows: list[dict]) -> None:
        """Replace the company list.

        Order is preserved. The file groups companies by board, and an export that lost the
        grouping would not be recognizable as the file the user edits.
        """
        with self._sm() as s:
            s.execute(sql_delete(Company).where(Company.user_id == self.user_id))
            for i, r in enumerate(rows):
                s.add(Company(user_id=self.user_id, name=r.get("name"), ats=r.get("ats"),
                              slug=r.get("slug"), tier=r.get("tier"), data=r, position=i))
            s.commit()

    # ---- answer bank -------------------------------------------------------------

    def get_answers(self) -> list[dict]:
        with self._sm() as s:
            rows = s.scalars(select(AnswerBankEntry)
                             .where(AnswerBankEntry.user_id == self.user_id)
                             .order_by(AnswerBankEntry.position, AnswerBankEntry.id)).all()
        return [dict(r.data or {}) for r in rows]

    def set_answers(self, rows: list[dict]) -> None:
        with self._sm() as s:
            s.execute(sql_delete(AnswerBankEntry)
                      .where(AnswerBankEntry.user_id == self.user_id))
            for i, r in enumerate(rows):
                s.add(AnswerBankEntry(
                    user_id=self.user_id, entry_id=str(r.get("id") or ""), key=r.get("key"),
                    scope=r.get("scope"), company=r.get("company"), status=r.get("status"),
                    data=r, position=i))
            s.commit()

    # ---- applied -----------------------------------------------------------------

    def get_applied(self) -> list[dict]:
        with self._sm() as s:
            rows = s.scalars(select(Applied).where(Applied.user_id == self.user_id)
                             .order_by(Applied.position, Applied.id)).all()
        return [dict(r.data or {}) for r in rows]

    def set_applied(self, rows: list[dict]) -> None:
        """Replace the applied log.

        A duplicate `dedup_key` is dropped rather than raising: the file this replaces had
        no constraint, so an existing log can contain one, and refusing to load it would
        strand the user with a database they cannot import into. The last row wins, which
        matches how the file's readers scanned it.
        """
        seen: dict[str, dict] = {}
        for r in rows:
            key = str(r.get("dedup_key") or "")
            if key:
                seen[key] = r
        with self._sm() as s:
            s.execute(sql_delete(Applied).where(Applied.user_id == self.user_id))
            for i, (key, r) in enumerate(seen.items()):
                s.add(Applied(user_id=self.user_id, dedup_key=key, company=r.get("company"),
                              title=r.get("title"), url=r.get("url"), date=r.get("date"),
                              status=r.get("status"), data=r, position=i))
            s.commit()

    def add_applied(self, row: dict) -> None:
        """Append one application, or update the row already under that key."""
        key = str(row.get("dedup_key") or "")
        with self._sm() as s:
            existing = s.scalars(select(Applied).where(Applied.user_id == self.user_id,
                                                       Applied.dedup_key == key)).first()
            if existing is None:
                nxt = s.scalars(select(Applied.position).where(Applied.user_id == self.user_id)
                                .order_by(Applied.position.desc())).first()
                existing = Applied(user_id=self.user_id, dedup_key=key,
                                   position=(nxt or 0) + 1)
                s.add(existing)
            existing.company = row.get("company")
            existing.title = row.get("title")
            existing.url = row.get("url")
            existing.date = row.get("date")
            existing.status = row.get("status")
            existing.data = row
            s.commit()

    # ---- pending -----------------------------------------------------------------

    def get_pending(self, dedup_key: str, archived: bool = False) -> dict | None:
        with self._sm() as s:
            row = self._pending_row(s, dedup_key, archived)
            return dict(row.data) if row else None

    def _pending_row(self, s, dedup_key: str, archived: bool):
        return s.scalars(select(Pending).where(Pending.user_id == self.user_id,
                                               Pending.dedup_key == dedup_key,
                                               Pending.archived == archived)).first()

    def put_pending(self, rec: dict, archived: bool = False) -> None:
        key = str(rec.get("dedup_key") or "")
        with self._sm() as s:
            row = self._pending_row(s, key, archived)
            if row is None:
                row = Pending(user_id=self.user_id, dedup_key=key, archived=archived)
                s.add(row)
            row.status = str(rec.get("status") or "awaiting")
            row.created = str(rec.get("created") or "")
            row.data = rec
            s.commit()

    def all_pending(self, status: str | None = None, archived: bool = False) -> list[dict]:
        """Every pending record, oldest first.

        Oldest first preserves the queue's freshest-first order, because phase 1 writes the
        records in the order triage served them.
        """
        with self._sm() as s:
            q = select(Pending).where(Pending.user_id == self.user_id,
                                      Pending.archived == archived)
            if status is not None:
                q = q.where(Pending.status == status)
            rows = s.scalars(q).all()
        return sorted((dict(r.data) for r in rows), key=lambda r: r.get("created") or "")

    def delete_pending(self, dedup_key: str, archived: bool = False) -> bool:
        with self._sm() as s:
            row = self._pending_row(s, dedup_key, archived)
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True

    def archive_pending(self, dedup_key: str) -> bool:
        """Retire a submitted record but keep every answer it holds.

        A prior archive under the same key is replaced, matching the `os.replace` the
        `submitted/` directory used.
        """
        with self._sm() as s:
            row = self._pending_row(s, dedup_key, False)
            if row is None:
                return False
            old = self._pending_row(s, dedup_key, True)
            if old is not None:
                s.delete(old)
                s.flush()
            row.archived = True
            s.commit()
            return True

    def restore_pending(self, dedup_key: str) -> bool:
        """Move an archived record back into the active batch. The inverse of archive."""
        with self._sm() as s:
            row = self._pending_row(s, dedup_key, True)
            if row is None:
                return False
            live = self._pending_row(s, dedup_key, False)
            if live is not None:
                s.delete(live)
                s.flush()
            row.archived = False
            s.commit()
            return True

    # ---- queue runs --------------------------------------------------------------

    def get_queue(self, kind: str = "queue") -> list:
        """The newest scrape of this kind, or [] when none has run."""
        with self._sm() as s:
            row = s.scalars(select(QueueRun).where(QueueRun.user_id == self.user_id,
                                                   QueueRun.kind == kind)
                            .order_by(QueueRun.ran_at.desc(), QueueRun.id.desc())).first()
            return list(row.jobs) if row and row.jobs else []

    def last_queue_run(self, kind: str = "queue") -> datetime | None:
        """When this kind was last scraped, or None before the first run.

        Replaces reading queue.json's mtime. A stale queue means stale postings and wasted
        fills, so the batch-start message warns on the age.
        """
        with self._sm() as s:
            row = s.scalars(select(QueueRun).where(QueueRun.user_id == self.user_id,
                                                   QueueRun.kind == kind)
                            .order_by(QueueRun.ran_at.desc(), QueueRun.id.desc())).first()
            if row is None or row.ran_at is None:
                return None
            # SQLite hands back a naive datetime, so re-attach UTC: the caller subtracts it
            # from an aware "now" and a naive one raises rather than returning an age.
            return row.ran_at if row.ran_at.tzinfo else row.ran_at.replace(tzinfo=timezone.utc)

    def set_queue(self, kind: str, jobs: list) -> None:
        with self._sm() as s:
            s.add(QueueRun(user_id=self.user_id, kind=kind, jobs=jobs, ran_at=_utcnow()))
            s.commit()

    # ---- prefs -------------------------------------------------------------------

    def get_prefs(self) -> dict:
        with self._sm() as s:
            rows = s.scalars(select(Pref).where(Pref.user_id == self.user_id)).all()
        # Values are wrapped so SQLite can hold a bare bool or string in a JSON column.
        return {r.key: (r.value or {}).get("v") for r in rows}

    def set_pref(self, key: str, value) -> None:
        with self._sm() as s:
            row = s.scalars(select(Pref).where(Pref.user_id == self.user_id,
                                               Pref.key == key)).first()
            if row is None:
                row = Pref(user_id=self.user_id, key=key)
                s.add(row)
            row.value = {"v": value}
            s.commit()

    def set_prefs(self, data: dict) -> None:
        for k, v in data.items():
            self.set_pref(k, v)

    # ---- outreach ----------------------------------------------------------------

    def get_outreach(self) -> dict:
        with self._sm() as s:
            rows = s.scalars(select(Contact).where(Contact.user_id == self.user_id)).all()
        return {r.profile_url: dict(r.data) for r in rows}

    def set_outreach(self, store: dict) -> None:
        """Replace the outreach store.

        Callers read the whole map, edit it and write it back, so a whole-map write is the
        operation they actually perform. Rows absent from `store` are deletions.
        """
        with self._sm() as s:
            existing = {r.profile_url: r for r in
                        s.scalars(select(Contact)
                                  .where(Contact.user_id == self.user_id)).all()}
            for url, rec in store.items():
                row = existing.pop(url, None)
                if row is None:
                    row = Contact(user_id=self.user_id, profile_url=url)
                    s.add(row)
                row.state = str((rec or {}).get("state") or "found")
                row.data = rec
            for row in existing.values():
                s.delete(row)
            s.commit()

    # ---- resumes -----------------------------------------------------------------

    def get_resume(self, tag: str) -> dict | None:
        with self._sm() as s:
            r = s.scalars(select(ResumeTrack).where(ResumeTrack.user_id == self.user_id,
                                                    ResumeTrack.tag == tag)).first()
            if r is None:
                return None
            return {"tag": r.tag, "label": r.label, "filename": r.filename,
                    "sha256": r.sha256, "pdf": r.pdf, "meta": dict(r.meta or {})}

    def put_resume(self, tag: str, pdf: bytes | None = None, *, label: str = "",
                   filename: str = "", meta: dict | None = None) -> str | None:
        """Store or update one track. Returns the sha256 of the bytes, or None if unset.

        The digest is computed here rather than taken from the caller, so the value the
        extension compares against is always the digest of the bytes actually stored.
        """
        digest = hashlib.sha256(pdf).hexdigest() if pdf is not None else None
        with self._sm() as s:
            row = s.scalars(select(ResumeTrack).where(ResumeTrack.user_id == self.user_id,
                                                      ResumeTrack.tag == tag)).first()
            if row is None:
                row = ResumeTrack(user_id=self.user_id, tag=tag)
                s.add(row)
            if label:
                row.label = label
            if filename:
                row.filename = filename
            if meta is not None:
                row.meta = meta
            if pdf is not None:
                row.pdf = pdf
                row.sha256 = digest
            s.commit()
            return row.sha256

    def all_resumes(self) -> list[dict]:
        """Every track's metadata, without the bytes. Listings never need the PDFs."""
        with self._sm() as s:
            rows = s.scalars(select(ResumeTrack)
                             .where(ResumeTrack.user_id == self.user_id)
                             .order_by(ResumeTrack.tag)).all()
        return [{"tag": r.tag, "label": r.label, "filename": r.filename, "sha256": r.sha256,
                 "size": len(r.pdf or b""), "meta": dict(r.meta or {})} for r in rows]

    def delete_resume(self, tag: str) -> bool:
        with self._sm() as s:
            row = s.scalars(select(ResumeTrack).where(ResumeTrack.user_id == self.user_id,
                                                      ResumeTrack.tag == tag)).first()
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True

    # ---- logs --------------------------------------------------------------------

    def append_fill_report(self, rec: dict) -> None:
        with self._sm() as s:
            s.add(FillReport(user_id=self.user_id, data=rec, ts=_utcnow()))
            s.commit()

    def fill_reports(self, limit: int | None = None) -> list[dict]:
        """Fill reports, oldest first, matching the order of the JSONL file.

        `limit` takes the NEWEST rows and then restores that order, because a caller asking
        for 50 wants the last 50, not the first.
        """
        with self._sm() as s:
            q = select(FillReport).where(FillReport.user_id == self.user_id)
            if limit:
                rows = s.scalars(q.order_by(FillReport.id.desc()).limit(limit)).all()
                rows = list(reversed(rows))
            else:
                rows = s.scalars(q.order_by(FillReport.id)).all()
        return [dict(r.data) for r in rows]

    def add_audit(self, name: str, png: bytes | None, summary: dict) -> None:
        with self._sm() as s:
            s.add(AuditDump(user_id=self.user_id, name=name, png=png, summary=summary,
                            ts=_utcnow()))
            s.commit()

    def audits(self, limit: int | None = None) -> list[dict]:
        with self._sm() as s:
            q = (select(AuditDump).where(AuditDump.user_id == self.user_id)
                 .order_by(AuditDump.id.desc()))
            rows = s.scalars(q.limit(limit) if limit else q).all()
        return [{"name": r.name, "ts": r.ts.isoformat() if r.ts else "",
                 "size": len(r.png or b""), "summary": dict(r.summary or {})} for r in rows]

    def audit_png(self, name: str) -> bytes | None:
        with self._sm() as s:
            r = s.scalars(select(AuditDump).where(AuditDump.user_id == self.user_id,
                                                  AuditDump.name == name)
                          .order_by(AuditDump.id.desc())).first()
            return r.png if r else None

    # ---- devices -----------------------------------------------------------------

    def add_device(self, token_hash: str, label: str = "") -> None:
        with self._sm() as s:
            row = s.scalars(select(Device).where(Device.user_id == self.user_id,
                                                 Device.token_hash == token_hash)).first()
            if row is None:
                s.add(Device(user_id=self.user_id, token_hash=token_hash, label=label))
            else:
                row.last_seen = _utcnow()
            s.commit()

    def devices(self) -> list[dict]:
        with self._sm() as s:
            rows = s.scalars(select(Device).where(Device.user_id == self.user_id)).all()
        return [{"label": r.label, "token_hash": r.token_hash,
                 "last_seen": r.last_seen.isoformat() if r.last_seen else None} for r in rows]

    # ---- config documents --------------------------------------------------------

    def get_config_doc(self, name: str) -> dict | None:
        with self._sm() as s:
            r = s.scalars(select(ConfigDoc).where(ConfigDoc.user_id == self.user_id,
                                                  ConfigDoc.name == name)).first()
            return ({"name": r.name, "raw_text": r.raw_text, "rows_hash": r.rows_hash,
                     "source": r.source} if r else None)

    def set_config_doc(self, name: str, raw_text: str, rows_hash: str,
                       source: str | None = None) -> None:
        with self._sm() as s:
            r = s.scalars(select(ConfigDoc).where(ConfigDoc.user_id == self.user_id,
                                                  ConfigDoc.name == name)).first()
            if r is None:
                r = ConfigDoc(user_id=self.user_id, name=name)
                s.add(r)
            r.raw_text = raw_text
            r.rows_hash = rows_hash
            if source is not None:
                r.source = source
            r.imported_at = _utcnow()
            s.commit()

    def is_own(self, name: str) -> bool:
        """True when this config came from the user's own file, not a shipped template.

        The console's setup checklist asks it. A fresh clone reads `profile.example.json`,
        so rows exist and are still nobody's answers; reporting that install as configured
        would tell the user they are done before they have entered anything.
        """
        doc = self.get_config_doc(name)
        return bool(doc and doc["source"] and not doc["source"].endswith(".example.json")
                    and not doc["source"].endswith(".example.yaml"))


def rows_hash(rows) -> str:
    """A stable digest of parsed rows, used to tell an edited config from an untouched one."""
    import json
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


_default: Store | None = None
_default_lock = threading.Lock()


def get_store() -> Store:
    """The process-wide store. Built once, from `DATABASE_URL` or the local SQLite file.

    An existing install has files and no database, so the first call ingests the files
    rather than handing back an empty store. Without this the store lands as a working
    change that empties somebody's console until they read the release notes.
    """
    global _default
    with _default_lock:
        if _default is None:
            fresh = not Path(engine_mod.local_db_path()).exists()
            _default = Store.from_url(engine_mod.default_url())
            if fresh:
                _default.adopt_files()
        return _default


def set_store(store: Store | None) -> None:
    """Replace the process-wide store. Tests use it; production code never calls it."""
    global _default
    with _default_lock:
        _default = store


def resolve(base=None) -> Store:
    """The store a caller meant.

    `bridge/pending.py` and its tests pass a `base` that used to be a directory. Accepting
    a directory here and building a SQLite store inside it keeps every one of those callers
    working, so the storage change does not also become a test rewrite.
    """
    if base is None:
        return get_store()
    if isinstance(base, Store):
        return base
    return Store.at(base)
