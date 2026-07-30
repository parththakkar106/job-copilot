"""prefs.py — a tiny shared preference store for runtime toggles Parth flips by hand.

Unlike env vars (read once at boot) these are flags he changes while things are running:
right now just `drafts_enabled` (the Tier-2 Claude drafting on/off switch). It lives in a
single JSON file under bridge/ so the two processes that share the on-disk state — the
bridge and the web console (which puts bridge/ on sys.path) — both import THIS module and
see the same file. A toggle in the web UI is therefore picked up by the running bridge on
its next read, with no restart.

Deliberately dumb and defensive: a missing/corrupt file reads as {} (callers supply their
own defaults), and writes go through a temp file + os.replace so the other process can
never observe a half-written file. Never raises.
"""
from __future__ import annotations

import json
import os
import pathlib
import tempfile

# One file, next to pending/ and the sentinels, so both processes resolve the same path.
PREFS_PATH = pathlib.Path(__file__).resolve().parent / "prefs.json"


def _read() -> dict:
    try:
        return json.loads(PREFS_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except Exception:
        # Corrupt/torn file: fall back to empty rather than crashing a fill or a push.
        return {}


def get(key: str, default=None):
    """Read one preference, or `default` when it was never set."""
    val = _read().get(key)
    return default if val is None else val


def set(key: str, value) -> None:
    """Persist one preference. Atomic (temp file + os.replace) so a concurrent reader in
    the other process never sees a partial write."""
    data = _read()
    data[key] = value
    fd, tmp = tempfile.mkstemp(dir=str(PREFS_PATH.parent), prefix=".prefs.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, PREFS_PATH)  # atomic on Windows + POSIX
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def all() -> dict:
    """The whole preference dict (a copy is unnecessary — _read builds a fresh one)."""
    return _read()
