// Workday adapter — the "DOM map" for the Apply engine (Tier 0 fill).
//
// Same contract as adapters/ashby.js: taxonomy.yaml + profile.json say WHAT to fill;
// this file says HOW/WHERE on a real Workday form. See greenhouse.js for the split.
//
// LIVE-VERIFIED 2026-08-03 against adobe.wd5.myworkdayjobs.com/external_experienced
// (job R170796) by walking an anonymous application from the job posting through to
// step 3 of 6. Steps 1-3 below are observed behaviour. Steps 4-6 are NOT — the walk was
// stopped before Review because that is where Submit lives.
//
//   * Controls are keyed by `data-automation-id`, which is Workday PLATFORM markup rather
//     than per-tenant config, so this map should carry to the other 10 tenants. CSS classes
//     are emotion-hashed (`css-15rz5ap`) and change per build: NEVER select on them.
//   * Plain DOM. No shadow roots, no cross-origin iframe (unlike Greenhouse embeds).
//   * NO ACCOUNT IS NEEDED TO REACH OR FILL THE FORM. Adobe let the whole flow run
//     anonymously with `utilityButtonSignIn` still showing. Workday collects the account at
//     the END of the flow, not the start, so registration is a step-6 problem — and likely
//     an emailed-verification problem, which this engine does not solve. Do not build
//     registration automation until step 6 is verified.
//
// THE TWO STRUCTURAL DIFFERENCES FROM THE OTHER THREE ADAPTERS:
//
// 1. IT IS NOT ONE PAGE. Six steps behind a client-side router (`multiStep`). The URL
//    NEVER CHANGES across them — it stays on /apply/applyManually the whole way, so the
//    only reliable "where am I" signal is the progress bar. fill.js's one-page/one-report
//    assumption does not survive this; a step driver has to wrap it.
//
// 2. `needsCDP` IS PER-WIDGET, NOT PER-ADAPTER. This is the thing that will bite. Workday
//    mixes widgets with opposite input requirements on the SAME page:
//      - simple listbox dropdowns  -> synthesized .click() commits fine. No CDP.
//      - hierarchical prompt trees -> synthesized events are IGNORED ENTIRELY. CDP.
//      - date spinners             -> synthesized value writes DISPLAY but never commit. CDP.
//    Verified both directions: country/state/degree committed on a plain .click(); the
//    source tree ignored .click() AND a full pointerdown/mousedown/pointerup/mouseup/click
//    sequence, and only moved under a real OS click.

export const WORKDAY = {
  id: "workday",

  // NOT a flat answer — see `cdpWidgets`. Left true so the orchestrator attaches the
  // debugger for the whole fill; the alternative is attach/detach per widget, which
  // races the step transitions.
  needsCDP: true,
  cdpWidgets: ["promptTree", "date"],
  synthesizedOkWidgets: ["text", "textarea", "listbox", "checkbox", "radio", "file"],

  urlPatterns: [
    /\.myworkdayjobs\.com\//i,
  ],

  // ── Entry flow ──────────────────────────────────────────────────────────────
  // The job posting page is NOT the form. `adventureButton` (Workday's name for Apply)
  // opens a modal offering four routes; the form only exists after one is chosen.
  //
  // `useMyLastApplication` is the one that matters at our volume: 143 dream-tier Workday
  // jobs sit across just 11 tenants (CrowdStrike 36, Adobe 33, BofA 22...), and step 2
  // alone demands full work history + education + resume. Applying manually ONCE per
  // tenant and cloning the rest is far cheaper than filling 143 six-step wizards.
  entry: {
    applyButton: '[data-automation-id="adventureButton"]',
    modal: '[data-automation-id="wd-popup-content"]',
    routes: {
      manual: '[data-automation-id="applyManually"]',
      autofillWithResume: '[data-automation-id="autofillWithResume"]',
      useMyLastApplication: '[data-automation-id="useMyLastApplication"]',
      linkedIn: '[data-automation-id="applyWithLinkedIn"]',
    },
    formUrlIncludes: "/apply/",

    // Route is decided per posting by core.apply_log.workday_route(url), reached over the
    // bridge (message "workday_route"), which answers "have we applied to this TENANT
    // before" from applied.json. Warm tenant -> clone; cold tenant -> seed from the resume.
    // Either way Workday does the bulk of the typing and the driver reconciles after.
    routePolicy: "core.apply_log.workday_route",

    // VERIFIED LIVE 2026-08-07. After the modal, Workday navigates to /apply/<routeKey>
    // and that page renders the SAME six-step progress bar, reporting "step 1 of 6" with
    // the route's own name. Without these, step detection reads the upload page as My
    // Information and fills a form that is not there yet.
    routePageNames: ["Autofill with Resume", "Use My Last Application",
                     "Apply Manually", "Apply with LinkedIn"],

    // Tried in order when the decided route is not on offer. `manual` is last because it
    // is the only one that types six steps from nothing.
    routePreference: ["useMyLastApplication", "autofillWithResume", "manual"],
    fallbackRoute: "autofillWithResume",

    // UNVERIFIED. Workday requires an account per tenant and shows a sign-in wall instead
    // of the apply modal when there is no session. Detection exists so the driver can say
    // "sign in first" rather than report a missing Apply button, which is what an
    // unauthenticated page looks like from the inside. Signing in is deliberately NOT
    // automated: it means handling credentials, and a wrong guess here locks an account.
    signIn: {
      verified: false,
      markers: ['[data-automation-id="signInLink"]',
                '[data-automation-id="createAccountLink"]',
                '[data-automation-id="signInSubmitButton"]',
                '[data-automation-id="createAccountSubmitButton"]'],
    },

    // UNVERIFIED. The autofill route asks for the PDF before it will build the form. The
    // file input is the standard one; attachResume() drives it exactly as it does on the
    // other three adapters, so the DataTransfer handling is shared, not re-invented.
    resumeUpload: {
      verified: false,
      input: 'input[type="file"]',

      // THE UPLOAD PAGE HAS ITS OWN NEXT, and the wizard does not exist until it is
      // clicked. An earlier version waited here for the wizard's first-name field to
      // appear on its own; it never can, so the run hung for the full parse budget and
      // then reported a parse that had actually succeeded. Wait for Next to become
      // enabled — that is Workday saying it has accepted and read the file — then click it.
      continueButton: '[data-automation-id="pageFooterNextButton"]',
      // Parsing is async and rebuilds the form when it lands, so nothing may be read or
      // written until Next is live. Generous: a big PDF on a slow tenant takes a while.
      settleTimeoutMs: 45000,
    },

    // THE GUARD THAT MATTERS: "Use My Last Application" copies the previous submission
    // WHOLESALE — its answers AND its resume. The four resume tracks describe the same
    // jobs in different vocabularies, so cloning across a stream boundary staples the
    // quant PDF to an AI role and contradicts every answer on the form. A clone is only
    // safe when the prior application to that tenant used the SAME track pickResume()
    // chose for this one; otherwise fall back to manual and fill fresh.
    //
    // This is also why applied.json should start recording the resume track on submit —
    // without it the stream of a past application is unknowable and every clone is a
    // guess. Until then, treat "track unknown" as a mismatch and fill manually.
    // A clone is now RECONCILED, not refused. The old policy required proving the previous
    // application used the same resume track before cloning, read from
    // applied.json.resume_track — which nothing wrote, so "unknown" was the only possible
    // answer and the clone path could never be taken. That made the volume argument for
    // supporting Workday at all unreachable. resume_track is recorded from now on, but it
    // is not a precondition: the driver clones, then overwrites every field that is
    // missing or disagrees with the track chosen for THIS role, including the attachment.
    clonePolicy: {
      appliedTrackField: "resume_track",
      // THE ONE THING RECONCILING TEXT DOES NOT FIX. A clone carries the previous
      // application's PDF, so a quant CV can arrive on an AI role with every visible
      // answer looking right. The attachment must be re-checked against the chosen track
      // and replaced on mismatch, or the pair still contradicts itself.
      reattachOnTrackMismatch: true,
      // A clone still lands in the 6-step wizard, pre-filled. It must be reviewed and
      // advanced like any other fill, not blind-submitted: the tenant questions on step 3
      // are per-requisition and the clone cannot answer ones the old req never asked.
      stillReviewSteps: true,
    },
  },

  // ── The wizard ──────────────────────────────────────────────────────────────
  multiStep: {
    container: '[data-automation-id="applyFlowPage"]',
    progressBar: '[data-automation-id="progressBar"]',
    activeStep: '[data-automation-id="progressBarActiveStep"]',
    nextButton: '[data-automation-id="pageFooterNextButton"]',
    // "current step 3 of 6|Application Questions"
    stepTextPattern: /step\s+(\d+)\s+of\s+(\d+)/i,

    // Validation is SERVER-SIDE and only runs on Next. A step that looks complete can
    // bounce with `inputAlert` errors — that bounce is the only proof a value committed,
    // and it is how the date-spinner trap below was caught. Always re-read the active
    // step after Next rather than assuming the click advanced.
    errorSelectors: ['[data-automation-id="errorHeading"]', '[data-automation-id="inputAlert"]'],

    // On step 6 the footer button is SUBMIT, not advance. The driver must never click
    // `pageFooterNextButton` blind — check the active step first.
    // A FLOOR, not the answer. The real "am I on the submit page" test is n >= of, read
    // off the progress bar — the step COUNT depends on the entry route, and the autofill
    // flow was measured live at 7 steps with My Information at 2. This only guards the
    // case where the total is missing or unparseable.
    submitStep: 6,

    // Pages are identified by NAME, never by number, for the same reason. Matched loosely
    // (letters only, substring) because tenants reword them: "My Experience" appears as
    // "Experience" and "My Information" as "Personal Information" on some boards.
    // The progress bar updates BEFORE the page behind it renders, so arriving at a step is
    // not the same as the step being ready. Reading too early reports every control as
    // missing — live, step 3 came back with "no Add Another button", "no listbox button"
    // and "file input not found" simultaneously, which is not three broken selectors, it
    // is one empty page. Where a step has a known container, wait for it.
    stepPages: {
      myInformation: '[data-automation-id="applyFlowMyInfoPage"]',
      myExperience: '[data-automation-id="applyFlowMyExpPage"]',
    },

    stepKinds: {
      myInformation: ["My Information", "Personal Information", "Contact Information"],
      myExperience: ["My Experience", "Experience", "Work Experience"],
      questions: ["Application Questions", "Questions"],
      disclosures: ["Voluntary Disclosures", "Disclosures"],
      selfIdentify: ["Self Identify", "Identification", "Disability"],
      review: ["Review", "Summary"],
    },

    steps: [
      { n: 1, name: "My Information",        page: '[data-automation-id="applyFlowMyInfoPage"]', verified: true },
      { n: 2, name: "My Experience",         page: '[data-automation-id="applyFlowMyExpPage"]',  verified: true },
      { n: 3, name: "Application Questions",                                                     verified: true },
      { n: 4, name: "Voluntary Disclosures", verified: false },  // EEO, assumed
      { n: 5, name: "Self Identify",         verified: false },  // disability form + signature
      { n: 6, name: "Review",                verified: false },  // submit lives here
    ],
  },

  // ── Step 1 gate ─────────────────────────────────────────────────────────────
  // VERIFIED AND LOAD-BEARING: on first paint step 1 renders ONLY the country dropdown.
  // Every other field is absent from the DOM until a country commits. A fill that starts
  // by scanning for inputs sees one control and concludes the page is not a form.
  //
  // Worse, the default is GEO-DERIVED, not empty — it arrived pre-set to "India" from the
  // connection's IP while the label still read "Select One". So set it EXPLICITLY every
  // time; treating a non-empty value as "already correct" silently files US applications
  // with a foreign country and poisons the work-authorization answers downstream.
  prerequisite: {
    field: "formField-country",
    profilePath: "identity.address.country_full",
    fallbackValue: "United States of America",
    thenWaitFor: '[data-automation-id="formField-legalName--firstName"]',
  },

  // ── Step 1: My Information (all live-verified) ──────────────────────────────
  // Two id spaces coexist and they do not match each other:
  //   the WRAPPER is `formField-<name>`, the CONTROL inside is `<group>--<name>`
  //   (name--legalName--firstName, address--addressLine1, country--country).
  // Select the wrapper to find a field, the control to set it.
  coreFields: [
    { key: "first_name", profilePath: "identity.first_name", control: "text",
      selectors: ['#name--legalName--firstName'] },
    { key: "last_name", profilePath: "identity.last_name", control: "text",
      selectors: ['#name--legalName--lastName'] },
    { key: "email", profilePath: "identity.email", control: "text",
      selectors: ['[data-automation-id="formField-emailAddress"] input'] },

    { key: "address_line1", profilePath: "identity.address.line1", control: "text",
      optional: true, selectors: ['#address--addressLine1'] },
    { key: "city", profilePath: "identity.address.city", control: "text",
      optional: true, selectors: ['#address--city'] },
    { key: "zip", profilePath: "identity.address.zip", control: "text",
      optional: true, selectors: ['#address--postalCode'] },

    // State is a listbox whose options are full state names — profile's two-letter
    // `state` will not match anything. Verified against a spelled-out name ("Ohio").
    { key: "state", profilePath: "identity.address.state_full", control: "listbox",
      optional: true,
      selectors: ['[data-automation-id="formField-countryRegion"] button[aria-haspopup="listbox"]'] },

    { key: "phone_type", value: "Mobile", control: "listbox", optional: true,
      selectors: ['[data-automation-id="formField-phoneType"] button[aria-haspopup="listbox"]'] },

    // Country phone code is a MULTISELECT, not a listbox — it has no button, so a
    // `button[aria-haspopup]` lookup finds nothing and the field looks missing. It also
    // self-corrects to match the country field (watched it flip India->US), so in practice
    // it needs no action once country is right. Left here documented rather than driven.
    { key: "phone_country_code", control: "multiselect", optional: true, skip: true,
      selectors: ['[data-automation-id="formField-countryPhoneCode"] [data-automation-id="multiSelectContainer"]'] },

    // Workday reformats this itself ("+1 555-123-4567" -> "(555) 123-4567"), so do not
    // compare the read-back value against profile to decide whether the write landed.
    // `compare: "phone"` so the reconcile pass compares digits, not text. Workday's own
    // reformat would otherwise read as a disagreement and rewrite a correct number on
    // every single run.
    { key: "phone", profilePath: "identity.phone", control: "text",
      selectors: ['[data-automation-id="formField-phoneNumber"] input'],
      transform: "stripCountryCode", compare: "phone" },

    // "How Did You Hear About Us?*" — REQUIRED, and a promptTree, not a text box. It has a
    // text input inside it, so naive probing reports control:"text"; typing into it fills
    // the search box and commits NOTHING, and the step bounces on Next. See promptTree.
    { key: "referral_source", profilePath: "preferences.referral_source", control: "promptTree",
      selectors: ['[data-automation-id="formField-source"]'],
      sourceMapPath: "workdaySourceMap" },
  ],

  // ── Step 2: My Experience ───────────────────────────────────────────────────
  // The heaviest step by far, and ALL OF IT IS REQUIRED — Workday will not let you past
  // without work history, education AND a resume. That is materially more than
  // Greenhouse/Lever/Ashby ask for, and it is why `work_history` was added to the profile.
  //
  // FILL FROM THE STREAM-RESOLVED PROFILE, NOT THE RAW ONE. Each work_history entry
  // carries a `by_stream` block because the four resumes describe the same two jobs in
  // four vocabularies; stream.js resolveStream() collapses it using the tag pickResume()
  // already chose. Typing raw-profile text next to a quant PDF on an AI role is the
  // mismatch this whole mechanism exists to avoid. Resolve once, at the top of the
  // application, before any step is filled.
  experience: {
    page: '[data-automation-id="applyFlowMyExpPage"]',
    // Repeat the work block once per entry, clicking `add-button` between them. Entries
    // are already newest-first in the profile, which is the order Workday expects.
    source: "profile.work_history (stream-resolved)",
    fieldMap: {
      jobTitle: "title", company: "employer", location: "location",
      startDate: "start", endDate: "end", roleDescription: "description",
      currentlyWorkHere: "current",
    },
    work: {
      jobTitle: '[data-automation-id="formField-jobTitle"]',       // required
      company: '[data-automation-id="formField-companyName"]',     // required
      location: '[data-automation-id="formField-location"]',
      currentlyWorkHere: '[data-automation-id="formField-currentlyWorkHere"]',
      startDate: '[data-automation-id="formField-startDate"]',     // required
      endDate: '[data-automation-id="formField-endDate"]',
      roleDescription: '[data-automation-id="formField-roleDescription"]',
      addAnother: '[data-automation-id="add-button"]',
      // Verified: ticking `currentlyWorkHere` REMOVES formField-endDate from the DOM.
      // Fill order matters — tick the box first, or the endDate write targets a node that
      // is about to be destroyed and the "To is required" error never clears.
      currentlyWorkHereRemovesEndDate: true,
      // The checkbox id carries an opaque row index (workExperience-11--currentlyWorkHere).
      // Never hardcode the number; scope to the wrapper.
    },
    education: {
      school: '[data-automation-id="formField-schoolName"]',       // required
      degree: '[data-automation-id="formField-degree"]',           // required, LISTBOX
      fieldOfStudy: '[data-automation-id="formField-fieldOfStudy"]',  // promptTree
      gpa: '[data-automation-id="formField-gradeAverage"]',
      firstYear: '[data-automation-id="formField-firstYearAttended"]',
      lastYear: '[data-automation-id="formField-lastYearAttended"]',
    },
    // Degree options are a short fixed list and profile's `education.degree_level` matches
    // them verbatim. Verified list: GED, High School, Associates, Bachelors, Masters,
    // Doctorate, JD. Note it is NOT "Bachelor's Degree" — do not "helpfully" expand it.
    degreeOptions: ["GED", "High School", "Associates", "Bachelors", "Masters", "Doctorate", "JD"],
    skills: '[data-automation-id="formField-skills"]',
    linkedin: '[data-automation-id="formField-linkedInAccount"]',
    resume: {
      // REQUIRED. Standard input[type=file], so attach.js's DataTransfer injection applies
      // as on the other three. The input is visually hidden behind `select-files`; find it
      // by automation-id, never by clicking the button (that opens the OS picker).
      input: '[data-automation-id="file-upload-input-ref"]',
      dropZone: '[data-automation-id="file-upload-drop-zone"]',
      container: '[data-automation-id="attachments-FileUpload"]',
      // The container's text becomes "<name>|<size>|Successfully Uploaded!" — a clean
      // success signal to assert on rather than trusting the change event.
      successText: /Successfully Uploaded/i,

      // A CLONE ARRIVES WITH THE PREVIOUS APPLICATION'S PDF ALREADY ATTACHED, and that is
      // the one mismatch reconciling text cannot fix: every visible answer can be correct
      // while the wrong CV hangs off the form. Our uploads are named Parth_Resume_<tag>.pdf
      // (see cleanResumeName), so the track that is currently attached is readable straight
      // off the container text — for anything we uploaded, which is every clone source from
      // now on.
      // ONE LETTER, not the tag. cleanResumeName() collapses the track tag to its initial
      // on the way out ("ds" -> Parth_Resume_d.pdf), so the filename never contains the
      // tag itself and a pattern demanding two or more characters matches nothing. The
      // four tags have distinct initials (q/d/t/a), which is what makes this readable at
      // all — asserted in the tests so a fifth track cannot quietly collide.
      attachedNamePattern: /_([a-z])\.pdf\b/i,
      // UNVERIFIED. Replacing an attachment needs Workday's own remove control; a second
      // upload may otherwise append rather than replace, leaving BOTH CVs on the form.
      removeButton: '[data-automation-id="delete-file"]',
      removeVerified: false,
    },
  },

  // ── Step 3: Application Questions ───────────────────────────────────────────
  // Ids are OPAQUE HEX and per-requisition (formField-305a1163be7301083ded7092b201bf8a).
  // Nothing about them is stable or semantic, so these are pure taxonomy territory:
  // extract the label, classify, answer. Verified question shapes on the Adobe req:
  //   - "Are you of legal age to work..."        listbox Yes/No  -> attestations.age_18_or_older
  //   - "Willing to submit a background check"   listbox Yes/No  -> attestations.background_check_consent
  //   - "Can you provide documentation ... right to work"        -> work_authorization.authorized_us
  //   - "Able to work on a daily basis ... willing to relocate"  -> work_authorization.relocation
  //   - "Have you ever worked at Adobe in the following capacity" CHECKBOX GROUP whose
  //     safe answer is the opt-out option "I have not worked for Adobe in the past."
  //     A checkbox group with an opt-out row is a shape the other adapters never hit.
  applicationQuestions: {
    idPattern: /^formField-[0-9a-f]{20,}$/i,
    // The label is not a direct <label> child on every question; fall through the ladder.
    labelFrom: ["label", "legend", "aria-label", "textContent"],
  },

  // ── What the taxonomy pass must NOT touch ───────────────────────────────────
  // The driver runs a taxonomy pass over EVERY step, not just 3-5, because steps 1 and 2
  // mix tenant questions in among the core fields (Adobe puts a required "Have you been
  // employed by Adobe in the past?" on step 1). These ids are the ones the driver already
  // set by hand from explicit profile paths; re-classifying them would re-answer a solved
  // field from a fuzzy label match. Everything NOT in this list is fair game.
  //
  // `formField-candidateIsPreviousWorker` is deliberately absent: it is a tenant question
  // wearing a semantic id, and the taxonomy is exactly what should answer it.
  customQuestion: {
    containerSelectors: ['[data-automation-id^="formField-"]'],
    knownCoreIds: [
      "formField-country", "formField-legalName--firstName", "formField-legalName--lastName",
      "formField-addressLine1", "formField-addressLine2", "formField-city",
      "formField-countryRegion", "formField-postalCode", "formField-emailAddress",
      "formField-phoneType", "formField-countryPhoneCode", "formField-phoneNumber",
      "formField-extension", "formField-source",
      // step 2 — set from work_history / education, not from labels
      "formField-jobTitle", "formField-companyName", "formField-location",
      "formField-currentlyWorkHere", "formField-startDate", "formField-endDate",
      "formField-roleDescription", "formField-schoolName", "formField-degree",
      "formField-fieldOfStudy", "formField-gradeAverage",
      "formField-firstYearAttended", "formField-lastYearAttended",
    ],

    // NOT QUESTIONS. The resume upload is owned by the attach path and no answer could
    // satisfy it, but it renders as a formField- wrapper and so reached the taxonomy pass
    // as "unanswered" — which paused the entire run for input on a field that had just
    // been filled successfully. Its wrapper arrives with an EMPTY name
    // (data-automation-id="formField-"), so the known-core-ids list cannot exclude it.
    //
    // Two overlapping tests because neither alone holds: the file input disappears once
    // the upload completes and Workday swaps in a file chip, and the container ids are
    // tenant-stable but unverified beyond Adobe.
    skipContaining: ['input[type=file]',
                     '[data-automation-id="attachments-FileUpload"]',
                     '[data-automation-id="file-upload-drop-zone"]',
                     '[data-automation-id="file-upload-input-ref"]'],
    // Last-resort backstop, matched against the wrapper's label. The prompt text outlives
    // the widget's own DOM, which is what makes it the reliable signal here.
    skipLabelPattern: /upload either|drop files|select files|\b\d+\s*MB max\b/i,
  },

  // Questions whose correct answer depends on WHO IS ASKING, so no fixed profile field can
  // hold it. The taxonomy resolves a static dotted path, so the only answer it could give
  // here is a hardcoded "No" — a false statement the day he applies to a former employer,
  // and one nothing would ever flag. Resolved from work_history instead; an undecidable
  // case is asked rather than guessed.
  //
  // Observed live on Adobe (formField-candidateIsPreviousWorker, required, Yes/No radios).
  // The wording varies by tenant, hence the pattern rather than an id.
  derivedQuestions: [
    { key: "priorEmployer",
      pattern: /\b(have you|were you)\b.*\b(employed|worked)\b.*\b(here|us|for us|in the past|previously|before)\b|\bprevious(ly)? (employed|worked)\b.*\b(by|at|for)\b/i,
      resolve: "priorEmployerAnswer" },
  ],

  // ── promptTree: the widget that needs CDP ───────────────────────────────────
  // Workday's hierarchical prompt. Used by "How Did You Hear About Us?", Field of Study,
  // Skills. Everything about it is a trap:
  //
  //  * It IGNORES synthesized events. Verified twice: `.click()` on a row did nothing, and
  //    a full PointerEvent/MouseEvent sequence with correct coordinates did nothing either.
  //    Only an OS-level click moves it. This is the Greenhouse react-select problem again.
  //  * Options live in a BODY-LEVEL PORTAL, not inside the formField wrapper. Querying
  //    within the field finds zero rows.
  //  * Rows are `menuItem[role=option]` inside `activeListContainer`. The inner
  //    `promptOption`/`promptLeafNode` nodes are text holders, not click targets.
  //  * IT IS TWO LEVELS. Level 1 is categories (Adobe Source, Job Board, Social Media,
  //    Through my University); the answer is a leaf inside one of them.
  //  * THE CLICK MUST LAND ON THE RADIO CIRCLE, NOT THE ROW TEXT. Clicking the text only
  //    highlights the row; the selection never commits. Cost an hour to find.
  //  * The list is VIRTUALIZED — only rendered rows are queryable, so a leaf that is
  //    scrolled out of view is not findable by text and needs scrolling first.
  //  * The panel COLLAPSES BACK TO ROOT on blur, and any tool round-trip between the
  //    drill-in click and the leaf click is enough to lose it. Read coordinates with a
  //    non-focusing DOM read, never a screenshot round-trip, between the two clicks.
  //  * The search box does NOT filter the tree. Typing "Company" left all six categories
  //    showing. It is decorative for our purposes; drill, do not type.
  promptTree: {
    field: '[data-automation-id="multiSelectContainer"]',
    portal: '[data-automation-id="activeListContainer"]',
    row: '[data-automation-id="menuItem"]',
    rowText: '[data-automation-id="promptOption"]',
    selected: '[data-automation-id="selectedItem"]',
    // Click target within the row. Measured live: the row spanned x=410..754 and the
    // radio sat at x=432, i.e. ~22px in from the row's LEFT edge. Offset from the left,
    // not the centre — the panel width varies with the longest option text, so a
    // centre-relative offset drifts off the radio on a narrower field.
    clickTarget: "radio",
    radioOffsetFromLeft: 22,
    needsCDP: true,
    levels: 2,
    committedText: /\d+ item selected/i,
  },

  // Referral-source vocabulary is TENANT-SPECIFIC and cannot come from one profile string.
  // profile's "Company website" does not exist on Adobe; the nearest leaf is
  // Adobe Source > Adobe.com. Every tenant will need its own mapping, so this belongs in
  // companies.yaml per tenant rather than in profile.json. Ladder used when unmapped:
  //   "<Company>.com" -> "Corporate Website" -> "Company Website" -> Job Board > LinkedIn.
  workdaySourceMap: {
    _note: "Per-tenant. Key is the tenant slug, value is the [category, leaf] path.",
    adobe: ["Adobe Source", "Adobe.com"],
  },

  // ── date: the other widget that needs CDP ───────────────────────────────────
  // Split month/year spinner sections, each its own input.
  //
  // THE TRAP, verified the expensive way: a synthesized native-setter write DISPLAYS the
  // value — the field read back "current value is 1/2024" and looked correct on screen —
  // but never commits to Workday's validation store, so Next bounced with "The field From
  // is required" while showing 01/2024. This is the Ashby "filled but missing" failure in
  // a new costume, and worse, the synthesized write POISONS the section: retyping over it
  // with real keystrokes did not clear the error either. The field had to be cleared
  // (8x Backspace) and then typed fresh with trusted keys before it committed.
  date: {
    wrapper: '[data-automation-id="dateInputWrapper"]',
    month: '[data-automation-id="dateSectionMonth-input"]',
    year: '[data-automation-id="dateSectionYear-input"]',
    needsCDP: true,
    // Never setNativeValue these. Clear first, then type MMYYYY as trusted keystrokes;
    // the widget auto-advances month -> year.
    strategy: "clearThenTypeTrusted",
    clearKeystrokes: 8,
    typeFormat: "MMYYYY",
  },

  // ── Simple listbox (no CDP) ─────────────────────────────────────────────────
  // button[aria-haspopup="listbox"] -> click to open -> options in a portal -> click the
  // one whose text matches. Verified committing on synthesized clicks for country (251
  // options), state and degree.
  //
  // THE TRAP: a document-wide `[role="option"]` query is WRONG. Workday keeps a second,
  // permanently-present listbox in the DOM — `selectedItemList`, holding `selectedItem`
  // nodes for values already chosen elsewhere on the page. On the country field that decoy
  // contributed a stray "India (+91)" (owned by the phone-code multiselect) ahead of the
  // 251 real options. Scope to the popup and exclude selectedItemList.
  dropdown: {
    kind: "workdayListbox",
    control: 'button[aria-haspopup="listbox"]',
    option: '[role="option"]',
    optionScope: '[role="listbox"]:not([data-automation-id="selectedItemList"])',
    excludeOption: '[data-automation-id="selectedItem"]',
    expandedAttr: "aria-expanded",
    typePrefixOnly: false,
    placeholderText: /^select one$/i,
  },

  // ── Label extraction ────────────────────────────────────────────────────────
  // Required fields carry a trailing "*" in an <abbr>; strip it before classification or
  // every label arrives with a spurious asterisk and misses its taxonomy entry.
  labelStrategy: ["label[for=ID]", "closest('[data-automation-id^=formField-]') label",
    "aria-label", "aria-labelledby", "placeholder", "id"],
  labelStripPattern: /\s*\*\s*$/,

  questionContainerSelectors: ['[data-automation-id^="formField-"]', 'fieldset'],
  optionValueIsText: false,

  // Radio/checkbox option ids are opaque and unstable ("eeap3"/"eeap4" for a Yes/No on
  // Adobe). ALWAYS match on the associated label's text, never on id or index.
  choiceMatch: "labelText",

  // ── Required detection ──────────────────────────────────────────────────────
  requiredSignals: ['[aria-required="true"]', '[required]'],
  requiredMarkerSelector: '[data-automation-id^="formField-"] abbr',
  requiredLabelMarker: /\*/,

  setters: {
    text: "nativeSetter(el,'value',v); dispatch input+change",
    listbox: "click button -> scope to popup listbox -> click [role=option] matching text",
    promptTree: "CDP: trusted-click category row -> trusted-click leaf RADIO",
    date: "CDP: clear 8x Backspace -> trusted-type MMYYYY",
    file: "attachViaDataTransfer(el,bytes,filename); assert 'Successfully Uploaded!'",
    choice: "clickChoiceByText(group,v)  // match the associated label's text",
  },

  // ── Submit ──────────────────────────────────────────────────────────────────
  // UNVERIFIED — step 6 was deliberately not reached. `pageFooterNextButton` is the
  // per-step advance and on step 6 it becomes the submit; the selectors below are a guess
  // and MUST be confirmed against a live Review page before anything clicks them.
  submit: {
    verified: false,
    selectors: ['[data-automation-id="pageFooterSubmitButton"]'],
    textMatch: "^submit$",
    needsCDP: false,
    confirm: {
      urlIncludes: ["submitted", "thank"],
      textIncludes: ["you have successfully submitted", "thank you for applying"],
    },
  },
};

export default WORKDAY;
