#!/usr/bin/env python
"""Unit tests for the Workday entry-route policy in core/apply_log.py.

The route decides how an application STARTS, and every route reaches the same six-step
wizard, so a wrong answer here is not a crash — it is a slower fill, or a replay of an
application that should not have been replayed. Nothing downstream notices. That makes
this one of the rules most worth pinning.

The tenant is recovered from the URL host because applied.json has no `ats` field. Every
case below therefore exercises the host parse as well as the policy.

Run: python test_workday_route.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import apply_log  # noqa: E402

n = 0


def ok(cond, msg):
    global n
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    n += 1


ADOBE = "https://adobe.wd5.myworkdayjobs.com/external_experienced/job/San-Francisco/Sr-Research-Engineer_R170796"
CROWD = "https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers/job/USA-Remote/Engineer_R12345"

# ── tenant parsing ──────────────────────────────────────────────────────────────────────
ok(apply_log.workday_tenant(ADOBE) == "adobe", "tenant comes from the subdomain")
ok(apply_log.workday_tenant(CROWD) == "crowdstrike", "tenant parse is not Adobe-specific")
ok(apply_log.workday_tenant("https://ADOBE.WD5.MyWorkdayJobs.com/x/job/y") == "adobe",
   "tenant is lowercased, and the host match is case-insensitive")
ok(apply_log.workday_tenant("https://boards.greenhouse.io/adobe/jobs/1") is None,
   "a non-Workday URL has no tenant")
ok(apply_log.workday_tenant(None) is None, "a missing URL is not an error")
ok(apply_log.workday_tenant("") is None, "an empty URL is not an error")

# The pod number varies by tenant (wd1, wd3, wd5...) and is not part of the identity.
ok(apply_log.workday_tenant("https://citi.wd3.myworkdayjobs.com/x/job/y") == "citi",
   "any pod number parses")

# A lookalike host must not read as a tenant — the pattern requires the real domain
# followed by a path separator.
ok(apply_log.workday_tenant("https://adobe.wd5.myworkdayjobs.com.evil.example/x/job/y") is None,
   "a suffix-lookalike host yields no tenant")

# ── the policy ──────────────────────────────────────────────────────────────────────────
# A tenant we have never applied to has nothing to clone, so the resume parser seeds it.
cold = apply_log.workday_route(ADOBE, rows=[])
ok(cold["tenant"] == "adobe", "cold route still reports the tenant")
ok(cold["route"] == "autofillWithResume",
   "THE POLICY: a cold tenant seeds from the resume, it does NOT fill an empty form by hand")
ok("adobe" in cold["reason"], "the reason names the tenant it decided about")

# One prior application to the SAME tenant is what unlocks the clone.
warm_rows = [{"dedup_key": "k1", "url": CROWD, "company": "CrowdStrike"}]
warm = apply_log.workday_route(CROWD, rows=warm_rows)
ok(warm["route"] == "useMyLastApplication", "a warm tenant clones its last application")

# Warmth is PER TENANT, not global. This is the whole reason the tenant is the unit of
# account: a CrowdStrike application tells you nothing about what Adobe will offer.
ok(apply_log.workday_route(ADOBE, rows=warm_rows)["route"] == "autofillWithResume",
   "a different tenant stays cold — warmth never leaks across tenants")

# Company NAMES drift between the tracker and the board ("Bank of America" vs "BofA"), so
# the match must key on the URL host and ignore the company column entirely.
misnamed = [{"dedup_key": "k2", "url": ADOBE, "company": "Adobe Inc. (US)"}]
ok(apply_log.workday_route(ADOBE, rows=misnamed)["route"] == "useMyLastApplication",
   "the tenant match ignores the company name, which drifts")

# Rows from the other three ATSs are the overwhelming majority of applied.json and must
# not make any Workday tenant look warm.
other_ats = [{"dedup_key": "k3", "url": "https://boards.greenhouse.io/adobe/jobs/1"},
             {"dedup_key": "k4", "url": "https://jobs.lever.co/adobe/abc"},
             {"dedup_key": "k5", "url": ""},
             {"dedup_key": "k6"}]
ok(apply_log.workday_route(ADOBE, rows=other_ats)["route"] == "autofillWithResume",
   "a Greenhouse application to the same company does NOT warm the Workday tenant")
ok(apply_log.applied_workday_tenants(other_ats) == set(),
   "rows with no url, or a non-Workday one, contribute no tenants")

# `manual` survives for exactly one case: there is no tenant to reason about.
off = apply_log.workday_route("https://jobs.ashbyhq.com/ramp/1a2b3c", rows=[])
ok(off["route"] == "manual" and off["tenant"] is None,
   "a non-Workday URL is the ONLY case that routes to manual")

# ── the set helper ──────────────────────────────────────────────────────────────────────
mixed = [{"url": ADOBE}, {"url": CROWD}, {"url": ADOBE}, {"url": "https://x.example/y"}]
ok(apply_log.applied_workday_tenants(mixed) == {"adobe", "crowdstrike"},
   "tenants dedupe, and non-Workday rows drop out")

print(f"workday route: {n}/{n} checks passed")
