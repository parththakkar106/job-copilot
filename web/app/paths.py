"""paths.py — locate the repo's shared data layer and make the pipeline's own Python
modules importable from the web app.

The web app is a *separate process* from the bridge on purpose (see web/README.md): the
bridge supersedes on every new WebSocket connection, so a web client that opened a socket
would kill Parth's live extension review. Instead the web app shares the exact same
on-disk state and reuses the pipeline's already-tested modules by importing them here.

`pending.py` and `triage.py` read the store, so importing them here is enough: both
processes open the same database and see the same rows. What remains below is the layout
this process still needs on disk — the repo root, the scraper entry point, and the two
trigger files one local process uses to poke the other.
"""
from __future__ import annotations

import sys
from pathlib import Path

# web/app/paths.py -> repo root is three levels up.
REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_DIR = REPO_ROOT / "bridge"

# Make the pipeline's modules importable before re-exporting core.paths' layout.
for _p in (str(REPO_ROOT), str(BRIDGE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import paths as core_paths  # noqa: E402

SCRAPER_MODULE = "core.scraper"   # invoked as `python -m core.scraper` from REPO_ROOT
EXTENSION_BUILD = REPO_ROOT / "extension" / "build.py"

# Sentinel files the bridge watches. Dropping one is how the web reaches the extension
# WITHOUT opening a WebSocket (which would supersede Parth's live session).
RUN_TRIGGER = BRIDGE_DIR / ".run_trigger"       # existing: fires a P1 collection run
FLUSH_TRIGGER = BRIDGE_DIR / ".flush_trigger"    # new: fires a P3 flush (send approved)

# Re-exported so routers import them from one place.
import pending as pending_mod        # noqa: E402  (bridge/pending.py)
import triage as triage_mod          # noqa: E402  (bridge/triage.py)
import drafter as drafter_mod        # noqa: E402  (bridge/drafter.py — Tier-2 draft flag)
from core import bank as bank_mod    # noqa: E402  (core/bank.py)

__all__ = [
    "REPO_ROOT", "BRIDGE_DIR", "SCRAPER_MODULE", "EXTENSION_BUILD",
    "RUN_TRIGGER", "FLUSH_TRIGGER",
    "pending_mod", "triage_mod", "bank_mod", "drafter_mod",
]
