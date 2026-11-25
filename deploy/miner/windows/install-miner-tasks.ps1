# Legt zwei geplante Aufgaben fuer den aktuell angemeldeten Benutzer an (PowerShell als Administrator):
#   1. MinerStart     - startet den Miner bei der Anmeldung, in der Benutzersitzung (GPU/Konsole),
#                       Neustart bei Fehler, nie zwei Instanzen. Dashboard/miner-gate starten ihn per
#                       `schtasks /run /tn MinerStart`.
#   2. MinerWatchdog  - alle 2 Minuten miner-watchdog.ps1 (unsichtbar).
#
# Aufruf, z. B.:
#   powershell -ExecutionPolicy Bypass -File install-miner-tasks.ps1 `
#     -MinerExe "D:\miner\SRBMiner-MULTI.exe" -MinerArgs "--config-file D:\miner\config.txt --api-enable --api-port 21550"
# oder mit einem eigenen Start-Programm/-Skript statt der exe (z. B. ein .bat oder ein eigenes Start-Programm).
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
Write-Host "Aufgabe $MinerTask angelegt."

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
Write-Host "Aufgabe $WatchdogTask angelegt (alle 2 min). Log: $dir\watchdog.log"
Write-Host "Pausieren ohne Watchdog-Eingriff: Datei $dir\paused.flag anlegen."
