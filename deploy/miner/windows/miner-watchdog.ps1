# Watchdog fuer einen Miner unter Windows (z. B. SRBMiner-MULTI).
# Wird von einer geplanten Aufgabe alle 2 Minuten gestartet (siehe install-miner-tasks.ps1).
#
# - Miner-Prozess fehlt                  -> Miner-Aufgabe starten (schtasks /run)
# - Prozess laeuft, API stumm / Hashrate 0 bei MaxBad Pruefungen in Folge -> beenden und neu starten
# Greift NICHT ein, wenn die Pausen-Datei existiert. Dashboard und miner-gate legen sie beim
# Stoppen an und loeschen sie beim Starten - sonst wuerde der Watchdog gegen sie arbeiten.
param(
  [string]$MinerTask   = "MinerStart",                  # geplante Aufgabe, die den Miner startet
  [string]$ProcessName = "SRBMiner-MULTI",              # ohne .exe
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
  Log "Prozess $ProcessName fehlt -> starte Aufgabe $MinerTask"
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
Log "Hashrate=$hr (API -1 = keine Antwort), schlecht $bad/$MaxBad"
if ($bad -ge $MaxBad) {
  Log "Neustart: beende $ProcessName, starte Aufgabe $MinerTask"
  Stop-Process -Name $ProcessName -Force
  Start-Sleep -Seconds 5
  schtasks /run /tn $MinerTask | Out-Null
  Remove-Item $state -Force
}
