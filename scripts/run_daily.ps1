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

# --- 1. Refresh today's queue (best-effort; the bridge warns if it's stale anyway) ------
# Narrow + non-destructive: fetch origin and check out ONLY the scraper outputs, so local
# commits and working changes are never touched. A local clone diverges from origin every
# run (Actions force-commits out/* back), which is exactly why a full `git pull` is unsafe
# here - see the project notes. Run against the DATA repo, which is what the Action commits
# the queue back to.
try {
    Push-Location $dataHome
    git fetch --quiet origin main 2>&1 | Out-Null
    # queue.json is the only file the bridge reads; check out just that one so a missing
    # sibling (digest.html isn't tracked on origin) can't fail the whole refresh.
    git checkout origin/main -- out/queue.json 2>&1 | Out-Null
    Log "Refreshed out/queue.json from origin/main in $dataHome."
} catch {
    Log "Queue refresh skipped ($($_.Exception.Message)); the bridge will flag a stale queue."
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
