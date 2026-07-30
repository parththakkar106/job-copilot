// Lever adapter — the "DOM map" for the Apply engine (Tier 0 fill).
//
// Same contract as adapters/greenhouse.js: taxonomy.yaml + profile.json say WHAT to
// fill; this file says HOW/WHERE to fill it on a real Lever form. See greenhouse.js
// for the responsibility split.
//
// LIVE-VERIFIED 2026-07-09 against jobs.lever.co/coupa/<id>/apply (DOM inspection,
// extension not yet loaded). Lever is the simplest of the three ATSs:
//   * Server-rendered classic form — NO React combobox weirdness, NO CDP needed.
//     Synthesized input/click/change events are honored throughout.
//   * Core fields are addressed by a stable `name` attribute (NOT id):
//       name, email, phone, location, org (current company), urls[LinkedIn] / [GitHub]
//       / [Portfolio] / [Other], comments (textarea). There is ONE full-name field
//       `name` (not first/last) — profile.identity.full_name.
//   * Resume file input is input[name="resume"] (#resume-upload-input), behind an
//     "Attach resume/CV" dropzone. DataTransfer inject (attach.js) applies.
//   * Location is a plain <input name="location" class="location-input"> with a
//     places-style suggestion dropdown as a sibling. Free text is accepted, so we
//     type the value and do NOT depend on picking a suggestion.
//   * Custom questions live under `cards[<UUID>][field<N>]` names and render as native
//     <select> (yes/no/maybe, country, "how did you hear"), text inputs, or
//     radio/checkbox groups. IDs change per job — discover at runtime, derive the
//     question label, classify via taxonomy.
//   * Every custom question is wrapped in `.application-question`; the human-readable
//     prompt is `.application-question .application-label` (an "✱" suffix marks required).
//   * Radio/checkbox OPTIONS: each <input> is wrapped in its own <label> whose text is
//     the option, and the input's `value` attribute IS the human option text
//     (e.g. value="He/him"). So match an answer to an option by value or label text,
//     then click the label.
//   * Submit button reads "Submit application" — never auto-clicked.

export const LEVER = {
  id: "lever",

  // No trusted-click requirement: classic form honors synthesized events.
  needsCDP: false,

  urlPatterns: [
    /(?:\/\/|\.)jobs\.lever\.co\//i,
  ],

  // ── Known core fields ───────────────────────────────────────────────────────
  // Addressed by `name`. `selectors` is an ordered candidate list; first hit wins.
  coreFields: [
    { key: "full_name", profilePath: "identity.full_name",
      control: "text", selectors: ['input[name="name"]'] },
    { key: "email", profilePath: "identity.email",
      control: "text", selectors: ['input[name="email"]', 'input[type="email"]'] },
    { key: "phone", profilePath: "identity.phone",
      control: "text", selectors: ['input[name="phone"]', 'input[type="tel"]'] },

    // Location: free-text with an optional autocomplete dropdown. Type the plain
    // string; do not require a suggestion pick (Lever accepts free text).
    { key: "location", profilePath: "identity.location_display", control: "text",
      selectors: ['input[name="location"]', 'input.location-input'] },

    // Current employer.
    { key: "org", profilePath: "employment.current_employer", control: "text",
      optional: true, selectors: ['input[name="org"]'] },

    // Profile links (Lever exposes named url slots).
    { key: "linkedin", profilePath: "links.linkedin", control: "text", optional: true,
      selectors: ['input[name="urls[LinkedIn]"]'] },
    { key: "github", profilePath: "links.github", control: "text", optional: true,
      selectors: ['input[name="urls[GitHub]"]'] },
    { key: "portfolio", profilePath: "links.portfolio", control: "text", optional: true,
      selectors: ['input[name="urls[Portfolio]"]'] },

    // Resume: DataTransfer inject (see attach.js).
    { key: "resume", control: "file", attach: "resume",
      selectors: ['input[name="resume"]', '#resume-upload-input',
        'input[type="file"][name="resume"]'] },
  ],

  // ── Custom questions (per-job) ──────────────────────────────────────────────
  // Names look like cards[<UUID>][field<N>]. Discover at runtime; label from the
  // wrapping .application-question; classify via taxonomy.
  customQuestion: {
    // Match by the cards[...] name pattern.
    match: /^cards\[[0-9a-f-]+\]\[field\d+\]/i,
    containerSelectors: ['.application-question', 'li.application-question',
      'ul.application-question'],
  },

  // ── EEO / demographic section ───────────────────────────────────────────────
  // Lever renders EEO as native <select> (gender, race, veteran, disability) inside
  // .application-question blocks near the bottom, or occasionally as radio groups.
  // No dedicated wrapper id — classified by label like any other question.
  eeo: {
    sectionSelectors: ['[class*="eeo" i]', '[class*="demographic" i]'],
    verify: false, // rendered as normal native selects/radios; label-classified
  },

  // ── Dropdown strategy ───────────────────────────────────────────────────────
  // Lever dropdowns are native <select> — fill.js's setNativeSelect handles them.
  // No custom combobox widget on Lever.
  dropdown: { kind: "native" },

  // ── Setter strategies per control type ──────────────────────────────────────
  setters: {
    text: "nativeSetter(el,'value',v); dispatch input+change",
    select: "selectByVisibleText(el,v); dispatch change",
    file: "attachViaDataTransfer(el,bytes,filename); dispatch change",
    // Radio/checkbox option group: match answer to option value/label, click the label.
    choice: "clickChoiceByText(group,v)  // group shares a name; option value == human text",
  },

  // ── Label extraction ────────────────────────────────────────────────────────
  // For a control inside a custom question, the prompt lives in the sibling/ancestor
  // .application-label, NOT a label[for]. fill.js tries the question-container label
  // first for grouped controls, then falls back to this order.
  labelStrategy: ["question-container .application-label", "label[for=ID]",
    "closest('label')", "aria-label", "placeholder", "name"],

  // Container that scopes one question (used to derive a group/question label and to
  // collect radio/checkbox option sets).
  questionContainerSelectors: ['.application-question', 'li.application-question'],

  // Radio/checkbox option value carries the human text on Lever.
  optionValueIsText: true,

  // ── Required / knockout detection ───────────────────────────────────────────
  // Required custom questions carry an "✱" in .application-label; core fields use the
  // `required` attr. A required custom question taxonomy can't answer must escalate.
  requiredSignals: ['[aria-required="true"]', '[required]'],
  requiredLabelMarker: /✱|\*/,

  // ── Submit + confirmation ───────────────────────────────────────────────────
  // Clicked by the orchestrator only after Parth's ✅. Synthesized click is honored
  // (needsCDP:false). Lever button reads "Submit application"; on success it redirects
  // to jobs.lever.co/<co>/<id>/thanks.
  submit: {
    selectors: ['button[type="submit"]', '.template-btn-submit', 'button.postings-btn'],
    textMatch: "^(submit application|submit|apply)$",
    needsCDP: false,
    confirm: {
      urlIncludes: ["/thanks", "thank", "confirmation", "submitted"],
      textIncludes: ["thank you for applying", "application received",
        "your application has been submitted", "thanks for applying"],
    },
  },
};

export default LEVER;
