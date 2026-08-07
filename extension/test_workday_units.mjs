#!/usr/bin/env node
/**
 * Unit tests for the Workday driver's decision logic.
 *
 * The DOM-touching parts need a real Workday page and are verified by running the thing.
 * What IS testable here is the logic that decides WHAT to do — and those are exactly the
 * places a silent wrong answer ends up on a real application:
 *
 *   currentStep      the only "where am I" signal, since the URL never changes
 *   step-6 guard     the difference between filling a form and submitting one
 *   date parsing     the field that already cost an hour by looking filled when it wasn't
 *   choice matching  radio ids are opaque ("eeap3"), so text matching is load-bearing
 *   label stripping  a stray "*" makes a label miss its taxonomy rule
 *
 * Run: node extension/test_workday_units.mjs
 */
import { readFileSync } from "fs";
import { WORKDAY } from "./adapters/workday.js";

let pass = 0, fail = 0;
const eq = (got, want, name) => {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g === w) { pass++; return; }
  fail++; console.error(`FAIL ${name}\n   got:  ${g}\n   want: ${w}`);
};
const ok = (cond, name) => eq(!!cond, true, name);

// The driver is an ES module but its helpers are not exported (they are internals).
// Pull the real source rather than re-implementing: a copy would only test the copy.
const src = readFileSync(new URL("./workday_driver.js", import.meta.url), "utf8");
function extract(name) {
  const i = src.search(new RegExp(`function ${name}\\s*\\(`));
  if (i < 0) throw new Error(`${name}() not found in workday_driver.js — renamed?`);
  let depth = 0, j = src.indexOf("{", i);
  for (; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}" && --depth === 0) break;
  }
  return src.slice(i, j + 1);
}

// ── minimal DOM so the extracted helpers can run ─────────────────────────────
class El {
  constructor(tag, attrs = {}, text = "", kids = []) {
    this.tagName = tag.toUpperCase(); this.attrs = attrs;
    this._text = text; this.kids = kids; this.id = attrs.id || "";
    this.type = attrs.type || ""; this.checked = false; this.clicked = 0;
  }
  get innerText() {
    return this._text || this.kids.map(k => k.innerText).filter(Boolean).join("\n");
  }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  // A real label[for] click activates its input — the driver clicks the LABEL (the option
  // text is the only stable handle), so the harness has to model that or the assertion
  // would test the harness rather than the driver.
  click() {
    this.clicked++;
    if (this.type === "radio" || this.type === "checkbox") this.checked = true;
    if (this.tagName === "LABEL" && this._owner) this._owner.click();
  }
  _all() { return this.kids.flatMap(k => [k, ...k._all()]); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    // Only the shapes these helpers actually use.
    return this._all().filter(e => sel.split(",").some(part => {
      part = part.trim();
      if (part === "label") return e.tagName === "LABEL";
      if (part === "legend") return e.tagName === "LEGEND";
      if (part === "input,textarea" || part === "input" ) return e.tagName === "INPUT";
      if (part === "input[type=radio]") return e.type === "radio";
      if (part === "input[type=checkbox]") return e.type === "checkbox";
      if (part === "input[type=radio]" ) return e.type === "radio";
      const m = part.match(/^label\[for="(.+)"\]$/);
      if (m) return e.tagName === "LABEL" && e.getAttribute("for") === m[1];
      return false;
    }));
  }
  closest() { return null; }
}
globalThis.CSS = { escape: s => s };

// ── currentStep + the step-6 guard ───────────────────────────────────────────
const currentStep = eval(`(${extract("currentStep")})`);
const mkDoc = (activeText) => ({
  querySelector: (sel) => sel === WORKDAY.multiStep.activeStep
    ? { innerText: activeText } : null,
});

globalThis.document = mkDoc("current step 1 of 6\nMy Information");
eq(currentStep(WORKDAY), { n: 1, of: 6, name: "My Information" }, "currentStep parses step 1");

globalThis.document = mkDoc("current step 3 of 6\nApplication Questions");
eq(currentStep(WORKDAY), { n: 3, of: 6, name: "Application Questions" }, "currentStep parses step 3");

globalThis.document = mkDoc("current step 6 of 6\nReview");
const at6 = currentStep(WORKDAY);
eq(at6.n, 6, "currentStep parses Review");
// THE guard: the loop must stop at submitStep, because on Review the footer button that
// the driver clicks on every other step IS the submit button.
ok(at6.n >= WORKDAY.multiStep.submitStep, "step 6 trips the submit guard");
ok(currentStep(WORKDAY) && 5 < WORKDAY.multiStep.submitStep, "step 5 does not trip it");

globalThis.document = { querySelector: () => null };
eq(currentStep(WORKDAY), null, "currentStep null off an apply page");

globalThis.document = mkDoc("no step marker here");
eq(currentStep(WORKDAY), null, "currentStep null when the text has no step number");

// ── date parsing (the MMYYYY the trusted-typing path sends) ──────────────────
const parseDate = (iso) => {
  const m = String(iso || "").match(/^(\d{4})-(\d{1,2})/);
  return m ? String(m[2]).padStart(2, "0") + m[1] : null;
};
eq(parseDate("2024-01"), "012024", "date: 2024-01 -> 012024");
eq(parseDate("2022-05"), "052022", "date: 2022-05 -> 052022");
eq(parseDate("2024-1"), "012024", "date: single-digit month is padded");
eq(parseDate("2024-12-31"), "122024", "date: full ISO keeps year+month only");
eq(parseDate("January 2024"), null, "date: display string rejected, not silently mangled");
eq(parseDate(null), null, "date: null rejected");
eq(parseDate(""), null, "date: empty rejected");

// ── label extraction ─────────────────────────────────────────────────────────
const extractLabel = eval(`(${extract("extractLabel")})`);
const wrap = (labelText, kids = []) =>
  new El("div", { "data-automation-id": "formField-x" }, "",
    [new El("label", {}, labelText), ...kids]);

eq(extractLabel(wrap("First Name*"), WORKDAY), "First Name", "label: trailing * stripped");
eq(extractLabel(wrap("Country*"), WORKDAY), "Country", "label: required marker stripped");
eq(extractLabel(wrap("Address Line 1"), WORKDAY), "Address Line 1", "label: plain kept");
eq(extractLabel(wrap("Are you of legal age to work?*"), WORKDAY),
   "Are you of legal age to work?", "label: question mark survives, * does not");

// ── choice matching by label text (ids are opaque: eeap3/eeap4) ──────────────
const clickChoiceByText = eval(`(${extract("clickChoiceByText")})`);
function radioGroup() {
  const yes = new El("input", { id: "eeap3", type: "radio" });
  const no  = new El("input", { id: "eeap4", type: "radio" });
  const yesLab = new El("label", { for: "eeap3" }, "Yes");
  const noLab  = new El("label", { for: "eeap4" }, "No");
  yesLab._owner = yes; noLab._owner = no;
  return new El("div", {}, "", [yes, yesLab, no, noLab]);
}
let g = radioGroup();
ok(clickChoiceByText(g, "No"), "choice: matches 'No' by label text");
eq(g.querySelectorAll("input[type=radio]")[1].checked, true, "choice: clicked the No radio");
eq(g.querySelectorAll("input[type=radio]")[0].checked, false, "choice: left Yes alone");

g = radioGroup();
ok(clickChoiceByText(g, "yes"), "choice: case-insensitive");
eq(g.querySelectorAll("input[type=radio]")[0].checked, true, "choice: clicked Yes");

g = radioGroup();
ok(!clickChoiceByText(g, "Maybe"), "choice: unknown option does NOT click anything");
eq(g.querySelectorAll("input[type=radio]").some(r => r.checked), false,
   "choice: a miss leaves the group untouched rather than guessing");

// ── adapter invariants the driver relies on ──────────────────────────────────
ok(WORKDAY.multiStep.submitStep === 6, "adapter: submitStep is Review");
ok(WORKDAY.submit.verified === false, "adapter: submit still flagged unverified");
ok(WORKDAY.entry.clonePolicy.requireSameStream === true,
   "adapter: cloning requires a matching resume stream");
ok(WORKDAY.promptTree.needsCDP && WORKDAY.date.needsCDP,
   "adapter: prompt tree and date both demand trusted input");
ok(WORKDAY.dropdown.optionScope.includes("not(") &&
   WORKDAY.dropdown.optionScope.includes("selectedItemList"),
   "adapter: option scope excludes the selectedItemList decoy");
ok(WORKDAY.customQuestion.knownCoreIds.includes("formField-source"),
   "adapter: source counted as core so the taxonomy pass skips it");
ok(!WORKDAY.customQuestion.knownCoreIds.includes("formField-candidateIsPreviousWorker"),
   "adapter: tenant question NOT in core ids, so the taxonomy pass sees it");

console.log(`workday units: ${pass}/${pass + fail} checks passed`);
process.exit(fail ? 1 : 0);
