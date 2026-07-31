"""Checks that JOBCOPILOT_HOME really does collect everything personal in one directory.

The whole point of a single variable is that the directory it names is portable: copy it
and you have moved the job search — identity, history, answers, drafts, resumes — with
nothing personal left behind in the checkout. That is only true if EVERY such path derives
from HOME, so this asserts the full set rather than spot-checking two of them. A new file
holding user data is expected to fail here until it is added to core/paths.py.

Paths are resolved in a subprocess because core/paths.py reads the environment once at
import, which is also how every real entry point sees it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent

# name -> is it personal? True must live under HOME; False must stay beside the code.
EXPECTED = {
    "PROFILE_WRITE": True,      # identity, address, work authorization, EEO answers
    "COMPANIES_WRITE": True,    # targets and a private ranking of them
    "ANSWER_BANK_WRITE": True,  # essays about real work
    "APPLIED": True,            # where this person actually applied
    "PREFS": True,              # runtime settings
    "RESUMES": True,            # the real PDFs
    "PENDING": True,            # in-flight answers and drafts
    "LOGS": True,               # fill reports quote real form values
    "FILL_REPORTS": True,
    "AUDIT_DIR": True,
    "AUDIT_INDEX": True,
    "OUT": True,                # a snapshot of who is being targeted
    "QUEUE": True,
    # Local IPC between two processes on one machine. Copying HOME elsewhere must not
    # carry a stale instruction to go and fill in a form.
    "RUN_TRIGGER": False,
    "FLUSH_TRIGGER": False,
}

PROBE = """
import json, sys
from core import paths
names = %r
print("@@" + json.dumps({n: str(getattr(paths, n)) for n in names}))
"""


def resolve(names: list[str], env_extra: dict[str, str]) -> dict[str, Path]:
    env = {**os.environ, "PYTHONPATH": str(REPO), **env_extra}
    for k, v in env_extra.items():
        if v is None:
            env.pop(k, None)
    r = subprocess.run([sys.executable, "-c", PROBE % names], cwd=REPO, env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"probe failed:\n{r.stderr}")
    line = next(ln for ln in r.stdout.splitlines() if ln.startswith("@@"))
    return {k: Path(v) for k, v in json.loads(line[2:]).items()}


def under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def main() -> int:
    checks = 0
    names = list(EXPECTED)

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "my-private-stuff"
        home.mkdir()
        got = resolve(names, {"JOBCOPILOT_HOME": str(home), "JOBCOPILOT_DATA": ""})

        for name, personal in EXPECTED.items():
            p = got[name]
            if personal:
                assert under(p, home), f"{name} is personal but resolved outside HOME: {p}"
                assert not under(p, REPO), f"{name} still points into the checkout: {p}"
            else:
                assert not under(p, home), f"{name} is not data but landed in HOME: {p}"
            checks += 1

        # The directory is portable only if it is also self-contained: no personal path may
        # sit above HOME either (a "../" escape would copy as a dangling reference).
        for name, personal in EXPECTED.items():
            if personal:
                assert ".." not in got[name].parts, f"{name} escapes HOME: {got[name]}"
                checks += 1

        # Tuning files fall back to the shipped defaults, so a fresh HOME still runs...
        tune = resolve(["FILTERS", "TAXONOMY"], {"JOBCOPILOT_HOME": str(home),
                                                 "JOBCOPILOT_DATA": ""})
        for n in ("FILTERS", "TAXONOMY"):
            assert under(tune[n], REPO), f"{n} should fall back to the shipped default"
            assert tune[n].exists(), f"the shipped {n} default must actually exist"
            checks += 2

        # ...but a copy in HOME wins, which is how they get personalised.
        (home / "filters.yaml").write_text("location:\n  us_only: false\n", encoding="utf-8")
        tune = resolve(["FILTERS", "TAXONOMY"], {"JOBCOPILOT_HOME": str(home),
                                                 "JOBCOPILOT_DATA": ""})
        assert under(tune["FILTERS"], home), "a filters.yaml in HOME must take precedence"
        assert under(tune["TAXONOMY"], REPO), "taxonomy.yaml was not overridden, so default"
        checks += 2

    # --- unset: the pre-split layout, so an existing checkout is untouched ---------------
    legacy = resolve(names + ["DATA"], {"JOBCOPILOT_HOME": "", "JOBCOPILOT_DATA": ""})
    assert legacy["DATA"] == REPO / "data", legacy["DATA"]
    assert legacy["APPLIED"] == REPO / "applied.json", legacy["APPLIED"]
    assert legacy["OUT"] == REPO / "out", legacy["OUT"]
    assert legacy["RESUMES"] == REPO / "resumes", legacy["RESUMES"]
    assert legacy["PENDING"] == REPO / "bridge" / "pending", legacy["PENDING"]
    assert legacy["LOGS"] == REPO / "bridge" / "logs", legacy["LOGS"]
    assert legacy["PREFS"] == REPO / "bridge" / "prefs.json", legacy["PREFS"]
    checks += 7

    # The older, narrower variable still means what it used to: config only, state at the
    # repo root. A machine that set it keeps the layout it already has.
    with tempfile.TemporaryDirectory() as tmp:
        old = resolve(["DATA", "APPLIED"], {"JOBCOPILOT_HOME": "", "JOBCOPILOT_DATA": tmp})
        assert old["DATA"] == Path(tmp).resolve(), old["DATA"]
        assert old["APPLIED"] == REPO / "applied.json", old["APPLIED"]
        checks += 2

    # With BOTH set the narrower one wins for config. Once HOME is set globally it is set
    # for every process on the machine, including tests and throwaway consoles that point
    # JOBCOPILOT_DATA at a temp directory on purpose; if HOME captured those, they would
    # quietly read and write the real files instead.
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        both = resolve(["DATA", "APPLIED", "PENDING"],
                       {"JOBCOPILOT_HOME": a, "JOBCOPILOT_DATA": b})
        assert both["DATA"] == Path(b).resolve(), f"explicit DATA must win: {both['DATA']}"
        # State still follows HOME: DATA has only ever meant the config directory.
        assert under(both["APPLIED"], Path(a)), both["APPLIED"]
        assert under(both["PENDING"], Path(a)), both["PENDING"]
        checks += 3

    # --- the bridge modules must agree with core.paths, never re-derive ------------------
    # triage.py used to compute its own repo root and applied.json; a test that redirected
    # one left the other pointing at the real file.
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "PYTHONPATH": str(REPO), "JOBCOPILOT_HOME": tmp,
               "JOBCOPILOT_DATA": ""}
        src = (
            "import json,sys\n"
            "sys.path.insert(0, 'bridge')\n"
            "from core import paths\n"
            "import triage, pending, prefs\n"
            "print('@@'+json.dumps({\n"
            "  'applied': [str(triage.APPLIED_PATH), str(paths.APPLIED)],\n"
            "  'queue':   [str(triage.QUEUE_PATH),   str(paths.QUEUE)],\n"
            "  'pending': [str(pending.PENDING_DIR), str(paths.PENDING)],\n"
            "  'prefs':   [str(prefs.PREFS_PATH),    str(paths.PREFS)],\n"
            "}))\n"
        )
        r = subprocess.run([sys.executable, "-c", src], cwd=REPO, env=env,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        pairs = json.loads(next(l for l in r.stdout.splitlines() if l.startswith("@@"))[2:])
        for key, (theirs, ours) in pairs.items():
            assert theirs == ours, f"bridge {key} disagrees with core.paths: {theirs} != {ours}"
            assert under(Path(theirs), Path(tmp)), f"bridge {key} ignored HOME: {theirs}"
            checks += 2

    print(f"home layout: {checks}/{checks} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
