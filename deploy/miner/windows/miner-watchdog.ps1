# Watchdog for a miner on Windows (e.g. SRBMiner-MULTI).
# Started every 2 minutes by a scheduled task (see install-miner-tasks.ps1).
#
# - miner process missing                                   -> start the miner task (schtasks /run)
# - process running, API silent / hashrate 0 for MaxBad checks in a row -> kill and restart
# Does NOT act while the pause file exists. The dashboard and miner-gate create it when stopping and
# delete it when starting - otherwise the watchdog would work against them.
param(
  [string]$MinerTask   = "MinerStart",                  # scheduled task that starts the miner
  [string]$ProcessName = "SRBMiner-MULTI",              # without .exe
  [string]$Api         = "http://127.0.0.1:21550/",
  [string]$PauseFlag   = "$env:ProgramData\miner\paused.flag",
  [int]$GraceSeconds   = 180,
  [int]$MaxBad         = 3
)
$ErrorActionPreference = "SilentlyContinue"
$dir   = Join-Path $env:ProgramData "miner"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$state = Join-Path $dir "watchdog.count"
$log   = Join-Path $dir "watchdog.log"
function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Add-Content -Path $log }

if (Test-Path $PauseFlag) { Remove-Item $state -Force; exit 0 }

$p = Get-Process -Name $ProcessName | Select-Object -First 1
if (-not $p) {
  Log "process $ProcessName missing -> starting task $MinerTask"
  schtasks /run /tn $MinerTask | Out-Null
  Remove-Item $state -Force
  exit 0
}
if (((Get-Date) - $p.StartTime).TotalSeconds -lt $GraceSeconds) { exit 0 }

$hr = -1
try {
  $d  = Invoke-RestMethod -Uri $Api -TimeoutSec 5
  $a  = $d.algorithms[0].hashrate
  $hr = [double]($(if ($a.'1min') { $a.'1min' } else { $a.now }))
} catch { $hr = -1 }

if ($hr -gt 0) { Remove-Item $state -Force; exit 0 }

$bad = 1 + [int](Get-Content $state -ErrorAction SilentlyContinue)
Set-Content -Path $state -Value $bad
Log "hashrate=$hr (-1 = API not answering), bad $bad/$MaxBad"
if ($bad -ge $MaxBad) {
  Log "restart: killing $ProcessName, starting task $MinerTask"
  Stop-Process -Name $ProcessName -Force
  Start-Sleep -Seconds 5
  schtasks /run /tn $MinerTask | Out-Null
  Remove-Item $state -Force
}
