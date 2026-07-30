#!/usr/bin/env python3
"""Writes must never land on a .example template.

On a fresh install the READ paths fall back to the shipped templates so nothing crashes.
That fallback made saving dangerous: the console's Profile form wrote through the same
path, so a new user's first save overwrote profile.example.json -- a TRACKED file that
.gitignore does not cover, because it only lists the real names. Their address and phone
would then be one `git add -A` from being published.
"""
import importlib, json, os, shutil, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

ok = fail = 0
def check(name, cond):
    global ok, fail
    print(("PASS " if cond else "FAIL ") + name)
    ok, fail = ok + (1 if cond else 0), fail + (0 if cond else 1)

with tempfile.TemporaryDirectory() as d:
    data = Path(d) / "data"; data.mkdir()
    # A fresh install: templates only, no real config.
    for n in ("profile.example.json", "companies.example.yaml", "answer_bank.example.yaml"):
        shutil.copy(REPO / "data" / n, data / n)
    shutil.copy(REPO / "data" / "taxonomy.yaml", data / "taxonomy.yaml")
    shutil.copy(REPO / "data" / "filters.yaml", data / "filters.yaml")

    os.environ["JOBCOPILOT_DATA"] = str(data)
    from core import paths
    importlib.reload(paths)

    check("fresh install is not 'configured'", paths.is_configured() is False)
    check("READ falls back to the template", paths.PROFILE.name == "profile.example.json")
    check("WRITE targets the real name", paths.PROFILE_WRITE.name == "profile.json")
    check("read and write differ on a fresh install", paths.PROFILE != paths.PROFILE_WRITE)
    for label, r, w in (("companies", paths.COMPANIES, paths.COMPANIES_WRITE),
                        ("answer bank", paths.ANSWER_BANK, paths.ANSWER_BANK_WRITE)):
        check(f"{label}: write is the real name", w.name == r.name.replace(".example", ""))

    before = (data / "profile.example.json").read_text(encoding="utf-8")

    from web.app import services as svc
    importlib.reload(svc)
    svc.PROFILE_WRITE = paths.PROFILE_WRITE
    svc.save_profile({"identity": {"first_name": "Test", "phone": "555-000-1111"}})

    check("template is byte-identical after a save",
          (data / "profile.example.json").read_text(encoding="utf-8") == before)
    check("the real profile.json was created", (data / "profile.json").exists())
    saved = json.loads((data / "profile.json").read_text(encoding="utf-8"))
    check("saved data landed in the real file",
          saved.get("identity", {}).get("phone") == "555-000-1111")
    check("no personal data reached the template", "555-000-1111" not in before
          and "555-000-1111" not in (data / "profile.example.json").read_text(encoding="utf-8"))

    importlib.reload(paths)
    check("install is 'configured' once the real file exists", paths.is_configured() is True)
    check("READ now prefers the real file", paths.PROFILE.name == "profile.json")

print(f"\nwrite-target: {ok}/{ok+fail} checks passed")
sys.exit(1 if fail else 0)
