"""Where everything lives on disk — the one place that knows the repo layout.

Before the 2026-07-25 restructure every module sat at the repo root and computed its own
`HERE = Path(__file__).parent`, so "my directory" and "the repo root" were the same thing.
They aren't any more: the code moved to core/ and the config it reads moved to data/, so
those anchors are defined once here and imported everywhere else.

ONE ENVIRONMENT VARIABLE: JOBCOPILOT_HOME
-----------------------------------------
Everything that identifies a person or records what they did lives under a single
directory, and that directory is named once:

    JOBCOPILOT_HOME=/somewhere/private

    <HOME>/profile.json        identity, work authorization, EEO answers
    <HOME>/companies.yaml      targets, and a private tier ranking of them
    <HOME>/answer_bank.yaml    essay answers about real work
    <HOME>/applied.json        where this person has actually applied
    <HOME>/prefs.json          runtime settings
    <HOME>/resumes/            the real PDFs
    <HOME>/pending/            in-flight applications: typed answers and drafts
    <HOME>/logs/               fill reports and audit dumps, full of form values
    <HOME>/out/                scrape results, a snapshot of who is being targeted

The point of one variable rather than several is that the directory is then a single
portable object: copy it and you have moved the whole job search, answers and history
included. Nothing personal is left behind in the checkout to be found later.

Unset (the default) means `<repo>/data` for config and `<repo>` for state, which is the
pre-split layout — so a plain clone still works with nothing configured, and so does a
setup that predates this variable.

WHAT IS *NOT* PERSONAL, and therefore stays with the code:
  filters.yaml, taxonomy.yaml   tuning, not identity. They ship as working defaults so a
                                fresh clone runs; a copy in HOME wins if you make one.
  bridge/.run_trigger etc.      one local process poking another, not data.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_dir(name: str) -> Path | None:
    """A directory named by an env var, or None. `~` and relative paths behave normally."""
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser().resolve() if raw else None


# The single anchor. JOBCOPILOT_DATA is the older, narrower name from when only the config
# files moved; it still means exactly what it meant then — "the config directory" — so a
# machine that set it keeps the layout it already has.
HOME = _env_dir("JOBCOPILOT_HOME")
_legacy_data = _env_dir("JOBCOPILOT_DATA")

# Hand-edited config. Under HOME the files sit directly in it (the whole directory is the
# private one, so a data/ subdirectory would only add a level); without either variable
# they stay in the repo's data/ where they have always been.
#
# The NARROWER variable wins. Anything that needs config somewhere specific — a test
# isolating itself, a second console on a throwaway data layer — says so with
# JOBCOPILOT_DATA, and that has to keep working on a machine where HOME is set globally,
# which is every machine once somebody adopts it. Precedence the other way round meant an
# ambient HOME silently captured tests that had carefully pointed themselves elsewhere.
DATA = _legacy_data or HOME or REPO_ROOT / "data"

# Generated and local state. Follows HOME when set, otherwise the repo root — which is
# where it has always lived, so an existing checkout is unaffected.
STATE = HOME or REPO_ROOT
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
    # The templates ship with the CODE, so on a fresh JOBCOPILOT_HOME — an empty directory
    # by definition, which is how everyone's starts — they are not in DATA at all. Look
    # beside them first anyway, so a HOME that keeps its own copies is still honoured.
    ex = DATA / example
    if not ex.exists():
        ex = REPO_ROOT / "data" / example
    if ex.exists():
        print(f"[paths] {name} not found, using {example}. Copy it to {name} and fill it "
              f"in before applying to anything real.", file=sys.stderr)
        return ex
    return real          # neither exists: let the caller raise the normal error


def _tuning(name: str) -> Path:
    """A tuning file: yours from HOME if you made one, else the default shipped with the code.

    filters.yaml and taxonomy.yaml are keyword lists and bucketing rules. They are not
    identity, so shipping working ones is what lets a fresh clone actually run, and denying
    them would leave a stranger with an empty scraper. But they are also the files most
    worth personalising, so a copy in HOME takes precedence over the shipped default.
    """
    mine = DATA / name
    if mine.exists():
        return mine
    return REPO_ROOT / "data" / name


# READ paths: may resolve to the shipped template on a fresh install (see _config).
PROFILE = _config("profile.json", "profile.example.json")
TAXONOMY = _tuning("taxonomy.yaml")
COMPANIES = _config("companies.yaml", "companies.example.yaml")
FILTERS = _tuning("filters.yaml")
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


# Generated / local state. Every one of these is a record of a real job search — where
# somebody applied, what they typed, which resume went out — so they follow HOME rather
# than sitting in the checkout. Defined here and imported, never re-derived: bridge/triage
# once computed its own repo root and its own applied.json path, and a test that pointed
# one of them somewhere safe wrote to the real file through the other.
OUT = STATE / "out"
QUEUE = OUT / "queue.json"
# The dream pile: roles the auto-apply loop deliberately skips, to be approached by hand or
# via a referral. Same shape as queue.json and just as much a snapshot of who is being
# targeted, so it follows HOME for the same reason. Named here because the referral flow
# reads it alongside the queue — a dream row is the best thing to put in front of a contact.
DREAM = OUT / "dream.json"
APPLIED = STATE / "applied.json"
# Outreach contacts: OTHER PEOPLE's names, headlines and profile URLs, gathered from public
# search results. Everything else under HOME is Parth's own data; this is the one file that
# is somebody else's, which makes it the most important one to keep out of the checkout.
OUTREACH = STATE / "outreach.json"
RESUMES = STATE / "resumes"
# Under HOME these sit together at the top level; without it they keep the exact places
# they have always occupied under bridge/, so an existing checkout finds its own files.
PREFS = STATE / "prefs.json" if HOME else REPO_ROOT / "bridge" / "prefs.json"
PENDING = STATE / "pending" if HOME else REPO_ROOT / "bridge" / "pending"
LOGS = STATE / "logs" if HOME else REPO_ROOT / "bridge" / "logs"
FILL_REPORTS = LOGS / "fill_reports.jsonl"
AUDIT_DIR = LOGS / "audit"
AUDIT_INDEX = LOGS / "audit_dumps.jsonl"

# Local IPC, not data: one process on this machine telling another to start. Always beside
# the code, never in HOME — copying your HOME to another machine should not carry a stale
# instruction to go and fill in a form.
RUN_TRIGGER = REPO_ROOT / "bridge" / ".run_trigger"
FLUSH_TRIGGER = REPO_ROOT / "bridge" / ".flush_trigger"
