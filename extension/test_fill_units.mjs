#!/usr/bin/env node
/**
 * Unit tests for the two fill.js functions the batch flow's correctness rests on.
 *
 *   askId        phase 1 collects a question, phase 3 must land Parth's answer on THAT
 *                same field — across a re-fill where the field set can shift.
 *   diffApproved the last gate before a real submit: what he approved vs what's on the
 *                page now.
 *
 * fill.js is a classic content script with no exports, so pull the real function source
 * out of it and evaluate that — a copy-pasted duplicate would only test the copy.
 *
 * Run: node extension/test_fill_units.mjs
 */
import { readFileSync } from "fs";

const src = readFileSync(new URL("./fill.js", import.meta.url), "utf8");

function extract(name) {
  const i = src.indexOf(`function ${name}(`);
  if (i < 0) throw new Error(`${name}() not found in fill.js — did it get renamed?`);
  let depth = 0, j = src.indexOf("{", i);
  for (; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}" && --depth === 0) break;
  }
  return src.slice(i, j + 1);
}

// diffApproved's equivalence tiers lean on the module-level norm/canon/wb consts (the
// same ones matchOption commits with — that's the point). They sit contiguously in
// fill.js, so lift the whole block: from `const norm` through the end of `const wb`.
function extractConsts() {
  const start = src.indexOf("const norm = ");
  const wbAt = src.indexOf("const wb = ", start);
  if (start < 0 || wbAt < 0) throw new Error("norm/canon/wb consts not found in fill.js");
  const end = src.indexOf("\n", wbAt);
  return src.slice(start, end);
}

// A `const name = ...;` declaration (the arrow-function helpers). Reads to the terminating
// semicolon, not the first newline: `searchable` is a multi-line chain, and taking only its
// first line silently yields a DIFFERENT function that quietly passes tests.
function extractConst(name) {
  const i = src.indexOf(`const ${name} = `);
  if (i < 0) throw new Error(`const ${name} not found in fill.js — did it get renamed?`);
  for (let j = src.indexOf("\n", i); j > 0; j = src.indexOf("\n", j + 1)) {
    if (src.slice(i, j).trimEnd().endsWith(";")) return src.slice(i, j);
  }
  throw new Error(`const ${name} has no terminating semicolon`);
}

// liveControl walks the DOM, so it gets a tiny fake `document`/`labelFor` injected rather
// than a real one — enough to pin the matching ORDER and the detached-node rule, which is
// the part that broke (a detached control clicks at 0,0 and fails as "no options").
const { askId, diffApproved, searchCandidates, otherOption, OTHER_OK_FIELD, makeLiveControl,
        isPickList, makeDegreeLevel, makePageCompany, canon, wb, sameAnswer,
        makeLiveField, makeCurrentValue, FOLLOW_UP, isNegative, splitMonthYear, eduDateField,
        answerCandidates, matchFirstCandidate, matchOption } =
  new Function(
  `${extract("matchOption")}\n${extract("answerCandidates")}\n${extract("matchFirstCandidate")}\n` +
  `${extract("onlyDigits")}\n${extract("phoneish")}\n${extract("phoneEq")}\n${extract("sameAnswer")}\n` +
  `${extractConst("FOLLOW_UP")}\n${extractConst("isNegative")}\n` +
  `${extractConst("EDU_DATE_ID")}\n${extractConst("MONTH_NAMES")}\n` +
  `${extract("splitMonthYear")}\n${extract("eduDateField")}\n` +
  `${extractConsts()}\n${extract("askId")}\n${extract("diffApproved")}\n` +
  `${extractConst("searchable")}\n${extractConst("GENERIC_SEARCH_WORD")}\n${extract("searchCandidates")}\n` +
  `${extractConst("OTHER_OK_FIELD")}\n${extractConst("otherOption")}\n` +
  `${extract("isPickList")}\n` +
  `${extractConst("DEGREE_FIELD")}\n${extractConst("DEGREE_LEVELS")}\n` +
  `const makeDegreeLevel = (labelFor) => { ${extract("degreeLevel")} return degreeLevel; };\n` +
  `const makeLiveControl = (document, labelFor) => { ${extract("liveControl")} return liveControl; };\n` +
  `const makeLiveField = (document, labelFor) => { ${extract("liveField")} return liveField; };\n` +
  `const makeCurrentValue = (cleanText, optionText) => { ${extract("currentValue")} return currentValue; };\n` +
  `const makePageCompany = (location, document) => { ${extract("pageCompany")} return pageCompany; };\n` +
  `return { askId, diffApproved, searchCandidates, otherOption, OTHER_OK_FIELD, makeLiveControl,` +
  ` isPickList, makeDegreeLevel, makePageCompany, canon, wb, sameAnswer,` +
  ` makeLiveField, makeCurrentValue, FOLLOW_UP, isNegative, splitMonthYear, eduDateField,` +
  ` answerCandidates, matchFirstCandidate, matchOption };`)();

let n = 0;
const ok = (cond, msg) => { if (!cond) { console.error("FAIL:", msg); process.exit(1); } n++; };

// --- askId -------------------------------------------------------------------------

// THE property: phase 1 and phase 3 are separate runs, so the same question must get
// the same id both times or an answer lands on the wrong control.
ok(askId("Why Ramp?", "text", new Set()) === askId("Why Ramp?", "text", new Set()),
   "same label+kind must yield the same id across runs");

ok(askId("Why Ramp?", "text", new Set()) !== askId("Why Notion?", "text", new Set()),
   "different labels must not collide");

// One label really can cover two DIFFERENT controls (Ashby's compound PhoneField: a tel
// input and a texting-consent radio group both resolve to "Phone"). Different kinds must
// get different ids OUTRIGHT — not order-dependent _2 suffixes, which shifted answers
// between phases whenever the escalated set differed.
ok(askId("Phone", "core", new Set()) !== askId("Phone", "group", new Set()),
   "same label, different control kinds must not collide");

// Label text can come back with different whitespace/case between renders.
ok(askId("Why  Ramp?", "text", new Set()) === askId("why ramp?", "text", new Set()),
   "case and whitespace must normalize away");
ok(askId(" Why Ramp? ", "text", new Set()) === askId("Why Ramp?", "text", new Set()),
   "surrounding whitespace must normalize away");

// A form really can ask the same thing twice; ids must stay distinct within one run.
{
  const used = new Set();
  const a = askId("Pronouns", "group", used), b = askId("Pronouns", "group", used);
  ok(a !== b, "duplicate label+kind within one form must get distinct ids");
  ok(b.startsWith(a), "the disambiguated id should extend the base, not replace it");
}

// Telegram caps callback_data at 64 bytes: "b:a:<16 hex>:<id>:blank"
{
  const id = askId("An extremely long question label that a real ATS might well use "
                   + "to ask about your motivations at considerable length", "text", new Set());
  ok(id.length <= 12, `id must stay short for callback_data, got ${id.length}`);
  ok(`b:a:0123456789abcdef:${id}:blank`.length < 64, "callback_data must fit in 64 bytes");
}

ok(typeof askId("", "", new Set()) === "string", "an empty label must not throw");
ok(typeof askId("X", undefined, new Set()) === "string", "a missing kind must not throw");

// --- diffApproved ------------------------------------------------------------------

const approved = [
  { label: "Email", value: "jordan.lee@example.com" },
  { label: "Why Ramp?", value: "Because I like Ramp." },
];

ok(diffApproved(approved, [...approved]).length === 0,
   "an identical fill must produce no mismatch");

ok(diffApproved(approved, [
  { label: "Email", value: "someone.else@example.com" },
  { label: "Why Ramp?", value: "Because I like Ramp." },
]).length === 1, "a changed value must be flagged");

// The Ashby hydration race: a field he approved silently isn't there this run.
{
  const d = diffApproved(approved, [{ label: "Email", value: "jordan.lee@example.com" }]);
  ok(d.length === 1 && /missing now/.test(d[0]), "a dropped field must be flagged");
}

// Cosmetic re-rendering must not block a good submit.
ok(diffApproved(approved, [
  { label: "email", value: "  jordan.lee@example.com " },
  { label: "Why  Ramp?", value: "Because I like Ramp." },
]).length === 0, "whitespace/case drift must not false-positive");

// A TYPED answer lands on the OPTION matchOption maps it to — the committed text is not
// the typed text, and the gate must accept the matcher's own equivalence tiers (raw
// string compare held EVERY typed answer at phase 3).
//
// Driven off a table so every rewrite rule in canon() is covered rather than the two that
// happened to break first. Both directions of each pair are listed: canon() is applied to
// the answer AND the options, so which side the profile wording lands on is arbitrary, and
// a rule that only collapses one way would pass a single-direction test.
const CANON_PAIRS = [
  // gender wording, both encodings and both directions
  ["Gender", "Male", "Man"], ["Gender", "Man", "Male"],
  ["Gender", "Male", "Men"], ["Gender", "Men", "Male"],
  ["Gender", "Female", "Woman"], ["Gender", "Woman", "Female"],
  ["Gender", "Female", "Women"], ["Gender", "Women", "Female"],
  // sexual-orientation wording: a profile may say either, Greenhouse offers either
  ["Sexual orientation", "Straight", "Heterosexual"],
  ["Sexual orientation", "Heterosexual", "Straight"],
  ["Sexual orientation", "Heterosexual", "Heterosexual / Straight"],
  // the No/None/N-A equivalence class
  ["Sponsorship", "No", "None"], ["Sponsorship", "No", "N/A"],
  ["Sponsorship", "No", "Not applicable"], ["Sponsorship", "None", "No"],
  // consent-checkbox wording: a deterministic Yes must satisfy an "I <verb>" option
  ["Consent", "Yes", "I agree"], ["Consent", "Yes", "I accept"],
  ["Consent", "Yes", "I acknowledge"], ["Consent", "Yes", "I consent"],
  // whole-word tier: committed as a longer option that contains the answer
  ["Consent", "Yes", "Yes - I consent to receiving text messages"],
];
for (const [label, typed, committed] of CANON_PAIRS) {
  ok(diffApproved([{ label, value: typed }],
                  [{ label, value: committed }]).length === 0,
     `typed '${typed}' committed as '${committed}' must not be drift (${label})`);
}

// ...but the equivalence must stay as strict as the matcher: a genuinely different value
// is still drift. "male" is a substring of "female", so a naive contains-check passes the
// first of these — that is the bug this row exists to catch.
const CANON_DRIFT = [
  ["Gender", "Male", "Female"], ["Gender", "Man", "Woman"],
  ["Gender", "Female", "Male"], ["Gender", "Women", "Men"],
  ["Sexual orientation", "Straight", "Bisexual"],
  ["Sponsorship", "Yes", "No"],
];
for (const [label, typed, committed] of CANON_DRIFT) {
  ok(diffApproved([{ label, value: typed }],
                  [{ label, value: committed }]).length === 1,
     `'${typed}' vs '${committed}' must still be drift (${label})`);
}

// He approved a blank -> nothing was promised, so nothing to enforce.
ok(diffApproved([{ label: "Portfolio", value: "" }], []).length === 0,
   "an approved-blank field must not be flagged when absent");

// A field that showed up this run but wasn't in his summary is the ATS's problem.
ok(diffApproved(approved, [...approved, { label: "Extra", value: "x" }]).length === 0,
   "an unapproved extra field must not block the submit");

ok(diffApproved([], []).length === 0 && diffApproved(null, []).length === 0,
   "no approved summary must not throw");

// The message has to name the field and both values, or it's unactionable on a phone.
{
  const [msg] = diffApproved(approved, [
    { label: "Email", value: "wrong@x.com" }, approved[1]]);
  ok(/Email/.test(msg) && /wrong@x\.com/.test(msg) && /jordan.lee/.test(msg),
     `mismatch message must name the field and both values, got: ${msg}`);
}

// REGRESSION seen on a live Ashby board. Ashby's compound PhoneField is a tel
// input plus a texting-consent radio group; fieldLabel resolves BOTH to "Phone", so the
// approved list carries two values under that one label. Keying a Map by label let the
// consent answer clobber the number and blocked a perfectly good submit.
{
  const approvedPhone = [
    { label: "Full Name", value: "Parth Thakkar" },
    { label: "Phone", value: "+1 555-012-3456" },                        // from `filled`
    { label: "Phone", value: "Yes - I consent to receiving text messages" }, // his answer
  ];
  ok(diffApproved(approvedPhone, [
    { label: "Full Name", value: "Parth Thakkar" },
    { label: "Phone", value: "+1 555-012-3456" },
    { label: "Phone", value: "Yes - I consent to receiving text messages" },
  ]).length === 0, "a label covering two controls must not self-mismatch");

  // ...but a genuinely wrong phone under that same shared label must STILL be caught.
  const d = diffApproved(approvedPhone, [
    { label: "Full Name", value: "Parth Thakkar" },
    { label: "Phone", value: "+1 555-000-0000" },
    { label: "Phone", value: "Yes - I consent to receiving text messages" },
  ]);
  ok(d.length === 1 && /555-012-3456/.test(d[0]),
     `a real drift under a shared label must still be caught, got: ${JSON.stringify(d)}`);
}

// Ashby labels carry non-breaking spaces ("Preferred  Name") — cosmetic, must not trip.
ok(diffApproved([{ label: "Preferred  Name", value: "Parth" }],
                [{ label: "Preferred Name", value: "Parth" }]).length === 0,
   "a non-breaking space in a label must normalize away");

// A required-marker (*) rides on the approved label (raw question text) but reviewValueSummary
// strips it on read-back — the two must key the same or a filled field reads as "missing now".
// From a real Greenhouse failure: both filled correctly, both wrongly held.
ok(diffApproved([{ label: "How did you first learn about Affirm as an employer? *", value: "LinkedIn" },
                 { label: "Have you previously been employed at Affirm for any length of time?*", value: "I have not previously been employed at Affirm" }],
                [{ label: "How did you first learn about Affirm as an employer?", value: "LinkedIn" },
                 { label: "Have you previously been employed at Affirm for any length of time?", value: "I have not previously been employed at Affirm" }]).length === 0,
   "a required-marker asterisk on the approved label must not read as missing");

// A site can reformat a phone on load ("(555) 012-3456" -> "555-012-3456"): same number, must
// not trip the guard...
ok(diffApproved([{ label: "Phone", value: "(555) 012-3456" }],
                [{ label: "Phone", value: "555-012-3456" }]).length === 0,
   "a reformatted phone (same digits) must not mismatch");
// ...but a genuinely different number must still be caught.
ok(diffApproved([{ label: "Phone", value: "(555) 012-3456" }],
                [{ label: "Phone", value: "(555) 012-9999" }]).length === 1,
   "a different phone number must still be caught");

// --- searchCandidates ---------------------------------------------------------------
// THE case this exists for: Greenhouse's school search returns nothing for the full formal
// name, so the ladder has to reach the distinctive word or the required field fails on
// every board forever.
const school = searchCandidates("Ashford Institute of Technology and Science, Norhaven");
ok(school[0] === "Ashford Institute of Technology and Science, Norhaven",
   "the full value must still be tried first — a list that HAS it should match exactly");
ok(school.includes("Norhaven") && school.includes("Ashford"),
   `the distinctive words must be in the ladder (got ${JSON.stringify(school)})`);
ok(!school.some(q => /^(institute|technology|science)$/i.test(q)),
   "generic institution words must never be a query on their own — they narrow nothing");
ok(school.indexOf("Ashford Institute of Technology and Science") < school.indexOf("Norhaven"),
   "queries must run most-specific first, so an exact list still wins before a broad one");

// A places autocomplete keeps its old behaviour: the first two comma-segments lead.
ok(searchCandidates("Springfield, Illinois, United States")[0] === "Springfield, Illinois",
   "a location must still lead with the first two comma-segments");

// Parentheticals are stripped before typing (the original "(AITS)" bug) and nothing is empty.
ok(!searchCandidates("Ashford Institute of Technology and Science (AITS), Norhaven").some(q => q.includes("(")),
   "no query may carry a parenthetical — the server-side search chokes on it");
ok(searchCandidates("").length === 0, "an empty value yields no queries rather than typing junk");
ok(new Set(searchCandidates("MIT")).size === searchCandidates("MIT").length,
   "a one-word value must not produce duplicate queries");

// --- liveField / currentValue: the cure for the green tick over an empty field --------
// THE BUG: runFill captures a node per unresolved question, then waits — for a panel tap
// or a phase-3 replay. Greenhouse re-keys its Education rows once the resume parses and
// Ashby re-inits after hydration, so the captured node is detached by then. Writing to a
// detached input SUCCEEDS silently, so the answer was reported filled while the visible
// field stayed empty. liveField re-points; currentValue proves the write landed.
{
  const el = (o = {}) => ({ tagName: "INPUT", type: "text", value: "", isConnected: true,
                            id: "", name: "", closest: () => null, ...o });
  const mkDoc = (nodes) => ({
    getElementById: (id) => nodes.find(n => n.id === id && n.isConnected) || null,
    getElementsByName: (nm) => nodes.filter(n => n.name === nm && n.isConnected),
    querySelectorAll: () => nodes.filter(n => n.isConnected),
  });
  const labelOfNode = (n) => (n && n._label) || "";

  // Still on the page: hand back the very same node, no searching.
  {
    const a = el({ id: "school--0" });
    const live = makeLiveField(mkDoc([a]), labelOfNode);
    ok(live(a, {}) === a, "a still-connected field is returned as-is");
  }
  // THE GREENHOUSE CASE: the node was replaced but kept its id.
  {
    const stale = el({ id: "phone", isConnected: false });
    const fresh = el({ id: "phone" });
    const live = makeLiveField(mkDoc([stale, fresh]), labelOfNode);
    ok(live(stale, {}) === fresh, "a detached field is re-found by id");
  }
  // A radio must resolve to ITSELF, not whichever sibling shares its name.
  {
    const stale = el({ type: "radio", name: "consent", value: "No", isConnected: false });
    const yes = el({ type: "radio", name: "consent", value: "Yes" });
    const no = el({ type: "radio", name: "consent", value: "No" });
    const live = makeLiveField(mkDoc([stale, yes, no]), labelOfNode);
    ok(live(stale, {}) === no, "name lookup picks the option with the SAME value, not a sibling");
  }
  // Ambiguous is worse than lost: several same-name options and none matching on value
  // means we cannot say WHICH one, and ticking the wrong radio is worse than reporting a
  // miss. (A single same-name candidate is unambiguous and is still returned.)
  {
    const stale = el({ type: "radio", name: "g", value: "Maybe", isConnected: false });
    const a = el({ type: "radio", name: "g", value: "Yes" });
    const b = el({ type: "radio", name: "g", value: "No" });
    ok(makeLiveField(mkDoc([a, b]), labelOfNode)(stale, {}) === null,
       "several same-name options and no value match resolves to null, never a guess");
    const lone = el({ name: "solo", value: "x" });
    ok(makeLiveField(mkDoc([lone]), labelOfNode)(el({ name: "solo", value: "y", isConnected: false }), {}) === lone,
       "a single same-name candidate is unambiguous and is returned");
  }
  // The label tier must not cross control kinds: Ashby's compound PhoneField resolves BOTH
  // a tel input and a texting-consent radio group to "Phone", and putting the number on the
  // consent radio would be a wrong answer, not a failed one.
  {
    const stale = el({ type: "tel", _label: "Phone", isConnected: false });
    const consent = el({ type: "radio", _label: "Phone", _opt: "Yes" });
    const tel = el({ type: "tel", _label: "Phone" });
    const live = makeLiveField(mkDoc([consent, tel]), labelOfNode);
    ok(live(stale, {}) === tel, "the label fallback requires a matching tag AND type");
  }
  // Nothing findable: report it. Returning the stale node would restore the silent bug.
  {
    const stale = el({ id: "gone", isConnected: false });
    const live = makeLiveField(mkDoc([]), labelOfNode);
    ok(live(stale, {}) === null, "an unfindable field returns null so the caller can say so");
  }

  const cleanTextF = (n) => String((n && n.textContent) || "").trim();
  const optionTextF = (n) => (n && n._opt) || "";
  const cur = makeCurrentValue(cleanTextF, optionTextF);

  ok(cur(el({ value: "Parth" }), {}) === "Parth", "a text input reads back its value");
  ok(cur(el({ value: "x", isConnected: false }), {}) === "",
     "a DETACHED field reads back empty — this is what turns a false tick into a failure");
  // A react-select keeps its answer in a value chip; the input still holds the search text,
  // so reading .value there would call a FAILED pick a success.
  {
    const chipless = { querySelectorAll: () => [] };
    const typed = el({ value: "Ashford", closest: () => chipless });
    ok(cur(typed, {}) === "", "a react-select with no committed chip reads empty despite typed text");
    const chipped = { querySelectorAll: () => [{ textContent: " Other " }] };
    ok(cur(el({ value: "Ashford", closest: () => chipped }), {}) === "Other",
       "a committed react-select reads back the chip, not the search box");
  }
  // A <select> still on its placeholder is empty, not filled with the word "Select...".
  {
    const sel = (idx, ...texts) => ({ tagName: "SELECT", isConnected: true, selectedIndex: idx,
                                      options: texts.map(text => ({ text })), closest: () => null });
    ok(cur(sel(0, "Select...", "Yes", "No"), {}) === "", "a placeholder selection reads empty");
    ok(cur(sel(1, "Select...", "Yes", "No"), {}) === "Yes", "a real selection reads its option text");
  }
  ok(cur(el({ type: "radio", checked: false, _opt: "Yes" }), {}) === "", "an unchecked radio reads empty");
  ok(cur(el({ type: "radio", checked: true, _opt: "Yes" }), {}) === "Yes", "a checked radio reads its option");
}

// sameAnswer is the shared judge for "did the write land?" and the submit gate. A field
// that REFORMATS what it was given still holds the answer and must not read as a failure.
ok(sameAnswer("+1 555-012-3456", "555-012-3456"), "a reformatted phone is the same answer");
ok(sameAnswer("Bachelor", "Bachelor's Degree"), "the option text the matcher committed still matches");
ok(!sameAnswer("Yes", "No"), "genuinely different answers stay different");
ok(!sameAnswer("Parth", ""), "an empty read-back never equals a real answer");

// --- Greenhouse education date rows ---------------------------------------------------
// Measured on a live Greenhouse board: ids are school/degree/discipline/start-month/
// start-year/end-month/end-year, each suffixed --N; month fields are react-selects whose
// options are FULL month names; year fields are <input type="number">. These four date
// fields were asked on 29 of 184 logged Greenhouse fills because always_ask carries
// "start date" (for "when could you start?") and is matched before everything else.
ok(splitMonthYear("September 2020").month === "September", "a full month name is read");
ok(splitMonthYear("September 2020").year === "2020", "the year is read alongside it");
ok(splitMonthYear("Sep 2020").month === "September", "an abbreviated month expands to the form's wording");
ok(splitMonthYear("July 2024").month === "July" && splitMonthYear("July 2024").year === "2024",
   "the graduation date splits the same way");
ok(splitMonthYear("2024-07").month === "July", "an ISO-ish date yields the month");
ok(splitMonthYear("07/2024").month === "July", "a slashed date yields the month");
// A year-only profile must fill the YEAR and ask for the month, never invent one.
ok(splitMonthYear("2020").year === "2020" && splitMonthYear("2020").month === "",
   "a bare year gives no month rather than guessing one");
ok(splitMonthYear("") === null && splitMonthYear(null) === null, "an empty date yields nothing");

{
  const doc = (ids) => ({ getElementById: (id) => (ids.includes(id) ? { id } : null) });
  const eduRow = doc(["school--0", "degree--0", "start-month--0", "start-year--0"]);
  ok(eduDateField({ id: "start-month--0" }, eduRow).profileKey === "education.start_date",
     "start-* rows take the education start date");
  ok(eduDateField({ id: "end-year--0" }, eduRow).profileKey === "education.graduation_date",
     "end-* rows take the graduation date");
  ok(eduDateField({ id: "start-month--0" }, eduRow).part === "month"
     && eduDateField({ id: "start-year--0" }, eduRow).part === "year",
     "the month and year halves are distinguished");
  // THE GUARD: another widget using --N ids must not be answered with a graduation date.
  // A wrong answer is worse than a failed one.
  ok(eduDateField({ id: "start-month--0" }, doc(["something-else--0"])) === null,
     "without a school/degree in the same row it is NOT the education block");
  // profile.json describes ONE education; a second row is a degree he does not have.
  ok(eduDateField({ id: "start-month--1" }, doc(["school--1", "start-month--1"])) === null,
     "a second education row is left to be asked, not filled from the first");
  ok(eduDateField({ id: "phone" }, eduRow) === null, "an ordinary field is untouched");
  ok(eduDateField({ id: "school--0" }, eduRow) === null,
     "school/degree/discipline still go through the taxonomy, not here");
}

// --- conditional follow-ups: the question that only exists if the last one said yes -----
// Live on Robinhood: the deterministic pass answered the sponsorship question
// "No", and the follow-up beneath it went to the drafter with nothing but its own label to
// go on — so the model chose what the answer above must have been and wrote a confident
// paragraph contradicting it. These pin the detector; FOLLOW_UP must be tight enough that a
// real essay opening with "If" is untouched.
for (const l of [
  'If you answered "Yes" to the above question, please provide additional information here:',
  "If yes, please explain",
  "If so, tell us more",
  "If you selected yes, describe the arrangement",
  "Please provide additional information to the above question",
  "(If yes, give details)",
]) ok(FOLLOW_UP.test(l), `must read as a follow-up: ${l.slice(0, 46)}`);

for (const l of [
  "If you could have any superpower, what would it be?",
  "What if anything would you change about our product?",
  "If you were starting this company today, what would you do differently?",
  "Why do you want to work here?",
  "Tell us about a time you disagreed with a colleague",
]) ok(!FOLLOW_UP.test(l), `must NOT read as a follow-up: ${l.slice(0, 46)}`);

// isNegative decides whether the trigger fired. canon() maps a boolean No onto "none".
ok(isNegative("No") && isNegative("None") && isNegative("N/A") && isNegative("Not applicable"),
   "the ways a form spells a refusal all read as negative");
ok(!isNegative("Yes") && !isNegative("I consent"),
   "an affirmative answer keeps the follow-up live");
// Word-bounded: a date beginning with the letters "no" is not a refusal.
ok(!isNegative("November 2026"), "a value merely starting with 'no' is not a No");
ok(!isNegative(""), "no answer found means the trigger state is unknown, not negative");

// --- the answer chain: profile value first, then alternates ---------------------------
// Every case here is a control that flagged on a live board because the FIRST answer
// matched none of its options. See taxonomy.yaml's `alternates` note.
const asOpts = (...t) => t.map(textContent => ({ textContent }));

ok(JSON.stringify(answerCandidates({ rendered: "Male", alternates: ["He/Him"] })) ===
   JSON.stringify(["Male", "He/Him"]),
   "the profile's own value is always tried first, alternates after");
ok(answerCandidates({ rendered: "", alternates: [] }).length === 0,
   "a deliberately blank value yields NO candidates — callers skip instead of escalating");
ok(answerCandidates({ rendered: "Yes", alternates: ["Yes", "Yeah"] }).length === 2,
   "an alternate identical to the primary is collapsed, not tried twice");
ok(answerCandidates({ rendered: "A", alternates: [null, "  ", "B"] }).join() === "A,B",
   "blank alternates are dropped rather than typed as empty answers");

// Robinhood: "What gender pronouns do you prefer?" classifies as gender.
const pron = matchFirstCandidate(asOpts("He / Him", "She / Her", "Them / They", "I don't wish to answer"),
                                 { rendered: "Male", alternates: ["He/Him"] });
ok(pron && pron.hit.textContent === "He / Him" && pron.viaAlternate,
   "gender falls through to pronouns on a pronoun option list, and is marked as an alternate");
// ...and the SAME rule must still answer a real gender question with the gender.
const gen = matchFirstCandidate(asOpts("Male", "Female", "Decline to self-identify"),
                                { rendered: "Male", alternates: ["He/Him"] });
ok(gen && gen.hit.textContent === "Male" && !gen.viaAlternate,
   "THE REGRESSION GUARD: a real gender list still takes the gender, never the pronoun");

// SentiLink: Ashby labels its SMS-consent group with the phone field's text.
const sms = matchFirstCandidate(asOpts("Yes - I consent to receiving text messages",
                                       "No - I do not consent to receiving text messages"),
                                { rendered: "+1 555-000-0000", alternates: ["Yes"] });
ok(sms && /^Yes/.test(sms.hit.textContent) && sms.viaAlternate,
   "a phone number offered to a consent Yes/No falls through to the consent answer");

// Robinhood: a preference SENTENCE offered to a Yes/No.
const office = matchFirstCandidate(asOpts("Yes", "No"),
  { rendered: "Open to remote, hybrid, or onsite", alternates: ["Yes"] });
ok(office && office.hit.textContent === "Yes" && office.viaAlternate,
   "the work-arrangement sentence falls through to the in-office Yes/No");

// DV Trading: a past graduation date against forward-looking windows.
const grad = matchFirstCandidate(asOpts("I've already graduated", "August 2026 - December 2026", "January 2027"),
  { rendered: "July 2024", alternates: ["I've already graduated"] });
ok(grad && grad.hit.textContent === "I've already graduated",
   "a graduation date already past takes the already-graduated option");

// The ranked-preference use: first alternate the form actually offers wins, in order.
const RANKED = ["San Francisco", "Menlo Park", "Los Angeles", "New York", "Seattle"];
const loc = matchFirstCandidate(asOpts("New York, NY", "Menlo Park, CA", "Bellevue, WA"),
  { rendered: "United States (open to relocation)", alternates: RANKED });
ok(loc && loc.hit.textContent === "Menlo Park, CA",
   "of the offered cities the highest-ranked one wins, not the first option on the page");
ok(matchFirstCandidate(asOpts("Toronto, Canada", "London, UK"),
     { rendered: "United States (open to relocation)", alternates: RANKED }) === null,
   "a list offering none of the ranked answers still reports no match, so it escalates");

// --- otherOption ----------------------------------------------------------------------
ok(otherOption(asOpts("Harvard University", "Other")).textContent === "Other",
   "a plain Other row is the fallback");
ok(otherOption(asOpts("My school is not listed")).textContent === "My school is not listed",
   '"not listed" wording counts as the escape hatch');
ok(otherOption(asOpts("Other (please specify)")).textContent === "Other (please specify)",
   "Other with a qualifier still counts");
// Anchored: a real school whose name merely contains the word must never be mistaken for it.
ok(otherOption(asOpts("Mother Teresa Institute", "Brotherhood College")) === undefined,
   "a school containing 'other' inside a word must not be picked as the fallback");
ok(otherOption(asOpts("Harvard University", "Stanford University")) === undefined,
   "a list with no escape hatch must report none");

// The gate: Other is an answer for education fields, a WRONG answer anywhere else.
ok(OTHER_OK_FIELD.test("school--0") && OTHER_OK_FIELD.test("What university did you attend?"),
   "education fields are otherable");
ok(!OTHER_OK_FIELD.test("Location (City)") && !OTHER_OK_FIELD.test("Gender") && !OTHER_OK_FIELD.test("Race"),
   "location and EEO fields must never fall back to Other");

// --- degreeLevel ----------------------------------------------------------------------
// A degree pick-list offers LEVELS, never credential names, so a stored "B.Eng. (Hons)"
// matched nothing and the field silently committed "Other" (observed on a live Greenhouse
// education block). The substitution only ever applies to a pick-list.
{
  // el stand-in; labelFor is injected the same way liveControl's is.
  const el = (id, label = "") => ({ tagName: "INPUT", id, name: "", _label: label,
                                    getAttribute: () => null, closest: () => null });
  const degreeLevel = makeDegreeLevel((e) => (e && e._label) || "");

  // Dotted initials plus a parenthetical — the shape that broke a live form, because
  // searchable() strips the parenthetical and the remaining stub matches no enum row.
  ok(degreeLevel(el("degree--0"), "B.Eng. (Hons)") === "Bachelor",
     "a dotted-initials credential with a parenthetical maps to its level");
  ok(degreeLevel(el("", "Degree"), "B.Eng. (Hons)") === "Bachelor",
     "the bare visible label 'Degree' is enough to trigger it");

  // THE property the whole fix rests on: the level term must actually match the option
  // text. canon() keeps the apostrophe, but \b sits against it, so the whole-word tier of
  // matchOption connects "Bachelor" to "Bachelor's Degree".
  ok(wb(canon("Bachelor")).test(canon("Bachelor's Degree")),
     "the level term must whole-word-match the option it is meant to select");
  ok(!wb(canon("Bachelor")).test(canon("Master's Degree")),
     "and must not match a different level");

  // Families. Order matters: a doctorate is not a master's, an MBA is its own row.
  const cases = [
    ["B.Tech", "Bachelor"], ["BS", "Bachelor"], ["B.Sc", "Bachelor"],
    ["Bachelor of Engineering", "Bachelor"],
    ["M.S.", "Master"], ["MSc", "Master"], ["Master of Science", "Master"],
    ["MBA", "MBA"], ["M.B.A.", "MBA"],
    ["Ph.D.", "Doctor"], ["PhD", "Doctor"], ["Doctorate", "Doctor"],
    ["Associate of Arts", "Associate"], ["A.S.", "Associate"],
    ["High School Diploma", "High School"],
  ];
  for (const [credential, level] of cases) {
    ok(degreeLevel(el("degree"), credential) === level,
       `${credential} should map to ${level}, got ${degreeLevel(el("degree"), credential)}`);
  }

  // Degree-ADJACENT fields want the subject, not a level. Getting this wrong would type
  // "Bachelor" into Discipline, which is a wrong answer rather than a failed one.
  ok(degreeLevel(el("discipline--0"), "Computer Science") === null,
     "discipline must be left alone");
  ok(degreeLevel(el("", "Major"), "Bachelor of Science in Biology") === null,
     "major must be left alone even when the value names a degree");
  ok(degreeLevel(el("", "Field of Study"), "B.S. Physics") === null,
     "field of study must be left alone");
  ok(degreeLevel(el("school--0"), "Some University") === null, "school must be left alone");

  // An unrecognisable credential returns null so the caller keeps the original value and
  // the existing ladder/Other path still runs — never a silent wrong answer.
  ok(degreeLevel(el("degree"), "Licenciatura") === null,
     "an unmapped credential falls through to the existing ladder");
  ok(degreeLevel(el("degree"), "") === null, "an empty value never substitutes");
}

// isPickList: the substitution is gated on the widget, since a free-text degree box
// legitimately wants "B.Eng. (Hons)".
{
  const plain = { tagName: "INPUT", getAttribute: () => null, closest: () => null };
  ok(isPickList({ tagName: "SELECT", getAttribute: () => null, closest: () => null }, {}),
     "a native <select> is a pick-list");
  ok(isPickList({ tagName: "INPUT", getAttribute: () => "combobox", closest: () => null }, {}),
     "a role=combobox is a pick-list");
  ok(isPickList({ tagName: "INPUT", getAttribute: () => null, closest: () => ({}) },
                { kind: "reactSelect" }), "an input inside a react-select control is a pick-list");
  ok(!isPickList(plain, {}), "a bare text input is not a pick-list");
  ok(!isPickList(plain, { kind: "reactSelect" }),
     "a text input with no react-select ancestor is not a pick-list");
  ok(!isPickList(null, {}), "a missing element must not throw");
}

// --- liveControl ----------------------------------------------------------------------
// Minimal DOM stand-ins. A "control" owns one input; `connected`/`size` model the two ways
// a node stops being clickable (removed from the tree, or laid out at zero size).
function ctl(id, {connected = true, size = 40, label = ""} = {}) {
  const c = {
    isConnected: connected,
    getBoundingClientRect: () => ({width: size, height: size}),
  };
  c.input = {id, isConnected: connected, closest: () => c, _label: label};
  return c;
}
function fakeDom(controls) {
  return {
    getElementById: (id) => (controls.find(c => c.input.id === id) || {}).input || null,
    querySelectorAll: () => controls.map(c => c.input),
  };
}
const labelOf = (el) => (el && el._label) || "";

// The bug: GH rebuilds its Education block after the resume parses, re-keying school--0 to
// school--1 and detaching the captured node. The stale node must NOT be returned — its rect
// is zeros, so the trusted click lands at (0,0) and nothing ever opens.
{
  const stale = ctl("school--0", {connected: false});
  const fresh = ctl("school--1", {label: "School"});
  const lc = makeLiveControl(fakeDom([fresh]), labelOf);
  ok(lc(stale, stale.input, {}) === fresh,
     "a detached control must resolve to the live re-keyed row (the school--0 -> school--1 bug)");
}
// A live control is returned untouched — no needless re-lookup.
{
  const live = ctl("school--0");
  const other = ctl("degree--0");
  ok(makeLiveControl(fakeDom([live, other]), labelOf)(live, live.input, {}) === live,
     "a still-live control must be used as-is");
}
// Zero-sized counts as unusable even while connected: same zero rect, same silent miss.
{
  const collapsed = ctl("school--0", {size: 0});
  const fresh = ctl("school--1");
  ok(makeLiveControl(fakeDom([fresh]), labelOf)(collapsed, collapsed.input, {}) === fresh,
     "a zero-sized control must be treated as unclickable");
}
// Exact id wins over the stem scan, so a re-render that KEPT the id doesn't jump rows.
{
  const same = ctl("school--0");
  const sibling = ctl("school--1");
  const lc = makeLiveControl(fakeDom([same, sibling]), labelOf);
  ok(lc({isConnected: false, getBoundingClientRect: () => ({width: 0, height: 0})},
        {id: "school--0"}, {}) === same,
     "the exact id must win over the id-stem fallback");
}
// The stem must not leak across fields: school--0 must never resolve to degree--1.
{
  const degree = ctl("degree--1");
  const lc = makeLiveControl(fakeDom([degree]), labelOf);
  ok(lc({isConnected: false, getBoundingClientRect: () => ({width: 0, height: 0})},
        {id: "school--0"}, {}) === null,
     "a different field's row must never be substituted");
}
// Last resort: no id match at all, fall back to the label text.
{
  const byLabel = ctl("q_9182", {label: "School"});
  const lc = makeLiveControl(fakeDom([byLabel]), labelOf);
  ok(lc({isConnected: false, getBoundingClientRect: () => ({width: 0, height: 0})},
        {id: "", _label: "School"}, {}) === byLabel,
     "an unrecognizable id falls back to matching the label");
}
// Nothing matches -> null, so the caller reports "left the page" instead of clicking (0,0).
{
  const lc = makeLiveControl(fakeDom([]), labelOf);
  ok(lc({isConnected: false, getBoundingClientRect: () => ({width: 0, height: 0})},
        {id: "school--0", _label: "School"}, {}) === null,
     "an unresolvable control must return null, never a stale node");
}

// --- pageCompany -------------------------------------------------------------------

// The company name is NOT cosmetic: it goes into the drafter's context and gets written
// into the why_company essay itself. A wrong name here ships "I want to work at Embed"
// to a real Discord application — which is exactly what happened, because background.js
// rewrites proxied career domains to /embed/job_app and the old first-path-segment rule
// read the literal "embed" as the tenant.
{
  const co = (href, title = "") => {
    // Spread the parsed URL rather than picking fields by hand. The stub used to carry
    // only {href, pathname}, so the moment pageCompany read location.hostname every case
    // died on `undefined.match` — a harness gap masquerading as a source bug.
    const u = new URL(href);
    return makePageCompany({ href, pathname: u.pathname, hostname: u.hostname,
                             host: u.host, origin: u.origin, search: u.search },
                           { title })();
  };

  // The canonical boards keep the first-path-segment rule.
  ok(co("https://job-boards.greenhouse.io/discord/jobs/7869543") === "Discord",
     "canonical greenhouse board still reads the tenant slug");
  ok(co("https://jobs.ashbyhq.com/ramp/1a2b3c") === "Ramp",
     "ashby board still reads the tenant slug");
  ok(co("https://jobs.lever.co/scale-ai/abc") === "Scale Ai",
     "lever slug is title-cased across the hyphen");

  // Workday inverts the rule: tenant in the subdomain, careers-site name in path[0].
  // Unfixed this read "External Experienced" for all 33 Adobe roles — and unlike the
  // Embed bug it is not only cosmetic, since pickResume() scores this string to choose
  // the resume track the entire Workday fill is then resolved through.
  ok(co("https://adobe.wd5.myworkdayjobs.com/external_experienced/job/San-Francisco/Sr-Research-Engineer_R170796") === "Adobe",
     "Workday reads the company from the tenant subdomain, not path[0]");
  ok(co("https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers/job/Remote/Eng_R1") === "Crowdstrike",
     "a tenant whose path segment repeats the name still resolves from the subdomain");

  // THE regression: the embed rewrite background.js emits for proxied career domains.
  ok(co("https://job-boards.greenhouse.io/embed/job_app?for=discord&token=7869543") === "Discord",
     "embed/job_app must read the company from ?for=, never the literal 'embed'");
  ok(co("https://boards.greenhouse.io/embed/job_app?for=roblox&token=99") === "Roblox",
     "the legacy embed host is covered too");

  // The gh_jid probe path rewrites to ?token= with no `for` — fall back to the page title
  // Greenhouse renders: "Job Application for <role> at <Company>".
  ok(co("https://boards.greenhouse.io/embed/job_app?token=7869543",
        "Job Application for Senior Data Scientist at Discord") === "Discord",
     "with no ?for=, the company comes off Greenhouse's page title");

  // Nothing to go on: empty beats a confidently wrong name, because the drafter omits an
  // empty company from its context but would happily write a wrong one into the essay.
  ok(co("https://boards.greenhouse.io/embed/job_app?token=7869543") === "",
     "an unresolvable embed company must be empty, never 'Embed'");
}

// ── looksLikeAppForm: the gate that decides whether the panel exists at all ─────────────
// Untested until it silently swallowed a whole ATS. Workday shipped with a manifest match,
// a content script and an adapter, and no panel ever appeared, because this gate listed
// only the three original hosts. A mount gate that says no is indistinguishable from a
// broken adapter from the outside, so every supported host is pinned here.
{
  const fn = extract("looksLikeAppForm");
  // The DOM fallbacks must NOT be what rescues a supported host — that is exactly the trap
  // Workday fell into — so every case runs with a document that finds nothing, which makes
  // each assertion below a statement about the URL clauses alone.
  // looksLikeAppForm reads the ambient `location`/`document`, so bind them as globals.
  const mounts = (href) => {
    globalThis.location = { href };
    globalThis.document = { querySelector: () => null };
    return eval(`(${fn})`)();
  };

  ok(mounts("https://adobe.wd5.myworkdayjobs.com/external_experienced/job/San-Francisco/Sr-Research-Engineer_R170796"),
     "a Workday posting page mounts the panel");
  ok(mounts("https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced/apply/applyManually"),
     "THE REGRESSION: the Workday apply wizard has no <form>, so it must qualify on URL alone");
  ok(mounts("https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers/job/USA-Remote/Engineer_R12345"),
     "any Workday tenant qualifies, not just the one the adapter was probed against");

  ok(mounts("https://job-boards.greenhouse.io/block/jobs/5369941008"), "greenhouse board still mounts");
  ok(mounts("https://boards.greenhouse.io/embed/job_app?for=asana&token=99"), "the legacy embed still mounts");
  ok(mounts("https://jobs.lever.co/scale-ai/abc"), "lever still mounts");
  ok(mounts("https://jobs.ashbyhq.com/ramp/1a2b3c"), "ashby still mounts");

  // The gate's other job: keep a force-injected panel off wrapper pages and stray iframes.
  ok(!mounts("https://www.asana.com/jobs/apply/12345"),
     "a wrapper career page with no applicant fields must NOT mount");
  // Leading dot AND a trailing slash are both load-bearing: this host contains the exact
  // string ".myworkdayjobs.com" and is still not Workday.
  ok(!mounts("https://adobe.myworkdayjobs.com.evil.example/apply/applyManually"),
     "a suffix-lookalike host must not match — the clause needs the slash after .com");
  delete globalThis.location; delete globalThis.document;
}

console.log(`fill units: ${n}/${n} checks passed`);
