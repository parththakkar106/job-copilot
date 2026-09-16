// Greenhouse adapter — the "DOM map" for the Apply engine (Tier 0 fill).
//
// Responsibility split (see CLAUDE.md, Apply engine #2):
//   - taxonomy.yaml + profile.json  = the "question map": label -> bucket -> value.
//   - THIS FILE                      = the "DOM map": how to locate a control on a
//                                      real Greenhouse page and set its value correctly.
//
// Grounded in the Greenhouse Job Board API field names
// (https://developers.greenhouse.io/job-board.html). Selectors marked `VERIFY`
// still need confirmation against a live form (do this when off the RC session:
// load the unpacked extension, open a public Greenhouse board, inspect the DOM).
//
// Two front-ends exist and differ in the DOM:
//   LEGACY  *.greenhouse.io/embed/job_app  -> server-rendered, native <select>, simple ids.
//   REACT   job-boards.greenhouse.io       -> React app, custom react-select comboboxes
//           (must click-open then click-option, NOT set .value).
//
// LIVE-VERIFIED 2026-07-09 against job-boards.greenhouse.io/vercel and /chime:
//   * boards.greenhouse.io/<tenant> now 301-REDIRECTS to job-boards (React). The legacy
//     hosted board is being retired — build React-first; legacy survives mainly on
//     company-site /embed/job_app iframes. React is the default path.
//   * Core ids (#first_name/#last_name/#email/#phone, input#resume, input#cover_letter)
//     all resolve on React. React drops the `name` attr, so the input[name="..."]
//     fallbacks match ONLY on legacy — keep them, but the #id is the real hit on React.
//   * NO native <select> on React (0 found); every dropdown is a react-select.
//   * react-select option nodes are [role="option"] (NOT .select__option). Control is
//     .select__control, menu is .select__menu.
//   * Custom questions keep question_<id> ids on React, but render as a text input backing
//     a react-select — route them through the reactSelect setter, not `select`.
//   * Submit button reads "Apply" on React (not "Submit Application").

export const GREENHOUSE = {
  id: "greenhouse",

  // The new React react-select IGNORES synthesized events — dropdowns need OS-trusted
  // clicks via chrome.debugger (CDP). See background.js + fill.js pickReactSelect.
  // (Lever/Ashby set this false; their widgets honor synthesized events.)
  needsCDP: true,

  // Skip fill.js's post-fill state resync. It exists for Ashby, which drops values from
  // its form state after the resume upload. Greenhouse keeps them, so the resync only
  // retypes every text field (seen live on the Vercel form, 2026-09-17).
  stateResync: false,

  // Dropdown widget descriptor consumed by fill.js. Greenhouse = react-select combobox.
  dropdown: {
    kind: "reactSelect",
    control: ".select__control",
    option: ".select__option",
    menu: ".select__menu",
    committed: ".select__single-value, .select__multi-value",
    // Rendered INSTEAD of rows when a search comes back empty. Its loading sibling
    // (--loading) deliberately isn't matched: that one means "wait", this one means "stop".
    noResults: ".select__menu-notice--no-options",
  },

  // Only run the adapter on pages we recognize as a Greenhouse application form.
  // Tested against location.href, so anchor the host to `//` (authority start) or a
  // `.` (subdomain) — NOT `^`, since the host is always preceded by `https://`.
  urlPatterns: [
    /(?:\/\/|\.)greenhouse\.io\/embed\/job_app/i,   // legacy embed
    /(?:\/\/|\.)boards\.greenhouse\.io\//i,          // legacy hosted board
    /(?:\/\/|\.)job-boards\.greenhouse\.io\//i,      // new React board
  ],

  // Distinguishes which front-end we're on (setter strategy differs). VERIFY the
  // React host reliably serves the new UI; some tenants still embed the legacy form.
  isReactFrontend() {
    return /job-boards\.greenhouse\.io/i.test(location.hostname);
  },

  // ── Known core fields ───────────────────────────────────────────────────────
  // Stable, human-readable names per the Job Board API. Filled DIRECTLY from the
  // given profile.json path — no label classification needed. `selectors` is an
  // ordered candidate list; first match on the page wins. `control` picks setter.
  coreFields: [
    { key: "first_name", profilePath: "identity.first_name",
      control: "text", selectors: ["#first_name", 'input[name="first_name"]',
        'input[autocomplete="given-name"]'] },
    { key: "last_name", profilePath: "identity.last_name",
      control: "text", selectors: ["#last_name", 'input[name="last_name"]',
        'input[autocomplete="family-name"]'] },
    { key: "email", profilePath: "identity.email",
      control: "text", selectors: ["#email", 'input[name="email"]',
        'input[type="email"]'] },
    { key: "phone", profilePath: "identity.phone",
      control: "text", selectors: ["#phone", 'input[name="phone"]',
        'input[type="tel"]'] },

    // Location: DIVERGES by front-end (live-verified 2026-07-09).
    //   LEGACY: google-places autocomplete text box (#job_application_location) — the
    //           `autocomplete` setter types + picks a suggestion.
    //   REACT : NO free-text location/places field. The "#country" react-select is NOT
    //           handled here — it falls through to the classify loop, where the taxonomy
    //           "Country" rule fills it ("United States"). fill.js treats a missing
    //           location control on the React front-end as expected (silent skip).
    { key: "location", profilePath: "identity.address", control: "autocomplete",
      selectors: ['#job_application_location', '#auto_complete_input',
        'input[name="job_application[location]"]'] },

    // Resume: file input behind an "Attach" drag/drop zone. id CONFIRMED input#resume on
    // React. We inject bytes via DataTransfer (see attach.js).
    { key: "resume", control: "file", attach: "resume",
      selectors: ['input#resume', 'input[type="file"][name="resume"]',
        'input[type="file"][id*="resume" i]'] },

    // Cover letter file input (only when present/required). id CONFIRMED input#cover_letter.
    { key: "cover_letter", control: "file", attach: null, optional: true,
      selectors: ['input#cover_letter', 'input[type="file"][name="cover_letter"]'] },
  ],

  // ── Custom questions (numeric, per-job) ─────────────────────────────────────
  // Names look like `question_12345` (+ `_content`/`_url` for file answers). The IDs
  // change on every job, so DO NOT hardcode. Discover them at runtime, derive a label,
  // and route the label through taxonomy.yaml. LinkedIn / GitHub / "how did you hear"
  // / work-auth knockouts all arrive as custom questions.
  customQuestion: {
    // Elements whose id/name matches this are custom questions.
    match: /^question_\d+/,
    // Single-select renders as native <select> (legacy) or react-select (new UI);
    // multi-select as checkboxes or a multi react-select. Detect by control type.
    // options carry a value ID (legacy <option value>) that the setter must match by
    // VISIBLE TEXT, since we only know the human answer, not Greenhouse's option IDs.
  },

  // ── EEO / demographic section ───────────────────────────────────────────────
  // Separate block near the form bottom. API shape: demographic_answers[][question_id]
  // + answer_options[][answer_option_id]. In the DOM these are dropdowns with a
  // "Decline to self-identify" option. Route by label -> profile.eeo.* (gender,
  // race_ethnicity, veteran_status, disability_status). Match option by visible text.
  eeo: {
    // Legacy wraps in #eeo_fields / #demographic_*. On React these ids do NOT exist,
    // and the Vercel form tested had no EEO block at all — so the React wrapper selector
    // is STILL UNVERIFIED (need a live React form that includes demographic questions;
    // they are likely just more question_<id> react-selects with gender/race/veteran/
    // disability labels rather than a separate wrapped section). Until confirmed, EEO on
    // React is classified by label like any other custom question, not by wrapper scope.
    sectionSelectors: ['#eeo_fields', '#demographic_questions',
      '[id*="demographic" i]', '[aria-label*="EEO" i]'],
    verify: true, // React wrapper unconfirmed
  },

  // ── Setter strategies per control type ──────────────────────────────────────
  // The fiddly part: React/legacy controls ignore a bare `.value =`. Each setter
  // documents the required event sequence. Implemented in fill.js; described here so
  // the map is the single source of truth for HOW to write each control.
  setters: {
    // Native text/tel/email input: set via the native value setter, then dispatch
    // input + change so React's synthetic-event tracker registers the change.
    text: "nativeSetter(el,'value',v); el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true}));",

    // Native <select> (legacy custom/EEO): find <option> by visible text (case-insensitive,
    // trimmed), set selectedIndex, dispatch change. Fall back to value match.
    select: "selectByVisibleText(el,v); el.dispatchEvent(new Event('change',{bubbles:true}));",

    // react-select combobox (new UI): can't set .value. Sequence: click the control to
    // open the listbox, wait for options to render, click the option whose text matches,
    // then blur. CONFIRMED 2026-07-09: option node is [role="option"] (NOT .select__option);
    // control is .select__control, menu is .select__menu.
    reactSelect: "openAndPickOption(el,v) /* click .select__control -> [role=option] match text -> click */",

    // Google-places autocomplete (location): type the value, wait for the suggestion
    // list, press the first suggestion (or ArrowDown+Enter). VERIFY whether a pick is
    // mandatory or free text is accepted.
    autocomplete: "typeThenPickFirstSuggestion(el,v)",

    // File input: build a DataTransfer with the cached resume bytes and assign .files,
    // then dispatch change so Greenhouse's uploader picks it up. See attach.js.
    file: "attachViaDataTransfer(el,bytes,filename); el.dispatchEvent(new Event('change',{bubbles:true}));",

    // Radio/checkbox (some knockout questions): click the label/input whose text matches.
    choice: "clickChoiceByText(group,v)",
  },

  // ── Label extraction (for custom + EEO fields) ──────────────────────────────
  // Fallback order confirmed against real autofillers. Used to build the string we
  // hand to taxonomy.yaml for classification.
  labelStrategy: ["label[for=ID]", "closest('label')", "aria-label",
    "aria-labelledby", "placeholder", "name", "id"],

  // ── Required / knockout detection ───────────────────────────────────────────
  // Required: `aria-required="true"`, a `required` attr, or an asterisk in the label.
  // Knockout questions (work auth, relocation) are just required custom questions —
  // no special DOM flag — so a blocking required custom question that taxonomy can't
  // answer deterministically must escalate (Tier 1 / phone), never be guessed.
  requiredSignals: ['[aria-required="true"]', '[required]', 'label:has(.required)',
    'label .asterisk'],

  // ── Submit + confirmation ───────────────────────────────────────────────────
  // Only ever clicked by the background orchestrator AFTER Parth taps ✅ on the
  // Telegram review — the human gate is unchanged. React submit button reads "Apply"
  // (legacy "Submit Application"); needsCDP so the click is OS-trusted like the
  // react-selects. `confirm` = signals that the submission actually landed (URL and/or
  // page text) — a click alone is NOT treated as applied; the confirmation must show.
  submit: {
    selectors: ['#submit_app', 'button[type="submit"]', 'input[type="submit"]'],
    textMatch: "^(apply|submit application|submit)$",
    needsCDP: true,
    confirm: {
      urlIncludes: ["confirmation", "thanks", "thank-you", "success", "submitted"],
      textIncludes: ["thank you for applying", "application submitted",
        "your application has been submitted", "successfully submitted",
        "we've received your application", "thanks for applying"],
    },
  },
};

export default GREENHOUSE;
