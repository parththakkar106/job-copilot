# Apply engine — browser extension (Next #2)

MV3 extension that fills Greenhouse / Lever / Ashby application forms from the
shared data layer (`profile.json` + `taxonomy.yaml`), attaches the right resume,
and escalates low-confidence fields to the phone. Submit is always the user's tap.

## Status

Being built as a thin **Tier-0-on-Greenhouse** slice first (silent deterministic
fill on one ATS, no AI / no Telegram yet), then widened.

| Piece | State |
|-------|-------|
| `adapters/greenhouse.js` — DOM map (selectors + setter strategies) | **Live-verified & working** (React react-select via CDP trusted clicks). Now carries `needsCDP:true` + a `dropdown` descriptor consumed generically by fill.js. |
| `adapters/lever.js` — DOM map | **Live-verified 2026-07-10** on jobs.lever.co/coupa (DOM dry-run). Classic server-rendered form, `needsCDP:false`. Core by `name`, native `<select>`, radio/checkbox groups, `input[name=resume]`. |
| `adapters/ashby.js` — DOM map | **Live-verified 2026-07-10** on jobs.ashbyhq.com/{notion,plaid} (DOM dry-run). React but honors synthesized events, `needsCDP:false`. `_systemfield_*` ids, combobox typeahead location, labeled-radios incl. full EEO block. |
| `manifest.json` (MV3) | **Built** — scoped to GH/Lever/Ashby, popup action, WAR for dynamic imports (all 3 adapters). |
| `classify.js` (taxonomy resolver port) | **Built + parity-verified** against `profile.py` (35 labels, all buckets identical). |
| `fill.js` content script (find controls → classify → set) | **Generalized + live-verified** — picks adapter by host; adapter-driven dropdown kinds (reactSelect/comboboxTypeahead/native); radio/checkbox **group** handler (classify the question, click the option). CDP attaches only when `needsCDP`. |
| `attach.js` DataTransfer resume injection | **Built** — untested against a live uploader. |
| `resume_pick.js` (ports profile.py best-of-4) | **Built + parity-verified** (7 titles, identical tag+score). |
| `popup` (fill button + report) | **Built** — renders filled / needs-input / skipped / flagged. |
| `build.py` (stage data layer → extension/) | **Built + runs** (taxonomy.json + profile + 4 resumes). |

## How the two maps combine

```
Greenhouse page
  → coreFields: known ids (#first_name…) filled directly from profile.json
  → every other control: extract label → taxonomy.yaml classify → profile value
  → set value per control type (adapters/greenhouse.js `setters`)
  → resume: DataTransfer attach
  → report filled / skipped (skipped = future Tier 1)
```

`taxonomy.yaml` = *what* to fill (semantic, ATS-agnostic). `adapters/*.js` = *how /
where* on this specific ATS (DOM, control types, event sequences).

## VERIFY checklist — live-checked 2026-07-09 (Vercel + Chime React forms)

- [x] Core field ids resolve on React (`job-boards`): `#first_name`, `#last_name`,
      `#email`, `#phone`, `input#resume`, `input#cover_letter`. React drops `name`, so
      `input[name=...]` fallbacks are legacy-only. **`boards.greenhouse.io` now 301s to
      `job-boards` (React) — legacy hosted board is being retired; build React-first.**
- [x] react-select option node = **`[role="option"]`** (not `.select__option`); control
      `.select__control`, menu `.select__menu`. No native `<select>` on React.
- [x] Resume input id confirmed `input#resume` (behind an "Attach" dropzone).
- [x] Custom questions keep `question_<id>` ids on React, render as text-input-backed
      react-selects (route via `reactSelect`, not `select`); labels via `label[for=id]`.
      Submit button reads "Apply".
- [~] Location: React has **no places autocomplete** — it's a `#country` react-select
      (fill "United States") + phone-country intl-tel-input. Legacy places box unverified.
- [ ] **EEO/demographic wrapper on React — STILL UNVERIFIED** (test form had no EEO
      block). Need a live React form with demographic questions.
- [ ] DataTransfer resume inject actually registering on the React uploader (needs the
      extension loaded to test — DOM inspection can't confirm the upload fires).
- [ ] True legacy `/embed/job_app` DOM (native selects, places box) if any tenant still
      serves it via a company-site iframe.

## Lever — live findings (DOM dry-run 2026-07-10, jobs.lever.co/coupa)

- Classic server-rendered form — **synthesized events honored, no CDP needed.**
- Core fields by `name`: `name` (one full-name field), `email`, `phone`,
  `location` (free-text `.location-input`, places dropdown optional), `org`,
  `urls[LinkedIn]`/`[GitHub]`/`[Portfolio]`. Resume = `input[name="resume"]`
  (`#resume-upload-input`). All filled correctly in the dry-run.
- Custom questions = `cards[<UUID>][field<N>]`: native `<select>` (all committed via
  setNativeSelect), text, or radio/checkbox groups. Prompt = `.application-question
  .application-label` ("✱" = required).
- Radio/checkbox options: each `<input>` wrapped in a `<label>`; `value` attr IS the
  human text. Group handler extracts the QUESTION label ("Pronouns", not an option like
  "He/Him" or "She/Her") and clicks the matching option — verified toggling.
- Submit reads "Submit application".

## Ashby — live findings (DOM dry-run 2026-07-10, jobs.ashbyhq.com/{notion,plaid})

- React app but **honors synthesized events end-to-end — no CDP** (unlike new Greenhouse).
- Core: `#_systemfield_name` (one full-name field), `#_systemfield_email`,
  `#_systemfield_resume`, phone/customs use per-job UUID ids. Clean `label[for]`
  associations. All filled in the dry-run.
- **Location = combobox typeahead** (`input[role=combobox]`): opens on a synthesized
  `input` event; type the city prefix, match the full display string against
  `[role="option"]` (state name expands, e.g. "Springfield, Illinois, United States"),
  synth-click commits. Verified: value committed correctly.
- Dropdowns also appear as native `<select>` (setNativeSelect handles them).
- Short single-selects render as **labeled radios**; multi-select as labeled checkboxes.
  The Notion form carried the full **EEO block as radio groups** (Gender / Race / Veteran
  Status) plus a work-auth sponsorship question — the group handler read every QUESTION
  label correctly and clicking committed. **This closes the "EEO on React unverified" gap
  for Ashby** (it's labeled-radios, not a native-select wrapper).
- Submit reads "Submit Application".

> Both dry-runs injected the adapter's selectors + setters into the live page (extension
> not yet loaded — the RC session can't load unpacked extensions). Still pending an actual
> loaded-extension run: DataTransfer resume upload firing on Lever/Ashby uploaders, and the
> full popup → fill → report round-trip.

## References

- Greenhouse Job Board API (canonical field names): https://developers.greenhouse.io/job-board.html
- Field/knockout guide: https://notchresume.com/resources/greenhouse-job-application.html
