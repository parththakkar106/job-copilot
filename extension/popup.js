// popup.js — dashboard over every pile. Reads list_all / set_state from the bridge via the
// background service worker, caches the last snapshot so it still renders when the bridge is
// off (read-only), and disables the mark actions while a queue/flush run owns the jobs.

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[<>&"]/g, c => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));
const send = (msg) => chrome.runtime.sendMessage(msg);

const BUCKETS = [["all", "All"], ["in_queue", "In queue"], ["filled", "Filled"], ["submitted", "Submitted"], ["blocked", "Blocked"]];
// Valid moves out of each bucket. First entry is the most likely next step.
const TARGETS = {
  in_queue: [["filled", "Filled (ready)"], ["skipped", "Skip"], ["submitted", "✓ Submitted"], ["blocked", "🚫 Block"]],
  filled:   [["in_queue", "↩ In queue"], ["submitted", "✓ Submitted"], ["skipped", "Skip"], ["blocked", "🚫 Block"]],
  submitted: [["in_queue", "↩ Un-submit"], ["blocked", "🚫 Block"]],
  blocked:  [["in_queue", "↩ Un-block"]],
};
const CONFIRM = new Set(["submitted", "blocked"]); // writes applied.json — ask first
// Where `python web/run.py` serves the console (local-only, 127.0.0.1:8000). Reuses an
// already-open console tab instead of piling up duplicates.
const CONSOLE_URL = "http://127.0.0.1:8000/";

let rows = [];
let rowsByKey = {};
let filter = "all";
let offline = false;
let running = false;
let offlineNote = "";

async function load() {
  try {
    const run = await send({ type: "RUN_STATE" });
    running = !!(run && run.running);
  } catch (_) { running = false; }

  let res = null;
  try { res = await send({ type: "LIST_ALL" }); } catch (e) { res = { ok: false, error: String((e && e.message) || e) }; }

  if (res && res.ok && Array.isArray(res.rows)) {
    rows = res.rows; offline = false;
    chrome.storage.local.set({ dashCache: rows, dashCacheAt: Date.now() });
  } else {
    const c = await chrome.storage.local.get(["dashCache", "dashCacheAt"]);
    rows = Array.isArray(c.dashCache) ? c.dashCache : [];
    offline = true;
    const when = c.dashCacheAt ? new Date(c.dashCacheAt).toLocaleString() : "never";
    offlineNote = `Bridge offline (${esc((res && res.error) || "unreachable")}) — showing the snapshot from ${esc(when)}. Start it to make changes.`;
  }
  rowsByKey = Object.fromEntries(rows.map(r => [r.dedup_key, r]));
  render();
}

function counts() {
  const c = { all: rows.length };
  for (const r of rows) c[r.bucket] = (c[r.bucket] || 0) + 1;
  return c;
}

function render() {
  $("banner").style.display = offline ? "block" : "none";
  $("banner").textContent = offline ? "⚠️ " + offlineNote : "";

  const c = counts();
  $("chips").innerHTML = BUCKETS.map(([id, label]) =>
    `<span class="chip ${id === filter ? "on" : ""}" data-f="${id}">${label}<span class="n">${c[id] || 0}</span></span>`).join("");

  const shown = rows.filter(r => filter === "all" || r.bucket === filter);
  const locked = offline || running;
  if (!shown.length) {
    $("list").innerHTML = `<p class="empty">${rows.length ? "Nothing in this bucket." : "No roles yet — run the queue to collect some."}</p>`;
  } else {
    $("list").innerHTML = shown.map(r => {
      const opts = (TARGETS[r.bucket] || []).map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join("");
      return `<div class="job b-${r.bucket}">
        <div class="top"><span class="date">${esc(r.date || "")}</span><span class="co">${esc(r.company)}</span><span class="title">${esc(r.title)}</span></div>
        <div class="bottom"><span class="tag">${esc(r.tag || r.bucket)}</span>
          ${r.url && r.apply_url && r.apply_url !== r.url ? `<a class="open" href="#" data-url="${esc(r.url)}" title="Job description">JD</a>` : ""}
          ${r.url ? `<a class="open" href="#" data-url="${esc(r.apply_url || r.url)}">${r.apply_url && r.apply_url !== r.url ? "fill form ↗" : "open ↗"}</a>` : ""}
          <select class="mark" data-key="${esc(r.dedup_key)}" ${locked ? "disabled" : ""} title="${locked ? (running ? "a run is active" : "bridge offline") : "move this role"}">
            <option value="">Mark…</option>${opts}
          </select>
        </div></div>`;
    }).join("");
  }
  $("note").textContent = running ? "A run is active — mark actions are paused." : `${rows.length} role(s) · newest first`;
}

async function mark(key, target, selectEl) {
  const row = rowsByKey[key];
  if (!row) return;
  if (CONFIRM.has(target) && !confirm(`Mark "${row.company} — ${row.title}" as ${target}?\nThis writes applied.json and removes it from the queue.`)) {
    selectEl.value = ""; return;
  }
  selectEl.disabled = true;
  let res = null;
  try {
    res = await send({ type: "SET_STATE", dedupKey: key, target, meta: { company: row.company, title: row.title, location: row.location, url: row.url } });
  } catch (e) { res = { ok: false, error: String((e && e.message) || e) }; }
  if (!res || !res.ok) { alert("Couldn't move it: " + esc((res && res.error) || "bridge error")); selectEl.disabled = false; selectEl.value = ""; return; }
  await load();
}

// ── events ───────────────────────────────────────────────────────────────────────────
$("chips").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (chip) { filter = chip.dataset.f; render(); }
});
$("list").addEventListener("click", (e) => {
  const a = e.target.closest("a.open");
  if (a) { e.preventDefault(); chrome.tabs.create({ url: a.dataset.url }); }
});
$("list").addEventListener("change", (e) => {
  const sel = e.target.closest("select.mark");
  if (sel && sel.value) mark(sel.dataset.key, sel.value, sel);
});
$("refresh").addEventListener("click", load);
$("activate").addEventListener("click", async () => {
  const btn = $("activate");
  btn.disabled = true;
  try {
    const res = await send({ type: "ACTIVATE_PANEL" });
    if (!res || !res.ok) throw new Error((res && res.error) || "couldn't inject");
    $("note").textContent = res.redirected
      ? "Embedded form → opening the standalone version; the 💼 panel will appear there."
      : res.already ? "Panel is already on this page." : "Panel activated — look for the 💼 at the bottom-right.";
    setTimeout(() => window.close(), 1000);
  } catch (e) {
    $("note").textContent = "Couldn't activate here: " + ((e && e.message) || e);
  } finally {
    btn.disabled = false;
  }
});
$("console").addEventListener("click", async () => {
  // Focus an already-open console tab if there is one; otherwise open a fresh one.
  const base = CONSOLE_URL.replace(/\/$/, "");
  const existing = await chrome.tabs.query({ url: base + "/*" });
  if (existing.length) {
    chrome.tabs.update(existing[0].id, { active: true });
    if (existing[0].windowId != null) chrome.windows.update(existing[0].windowId, { focused: true });
  } else {
    chrome.tabs.create({ url: CONSOLE_URL });
  }
  window.close();
});
$("runqueue").addEventListener("click", async () => {
  try {
    const res = await send({ type: "RUN_QUEUE" });
    if (!res || !res.ok) throw new Error((res && res.error) || "background didn't start the run");
    $("note").textContent = "Queue started — watch Telegram. Refreshing…";
    setTimeout(load, 1500);
  } catch (e) {
    $("note").textContent = "Couldn't start: " + ((e && e.message) || e) + " — is the bridge running?";
  }
});
$("stopqueue").addEventListener("click", async () => {
  try { const res = await send({ type: "STOP_QUEUE" }); $("note").textContent = (res && res.running) ? "🛑 Stopping after the current form." : "No run is active."; }
  catch (_) {}
});

load();
