#!/usr/bin/env python3
"""Checks for the console's login gate and the pasted-link routes, on a temp data layer.

    python web/test_access.py
"""
import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from web import test_api  # noqa: E402  (its _isolate points every path at a temp dir)
from web.app import auth, services as svc  # noqa: E402
from web.app.main import app  # noqa: E402
from web.app.paths import pending_mod, triage_mod  # noqa: E402

PASSWORD = "correct horse battery"
TUNNEL = {"cf-connecting-ip": "203.0.113.9"}
checks = 0


def ok(cond, msg=""):
    global checks
    assert cond, msg
    checks += 1


def client(**kw):
    return TestClient(app, follow_redirects=False, **kw)


def login(c, password=PASSWORD, headers=None):
    return c.post("/login", content=f"password={password.replace(' ', '+')}",
                  headers={"content-type": "application/x-www-form-urlencoded", **(headers or {})})


def test_gate():
    # Without a password file, loopback works and tunnel traffic gets nothing.
    ok(client().get("/api/status").status_code == 200)
    ok(client().get("/api/status", headers=TUNNEL).status_code == 503)
    ok(client().get("/", headers=TUNNEL).status_code == 503)

    auth.write_password(PASSWORD, auth.AUTH_PATH)
    c = client()
    for path in ("/api/status", "/api/profile", "/api/applied", "/api/health"):
        ok(c.get(path).status_code == 401, path)
    ok(c.post("/api/run/flush").status_code == 401)
    ok(c.get("/").status_code == 303 and c.get("/").headers["location"] == "/login")
    ok(c.get("/docs").status_code == 303)
    ok(c.get("/openapi.json").status_code == 303)
    ok(c.get("/login").status_code == 200)

    # A wrong password, a forged cookie, and an expired token all stay out.
    ok(login(c, "wrong password here").status_code == 401)
    ok(client(cookies={auth.COOKIE: "9999999999.forged"}).get("/api/status").status_code == 401)
    stored = json.loads(auth.AUTH_PATH.read_text())
    expired = f"1000.{auth._sign('1000', stored)}"
    ok(client(cookies={auth.COOKIE: expired}).get("/api/status").status_code == 401)

    # The right password opens the console, and over the tunnel the cookie is Secure.
    auth.reset_limits()
    r = login(c)
    ok(r.status_code == 303, r.text)
    cookie = r.headers["set-cookie"].lower()
    ok("httponly" in cookie and "samesite=lax" in cookie and "secure" not in cookie, cookie)
    ok("secure" in login(client(), headers=TUNNEL).headers["set-cookie"].lower())
    ok(c.get("/api/status").status_code == 200)
    ok("private" in c.get("/api/status").headers["cache-control"])

    # A write that another site started is refused even with a valid session.
    r = c.post("/api/run/flush", headers={"host": "jobs.parth.party",
                                          "origin": "https://dnd.parth.party"})
    ok(r.status_code == 403, r.text)

    # A new password ends existing sessions.
    auth.write_password(PASSWORD + "2", auth.AUTH_PATH)
    ok(c.get("/api/status").status_code == 401)
    auth.write_password(PASSWORD, auth.AUTH_PATH)

    # Five misses lock one address, and a right password then fails too.
    auth.reset_limits()
    for _ in range(auth.MAX_FAILS_PER_ADDRESS):
        ok(login(client(), "nope nope nope", TUNNEL).status_code == 401)
    ok(login(client(), PASSWORD, TUNNEL).status_code == 429)
    ok(login(client(), PASSWORD, {"cf-connecting-ip": "203.0.113.10"}).status_code == 303)

    # Rotating addresses reaches the global limit.
    auth.reset_limits()
    for i in range(auth.MAX_FAILS_GLOBAL):
        login(client(), "nope nope nope", {"cf-connecting-ip": f"198.51.100.{i}"})
    ok(login(client(), PASSWORD, {"cf-connecting-ip": "192.0.2.1"}).status_code == 429)
    auth.reset_limits()

    try:
        auth.write_password("short", auth.AUTH_PATH)
        ok(False, "a short password was accepted")
    except ValueError:
        ok(True)

    # A path outside dist/ never resolves to a file.
    r = c.get("/..%2f..%2fapp%2fauth.py")
    ok(b"write_password" not in r.content)
    auth.AUTH_PATH.unlink()


def test_applied():
    c = client()
    rows = c.get("/api/applied").json()["rows"]
    ok([r["company"] for r in rows] == ["Neon"], rows)

    today = date.today().isoformat()
    role = {"company": "Acme", "title": "Data Scientist", "location": "Austin",
            "url": "https://jobs.example/1", "applied": today}
    ok(c.post("/api/applied", json=role).status_code == 200)
    ok(c.post("/api/applied", json=role).status_code == 409, "logged twice")
    ok(c.get("/api/applied").json()["rows"][0]["company"] == "Acme")

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    ok(c.post("/api/applied", json={**role, "applied": tomorrow}).status_code == 400)
    ok(c.post("/api/applied", json={**role, "url": "file:///x"}).status_code == 400)
    ok(c.post("/api/applied", json={**role, "company": ""}).status_code == 422)

    # A rejected row stays hidden.
    triage_mod.append_applied({"dedup_key": "rej", "company": "Passed", "status": "rejected"},
                              triage_mod.APPLIED_PATH)
    ok("Passed" not in [r["company"] for r in c.get("/api/applied").json()["rows"]])

    # Logging a role the queue holds reuses its key, even with different page text, so the
    # collect run cannot apply a second time.
    queue = json.loads(svc.QUEUE_PATH.read_text())
    queue.append({"dedup_key": "qk2", "company": "Figma", "title": "Backend",
                  "url": "https://boards.greenhouse.io/figma/jobs/55"})
    svc.QUEUE_PATH.write_text(json.dumps(queue))
    r = c.post("/api/applied", json={"company": "Figma Inc", "title": "Backend Engineer",
                                     "location": "", "url": "https://x.example/?gh_jid=55",
                                     "applied": today})
    ok(r.status_code == 200, r.text)
    ok("qk2" in triage_mod.applied_keys(triage_mod.APPLIED_PATH))

    # A role with a pending record leaves its pile, and its answers are kept.
    pending_mod.put({"dedup_key": "pk1", "company": "Ramp", "title": "SWE",
                     "url": "https://jobs.ashbyhq.com/ramp/11111111-1111-1111-1111-111111111111"},
                    [{"id": "q", "label": "Why", "answer": "because"}], [])
    r = c.post("/api/applied", json={
        "company": "Ramp", "title": "SWE", "location": "",
        "url": "https://jobs.ashbyhq.com/ramp/11111111-1111-1111-1111-111111111111/application",
        "applied": today})
    ok(r.status_code == 200, r.text)
    ok(pending_mod.load("pk1") is None and pending_mod.load_archived("pk1"))


def test_links():
    c = client()
    link = "https://jobs.lever.co/acme/22222222-2222-2222-2222-222222222222"
    r = c.post("/api/links", json={"url": link})
    ok(r.status_code == 200, r.text)
    row = r.json()["link"]
    ok(row["form_url"].endswith("/apply") and row["status"] == "waiting", row)
    ok(c.post("/api/links", json={"url": link + "/apply"}).status_code == 409, "queued twice")
    ok(c.post("/api/links", json={"url": "javascript:alert(1)"}).status_code == 400)
    ok(c.post("/api/links", json={
        "url": "https://x.example/?gh_jid=55"}).status_code == 409, "queued an applied role")
    ok(len(c.get("/api/links").json()["links"]) == 1)
    ok(c.delete("/api/links/not-an-id").status_code == 404)
    ok(c.delete(f"/api/links/{row['id']}").status_code == 200)
    ok(c.get("/api/links").json()["links"] == [])

    # A filled link shows its record's state, and the page can act on it.
    today = date.today().isoformat()
    svc.LINK_QUEUE.mkdir(exist_ok=True)

    def link(link_id, key):
        (svc.LINK_QUEUE / f"{link_id}.json").write_text(json.dumps(
            {"id": link_id, "url": "https://x.example/" + link_id, "status": "filled",
             "dedup_key": key}))

    def status(link_id):
        return next((r for r in c.get("/api/links").json()["links"] if r["id"] == link_id), {})

    pending_mod.put({"dedup_key": "lk1", "company": "Acme", "title": "SWE", "url": "https://l/1"},
                    [{"id": "q", "label": "Why", "answer": None}], [])
    link("1-aaaaaa", "lk1")
    row = status("1-aaaaaa")
    ok(row["status"] == "needs_answers" and row["unanswered"] == 1 and row["company"] == "Acme", row)
    pending_mod.set_answer("lk1", "q", "because")
    ok(status("1-aaaaaa")["status"] == "ready")
    pending_mod.approve("lk1")
    ok(status("1-aaaaaa")["status"] == "sending")
    pending_mod.hold("lk1", reason="needs a code")
    ok(status("1-aaaaaa")["status"] == "held" and status("1-aaaaaa")["note"] == "needs a code")

    # A sent role shows through today, then leaves the list, as does a role passed on.
    triage_mod.append_applied({"dedup_key": "lk2", "company": "Beta", "title": "DS",
                               "applied": today}, triage_mod.APPLIED_PATH)
    link("2-bbbbbb", "lk2")
    ok(status("2-bbbbbb")["status"] == "sent")
    triage_mod.append_applied({"dedup_key": "lk3", "applied": "2020-01-01"},
                              triage_mod.APPLIED_PATH)
    link("3-cccccc", "lk3")
    ok(status("3-cccccc") == {} and not (svc.LINK_QUEUE / "3-cccccc.json").exists())
    triage_mod.append_applied({"dedup_key": "lk4", "status": "rejected", "applied": today},
                              triage_mod.APPLIED_PATH)
    link("4-dddddd", "lk4")
    ok(status("4-dddddd") == {})


def test_refer(tmp):
    from core import refer_messages
    refer_messages.paths.REFERRAL_TEMPLATES = tmp / "templates.yaml"
    refer_messages.paths.REFERRAL_TEMPLATES.write_text("ai:\n  hm: 'Hey {name}'\n  employee: ''\n")
    c = client()
    got = c.get("/api/refer-templates").json()
    ok(got["templates"] == {"ai": {"hm": "Hey {name}"}}, got)
    triage_mod.append_applied({"dedup_key": "ref1", "company": "Acme", "title": "AI Engineer",
                               "applied": date.today().isoformat()}, triage_mod.APPLIED_PATH)
    row = next(r for r in c.get("/api/applied").json()["rows"] if r["dedup_key"] == "ref1")
    ok(row["kind"] == "ai", row)

    # The templates hold your own words, so they stay behind the login.
    auth.write_password(PASSWORD, auth.AUTH_PATH)
    ok(client().get("/api/refer-templates").status_code == 401)
    auth.AUTH_PATH.unlink()


def main():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_api._isolate(tmp)
        svc.LINK_QUEUE = tmp / "links"
        test_gate()
        test_applied()
        test_links()
        test_refer(tmp)
    print(f"access: {checks}/{checks} checks passed")


if __name__ == "__main__":
    main()
