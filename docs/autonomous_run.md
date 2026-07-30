# Autonomous daily run

A fully unattended **P1 collection** pass, fired by Windows Task Scheduler each morning.
It opens the day's queued jobs, fills them deterministically, records what each still
needs, and closes the tabs — then you review on Telegram at your own pace and the flush
submits what you approve. **Nothing is submitted by the daily run.** Submit stays your
tap; that split is the locked architecture.

## Pieces

| Piece | What it does |
|---|---|
| `scripts/install_daily_task.ps1` | Registers the scheduled task (run once). |
| `scripts/run_daily.ps1` | The task's action: refresh queue → ensure bridge → ensure Chrome → drop the trigger. |
| `bridge/.run_trigger` | Sentinel file the launcher drops; the bridge watches for it. (gitignored) |
| `trigger_watcher()` in `bridge.py` | Sees the sentinel, waits for the extension to connect, fires `runQueue`. |
| `run_queue` WS push → `background.js` | The extension starts the collection pass (same entry point as the popup ▶). |

### Why a file and not a WebSocket poke
The bridge **supersedes on every new WS connection** (it closes the old client and drains
the inbox). So the launcher can't open a socket to say "go" — that would kill a live
review. The sentinel file is WS-free, token-free local IPC that works whether the bridge
was cold-started by the launcher or was already running from earlier in the day.

## Install (once)

```powershell
# from the repo root, in a normal (non-elevated) PowerShell:
powershell -ExecutionPolicy Bypass -File .\scripts\install_daily_task.ps1
# different time (local): ... -File .\scripts\install_daily_task.ps1 -At 18:00
```

Default is **09:30 local** — the machine is on India Standard Time (UTC+5:30), so that's
one hour after the 08:30 IST discovery scrape (03:00 UTC), when today's `queue.json` is
fresh. The hour is slack for GitHub's best-effort cron; a late scrape means this run
silently reuses yesterday's queue. If you move either time, keep discovery first. The task runs only while you're logged
in (Chrome needs your interactive desktop + real profile), wakes the machine if asleep,
and catches up if the machine was off at the trigger time.

Manage it:
```powershell
Get-ScheduledTask -TaskName JobPipeline-DailyCollect        # inspect
Start-ScheduledTask -TaskName JobPipeline-DailyCollect      # run now (a live dry run)
Unregister-ScheduledTask -TaskName JobPipeline-DailyCollect -Confirm:$false   # remove
```

## What a run does, step by step

1. **Refresh the queue.** `git fetch origin main` + check out ONLY `out/queue.json` from
   `origin/main`. Non-destructive — never touches your local commits or
   working changes (a full `git pull` is unsafe here because Actions force-commits `out/*`
   back and the clone diverges every run). Guarded: a git failure just logs and continues;
   the bridge warns separately if the queue is stale (>26h).
2. **Ensure the bridge.** If port 8765 isn't listening, start `bridge/bridge.py` hidden and
   wait up to 30s for it to come up. It inherits the User env vars and loads `bridge/.env`.
3. **Ensure Chrome.** Launch Chrome on the `Default` profile with
   `--silent-debugger-extension-api`. If Chrome was closed overnight this is a clean start
   and the flag applies; if it was already open the flag no-ops and Greenhouse fills show a
   harmless "extension is debugging this browser" info bar — the run still works, so we
   never force-close your Chrome.
4. **Drop the trigger.** Create `bridge/.run_trigger`. The bridge's `trigger_watcher`
   (polling ~3s) removes it and calls `runQueue`, waiting up to 2 min for the extension to
   connect first. `runQueue` posts its own "🔁 Reading N role(s)" progress to Telegram.

## Manual trigger

Send **`/run`** on Telegram anytime to start a collection pass on demand — same path as the
scheduled trigger. Useful if you missed a day, or want to re-collect after a fresh scrape.

## Troubleshooting

- **"Tried to start… but Chrome/the extension never connected."** Chrome wasn't open (or
  the service worker hadn't reconnected) within 2 min of the trigger. Open Chrome and send
  `/run`. The extension re-dials the bridge every minute, so just having Chrome open is
  enough.
- **Run didn't fire at all.** Check the task last-run result: `Get-ScheduledTaskInfo
  -TaskName JobPipeline-DailyCollect`. The task only runs while you're logged in.
- **Nothing collected / "Nothing to apply to today."** The queue is empty or everything's
  already applied/in-flight. Confirm `out/queue.json` refreshed (step 1) — the run logs
  whether the refresh succeeded.
- **Bridge deploy note:** changing `bridge.py` requires a bridge restart to take effect —
  kill the PID on 8765 and relaunch (or just let the next `scripts/run_daily.ps1` start a fresh
  one if it isn't running). Changing `background.js` requires reloading the unpacked
  extension at `chrome://extensions`.
