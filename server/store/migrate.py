"""Alembic, wrapped so the rest of the code never shells out to it.

Alembic is set up in phase 1 rather than when it is first needed. Once a database holds a
real job history, a schema change has to preserve it, and retrofitting migrations onto data
that already exists means hand-writing the first one against a database you cannot recreate.

`upgrade()` runs on start. A fresh database is created from the models and stamped at head
instead, because running every migration against an empty file produces the same schema
more slowly and gives the first revision a second, untested code path.
"""
from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

from . import engine as engine_mod
from .models import Base

SERVER_DIR = Path(__file__).resolve().parents[1]
INI = SERVER_DIR / "alembic.ini"


def config(url: str | None = None) -> Config:
    cfg = Config(str(INI))
    cfg.set_main_option("script_location", str(SERVER_DIR / "migrations"))
    if url:
        cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _is_empty(engine) -> bool:
    names = set(inspect(engine).get_table_names())
    return not (names - {"alembic_version"})


def _stamped(engine) -> bool:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision() is not None


def upgrade(url: str | None = None) -> str:
    """Bring the database to head. Returns what it did, for the CLI to print."""
    url = url or engine_mod.default_url()
    engine = engine_mod.make_engine(url)
    cfg = config(url)
    if _is_empty(engine):
        Base.metadata.create_all(engine)
        command.stamp(cfg, "head")
        return "created"
    if not _stamped(engine):
        # A database built by create_all() before migrations existed. Its schema already
        # matches the first revision, so stamp it rather than replaying a revision that
        # would fail on tables that are already there.
        command.stamp(cfg, "head")
        return "stamped"
    command.upgrade(cfg, "head")
    return "upgraded"


def revision(message: str, url: str | None = None) -> None:
    command.revision(config(url or engine_mod.default_url()), message=message,
                     autogenerate=True)


def current(url: str | None = None) -> None:
    command.current(config(url or engine_mod.default_url()), verbose=True)


if __name__ == "__main__":
    print(upgrade(sys.argv[1] if len(sys.argv) > 1 else None))
