# Job Pipeline Console (web UI)

A local web dashboard over the whole pipeline — everything you can do from the extension
popup and Telegram, plus editing the cached answers, profile, and company tiers, all in
one place.

- **Dashboard** — every pile (In queue / Filled / Submitted / Blocked) in one dated,
  filterable list, with dream vs pipeline badges. Open a role to edit its answers, pin a
  resume track, move it between piles, or submit it.
- **Answer bank** — view / edit / add / delete the reusable answers served at apply-time.
- **Profile** — edit the deterministic-autofill values in `profile.json`, then restage
  them into the extension with one click.
- **Companies** — see your targets split dream vs pipeline; flip a tier in place.
- **Run controls** — refresh discovery (scraper), start a collection run, send the
  approved pile.

## Architecture — why it's a separate process

The console is a **standalone FastAPI + React app**, not part of the bridge. It shares the
exact same on-disk state and reuses the pipeline's own modules (`bridge/pending.py`,
`bridge/triage.py`, `core/bank.py`), but it **never opens a WebSocket to the bridge** — the
bridge supersedes on every new WS connection, so a second client would kill the
extension's live review mid-run.

To reach the extension (start a collection run, send the approved pile) the console drops
a **sentinel file** the bridge already watches:

- `bridge/.run_trigger`  → P1 collection (fill, never submit) — the existing daily-run path.
- `bridge/.flush_trigger` → P3 flush (submit the approved pile) — added for this console.

Submitting is still done **only** by the extension on your machine with your live session.
The console's "Submit this one" / "Send approved" buttons approve the job(s) and request a
flush; the extension does the actual fill + submit through its hardened loop.

```
  React SPA ──HTTP──> FastAPI (web/app) ──imports──> pending.py / triage.py / core.bank
                          │                              │
                          │ writes                       │ read/write
                          ▼                              ▼
                 bridge/.flush_trigger          bridge/pending/*.json, applied.json,
                 bridge/.run_trigger            data/answer_bank.yaml, data/profile.json,
                          │                      data/companies.yaml, out/queue.json
                          ▼
                 bridge (watches sentinels) ──WS──> extension ──> submits
```

## Running it

First time only, install the Python deps (FastAPI/uvicorn on top of the bridge deps):

```bash
pip install -r web/requirements.txt
```

### Start it

From the repo root:

```bash
python start.py            # bridge + console at http://127.0.0.1:8000
python start.py --dev      # + Vite hot reload on :5173 (open that one instead)
python start.py --no-bridge
```

`start.py` is the single launcher for the whole pipeline — it builds the UI when its
sources have changed, then runs the console and the bridge together. See the repo README
for the full flag list.

The backend serves the API under `/api` and, when `web/frontend/dist` exists, the built UI
at `/`. It binds 127.0.0.1 (loopback only).

For the run controls and per-job submit to actually do anything the **bridge must be
running** with Chrome + the extension connected — same as Telegram. `start.py` starts it
for you (given `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in the environment or
`bridge/.env`); the bridge status pill (top-right) shows green when it's reachable.

## Tests

```bash
python web/test_api.py            # 42 checks — API over an isolated temp data layer
python bridge/test_flush_trigger.py   # 8 checks — the .flush_trigger handoff
```

## Local-first, and hosting later

`start.py` binds the console to `127.0.0.1` on purpose — the state files hold PII (address, phone,
per-job answers). To reach it from your phone or another device later, don't change the
bind: put it behind a tunnel / reverse proxy that adds authentication (e.g. Cloudflare
Access, Tailscale, or an nginx basic-auth front). The app itself has **no auth** yet, so
never expose it directly on `0.0.0.0`.
