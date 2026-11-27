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

# PROFILES: several people, one folder each. The folder JOBCOPILOT_HOME names is BASE and
# holds the first person's files where they always were. Every other person gets
# BASE/profiles/<slug>/ with the same layout. ACTIVE_FILE names the selected slug, and an
# empty slug means BASE. HOME then points at the selected folder, so every path below
# follows the selected person.
#
# Profiles need JOBCOPILOT_HOME. If JOBCOPILOT_DATA is set (a test or a throwaway console),
# or if the checkout sits inside HOME, profiles are off and HOME means what it always did.
BASE = HOME
PROFILES_DIR = ACTIVE_FILE = None
if BASE and not _legacy_data and not REPO_ROOT.is_relative_to(BASE):
    PROFILES_DIR = BASE / "profiles"
    ACTIVE_FILE = BASE / "active_profile"
_SLUG_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def read_active() -> str:
    """The selected profile's slug from ACTIVE_FILE, or "" for BASE.

    If the file names a slug whose folder is gone, this returns "" so nothing is written
    into a folder that nobody set up.
    """
    if ACTIVE_FILE is None or not ACTIVE_FILE.exists():
        return ""
    slug = ACTIVE_FILE.read_text(encoding="utf-8").strip()
    if slug and (set(slug) - _SLUG_CHARS or not (PROFILES_DIR / slug).is_dir()):
        print(f"[paths] active profile {slug!r} has no folder, using the main one",
              file=sys.stderr)
        return ""
    return slug


ACTIVE = read_active()
if ACTIVE:
    HOME = PROFILES_DIR / ACTIVE

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
    # Every profile shares the main profile's tuning, so check BASE before the default.
    if BASE and (BASE / name).exists():
        return BASE / name
    return REPO_ROOT / "data" / name


# READ paths: may resolve to the shipped template on a fresh install (see _config).
PROFILE = _config("profile.json", "profile.example.json")
TAXONOMY = _tuning("taxonomy.yaml")
COMPANIES = _config("companies.yaml", "companies.example.yaml")
FILTERS = _tuning("filters.yaml")
ANSWER_BANK = _config("answer_bank.yaml", "answer_bank.example.yaml")
REFERRAL_TEMPLATES = _config("referral_templates.yaml", "referral_templates.example.yaml")

# WRITE paths: ALWAYS the real filename, never the template — the read paths above may
# point at a .example on a fresh install, and saving through one would (a) overwrite the
# shipped template with somebody's real details and (b) put those details in a file
# .gitignore does NOT cover, since it only lists the real names. That is precisely the
# leak this project exists to prevent, so anything that persists config must use these.
PROFILE_WRITE = DATA / "profile.json"
COMPANIES_WRITE = DATA / "companies.yaml"
ANSWER_BANK_WRITE = DATA / "answer_bank.yaml"
# The topics and levels you chose on the Search page. It has no template: without it, the
# scraper searches the defaults marked in filters.yaml.
SEARCH = DATA / "search.yaml"


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
# Roles parked while Parth waits on a referral, with when each was parked and asked about.
REFERRAL_WAIT = STATE / "referral_wait.json"
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
# Dropped by the web console on every edit or approval. The bridge treats it as
# "Parth is still working" and restarts the idle countdown, so approving a job on the
# dashboard no longer lets the idle-watcher send the pile seconds later.
ACTIVITY_PING = REPO_ROOT / "bridge" / ".activity"
# One JSON file per link the console queued. One file per link means the console and the
# bridge never rewrite the same file, so neither can drop the other's change.
LINK_QUEUE = REPO_ROOT / "bridge" / ".link_queue"
# The bridge writes the slug it serves here, so the console can show when the bridge has
# not caught up with a switch yet.
BRIDGE_PROFILE = REPO_ROOT / "bridge" / ".bridge_profile"


# ── Profiles ──────────────────────────────────────────────────────────────────────────
#
# Switching re-runs this module for the new folder, then re-points every Path that other
# loaded modules copied from it, such as `pending.PENDING_DIR` or a `path=PROFILE` default.
# That keeps the ~20 modules that copy paths at import unchanged, and keeps tests that
# rebind those copies working.

# Paths that do not depend on the selected profile. They are never re-pointed.
_FIXED = {"REPO_ROOT", "BASE", "PROFILES_DIR", "ACTIVE_FILE", "BRIDGE_PROFILE"}


def profile_dir(slug: str) -> Path:
    return BASE if not slug else PROFILES_DIR / slug


def list_profiles() -> list[str]:
    """Every profile's slug, the main one ("") first. Only [""] when profiles are off."""
    if PROFILES_DIR is None or not PROFILES_DIR.is_dir():
        return [""]
    return ["", *sorted(p.name for p in PROFILES_DIR.iterdir()
                        if p.is_dir() and not set(p.name) - _SLUG_CHARS)]


def create_profile(slug: str) -> Path:
    """Make an empty folder for a new person. Setup then walks them through filling it."""
    if PROFILES_DIR is None:
        raise ValueError("profiles need JOBCOPILOT_HOME")
    if not slug or len(slug) > 30 or set(slug) - _SLUG_CHARS or slug.startswith("-"):
        raise ValueError("use 1 to 30 lowercase letters, digits, or dashes")
    d = PROFILES_DIR / slug
    if d.exists():
        raise ValueError(f"a profile named {slug!r} already exists")
    d.mkdir(parents=True)
    return d


def set_active(slug: str) -> None:
    """Select a profile for every process. Each process applies it on its next sync()."""
    if ACTIVE_FILE is None:
        raise ValueError("profiles need JOBCOPILOT_HOME")
    if slug not in list_profiles():
        raise ValueError(f"no profile named {slug!r}")
    tmp = ACTIVE_FILE.with_suffix(".tmp")
    tmp.write_text(slug, encoding="utf-8")
    os.replace(tmp, ACTIVE_FILE)


def pending_switch() -> bool:
    """True if another process selected a profile this process does not serve yet."""
    return ACTIVE_FILE is not None and read_active() != ACTIVE


def sync() -> bool:
    """Apply the selected profile if it changed. Returns True if this process switched.

    shortcut: no lock, so a request already running during a switch can finish on the old
    paths. Add a read/write lock around requests if two people ever use one console at once.
    """
    if not pending_switch():
        return False
    _switch()
    return True


def _switch() -> None:
    import importlib
    from types import FunctionType

    me = sys.modules[__name__]
    old = {k: v for k, v in vars(me).items()
           if k.isupper() and isinstance(v, Path) and k not in _FIXED}
    old_home = HOME
    importlib.reload(me)          # re-runs this file, so every name now follows the new HOME

    # Old value -> {name: new value}. Two names share an old value when the old profile had
    # its own file for both a READ and a WRITE name, such as PROFILE and PROFILE_WRITE. If the
    # new profile has no such file yet, the two new values differ, so the copy's own name
    # decides: a name containing WRITE gets the WRITE path.
    by_old: dict[Path, dict[str, Path]] = {}
    for k, v in old.items():
        by_old.setdefault(v, {})[k] = globals()[k]

    def new_for(v: Path, name: str = "") -> Path | None:
        cands = by_old.get(v)
        if cands:
            if len(set(cands.values())) == 1:
                return next(iter(cands.values()))
            want_write = "WRITE" in name
            return next((nv for k, nv in cands.items() if ("WRITE" in k) == want_write),
                        next(iter(cands.values())))
        # A path built from a profile path, such as LOGS / "bridge.log".
        if (old_home and v.is_relative_to(old_home)
                and not (PROFILES_DIR and v.is_relative_to(PROFILES_DIR) and old_home == BASE)):
            return HOME / v.relative_to(old_home)
        return None

    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if mod is me or not f:
            continue
        fp = Path(f).resolve()
        if not fp.is_relative_to(REPO_ROOT) or {".venv", "node_modules"} & set(fp.parts):
            continue
        g = vars(mod)
        for name, val in list(g.items()):
            if isinstance(val, Path):
                nv = new_for(val, name)
                if nv is not None:
                    g[name] = nv
            elif (isinstance(val, FunctionType) and val.__module__ == mod.__name__
                  and val.__defaults__):
                val.__defaults__ = tuple(
                    (new_for(d) or d) if isinstance(d, Path) else d for d in val.__defaults__)
        # A module that loaded profile data at import reloads it here.
        hook = g.get("_on_profile_switch")
        if callable(hook):
            hook()
    print(f"[paths] switched to profile {ACTIVE or '(main)'} at {HOME}", file=sys.stderr)
