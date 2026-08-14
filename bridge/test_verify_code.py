#!/usr/bin/env python
"""Tests for bridge.handle_verify_code — the WS handler over mailcode.py.

Run: python bridge/test_verify_code.py

Fixtures are invented (see test_mailcode.py's note). The handler's contract is that it is
NEVER fatal: every failure path still replies on the answers channel, because the
extension is awaiting a reqId and a silent handler would hang a fill.
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, __file__.rsplit("test_verify_code.py", 1)[0])
import bridge as br
import mailcode as mc

CHECKS = 0
FAILED = []


def check(cond, label):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"PASS {label}")
    else:
        FAILED.append(label)
        print(f"FAIL {label}")


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def last(self):
        return self.sent[-1] if self.sent else None


def call(msg, reader=None, maker=None):
    """Run the handler with make_reader stubbed, and return the reply.

    `reader` stubs what make_reader RETURNS; `maker` replaces make_reader itself, which is
    how the "not configured" path is tested — there the failure happens while BUILDING the
    reader, not while using it.
    """
    ws = FakeWS()
    real = mc.make_reader
    if maker is not None:
        mc.make_reader = maker
    elif reader is not None:
        mc.make_reader = lambda: reader
    try:
        asyncio.run(br.handle_verify_code(msg, ws))
    finally:
        mc.make_reader = real
    return ws.last()


T0 = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
SINCE = T0.timestamp()


def msg(uid, mins, **kw):
    return mc.MailMessage(uid=uid, received=T0 + timedelta(minutes=mins), **kw)


good = msg("g", 1, subject="Your verification code",
           sender="mailer.greenhouse.io",
           body="Verify your email for your Cloudflare application. Code: CFA11111")

# --- the happy path -------------------------------------------------------------------
mc.reset_consumed()
reply = call({"reqId": "r1", "since": SINCE, "company": "Cloudflare", "key": "job-1"},
             reader=mc.FakeReader([good]))
check(reply["type"] == "answers", "replies on the answers channel")
check(reply["reqId"] == "r1", "echoes the reqId the extension is awaiting")
check(reply["answers"]["ok"] is True, "ok on a hit")
check(reply["answers"]["code"] == "CFA11111", "returns the code")

# --- nothing to find ------------------------------------------------------------------
mc.reset_consumed()
reply = call({"reqId": "r2", "since": SINCE, "company": "Cloudflare"},
             reader=mc.FakeReader([]))
check(reply["answers"]["ok"] is False and reply["answers"]["code"] is None,
      "empty inbox -> ok:false, no code")
check(reply["reqId"] == "r2", "still replies when there is nothing to send")

# --- an old code is not offered -------------------------------------------------------
mc.reset_consumed()
stale = msg("s", -30, subject="Your verification code", sender="mailer.greenhouse.io",
            body="Code: OLD11111")
reply = call({"reqId": "r3", "since": SINCE, "company": "Cloudflare"},
             reader=mc.FakeReader([stale]))
check(reply["answers"]["code"] is None, "a code that predates the submit is not returned")

# --- no reader configured -------------------------------------------------------------
# Stubbed rather than leaning on the real environment. This test used to call the real
# make_reader() and passed only because no credentials existed yet; the moment they were
# configured it started reaching the live network and failed. A test must not read ambient
# state — the same trap JOBCOPILOT_HOME set for the data-layer tests.
def unconfigured():
    raise NotImplementedError("no mail reader configured — set GMAIL_ADDRESS ...")


mc.reset_consumed()
reply = call({"reqId": "r4", "since": SINCE, "company": "Cloudflare"}, maker=unconfigured)
check(reply["answers"]["ok"] is False, "unconfigured reader -> ok:false, not a crash")
check(reply["answers"]["why"] == "no mail reader configured",
      "and says WHY, so the panel can tell him it is not set up")

# --- a broken mail server must not take the fill down ---------------------------------
class ExplodingReader(mc.MailReader):
    def search(self, since):
        raise OSError("connection reset by peer")


mc.reset_consumed()
reply = call({"reqId": "r5", "since": SINCE, "company": "Cloudflare"}, reader=ExplodingReader())
check(reply["answers"]["ok"] is False and reply["answers"]["why"] == "mail lookup failed",
      "a mail server error is caught and reported, never raised")
check(reply["reqId"] == "r5", "and the extension's await still resolves")

# --- a missing `since` must not silently widen the window -----------------------------
# No since = "now", which can only ever return LESS than a real click time would. The
# failure mode of a bug here is offering a stale code, so it has to fail closed.
mc.reset_consumed()
reply = call({"reqId": "r6", "company": "Cloudflare"}, reader=mc.FakeReader([good]))
check(reply["answers"]["code"] is None,
      "a missing `since` falls back to now() and finds nothing, rather than any recent code")

# --- the code value never reaches a log -----------------------------------------------
import io
import contextlib

mc.reset_consumed()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    call({"reqId": "r7", "since": SINCE, "company": "Cloudflare", "key": "job-9"},
         reader=mc.FakeReader([good]))
check("CFA11111" not in buf.getvalue(), "the code itself is never printed to the log")
check("Cloudflare" in buf.getvalue(), "but the company is, so a run can be followed")

print()
print(f"verify_code: {CHECKS - len(FAILED)}/{CHECKS} checks passed")
if FAILED:
    for f in FAILED:
        print("  FAILED:", f)
    sys.exit(1)
