// fill.js — Apply engine content script (Tier 0 orchestrator).
//
// Runs on a Greenhouse/Lever/Ashby application page. On demand (popup -> "fill"):
//   1. load the staged data layer (profile.json + taxonomy.json).
//   2. pick the matching ATS adapter (DOM map) by host.
//   3. fill known core fields directly (name, email, resume, ...).
//   4. handle radio/checkbox OPTION GROUPS (match the question, click the option).
//   5. for every other control: extract label -> classify (taxonomy) -> set value.
//   6. attach the best-of-4 resume via DataTransfer.
//   7. report {filled, skipped, needsInput, flagged} back to the popup.
// NEVER submits — the submit button is deliberately left for the user's tap.
//
// Dropdown handling is adapter-driven (adapter.dropdown.kind):
//   * reactSelect (Greenhouse) — react-select that ignores synthesized events; filled
//     with OS-trusted clicks via chrome.debugger (CDP). LIVE-TESTED & WORKING.
//   * comboboxTypeahead (Ashby) — role=combobox that opens on a synthesized `input`
//     event; option [role=option] commits on a synthesized click. No CDP.
//   * native (Lever) — plain <select>; setNativeSelect. No CDP.

// Content scripts can't statically import ES modules, so pull helpers dynamically
// from the extension bundle (all are web_accessible_resources).
async function loadDeps() {
  const url = (p) => chrome.runtime.getURL(p);
  const [classifyMod, ghMod, leverMod, ashbyMod, wdMod, pickMod, attachMod] = await Promise.all([
    import(url("classify.js")),
    import(url("adapters/greenhouse.js")),
    import(url("adapters/lever.js")),
    import(url("adapters/ashby.js")),
    import(url("adapters/workday.js")),
    import(url("resume_pick.js")),
    import(url("attach.js")),
  ]);
  return {
    classify: classifyMod.classify,
    dig: classifyMod.dig,
    render: classifyMod.render,
    adapters: [ghMod.GREENHOUSE, leverMod.LEVER, ashbyMod.ASHBY, wdMod.WORKDAY],
    pickResume: pickMod.pickResume,
    attachResume: attachMod.attachResume,
    resumeFilename: attachMod.resumeFilename,
  };
}

async function loadData() {
  const get = async (p) => (await fetch(chrome.runtime.getURL(p))).json();
  const [profile, tax] = await Promise.all([get("profile.json"), get("taxonomy.json")]);
  return { profile, tax };
}

// ── setters ──────────────────────────────────────────────────────────────────
// React tracks input values via a private setter; a bare `el.value = v` is ignored.
// Use the prototype's native setter, then dispatch input+change.
function setNativeValue(el, value) {
  const proto = el instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
  setter.call(el, value);
}
function setText(el, value) {
  // Ashby's validation/submission store (what "Submit" actually reads — separate from the
  // React controlled value) ignores a bare Event("input"); it only commits values arriving
  // via a genuine InputEvent (inputType/data present), like real typing produces. Verified
  // live on the Ramp form 2026-07-15: identical fill with Event → "Missing entry for
  // required field", with InputEvent → accepted. React's onChange reads both identically,
  // and GH/Lever don't care, so InputEvent is strictly safer everywhere.
  try { el.focus(); } catch (_) { /* detached/hidden: ignore */ }
  // Simulate real typing PER CHARACTER (keydown → beforeinput → value → input → keyup).
  // Live-verified on Ramp 2026-07-15: a one-shot InputEvent commits to Ashby's store only
  // SOMETIMES (race with the store's listener hydration — the source of the "field looks
  // filled but submit says missing" randomness), and Ashby's compound PhoneField never
  // accepts it; per-char typing landed 4/4 across name/email/tel including as a resync.
  // Sync loop, no waits — ~instant even for essays.
  if (el.value) {
    // start from scratch so stateful parsers (PhoneField, date) don't see a mid-edit mess
    try { if (el._valueTracker) el._valueTracker.setValue("x"); } catch (_) { /* ignore */ }
    setNativeValue(el, "");
    el.dispatchEvent(new InputEvent("input", { inputType: "deleteContentBackward", bubbles: true }));
  }
  let cur = "";
  for (const ch of String(value)) {
    const nl = ch === "\n";
    el.dispatchEvent(new KeyboardEvent("keydown", { key: nl ? "Enter" : ch, bubbles: true, cancelable: true }));
    el.dispatchEvent(new InputEvent("beforeinput", nl
      ? { inputType: "insertLineBreak", bubbles: true, cancelable: true }
      : { data: ch, inputType: "insertText", bubbles: true, cancelable: true }));
    cur += ch;
    try { if (el._valueTracker) el._valueTracker.setValue(""); } catch (_) { /* ignore */ }
    setNativeValue(el, cur);
    el.dispatchEvent(new InputEvent("input", nl
      ? { inputType: "insertLineBreak", bubbles: true }
      : { data: ch, inputType: "insertText", bubbles: true }));
    el.dispatchEvent(new KeyboardEvent("keyup", { key: nl ? "Enter" : ch, bubbles: true }));
  }
  el.dispatchEvent(new Event("change", { bubbles: true }));
  // Blur so date-picker inputs (Ashby "Pick date...") parse+commit the typed value and
  // close their calendar popup; harmless for plain text/textarea (just loses focus).
  try { el.blur(); } catch (_) { /* ignore */ }
  el.dispatchEvent(new FocusEvent("blur", { bubbles: true }));
}
// Like setText, but first resets React's value tracker so onChange fires EVEN IF the input
// already displays `value`. Needed when a field's framework STATE is stale relative to its
// DOM value — e.g. Ashby's resume parser clears the phone in its form state but the visible
// input still shows our text; a plain setText sees "no change" and never syncs the state, so
// submit still reports the field empty. Resetting the tracker forces the sync.
function forceSetText(el, value) {
  try { if (el._valueTracker) el._valueTracker.setValue(""); } catch (_) { /* ignore */ }
  setText(el, value);
}
// Some deterministic answers are worded generically but a company's dropdown uses its own
// label — most commonly the referral source: "Company website" vs an option like
// "<Company> Career Site" / "Careers". When no literal option matches, also try these
// substrings so the generic answer still lands on the right bespoke option.
const SELECT_SYNONYMS = [
  { when: /company (web ?site|site)|career site|careers/, tries: ["career site", "careers", "company site", "company web", "career"] },
];

// Native <select>: choose the option whose visible text matches (case-insensitive), then a
// whole-word match, then a value match, then a synonym substring, then a fuzzy label match.
// The whole-word step must NOT be a bare substring: want "No" was matching inside
// "Yes, now" / "Unknown" and silently committing the wrong polarity. Returns true if selected.
function setNativeSelect(el, value) {
  const want = String(value).trim().toLowerCase();
  const opts = [...el.options].filter(o => o.value && !/^select\.*\.?$/i.test(o.text.trim()));
  const lc = (o) => o.text.trim().toLowerCase();
  let hit = opts.find(o => lc(o) === want)
        || opts.find(o => wb(want).test(lc(o)))
        || opts.find(o => o.value.trim().toLowerCase() === want);
  if (!hit) {
    const syn = SELECT_SYNONYMS.find(s => s.when.test(want));
    if (syn) hit = opts.find(o => syn.tries.some(t => lc(o).includes(t)));
  }
  if (!hit) hit = matchOption(opts, value); // fuzzy: token overlap / whole-word
  if (!hit) return false;
  el.value = hit.value;
  el.dispatchEvent(new Event("change", { bubbles: true }));
  return true;
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// The new Greenhouse react-select IGNORES synthesized (untrusted) events — verified live:
// dispatched mousedown/pointerdown/keyboard never open the menu or commit a choice, even
// with the menu already open. Only OS-trusted clicks work. Content scripts can't make
// those, so we relay coordinates to the background service worker, which produces trusted
// clicks via chrome.debugger (CDP Input.dispatchMouseEvent). See background.js.
// (Only used when the active adapter sets needsCDP; Lever/Ashby leave it false.)
let CDP_READY = false; // set by runFill after attaching the debugger

const rpc = (msg) => new Promise(res => chrome.runtime.sendMessage(msg, res));

// Every step that writes to the form, in order, with the fields it touched. If a run
// fills the same field more than once, the trace shows which step did it. `runFill`
// starts a new trace. `submitWithRetry` adds to the same trace, because phase 3 sends
// SUBMIT to the same page right after its fill.
let WRITE_TRACE = { t0: Date.now(), steps: [] };
function traceStart() { WRITE_TRACE = { t0: Date.now(), steps: [] }; }
function traceStep(step, fields) {
  const names = (fields || []).map(f => String(f || "").slice(0, 80));
  WRITE_TRACE.steps.push({ ms: Date.now() - WRITE_TRACE.t0, step, count: names.length, fields: names });
}
// Write the trace to `fill_reports.jsonl` as its own row. The caller does not await this.
// A submit that navigates unloads this script, so the message must leave before the click.
function logTrace(stage, extra) {
  try {
    chrome.runtime.sendMessage({ type: "LOG_REPORT", report: {
      mode: "write-trace", stage, url: location.href, trace: WRITE_TRACE.steps, ...(extra || {}) } });
  } catch (_) { /* extension reloaded: skip */ }
}

const centerOf = (el) => {
  const r = el.getBoundingClientRect();
  return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
};

// The page may use scroll-behavior:smooth, so scrollIntoView animates and reading a rect
// before it settles yields an off-screen coordinate (the CDP click then misses and no
// menu opens). Force an instant scroll so the rect is correct synchronously.
const bringIntoView = (el) => el.scrollIntoView({ block: "center", behavior: "instant" });

// Treat "/" as a separator so a slash-joined option ("Undergraduate/Bachelors") tokenizes
// into ["undergraduate","bachelors"] and can match a single-word answer ("Bachelors").
const norm = (s) => String(s).trim().toLowerCase().replace(/\//g, " ").replace(/\s+/g, " ");
// Canonicalize gender wording so "Male" matches an ATS that only offers "Man" (and
// Female/Woman). Applied to BOTH the answer and the options, so either encoding matches.
const canon = (s) => norm(s)
  .replace(/\bmen\b/g, "male").replace(/\bman\b/g, "male")
  .replace(/\bwomen\b/g, "female").replace(/\bwoman\b/g, "female")
  // Sexual-orientation wording: "Straight" and "Heterosexual" are the same answer, and a
  // profile may store either while Greenhouse's EEO option offers the other (sometimes
  // both, as "Heterosexual / Straight"). Collapse them so either matches either. Applied
  // to answer AND options.
  .replace(/\bheterosexual\b/g, "straight")
  // Consent/agreement: a deterministic "Yes" should select an "I agree" / "I accept" /
  // "I acknowledge" option. The "I <verb>" phrasing is consent-checkbox wording (not a
  // Likert "Agree/Strongly agree" scale, which has no "I"), so this is safe to map to yes.
  .replace(/\bi (agree|accept|acknowledge|consent)\b/g, "yes")
  // Treat No / None / N/A / Not applicable as one equivalence class so a boolean-"No"
  // answer fills a "None" option (e.g. sponsorship "require visa? -> OPT/H1B/TN/None/Other",
  // where require_sponsorship=false renders "No"). Safe because exact/whole-word matches run
  // first: if a literal "No" option exists it still wins before this ever collapses them.
  //
  // The separator is a CHARACTER CLASS, not a literal slash, because norm() above has
  // already rewritten "/" to a space by the time this runs — matching on "\/" made the
  // N/A half of this rule dead code, so an ATS offering "N/A" was read as drift against a
  // deterministic "No". Requiring at least one separator keeps a bare "na" from matching.
  .replace(/\bn[\s\/]+a\b|\bnot applicable\b/g, "none").replace(/\bno\b/g, "none");
const wb = (s) => new RegExp(`\\b${s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`);

// The string we TYPE into a type-to-search dropdown (Greenhouse/Ashby run a server-side
// search on exactly what's typed). Strip parentheticals first: a stored value like
// "Ashford Institute of Technology and Science (AITS), Norhaven" typed verbatim matches nothing
// in the school list (the "(AITS)" breaks the search), so the field silently failed and got
// kicked to a human on every Greenhouse board. The option PICK still uses the full original
// value via matchOption, so which option gets chosen is unchanged — this only makes the
// search actually return the list to pick from.
const searchable = (v) => String(v)
  .replace(/\s*\([^)]*\)/g, " ")   // drop "(AITS)" and any parenthetical
  .replace(/\s+([,;])/g, "$1")      // tidy the space left before a comma
  .replace(/\s+/g, " ").trim();

// One typed query = one shot at a long server-side list, so try several, most specific
// first. Generic institution words ("Institute", "University", "of") match thousands of
// rows and narrow nothing; the distinctive words (a place, a founder's name) are what
// actually find the entry — Greenhouse returns nothing for "Ashford Institute of Technology
// and Science, Norhaven" but finds it from "Norhaven". Broadening the QUERY is safe: the PICK
// still matches the full original value via matchOption, so accuracy is unchanged.
const GENERIC_SEARCH_WORD = /^(the|of|and|for|at|in|institute|institutes|institution|university|universities|college|colleges|school|schools|technology|technological|science|sciences|engineering|studies|state|national|international|academy|center|centre|campus|department)$/i;
function searchCandidates(value) {
  const full = searchable(value);
  const segs = full.split(",").map(s => s.trim()).filter(Boolean);
  // The first two comma-segments lead: that's what a places autocomplete wants
  // ("Springfield, Illinois"), and for a two-part school name it IS the full string.
  const out = [segs.slice(0, 2).join(", "), full, segs[0]];
  const words = full.split(/[^A-Za-z0-9&]+/).filter(w => w.length > 2 && !GENERIC_SEARCH_WORD.test(w));
  words.sort((a, b) => b.length - a.length);
  out.push(...words.slice(0, 2));
  return [...new Set(out.map(s => (s || "").trim()).filter(Boolean))];
}

// Where is "Other" a legitimate answer? Greenhouse serves ONE canonical school list and
// plenty of non-US institutions simply aren't in it (verified live: some return "No options"
// for their full name, their city AND their founder name, while "Harvard" returns a hit —
// so the list genuinely lacks them, it isn't a broken search), so a required
// school field fails on every board forever. "Other"/"not listed" is the list's own escape
// hatch for exactly that. Gate it hard: on a location or an EEO dropdown "Other" is a WRONG
// answer, not a fallback, so only education fields get it.
// Do two institution names mean the SAME PLACE, or do they merely speak the same language?
// Strip the words every school shares — the very list the query ladder above already treats
// as uninformative — and see whether anything distinctive survives on both sides. "Ashford
// Institute of Technology and Science, Norhaven" keeps {ashford, norhaven}; "Air Force
// Institute of Technology" keeps {air, force}; they overlap in nothing, and are not the same
// school however much of their name they have in common.
//
// Returns TRUE when either side has no distinctive word left (a bare "Other", a one-word
// discipline): there is nothing to disagree about, so this must not veto those.
function sharesDistinctiveWord(a, b) {
  const words = (s) => new Set(String(s == null ? "" : s).toLowerCase()
    .split(/[^a-z0-9&]+/).filter(w => w.length > 2 && !GENERIC_SEARCH_WORD.test(w)));
  const wa = words(a), wbb = words(b);
  if (!wa.size || !wbb.size) return true;
  for (const w of wa) if (wbb.has(w)) return true;
  return false;
}

// A menu this long is almost certainly one page of something longer; anything shorter is
// plausibly the entire list. Not a precise fact about any ATS — a deliberately generous
// line, because the cost of guessing "truncated" wrongly is a few seconds of typing, while
// guessing "complete" wrongly is answering from a list that never contained the answer.
const LIST_LOOKS_TRUNCATED = 25;

// Having failed to find the answer among the rows on screen, is it worth typing to look for
// more — or have we already seen the whole list, making this miss a real one?
//
// Typing costs a query round-trip apiece and there are several in the ladder, so this must
// stay false for the ordinary short enum (Gender, a Yes/No, a handful of degree levels)
// whose every option was already rendered and already considered.
function shouldSearchList(renderedCount, isEducationField) {
  if (!renderedCount) return true;          // nothing shown: only a query can populate it
  if (isEducationField) return true;        // school/college lists ARE the server-side kind
  return renderedCount >= LIST_LOOKS_TRUNCATED;
}

const OTHER_OK_FIELD = /\b(school|university|college|institution|discipline|major|field[_ ]of[_ ]study|degree)\b/i;
const isOtherable = (el) => !!el && OTHER_OK_FIELD.test([el.id, el.name, labelFor(el)].filter(Boolean).join(" "));
// "Other", "Other (please specify)", "My school is not listed". Anchored so it can't grab
// a real school whose name merely contains the word.
const otherOption = (opts) => opts.find(o => /^other\b|\bnot listed\b/.test(canon(o.textContent)));

// A degree PICK-LIST can only ever offer LEVELS ("Bachelor's Degree", "Master's Degree") —
// it is an enum, not a free-text credential box. A stored credential like "B.Eng. (Hons)"
// therefore returns nothing for every query in the ladder, and because degree is in
// OTHER_OK_FIELD the field then quietly commits "Other" (observed live on a Greenhouse
// education block). Map the credential to its level instead, and use that as BOTH the
// search query and the match target: canon() leaves the apostrophe in place but \b sits
// against it, so a want of "Bachelor" whole-word-matches the option "Bachelor's Degree".
// A free-text degree box is left alone — there the credential name is the right answer.
const DEGREE_FIELD = /\b(degree|degrees|education level|level of education|qualification)\b/i;
// Order is load-bearing: the longer/more specific credentials must be tested before the
// families that would also match them (a doctorate is not a master's; an MBA is its own
// row on most lists and only falls back to the master's family if that row is absent).
const DEGREE_LEVELS = [
  [/\b(ph\.?\s?d|d\.?\s?phil|doctora(l|te)|doctor of|ed\.?\s?d|j\.?\s?d|m\.?\s?d)\b/i, "Doctor"],
  [/\bm\.?\s?b\.?\s?a\b/i, "MBA"],
  [/\b(m\.?\s?(s|a|e|ed|eng|tech|sc|com|phil|fa)|master)\b/i, "Master"],
  [/\b(b\.?\s?(s|a|e|ed|eng|tech|sc|com|ba|ca|fa)|bachelor|under\s?grad)\b/i, "Bachelor"],
  [/\b(a\.?\s?(a|s)|associate)\b/i, "Associate"],
  [/\b(high school|secondary|hsc|g\.?\s?e\.?\s?d|diploma)\b/i, "High School"],
];
// Is this control a pick-from-a-list, as opposed to a text box? Only a pick-list gets the
// degree substitution.
function isPickList(el, dd) {
  if (!el || !el.tagName) return false;
  if (el.tagName.toLowerCase() === "select") return true;
  if (dd && dd.kind === "reactSelect" && el.closest && el.closest(dd.control || ".select__control")) return true;
  return !!(el.getAttribute && el.getAttribute("role") === "combobox");
}
// The level term to search/match for, or null to leave the value untouched. Exported for
// the fill units.
function degreeLevel(el, value) {
  const label = [el && el.id, el && el.name, labelFor(el)].filter(Boolean).join(" ");
  // "Discipline"/"Major"/"Field of study" are degree-adjacent but want the SUBJECT
  // ("Computer Science"), so DEGREE_FIELD deliberately excludes them.
  if (!DEGREE_FIELD.test(label)) return null;
  const v = String(value || "");
  for (const [re, level] of DEGREE_LEVELS) if (re.test(v)) return level;
  return null;
}

// Match a desired answer to one of a set of options. Each option is any object with a
// `.textContent` (real DOM node OR a lightweight {textContent, ...} descriptor) — the
// matched object is returned so the caller can act on it. ATS option wording varies
// ("I am not a protected veteran" vs "No, I am not a veteran"), so go beyond exact:
// exact → answer appears as whole word(s) in an option → an option appears as whole
// word(s) in the answer → best token overlap (with a margin). Whole-word (not substring)
// so "male" can't match "female". Returns the option object or null.
function matchOption(optEls, wantRaw) {
  const want = canon(wantRaw);
  const opts = optEls.map(el => ({ el, t: canon(el.textContent) }));
  let m = opts.find(o => o.t === want); if (m) return m.el;
  m = opts.find(o => wb(want).test(o.t)); if (m) return m.el;
  m = opts.find(o => o.t.length >= 2 && wb(o.t).test(want)); if (m) return m.el;
  // Relocation questions often replace Yes/No with verbose sentences ("I am willing to
  // relocate to this job's location" / "I currently live…" / "I do not live and am not
  // willing…"). Map a boolean answer onto them. Self-scoped: only fires when the options are
  // actually about relocating, so it can't hijack any other question. Runs after the literal
  // passes so a real "Yes"/"No" option still wins first.
  if (opts.some(o => /\brelocat/.test(o.t))) {
    if (/\b(yes|willing|open)\b/.test(want)) {
      m = opts.find(o => /willing to relocate|open to relocat|will relocate/.test(o.t))
        || opts.find(o => /currently live/.test(o.t));
      if (m) return m.el;
    } else if (/\b(none|no|not)\b/.test(want)) {   // canon maps boolean "No" -> "none"
      m = opts.find(o => /not willing|do not (live|relocat)|unwilling|will not relocate/.test(o.t));
      if (m) return m.el;
    }
  }
  const wt = new Set(want.split(" ").filter(w => w.length > 1));
  let best = null, bestScore = 0, second = 0;
  for (const o of opts) {
    const ot = new Set(o.t.split(" ").filter(w => w.length > 1));
    let inter = 0; for (const w of wt) if (ot.has(w)) inter++;
    const score = inter / Math.max(1, Math.min(wt.size, ot.size));
    if (score > bestScore) { second = bestScore; bestScore = score; best = o; }
    else if (score > second) { second = score; }
  }
  return (best && bestScore >= 0.5 && bestScore > second) ? best.el : null;
}

// The ordered answers to try for one classified field: the profile's own value first,
// then the rule's `alternates` and the profile's `answer_alternates` (classify.js has
// already resolved and merged those, in order).
//
// The point is that a LABEL cannot tell you what widget is behind it. "Phone Number"
// fronts a text box on most boards and an SMS-consent Yes/No group on Ashby; "What
// gender pronouns do you prefer?" reads as a gender question and offers He/Him. Rather
// than guess, we let the control itself decide: offer each answer in turn and keep the
// first one it accepts. A control that accepts the first (every text field does) never
// sees the rest, so this is inert everywhere it isn't needed.
//
// An empty rendered value is NOT a candidate but is also NOT a failure — links.portfolio
// and links.twitter are deliberately "" so those fields stay silent instead of escalating
// on every form that asks. Callers treat an empty candidate list as "skip, on purpose".
function answerCandidates(c) {
  const out = [];
  if (c.rendered !== undefined && c.rendered !== null && String(c.rendered).trim() !== "") out.push(String(c.rendered));
  for (const a of c.alternates || []) {
    const s = String(a == null ? "" : a).trim();
    if (s && !out.includes(s)) out.push(s);
  }
  return out;
}

// Walk answerCandidates() against a set of option elements, returning the first that
// matches — plus which candidate it was, so the report can say an alternate was used
// rather than silently claiming the profile's own value went in.
function matchFirstCandidate(items, c) {
  const tried = answerCandidates(c);
  for (let i = 0; i < tried.length; i++) {
    const hit = matchOption(items, tried[i]);
    if (hit) return { hit, want: tried[i], viaAlternate: i > 0 };
  }
  return null;
}

// Re-find a dropdown control that the page may have replaced since the scan captured it.
//
// Greenhouse REBUILDS its Education block after the resume parses, re-keying the rows
// (`school--0` becomes `school--1`) and DETACHING the node we're holding. A detached element
// reports a rect of all zeros, so `centerOf` yields (0,0), the trusted click lands in the
// top-left corner of the page, no menu ever opens, and nothing types — which reads in the
// report exactly like "this ATS's list doesn't have your school". Verified live: the row
// re-keyed on its own mid-session, and the failing fill probed as box="" expanded=false.
//
// Match order: the same node if it's still live → its id → the id STEM (GH education ids are
// `<field>--<row>`, and only the row number changes on a re-key) → the label text.
function liveControl(control, field, dd) {
  const sel = (dd && dd.control) || ".select__control";
  const live = (c) => {
    if (!c || !c.isConnected) return null;
    const r = c.getBoundingClientRect();
    return (r.width > 1 && r.height > 1) ? c : null;
  };
  const still = live(control);
  if (still) return still;
  if (!field) return null;
  const byId = field.id ? document.getElementById(field.id) : null;
  const fromId = live(byId && byId.closest(sel));
  if (fromId) return fromId;
  const stem = field.id && field.id.includes("--") ? field.id.slice(0, field.id.indexOf("--")) : "";
  if (stem) {
    for (const el of document.querySelectorAll(`${sel} input`)) {
      if (el.id && el.id.startsWith(stem + "--")) {
        const c = live(el.closest(sel));
        if (c) return c;
      }
    }
  }
  const want = norm(labelFor(field) || "");
  if (want) {
    for (const el of document.querySelectorAll(`${sel} input`)) {
      if (norm(labelFor(el) || "") === want) {
        const c = live(el.closest(sel));
        if (c) return c;
      }
    }
  }
  return null;
}

// ── Greenhouse education rows ─────────────────────────────────────────────────
// Greenhouse renders education as a repeating row: school--N, degree--N, discipline--N,
// start-month--N, start-year--N, end-month--N, end-year--N. The first three fill from the
// taxonomy; the four DATE fields were asked on every board with this block, 29 of 184
// logged Greenhouse fills, for a month.
//
// WHY THE TAXONOMY CANNOT FIX IT — the same reason degreeLevel() exists. always_ask carries
// `start date` (for "when could you start?", which genuinely is Parth's call) and always_ask
// is matched FIRST by design, so it captured "Start date month" and parked it forever. The
// words are identical in an availability question and an education row; only the page knows
// which. "End date" meanwhile matched no rule at all, since the graduation rule keys on the
// word "graduate".
//
// So key on the ID, not the label. Measured live on a Greenhouse board: the id scheme is
// exactly as above, the MONTH fields are react-selects whose options are full month names
// ("January".."December"), and the YEAR fields are plain <input type="number">. The id is
// also the more reliable handle — a collect run scanning early sees no label at all and
// falls back to the id (that is where the "start-month--0" report labels came from), while
// a later panel fill on the same page reads "Start date month*". Ids do not move.
const EDU_DATE_ID = /^(start|end)-(month|year)--(\d+)$/i;
const MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
                     "July", "August", "September", "October", "November", "December"];

// "September 2020" / "Sep 2020" / "2024-07" / "07/2024" / "2020" -> {month, year}.
// Either half may be "" — a profile that records only a year still fills the year box.
function splitMonthYear(s) {
  const t = String(s == null ? "" : s).trim();
  if (!t) return null;
  const year = (t.match(/\b(?:19|20)\d{2}\b/) || [""])[0];
  let month = MONTH_NAMES.find(M => new RegExp(`\\b${M.slice(0, 3)}`, "i").test(t)) || "";
  if (!month) {
    // Numeric forms. Anchored to a separator so a bare "2020" cannot read as a month.
    const m = t.match(/\b(0?[1-9]|1[0-2])\s*[\/\-]/) || t.match(/[\/\-]\s*(0?[1-9]|1[0-2])\b/);
    if (m) month = MONTH_NAMES[Number(m[1]) - 1] || "";
  }
  return (year || month) ? { month, year } : null;
}

// A label that is really just the control's id, turned into something a human can answer.
//
// labelFor()'s last resort is `el.name || el.id`, and on Greenhouse that resort gets used
// more than it looks: the education block is scanned before the page has wired its
// <label for> elements, so the fields captured in that window carry ids as labels.
// school--0 and degree--0 survive it (the taxonomy matches the words "school" and
// "degree" and fills them anyway), but end-month--0 matches nothing, escalates as an
// "unrecognized required field", and Parth is asked to answer a question called
// "end-month--0" -- on Telegram and in the console drawer both.
//
// This is deliberately PRESENTATIONAL and lives at the escalate() choke point, after
// classify() has already run on the original text. Renaming a field before classification
// would change which taxonomy rules match it and therefore what gets auto-filled, which is
// a fill-behaviour change wearing a labelling change's clothes. Nothing here can do that.
//
// Note this does not stop end-month--0 being escalated at all -- eduDateField() refuses it
// because Greenhouse re-keys school--0 to school--1 when the resume parses, so its
// same-row block proof fails. That is a real and separate bug about WHICH row a date
// belongs to after a re-key, and it needs a live form to settle. This makes the question
// readable meanwhile.
function readableLabel(label) {
  const raw = String(label == null ? "" : label).trim();
  // Only machine ids qualify: one token, no spaces, and a separator. A real question has
  // spaces, so no human-written label can reach the rewrites below.
  if (!raw || /\s/.test(raw) || !/[-_]/.test(raw)) return raw;

  const edu = EDU_DATE_ID.exec(raw);
  if (edu) {
    const [, which, part, row] = edu;
    const when = which.toLowerCase() === "start" ? "Start" : "End";
    const unit = part.toLowerCase() === "year" ? "year" : "month";
    // Row 0 is the first education entry and is named exactly as Greenhouse labels it once
    // hydrated, so the escalated question reads the same as the filled sibling. A later row
    // says which entry it is -- "End date month" twice over would be unanswerable.
    return row === "0" ? `${when} date ${unit}` : `${when} date ${unit} (education ${Number(row) + 1})`;
  }

  // Anything else id-shaped: strip a trailing --N row suffix, split on the separators, and
  // sentence-case it. "work-authorization--2" -> "Work authorization (row 3)".
  const m = /^(.*?)--(\d+)$/.exec(raw);
  const stem = (m ? m[1] : raw).replace(/[-_]+/g, " ").replace(/\s+/g, " ").trim();
  if (!stem) return raw;
  const nice = stem.charAt(0).toUpperCase() + stem.slice(1);
  return m && m[2] !== "0" ? `${nice} (row ${Number(m[2]) + 1})` : nice;
}

// One control, ONE key — whatever it happened to be called when it was recorded.
//
// The accounting in section 4a dedups by label, and the same control can reach it under two
// different names: an id when it was scanned before Greenhouse wired its <label for>, and
// the human label once hydrated. So school--0 and "School*" read as two different fields and
// the same answer was recorded twice — 3 of the 23 `filled` entries on a live Robinhood
// record were duplicates of entries already in the list.
//
// Strips the required marker (norm() does not), then routes through readableLabel so an
// id-named entry compares as the field it actually is.
const fieldKey = (label) => norm(readableLabel(String(label == null ? "" : label).replace(/[✱*]/g, "").trim()));

// Which education date does this control want, if any? `doc` is injected so this stays
// testable. Returns {profileKey, part} or null.
function eduDateField(el, doc) {
  const m = EDU_DATE_ID.exec((el && el.id) || "");
  if (!m) return null;
  const [, which, part, row] = m;
  // PROVE it is the education block. Other widgets use --N row ids too, and answering a
  // stranger's date field with a graduation date would be a wrong answer, not a failed one.
  if (!doc.getElementById(`school--${row}`) && !doc.getElementById(`degree--${row}`)) return null;
  // profile.json describes ONE education. A second row is a degree Parth does not have, so
  // leave it to be asked rather than repeating the first one into it.
  if (row !== "0") return null;
  return {
    profileKey: which.toLowerCase() === "start" ? "education.start_date" : "education.graduation_date",
    part: part.toLowerCase(),
  };
}

// Re-find a plain control (text/textarea/select/checkbox/radio) the page may have replaced
// since the scan captured it. The general-purpose sibling of liveControl(), which does the
// same job for react-select wrappers.
//
// WHY THIS EXISTS: runFill captures a node per unresolved question and then WAITS — for
// Parth to answer in the panel, or for phase 3 to replay a stored answer. The page does not
// wait. Greenhouse rebuilds its Education block once the resume parses (re-keying
// school--0 -> school--1) and Ashby re-inits its form after hydration; either way the
// captured node is detached. Writing to a detached input SUCCEEDS — nothing throws, the
// value sets — so the answer was reported as filled while the visible field stayed empty.
// That silent success is the bug; re-finding the node is half the cure and reading the
// value back (see currentValue) is the other half.
//
// Order: the same node if it is still in the document, then its id, then its name (+ value,
// so one radio option resolves to itself and not a sibling), then its label text.
function liveField(el, adapter) {
  if (!el) return null;
  if (el.isConnected) return el;
  if (el.id) {
    const byId = document.getElementById(el.id);
    if (byId) return byId;
  }
  // THE ID STEM — Greenhouse re-keys a repeating row rather than re-filling it, so once the
  // resume parses `school--0` does not exist any more and the same visible box is now
  // `school--1`. liveControl has recovered from this for a while (see its note), but that is
  // not enough on its own: applyValue calls liveField as its FIRST gate and returns
  // "no longer on the page" before pickReactSelect — and therefore liveControl — is ever
  // reached. school--0 and degree--0 were abandoned on every SpaceX run while sitting in
  // plain view on the page, filled in by the resume parser and never checked.
  //
  // DELIBERATELY NOT FOR RADIOS AND CHECKBOXES: an option group uses the very same `--N`
  // suffix for the OPTION index, so a stem match there resolves "no longer on the page" into
  // a confidently ticked WRONG ANSWER. Those keep the name+value path below, which is built
  // to tell siblings apart, and losing a field stays better than mis-answering one.
  const type = (el.type || "").toLowerCase();
  const stem = el.id && el.id.includes("--") ? el.id.slice(0, el.id.indexOf("--")) : "";
  if (stem && type !== "radio" && type !== "checkbox") {
    for (const c of document.querySelectorAll("input, select, textarea")) {
      if (c.tagName !== el.tagName || (c.type || "").toLowerCase() !== type) continue;
      if (c.id && c.id.startsWith(stem + "--")) return c;
    }
  }
  if (el.name) {
    const named = [...document.getElementsByName(el.name)];
    const byValue = named.find(c => c.value === el.value);
    // One candidate is unambiguous. Several that all miss on value is NOT: a radio group
    // shares one name, and handing back the wrong option would tick the wrong answer —
    // worse than admitting we lost the field.
    if (byValue) return byValue;
    if (named.length === 1) return named[0];
    if (named.length > 1) return null;
  }
  // Last resort. ONE label can legitimately cover two different controls — Ashby's compound
  // PhoneField resolves both a tel input and a texting-consent radio group to "Phone" — so
  // require the same tag and type too, or a phone number could land on a consent radio.
  const want = norm(labelFor(el) || "");
  if (want) {
    const tag = el.tagName;                        // `type` is computed for the stem pass above
    for (const c of document.querySelectorAll("input, select, textarea")) {
      if (c.tagName !== tag || (c.type || "").toLowerCase() !== type) continue;
      if (norm(labelFor(c) || "") === want) return c;
    }
  }
  return null;
}

// What one control currently HOLDS, as the text a human would read off the page. Returns ""
// for "nothing committed", which is the signal that a write did not land. Mirrors the field
// walk in reviewValueSummary, narrowed to a single element.
function currentValue(el, adapter) {
  if (!el || !el.isConnected) return "";
  const tag = el.tagName.toLowerCase();
  if (tag === "select") {
    const o = el.options[el.selectedIndex];
    const t = o ? o.text.trim() : "";
    return /^select\.*\.?$/i.test(t) ? "" : t;     // still on its placeholder = empty
  }
  const dd = (adapter && adapter.dropdown) || {};
  const rs = el.closest && el.closest(dd.control || ".select__control");
  if (rs) {
    // A react-select holds its answer in a value chip, NOT in the input — the input still
    // shows whatever was typed to search, so reading .value here would call a failed pick
    // a success.
    const chips = [...rs.querySelectorAll(dd.committed || ".select__single-value, .select__multi-value")];
    return chips.length ? chips.map(cleanText).join(", ") : "";
  }
  const t = (el.type || "text").toLowerCase();
  if (t === "checkbox" || t === "radio") return el.checked ? (optionText(el, adapter) || "Yes") : "";
  return String(el.value == null ? "" : el.value).trim();
}

// A label whose whole meaning is "expand on the question above" — a pointer with no target.
// Mirrors _REFERS_ELSEWHERE in bridge/drafter.py; keep the two in step (both have units).
// Anchored at the start, or on an explicit back-reference, so a real essay that merely opens
// with "If" ("If you could have any superpower…") is untouched.
const FOLLOW_UP = /^\s*\(?\s*if\s+(you\s+)?(answered|selected|checked|chose|responded|said)?\s*["'“”]?\s*yes\b|^\s*\(?\s*if\s+(yes|so|applicable|any|selected)\b|\bto\s+the\s+(above|previous|preceding)\s+question\b/i;

// Does this answer mean "no"? canon() maps a boolean No onto "none", and ATS forms spell it
// several ways. Word-bounded so a value like "November 2026" is not read as a refusal.
const isNegative = (v) => /^(no|none|not|n a)\b/.test(canon(v || ""));

// The nearest question BEFORE this control that currently holds an answer.
//
// Used only to decide whether a conditional follow-up is live. Walks controls in document
// order rather than through adapter.questionContainerSelectors, because Greenhouse — the ATS
// this was found on — declares none. An unchecked radio reads back empty (see currentValue),
// so a radio group naturally resolves to its CHECKED option and nothing else.
function precedingAnswer(el, adapter) {
  const all = [...document.querySelectorAll("input, select, textarea")]
    .filter(x => !x.disabled && x.offsetParent !== null);
  const i = all.indexOf(el);
  if (i <= 0) return null;
  // Bounded look-back: past a certain distance the "previous question" is a different
  // section of the form and any answer found there says nothing about this field.
  for (let j = i - 1; j >= 0 && j >= i - 40; j--) {
    const v = currentValue(all[j], adapter);
    if (!v) continue;
    return { label: fieldLabel(all[j], adapter) || labelFor(all[j]) || "the question above", value: v };
  }
  return null;
}

// ── react-select (Greenhouse, CDP trusted clicks) ─────────────────────────────
const rsCommitted = (control, dd) =>
  !!control.querySelector(dd.committed || ".select__single-value, .select__multi-value");

// Fill a react-select by trusted click: open the menu, find the option by visible text,
// trusted-click it, confirm it committed. `control` is the .select__control element.
// `dd` = adapter.dropdown (selectors), so this is not hardcoded to one ATS.
async function pickReactSelect(control, value, dd, field) {
  if (!CDP_READY) return { ok: false, why: "dropdown needs trusted clicks; debugger not attached (close DevTools on this tab and retry)" };
  const optSel = dd.option || ".select__option";
  const readOpts = () => [...document.querySelectorAll(optSel)];
  // Re-point at the live control before every click. The form can swap this node out at any
  // moment (see liveControl), and clicking a detached one is a silent no-op.
  const refresh = () => {
    const c = liveControl(control, field, dd);
    if (c) control = c;
    return !!c;
  };
  const openMenu = async () => {
    if (!refresh()) return { ok: false, error: "control is no longer on the page" };
    bringIntoView(control);
    await sleep(60);
    return await rpc({ type: "CDP_CLICK", ...centerOf(control) });
  };
  const menuOpen = () => {
    if (document.querySelector(dd.menu || ".select__menu")) return true;
    const inp = control.querySelector("input");
    return !!inp && inp.getAttribute("aria-expanded") === "true";
  };
  // Typing only reaches the widget while its menu is open and its input focused. A closed
  // menu mid-ladder (a search that returned nothing, a stray backspace on an empty box) would
  // otherwise make EVERY later query — including the "Other" fallback — silently type into
  // nowhere and read as "this list has nothing", which is indistinguishable from the truth.
  const ensureOpen = async () => {
    if (menuOpen()) return true;
    const r = await openMenu();
    await sleep(150);
    return !!(r && r.ok) && menuOpen();
  };
  // Type a query and wait for the server-side search to render rows.
  const searchFor = async (q) => {
    if (!await ensureOpen()) return [];
    const typed = await rpc({ type: "CDP_TYPE", text: q });
    if (!typed || !typed.ok) return [];
    let o = [];
    for (let i = 0; i < 30 && !o.length; i++) {
      await sleep(70);
      o = readOpts();
      // react-select answers an empty search with a NOTICE where the rows would be, so once
      // that notice says "no options" the remaining ticks can only re-read the same nothing —
      // two seconds per query, four queries per field, on every miss. Wait out the loading
      // notice (same element, different modifier) but stop on a definite no.
      if (!o.length && document.querySelector(dd.noResults || ".select__menu-notice--no-options")) break;
    }
    return o;
  };
  // Clear the search box so the NEXT query isn't appended to the failed one (insertText
  // always appends at the caret). Trusted backspaces rather than closing the menu: whether
  // react-select clears its input on close is version- and prop-dependent, and an appended
  // query fails silently in a way that looks exactly like "the school isn't in the list".
  // Verify by reading the input back — if it didn't clear, stop rather than type nonsense.
  const clearTyped = async () => {
    const inp = control.querySelector("input");
    if (!inp) return false;
    const n = (inp.value || "").length;
    if (!n) return true;
    await rpc({ type: "CDP_ERASE", times: n + 2 });
    await sleep(120);
    return !(control.querySelector("input") || {}).value;
  };
  // What the widget actually looks like at the moment we give up. Without this a failure is
  // unfalsifiable from the report: "no options" reads the same whether the menu never opened,
  // the typing never landed, or the list genuinely lacks the value — three different bugs.
  const probe = () => {
    const inp = control.querySelector("input");
    const notice = document.querySelector(".select__menu-notice");
    return `[box=${JSON.stringify((inp && inp.value) || "")}` +
      ` expanded=${(inp && inp.getAttribute("aria-expanded")) || "?"}` +
      ` menu=${document.querySelector(dd.menu || ".select__menu") ? "open" : "gone"}` +
      ` opts=${readOpts().length}` +
      (notice ? ` notice=${JSON.stringify(notice.textContent.trim().slice(0, 24))}` : "") + "]";
  };

  let res = await openMenu();
  if (!res || !res.ok) return { ok: false, why: `open click failed: ${res && res.error}` };
  // The click may itself have triggered the re-render (the row re-keys as the form settles),
  // so re-point before reading anything off the control below.
  refresh();

  let opts = [];
  for (let i = 0; i < 24 && !opts.length; i++) { await sleep(50); opts = readOpts(); }

  // Type-to-search react-select (a "Location (City)" places autocomplete, the school list):
  // the menu opens empty and options load async only after real input. Work down the query
  // ladder until one returns rows — a long official name usually returns nothing where its
  // distinctive word finds the entry.
  const canType = !!control.querySelector("input");
  const tried = [];
  // Choosing a row from whatever list is currently rendered, with the school guard applied.
  const pick = () => {
    let h = opts.length ? matchOption(opts, value) : null;
    // A SEARCH LIST'S ROWS ARE STRANGERS, NOT SYNONYMS. matchOption's last tier scores SHARED
    // TOKENS, and institution names are mostly shared vocabulary, so a list that does not
    // contain your school will still happily offer one that agrees on "institute of
    // technology" — which is how "Air Force Institute of Technology" got committed as a real
    // answer on a real application. On fields carrying long institutional names, the pick
    // must agree on something that actually identifies the place.
    if (h && isOtherable(field) && !sharesDistinctiveWord(value, cleanText(h))) h = null;
    return h;
  };
  let hit = pick();
  // THE LIST A SEARCH BOX SHOWS BEFORE YOU TYPE IS A SUGGESTION, NOT A SEARCH RESULT.
  // The ladder used to run only when the menu opened EMPTY (`!opts.length`), so a picker
  // that greets you with a default page of rows was answered from that page and never
  // searched at all. That is the whole story of the school field: the real entry is only
  // reachable by typing, we never typed, and the closest generic-sounding row on the default
  // page won instead. So a miss on the rendered list can no longer end the attempt.
  //
  // BUT TYPING IS NOT FREE — four queries against a slow server-side search cost seconds,
  // and spending them on "Gender" is pure waste: its three options were all on screen, so no
  // amount of typing will produce a fourth. Only search a list we have reason to believe we
  // are seeing just part of:
  //
  //   • it rendered nothing at all      — the original condition; places autocompletes
  //                                       (Location (City)) open empty and only ever
  //                                       populate from a query
  //   • it is an education field        — school/college/degree/discipline lists are the
  //                                       canonical server-side ones, and the case we
  //                                       actually proved: the default page is a handful of
  //                                       rows out of thousands
  //   • it rendered a full page of rows — a short list is the whole list, but a list this
  //                                       long is almost certainly truncated, so what we
  //                                       want may simply be further down than it will draw
  //
  // Anything else is a small, complete list that we have already read in full: if the answer
  // were there, matchOption would have found it, and a miss is a real miss worth reporting
  // immediately rather than after four fruitless searches.
  if (!hit && canType && shouldSearchList(opts.length, isOtherable(field))) {
    for (const q of searchCandidates(value)) {
      tried.push(q);
      opts = await searchFor(q);
      if (opts.length) { const h = pick(); if (h) { hit = h; break; } }
      if (!await clearTyped()) break;
    }
  }
  let chose = "";
  // Nothing matched anywhere — either the search never returned rows or the rows don't
  // include this value. For a school/degree field that means the list genuinely doesn't
  // carry it, which is what its "Other" row is for. Filling Other beats failing a required
  // field on every board; anywhere else we still fail loudly rather than answer wrongly.
  let otherTried = false, usedOther = false;
  if (!hit && canType && isOtherable(field)) {
    // Clear by backspaces; if that somehow leaves text behind, close+reopen the menu, which
    // resets react-select's search box. Don't let a failed clear silently skip the fallback —
    // that turns "your school isn't in their list" back into a dead required field.
    let clean = await clearTyped();
    if (!clean) {
      await rpc({ type: "CDP_CLICK", ...centerOf(control) });
      await sleep(150);
      const re = await openMenu();
      await sleep(120);
      clean = !!(re && re.ok) && !(control.querySelector("input") || {}).value;
    }
    if (clean) {
      otherTried = true;
      const other = otherOption(await searchFor("Other"));
      if (other) { hit = other; chose = cleanText(other) || "Other"; usedOther = true; }
    }
  }
  if (!hit) {
    const state = probe();   // read BEFORE the closing click, or it always says "gone"
    await rpc({ type: "CDP_CLICK", ...centerOf(control) }); // close menu
    // Name what was actually tried. "No options" alone reads like the widget is broken, when
    // the real finding is usually that this ATS's list doesn't carry the value at all.
    const searched = tried.length ? ` — searched ${tried.map(q => `"${q}"`).join(", ")}` : "";
    const otherNote = otherTried ? ' and no "Other" came back' : "";
    return { ok: false, why: opts.length
      ? `no option matched "${value}" (options: ${opts.slice(0, 8).map(o => o.textContent.trim()).join(" | ")})`
      : `this list has nothing for "${value}"${searched}${otherNote} ${state}` };
  }
  // cleanText takes an ELEMENT. Every call here passed `hit.textContent` — a string, whose
  // own .textContent is undefined — so this yielded "" on every pick this file has ever
  // made, and the value below silently fell back to what we ASKED for. That is why the
  // reports could never show a wrong pick: they were structurally incapable of it, printing
  // the intended answer whatever landed on the page. It also silently disarmed anything
  // downstream that reasoned about the chosen row's text.
  //
  // The option's own text, whatever path selected it. matchOption deliberately accepts
  // near-misses (whole-word and token-overlap tiers), so the row that commits is routinely
  // worded differently from what we asked for — "Bachelor" selects "Bachelor's Degree",
  // "No" selects "I am not a protected veteran". Recording the ASKED value there was a
  // latent flush bug: phase 1 stored our wording, phase 3 re-read the form's wording, and
  // diffApproved saw drift on a field nobody had touched.
  chose = chose || cleanText(hit) || "";
  bringIntoView(hit);
  await sleep(50);
  res = await rpc({ type: "CDP_CLICK", ...centerOf(hit) });
  if (!res || !res.ok) return { ok: false, why: `option click failed: ${res && res.error}` };
  await sleep(90);
  refresh();   // the pick can re-render the row too; read the commit off the live node
  const ok = rsCommitted(control, dd);
  // Multi-select keeps its menu open after a pick; close it so it doesn't cover the next
  // field (single-select closes itself). A second click on the control toggles it shut.
  if (ok && document.querySelector(dd.menu || ".select__menu")) { await rpc({ type: "CDP_CLICK", ...centerOf(control) }); await sleep(40); }
  // Falling back to "Other" is a real answer, but it is also a claim that this list does not
  // carry the value — and that claim is only checkable if the report says which queries were
  // asked. Without it, an "Other" that came from a search bug and an "Other" that came from a
  // genuinely missing entry read identically.
  const why = ok
    ? (usedOther ? `used "Other" — searched ${tried.map(q => `"${q}"`).join(", ") || "(nothing typed)"}` : "")
    : "clicked option but value did not commit";
  return { ok, value: chose || undefined, why };
}

// ── combobox typeahead (Ashby, synthesized events) ────────────────────────────
// LIVE-VERIFIED: an <input role=combobox> opens its listbox on a synthesized `input`
// event (typeahead) or ArrowDown; a synthesized click on a [role=option] commits.
// Type a prefix to filter (option labels can expand wording, e.g. state names), then
// match the FULL desired value against the rendered options.
async function pickComboboxTypeahead(el, value, dd) {
  el.focus();
  const optSel = dd.option || '[role="option"]';
  // Retyping is free here (setNativeValue replaces the whole value), so the query ladder is
  // just a loop — no menu toggling like the react-select path needs.
  const searchFor = async (q) => {
    setNativeValue(el, q);
    // InputEvent (not bare Event) so Ashby's submission store also sees the typeahead text
    // — same requirement as setText; the listbox opens on either.
    el.dispatchEvent(new InputEvent("input", { data: q, inputType: "insertText", bubbles: true }));
    let o = [];
    for (let i = 0; i < 20 && !o.length; i++) { await sleep(60); o = [...document.querySelectorAll(optSel)]; }
    return o;
  };

  // typePrefixOnly (Ashby's location typeahead) still LEADS with the bare prefix — that's
  // what that flag is for — but it no longer ends there: if the prefix renders nothing, the
  // rest of the ladder is a free extra shot rather than a failed field.
  const queries = [...new Set((dd.typePrefixOnly ? [searchable(value).split(",")[0].trim()] : [])
    .concat(searchCandidates(value)).filter(Boolean))];
  let opts = [];
  for (const q of queries) {
    opts = await searchFor(q);
    if (opts.length) break;
  }
  if (!opts.length) {
    // Some comboboxes need an explicit open (fixed option list, not typeahead).
    el.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    for (let i = 0; i < 12 && !opts.length; i++) { await sleep(60); opts = [...document.querySelectorAll(optSel)]; }
  }

  let hit = opts.length ? matchOption(opts, value) : null;
  // Same guard as the react-select picker — this typeahead searches the same kind of long
  // institution list and is open to the same shared-vocabulary mismatch.
  if (hit && isOtherable(el) && !sharesDistinctiveWord(value, cleanText(hit))) hit = null;
  let chose = "";
  // Same escape hatch as the react-select path: a school this list doesn't carry is what its
  // "Other" row exists for. Education fields only — see isOtherable.
  if (!hit && isOtherable(el)) {
    const other = otherOption(await searchFor("Other"));
    if (other) { hit = other; chose = cleanText(other) || "Other"; }
  }
  if (!hit) {
    el.blur();
    return { ok: false, why: opts.length
      ? `no option matched "${value}" (options: ${opts.slice(0, 8).map(o => o.textContent.trim()).join(" | ")})`
      : "combobox opened but no options rendered" };
  }
  // Report the option's own wording, not ours — same reason as the react-select path.
  chose = chose || cleanText(hit) || "";
  bringIntoView(hit);
  hit.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  hit.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  await sleep(120);
  const committed = !!(el.value && el.value.trim());
  return { ok: committed, value: chose || undefined, why: committed ? "" : "clicked option but value did not commit" };
}

// Harvest a react-select's option labels (open → read [role=option]/`.select__option` →
// close) so an ESCALATED dropdown can be shown as tappable Telegram buttons instead of a
// free-text ask. Only for reactSelect + a short list; combobox typeaheads (huge async
// place lists) and unavailable CDP fall through to free-text. Best-effort; never throws.
async function harvestOptions(el, adapter) {
  const dd = adapter.dropdown || {};
  if (dd.kind !== "reactSelect") return undefined;
  const control = el.closest && el.closest(dd.control || ".select__control");
  if (!control || !CDP_READY) return undefined;
  try {
    bringIntoView(control); await sleep(60);
    let res = await rpc({ type: "CDP_CLICK", ...centerOf(control) });
    if (!res || !res.ok) return undefined;
    let opts = [];
    const optSel = dd.option || ".select__option";
    for (let i = 0; i < 16 && !opts.length; i++) { await sleep(50); opts = [...document.querySelectorAll(optSel)]; }
    const texts = opts.map(o => o.textContent.trim()).filter(Boolean);
    if (document.querySelector(dd.menu || ".select__menu")) { await rpc({ type: "CDP_CLICK", ...centerOf(control) }); await sleep(30); }
    return (texts.length && texts.length <= 12) ? texts : undefined; // finite list only (button cap)
  } catch (_) { return undefined; }
}

const firstMatch = (selectors) => selectors.map(s => document.querySelector(s)).find(Boolean) || null;

// Label extraction, per adapter.labelStrategy order (default order below).
function labelFor(el) {
  if (el.id) {
    const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (l) return l.textContent.trim();
  }
  const parent = el.closest("label");
  if (parent) return parent.textContent.trim();
  if (el.getAttribute("aria-label")) return el.getAttribute("aria-label").trim();
  const labelledby = el.getAttribute("aria-labelledby");
  if (labelledby) {
    const l = document.getElementById(labelledby);
    if (l) return l.textContent.trim();
  }
  if (el.placeholder) return el.placeholder.trim();
  return (el.name || el.id || "").trim();
}

const cleanText = (el) => (el && el.textContent || "").replace(/\s+/g, " ").trim();

// Label for a single control, adapter-aware. Lever puts the real question prompt in the
// enclosing `.application-question .application-label`, NOT a label[for]/aria — so a bare
// labelFor() falls back to the raw cards[...] name and taxonomy can't classify it. Prefer
// the question-container prompt when present, then the standard strategies. (Ashby has no
// .application-label so this cleanly falls through to labelFor's label[for] route.)
function fieldLabel(el, adapter) {
  const sel = (adapter.questionContainerSelectors || []).join(",");
  const container = sel ? el.closest(sel) : null;
  if (container) {
    const al = container.querySelector(".application-label");
    if (al) { const t = cleanText(al).replace(/[✱*]/g, "").trim(); if (t) return t; }
  }
  const direct = labelFor(el);
  // Ashby date-picker inputs have no id/name/aria and their label[for] points at the
  // widget wrapper, so labelFor returns only the "Pick date..." placeholder. When the
  // direct label is that weak (placeholder/name/id), fall back to the question container's
  // heading ("Graduation Date"). Well-labeled fields keep their real label[for] text.
  const ph = (el.placeholder || "").trim();
  const weak = !direct || direct === ph || direct === (el.name || "").trim() || direct === (el.id || "").trim();
  if (weak && container) {
    const h = container.querySelector('[class*="_heading"]');
    if (h) { const t = cleanText(h).replace(/[✱*]/g, "").trim(); if (t) return t; }
  }
  // GH acknowledgement checkboxes: the option's own label is a generic "Acknowledge/Confirm"
  // while the real question sits in the enclosing <fieldset class="checkbox"><legend class="label">
  // (e.g. "Please review and acknowledge …Candidate Privacy Policy"). Prefer that legend so
  // taxonomy classifies the actual question, not the button text. Ashby/Lever use no
  // fieldset+legend, so this is effectively GH-scoped; the length guard means it only wins
  // when the legend is the fuller prompt.
  if (el.type === "checkbox" || el.type === "radio") {
    const fs = el.closest("fieldset");
    const legend = fs && fs.querySelector("legend");
    if (legend) {
      const lt = cleanText(legend).replace(/[✱*]/g, "").trim();
      if (lt && lt.length > (direct || "").length) return lt;
    }
  }
  return direct;
}

// Is this control required? Native attrs and a text asterisk are the easy signals, but
// some ATSs (Ashby) mark required ONLY via a CSS class / ::after "*" on the question's
// title label — invisible to the attribute/text checks. Missing that silently drops the
// field: an unmatched one looks optional, so it's skipped instead of escalated (this is
// exactly why Ashby's "...able to work out of our office 2-3 days?" got dropped). So also
// inspect the enclosing question container's title.
function isRequired(el, adapter) {
  if (el.required || el.getAttribute("aria-required") === "true") return true;
  if (/\*|✱/.test(labelFor(el))) return true;
  const sel = (adapter && adapter.questionContainerSelectors || []).join(",");
  const container = (sel && el.closest) ? el.closest(sel) : null;
  if (container) {
    const title = container.querySelector('[class*="_heading"], [class*="_label"], label, legend');
    if (title) {
      if (/_required/i.test(title.className || "")) return true;      // Ashby marker class
      if (/\*|✱/.test(title.textContent || "")) return true;
      try { if ((getComputedStyle(title, "::after").content || "").includes("*")) return true; } catch (_) { /* ignore */ }
    }
  }
  return false;
}

// Required-ness of a widget that has no fillable input to test (Ashby's Yes/No buttons are
// backed by a hidden, non-required checkbox), read from the enclosing question container's
// title marker. Same container/`_required`/asterisk logic as isRequired's container branch.
function isRequiredByTitle(el, adapter) {
  const sel = (adapter && adapter.questionContainerSelectors || []).join(",");
  const container = (sel && el && el.closest) ? el.closest(sel) : null;
  if (!container) return false;
  const title = container.querySelector('[class*="_heading"], [class*="_label"], label, legend');
  if (!title) return false;
  if (/_required/i.test(title.className || "")) return true;
  if (/\*|✱/.test(title.textContent || "")) return true;
  try { if ((getComputedStyle(title, "::after").content || "").includes("*")) return true; } catch (_) { /* ignore */ }
  return false;
}

// intl-tel-input (the Greenhouse React phone widget) renders a SEPARATE country-code
// selector that already supplies the "+1". Typing the country code again into the
// national-number input duplicates it ("+1 +1 …"), so strip a leading "+<code>" when such
// a widget wraps this field. Forms with no country selector (Lever/Ashby plain tel) keep
// the full "+1 …" number unchanged.
function normalizePhone(el, phone) {
  const iti = (el.closest && el.closest('.iti, [class*="intl-tel" i]')) ||
    document.querySelector('.iti__selected-flag, .iti__flag-container, .iti__country-container, .iti__selected-country');
  if (!iti) return phone;
  // Require a separator after the code so an ambiguous "+1678…" is left alone, not mangled;
  // A stored number like "+1 555-…" strips cleanly to the national part.
  return String(phone).replace(/^\s*\+\d{1,3}[\s\-.]+/, "").trim();
}

// A single checkbox (consent / boolean question). Click only if the desired state
// differs, so React's onChange fires with a real toggle. Truthy strings -> checked.
function setCheckbox(el, value) {
  const want = value === true || /^(yes|true|1|checked|on)$/i.test(String(value).trim());
  if (el.checked !== want) el.click();
  return true;
}

// Visible option text of a radio/checkbox: the wrapping <label> text (Lever), else a
// label[for=id] (Ashby), else the input's own value when the adapter says value==text.
function optionText(input, adapter) {
  const wrap = input.closest("label");
  if (wrap) { const t = cleanText(wrap); if (t) return t; }
  if (input.id) {
    const l = document.querySelector(`label[for="${CSS.escape(input.id)}"]`);
    if (l) { const t = cleanText(l); if (t) return t; }
  }
  if (adapter.optionValueIsText && input.value) return input.value;
  return input.value || "";
}

// Click a radio/checkbox option: prefer its wrapping/associated label (Ashby needs the
// label click), fall back to the input.
function clickOption(input) {
  const wrap = input.closest("label")
    || (input.id ? document.querySelector(`label[for="${CSS.escape(input.id)}"]`) : null);
  (wrap || input).click();
}

// Route a resolved value to the right setter by control kind. Returns {ok, why}.
async function applyValue(el, value, adapter) {
  const dd = adapter.dropdown || { kind: "native" };
  // The node may have been captured before an async page event (resume parse, hydration)
  // replaced it. Re-point first: a detached element accepts every write and shows none.
  const live = liveField(el, adapter);
  if (!live) return { ok: false, why: "that field is no longer on the page — re-run Fill" };
  el = live;
  const tag = el.tagName.toLowerCase();
  // A pick-list wants the LEVEL, a text box wants the credential — see degreeLevel().
  // Done here because this is the only place that knows the widget kind: the taxonomy
  // classifies on the label alone, and "Degree" is the same label either way.
  if (isPickList(el, dd)) value = degreeLevel(el, value) || value;
  if (tag === "select") return { ok: setNativeSelect(el, value), why: "no matching <option>" };
  // react-select (Greenhouse): its labelled control is a text input inside
  // .select__control — must be driven by trusted clicks (CDP), never setText.
  if (dd.kind === "reactSelect") {
    const rs = el.closest && el.closest(dd.control || ".select__control");
    if (rs) return await pickReactSelect(rs, value, dd, el);
  }
  // combobox typeahead (Ashby): role=combobox input filled with synthesized events.
  if (el.getAttribute && el.getAttribute("role") === "combobox") {
    if (dd.kind === "comboboxTypeahead") return await pickComboboxTypeahead(el, value, dd);
  }
  // Text-ish controls: WRITE THEN READ BACK. setText() reports nothing and swallows a
  // detached-node focus error, so "return {ok:true}" here used to be a promise the code had
  // no evidence for — the origin of a green ✓ over an empty field.
  //
  // Only an EMPTY read-back counts as failure. A field that reformats what it was given
  // (phone punctuation, date pickers, a masked input) genuinely holds the answer, so the
  // committed text is reported as `value` rather than treated as a miss — inventing false
  // failures would just move the dishonesty to the other side.
  const verified = () => {
    const got = currentValue(el, adapter);
    if (!got) return { ok: false, why: "wrote it but the field is still empty — the page may have replaced it" };
    return { ok: true, value: sameAnswer(got, value) ? value : got };
  };
  if (tag === "textarea") { setText(el, value); return verified(); }
  if (tag === "input") {
    const t = (el.type || "text").toLowerCase();
    if (t === "checkbox") return { ok: setCheckbox(el, value) };
    if (["text", "email", "tel", "url", "search", ""].includes(t)) { setText(el, value); return verified(); }
    // radios handled by group elsewhere; skip here.
  }
  return { ok: false, why: "unsupported control type" };
}

// react-select injects a hidden, unlabeled proxy <input class="…requiredInput"> per
// combobox purely to drive native `required` validation. They're not real questions —
// exclude them (and any zero-area invisible input) so they don't spam the report.
function isRealField(el) {
  if (/requiredInput/i.test(el.className || "")) return false;
  const r = el.getBoundingClientRect();
  if (r.width < 2 && r.height < 2 && el.type !== "checkbox" && el.type !== "radio") return false;
  return true;
}

// ── radio/checkbox OPTION GROUPS ───────────────────────────────────────────────
// A multi-option question (Lever pronouns / yes-no cards, Ashby labeled radios) renders
// as several inputs sharing a `name`. Each input's own label is the OPTION text, so
// classifying it individually would misfire — we must classify the QUESTION and then
// click the matching option. Collect groups of size >= 2 here; lone checkboxes (consent
// booleans) stay in the per-control loop.
function collectChoiceGroups(adapter) {
  const byKey = new Map();
  for (const el of document.querySelectorAll('input[type="radio"], input[type="checkbox"]')) {
    if (el.disabled || el.offsetParent === null) continue;
    // Group key: prefer the shared field id-prefix over `name`. Radios in one question
    // share a `name`, but Ashby's multi-select CHECKBOXES give each option a DIFFERENT
    // `name` (= the option text, e.g. "LinkedIn") and only share an id of the shape
    // "<fieldId>-labeled-(checkbox|radio)-N". Grouping by `name` there yields groups of
    // one, so each option's own label gets classified — leaking options to Tier 1 and
    // even false-matching URL rules ("LinkedIn"). The id-prefix collapses them into the
    // real question; radios still group identically (their id-prefix == their name).
    const m = (el.id || "").match(/^(.*)-labeled-(?:checkbox|radio)-\d+$/);
    const key = m ? "p:" + m[1] : (el.name ? "n:" + el.name : "");
    if (!key) continue;
    if (!byKey.has(key)) byKey.set(key, []);
    byKey.get(key).push(el);
  }
  const groups = [];
  for (const [key, inputs] of byKey) {
    if (inputs.length >= 2) groups.push({ name: key, inputs, kind: inputs[0].type });
  }
  return groups;
}

// Ashby Yes/No button widgets: <div class="_yesno"> with <button>Yes</button><button>No</button>
// backed by a hidden native checkbox. The hidden input means these never form a radio group
// and would be dropped — collect the widgets directly (gated on adapter.yesNo). Returns
// { container, buttons } groups; the enclosing question prompt is derived separately.
function collectYesNoGroups(adapter) {
  const yn = adapter.yesNo;
  if (!yn) return [];
  const groups = [];
  for (const container of document.querySelectorAll((yn.containerSelectors || []).join(",") || "\0")) {
    if (container.offsetParent === null) continue;
    const buttons = [...container.querySelectorAll(yn.option || "button")].filter(b => b.offsetParent !== null);
    if (buttons.length >= 2) groups.push({ container, buttons });
  }
  return groups;
}

// Prompt for a Yes/No widget: the enclosing question container's heading, excluding the
// "Yes"/"No" button labels themselves.
function yesNoQuestion(container, adapter) {
  const sel = (adapter.questionContainerSelectors || []).join(",");
  const fs = sel ? container.closest(sel) : container.parentElement;
  if (!fs) return "";
  for (const l of fs.querySelectorAll('[class*="_heading"], label, legend')) {
    const t = cleanText(l).replace(/[✱*]/g, "").trim();
    if (t && !/^(yes\s*\/?\s*no|yes|no)$/i.test(t)) return t;
  }
  return "";
}

// The QUESTION prompt for a choice group: prefer an explicit prompt in the enclosing
// question container that is NOT one of the option labels.
function questionLabel(group, adapter) {
  const sel = (adapter.questionContainerSelectors || []).join(",");
  const container = sel ? group.inputs[0].closest(sel) : null;
  if (container) {
    const al = container.querySelector(".application-label"); // Lever explicit prompt
    if (al) return cleanText(al).replace(/[✱*]/g, "").trim();
    const optIds = new Set(group.inputs.map(i => i.id).filter(Boolean));
    const optTexts = new Set(group.inputs.map(i => norm(optionText(i, adapter))));
    for (const l of container.querySelectorAll("label, legend")) {
      const f = l.getAttribute && l.getAttribute("for");
      if (f && optIds.has(f)) continue;            // an option's own label
      const t = cleanText(l);
      if (t && !optTexts.has(norm(t))) return t.replace(/[✱*]/g, "").trim();
    }
  }
  return "";
}

// Write one answer (typed, tapped, or stored) back to its own control, via the closure
// escalate() captured for it, and move it out of its pending bucket. Module-level rather
// than a runFill local because the panel answers questions AFTER runFill has returned —
// the ask closures outlive the call.
// Returns the setter's own {ok, why, value} rather than a bare boolean, so a caller can
// tell Parth WHY an answer didn't land ("that field is no longer on the page — re-run
// Fill") instead of the generic "couldn't write that". Callers must test `.ok` — the
// object itself is always truthy.
async function applyAskAnswer(report, a, raw) {
  if (raw == null || String(raw).trim() === "") return { ok: false, why: "left blank" };
  const v = String(raw).trim();
  let r;
  try { r = await a.apply(v); } catch (e) { r = { ok: false, why: String((e && e.message) || e) }; }
  resolveAsk(report, a.id, v, r);
  return r;
}

// After Parth answers on Telegram, move a resolved field out of its needs/flag bucket:
// into `filled` if his answer applied, or back to `flagged` (annotated) if it didn't.
function resolveAsk(report, askId, value, r) {
  for (const bucket of ["needsInput", "flagged"]) {
    const idx = report[bucket].findIndex(i => i._askId === askId);
    if (idx < 0) continue;
    const [item] = report[bucket].splice(idx, 1);
    delete item._askId;
    if (r.ok) report.filled.push({ label: item.label || item.key, value, source: "telegram" });
    else { item.why = `you answered "${value}" but it didn't apply: ${r.why || "setter failed"}`; report.flagged.push(item); }
    return;
  }
}

// Read back the CURRENT value/selection of every visible field, so the end-of-form review
// shows the WHOLE form (deterministic fills included), not just what was escalated. This
// reflects true committed DOM state, so it also surfaces anything that silently didn't take.
function reviewValueSummary(adapter) {
  const seen = new Set();
  const rows = [];
  const push = (label, value) => {
    const l = String(label || "").replace(/[✱*]/g, "").trim();
    const v = String(value == null ? "" : value).trim();
    if (l && v) rows.push({ label: l, value: v });
  };

  // radio / checkbox groups -> the checked option(s)
  for (const group of collectChoiceGroups(adapter)) {
    group.inputs.forEach(i => seen.add(i));
    const checked = group.inputs.filter(i => i.checked);
    if (checked.length) push(questionLabel(group, adapter) || labelFor(group.inputs[0]),
      checked.map(i => optionText(i, adapter)).join(", "));
  }
  // Ashby Yes/No button widgets -> the active button
  for (const g of collectYesNoGroups(adapter)) {
    const sel = (adapter.questionContainerSelectors || []).join(",");
    const cb = (sel ? g.container.closest(sel) : g.container.parentElement)?.querySelector('input[type="checkbox"]');
    if (cb) seen.add(cb);
    const active = g.buttons.find(b => adapter.yesNo && b.matches(adapter.yesNo.committed));
    if (active) push(yesNoQuestion(g.container, adapter), cleanText(active));
  }
  // native <select> -> selected option text
  for (const el of document.querySelectorAll("select")) {
    if (el.offsetParent === null) continue;
    seen.add(el);
    const o = el.options[el.selectedIndex];
    const t = o ? o.text.trim() : "";
    if (t && !/^select\.*\.?$/i.test(t)) push(fieldLabel(el, adapter), t);
  }
  // react-select (Greenhouse) -> committed value chip(s)
  for (const ctl of document.querySelectorAll(".select__control")) {
    const chips = [...ctl.querySelectorAll(".select__single-value, .select__multi-value")];
    if (!chips.length) continue;
    const inp = ctl.querySelector("input");
    if (inp) seen.add(inp);
    push(inp ? fieldLabel(inp, adapter) : "", chips.map(cleanText).join(", "));
  }
  // text inputs / textareas with a value
  for (const el of document.querySelectorAll("input, textarea")) {
    if (seen.has(el)) continue;
    const t = (el.type || "text").toLowerCase();
    if (["hidden", "submit", "button", "file", "password", "radio", "checkbox"].includes(t)) continue;
    if (el.offsetParent === null || !isRealField(el)) continue;
    if (el.value && el.value.trim()) push(fieldLabel(el, adapter), el.value);
  }
  // resume file name, if attached
  const rf = adapter.coreFields.find(f => f.control === "file");
  const ri = rf ? firstMatch(rf.selectors) : null;
  if (ri && ri.files && ri.files.length) push("Resume", ri.files[0].name);
  return rows;
}

// PHASE 3 SAFETY NET. Parth approves a SUMMARY on Telegram, but phase 3 re-fills the
// same form minutes-to-hours later from scratch — and Ashby's hydration race demonstrably
// drops a different random field on every run. So what's on the page now is NOT guaranteed
// to be what he said yes to. Diff the two; any drift and background refuses to submit,
// leaves the tab open, and tells him. Fails toward not-submitting, like the rest of the
// pipeline: a false positive costs one manual look, a false negative sends a wrong
// application to a real employer.
// Only checks fields he was actually SHOWN — a field that appears now but wasn't in the
// approved summary is left to the ATS's own validation.
// Is what the form now HOLDS the same answer we tried to put there? Lifted out of
// diffApproved so the submit gate and the post-write read-back judge equivalence by one
// rule — if they drifted, a value could pass the write check and then trip the gate.
//
// Phone equivalence: a site often reformats the number it's given — "(555) 012-3456" comes
// back as "555-012-3456", the SAME number. Narrowly guarded (phone punctuation only, 7+
// digits) so it can never equate two different numeric answers or anything with letters.
// Declarations, not const arrows: these are defined far below the norm/canon/wb block they
// depend on and far above some callers, and a function declaration hoists so neither
// ordering can produce a temporal-dead-zone error at runtime or in the unit harness.
function onlyDigits(s) { return String(s).replace(/\D/g, ""); }
function phoneish(s) {
  return /^[+()\-.\s\d]+$/.test(String(s).trim()) && onlyDigits(s).length >= 7;
}
function phoneEq(a, b) {
  if (!phoneish(a) || !phoneish(b)) return false;
  const da = onlyDigits(a).replace(/^1(?=\d{10}$)/, "");
  const db = onlyDigits(b).replace(/^1(?=\d{10}$)/, "");
  return da === db;
}
// The same equivalence tiers the matcher uses to COMMIT a value (exact after canon, then
// whole-word either way), and nothing looser — token-overlap fuzziness has no place in a
// correctness check. A typed answer and the option it lands on rarely match verbatim.
function sameAnswer(a, b) {
  const na = String(a == null ? "" : a).toLowerCase().replace(/\s+/g, " ").trim();
  const nb = String(b == null ? "" : b).toLowerCase().replace(/\s+/g, " ").trim();
  if (na === nb) return true;
  if (phoneEq(na, nb)) return true;
  const ca = canon(na), cb = canon(nb);
  if (ca === cb) return true;
  if (ca.length >= 2 && wb(ca).test(cb)) return true;
  if (cb.length >= 2 && wb(cb).test(ca)) return true;
  return false;
}

function diffApproved(approved, current) {
  if (!approved || !approved.length) return [];
  const norm = v => String(v == null ? "" : v)
    .replace(/ /g, " ").toLowerCase().replace(/\s+/g, " ").trim();
  // Group by label rather than keying a Map on it: ONE label can legitimately cover
  // several controls. Ashby's compound PhoneField renders a tel input AND a texting-
  // consent radio group, and fieldLabel resolves both to "Phone" — so the approved list
  // holds "+1 555-…" (deterministic) and "Yes - I consent…" (the typed answer) under the
  // same key. A plain Map lets the last one win and then reports a mismatch against a
  // form that is actually correct — which is exactly what blocked the first live run.
  // Key labels with the required-marker (✱/*) stripped. reviewValueSummary drops it when it
  // reads the form back, but an approved label came straight from the raw question text and
  // still carries it — so "…employer? *" (approved) and "…employer?" (read back) would key
  // differently and a correctly-filled required question reads as "missing now". This blocked
  // Affirm's two questions on the 2026-07-20 run though both were filled with the right value.
  const keyLabel = s => norm(String(s == null ? "" : s).replace(/[✱*]/g, ""));
  const group = rows => {
    const m = new Map();
    for (const r of rows || []) {
      const k = keyLabel(r.label);
      if (!k) continue;
      if (!m.has(k)) m.set(k, { label: r.label, values: [] });
      m.get(k).values.push(norm(r.value));
    }
    return m;
  };
  // A typed Telegram answer and the option it lands on rarely match verbatim: he types
  // a typed orientation, matchOption commits the ATS spelling of it; "No" commits "None".
  // Comparing raw strings held EVERY typed answer at phase 3 — the guard was rejecting
  // the matcher's own successful mapping. So accept the same equivalence tiers the
  // matcher uses to COMMIT a value (exact after canon, whole-word either way), and
  // nothing looser: token-overlap fuzziness has no place in a submit gate.
  // (Phone equivalence and the tier list now live in module-level sameAnswer, shared with
  // the post-write read-back so both judge "is this the same answer?" identically.)
  const equiv = sameAnswer;
  const now = group(current);
  const out = [];
  for (const [key, { label, values }] of group(approved)) {
    const want = values.filter(Boolean);  // an approved blank promises nothing
    if (!want.length) continue;
    const have = (now.get(key) || { values: [] }).values.filter(Boolean);
    if (!have.length) { out.push(`${label}: missing now`); continue; }
    // Every value he approved under this label must still be somewhere under it.
    for (const v of want) {
      if (!have.some(h => equiv(v, h))) {
        out.push(`${label}: approved "${v}", now ${have.map(h => `"${h}"`).join(" / ")}`);
      }
    }
  }
  return out;
}

// Labels of every visible REQUIRED field that is currently EMPTY in the DOM — the
// end-of-form backstop. Reads true committed state, so it catches a required field that was
// filled then cleared (Ashby's resume parser wiping phone/email) or dropped for any reason,
// letting the review warn "still empty (required)" so Parth can hold instead of submitting a
// form that will bounce. Mirrors reviewValueSummary's field walk, inverted to empties.
function emptyRequiredLabels(adapter) {
  const out = [];
  const seen = new Set();
  const add = (label) => {
    const l = String(label || "").replace(/[✱*]/g, "").trim();
    if (l && !seen.has(l)) { seen.add(l); out.push(l); }
  };
  // radio/checkbox groups: required but nothing checked
  const grouped = new Set();
  for (const g of collectChoiceGroups(adapter)) {
    g.inputs.forEach(i => grouped.add(i));
    if (!g.inputs.some(i => isRequired(i, adapter))) continue;
    if (!g.inputs.some(i => i.checked)) add(questionLabel(g, adapter) || labelFor(g.inputs[0]));
  }
  // lone radios (group collapsed to one visible option): same required-but-unchecked test
  for (const el of document.querySelectorAll('input[type="radio"]')) {
    if (grouped.has(el) || el.disabled || el.offsetParent === null) continue;
    if (isRequired(el, adapter) && !el.checked) add(fieldLabel(el, adapter) || el.name);
  }
  // required file inputs with nothing attached (cover letter). No offsetParent test:
  // uploaders hide the real input behind a dropzone, so hidden IS the normal state.
  for (const el of document.querySelectorAll('input[type="file"]')) {
    if (!isRequired(el, adapter)) continue;
    if (!(el.files && el.files.length)) add(fieldLabel(el, adapter) || "File upload");
  }
  // Ashby Yes/No widgets: required but no active button
  for (const g of collectYesNoGroups(adapter)) {
    if (!isRequiredByTitle(g.container, adapter)) continue;
    const active = g.buttons.some(b => adapter.yesNo && b.matches(adapter.yesNo.committed));
    if (!active) add(yesNoQuestion(g.container, adapter));
  }
  // text / textarea / native select / combobox: required but empty
  for (const el of document.querySelectorAll("input, select, textarea")) {
    const t = (el.type || "").toLowerCase();
    if (["hidden", "submit", "button", "file", "password", "radio", "checkbox"].includes(t)) continue;
    if (el.offsetParent === null || !isRealField(el)) continue;
    if (!isRequired(el, adapter)) continue;
    const empty = el.tagName.toLowerCase() === "select"
      ? (!el.value || /^select\.*\.?$/i.test((el.options[el.selectedIndex] || {}).text || ""))
      : !(el.value && el.value.trim());
    if (empty) add(fieldLabel(el, adapter));
  }
  return out;
}

// Best-effort job facts for the Telegram header (ATS-agnostic; include only what's found).
function jobInfo() {
  const info = {};
  const text = (document.body && document.body.innerText) || "";
  const comp = text.match(/\$\s?[\d.,]+\s*[KkMm]?\s*[–\-—to]+\s*\$?\s?[\d.,]+\s*[KkMm]?/);
  if (comp) info.compensation = comp[0].replace(/\s+/g, " ").trim();
  const emp = text.match(/\b(Full[\s-]?time|Part[\s-]?time|Contract(?:or)?|Internship|Intern|Temporary|Freelance)\b/i);
  if (emp) info.employment_type = emp[0];
  return info;
}

// ── main ─────────────────────────────────────────────────────────────────────
// autoSubmit: true when the background orchestrator drives this fill (it will submit on
// ✅). false for the manual popup "Fill this application" path (Parth taps submit himself).
// Only affects the review message wording; fill.js never submits either way.
// A per-field id derived from its LABEL + CONTROL KIND, not from the order fields happen
// to come up in. This is load-bearing for the batch: phase 1 collects the questions and
// phase 3 re-fills the same form minutes-to-hours later, and Ashby's hydration race drops
// a different random field on each run. A sequence number ("ask_0") would silently shift
// under that race and land Parth's essay on the wrong control; a label hash cannot.
// The kind is folded into the hash because one label can cover two DIFFERENT controls
// (Ashby's compound PhoneField labels both a tel input and a consent radio group "Phone").
// With label-only hashing their ids were `_2`-suffixed in ENCOUNTER ORDER — and if the
// escalated set differed between the phases, an answer landed on the wrong control.
// Same-label same-kind duplicates remain order-suffixed (accepted residual risk).
// FNV-1a, 8 hex chars — short enough for Telegram's 64-byte callback_data budget.
// NOTE: changing the hash input invalidates the ids inside pre-2026-07-18 pending
// records — their stored answers won't apply on a phase-3 refill (fields re-ask; nothing
// wrong is ever written). Clear bridge/pending/ when deploying this.
function askId(label, kind, used) {
  const s = String(label || "").toLowerCase().replace(/\s+/g, " ").trim() + "|" + String(kind || "");
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  const base = "q" + (h >>> 0).toString(16);
  let id = base, n = 2;
  while (used.has(id)) id = `${base}_${n++}`;  // same label+kind twice on one form
  used.add(id);
  return id;
}

/**
 * @param {boolean} autoSubmit  background will submit after a ✅ (live queue path)
 * @param {object}  opts
 *   mode: "live"     ask Telegram now and wait      (single-pass queue path)
 *         "panel"    hand the questions to the on-page panel; Telegram stays silent
 *         "collect"  PHASE 1 — gather questions, ask nobody, never submit
 *         "apply"    PHASE 3 — write Parth's stored answers in, then submit
 *   answers:  {askId: value}  (mode "apply")
 *   approved: [{label,value}] the summary he approved (mode "apply") — diffed before submit
 *   company:  the queue's display name, when the caller knows it. Overrides the URL guess.
 */
async function runFill(autoSubmit = false, opts = {}) {
  const mode = opts.mode || "live";
  traceStart();
  // `optional` = fields the form does NOT require and no rule recognized. Collected in panel
  // mode only, so Parth can open them on demand; the queue modes still skip them silently
  // (they'd be noise on Telegram and would drag every unattended fill into a human wait).
  const report = { filled: [], skipped: [], needsInput: [], flagged: [], optional: [], errors: [], mode };
  // Fields the deterministic pass couldn't resolve, to send to Telegram at the page
  // boundary. Each carries an `apply(value)` closure that writes Parth's answer back to
  // its own control. Items are ALSO pushed to a report bucket so the popup shows them even
  // if the bridge is down. `bucket` = "needsInput" | "flagged".
  const asks = [];
  const usedIds = new Set();
  // item.kind = the control shape ("core"/"group"/"yesno"/"select"/"combobox"/"text"),
  // folded into the ask id so two same-labeled fields of different kinds can't swap
  // answers between phase 1 and phase 3 (see askId).
  const escalate = (bucket, item, apply) => {
    // askId still hashes the ORIGINAL label: it is the identity that has to survive between
    // phase 1 collecting a question and phase 3 landing the answer on that same field, and
    // re-keying it here would strand every answer already stored against the old id.
    const id = askId(item.label || item.key, item.kind, usedIds);
    item._askId = id;
    // What Parth READS, though, must be answerable. See readableLabel: a control whose
    // label is really its id reaches here as "end-month--0".
    const shown = readableLabel(item.label || item.key);
    asks.push({ id, label: shown, why: item.why, draft: item.draft, answer_key: item.answer_key, required: item.required, options: item.options, optional: bucket === "optional", apply });
    report[bucket].push({ ...item, label: shown });
  };
  try {
    const deps = await loadDeps();
    const { profile, tax } = await loadData();
    // dig/render are only read by the Workday branch below, but they must be destructured
    // here with the rest: the branch builds one ctx object literal, so a name missing from
    // this line is a ReferenceError that kills the whole fill before it touches the page.
    const { adapters, classify, dig, render, pickResume, attachResume, resumeFilename } = deps;

    const adapter = adapters.find(a => a.urlPatterns.some(re => re.test(location.href)));
    if (!adapter) { report.errors.push("no adapter matched this URL"); return report; }
    report.adapter = adapter.id;

    // Serializable submit descriptor for the background orchestrator. fill.js NEVER
    // clicks submit itself — after the review ✅, background clicks it (a trusted CDP
    // click on Greenhouse) and detects the confirmation page. Returning this in the
    // report (which is sent BEFORE any submit/navigation) is how background learns how.
    report.submitMeta = adapter.submit ? {
      selectors: adapter.submit.selectors || [],
      textMatch: adapter.submit.textMatch || "submit",
      needsCDP: !!adapter.submit.needsCDP,
      confirm: adapter.submit.confirm || { urlIncludes: [], textIncludes: [] },
    } : null;

    // Company name for the Telegram header (so Parth knows which application the questions
    // belong to) AND for the drafter's why_company context — a wrong name here ships a
    // wrong name inside the essay, so prefer the authoritative one.
    // opts.company is the queue's own display name, passed down by background.js whenever
    // the fill was driven from a queue/pending record. Only when nobody told us (the manual
    // popup path, /apply <url>) do we guess it from the URL — see pageCompany.
    const company = (opts.company || "").trim() || pageCompany();
    report.company = company;
    report.url = location.href;

    // Workday is a 6-step wizard on a URL that never changes, so everything below — one
    // page, one pass, one report — does not apply to it. Hand the whole flow to the step
    // driver, which reuses these same primitives (CDP, classify, attachResume) so there is
    // still one implementation of each. See workday_driver.js.
    if (adapter.multiStep) {
      const { runWorkday } = await import(chrome.runtime.getURL("workday_driver.js"));
      const { resolveStream } = await import(chrome.runtime.getURL("stream.js"));

      // Pick the resume track FIRST, then resolve the profile through that same tag. The
      // four resumes describe the same jobs in four vocabularies, so filling from the raw
      // profile would type quant wording onto a form carrying the AI PDF.
      // Carry the outgoing filename ON the track: the Workday driver attaches through
      // the same `track` object several steps later, and one resolved name keeps the
      // attach, the report and the Telegram summary all saying the same thing.
      const picked = pickResume(opts.field || report.company || "", opts.jd || "", profile);
      const track = { ...picked, send_as: resumeFilename(picked, profile) };
      // Bind the bridge into the attach helper once. The Workday driver attaches from
      // several places and must not have to know where the bytes come from.
      const attachWithBridge = (el, t) => attachResume(el, t || track, { rpc, profile });
      const streamed = resolveStream(profile, track.tag);

      const wd = await runWorkday({
        adapter, tax, classify, dig, render, attachResume: attachWithBridge, rpc, bringIntoView,
        // Read the flag through a function, not by value: this run started before Stop was
        // pressed, so anything captured up front is false forever.
        shouldStop: () => PANEL_STOP,
        profile: streamed, track, company,
        tenant: (location.hostname.match(/^([a-z0-9-]+)\.wd\d+\./i) || [])[1] || null,
      });
      Object.assign(report, wd);
      // The SAME key the single-page path uses (see resumeChoice below). One name for one
      // fact: logApplied reads exactly one place to record which CV an application used,
      // and a Workday-only alias would be the one it silently missed.
      report.resumeChoice = { tag: track.tag, file: track.send_as, overridden: false };

      // Log here too. The single LOG_REPORT at the bottom of runFill is past the early
      // return above, so without this the Workday path writes NOTHING to
      // fill_reports.jsonl — and since the adapter needs the debugger, that log is the
      // only way to see what a run did without stealing the tab's CDP client from it.
      try { await rpc({ type: "LOG_REPORT", report }); } catch (_) { /* bridge down: skip */ }
      return report;
    }

    // Attach the debugger ONLY when the adapter's dropdowns need trusted clicks
    // (Greenhouse). Lever/Ashby honor synthesized events, so no debugger banner appears.
    if (adapter.needsCDP) {
      const att = await rpc({ type: "CDP_ATTACH" });
      CDP_READY = !!(att && att.ok);
      if (!CDP_READY) report.errors.push(`debugger attach failed: ${att && att.error} — dropdowns can't be filled. Close DevTools on this tab and retry.`);
    }

    const handled = new Set();

    // 1) core fields — direct fill from profile paths
    for (const f of adapter.coreFields) {
      if (f.control === "file") {
        // Resume is attached below. Any OTHER file field (cover letter) isn't automated —
        // but a present one must not be invisible: a REQUIRED cover-letter upload used to
        // surface nowhere and the submit just failed late with no explanation.
        if (f.attach !== "resume") {
          const fileEl = firstMatch(f.selectors);
          if (fileEl && !(fileEl.files && fileEl.files.length)) {
            report.skipped.push({ key: f.key, why: isRequired(fileEl, adapter)
              ? "file upload REQUIRED — attach it by hand before submitting"
              : "file upload not automated — attach by hand if you want one" });
          }
        }
        continue;
      }
      const el = firstMatch(f.selectors);
      if (!el) {
        // React Greenhouse has no free-text location field (country arrives as its own
        // question), so a missing `location` control there is expected, not a failure.
        const silent = f.optional ||
          (f.key === "location" && adapter.isReactFrontend && adapter.isReactFrontend());
        if (!silent) report.skipped.push({ key: f.key, why: "control not found (VERIFY selector)" });
        continue;
      }
      const val = deps.dig(profile, f.profilePath);
      const rendered = deps.render(val);
      // Claim the control regardless of outcome so the label-classify loop below doesn't
      // re-report an unset core field (e.g. github/portfolio FILL_ME) a second time.
      handled.add(el);
      // FILL_ME = Parth still needs to provide it -> surface. An explicitly empty value
      // (e.g. portfolio he doesn't have) = intentionally blank -> skip silently, don't nag.
      if (val === "FILL_ME") {
        escalate("needsInput", { key: f.key, label: labelFor(el) || f.key, why: "profile value unset", required: !f.optional && isRequired(el, adapter), kind: "core" },
          (v) => applyValue(el, v, adapter));
        continue;
      }
      if (!rendered) { report.skipped.push({ key: f.key, why: "intentionally blank" }); continue; }
      // Phone: drop the country code when an intl-tel-input country selector is present.
      const value = f.key === "phone" ? normalizePhone(el, rendered) : rendered;
      const r = await applyValue(el, value, adapter);
      // r.value = what the control actually holds when a setter had to substitute (a school
      // that isn't in the ATS's list committing as "Other"). Report that, not what we asked for.
      (r.ok ? report.filled : report.skipped).push({ key: f.key, value: r.value || value, ...(r.ok ? {} : { why: r.why || "setter failed" }) });
    }

    // 2) resume attach — pick best-of-4 from the role title
    const roleTitle = pageRole();
    report.role = roleTitle;
    const resumeField = adapter.coreFields.find(f => f.control === "file" && f.attach === "resume");
    const resumeInput = resumeField ? firstMatch(resumeField.selectors) : null;
    if (resumeInput) {
      // Parth's per-job /resume override wins over the best-of-4 pick. If he pinned a tag
      // that no longer exists in profile.json (bridge/extension profile drift), DON'T
      // silently fall back to the auto pick and submit the wrong resume unattended:
      // attach nothing, flag it, and force the review to HOLD so a human looks.
      let track = null;
      if (opts.resumeTag) {
        track = (profile.resume_tracks || []).find(t => t.tag === opts.resumeTag) || null;
        if (!track) {
          report.resumeOverrideBad = opts.resumeTag;
          report.errors.push(`resume override "${opts.resumeTag}" is not a known track — attached nothing, holding`);
        }
      }
      if (!track && !opts.resumeTag) track = pickResume(roleTitle, "", profile);
      if (track) {
        track = { ...track, send_as: resumeFilename(track, profile) };
        // Surface which resume is going out (name + track) in the end-of-form review.
        report.resumeChoice = { tag: track.tag, file: track.send_as,
                                overridden: !!opts.resumeTag && track.tag === opts.resumeTag };
        try {
          const { attached, source } = await attachResume(resumeInput, track, { rpc, profile });
          handled.add(resumeInput);
          // Record where the bytes came from. "bundle" means the bridge was unreachable and
          // the attach used the extension's build-time copy, which can be behind.
          report.resumeChoice.source = source;
          (attached ? report.filled : report.skipped).push(
            { key: "resume", value: `${track.tag} (${track.send_as}, ${source})` });
        } catch (e) { report.errors.push(`resume attach: ${e.message}`); }
      }
    } else {
      report.skipped.push({ key: "resume", why: "file input not found (VERIFY selector)" });
    }

    // 3) radio/checkbox OPTION GROUPS — classify the question, click the matching option
    for (const group of collectChoiceGroups(adapter)) {
      const label = questionLabel(group, adapter) || labelFor(group.inputs[0]);
      group.inputs.forEach(i => handled.add(i));
      if (!label) { report.skipped.push({ label: "(no label)", why: "choice group unlabeled -> Tier 1" }); continue; }
      const c = classify(label, /* company */ null, profile, tax);
      const groupOptions = group.inputs.map(i => optionText(i, adapter));
      const req = group.inputs.some(i => isRequired(i, adapter)) || /\*|✱/.test(label);
      // Writes Parth's Telegram answer back by matching it to one of the group's options.
      // Re-points at the live inputs first and confirms the option actually went checked:
      // this closure runs long after the scan (he answers in the panel minutes later, or
      // phase 3 replays a stored answer into a freshly loaded page), by which time the
      // captured radios may be detached — and clicking a detached radio does nothing at all
      // while the old code still reported success.
      const applyGroup = async (v) => {
        const live = group.inputs.map(i => liveField(i, adapter)).filter(Boolean);
        if (!live.length) return { ok: false, why: "that question is no longer on the page — re-run Fill" };
        const opts = live.map(i => ({ textContent: optionText(i, adapter), input: i }));
        const hit = matchOption(opts, v);
        if (!hit) return { ok: false, why: `no option matched "${v}"` };
        clickOption(hit.input);
        await sleep(60);
        if (!hit.input.checked) return { ok: false, why: "clicked the option but it did not stick" };
        return { ok: true, value: hit.textContent };
      };
      if (c.bucket !== "deterministic") {
        if (c.bucket === "draftable") escalate("needsInput", { label, why: `draftable -> ${c.answer_key} (Tier 2)`, answer_key: c.answer_key, required: req, options: groupOptions, kind: "group" }, applyGroup);
        else if (c.bucket === "always_ask") escalate("needsInput", { label, why: "always_ask", required: req, options: groupOptions, kind: "group" }, applyGroup);
        // Unrecognized: skip optional ones (genuine Tier 1), but never silently drop a
        // REQUIRED field — surface it so it can't block submit unnoticed.
        else if (req) escalate("needsInput", { label, why: "unrecognized required field", required: true, options: groupOptions, kind: "group" }, applyGroup);
        else report.skipped.push({ label, why: "no rule matched -> Tier 1" });
        continue;
      }
      if (!c.filled) { escalate("needsInput", { label, why: `profile ${c.value_key} unset`, required: req, options: groupOptions, kind: "group" }, applyGroup); continue; }
      const items = group.inputs.map(i => ({ textContent: optionText(i, adapter), input: i }));
      const pick = matchFirstCandidate(items, c);
      if (!pick) { escalate("flagged", { label, value: c.rendered, why: `no option matched (options: ${items.map(o => o.textContent).slice(0, 6).join(" | ")})`, required: req, options: groupOptions, kind: "group" }, applyGroup); continue; }
      clickOption(pick.hit.input);
      await sleep(60);
      // Same read-back the yes/no path has always done: a click that didn't take must not
      // be recorded as filled, or the end-of-form review vouches for an empty question.
      const stuck = pick.hit.input.checked;
      (stuck ? report.filled : report.flagged).push({ label, value: pick.hit.textContent,
        ...(pick.viaAlternate ? { via: pick.want } : {}), ...(stuck ? {} : { why: "clicked but did not commit" }) });
    }

    // 3a) LONE radios: a radio whose siblings are hidden forms no group (needs >= 2
    // visible) and the per-control loop excludes radios entirely — so it was dropped
    // with no trace, even when required. We still can't fill a one-option question
    // safely, but it must at least show up in the report (and in the required-empty
    // warnings via emptyRequiredLabels) instead of vanishing.
    for (const el of document.querySelectorAll('input[type="radio"]')) {
      if (handled.has(el) || el.disabled || el.offsetParent === null) continue;
      handled.add(el);
      report.skipped.push({ label: fieldLabel(el, adapter) || el.name || "(radio)",
        why: "lone radio option (rest of the group hidden?) — Tier 1" });
    }

    // 3b) Ashby Yes/No button widgets (hidden checkbox behind two <button>s) — classify the
    // question and click the matching button; a synthesized click commits (checkbox flips +
    // button gains an _active class). Without this these questions are silently dropped.
    for (const g of collectYesNoGroups(adapter)) {
      const label = yesNoQuestion(g.container, adapter);
      const sel = (adapter.questionContainerSelectors || []).join(",");
      const cb = (sel ? g.container.closest(sel) : g.container.parentElement)?.querySelector('input[type="checkbox"]');
      if (cb) handled.add(cb);
      if (!label) { report.skipped.push({ label: "(no label)", why: "yes/no widget unlabeled -> Tier 1" }); continue; }
      const c = classify(label, /* company */ null, profile, tax);
      const ynOptions = g.buttons.map(b => cleanText(b));
      const req = (cb && isRequired(cb, adapter)) || isRequiredByTitle(g.container, adapter) || /\*|✱/.test(label);
      // Writes Parth's Telegram answer back by matching it to Yes / No and clicking.
      const applyYesNo = async (v) => {
        const opts = g.buttons.map(b => ({ textContent: cleanText(b), el: b }));
        const hit = matchOption(opts, v);
        if (!hit) return { ok: false, why: `no button matched "${v}"` };
        hit.el.click();
        await sleep(60);
        const ok = hit.el.matches(adapter.yesNo.committed) || (cb && cb.checked);
        return { ok, why: ok ? "" : "clicked but did not commit" };
      };
      if (c.bucket !== "deterministic") {
        if (c.bucket === "draftable") escalate("needsInput", { label, why: `draftable -> ${c.answer_key} (Tier 2)`, answer_key: c.answer_key, required: req, options: ynOptions, kind: "yesno" }, applyYesNo);
        else if (c.bucket === "always_ask") escalate("needsInput", { label, why: "always_ask", required: req, options: ynOptions, kind: "yesno" }, applyYesNo);
        else if (req) escalate("needsInput", { label, why: "unrecognized required field", required: true, options: ynOptions, kind: "yesno" }, applyYesNo);
        else report.skipped.push({ label, why: "no rule matched -> Tier 1" });
        continue;
      }
      if (!c.filled) { escalate("needsInput", { label, why: `profile ${c.value_key} unset`, required: req, options: ynOptions, kind: "yesno" }, applyYesNo); continue; }
      const items = g.buttons.map(b => ({ textContent: cleanText(b), el: b }));
      const pick = matchFirstCandidate(items, c);
      if (!pick) { escalate("flagged", { label, value: c.rendered, why: `no button matched (options: ${items.map(o => o.textContent).join(" | ")})`, required: req, options: ynOptions, kind: "yesno" }, applyYesNo); continue; }
      pick.hit.el.click();
      await sleep(60);
      const committed = pick.hit.el.matches(adapter.yesNo.committed) || (cb && cb.checked);
      (committed ? report.filled : report.flagged).push({ label, value: pick.hit.textContent,
        ...(pick.viaAlternate ? { via: pick.want } : {}), ...(committed ? {} : { why: "clicked but did not commit" }) });
    }

    // 4) every other control -> classify by label
    const controls = [...document.querySelectorAll("input, select, textarea")].filter(el => {
      if (handled.has(el)) return false;
      const t = (el.type || "").toLowerCase();
      if (["hidden", "submit", "button", "file", "password", "radio"].includes(t)) return false;
      if (el.disabled || el.offsetParent === null) return false; // skip hidden/disabled
      if (!isRealField(el)) return false; // drop react-select proxies / invisible inputs
      return true;
    });

    for (const el of controls) {
      const label = fieldLabel(el, adapter);
      if (!label) { report.skipped.push({ label: "(no label)", why: "unlabeled — Tier 1" }); continue; }
      const c = classify(label, /* company */ null, profile, tax);
      const applyCtl = (v) => applyValue(el, v, adapter);
      const req = isRequired(el, adapter);
      // Options for tappable Telegram buttons. Native <select> options are in the DOM;
      // react-select menus aren't until opened, so harvest them ONLY when escalating (lazy,
      // so deterministic-success fills don't needlessly open dropdowns). Combobox typeaheads
      // stay free-text. `optsFor()` is awaited only in the escalate branches below.
      const nativeOpts = el.tagName.toLowerCase() === "select"
        ? [...el.options].map(o => o.text.trim()).filter(t => t && !/^select\.*\.?$/i.test(t))
        : undefined;
      const optsFor = async () => nativeOpts || await harvestOptions(el, adapter);
      const ctlKind = el.tagName.toLowerCase() === "select" ? "select"
        : (el.getAttribute && el.getAttribute("role")) === "combobox" ? "combobox" : "text";
      // Greenhouse education date rows, decided by field id BEFORE the taxonomy sees the
      // label — see eduDateField(). Must come first: always_ask carries `start date` and is
      // matched ahead of everything, which is exactly what parked these fields.
      const eduDate = eduDateField(el, document);
      if (eduDate) {
        const parts = splitMonthYear(render(dig(profile, eduDate.profileKey)));
        const want = parts && parts[eduDate.part];
        if (!want) {
          // Half a date in the profile: ask for the missing half rather than guessing a
          // month nobody wrote down.
          escalate("needsInput", { label, why: `profile ${eduDate.profileKey} has no ${eduDate.part}`, required: req, options: await optsFor(), kind: ctlKind }, applyCtl);
          continue;
        }
        // The report label falls back to the id when the page has not wired the label yet,
        // which is unreadable in a fill report — name the field by what it is instead.
        const nice = /--\d+$/.test(label) ? `${eduDate.profileKey.split(".")[1]} (${eduDate.part})` : label;
        const r = await applyValue(el, want, adapter);
        if (r.ok) report.filled.push({ label: nice, value: r.value || want });
        else escalate("flagged", { label, value: want, why: r.why || "setter failed", required: req, options: await optsFor(), kind: ctlKind }, applyCtl);
        continue;
      }

      // A CONDITIONAL FOLLOW-UP WHOSE TRIGGER DID NOT FIRE. Live on Robinhood:
      // the deterministic pass filled "Will you now (or in the future) require visa
      // sponsorship? -> No", and this field ("If you answered "Yes" to the above question,
      // please provide additional information here:") went to the drafter, which had only
      // that label to work from and wrote a confident paragraph about being on OPT and
      // needing H1B support — the opposite of what the same form said two fields earlier.
      // A second one invented a non-compete with the current employer.
      //
      // The cure is not a better prompt. The question genuinely does not apply, so nothing
      // should be written about it at all. Checked BEFORE the bucket dispatch so it covers
      // the draftable, always_ask and unrecognized paths alike; deterministic is excluded
      // deliberately, since a profile answer is correct whatever the question above said.
      // Required ones take "N/A" (the honest human answer, and it unblocks submit); optional
      // ones are left alone. If the parent said YES, or we cannot find it, nothing changes.
      if (c.bucket !== "deterministic" && FOLLOW_UP.test(label)) {
        const parent = precedingAnswer(el, adapter);
        if (parent && isNegative(parent.value)) {
          const why = `follow-up to "${String(parent.label).slice(0, 40)}", answered ${parent.value}`;
          if (req) {
            const r = await applyValue(el, "N/A", adapter);
            (r.ok ? report.filled : report.flagged).push({ label, value: "N/A",
              ...(r.ok ? { via: why } : { why: r.why || "setter failed" }) });
          } else {
            report.skipped.push({ label, why });
          }
          continue;
        }
      }
      if (c.bucket === "deterministic") {
        if (!c.filled) { escalate("needsInput", { label, why: `profile ${c.value_key} unset`, required: req, options: await optsFor(), kind: ctlKind }, applyCtl); continue; }
        // A value_key that is deliberately "" (links.portfolio, links.twitter — Parth has
        // neither) used to be written anyway: setText("") returns ok, so the report logged
        // a successful fill of an empty string and could clear a field the ATS prefilled.
        // Skip the write, but do NOT escalate — surfacing these on every form is exactly
        // what the empty string is there to avoid.
        const tries = answerCandidates(c);
        if (!tries.length) { report.skipped.push({ label, why: `profile ${c.value_key} is blank on purpose` }); continue; }
        let r = null, used = null;
        for (const want of tries) {
          r = await applyValue(el, want, adapter);
          if (r.ok) { used = want; break; }
        }
        if (r && r.ok) report.filled.push({ label, value: r.value || used, ...(used !== tries[0] ? { via: used } : {}) });
        else escalate("flagged", { label, value: c.rendered, why: (r && r.why) || "setter failed", required: req, options: await optsFor(), kind: ctlKind }, applyCtl);
      } else if (c.bucket === "draftable") {
        escalate("needsInput", { label, why: `draftable -> ${c.answer_key} (Tier 2)`, answer_key: c.answer_key, required: req, options: await optsFor(), kind: ctlKind }, applyCtl);
      } else if (c.bucket === "always_ask") {
        escalate("needsInput", { label, why: "always_ask", required: req, options: await optsFor(), kind: ctlKind }, applyCtl);
      } else if (req) {
        // ALREADY ANSWERED ON THE PAGE. Greenhouse's resume parser fills the education
        // block (school, degree, discipline, the end date) before we ever scan it, and none
        // of those match a taxonomy rule, so they came through here and Parth was asked to
        // type an answer into a box that visibly already had one. Live on Robinhood: two
        // questions, "End date month" and "End date year", both sitting in the summary with
        // values while the console showed them as still needing an answer.
        //
        // An unrecognized field means we do not know what SHOULD go there — it does not
        // mean the form is empty. When the control already holds a committed value, record
        // it instead of asking. The value is not taken on trust: it rides into the ✅
        // summary like every other field, which is the gate that exists for exactly this.
        // `via` marks where it came from so the report never implies we chose it.
        const already = currentValue(el, adapter);
        if (already) {
          report.filled.push({ label, value: already, via: "already on the form" });
          continue;
        }
        escalate("needsInput", { label, why: "unrecognized required field", required: true, options: await optsFor(), kind: ctlKind }, applyCtl);
      } else if (mode === "panel") {
        // Optional and unrecognized: the form will submit without it, but Parth is sitting
        // right here and may want it anyway (an optional "what excites you" essay is worth
        // answering). Collect it with its setter so the panel can offer it behind a
        // disclosure — never opened by default, so a fill he doesn't care about is unchanged.
        // `nativeOpts` only: harvesting a react-select's options costs a CDP menu open per
        // field, which isn't worth paying on every optional control before he's asked to see
        // one. He still gets a text box, and the dropdown's own options once he opens a card.
        escalate("optional", { label, why: "optional — the form doesn't require it", required: false, options: nativeOpts, kind: ctlKind }, applyCtl);
      } else {
        report.skipped.push({ label, why: "no rule matched -> Tier 1" });
      }
    }

    // 4a) react-select reconciliation — a react-select that already holds a committed value
    // collapses its search input to a ~1px cursor, so isRealField() drops it and the
    // input-based scans above (sections 1 & 4) never see it. The chip is still in the DOM,
    // so reviewValueSummary reports the value to Parth — but report.filled/skipped/flagged
    // never learned about it, undercounting the fill (the Affirm `id="race"` -> "Asian"
    // field, distinct from the "race/ethnicity" one, was the case that surfaced this: it
    // showed on the review yet the safety-net accounting couldn't see it). Walk the
    // committed chips directly (same source reviewValueSummary uses) and record any not
    // already claimed by label, so the report matches what will actually be submitted.
    // fieldKey, not norm: `handled` alone is not enough to tell "already recorded" here.
    // It holds the nodes captured during the scan, and Greenhouse REPLACES the education
    // block's nodes when the resume parses — so the chip's live input is a different object
    // than the one we filled, `handled` misses it, and the label comparison is all that is
    // left. That comparison then failed too, because the scan recorded those fields under
    // their ids. Both halves had to be wrong to produce a duplicate, and both were.
    const recordedLabels = new Set(
      [...report.filled, ...report.skipped, ...report.flagged, ...report.needsInput]
        .map(i => fieldKey(i.label || i.key || "")).filter(Boolean));
    for (const ctl of document.querySelectorAll(".select__control")) {
      const chips = [...ctl.querySelectorAll(".select__single-value, .select__multi-value")];
      if (!chips.length) continue;
      const inp = ctl.querySelector("input");
      if (inp && handled.has(inp)) continue;          // a core field already owns it
      const label = inp ? fieldLabel(inp, adapter) : "";
      if (label && recordedLabels.has(fieldKey(label))) continue; // already in the report
      const value = chips.map(cleanText).join(", ");
      report.filled.push({ label: label || "(unlabeled dropdown)", value });
    }
    traceStep("fill", report.filled.map(f => f.label || f.key));

    // 5) page-boundary escalation — send every unresolved field to Telegram and apply the
    // answers. Runs INSIDE the try so a Greenhouse react-select answer can still use the
    // (still-attached) debugger. Best-effort: if the bridge is down, the fields stay in
    // needsInput/flagged for Parth to handle manually and we note the error.
    const serializeAsk = a => ({
      id: a.id, label: a.label, why: a.why,
      ...(a.draft ? { draft: a.draft } : {}),
      // The bridge resolves answer_key -> answer_bank.yaml to offer a draft. The
      // extension can't do it itself: the bank lives on disk, not in the bundle.
      ...(a.answer_key ? { answer_key: a.answer_key } : {}),
      ...(a.options && a.options.length ? { options: a.options } : {}),
      ...(a.required ? { required: true } : {}),
    });
    const applyAnswer = (a, raw) => applyAskAnswer(report, a, raw);

    if (mode === "panel") {
      // Manual single-form fill: Parth answers in the page itself, so nobody is messaged
      // and nothing blocks here. Hand the live asks (apply closures and all) to the panel
      // and return — it applies each answer as he saves it.
      //
      // Not holding the debugger open across his typing is deliberate: MV3 would evict this
      // worker mid-think and detach it anyway. The CDP relays re-attach per click, so a
      // Greenhouse dropdown answered ten minutes from now still lands.
      PANEL_ASKS = asks;
      PANEL_REPORT = report;
      // Split, don't merge: the optional pile is a separate disclosure in the panel, and
      // `collected` must keep meaning "questions that actually need an answer".
      report.questions = asks.filter(a => !a.optional).map(serializeAsk);
      report.optionalQuestions = asks.filter(a => a.optional).map(serializeAsk);
      report.collected = report.questions.length;
    } else if (mode === "collect") {
      // PHASE 1: say what this job needs and get out. Nobody is waiting on Telegram —
      // Parth answers in his own time and phase 3 re-opens the form to apply them. The
      // tab closes right after this, so never park it waiting on a human.
      report.questions = asks.map(serializeAsk);
      report.collected = report.questions.length;
      // The free job facts (comp / type / location) for his Telegram card — scraped, not
      // written by a model. In the live path these ride along in `context` instead.
      report.jobInfo = jobInfo();
      // The scraped role title. The queue path already knows it (from the scraper), but the
      // ad-hoc /apply path has only what's on the page, so surface it for the collect record.
      report.title = roleTitle;
    } else if (mode === "apply") {
      // PHASE 3: he answered these already. Ids are label-derived, so they still match
      // even if this fill's field set came out different from phase 1's.
      let applied = 0;
      const wrote = [];
      for (const a of asks) if ((await applyAnswer(a, (opts.answers || {})[a.id])).ok) { applied++; wrote.push(a.label); }
      traceStep("stored answers", wrote);
      report.answered = applied;
      report.unanswered = asks.filter(a => !((opts.answers || {})[a.id] || "").trim())
                              .map(a => a.label);
    } else if (asks.length) {
      report.asked = asks.length;
      try {
        const items = asks.map(serializeAsk);
        const context = { company, role: roleTitle, url: location.href, ...jobInfo() };
        const res = await rpc({ type: "ASK_USER", items, context });
        if (!res || !res.ok) throw new Error((res && res.error) || "no response from the control plane");
        const answers = res.answers || {};
        let applied = 0;
        for (const a of asks) if ((await applyAnswer(a, answers[a.id])).ok) applied++;
        report.answered = applied;
      } catch (e) {
        report.errors.push("control plane: " + String((e && e.message) || e));
      }
    }

    // 6) end-of-form review gate — send Parth the full picture of what will be submitted
    // (plus any still-empty REQUIRED fields), and wait for his ✅/✋. The extension NEVER
    // submits; the tap only records his go-ahead. Skipped only if the fill produced nothing.
    if (report.filled.length) {
      try {
        // STATE RESYNC — the fix for Ashby's "field is visibly filled but submit says it's
        // missing" bug. Ashby re-initializes its React form state asynchronously after the
        // resume finishes uploading, which DISCARDS from state any value we set before that
        // point while the DOM keeps showing it — so the field looks filled but validation
        // (which reads state, not the DOM) reports it empty. The affected set varies per run
        // = a re-render race. By here the upload has long settled (the Telegram asks above
        // took human time, and there were none-or-few before this), so re-commit every
        // currently-shown value straight back into React state via a tracker-reset onChange.
        // This syncs state<-DOM uniformly for core AND custom fields with no re-derivation,
        // and re-clicking checked radios re-commits those too (safe: a radio can't untoggle).
        // Skipped in collect mode: phase 1 never submits, so there is no state to get right.
        // Also skipped when the adapter sets `stateResync: false`. That form keeps the values
        // it was given, so the resync only types every field a second time.
        if (mode !== "collect" && adapter.stateResync !== false) {
        await sleep(400);
        const retyped = [];
        for (const el of document.querySelectorAll("input, textarea")) {
          const t = (el.type || "text").toLowerCase();
          if (["hidden", "submit", "button", "file", "password", "radio", "checkbox"].includes(t)) continue;
          if (el.offsetParent === null || !isRealField(el)) continue;
          if (el.getAttribute && el.getAttribute("role") === "combobox") continue; // committed elsewhere
          if (!(el.value && el.value.trim())) continue;         // only resync fields that hold a value
          retyped.push(fieldLabel(el, adapter) || el.id);
          forceSetText(el, el.value);
        }
        for (const g of collectChoiceGroups(adapter)) {
          if (g.kind !== "radio") continue;                     // re-clicking a checkbox would toggle it off
          const checked = g.inputs.find(i => i.checked);
          if (checked) { retyped.push(questionLabel(g, adapter) || labelFor(checked)); clickOption(checked); } // re-fire onChange -> state, stays selected
        }
        // Ashby Yes/No button widgets: the pick lives in a hidden backing checkbox. If the
        // re-init cleared that checkbox (state lost) while the button still shows active (DOM),
        // re-click the active button to re-commit — but ONLY then, so a correctly-set one is
        // never toggled back off.
        for (const g of collectYesNoGroups(adapter)) {
          const active = g.buttons.find(b => adapter.yesNo && b.matches(adapter.yesNo.committed));
          if (!active) continue;
          const sel = (adapter.questionContainerSelectors || []).join(",");
          const cb = (sel ? g.container.closest(sel) : g.container.parentElement)?.querySelector('input[type="checkbox"]');
          if (cb && !cb.checked) { retyped.push(yesNoQuestion(g.container, adapter)); active.click(); }
        }
        traceStep("retype", retyped);
        }
        // PHASE 3 wipe-recovery from INTENT. The resync above only re-commits values the DOM
        // STILL shows; Ashby's re-init can clear a field outright (empty DOM), and that clear
        // can land AFTER the resync — which is how Affirm's two questions and Cloudflare's
        // country came out "missing now" and got held on the last run. So before we diff, wait
        // a beat for a late wipe and re-apply anything now empty straight from the approved
        // intent, across every control type (choices + text/select/combobox). Only fills
        // empties, so a value that survived is never touched; then diffApproved sees the
        // restored form. Unattended-only — the panel has its own post-fill reassert loop.
        if (mode === "apply") {
          for (let i = 0; i < 3; i++) {
            await sleep(1200);
            const wrote = [];
            const note = (m) => wrote.push(m);
            const again = reassertChoices(adapter, report, note)
                        + await reassertText(adapter, report, note);
            traceStep(`recheck ${i + 1}`, wrote);
            if (!again) break;
          }
        }
        // Read the whole form back from the DOM so Parth reviews EVERYTHING that will be
        // submitted (deterministic + escalated), not only the fields he answered.
        const summary = reviewValueSummary(adapter);
        // Kept on the report (not just sent to Telegram) so the audit dump captured right
        // before submit has the full field-value picture, not just report.filled/escalated.
        report.summary = summary;
        // Warn on required fields still unresolved: the ones we escalated AND anything the
        // DOM says is required-but-empty (a late resume-parser wipe, or any silent drop).
        const escalatedEmpty = [...report.needsInput, ...report.flagged]
          .filter(i => i.required).map(i => i.label || i.key);
        // Dedup on the label WITHOUT its required marker. emptyRequiredLabels strips ✱/*
        // as it collects and the escalated labels keep it, so a plain Set over the union
        // compared "Military status?*" against "Military status?" and kept both — every
        // escalated required field was warned about twice. Measured on a live record:
        // 4 of 17 warnings were twins of another 4.
        const warnKey = (s) => String(s || "").replace(/[✱*]/g, "").replace(/\s+/g, " ").trim();
        const warnings = [];
        const warnSeen = new Set();
        for (const w of [...escalatedEmpty, ...emptyRequiredLabels(adapter)]) {
          const k = warnKey(w);
          if (!k || warnSeen.has(k)) continue;
          warnSeen.add(k);
          warnings.push(k);
        }
        // Ride on the report too: in collect mode these used to be computed and thrown
        // away, so the ✅ summary couldn't warn about required fields the fill left empty.
        report.warnings = warnings;
        // autoSubmit rides in the context so the bridge can word the ✅ confirmation
        // ("submitting now…") vs the manual path ("tap Submit yourself"). context_lines
        // only renders the job-fact keys, so this extra key never shows in the header.
        const context = { company, role: roleTitle, url: location.href, ...jobInfo(), autoSubmit,
          ...(report.resumeChoice ? { resume: `${report.resumeChoice.file} (${report.resumeChoice.tag} track)` } : {}) };
        if (mode === "collect") {
          // Nothing to approve yet — phase 1 just reports. The summary rides back in the
          // report and becomes the `filled` list Parth sees on Telegram.
        } else if (mode === "apply") {
          // Phase 3 submits through the SAME submitWithRetry the panel uses (background
          // sends a SUBMIT message right after this fill). That loop reads PANEL_REPORT for
          // its Ashby choice-reassert pass, so seed it here — otherwise the unattended path
          // would miss the post-resume wipe recovery the manual path has always had.
          PANEL_REPORT = report;
          // He approved a SUMMARY, not this fill. This is a fresh fill of the same form
          // minutes-to-hours later, and Ashby's race means it can genuinely come out
          // different. Diff before we let background click anything.
          report.mismatch = diffApproved(opts.approved, summary);
          // A bad resume override is a hard hold even when every value matches — the
          // resume just isn't attached, and that must never go out unattended.
          report.reviewed = (report.mismatch.length || report.resumeOverrideBad) ? "hold" : "ok";
        } else if (mode === "panel") {
          // Parth is sitting in front of the form: he reviews it on screen and clicks the
          // panel's own Submit, so there's nothing to send anywhere. The summary still rides
          // back on the report for the audit log.
        } else {
          const res = await rpc({ type: "REVIEW_USER", summary, warnings, context });
          if (!res || !res.ok) throw new Error((res && res.error) || "no response from the control plane");
          report.reviewed = (res.answers && res.answers.__review) || null; // "ok" | "hold"
        }
      } catch (e) {
        report.errors.push("review: " + String((e && e.message) || e));
      }
    }
  } catch (e) {
    report.errors.push(String(e && e.stack || e));
  } finally {
    // Detach so the "debugging this browser" banner disappears before the user reviews
    // and taps submit. Their own submit tap needs no debugger.
    if (CDP_READY) { await rpc({ type: "CDP_DETACH" }); CDP_READY = false; }
  }
  // Persist this run so audits don't require live re-testing. Best-effort and
  // never blocks the popup/orchestrator from getting `report` back.
  report.trace = WRITE_TRACE.steps.slice();
  try { await rpc({ type: "LOG_REPORT", report }); } catch (_) { /* bridge down: skip */ }
  return report;
}

// ── on-page panel: Fill + answer + self-healing Submit ────────────────────────
// A floating control on the application page (replaces the popup for manual use). Fill runs
// the deterministic pass and renders whatever it couldn't resolve as question cards right
// here — Parth is already looking at the form, so routing him to his phone to answer it
// would be silly. Telegram stays out of the manual path entirely. Submit runs a retry LOOP
// that beats Ashby's DOM-vs-state desync race: click Submit (safe — an invalid form only
// shows errors, it never submits), read exactly which fields Ashby rejects, re-commit just
// those into React state, and click again — until the form is clean and actually submits.

let PANEL_STOP = false;
// The live asks (each still holding its apply closure) + the report they belong to, handed
// over by runFill in "panel" mode. Parth answers minutes later, long after runFill returned.
let PANEL_ASKS = null;
let PANEL_REPORT = null;

function currentAdapter(adapters) {
  return adapters.find(a => a.urlPatterns.some(re => re.test(location.href)));
}

// The submit button, by adapter selectors then a text match (mirrors background's finder).
function findSubmitButton(meta) {
  const bySel = (meta.selectors || []).map(s => { try { return document.querySelector(s); } catch (_) { return null; } }).find(Boolean);
  if (bySel && bySel.offsetParent !== null) return bySel;
  const re = new RegExp(meta.textMatch || "submit", "i");
  return [...document.querySelectorAll('button, input[type="submit"], a[role="button"]')]
    .find(b => b.offsetParent !== null && re.test(((b.innerText || b.value || "")).trim()));
}

// Field titles Ashby is currently complaining about. Each per-field error node reads
// "Missing entry for required field: <Title>" / "Invalid value: <Title>"; the concatenated
// summary node holds several at once, so keep only single-message nodes that map to a real
// field, which drops the summary automatically.
function readValidationErrors() {
  const titles = new Set();
  for (const el of document.querySelectorAll('[class*="_error"]')) {
    const txt = (el.textContent || "").trim();
    // skip the summary node (it strings multiple messages together)
    if ((txt.match(/missing entry|invalid value/gi) || []).length !== 1) continue;
    const m = txt.match(/(?:missing entry for required field|invalid value):\s*(.+)$/i);
    if (m) titles.add(m[1].trim());
  }
  return [...titles];
}

// Confirmation phrases visible on the page right now. The JD often shares the page with
// the form (Greenhouse React, Lever) and recruiting copy loves "we'll contact you" — so
// a phrase only counts as a confirmation if it was NOT already on the page before we
// started submitting (the caller snapshots a baseline set at loop start). Without the
// baseline, Submit could return "confirmed" off the JD text WITHOUT EVER CLICKING and
// log an application that never went out.
function matchedConfirmPhrases(adapter) {
  const t = ((document.body && document.body.innerText) || "").toLowerCase();
  const phrases = ["thank you for applying", "thanks for applying", "application submitted",
    "your application has been submitted", "application received", "successfully submitted",
    "successfully completed", "we'll contact you", "we will contact you", "application complete",
    ...(((adapter.submit || {}).confirm || {}).textIncludes || [])];
  return phrases.filter(p => t.includes(p));
}
function confirmationVisible(adapter, baseline) {
  return matchedConfirmPhrases(adapter).some(p => !baseline || !baseline.has(p));
}

// Re-commit into React state every field inside the field-entry whose title matches. Returns
// "fixed" (had a value, re-synced), "empty" (nothing to re-sync — a genuinely blank field the
// user must fill), or "notfound".
function resyncFieldByTitle(title, adapter) {
  const fe = [...document.querySelectorAll('[class*="_fieldEntry"], fieldset')].find(box => {
    const lbl = box.querySelector('[class*="_heading"], [class*="_label"], label, legend');
    return lbl && lbl.textContent.replace(/[✱*]/g, "").trim() === title;
  });
  if (!fe) return "notfound";
  let hadValue = false;
  for (const el of fe.querySelectorAll("input, textarea")) {
    const t = (el.type || "text").toLowerCase();
    // A file input that already holds our resume can still be flagged missing (Ashby's
    // store missed the original attach) — re-fire the uploader events on the same files.
    if (t === "file") {
      if (el.files && el.files.length) {
        hadValue = true;
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      }
      continue;
    }
    if (["hidden", "submit", "button", "password"].includes(t)) continue;
    if (t === "radio") {
      if (el.checked) { hadValue = true; clickOption(el); }   // re-fire onChange; a checked radio can't untoggle
      continue;
    }
    if (t === "checkbox") {
      // A single click on a CHECKED checkbox would toggle it OFF — that "fix" was
      // destroying a correct answer and then reporting the field empty. Re-commit by
      // clicking twice: off→on lands back in the checked state having fired the real
      // click→onChange path both times, which is what pushes it into React's store.
      if (el.checked) { hadValue = true; clickOption(el); clickOption(el); }
      continue;
    }
    if (el.getAttribute && el.getAttribute("role") === "combobox") { if (el.value && el.value.trim()) hadValue = true; continue; }
    if (el.value && el.value.trim()) { hadValue = true; forceSetText(el, el.value); }
  }
  // Ashby Yes/No widget: re-click the active button if its backing checkbox lost state.
  if (adapter.yesNo) {
    for (const c of fe.querySelectorAll((adapter.yesNo.containerSelectors || []).join(",") || "\0")) {
      const active = [...c.querySelectorAll(adapter.yesNo.option || "button")].find(b => b.matches(adapter.yesNo.committed));
      if (active) { hadValue = true; const cb = fe.querySelector('input[type="checkbox"]'); if (cb && !cb.checked) active.click(); }
    }
  }
  return hadValue ? "fixed" : "empty";
}

// A server-side REJECTION banner (not a validation error): the application was processed
// and refused — e.g. "You have reached your application limit". Seen live on a real board.
// Distinct from success AND from field errors; retrying is pointless.
// Same baseline rule as confirmations: JDs can contain "if you have already applied…",
// so only a phrase that appeared AFTER we started submitting counts.
const REJECT_PHRASES = ["couldn't submit your application", "could not submit your application",
  "application limit", "already applied", "unable to submit your application"];
function matchedRejectPhrases() {
  const t = ((document.body && document.body.innerText) || "").toLowerCase();
  return REJECT_PHRASES.filter(p => t.includes(p));
}
function submitRejectedText(baseline) {
  if (!matchedRejectPhrases().some(p => !baseline || !baseline.has(p))) return null;
  const el = [...document.querySelectorAll("div,section,p")].find(e =>
    e.offsetParent !== null && /couldn.?t submit|application limit|already applied|unable to submit/i.test(e.textContent) && e.textContent.length < 600);
  return el ? el.textContent.trim().replace(/\s+/g, " ").slice(0, 300) : "application was rejected by the ATS";
}

// Greenhouse (and some other ATSs) gate submit behind an EMAIL human-verification code: after
// the submit click the page shows "A verification code was sent to <email>… enter the
// 8-character code to confirm you're a human."
//
// We DETECT it here. Since 2026-08-14 we can also fill it — but only off the back of a human
// action (his Submit click, or the panel's 🔐 button), never on the queue's own initiative;
// see autoFillVerifyCode below. Detection stays the first step either way, and if no code can
// be found the behaviour is exactly what it always was: surface it, leave the tab open, park.
// Baseline-aware like confirmations/rejections (only text that appeared AFTER we started
// submitting counts, so a JD mention can't fire it).
const VERIFY_PHRASES = ["verification code was sent", "confirm you're a human", "confirm you are a human",
  "enter the 8-character code", "8-character code to confirm", "enter the code to confirm", "security code"];
function matchedVerifyPhrases() {
  const t = ((document.body && document.body.innerText) || "").toLowerCase();
  return VERIFY_PHRASES.filter(p => t.includes(p));
}
function submitVerificationText(baseline) {
  if (!matchedVerifyPhrases().some(p => !baseline || !baseline.has(p))) return null;
  const el = [...document.querySelectorAll("div,section,p,form")].find(e =>
    e.offsetParent !== null && /verification code|confirm you.?re a human|8-character code/i.test(e.textContent) && e.textContent.length < 600);
  return el ? el.textContent.trim().replace(/\s+/g, " ").slice(0, 300) : "email verification code required to submit";
}

// --- the verification code, when Parth has already acted ----------------------------
//
// Added 2026-08-14. The gate above is still an anti-bot control and we still never solve
// it on our own initiative — but when PARTH clicks Submit, finishing that submit is a
// decision he already made, and making him go dig an 8-character code out of Gmail is
// friction, not security. His rule, verbatim: "never an otp fetch without human
// explicitly asking for it or has submit."
//
// So every path into here starts at a human: the Submit click below, or the panel's
// 🔐 button. Nothing polls, and the queue never triggers this by itself.
//
// UNMEASURED, like the mail patterns in bridge/mailcode.py: no real code gate was open
// while this was written, so the field finder is a heuristic over attributes and shape
// rather than a selector read off a live form. It fails closed — no field found means the
// old behaviour, which is to tell Parth and leave the tab open.
const CODE_ATTR = /\b(code|otp|verification|verify|confirm)\b/i;

// A field whose name contains "code" is usually NOT the verification box. Greenhouse asks
// for a zip code on the same form, and "Zip Code" matches CODE_ATTR, so the emailed code
// was typed into the address block. Anything matching this is dropped from the candidate
// pool before either the name test or the shape fallback runs, because a 5-character zip
// field also passes the short-maxlength test.
const NOT_CODE_ATTR = /\b(zip|zipcode|postal|postcode|post ?code|area ?code|country ?code|dial ?code|phone|state|province|county|city|address|promo|coupon|discount|referral|requisition|req ?id)\b/i;

function findVerifyInput() {
  const inputs = [...document.querySelectorAll(
    'input[type="text"], input[type="tel"], input[type="number"], input:not([type])')]
    .filter(el => el.offsetParent !== null && !el.disabled && !el.readOnly);
  if (!inputs.length) return null;
  const attrs = el => [el.id, el.name, el.placeholder, el.getAttribute("aria-label"),
                       el.getAttribute("autocomplete"), labelFor(el)].filter(Boolean).join(" ");
  // Drop the address and phone fields first. Every test below, the name test and the shape
  // fallback alike, then runs over candidates that cannot be a zip code.
  const pool = inputs.filter(el => !NOT_CODE_ATTR.test(attrs(el)));
  if (!pool.length) return null;
  // The form's own answer, when it gives one. `autocomplete="one-time-code"` is the only
  // unambiguous signal on the page, so it outranks the heuristics.
  const declared = pool.find(el => /one-time-code/i.test(el.getAttribute("autocomplete") || ""));
  if (declared) return declared;
  // A code-ish name beats the shape tests: this is the signal that says "this field, not
  // the email field two rows up that is also a visible text input".
  const named = pool.filter(el => CODE_ATTR.test(attrs(el)));
  if (named.length === 1) return named[0];
  // Several code-ish names is either one group of single-character boxes, where the first
  // box is the right answer, or a genuine ambiguity. Guessing between separate fields is
  // how a live code landed in the zip box, so anything else fails closed.
  if (named.length > 1) {
    return named.every(el => el.getAttribute("maxlength") === "1") ? named[0] : null;
  }
  // A run of single-character boxes IS a code field, whatever it is called — no ordinary
  // application question is four or more inputs of maxlength 1 in a row. Checked before
  // the width heuristic because it is the shape actually seen on a live gate.
  const singles = pool.filter(el => el.getAttribute("maxlength") === "1");
  if (singles.length >= 4) return singles[0];
  // Otherwise fall back to shape — the gate says "8-character code", and a maxlength that
  // short is not something an ordinary application field carries.
  const short = pool.filter(el => {
    const n = parseInt(el.getAttribute("maxlength") || "0", 10);
    return n >= 4 && n <= 10;
  });
  // Only when it is UNAMBIGUOUS. Two candidates and we would be guessing which one the
  // code goes in, and a code typed into the wrong box is worse than asking him.
  return short.length === 1 ? short[0] : null;
}

// A code gate is often EIGHT BOXES, not one field.
//
// MEASURED on a live Greenhouse gate (Truveta, 2026-08): ids `security-input-0` through
// `security-input-7`, every one `maxlength="1"`, React-controlled, laid out as eight 50px
// squares. The group's label ("Security code") is associated with the FIRST box only,
// which is how findVerifyInput locates it — the ids themselves match nothing.
//
// The bug this fixes: assigning .value programmatically IGNORES maxlength, so the whole
// 8-character code went into box 0 and the other seven stayed empty. The finder was right;
// the writing was wrong.
function codeBoxGroup(input) {
  if (!input || input.getAttribute("maxlength") !== "1") return null;
  const m = (input.id || "").match(/^(.*?)(\d+)$/);
  if (!m) return null;
  const stem = m[1].replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const idx = el => parseInt((el.id.match(/(\d+)$/) || [0, "0"])[1], 10);
  const group = [...document.querySelectorAll('input[maxlength="1"]')]
    .filter(el => el.offsetParent !== null && !el.disabled && !el.readOnly
                  && new RegExp("^" + stem + "\\d+$").test(el.id))
    .sort((a, b) => idx(a) - idx(b));
  return group.length > 1 ? group : null;
}

// One character per box. Clearing first matters: a previous attempt may have stuffed the
// whole code into box 0, and React will not drop that on its own.
function fillCodeBoxes(boxes, code) {
  for (const b of boxes) if (b.value) setText(b, "");
  const chars = String(code).split("");
  for (let i = 0; i < chars.length && i < boxes.length; i++) setText(boxes[i], chars[i]);
  return boxes.map(b => b.value || "").join("");
}

// Ask the bridge for the code belonging to THIS submit. `since` is the click.
async function requestVerifyCode(since) {
  const r = PANEL_REPORT || {};
  try {
    return await rpc({ type: "VERIFY_CODE", since: Math.floor(since / 1000),
                       company: r.company || "", role: r.role || "", key: r.dedup_key || r.url || "" });
  } catch (_) { return { ok: false, code: null, why: "bridge unreachable" }; }
}

// Fetch + fill, once. Returns {status, code}.
//
// The STATUS tells "the code has not arrived yet" (worth waiting for) apart from "there is
// nowhere to put it" (never will be):
//   "filled" | "nofield" | "nocode" | "notconfigured" | "stale" | "mismatch"
//
// The CODE comes back on "filled" because the submit loop has to recognize its own code
// afterwards. The gate's wording stays on the page once the code is typed — it only clears
// when the server answers — so "is the gate still showing?" cannot distinguish "not
// answered yet" from "that code was rejected". Whether OUR code is still sitting in the box
// can. See verifyCodeStillShown.
async function autoFillVerifyCode(log, since, tried, opts = {}) {
  const quiet = !!opts.quiet;                 // suppress the repeat lines while polling
  const input = findVerifyInput();
  if (!input) { log("couldn't find the code box on this page — enter it yourself", "warn"); return { status: "nofield" }; }
  const res = await requestVerifyCode(since);
  if (!res || !res.ok || !res.code) {
    const why = (res && res.why) || "no code found";
    // "not configured" is a setup problem and "no code yet" is a timing one. Saying which
    // is the difference between him fixing it and him waiting for nothing — and only the
    // timing one is worth polling on.
    const setup = /not configured|unreachable/i.test(why);
    if (!quiet || setup) log(`🔐 no code from your inbox (${why})`, "warn");
    return { status: setup ? "notconfigured" : "nocode" };
  }
  if (tried.has(res.code)) {
    // Not a failure yet: the newest message may still be the PREVIOUS code, with this
    // submit's mail seconds behind it. Treat it as "not here yet" so the poll continues.
    if (!quiet) log("🔐 only the previous code is in the inbox so far — waiting", "warn");
    return { status: "stale" };
  }
  tried.add(res.code);

  const boxes = codeBoxGroup(input);
  if (boxes) {
    if (boxes.length < res.code.length) {
      log(`🔐 ${boxes.length} code boxes but a ${res.code.length}-character code — enter it yourself`, "warn");
      return { status: "mismatch" };
    }
    const landed = fillCodeBoxes(boxes, res.code);
    // Read back rather than trust the write: one char per box is a stronger claim than
    // "we typed something", and a React group that rejects a character fails silently.
    if (landed !== res.code) {
      log("🔐 the code didn't land cleanly across the boxes — check it before submitting", "warn");
      return { status: "mismatch" };
    }
  } else {
    setText(input, res.code);
  }
  // Never log the code itself: it is a live credential for this submit, and fill reports
  // are written to disk and read back for debugging.
  log(`🔐 filled the code from your inbox (${res.why})`, "ok");
  return { status: "filled", code: res.code };
}


// Is the code we typed still sitting in the gate's box?
//
// The question the submit loop cannot answer from the page text alone. After the code is
// filled and Submit is clicked again, the gate's wording stays put until the server
// answers, so a visible gate on the next pass means one of two opposite things:
//
//   code still in the box  → the page has not answered yet. Wait for the outcome.
//   box empty or changed   → the page cleared it, which is how these forms reject a code.
//                            Now a fresh fetch is worth doing.
//
// Fails towards WAITING: if the field cannot be found at all, that is not evidence the code
// was rejected, and re-fetching on no evidence is what burned the retry budget before.
function verifyCodeStillShown(code) {
  if (!code) return false;
  const input = findVerifyInput();
  if (!input) return true;
  const boxes = codeBoxGroup(input);
  const shown = boxes ? boxes.map(b => b.value || "").join("") : (input.value || "");
  return shown === code;
}


// Poll until the code arrives, or the budget runs out.
//
// THE BUG THIS REPLACES: the old code waited a flat 3s, asked once, and on a miss fell
// straight through to "needs_verification" — so its two-attempt cap was dead code and it
// gave up about three seconds after the gate appeared. The mail simply is not there yet
// that early: Greenhouse sends it on submit and it has to be delivered AND indexed.
//
// Polling is only affordable because the lookup got cheap. Measured on the real mailbox:
// a day of messages fetched in full cost 11.8s per attempt, and Gmail-side narrowing
// (X-GM-RAW, see mailcode.ImapReader._narrow) took that to ~1.6s.
// Returns THE CODE it filled, or "" — truthy either way for a caller that only wants to
// know whether to press on, and the value itself for the submit loop, which has to
// recognize its own code on the next pass (verifyCodeStillShown).
async function waitForVerifyCode(log, since, tried, budgetMs = 90000) {
  const deadline = Date.now() + budgetMs;
  let announced = false;
  for (;;) {
    const r = await autoFillVerifyCode(log, since, tried, { quiet: announced });
    // Only "not here yet" is worth waiting on. No field, no credentials, or a code that
    // would not fit are all permanent for this page — say so once and stop.
    if (r.status !== "nocode" && r.status !== "stale") return r.status === "filled" ? r.code : "";
    if (Date.now() >= deadline) {
      log("🔐 the code hasn't arrived — enter it from your inbox, then submit again", "warn");
      return "";
    }
    if (!announced) { log("🔐 waiting for the code to arrive in your inbox…"); announced = true; }
    await sleep(4000);
  }
}

// Click the submit button (trusted CDP click when the adapter needs it, else synthesized).
async function clickSubmit(adapter, log) {
  const btn = findSubmitButton(adapter.submit || {});
  if (!btn) { log("submit button not found", "err"); return false; }
  btn.scrollIntoView({ block: "center", behavior: "instant" });
  await sleep(60);
  if ((adapter.submit || {}).needsCDP) {
    const att = await rpc({ type: "CDP_ATTACH" });
    if (att && att.ok) {
      await rpc({ type: "CDP_CLICK", ...centerOf(btn) });
      await rpc({ type: "CDP_DETACH" });
    } else { btn.click(); }
  } else {
    btn.click();
  }
  return true;
}

// Ashby re-initializes its form state a few seconds after the resume upload parses,
// silently WIPING radio/checkbox/yes-no selections set before it (the "affected set varies
// every run" hydration race — same class as the text-field desync, except clickOption
// already reported the field filled). resyncFieldByTitle can't recover a wiped group
// (nothing is checked to re-fire), and an OPTIONAL wiped field never trips a validation
// error to trigger a retry. So before each submit, re-apply every deterministic choice
// answer from the report's intent to any group that currently has nothing selected.
// Guarded on "already selected" so a good answer is never toggled off. Returns the count.
function reassertChoices(adapter, report, log) {
  if (!report || !Array.isArray(report.filled)) return 0;
  const want = new Map(report.filled.map(f => [norm(f.label || f.key || ""), f.value]));
  let n = 0;
  for (const group of collectChoiceGroups(adapter)) {
    const label = questionLabel(group, adapter) || labelFor(group.inputs[0]);
    const v = want.get(norm(label));
    if (v == null || v === "") continue;
    if (group.inputs.some(i => i.checked)) continue;            // still holds → leave it
    const opts = group.inputs.map(i => ({ textContent: optionText(i, adapter), input: i }));
    const hit = matchOption(opts, v);
    if (hit) { clickOption(hit.input); n++; if (log) log(`re-applied "${label.slice(0, 40)}" → ${v}`, "warn"); }
  }
  if (adapter.yesNo) {
    for (const g of collectYesNoGroups(adapter)) {
      const label = yesNoQuestion(g.container, adapter);
      const v = want.get(norm(label));
      if (v == null || v === "") continue;
      if (g.buttons.some(b => b.matches(adapter.yesNo.committed))) continue;
      const opts = g.buttons.map(b => ({ textContent: cleanText(b), el: b }));
      const hit = matchOption(opts, v);
      if (hit) { hit.el.click(); n++; if (log) log(`re-applied "${label.slice(0, 40)}" → ${v}`, "warn"); }
    }
  }
  return n;
}

// Companion to reassertChoices for NON-choice controls: re-apply any intent value the Ashby
// post-resume re-init wiped from a text / textarea / <select> / react-select / combobox
// field. reassertChoices only covers radio/checkbox/yes-no groups, so a wiped dropdown or
// typed answer (Affirm's two questions, Cloudflare's country on the 2026-07-20 run) slipped
// through and tripped the diff-guard as "missing now". Only touches fields that are currently
// EMPTY (never overwrites a value still present) and only labels the intent has a value for,
// so a correct field is never disturbed and a deliberately-blank one is never filled. Mirrors
// reviewValueSummary's field walk. Async because dropdown re-selection drives real events.
// Returns the count re-applied.
async function reassertText(adapter, report, log) {
  if (!report || !Array.isArray(report.filled)) return 0;
  const want = new Map(report.filled.map(f => [norm(f.label || f.key || ""), f.value]));
  let n = 0;
  const tryApply = async (el, label) => {
    const v = want.get(norm(label));
    if (v == null || v === "") return;
    const r = await applyValue(el, v, adapter);
    if (r && r.ok) { n++; if (log) log(`re-applied "${String(label).slice(0, 40)}" → ${v}`, "warn"); }
  };
  // native <select> still sitting on its placeholder
  for (const el of document.querySelectorAll("select")) {
    if (el.offsetParent === null) continue;
    const o = el.options[el.selectedIndex];
    const t = o ? o.text.trim() : "";
    if (t && !/^select\.*\.?$/i.test(t)) continue;               // already has a real selection
    await tryApply(el, fieldLabel(el, adapter));
  }
  // react-select (Greenhouse): empty when it shows no value chip
  for (const ctl of document.querySelectorAll(".select__control")) {
    if (ctl.querySelector(".select__single-value, .select__multi-value")) continue;
    const inp = ctl.querySelector("input");
    if (inp) await tryApply(inp, fieldLabel(inp, adapter));
  }
  // Ashby combobox typeahead: role=combobox input with an empty value. Skip react-select
  // inputs. Greenhouse gives them role=combobox too, but they stay empty after a pick
  // because the answer shows in a chip. The loop above already checks those by chip.
  for (const el of document.querySelectorAll('[role="combobox"]')) {
    if (el.closest(".select__control")) continue;
    if (el.offsetParent === null || (el.value && el.value.trim())) continue;
    await tryApply(el, fieldLabel(el, adapter));
  }
  // plain text inputs / textareas that came out empty
  for (const el of document.querySelectorAll("input, textarea")) {
    const t = (el.type || "text").toLowerCase();
    if (["hidden", "submit", "button", "file", "password", "radio", "checkbox"].includes(t)) continue;
    if (el.offsetParent === null || !isRealField(el)) continue;
    if (el.getAttribute && el.getAttribute("role") === "combobox") continue;   // handled above
    if (el.value && el.value.trim()) continue;                    // still filled
    await tryApply(el, fieldLabel(el, adapter));
  }
  return n;
}

// The self-healing submit loop. Each pass is safe: an invalid form only renders errors.
// Returns an outcome the caller can act on, because "did this actually get submitted?" is
// the one question the panel must answer to decide whether to write applied.json:
//   "confirmed"  — the ATS showed a confirmation, or navigated away. Safe to log.
//   "likely"     — the form vanished with no errors. Probably sent, not provable.
//   "unknown"    — clicked, nothing happened. Genuinely undecidable from here.
//   "rejected"   — the ATS refused it (duplicate/limit); NOT a field problem.
//   "needs_verification" — an email human-verification gate is up; Parth enters the code.
//   "needs_input"/"failing"/"stopped"/"no_adapter" — didn't submit.
// Only "confirmed" is auto-logged; the ambiguous ones are Parth's call (see the panel).
async function submitWithRetry(log) {
  const { adapters } = await loadDeps();
  const adapter = currentAdapter(adapters);
  if (!adapter) { log("no adapter matches this page", "err"); return "no_adapter"; }
  // Baselines: confirmation/rejection phrases already on the page BEFORE the first click
  // (i.e. the JD) can never decide the outcome — only NEW text can. See matchedConfirmPhrases.
  const confirmBase = new Set(matchedConfirmPhrases(adapter));
  const rejectBase = new Set(matchedRejectPhrases());
  const verifyBase = new Set(matchedVerifyPhrases());
  const MAX = 6;
  let ambiguous = 0; // consecutive passes where the click produced neither errors nor a confirmation
  // The human action this whole submit hangs off. Captured HERE, before the first click,
  // because it is what the bridge uses to decide which emailed code belongs to this
  // submit — see autoFillVerifyCode and bridge/mailcode.py.
  const submitStartedAt = Date.now();
  const codesTried = new Set();
  let codeAttempts = 0;
  // The code this loop last typed into the gate, so a still-visible gate on a later pass
  // can be read correctly — see verifyCodeStillShown and handleVerifyGate.
  let codeFilled = "";
  // Each attempt is a full ~90s polling window (waitForVerifyCode), not a single shot.
  const MAX_CODE_ATTEMPTS = 2; // a code the form rejects twice is not going to start working

  // What to do about a gate that is currently on the page. Returns:
  //   "submit" — a code was just fetched and filled: CLICK SUBMIT AGAIN.
  //   "wait"   — our code is already in the box and the page has not answered yet. Nothing
  //              to fetch and nothing to click; keep polling for the outcome.
  //   "park"   — no code to be had; tell Parth and leave the tab open.
  //
  // THE BUG THIS REPLACES: filling a code used to `break` out of the polling loop, which
  // fell through to the ambiguous-click branch below, saw the same gate text still on the
  // page, and spent the second and last code attempt polling the inbox for a NEWER code.
  // The only code there was the one already typed, so it came back "stale" for the full 90s
  // and the run ended on "the code hasn't arrived" — having read the mail correctly, filled
  // the code correctly, and never once clicked Submit with it. Two rules come out of that:
  // a filled code must always lead to another click, and a gate still showing while our own
  // code sits in its box is not evidence that any mail is missing.
  const handleVerifyGate = async () => {
    if (verifyCodeStillShown(codeFilled)) return "wait";
    if (codeAttempts < MAX_CODE_ATTEMPTS) {
      codeAttempts++;
      // He clicked Submit, so fetching the code finishes what he started.
      const code = await waitForVerifyCode(log, submitStartedAt, codesTried);
      if (code) { codeFilled = code; return "submit"; }
    }
    log("🔐 Email verification needed — enter the code from your inbox in the browser, then submit.", "warn");
    return "park";
  };

  passes:
  for (let pass = 1; pass <= MAX; pass++) {
    if (PANEL_STOP) { log("stopped", "warn"); return "stopped"; }
    // A late confirmation from the previous pass's click may have landed while we waited.
    if (confirmationVisible(adapter, confirmBase)) { log("✅ Submitted & confirmed", "ok"); return "confirmed"; }
    // Re-apply any choice answer Ashby's post-resume re-init wiped since fill (idempotent —
    // skips groups that still hold their selection). Catches optional wiped fields too,
    // which never surface as validation errors.
    const wrote = [];
    const note = (m, kind) => { wrote.push(m); log(m, kind); };
    const reasserted = reassertChoices(adapter, PANEL_REPORT, note)
                     + await reassertText(adapter, PANEL_REPORT, note);
    traceStep(`submit ${pass}: recheck`, wrote);
    if (reasserted) { log(`re-asserted ${reasserted} wiped field(s)`, "warn"); await sleep(400); }
    log(`Pass ${pass}: submitting…`);
    const urlBefore = location.href;
    // Errors from the previous pass persist in the DOM until Ashby's server round-trip
    // answers, so a too-early read sees the STALE banner. Snapshot what's shown now: a
    // set that DIFFERS from this is definitely a fresh response.
    const errsBefore = readValidationErrors().sort().join("|");
    traceStep(`submit ${pass}: click`, []);
    logTrace(`before submit click ${pass}`);
    await clickSubmit(adapter, log);
    // Ashby validates on the SERVER: the error banner (or the confirmation) renders
    // 2–8s after the click (measured live on a real board). The old fixed 1.6s wait
    // read the page too early → "no errors and no confirmation". Poll up to 15s.
    let errs = [];
    const t0 = Date.now();
    while (Date.now() - t0 < 15000) {
      await sleep(500);
      const rej = submitRejectedText(rejectBase);
      if (rej) { log("❌ Rejected by the ATS (not a field problem): " + rej, "err"); return "rejected"; }
      // Email human-verification gate (anti-bot): submit was accepted but the ATS now wants a
      // code from Parth's inbox. We don't solve it — surface it and leave the tab open.
      const ver = submitVerificationText(verifyBase);
      if (ver) {
        const next = await handleVerifyGate();
        if (next === "park") return "needs_verification";
        if (next === "submit") continue passes;   // next pass clicks Submit with the code in
        // "wait" falls through to the confirmation and error checks below: the code is in
        // the box, so the answer we are waiting for is this page's, not the inbox's.
      }
      if (location.href !== urlBefore || confirmationVisible(adapter, confirmBase)) { log("✅ Submitted & confirmed", "ok"); return "confirmed"; }
      const now = readValidationErrors();
      if (now.length && now.sort().join("|") !== errsBefore) { errs = now; break; } // fresh error set
      if (now.length && Date.now() - t0 > 11000) { errs = now; break; } // identical set that persisted = genuinely still failing
    }
    if (!errs.length) {
      const rej = submitRejectedText(rejectBase);
      if (rej) { log("❌ Rejected by the ATS (not a field problem): " + rej, "err"); return "rejected"; }
      const ver = submitVerificationText(verifyBase);
      if (ver) {
        const next = await handleVerifyGate();
        if (next === "park") return "needs_verification";
        if (next === "submit") continue passes;   // re-submit with it
        // "wait": the code is in the box but a full poll window produced no answer at all.
        // Most likely the same swallowed click handled below, so click again — bounded by
        // the same `ambiguous` counter, and reported as the code gate it actually is
        // rather than as a generic unknown.
        if (++ambiguous >= 2) {
          log("🔐 the code is filled but the form hasn't accepted it — check the page and submit yourself", "warn");
          return "needs_verification";
        }
        log("🔐 code is in place but the click produced nothing — clicking Submit again…", "warn");
        continue passes;
      }
      if (location.href !== urlBefore || confirmationVisible(adapter, confirmBase)) { log("✅ Submitted", "ok"); return "confirmed"; }
      // If the whole form is gone, the submit almost certainly went through and we just
      // don't recognize the success screen's wording — don't click anything again.
      if (!document.querySelector('[class*="_fieldEntry"], form')) { log("form is gone — likely submitted; verify on the page", "warn"); return "likely"; }
      // Otherwise the click was probably SWALLOWED (verified live on Ramp: the first
      // synthesized Submit click after a page load often does nothing — no request, no
      // errors). Re-clicking is safe: an invalid form only renders errors, and a valid
      // one submitting is the goal. Give up after two ambiguous passes in a row.
      if (++ambiguous >= 2) { log("no errors and no confirmation after 2 attempts — check the page manually", "warn"); return "unknown"; }
      log("click produced nothing (likely swallowed) — clicking Submit again…", "warn");
      continue;
    }
    ambiguous = 0;
    log(`${errs.length} error(s): ${errs.join(", ")}`, "err");
    const unfixable = [];
    let fixed = 0;
    for (const title of errs) {
      const r = resyncFieldByTitle(title, adapter);
      if (r === "fixed") fixed++;
      else unfixable.push(title);   // "empty" (blank field) or "notfound"
    }
    traceStep(`submit ${pass}: retype errored fields`, errs.filter(e => !unfixable.includes(e)));
    log(`re-synced ${fixed} field(s)` + (unfixable.length ? ` · needs you: ${unfixable.join(", ")}` : ""),
      unfixable.length ? "warn" : "ok");
    if (unfixable.length) { log("Fill the fields above by hand, then press Submit again.", "warn"); return "needs_input"; }
    await sleep(300);
  }
  log(`still failing after ${MAX} passes — press Submit to keep trying`, "err");
  return "failing";
}

// Who is this form for? All three ATSs put the tenant slug as the first path segment
// (ashbyhq.com/<co>/..., lever.co/<co>/..., greenhouse.io/<co>/...); title-case it.
// Read off the URL/DOM alone so the panel can name a job it never filled (the
// "I submitted this by hand" path runs with no report behind it).
function pageCompany() {
  const titleCase = s => String(s || "").trim().replace(/[-_]+/g, " ").replace(/\b\w/g, c => c.toUpperCase());

  // The tenant-slug-as-first-path-segment rule holds for the CANONICAL board URLs
  // (ashbyhq.com/<co>/…, lever.co/<co>/…, greenhouse.io/<co>/…) but NOT for the Greenhouse
  // embed endpoint that background.js rewrites custom career domains to:
  //   job-boards.greenhouse.io/embed/job_app?for=<slug>&token=<id>
  // There the first segment is the literal "embed", which is how every Discord/Airbnb/
  // Roblox-style proxied board came out named "Embed" — and that name is what the drafter
  // writes into the why_company essays ("I want to work at Embed…").
  if (/\/embed\/job_app/i.test(location.pathname)) {
    let forSlug = "";
    try { forSlug = new URL(location.href).searchParams.get("for") || ""; } catch (_) {}
    if (forSlug) return titleCase(forSlug);
    // The legacy ?token=<id> rewrite (background.js's gh_jid probe) carries no `for`, so
    // fall back to Greenhouse's own page title: "Job Application for <role> at <Company>".
    // That's the real display name, better-cased than any slug.
    const m = (document.title || "").match(/\bat\s+(.+?)\s*$/i);
    if (m) return m[1].trim();
    return "";   // better empty than "Embed" — the drafter omits an empty company
  }

  // Workday breaks the first-path-segment rule in the other direction: the tenant is the
  // SUBDOMAIN (adobe.wd5.myworkdayjobs.com) and the first path segment is the careers-site
  // name — "external_experienced", "crowdstrikecareers". The generic rule below therefore
  // named every Adobe role "External Experienced", which is the "Embed" failure again, and
  // it costs more here than a bad essay: this value is what pickResume() scores to choose
  // the resume track, and the whole Workday fill is then resolved through that track.
  const wd = location.hostname.match(/^([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com$/i);
  if (wd) return titleCase(wd[1]);

  const slug = (location.pathname.split("/").filter(Boolean)[0] || "").trim();
  return titleCase(slug);
}
function pageRole() {
  return (document.querySelector("h1, .app-title, [class*=posting-headline]")?.textContent
    || document.title || "").trim();
}

// Build the floating panel (once). Shadow DOM so the page's CSS can't touch it.
function mountPanel() {
  if (window.__jobfillPanel || !document.body) return;
  const host = document.createElement("div");
  host.id = "__jobfill_host";
  host.style.cssText = "position:fixed;bottom:20px;right:20px;z-index:2147483647;";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `
    <style>
      /* Shadow DOM blocks the page's stylesheets, but INHERITABLE typography still
         cascades in through the host element — a page with line-height:0.75, uppercase,
         or wide letter-spacing on <body> leaks into every card that doesn't set its own,
         garbling the text (overlapping lines). Pin the inherited baseline on :host so the
         page can't reach in; !important covers the rare page that sets these on the universal selector. */
      :host{
        line-height:normal!important;letter-spacing:normal!important;word-spacing:normal!important;
        text-transform:none!important;font-size:medium!important;font-style:normal!important;
        font-weight:normal!important;font-variant:normal!important;text-indent:0!important;
        white-space:normal!important;text-align:left!important;color:#111!important;
      }
      *{box-sizing:border-box;font-family:-apple-system,Segoe UI,Roboto,sans-serif}
      .fab{width:48px;height:48px;border-radius:50%;background:#4f46e5;color:#fff;font-size:22px;
        display:flex;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.3);margin-left:auto}
      .panel{width:320px;background:#fff;color:#111;border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.35);
        overflow:hidden;margin-bottom:10px}
      .panel.hidden{display:none}
      .hd{background:#4f46e5;color:#fff;padding:10px 12px;font-weight:600;font-size:13px;display:flex;justify-content:space-between;align-items:center}
      .hd .x{cursor:pointer;opacity:.85}
      .btns{display:flex;gap:6px;padding:10px 12px}
      button{flex:1;padding:8px;border:0;border-radius:7px;font-size:13px;font-weight:600;cursor:pointer;color:#fff}
      #fill{background:#2563eb}#submit{background:#16a34a}#stop{background:#dc2626;flex:0 0 64px}
      button:disabled{opacity:.5;cursor:default}
      /* secondary row: the escape hatch, deliberately quieter than Fill/Submit */
      .alt{display:flex;padding:0 12px 10px}
      .alt button{background:#6b7280;font-size:12px;font-weight:600;padding:6px}
      .alt button.arm{background:#b45309}
      .log{max-height:220px;overflow:auto;padding:6px 12px 12px;font-size:12px;line-height:1.5;border-top:1px solid #eee}
      .line{white-space:pre-wrap}.line.err{color:#dc2626}.line.ok{color:#16a34a}.line.warn{color:#b45309}
      .empty{color:#888}
      /* question cards — the manual answer path; nothing here goes to Telegram */
      .qs{max-height:320px;overflow:auto;border-top:1px solid #eee}
      .qs:empty{display:none}
      .qhd{padding:8px 12px 2px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:#6b7280}
      .q{padding:8px 12px 12px;border-bottom:1px solid #f1f1f1}
      .q.done{opacity:.5}
      .q.done .qbody{display:none}
      .qlabel{font-size:12px;font-weight:600;line-height:1.35;display:flex;gap:6px;align-items:baseline}
      .badge{font-size:9px;font-weight:700;padding:1px 5px;border-radius:99px;flex:0 0 auto}
      .badge.req{background:#fee2e2;color:#b91c1c}.badge.opt{background:#e5e7eb;color:#4b5563}
      .qwhy{font-size:11px;color:#6b7280;margin-top:2px}
      .qin{width:100%;margin-top:6px;padding:6px;border:1px solid #d1d5db;border-radius:6px;font-size:12px;
        font-family:inherit;resize:vertical;min-height:34px;background:#fff;color:#111}
      .qbtns{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
      .qbtns button{flex:0 1 auto;padding:5px 9px;font-size:11px;border-radius:6px}
      .save{background:#2563eb}.useDraft{background:#7c3aed}.skipq{background:#6b7280}
      .askAI{background:#7c3aed}
      .opt{background:#0f766e}
      /* optional pile: a single quiet line until he opens it */
      .more{padding:8px 12px;font-size:11px;font-weight:600;color:#4f46e5;cursor:pointer;
        border-top:1px solid #eee;user-select:none}
      .more:hover{background:#f5f3ff}
      .optqs{max-height:320px;overflow:auto}
      .qstate{font-size:11px;margin-top:4px}
      .qstate.ok{color:#16a34a}.qstate.err{color:#dc2626}
      @media (prefers-color-scheme:dark){
        .panel{background:#1f2937;color:#e5e7eb}.log{border-top-color:#374151}.empty{color:#9ca3af}
        .qs{border-top-color:#374151}.q{border-bottom-color:#374151}
        .more{border-top-color:#374151;color:#a5b4fc}.more:hover{background:#111827}
        .qin{background:#111827;color:#e5e7eb;border-color:#4b5563}
        .badge.opt{background:#374151;color:#d1d5db}.badge.req{background:#7f1d1d;color:#fecaca}
      }
    </style>
    <div class="panel hidden">
      <div class="hd"><span>💼 Job Autofill</span><span class="x">✕</span></div>
      <div class="btns"><button id="fill">▶ Fill</button><button id="submit">✓ Submit</button><button id="code" title="Fetch the emailed verification code for this form">🔐 Code</button><button id="stop">⏹ Stop</button></div>
      <div class="alt"><button id="manual">✋ I submitted this by hand</button></div>
      <div class="qs"></div>
      <div class="opts"></div>
      <div class="log"><div class="line empty">Fill the form, then Submit — I'll fix any rejected fields and retry.</div></div>
    </div>
    <div class="fab" title="Job Autofill">💼</div>`;
  (document.body || document.documentElement).appendChild(host);
  window.__jobfillPanel = host;

  const $ = (s) => root.querySelector(s);
  const panel = $(".panel"), fab = $(".fab"), logEl = $(".log");
  const log = (msg, cls = "") => {
    const first = logEl.querySelector(".empty"); if (first) first.remove();
    const d = document.createElement("div"); d.className = "line " + cls; d.textContent = msg;
    logEl.appendChild(d); logEl.scrollTop = logEl.scrollHeight;
  };
  const setBusy = (b) => { $("#fill").disabled = b; $("#submit").disabled = b; };
  fab.onclick = () => panel.classList.toggle("hidden");
  $(".x").onclick = () => panel.classList.add("hidden");
  const qsEl = $(".qs"), optEl = $(".opts");

  // Every string here (labels, options, drafts) comes off the page or the bank, so build
  // the cards with textContent and never innerHTML — a form label is not something to
  // trust with markup, even inside our own shadow root.
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  // `into`/`heading` let the optional pile render into its own container below the required
  // cards, using the exact same card (and the same apply path) rather than a parallel UI.
  function renderQuestions(questions, drafts, into = qsEl, heading = null) {
    into.textContent = "";
    if (!questions || !questions.length) return;
    into.appendChild(el("div", "qhd", heading
      || `${questions.length} question${questions.length > 1 ? "s" : ""} need you`));

    for (const q of questions) {
      const card = el("div", "q");
      const head = el("div", "qlabel");
      head.appendChild(el("span", "", q.label || "(unlabeled)"));
      head.appendChild(el("span", "badge " + (q.required ? "req" : "opt"), q.required ? "Required" : "Optional"));
      card.appendChild(head);
      if (q.why) card.appendChild(el("div", "qwhy", q.why));

      const body = el("div", "qbody");
      const state = el("div", "qstate");
      const input = el("textarea", "qin");
      input.rows = 2;

      // Mark the card done and report it. `applied` false => he deliberately left it blank.
      const finish = async (value, applied, why) => {
        card.classList.add("done");
        state.className = "qstate " + (applied ? "ok" : "err");
        state.textContent = applied ? `✓ ${value}` : (why || "left blank");
        card.appendChild(state);
        log(applied ? `✓ ${q.label}` : `⏭️ ${q.label} — left blank`, applied ? "ok" : "");
      };

      const answer = async (value, fromBankId) => {
        const a = (PANEL_ASKS || []).find(x => x.id === q.id);
        if (!a) return finish(value, false, "that field is gone from the page — re-run Fill");
        const r = await applyAskAnswer(PANEL_REPORT, a, value);
        if (r.ok && fromBankId) {
          // Only an ACCEPTED draft counts as a use — same rule as the Telegram path, so an
          // answer he keeps rewriting still goes stale and surfaces for a rewrite.
          rpc({ type: "BANK_TOUCH", bankId: fromBankId }).catch(() => {});
        }
        // Report the setter's own reason. "couldn't write that into the field" was true of
        // every failure and told him nothing; the field having left the page and the value
        // matching no option need different responses from him.
        finish(r.ok && r.value ? r.value : value, r.ok,
               r.ok ? "" : `${r.why || "the field didn't take it"} — set it by hand`);
      };

      let d = (drafts && drafts[q.id]) || (q.draft ? { draft: q.draft, bank_id: null } : null);
      // The draft goes into the EDITABLE textarea only. It used to also render into a
      // read-only .draft box above it, which showed every essay twice in the panel —
      // the box and the textarea held identical text. The textarea alone still supports
      // both outcomes: "👍 Use draft" accepts d.draft verbatim (and counts as a bank use),
      // "Save" sends whatever he edited it into.
      const showDraft = (text) => {
        input.value = text;
        input.rows = Math.min(10, Math.max(4, Math.ceil(text.length / 60)));
      };
      if (d && d.draft) showDraft(d.draft);
      body.appendChild(input);

      const btns = el("div", "qbtns");
      // Tappable options, same as Telegram's inline keyboard: these are the exact strings
      // the control accepts, so clicking beats retyping one and hoping the matcher agrees.
      for (const o of (q.options || []).slice(0, 12)) {
        const b = el("button", "opt", o.length > 28 ? o.slice(0, 27) + "…" : o);
        b.title = o;
        b.onclick = () => answer(o, null);
        btns.appendChild(b);
      }
      const useBtn = el("button", "useDraft", "👍 Use draft");
      useBtn.onclick = () => answer(d.draft, d.bank_id);
      if (d && d.draft) btns.appendChild(useBtn);
      // Ask Claude to write one, for a question nothing recognized. The taxonomy only tags
      // shapes it has seen, so a one-off prompt ("what does our purpose mean to you?") reaches
      // the panel with no answer_key and therefore no draft — this is the way to get one
      // without teaching taxonomy.yaml a new rule first. Explicit tap, never automatic: each
      // call spends real subscription quota. Free-text only; a dropdown is a pick, not an essay.
      if (!(q.options || []).length) {
        const dr = el("button", "askAI", "✨ Draft it");
        dr.onclick = async () => {
          dr.disabled = true; dr.textContent = "✨ drafting…";
          try {
            const res = await rpc({ type: "DRAFTS_FOR", items: [{ id: q.id, label: q.label, answer_key: q.answer_key }],
              context: { company: (PANEL_REPORT || {}).company, role: (PANEL_REPORT || {}).role }, force: true });
            const got = res && res.drafts && res.drafts[q.id];
            if (!got || !got.draft) {
              dr.textContent = "✨ Draft it";
              dr.disabled = false;
              log(res && res.ok === false ? "Drafting needs the bridge running." : "No draft came back — write it yourself or try again.", "warn");
              return;
            }
            d = got;
            showDraft(got.draft);   // editable: the draft is a starting point, not the answer
            if (!useBtn.isConnected) btns.insertBefore(useBtn, dr);
            dr.remove();
          } catch (e) {
            dr.disabled = false; dr.textContent = "✨ Draft it";
            log("draft failed: " + ((e && e.message) || e), "err");
          }
        };
        btns.appendChild(dr);
      }
      const save = el("button", "save", "Save");
      save.onclick = () => {
        const v = input.value.trim();
        if (!v) return finish("", false, "type something first");
        answer(v, d && d.draft === v ? d.bank_id : null); // edited a draft => not an accept
      };
      btns.appendChild(save);
      const skip = el("button", "skipq", "Skip");
      skip.onclick = () => finish("", false, "left blank");
      btns.appendChild(skip);

      body.appendChild(btns);
      card.appendChild(body);
      into.appendChild(card);
    }
  }

  // The optional pile: one line by default, cards once he asks. Rendering them expanded
  // would bury the questions that actually block the submit under fields nobody needs.
  function renderOptional(questions) {
    optEl.textContent = "";
    if (!questions || !questions.length) return;
    const more = el("div", "more", `＋ ${questions.length} optional field${questions.length > 1 ? "s" : ""} the form doesn't need`);
    const list = el("div", "optqs");
    more.onclick = () => {
      if (list.childElementCount) { list.textContent = ""; more.textContent = `＋ ${questions.length} optional field${questions.length > 1 ? "s" : ""} the form doesn't need`; return; }
      more.textContent = `− optional fields`;
      renderQuestions(questions, {}, list, `${questions.length} optional — answer any you want`);
    };
    optEl.appendChild(more);
    optEl.appendChild(list);
  }

  $("#fill").onclick = async () => {
    setBusy(true); PANEL_STOP = false; qsEl.textContent = ""; optEl.textContent = ""; log("Filling…");
    try {
      const r = await runFill(false, { mode: "panel" });
      if (r.stopped) log(`Stopped. ${r.filled.length} field(s) were filled before that.`, "warn");
      else log(`Filled ${r.filled.length}.`, "ok");
      // Ashby wipes freshly-clicked radios a few seconds after fill (the resume-parse
      // re-render). The click already reported "filled", so re-apply from intent once the
      // form settles — otherwise the preview shows blanks and looks broken. Non-blocking so
      // the cards render now; each pass skips groups that still hold their selection. The
      // Submit-time reassert is the backstop for a wipe that lands after this window.
      (async () => {
        try {
          const { adapters } = await loadDeps();
          const adapter = currentAdapter(adapters);
          if (!adapter) return;
          for (let i = 0; i < 4; i++) {
            await sleep(1500);
            const n = reassertChoices(adapter, PANEL_REPORT, null);
            if (n) log(`re-applied ${n} choice field(s) the form had dropped.`, "ok");
          }
        } catch (_) { /* best-effort preview fix */ }
      })();
      let qs = r.questions || [];
      // Restore the answers Parth already saved for THIS job (a parked form he's re-filling
      // by hand) so he doesn't re-type them. Match by label — ask ids are regenerated each
      // fill and won't line up across runs — with the required-marker stripped, same as the
      // diff-guard. Applied answers drop out of the cards below; only genuinely-new questions
      // remain. Best-effort: no match / bridge off simply falls through to asking normally.
      try {
        const saved = await rpc({ type: "PENDING_ANSWERS", url: location.href });
        const key = s => norm(String(s || "").replace(/[✱*]/g, ""));
        if (saved && saved.found && saved.answers && Object.keys(saved.answers).length) {
          const byLabel = new Map(Object.entries(saved.answers).map(([k, v]) => [key(k), v]));
          const remaining = [];
          let applied = 0;
          for (const q of qs) {
            const v = byLabel.get(key(q.label));
            const a = (v != null && v !== "") ? (PANEL_ASKS || []).find(x => x.id === q.id) : null;
            if (a && (await applyAskAnswer(PANEL_REPORT, a, v)).ok) applied++;
            else remaining.push(q);
          }
          if (applied) log(`Restored ${applied} answer(s) you'd already saved.`, "ok");
          qs = remaining;
        }
        // The drafts collect-time already wrote for the questions still open. Seeding them
        // here (same label match, ask ids don't survive across fills) is what stops the panel
        // buying a SECOND draft for an essay Claude has already written — see the keyed
        // filter below, which now only asks for questions that arrived without one.
        if (saved && saved.found && saved.drafts) {
          const byDraft = new Map(Object.entries(saved.drafts).map(([k, v]) => [key(k), v]));
          let seeded = 0;
          for (const q of qs) {
            const d = byDraft.get(key(q.label));
            if (d && !q.draft) { q.draft = d; seeded++; }
          }
          if (seeded) log(`Reused ${seeded} draft(s) already written for this job.`, "ok");
        }
      } catch (_) { /* bridge off or no saved record — ask cold, as before */ }
      if (!qs.length) { log("Nothing else needed — review it and hit Submit.", "ok"); }
      else {
        // Drafts are a bonus, not a dependency: the bank lives on disk behind the bridge, so
        // with the bridge down we simply ask cold rather than failing the fill.
        let drafts = {};
        // Only what we don't already have. A question carrying a draft (seeded above from the
        // pending record) is served; sending it would spend a fresh Claude call to rewrite
        // text already on disk, and handle_drafts asks one question per call.
        const keyed = qs.filter(q => q.answer_key && !q.draft);
        if (keyed.length) {
          const res = await rpc({ type: "DRAFTS_FOR", items: keyed, context: { company: r.company } });
          drafts = (res && res.drafts) || {};
          if (!res || !res.ok) log("No saved answers (bridge is off) — answer these cold.", "warn");
        }
        renderQuestions(qs, drafts);
        log(`${qs.length} question(s) need you — answer them above.`, "warn");
      }
      // Whatever the form doesn't require, offered but not pushed. Drafts aren't fetched for
      // these — most carry no answer_key, and pre-drafting a pile he may never open would
      // spend quota on nothing. Each card has its own "✨ Draft it" for that.
      const optQs = r.optionalQuestions || [];
      renderOptional(optQs);
      if (optQs.length) log(`${optQs.length} optional field(s) available below if you want them.`);
    } catch (e) { log("fill error: " + ((e && e.message) || e), "err"); }
    setBusy(false);
  };
  // Write this form to applied.json — the same record phase 3 logs. Without it a form
  // filled by hand stays in the daily queue and gets applied to a SECOND time tomorrow,
  // which some ATSs reject outright (Ramp did) and which burns a slot that can't be undone.
  // The bridge resolves the dedup_key: it matches the queue by URL, so a panel fill and a
  // queue fill of the same role land on one key. Location is the bridge's problem too — the
  // page doesn't carry the scraper's version of it.
  async function logApplied(opts = {}) {
    const r = PANEL_REPORT || {};
    // Fall back to the page when there's no report: the by-hand path can run on a form the
    // panel never filled, and the bridge still needs a company/title to mint a key with if
    // the URL isn't in today's queue.
    const res = await rpc({ type: "LOG_APPLIED", record: {
      company: r.company || pageCompany(), title: r.role || pageRole(), location: "",
      url: location.href,
      // WHICH CV went out. Never recorded before, across every application — which is what
      // made Workday's "use my last application" unusable: the clone guard asks what track
      // the previous submission used and nothing could answer. Omitted rather than sent
      // empty on the by-hand path, where no fill ran and there is genuinely no answer.
      ...((r.resumeChoice || {}).tag ? { resume_track: r.resumeChoice.tag } : {}),
      ...(opts.byHand ? { by_hand: true } : {}) } });
    if (res && res.ok && !res.queued) log("📌 Logged as applied — the queue won't offer this again.", "ok");
    else if (res && res.queued) log("📌 Bridge is off — the record is parked and will log itself when it's back.", "ok");
    else log("Couldn't log it (bridge off?) — run apply_log.py so it isn't applied to twice.", "warn");
  }

  // The explicit half of Parth's rule: a tap IS the human action, so this fetches even
  // when no submit has been clicked in this tab — he may have submitted by hand, or
  // reloaded onto a form that is already sitting at the code gate.
  //
  // The window opens at the tap and looks BACKWARDS a few minutes, because by the time he
  // reaches for this button the mail has usually already arrived. That is the one place a
  // window is not bounded by a click, which is why it is minutes and not hours.
  $("#code").onclick = async () => {
    setBusy(true);
    try {
      const since = Date.now() - 10 * 60 * 1000;
      const ok = await waitForVerifyCode(log, since, new Set(), 45000);
      if (ok) log("Check it looks right, then tap Submit.", "ok");
    } finally { setBusy(false); }
  };

  $("#submit").onclick = async () => {
    setBusy(true); PANEL_STOP = false;
    try {
      const outcome = await submitWithRetry(log);
      logTrace("submit finished", { outcome });
      if (outcome === "confirmed") await logApplied();
      else if (outcome === "needs_verification") {
        // Anti-bot email code gate. We don't solve it — tell Parth on his phone and leave
        // the tab open. NOT logged applied (it hasn't submitted); he enters the code, then
        // taps Submit again → the loop re-checks and logs on the real confirmation.
        const r = PANEL_REPORT || {};
        const co = r.company || "This application";
        log("🔐 Enter the code from your email in the browser, then tap Submit again.", "warn");
        rpc({ type: "NOTIFY", text: `🔐 <b>${co}</b> needs an email verification code to submit.\nCheck your inbox, enter the code in the browser, then submit.` })
          .catch(() => {});
      }
      else if (outcome === "likely" || outcome === "unknown") {
        // Don't guess either way. Auto-logging risks marking an unsent application as done
        // (he'd never see the role again); not logging risks a duplicate submit. He's
        // sitting right here and can see the page, so let him make the call.
        log("Did it go through? If the page says so, log it:", "warn");
        const b = el("button", "save", "📌 Mark as applied");
        b.style.marginTop = "6px";
        b.onclick = async () => { b.disabled = true; await logApplied(); };
        logEl.appendChild(b);
        logEl.scrollTop = logEl.scrollHeight;
      }
    } catch (e) { log("submit error: " + ((e && e.message) || e), "err"); }
    setBusy(false);
  };
  // Stop takes effect at the next safe boundary — between submit passes, or between
  // Workday steps — so it is never instant. Say so, because a button that sets a flag and
  // changes nothing on screen is indistinguishable from a button that does not work, which
  // is exactly how this one read while the fill path ignored the flag entirely.
  $("#stop").onclick = () => {
    if (PANEL_STOP) return;
    PANEL_STOP = true;
    log("Stopping — finishing the current step first…", "warn");
  };

  // "I filled part of this here but submitted it myself." Without this the role stays in the
  // queue and tomorrow's run applies to it a SECOND time (some ATSs reject that outright).
  // Same record the confirmed-submit path writes — logging it also archives the pending
  // record, so the answers already given survive. Two-tap because it's effectively
  // irreversible from here (undo lives in the web console's pile move).
  const manual = $("#manual");
  let armed = false;
  manual.onclick = async () => {
    if (!armed) {
      armed = true;
      manual.classList.add("arm");
      manual.textContent = "Tap again to log it as applied";
      setTimeout(() => {
        if (!armed) return;
        armed = false; manual.classList.remove("arm");
        manual.textContent = "✋ I submitted this by hand";
      }, 5000);
      return;
    }
    armed = false; manual.classList.remove("arm"); manual.disabled = true;
    manual.textContent = "✋ Logged as submitted by hand";
    try { await logApplied({ byHand: true }); }
    catch (e) {
      log("couldn't log it: " + ((e && e.message) || e), "err");
      manual.disabled = false; manual.textContent = "✋ I submitted this by hand";
    }
  };
}

// Popup can still trigger a fill; the on-page panel is the primary manual UI.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "FILL") {
    // opts carries the batch modes: {mode:"collect"} for phase 1, or
    // {mode:"apply", answers, approved} for phase 3. Absent => the live single-pass path.
    runFill(!!msg.autoSubmit, msg.opts || {}).then(sendResponse);
    return true; // async response
  }
  if (msg && msg.type === "SUBMIT") {
    // PHASE 3 unattended submit — run the exact hardened loop the panel's Submit button
    // runs (multi-pass, reassertChoices for Ashby's post-resume wipe, server-side
    // validation reading, reject/verify detection) instead of a single background click.
    // PANEL_REPORT was seeded by the apply-mode runFill just before this message. Returns
    // the outcome string and the captured log lines so background can word the Telegram
    // note and decide whether to log applied (only "confirmed" is safe to log).
    const logs = [];
    const log = (m, kind) => { logs.push({ m: String(m), kind: kind || "info" }); };
    submitWithRetry(log)
      .then(outcome => { logTrace("submit finished", { outcome }); sendResponse({ outcome, logs }); })
      .catch(e => sendResponse({ outcome: "error", logs, error: String((e && e.message) || e) }));
    return true; // async response
  }
  if (msg && msg.type === "OPEN_PANEL") {   // popup "open panel" button, if wired
    mountPanel();
    const p = window.__jobfillPanel && window.__jobfillPanel.shadowRoot.querySelector(".panel");
    if (p) p.classList.remove("hidden");
    sendResponse && sendResponse({ ok: true });
  }
});

// Does THIS frame actually hold an application form? A known ATS URL always qualifies (the
// content_scripts matches already restrict auto-injection to those). The DOM checks only
// matter for the manual "Activate on this page" path (popup -> background.executeScript),
// which injects into EVERY frame regardless of URL — without this gate it would drop a
// stray panel on the wrapper page (e.g. asana.com) and unrelated iframes (optimizely).
function looksLikeAppForm() {
  const h = location.href;
  if (/(?:\/\/|\.)(?:boards|job-boards)\.greenhouse\.io\//i.test(h)) return true;
  if (/(?:\/\/|\.)greenhouse\.io\/embed\/job_app/i.test(h)) return true;
  if (/(?:\/\/|\.)jobs\.lever\.co\//i.test(h)) return true;
  if (/(?:\/\/|\.)jobs\.ashbyhq\.com\//i.test(h)) return true;
  // Workday qualifies on the URL alone and CANNOT fall through to the DOM checks below:
  // its wizard is built from data-automation-id divs, with no <form> carrying a file or
  // email input, so every one of those checks fails on the page we most need the panel on.
  // The posting page qualifies too. Nothing useful can be filled there yet — the entry
  // flow (Apply -> route modal) is not automated — but Fill answers with a plain "not on
  // an apply flow page", which beats no panel at all and is where entry automation lands.
  if (/\.myworkdayjobs\.com\//i.test(h)) return true;
  // Inline embed on a custom host: require a form that actually carries applicant fields
  // (a bare #grnhse_app container wrapping the iframe lives on the parent page too, so it
  // is not enough on its own).
  if (document.querySelector('#application_form, form[action*="greenhouse" i]')) return true;
  const f = document.querySelector('form');
  if (f && f.querySelector('input[type="file"], input[name="first_name"], input[type="email"]')) return true;
  return false;
}

// Auto-mount the panel on every application page (and, when force-injected, only on the
// frame that holds the form — see looksLikeAppForm).
function mountIfForm() { if (looksLikeAppForm()) mountPanel(); }
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mountIfForm);
else mountIfForm();
