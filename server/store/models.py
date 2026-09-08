"""SQLAlchemy tables for the job-copilot store.

Every table carries `user_id`. Local mode has exactly one user row and hosted mode has
many, so the column exists from the first migration rather than being added later against
live data. See `docs/server_migration_plan.md`, phase 1.

Two shapes recur:

`data` JSON columns hold the record verbatim, and the named columns beside them are a
projection of it for querying. The JSON is the truth, so a row survives a round trip
exactly: a company whose `slug` is null keeps a null rather than gaining an empty string,
and a row missing an optional key does not gain a default. That fidelity is what lets
`exporter` prove a config file is unedited and write the original text back, comments
included.

Read the projection when you filter (`dedup_key`, `status`, `tag`, `key`). Read `data` when
you want the record.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Integer, LargeBinary, String,
                        Text, TypeDecorator, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: Marks an encoded date inside a JSON document. Chosen because YAML and JSON both refuse
#: a bare `$` as the start of a plain key, so no real config row can collide with it.
DATE_TAG = "$date"
DATETIME_TAG = "$datetime"


class JSONDoc(TypeDecorator):
    """A JSON column that can also hold dates.

    YAML parses an unquoted `created: 2026-07-09` into a `datetime.date`, and the answer
    bank has three of them. JSON has no date, so storing one means either failing the write
    or quietly turning it into a string. A string is the worse option: re-exporting it
    writes `created: '2026-07-09'`, which is a different file and a different type from the
    one imported, and the verbatim check would then report every untouched answer bank as
    edited.

    So dates are tagged on the way in and rebuilt on the way out. A one-key dict under the
    tag is the only thing decoded, so an ordinary document passes through untouched.
    """
    impl = JSON
    cache_ok = True

    @staticmethod
    def _encode(value):
        if isinstance(value, datetime):
            return {DATETIME_TAG: value.isoformat()}
        if isinstance(value, date):
            return {DATE_TAG: value.isoformat()}
        if isinstance(value, dict):
            return {k: JSONDoc._encode(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [JSONDoc._encode(v) for v in value]
        return value

    @staticmethod
    def _decode(value):
        if isinstance(value, dict):
            if len(value) == 1:
                raw = value.get(DATE_TAG)
                if isinstance(raw, str):
                    return date.fromisoformat(raw)
                raw = value.get(DATETIME_TAG)
                if isinstance(raw, str):
                    return datetime.fromisoformat(raw)
            return {k: JSONDoc._decode(v) for k, v in value.items()}
        if isinstance(value, list):
            return [JSONDoc._decode(v) for v in value]
        return value

    def process_bind_param(self, value, dialect):
        return self._encode(value)

    def process_result_value(self, value, dialect):
        return self._decode(value)


class Base(DeclarativeBase):
    pass


class User(Base):
    """One person. Local mode creates a single row and never offers a signup flow.

    `email` and `password_hash` are nullable because the local user has neither until
    phase 5 adds the console login.
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Profile(Base):
    """`profile.json`, one row per user.

    The document stays whole. `core/profile.py` digs into it with dotted paths that the
    taxonomy names, so a column per field would mean editing the schema every time a
    taxonomy rule refers to something new.
    """
    __tablename__ = "profiles"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    data: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class ResumeTrack(Base):
    """One resume track, with the PDF bytes beside its metadata.

    The bytes live here rather than in a file next to the database because hosted mode has
    no durable disk: a Fly machine is replaced on every deploy. `sha256` is what the
    extension compares against before asking for a fresh copy, so it is stored, not
    recomputed.
    """
    __tablename__ = "resume_tracks"
    __table_args__ = (UniqueConstraint("user_id", "tag", name="uq_resume_user_tag"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    tag: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(255), default="")
    filename: Mapped[str] = mapped_column(String(255), default="")
    meta: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    pdf: Mapped[bytes | None] = mapped_column(LargeBinary)
    sha256: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class Company(Base):
    """One target company from `companies.yaml`.

    Identity is `(ats, slug, name)`. Slug alone is not enough: the Workday rows carry no
    slug at all and are addressed by the `host` and `board` inside `data`, so twelve of them
    would collide on a null. Name alone is not enough either, because two companies can
    share one.
    """
    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("user_id", "ats", "slug", "name",
                                       name="uq_company_user_ats_slug_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    ats: Mapped[str | None] = mapped_column(String(32))
    slug: Mapped[str | None] = mapped_column(String(255))
    tier: Mapped[str | None] = mapped_column(String(32), index=True)
    data: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    position: Mapped[int] = mapped_column(Integer, default=0)


class AnswerBankEntry(Base):
    """One reusable answer.

    `entry_id` is the hand-written id from the YAML, kept because the CLI addresses entries
    by it and a user's notes refer to it. The surrogate `id` exists so the row survives a
    rename.
    """
    __tablename__ = "answer_bank"
    __table_args__ = (UniqueConstraint("user_id", "entry_id", name="uq_answer_user_entry"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    entry_id: Mapped[str] = mapped_column(String(255))
    key: Mapped[str | None] = mapped_column(String(255), index=True)
    scope: Mapped[str | None] = mapped_column(String(32))
    company: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str | None] = mapped_column(String(32), index=True)
    data: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    position: Mapped[int] = mapped_column(Integer, default=0)


class Pending(Base):
    """One in-flight application, replacing `pending/<dedup_key>.json`.

    `archived` replaces the `submitted/` subdirectory. The directory kept submitted records
    out of the active batch while preserving the answers; a boolean does the same thing and
    lets one query serve both piles.
    """
    __tablename__ = "pending"
    __table_args__ = (UniqueConstraint("user_id", "dedup_key", "archived",
                                       name="uq_pending_user_key_archived"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    dedup_key: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="awaiting", index=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created: Mapped[str] = mapped_column(String(64), default="")
    data: Mapped[dict] = mapped_column(JSONDoc, default=dict)


class Applied(Base):
    """The permanent record of an application, replacing `applied.json`.

    This table is what dedup runs against, so `dedup_key` is indexed and unique per user. A
    second row for the same key would offer a job the user already applied to.
    """
    __tablename__ = "applied"
    __table_args__ = (UniqueConstraint("user_id", "dedup_key", name="uq_applied_user_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    dedup_key: Mapped[str] = mapped_column(String(64), index=True)
    company: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(512))
    url: Mapped[str | None] = mapped_column(Text)
    date: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str | None] = mapped_column(String(32), index=True)
    data: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    position: Mapped[int] = mapped_column(Integer, default=0)


class QueueRun(Base):
    """One scrape result set: `queue.json`, `dream.json` or `manual.json`.

    Only the newest run per kind is read. Older rows stay because they record what the
    pipeline was targeting on a given day, which nothing else preserves once the scraper
    stops force-committing `out/`.
    """
    __tablename__ = "queue_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    jobs: Mapped[list] = mapped_column(JSONDoc, default=list)


class FillReport(Base):
    """One line of `logs/fill_reports.jsonl`. Append only, never updated."""
    __tablename__ = "fill_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    data: Mapped[dict] = mapped_column(JSONDoc, default=dict)


class AuditDump(Base):
    """A screenshot of a form plus the summary written beside it.

    The PNG is a column for the same reason the resume bytes are: hosted mode has no disk
    that survives a deploy.
    """
    __tablename__ = "audit_dumps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    png: Mapped[bytes | None] = mapped_column(LargeBinary)
    summary: Mapped[dict] = mapped_column(JSONDoc, default=dict)


class Pref(Base):
    """One runtime setting from `prefs.json`. Values are JSON, so a bool stays a bool."""
    __tablename__ = "prefs"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_pref_user_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    key: Mapped[str] = mapped_column(String(255))
    value: Mapped[dict] = mapped_column(JSONDoc, default=dict)


class Contact(Base):
    """One person in the outreach store, keyed by normalized profile URL.

    `core/contacts.py` and `core/referral.py` both write this. They agree on the key rule,
    and keeping one table is what stops a person found by search and the same person pasted
    in by hand from becoming two rows.
    """
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("user_id", "profile_url", name="uq_contact_user_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    profile_url: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), default="found", index=True)
    data: Mapped[dict] = mapped_column(JSONDoc, default=dict)


class Device(Base):
    """A paired extension.

    Only the hash of the token is stored. Hosted mode authenticates the WebSocket with it in
    phase 3, and a stored plaintext token would be a credential sitting in the database for
    no gain.
    """
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("user_id", "token_hash", name="uq_device_user_token"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConfigDoc(Base):
    """The verbatim text of an imported config file, with a hash of the rows it produced.

    `companies.yaml` carries 82 off-board targets as YAML comments, and the answer bank's
    header explains every field. A parse-and-render round trip drops all of it, so the
    export would silently destroy the file it claims to reproduce. Keeping the source text
    lets `export` re-emit the original byte for byte while the rows are unedited, and warn
    rather than pretend once they are not.
    """
    __tablename__ = "config_docs"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_configdoc_user_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    raw_text: Mapped[str] = mapped_column(Text, default="")
    rows_hash: Mapped[str] = mapped_column(String(64), default="")
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
