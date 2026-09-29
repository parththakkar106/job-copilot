"""paths.py — locate the repo's shared data layer and make the pipeline's own Python
modules importable from the web app.

The web app is a *separate process* from the bridge on purpose (see web/README.md): the
bridge supersedes on every new WebSocket connection, so a web client that opened a socket
would kill Parth's live extension review. Instead the web app shares the exact same
on-disk state and reuses the pipeline's already-tested modules by importing them here.

`pending.py` / `triage.py` anchor their file paths to their own location
(`Path(__file__).resolve().parent`), so importing them automatically points at THIS
checkout's pending/, applied.json and queue.json — no path juggling. The data-layer paths
below are re-exported straight from `core.paths`, the single source of truth for layout.
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

# Shared data files (all read/written by the running pipeline too). Defined once in
# core/paths.py; aliased here under the names the routers already use.
PROFILE_PATH = core_paths.PROFILE
ANSWER_BANK_PATH = core_paths.ANSWER_BANK
COMPANIES_PATH = core_paths.COMPANIES
# Save targets. On a fresh install the three READ paths above resolve to the shipped
# .example templates, so writing through them would overwrite a tracked template with the
# user's real details — see the note in core/paths.py. Persist through these instead.
PROFILE_WRITE = core_paths.PROFILE_WRITE
ANSWER_BANK_WRITE = core_paths.ANSWER_BANK_WRITE
COMPANIES_WRITE = core_paths.COMPANIES_WRITE
FILTERS_PATH = core_paths.FILTERS
SEARCH_PATH = core_paths.SEARCH
QUEUE_PATH = core_paths.QUEUE
DREAM_PATH = core_paths.OUT / "dream.json"
# Roles on a board with no adapter (see core/scraper.py). Same shape as queue.json, same
# "the extension never reads this" contract as dream.json, different reason.
MANUAL_PATH = core_paths.OUT / "manual.json"
APPLIED_PATH = core_paths.APPLIED
SCRAPER_MODULE = "core.scraper"   # invoked as `python -m core.scraper` from REPO_ROOT
EXTENSION_BUILD = REPO_ROOT / "extension" / "build.py"

# Sentinel files the bridge watches. Dropping one is how the web reaches the extension
# WITHOUT opening a WebSocket (which would supersede Parth's live session).
RUN_TRIGGER = BRIDGE_DIR / ".run_trigger"       # existing: fires a P1 collection run
FLUSH_TRIGGER = BRIDGE_DIR / ".flush_trigger"    # new: fires a P3 flush (send approved)
# Not a command: it only tells the bridge Parth is still working, so its idle-watcher
# restarts the countdown instead of flushing the pile he just approved.
ACTIVITY_PING = BRIDGE_DIR / ".activity"

# Re-exported so routers import them from one place.
import pending as pending_mod        # noqa: E402  (bridge/pending.py)
import triage as triage_mod          # noqa: E402  (bridge/triage.py)
import drafter as drafter_mod        # noqa: E402  (bridge/drafter.py — Tier-2 draft flag)
import aipass as aipass_mod          # noqa: E402  (bridge/aipass.py — AI fill+verify flag)
from core import bank as bank_mod    # noqa: E402  (core/bank.py)

__all__ = [
    "REPO_ROOT", "BRIDGE_DIR", "PROFILE_PATH", "ANSWER_BANK_PATH", "COMPANIES_PATH",
    "PROFILE_WRITE", "ANSWER_BANK_WRITE", "COMPANIES_WRITE",
    "QUEUE_PATH", "DREAM_PATH", "APPLIED_PATH", "SCRAPER_MODULE", "EXTENSION_BUILD",
    "RUN_TRIGGER", "FLUSH_TRIGGER", "ACTIVITY_PING",
    "pending_mod", "triage_mod", "bank_mod", "drafter_mod", "aipass_mod",
]
