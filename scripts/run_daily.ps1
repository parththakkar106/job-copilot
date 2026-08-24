# run_daily.ps1 - the autonomous daily collection run.
#
# Fired by Windows Task Scheduler (see install_daily_task.ps1). Brings up everything the
# P1 collection pass needs, then hands off to the bridge to start it:
#
#   1. Refresh out/queue.json from origin (today's scrape) - non-destructive, guarded.
#   2. Ensure the Telegram bridge is running (start it hidden if the port is dead).
#   3. Ensure Chrome is running on Parth's real profile, with the CDP info-bar suppressed.
#   4. Drop the sentinel file the bridge watches; its trigger_watcher fires runQueue once
#      the extension has connected.
#
# This runs ONLY P1 collection. Nothing is submitted here - Parth still reviews on
# Telegram and the flush submits what he approves. That split is the locked architecture.
#
# Design notes:
#   * The bridge SUPERSEDES on every new WebSocket connection, so the launcher must NOT
#     poke the WS to start the run (that would kill a live review). The sentinel file is
#     the WS-free handoff - it works whether the bridge is cold-started here or was
#     already running from earlier in the day.
#   * --silent-debugger-extension-api only takes effect on a FRESH Chrome start. If Chrome
#     is already open the flag is ignored and Greenhouse fills show a harmless "extension
#     is debugging this browser" info bar - the run still works, so we never force-close
#     Chrome to claim the flag.

$ErrorActionPreference = 'Continue'
# This script lives in scripts/, so the repo root is one level up. The CODE (the bridge,
# the sentinel) is relative to that root.
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

function Log($msg) { Write-Host "[$(Get-Date -Format HH:mm:ss)] $msg" }

# out/queue.json is DATA, so it lives in JOBCOPILOT_HOME - a different repo from this one
# once the two halves were split. Unset, it is the repo root, which is the pre-split layout.
$dataHome = $env:JOBCOPILOT_HOME
if (-not $dataHome) { $dataHome = $repo }

# --- 1. Sync with origin, publish applied.json, refresh today's queue -------------------
# Three things against the DATA repo, which is what the Action commits the queue back to.
# All best-effort: a missed sync costs one day of freshness, a thrown error costs the whole
# collection run, so every step here logs and swallows.
#
# THIS MACHINE IS THE ONLY PLACE applied.json EXISTS. The Action runs on a fresh checkout,
# so the ledger reaches tomorrow's scrape only if it is pushed today. It was gitignored
# until 2026-08-18, which made scraper.py's already-applied filter a no-op in CI: `applied`
# was an empty set every morning and 8 of the 20 queued roles (measured) were ones already
# applied to. Publishing it here is the half of that fix which cannot live in the repo.
try {
    Push-Location $dataHome
    git fetch --quiet origin main 2>&1 | Out-Null

    # Fast-forward rather than cherry-picking one file. The Action force-commits out/* every
    # morning, so this clone is permanently behind origin - which meant the checkout below
    # left out/queue.json staged indefinitely, and a staged file blocks the rebase the push
    # needs. --ff-only is the safe form: with local commits it refuses outright and changes
    # nothing, which is why the narrow checkout is kept below as the fallback.
    git merge --ff-only --quiet origin/main 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Log "Fast-forwarded $dataHome to origin/main."
    } else {
        Log "No fast-forward (local commits present) - falling back to a file-only refresh."
    }

    # Only ever commit this one path: --only ignores whatever else may be staged, so a
    # generated artifact can never ride along into the ledger's history.
    if (git status --porcelain -- applied.json) {
        git commit --only applied.json -q -m "applied $(Get-Date -UFormat %Y-%m-%d)" 2>&1 | Out-Null
        git push --quiet origin main 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Log "Pushed applied.json - tomorrow's scrape will dedup against it."
        } else {
            # "Retries tomorrow" is only true if the push CAN succeed tomorrow. A rejected
            # push means this clone has diverged, and every later day rejects identically -
            # so the ledger stops reaching CI and the scrape's already-applied filter goes
            # back to being the no-op that ab933ec existed to fix.
            Log "applied.json is committed locally but the push was REJECTED - this clone has"
            Log "diverged from origin. It will keep failing until you run, in ${dataHome}:"
            Log "    git pull --rebase origin main; git push"
        }
    } else {
        Log "applied.json unchanged since the last run."
    }

    # Redundant after a clean fast-forward, and load-bearing on a day the fast-forward was
    # refused. Each path is checked out SEPARATELY on purpose: a single command listing all
    # of them fails wholesale if one path is missing on origin, and a missing sibling must
    # never cost the queue its refresh.
    #
    # dream.json and digest.md are here as of 2026-08-24 because they weren't, and it broke
    # the console's Dream pile in the least visible way possible. One un-pushed local commit
    # ("applied 2026-08-21") made --ff-only refuse EVERY morning from then on, so this
    # fallback was the only refresh happening - and it refreshed queue.json alone. The board
    # showed a queue from today beside a Dream pile frozen a week earlier, with nothing
    # anywhere saying so. The bridge still only reads queue.json; the web console reads all
    # three, and it is just as much a consumer of the scrape.
    foreach ($f in @('out/queue.json', 'out/dream.json', 'out/manual.json', 'out/digest.md')) {
        git checkout origin/main -- $f 2>&1 | Out-Null
    }
    Log "Refreshed out/queue.json + dream.json + manual.json + digest.md from origin/main in $dataHome."

    # A refusal above means this clone has drifted off origin and is now living on the
    # narrow fallback indefinitely - every generated file NOT in that list silently rots.
    # It used to log one indistinguishable line and carry on for a week. Say it plainly.
    $ahead = (git rev-list --count origin/main..HEAD 2>$null)
    if ($ahead -and [int]$ahead -gt 0) {
        Log "WARNING: $dataHome is $ahead commit(s) ahead of origin/main, so the daily"
        Log "         fast-forward is refused and only the files listed above refresh."
        Log "         Fix it once with:  git -C `"$dataHome`" pull --rebase origin main; git -C `"$dataHome`" push"
    }
} catch {
    Log "Origin sync skipped ($($_.Exception.Message)); the bridge will flag a stale queue."
} finally {
    Pop-Location
}

# --- 2. Ensure the bridge is running ----------------------------------------------------
function Test-BridgeUp {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect('127.0.0.1', 8765); $c.Close(); return $true
    } catch { return $false }
}

if (Test-BridgeUp) {
    Log "Bridge already up on 8765."
} else {
    Log "Starting the bridge..."
    # Hidden, detached, working dir = repo root. It inherits the User env vars
    # (TELEGRAM_BOT_TOKEN / CHAT_ID) and also loads bridge/.env on its own.
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue).Source }
    if (-not $py) { Log "ERROR: python not found on PATH - cannot start the bridge."; exit 1 }
    Start-Process -FilePath $py -ArgumentList 'bridge\bridge.py' `
        -WorkingDirectory $repo -WindowStyle Hidden | Out-Null

    $deadline = (Get-Date).AddSeconds(30)
    while (-not (Test-BridgeUp) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    if (Test-BridgeUp) {
        Log "Bridge is up."
        # Let its trigger_watcher clear any stale sentinel from a prior crash BEFORE we
        # drop ours, or the boot-clear would eat the fresh one and the run wouldn't fire.
        Start-Sleep -Seconds 5
    } else {
        Log "ERROR: bridge did not come up within 30s."; exit 1
    }
}

# --- 3. Ensure Chrome is running on the real profile ------------------------------------
$chrome = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LocalAppData\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $chrome) {
    Log "ERROR: chrome.exe not found in the usual locations."; exit 1
}

# --silent-debugger-extension-api -> no info bar when the extension attaches chrome.debugger
# for Greenhouse trusted clicks. --profile-directory keeps his real, logged-in profile
# (cookies matter). If Chrome is already open this just focuses it and the flag no-ops -
# that's fine (see the header note).
Start-Process -FilePath $chrome -ArgumentList `
    '--silent-debugger-extension-api', '--profile-directory=Default' | Out-Null
Log "Chrome launched (or focused if already open)."

# --- 4. Hand off to the bridge ----------------------------------------------------------
# The trigger_watcher (polling every ~3s) removes this file and fires runQueue, waiting up
# to 2 min for the extension to connect first. runQueue posts its own progress to Telegram.
New-Item -ItemType File -Path (Join-Path $repo 'bridge\.run_trigger') -Force | Out-Null
Log "Dropped the run trigger - the bridge will start collecting once the extension connects."
