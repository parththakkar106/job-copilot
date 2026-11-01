#!/usr/bin/env python3
"""Checks for core/joblink.py. No network: every fetch is stubbed.

    python core/test_joblink.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import joblink as J  # noqa: E402

UUID = "12345678-1234-1234-1234-123456789abc"
checks = 0


def ok(cond, msg=""):
    global checks
    assert cond, msg
    checks += 1


def page(body: str):
    class R:
        status_code = 200
        text = body
    return R()


# Two links to one role share a token.
ok(J.url_token(f"https://jobs.ashbyhq.com/acme/{UUID}") ==
   J.url_token(f"https://jobs.ashbyhq.com/acme/{UUID}/application"))
ok(J.url_token("https://careers.acme.com/x?gh_jid=77") ==
   J.url_token("https://job-boards.greenhouse.io/acme/jobs/77"))
ok(J.url_token("") == "")

# A job description link becomes its form.
ok(J.form_url(f"https://jobs.ashbyhq.com/acme/{UUID}").endswith(f"{UUID}/application"))
ok(J.form_url(f"https://jobs.ashbyhq.com/acme/{UUID}/application").count("application") == 1)
ok(J.form_url(f"https://jobs.lever.co/acme/{UUID}").endswith("/apply"))
ok(J.form_url("https://job-boards.greenhouse.io/acme/jobs/77")
   == "https://boards.greenhouse.io/embed/job_app?token=77")
ok(J.form_url("https://careers.acme.com/x?gh_jid=77").endswith("token=77"))
ok(J.form_url("https://example.com/job") == "https://example.com/job")

# Private addresses never get fetched, including by name and through a redirect.
fetched = []
real_get = J.requests.get
J.requests.get = lambda *a, **k: fetched.append(a)
for bad in ("http://127.0.0.1:8765/", "http://localhost:8000/api/profile", "http://10.0.0.5/",
            "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "file:///etc/passwd",
            "ftp://example.com/"):
    ok(J.safe_get(bad) is None, bad)
ok(not fetched, "a private address was fetched")


class Redirect:
    is_redirect = True
    headers = {"location": "http://127.0.0.1:8000/api/profile"}

    def close(self):
        pass


J.requests.get = lambda *a, **k: Redirect()
real_public = J._is_public
J._is_public = lambda host: host == "safe.example"
ok(J.safe_get("https://safe.example/job") is None, "followed a redirect to loopback")
J.requests.get = real_get
J._is_public = real_public

# Each read layer fills only what the earlier ones left empty.
J.safe_get = lambda url: page(
    '<script type="application/ld+json">{"@graph":[{"@type":"JobPosting","title":"Data &amp; ML",'
    '"hiringOrganization":{"name":"Acme"},"jobLocation":{"address":{"addressLocality":"Austin",'
    '"addressRegion":"TX"}}}]}</script>')
r = J.read("https://acme.example/jobs/ml")
ok((r["company"], r["title"], r["location"], r["source"])
   == ("Acme", "Data & ML", "Austin, TX", "page"), r)

J.safe_get = lambda url: page(
    '<meta property="og:title" content="Acme hiring Quant Analyst in New York, NY | LinkedIn">')
r = J.read("https://www.linkedin.com/jobs/view/1")
ok((r["company"], r["title"], r["location"]) == ("Acme", "Quant Analyst", "New York, NY"), r)

J.safe_get = lambda url: page("<body><p>We need a Backend Engineer at Acme.</p></body>")
asked = []
r = J.read("https://acme.example/j", ask=lambda p: asked.append(p) or
           'Sure: {"company": "Acme", "title": "Backend Engineer", "location": ""}')
ok(r["source"] == "claude" and r["company"] == "Acme", r)
ok("Backend Engineer" in asked[0])
ok(J.read("https://acme.example/j", ask=lambda p: "no json here")["company"] == "")

J.safe_get = lambda url: None
r = J.read("https://acme.example/j")
ok(r["source"] == "none" and r["url"] == "https://acme.example/j", r)

# A tracked company uses its companies.yaml name, not the board's.
J._APIS["jobs.lever.co"] = lambda parts: {"ats": "lever", "slug": "acme", "title": "SWE",
                                          "location": "Remote"}
r = J.read(f"https://jobs.lever.co/acme/{UUID}", {("lever", "acme"): "Acme Corp"})
ok(r["company"] == "Acme Corp" and r["source"] == "job board", r)

print(f"joblink: {checks}/{checks} checks passed")
