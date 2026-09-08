#!/usr/bin/env python3
"""Checks for the store: the round trip, the codecs, and the rules that guard the data.

Run it directly: `python server/test_store.py`. No test runner, no fixtures — the same
shape as the rest of the suite.

The round trip is the load-bearing one. An export that is not byte-identical to what was
imported means the store has quietly rewritten somebody's config, and the failure surfaces
the first time they open the file rather than when it happens.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.store import Store, rows_hash  # noqa: E402
from server.store import exporter, importer  # noqa: E402

ok = fail = 0


def check(name: str, cond) -> None:
    global ok, fail
    print(("PASS " if cond else "FAIL ") + name)
    ok, fail = ok + (1 if cond else 0), fail + (0 if cond else 1)


# A companies.yaml with everything that has broken a round trip: a header comment, section
# comments, a null slug on a Workday row, and a trailing block of off-board targets.
COMPANIES_YAML = """# Targets. Edit freely.
companies:

  # ---- GREENHOUSE ----
  - {name: Airbnb, ats: greenhouse, slug: airbnb, tier: pipeline}
  - {name: Asana, ats: greenhouse, slug: asana, tier: pipeline}

  # ---- WORKDAY (no slug: addressed by host + board) ----
  - {name: Bank of America, ats: workday, slug: null, tier: dream,
     host: ghr.wd1.myworkdayjobs.com, board: lateral-us}
  - {name: Adobe, ats: workday, slug: null, tier: dream,
     host: adobe.wd5.myworkdayjobs.com, board: external_experienced}

# Not on any supported board, kept here so they are not forgotten:
#   - Citadel
#   - Two Sigma
#   - DRW
"""

# `created` is unquoted, so YAML parses it as a date and JSON cannot hold it.
ANSWER_BANK_YAML = """# The bank. Only status: active is served.
answers:
  - id: why_role_default
    key: why_role
    scope: common
    company: null
    answer: I want an early-career role where the work is measured.
    status: active
    created: 2026-07-09
    last_used: null
    uses: 0
"""

PROFILE = {"identity": {"first_name": "Test", "phone": "FILL_ME"},
           "resume_tracks": [{"tag": "ai", "local_path": "resumes/ai.pdf",
                              "keywords": ["ml"]}]}


def seed(home: Path) -> None:
    (home / "companies.yaml").write_text(COMPANIES_YAML, encoding="utf-8")
    (home / "answer_bank.yaml").write_text(ANSWER_BANK_YAML, encoding="utf-8")
    (home / "profile.json").write_text(json.dumps(PROFILE, indent=2), encoding="utf-8")
    (home / "applied.json").write_text(json.dumps(
        [{"dedup_key": "k1", "company": "Neon", "title": "SWE", "url": "https://x",
          "status": "applied", "applied": "2026-07-20"}], indent=2), encoding="utf-8")


def test_round_trip(home: Path, store: Store) -> None:
    importer.import_companies(store, home / "companies.yaml")
    importer.import_answers(store, home / "answer_bank.yaml")
    importer.import_profile(store, home / "profile.json")
    importer.import_applied(store, home / "applied.json")

    check("every company row survived, null slugs included",
          [c.get("slug") for c in store.get_companies()] == ["airbnb", "asana", None, None])
    check("a Workday row keeps the host and board that identify it",
          store.get_companies()[2]["host"] == "ghr.wd1.myworkdayjobs.com")
    check("an unquoted YAML date comes back as a date, not a string",
          store.get_answers()[0]["created"] == date(2026, 7, 9))

    for name, src in ((importer.COMPANIES_DOC, "companies.yaml"),
                      (importer.ANSWER_BANK_DOC, "answer_bank.yaml"),
                      (importer.PROFILE_DOC, "profile.json")):
        fn = {importer.COMPANIES_DOC: exporter.export_companies,
              importer.ANSWER_BANK_DOC: exporter.export_answers,
              importer.PROFILE_DOC: exporter.export_profile}[name]
        check(f"{src} exports byte-identical to what was imported",
              fn(store) == (home / src).read_text(encoding="utf-8"))

    check("the exported companies keep the commented-out off-board targets",
          "#   - Two Sigma" in exporter.export_companies(store))
    check("applied exports byte-identical, trailing newline included",
          exporter.export_applied(store) == (home / "applied.json").read_text(encoding="utf-8"))


def test_edit_stops_the_verbatim_replay(store: Store) -> None:
    """Once a row changes, the kept text no longer describes it and must not be replayed."""
    rows = store.get_companies()
    rows[0]["tier"] = "dream"
    store.set_companies(rows)
    out = exporter.export_companies(store)
    check("an edited list is rendered, not replayed", "# ---- GREENHOUSE ----" not in out)
    check("the edit is in the rendered output", "tier: dream" in out)


def test_config_source(store: Store, home: Path) -> None:
    check("a real config file counts as the user's own",
          store.is_own(importer.PROFILE_DOC) is True)
    example = home / "profile.example.json"
    example.write_text(json.dumps(PROFILE), encoding="utf-8")
    importer.import_profile(store, example)
    check("a shipped template does not count as the user's own",
          store.is_own(importer.PROFILE_DOC) is False)


def test_pending_archive(store: Store) -> None:
    store.put_pending({"dedup_key": "p1", "status": "awaiting", "created": "2026-01-01"})
    check("a stored record reads back", store.get_pending("p1")["status"] == "awaiting")
    check("archiving takes it out of the active batch",
          store.archive_pending("p1") and store.all_pending() == [])
    check("an archived record keeps its answers",
          store.get_pending("p1", archived=True)["created"] == "2026-01-01")
    check("restoring puts it back", store.restore_pending("p1")
          and len(store.all_pending()) == 1)

    # Records written in the same second share a `created`, so the tie has to break on
    # insertion order. Sorting alone left it to whatever order the rows came back in.
    for key in ("p2", "p3", "p4"):
        store.put_pending({"dedup_key": key, "status": "awaiting", "created": "2026-02-02"})
    check("records sharing a timestamp list in the order they were stored",
          [r["dedup_key"] for r in store.all_pending()][1:] == ["p2", "p3", "p4"])


def test_applied_rules(store: Store) -> None:
    store.set_applied([{"dedup_key": "d1", "company": "A"}, {"dedup_key": "d1", "company": "B"}])
    rows = store.get_applied()
    check("a duplicate dedup_key collapses to one row, last wins",
          len(rows) == 1 and rows[0]["company"] == "B")
    store.add_applied({"dedup_key": "d2", "company": "C", "status": "rejected"})
    check("add_applied appends", len(store.get_applied()) == 2)
    store.add_applied({"dedup_key": "d2", "company": "C", "status": "applied"})
    check("adding a known key corrects it rather than appending",
          len(store.get_applied()) == 2
          and store.get_applied()[1]["status"] == "applied")


def test_prefs_and_resumes(store: Store) -> None:
    store.set_pref("drafts_enabled", True)
    store.set_pref("threshold", 0.5)
    prefs = store.get_prefs()
    check("a bool stays a bool", prefs["drafts_enabled"] is True)
    check("a float stays a float", prefs["threshold"] == 0.5)

    digest = store.put_resume("ai", b"%PDF-1.4 hello", filename="ai.pdf")
    check("the digest is of the bytes actually stored",
          store.get_resume("ai")["sha256"] == digest)
    check("a listing carries the size but not the bytes",
          store.all_resumes()[0]["size"] == 14 and "pdf" not in store.all_resumes()[0])
    store.put_resume("ai", None, label="AI track")
    check("updating metadata leaves the bytes alone",
          store.get_resume("ai")["pdf"] == b"%PDF-1.4 hello")


def test_user_isolation(tmp: Path) -> None:
    """Two users in one database must never see each other's rows.

    This is the check the shared Telegram bot depends on later: a mix-up sends one person's
    salary answer to another's phone.
    """
    url = f"sqlite+pysqlite:///{tmp / 'shared.db'}"
    a = Store.from_url(url, user_id=1)
    b = Store.from_url(url, user_id=2)
    a.set_profile({"identity": {"first_name": "A"}})
    b.set_profile({"identity": {"first_name": "B"}})
    a.put_pending({"dedup_key": "x", "status": "awaiting", "created": "2026-01-01"})
    check("each user reads their own profile",
          a.get_profile()["identity"]["first_name"] == "A"
          and b.get_profile()["identity"]["first_name"] == "B")
    check("one user's pending record is invisible to the other",
          len(a.all_pending()) == 1 and b.all_pending() == [])
    check("and invisible by key too", b.get_pending("x") is None)


def test_migration_runs(tmp: Path) -> None:
    """Every revision must apply to an empty database, and match what the models describe.

    `migrate.upgrade()` creates a fresh database from the models and stamps it, so it never
    runs the revisions — which is how a revision naming a custom column type it had not
    imported sat there working until the first real upgrade.
    """
    from alembic import command
    from sqlalchemy import inspect

    from server.store import engine as engine_mod
    from server.store import migrate
    from server.store.models import Base

    url = f"sqlite+pysqlite:///{tmp / 'migrated.db'}"
    command.upgrade(migrate.config(url), "head")
    got = set(inspect(engine_mod.make_engine(url)).get_table_names()) - {"alembic_version"}
    check("the revisions apply to an empty database", bool(got))
    check("and produce the tables the models describe", got == set(Base.metadata.tables))


def test_rows_hash() -> None:
    check("the hash ignores key order",
          rows_hash([{"a": 1, "b": 2}]) == rows_hash([{"b": 2, "a": 1}]))
    check("the hash notices a changed value",
          rows_hash([{"a": 1}]) != rows_hash([{"a": 2}]))


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        home = tmp / "home"
        home.mkdir()
        seed(home)
        store = Store.at(tmp / "db")
        test_round_trip(home, store)
        test_edit_stops_the_verbatim_replay(store)
        test_config_source(store, home)
        test_pending_archive(store)
        test_applied_rules(store)
        test_prefs_and_resumes(store)
        test_user_isolation(tmp)
        test_migration_runs(tmp)
        test_rows_hash()
    print(f"\nstore: {ok}/{ok + fail} checks passed")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
