# install_daily_task.ps1 - register (or update) the autonomous daily collection run in
# Windows Task Scheduler. Run this ONCE (from a normal, non-elevated PowerShell):
#
#     powershell -ExecutionPolicy Bypass -File .\install_daily_task.ps1
#
# Re-running it safely replaces the existing task. To change the time, pass -At:
#
#     powershell -ExecutionPolicy Bypass -File .\install_daily_task.ps1 -At 18:00
#
# The task runs run_daily.ps1 daily at the chosen LOCAL time. Parth's machine is on
# India Standard Time (UTC+5:30), so 09:30 local = 09:30 IST - one hour after the 08:30 IST
# discovery scrape (03:00 UTC, see .github/workflows/scrape.yml). It runs
# only while he's logged in (Chrome needs his interactive desktop + real profile), and is
# set to wake the machine and to catch up if the machine was asleep at the trigger time.

param(
    [string]$At = "09:30",
    [string]$TaskName = "JobPipeline-DailyCollect"
)

# Both scripts live in scripts/; the task's working dir must be the repo ROOT (one up).
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot 'run_daily.ps1'
if (-not (Test-Path $script)) { Write-Error "run_daily.ps1 not found next to this installer."; exit 1 }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"" `
    -WorkingDirectory $repo

$trigger = New-ScheduledTaskTrigger -Daily -At $At

# WakeToRun: pull the machine out of sleep for the run. StartWhenAvailable: if it was off
# or asleep past the trigger, run as soon as it's back rather than skipping the day.
# RunOnlyIfNetworkAvailable: delay the start until Windows reports a connection. This is a
# coarse check, so run_daily.ps1 also retries its fetch until origin is reachable.
$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

# Interactive (logged-on) principal - Chrome must run in Parth's desktop session with his
# real profile. No stored password needed; the task simply won't fire while he's logged out.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "Job pipeline: daily P1 collection pass (fills the day's queue, never submits)." `
    -Force | Out-Null

Write-Host "Registered '$TaskName' - daily at $At (local time)."
Write-Host "Inspect it:   Get-ScheduledTask -TaskName $TaskName"
Write-Host "Run it now:   Start-ScheduledTask -TaskName $TaskName"
Write-Host "Remove it:    Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
