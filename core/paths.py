"""Where everything lives on disk — the one place that knows the repo layout.

Before the 2026-07-25 restructure every module sat at the repo root and computed its own
`HERE = Path(__file__).parent`, so "my directory" and "the repo root" were the same thing.
They aren't any more: the code moved to core/ and the config it reads moved to data/, so
those anchors are defined once here and imported everywhere else.

What stays at the repo ROOT (not in data/) and why:
  applied.json   local, gitignored, grows over time — state, not config
  out/           scraper output, force-committed by the daily GitHub Action
  resumes/       gitignored byte-cache of the real PDFs
Moving either of the first two would break the Action and existing local files
for no gain, so only the hand-edited config files live in data/.

DATA can be pointed elsewhere with the JOBCOPILOT_DATA environment variable. Unset (the
default) means `<repo>/data`, which is the layout every other module and the extension
build already assume — so leaving it alone changes nothing. Setting it lets the config
that identifies a person (profile.json above all) live outside the checkout entirely,
which is what makes one copy of this code usable by someone else without their details
and yours sharing a directory.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Hand-edited config + the data layer (tracked in git unless relocated — see the module
# docstring). Expanded/resolved so a `~` or a relative override behaves like a real path.
_data_override = os.environ.get("JOBCOPILOT_DATA", "").strip()
DATA = Path(_data_override).expanduser().resolve() if _data_override else REPO_ROOT / "data"
def _config(name: str, example: str) -> Path:
    """The real config file, falling back to its shipped .example template.

    The three files holding personal data (profile, companies, answer bank) are gitignored
    in the public repo and only the templates are tracked, so a fresh clone has no
    profile.json at all. Without this fallback the first command anyone runs dies on a
    FileNotFoundError traceback, which is a miserable way to meet a tool.

    Falling back keeps a fresh clone working end to end — the scraper runs against the
    example companies, the resolver loads, the tests pass — while `python -m core.profile
    checkfill` still reports every FILL_ME the user owes. The notice goes to stderr so it
    is visible without corrupting piped stdout. Once the real file exists this is inert,
    which is why it changes nothing for an existing setup.
    """
    real = DATA / name
    if real.exists():
        return real
    ex = DATA / example
    if ex.exists():
        print(f"[paths] {name} not found, using {example}. Copy it to {name} and fill it "
              f"in before applying to anything real.", file=sys.stderr)
        return ex
    return real          # neither exists: let the caller raise the normal error


# READ paths: may resolve to the shipped template on a fresh install (see _config).
PROFILE = _config("profile.json", "profile.example.json")
TAXONOMY = DATA / "taxonomy.yaml"
COMPANIES = _config("companies.yaml", "companies.example.yaml")
FILTERS = DATA / "filters.yaml"
ANSWER_BANK = _config("answer_bank.yaml", "answer_bank.example.yaml")

# WRITE paths: ALWAYS the real filename, never the template — the read paths above may
# point at a .example on a fresh install, and saving through one would (a) overwrite the
# shipped template with somebody's real details and (b) put those details in a file
# .gitignore does NOT cover, since it only lists the real names. That is precisely the
# leak this project exists to prevent, so anything that persists config must use these.
PROFILE_WRITE = DATA / "profile.json"
COMPANIES_WRITE = DATA / "companies.yaml"
ANSWER_BANK_WRITE = DATA / "answer_bank.yaml"


def is_configured() -> bool:
    """True once the user has their own profile, rather than reading the shipped template.

    The single question a first-run flow needs answered, kept here because paths.py is the
    only module that knows the template fallback exists.
    """
    return PROFILE_WRITE.exists()


# Generated / local state (gitignored, or written by the daily Action).
OUT = REPO_ROOT / "out"
QUEUE = OUT / "queue.json"
APPLIED = REPO_ROOT / "applied.json"
RESUMES = REPO_ROOT / "resumes"
