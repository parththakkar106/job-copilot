// Ashby adapter — the "DOM map" for the Apply engine (Tier 0 fill).
//
// Same contract as adapters/greenhouse.js: taxonomy.yaml + profile.json say WHAT to
// fill; this file says HOW/WHERE on a real Ashby form. See greenhouse.js for the split.
//
// LIVE-VERIFIED 2026-07-09 against jobs.ashbyhq.com/{notion,plaid}/<id>/application
// (DOM inspection, extension not yet loaded). Ashby is a React app, BUT unlike the new
// Greenhouse react-select it HONORS SYNTHESIZED EVENTS — so NO chrome.debugger / trusted
// clicks are needed (major difference from Greenhouse):
//   * Core system fields have stable ids:
//       #_systemfield_name  (ONE full-name field, not first/last -> identity.full_name)
//       #_systemfield_email
//       #_systemfield_resume (input[type=file])
//       #_systemfield_location (a combobox typeahead, see below)
//     Phone and every custom question use a per-job UUID id (discover at runtime).
//   * Labels are proper `label[for="<id>"]` associations — the default labelStrategy
//     works cleanly (no scraping ancestors like Lever).
//   * Dropdowns render TWO ways, both handled with synthesized events:
//       (a) native <select>  -> fill.js setNativeSelect (EEO often uses these).
//       (b) an Ashby combobox: <input role="combobox" aria-haspopup="listbox"
//           class="_input_d7ago_28">. It opens on a synthesized `input` event (typeahead)
//           or ArrowDown; options render as [role="option"] (class _result_d7ago_*);
//           a synthesized click on an option COMMITS it (verified: value filled, menu
//           closed). #_systemfield_location is this kind (a places typeahead).
//   * Single-select short questions render as labeled RADIOS: each option is a
//     label[for="...radio-N"] with an <input type=radio>. Click the label whose text
//     matches. Multi-select uses labeled CHECKBOXES the same way.
//   * URL questions (LinkedIn/GitHub) are input[type=url]; text/email/tel are standard.
//   * Submit button reads "Submit Application" — never auto-clicked.

export const ASHBY = {
  id: "ashby",

  // Synthesized events are honored (typeahead opens on input, option-click commits).
  needsCDP: false,

  urlPatterns: [
    /(?:\/\/|\.)jobs\.ashbyhq\.com\//i,
  ],

  // ── Known core fields ───────────────────────────────────────────────────────
  coreFields: [
    { key: "full_name", profilePath: "identity.full_name",
      control: "text", selectors: ['#_systemfield_name', 'input[name="_systemfield_name"]'] },
    { key: "email", profilePath: "identity.email",
      control: "text", selectors: ['#_systemfield_email', 'input[type="email"]'] },

    // Phone has no stable id (per-job UUID). Fall back to the single tel input; if a
    // form ever has two tel inputs this needs the label route, but one is the norm.
    { key: "phone", profilePath: "identity.phone",
      control: "text", optional: true, selectors: ['input[type="tel"]'] },

    // Location: Ashby combobox typeahead. Type a prefix to filter, then pick the option
    // matching the full display string. Handled by the `comboboxTypeahead` dropdown kind.
    { key: "location", profilePath: "identity.location_display", control: "combobox",
      optional: true, selectors: ['#_systemfield_location', 'input[role="combobox"]'] },

    // Resume: DataTransfer inject (see attach.js).
    { key: "resume", control: "file", attach: "resume",
      selectors: ['#_systemfield_resume', 'input[type="file"]'] },
  ],

  // ── Custom questions (per-job) ──────────────────────────────────────────────
  // Custom fields use UUID ids with clean label[for] associations. Discover at runtime;
  // classify the label via taxonomy. No stable name pattern (unlike Lever's cards[...]),
  // so identify them as "any labeled control that isn't a known core field".
  customQuestion: {
    // UUID-shaped id (8-4-4-4-12 hex).
    match: /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i,
    containerSelectors: ['[class*="_fieldEntry"]', 'fieldset'],
  },

  // ── EEO / demographic section ───────────────────────────────────────────────
  // Ashby renders EEO as native <select> (gender/race/veteran/disability) — verified a
  // Plaid form carried 4 native selects with demographic labels. Label-classified like
  // any other question; setNativeSelect handles them.
  eeo: {
    sectionSelectors: ['[class*="_demographic" i]', '[class*="eeo" i]'],
    verify: false,
  },

  // ── Dropdown strategy ───────────────────────────────────────────────────────
  // Two kinds coexist: native <select> (setNativeSelect) and the Ashby combobox
  // typeahead. The combobox descriptor tells fill.js how to open + read + commit it
  // WITHOUT CDP: set the input value + dispatch `input` to open the listbox, read
  // [role="option"] nodes, synthesized-click the match.
  dropdown: {
    kind: "comboboxTypeahead",
    control: 'input[role="combobox"]',
    option: '[role="option"]',
    // Type only the first comma-segment (e.g. "Springfield") to filter, then match the
    // full display value against the rendered options. Ashby option labels expand the
    // state name ("Springfield, Illinois, United States"), so filtering on the full string
    // can miss — filter on the city prefix, match on the full value.
    typePrefixOnly: true,
  },

  // ── Yes/No button widget ────────────────────────────────────────────────────
  // Short boolean questions render as a <div class="_yesno"> holding two <button>s
  // ("Yes"/"No"), backed by a HIDDEN native checkbox (no id, offsetParent null). The
  // hidden input means these never surface as a radio group and get silently dropped —
  // so handle the widget directly: classify the question, click the matching button (a
  // synthesized click commits: checkbox flips + button gains an _active class).
  yesNo: {
    containerSelectors: ['[class*="_yesno"]'],
    option: "button",
    committed: '[class*="_active"]',
  },

  // ── Setter strategies per control type ──────────────────────────────────────
  setters: {
    text: "nativeSetter(el,'value',v); dispatch input+change",
    select: "selectByVisibleText(el,v); dispatch change",
    combobox: "setValue+dispatch('input') to open -> match [role=option] -> click to commit",
    file: "attachViaDataTransfer(el,bytes,filename); dispatch change",
    choice: "clickChoiceByText(group,v)  // labeled radios/checkboxes; click matching label",
  },

  // ── Label extraction ────────────────────────────────────────────────────────
  // Clean label[for] associations — the default order resolves everything.
  labelStrategy: ["label[for=ID]", "closest('label')", "aria-label",
    "aria-labelledby", "placeholder", "name", "id"],

  // Container scoping one question (for grouping labeled radios/checkboxes + deriving
  // the question label when the option labels would otherwise mislead classification).
  questionContainerSelectors: ['[class*="_fieldEntry"]', 'fieldset'],

  // Ashby radio/checkbox option `value` is "on" (not the human text) — match on the
  // wrapping label's text instead.
  optionValueIsText: false,

  // ── Required / knockout detection ───────────────────────────────────────────
  requiredSignals: ['[aria-required="true"]', '[required]'],
  requiredLabelMarker: /✱|\*/,

  // ── Submit + confirmation ───────────────────────────────────────────────────
  // Clicked by the orchestrator only after Parth's ✅. Synthesized click is honored
  // (needsCDP:false). Ashby button reads "Submit Application"; on success it swaps the
  // form for a confirmation panel ("your application has been submitted").
  submit: {
    selectors: ['button[type="submit"]', 'button[class*="_submit"]'],
    textMatch: "^(submit application|submit|apply)$",
    needsCDP: false,
    confirm: {
      urlIncludes: ["success", "thank", "confirmation", "submitted"],
      textIncludes: ["your application has been submitted", "application submitted",
        "thank you for applying", "thanks for applying", "we've received your application"],
    },
  },
};

export default ASHBY;
