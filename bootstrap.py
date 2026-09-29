"""One-command first-time setup.

    python bootstrap.py

Installs the Python dependencies, creates your config files from the shipped templates,
and launches the console, which walks you through the rest in a browser.

WHY THIS EXISTS
---------------
The steps this replaces were never difficult, only numerous, and a numbered list of
fifteen shell commands reads as harder than it is. Everything here is something you could
do by hand from docs/guide.md; nothing is hidden or magic, and each step prints what it did.

What this deliberately does NOT do:
  - install Python (you already have it if this is running). Node is not needed: the
    console UI ships prebuilt, and Node only matters if you change it
  - load the Chrome extension. Chrome requires a human for unpacked extensions, by design
  - talk to Telegram. BotFather is a conversation, and the bot is optional anyway
  - ask Google for a mail credential, which is the same kind of conversation. Optional
    too: without one you paste an emailed verification code yourself
  - store a Workday password. It lives only in the extension's own storage, set from the
    popup, so it never passes through this script or a file in the checkout
  - overwrite any config you already have. Existing files are left strictly alone
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

TEMPLATES = [
    ("profile.example.json", "profile.json", "your details"),
    ("companies.example.yaml", "companies.yaml", "companies to watch"),
    ("answer_bank.example.yaml", "answer_bank.yaml", "reusable answers"),
]


def say(step: str, msg: str) -> None:
    print(f"[{step}] {msg}", flush=True)


def python() -> str:
    """The interpreter to install into and run with.

    Outside a virtual environment, create .venv and use it, so these packages never mix
    with your other Python projects. start.py and the daily run both look for .venv.
    Inside a virtual environment, use it as is.
    """
    if sys.prefix != sys.base_prefix:
        say("python", "using the active virtual environment")
        return sys.executable
    venv_py = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if venv_py.exists():
        say("python", "using .venv")
        return str(venv_py)
    say("python", "creating .venv for this project's packages")
    try:
        subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")], check=True)
    except subprocess.CalledProcessError:
        say("python", "could not create .venv; installing into this Python instead")
        return sys.executable
    return str(venv_py)


def deps(py: str) -> bool:
    """Install the Python requirements. Returns False if it failed, so we can carry on and
    report everything at the end rather than dying on the first problem."""
    reqs = [ROOT / "bridge" / "requirements.txt", ROOT / "web" / "requirements.txt"]
    present = [r for r in reqs if r.exists()]
    if not present:
        say("deps", "no requirements files found, skipping")
        return True
    cmd = [py, "-m", "pip", "install", "-q"]
    for r in present:
        cmd += ["-r", str(r)]
    say("deps", "installing Python packages (this can take a minute)")
    try:
        subprocess.run(cmd, check=True)
        say("deps", "done")
        return True
    except subprocess.CalledProcessError as e:
        say("deps", f"pip failed ({e.returncode}). Try running this by hand:\n"
                    f"       {' '.join(cmd)}")
        return False


def config() -> None:
    """Copy each template to its real name, never clobbering an existing file."""
    for src_name, dst_name, what in TEMPLATES:
        src, dst = DATA / src_name, DATA / dst_name
        if dst.exists():
            say("config", f"{dst_name} already exists, leaving it alone")
            continue
        if not src.exists():
            say("config", f"template {src_name} is missing, skipping {dst_name}")
            continue
        shutil.copy2(src, dst)
        say("config", f"created {dst_name} ({what}) from the template")


def hooks() -> None:
    """Activate the tracked git hooks, which is one config line nobody reads the README for.

    Git will not run a hook out of a tracked directory unless it is told to, so a clone
    has the pii-guard check sitting inert on disk until this runs. Skipped outside a git
    checkout, and never overrides a hooksPath somebody has already chosen.
    """
    if not (ROOT / ".git").exists():
        return
    try:
        cur = subprocess.run(["git", "config", "--get", "core.hooksPath"], cwd=ROOT,
                             capture_output=True, text=True).stdout.strip()
        if cur and cur != ".githooks":
            say("hooks", f"core.hooksPath is already set to {cur}, leaving it alone")
            return
        if cur == ".githooks":
            say("hooks", "personal-details check already active")
            return
        subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=ROOT,
                       check=True, capture_output=True)
        say("hooks", "enabled the pre-commit check that keeps your details out of commits")
    except (subprocess.CalledProcessError, FileNotFoundError):
        say("hooks", "could not configure git hooks; run this by hand if you commit here:\n"
                     "       git config core.hooksPath .githooks")


def extension(py: str) -> None:
    """Stage your config into extension/, so the folder is ready to load in Chrome.

    The extension reads its own copy of profile.json and taxonomy.json, and those copies
    are gitignored. Without this step, Chrome would load an extension with no profile.
    """
    try:
        subprocess.run([py, str(ROOT / "extension" / "build.py")], cwd=ROOT,
                       check=True, capture_output=True)
        say("extension", f"built; load this folder in Chrome: {ROOT / 'extension'}")
    except subprocess.CalledProcessError:
        say("extension", "build failed; use Rebuild extension on the console's Profile page")


def launch(py: str) -> None:
    say("launch", "starting the console; a setup checklist opens in your browser")
    print()
    try:
        subprocess.run([py, str(ROOT / "start.py")], check=False)
    except KeyboardInterrupt:
        pass


def main() -> int:
    print("job copilot - first-time setup\n")
    py = python()
    ok = deps(py)
    config()
    hooks()
    if ok:
        extension(py)
    print()
    say("next", "The console will show a checklist. Things it cannot do for you:")
    say("next", "  1. load the extension at chrome://extensions > Developer mode > Load unpacked")
    say("next", "  2. create a Telegram bot, if you want phone control (optional)")
    say("next", "  3. set up a mail credential for emailed codes (optional, see bridge/README.md)")
    say("next", "  4. save a Workday password in the extension popup (optional, Workday only)")
    print()
    if not ok:
        say("next", "Dependencies did not install cleanly - fix that first, then: python start.py")
        return 1
    launch(py)
    return 0


if __name__ == "__main__":
    sys.exit(main())
