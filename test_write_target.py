#!/usr/bin/env python3
"""Saving personal details must never land on a .example template.

On a fresh install the READ paths fall back to the shipped templates so nothing crashes.
That fallback made saving dangerous: the console's Profile form wrote through the same
path, so a new user's first save overwrote profile.example.json -- a TRACKED file that
.gitignore does not cover, because it only lists the real names. Their address and phone
would then be one `git add -A` from being published.

Saves now go to the store, so no write can reach a template at all. The paths half of this
still matters: the importer reads through those same fallbacks, and it is what decides
whether an install counts as configured or as still holding the shipped example.
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

    templates = {n: (data / n).read_text(encoding="utf-8")
                 for n in ("profile.example.json", "companies.example.yaml",
                           "answer_bank.example.yaml")}

    from server.store import use_temp_store
    store = use_temp_store(Path(d) / "store")

    from server.store import importer
    importer.import_all(store)
    check("a fresh install imports the template, and says so",
          store.is_own(importer.PROFILE_DOC) is False)

    from web.app import services as svc
    importlib.reload(svc)
    svc.save_profile({"identity": {"first_name": "Test", "phone": "555-000-1111"}})

    check("saved details are in the store",
          store.get_profile().get("identity", {}).get("phone") == "555-000-1111")
    for n, before in templates.items():
        check(f"{n} is byte-identical after a save",
              (data / n).read_text(encoding="utf-8") == before)
    check("no personal detail reached any template",
          not any("555-000-1111" in (data / n).read_text(encoding="utf-8") for n in templates))
    check("no real config file was created either",
          not (data / "profile.json").exists())

    # The exported file is what a text-editor workflow reads. It must carry the save
    # rather than replaying the template text that was imported.
    from server.store import exporter
    check("export renders the saved profile, not the imported template",
          "555-000-1111" in exporter.export_profile(store))

print(f"\nwrite-target: {ok}/{ok+fail} checks passed")
sys.exit(1 if fail else 0)
