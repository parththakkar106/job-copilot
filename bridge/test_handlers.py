#!/usr/bin/env python3
"""Offline tests for the new orchestrator WS handlers (queue / applied / notify).

Mocks Telegram (tg_send) and the WebSocket so the handlers run without a bot or a live
extension — validates the wiring + reply shape that background.js's requestBridge expects.
"""
import asyncio
import json
import tempfile
from pathlib import Path

import bridge as B
import triage


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _job(company, title, key):
    return {"company": company, "title": title, "location": "US", "url": f"https://x/{key}",
            "ats": "greenhouse", "tier": "pipeline", "dedup_key": key}


def run():
    checks = 0
    # Silence Telegram: record calls instead of hitting the API.
    calls = []
    B.tg_send = lambda text, buttons=None: calls.append(text) or 1

    with tempfile.TemporaryDirectory() as tmp:
        qp = Path(tmp) / "queue.json"
        ap = Path(tmp) / "applied.json"
        qp.write_text(json.dumps([_job("Acme", "SWE", "k1"), _job("Beta", "SWE", "k2")]), encoding="utf-8")
        ap.write_text("[]", encoding="utf-8")
        # Point triage at the temp files.
        triage.QUEUE_PATH, triage.APPLIED_PATH = qp, ap

        # 1) queue -> replies over the answers channel with jobs + stats
        ws = FakeWS()
        asyncio.run(B.handle_queue({"reqId": "r1", "type": "queue"}, ws))
        msg = ws.sent[0]
        assert msg["type"] == "answers" and msg["reqId"] == "r1", msg; checks += 1
        assert len(msg["answers"]["jobs"]) == 2, msg["answers"]; checks += 1
        assert msg["answers"]["stats"]["selected"] == 2; checks += 1

        # 2) applied -> logs to applied.json, idempotent, and answers ok/added
        ws = FakeWS()
        rec = {"dedup_key": "k1", "company": "Acme", "title": "SWE", "location": "US", "url": "https://x/k1"}
        asyncio.run(B.handle_applied({"reqId": "r2", "type": "applied", "record": rec}, ws))
        assert ws.sent[0]["answers"] == {"ok": True, "added": True}, ws.sent[0]; checks += 1
        ws = FakeWS()
        asyncio.run(B.handle_applied({"reqId": "r3", "type": "applied", "record": rec}, ws))
        assert ws.sent[0]["answers"] == {"ok": True, "added": False}, ws.sent[0]; checks += 1
        assert "k1" in triage.applied_keys(ap); checks += 1

        # 3) after logging k1, queue no longer offers it
        ws = FakeWS()
        asyncio.run(B.handle_queue({"reqId": "r4", "type": "queue"}, ws))
        keys = [j["dedup_key"] for j in ws.sent[0]["answers"]["jobs"]]
        assert keys == ["k2"], keys; checks += 1

        # 3b) the on-page panel logs WITHOUT a dedup_key (it can't know it). The bridge must
        # recover k2's real key by URL — otherwise the panel and the queue would key the same
        # job differently and it'd be applied to twice. Resolve, log, then confirm the queue
        # drops it.
        ws = FakeWS()
        panel_rec = {"company": "Beta", "title": "SWE", "location": "", "url": "https://x/k2"}
        asyncio.run(B.handle_applied({"reqId": "r4b", "type": "applied", "record": panel_rec}, ws))
        assert ws.sent[0]["answers"] == {"ok": True, "added": True}, ws.sent[0]; checks += 1
        assert "k2" in triage.applied_keys(ap), "panel apply must land on the queue's own key"; checks += 1
        ws = FakeWS()
        asyncio.run(B.handle_queue({"reqId": "r4c", "type": "queue"}, ws))
        assert ws.sent[0]["answers"]["jobs"] == [], "queue must not re-offer a panel-applied role"; checks += 1

        # 3c) a role NOT in today's queue (Parth found it himself) still gets a stable computed
        # key so it's idempotent, rather than being dropped.
        ws = FakeWS()
        off = {"company": "Zeta", "title": "ML Eng", "location": "Remote", "url": "https://z/none"}
        asyncio.run(B.handle_applied({"reqId": "r4d", "type": "applied", "record": off}, ws))
        assert ws.sent[0]["answers"]["added"] is True, ws.sent[0]
        expected = B.scraper_mod.dedup_key("Zeta", "ML Eng", "Remote")
        assert expected in triage.applied_keys(ap), "off-queue apply needs a computed key"; checks += 1
        ws = FakeWS()
        asyncio.run(B.handle_applied({"reqId": "r4e", "type": "applied", "record": off}, ws))
        assert ws.sent[0]["answers"]["added"] is False, "off-queue apply must be idempotent"; checks += 1

        # 3d) the panel's "I submitted this by hand" flag: logs the same as any other apply
        # (the row shape drops by_hand), but the Telegram note has to say so — it's the only
        # record that nothing on our side ever saw a confirmation page.
        ws = FakeWS()
        hand = {"company": "Manu", "title": "Analyst", "location": "NY", "url": "https://m/none",
                "by_hand": True}
        asyncio.run(B.handle_applied({"reqId": "r4f", "type": "applied", "record": hand}, ws))
        assert ws.sent[0]["answers"]["added"] is True, ws.sent[0]; checks += 1
        assert "submitted by hand" in calls[-1], calls[-1]; checks += 1
        assert "by_hand" not in json.loads(ap.read_text(encoding="utf-8"))[-1]; checks += 1
        assert "submitted by hand" not in calls[-3], "a normal apply must not claim by-hand"; checks += 1

        # 4) notify -> posts to Telegram and answers ok
        ws = FakeWS()
        asyncio.run(B.handle_notify({"reqId": "r5", "type": "notify", "text": "hello"}, ws))
        assert ws.sent[0]["answers"] == {"ok": True}; checks += 1
        assert "hello" in calls; checks += 1

        # 5) log_report -> appends a JSON line per call and answers ok
        log_path = Path(tmp) / "fill_reports.jsonl"
        B.LOG_PATH = log_path
        ws = FakeWS()
        rep1 = {"company": "Acme", "role": "SWE", "filled": [{"key": "email", "value": "a@b.com"}]}
        asyncio.run(B.handle_log_report({"reqId": "r6", "type": "log_report", "report": rep1}, ws))
        assert ws.sent[0]["answers"] == {"ok": True}; checks += 1
        ws = FakeWS()
        rep2 = {"company": "Beta", "role": "DS", "errors": ["boom"]}
        asyncio.run(B.handle_log_report({"reqId": "r7", "type": "log_report", "report": rep2}, ws))
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2, lines; checks += 1
        rows = [json.loads(l) for l in lines]
        assert rows[0]["company"] == "Acme" and "ts" in rows[0]; checks += 1
        assert rows[1]["company"] == "Beta" and rows[1]["errors"] == ["boom"]; checks += 1

        # 6) audit_dump -> decodes the PNG, writes it under audit/, indexes it, answers ok+path
        import base64
        B.AUDIT_DIR = Path(tmp) / "audit"
        B.AUDIT_INDEX_PATH = Path(tmp) / "audit_dumps.jsonl"
        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
        data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode()
        ws = FakeWS()
        asyncio.run(B.handle_audit_dump({"reqId": "r8", "type": "audit_dump", "company": "Acme",
                                          "role": "SWE", "url": "https://x/k1",
                                          "summary": [{"label": "Email", "value": "a@b.com"}],
                                          "screenshot": data_url}, ws))
        ans = ws.sent[0]["answers"]
        assert ans["ok"] is True and ans["path"], ans; checks += 1
        saved = Path(ans["path"])
        assert saved.exists() and saved.read_bytes() == png_bytes; checks += 1
        idx = [json.loads(l) for l in B.AUDIT_INDEX_PATH.read_text(encoding="utf-8").strip().splitlines()]
        assert len(idx) == 1 and idx[0]["company"] == "Acme" and idx[0]["screenshot"] == saved.name; checks += 1
        assert idx[0]["summary"] == [{"label": "Email", "value": "a@b.com"}]; checks += 1

        # 7) ad-hoc /apply collect: the job carries NO dedup_key (the extension can't mint
        # one). The bridge must resolve it — URL-match today's queue so it reuses that key
        # (no second application), else compute a stable one — rather than raising.
        pend_dir = Path(tmp) / "pending"
        pend_dir.mkdir(parents=True, exist_ok=True)
        B.pending_mod.PENDING_DIR = pend_dir
        ws = FakeWS()
        adhoc = {"dedup_key": "", "company": "Beta", "title": "SWE", "location": "", "url": "https://x/k2"}
        asyncio.run(B.handle_collect({"reqId": "r9", "type": "collect", "job": adhoc,
                                      "questions": [{"id": "q1", "label": "Why?"}], "filled": []}, ws))
        assert ws.sent[0]["answers"]["ok"] is True, ws.sent[0]; checks += 1
        assert (pend_dir / "k2.json").exists(), "ad-hoc collect must reuse the queue's own key"; checks += 1
        # a link NOT in today's queue still persists under a computed key (never dropped).
        ws = FakeWS()
        off_url = {"dedup_key": "", "company": "Zeta", "title": "ML Eng", "location": "Remote",
                   "url": "https://z/self-found"}
        asyncio.run(B.handle_collect({"reqId": "r10", "type": "collect", "job": off_url,
                                      "questions": [], "filled": []}, ws))
        ck = B.scraper_mod.dedup_key("Zeta", "ML Eng", "Remote")
        assert (pend_dir / f"{ck}.json").exists(), "off-queue collect needs a computed key"; checks += 1

        # 8) /apply push: with a connected client it emits an apply_url message; with none it
        # warns Parth Chrome isn't connected; with no link it asks for one.
        push_ws = FakeWS()
        B.CLIENTS.clear(); B.CLIENTS.add(push_ws)
        asyncio.run(B.handle_apply("/apply https://boards.greenhouse.io/acme/jobs/9"))
        assert any(p.get("type") == "apply_url" and "jobs/9" in p.get("url", "")
                   for p in push_ws.sent), push_ws.sent; checks += 1
        B.CLIENTS.clear(); calls.clear()
        asyncio.run(B.handle_apply("/apply https://x/y"))
        assert any("isn't connected" in c for c in calls), calls; checks += 1
        calls.clear()
        asyncio.run(B.handle_apply("/apply"))
        assert any("application link" in c for c in calls), calls; checks += 1
        B.CLIENTS.clear()

        # 9) pending_answers: what the on-page panel gets back for a job it's re-filling.
        # Answers AND drafts both ride along — a draft left on disk that the panel can't see
        # is one Claude re-writes and re-charges for, which is the whole point of persisting
        # it at collect time.
        B.pending_mod.save({
            "dedup_key": "k9", "company": "Acme", "title": "SWE", "url": "https://x/k9",
            "created": "2026-01-01T00:00:00", "status": "awaiting",
            "questions": [
                {"id": "a1", "label": "Why us?", "answer": "because", "draft": "stale draft"},
                {"id": "a2", "label": "What excites you?", "answer": None, "draft": "a paid-for draft"},
                {"id": "a3", "label": "Salary?", "answer": None},
                {"id": "a4", "label": "Anything else?", "answer": "", "draft": "unwanted"},
            ],
        }, pend_dir)
        ws = FakeWS()
        asyncio.run(B.handle_pending_answers({"reqId": "r11", "url": "https://x/k9/application"}, ws))
        ans = ws.sent[0]["answers"]
        assert ans["found"] is True, ans; checks += 1
        assert ans["answers"] == {"Why us?": "because"}, ans["answers"]; checks += 1
        # An answered question's draft is dead weight (its card never renders), and a
        # deliberate blank ("") must NOT be handed its draft back — that would re-open a
        # decision he already made.
        assert ans["drafts"] == {"What excites you?": "a paid-for draft"}, ans["drafts"]; checks += 1
        ws = FakeWS()
        asyncio.run(B.handle_pending_answers({"reqId": "r12", "url": "https://nowhere/x"}, ws))
        assert ws.sent[0]["answers"] == {"found": False, "answers": {}, "drafts": {}}; checks += 1

        # 10) workday_route: the extension asks Python which entry route to take, so the
        # "have we applied to this tenant" rule has exactly one implementation.
        # Stub load() rather than the route function itself — the point is to test the real
        # policy against fixture rows, and reading the live applied.json here would make the
        # result depend on whichever applications Parth happens to have sent.
        real_load = B.apply_log_mod.load
        adobe = ("https://adobe.wd5.myworkdayjobs.com/external_experienced/job/"
                 "San-Francisco/Sr-Research-Engineer_R170796")
        try:
            B.apply_log_mod.load = lambda: []
            ws = FakeWS()
            asyncio.run(B.handle_workday_route({"reqId": "r13", "url": adobe}, ws))
            a = ws.sent[0]["answers"]
            assert a["tenant"] == "adobe", a; checks += 1
            assert a["route"] == "autofillWithResume", a; checks += 1

            B.apply_log_mod.load = lambda: [{"dedup_key": "k", "url": adobe}]
            ws = FakeWS()
            asyncio.run(B.handle_workday_route({"reqId": "r14", "url": adobe}, ws))
            assert ws.sent[0]["answers"]["route"] == "useMyLastApplication", ws.sent[0]; checks += 1

            # A lookup that blows up must still answer. manual is the route that requires
            # nothing to be true, so a bridge fault costs speed, never correctness.
            def boom():
                raise RuntimeError("applied.json unreadable")
            B.apply_log_mod.load = boom
            ws = FakeWS()
            asyncio.run(B.handle_workday_route({"reqId": "r15", "url": adobe}, ws))
            assert ws.sent[0]["answers"]["route"] == "manual", ws.sent[0]; checks += 1
        finally:
            B.apply_log_mod.load = real_load

        # A handler the dispatch table does not name is unreachable, and every test above
        # would still pass because they call it directly. The table is built inside the
        # connection handler, so read the source to check the wiring.
        import inspect
        wiring = inspect.getsource(B)
        assert '"workday_route": handle_workday_route' in wiring, "handler not dispatched"
        checks += 1

    print(f"handlers: {checks}/{checks} checks passed")


if __name__ == "__main__":
    run()
