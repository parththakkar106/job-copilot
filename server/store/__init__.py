"""The job-copilot store: one interface over SQLite locally and Postgres when hosted."""
from .store import (LOCAL_USER_ID, Store, get_store, resolve, rows_hash,  # noqa: F401
                    set_store)

__all__ = ["Store", "get_store", "set_store", "resolve", "rows_hash", "LOCAL_USER_ID"]
