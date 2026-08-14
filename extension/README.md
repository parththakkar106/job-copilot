# Apply engine — browser extension

An MV3 extension that fills Greenhouse, Lever and Ashby application forms from the shared
data layer (`profile.json` + `taxonomy.yaml`), attaches the right resume, and escalates
anything it cannot answer confidently.

**It never submits by itself.** Submitting is always a deliberate tap, either yours on the
page or an approval you sent from Telegram.

It has to run in your own browser profile: these forms need real cookies, and Greenhouse's
dropdowns only accept trusted input events, which rules out headless and cloud runners.

## Layout

| File | Role |
|------|------|
| `manifest.json` | MV3 manifest, scoped to the three ATS hosts |
| `fill.js` | the content script: find controls → classify → set → report |
| `classify.js` | JS port of the Python taxonomy resolver (parity-tested against it) |
| `resume_pick.js` | JS port of the best-of-N resume chooser (parity-tested) |
| `attach.js` | DataTransfer injection, which is how a file lands in an upload input |
| `background.js` | service worker: bridge socket, and the CDP calls Greenhouse needs |
| `popup.html` / `popup.js` | dashboard over every pile, and the manual run controls |
| `adapters/*.js` | one DOM map per ATS — selectors, control kinds, event sequences |
| `build.py` | stages `taxonomy.json`, `profile.json` and the resumes into this folder |

The split that matters: **`taxonomy.yaml` says *what* to fill** (semantic, ATS-agnostic),
**`adapters/*.js` say *how and where*** on one specific ATS. A new ATS should be a new
adapter and nothing else.

## Two ways to run it

**Panel mode** — a floating panel mounts on any recognised application form. *Fill* fills
everything deterministic and shows whatever is left as cards you answer in place, with
drafts offered per question. Telegram stays silent. This is the mode for applying to one
role while sitting at the keyboard.

**Queue mode** — three phases over disk-backed state. Phase 1 opens each queued job, fills
it, sends the open questions to the bridge and closes the tab, asking nobody. Phase 2 is
you answering on Telegram whenever you get to it. Phase 3 reopens the approved jobs,
refills them with your stored answers, diffs the result against what you approved, and
submits only if they still match.

## Installing and updating

```powershell
python extension/build.py          # stage taxonomy + profile + resumes into extension/
```

Then `chrome://extensions` → Developer mode → **Load unpacked** → `extension/`.

**Any change to a file in this folder needs a reload of the unpacked extension** — that
includes a `build.py` re-run, since it rewrites the staged data the content script reads.
This is the single most common reason a fix "did not work".

## Per-ATS notes

The three adapters exist because these forms behave very differently.

### Greenhouse

- Custom questions render as text-input-backed **react-selects**, not native `<select>`.
  They ignore synthesized events entirely, so this adapter sets `needsCDP: true` and drives
  them with **trusted clicks via `chrome.debugger`**, attached for the fill and detached in
  a `finally`.
- One consequence worth knowing: `chrome.debugger` allows **one client per tab**, so any
  other tool attached to that tab (DevTools, a browser-automation agent) will take the
  debugger and hang the fill. Nothing else may be attached while Greenhouse fills.
- **Typeahead dropdowns run a server-side search on exactly the string typed.** A value
  with a parenthetical in it returns zero options and the field silently fails, so queries
  are stripped and tried as a ladder of progressively broader forms, while the option
  *match* still uses the full value. Type a clean search string, match on the full value.
- The school list is one shared canonical database, and it genuinely does not contain every
  institution. Education fields therefore fall back to the list's own **"Other"** row rather
  than failing a required field forever. That fallback is gated to education fields only —
  on a location or an EEO dropdown "Other" is a wrong answer, not a safe default.
- **Greenhouse rebuilds its education block after the resume parses**, re-keying the rows.
  A node captured before that is detached, and a detached element's bounding rect is all
  zeros, so a click computed from it lands in the page corner. Every click re-resolves the
  control first. The general rule: never click a node captured before an async page event
  without re-checking that it is still connected and has a non-zero rect.
- Many companies embed the form as a cross-origin iframe on their own careers site, where a
  content script in the top frame sees no fields. Mounting inside the iframe is not the fix
  (fixed positioning pins to the iframe's own viewport, and CDP coordinates are relative to
  the top-level one). The fix is the **legacy embed endpoint**, which serves the same form
  standalone at top level.

### Lever

Classic server-rendered form. Synthesized events are honoured, so no CDP. Core fields go by
`name`, custom questions are native `<select>`, text, or radio/checkbox groups. Radio
options wrap their input in a `<label>` whose text is the human-readable option, and the
group handler classifies the *question* and clicks the matching option.

### Ashby

React, but it honours synthesized events, so no CDP either. Location is a combobox
typeahead; short single-selects render as labelled radios rather than a select.

The thing to know about Ashby: **it re-initialises form state asynchronously after the
resume parses, discarding a varying random subset of values already set** while the DOM
keeps displaying them. Text is therefore typed per character rather than set in one shot,
and values are re-asserted from intent at submit time. Do not chase a perfect first fill —
the retry loop is the designed cure. Submitting an invalid Ashby form is safe and is used
as a validation probe: it renders inline errors and does not navigate.

## Things this deliberately does not do

- **It does not fetch an email verification code on its own initiative.** Some boards gate
  submission behind a code sent to your inbox. The extension will read that code out of
  your mail and fill it — but only after *you* act: you clicked Submit, or you tapped the
  panel's 🔐 button. Nothing polls your inbox, and the unattended queue never triggers a
  fetch by itself. If no code can be found, the old behaviour stands: it parks the
  application with a reason and tells you. See `bridge/mailcode.py` for the matching rules
  (only mail that arrived after that click; never a code from an unrelated message).
- **It does not submit unattended in panel mode**, and in queue mode it only submits what
  you explicitly approved, after re-checking the form still matches that approval.

## Tests

```powershell
node extension/test_fill_units.mjs
```

Unit tests over the pure functions the batch flow's correctness rests on — question
identity across phases, the approval diff, the search ladder, degree/level mapping and the
stale-node resolver. `fill.js` is a content script with no exports, so the harness pulls
the real function source out of the file rather than testing a copy.

## References

- Greenhouse Job Board API (canonical field names): https://developers.greenhouse.io/job-board.html
