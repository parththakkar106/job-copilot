# Server migration plan — one bridge, two ways to run it

**Status:** plan, not yet started. **Branch:** `claude/job-copilot-server-migration-v38fyd`.

## The idea in one paragraph

Today the bridge is a Python process on Parth's laptop. It talks to Telegram, remembers
everything in JSON files, and holds a WebSocket the extension connects to on
`127.0.0.1:8765`. Laptop asleep means the tool is off. After this migration the *same*
bridge code runs in two modes: **local**, exactly as it does now, and **hosted**, on a
Fly.io machine with a Neon Postgres database behind it. Hosted mode means the phone works
with the laptop closed. The extension always stays on the laptop, because it holds the
logged-in Chrome session and the submit is always a human tap.

## Decisions already made

| Question | Decision |
|---|---|
| What moves | Everything except the browser. Laptop keeps the extension + a thin runner. |
| Personal data | Lives in the database. No app-level encryption — rely on Neon's disk encryption. |
| Multi-user | Design for it, don't build it. `user_id` on every table from day one, no login flow for other people yet. |
| Web console | Moves to the server, behind email + password. |
| Hosting | Fly.io (keeps the Python), Neon Postgres. |
| Scraper | Stays in GitHub Actions; posts results to the server instead of committing them. |
| Local storage | SQLite file. Same schema as hosted, swapped by a setting. |
| Telegram (later, multi-user) | One shared bot, users linked by chat id. |
| Deployment shape | **Two modes, one codebase.** Local mode never stops working. |
| Repo | `job-copilot/server/`, same repo. |

## What runs where, after

```
  LAPTOP                                  FLY.IO                          NEON
  ┌──────────────────────┐                ┌──────────────────────┐        ┌──────────┐
  │ Chrome + extension   │───wss──────────│ bridge (hosted mode) │────────│ Postgres │
  │   fill.js            │  device token  │   ws hub             │        └──────────┘
  │   background.js      │                │   telegram webhook   │
  │   adapters/          │                │   drafter (API)      │        TELEGRAM
  │                      │                │   web console + API  │────────  webhook
  │ resumes/ (cache)     │                └──────────────────────┘
  └──────────────────────┘                          ▲
                                                    │ POST /api/ingest/queue
                                            GITHUB ACTIONS (scraper, unchanged)
```

Local mode is the same picture with the middle box on the laptop, SQLite instead of
Postgres, `ws://127.0.0.1:8765` instead of `wss://`, and Telegram long-polling instead of
a webhook.

## The two seams that make this work

Everything below is in service of exactly two abstractions. Get these right and the rest
is mechanical.

**Seam 1 — the store.** No module reads or writes a file directly any more. They call a
store object. `SQLiteStore` and `PostgresStore` are the same SQLAlchemy code pointed at a
different URL, so this is one implementation, not two.

**Seam 2 — the context.** Every handler currently reaches for module-level globals
(`INBOX`, `LOCK`, `ACTIVE_HANDLER`, the Telegram token). Those become fields on a `Ctx`
object carried through every call. In local mode there is exactly one `Ctx`. In hosted
mode there is one per connected user. This is the change that makes multi-user possible
later without a rewrite.

---

# Phases

Ordered so that the tool keeps working at the end of every phase. Nothing is deleted until
Phase 9.

## Phase 1 — The store (the big one)

**Goal:** every read and write goes through one interface, backed by SQLAlchemy.

New package `server/store/`:

- `models.py` — SQLAlchemy tables. Every single one carries `user_id`.
- `store.py` — the interface the rest of the code calls.
- `engine.py` — builds SQLite or Postgres from `DATABASE_URL`.
- `migrate.py` — Alembic. Set up now, not later.
- `importer.py` / `exporter.py` — read today's files into the DB, and dump back to YAML/JSON.

Schema, first cut:

| Table | Replaces | Notes |
|---|---|---|
| `users` | (new) | id, email, password_hash, created_at. One row for now. |
| `profiles` | `profile.json` | JSON column. Resume tracks in `resume_tracks`. |
| `resume_tracks` | profile's tracks + `resumes/` | tag, label, keywords, **pdf bytes**, sha256. |
| `companies` | `companies.yaml` | slug, ats, tier (dream/pipeline). |
| `answer_bank` | `answer_bank.yaml` | key, company scope, text, status, used_count. |
| `pending` | `bridge/pending/*.json` | dedup_key, job JSON, questions JSON, status. |
| `applied` | `applied.json` | dedup_key, company, title, url, date, status. |
| `queue_runs` | `out/queue.json` | one row per scrape, jobs as JSON. |
| `fill_reports` | `logs/fill_reports.jsonl` | append-only. |
| `audit_dumps` | `logs/audit/` | screenshot bytes + summary. |
| `prefs` | `prefs.json` | key/value per user. |
| `contacts` / `referrals` | `core/contacts.py`, `core/referral.py` state | port as-is. |
| `devices` | (new) | paired extensions: token hash, label, last seen. |

Two things to settle while writing this:

1. **Config becomes rows.** `companies.yaml` and `answer_bank.yaml` stop being the source
   of truth. `python -m server.store export --companies > companies.yaml` and
   `... import companies.yaml` keep the text-editor workflow alive as an explicit round
   trip. `bootstrap.py` seeds a new install from the shipped `.example` files.
2. **`core/paths.py` survives, narrowed.** `JOBCOPILOT_HOME` still names where the SQLite
   file and the resume source folder live. It stops being where config lives.

**Done when:** `python -m server.store import` ingests the current `job-pipeline/` home
into a SQLite file, `export` round-trips it byte-identically, and the existing bridge
tests pass against the store instead of tmpdirs.

## Phase 2 — Ctx, and killing the globals

**Goal:** nothing in `bridge/` reads a module global or an environment variable at call
time.

- Define `Ctx`: `user_id`, `store`, `telegram` (a sender object), `drafter`, `inbox`,
  `lock`, `active_handler`.
- Thread it through every `handle_*` in `bridge.py`, plus `pending.py`, `drafter.py`,
  `triage.py`, `prefs.py`.
- `bridge.py` is 2,865 lines and does too much. Split while you're in there:
  `bridge/handlers/` (one file per message type), `bridge/telegram.py` (send, callbacks,
  keyboards), `bridge/session.py` (the WS hub).
- **Hard test:** two `Ctx` objects in one process, two fake extensions, two fake Telegram
  chats. Every answer must land on the right user. This test is the security boundary for
  the shared-bot decision — it stays green forever.

**Done when:** the local bridge runs on a single `Ctx` and behaves identically, and the
two-user isolation test passes.

## Phase 3 — Transport split

**Goal:** the same handlers behind two different front doors.

- `MODE=local` → Telegram long-poll (today's `telegram_poller`), WS on `127.0.0.1:8765`,
  no auth, `Ctx` for user 1.
- `MODE=hosted` → FastAPI. `POST /telegram/webhook/{secret}` receives updates. `GET /ws`
  upgrades to a WebSocket, authenticated by a device token, and resolves the `Ctx`.
- Device pairing: console shows a one-time code, the extension popup takes it, server
  returns a long-lived token, extension stores it in `chrome.storage.local`. Token hash in
  `devices`.
- Telegram linking: `/start <code>` in the bot maps a chat id to a user.

Keep `handle_ask` and friends untouched here. If a handler needs to know the mode, the
seam is wrong.

**Done when:** the hosted app runs locally under uvicorn, the extension connects to it
with a token, and one real application gets filled end to end through it.

## Phase 4 — Drafting

**Today:** `drafter.py` shells out to the `claude` CLI, billed to the Claude Code
subscription, no API key, no per-token cost.

**That does not transfer.** A server can't ride a personal subscription, and it certainly
can't for several users.

- `drafter/backends/cli.py` — today's subprocess, unchanged. Default in local mode.
- `drafter/backends/api.py` — the `anthropic` Python SDK. Default in hosted mode.
- Same `draft_answer()` / `draft_answers()` / `draft_outreach()` signatures, same
  `sanitize()`, same "return None and degrade to asking cold" contract.

API backend specifics: `anthropic.Anthropic()`, model `claude-opus-5`, adaptive thinking
(`thinking={"type": "adaptive"}`), `output_config={"effort": "low"}` for short essay
drafts. `ANTHROPIC_API_KEY` as a Fly secret. Cache the standing part of the prompt
(identity, facts from the points dump) with `cache_control` — it's identical across every
draft in a run.

**Cost is new and real.** Drafting is free today. Budget it, log `response.usage` into
`fill_reports`, and keep the existing on/off preference so it can be switched off.

**Done when:** the same essay prompt through both backends produces drafts that pass the
existing `test_drafter.py` style assertions.

## Phase 5 — Console + auth

- `web/app` moves under `server/web`, serving the built SPA and the API from the same
  process.
- Email + password. `passlib[argon2]` for hashing, signed HTTP-only session cookie,
  `POST /auth/login`, `/logout`, `/signup` (gated off for now), and a password-reset path
  stubbed but not wired.
- Every existing route in `routes.py` gains a `ctx: Ctx = Depends(current_user)`.
- **Rule:** no route may take a `user_id` from the request body or path. It comes from the
  session, always. This is the other half of the shared-bot safety story.
- CORS drops the hardcoded `127.0.0.1:5173` allowance in hosted mode.

**Done when:** the console is unusable logged out, and every route is scoped by session.

## Phase 6 — Deploy

- `server/Dockerfile`, `fly.toml`. One machine, `min_machines_running = 1` so a held
  WebSocket survives (do NOT let it scale to zero — a sleeping machine drops the
  extension's connection).
- Neon project, `DATABASE_URL` as a Fly secret, Alembic migration on release.
- Secrets: `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`,
  `ANTHROPIC_API_KEY`, `SESSION_SECRET`, `GMAIL_*`.
- Register the Telegram webhook once, pointed at the Fly hostname.
- Import the real data: `export` from the local SQLite, `import` into Neon.
- **Backups from day one.** Neon has its own, but add a nightly `export` to a private
  bucket or a gitignored local pull. Losing `applied.json` means losing dedup history.

**Done when:** `fly deploy` gets a green health check and the phone can answer a question.

## Phase 7 — The extension gets two modes

- Popup setting: **Local** (`ws://127.0.0.1:8765`, today) or **Hosted** (a `wss://` URL +
  a paired device token).
- `manifest.json` host permissions gain the Fly hostname; keep the localhost entry.
- `BRIDGE_URL` becomes a stored setting, not a constant.
- Reconnect with backoff. The current `onclose` handler rejects every in-flight ask — over
  the internet that will fire on ordinary network blips, so it needs to retry rather than
  fail the fill.
- **No behaviour change:** resume bytes still come from the bridge over the socket, just a
  longer wire. The DataTransfer attach is untouched.

**Done when:** the same extension build works against either mode by flipping a setting.

## Phase 8 — Scraper hand-off

- Add `POST /api/ingest/queue`, authenticated by a scoped token.
- `.github/workflows/scrape.yml` posts `queue.json` instead of force-committing `out/`.
- Keep the emailed HTML digest exactly as-is.
- Side benefit: the "local clone falls behind every run, remember to `git pull --ff-only`"
  gotcha in `CLAUDE.md` goes away.

## Phase 9 — Cutover and cleanup

- Run hosted mode for a week of real applications while local mode still works.
- Then: delete the old file-path code, delete `bridge/pending/`, update `README.md`,
  `CLAUDE.md` and `bridge/README.md` to describe both modes.
- `start.py` grows `--mode local|hosted`.

---

# Risks, honestly

| Risk | Why it bites | What to do |
|---|---|---|
| **Cross-user leak** (shared bot) | One chat-id → user mistake sends your salary answer to a stranger's phone | The Phase 2 isolation test, and the "never take user_id from the request" rule |
| **Fly machine sleeps** | Held WebSocket dies mid-application | `min_machines_running = 1`; extension reconnects with backoff |
| **Drafting now costs money** | It's free today via the CLI subscription | Log usage, cache the prompt prefix, keep the off switch |
| **Config stops being files** | You lose the text-editor workflow you use daily | `import`/`export` commands, built in Phase 1, not bolted on later |
| **`bridge.py` is 2,865 lines** | Threading `Ctx` through it by hand will miss something | Split into `handlers/` first, then thread. Lean on the 15 existing test files |
| **Losing applied.json** | It's the dedup key store — losing it means re-applying to places you already applied | Nightly export, verified restore, before cutover |
| **Verification-code email** | `MailCodeReader` needs credentials to a real mailbox, now server-side | Use the existing `GmailReader` OAuth path, not the App Password path. Read-only scope |
| **Fly + IPv6/DNS** | `bridge.py` already carries an IPv6 workaround (`_probe_family`) for Telegram | Retest on Fly; the workaround may be unnecessary there, or differently necessary |

# Explicitly not in scope

- Signup for other people, billing, a marketing page.
- Moving the scraper onto the server.
- Making applications unattended. Chrome, the session cookies and the submit tap stay on
  the laptop by design.
- Any change to `fill.js`, the adapters, or the Tier-0 fill engine.
