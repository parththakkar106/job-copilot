"""Builds the SQLAlchemy engine, for SQLite locally and Postgres when hosted.

One implementation covers both. `DATABASE_URL` picks the backend, and nothing above this
module knows which one it got. Local mode leaves the variable unset and gets a SQLite file
under `JOBCOPILOT_HOME`, beside the resumes and the logs it replaces.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core import paths  # noqa: E402

from .models import Base  # noqa: E402

DB_FILENAME = "store.db"


def default_url() -> str:
    """The database this machine uses: `DATABASE_URL` if set, else the local SQLite file."""
    url = os.environ.get("DATABASE_URL", "").strip()
    return url or sqlite_url(paths.STATE / DB_FILENAME)


def sqlite_url(path: Path | str) -> str:
    return f"sqlite+pysqlite:///{Path(path).resolve()}"


def make_engine(url: str | None = None) -> Engine:
    """An engine for `url`, with SQLite taught to behave.

    SQLite needs two settings that Postgres already gets right. Foreign keys are off by
    default, so a bad `user_id` would be accepted silently. The default journal replaces the
    whole file on each write, so a reader during a write sees a locked database; WAL lets
    the console read while the bridge writes, which is the normal case here.
    """
    url = url or default_url()
    is_sqlite = url.startswith("sqlite")
    kwargs: dict = {"future": True}
    if is_sqlite:
        Path(url.split("///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
        # The bridge answers Telegram on one thread and the WS hub on another. Both reach
        # the same store, and SQLite's default check would reject the second one.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    eng = create_engine(url, **kwargs)
    if is_sqlite:
        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    return eng


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def create_all(engine: Engine) -> None:
    """Create every table.

    Alembic owns schema changes once data exists. This stays for the two cases where a
    migration run is the wrong tool: a test building a throwaway database, and the first
    `import` on a machine that has no database yet. `server.store.migrate` stamps a fresh
    database at head so the two paths do not diverge.
    """
    Base.metadata.create_all(engine)
