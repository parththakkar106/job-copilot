# Job Dashboard — state model & endpoints (spec v1)

Popup becomes a dashboard over every pile: see all roles by date, open the link, and
mark any role to a new state. The popup already talks to the bridge through the
background service worker's WebSocket, so this reuses that channel — no new socket.

Design decisions (locked with Parth 2026-07-20):
- **Offline:** cached read-only view. Every successful `list_all` is cached to
  `chrome.storage`; when the bridge is unreachable the popup renders the cache with an
  "offline" banner and disables the mark actions.
- **"Filled" granularity:** one bucket, reason tag (approved = "ready", held = its reason).
- Applied pile defaults to a recent window (60 days) with a "show all" override.

## 1. Canonical states (one bucket per job)

| Bucket | Underlying status | Source | Tag |
|---|---|---|---|
| **In queue** | `awaiting` (unanswered Qs, or complete-but-unapproved) | pending | "N questions" / "ready to approve" |
| | `skipped` *(new)* — deferred, kept visible | pending | "skipped" |
| **Filled (not submitted)** | `approved` — auto-submits on next flush | pending | "ready to submit" |
| | `held` — filled, couldn't submit, needs you | pending | hold reason, e.g. "OTP" |
| **Submitted** | applied.json `applied` | applied.json | applied date |
| **Blocked** | applied.json `rejected` — permanent skip | applied.json | "blocked" |

Sort date = pending `created` or applied `applied` (both already stored).

## 2. Transitions — `set_state(dedup_key, target, reason?, meta?)`

`meta` = {company,title,location,url} so an applied-only row (no pending file) still
writes a complete applied.json record.

- **→ submitted**: append applied.json (`applied`) + delete pending. Popup confirms first.
- **→ blocked**: append applied.json (`rejected`) + delete pending.
- **→ filled**: `pending.approve()` — guarded, refuses if questions are still unanswered
  or there's no pending record (popup surfaces "answer its questions first").
- **→ in_queue**: if it's in applied.json, `remove_applied()` (un-submit / un-block →
  re-enters the scrape pool); if a pending record exists, `reopen()` back to `awaiting`.
- **→ skipped**: `pending.skip()` (only for a job that has a pending record).

**Rule:** forward moves are clean; backward = "remove the terminal record, let it
re-scrape." We do NOT reconstruct a filled form after submit (that data is gone) — so
un-submit = re-queue for a fresh fill, not restore the old answers.

## 3. Bridge endpoints
- `list_all {since_days?}` → `{rows:[{dedup_key, company, title, location, url, bucket,
  tag, date, unanswered}]}`, sorted date-desc. Applied pile limited to `since_days`
  (default 60) — pending is always included in full.
- `set_state {dedup_key, target, reason?, meta?}` → `{ok, error?}`. Applied-log writes are
  idempotent (`append_applied`) and deletes are idempotent, so a race with an in-flight
  flush degrades safely (no double-apply).

## 4. Popup UI
- Toolbar: ▶ Run the queue · ⏹ Stop · 💼 panel.
- Filter chips: All · In queue · Filled · Submitted · Blocked (with counts).
- Row (date-desc): `MM-DD  Company — Title  ·tag·  🔗  Mark ▾`.
  - 🔗 opens the url in a new tab.
  - Mark ▾ lists valid targets; **submitted / blocked confirm first**.
- Concurrency guard: the popup asks background `RUN_STATE`; while a queue/flush run is
  live it disables the mark actions (an in-flight fill owns those jobs).
- Offline: cache each `list_all`; on bridge-unreachable render the cache read-only + banner.

## 5. Code touch points
- `pending.py`: add `SKIPPED`; `skip()`, `reopen()`; `counts()` gains skipped. Flush +
  idle-watcher already select only `APPROVED`, so `skipped` is excluded for free.
- `triage.py`: add `remove_applied()` (un-submit / un-block).
- `bridge.py`: `handle_list_all`, `handle_set_state`; register in the dispatch map.
  `handle_queue` already excludes every pending key, so `skipped` is not re-served.
- `background.js`: `LIST_ALL`, `SET_STATE`, `RUN_STATE` message handlers.
- `popup.html` / `popup.js`: rewrite to the dashboard.
- Tests: extend `test_pending` / `test_handlers`.
