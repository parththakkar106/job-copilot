#!/usr/bin/env node
/**
 * Unit tests for background.js `confirmFromTab`, the check that runs when a submit
 * navigates and takes its own reporter with it.
 *
 * This function decides whether an application is logged as applied. A wrong "confirmed"
 * records an application that never went out, and a wrong "error" parks a real one and
 * invites a duplicate submit on the next `/retry`. Both failures are expensive, so pin the
 * signals here.
 *
 * background.js is a service worker with no exports, so pull the real function source out
 * of it and evaluate that. A copy-pasted duplicate would only test the copy.
 *
 * Run: node extension/test_submit_confirm.mjs
 */
import { readFileSync } from "fs";

const src = readFileSync(new URL("./background.js", import.meta.url), "utf8");

function extract(name) {
  const i = src.indexOf(`async function ${name}(`);
  if (i < 0) throw new Error(`${name}() not found in background.js — did it get renamed?`);
  let depth = 0, j = src.indexOf("{", i);
  for (; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}" && --depth === 0) break;
  }
  return src.slice(i, j + 1);
}

// The phrase lists are plain `const NAME = [...];` declarations. Read to the terminating
// semicolon, because both wrap across several lines.
function extractConst(name) {
  const i = src.indexOf(`const ${name} = `);
  if (i < 0) throw new Error(`const ${name} not found in background.js`);
  for (let j = src.indexOf("\n", i); j > 0; j = src.indexOf("\n", j + 1)) {
    if (src.slice(i, j).trimEnd().endsWith(";")) return src.slice(i, j);
  }
  throw new Error(`const ${name} has no terminating semicolon`);
}

// `chrome` and the two timing helpers are injected. The real `sleepBg`/`waitForComplete`
// only add delay, and the tests care about the verdict, not the wait.
const makeConfirmFromTab = (chrome) => new Function(
  "chrome", "sleepBg", "waitForComplete", "console",
  `${extractConst("CONFIRM_TEXT")}
   ${extractConst("REJECT_TEXT")}
   ${extract("confirmFromTab")}
   return confirmFromTab;`
)(chrome, async () => {}, async () => true, { warn() {} });

// A tab that answers with `url`, and a page whose text is `text`. Pass `text: null` to
// model a landing page the extension has no host permission to read.
function stubChrome({ url, text }) {
  return {
    tabs: { get: async () => ({ url }) },
    scripting: {
      executeScript: async () => {
        if (text === null) throw new Error("Cannot access contents of the page");
        return [{ result: String(text).toLowerCase() }];
      },
    },
  };
}

let pass = 0, fail = 0;
function ok(cond, what) {
  if (cond) { pass++; return; }
  fail++;
  console.error("FAIL:", what);
}

const BFCACHE = "The page keeping the extension port is moved into back/forward cache, "
              + "so the message channel is closed.";
const APPLY = "https://job-boards.greenhouse.io/embed/job_app?for=cloudflare&token=99";

{
  // The reported failure: the submit navigated to a thank-you page, which is why the
  // channel closed at all. Both halves of the evidence agree, so it is confirmed.
  const f = makeConfirmFromTab(stubChrome({
    url: "https://job-boards.greenhouse.io/embed/job_app/thanks?token=99",
    text: "Thank you for applying. We will contact you.",
  }));
  const r = await f(1, APPLY, BFCACHE);
  ok(r.outcome === "confirmed", "a navigation plus confirmation text reads as confirmed");
}

{
  // Confirmation text alone decides it, even on a board that rewrites the form in place
  // and leaves the URL untouched.
  const f = makeConfirmFromTab(stubChrome({ url: APPLY, text: "Your application has been submitted." }));
  const r = await f(1, APPLY, BFCACHE);
  ok(r.outcome === "confirmed", "confirmation text alone is enough when the URL is unchanged");
}

{
  // A board that redirects to a company domain grants no host permission, so the text read
  // throws. The URL change still stands on its own, and it is the same test the content
  // script applies at fill.js `location.href !== urlBefore`.
  const f = makeConfirmFromTab(stubChrome({ url: "https://www.cloudflare.com/careers/thanks/", text: null }));
  const r = await f(1, APPLY, BFCACHE);
  ok(r.outcome === "confirmed", "an unreadable landing page still confirms on the URL change");
}

{
  // Rejection outranks everything. The ATS processed the form and refused it, so the job
  // must not be logged applied and must not be re-driven.
  const f = makeConfirmFromTab(stubChrome({
    url: "https://job-boards.greenhouse.io/embed/job_app/error",
    text: "You have already submitted an application for this role.",
  }));
  const r = await f(1, APPLY, BFCACHE);
  ok(r.outcome === "rejected", "rejection text wins over the URL change");
}

{
  // Nothing moved and nothing on the page says the form went through, so the original
  // channel error stands and the job parks.
  const f = makeConfirmFromTab(stubChrome({ url: APPLY, text: "First name Last name Submit" }));
  const r = await f(1, APPLY, BFCACHE);
  ok(r.outcome === "error", "no navigation and no confirmation text parks the job");
  ok(r.detail === BFCACHE, "a parked job reports the original channel error");
}

{
  // A fragment the form appends is not a navigation.
  const f = makeConfirmFromTab(stubChrome({ url: APPLY + "#app", text: "Submit" }));
  const r = await f(1, APPLY, BFCACHE);
  ok(r.outcome === "error", "a hash-only change is not treated as a navigation");
}

{
  // With no URL to compare against there is no evidence either way, so park it.
  const f = makeConfirmFromTab(stubChrome({ url: "https://example.com/thanks", text: "Submit" }));
  const r = await f(1, "", BFCACHE);
  ok(r.outcome === "error", "an unknown starting URL parks rather than guessing confirmed");
}

{
  // Every tab read failing must not throw out of the check.
  const f = makeConfirmFromTab({
    tabs: { get: async () => { throw new Error("No tab with id"); } },
    scripting: { executeScript: async () => { throw new Error("no"); } },
  });
  const r = await f(1, APPLY, BFCACHE);
  ok(r.outcome === "error", "a closed tab parks instead of throwing");
}

console.log(`submit confirm: ${pass}/${pass + fail} checks passed`);
process.exit(fail ? 1 : 0);
