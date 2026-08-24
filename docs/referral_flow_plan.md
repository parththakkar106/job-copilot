# Referral flow — paste a person, get reqs + the drafts (scope)

Status: **SCOPE ONLY, nothing built.** Decided with Parth 2026-08-18. This supersedes the
*direction* of `outreach_plan.md` phases 2-4; that document's phase 1 (`core/contacts.py`)
stays valid and stays in the repo, but is **not on this path**. Read the constraints,
provider table, title filter, and PII sections of `outreach_plan.md` first — they still hold
and are not repeated here.

## The inversion

`outreach_plan.md` was written job -> people: take a queue row, search for contacts, draft a
note. Parth's call 2026-08-18: **people -> jobs.** He finds the person himself (alumni
network, mutuals, whatever), pastes the profile in, and the pipeline answers "here are your
open reqs at that company and here is what to send".

Consequence: **`core/contacts.py` discovery is not invoked by this flow.** No search
provider, no Tavily credits, no query ladder, no title filter at request time. That module
is 1151 lines of measured, working code that this path does not call. It is not deleted —
if the paste flow proves the drafting half, a "find me people at this company" button can
turn it back on later. But nothing here depends on it.

What IS reused from phase 1: `outreach.json` (`paths.OUTREACH`), the `Contact` shape, the
state machine, dedupe by normalised profile URL, `employer_is()`, `parse_profile_card()`,
and the PII rules in `scripts/pii_guard.py`.

## Decisions taken 2026-08-18

| question | answer |
|---|---|
| direction | contact first only |
| job source | **local files only.** No live re-scrape of all companies. |
| surface | **both, Telegram first**, console page after |
| profile input | pasted profile text **or a screenshot**, plus free-text comments from Parth |
| draft output | every variant in one run |
| trigger / timing | see `apply_after` below — settled by the ATS rule, no longer an open question |

## The message sequence (settled 2026-08-18)

Supersedes `outreach_plan.md`'s two variants (`invite_note` + `dm`). The reasoning behind
the ordering: an unopened attachment from a stranger is friction and reads as a mass send,
while naming one specific req turns a vague favour into a form fill.

1. **`invite_note`** — <=300 chars, sent with the connection request. No ask, no link, no
   attachment. Just enough context that the person knows why he exists.
2. **`ask_dm`** — sent within a day of acceptance. ONE named req, its link and req id, one
   concrete hook from his own work, and a yes/no question. Explicitly offers to send a
   resume plus pasteable lines so the favour is two minutes of work. **No resume attached.**
3. **`yes_pack`** — sent only after they agree. Resume for the req's `field`, the link and
   req id again, three pasteable bullets, and one line stating US green card / no
   sponsorship needed, because referral forms ask.
4. **`bump`** — one line, once, at 5-7 days of silence. Then the contact is dropped forever.
5. **`already_applied`** — replaces (2) when the company is an `applied.json` hit. Different
   ask entirely: "I applied on <date>, req <id>, would you be able to add a referral to it."
   Most companies support referring an existing candidate; this is the common case, since 46
   of the ~50 reachable companies are ones he already applied to.

**No deliberate typo in outreach.** `feedback-answer-writing-style` mandates one subtle typo
in application answers to dodge AI-writing detectors. That rule does NOT extend here: the
reader is a human being asked for a favour, not a detector, and a typo in a referral ask
reads as careless. Everything else from that rule carries: human and natural, short, and
**no dashes** (no em dash, no hyphen as punctuation).

`sanitize()` already strips model preamble; it needs a dash pass and a 300-char validator
for `invite_note` (regenerate once if over, then hard-truncate at a word boundary).

## Company resolution — four tiers

The single most important piece of behaviour, because it decides whether a paste is useful.
Match the pasted employer against local files only, in this order. `employer_is()` in
`core/contacts.py` already solves exact-match against single-word company names ("Writer",
"Sierra", "Elastic") and MUST be reused rather than rewritten as a substring test.

| tier | source | result |
|---|---|---|
| 1 | `out/queue.json`, `out/dream.json` | Open reqs with `ats_job_id`, `url`, `posted`, `field`. Rank, take 2-4, draft `invite_note` + `ask_dm`. The good case. |
| 2 | `applied.json` | He already applied. Draft `invite_note` + `already_applied`, carrying the `applied` date and the req id parsed from the row's `url`. |
| 3 | `companies.yaml` (tracked, no current row) | Tracked but nothing open matched last night. **Offer a one-company live pull** — a single ATS call for that slug, not a re-scrape. Opt-in per paste, never automatic. This respects "don't re-fetch all companies" while not dead-ending. |
| 4 | unknown | Say so plainly, offer to append the company to `companies.yaml` so tonight's run picks it up. Draft nothing — a referral ask with no req in it is the weak version of this whole idea. |

Tier 3 needs `core/scraper.py --only <company>`; today it takes `--companies <file>` and
nothing narrower, so the alternative is writing a temp yaml. Ten-line add.

A company can hit tiers 1 and 2 at once (open req AND a past application). Tier 1 wins for
the ask, but the draft should mention the earlier application rather than pretend it did not
happen.

### Coverage is the risk to watch

Measured 2026-08-18 against the live files. The reachable set is the union of the companies
appearing in `queue.json`, `dream.json` and `applied.json` — and it came to roughly **two
fifths** of the companies tracked in `companies.yaml`. (Re-measure rather than trusting that
fraction: it moves with every scrape. The counts themselves are personal, so they are not
recorded here.) So a pasted alum at a company outside that set lands in tier 3 or 4 and
returns no req. If tier 3/4 turns out to be the majority of pastes
in real use, the local-only constraint is the thing to revisit — not the drafting.
**Log the tier on every paste from day one**, or there will be no way to tell.

## Req ranking within a company

Cheap and local, no model call. `profile.json` supplies the field preference; queue rows
already carry `field`, `title`, `location`, `location_hint`, `posted`.

- drop anything the existing early-career / US / no-sponsorship filters already reject
- prefer `field` matching his strongest field, then title-token overlap with the contact's
  own headline (an alum in payments should be pointed at the payments req)
- freshness from `posted` as the tiebreak
- **cap at 4, show 2 by default.** A referral ask naming five reqs is a mass send.

## `apply_after` — when to stop waiting

Settled 2026-08-18. A referral arriving after the application keeps nearly full value at
almost every company; being applicant #400 instead of #40 does not. So the two tracks run in
parallel and the only thing ever waited on is a Workday link from a contact who already
replied. Computed per req, stored on the record, surfaced in the card:

- `greenhouse` / `lever` / `ashby` -> `now` (a referral attaches after the fact cleanly)
- `workday` -> `min(sent + 4 days, now)` — the referral link itself creates the
  application, so a self-apply first can downgrade or block it
- posting older than ~10 days -> `now` regardless of ATS
- contact has not replied at all -> `now`. An accepted invite is not a pending referral.

## Data model

Extends the `contacts[]` block in `outreach_plan.md`. Lives in `outreach.json` in
`JOBCOPILOT_HOME` (private repo), keyed by normalised profile URL — **never** in
`job-copilot`.

```jsonc
{
  "id": "fnv1a(profile_url)",
  "name": "...", "headline": "...", "profile_url": "...", "company": "...",
  "source": "paste|screenshot",
  "notes": "Parth's free-text comments, verbatim, fed to the drafter",
  "tie": "alum|ex-colleague|mutual|cold",
  "tier": 1,
  "reqs": [{"dedup_key": "...", "ats_job_id": "...", "url": "...", "title": "...",
            "ats": "greenhouse", "apply_after": "2026-08-22"}],
  "drafts": {"invite_note": "...", "ask_dm": "...", "yes_pack": "...",
             "bump": "...", "already_applied": "..."},
  "state": "found|invited|accepted|messaged|replied|no_reply|skipped",
  "history": [{"state": "invited", "ts": "..."}]
}
```

`accepted` is the only transition Parth reports by hand — the pipeline cannot see his
LinkedIn. A Telegram nudge sweeps invites older than N days and asks, rather than making him
remember. Dedupe on `id` at paste time: **the same recruiter covers three roles, and
inviting one person twice is the failure that gets an account restricted.**

## Phasing

**P1 — `core/referral.py`, library + CLI, no bridge.** Profile-text parser (name, headline,
employer, school, location; reuse `parse_profile_card()` where the shape matches), the
four-tier resolver, req ranking, `apply_after`, `outreach.json` read/write with dedupe.
Pure functions with `--selftest`, invented people in fixtures. **`scripts/pii_guard.py`
learns the pasted-profile shape BEFORE the first fixture is written** — it has caught its own
author three times and this feature is entirely other people's personal data.

**P2 — `drafter.draft_outreach()`.** One batched Claude call per contact producing every
variant. Reuses `build_prompt`'s voice rules, `_run_claude`, the `DRAFTS_ENABLED` kill
switch, and persistence. New: the 300-char validator, the no-dash pass, the tie and Parth's
`notes` in the prompt, and a hard rule that a note must never assert the person is hiring
for the req.

**P3 — Telegram.** Paste text to the bot -> card with reqs and drafts, buttons per the
existing `CB` batch namespace (`copy` / `edit` / `next req` / `skip`). State transitions
write `outreach.json`. The bump sweep rides the existing idle-flush loop.

**P4 — screenshot input (spike first, may not land).** Two unknowns, both new surface:
`getFile` + download in the bridge (no photo handling exists there today), and getting an
image into `_run_claude`, which runs `claude -p` in a throwaway temp cwd — the file must be
copied in and read by path. **Timebox it. Text paste is the fallback and must work
standalone.**

**P5 — `Outreach.jsx`.** State table, follow-up queue, `apply_after` countdown. Reuses
`MoveMenu.jsx`.

## Open

- **Resume delivery on Telegram.** `yes_pack` needs the right PDF from `resumes/`
  (`quant|ds|tech|ai` by req `field`). `sendDocument` does not exist in the bridge yet.
  Cheapest v1: name the file and let him grab it off disk. Decide at P3.
- **Volume ceiling.** Still open from `outreach_plan.md`. Premium removes the note cap, but
  a sub-30% acceptance rate is what triggers restrictions, so this stays a quality problem.
- Nothing is ever sent automatically. Same rule as submit.
