"""The job-copilot store: one interface over SQLite locally and Postgres when hosted."""
from pathlib import Path

from .store import (LOCAL_USER_ID, Store, get_store, resolve, rows_hash,  # noqa: F401
                    set_store)

__all__ = ["Store", "get_store", "set_store", "resolve", "rows_hash", "LOCAL_USER_ID",
           "use_temp_store"]


def use_temp_store(directory: Path | str) -> Store:
    """Point this process at a throwaway store in `directory`, and return it.

    Tests used to isolate by assigning a temp path to each module's file constant, which
    meant every new file needed another assignment and a missed one wrote to the real data.
    One store replaces all of them, so isolation is this single call.
    """
    st = Store.at(directory)
    set_store(st)
    return st
