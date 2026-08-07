#!/usr/bin/env python3
"""Offline unit tests for triage.py (the orchestrator's brain). No bridge/websockets."""
import json
import tempfile
from pathlib import Path

import triage


def _job(company, title, key, tier="pipeline"):
    return {"company": company, "title": title, "location": "US", "url": f"https://x/{key}",
            "ats": "greenhouse", "tier": tier, "dedup_key": key}


def _write(tmp, name, obj):
    p = Path(tmp) / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def run():
    checks = 0

    with tempfile.TemporaryDirectory() as tmp:
        # queue: 3 companies, one with 3 roles (to exercise the per-company cap)
        queue = [
            _job("Acme", "SWE 1", "k1"),
            _job("Acme", "SWE 2", "k2"),
            _job("Acme", "SWE 3", "k3"),
            _job("Beta", "SWE",   "k4"),
            _job("Gamma", "SWE",  "k5"),
        ]
        qp = _write(tmp, "queue.json", queue)
        ap = _write(tmp, "applied.json", [{"dedup_key": "k4", "company": "Beta"}])  # already applied

        # 1) dedup drops the already-applied Beta role
        res = triage.triage(qp, ap, max_per_day=10, max_per_company=5)
        keys = [j["dedup_key"] for j in res["jobs"]]
        assert "k4" not in keys, keys; checks += 1
        assert res["stats"]["already_applied"] == 1, res["stats"]; checks += 1

        # 2) per-company cap of 2 keeps only 2 Acme roles (the freshest two, order-preserving)
        res = triage.triage(qp, ap, max_per_day=10, max_per_company=2)
        acme = [j["dedup_key"] for j in res["jobs"] if j["company"] == "Acme"]
        assert acme == ["k1", "k2"], acme; checks += 1
        assert res["stats"]["over_company_cap"] == 1, res["stats"]; checks += 1

        # 3) daily cap truncates to N, freshest-first (queue order preserved)
        res = triage.triage(qp, ap, max_per_day=2, max_per_company=5)
        assert [j["dedup_key"] for j in res["jobs"]] == ["k1", "k2"], res["jobs"]; checks += 1
        assert res["stats"]["hit_daily_cap"] is True; checks += 1

        # 4) combined: cap 2/day + 2/co -> k1,k2
        res = triage.triage(qp, ap, max_per_day=2, max_per_company=2)
        assert [j["dedup_key"] for j in res["jobs"]] == ["k1", "k2"]; checks += 1

        # 5) append_applied is idempotent on dedup_key and appends the right shape
        added1 = triage.append_applied(_job("Gamma", "SWE", "k5"), ap)
        added2 = triage.append_applied(_job("Gamma", "SWE", "k5"), ap)
        assert added1 is True and added2 is False, (added1, added2); checks += 1
        rows = triage.load_applied(ap)
        row = next(r for r in rows if r["dedup_key"] == "k5")
        assert row["company"] == "Gamma" and row["applied"], row; checks += 1

        # 6) after logging k5, triage no longer surfaces it
        res = triage.triage(qp, ap, max_per_day=10, max_per_company=5)
        assert "k5" not in [j["dedup_key"] for j in res["jobs"]]; checks += 1

        # 7) legacy bare-string applied.json is tolerated
        ap2 = _write(tmp, "applied2.json", ["k1"])
        res = triage.triage(qp, ap2, max_per_day=10, max_per_company=5)
        assert "k1" not in [j["dedup_key"] for j in res["jobs"]]; checks += 1

        # 8) missing queue file -> empty, no crash
        res = triage.triage(Path(tmp) / "nope.json", ap, 10, 5)
        assert res["jobs"] == [] and res["stats"]["queue_total"] == 0; checks += 1

        # 9) set_applied_status corrects an already-decided role in place. append_applied
        # deliberately no-ops on a known key, so the dashboard's Submitted <-> Blocked move
        # needs this path or it silently does nothing.
        ap3 = _write(tmp, "applied3.json", [
            {"dedup_key": "s1", "company": "Neon", "status": "applied", "applied": "2026-07-01"},
        ])
        assert triage.append_applied({"dedup_key": "s1", "status": "rejected"}, ap3) is False
        checks += 1
        assert (triage.load_applied(ap3)[0]["status"] == "applied"), "append_applied won't correct"
        checks += 1
        assert triage.set_applied_status("s1", "rejected", ap3) is True; checks += 1
        row = triage.load_applied(ap3)[0]
        assert row["status"] == "rejected", row; checks += 1
        assert row["applied"] == "2026-07-01", "the decision changed, not its date"; checks += 1
        assert len(triage.load_applied(ap3)) == 1, "corrected in place, not appended"; checks += 1
        assert triage.set_applied_status("s1", "rejected", ap3) is False, "already that status"
        checks += 1
        assert triage.set_applied_status("ghost", "rejected", ap3) is False, "unknown key"
        checks += 1

    # ── resume_track: which CV an application actually went out with ───────────────────
    # append_applied builds a FIXED row and drops anything it does not name, which is how
    # this fact went unrecorded across 46 applications and left Workday's clone guard with
    # nothing to check.
    with tempfile.TemporaryDirectory() as tmp:
        ap = _write(tmp, "applied.json", [])

        assert triage.append_applied(
            {"dedup_key": "w1", "company": "Adobe", "title": "Sr Research Engineer",
             "url": "https://adobe.wd5.myworkdayjobs.com/x/job/y", "resume_track": "ai"}, ap)
        row = triage.load_applied(ap)[0]
        assert row["resume_track"] == "ai", row; checks += 1

        # Absent, not empty. A reader must be able to tell "went out with the quant CV"
        # from "nobody recorded it" — the clone guard reconciles instead of guessing on the
        # second, and an empty string would read as a real answer.
        assert triage.append_applied(
            {"dedup_key": "w2", "company": "Beta", "title": "SWE", "url": "https://x/w2"}, ap)
        row2 = [r for r in triage.load_applied(ap) if r["dedup_key"] == "w2"][0]
        assert "resume_track" not in row2, row2; checks += 1
        assert triage.append_applied(
            {"dedup_key": "w3", "company": "Beta", "title": "SWE", "url": "https://x/w3",
             "resume_track": ""}, ap)
        row3 = [r for r in triage.load_applied(ap) if r["dedup_key"] == "w3"][0]
        assert "resume_track" not in row3, "an empty track is not an answer"; checks += 1

        # The rows written before the field existed must stay readable and unmodified.
        for r in triage.load_applied(ap):
            assert set(r) >= {"dedup_key", "company", "title", "location", "url",
                              "status", "applied"}, r
        checks += 1

    print(f"triage: {checks}/{checks} checks passed")


if __name__ == "__main__":
    run()
