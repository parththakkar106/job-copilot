#!/usr/bin/env node
/**
 * Unit tests for attach() in background.js: taking a debugger session on a tab.
 *
 * MV3 evicts the service worker after about 30 seconds idle, and the `attached` set goes
 * with it. Chrome keeps the debugger session, so the next attach fails with "Another
 * debugger is already attached" and every trusted click on the form is refused.
 *
 * background.js is a classic service worker with no exports, so the real function source
 * is extracted and run against a fake `chrome.debugger`.
 *
 * Run: node extension/test_debugger_attach.mjs
 */
import { readFileSync } from "fs";

const src = readFileSync(new URL("./background.js", import.meta.url), "utf8");
let passed = 0, failed = 0;
const ok = (cond, name) => {
  if (cond) passed++; else { failed++; console.log("FAIL " + name); }
};

// The slice from rawAttach through the end of attach() holds all three helpers.
const start = src.indexOf("const rawAttach = ");
const at = src.indexOf("async function attach(", start);
let depth = 0, end = src.indexOf("{", at);
for (; end < src.length; end++) {
  if (src[end] === "{") depth++;
  else if (src[end] === "}" && --depth === 0) break;
}
const body = src.slice(start, end + 1);

// A fake debugger. `holder` is who holds the tab: null, "stale" (a session this worker
// forgot), or "devtools" (which refuses a detach from the extension).
function build(holder) {
  const calls = [];
  const chrome = {
    runtime: { lastError: null },
    debugger: {
      attach(_target, _version, cb) {
        calls.push("attach");
        chrome.runtime.lastError = holder
          ? { message: "Another debugger is already attached to the tab with id: 7." } : null;
        if (!holder) holder = "ours";
        cb();
      },
      detach(_target, cb) {
        calls.push("detach");
        if (holder === "devtools") {
          chrome.runtime.lastError = { message: "Cannot detach a debugger it did not attach." };
        } else { holder = null; chrome.runtime.lastError = null; }
        cb();
      },
    },
  };
  const attached = new Set();
  const attach = new Function("chrome", "attached", body + "; return attach;")(chrome, attached);
  return { attach, attached, calls };
}

{
  const { attach, attached, calls } = build(null);
  await attach(7);
  ok(attached.has(7), "a free tab is attached and recorded");
  ok(calls.join() === "attach", "with a single attach call");
}

{
  const { attach, attached, calls } = build("stale");
  let err = null;
  try { await attach(7); } catch (e) { err = e; }
  ok(!err, "a session the evicted worker forgot is recovered, not reported as an error");
  ok(attached.has(7), "and the tab is recorded as attached");
  ok(calls.join() === "attach,detach,attach", "by detaching the stale session and attaching again");
}

{
  const { attach, attached } = build("devtools");
  let err = null;
  try { await attach(7); } catch (e) { err = e; }
  ok(err && /already attached/i.test(err.message),
     "when DevTools holds the tab, the original error is re-thrown so the report names it");
  ok(!attached.has(7), "and the tab is not recorded as attached");
}

{
  const { attach, calls } = build(null);
  await attach(7);
  await attach(7);
  ok(calls.join() === "attach", "a tab already recorded is not attached twice");
}

console.log(`debugger attach: ${passed}/${passed + failed} checks passed`);
if (failed) process.exit(1);
