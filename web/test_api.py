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
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from web.app import services as svc  # noqa: E402
from web.app.main import app  # noqa: E402
# paths.py (imported above) puts bridge/ on sys.path, so `prefs` is importable after it.
from web.app.paths import bank_mod, drafter_mod, pending_mod, triage_mod  # noqa: E402

import prefs  # noqa: E402  (bridge/prefs.py)


def _isolate(tmp: Path) -> None:
    """Point every module + service path at tmp, seeded from the real files.

    Seeded via core.paths rather than by recomputing REPO_ROOT/"data"/... — that shortcut
    is the habit paths.py exists to kill, and it broke a fresh clone, where the real
    config files are gitignored and only the .example templates are present.
    """
    from core import paths as core_paths
    (tmp / "pending").mkdir()
    (tmp / "out").mkdir()
    shutil.copy(core_paths.COMPANIES, tmp / "companies.yaml")
    shutil.copy(core_paths.ANSWER_BANK, tmp / "answer_bank.yaml")
    shutil.copy(core_paths.PROFILE, tmp / "profile.json")
    (tmp / "applied.json").write_text(json.dumps([
        {"dedup_key": "appl1", "company": "Neon", "title": "SWE",
         "location": "NYC", "url": "https://x", "status": "applied",
         # Inside the dashboard's 60-day window whatever day the suite runs.
         "applied": (date.today() - timedelta(days=5)).isoformat()},
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

    prefs.PREFS_PATH = tmp / "prefs.json"        # keep the real prefs.json untouched
    drafter_mod._ENABLED = None                  # production path: read from prefs
    pending_mod.PENDING_DIR = tmp / "pending"
    triage_mod.APPLIED_PATH = tmp / "applied.json"
    pending_mod.REFERRAL_WAIT_PATH = tmp / "referral_wait.json"
    triage_mod.QUEUE_PATH = tmp / "out" / "queue.json"
    bank_mod.BANK = tmp / "answer_bank.yaml"
    bank_mod.BANK_WRITE = tmp / "answer_bank.yaml"
    svc.COMPANIES_PATH = tmp / "companies.yaml"
    svc.COMPANIES_WRITE = tmp / "companies.yaml"
    svc.ANSWER_BANK_PATH = tmp / "answer_bank.yaml"
    svc.PROFILE_PATH = tmp / "profile.json"
    svc.PROFILE_WRITE = tmp / "profile.json"
    svc.SEARCH_PATH = tmp / "search.yaml"
    svc.FILTERS_PATH = core_paths.REPO_ROOT / "data" / "filters.yaml"   # the shipped catalog
    svc.QUEUE_PATH = tmp / "out" / "queue.json"
    svc.DREAM_PATH = tmp / "out" / "dream.json"
    svc.MANUAL_PATH = tmp / "out" / "manual.json"
    # Sentinel files too: a test must never poke the real bridge into sending anything.
    svc.FLUSH_TRIGGER = tmp / ".flush_trigger"
    svc.RUN_TRIGGER = tmp / ".run_trigger"
    svc.ACTIVITY_PING = tmp / ".activity"
    from core import resumes as resumes_mod     # resume uploads land in tmp, not the real cache
    resumes_mod.CACHE = tmp / "resumes"
    resumes_mod.MANIFEST = tmp / "resumes" / "manifest.json"


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

        # 2a. A sent role opens read-only from its archive. One with no kept record stays closed.
        by_key = {row["dedup_key"]: row for row in r["rows"]}
        assert by_key["pk1"]["has_record"] and not by_key["appl1"]["has_record"], by_key; checks += 1
        assert c.get("/api/jobs/appl1").status_code == 404; checks += 1
        assert c.get("/api/jobs/pk1").json()["_read_only"] is False; checks += 1
        arch = tmp / "pending" / pending_mod.ARCHIVE_SUBDIR
        arch.mkdir(exist_ok=True)
        (arch / "appl1.json").write_text(json.dumps(
            {"dedup_key": "appl1", "company": "Neon", "status": "approved",
             "questions": [{"id": "q1", "label": "Why?", "answer": "because"}]}), encoding="utf-8")
        rows2a = c.get("/api/dashboard").json()["rows"]
        assert next(x for x in rows2a if x["dedup_key"] == "appl1")["has_record"]; checks += 1
        assert c.get("/api/jobs/appl1").json()["_read_only"] is True; checks += 1
        (arch / "appl1.json").unlink()

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
        assert a1["date"] == (date.today() - timedelta(days=5)).isoformat(), a1  # the decision changed, not when it left
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
        # Approving adds to the pile; it never sends. The pile leaves on "Send approved"
        # or after the bridge's idle window, so no flush sentinel may appear here.
        assert not svc.FLUSH_TRIGGER.exists(), "approving must not request a flush"; checks += 1
        # And it counts as activity, which restarts that idle window. Without this ping the
        # bridge saw a quiet console and flushed the pile on its next poll.
        assert svc.ACTIVITY_PING.exists(), "approving must reset the idle countdown"; checks += 1
        svc.ACTIVITY_PING.unlink()
        c.put("/api/jobs/pk1/answers", json={"answers": [{"id": "q1", "value": "because"}]})
        assert svc.ACTIVITY_PING.exists(), "answering must reset the idle countdown"; checks += 1

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
        assert st["total"] == 7 and len(st["steps"]) == 7, st; checks += 1
        # ORDER, not just membership: the extension is the product, so it comes second -
        # right after the details that feed it, and ahead of everything optional.
        assert [s["id"] for s in st["steps"]] == [
            "profile", "search", "extension", "companies", "resumes", "telegram",
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
        assert st["core_total"] == 5 and st["core_done"] <= 5, st; checks += 1
        assert st["core_done"] == sum(
            1 for s in st["steps"] if not s["optional"] and s["done"]), st; checks += 1

        # 8c-ii. The extension step names the folder to load, and it is the real folder.
        ext = next(s for s in st["steps"] if s["id"] == "extension")
        assert Path(ext["path"], "manifest.json").is_file(), ext; checks += 1

        # 8d. Resume upload writes the cache the extension attaches from, and only a PDF.
        tag = json.loads((tmp / "profile.json").read_text(encoding="utf-8"))["resume_tracks"][0]["tag"]
        assert c.put(f"/api/resumes/{tag}", content=b"not a pdf").status_code == 400; checks += 1
        assert c.put("/api/resumes/nosuchtrack", content=b"%PDF-1.4").status_code == 404; checks += 1
        up = c.put(f"/api/resumes/{tag}", content=b"%PDF-1.4 test").json()
        assert up == {"ok": True, "changed": True}, up; checks += 1
        again = c.put(f"/api/resumes/{tag}", content=b"%PDF-1.4 test").json()
        assert again["changed"] is False, again; checks += 1
        assert list((tmp / "resumes").glob("*.pdf")), "upload must land in the isolated cache"; checks += 1
        rs = next(s for s in c.get("/api/setup/status").json()["steps"] if s["id"] == "resumes")
        assert rs["done"] and any(t["tag"] == tag and t["cached"] for t in rs["tracks"]), rs; checks += 1
        # Resume-source fields are not "your details": an uploader never fills them in.
        prof_step = next(s for s in c.get("/api/setup/status").json()["steps"]
                         if s["id"] == "profile")
        holes = prof_step["unfilled"]
        assert not any("local_source" in h or "_local_resume_source" in h for h in holes), holes
        checks += 1
        # The list and the count in the detail line must agree, checked on the template,
        # which has more unset fields than any display cap would allow.
        real = (tmp / "profile.json").read_bytes()
        shutil.copy(Path(__file__).resolve().parents[1] / "data" / "profile.example.json",
                    tmp / "profile.json")
        tpl = next(s for s in c.get("/api/setup/status").json()["steps"] if s["id"] == "profile")
        (tmp / "profile.json").write_bytes(real)
        assert len(tpl["unfilled"]) > 40, tpl; checks += 1
        assert tpl["detail"] == f"{len(tpl['unfilled'])} field(s) still unset.", tpl; checks += 1

        # 8e. Search picks: the catalog comes back with the defaults marked, bad picks are
        # refused, and a save matches the profile's resume tracks to the chosen paths.
        so = c.get("/api/search").json()
        assert len(so["paths"]) == 20 and [l["key"] for l in so["levels"]] == [
            "entry", "mid", "senior"], so; checks += 1
        assert so["tracks"] == ["quant", "ai", "ds", "tech"], so; checks += 1
        bad = [{"paths": [], "levels": ["entry"]}, {"paths": ["nope"], "levels": ["entry"]},
               {"paths": ["qa"], "levels": []}]
        assert all(c.put("/api/search", json=b).status_code == 400 for b in bad); checks += 1
        assert not (tmp / "search.yaml").exists(), "a refused pick must not be saved"; checks += 1
        sv = c.put("/api/search", json={"paths": ["qa", "security"], "levels": ["entry"]}).json()
        assert sv == {"ok": True, "tracks": ["security", "qa"]}, sv; checks += 1
        prof = json.loads((tmp / "profile.json").read_text(encoding="utf-8"))
        assert [t["tag"] for t in prof["resume_tracks"]] == ["security", "qa"], prof; checks += 1
        assert prof["default_resume_tag"] == "security", prof; checks += 1
        assert prof["resume_tracks"][0]["local_path"] == "resumes/security.pdf"; checks += 1
        st = c.get("/api/setup/status").json()["steps"]
        assert next(s for s in st if s["id"] == "search")["done"]; checks += 1
        assert [t["tag"] for t in next(s for s in st if s["id"] == "resumes")["tracks"]] == [
            "security", "qa"]; checks += 1
        again = c.get("/api/search").json()
        assert {p["key"] for p in again["paths"] if p["selected"]} == {"qa", "security"}; checks += 1

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

        # 10. Dashboard order: rank, then newest date, then fewest questions. An unopened
        # discovered role sorts after a pending one with the same date.
        def row(key, date, rank=None, unanswered=0, bucket="in_queue"):
            r = {"dedup_key": key, "date": date, "unanswered": unanswered, "bucket": bucket}
            if rank is not None:
                r["rank"] = rank
            return r
        rows = [
            row("done", "2026-09-12", bucket="submitted"),
            row("skip", "2026-09-12", svc.RANK_SKIPPED),
            row("held", "2026-09-12", svc.RANK_HELD),
            row("disc", "2026-09-10", bucket="discovered"),
            row("old3", "2026-09-01", svc.RANK_UNTOUCHED, 1),
            row("new5", "2026-09-10", svc.RANK_UNTOUCHED, 5),
            row("new2", "2026-09-10", svc.RANK_UNTOUCHED, 2),
            row("appr", "2026-08-01", svc.RANK_READY_TO_APPROVE),
            row("half", "2026-08-02", svc.RANK_IN_PROGRESS, 1),
            row("send", "2026-07-01", svc.RANK_READY_TO_SUBMIT, bucket="filled"),
        ]
        svc.sort_rows(rows)
        assert [r["dedup_key"] for r in rows] == [
            "send", "half", "appr", "new2", "new5", "disc", "old3", "held", "skip", "done",
        ], [r["dedup_key"] for r in rows]
        checks += 1

    print(f"web_api: {checks}/{checks} checks passed")


if __name__ == "__main__":
    main()
