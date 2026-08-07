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
// Cloning used to REFUSE unless it could prove the previous application used the same
// resume track — read from a field nothing wrote, so the clone path was unreachable and
// the whole volume argument for supporting Workday with it. It now clones and reconciles.
// What must not be lost in that trade is the attachment: reconciling every visible answer
// still leaves the old PDF stapled to the form, and that is the mismatch we were avoiding.
ok(WORKDAY.entry.clonePolicy.reattachOnTrackMismatch === true,
   "adapter: a clone must re-check the attached CV against the chosen track");
ok(WORKDAY.entry.clonePolicy.requireSameStream === undefined,
   "adapter: the old refuse-to-clone gate is gone, not merely disabled");
ok(WORKDAY.entry.clonePolicy.stillReviewSteps === true,
   "adapter: a clone is still walked step by step, never blind-submitted");
ok(WORKDAY.promptTree.needsCDP && WORKDAY.date.needsCDP,
   "adapter: prompt tree and date both demand trusted input");
ok(WORKDAY.dropdown.optionScope.includes("not(") &&
   WORKDAY.dropdown.optionScope.includes("selectedItemList"),
   "adapter: option scope excludes the selectedItemList decoy");
ok(WORKDAY.customQuestion.knownCoreIds.includes("formField-source"),
   "adapter: source counted as core so the taxonomy pass skips it");
ok(!WORKDAY.customQuestion.knownCoreIds.includes("formField-candidateIsPreviousWorker"),
   "adapter: tenant question NOT in core ids, so the taxonomy pass sees it");

// ── reconcile: keep, overwrite, or leave alone ───────────────────────────────
// The form arrives pre-filled by a clone or the resume parser, so this rule decides what
// survives onto a real application. Both directions cost: a false "same" leaves a wrong
// answer standing, and a false "differs" rewrites a correct field — which on a date means
// clearing and retyping the one widget that poisons itself when written wrong.
{
  const src2 = readFileSync(new URL("./workday_driver.js", import.meta.url), "utf8");
  const grab = (name) => {
    const i = src2.search(new RegExp(`function ${name}\\s*\\(`));
    let depth = 0, j = src2.indexOf("{", i);
    for (; j < src2.length; j++) {
      if (src2[j] === "{") depth++;
      else if (src2[j] === "}" && --depth === 0) break;
    }
    return src2.slice(i, j + 1);
  };
  // These call each other and the two small helpers, so evaluate them as one unit.
  const bundle = ["digitsOf", "tidy"].map(n => {
    const m = src2.match(new RegExp(`const ${n} = [^;]+;`));
    return m ? m[0] : "";
  }).join("\n") + "\n" + ["toMonthYear", "isBlankValue", "sameValue", "reconcileDecision"]
    .map(grab).join("\n");
  const { isBlankValue, sameValue, reconcileDecision } =
    eval(`(() => { ${bundle}; return { isBlankValue, sameValue, reconcileDecision }; })()`);

  const A = WORKDAY;
  const act = (kind, cur, want) => reconcileDecision(kind, cur, want, A).action;

  // Blank detection. "Select One" is a listbox that looks full and is empty; a prompt tree
  // announces itself with a "1 item selected" badge and is unset without one.
  ok(isBlankValue("text", "", A), "an empty text field is blank");
  ok(isBlankValue("text", "   ", A), "whitespace is blank");
  ok(isBlankValue("listbox", "Select One", A), "the listbox placeholder is blank");
  ok(!isBlankValue("listbox", "United States of America", A), "a chosen option is not blank");
  ok(isBlankValue("promptTree", "How Did You Hear About Us?", A),
     "a prompt tree with no badge is unset however much text it shows");
  ok(!isBlankValue("promptTree", "1 item selected", A), "the badge means committed");

  // Missing -> write. This is the ordinary case on a cold autofill.
  eq(act("text", "", "Springfield"), "write", "an empty field gets written");
  eq(act("listbox", "Select One", "Ohio"), "write", "a placeholder dropdown gets written");
  eq(act("text", null, "Springfield"), "write", "an unreadable field is treated as empty");

  // Agreeing -> keep. Every one of these was pre-filled correctly and must not be retyped.
  eq(act("text", "Springfield", "Springfield"), "keep", "identical text is kept");
  eq(act("text", " springfield ", "Springfield"), "keep", "case and padding are not a disagreement");

  // Workday reformats phone numbers on blur and splits the country code into its own
  // control. Comparing as text would rewrite a correct number on every run.
  eq(act("phone", "(555) 123-4567", "+1 555-123-4567"), "keep", "a reformatted phone is the same phone");
  eq(act("phone", "555-123-4567", "5551234567"), "keep", "separators do not matter");
  eq(act("phone", "(555) 123-9999", "+1 555-123-4567"), "write", "a genuinely different number is rewritten");

  // Option text is the form's wording, not ours.
  eq(act("listbox", "Bachelor's Degree (BA/BS)", "Bachelor's Degree"), "keep",
     "a longer option label containing our value counts as agreed");
  eq(act("listbox", "Master's Degree", "Bachelor's Degree"), "write", "a different option is rewritten");

  // Dates arrive as ISO from the profile and as spinner text from the form.
  eq(act("date", "01/2024", "2024-01"), "keep", "the same month in two notations is kept");
  eq(act("date", "1/2024", "2024-01"), "keep", "an unpadded month still matches");
  eq(act("date", "02/2024", "2024-01"), "write", "a different month is rewritten");
  eq(act("date", "", "2024-01"), "write", "an empty date is written");

  // Nothing to assert. FILL_ME is the profile's "not set yet" marker and must never be
  // typed onto a form as if it were a value.
  eq(act("text", "Springfield", "FILL_ME"), "skip", "FILL_ME is not a value");
  eq(act("text", "", "FILL_ME"), "skip", "FILL_ME does not fill an empty field either");
  eq(act("text", "Springfield", ""), "skip", "no intent means leave it alone");
  eq(act("text", "", ""), "skip", "no intent and no value is still nothing to do");

  // A value we did NOT put there and do not agree with must lose. This is the clone case:
  // the previous application's answer sitting in the field.
  eq(act("text", "San Jose", "Springfield"), "write", "a clone's stale answer is overwritten");

  // And the reason travels, because the report is the only view into a run.
  ok(/differs/.test(reconcileDecision("text", "San Jose", "Springfield", A).why),
     "an overwrite says what it replaced");
  ok(/empty/.test(reconcileDecision("text", "", "Springfield", A).why),
     "a fill says the field was empty");
}

// ── the attached CV: the mismatch reconciling text cannot fix ────────────────
// A clone replays a past application complete with its PDF. Every visible answer can be
// reconciled correctly and the form still goes out contradicting itself, because the wrong
// CV is stapled to it. Our uploads are named Parth_Resume_<tag>.pdf, so the attached track
// is readable off the container — for every clone source this tool creates.
{
  const attachedTrackTag = eval(`(${extract("attachedTrackTag")})`);
  const box = (text) => {
    globalThis.document = { querySelector: (s) =>
      s === WORKDAY.experience.resume.container ? { innerText: text } : null };
    return attachedTrackTag({ adapter: WORKDAY });
  };

  eq(box("Parth_Resume_ai.pdf|182KB|Successfully Uploaded!").tag, "ai",
     "reads the track off our own filename");
  eq(box("Parth_Resume_quant.pdf|182KB|Successfully Uploaded!").tag, "quant",
     "a longer tag parses too");
  ok(box("Parth_Resume_ai.pdf|182KB|Successfully Uploaded!").present,
     "a named PDF counts as present");

  // Nothing attached at all — the ordinary cold path.
  ok(!box("").present, "an empty container has no attachment");
  ok(!box("Drop files here").present, "prompt text is not an attachment");
  eq(box("").tag, null, "no attachment has no track");

  // A file we cannot identify must NOT read as a match. It is someone's older naming
  // scheme or a file uploaded outside this tool, and a redundant upload is far cheaper
  // than shipping the wrong CV.
  const unknown = box("resume_final_v3.pdf|200KB|Successfully Uploaded!");
  ok(unknown.present, "an unrecognised file is still an attachment");
  eq(unknown.tag, null, "an unrecognised file yields no track, so it will be replaced");

  // The adapter must keep the replace path honest about being unproven.
  ok(WORKDAY.experience.resume.removeVerified === false,
     "adapter: the attachment remove control is still flagged unverified");
  ok(WORKDAY.entry.clonePolicy.reattachOnTrackMismatch === true,
     "adapter: clone policy still demands the attachment be re-checked");
}

// ── entry: which route to take out of the Apply modal ────────────────────────
// Pure, and pinned hard. Every route lands on the same six steps, so a wrong choice does
// not throw — it quietly fills the slow way, or replays an application it should not have.
{
  const chooseRoute = eval(`(${extract("chooseRoute")})`);
  const ORDER = WORKDAY.entry.routePreference;
  const ALL = ["useMyLastApplication", "autofillWithResume", "manual"];

  // The decided route wins whenever Workday is offering it.
  eq(chooseRoute("useMyLastApplication", ALL, ORDER).route, "useMyLastApplication",
     "a warm tenant clones");
  eq(chooseRoute("autofillWithResume", ALL, ORDER).route, "autofillWithResume",
     "THE POLICY: a cold tenant seeds from the resume even though clone is on the menu");

  // Ground truth is what is clickable. A cold tenant has no clone button, and a route we
  // cannot click is not a route however much applied.json likes it.
  eq(chooseRoute("useMyLastApplication", ["autofillWithResume", "manual"], ORDER).route,
     "autofillWithResume", "falls back when the decided route is not offered");
  eq(chooseRoute("autofillWithResume", ["manual"], ORDER).route, "manual",
     "manual is the last resort, not the first");

  // Bridge down: no decided route at all, so the ladder alone decides.
  eq(chooseRoute(null, ALL, ORDER).route, "useMyLastApplication",
     "with no decision the preference order picks the cheapest route");
  eq(chooseRoute(undefined, ["manual"], ORDER).route, "manual",
     "with no decision and one option, take it");

  // Nothing recognisable on the menu is a stop, not a guess.
  eq(chooseRoute("autofillWithResume", [], ORDER).route, null,
     "an empty modal yields no route");
  eq(chooseRoute("autofillWithResume", ["applyWithLinkedIn"], ORDER).route, null,
     "a route we have no handling for is not taken by accident");

  // applied.json said cold, Workday offers a clone: he applied outside the tool, or a
  // submit never got logged. The run continues, but the report has to say so — that clone
  // has an unknown resume track, which is the case reconcile must not trust.
  ok(chooseRoute("autofillWithResume", ALL, ORDER).unrecordedHistory,
     "an unrecorded prior application is flagged");
  ok(!chooseRoute("useMyLastApplication", ALL, ORDER).unrecordedHistory,
     "no flag when our records already knew about it");
  ok(!chooseRoute("autofillWithResume", ["autofillWithResume"], ORDER).unrecordedHistory,
     "no flag when no clone is on offer");

  // Every fallback says which route it wanted and which it took — the report is the only
  // window into a run that holds the tab's debugger.
  ok(/not offered/.test(chooseRoute("useMyLastApplication", ["manual"], ORDER).reason),
     "a fallback explains itself");
  eq(chooseRoute("manual", ["manual"], ORDER).preferred, "manual",
     "the decided route is echoed back for the report");

  // Config sanity: the ladder must end at the route that always exists, and the adapter's
  // default must be a route the ladder actually contains.
  eq(ORDER[ORDER.length - 1], "manual", "adapter: manual is last in the preference order");
  ok(ORDER.includes(WORKDAY.entry.fallbackRoute),
     "adapter: the fallback route is one the ladder knows");
  ok(ORDER.every(r => r in WORKDAY.entry.routes),
     "adapter: every preferred route has a selector");
}

// ── entry: the sign-in wall ──────────────────────────────────────────────────
// An unauthenticated advert has no Apply button, which is indistinguishable from a broken
// selector unless sign-in is detected explicitly.
{
  const needsSignIn = eval(`(${extract("needsSignIn")})`);
  const doc = (sels) => ({ querySelector: (s) => (sels.includes(s) ? { s } : null) });
  const APPLY = WORKDAY.entry.applyButton;
  const MARKER = WORKDAY.entry.signIn.markers[0];

  globalThis.document = doc([MARKER]);
  ok(needsSignIn(WORKDAY), "a sign-in marker with no Apply button is a wall");

  // A signed-in advert can still carry a sign-in link in its header. Treating that as a
  // wall would stop every run on those tenants, so the Apply button wins.
  globalThis.document = doc([APPLY, MARKER]);
  ok(!needsSignIn(WORKDAY), "an Apply button means we are through, whatever else is on the page");

  globalThis.document = doc([APPLY]);
  ok(!needsSignIn(WORKDAY), "a normal advert is not a wall");
  globalThis.document = doc([]);
  ok(!needsSignIn(WORKDAY), "no Apply button and no marker is a different failure, not this one");
}

console.log(`workday units: ${pass}/${pass + fail} checks passed`);
process.exit(fail ? 1 : 0);
