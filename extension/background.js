// background.js — MV3 service worker. Sole owner of chrome.debugger.
//
// Why this exists: the new Greenhouse react-select dropdowns ignore synthesized
// (untrusted) DOM events — verified live. Only OS-trusted input works, and a content
// script can't produce it. The debugger protocol (CDP Input.dispatchMouseEvent) CAN
// issue trusted clicks, but chrome.debugger is only callable from the background, not a
// content script. So fill.js relays viewport coordinates here and we click for it.
//
// The debugger attaches only for the duration of a fill (fill.js sends CDP_DETACH in its
// finally block), so the "debugging this browser" banner is only shown while filling.

const attached = new Set(); // tabIds we currently hold a debugger session on
let stopRequested = false;  // set by the popup Stop button or a Telegram /stop; runQueue halts between jobs

const rawAttach = (tabId) => new Promise((resolve, reject) => {
  chrome.debugger.attach({ tabId }, "1.3", () => {
    if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
    resolve();
  });
});

// Detach without consulting `attached`. The recovery in attach() drops a session that the
// set does not list.
const rawDetach = (tabId) => new Promise((resolve, reject) => {
  chrome.debugger.detach({ tabId }, () => {
    if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
    resolve();
  });
});

// Take a debugger session on `tabId`, and recover one this worker has forgotten.
//
// MV3 evicts the service worker after about 30 seconds idle, and `attached` goes with it.
// Chrome keeps the debugger session, so the next attach fails with "Another debugger is
// already attached" and every trusted click on the form is refused. A fill that ended
// without its CDP_DETACH leaves the same state. Detaching first clears it. If DevTools
// holds the tab instead, the detach fails and the original error is re-thrown, so the
// report names the case you have to fix by hand.
async function attach(tabId) {
  if (attached.has(tabId)) return;
  try {
    await rawAttach(tabId);
  } catch (e) {
    if (!/already attached/i.test(e.message || "")) throw e;
    try { await rawDetach(tabId); } catch (_) { throw e; }
    await rawAttach(tabId);
  }
  attached.add(tabId);
}

function detach(tabId) {
  return new Promise((resolve) => {
    if (!attached.has(tabId)) return resolve();
    chrome.debugger.detach({ tabId }, () => { attached.delete(tabId); resolve(); });
  });
}

function send(tabId, method, params) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand({ tabId }, method, params, (result) => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      resolve(result);
    });
  });
}

// A single trusted left-click at viewport coordinates (CSS pixels, matching
// getBoundingClientRect). Move → press → release, as a real click.
async function trustedClick(tabId, x, y) {
  await send(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y, buttons: 0 });
  await send(tabId, "Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
  await send(tabId, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 1, clickCount: 1 });
}

// Trusted typing into the currently-focused element. Needed for type-to-search
// react-selects (e.g. a "Location (City)" places autocomplete) whose option list is loaded
// async only AFTER real input — synthesized input is ignored, same as the react-select
// clicks. insertText fires a genuine input event so React's onInputChange runs the search.
async function trustedType(tabId, text) {
  await send(tabId, "Input.insertText", { text: String(text) });
}

// Trusted Backspace, one keypress per character. Needed to RETRY a type-to-search dropdown
// with a different query: Input.insertText appends at the caret and cannot replace what's
// already in the box, and the box holds a controlled React value that a synthesized event
// won't clear. Bounded so a bad length can't hang the fill.
async function trustedErase(tabId, times) {
  for (let i = 0; i < Math.min(Number(times) || 0, 120); i++) {
    await send(tabId, "Input.dispatchKeyEvent", {
      type: "rawKeyDown", key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8, nativeVirtualKeyCode: 8 });
    await send(tabId, "Input.dispatchKeyEvent", {
      type: "keyUp", key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8, nativeVirtualKeyCode: 8 });
  }
}

// Trusted navigation keys. Workday's date control is several sections inside one widget,
// and CLICKING a section to move between them proved unreliable: a click aimed at the year
// kept focus on the month, so the year's digits were typed into a month spinner and
// clamped to 12 ("Invalid Date: 12/"). Moving with a keypress is what a person does and
// removes the coordinate guess entirely.
//
// Enter is here for one reason: Workday's Skills search is SERVER-SIDE and submits on
// Enter. Typing alone leaves the panel on "No Items." indefinitely — which I measured and
// read as an empty vocabulary, when in fact "python" + Enter returns 20 results. Enter is
// still a submit key, so it is only ever sent to a search box that is already open, never
// to a form.
const NAV_KEYS = { ArrowRight: 39, ArrowLeft: 37, Tab: 9, Enter: 13 };
async function trustedKey(tabId, key, times) {
  const vk = NAV_KEYS[key];
  if (!vk) return false;                       // allowlist: never dispatch arbitrary keys
  for (let i = 0; i < Math.min(Number(times) || 1, 20); i++) {
    await send(tabId, "Input.dispatchKeyEvent", {
      type: "rawKeyDown", key, code: key, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk });
    await send(tabId, "Input.dispatchKeyEvent", {
      type: "keyUp", key, code: key, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk });
  }
  return true;
}

// If the user closes DevTools / the session drops, forget the tab so a later fill re-attaches.
chrome.debugger.onDetach.addListener((source) => { if (source.tabId != null) attached.delete(source.tabId); });

// --- Control plane: WebSocket client to the local Telegram bridge -------------------
//
// Second privileged channel (alongside CDP): the bridge holds the Telegram bot + the
// Anthropic key and lives outside the extension, so secrets never touch a file here.
// fill.js (or the popup) sends ASK_USER; we relay to the bridge over ws and resolve with
// Parth's answers. The bridge heartbeats ({type:"hb"}) so this worker stays alive while
// he takes his time replying on Telegram.

const BRIDGE_URL = "ws://127.0.0.1:8765";
const ASK_TIMEOUT_MS = 30 * 60 * 1000; // a human on Telegram; be patient
let bridge = null;                     // the WebSocket
let bridgeReady = null;                // Promise<void> resolving on OPEN
const pendingAsks = new Map();         // reqId -> { resolve, reject, timer }
let reqSeq = 0;

function connectBridge() {
  if (bridge && (bridge.readyState === WebSocket.OPEN || bridge.readyState === WebSocket.CONNECTING)) {
    return bridgeReady;
  }
  bridgeReady = new Promise((resolve, reject) => {
    let settled = false;
    try { bridge = new WebSocket(BRIDGE_URL); }
    catch (e) { return reject(e); }
    bridge.onopen = () => { settled = true; resolve(); };
    bridge.onerror = () => {
      if (!settled) { settled = true; reject(new Error("can't reach the bridge at " + BRIDGE_URL + " — is bridge.py running?")); }
    };
    bridge.onclose = () => {
      bridge = null; bridgeReady = null;
      for (const p of pendingAsks.values()) { clearTimeout(p.timer); p.reject(new Error("bridge disconnected before you replied")); }
      pendingAsks.clear();
    };
    bridge.onmessage = (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.type === "hb" || m.type === "pong") return; // keepalive only
      if (m.type === "answers") {
        const p = pendingAsks.get(m.reqId);
        if (p) { pendingAsks.delete(m.reqId); clearTimeout(p.timer); p.resolve(m.answers || {}); }
      } else if (m.type === "stop") {
        // Unsolicited push from the bridge when Parth sends /stop on Telegram.
        stopRequested = true;
        console.log("[bridge] stop requested from Telegram");
      } else if (m.type === "flush") {
        // Unsolicited push: Parth went quiet for a few minutes, so send out whatever
        // he already approved. Fire-and-forget — the bridge isn't waiting on us.
        console.log("[bridge] idle flush requested");
        runFlush().catch(e => console.warn("[flush] failed:", (e && e.message) || e));
      } else if (m.type === "run_queue") {
        // Unsolicited push: the daily launcher (or a /run tap) is kicking off a P1
        // collection pass. Same entry point as the popup's ▶ button; runQueue guards
        // against a double-run itself. Fire-and-forget — the bridge isn't waiting on us.
        console.log("[bridge] queue run requested");
        runQueue(m.opts || {}).catch(e => console.warn("[queue] failed:", (e && e.message) || e));
      } else if (m.type === "apply_url") {
        // Unsolicited push: Parth sent /apply <link> on Telegram. Open that one link,
        // fill it deterministically, and collect its questions into the batch — exactly a
        // one-off runQueue iteration. Fire-and-forget; the collect ride-back does the rest.
        console.log("[bridge] apply-url requested from Telegram");
        runApplyUrl(m.url).catch(e => console.warn("[apply_url] failed:", (e && e.message) || e));
      } else if (m.type === "error") {
        console.warn("[bridge] error:", m.error);
      }
    };
  });
  return bridgeReady;
}

// Send a request to the bridge and resolve with its {type:"answers"} reply (correlated by
// reqId). Shared by both the per-field ask and the end-of-form review.
async function requestBridge(payload) {
  await connectBridge();
  const reqId = "r" + (++reqSeq) + "_" + Date.now();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pendingAsks.delete(reqId); reject(new Error("timed out waiting for your Telegram reply")); }, ASK_TIMEOUT_MS);
    pendingAsks.set(reqId, { resolve, reject, timer });
    bridge.send(JSON.stringify({ ...payload, reqId }));
  });
}

// ── Reconnect after MV3 evicts this worker ───────────────────────────────────────────
//
// The bridge's heartbeat only keeps an ALREADY-OPEN socket's worker alive — it cannot
// revive a worker Chrome has already evicted. So when the socket dies (bridge restart,
// machine sleep, Chrome suspending an idle worker) nothing reconnects until Parth
// manually opens the popup. On 2026-07-15 that silently stranded an approved application
// for 15+ minutes: he'd approved it, and it simply never went out.
//
// An alarm is the only thing that wakes an evicted worker on its own. Two details are
// load-bearing:
//   - the listener MUST be registered at the top level, not inside onInstalled. An
//     evicted worker re-runs this file from scratch on every wake, and a listener added
//     any later than that would miss the very tick that woke it.
//   - create the alarm only if it doesn't already exist. Creating it unconditionally
//     re-arms the countdown on every worker start, so anything else that wakes the worker
//     more often than the period would keep pushing the tick back and it would never fire.
const RECONNECT_ALARM = "bridge-reconnect";
const RECONNECT_MINUTES = 1; // Chrome's floor for a packed extension

chrome.alarms.get(RECONNECT_ALARM, (a) => {
  if (!a) chrome.alarms.create(RECONNECT_ALARM, { periodInMinutes: RECONNECT_MINUTES });
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== RECONNECT_ALARM) return;
  // A no-op while the socket is OPEN or CONNECTING, so this only ever dials when we've
  // actually lost it — it can't disturb a fill that's mid-ask on Telegram (that socket is
  // open by definition, and the bridge supersedes on new connections).
  // Failure is the normal case, not an error: the bridge is only running when Parth runs
  // it, so stay quiet rather than logging a rejection every minute.
  // Once connected, deliver any applied-records parked while the bridge was down —
  // that's the retry loop that makes a confirmed submit's log durable.
  connectBridge().then(() => flushAppliedQueue()).catch(() => {});
});

// Connect the INSTANT this worker starts — don't wait for the first alarm tick. An evicted
// MV3 worker re-runs this whole file on every wake (an incoming message, an alarm, opening
// the service-worker DevTools), so dialing at top level means we're connected the moment
// we're alive. Without this there was a up-to-60s window where the worker was awake but not
// yet reconnected, and an idle-flush push arriving in that window hit an empty client list —
// the bridge then wrongly reported "Chrome isn't running" while Chrome was plainly running,
// and /retry appeared to do nothing. Idempotent (connectBridge no-ops on an OPEN/CONNECTING
// socket) and quiet on failure (the bridge is only up when Parth runs it; the alarm retries).
connectBridge().then(() => flushAppliedQueue()).catch(() => {});

const askUser = (items, context) =>
  requestBridge({ type: "ask", items: items || [], context: context || null });

// End-of-form review: send the full summary + any still-empty required fields; the bridge
// returns { __review: "ok" | "hold" } from Parth's tap. The extension never submits either way.
const reviewUser = (summary, warnings, context, ai) =>
  requestBridge({ type: "review", summary: summary || [], warnings: warnings || [],
                  context: context || null, ai: ai || [] });

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || !msg.type) return; // not for us
  // Control-plane asks may originate from the popup (no sender.tab) or a content script,
  // so handle them before the tabId guard the CDP relays need.
  if (msg.type === "ASK_USER") {
    (async () => {
      try { sendResponse({ ok: true, answers: await askUser(msg.items, msg.context) }); }
      catch (e) { sendResponse({ ok: false, error: String((e && e.message) || e) }); }
    })();
    return true; // async sendResponse
  }
  // The on-page panel asking what the answer bank holds for its questions, and reporting
  // that Parth accepted one. Both are best-effort: the panel is designed to work with the
  // bridge down (it just asks cold), so a failure here resolves to "no drafts" rather than
  // surfacing an error he can't act on.
  if (msg.type === "DRAFTS_FOR") {
    (async () => {
      // force = Parth tapped "✨ Draft it" on a question the taxonomy never tagged. It's the
      // only way past the answer_key requirement, and it's always a deliberate tap because
      // each one spends subscription quota.
      try { sendResponse({ ok: true, drafts: await requestBridge({ type: "drafts", items: msg.items || [], context: msg.context || null, force: !!msg.force }) }); }
      catch (_) { sendResponse({ ok: false, drafts: {} }); }
    })();
    return true; // async sendResponse
  }
  // The optional AI fill + verify pass: fill.js hands over the whole serialized form and
  // gets back what to write, replace, flag or propose (bridge/aipass.py). One Claude call
  // per form, and only when the flag is on — the bridge answers {enabled:false} cheaply
  // when it is off, so the extension never has to know the state of a preference.
  // Best-effort like every other bridge call: a bridge that is down means no AI pass and
  // the deterministic fill stands, which is exactly today's behaviour.
  if (msg.type === "AI_PASS") {
    (async () => {
      try { sendResponse(await requestBridge({ type: "ai_pass", fields: msg.fields || [], context: msg.context || null })); }
      catch (_) { sendResponse({ enabled: false, decisions: [] }); }
    })();
    return true; // async sendResponse
  }
  // The on-page panel asking for the answers Parth already saved for THIS job (a parked
  // form he's re-filling by hand), so it can restore them instead of making him re-type.
  if (msg.type === "PENDING_ANSWERS") {
    (async () => {
      try { sendResponse(await requestBridge({ type: "pending_answers", url: msg.url || "" })); }
      catch (_) { sendResponse({ found: false, answers: {} }); }
    })();
    return true; // async sendResponse
  }
  if (msg.type === "WORKDAY_ROUTE") {
    (async () => {
      // "Have we applied to this tenant before?" — only applied.json knows, so the rule
      // stays in Python and this just relays. A bridge that is down answers null rather
      // than an invented route: the driver then uses the adapter's default and, either
      // way, can only click a route the modal is actually offering.
      try { sendResponse(await requestBridge({ type: "workday_route", url: msg.url || "" })); }
      catch (_) { sendResponse(null); }
    })();
    return true; // async sendResponse
  }
  if (msg.type === "VERIFY_CODE") {
    (async () => {
      // A board is holding a submit behind a code mailed to Parth, and he has ALREADY
      // acted — clicked Submit, or tapped the panel's code button. Relaying is all this
      // does; the bridge owns the mailbox and the matching rules (bridge/mailcode.py).
      //
      // `since` is the moment of that click. It travels with the request because it is
      // what makes the fetch safe: it binds the code to THIS submit, so a role submitted
      // ten minutes later cannot be handed an earlier one's code. Never synthesize it
      // here — a "now" invented at relay time would quietly widen the window.
      try { sendResponse(await requestBridge({ type: "verify_code", since: msg.since,
                                               company: msg.company || "", role: msg.role || "",
                                               key: msg.key || "" })); }
      catch (_) { sendResponse({ ok: false, code: null, why: "bridge unreachable" }); }
    })();
    return true; // async sendResponse
  }
  if (msg.type === "BANK_TOUCH") {
    (async () => {
      try { await requestBridge({ type: "bank_touch", bank_id: msg.bankId }); } catch (_) {}
      sendResponse({ ok: true });
    })();
    return true; // async sendResponse
  }
  // A panel-driven submit that reached a confirmation page. Same record the queue's phase 3
  // logs — without it, a form Parth fills by hand stays in the queue and gets applied to a
  // second time tomorrow. Durable: bridge down => the record parks and retries, and the
  // panel is told "queued" instead of "failed, go run apply_log.py".
  if (msg.type === "LOG_APPLIED") {
    (async () => {
      const r = await sendAppliedDurable(msg.record || {});
      sendResponse({ ok: r.sent || r.queued, queued: !r.sent && r.queued });
    })();
    return true; // async sendResponse
  }
  if (msg.type === "REVIEW_USER") {
    (async () => {
      try { sendResponse({ ok: true, answers: await reviewUser(msg.summary, msg.warnings, msg.context, msg.ai) }); }
      catch (e) { sendResponse({ ok: false, error: String((e && e.message) || e) }); }
    })();
    return true; // async sendResponse
  }
  // Persist the run's report to the bridge's audit log. Best-effort: nothing else
  // reads a fill report after the popup/orchestrator moves on, so this is the only
  // record of what got filled/skipped/flagged on a given run.
  if (msg.type === "LOG_REPORT") {
    (async () => {
      try { await requestBridge({ type: "log_report", report: msg.report || {} }); }
      catch (_) { /* bridge down: the report still reached Parth via the review message */ }
      sendResponse({ ok: true });
    })();
    return true; // async sendResponse
  }
  // The resume bytes for one track, straight from the bridge, which syncs its cache from
  // the source folder before answering. The bundled copy under extension/resumes/ is only
  // a fallback now: it is written at build time, so it is stale from the first edit after
  // a build and nothing in the browser can tell. Failure resolves to { ok: false } rather
  // than throwing — attach.js then uses the bundled copy, which is the old behaviour.
  if (msg.type === "RESUME_BYTES") {
    (async () => {
      try { sendResponse(await requestBridge({ type: "resume_bytes", tag: msg.tag })); }
      catch (e) { sendResponse({ ok: false, error: String((e && e.message) || e) }); }
    })();
    return true; // async sendResponse
  }
  // One-off Telegram ping from the on-page panel (e.g. an email-verification gate that needs
  // Parth). Best-effort — the panel message is already on screen; this just reaches his phone.
  if (msg.type === "NOTIFY") {
    (async () => { await notify(msg.text || "", msg.buttons); sendResponse({ ok: true }); })();
    return true; // async sendResponse
  }
  // Orchestrator kick (from the popup or the daily alarm). Long-running: acknowledge
  // immediately and let it report progress over Telegram; the open bridge WS keeps this
  // worker alive across the whole batch.
  if (msg.type === "RUN_QUEUE") {
    runQueue(msg.opts || {});
    sendResponse({ ok: true, started: true });
    return; // responded synchronously
  }
  if (msg.type === "FLUSH_QUEUE") {
    // Manual kick for phase 3. Normally the bridge's idle push does this on its own.
    runFlush();
    sendResponse({ ok: true, started: true });
    return;
  }
  if (msg.type === "STOP_QUEUE") {
    stopRequested = true;
    if (queueRunning || flushRunning) notify("🛑 Stop received — halting after the current form.");
    sendResponse({ ok: true, running: queueRunning || flushRunning });
    return;
  }
  // Dashboard: the popup's merged pile view. Bridge-backed, so it's blank when the bridge
  // is off — the popup falls back to its chrome.storage cache in that case.
  if (msg.type === "LIST_ALL") {
    (async () => {
      try { sendResponse({ ...(await requestBridge({ type: "list_all", since_days: msg.since })) }); }
      catch (e) { sendResponse({ ok: false, error: String((e && e.message) || e) }); }
    })();
    return true; // async sendResponse
  }
  // Dashboard: move one role to a new bucket (submitted / filled / in_queue / skipped / blocked).
  if (msg.type === "SET_STATE") {
    (async () => {
      try { sendResponse(await requestBridge({ type: "set_state", dedup_key: msg.dedupKey, target: msg.target, reason: msg.reason, meta: msg.meta })); }
      catch (e) { sendResponse({ ok: false, error: String((e && e.message) || e) }); }
    })();
    return true; // async sendResponse
  }
  // Popup "Activate on this page": make the fill panel usable on the current tab. Many sites
  // (asana.com/jobs, careers.roblox.com, ...) embed the Greenhouse form as a cross-origin
  // *.greenhouse.io/embed/job_app IFRAME. A panel mounted inside that iframe can't float over
  // the window (it's pinned to the iframe's viewport) and its react-select CDP clicks misfire
  // (iframe-relative coords vs the top-level viewport CDP uses). You also can't open the form
  // via the iframe's own src (parent-bound validityToken -> 503) or the canonical board URL
  // (302s back to the company site). What DOES work: the legacy embed endpoint
  // boards.greenhouse.io/embed/job_app?token=<gh_jid> renders the SAME form standalone
  // top-level (verified), where the panel floats and CDP lines up. So if this tab wraps a
  // Greenhouse form, REDIRECT it there; otherwise mount the panel in place.
  if (msg.type === "ACTIVATE_PANEL") {
    (async () => {
      try {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (!tab || tab.id == null) { sendResponse({ ok: false, error: "no active tab" }); return; }

        // If this is a company-hosted page wrapping a Greenhouse form (gh_jid in the URL, or a
        // *.greenhouse.io/embed/job_app iframe), the form only works inside that iframe and the
        // panel can't float. The legacy embed endpoint boards.greenhouse.io/embed/job_app?token=<id>
        // renders the SAME form standalone (verified), where the panel floats and CDP lines up.
        // So redirect this tab there instead of mounting in the iframe. Skip if we're already on
        // a greenhouse embed/board page (nothing to fix — just mount).
        let probe = null;
        try {
          const [res] = await chrome.scripting.executeScript({
            target: { tabId: tab.id, frameIds: [0] },
            func: () => {
              const onGH = /(?:\/\/|\.)(?:boards|job-boards)\.greenhouse\.io\//i.test(location.href) ||
                /(?:\/\/|\.)greenhouse\.io\/embed\/job_app/i.test(location.href);
              let jid = null;
              try { jid = new URL(location.href).searchParams.get("gh_jid"); } catch (_) {}
              if (!jid) {
                const fr = document.querySelector('iframe[src*="/embed/job_app"], #grnhse_app iframe[src]');
                if (fr) { try { const u = new URL(fr.src); jid = u.searchParams.get("token") || u.searchParams.get("gh_jid"); } catch (_) {} }
              }
              return { onGH, jid: jid && /^\d+$/.test(jid) ? jid : null };
            },
          });
          probe = res && res.result;
        } catch (_) { /* fall through to in-place inject */ }

        if (probe && probe.jid && !probe.onGH) {
          await chrome.tabs.update(tab.id, { url: `https://boards.greenhouse.io/embed/job_app?token=${probe.jid}` });
          sendResponse({ ok: true, redirected: true });
          return;
        }

        // Already on a workable page (or a non-Greenhouse form): mount the panel in place.
        try {
          const r = await chrome.tabs.sendMessage(tab.id, { type: "OPEN_PANEL" });
          if (r && r.ok) { sendResponse({ ok: true, already: true }); return; }
        } catch (_) { /* no content script yet -> inject */ }
        await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, files: ["fill.js"] });
        sendResponse({ ok: true, injected: true });
      } catch (e) {
        sendResponse({ ok: false, error: String((e && e.message) || e) });
      }
    })();
    return true; // async sendResponse
  }
  // Concurrency guard: the popup disables its mark actions while a run owns these jobs.
  if (msg.type === "RUN_STATE") {
    sendResponse({ ok: true, running: queueRunning || flushRunning });
    return; // responded synchronously
  }
  const tabId = sender.tab && sender.tab.id;
  if (tabId == null) return; // CDP relays require a tab (RUN_QUEUE handled above)
  (async () => {
    try {
      if (msg.type === "CDP_ATTACH") { await attach(tabId); sendResponse({ ok: true }); }
      else if (msg.type === "CDP_CLICK") { await attach(tabId); await trustedClick(tabId, msg.x, msg.y); sendResponse({ ok: true }); }
      else if (msg.type === "CDP_TYPE") { await attach(tabId); await trustedType(tabId, msg.text); sendResponse({ ok: true }); }
      else if (msg.type === "CDP_ERASE") { await attach(tabId); await trustedErase(tabId, msg.times); sendResponse({ ok: true }); }
      else if (msg.type === "CDP_KEY") { await attach(tabId); const ok = await trustedKey(tabId, msg.key, msg.times); sendResponse({ ok }); }
      else if (msg.type === "CDP_DETACH") { await detach(tabId); sendResponse({ ok: true }); }
      else return; // unknown type: ignore (let other listeners handle)
    } catch (e) {
      sendResponse({ ok: false, error: String((e && e.message) || e) });
    }
  })();
  return true; // async sendResponse
});

// ── Orchestrator: work the whole daily queue unattended ──────────────────────────────
//
// The extension can drive tabs + own CDP; the bridge holds the queue on disk + Telegram.
// So: ask the bridge for today's triaged list (pipeline-only, deduped, capped), then for
// each role — open the tab, run the deterministic fill (which asks Parth what it can't and
// runs the ✅/✋ review), and on ✅ click Submit + confirm it landed. Zero Claude tokens:
// all local JS + a Chrome API; Parth's only touch is answering asks + one ✅ per form.
//
// Sequential by design (Fable's review): parking many filled tabs to await taps fails —
// Chrome discards background tabs (state lost) and MV3 evicts THIS worker after ~30s idle,
// which detaches the debugger. We hold the bridge WS open across the run (its 15s heartbeat
// keeps the worker alive) and work one tab at a time, closing before the next.

let queueRunning = false;
const sleepBg = (ms) => new Promise(r => setTimeout(r, ms));
const jitter = (min, max) => Math.floor(min + Math.random() * (max - min)); // human-ish pacing
// Company/title strings get interpolated into Telegram parse_mode=HTML messages; a "<"
// or "&" in a job title 400s the whole sendMessage. Bridge-composed text escapes itself;
// everything WE compose here must escape what it interpolates.
const escHtml = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

async function notify(text, buttons) {
  try { await requestBridge({ type: "notify", text, ...(buttons ? { buttons } : {}) }); } catch (_) { /* bridge down: skip */ }
}

// ── Durable applied-log ───────────────────────────────────────────────────────────────
// A CONFIRMED submit must reach applied.json even if the bridge is down at that exact
// moment. The old fire-and-forget lost the record silently: the pending file stayed
// "approved", the next idle flush re-opened the form, and an already-submitted
// application went out a SECOND time. So: try to send; on failure park the record in
// chrome.storage.local and retry on every reconnect tick until the bridge acks.
// append_applied is idempotent on dedup_key, so a duplicate retry is harmless.
const APPLIED_QUEUE_KEY = "appliedQueue";

async function sendAppliedDurable(record) {
  try {
    const res = await requestBridge({ type: "applied", record });
    if (res && res.ok === false) throw new Error(res.error || "bridge refused the record");
    return { sent: true };
  } catch (e) {
    try {
      const { [APPLIED_QUEUE_KEY]: q = [] } = await chrome.storage.local.get(APPLIED_QUEUE_KEY);
      q.push({ record, ts: Date.now() });
      await chrome.storage.local.set({ [APPLIED_QUEUE_KEY]: q });
      console.warn("[applied] bridge unreachable — parked the record for retry:", (e && e.message) || e);
      return { sent: false, queued: true };
    } catch (e2) {
      console.warn("[applied] could not park the record either:", (e2 && e2.message) || e2);
      return { sent: false, queued: false };
    }
  }
}

// Park a job the flush FILLED but could not submit unattended (form differed from what
// Parth approved, a field only he can fill, a verification gate, or a click that never
// confirmed). Moves it to the bridge's held pile so the idle-watcher stops re-driving an
// already-filled form — which was both wasteful (reopening tabs every 5 min) and unsafe
// (re-driving a form that may have half-submitted). Best-effort: the Telegram note to
// Parth already went out, so a failed hold just means the flush-attempt cap catches it.
async function holdJob(rec, reason) {
  try { await requestBridge({ type: "hold_job", dedup_key: rec.dedup_key, reason }); }
  catch (e) { console.warn("[hold] couldn't park", rec && rec.company, (e && e.message) || e); }
}

async function flushAppliedQueue() {
  let q;
  try { ({ [APPLIED_QUEUE_KEY]: q = [] } = await chrome.storage.local.get(APPLIED_QUEUE_KEY)); }
  catch (_) { return; }
  if (!q.length) return;
  const remain = [];
  for (const item of q) {
    try {
      const res = await requestBridge({ type: "applied", record: item.record });
      if (res && res.ok === false) throw new Error(res.error || "refused");
    } catch (_) { remain.push(item); }
  }
  try { await chrome.storage.local.set({ [APPLIED_QUEUE_KEY]: remain }); } catch (_) {}
  if (remain.length < q.length) console.log(`[applied] delivered ${q.length - remain.length} parked record(s)`);
}
// A tappable 🛑 button (callback "stop", intercepted by the bridge's Telegram poller) so
// Parth can halt the queue without typing /stop.
const STOP_BUTTON = [[{ text: "🛑 Stop queue", callback_data: "stop" }]];

// Run a self-contained function in the page and return its result (MV3 scripting API).
async function waitForComplete(tabId, timeoutMs = 45000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    let tab; try { tab = await chrome.tabs.get(tabId); } catch (_) { return false; }
    if (tab.status === "complete") return true;
    await sleepBg(400);
  }
  return false;
}

// Ask the content script to fill this tab; resolve with its report (or an error report).
function fillTab(tabId, autoSubmit, opts) {
  return new Promise((resolve) => {
    chrome.tabs.sendMessage(tabId, { type: "FILL", autoSubmit, opts: opts || {} }, (report) => {
      if (chrome.runtime.lastError) resolve({ errors: [chrome.runtime.lastError.message], filled: [] });
      else resolve(report || { errors: ["no report from content script"], filled: [] });
    });
  });
}

// Screenshot + full field-value dump, captured right before the submit click (Fable's
// audit-trail ask: "when a mapping bug ships a wrong answer to 15 companies, you need to
// know which 15"). captureVisibleTab needs no extra permission beyond the host permissions
// already scoped to the ATS hosts. Best-effort — never blocks the submit if it fails.
async function captureAudit(tab, report, job) {
  try {
    const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
    await requestBridge({
      type: "audit_dump",
      company: job.company, role: job.title, url: job.url,
      summary: report.summary || [],
      screenshot: dataUrl,
    });
  } catch (e) {
    console.warn("[audit] capture/send failed:", (e && e.message) || e);
  }
}

// Confirmation and rejection wording, duplicated from fill.js `matchedConfirmPhrases` and
// `matchedRejectPhrases`. Background keeps its own copy because the page that answers this
// check is the one that replaced the form, so the content script holding those lists is
// gone by the time the check runs.
const CONFIRM_TEXT = ["thank you for applying", "thanks for applying", "application submitted",
  "your application has been submitted", "application received", "successfully submitted",
  "successfully completed", "we'll contact you", "we will contact you", "application complete"];
const REJECT_TEXT = ["already applied", "duplicate application", "you have already submitted"];

// Decide the outcome of a submit whose message channel closed, by reading the tab.
//
// Three signals, in order of strength. Rejection text means the ATS processed the form and
// refused it. Confirmation text means it accepted the form. A URL that changed means the
// form navigated, which is the same test `submitWithRetry` applies at fill.js
// `location.href !== urlBefore`, so treating it as confirmation here matches the verdict
// the content script would have returned had it survived to report.
//
// If none of the three holds, return the original channel error unchanged and let the
// caller park the job. Reading the text needs host permission for whatever page the submit
// landed on, and a board that redirects to a company domain does not grant it, so a failed
// read falls back to the URL comparison rather than failing the check.
async function confirmFromTab(tabId, urlBefore, channelError) {
  const parked = { outcome: "error", detail: channelError };
  try {
    // The channel closes when the navigation starts, so the replacement document is still
    // loading. Wait for it before reading anything off the tab.
    await sleepBg(1000);
    await waitForComplete(tabId);
    const tab = await chrome.tabs.get(tabId);
    const urlNow = String((tab && tab.url) || "");

    let text = "";
    try {
      const [res] = await chrome.scripting.executeScript({
        target: { tabId },
        func: () => ((document.body && document.body.innerText) || "").toLowerCase(),
      });
      text = (res && res.result) || "";
    } catch (_) { /* no host permission for the landing page: URL comparison decides */ }

    const rejected = REJECT_TEXT.find(t => text.includes(t));
    if (rejected) return { outcome: "rejected", detail: rejected };
    const confirmed = CONFIRM_TEXT.find(t => text.includes(t));
    if (confirmed) return { outcome: "confirmed", detail: `channel closed; page says "${confirmed}"` };

    // Compare without the hash, so a fragment the form appends on its own is not read as a
    // navigation. An empty `urlBefore` or `urlNow` leaves the job parked.
    const bare = u => u.split("#")[0];
    if (urlBefore && urlNow && bare(urlNow) !== bare(urlBefore)) {
      return { outcome: "confirmed", detail: `channel closed; the form navigated to ${urlNow}` };
    }
    return parked;
  } catch (e) {
    console.warn("[submit] post-navigation check failed:", (e && e.message) || e);
    return parked;
  }
}

// Submit the approved form by driving the content script's submitWithRetry — the SAME
// hardened loop the on-page panel's Submit button uses: multi-pass, reassertChoices for
// Ashby's post-resume choice-wipe, server-side validation reading with per-field resync,
// plus reject / email-verification detection. The old background path was a single
// synthesized click with no retry or reassert, so Ashby's first-click-swallow and its
// wipe race made otherwise-fine applications silently never submit. Returns
// { outcome, detail } where outcome is submitWithRetry's verdict and detail is the last
// error/warn log line for the Telegram note. See fill.js submitWithRetry for the outcomes.
//
// A submit that navigates kills its own reporter. `submitWithRetry` treats a changed
// `location.href` as success, but the navigation that proves it also destroys the content
// script mid-poll, so `sendResponse` never runs and Chrome closes the message channel with
// "back/forward cache" or "Receiving end does not exist". Both messages describe a page
// that submitted and moved on. If the channel dies, read the tab instead of the content
// script: see `confirmFromTab`.
function submitViaRetry(tabId, urlBefore) {
  return new Promise((resolve) => {
    chrome.tabs.sendMessage(tabId, { type: "SUBMIT" }, (res) => {
      if (chrome.runtime.lastError) {
        const detail = chrome.runtime.lastError.message;
        confirmFromTab(tabId, urlBefore, detail).then(resolve);
        return;
      }
      const logs = (res && res.logs) || [];
      const last = [...logs].reverse().find(l => l.kind === "err" || l.kind === "warn");
      resolve({ outcome: (res && res.outcome) || "error", detail: (last && last.m) || "" });
    });
  });
}

// Rewrite a queue/pending URL to the page the FORM actually lives on:
//   * Ashby links point at the OVERVIEW (jobs.ashbyhq.com/<co>/<uuid>); the form is at
//     .../application — without this the content script has nothing to fill and stalls.
//   * Lever hostedUrl is the DESCRIPTION page; the form is at .../apply. Opening the
//     description made phase 1 "collect" an empty 0-filled/0-question shell.
//   * Greenhouse absolute_url is often a CUSTOM career domain (careers.airbnb.com,
//     www.mongodb.com, careerpuck …) where our content script can't inject at all —
//     5 of 16 jobs in the 07-17 queue. Every GH tenant also serves the hosted board, so
//     rebuild the canonical job-boards URL from ats_slug + ats_job_id (both in queue.json
//     and, since 2026-07-18, in pending records). Worst case the hosted page 404s and the
//     job fails exactly as it did before — no new failure mode.
function applyUrl(job) {
  let url = job.url;
  try {
    const u = new URL(url);
    if (/(^|\.)jobs\.ashbyhq\.com$/i.test(u.hostname) && !/\/application\/?$/i.test(u.pathname)) {
      u.pathname = u.pathname.replace(/\/+$/, "") + "/application";
      url = u.toString();
    } else if (/(^|\.)jobs(\.eu)?\.lever\.co$/i.test(u.hostname) && !/\/apply\/?$/i.test(u.pathname)) {
      u.pathname = u.pathname.replace(/\/+$/, "") + "/apply";
      url = u.toString();
    } else if (job.ats === "greenhouse" && job.ats_slug && job.ats_job_id
               && !/(^|\.)greenhouse\.io$/i.test(u.hostname)) {
      // Custom career domains (careers.airbnb.com, careers.roblox.com, www.samsara.com,
      // www.mongodb.com, app.careerpuck.com…) proxy Greenhouse. The canonical
      // job-boards.greenhouse.io/<slug>/jobs/<id> URL 301-redirects RIGHT BACK to the
      // company's own domain (verified live on a real board), where our content
      // script isn't scoped to inject — so those jobs silently "read nothing" and never
      // collect. The /embed/job_app form URL does NOT redirect: it renders the same
      // React application on a greenhouse.io host the manifest already covers.
      url = `https://job-boards.greenhouse.io/embed/job_app?for=${encodeURIComponent(job.ats_slug)}&token=${encodeURIComponent(job.ats_job_id)}`;
    }
  } catch (_) {}
  return url;
}

// PHASE 3 — everything Parth approved, filled fresh and submitted, unattended.
// Triggered by the bridge's idle push (he went quiet for ~5 min) or manually.
//
// Why re-fill instead of reusing phase 1's tab: MV3 evicts an idle service worker after
// ~30s, which detaches chrome.debugger and kills the fill. Parking a tab for an hour
// while he answers is therefore not an option — so phase 1 closes it and we start over
// here. Filling is free (local JS), so the only cost is a page load.
let flushRunning = false;

async function runFlush() {
  if (queueRunning || flushRunning) return;  // never drive two tab loops at once
  flushRunning = true;
  // A flush push is fresh intent: the jobs it carries are explicit ✅s. A leftover stop
  // from a /stop hours ago used to survive here and break the loop at job one — every
  // idle push then re-notified "🛑 Stopped" forever while approved work sat stranded.
  stopRequested = false;
  try {
    await connectBridge(); // its heartbeat keeps THIS worker alive across the batch
    await flushAppliedQueue(); // deliver any applied-records parked while offline FIRST
    let res;
    try {
      res = await requestBridge({ type: "approved" });
    } catch (e) {
      await notify("⚠️ Couldn't reach the bridge for the approved list: " + ((e && e.message) || e));
      return;
    }
    const jobs = (res && res.jobs) || [];
    if (!jobs.length) return;

    // applied = submitted & confirmed; parked = FILLED but couldn't submit unattended, so
    // held for Parth (never re-driven — a filled form must not be re-attempted); retriable =
    // never got as far as filling (page didn't load), safe to try again, stays approved.
    const summary = { applied: 0, parked: 0, retriable: 0 };
    for (const rec of jobs) {
      if (stopRequested) { await notify("🛑 Stopped — not submitting any more."); break; }
      let tab = null;
      try {
        tab = await chrome.tabs.create({ url: applyUrl(rec), active: true });
        if (!(await waitForComplete(tab.id))) {
          summary.retriable++;
          await notify(`⏭️ <b>${escHtml(rec.company)}</b> — page didn't load; still approved, I'll retry.`);
          continue;
        }
        await sleepBg(jitter(1500, 3000));

        const answers = {};
        for (const q of rec.questions || []) if (q.answer != null) answers[q.id] = q.answer;
        // Exactly what he was shown on Telegram: the deterministic fills plus his answers.
        // fill.js diffs the finished form against this and refuses to submit on drift.
        const approved = [
          ...(rec.filled || []),
          ...(rec.questions || []).map(q => ({ label: q.label, value: q.answer })),
        ];

        // resumeTag: Parth's per-job /resume override, if any. fill.js attaches that
        // track instead of its best-of-4 pick; unset => the auto pick stands.
        const report = await fillTab(tab.id, /* autoSubmit */ true,
          { mode: "apply", answers, approved, resumeTag: rec.resume_override || null,
            company: rec.company || "",
            // What the AI pass decided back in phase 1, already reflected in the summary he
            // approved. Replayed, never recomputed: a second call could answer differently
            // and the drift guard would (rightly) refuse to submit its own fill.
            aiDecisions: rec.ai_decisions || [] });

        if (report.reviewed !== "ok") {
          summary.parked++;
          const mismatched = (report.mismatch || []).length;
          const why = mismatched
            ? "the form came out different from what you approved:\n• " + escHtml(report.mismatch.join("\n• "))
            : escHtml((report.errors || []).join("; ") || "couldn't verify the fill");
          await notify(`🛑 <b>${escHtml(rec.company)}</b> — <b>not</b> submitted: ${why}\nParked for you — finish in the panel, or /retry after it's sorted.`);
          await holdJob(rec, mismatched ? "form differed from what you approved" : "couldn't verify the fill");
          tab = null; // leave it open to inspect
          continue;
        }
        if (!report.submitMeta) {
          summary.parked++;
          await notify(`⚠️ <b>${escHtml(rec.company)}</b>: approved but I don't have a submit map — finish this one by hand.`);
          await holdJob(rec, "no submit button found — finish by hand");
          tab = null;
          continue;
        }

        await captureAudit(tab, report, { company: rec.company, title: rec.title, url: rec.url });
        // Read the URL now rather than reusing `applyUrl(rec)`. The page may have
        // redirected while it loaded, and the post-navigation check compares against
        // wherever the form actually sits.
        let urlBefore = "";
        try { urlBefore = (await chrome.tabs.get(tab.id)).url || ""; } catch (_) {}
        const sub = await submitViaRetry(tab.id, urlBefore);
        const who = `<b>${escHtml(rec.company)}</b> — ${escHtml(rec.title)}`;
        if (sub.outcome === "confirmed") {
          // Also retires the pending record — applied.json is the permanent one now.
          // Durable: if the bridge is down at this exact moment the record parks in
          // chrome.storage and retries, instead of silently vanishing (which left the
          // pending file approved and re-submitted the same application next flush).
          const logged = await sendAppliedDurable({
            dedup_key: rec.dedup_key, company: rec.company, title: rec.title,
            location: rec.location, url: rec.url });
          summary.applied++;
          await notify(`📨 ${who}: submitted & confirmed. ✅`
            + (logged.sent ? "" : "\n<i>(log parked — bridge was unreachable; it'll retry)</i>"));
        } else if (sub.outcome === "needs_verification") {
          // Waiting on Parth's email code. Park it (the form is filled + the submit was
          // accepted — re-driving it would be wrong) and ping him; he enters the code and
          // finishes in the panel, which logs it applied on the real confirmation.
          summary.parked++;
          await notify(`🔐 ${who}: needs an email verification code.\nCheck your inbox, enter the code in the browser, then submit. <i>(Left the tab open.)</i>`);
          await holdJob(rec, "needs an email verification code");
          tab = null;
        } else if (sub.outcome === "needs_input") {
          // A required field the retry loop couldn't resync (genuinely blank, or one only Parth
          // can answer). Park for him to finish by hand — never logged applied, never re-driven.
          summary.parked++;
          await notify(`✋ ${who}: a field needs you by hand${sub.detail ? " — " + escHtml(sub.detail) : ""}.\n<i>(Left the tab open.)</i>`);
          await holdJob(rec, "a required field needs you");
          tab = null;
        } else if (sub.outcome === "rejected") {
          // The ATS processed and refused it (duplicate / application limit). Retrying is
          // pointless, so park it — Parth decides (often it's already applied elsewhere).
          summary.parked++;
          await notify(`❌ ${who}: the ATS refused it${sub.detail ? " — " + escHtml(sub.detail) : " (duplicate or application limit)"}. Left the tab open.`);
          await holdJob(rec, "the ATS refused it (duplicate or limit)");
          tab = null;
        } else {
          // likely | unknown | failing | no_adapter | stopped | error — filled but the submit
          // could not be confirmed. NOT logged applied (a false "applied" would hide the role
          // forever) and NOT re-driven (a re-fill could double-submit if it actually went
          // through) — park it for Parth to check on the open tab.
          summary.parked++;
          await notify(`⚠️ ${who}: couldn't confirm the submit (${escHtml(sub.outcome)})${sub.detail ? " — " + escHtml(sub.detail) : ""}. Left the tab open to check.`);
          await holdJob(rec, `couldn't confirm the submit (${sub.outcome})`);
          tab = null;
        }
      } catch (e) {
        // An exception here almost always fired before/around the fill (tab closed, timeout),
        // so the form wasn't submitted — leave it approved and let the attempt-cap backstop a
        // job that keeps throwing, rather than parking it after a single transient error.
        summary.retriable++;
        await notify(`⚠️ <b>${escHtml(rec.company)}</b>: ${escHtml(String((e && e.message) || e))}`);
      } finally {
        if (tab && tab.id != null) { try { await chrome.tabs.remove(tab.id); } catch (_) {} }
      }
      await sleepBg(jitter(1500, 3500));
    }

    if (summary.applied || summary.parked || summary.retriable) {
      await notify(`✅ <b>Sent ${summary.applied}.</b>`
        + (summary.parked ? ` · ${summary.parked} parked for you (/retry after fixing)` : "")
        + (summary.retriable ? ` · ${summary.retriable} will retry` : ""));
    }
  } finally {
    flushRunning = false;
  }
}

// AD-HOC — one link Parth pasted on Telegram (/apply <url>), not from the daily queue.
// Same phase-1 shape as a single runQueue iteration: open the tab, fill deterministically,
// collect the questions into the batch, close the tab. Submits nothing — he answers on
// Telegram with /next and phase 3 (runFlush) submits it later, exactly like a queued role.
// The dedup_key is left empty; the bridge resolves it (matching today's queue by URL if the
// link is already in it, so an /apply of a queued role can't cause a second application).
async function runApplyUrl(url) {
  if (queueRunning || flushRunning) { await notify("↩️ I'm mid-run — send /apply again once it's finished."); return; }
  if (!/^https?:\/\//i.test(String(url || ""))) { await notify("⚠️ That didn't look like a link. Send /apply followed by the application URL."); return; }
  queueRunning = true;
  stopRequested = false;
  let tab = null;
  try {
    await connectBridge(); // its heartbeat keeps THIS worker alive across the fill
    tab = await chrome.tabs.create({ url, active: true });
    if (!(await waitForComplete(tab.id))) {
      await notify("⏭️ That page didn't load — check the link and try /apply again.");
      return;
    }
    await sleepBg(jitter(1500, 3000)); // React hydration + human-ish delay

    const report = await fillTab(tab.id, /* autoSubmit */ false, { mode: "collect" });
    if ((report.errors || []).length && !(report.filled || []).length) {
      await notify(`⚠️ Couldn't fill it: ${escHtml((report.errors || []).join("; "))}`);
      return;
    }
    const questions = report.questions || [];
    // Nothing read = wrong URL shape (a job description or an unsupported ATS), not a
    // zero-question form. Tell him rather than collecting an empty shell (same guard runQueue uses).
    if (!(report.filled || []).length && !questions.length) {
      await notify("⚠️ I read nothing off that page. Send the <b>application form</b> link (Greenhouse, Lever or Ashby), not the job description.");
      return;
    }
    await requestBridge({
      type: "collect",
      job: {
        dedup_key: "", // the bridge resolves it (URL-match today's queue, else compute)
        company: report.company || "", title: report.title || "",
        location: "", url: report.url || url,
        compensation: (report.jobInfo && report.jobInfo.compensation) || "",
        employment_type: (report.jobInfo && report.jobInfo.employment_type) || "",
        resume: report.resumeChoice ? `${report.resumeChoice.file} (${report.resumeChoice.tag} track)` : "",
      },
      questions,
      filled: report.summary || report.filled || [],
      warnings: report.warnings || [],
      ai_decisions: report.aiDecisions || [],
    });
    await notify(
      `📋 <b>${escHtml(report.company || "This role")}</b> — filled what I can.\n`
      + (questions.length
          ? `• ${questions.length} question(s) need you — send <b>/next</b>.`
          : `• Nothing needed but a ✅ — send <b>/next</b> to review and approve.`));
  } catch (e) {
    await notify(`⚠️ ${escHtml(String((e && e.message) || e))}`);
  } finally {
    if (tab && tab.id != null) { try { await chrome.tabs.remove(tab.id); } catch (_) {} }
    queueRunning = false;
  }
}

// PHASE 1 — read every job in today's queue: fill what's deterministic, note what needs
// Parth, close the tab. Submits nothing; he answers on Telegram at his own pace and
// phase 3 (runFlush) does the submitting.
async function runQueue(opts = {}) {
  if (queueRunning || flushRunning) { await notify("↩️ Already working — ignoring the extra kick."); return; }
  queueRunning = true;
  stopRequested = false; // clear any leftover stop from a previous run
  try {
    await connectBridge(); // its heartbeat keeps THIS worker alive across the batch
    await flushAppliedQueue(); // deliver applied-records parked while the bridge was down
    let res;
    try {
      res = await requestBridge({ type: "queue", max_per_day: opts.maxPerDay, max_per_company: opts.maxPerCompany });
    } catch (e) {
      await notify("⚠️ Couldn't reach the bridge for the queue: " + ((e && e.message) || e));
      return;
    }
    const jobs = (res && res.jobs) || [];
    const stats = (res && res.stats) || {};
    if (!jobs.length) {
      await notify(`📭 Nothing to apply to today (${stats.queue_total || 0} in queue, ${stats.already_applied || 0} already applied`
        + (stats.in_flight ? `, ${stats.in_flight} already in your batch` : "") + `).`);
      return;
    }

    // Triage serves whatever out/queue.json is on disk; if it's a day+ old, Parth
    // probably forgot to git pull after the Actions run and half the roles may be gone.
    const staleWarn = (stats.queue_age_hours != null && stats.queue_age_hours > 26)
      ? `\n⚠️ <i>queue.json is ${Math.round(stats.queue_age_hours)}h old — git pull for today's scrape?</i>` : "";
    await notify(`🔁 <b>Reading ${jobs.length} role(s).</b>\n<i>Filling what I can and noting what needs you — nothing gets submitted in this pass.</i>${staleWarn}\nTap 🛑 or send /stop anytime to halt.`, STOP_BUTTON);
    const summary = { collected: 0, ready: 0, questions: 0, failed: 0 };

    for (const job of jobs) {
      if (stopRequested) { await notify("🛑 Stopped — not reading any more roles."); break; }
      let tab = null;
      try {
        tab = await chrome.tabs.create({ url: applyUrl(job), active: true });
        if (!(await waitForComplete(tab.id))) {
          summary.failed++;
          await notify(`⏭️ <b>${escHtml(job.company)}</b> — page didn't load, skipped.`);
          continue;
        }
        await sleepBg(jitter(1500, 3000)); // React hydration + human-ish delay

        // PHASE 1: fill deterministically, note what's unresolved, ask nobody. The tab
        // closes right after — parking it waiting on a human is what MV3 eviction (which
        // detaches chrome.debugger after ~30s idle) makes impossible.
        const report = await fillTab(tab.id, /* autoSubmit */ false,
          { mode: "collect", company: job.company || "" });
        if ((report.errors || []).length && !report.filled.length) {
          summary.failed++;
          await notify(`⚠️ <b>${escHtml(job.company)}</b>: ${escHtml((report.errors || []).join("; "))}`);
          continue;
        }
        const questions = report.questions || [];
        // A page that yielded NOTHING (0 filled, 0 questions, no errors) is a form we
        // never actually saw — a wrong URL shape, not a zero-question application. It
        // used to collect as "needs nothing but a ✅" and Parth could approve an empty
        // shell (this is exactly how the Lever description-page bug presented).
        if (!report.filled.length && !questions.length) {
          summary.failed++;
          await notify(`⚠️ <b>${escHtml(job.company)}</b>: read nothing off this page (no fields found) — skipped; it'll retry tomorrow.`);
          continue;
        }
        await requestBridge({
          type: "collect",
          job: {
            dedup_key: job.dedup_key, company: job.company, title: job.title,
            location: job.location, url: job.url,
            ats: job.ats || "", ats_slug: job.ats_slug || "", ats_job_id: job.ats_job_id || "",
            compensation: (report.jobInfo && report.jobInfo.compensation) || job.compensation || "",
            employment_type: (report.jobInfo && report.jobInfo.employment_type) || "",
            resume: report.resumeChoice
              ? `${report.resumeChoice.file} (${report.resumeChoice.tag} track)` : "",
          },
          questions,
          filled: report.summary || report.filled || [],
          ai_decisions: report.aiDecisions || [],
          warnings: report.warnings || [],
        });
        summary.collected++;
        summary.questions += questions.length;
        if (!questions.length) summary.ready++;
      } catch (e) {
        summary.failed++;
        await notify(`⚠️ <b>${escHtml(job.company)}</b>: ${escHtml(String((e && e.message) || e))}`);
      } finally {
        if (tab && tab.id != null) { try { await chrome.tabs.remove(tab.id); } catch (_) {} }
      }
      await sleepBg(jitter(1500, 3500)); // gap between jobs (bot-detection hygiene)
    }

    await notify(
      `📋 <b>Read ${summary.collected} role(s).</b>\n`
      + `• ${summary.questions} question(s) need you across ${summary.collected - summary.ready} role(s)\n`
      + `• ${summary.ready} need nothing but a ✅\n`
      + (summary.failed ? `• ${summary.failed} failed to read\n` : "")
      + `\nSend <b>/next</b> when you have a few minutes.`);
  } finally {
    queueRunning = false;
  }
}
