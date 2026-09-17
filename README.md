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
- The extension is the product. Everything else — the console, the Telegram control plane,
  the mail credential that reads an emailed verification code — is optional scaffolding
  around it, and the fill engine works with none of them configured.

If you want a hosted, hands-off job-applier, this is the wrong project.

## First-time setup

```bash
python bootstrap.py
```

That installs the Python dependencies, creates your config files from the shipped
templates, and opens the console on a setup checklist that walks you through the rest in
the browser. Requires Python 3.10+, and Node 18+ for the console UI.

**Four steps get you to a filled application**, and only the first is required. The
extension is the part that does the work; the rest of the list is about feeding it:

| Step | What it means |
|---|---|
| **Your details** | Name, contact, work authorization, EEO answers. Edited as a form; `checkfill` counts what's left |
| **Extension** | The fill engine itself. Rebuild from the Profile page, then load it once at `chrome://extensions` |
| **Companies** | Ten real API-verified examples ship, so a scrape works immediately. Edit any tier to make the list yours |
| **Resumes** | Only needed for the apply half. Point `_local_resume_source.base_dir` at your PDFs, then `python -m core.resumes sync-local` |

Then two **optional extras**, listed separately in the console and left out of its progress
bar. The extension fills and submits without either one, and did for months before they
existed:

| Extra | What it adds | If you skip it |
|---|---|---|
| **Telegram** | Answer a form's open questions from your phone | Answer them in the console instead |
| **Verification codes** | Fills the emailed code some Greenhouse boards ask for at submit | The application is parked and you paste the code yourself |

Two of the four, plus both extras, the setup flow **cannot** do for you, and it says so
rather than pretending: Chrome deliberately requires a human to load an unpacked extension,
creating a Telegram bot is a conversation with BotFather, and so is asking Google for a mail
credential. Each has an "I did this, re-check" button.

The extras read `bridge/.env` as well as your environment, since that file is where the
setup instructions put those values — a checklist that called the documented setup "not
configured" would be worse than no checklist.

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

### Keeping your data out of the checkout

Set `JOBCOPILOT_HOME` and everything personal moves to one directory — config, history,
answers, drafts, resumes, scrape output:

```bash
JOBCOPILOT_HOME=~/private/jobcopilot python start.py
```

```
~/private/jobcopilot/
  profile.json  companies.yaml  answer_bank.yaml   your config
  applied.json  prefs.json                         history and settings
  resumes/  pending/  logs/  out/                  PDFs, in-flight answers, reports
```

One variable, so that directory is a single portable thing: copy it and you have moved the
whole job search, with nothing personal left behind in the checkout. `filters.yaml` and
`taxonomy.yaml` are tuning rather than identity, so the ones shipped here are used by
default — drop your own copies in `JOBCOPILOT_HOME` to override them.

(`JOBCOPILOT_DATA` is the older, narrower variable: config directory only, state stays in
the checkout. It still works, and it still wins over `JOBCOPILOT_HOME` for config, which is
how a test or a throwaway console points itself somewhere safe.)
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
python start.py --stop       # stop the running services
python start.py --status     # show what is running
```

Every start stops the copy that is already running first, so running `start.py` again is
a restart. On Windows the services run hidden and the launcher exits. Their output goes
to `logs/bridge.log` and `logs/console.log`. The bridge is skipped with a note unless
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

- `queue.json` — pipeline roles: live, fresh, US, not-yet-applied, freshest first, **and
  on a board the extension has an adapter for**. This is what the apply engine feeds off.
- `dream.json` — dream-tier roles. Apply-by-hand (referral first); the extension skips them.
- `manual.json` — roles on a board with no adapter (Apple, Amazon, SmartRecruiters, iCIMS).
  Also apply-by-hand, for a different reason. Kept out of `queue.json` deliberately: a
  collection run that opened one would get a tab where no content script mounts.
- `digest.md` / `digest.html` — human-readable, grouped by field; dream and by-hand flagged.
- `raw_YYYYMMDD.json` — everything fetched that run (audit).

Sources are two kinds. Every board in `companies.yaml` is fetched directly, and on top of
that `filters.yaml` lists **feeds** — aggregated postings that surface employers who aren't
on your company list at all. One ships enabled: the public
[SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions) JSON,
one GET per run. Feed entries are judged by the same filters as everything else, and a
posting reached by both routes is deduped on its ATS identity (board + job id), so the
company's own board record — which has the better location and a true posted date — wins.

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

## Emailed verification codes (optional extra)

**Nothing needs this.** Some Greenhouse boards mail you an 8-character code on submit and
hold the application behind it; with no mailbox credential configured the extension parks
that application and tells you why, which is what it has always done and costs you one
paste. Configure one and it reads the code out of your own inbox and fills it instead.

**A fetch only ever follows something you did** — your Submit click, the panel's 🔐 button,
or your approval in Telegram. Nothing polls your mail, and the unattended queue never
triggers a fetch on its own. The matcher only considers mail that arrived *after* that
click, and only messages that read as verification, so an unrelated code sitting in your
inbox cannot be lifted into a job form.

Pick one credential shape — see [`bridge/README.md`](./bridge/README.md#4-verification-codes-optional):

- `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` — one value to paste, and the mailbox is opened
  read-only in code (IMAP `EXAMINE`), though the credential itself cannot be scoped.
- `python bridge/gmail_auth.py` — a `gmail.readonly` OAuth token, read-only by the
  credential and revocable on its own. More clicks once, stronger afterwards.

Use the mailbox your applications actually go to: the address in `profile.json`
`identity.email`, which is not necessarily the account you sign in with elsewhere.

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

With the optional AI fill + verify pass on (`/ai`, or the console), free-text answers you
approve are banked automatically as `status: draft` rows tagged `ai-drafted` — review them
with `python -m core.bank list --status draft` and promote the ones worth keeping. They
never serve until you do. See `bridge/README.md` for what that pass may and may not write.

## Resumes

`resumes/` is a local, gitignored byte-cache of the real PDFs that the extension attaches
via DataTransfer. Source of truth is currently the local Finals folder:

```bash
python -m core.resumes sync-local        # refresh the cache by hand
python -m core.resumes status            # show the configured source + what's cached
```

**You do not have to run either.** The bridge syncs the cache itself: at startup, before a
queue run, and before every attach. Syncing compares content hashes, so it copies only
what changed and re-running it writes nothing. Edit a PDF in the source folder and the
next application sends the new one.

The extension goes further and asks the bridge for the bytes at attach time, so the copy
staged inside `extension/` by `build.py` is only a fallback for when the bridge is down.
The fill report records which of the two an application used.

Whatever a PDF is called on disk, it is attached as `First_Last_<letter>.pdf` — built from
`identity` in your profile, with the track tag's initial as the suffix, and no suffix at
all when the profile defines one track. `core/profile.py resume_filename` and its port in
`extension/attach.js` are the one source of that name; `test_stream_parity.py` checks the
two agree.

Send yourself the current PDF from the phone with `/myresume` (a list of tracks with their
cache dates) or `/myresume ai` (that track straight away).

## Daily operation

The GitHub Actions workflow runs `python -m core.scraper` daily at 03:00 UTC (08:30 IST):
it scrapes, uploads the artifact, emails the HTML digest, and commits the queue back.
Because it force-commits `out/`, origin diverges from a local clone each run — **rebase,
don't pull**: `git rebase origin/main` before pushing.

For the unattended local collection run (Task Scheduler → bridge → extension), see
[`docs/autonomous_run.md`](./docs/autonomous_run.md) and `scripts/`.

## Tests

Every suite is a plain script with its own `main()` — there is no pytest, no unittest
discovery, and no conftest. Run each file directly; each prints an `N/N checks passed`
line and exits non-zero on failure.

One-time setup. The venv is optional but keeps the test-only dep out of the interpreter
the bridge runs on:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r bridge/requirements.txt \
    -r web/requirements.txt -r requirements-dev.txt
```

```bash
for t in bridge/test_*.py; do .venv/Scripts/python "$t"; done
.venv/Scripts/python web/test_api.py
.venv/Scripts/python -m core.profile --selftest
.venv/Scripts/python scripts/test_pii_guard.py
.venv/Scripts/python test_home_layout.py && .venv/Scripts/python test_write_target.py
node extension/test_fill_units.mjs
node extension/test_aipass_units.mjs
```

Run the whole thing **twice back to back, then `git status data/`**. Every suite is
supposed to work on an isolated temp copy of the data layer; one that isn't passes alone
and fails on the rerun, because the first run clobbered the live state it then reads.
That is how `core/bank.py`'s save-through-a-READ-path bug was caught.

## Constraints

- Job-board APIs need network; some sandboxes can't reach them — run locally or in Actions.
- Respect ATS bot-detection: cap ~10–20/day per platform, vary timing.
- Never commit secrets. The console binds 127.0.0.1 only and has **no auth** — the state
  files hold PII, so front it with an authed tunnel before exposing it.
