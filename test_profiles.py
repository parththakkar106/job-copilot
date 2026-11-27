"""Check that a profile switch re-points every path the loaded modules copied.

Runs the switch in a child process against a temporary JOBCOPILOT_HOME, so the real
profiles and active_profile file are never touched.

    python test_profiles.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent

CHILD = r'''
import sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/bridge"]
from core import paths
import pending, triage, prefs, drafter
from core import bank, profile, apply_log, resumes
from web.app import paths as web_paths

base = paths.BASE
assert paths.ACTIVE == "" and pending.PENDING_DIR == base / "pending"
assert drafter.NAME == "Main Person", drafter.NAME

paths.create_profile("alex")
paths.set_active("alex")
assert paths.sync()
alex = base / "profiles" / "alex"

# Nothing a repo module holds may still point into the main profile's folder.
stale = []
for mod in list(sys.modules.values()):
    f = getattr(mod, "__file__", None) or ""
    if not f.startswith(sys.argv[1]) or ".venv" in f:
        continue
    for name, val in vars(mod).items():
        if mod is paths and name in paths._FIXED:
            continue
        if (isinstance(val, Path) and val.is_relative_to(base)
                and not val.is_relative_to(alex) and val != paths.ACTIVE_FILE
                and val.name not in ("filters.yaml", "taxonomy.yaml")):
            stale.append(f"{mod.__name__}.{name}={val}")
assert not stale, stale

assert pending.PENDING_DIR == alex / "pending"
assert triage.APPLIED_PATH == alex / "applied.json"
# Alex has no answer bank yet: reads fall back to the template, writes go to Alex.
assert bank.BANK_WRITE == alex / "answer_bank.yaml"
assert bank.BANK.name == "answer_bank.example.yaml", bank.BANK
assert web_paths.PROFILE_WRITE == alex / "profile.json"
assert web_paths.PROFILE_PATH.name == "profile.example.json"
assert drafter.NAME != "Main Person"

paths.set_active("")
assert paths.sync()
assert pending.PENDING_DIR == base / "pending"
assert bank.BANK == bank.BANK_WRITE == base / "answer_bank.yaml"
assert drafter.NAME == "Main Person"
print("ok")
'''


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "home"
        base.mkdir()
        (base / "profile.json").write_text(json.dumps(
            {"identity": {"first_name": "Main", "full_name": "Main Person"}}), encoding="utf-8")
        (base / "answer_bank.yaml").write_text("answers: []\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "JOBCOPILOT_DATA"}
        env["JOBCOPILOT_HOME"] = str(base)
        r = subprocess.run([sys.executable, "-c", CHILD, str(REPO)], env=env,
                           capture_output=True, text=True, cwd=td)
        print(r.stdout.strip())
        if r.returncode:
            print(r.stderr[-3000:])
        return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
