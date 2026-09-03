#!/usr/bin/env python3
"""
bridge.py — the Apply control plane (Telegram <-> browser extension).

Why this exists: the extension fills what it can deterministically (Tier 0, zero
Claude credits), but some fields need Parth — always_ask values (salary), low-confidence
field mappings (Tier 1), and answer-bank drafts (Tier 2). Those can't live in the
extension: they need a human in the loop and, later, Claude API calls. This local
process is that loop.

Shape (locked 2026-07-10):
  * Runs on Parth's laptop, only while he's applying.
  * Holds secrets in ENV only (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID; ANTHROPIC_API_KEY
    added when Tier 1/2 drafting lands). Never in files.
  * Talks to Telegram (zero custom UI): sends a prompt, waits for a reply or a 👍 tap.
  * Exposes a localhost WebSocket the extension's background.js connects to.

Protocol over the WebSocket (JSON per message):
  extension -> bridge:
    {type:"ping"}
    {type:"ask", reqId, context?:{company?, role?, url?},
                 items:[{id, label, why?, draft?, options?:[str], required?}]}
        # options -> rendered as tappable inline-keyboard buttons (reply still overrides);
        # draft   -> a 👍 button to accept it; neither -> free-text reply.
    {type:"review", reqId, context?, summary:[{label,value}], warnings?:[str]}
        # end-of-form gate: shows the full summary + a ✅/✋ tap; returns {__review:"ok"|"hold"}.
    {type:"queue", reqId, max_per_day?, max_per_company?}
        # orchestrator asks for the day's work list; reply answers = {jobs:[...], stats:{...}}.
    {type:"collect", reqId, job:{dedup_key, company, title, location, url, ...},
                     questions:[{id, label, why?, answer_key?, options?, required?}],
                     filled:[{label,value}]}
        # PHASE 1 (batch): report what one job still needs and move on — nobody waits for
        # Parth here. Written to bridge/pending/<dedup_key>.json; he answers on Telegram at
        # his own pace. Reply answers = {ok, added} (added=false => already pending).
    {type:"approved", reqId}
        # PHASE 3 (batch): the approved pile, each with Parth's answers attached, ready to
        # fill + submit unattended. Reply answers = {jobs:[...]}.
    {type:"applied", reqId, record:{dedup_key?, company, title, location, url}}
        # log a CONFIRMED application to applied.json (idempotent); reply answers = {ok, added}.
        # dedup_key is optional: the on-page panel can't know it (the scraper derives it from
        # API fields the page doesn't carry), so we resolve it here — see resolve_dedup_key.
    {type:"drafts", reqId, context?:{company?}, items:[{id, answer_key?, draft?}]}
        # the on-page panel asking what the answer bank has for these questions, with no
        # Telegram involved (Parth is at the keyboard). Reply answers = {id: {draft, bank_id}}.
    {type:"verify_code", reqId, since, company?, role?, key?}
        # a board is asking for an emailed verification code and Parth has ALREADY acted
        # (clicked Submit, or tapped the panel's code button). `since` = that click, epoch
        # seconds, which binds the code to this submit. Reply answers = {ok, code, why}.
        # Never sent on the queue's own initiative — see handle_verify_code.
    {type:"bank_touch", reqId, bank_id}
        # the panel reporting that Parth ACCEPTED a bank draft, so the row counts as used.
        # Only ever sent on accept — see handle_bank_touch. Reply answers = {ok}.
    {type:"notify", reqId, text}
        # post a status line to Telegram (orchestrator progress); reply answers = {ok}.
    {type:"log_report", reqId, report:{...}}
        # append fill.js's per-run report (filled/skipped/needsInput/flagged/errors/...) to
        # bridge/logs/fill_reports.jsonl for later audit, since nothing else persists it;
        # reply answers = {ok}. Best-effort — a log failure never blocks the fill.
    {type:"audit_dump", reqId, company?, role?, url?, summary?:[{label,value}], screenshot}
        # captured by background.js right before the trusted submit click (Fable's
        # audit-trail ask): a full-page-viewport PNG (data: URL) + the field-value summary,
        # saved to bridge/logs/audit/ so a bad mapping's blast radius is knowable after the
        # fact; reply answers = {ok, path}. Best-effort — never blocks the submit.
  bridge -> extension:
    {type:"pong"}
    {type:"hb"}                              # heartbeat; keeps the MV3 worker alive
    {type:"answers", reqId, answers:{id: value}}
    {type:"error", error}

Two asking modes, deliberately:
  * LIVE (`ask`/`review`): a manual panel fill. Parth is at the browser, the form is open,
    and the handler blocks on his reply. Sequential — one item at a time, the next chat
    message (or 👍) is its answer.
  * BATCH (`collect` + Telegram + `approved`): the daily queue. Nothing blocks and nothing
    times out; the questions live in bridge/pending/ and Telegram is a stateless view over
    them, so Parth can answer for five minutes, walk away for an hour, and pick up exactly
    where he left off — across a bridge restart or an extension reload.
A typed reply is routed to the LIVE flow only while a live ask is actually in flight
(LOCK held); otherwise it answers the batch.

Run:  TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python bridge/bridge.py
See bridge/README.md for BotFather + chat-id setup.
"""
from __future__ import annotations

import asyncio
import base64
import html
from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
import pathlib
import re
import socket
import sys
import time

import requests
import urllib3.util.connection as _urllib3_conn
import websockets

import pending as pending_mod
import triage as triage_mod
import drafter as drafter_mod  # Tier-2 essay drafting via the Claude Code CLI
import mailcode as mailcode_mod  # emailed verification codes, human-triggered only

# The shared pipeline modules live in core/ (data-layer tools, not bridge ones), so put the
# repo root on the path before importing them. triage.py is a sibling and needs no help.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from core import apply_log as apply_log_mod  # Workday entry-route policy (reads applied.json)
from core import bank as bank_mod
from core import paths as paths_mod       # the one place that knows where anything lives
from core import profile as profile_mod  # resume track tags + display names for /resume
from core import referral as referral_mod  # pasted-profile resolution + the outreach store
from core import scraper as scraper_mod  # dedup_key — the panel path must mint the same key


# --- network setup: pin the family that actually works + reuse one connection --------
# The logs once showed a near-constant ~21s per Telegram call — the signature of an IPv6
# route to api.telegram.org that times out before falling back to IPv4, paid fresh on
# every request. The cure was to pin outbound connections to one family so the dead route
# is never tried; route every Telegram call through ONE persistent Session too, so the
# TCP+TLS handshake happens once (keep-alive) instead of per message.
#
# But WHICH family is dead is a property of the NETWORK, not of Telegram, and it changes:
# the hardcoded IPv4 pin worked for a month, then died on a network whose IPv4 path was
# down entirely (and whose router answered every A query with a wildcard parking IP) while
# IPv6 reached Telegram in under a second. A VPN being on or off flips it back.
#
# So measure instead of guessing, and re-measure when the measurement stops holding (see
# _RefamilyingAdapter below). Override with TELEGRAM_IP_FAMILY=4|6|auto when needed.
TG_HOST = "api.telegram.org"


def _probe_family(host: str = TG_HOST, port: int = 443, timeout: float = 3.0):
    """Return the address family that can actually open a socket to host, else None.

    Tries IPv4 first (the historical good path), then IPv6. A blocked route shows up
    here as the same timeout it would cost on every single API call.
    """
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
        except socket.gaierror:
            continue
        for *_, addr in infos:
            try:
                with socket.socket(family, socket.SOCK_STREAM) as s:
                    s.settimeout(timeout)
                    s.connect(addr)
                return family
            except OSError:
                break  # one address is enough of a sample; the whole family is routed alike
    return None


def _resolve_family():
    want = os.environ.get("TELEGRAM_IP_FAMILY", "").strip().lower()
    if want in ("4", "ipv4", "inet"):
        return socket.AF_INET
    if want in ("6", "ipv6", "inet6"):
        return socket.AF_INET6
    if want in ("auto", "any", "unspec"):
        return socket.AF_UNSPEC
    # Unset: measure. If neither family connects the box is offline — fall back to
    # AF_UNSPEC so the failure surfaces as a normal request error, not a bad pin.
    return _probe_family() or socket.AF_UNSPEC


# Resolved LAZILY, on the first outbound connection. Probing at import time would make
# every test file that imports this module pay the dead family's timeout, and the tests
# never open a socket.
_IP_FAMILY = None


def _allowed_gai_family():
    global _IP_FAMILY
    if _IP_FAMILY is None:
        _IP_FAMILY = _resolve_family()
    return _IP_FAMILY


def _family_name() -> str:
    return {socket.AF_INET: "IPv4", socket.AF_INET6: "IPv6"}.get(_IP_FAMILY, "auto")


_urllib3_conn.allowed_gai_family = _allowed_gai_family

SESSION = requests.Session()


# A pin measured once at boot is still a pin: the bridge runs for days, and the family
# that worked at startup can stop working mid-run (a VPN dropping is the everyday case —
# turning one ON is what made IPv4 usable here in the first place). Cached forever, that
# leaves every later call hanging on a dead route until someone restarts the bridge —
# the exact stall the pin exists to avoid.
#
# So treat a connection failure as evidence the measurement expired: drop the cached
# family and the keep-alive pool with it (pooled sockets belong to the dead family), and
# the next call re-probes. Only for connect/timeout errors — an HTTP status is Telegram
# answering, which proves the route is fine.
class _RefamilyingAdapter(requests.adapters.HTTPAdapter):
    def send(self, *args, **kwargs):
        global _IP_FAMILY
        try:
            return super().send(*args, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if _IP_FAMILY is not None:
                dlog(f"[telegram] {_family_name()} stopped working — re-probing next call")
                _IP_FAMILY = None
                self.poolmanager.clear()
            raise


SESSION.mount("https://", _RefamilyingAdapter())

# A non-WebSocket TCP connection to the WS port (a port probe, a browser hitting the URL)
# makes the websockets server log a full handshake traceback. It's harmless — the real
# extension connects fine — so raise this logger's floor to silence that noise. Genuine
# server errors are CRITICAL and still get through.
logging.getLogger("websockets.server").setLevel(logging.CRITICAL)


def redact(s) -> str:
    """Strip the bot token out of anything headed for a log.

    requests puts the full request URL in its exception text, and every Telegram URL
    embeds the token — so a plain `print(f"...{e}")` on a network blip writes the bot's
    credentials to disk in plaintext. The whole point of keeping secrets in env is
    undone by that one line. Anything derived from a Telegram exception goes through
    here first.
    """
    return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot<redacted>", str(s))


def _load_dotenv():
    """Load bridge/.env (gitignored) into the environment if present. Real env vars
    already set take precedence, so an exported value always wins over the file."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"

WS_HOST = "127.0.0.1"
WS_PORT = 8765
HEARTBEAT_SECS = 15          # < MV3's ~30s idle kill, so the worker survives a slow reply
POLL_TIMEOUT = 25            # Telegram long-poll seconds

# The Telegram poller fills this; the ask handler drains it. One browser, one user,
# so a single global inbox is all we need. Created inside main() (needs the loop).
INBOX: "asyncio.Queue[dict]" = None  # type: ignore
LOCK: asyncio.Lock = None            # type: ignore
CLIENTS: set = set()                 # connected extension sockets, for unsolicited pushes (stop)
ACTIVE_HANDLER: asyncio.Task = None  # the in-flight ask/review task, so a reconnect can cancel it
STOP_WORDS = {"/stop", "stop", "cancel", "/cancel", "halt"}

# Fill reports and audit dumps quote real form values, so they are state, not scratch:
# they follow JOBCOPILOT_HOME with everything else. Unset, this stays bridge/logs.
LOG_PATH = paths_mod.FILL_REPORTS
AUDIT_DIR = paths_mod.AUDIT_DIR
AUDIT_INDEX_PATH = paths_mod.AUDIT_INDEX


# --- timing/debug logging -----------------------------------------------------------
# Slowness is almost always a slow Telegram round-trip or a handler blocked on the
# LOCK/INBOX. These logs stamp every such call with its wall-clock cost so a slow run
# shows exactly which step ate the time. Enabled by default; set BRIDGE_DEBUG=0 to mute.
DEBUG = os.environ.get("BRIDGE_DEBUG", "1").strip() not in ("0", "false", "", "no")


def dlog(msg: str) -> None:
    if DEBUG:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# --- Telegram (blocking HTTP; always called via asyncio.to_thread) ------------------

def tg_send(text: str, buttons=None) -> int:
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    t0 = time.monotonic()
    r = SESSION.post(f"{API}/sendMessage", json=payload, timeout=30)
    dt = time.monotonic() - t0
    if dt > 1.0:
        dlog(f"[telegram] sendMessage took {dt:.2f}s (slow)")
    r.raise_for_status()
    return r.json()["result"]["message_id"]


def tg_send_plain(text: str) -> int:
    """Send text with NO HTML parsing — for a message meant to be long-pressed and copied.

    Every other send here is HTML, which is right for a card: bold, links, escaped values.
    It is wrong for a drafted message to a real person. `&amp;` and `&#39;` are what an
    escaped apostrophe looks like once it has been copied into LinkedIn, and a stray `<`
    in somebody's note would make Telegram reject the whole send.
    """
    r = SESSION.post(f"{API}/sendMessage",
                     json={"chat_id": CHAT_ID, "text": text,
                           "disable_web_page_preview": True}, timeout=30)
    r.raise_for_status()
    return r.json()["result"]["message_id"]


def tg_answer_callback(cb_id: str, text: str = "") -> None:
    try:
        SESSION.post(f"{API}/answerCallbackQuery",
                     json={"callback_query_id": cb_id, "text": text}, timeout=15)
    except Exception:
        pass  # cosmetic (stops the button spinner); never fatal


def tg_get_updates(offset, timeout=POLL_TIMEOUT):
    t0 = time.monotonic()
    r = SESSION.get(f"{API}/getUpdates",
                    params={"offset": offset, "timeout": timeout},
                    timeout=timeout + 10)
    dt = time.monotonic() - t0
    # A long-poll normally returns fast when there's a message, or after `timeout`s when
    # idle. Anything markedly longer than the requested long-poll window is a network stall.
    if dt > timeout + 2:
        dlog(f"[telegram] getUpdates took {dt:.2f}s (network stall; requested {timeout}s long-poll)")
    r.raise_for_status()
    return r.json()["result"]


def tg_get_me():
    r = SESSION.get(f"{API}/getMe", timeout=15)
    r.raise_for_status()
    return r.json()["result"]


def tg_set_my_commands(commands) -> None:
    """Publish the command list to Telegram so the client shows them in the ☰ menu / the
    '/' autocomplete. `commands` is [(name, description), ...]. Best-effort at the call site;
    a failure here never blocks the bridge from serving."""
    payload = [{"command": name, "description": desc} for name, desc in commands]
    r = SESSION.post(f"{API}/setMyCommands", json={"commands": payload}, timeout=15)
    r.raise_for_status()


# --- Telegram poller: normalize updates into INBOX ---------------------------------

async def drain_startup_backlog() -> int:
    """Skip any messages that arrived before the bridge started, so a stale reply
    from yesterday never gets consumed as today's answer. Returns the next offset."""
    updates = await asyncio.to_thread(tg_get_updates, None, 0)
    return (updates[-1]["update_id"] + 1) if updates else None


async def push_stop():
    """Tell every connected extension to halt the queue (unsolicited WS push), then ack on
    Telegram. Intercepted before INBOX so a /stop is never mistaken for an answer."""
    for ws in list(CLIENTS):
        try:
            await ws.send(json.dumps({"type": "stop"}))
        except Exception:
            pass
    await asyncio.to_thread(tg_send, "🛑 Stopping the queue — no more applications will start. "
                                     "(The current form may still be waiting on you; ✋ Hold it to skip.)")


async def _safe(label: str, coro):
    """Run one poller-dispatched command without letting its failure kill the poller.
    A transient tg_send network error inside /next used to propagate out of the update
    loop and take the WHOLE bridge down mid-batch — the poller is main()'s foreground
    task, so an escaped exception here is a process exit."""
    try:
        await coro
    except Exception as e:
        print(f"[telegram] {label} failed: {redact(e)}", file=sys.stderr)


async def telegram_poller(offset):
    global REFER_WAITING
    while True:
        try:
            updates = await asyncio.to_thread(tg_get_updates, offset)
        except Exception as e:
            print(f"[telegram] getUpdates error: {redact(e)}; retrying", file=sys.stderr)
            await asyncio.sleep(3)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            if "callback_query" in u:
                cq = u["callback_query"]
                data = str(cq.get("data", ""))
                if data == "stop":
                    await asyncio.to_thread(tg_answer_callback, cq["id"], "Stopping")
                    await _safe("stop", push_stop())
                    continue
                if data in ("draft:on", "draft:off"):  # Claude-drafting toggle button
                    await asyncio.to_thread(tg_answer_callback, cq["id"],
                                            "Drafting " + ("on" if data == "draft:on" else "off"))
                    await _safe("/draft", handle_draft_toggle(data == "draft:on"))
                    continue
                if data.startswith(f"{CB}:"):  # batch button — carries its own ids
                    await _safe(f"callback {data!r}", batch_callback(data, cq["id"]))
                    continue
                await INBOX.put({"kind": "callback", "data": data, "cb_id": cq["id"]})
            elif "message" in u and u["message"].get("text"):
                text = u["message"]["text"]
                low = text.strip().lower()
                if low in STOP_WORDS:
                    await _safe("stop", push_stop())
                    continue
                if low in ("/next", "next", "/start", "▶️"):
                    await _safe("/next", serve_next())
                    continue
                if low == "/jobs" or low.startswith("/jobs ") or low.startswith("/jobs\n"):
                    await _safe("/jobs", handle_jobs(text.strip()))
                    continue
                if low in ("/status", "status"):
                    await _safe("/status", send_status())
                    continue
                if low == "/edit" or low.startswith("/edit ") or low.startswith("/edit\n"):
                    await _safe("/edit", handle_edit(text.strip()))
                    continue
                if low == "/resume":  # slash required: "resume" is too plausible as an answer
                    await _safe("/resume", handle_resume())
                    continue
                if low in ("/retry", "retry"):
                    await _safe("/retry", handle_retry())
                    continue
                if low == "/draft" or low.startswith("/draft ") or low.startswith("/draft\n"):
                    await _safe("/draft", handle_draft(text.strip()))
                    continue
                if low in ("/run", "run"):  # manual kick of the P1 collection run
                    await _safe("/run", fire_run_queue("telegram"))
                    continue
                if low == "/apply" or low.startswith("/apply ") or low.startswith("/apply\n"):
                    await _safe("/apply", handle_apply(text.strip()))
                    continue
                if low == "/refer" or low.startswith("/refer ") or low.startswith("/refer\n"):
                    await _safe("/refer", handle_refer(text.strip()))
                    continue
                if low in ("/refers", "/outreach"):
                    await _safe("/refers", handle_outreach_list())
                    continue
                if low == "/cancel" and REFER_WAITING:
                    REFER_WAITING = False
                    await _safe("/cancel", asyncio.to_thread(tg_send, "👍 Forgotten."))
                    continue
                if low in ("/help", "help", "/commands"):
                    await _safe("/help", handle_help())
                    continue
                # Anything else starting with "/" is an attempted command (a typo like
                # "/edt 3"), not an answer — swallowing it as the current question's
                # answer would silently store garbage on a real application.
                if low.startswith("/"):
                    await _safe("unknown command", asyncio.to_thread(
                        tg_send, "❓ Unknown command. Send /help to see them all."))
                    continue
                # An armed /refer eats exactly the next message. Checked BEFORE the answer
                # paths, because a pasted profile swallowed as the current question's answer
                # is a profile submitted to a real employer.
                if REFER_WAITING:
                    REFER_WAITING = False
                    await _safe("/refer paste", resolve_referral(text.strip()))
                    continue
                # A typed reply belongs to the legacy blocking ask ONLY while one is
                # actually in flight (LOCK held) — that's a manual panel fill, where
                # Parth is sat at the browser. Otherwise it answers the batch.
                if LOCK is not None and LOCK.locked():
                    await INBOX.put({"kind": "text", "text": text})
                    continue
                try:
                    if await answer_current(text):
                        continue
                except Exception as e:
                    print(f"[batch] answering {text!r} failed: {redact(e)}", file=sys.stderr)
                await INBOX.put({"kind": "text", "text": text})


# --- WebSocket handlers -------------------------------------------------------------

async def heartbeat(ws):
    """Keep the extension's MV3 service worker alive while it waits on a slow human."""
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_SECS)
            await ws.send(json.dumps({"type": "hb"}))
    except (asyncio.CancelledError, websockets.ConnectionClosed):
        pass


def context_lines(ctx):
    """Header lines describing the job, shown on both the per-field ask and the review, so
    Parth has the context up front. Includes only the facts the extension found."""
    ctx = ctx or {}
    out = []
    pairs = [("company", "🏢"), ("role", "💼"), ("location", "📍"),
             ("employment_type", "🕒"), ("compensation", "💰"), ("resume", "📄")]
    for key, icon in pairs:
        val = str(ctx.get(key) or "").strip()
        if val:
            out.append(f"{icon} {html.escape(val)}")
    return out


def bank_draft(item: dict, company: str | None) -> tuple[str | None, str | None]:
    """The draft to offer for this item, sourced from answer_bank.yaml.

    Returns (draft_text, bank_id). bank_id is None when the draft didn't come from the
    bank, and is what lets the caller touch() the row if Parth actually accepts it.
    A draft the extension already supplied always wins. Never raises: a broken bank
    degrades to asking Parth cold, which is the pre-bank behaviour.
    """
    if item.get("draft"):
        return str(item["draft"]), None
    key = item.get("answer_key")
    if not key:
        return None, None
    try:
        row = bank_mod.resolve(key, company)
    except Exception as e:
        dlog(f"[ask] bank lookup for {key!r} failed ({e}) — asking without a draft")
        return None, None
    if not row:
        return None, None
    return str(row.get("answer") or "").strip() or None, row.get("id")


async def resolve_draft(item: dict, context: dict | None, force: bool = False) -> tuple[str | None, str | None]:
    """The draft to offer, bank first then Claude. A saved bank answer always wins (it's
    Parth's own confirmed wording); only when the bank has nothing AND the item is a
    draftable essay do we spend a Claude Code call to write one. `force` is Parth explicitly
    asking for a draft from the panel: it waives the answer_key requirement (the taxonomy
    only tags question shapes it has SEEN, so a one-off prompt reaches us untagged) but not
    the essay shape — a multiple-choice item is a pick and drafting prose for it is useless.
    A generated draft is
    stamped onto `item["draft"]` so any later read of the same item (send_question, a
    re-push) reuses it for free instead of drafting again. bank_id is None for a Claude
    draft, so accepting it never touch()es a bank row. Never raises: on any failure the
    caller falls back to asking cold, the pre-drafting behaviour."""
    draft, bank_id = bank_draft(item, (context or {}).get("company"))
    wanted = drafter_mod.is_draftable(item) or (force and drafter_mod.drafts_on_request(item))
    if draft or not drafter_mod.available() or not wanted:
        return draft, bank_id
    try:
        text = await asyncio.to_thread(drafter_mod.draft_answer, item, context or {}, force)
    except Exception as e:
        dlog(f"[draft] claude draft for {item.get('id')!r} failed ({e}) — asking cold")
        return None, None
    if text:
        item["draft"] = text  # cache on the item so downstream reads reuse it
        return text, None
    return None, None


async def handle_ask(msg, ws):
    """Ask each item on Telegram in turn; collect answers; return them over WS."""
    items = msg.get("items", [])
    req_id = msg.get("reqId")
    ctx = msg.get("context") or {}
    answers: dict = {}

    _lock_t0 = time.monotonic()
    async with LOCK:
        _lock_dt = time.monotonic() - _lock_t0
        if _lock_dt > 0.5:
            dlog(f"[ask] waited {_lock_dt:.2f}s for LOCK (a previous ask/review was still open)")
        # Any chat messages that piled up before this ask are not answers to it.
        while not INBOX.empty():
            INBOX.get_nowait()

        # Header so Parth knows which application (and which job) these questions belong to.
        info = context_lines(ctx)
        if info:
            head = "🧩 <b>New application needs you</b>\n" + "\n".join(info)
            head += f"\n<i>{len(items)} question(s)</i>"
            await asyncio.to_thread(tg_send, head)

        for item in items:
            label = item.get("label", "(unlabeled)")
            why = item.get("why", "")
            draft, bank_id = await resolve_draft(item, ctx)
            options = item.get("options") or []
            required = bool(item.get("required"))

            text = f"📝 <b>{html.escape(str(label))}</b>"
            if why:
                text += f"\n<i>{html.escape(str(why))}</i>"
            # Tell Parth up front whether he has to answer this one.
            text += "\n🔴 <b>Required</b>" if required else "\n⚪ <b>Optional</b>"
            buttons = []
            if options:
                # Tappable choices. callback_data carries the option index (compact, well
                # under Telegram's 64-byte cap); the answer is looked up from `options`.
                buttons += [[{"text": str(o)[:60], "callback_data": f"opt:{i}"}]
                            for i, o in enumerate(options)]
                hint = "tap an option, or reply to type your own"
            elif draft is not None and str(draft) != "":
                # Say where the draft came from: a bank draft is Parth's own vetted words,
                # so it warrants a quicker 👍 than one a model wrote for him (Tier 2).
                # Display clamped; the 👍 answer uses the full `draft` variable below.
                src = "your saved answer" if bank_id else "drafted"
                text += f"\n\n<b>Draft</b> <i>({src})</i>:\n{html.escape(_clamp_draft(draft))}"
                buttons.append([{"text": "👍 Use draft", "callback_data": "ok"}])
                hint = "reply to edit, or tap 👍 to use the draft"
            else:
                hint = "reply with your answer"
            # Optional questions get a one-tap way out — no need to type anything to skip.
            if not required:
                buttons.append([{"text": "⏭️ Leave blank", "callback_data": "skip"}])
                hint += " · or ⏭️ leave it blank"
            text += f"\n\n<i>{hint}</i>"

            await asyncio.to_thread(tg_send, text, buttons or None)

            ev = await INBOX.get()
            if ev["kind"] == "callback":
                data = str(ev.get("data", ""))
                if data == "skip":  # optional field -> leave it blank (empty answer)
                    await asyncio.to_thread(tg_answer_callback, ev["cb_id"], "Left blank")
                    answers[item.get("id")] = ""
                elif data.startswith("opt:"):
                    try:
                        idx = int(data.split(":", 1)[1])
                    except ValueError:
                        idx = -1
                    picked = options[idx] if 0 <= idx < len(options) else ""
                    await asyncio.to_thread(tg_answer_callback, ev["cb_id"], "Selected")
                    answers[item.get("id")] = picked
                else:  # "ok" -> use the draft (or empty if there wasn't one)
                    await asyncio.to_thread(tg_answer_callback, ev["cb_id"], "Using draft")
                    answers[item.get("id")] = draft if draft is not None else ""
                    # Only an ACCEPTED draft counts as a use. An answer Parth keeps
                    # retyping stays untouched and eventually shows up in `bank.py stale`
                    # — which is exactly the signal that it needs rewriting.
                    if bank_id:
                        await asyncio.to_thread(bank_mod.touch, bank_id)
            else:
                answers[item.get("id")] = ev["text"]

    await ws.send(json.dumps({"type": "answers", "reqId": req_id, "answers": answers}))
    await asyncio.to_thread(tg_send, "✅ Got it — sending back to the form.")


async def handle_review(msg, ws):
    """End-of-form gate: show everything that will be submitted + any still-empty required
    fields, and wait for a ✅/✋ tap. Returns {__review: "ok"|"hold"}. Never submits."""
    ctx = msg.get("context") or {}
    summary = msg.get("summary") or []
    warnings = msg.get("warnings") or []
    req_id = msg.get("reqId")

    # Budget per LINE, never a blind text[:N] slice: cutting mid-<b> makes Telegram
    # reject the whole message ("can't parse entities") and the review dies as
    # "bridge disconnected". Each line here is self-contained escaped HTML, so
    # dropping whole lines keeps the tags balanced. (Same approach as send_summary.)
    head = ["🧾 <b>Final review — nothing is submitted yet</b>"]
    head += context_lines(ctx)
    head += ["", "<b>Will be submitted:</b>"]
    tail = []
    if warnings:
        tail = ["", "⚠️ <b>Still empty (required):</b> "
                + html.escape(_one_line(", ".join(str(w) for w in warnings), 400))]
    used = sum(len(l) + 1 for l in head + tail)
    body = []
    for i, it in enumerate(summary):
        lab = html.escape(_one_line(it.get("label", ""), 80))
        val = html.escape(_one_line(it.get("value", ""), 300))
        line = f"• <b>{lab}</b>: {val}"
        if used + len(line) > 3900:            # Telegram hard cap is 4096
            body.append(f"<i>…and {len(summary) - i} more field(s)</i>")
            break
        body.append(line)
        used += len(line) + 1
    text = "\n".join(head + body + tail)

    buttons = [[{"text": "✅ Looks good", "callback_data": "rev:ok"}],
               [{"text": "✋ Hold", "callback_data": "rev:hold"}]]

    async with LOCK:
        while not INBOX.empty():
            INBOX.get_nowait()
        await asyncio.to_thread(tg_send, text, buttons)
        ev = await INBOX.get()
        if ev["kind"] == "callback":
            ans = "ok" if str(ev.get("data", "")) == "rev:ok" else "hold"
            await asyncio.to_thread(tg_answer_callback, ev["cb_id"], "Noted")
        else:
            ans = "hold"   # a typed reply during review = don't proceed

    await ws.send(json.dumps({"type": "answers", "reqId": req_id, "answers": {"__review": ans}}))
    if ans == "ok":
        # When the orchestrator drives the fill it submits on ✅ itself; the manual popup
        # path still needs Parth's own submit tap. context.autoSubmit distinguishes them.
        if ctx.get("autoSubmit"):
            await asyncio.to_thread(tg_send, "👍 Looks good — <b>submitting it now…</b> I'll confirm when it lands.")
        else:
            await asyncio.to_thread(tg_send, "👍 Looks good — tap <b>Submit</b> in the browser when ready.")
    else:
        await asyncio.to_thread(tg_send, "✋ Holding. Nothing submitted — fix what you need and re-run the fill.")


# --- Batch flow: Telegram as a stateless view over bridge/pending/ -------------------
#
# There is deliberately NO cursor. "The current question" is always derivable from disk:
# the first unanswered question of the oldest awaiting job. That's what makes an
# hour-long gap, a bridge restart, and an extension reload all survivable — there is no
# session to lose. Buttons carry their own job+question ids, so even a tap on a message
# from yesterday lands on the right record (or is politely ignored if it's gone).

CB = "b"  # callback_data namespace for batch buttons, keeps them off the legacy ask path
TG_LIMIT = 3400  # Telegram caps a message at 4096; leave room for the tail line


def _one_line(v, cap: int = 180) -> str:
    v = str(v if v is not None else "").replace("\n", " ").strip()
    return (v[:cap] + "…") if len(v) > cap else v


def _clamp_draft(draft: str, cap: int = 1500) -> str:
    """Displayed draft text only — the 👍 tap re-resolves the FULL draft from the bank at
    callback time, so trimming here never truncates what gets filled. Without this a long
    bank answer pushes the message past Telegram's 4096 cap and tg_send raises."""
    d = str(draft)
    return d if len(d) <= cap else d[:cap] + "…"


def job_title_line(rec: dict) -> str:
    bits = [f"🏢 <b>{html.escape(str(rec.get('company') or '?'))}</b>"]
    if rec.get("title"):
        bits.append(html.escape(str(rec["title"])))
    return " · ".join(bits)


def job_meta_lines(rec: dict) -> list[str]:
    """The free metadata — scraped already, no Claude involved. Parth reads the real JD
    at the link if he wants it."""
    out = []
    meta = [f"{icon} {html.escape(str(rec[k]))}"
            for k, icon in (("location", "📍"), ("compensation", "💰"),
                            ("employment_type", "🕒"))
            if rec.get(k)]
    if meta:
        out.append(" · ".join(meta))
    if rec.get("url"):
        out.append(f'🔗 <a href="{html.escape(str(rec["url"]))}">open the job post</a>')
    return out


def _found_on(rec: dict) -> str:
    """When phase 1 collected this role, as "24 Aug". The batch runs weeks deep, so
    "found today" and "found three weeks ago" are very different things to be answering."""
    raw = str(rec.get("created") or "")[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%d %b").lstrip("0")
    except ValueError:
        return raw


# The last job a card was sent for. The batch is otherwise cursorless on purpose, and this
# stays deliberately in-memory rather than on disk: it is not state anyone needs to be
# correct about, only a de-duplicator so the card doesn't repeat above every question of
# the same job. Losing it to a restart re-shows the card once, which is the right answer
# anyway — he has been away.
_LAST_CARD_KEY = None


def send_job_card(rec: dict) -> None:
    """The little box that goes above a job's FIRST question.

    Answering on the phone, the questions used to arrive with nothing but a bolded
    company name for context: no idea which role at that company, whether it was found
    today or three weeks ago, or how many questions were coming. This is that context,
    once per job, before the asking starts."""
    lines = [f"📋 {job_title_line(rec)}"]
    lines += job_meta_lines(rec)
    tail = [f"📅 found {html.escape(_found_on(rec))}"]
    qs = rec.get("questions") or []
    if qs:
        tail.append(f"❓ {pending_mod.answered_count(rec)}/{len(qs)} answered")
    if rec.get("resume"):
        tail.append(f"📄 {html.escape(_one_line(str(rec['resume']), 60))}")
    lines.append(" · ".join(tail))
    tg_send("\n".join(lines))


def send_question(rec: dict, q: dict) -> None:
    key, qid = rec["dedup_key"], q["id"]
    qs = rec.get("questions") or []
    idx = next((i for i, x in enumerate(qs) if x.get("id") == qid), 0) + 1

    text = f"{job_title_line(rec)} — Q{idx} of {len(qs)}\n"
    text += "🔴 <b>Required</b>" if q.get("required") else "⚪ <b>Optional</b>"
    text += f"\n\n📝 <b>{html.escape(str(q.get('label') or '(unlabeled)'))}</b>"

    buttons = []
    options = q.get("options") or []
    draft, bank_id = bank_draft(q, rec.get("company"))
    if options:
        buttons += [[{"text": str(o)[:60], "callback_data": f"{CB}:o:{key}:{qid}:{i}"}]
                    for i, o in enumerate(options[:10])]
        hint = "tap an option, or reply to type your own"
        if len(options) > 10:  # buttons are capped; don't let the rest silently vanish
            hint = f"{len(options) - 10} more option(s) not shown — reply to type one · " + hint
    elif draft:
        src = "your saved answer" if bank_id else "drafted"
        text += f"\n\n<b>Draft</b> <i>({src})</i>:\n{html.escape(_clamp_draft(draft))}"
        buttons.append([{"text": "👍 Use draft", "callback_data": f"{CB}:a:{key}:{qid}:ok"}])
        hint = "reply to edit, or tap 👍 to use the draft"
    else:
        hint = "reply with your answer"
    if not q.get("required"):
        buttons.append([{"text": "⏭️ Leave blank", "callback_data": f"{CB}:a:{key}:{qid}:blank"}])
        hint += " · or ⏭️ leave it blank"
    # ✋ mid-answer: the summary's skip is unreachable from here (it needs every question
    # answered first), so without this the only way out of a role he doesn't want is to
    # answer the rest of it. Defers rather than deletes — the answers already given are
    # kept, and /jobs brings it back.
    buttons.append([{"text": "⏸ Later", "callback_data": f"{CB}:later"},
                    {"text": "✋ Skip this job", "callback_data": f"{CB}:j:{key}:defer"}])

    text += f"\n\n<i>{hint}</i>"
    tg_send(text, buttons)


def send_summary(rec: dict) -> None:
    """Everything that will be submitted, plus the three ways out. This is the gate:
    nothing reaches an employer without a tap here."""
    key = rec["dedup_key"]
    lines = [f"✅ {job_title_line(rec)} — ready to submit"]
    lines += job_meta_lines(rec)
    if rec.get("resume"):
        lines.append(f"📄 {html.escape(str(rec['resume']))}")
    # Phase-1 saw these required fields still empty in the DOM (beyond the questions) —
    # Parth shouldn't approve blind; phase 3 will retry them, the ATS validates last.
    # Filtered through open_warnings: the raw snapshot lists fields this same record has
    # since filled, and a gate whose warning line is mostly false is a gate you learn to
    # tap through. See pending.open_warnings.
    warns = pending_mod.open_warnings(rec)
    if warns:
        lines.append("⚠️ <i>Form still showed empty (required): "
                     + html.escape(_one_line(", ".join(warns), 250)) + "</i>")
    lines.append("")

    rows = list(rec.get("filled") or [])
    rows += [{"label": q.get("label"), "value": q.get("answer")}
             for q in (rec.get("questions") or [])]
    used, shown = sum(len(x) for x in lines), 0
    for r in rows:
        val = _one_line(r.get("value"))
        if not val:
            continue
        line = f"• <b>{html.escape(str(r.get('label') or '?'))}:</b> {html.escape(val)}"
        if used + len(line) > TG_LIMIT:
            lines.append(f"<i>…and {len(rows) - shown} more fields</i>")
            break
        lines.append(line)
        used += len(line)
        shown += 1

    lines.append("\n<i>/edit to change an answer · /resume to swap the resume — before you ✅</i>")
    tg_send("\n".join(lines), [
        [{"text": "✅ Submit", "callback_data": f"{CB}:j:{key}:approve"}],
        [{"text": "✋ Skip for now", "callback_data": f"{CB}:j:{key}:skip"},
         {"text": "🚫 Never show again", "callback_data": f"{CB}:j:{key}:reject"}],
    ])


# --- idle debounce: Parth goes quiet -> send out what he already approved ------------
#
# NOT a deadline. The 5 minutes only ever releases jobs he has explicitly ✅'d; a
# half-answered form isn't approved, so it can't be caught by this. Nothing he hasn't
# seen is ever submitted.

IDLE_FLUSH_SECS = int(os.environ.get("APPLY_IDLE_FLUSH_SECS", "300") or 300)
IDLE_POLL_SECS = 15    # how often to check; tests shrink this
# How many times phase 3 may try to submit one approved job before giving up and
# HOLDing it. Without a cap, a job that never confirms (swallowed submit, verification
# gate, hand-needed form) is re-flushed every IDLE_FLUSH_SECS forever — the nag loop
# Parth hit. Each flush already runs the extension's own multi-pass submit retry, so a
# couple of whole-flush attempts is plenty before it's clearly his to finish by hand.
MAX_FLUSH_ATTEMPTS = int(os.environ.get("APPLY_MAX_FLUSH_ATTEMPTS", "2") or 2)
LAST_ACTIVITY = 0.0
_LAST_FLUSH = 0.0
_NUDGED = False        # told him Chrome is gone; don't tell him again until it's back

# --- autonomous daily run: a file the launcher drops to kick off P1 collection --------
#
# The launcher (run_daily.ps1, fired by Windows Task Scheduler) can't poke the WS to
# start the run: the bridge SUPERSEDES on every new connection, so a second client would
# kill Parth's live extension session. Instead it drops a sentinel FILE that this process
# watches — WS-free, token-free local IPC that works whether the bridge is cold-started
# by the same launcher or was already running from earlier in the day. `/run` on Telegram
# is the manual equivalent. The run is P1 collection only; nothing is ever submitted here.
TRIGGER_FILE = paths_mod.RUN_TRIGGER
# The web console (web/) drops this to ask for a flush of the approved pile without
# opening a WebSocket (which would supersede Parth's live extension session). Same
# file-handoff pattern as .run_trigger; watched by flush_trigger_watcher below.
FLUSH_TRIGGER_FILE = paths_mod.FLUSH_TRIGGER
TRIGGER_POLL_SECS = 3            # how often to look for the sentinel; tests shrink this
RUN_CLIENT_WAIT_SECS = 120       # how long to wait for the extension to connect before giving up
_LAST_RUN_KICK = 0.0             # cooldown so a double-drop / fat-fingered /run can't double-fire
RUN_KICK_COOLDOWN_SECS = 60


def touch_activity() -> None:
    """Called on every batch interaction — resets the idle countdown."""
    global LAST_ACTIVITY
    LAST_ACTIVITY = time.monotonic()


async def push_flush(count: int) -> None:
    """Unsolicited push: tell the extension to fill + submit the approved pile."""
    for ws in list(CLIENTS):
        try:
            await ws.send(json.dumps({"type": "flush"}))
        except Exception:
            pass
    c = await asyncio.to_thread(pending_mod.counts)
    msg = f"📤 Sending out {count} approved application(s) now."
    if c["awaiting"]:
        msg += (f"\n<i>{c['awaiting']} still waiting on you "
                f"({c['questions_left']} question(s)) — /next when you're free.</i>")
    await asyncio.to_thread(tg_send, msg)


async def push_run_queue() -> None:
    """Unsolicited push: tell the extension to start a P1 collection run (runQueue).
    Fire-and-forget — runQueue posts its own '🔁 Reading N role(s)' progress to Telegram,
    so we stay quiet here on success and only speak up when nothing's listening."""
    for ws in list(CLIENTS):
        try:
            await ws.send(json.dumps({"type": "run_queue"}))
        except Exception:
            pass


async def fire_run_queue(source: str) -> None:
    """Kick off the daily collection run. Waits briefly for the extension to connect —
    on a cold start the launcher opens Chrome and the MV3 worker takes a moment to wake
    and dial in — then pushes run_queue. If Chrome never shows up, say so instead of
    failing silently; a missed daily run must not look like a completed one."""
    global _LAST_RUN_KICK
    now = time.monotonic()
    if now - _LAST_RUN_KICK < RUN_KICK_COOLDOWN_SECS:
        dlog(f"[run] ignoring {source} kick - one fired {now - _LAST_RUN_KICK:.0f}s ago")
        return
    waited = 0.0
    while not CLIENTS and waited < RUN_CLIENT_WAIT_SECS:
        await asyncio.sleep(TRIGGER_POLL_SECS)
        waited += TRIGGER_POLL_SECS
    if not CLIENTS:
        await asyncio.to_thread(
            tg_send,
            "🔌 Tried to start the daily collection run, but Chrome/the extension never "
            "connected.\n<i>Open Chrome and send /run when you're ready.</i>")
        return
    _LAST_RUN_KICK = time.monotonic()
    touch_activity()  # a fresh run resets the idle clock; don't flush a stale pile mid-collect
    dlog(f"[run] firing runQueue ({source})")
    await push_run_queue()


async def trigger_watcher() -> None:
    """Watch for the launcher's sentinel file and fire the run when it appears.

    The file is the Task Scheduler -> bridge handoff (see TRIGGER_FILE). We remove it
    BEFORE firing so a run that takes a while can't be re-triggered by the same drop, and
    a stale file left over from a crash fires exactly one run on the next boot, not a loop.
    """
    # Clear any sentinel left from a previous process death so boot doesn't auto-run
    # off yesterday's trigger before we've even established what's connected.
    try:
        TRIGGER_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    while True:
        await asyncio.sleep(TRIGGER_POLL_SECS)
        try:
            if TRIGGER_FILE.exists():
                TRIGGER_FILE.unlink(missing_ok=True)
                dlog("[run] sentinel seen - starting the daily collection run")
                await fire_run_queue("scheduled")
        except Exception as e:
            print(f"[run] trigger watcher error: {redact(e)}", file=sys.stderr)


async def fire_flush(source: str) -> None:
    """Send the approved pile now, on request from the web console. Same guardrails as the
    idle flush: only fire if the extension is connected and something is actually approved;
    otherwise say so rather than fail silently. Resets the idle clock so the idle-watcher
    doesn't immediately re-fire on top of us."""
    if not CLIENTS:
        c = await asyncio.to_thread(pending_mod.counts)
        if c["approved"]:
            await asyncio.to_thread(
                tg_send,
                f"🔌 Web asked to send {c['approved']} approved application(s), but "
                "Chrome/the extension isn't connected.\n<i>Open Chrome and try again.</i>")
        return
    c = await asyncio.to_thread(pending_mod.counts)
    if not c["approved"]:
        dlog(f"[flush] {source} flush ignored - nothing approved")
        return
    touch_activity()
    dlog(f"[flush] firing flush of {c['approved']} approved ({source})")
    await push_flush(c["approved"])


async def flush_trigger_watcher() -> None:
    """Watch for the web console's flush sentinel and send the approved pile when it drops.
    Mirrors trigger_watcher: clear any stale file at boot, then unlink BEFORE firing so one
    drop fires exactly one flush."""
    try:
        FLUSH_TRIGGER_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    while True:
        await asyncio.sleep(TRIGGER_POLL_SECS)
        try:
            if FLUSH_TRIGGER_FILE.exists():
                FLUSH_TRIGGER_FILE.unlink(missing_ok=True)
                dlog("[flush] sentinel seen - sending the approved pile")
                await fire_flush("web")
        except Exception as e:
            print(f"[flush] trigger watcher error: {redact(e)}", file=sys.stderr)


async def idle_watcher() -> None:
    """Watch for Parth going quiet, then release the approved pile.

    Self-limiting in the normal case: phase 3 submits each job and sends `applied`,
    which deletes the pending file, so the approved count drops to zero and nothing
    re-fires. _LAST_FLUSH is the backstop for the abnormal case — extension offline,
    or a submit that never confirms — so we don't spam a push every 15 seconds.
    """
    global _LAST_FLUSH, _NUDGED
    while True:
        await asyncio.sleep(IDLE_POLL_SECS)
        try:
            now = time.monotonic()
            if not LAST_ACTIVITY:
                continue
            if not CLIENTS:
                # Chrome is gone: closed, asleep, or the extension is off. It used to also
                # mean "MV3 evicted the worker and nothing will ever reconnect", but the
                # extension now re-dials on a chrome.alarms tick, so a running Chrome comes
                # back within a minute on its own and we never get here. What's left is the
                # genuinely un-fixable case — Chrome isn't running — which only Parth can
                # resolve. Approved work would otherwise sit here in total silence and he'd
                # assume it went out, so tell him ONCE per disconnection: the state can't
                # change until Chrome is back, so repeating it adds no information and just
                # pings his phone at 2am (it did, on 2026-07-16).
                if not _NUDGED:
                    c = await asyncio.to_thread(pending_mod.counts)
                    if c["approved"]:
                        _NUDGED = True
                        await asyncio.to_thread(
                            tg_send,
                            f"🔌 {c['approved']} application(s) approved and ready, but Chrome "
                            f"isn't running.\n<i>Open it and they'll go out on their own within "
                            f"a minute — I won't chase you about it again.</i>")
                continue
            _NUDGED = False  # Chrome is back; a future disconnect is worth one more nudge
            if now - LAST_ACTIVITY < IDLE_FLUSH_SECS:
                continue
            if now - _LAST_FLUSH < IDLE_FLUSH_SECS:
                continue
            c = await asyncio.to_thread(pending_mod.counts)
            if not c["approved"]:
                continue
            _LAST_FLUSH = now
            dlog(f"[idle] {IDLE_FLUSH_SECS}s quiet — flushing {c['approved']} approved")
            await push_flush(c["approved"])
        except Exception as e:
            print(f"[idle] watcher error: {redact(e)}", file=sys.stderr)


async def serve_next() -> None:
    """The whole batch loop: next job wanting attention -> its next question, or its
    summary if it's fully answered. Derived from disk every time."""
    touch_activity()
    rec = await asyncio.to_thread(pending_mod.next_job)
    if rec is None:
        c = await asyncio.to_thread(pending_mod.counts)
        msg = "🎉 Nothing left to answer."
        if c["approved"]:
            msg += f"\n{c['approved']} approved — submitting in the background."
        await asyncio.to_thread(tg_send, msg)
        return
    global _LAST_CARD_KEY
    q = pending_mod.next_question(rec)
    fresh_job = rec["dedup_key"] != _LAST_CARD_KEY
    _LAST_CARD_KEY = rec["dedup_key"]
    if q:
        # The card only leads a job's questions. The summary carries the same facts
        # already, so heading it with a card would just say everything twice.
        if fresh_job:
            await asyncio.to_thread(send_job_card, rec)
        await asyncio.to_thread(send_question, rec, q)
    else:
        await asyncio.to_thread(send_summary, rec)


# --- /jobs: the whole batch as a pickable list ---------------------------------------
#
# /next serves what it thinks is most useful next; this is the override for when Parth
# knows which role he wants to do. Rows are numbered across the whole list (not per page)
# so a number means the same thing on page 3 as on page 1.

JOBS_PAGE = 8      # rows per page — one Telegram screen, and 8 inline buttons stay tappable


def _jobs_listing() -> list[dict]:
    """Every live record, in the order /jobs shows them: the ones wanting answers first
    (in /next's own order, so the list and the queue agree), then the piles that need a
    different kind of attention, then what's already out of his hands."""
    out = pending_mod.serving_order()
    for status in (pending_mod.HELD, pending_mod.SKIPPED, pending_mod.APPROVED):
        out += list(reversed(pending_mod.all_jobs(status)))   # newest first within a pile
    return out


def _jobs_status_label(rec: dict) -> str:
    status = rec.get("status")
    if status == pending_mod.APPROVED:
        return "📤 queued to submit"
    if status == pending_mod.HELD:
        reason = rec.get("hold_reason")
        return "⚠️ needs you" + (f" — {_one_line(reason, 40)}" if reason else "")
    if status == pending_mod.SKIPPED:
        return "✋ skipped"
    return "✅ ready to submit" if pending_mod.is_complete(rec) else "⏳ waiting on you"


def jobs_page(rows: list[dict], page: int) -> tuple[str, list]:
    """Render one page of the listing. Pure — takes the rows, returns text + buttons —
    so the test can read what Parth would see without a bot token."""
    pages = max(1, (len(rows) + JOBS_PAGE - 1) // JOBS_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * JOBS_PAGE
    chunk = rows[start:start + JOBS_PAGE]

    lines = [f"📋 <b>Jobs</b> — {start + 1}–{start + len(chunk)} of {len(rows)}", ""]
    buttons = []
    for i, rec in enumerate(chunk, start + 1):
        lines.append(f"<b>{i}.</b> {job_title_line(rec)}")
        lines.append(f"    📅 {html.escape(_found_on(rec))} · ❓ {pending_mod.progress(rec)} · "
                     f"{html.escape(_jobs_status_label(rec))}")
        if rec.get("status") != pending_mod.APPROVED:
            # An approved job is mid-flight to a real employer; re-opening it under a flush
            # is the one race the store exists to prevent, so it lists but doesn't tap.
            label = _one_line(f"{i} · {rec.get('company') or '?'} — {rec.get('title') or ''}", 55)
            buttons.append([{"text": label,
                             "callback_data": f"{CB}:f:{rec['dedup_key']}"}])

    nav = []
    if page > 0:
        nav.append({"text": "⬅️ Prev", "callback_data": f"{CB}:p:{page - 1}"})
    if page < pages - 1:
        nav.append({"text": "Next ➡️", "callback_data": f"{CB}:p:{page + 1}"})
    if nav:
        buttons.append(nav)

    lines.append("")
    lines.append("<i>Tap one to answer it next — or /next to carry on where you were.</i>")
    return "\n".join(lines), buttons


async def handle_jobs(text: str = "/jobs") -> None:
    """/jobs [page] — the whole batch at a glance, and a tap to pick which one to fill."""
    touch_activity()
    parts = text.strip().split()
    try:
        page = max(1, int(parts[1])) - 1 if len(parts) > 1 else 0
    except ValueError:
        page = 0
    await send_jobs_page(page)


async def send_jobs_page(page: int) -> None:
    rows = await asyncio.to_thread(_jobs_listing)
    if not rows:
        await asyncio.to_thread(tg_send,
            "📋 No jobs in the batch. /run to collect today's queue.")
        return
    text, buttons = jobs_page(rows, page)
    await asyncio.to_thread(tg_send, text, buttons)


async def send_status() -> None:
    touch_activity()
    c = await asyncio.to_thread(pending_mod.counts)
    if not c["jobs"]:
        await asyncio.to_thread(tg_send, "📋 Nothing pending. Run the queue to fill this up.")
        return
    held_line = ""
    if c.get("held"):
        hj = await asyncio.to_thread(pending_mod.all_jobs, pending_mod.HELD)
        rows = "\n".join(
            f"   · <b>{html.escape(str(r.get('company') or '?'))}</b>"
            + (f" — {html.escape(str(r.get('hold_reason')))}" if r.get("hold_reason") else "")
            for r in hj)
        held_line = (f"• {c['held']} filled but not submitted — need you "
                     f"(/retry to re-queue):\n{rows}\n")
    await asyncio.to_thread(tg_send,
        f"📋 <b>Batch</b>\n"
        f"• {c['awaiting']} waiting on you ({c['questions_left']} question(s) left)\n"
        f"• {c['ready']} answered, needing a ✅\n"
        f"• {c['approved']} approved, queued to submit\n"
        f"{held_line}\n"
        f"<i>/next to carry on · /jobs to pick a particular one</i>")


async def handle_retry() -> None:
    """Re-queue every job phase 3 gave up on (HELD -> APPROVED, attempt counter reset),
    then release them straight away. Meant to be used AFTER fixing whatever blocked the
    submit — if the cause is still there they'll just re-hold after MAX_FLUSH_ATTEMPTS."""
    global _LAST_FLUSH
    touch_activity()
    requeued = await asyncio.to_thread(pending_mod.unhold_all)
    if not requeued:
        await asyncio.to_thread(tg_send, "Nothing held to retry. /status to see the batch.")
        return
    lines = "\n".join(f"• <b>{html.escape(str(r.get('company') or '?'))}</b> — "
                      f"{html.escape(str(r.get('title') or ''))}" for r in requeued)
    await asyncio.to_thread(tg_send, f"🔁 Re-queued {len(requeued)} for another try:\n{lines}")
    await push_flush(len(requeued))
    _LAST_FLUSH = time.monotonic()  # this manual flush counts; don't let idle double-fire


async def handle_apply(text: str) -> None:
    """/apply <link> — fill a job link Parth pastes, on demand (not from the daily queue).

    Pushes the URL to the extension (same fire-and-forget channel as /stop and idle-flush);
    it opens the link, fills what it can, and rides the result back as a normal `collect`, so
    the role lands in the batch and he answers it with /next. Nothing is submitted here — the
    usual ✅ gate still stands. The link must be an application FORM (Greenhouse/Lever/Ashby),
    not a job description; a page we can't read comes back as 'read nothing' from the extension.
    """
    touch_activity()
    parts = text.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    m = re.search(r"https?://\S+", arg)  # tolerate <url>, trailing words, etc.
    url = m.group(0).rstrip(">).,") if m else ""
    if not url:
        await asyncio.to_thread(tg_send,
            "Send a job application link to fill, like:\n"
            "<code>/apply https://boards.greenhouse.io/acme/jobs/123</code>")
        return
    if not CLIENTS:
        await asyncio.to_thread(tg_send,
            "🔌 Chrome isn't connected right now. Open Chrome with the extension loaded, "
            "then send the link again.")
        return
    for ws in list(CLIENTS):
        try:
            await ws.send(json.dumps({"type": "apply_url", "url": url}))
        except Exception:
            pass
    await asyncio.to_thread(tg_send,
        "🔗 On it — opening that link and filling what I can. "
        "I'll tell you what needs you, then send <b>/next</b>.")


# Single source of truth for the command list: drives both /help and the Telegram command
# menu (setMyCommands, the "/" button). Keep the two in sync by construction.

# --- referrals: paste a person, get the reqs and the drafts -------------------------
#
# P3 of docs/referral_flow_plan.md. core/referral.py resolves the paste and drafter.py
# writes the messages; everything here is the view over them. Nothing is ever sent to the
# contact from the bridge — the messages are shown so Parth can copy them, which is the same
# rule as submit and the reason this feature cannot get his account restricted.
#
# WHY A PASTE NEEDS ITS OWN MODE
# ------------------------------
# Free text in this poller answers the current batch question. A profile pasted while a form
# is half-answered would be stored verbatim as somebody's answer to "Why us?", on a real
# application. So a paste only ever arrives one of two ways: inside `/refer <text>`, or as
# the ONE message after a bare `/refer`, which arms REFER_WAITING and is checked before the
# answer path. Anything else is an answer, exactly as before.
REFER_WAITING = False
# The outreach namespace inside the batch callback space. `a`/`o` are answers, `j` is a job,
# `r` is a resume; `x` is a contact.
CB_REF = "x"


def _refer_state_label(state: str) -> str:
    return {"found": "🆕 not contacted", "invited": "📨 invited", "accepted": "🤝 accepted",
            "messaged": "💬 asked", "replied": "✅ replied", "no_reply": "🔇 no reply",
            "bumped": "👋 bumped", "skipped": "⏭️ skipped"}.get(str(state or ""), str(state or "?"))


def referral_card(rec: dict) -> tuple[str, list]:
    """(text, buttons) for one contact. Pure, so the tests can read it without a network."""
    ref = referral_mod.short_id(rec.get("id") or "")
    tier = rec.get("tier")
    lines = [f"👤 <b>{html.escape(str(rec.get('name') or '?'))}</b> · "
             f"{html.escape(str(rec.get('tie') or 'cold'))}"]
    if rec.get("headline"):
        lines.append(html.escape(_one_line(rec["headline"], 120)))
    if rec.get("profile_url"):
        lines.append(f'🔗 <a href="{html.escape(str(rec["profile_url"]))}">their profile</a>')
    lines.append(f"🏢 <b>{html.escape(str(rec.get('company') or '?'))}</b> · "
                 f"tier {tier} ({html.escape(referral_mod.TIER_NAMES.get(tier, '?'))}) · "
                 f"{_refer_state_label(rec.get('state'))}")

    reqs = list(rec.get("reqs") or [])
    show = int(rec.get("show") or referral_mod.REQ_SHOW)
    for r in reqs[:max(1, show)] if reqs else []:
        bits = [f"• <b>{html.escape(str(r.get('title') or '?'))}</b>"]
        if r.get("location"):
            bits.append(html.escape(str(r["location"])))
        if r.get("ats_job_id"):
            bits.append(f"req {html.escape(str(r['ats_job_id']))}")
        lines.append(" · ".join(bits))
        if r.get("url"):
            lines.append(f'  <a href="{html.escape(str(r["url"]))}">open</a>')
        # The parallel-tracks rule: this is when to stop waiting and apply yourself, and it
        # is nearly always today. Only a Workday req with a reply pending ever says later.
        if r.get("apply_after"):
            when = str(r["apply_after"])
            lines.append(f"  <i>apply yourself: {html.escape(when)}</i>")
    for a in (rec.get("applied") or [])[:1]:
        lines.append(f"📌 <i>you already applied "
                     f"{html.escape(str(a.get('applied') or ''))}: "
                     f"{html.escape(_one_line(a.get('title'), 60))}</i>")

    buttons = []
    drafts = {k: v for k, v in (rec.get("drafts") or {}).items() if v}
    if drafts:
        row = [{"text": "✉️ Note", "callback_data": f"{CB}:{CB_REF}:{ref}:m:invite_note"}]
        # already_applied REPLACES the ask when there is an application to attach to, so
        # only one of the two is ever offered. Showing both would put two different asks in
        # front of him for the same person and invite sending the wrong one.
        ask = "already_applied" if drafts.get("already_applied") else "ask_dm"
        if drafts.get(ask):
            row.append({"text": "🙋 Ask", "callback_data": f"{CB}:{CB_REF}:{ref}:m:{ask}"})
        buttons.append(row)
        row2 = []
        if drafts.get("yes_pack"):
            row2.append({"text": "📦 Pack", "callback_data": f"{CB}:{CB_REF}:{ref}:m:yes_pack"})
        if drafts.get("bump"):
            row2.append({"text": "👋 Bump", "callback_data": f"{CB}:{CB_REF}:{ref}:m:bump"})
        if row2:
            buttons.append(row2)
    else:
        buttons.append([{"text": "✍️ Draft the messages",
                         "callback_data": f"{CB}:{CB_REF}:{ref}:draft"}])
    if len(reqs) > max(1, show):
        buttons.append([{"text": f"➡️ Other reqs ({len(reqs) - show})",
                         "callback_data": f"{CB}:{CB_REF}:{ref}:more"}])
    if tier == referral_mod.TIER_TRACKED:
        buttons.append([{"text": "🔎 Check this company now",
                         "callback_data": f"{CB}:{CB_REF}:{ref}:live"}])
    buttons.append([{"text": "📨 Invited", "callback_data": f"{CB}:{CB_REF}:{ref}:s:invited"},
                    {"text": "🤝 Accepted", "callback_data": f"{CB}:{CB_REF}:{ref}:s:accepted"}])
    buttons.append([{"text": "✅ Replied", "callback_data": f"{CB}:{CB_REF}:{ref}:s:replied"},
                    {"text": "⏭️ Drop", "callback_data": f"{CB}:{CB_REF}:{ref}:s:skipped"}])
    return "\n".join(lines), buttons


async def send_referral_card(rec: dict) -> None:
    text, buttons = referral_card(rec)
    await asyncio.to_thread(tg_send, text, buttons)


async def handle_refer(text: str) -> None:
    """/refer — resolve a pasted profile into reqs, or arm the paste mode when sent bare."""
    global REFER_WAITING
    touch_activity()
    body = re.sub(r"(?is)^/refer\b[ \t]*", "", str(text or "")).strip()
    if not body:
        REFER_WAITING = True
        await asyncio.to_thread(tg_send,
            "👤 Send me the profile: paste the LinkedIn page text, or just the link.\n"
            "<i>Add a line about how you know them and I'll use it in the drafts. "
            "/cancel to forget it.</i>")
        return
    REFER_WAITING = False
    await resolve_referral(body)


async def resolve_referral(body: str, live: bool = False) -> None:
    """Resolve one paste and show the card. Everything after the profile is Parth's notes.

    The notes are the highest-value line the drafter gets — he knows things about this person
    that no profile states — so a paste that carries any is worth more than one that does
    not, and the split is done here rather than asking him for two messages.
    """
    text, notes = _split_paste(body)
    rec = await asyncio.to_thread(referral_mod.resolve_paste, text, notes, "", live)
    if rec is None:
        await asyncio.to_thread(tg_send,
            "🤔 I couldn't find a person in that. Paste the profile text or the "
            "linkedin.com/in/... link.")
        return
    if not rec.get("id"):
        # No profile URL means no dedupe key, and the store is what stops the same recruiter
        # being invited about three different roles. The card is still worth showing.
        await asyncio.to_thread(tg_send,
            "⚠️ <i>No profile link in that paste, so I can't remember this person or check "
            "whether you've already contacted them. Send the link and I'll file it.</i>")
    await send_referral_card(rec)
    if rec.get("tier") == referral_mod.TIER_UNKNOWN and rec.get("company"):
        await asyncio.to_thread(tg_send,
            f"🆕 <b>{html.escape(str(rec['company']))}</b> isn't in companies.yaml. Add it "
            f"and tonight's run will find its roles.")


_NOTE_SPLIT = re.compile(r"(?im)^\s*(?:notes?|how i know them|context)\s*:\s*")


def _split_paste(body: str) -> tuple[str, str]:
    """(profile text, Parth's notes). An explicit `notes:` line wins; otherwise a short
    trailing line after a blank line is treated as his comment.

    Deliberately conservative: mis-reading half a profile as a note loses the employer and
    drops the paste to tier 4, while mis-reading a note as profile text costs nothing but a
    slightly noisier parse.
    """
    m = _NOTE_SPLIT.search(body or "")
    if m:
        return body[:m.start()].strip(), body[m.end():].strip()
    parts = re.split(r"\n\s*\n", str(body or "").strip())
    if len(parts) > 1 and len(parts[-1]) < 200 and "\n" not in parts[-1].strip():
        return "\n\n".join(parts[:-1]).strip(), parts[-1].strip()
    return str(body or "").strip(), ""


async def referral_callback(ref: str, rest: list, cb_id: str) -> None:
    """Every contact button. `rest` is whatever followed the token in the callback data."""
    touch_activity()
    rec = await asyncio.to_thread(referral_mod.find_by_short, ref)
    if rec is None:
        await asyncio.to_thread(tg_answer_callback, cb_id, "That contact's gone")
        return
    action = rest[0] if rest else ""

    if action == "draft":
        if not drafter_mod.available():
            await asyncio.to_thread(tg_answer_callback, cb_id, "Drafting is off")
            await asyncio.to_thread(tg_send, "✍️ Drafting is off or unavailable. /draft on")
            return
        await asyncio.to_thread(tg_answer_callback, cb_id, "Drafting…")
        drafts = await asyncio.to_thread(drafter_mod.draft_outreach, rec)
        if not drafts:
            await asyncio.to_thread(tg_send, "✍️ Couldn't draft that one. Try again, or "
                                             "write it yourself.")
            return
        fresh = await asyncio.to_thread(referral_mod.find_by_short, ref)
        await send_referral_card(fresh or {**rec, "drafts": drafts})
        return

    if action == "m" and len(rest) > 1:
        # One message per send, as plain text with NO html parsing and no link preview:
        # this is meant to be long-pressed and copied, and an escaped entity or a rendered
        # tag would be copied along with it into a message to a real person.
        variant = rest[1]
        body = ((rec.get("drafts") or {}).get(variant) or "").strip()
        if not body:
            await asyncio.to_thread(tg_answer_callback, cb_id, "Not drafted")
            return
        await asyncio.to_thread(tg_answer_callback, cb_id, "Copy it below")
        label = {"invite_note": "connection note", "ask_dm": "the ask",
                 "already_applied": "the ask (you already applied)",
                 "yes_pack": "the pack", "bump": "the bump"}.get(variant, variant)
        extra = ""
        if variant == "invite_note":
            extra = f"\n\n({len(body)}/{drafter_mod.INVITE_CAP} characters)"
        if variant == "yes_pack":
            res = next((r.get("resume") for r in (rec.get("reqs") or []) if r.get("resume")), None)
            if res and res.get("path"):
                # Naming the file is the agreed v1: the bridge has no sendDocument, and
                # attaching the wrong track's resume is worse than fetching it by hand.
                extra = f"\n\nattach: {res['path']}"
        await asyncio.to_thread(tg_send_plain, f"— {label} —\n\n{body}{extra}")
        return

    if action == "more":
        rec["show"] = len(rec.get("reqs") or []) or 1
        await asyncio.to_thread(tg_answer_callback, cb_id, "All reqs")
        await send_referral_card(rec)
        return

    if action == "live":
        await asyncio.to_thread(tg_answer_callback, cb_id, "Checking…")
        await asyncio.to_thread(tg_send, "🔎 Pulling that one company's board, one moment.")
        text = rec.get("profile_url") or rec.get("name") or ""
        # Re-resolve from the stored profile URL rather than the original paste: the paste
        # is not kept, and the URL is enough to rebuild the same person.
        await resolve_referral(f"{rec.get('name', '')}\n{rec.get('headline', '')}\n{text}",
                               live=True)
        return

    if action == "s" and len(rest) > 1:
        state = rest[1]
        row = await asyncio.to_thread(referral_mod.set_state, rec.get("id") or "", state)
        await asyncio.to_thread(tg_answer_callback, cb_id, _refer_state_label(state))
        if row:
            await send_referral_card(row)
        return

    await asyncio.to_thread(tg_answer_callback, cb_id, "")


async def handle_outreach_list() -> None:
    """/refers — everyone in flight, grouped by state, so nothing is quietly dropped."""
    touch_activity()
    store = await asyncio.to_thread(referral_mod.load_store)
    rows = [r for r in store.values() if isinstance(r, dict)]
    live = [r for r in rows if str(r.get("state")) not in referral_mod.NUDGE_DONE_STATES]
    if not rows:
        await asyncio.to_thread(tg_send,
            "🙂 No contacts yet. Send /refer and paste somebody's profile.")
        return
    lines = [f"👥 <b>Outreach</b> — {len(live)} in flight, {len(rows)} total"]
    for r in sorted(live, key=lambda x: str(x.get("company") or ""))[:25]:
        lines.append(f"• {html.escape(str(r.get('name') or '?'))} · "
                     f"{html.escape(str(r.get('company') or '?'))} · "
                     f"{_refer_state_label(r.get('state'))}")
    if len(live) > 25:
        lines.append(f"<i>…and {len(live) - 25} more</i>")
    await asyncio.to_thread(tg_send, "\n".join(lines))


# How often the follow-up sweep looks. Deliberately slow: every due nudge is a question for
# a human, `due_nudges` measures in DAYS, and each one fires at most once per contact.
OUTREACH_SWEEP_SECS = int(os.environ.get("OUTREACH_SWEEP_SECS", "3600") or 3600)


async def outreach_watcher() -> None:
    """Ask about invites that have gone quiet, and offer the bump when one is due.

    `accepted` is the only transition the pipeline cannot observe — it cannot see his
    LinkedIn — so without this the state machine stalls at `invited` forever and the
    follow-up that makes outreach work never happens. It asks rather than assuming, and
    `mark_nudged` means it asks once.
    """
    while True:
        await asyncio.sleep(OUTREACH_SWEEP_SECS)
        try:
            due = await asyncio.to_thread(referral_mod.due_nudges)
            for key, kind in due[:5]:      # a phone buzzing ten times is a muted phone
                store = await asyncio.to_thread(referral_mod.load_store)
                rec = store.get(key)
                if not rec:
                    continue
                ref = referral_mod.short_id(key)
                who = html.escape(str(rec.get("name") or "?"))
                company = html.escape(str(rec.get("company") or "?"))
                if kind == "accepted":
                    await asyncio.to_thread(tg_send,
                        f"🤝 Did <b>{who}</b> ({company}) accept your invite?",
                        [[{"text": "Yes", "callback_data": f"{CB}:{CB_REF}:{ref}:s:accepted"},
                          {"text": "Not yet", "callback_data": f"{CB}:{CB_REF}:{ref}:s:invited"}],
                         [{"text": "Give up on this one",
                           "callback_data": f"{CB}:{CB_REF}:{ref}:s:no_reply"}]])
                else:
                    bump = ((rec.get("drafts") or {}).get("bump") or "").strip()
                    await asyncio.to_thread(tg_send,
                        f"👋 <b>{who}</b> ({company}) has been quiet for "
                        f"{referral_mod.NUDGE_BUMP_DAYS} days. One bump, then drop it.",
                        [[{"text": "👋 Bump text",
                           "callback_data": f"{CB}:{CB_REF}:{ref}:m:bump"}] if bump else
                         [{"text": "✍️ Draft it", "callback_data": f"{CB}:{CB_REF}:{ref}:draft"}],
                         [{"text": "Sent it", "callback_data": f"{CB}:{CB_REF}:{ref}:s:bumped"},
                          {"text": "Drop it", "callback_data": f"{CB}:{CB_REF}:{ref}:s:no_reply"}]])
                await asyncio.to_thread(referral_mod.mark_nudged, key, kind)
        except Exception as e:
            print(f"[outreach] sweep error: {redact(e)}", file=sys.stderr)


COMMANDS = [
    ("run", "Start a collection pass over today's queue"),
    ("apply", "Fill a job link and collect its questions"),
    ("next", "Next question, or the summary to approve"),
    ("status", "What's pending"),
    ("jobs", "List every job and pick which one to fill"),
    ("edit", "Change an answer before you approve"),
    ("resume", "Swap the resume for the current job"),
    ("retry", "Re-queue jobs that couldn't submit"),
    ("refer", "Paste someone's profile, get the reqs and the messages"),
    ("refers", "Everyone you're in outreach with"),
    ("draft", "Turn Claude answer-drafting on or off"),
    ("stop", "Halt the queue"),
    ("help", "Show all commands"),
]


async def handle_help() -> None:
    touch_activity()
    lines = ["🤖 <b>Commands</b>"]
    for name, desc in COMMANDS:
        arg = " &lt;link&gt;" if name == "apply" else ""
        lines.append(f"/{name}{arg} — {desc}")
    lines.append("\n<i>Tip: tap the ☰ menu (or type /) to pick a command.</i>")
    await asyncio.to_thread(tg_send, "\n".join(lines))


def _draft_status_text() -> str:
    """The status line shown for /draft — the flag state plus a warning if the flag is on
    but no `claude` binary is around to honour it."""
    on = drafter_mod.is_enabled()
    model = drafter_mod.DRAFT_MODEL or "CLI default"
    if on:
        body = (f"🖊️ Claude drafting is <b>ON</b> (model: <code>{html.escape(model)}</code>).\n"
                "I'll draft essay answers the bank hasn't seen; you still 👍 or edit before it's used.")
        if not drafter_mod.CLAUDE_BIN:
            body += "\n\n⚠️ But no <code>claude</code> binary was found, so nothing will actually draft."
    else:
        body = ("🚫 Claude drafting is <b>OFF</b>.\n"
                "Only your saved answer-bank replies are offered — no Claude credits are spent.")
    return body


async def handle_draft_toggle(on: bool) -> None:
    """Set the flag and echo the new state. Shared by the /draft on|off text form and the
    inline buttons."""
    touch_activity()
    drafter_mod.set_enabled(on)
    await asyncio.to_thread(tg_send, _draft_status_text())


async def handle_draft(text: str) -> None:
    """/draft            show the flag state with on/off buttons
       /draft on|off     set it directly

    Tier-2 drafting is opt-in: OFF by default so no Claude credit is spent silently.
    The answer bank always works regardless of this flag."""
    touch_activity()
    parts = text.strip().split()
    arg = parts[1].lower() if len(parts) > 1 else ""
    if arg in ("on", "enable", "1", "yes"):
        await handle_draft_toggle(True)
        return
    if arg in ("off", "disable", "0", "no"):
        await handle_draft_toggle(False)
        return
    # Bare /draft (or an unrecognised arg): show status + the opposite-state button.
    on = drafter_mod.is_enabled()
    button = ({"text": "🚫 Turn OFF", "callback_data": "draft:off"} if on
              else {"text": "🖊️ Turn ON", "callback_data": "draft:on"})
    await asyncio.to_thread(tg_send, _draft_status_text(), [[button]])


def _resume_display(track: dict) -> str:
    """The string shown on the summary's 📄 line — mirrors fill.js's report format
    (`<file> (<tag> track)`) so an overridden resume reads the same as an auto-picked one."""
    return f"{track.get('current_file') or track.get('tag')} ({track.get('tag')} track)"


def _track_button_label(track: dict) -> str:
    return f"{str(track.get('tag') or '?').title()} — {track.get('current_file') or ''}".strip(" —")


async def handle_edit(text: str) -> None:
    """Change an answer on the job currently in front of Parth, BEFORE he taps ✅.
    Only ever touches the awaiting job next_job() returns, so an already-approved or
    already-submitted job (its file is gone) can't be reached here — editing is
    structurally before-submit only.

      /edit            list the editable questions with their current answers
      /edit 3 <text>   set question 3 to <text> in one shot
      /edit 3          re-open question 3 (clear it) so the normal reply flow re-asks it
    """
    touch_activity()
    rec = await asyncio.to_thread(pending_mod.next_job)
    if rec is None:
        await asyncio.to_thread(tg_send,
            "Nothing in front of you to edit right now. /status to see the batch.")
        return
    qs = rec.get("questions") or []
    parts = text.split(maxsplit=2)  # ["/edit", "N", "rest..."]

    if len(parts) == 1:  # bare /edit -> the numbered list
        if not qs:
            await asyncio.to_thread(tg_send,
                f"{job_title_line(rec)}\nNo free-text questions on this one — every field is "
                f"a profile default. Use /resume to change the resume.")
            return
        lines = [f"✏️ <b>Edit</b> — {job_title_line(rec)}", ""]
        for i, q in enumerate(qs, 1):
            ans = q.get("answer")
            shown = "—" if ans is None else (html.escape(_one_line(ans, 80)) or "<i>(blank)</i>")
            lines.append(f"{i}. <b>{html.escape(_one_line(q.get('label') or '?', 60))}</b>: {shown}")
        lines += ["", "<i>/edit 3 your new answer — or /edit 3 alone to retype it. "
                      "/resume for the resume.</i>"]
        await asyncio.to_thread(tg_send, "\n".join(lines))
        return

    try:
        n = int(parts[1])
        if not (1 <= n <= len(qs)):
            raise ValueError
    except ValueError:
        await asyncio.to_thread(tg_send,
            f"Pick a question number 1–{len(qs)}. Send /edit to see the list.")
        return
    q = qs[n - 1]

    if len(parts) == 3 and parts[2].strip():  # one-shot edit
        await asyncio.to_thread(pending_mod.set_answer, rec["dedup_key"], q["id"], parts[2].strip())
        # Echo WHICH job got edited: next_job() can shift between the /edit list and this
        # command (an approve/answer in between), so the company name is the receipt.
        await asyncio.to_thread(tg_send,
            f"✏️ Updated <b>{html.escape(_one_line(q.get('label') or '?', 50))}</b> on "
            f"<b>{html.escape(str(rec.get('company') or '?'))}</b>.")
        await serve_next()
    else:  # re-open: clear it, then serve it again so his next reply fills it
        await asyncio.to_thread(pending_mod.set_answer, rec["dedup_key"], q["id"], None)
        await serve_next()


async def handle_resume() -> None:
    """Override the resume track for the job in front of Parth. Four buttons (one per
    track); tapping sets pending's resume_override, which fill.js reads at attach time."""
    touch_activity()
    rec = await asyncio.to_thread(pending_mod.next_job)
    if rec is None:
        await asyncio.to_thread(tg_send,
            "Nothing in front of you right now. /status to see the batch.")
        return
    key = rec["dedup_key"]
    tracks = (profile_mod.load_profile() or {}).get("resume_tracks", [])
    cur = rec.get("resume") or "auto-picked at fill time"
    buttons = [[{"text": _track_button_label(t), "callback_data": f"{CB}:r:{key}:{t['tag']}"}]
               for t in tracks]
    await asyncio.to_thread(tg_send,
        f"📄 <b>Resume</b> — {job_title_line(rec)}\n"
        f"Currently: <b>{html.escape(str(cur))}</b>\n\n<i>Tap a track to override.</i>",
        buttons)


async def answer_current(text: str) -> bool:
    """Route a typed message to the current question. False if there isn't one, so the
    caller can let it fall through to the legacy ask flow."""
    rec = await asyncio.to_thread(pending_mod.next_job)
    if rec is None:
        return False
    q = pending_mod.next_question(rec)
    if q is None:
        return False
    await asyncio.to_thread(pending_mod.set_answer, rec["dedup_key"], q["id"], text)
    await serve_next()
    return True


async def batch_callback(data: str, cb_id: str) -> None:
    touch_activity()
    parts = data.split(":")
    kind = parts[1] if len(parts) > 1 else ""

    if kind == "later":
        await asyncio.to_thread(tg_answer_callback, cb_id, "Paused")
        await asyncio.to_thread(tg_send, "⏸ Paused — nothing lost. Send /next whenever.")
        return

    if kind == "p" and len(parts) == 3:            # /jobs paging -> b:p:<page>
        await asyncio.to_thread(tg_answer_callback, cb_id, "")
        await send_jobs_page(int(parts[2]) if parts[2].isdigit() else 0)
        return

    if kind == "f" and len(parts) == 3:            # /jobs pick -> b:f:<key>
        key = parts[2]
        rec = await asyncio.to_thread(pending_mod.load, key)
        if rec is None:
            await asyncio.to_thread(tg_answer_callback, cb_id, "That one's gone")
            return
        if rec.get("status") == pending_mod.APPROVED:
            await asyncio.to_thread(tg_answer_callback, cb_id, "Already approved — it's on its way")
            return
        global _LAST_CARD_KEY
        picked = await asyncio.to_thread(pending_mod.focus, key)
        await asyncio.to_thread(tg_answer_callback, cb_id,
                                str(picked.get("company") or "Picked")[:60])
        _LAST_CARD_KEY = None      # picking it deliberately means he wants the card again
        await serve_next()
        return

    if kind == CB_REF and len(parts) >= 3:      # b:x:<token>:<action>[:<arg>]
        await referral_callback(parts[2], parts[3:], cb_id)
        return

    if kind in ("a", "o") and len(parts) == 5:
        _, _, key, qid, action = parts
        rec = await asyncio.to_thread(pending_mod.load, key)
        q = next((x for x in (rec or {}).get("questions", []) if x.get("id") == qid), None)
        if q is None:
            # Stale button: the job was skipped/rejected since this message was sent.
            await asyncio.to_thread(tg_answer_callback, cb_id, "That one's gone")
            return
        if kind == "o":
            opts = q.get("options") or []
            i = int(action) if action.isdigit() else -1
            val = opts[i] if 0 <= i < len(opts) else ""
        elif action == "blank":
            val = ""
        else:  # "ok" -> accept the draft we offered
            draft, bank_id = bank_draft(q, rec.get("company"))
            val = draft or ""
            if bank_id:
                await asyncio.to_thread(bank_mod.touch, bank_id)
        await asyncio.to_thread(pending_mod.set_answer, key, qid, val)
        await asyncio.to_thread(tg_answer_callback, cb_id, "Saved")
        await serve_next()
        return

    if kind == "r" and len(parts) == 4:  # resume override -> b:r:<key>:<tag>
        _, _, key, tag = parts
        rec = await asyncio.to_thread(pending_mod.load, key)
        if rec is None:
            await asyncio.to_thread(tg_answer_callback, cb_id, "That one's gone")
            return
        if rec.get("status") != pending_mod.AWAITING:
            # He already ✅'d this one; it's queued/flushing. Changing the resume now would
            # race the unattended submit. Editing is before-approval only.
            await asyncio.to_thread(tg_answer_callback, cb_id, "Already approved — can't change")
            return
        tracks = (profile_mod.load_profile() or {}).get("resume_tracks", [])
        track = next((t for t in tracks if t.get("tag") == tag), None)
        if track is None:
            await asyncio.to_thread(tg_answer_callback, cb_id, "Unknown track")
            return
        await asyncio.to_thread(pending_mod.set_resume_override, key, tag, _resume_display(track))
        await asyncio.to_thread(tg_answer_callback, cb_id, f"Resume → {tag}")
        await asyncio.to_thread(tg_send,
            f"📄 <b>{html.escape(str(rec.get('company')))}</b> resume set to "
            f"<b>{html.escape(_resume_display(track))}</b>.")
        await serve_next()
        return

    if kind == "j" and len(parts) == 4:
        _, _, key, action = parts
        rec = await asyncio.to_thread(pending_mod.load, key)
        if rec is None:
            await asyncio.to_thread(tg_answer_callback, cb_id, "That one's gone")
            return

        if action == "approve":
            ok = await asyncio.to_thread(pending_mod.approve, key)
            if ok is None:  # guard: questions still outstanding
                await asyncio.to_thread(tg_answer_callback, cb_id, "Still has questions")
                await serve_next()
                return
            await asyncio.to_thread(tg_answer_callback, cb_id, "Approved")
            await asyncio.to_thread(
                tg_send, f"✅ <b>{html.escape(str(rec.get('company')))}</b> approved — "
                         f"it'll go out shortly.")

        elif action == "defer":
            # The ✋ on a QUESTION, not on the summary. Different verb on purpose: it keeps
            # the record and every answer he already typed (pending.skip), where the
            # summary's ✋ deletes it for tomorrow's queue to re-serve. Half-answered work
            # is exactly what you must not throw away to get out of a question.
            await asyncio.to_thread(pending_mod.skip, key)
            await asyncio.to_thread(tg_answer_callback, cb_id, "Skipped")
            await asyncio.to_thread(
                tg_send, f"✋ Skipped <b>{html.escape(str(rec.get('company')))}</b> — "
                         f"your answers are kept. /jobs to come back to it.")

        elif action == "skip":
            await asyncio.to_thread(pending_mod.delete, key)
            await asyncio.to_thread(tg_answer_callback, cb_id, "Skipped")
            await asyncio.to_thread(
                tg_send, f"✋ Skipped <b>{html.escape(str(rec.get('company')))}</b> — "
                         f"it'll come back in tomorrow's queue.")

        elif action == "reject":
            # Permanent: a record in applied.json is what keeps triage from ever
            # serving this role again. status distinguishes it from a real application.
            await asyncio.to_thread(triage_mod.append_applied, {
                "dedup_key": key, "company": rec.get("company", ""),
                "title": rec.get("title", ""), "location": rec.get("location", ""),
                "url": rec.get("url", ""), "status": "rejected",
            }, triage_mod.APPLIED_PATH)
            await asyncio.to_thread(pending_mod.delete, key)
            await asyncio.to_thread(tg_answer_callback, cb_id, "Rejected")
            await asyncio.to_thread(
                tg_send, f"🚫 Rejected <b>{html.escape(str(rec.get('company')))}</b> — "
                         f"you won't see this role again.")
        await serve_next()


# Keep strong refs to background draft tasks so the loop doesn't GC them mid-run.
_BG_TASKS: set = set()


async def _predraft_record(dedup_key: str, context: dict) -> None:
    """Background: draft this freshly-collected job's essay questions via Claude and persist
    them, so the P2 Telegram push already carries a 👍-able draft. Off the critical path —
    collect has already replied. ALL of the job's essays are drafted in one batched Claude
    call (drafter.draft_answers), so an application with several essays costs one call, not
    one per question. Drafts into a dict first, then re-loads and merges under a tiny write
    window so a save can't clobber an answer Parth typed in the meantime; only stamps
    questions still without a draft or answer."""
    if not drafter_mod.available():
        return
    try:
        rec = await asyncio.to_thread(pending_mod.load, dedup_key)
        if not rec:
            return
        # Only the still-open essays need a draft; a question already answered or drafted
        # (or one that isn't a free-text essay) is skipped before we spend the call.
        todo = [q for q in (rec.get("questions") or [])
                if not q.get("draft") and q.get("answer") is None and drafter_mod.is_draftable(q)]
        if not todo:
            return
        drafts = await asyncio.to_thread(drafter_mod.draft_answers, todo, context)
        if not drafts:
            return
        fresh = await asyncio.to_thread(pending_mod.load, dedup_key)  # re-read: minimize clobber
        if not fresh:
            return
        changed = False
        for q in fresh.get("questions") or []:
            t = drafts.get(q.get("id"))
            if t and not q.get("draft") and q.get("answer") is None:
                q["draft"] = t
                changed = True
        if changed:
            await asyncio.to_thread(pending_mod.save, fresh)
            dlog(f"[predraft] stamped {sum(1 for q in fresh['questions'] if q.get('draft'))} draft(s) on {dedup_key!r}")
    except Exception as e:
        dlog(f"[predraft] {dedup_key!r} failed ({e})")


async def handle_collect(msg, ws):
    """Phase 1: the orchestrator reports what one job needs, and moves on. Fire-and-forget
    by design — nobody waits for Parth here, which is the whole point of batching."""
    req_id = msg.get("reqId")
    try:
        job = msg.get("job") or {}
        # The queue path always carries the scraper's dedup_key; the ad-hoc /apply path does
        # not (the extension can't mint one). Resolve it here — URL-match today's queue so an
        # /apply of a role already queued reuses its key (no second application), else compute
        # one from the scraped company/title/location. A key already present is left untouched.
        if not str(job.get("dedup_key") or "").strip():
            job = {**job, "dedup_key": await asyncio.to_thread(resolve_dedup_key, job)}
        rec = await asyncio.to_thread(
            pending_mod.put, job, msg.get("questions") or [],
            msg.get("filled") or [], None, msg.get("warnings") or [])
        answers = {"ok": True, "added": rec is not None}
        # Kick off Claude drafting in the background so the push finds ready drafts. Context
        # is the job's own facts; the tab has already closed, so nobody is waiting on this.
        if rec and drafter_mod.available():
            ctx = {"company": job.get("company"), "role": job.get("title")}
            task = asyncio.create_task(_predraft_record(rec.get("dedup_key"), ctx))
            _BG_TASKS.add(task)
            task.add_done_callback(_BG_TASKS.discard)
    except Exception as e:
        print(f"[collect] failed: {e}", file=sys.stderr)
        answers = {"ok": False, "error": str(e)}
    await ws.send(json.dumps({"type": "answers", "reqId": req_id, "answers": answers}))


def _approved_not_yet_applied() -> list[dict]:
    """The approved pile, minus anything already in applied.json.

    This is the double-apply guard. Parth can panel-fill a role by hand in the afternoon
    that's also sitting approved in the batch from the morning — without this, phase 3
    would open it and apply a second time. Ramp's server rejects a duplicate outright
    ("you have reached your application limit"), so a double-apply does not just look
    sloppy, it burns a real slot. Filtering here rather than in the extension keeps the
    check next to the file that answers it.
    """
    done = triage_mod.applied_keys(triage_mod.APPLIED_PATH)
    out, held = [], []
    for rec in pending_mod.all_jobs(pending_mod.APPROVED):
        key = rec.get("dedup_key")
        if key in done:
            pending_mod.archive(key)  # already handled; retire the record (keep its answers)
            continue
        attempts = rec.get("flush_attempts", 0)
        if attempts >= MAX_FLUSH_ATTEMPTS:
            # Tried enough times without a confirmation — stop the loop, park it for Parth.
            pending_mod.hold(key)
            held.append(rec)
            continue
        rec["flush_attempts"] = attempts + 1
        pending_mod.save(rec)
        out.append(rec)
    return out, held


async def handle_approved(msg, ws):
    """Phase 3: hand the orchestrator the approved pile, with Parth's answers attached.

    Each job counts one flush attempt here; a job that's been served MAX_FLUSH_ATTEMPTS
    times without ever landing in applied.json (so it's still approved) is held instead of
    served again, and Parth is told once — that's what breaks the every-5-minutes retry."""
    req_id = msg.get("reqId")
    try:
        jobs, held = await asyncio.to_thread(_approved_not_yet_applied)
        answers = {"jobs": jobs}
    except Exception as e:
        print(f"[approved] failed: {e}", file=sys.stderr)
        jobs, held = [], []
        answers = {"jobs": [], "error": str(e)}
    await ws.send(json.dumps({"type": "answers", "reqId": req_id, "answers": answers}))
    if held:
        lines = "\n".join(f"• <b>{html.escape(str(r.get('company') or '?'))}</b> — "
                          f"{html.escape(str(r.get('title') or ''))}" for r in held)
        await asyncio.to_thread(
            tg_send,
            f"🖐️ Gave up auto-submitting after {MAX_FLUSH_ATTEMPTS} tries — these need you "
            f"(finish with the 💼 panel, or /retry to re-queue):\n{lines}")


def _queue_age_hours() -> float | None:
    """How stale out/queue.json is. Triage happily serves a week-old queue (closed
    postings, wasted fills) with no signal — surface the age so the batch-start
    message can warn when Parth forgot to git pull after the daily Actions run."""
    try:
        p = pathlib.Path(triage_mod.QUEUE_PATH)
        if not p.exists():
            return None
        return round((time.time() - p.stat().st_mtime) / 3600, 1)
    except OSError:
        return None


async def handle_queue(msg, ws):
    """Serve the orchestrator its work list: triage the queue off disk (pipeline-only,
    dedup vs applied.json, daily + per-company caps). Reply over the `answers` channel so
    background.js needs no new reply handling — the orchestrator reads answers.jobs."""
    req_id = msg.get("reqId")
    try:
        t0 = time.monotonic()
        # Jobs already in bridge/pending/ are in flight (being answered on Telegram) —
        # re-collecting them is a no-op that still burns a daily slot and a tab visit.
        pend_keys = {r.get("dedup_key")
                     for r in await asyncio.to_thread(pending_mod.all_jobs)}
        res = await asyncio.to_thread(
            triage_mod.triage, triage_mod.QUEUE_PATH, triage_mod.APPLIED_PATH,
            msg.get("max_per_day"), msg.get("max_per_company"), pend_keys)
        dlog(f"[queue] triage read {len(res['jobs'])} jobs in {time.monotonic() - t0:.2f}s")
        stats = dict(res["stats"])
        stats["queue_age_hours"] = _queue_age_hours()
        answers = {"jobs": res["jobs"], "stats": stats}
    except Exception as e:
        print(f"[queue] triage failed: {e}", file=sys.stderr)
        answers = {"jobs": [], "stats": {"error": str(e)}}
    await ws.send(json.dumps({"type": "answers", "reqId": req_id, "answers": answers}))


# --- URL identity for the panel->queue dedup match ----------------------------------
# The panel's live URL is NEVER byte-equal to the queue's URL for the same job:
#   ashby       queue .../<uuid>              live .../<uuid>/application
#   greenhouse  queue boards.greenhouse.io/... (or careers.<co>.com/...?gh_jid=N)
#               live  job-boards.greenhouse.io/<slug>/jobs/N  (after the 301)
#   lever       queue .../<posting-uuid>       live .../<posting-uuid>/apply
# So compare a TOKEN both worlds share: the Ashby/Lever posting UUID, or the Greenhouse
# numeric job id (gh_jid=N and /jobs/N are the same id). Only when no token exists fall
# back to a normalized host+path compare.

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_GH_ID_RE = re.compile(r"[?&]gh_jid=(\d+)|/jobs/(\d+)(?:[/?#]|$)")


def _url_token(url: str) -> str:
    u = str(url or "").strip().lower()
    if not u:
        return ""
    m = _UUID_RE.search(u)
    if m:
        return "uuid:" + m.group(0)
    m = _GH_ID_RE.search(u)
    if m:
        return "gh:" + (m.group(1) or m.group(2))
    # No recognizable job id: normalized host+path (strip scheme/query/fragment,
    # the /apply|/application suffix, trailing slash; fold the GH host rename).
    m = re.match(r"^[a-z]+://([^/?#]+)([^?#]*)", u)
    if not m:
        return "url:" + u
    host, path = m.group(1), m.group(2)
    host = host.replace("job-boards.greenhouse.io", "boards.greenhouse.io")
    path = re.sub(r"/(apply|application)/?$", "", path).rstrip("/")
    return "url:" + host + path


def resolve_dedup_key(record: dict) -> str:
    """The dedup_key for a record that may not carry one.

    The queue path always knows its key — it came from the scraper. The on-page panel does
    not: it only has what's scraped off the form, and the scraper derives the key from the
    ATS API's own company/title/location strings, which the page doesn't reproduce
    faithfully (the location especially). Recomputing from panel-scraped text would mint a
    DIFFERENT key for the same job, and the queue would happily serve it again tomorrow —
    a real second application, which some ATSs (Ramp) reject outright and which burns a
    slot Parth can't get back.

    So: match the queue by URL identity (job-id token, see _url_token — an exact string
    compare never matched because the live form URL always differs from the queue URL),
    and take that job's real key. Only fall back to computing one when the role isn't in
    today's queue at all (a form Parth found himself), where there's nothing to collide with.
    """
    key = (record.get("dedup_key") or "").strip()
    if key:
        return key
    url = (record.get("url") or "").strip()
    if url:
        try:
            token = _url_token(url)
            for job in triage_mod.load_queue(triage_mod.QUEUE_PATH):
                if token and _url_token(job.get("url")) == token:
                    return job.get("dedup_key") or ""
        except Exception as e:
            dlog(f"[applied] queue lookup for {url!r} failed ({e}) — computing a key instead")
    return scraper_mod.dedup_key(record.get("company", ""), record.get("title", ""),
                                record.get("location", ""))


async def handle_applied(msg, ws):
    """Record a CONFIRMED application to applied.json so it never re-surfaces. The
    extension only sends this after it has detected a submit-confirmation page — clicking
    Submit is not enough. Idempotent on dedup_key."""
    req_id = msg.get("reqId")
    record = msg.get("record") or {}
    try:
        record = {**record, "dedup_key": await asyncio.to_thread(resolve_dedup_key, record)}
        added = await asyncio.to_thread(triage_mod.append_applied, record, triage_mod.APPLIED_PATH)
        # The job is done: applied.json is the permanent dedup record. Retire the in-flight
        # pending file so it leaves the batch and /status stops counting it — but ARCHIVE
        # it (move to submitted/) rather than delete, so the answers Parth gave survive the
        # submit. No-op on the single-pass path, where there was never a pending file.
        if record.get("dedup_key"):
            await asyncio.to_thread(pending_mod.archive, record["dedup_key"])
        answers = {"ok": True, "added": added}
        if added:
            co = record.get("company", "?")
            title = record.get("title", "")
            # by_hand = Parth filled part of it with the panel but submitted it himself.
            # applied.json can't hold the distinction (append_applied fixes the row shape),
            # so the note is the only place it shows — worth saying, since a by-hand log is
            # the one case where nothing on our side ever saw a confirmation page.
            how = " (submitted by hand)" if record.get("by_hand") else ""
            await asyncio.to_thread(tg_send, f"📌 Logged as applied{how}: <b>{html.escape(str(co))}</b> — {html.escape(str(title))}")
    except Exception as e:
        print(f"[applied] log failed: {redact(e)}", file=sys.stderr)
        answers = {"ok": False, "error": str(e)}
    await ws.send(json.dumps({"type": "answers", "reqId": req_id, "answers": answers}))


def _pending_by_url(url: str) -> dict | None:
    """Find the pending record whose job matches this live form URL, by the same job-id
    token the applied-log dedup uses (the live URL is never byte-equal to the stored one —
    /application, /apply, the GH host rename). Returns the record or None."""
    token = _url_token(url)
    if not token:
        return None
    for rec in pending_mod.all_jobs():
        if _url_token(rec.get("url")) == token:
            return rec
    return None


async def handle_pending_answers(msg, ws):
    """Give the on-page panel everything already saved for THIS job, so re-filling a parked
    form by hand doesn't make him re-type — or re-buy — it. Match the job by URL, then return
    {label: answer} for every resolved question AND {label: draft} for the still-open ones
    that collect-time drafting already wrote. Without the drafts the panel sees a question
    with no draft on it and pays Claude to write a second one; worse, it asks per question
    while collect batched them, so a multi-essay job costs several calls for text we already
    have. Only UNSEEN questions offer a draft: `answer == ""` is a deliberate blank, and
    pushing the draft back at him would undo that decision. Best-effort — no match =>
    found:false."""
    req_id = msg.get("reqId")
    url = (msg.get("url") or "").strip()
    out = {"found": False, "answers": {}, "drafts": {}}
    try:
        rec = await asyncio.to_thread(_pending_by_url, url)
        if rec:
            questions = rec.get("questions", [])
            answers = {q.get("label"): q.get("answer")
                       for q in questions
                       if q.get("answer") not in (None, "")}
            drafts = {q.get("label"): q.get("draft")
                      for q in questions
                      if q.get("answer") is None and q.get("draft")}
            out = {"found": True, "answers": answers, "drafts": drafts,
                   "resume_override": rec.get("resume_override") or None}
    except Exception as e:
        print(f"[pending_answers] {url!r} failed: {redact(e)}", file=sys.stderr)
    await ws.send(json.dumps({"type": "answers", "reqId": req_id, "answers": out}))


async def handle_hold_job(msg, ws):
    """Phase 3 filled this job but could NOT submit it unattended (the form differed from
    what Parth approved, a required field needs him, an email-verification gate, or a click
    that never confirmed). Park it straight into the held pile so the idle-watcher stops
    re-driving an already-filled form — no auto-retry, no double-apply risk. Parth finishes
    it in the 💼 panel, or /retry re-queues it once the cause is fixed. Idempotent."""
    req_id = msg.get("reqId")
    key = (msg.get("dedup_key") or "").strip()
    reason = str(msg.get("reason") or "").strip() or None
    ok = False
    try:
        if key:
            ok = bool(await asyncio.to_thread(pending_mod.hold, key, None, reason))
    except Exception as e:
        print(f"[hold] {key!r} failed: {redact(e)}", file=sys.stderr)
    await ws.send(json.dumps({"type": "answers", "reqId": req_id, "answers": {"ok": ok}}))


async def handle_drafts(msg, ws):
    """What the answer bank has for these questions — for the on-page panel, no Telegram.

    The panel is the manual path: Parth is at the keyboard, so he answers in the page. He
    should still be offered his own saved wording there, and the bank lives on disk where
    only the bridge can read it. Deliberately best-effort: the panel treats an unreachable
    bridge as "no drafts" and asks cold, which is exactly the pre-bank behaviour.
    """
    req_id = msg.get("reqId")
    ctx = msg.get("context") or {}
    force = bool(msg.get("force"))
    out: dict = {}
    for item in msg.get("items") or []:
        try:
            draft, bank_id = await resolve_draft(item, ctx, force=force)
        except Exception as e:
            dlog(f"[drafts] {item.get('id')!r} failed ({e}) — no draft for it")
            continue
        if draft:
            out[item.get("id")] = {"draft": draft, "bank_id": bank_id}
    await ws.send(json.dumps({"type": "answers", "reqId": req_id, "answers": out}))


async def handle_bank_touch(msg, ws):
    """Mark a bank answer as used. Only ever sent when Parth ACCEPTS its draft.

    Same rule the Telegram path follows: an answer he keeps rewriting must stay untouched
    so it surfaces in `bank.py stale` — that's the signal it needs a rewrite. Touching on
    'offered' rather than 'accepted' would quietly destroy that signal.
    """
    req_id = msg.get("reqId")
    bank_id = msg.get("bank_id")
    ok = False
    try:
        if bank_id:
            ok = bool(await asyncio.to_thread(bank_mod.touch, bank_id))  # False on unknown id
    except Exception as e:
        print(f"[bank] touch of {bank_id!r} failed: {redact(e)}", file=sys.stderr)
    await ws.send(json.dumps({"type": "answers", "reqId": req_id, "answers": {"ok": ok}}))


async def handle_workday_route(msg, ws):
    """Which entry route the extension should take on a Workday posting.

    The question is "have we applied to this TENANT before", and only applied.json can
    answer it. Answering here instead of porting the rule into the extension keeps ONE
    implementation: a JS copy would drift the first time the tenant regex or the
    applied-row shape changed, and it would drift silently — a wrong route does not throw,
    it just fills a form the slow way or replays an application it should not have.

    Never fatal. A failed lookup answers `manual`, which is the route that needs nothing
    to be true, so a bridge problem costs speed rather than correctness.
    """
    req_id = msg.get("reqId")
    url = (msg.get("url") or "").strip()
    out = {"tenant": None, "route": "manual", "reason": "route lookup failed"}
    try:
        out = await asyncio.to_thread(apply_log_mod.workday_route, url)
    except Exception as e:
        print(f"[workday_route] {url!r} failed: {redact(e)}", file=sys.stderr)
    await ws.send(json.dumps({"type": "answers", "reqId": req_id, "answers": out}))


async def handle_verify_code(msg, ws):
    """Fetch the emailed verification code for a submit Parth already started.

    ONLY EVER IN RESPONSE TO A HUMAN ACTION. The extension sends this after Parth clicks
    Submit and the board answers with its code gate, or when he taps the panel's code
    button — never on the queue's own initiative. Parth set that rule explicitly: "never
    an otp fetch without human explicitly asking for it or has submit."

    `since` is the submit click, in epoch seconds, and it is the whole reason this is safe
    to automate: it binds the code to THIS submit, so a second role submitted ten minutes
    later cannot be handed the first one's code. See mailcode.py for the matching rules.

    Never fatal. No reader configured, nothing in the inbox yet, or a mail server that
    will not answer all reply the same way — no code — and the extension falls back to
    exactly what it did before this existed: tell Parth, leave the tab open, park the job.
    """
    req_id = msg.get("reqId")
    out = {"ok": False, "code": None, "why": "no code found"}
    try:
        since = msg.get("since")
        since_dt = (datetime.fromtimestamp(float(since), timezone.utc)
                    if since else datetime.now(timezone.utc))
        reader = await asyncio.to_thread(mailcode_mod.make_reader)
        hit = await asyncio.to_thread(
            mailcode_mod.find_code, reader, since_dt,
            str(msg.get("company") or ""), str(msg.get("role") or ""),
            str(msg.get("key") or ""))
        if hit:
            out = {"ok": True, "code": hit.code, "why": hit.why}
            # The code itself is a live credential for one submit — log that one was found
            # and how fresh it was, never the value.
            print(f"[verify] {msg.get('company') or '?'}: {hit.why}")
        else:
            print(f"[verify] {msg.get('company') or '?'}: no matching message since submit")
    except NotImplementedError as e:
        out["why"] = "no mail reader configured"
        print(f"[verify] {redact(e)}", file=sys.stderr)
    except Exception as e:
        out["why"] = "mail lookup failed"
        print(f"[verify] lookup failed: {redact(e)}", file=sys.stderr)
    await ws.send(json.dumps({"type": "answers", "reqId": req_id, "answers": out}))


async def handle_notify(msg, ws):
    """Post a one-off status line to Telegram (orchestrator progress). Reply over the
    answers channel so background.js's requestBridge resolves."""
    req_id = msg.get("reqId")
    text = str(msg.get("text") or "").strip()
    buttons = msg.get("buttons")
    try:
        if text:
            await asyncio.to_thread(tg_send, text, buttons)
        answers = {"ok": True}
    except Exception as e:
        print(f"[notify] send failed: {redact(e)}", file=sys.stderr)
        answers = {"ok": False, "error": str(e)}
    await ws.send(json.dumps({"type": "answers", "reqId": req_id, "answers": answers}))


def _append_log(report: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **report}
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


async def handle_log_report(msg, ws):
    """Persist a fill.js run report to disk so audits don't require live re-testing.
    Best-effort: a write failure is logged to stderr, never surfaced to the extension
    as an error (the fill itself already completed)."""
    req_id = msg.get("reqId")
    report = msg.get("report") or {}
    try:
        await asyncio.to_thread(_append_log, report)
        answers = {"ok": True}
    except Exception as e:
        print(f"[log_report] write failed: {e}", file=sys.stderr)
        answers = {"ok": False, "error": str(e)}
    await ws.send(json.dumps({"type": "answers", "reqId": req_id, "answers": answers}))


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(text or "")).strip("-").lower()
    return s[:60] or "unknown"


def _save_audit(company: str, role: str, url: str, summary: list, screenshot: str) -> str:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = f"{ts}__{_slug(company)}__{_slug(role)}"
    png_path = AUDIT_DIR / f"{base}.png"
    # screenshot arrives as a data: URL ("data:image/png;base64,...."); strip the header.
    _, _, b64 = str(screenshot or "").partition(",")
    if b64:
        png_path.write_bytes(base64.b64decode(b64))
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "company": company, "role": role,
            "url": url, "summary": summary, "screenshot": png_path.name if b64 else None}
    AUDIT_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_INDEX_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return str(png_path) if b64 else ""


async def handle_audit_dump(msg, ws):
    """Save the pre-submit screenshot + field-value summary to bridge/logs/audit/.
    Best-effort: a save failure is logged to stderr, never surfaced as an error to the
    extension (the submit itself is not gated on this)."""
    req_id = msg.get("reqId")
    try:
        path = await asyncio.to_thread(
            _save_audit, msg.get("company"), msg.get("role"), msg.get("url"),
            msg.get("summary") or [], msg.get("screenshot"))
        answers = {"ok": True, "path": path}
    except Exception as e:
        print(f"[audit_dump] save failed: {e}", file=sys.stderr)
        answers = {"ok": False, "error": str(e)}
    await ws.send(json.dumps({"type": "answers", "reqId": req_id, "answers": answers}))


# ── Dashboard: one view over every pile, and transitions between them ────────────────
# See dashboard_spec.md. The popup renders these rows and calls set_state to move a job.

def _apply_url(rec: dict) -> str:
    """Where to actually open the application. Greenhouse forms are often embedded on the
    company's own careers site (asana.com, careers.roblox.com, ...) where the form lives in a
    cross-origin iframe — the extension panel can't float there and its dropdown clicks
    misfire. The legacy embed endpoint serves the SAME form standalone (verified), so route
    Greenhouse roles there. Lever/Ashby are already standalone; anything without a job id
    keeps its stored URL."""
    if rec.get("ats") == "greenhouse" and rec.get("ats_job_id"):
        return f"https://boards.greenhouse.io/embed/job_app?token={rec['ats_job_id']}"
    return rec.get("url", "")


def _pending_row(rec: dict) -> dict:
    """Map a pending record to a dashboard row (bucket + human tag)."""
    st = rec.get("status")
    n = len(pending_mod.unanswered(rec))
    if st == pending_mod.APPROVED:
        bucket, tag = "filled", "ready to submit"
    elif st == pending_mod.HELD:
        bucket, tag = "filled", rec.get("hold_reason") or "needs you"
    elif st == pending_mod.SKIPPED:
        bucket, tag = "in_queue", "skipped"
    else:  # awaiting
        bucket = "in_queue"
        tag = f"{n} question{'' if n == 1 else 's'}" if n else "ready to approve"
    return {"dedup_key": rec.get("dedup_key"), "company": rec.get("company", ""),
            "title": rec.get("title", ""), "location": rec.get("location", ""),
            "url": rec.get("url", ""), "apply_url": _apply_url(rec), "bucket": bucket, "tag": tag,
            "date": (rec.get("created") or "")[:10], "unanswered": n}


def _dashboard_rows(since_days: int = 60) -> list[dict]:
    """Every pile merged into dated, bucketed rows, newest first. Pending is always shown
    in full; the applied/blocked pile (which only grows) is windowed to since_days."""
    rows = [_pending_row(r) for r in pending_mod.all_jobs()]
    cutoff = (date.today() - timedelta(days=since_days)).isoformat() if since_days else None
    for r in triage_mod.load_applied(triage_mod.APPLIED_PATH):
        when = (r.get("applied") or "")[:10]
        if cutoff and when and when < cutoff:
            continue
        blocked = (r.get("status") == "rejected")
        rows.append({"dedup_key": r.get("dedup_key"), "company": r.get("company", ""),
                     "title": r.get("title", ""), "location": r.get("location", ""),
                     "url": r.get("url", ""), "bucket": "blocked" if blocked else "submitted",
                     "tag": "blocked" if blocked else "applied", "date": when, "unanswered": 0})
    rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    return rows


async def handle_list_all(msg, ws):
    """Serve the dashboard its merged pile view. since_days windows the applied pile only."""
    req_id = msg.get("reqId")
    since = msg.get("since_days")
    try:
        since = int(since) if since is not None else 60
    except (TypeError, ValueError):
        since = 60
    try:
        rows = await asyncio.to_thread(_dashboard_rows, since)
        answers = {"ok": True, "rows": rows}
    except Exception as e:
        print(f"[list_all] failed: {redact(e)}", file=sys.stderr)
        answers = {"ok": False, "rows": [], "error": str(e)}
    await ws.send(json.dumps({"type": "answers", "reqId": req_id, "answers": answers}))


def _apply_transition(key: str, target: str, reason: str | None, meta: dict) -> dict:
    """Move one job to the target bucket. See dashboard_spec.md §2. append_applied and
    archive are both idempotent, so a race with an in-flight flush can't double-apply."""
    if not key:
        return {"ok": False, "error": "no dedup_key"}
    pend = pending_mod.load(key)
    in_applied = key in triage_mod.applied_keys(triage_mod.APPLIED_PATH)

    if target in ("submitted", "blocked"):
        # Prefer the pending record's fields; fall back to what the dashboard row carried,
        # so an applied-only row (no pending file) still writes a complete record.
        rec = {**(meta or {}), **(pend or {}),
               "dedup_key": key, "status": "applied" if target == "submitted" else "rejected"}
        triage_mod.append_applied(rec, triage_mod.APPLIED_PATH)
        if pend:
            pending_mod.archive(key)  # keep the answers, just retire it from the batch
        return {"ok": True}

    if target == "filled":
        r = pending_mod.approve(key)  # guarded: refuses if incomplete / no record
        if r:
            return {"ok": True}
        return {"ok": False, "error": "answer its questions first (or no filled record exists)"}

    if target == "in_queue":
        moved = False
        if in_applied:
            moved = triage_mod.remove_applied(key, triage_mod.APPLIED_PATH) or moved  # un-submit / un-block
        if pend:
            moved = bool(pending_mod.reopen(key)) or moved
        if not moved:
            return {"ok": False, "error": "nothing to move back for this role"}
        return {"ok": True}

    if target == "skipped":
        if pend and pending_mod.skip(key):
            return {"ok": True}
        return {"ok": False, "error": "only a queued or filled role can be skipped"}

    return {"ok": False, "error": f"unknown target: {target}"}


async def handle_set_state(msg, ws):
    """Move a job between buckets from the dashboard. Idempotent-safe (see _apply_transition)."""
    req_id = msg.get("reqId")
    key = (msg.get("dedup_key") or "").strip()
    target = (msg.get("target") or "").strip()
    reason = str(msg.get("reason") or "").strip() or None
    meta = msg.get("meta") or {}
    try:
        answers = await asyncio.to_thread(_apply_transition, key, target, reason, meta)
    except Exception as e:
        print(f"[set_state] {key!r}->{target!r} failed: {redact(e)}", file=sys.stderr)
        answers = {"ok": False, "error": str(e)}
    await ws.send(json.dumps({"type": "answers", "reqId": req_id, "answers": answers}))


async def ws_handler(ws):
    global ACTIVE_HANDLER
    print(f"[ws] extension connected from {ws.remote_address}")
    # A fresh connection SUPERSEDES the old session (e.g. Parth reloaded the extension while
    # a question was pending). Cancel any handler still blocked awaiting a Telegram reply on
    # the dead socket — otherwise it keeps holding LOCK and the reloaded extension's questions
    # deadlock and never appear. Cancelling unwinds its `async with LOCK`, freeing it. Then
    # drop the stale client + any queued Telegram messages so nothing crosses sessions.
    if ACTIVE_HANDLER and not ACTIVE_HANDLER.done():
        ACTIVE_HANDLER.cancel()
        try:
            await ACTIVE_HANDLER
        except BaseException:
            pass
    for old in list(CLIENTS):
        if old is not ws:
            try:
                await old.close()
            except Exception:
                pass
    CLIENTS.clear()
    CLIENTS.add(ws)
    while INBOX is not None and not INBOX.empty():
        INBOX.get_nowait()

    dispatch = {"ask": handle_ask, "review": handle_review, "queue": handle_queue,
                "applied": handle_applied, "notify": handle_notify,
                "log_report": handle_log_report, "audit_dump": handle_audit_dump,
                "collect": handle_collect, "approved": handle_approved,
                "drafts": handle_drafts, "bank_touch": handle_bank_touch,
                "hold_job": handle_hold_job, "pending_answers": handle_pending_answers,
                "list_all": handle_list_all, "set_state": handle_set_state,
                "workday_route": handle_workday_route,
                "verify_code": handle_verify_code}
    hb = asyncio.create_task(heartbeat(ws))
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                await ws.send(json.dumps({"type": "error", "error": "bad JSON"}))
                continue
            t = msg.get("type")
            if t == "ping":
                await ws.send(json.dumps({"type": "pong"}))
                continue
            fn = dispatch.get(t)
            if not fn:
                await ws.send(json.dumps({"type": "error", "error": f"unknown type: {t}"}))
                continue
            # Run as a tracked task so a later reconnect can cancel it mid-wait.
            locked = LOCK.locked()
            if locked:
                dlog(f"[ws] '{t}' arrived while LOCK is held — it will wait for the "
                     f"in-flight handler before doing anything")
            t0 = time.monotonic()
            dlog(f"[ws] handling '{t}' (reqId={msg.get('reqId')})")
            ACTIVE_HANDLER = asyncio.create_task(fn(msg, ws))
            await ACTIVE_HANDLER
            dlog(f"[ws] done '{t}' in {time.monotonic() - t0:.2f}s")
    except websockets.ConnectionClosed:
        pass
    except asyncio.CancelledError:
        pass  # superseded by a newer connection
    finally:
        hb.cancel()
        CLIENTS.discard(ws)
        print("[ws] extension disconnected")


# --- main ---------------------------------------------------------------------------

async def main():
    global INBOX, LOCK
    if not TOKEN or not CHAT_ID:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment "
                 "(see bridge/README.md).")

    # _family_name() is only meaningful AFTER a connection has been attempted — the probe
    # is lazy, so read it inside the handlers, not before the call.
    try:
        me = await asyncio.to_thread(tg_get_me)
        print(f"[telegram] bot @{me.get('username')} ready over {_family_name()}; "
              f"chat_id={CHAT_ID}")
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        # Never reached Telegram at all — a bad token answers 401 (an HTTPError, which
        # falls through to the token message below), it does not hang.
        # Blaming the token here sent a past debug session down the wrong path.
        sys.exit(f"Telegram is unreachable over {_family_name()} — a network/route problem, "
                 f"not the token. Try TELEGRAM_IP_FAMILY=6 (or 4), or a VPN if this "
                 f"network blocks Telegram: {redact(e)}")
    except Exception as e:
        sys.exit(f"Telegram getMe failed — check TELEGRAM_BOT_TOKEN: {redact(e)}")

    # Publish the command menu so Telegram's ☰ / '/' picker lists them. Best-effort: a
    # network blip here shouldn't stop the bridge from coming up.
    try:
        await asyncio.to_thread(tg_set_my_commands, COMMANDS)
    except Exception as e:
        print(f"[telegram] setMyCommands failed (non-fatal): {redact(e)}", file=sys.stderr)

    INBOX = asyncio.Queue()
    LOCK = asyncio.Lock()
    # Start the idle clock at boot, not at zero: if the bridge restarted while an
    # approved pile was still on disk, that pile must still go out.
    touch_activity()
    offset = await drain_startup_backlog()

    async with websockets.serve(ws_handler, WS_HOST, WS_PORT):
        print(f"[ws] bridge up on ws://{WS_HOST}:{WS_PORT}")
        c = await asyncio.to_thread(pending_mod.counts)
        hello = "🟢 Apply bridge is up. Ready when you are."
        if c["jobs"]:
            hello += (f"\n📋 {c['awaiting']} application(s) waiting on you "
                      f"({c['questions_left']} question(s)) — send <b>/next</b> to carry on.")
        hello += "\n<i>/run starts a collection pass · /status anytime · /stop halts the queue</i>"
        await asyncio.to_thread(tg_send, hello)
        idle = asyncio.create_task(idle_watcher())
        trigger = asyncio.create_task(trigger_watcher())
        flush_trigger = asyncio.create_task(flush_trigger_watcher())
        outreach = asyncio.create_task(outreach_watcher())
        try:
            await telegram_poller(offset)
        finally:
            idle.cancel()
            trigger.cancel()
            flush_trigger.cancel()
            outreach.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbridge stopped")
