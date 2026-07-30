# job copilot

Personal tool to semi-automate job applications: scrape currently-open roles from
Greenhouse / Lever / Ashby, filter to well-matched early-career quant/DS/tech/AI
openings, auto-fill most of each application form, and review + submit with a human in
the loop.

> **Architecture in one paragraph:** two runtimes over one shared data layer. *Discovery*
> is a Python scraper hitting the Greenhouse / Lever / Ashby public APIs — no logged-in
> browsing, no LinkedIn scraping. *Apply* is a Chrome extension that fills forms locally
> using your own session, escalating anything it cannot answer deterministically to you
> rather than guessing. Submission is always a human tap. The question taxonomy is config,
> not code, so which fields are auto-filled, drafted, or always asked is yours to change.

## What this actually is, before you invest an evening

Be clear-eyed about the shape of this tool. **Discovery** (scraping and filtering openings)
is ordinary automation and runs anywhere, including in GitHub Actions on a schedule. **Apply**
is a browser extension that runs on *your* machine in *your* logged-in Chrome profile, because
filling a real application form needs your real session cookies and, on Greenhouse, genuinely
trusted clicks. That is not a packaging gap that a Docker image would fix; it is inherent.

Practical consequences:

- Setup is not `pip install`. You load an unpacked extension in Chrome and keep it loaded.
- It never submits by itself. Submission is always an explicit tap by you — these companies
  track duplicate applications and there is no undo.
- It is not headless and not a bulk-apply tool. It is built for 5-8 considered applications a
  day, not hundreds.
- The Telegram control plane is optional. Skip it and use the local web console instead.

If you want a hosted, hands-off job-applier, this is the wrong project.

## First-time setup

```bash
python bootstrap.py
```

That installs the Python dependencies, creates your config files from the shipped
templates, and opens the console on a setup checklist that walks you through the rest in
the browser. Requires Python 3.10+, and Node 18+ for the console UI.

The checklist tracks five things, and only the first is required:

| Step | What it means |
|---|---|
| **Your details** | Name, contact, work authorization, EEO answers. Edited as a form; `checkfill` counts what's left |
| **Companies** | Ten real API-verified examples ship, so a scrape works immediately. Edit any tier to make the list yours |
| **Resumes** | Only needed for the apply half. Point `_local_resume_source.base_dir` at your PDFs, then `python -m core.resumes sync-local` |
| **Extension** | Rebuild from the Profile page, then load it once at `chrome://extensions` |
| **Telegram** | Optional. Skip it and use the console instead |

Two of those the setup flow **cannot** do for you, and it says so rather than pretending:
Chrome deliberately requires a human to load an unpacked extension, and creating a Telegram
bot is a conversation with BotFather. Both have an "I did this, re-check" button.

Your three config files (`data/profile.json`, `data/companies.yaml`,
`data/answer_bank.yaml`) are **gitignored**, so filling them in can never commit your
personal details back to a public fork. The `.example` originals stay tracked as reference,
and nothing ever writes back to them.

Anything left as `FILL_ME` is treated as missing: the extension surfaces that question for
you to answer by hand rather than guessing, which is intended behaviour, not a bug. Review
the work-authorization and EEO blocks especially — those answers go to real employers, and
a plausible default is still someone else's answer.

<details>
<summary>Prefer the manual route, or want your data outside the checkout?</summary>

```bash
pip install -r bridge/requirements.txt -r web/requirements.txt
cp data/profile.example.json     data/profile.json
cp data/companies.example.yaml   data/companies.yaml
cp data/answer_bank.example.yaml data/answer_bank.yaml
python -m core.profile checkfill      # lists everything still unset
python -m core.profile --selftest     # resolver checks against your profile
```

Point `JOBCOPILOT_DATA` at any directory holding those config files and the whole pipeline
reads from there instead, keeping personal data out of the repo entirely:

```bash
JOBCOPILOT_DATA=~/private/jobcopilot-data python start.py
```
</details>

## Run it

```bash
python start.py
```

That's the whole thing: the apply bridge on `ws://127.0.0.1:8765` and the web console at
**http://127.0.0.1:8000**. The console UI is rebuilt automatically if its sources changed.

```bash
python start.py --dev        # frontend hot-reload via Vite on :5173 (open that instead)
python start.py --no-bridge  # console only, no Telegram / extension control
python start.py --inline     # stream all output into this terminal (default off Windows)
python start.py --build      # force a UI rebuild even if it looks fresh
```

On Windows each service gets its own titled console window; keep the launcher window and
Ctrl+C there to stop everything. The bridge is skipped with a note unless
`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` are set (environment or `bridge/.env`).

First run needs deps:

```bash
pip install -r bridge/requirements.txt -r web/requirements.txt
```

Requires Python 3.10+ and Node (for the console UI).

## Layout

```
core/       shared pipeline modules — scraper, profile/taxonomy resolver, answer bank,
            resume cache, applied-log helper. paths.py defines where everything lives.
data/       hand-edited config: companies, filters, taxonomy, profile, answer bank.
bridge/     local Telegram + extension bridge (WebSocket :8765) and the pending batch.
extension/  MV3 browser extension: the adapters and the deterministic fill engine.
web/        the console — FastAPI backend (app/) + React frontend (frontend/).
scripts/    Windows Task Scheduler install + the autonomous daily run.
docs/       audits, specs, and the autonomous-run runbook.
out/        scraper output (queue.json, digests) — written by the daily Actions run.
```

`applied.json`, `resumes/` and `out/` stay at the repo root: they're local/generated
state rather than hand-edited config, and the daily GitHub Action force-commits `out/`.

## Discovery (the scraper)

```bash
python -m core.scraper                              # full run (needs network)
python -m core.scraper --verify-liveness --limit 40 # + per-job liveness re-check
python -m core.scraper --selftest                   # offline sanity check
```

Run these from the repo root — `-m` resolves `core.*` off the working directory.

Outputs (written to `out/`):

- `queue.json` — pipeline roles: live, fresh, US, not-yet-applied, freshest first. This
  is what the apply engine feeds off.
- `digest.md` / `digest.html` — human-readable, grouped by field; dream roles flagged.
- `raw_YYYYMMDD.json` — everything fetched that run (audit).

Dedup is against `applied.json`. The bridge writes it on every confirmed submit; to add a
role you applied to by hand:

```bash
python -m core.apply_log add --from-queue 3    # copy company/title/location from queue #3
python -m core.apply_log add --company Ramp --title "Software Engineer" --location "New York, NY"
python -m core.apply_log list
```

It computes the same dedup key the scraper uses, so entries match exactly.

### Config you edit by hand (all in `data/`)

- **`companies.yaml`** — the companies to scrape. `ats` is `greenhouse|lever|ashby`;
  `slug` must match the board URL or the fetch 404s. `tier` is `pipeline` (auto-apply) or
  `dream` (surface only, apply manually + referral). Editable in the console too.
- **`filters.yaml`** — field keyword lists (drive both classification and relevance),
  seniority exclusions, US-location rules, and `freshness_days`.
- **`taxonomy.yaml`** — keyword/regex rules bucketing form fields into deterministic /
  draftable / always_ask. **Insertion order is load-bearing:** specific rules above
  generic ones.
- **`profile.json`** — the deterministic autofill source (identity, work auth, EEO
  defaults, resume tracks). Editable in the console.
- **`answer_bank.yaml`** — reusable answers, served at apply-time.

After editing `taxonomy.yaml` or `profile.json`, restage the extension's copies:

```bash
python extension/build.py     # then reload the unpacked extension in chrome://extensions
```

## Answer bank

```bash
python -m core.bank list
python -m core.bank get  --key why_company --company Ramp   # what apply-time would serve
python -m core.bank add  --key why_company --scope company --company Ramp --answer "..."
python -m core.bank update <id> --answer "..." --status active
python -m core.bank prune --days 90                         # mark unused active answers stale
```

Resolution order at apply-time: company-scoped (active) → common (active) → Claude draft
→ ask user. Only `status: active` answers are served. The console has a UI for this.

## Resumes

`resumes/` is a local, gitignored byte-cache of the real PDFs that the extension attaches
via DataTransfer. Source of truth is currently the local Finals folder:

```bash
python -m core.resumes sync-local        # refresh the cache
python -m core.resumes status            # show the configured source + what's cached
```

## Daily operation

The GitHub Actions workflow runs `python -m core.scraper` daily at 03:00 UTC (08:30 IST):
it scrapes, uploads the artifact, emails the HTML digest, and commits the queue back.
Because it force-commits `out/`, origin diverges from a local clone each run — **rebase,
don't pull**: `git rebase origin/main` before pushing.

For the unattended local collection run (Task Scheduler → bridge → extension), see
[`docs/autonomous_run.md`](./docs/autonomous_run.md) and `scripts/`.

## Tests

```bash
for t in bridge/test_*.py; do python "$t"; done
python web/test_api.py
python -m core.profile --selftest
node extension/test_fill_units.mjs
```

## Constraints

- Job-board APIs need network; some sandboxes can't reach them — run locally or in Actions.
- Respect ATS bot-detection: cap ~10–20/day per platform, vary timing.
- Never commit secrets. The console binds 127.0.0.1 only and has **no auth** — the state
  files hold PII, so front it with an authed tunnel before exposing it.
