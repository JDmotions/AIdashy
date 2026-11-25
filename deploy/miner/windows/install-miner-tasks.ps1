# Creates two scheduled tasks for the currently logged-on user (PowerShell as administrator):
#   1. MinerStart     - starts the miner at logon, in the user session (GPU/console),
#                       restart on failure, never two instances. The dashboard/miner-gate start it with
#                       `schtasks /run /tn MinerStart`.
#   2. MinerWatchdog  - runs miner-watchdog.ps1 every 2 minutes (hidden).
#
# Usage, e.g.:
#   powershell -ExecutionPolicy Bypass -File install-miner-tasks.ps1 `
#     -MinerExe "D:\miner\SRBMiner-MULTI.exe" -MinerArgs "--config-file D:\miner\config.txt --api-enable --api-port 21550"
# or with your own launcher/script instead of the exe (e.g. a .bat).
param(
  [Parameter(Mandatory = $true)][string]$MinerExe,
  [string]$MinerArgs = "",
  [string]$MinerTask = "MinerStart",
  [string]$WatchdogTask = "MinerWatchdog",
  [string]$ProcessName = "SRBMiner-MULTI"
)
$ErrorActionPreference = "Stop"
$user = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest

# 1. Miner
$workDir = Split-Path -Parent $MinerExe
$action  = if ($MinerArgs) { New-ScheduledTaskAction -Execute $MinerExe -Argument $MinerArgs -WorkingDirectory $workDir }
           else            { New-ScheduledTaskAction -Execute $MinerExe -WorkingDirectory $workDir }
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$set = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
       -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
       -StartWhenAvailable
Register-ScheduledTask -TaskName $MinerTask -Action $action -Trigger $trigger -Principal $principal -Settings $set -Force | Out-Null
Write-Host "Task $MinerTask created."

# 2. Watchdog
$dir = Join-Path $env:ProgramData "miner"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$wd = Join-Path $dir "miner-watchdog.ps1"
Copy-Item -Force (Join-Path $PSScriptRoot "miner-watchdog.ps1") $wd
$wdAction = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$wd`" -MinerTask $MinerTask -ProcessName $ProcessName"
$wdTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes 2)
$wdSet = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 1) `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $WatchdogTask -Action $wdAction -Trigger $wdTrigger -Principal $principal -Settings $wdSet -Force | Out-Null
Write-Host "Task $WatchdogTask created (every 2 min). Log: $dir\watchdog.log"
Write-Host "To pause without the watchdog interfering: create $dir\paused.flag."
