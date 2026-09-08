"""prefs.py — a tiny shared preference store for runtime toggles Parth flips by hand.

Unlike env vars (read once at boot) these are flags he changes while things are running:
right now just `drafts_enabled` (the Tier-2 Claude drafting on/off switch). Values live in
the store, so the two processes that share this state — the bridge and the web console —
both read the same rows. A toggle in the web UI is picked up by the running bridge on its
next read, with no restart.

Reads never raise: an unreadable store gives {} and callers supply their own defaults.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from server.store import get_store  # noqa: E402

# One store, so the bridge and the web console — separate processes that share this state
# — always agree on where it is. The atomic temp-file write this replaced existed to stop a
# concurrent reader seeing a torn file; a row write is atomic already.


def _read() -> dict:
    try:
        return get_store().get_prefs()
    except Exception:
        # A store that cannot be read must not crash a fill or a push, which is the same
        # rule the corrupt-file fallback followed.
        return {}


def get(key: str, default=None):
    """Read one preference, or `default` when it was never set."""
    val = _read().get(key)
    return default if val is None else val


def set(key: str, value) -> None:
    """Persist one preference."""
    get_store().set_pref(key, value)


def all() -> dict:
    """The whole preference dict (a copy is unnecessary — _read builds a fresh one)."""
    return _read()
