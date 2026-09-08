"""Alembic environment.

The URL comes from the config when a caller set one, and otherwise from
`server.store.engine`. No connection string is committed: `alembic.ini` leaves
`sqlalchemy.url` unset, so the default is whatever the application itself would open.

Honoring the config's URL is what lets a caller migrate a database other than the default
one. Reading only the default meant `migrate.config(url)` silently targeted the wrong
database, which a test that migrates a temp file finds immediately and a person does not.
"""
from __future__ import annotations

import sys
from pathlib import Path

from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.store import engine as engine_mod  # noqa: E402
from server.store.models import Base  # noqa: E402

target_metadata = Base.metadata


def _url() -> str:
    return context.config.get_main_option("sqlalchemy.url", None) or engine_mod.default_url()


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata,
                      literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_mod.make_engine(_url())
    with connectable.connect() as connection:
        # SQLite cannot ALTER a column in place, so Alembic has to rebuild the table.
        # render_as_batch makes that automatic and is inert on Postgres.
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
