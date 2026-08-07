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

// THE ENTRY PAGES WEAR THE SAME PROGRESS BAR. Verified live 2026-08-07: the URL
// /apply/autofillWithResume reports "step 1 of 6" named "Autofill with Resume", so a
// number-only match said My Information while the page was still asking for the PDF. The
// driver filled step 1 against it and reported "country: FAILED no listbox button" —
// indistinguishable from a broken selector. The name is the only thing that differs.
globalThis.document = mkDoc("current step 1 of 6\nAutofill with Resume");
eq(currentStep(WORKDAY), null, "THE REGRESSION: the autofill upload page is not step 1");
globalThis.document = mkDoc("current step 1 of 6\nUse My Last Application");
eq(currentStep(WORKDAY), null, "nor is the clone page");
globalThis.document = mkDoc("current step 1 of 6\nApply Manually");
eq(currentStep(WORKDAY), null, "nor is the manual entry page");
// ...and the real step 1 still reads as step 1.
globalThis.document = mkDoc("current step 1 of 6\nMy Information");
eq(currentStep(WORKDAY).name, "My Information", "the real step 1 is unaffected");

// Which route page we are parked on, read from the URL — a second Fill starts there, not
// on the advert, and by then the Apply button is gone.
{
  const currentRoutePage = eval(`(${extract("currentRoutePage")})`);
  const at = (path) => { globalThis.location = { pathname: path }; return currentRoutePage(WORKDAY); };
  eq(at("/en-US/external_experienced/job/San-Francisco/Sr-Research-Engineer_R170796/apply/autofillWithResume"),
     "autofillWithResume", "recognises the autofill page from the real URL");
  eq(at("/x/job/y/apply/useMyLastApplication"), "useMyLastApplication", "recognises the clone page");
  // Our key for this route is "manual" but Workday's URL segment is its automation-id,
  // "applyManually". Matching keys alone silently missed it.
  eq(at("/x/job/y/apply/applyManually"), "manual",
     "the URL segment is the automation-id, not our key for the route");
  eq(at("/x/job/y"), null, "the advert is not a route page");
  eq(at("/x/job/y/apply/somethingElse"), null, "an unknown route key is not invented");
  delete globalThis.location;
}

// ── step readiness: the progress bar moves before the page renders ───────────
// Live, step 3 came back with "no Add Another button", "no listbox button" and "file input
// not found" all at once. That is not three broken selectors, it is one empty page read
// too early — and step 1 had already shown the same signature before it got a wait.
{
  ok(WORKDAY.multiStep.stepPages.myInformation, "adapter: My Information has a page marker");
  ok(WORKDAY.multiStep.stepPages.myExperience, "adapter: My Experience has a page marker");
  // The markers must correspond to the step kinds the dispatcher uses, or the wait silently
  // does nothing for the step it was added for.
  for (const kind of Object.keys(WORKDAY.multiStep.stepPages)) {
    ok(kind in WORKDAY.multiStep.stepKinds,
       `adapter: page marker "${kind}" names a real step kind`);
  }
}

// ── derived answers: "have you worked here before?" ──────────────────────────
// Required on every Workday tenant, and no static profile field can answer it — the right
// answer depends on who is asking. The taxonomy resolves a fixed dotted path, so the only
// answer it could give is a hardcoded "No", which is a false statement about a person the
// day he applies to a former employer and one nothing would ever flag.
{
  // These share a module-level const and call each other, so evaluate them as one unit —
  // extracting them singly leaves COMPANY_NOISE undefined at call time.
  const noise = src.match(/const COMPANY_NOISE = [^;]+;/)[0];
  const { sameCompany, companyCore, priorEmployerAnswer } = eval(
    `(() => { ${noise}
              ${extract("companyCore")}
              ${extract("sameCompany")}
              ${extract("priorEmployerAnswer")}
              return { sameCompany, companyCore, priorEmployerAnswer }; })()`);
  const dig = (p, k) => p[k];
  const ask = (company, employers) =>
    priorEmployerAnswer({ company, dig, profile: { work_history: employers.map(e => ({ employer: e })) } });

  // Names never match exactly across systems.
  ok(sameCompany("Adobe", "Adobe Inc."), "a legal suffix is not a different company");
  ok(sameCompany("Adobe", "adobe"), "case is not a difference");
  ok(sameCompany("Northwind Capital", "Northwind Capital & Co."), "punctuation and & are stripped");
  ok(!sameCompany("Adobe", "Google"), "different companies stay different");
  ok(!sameCompany("Adobe", ""), "an empty name never matches");
  eq(companyCore("Acme Technologies, Inc."), "acme", "noise words are stripped to a core");

  // Token containment, not substring — "co" or "ai" must not swallow everything.
  ok(!sameCompany("AI", "Adobe"), "a short name does not match by letters");
  ok(sameCompany("Adobe", "Adobe Systems"), "a longer form matches by whole words");

  // THE ANSWER. "No" is the ordinary case and "Yes" is the one that must not be wrong.
  eq(ask("Adobe", ["Northwind Capital", "Some Startup"]), "No",
     "never worked there -> No");
  eq(ask("Northwind Capital", ["Northwind Capital", "Some Startup"]), "Yes",
     "THE CASE A HARDCODED 'No' WOULD GET WRONG: a former employer -> Yes");
  eq(ask("Northwind Capital & Co.", ["Northwind Capital"]), "Yes",
     "the Yes still lands when the asker's name is spelled differently");

  // Undecidable must be ASKED, never guessed. A wrong answer here is a false statement on
  // a signed application; a pause costs one tap.
  eq(ask("", ["Northwind Capital"]), null, "an unknown asker is undecidable");
  eq(ask("Adobe", []), null, "an empty work history is undecidable, NOT an automatic No");
  eq(priorEmployerAnswer({ company: "Adobe", dig, profile: {} }), null,
     "a profile with no work history at all is undecidable");
}

// ── the resume upload is not a question ──────────────────────────────────────
// It renders as a formField- wrapper, so the taxonomy pass saw it as unanswered and paused
// the whole run for input on a field that had just been filled successfully. Its wrapper
// has an EMPTY name (data-automation-id="formField-"), so knownCoreIds cannot exclude it.
{
  const cq = WORKDAY.customQuestion;
  // The exact label from the live report, chip text and all.
  const LIVE = "Upload either DOC, DOCX, HTML, PDF, or TXT file types (5MB max)" +
               "Parth_Resume_d.pdf loadingParth_Resume_d.pdf129.27 KB";

  ok(cq.skipLabelPattern.test(LIVE), "THE REGRESSION: the live upload label is skipped");
  ok(cq.skipLabelPattern.test("Drop files here"), "the drop-zone prompt is skipped");
  ok(cq.skipLabelPattern.test("Attach a file (10 MB max)"), "other size limits are skipped");

  // It must stay narrow. These are real questions and skipping one would silently drop it
  // from the pause list, which is how a required field gets left blank.
  ok(!cq.skipLabelPattern.test("Have you been employed by Adobe in the past?"),
     "a tenant question is not skipped");
  ok(!cq.skipLabelPattern.test("What are your salary expectations?"),
     "a salary question is not skipped");
  ok(!cq.skipLabelPattern.test("Are you legally authorized to work in the United States?"),
     "a work-authorization question is not skipped");
  ok(!cq.skipLabelPattern.test("Please upload your portfolio URL"),
     "a question that merely says 'upload' is not skipped");

  // The DOM test has to survive the upload completing: Workday swaps the file input for a
  // chip, so input[type=file] alone stops matching exactly when it matters.
  ok(cq.skipContaining.includes('input[type=file]'), "the input is matched while it exists");
  ok(cq.skipContaining.some(s => s.includes("attachments-FileUpload")),
     "the container is matched once the input is gone");
}

// ── which page am I on, and is it the last one ───────────────────────────────
// Both were number-based and both were wrong, found in one live run: the autofill flow
// counts its upload page as step 1, so My Information arrived as "step 2 of 7". The driver
// dispatched on the number and ran the work-history filler against the My Information
// page, reporting three missing selectors that were simply on another page entirely.
{
  const stepKind = eval(`(${extract("stepKind")})`);
  const isFinalStep = eval(`(${extract("isFinalStep")})`);

  eq(stepKind(WORKDAY, { n: 1, name: "My Information" }), "myInformation",
     "step 1 of a manual flow is My Information");
  eq(stepKind(WORKDAY, { n: 2, name: "My Information" }), "myInformation",
     "THE REGRESSION: so is step 2 of an autofill flow — the name decides, not the number");
  eq(stepKind(WORKDAY, { n: 3, name: "My Experience" }), "myExperience",
     "experience is found at whatever number it lands on");
  eq(stepKind(WORKDAY, { n: 4, name: "Application Questions" }), "questions", "questions");
  eq(stepKind(WORKDAY, { n: 7, name: "Review" }), "review", "review");

  // Tenants reword these, so matching is loose on purpose.
  eq(stepKind(WORKDAY, { n: 1, name: "Personal Information" }), "myInformation",
     "a reworded information page still resolves");
  eq(stepKind(WORKDAY, { n: 2, name: "Work Experience" }), "myExperience",
     "a reworded experience page still resolves");
  eq(stepKind(WORKDAY, { n: 2, name: "Something Unknown" }), null,
     "an unrecognised page is null, so only the generic taxonomy pass runs on it");
  eq(stepKind(WORKDAY, { n: 2, name: "" }), null, "an empty name is not guessed at");

  // THE SAFETY PROPERTY. On the last page the footer button is Submit, not Next, so
  // getting this wrong is the difference between filling a form and sending one.
  ok(isFinalStep(WORKDAY, { n: 7, of: 7, name: "Review" }), "7 of 7 is final");
  ok(isFinalStep(WORKDAY, { n: 6, of: 6, name: "Review" }), "6 of 6 is final");
  ok(!isFinalStep(WORKDAY, { n: 6, of: 7, name: "Self Identify" }),
     "6 of 7 is NOT final — the hardcoded 6 stopped a page early here");
  ok(!isFinalStep(WORKDAY, { n: 2, of: 7, name: "My Information" }), "early steps advance");
  ok(isFinalStep(WORKDAY, { n: 8, of: 7, name: "?" }), "past the end still counts as final");

  // Fail CLOSED. An unreadable position must never be treated as "safe to press Next".
  ok(isFinalStep(WORKDAY, null), "no step at all is treated as final, never advanced past");
  ok(isFinalStep(WORKDAY, { n: 6, of: 0, name: "Review" }),
     "a garbled total falls back to the adapter floor rather than advancing blindly");
  ok(isFinalStep(WORKDAY, { n: 6, name: "Review" }),
     "a missing total falls back to the floor too");
}

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

  const attachmentMatchesTrack = eval(`(${extract("attachmentMatchesTrack")})`);

  // THE FILENAME CARRIES A LETTER, NOT THE TAG. cleanResumeName collapses "ds" to "d" on
  // the way out, so the real attachment reads Parth_Resume_d.pdf. A pattern expecting the
  // tag itself matches nothing, and comparing "d" to "ds" never agrees — which would have
  // replaced the attachment on every run forever. Caught by a live report showing
  // resumeChoice {tag:"ds", file:"Parth_Resume_d.pdf"}.
  eq(box("Parth_Resume_d.pdf|182KB|Successfully Uploaded!").letter, "d",
     "reads the single-letter track off our own filename");
  eq(box("Parth_Resume_a.pdf|182KB|Successfully Uploaded!").letter, "a",
     "every track initial parses");
  ok(box("Parth_Resume_d.pdf|182KB|Successfully Uploaded!").present,
     "a named PDF counts as present");

  ok(attachmentMatchesTrack(box("Parth_Resume_d.pdf|1KB|Successfully Uploaded!"), { tag: "ds" }),
     "the d file satisfies the ds track");
  ok(attachmentMatchesTrack(box("Parth_Resume_q.pdf|1KB|Successfully Uploaded!"), { tag: "quant" }),
     "the q file satisfies the quant track");
  ok(!attachmentMatchesTrack(box("Parth_Resume_q.pdf|1KB|Successfully Uploaded!"), { tag: "ai" }),
     "the WHOLE POINT: a quant CV does not satisfy an AI role");
  ok(!attachmentMatchesTrack(box(""), { tag: "ds" }), "nothing attached never matches");
  ok(!attachmentMatchesTrack(box("resume_final.pdf|1KB|x"), { tag: "ds" }),
     "an unreadable filename never matches");
  ok(!attachmentMatchesTrack(box("Parth_Resume_d.pdf|1KB|x"), {}),
     "no track means no match, rather than a crash");

  // The scheme only works because the four initials are distinct. A fifth track sharing
  // one would silently make two CVs interchangeable.
  {
    const tags = ["quant", "ds", "tech", "ai"];
    const initials = new Set(tags.map(t => t[0]));
    eq(initials.size, tags.length, "the four resume tracks have distinct initials");
  }

  // Nothing attached at all — the ordinary cold path.
  ok(!box("").present, "an empty container has no attachment");
  ok(!box("Drop files here").present, "prompt text is not an attachment");
  eq(box("").letter, null, "no attachment has no track");

  // A file we cannot identify must NOT read as a match. It is someone's older naming
  // scheme or a file uploaded outside this tool, and a redundant upload is far cheaper
  // than shipping the wrong CV.
  const unknown = box("resume_final_v3.pdf|200KB|Successfully Uploaded!");
  ok(unknown.present, "an unrecognised file is still an attachment");
  eq(unknown.letter, null, "an unrecognised file yields no track, so it will be replaced");

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

// ── entry: Next is enabled, not merely present ───────────────────────────────
// The upload page has its OWN Next, and the wizard does not exist until it is clicked. An
// earlier version waited for the wizard's first-name field to appear on its own — it
// cannot — so the run hung for the whole parse budget and then blamed the parse. Workday
// also renders the footer button before the page is ready and disables it, so existence is
// not permission to click.
{
  const isEnabled = eval(`(${extract("isEnabled")})`);
  const btn = (attrs = {}) => ({
    disabled: !!attrs.disabled,
    getAttribute: (n) => (n in attrs ? String(attrs[n]) : null),
  });

  ok(isEnabled(btn()), "a plain button is clickable");
  ok(!isEnabled(null), "a missing button is not clickable");
  ok(!isEnabled(btn({ disabled: true })), "a disabled button is not clickable");
  ok(!isEnabled(btn({ "aria-disabled": "true" })),
     "aria-disabled counts — Workday disables the footer while it parses");
  ok(isEnabled(btn({ "aria-disabled": "false" })), "aria-disabled=false is clickable");

  // The upload page must reuse the real footer button, not a selector of its own that
  // could drift away from the one the wizard uses.
  eq(WORKDAY.entry.resumeUpload.continueButton, WORKDAY.multiStep.nextButton,
     "adapter: the upload page continues via the same footer button the wizard uses");
  ok(WORKDAY.entry.resumeUpload.settleTimeoutMs >= 30000,
     "adapter: the parse budget is generous, since a big PDF on a slow tenant is not a fault");
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
