#!/usr/bin/env python3
"""Tests for the web console's JSON API.

Runs entirely on an isolated temp copy of the data layer — every module path (pending
dir, applied.json, companies.yaml, answer_bank.yaml, profile.json, queue.json) is pointed
at a throwaway directory, so the real pipeline state is never touched. No bridge, no
browser: the run-trigger endpoints are exercised only for their guard behaviour.

    python web/test_api.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from web.app import services as svc  # noqa: E402
from web.app.main import app  # noqa: E402
# paths.py (imported above) puts bridge/ on sys.path, so `prefs` is importable after it.
from web.app.paths import bank_mod, drafter_mod, pending_mod, triage_mod  # noqa: E402

import prefs  # noqa: E402  (bridge/prefs.py)

from server.store import use_temp_store  # noqa: E402
from server.store import importer  # noqa: E402


def _isolate(tmp: Path) -> None:
    """Seed a throwaway store from files written into tmp, and point the process at it.

    The config is copied via core.paths rather than by recomputing REPO_ROOT/"data"/... —
    that shortcut is the habit paths.py exists to kill, and it broke a fresh clone, where
    the real config files are gitignored and only the .example templates are present.

    The fixtures stay files and go in through the importer, so this exercises the same path
    a real install crosses over on.
    """
    from core import paths as core_paths
    (tmp / "pending").mkdir()
    (tmp / "out").mkdir()
    shutil.copy(core_paths.COMPANIES, tmp / "companies.yaml")
    shutil.copy(core_paths.ANSWER_BANK, tmp / "answer_bank.yaml")
    shutil.copy(core_paths.PROFILE, tmp / "profile.json")
    (tmp / "applied.json").write_text(json.dumps([
        {"dedup_key": "appl1", "company": "Neon", "title": "SWE",
         "location": "NYC", "url": "https://x", "status": "applied", "applied": "2026-07-20"},
    ]), encoding="utf-8")
    (tmp / "out" / "queue.json").write_text(json.dumps([
        {"dedup_key": "qk1", "company": "Asana", "title": "Backend", "tier": "pipeline"},
    ]), encoding="utf-8")
    (tmp / "out" / "dream.json").write_text(json.dumps([
        {"dedup_key": "dk1", "company": "Citadel", "title": "Quant Researcher",
         "location": "NYC", "url": "https://c", "posted": "2026-07-22", "tier": "dream"},
    ]), encoding="utf-8")
    # A role on a board with no adapter. Its `ats` is exactly what the scraper writes for
    # one, and the tier is plain pipeline - what makes it manual is the BOARD, not the
    # company, which is the whole distinction manual.json exists to keep separate.
    (tmp / "out" / "manual.json").write_text(json.dumps([
        {"dedup_key": "mk1", "company": "Apple", "title": "ML Engineer",
         "location": "Cupertino, CA", "url": "https://jobs.apple.com/x",
         "posted": "2026-07-23", "tier": "pipeline", "ats": "other"},
    ]), encoding="utf-8")

    drafter_mod._ENABLED = None                  # production path: read from prefs
    store = use_temp_store(tmp / "db")
    importer.import_profile(store, tmp / "profile.json")
    importer.import_companies(store, tmp / "companies.yaml")
    importer.import_answers(store, tmp / "answer_bank.yaml")
    importer.import_applied(store, tmp / "applied.json")
    importer.import_queues(store, tmp / "out")


def _seed_pending() -> None:
    pending_mod.put(
        {"dedup_key": "pk1", "company": "Notion", "title": "SWE", "location": "SF",
         "url": "https://n", "ats": "ashby"},
        [{"id": "q1", "label": "Why?", "kind": "textarea", "answer": None}],
        [{"label": "First name", "value": "Parth"}],
    )


def main() -> None:
    checks = 0
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _isolate(tmp)
        _seed_pending()
        c = TestClient(app)

        # 1. Dashboard merges pending (in_queue) + applied (submitted), newest first.
        r = c.get("/api/dashboard").json()
        buckets = {row["dedup_key"]: row["bucket"] for row in r["rows"]}
        assert buckets.get("pk1") == "in_queue", buckets; checks += 1
        assert buckets.get("appl1") == "submitted", buckets; checks += 1

        # 2. Company tier is attached to a pending row.
        pk = next(row for row in r["rows"] if row["dedup_key"] == "pk1")
        assert pk["tier"] in ("pipeline", "dream"), pk; checks += 1

        # 2b. Discovered dream roles surface as a manual-only 'dream' pile, tier=dream.
        dk = next((row for row in r["rows"] if row["dedup_key"] == "dk1"), None)
        assert dk and dk["bucket"] == "dream" and dk["tier"] == "dream", dk; checks += 1
        assert r["counts"].get("dream") == 1 and r["counts"].get("tier_dream") == 1, r["counts"]; checks += 1

        # 2b-ii. Roles on a board with no adapter get their OWN pile, not the dream one.
        # Folding them into dream would have been cheaper and would have quietly broken
        # what the Dream badge means, so the two are asserted apart on purpose.
        mk = next((row for row in r["rows"] if row["dedup_key"] == "mk1"), None)
        assert mk and mk["bucket"] == "manual", mk; checks += 1
        assert mk["tier"] == "pipeline", mk        # a by-hand board is not a dream company
        checks += 1
        assert r["counts"].get("manual") == 1, r["counts"]; checks += 1

        # 2c. Marking a dream role applied-by-hand moves it to submitted and out of the pile.
        assert c.post("/api/jobs/dk1/state", json={"target": "submitted",
                      "meta": {"company": "Citadel", "title": "Quant Researcher",
                               "location": "NYC", "url": "https://c"}}).json()["ok"]; checks += 1
        r2 = c.get("/api/dashboard").json()
        b2 = {row["dedup_key"]: row["bucket"] for row in r2["rows"]}
        assert b2.get("dk1") == "submitted", b2; checks += 1

        # 2d. Submitted -> Blocked is a CORRECTION of a role already in applied.json.
        # append_applied is idempotent on dedup_key, so routing this through it made the
        # move a silent no-op that still reported success. It must flip the status in place,
        # keep the original applied date, and land the row in the blocked bucket.
        assert c.post("/api/jobs/appl1/state", json={"target": "blocked"}).json()["ok"]; checks += 1
        rows3 = c.get("/api/dashboard").json()["rows"]
        a1 = next(row for row in rows3 if row["dedup_key"] == "appl1")
        assert a1["bucket"] == "blocked", a1; checks += 1
        assert a1["date"] == "2026-07-20", a1  # the decision changed, not when it left
        checks += 1
        assert sum(1 for row in rows3 if row["dedup_key"] == "appl1") == 1, "no duplicate row"
        checks += 1
        # ...and back again, so the correction is reversible in both directions.
        assert c.post("/api/jobs/appl1/state", json={"target": "submitted"}).json()["ok"]; checks += 1
        b3 = {row["dedup_key"]: row["bucket"] for row in c.get("/api/dashboard").json()["rows"]}
        assert b3.get("appl1") == "submitted", b3; checks += 1

        # 2e. Scraped-but-not-collected roles surface as a 'discovered' pile, and every
        # to-apply bucket rolls up into the count the default view is keyed on.
        assert b3.get("qk1") == "discovered", b3; checks += 1
        cnt = c.get("/api/dashboard").json()["counts"]
        assert cnt["to_apply"] == (cnt["discovered"] + cnt["in_queue"] + cnt["filled"]
                                   + cnt["dream"] + cnt["manual"]), cnt
        checks += 1
        # 2e-ii. A by-hand role is applied to in the tab, so the only move it offers is the
        # same applied-by-hand one dream gets - and it must leave the pile afterwards.
        assert c.post("/api/jobs/mk1/state", json={"target": "submitted",
                      "meta": {"company": "Apple", "title": "ML Engineer",
                               "location": "Cupertino, CA",
                               "url": "https://jobs.apple.com/x"}}).json()["ok"]; checks += 1
        b4 = {row["dedup_key"]: row["bucket"] for row in c.get("/api/dashboard").json()["rows"]}
        assert b4.get("mk1") == "submitted", b4; checks += 1
        assert c.get("/api/dashboard").json()["counts"]["manual"] == 0; checks += 1
        # ...and undo puts it back where it came from, reading manual.json again.
        assert c.post("/api/jobs/mk1/state", json={"target": "in_queue"}).json()["ok"]; checks += 1
        b5 = {row["dedup_key"]: row["bucket"] for row in c.get("/api/dashboard").json()["rows"]}
        assert b5.get("mk1") == "manual", b5; checks += 1
        # Counts describe exactly the rows returned, not a differently-windowed set.
        dash = c.get("/api/dashboard").json()
        assert dash["counts"]["all"] == len(dash["rows"]), (dash["counts"]["all"], len(dash["rows"]))
        checks += 1

        # 3. Job detail shows the open question; editing it flips _complete.
        j = c.get("/api/jobs/pk1").json()
        assert j["_complete"] is False, j; checks += 1
        j2 = c.put("/api/jobs/pk1/answers", json={"answers": [{"id": "q1", "value": "because"}]}).json()
        assert j2["_complete"] is True, j2; checks += 1

        # 4. Approve -> filled; a still-open job would be refused (guarded).
        assert c.post("/api/jobs/pk1/state", json={"target": "filled"}).json()["ok"]; checks += 1

        # 5. Resume override.
        rr = c.put("/api/jobs/pk1/resume", json={"tag": "tech"}).json()
        assert rr.get("resume_override") == "tech", rr; checks += 1

        # 5b. Leaving the queue must not destroy the record. The console moves piles on a
        # single keypress with no confirm, so Block archives instead of deleting and coming
        # back restores the archive — answers, resume pin and all.
        assert c.post("/api/jobs/pk1/state", json={"target": "blocked"}).json()["ok"]; checks += 1
        assert pending_mod.load("pk1") is None, "blocked record must leave the active batch"
        checks += 1
        kept = pending_mod.load_archived("pk1")
        assert kept and kept["questions"][0]["answer"] == "because", kept; checks += 1
        assert c.post("/api/jobs/pk1/state", json={"target": "in_queue"}).json()["ok"]; checks += 1
        back = pending_mod.load("pk1")
        assert back and back["status"] == "awaiting", back; checks += 1
        assert back["questions"][0]["answer"] == "because", "answers survive the round trip"
        checks += 1
        assert back.get("resume_override") == "tech", back; checks += 1
        # It's back in the queue and no longer counted as blocked.
        b5 = {row["dedup_key"]: row["bucket"] for row in c.get("/api/dashboard").json()["rows"]}
        assert b5.get("pk1") == "in_queue", b5; checks += 1

        # 6. Answer bank round-trip: upsert a new row, then delete it.
        before = len(c.get("/api/bank").json()["answers"])
        up = c.put("/api/bank", json={"key": "why_company", "scope": "company",
                                      "company": "TestCo", "answer": "test", "status": "active"}).json()
        assert up["id"], up; checks += 1
        after = len(c.get("/api/bank").json()["answers"])
        assert after == before + 1, (before, after); checks += 1
        assert c.delete(f"/api/bank/{up['id']}").json()["ok"]; checks += 1

        # 7. Company tier toggle preserves the file's comments (line-edit, not re-dump).
        raw_before = (tmp / "companies.yaml").read_text(encoding="utf-8")
        assert c.put("/api/companies/tier", json={"name": "Abridge", "tier": "dream"}).status_code == 200; checks += 1
        raw_after = (tmp / "companies.yaml").read_text(encoding="utf-8")
        # Compare the actual comment lines rather than looking for one specific header —
        # this is the real invariant (a line-edit, not a yaml re-dump), and it holds for
        # any companies.yaml instead of only the one this repo happens to ship.
        comments = lambda s: [l for l in s.splitlines() if l.lstrip().startswith("#")]
        assert comments(raw_after) == comments(raw_before), "comments must survive a tier edit"; checks += 1
        assert raw_after.count("\n") == raw_before.count("\n"), "line-edit must not add/remove lines"; checks += 1
        cos = {x["name"]: x["tier"] for x in c.get("/api/companies").json()["companies"]}
        assert cos.get("Abridge") == "dream", cos.get("Abridge"); checks += 1

        # 8. Profile read is the real object — compared against the file on disk rather
        # than a hardcoded name, so this holds for whoever's profile.json is loaded.
        prof = c.get("/api/profile").json()
        on_disk = json.loads((tmp / "profile.json").read_text(encoding="utf-8"))
        assert prof.get("identity") == on_disk.get("identity"), prof.get("identity"); checks += 1
        assert "first_name" in prof.get("identity", {}), prof.get("identity"); checks += 1

        # 8c. Setup checklist reports every step and a ready flag.
        st = c.get("/api/setup/status").json()
        assert st["total"] == 6 and len(st["steps"]) == 6, st; checks += 1
        # ORDER, not just membership: the extension is the product, so it comes second -
        # right after the details that feed it, and ahead of everything optional.
        assert [s["id"] for s in st["steps"]] == [
            "profile", "extension", "companies", "resumes", "telegram",
            "verify_code"], st; checks += 1
        assert isinstance(st["ready"], bool) and "blocking_left" in st, st; checks += 1
        # profile is the only blocking step, and it exists here, so nothing blocks.
        assert st["ready"] and st["blocking_left"] == [], st; checks += 1
        # The steps no script can do must be flagged, or the page would imply the install
        # finishes itself. Nothing here asserts whether they are DONE: the credential steps
        # read the real environment and bridge/.env, so a machine that has them configured
        # would fail an assertion written against a fresh clone.
        manual = {s["id"] for s in st["steps"] if s["manual"]}
        assert manual == {"extension", "telegram", "verify_code"}, manual; checks += 1
        # Optional means optional: only the profile may ever block, and the two add-ons
        # are flagged so the page can group them out of the main path.
        assert [s["id"] for s in st["steps"] if s["blocking"]] == ["profile"], st; checks += 1
        assert [s["id"] for s in st["steps"] if s["optional"]] == [
            "telegram", "verify_code"], st; checks += 1
        # The progress bar counts core steps only - an install that declines both extras is
        # finished, and must not read as 4 of 6 forever.
        assert st["core_total"] == 4 and st["core_done"] <= 4, st; checks += 1
        assert st["core_done"] == sum(
            1 for s in st["steps"] if not s["optional"] and s["done"]), st; checks += 1

        # 8b. Drafting flag: defaults ON, PUT flips it and persists to the shared prefs.
        assert c.get("/api/prefs/drafts").json()["enabled"] is True; checks += 1
        off = c.put("/api/prefs/drafts", json={"enabled": False}).json()
        assert off["enabled"] is False and "model" in off, off; checks += 1
        assert prefs.get("drafts_enabled") is False, "flag must persist to prefs file"; checks += 1
        assert c.put("/api/prefs/drafts", json={"enabled": True}).json()["enabled"] is True; checks += 1

        # 9. Run endpoints guard on bridge liveness (bridge not required for this test).
        alive = svc.bridge_alive()
        rc = c.post("/api/run/collect")
        if alive:
            assert rc.status_code == 200, rc.text
        else:
            assert rc.status_code == 409, rc.text
        checks += 1

    print(f"web_api: {checks}/{checks} checks passed")


if __name__ == "__main__":
    main()
