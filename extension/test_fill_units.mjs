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
const { askId, diffApproved, searchCandidates, otherOption, OTHER_OK_FIELD, makeLiveControl } =
  new Function(
  `${extractConsts()}\n${extract("askId")}\n${extract("diffApproved")}\n` +
  `${extractConst("searchable")}\n${extractConst("GENERIC_SEARCH_WORD")}\n${extract("searchCandidates")}\n` +
  `${extractConst("OTHER_OK_FIELD")}\n${extractConst("otherOption")}\n` +
  `const makeLiveControl = (document, labelFor) => { ${extract("liveControl")} return liveControl; };\n` +
  `return { askId, diffApproved, searchCandidates, otherOption, OTHER_OK_FIELD, makeLiveControl };`)();

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
  { label: "Email", value: "someone.else@gmail.com" },
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
// string compare held EVERY typed answer at phase 3):
ok(diffApproved([{ label: "Sexual orientation", value: "Straight" }],
                [{ label: "Sexual orientation", value: "Heterosexual" }]).length === 0,
   "typed 'Straight' committed as 'Heterosexual' must not be drift (canon tier)");
ok(diffApproved([{ label: "Sponsorship", value: "No" }],
                [{ label: "Sponsorship", value: "None" }]).length === 0,
   "typed 'No' committed as 'None' must not be drift (canon tier)");
ok(diffApproved([{ label: "Gender", value: "Female" }],
                [{ label: "Gender", value: "Woman" }]).length === 0,
   "'Female' committed as 'Woman' must not be drift (canon tier)");
ok(diffApproved([{ label: "Consent", value: "Yes" }],
                [{ label: "Consent", value: "Yes - I consent to receiving text messages" }]).length === 0,
   "an answer committed as a longer option containing it must not be drift (whole-word tier)");
// ...but the equivalence must stay as strict as the matcher: a genuinely different value
// is still drift.
ok(diffApproved([{ label: "Gender", value: "Male" }],
                [{ label: "Gender", value: "Female" }]).length === 1,
   "male vs female must still be drift (whole-word, not substring)");

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

// --- otherOption ----------------------------------------------------------------------
const asOpts = (...t) => t.map(textContent => ({ textContent }));
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

console.log(`fill units: ${n}/${n} checks passed`);
