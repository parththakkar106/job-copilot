"""Alembic environment.

The URL comes from `server.store.engine`, never from `alembic.ini`, so a migration always
runs against the same database the application does and no connection string is committed.
"""
from __future__ import annotations

import sys
from pathlib import Path

from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.store import engine as engine_mod  # noqa: E402
from server.store.models import Base  # noqa: E402

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=engine_mod.default_url(), target_metadata=target_metadata,
                      literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_mod.make_engine()
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
