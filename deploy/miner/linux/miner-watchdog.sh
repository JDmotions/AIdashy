#!/usr/bin/env bash
# Watchdog fuer einen Miner unter systemd: startet die Unit neu, wenn sie laeuft,
# aber die Miner-API nicht antwortet oder die Hashrate mehrfach hintereinander 0 ist.
# Laeuft als root ueber miner-watchdog.timer (alle 2 min).
#
# Greift NICHT ein, wenn
#   - die Unit absichtlich gestoppt ist (Dashboard-Schalter, miner-gate: systemctl stop),
#   - der Prozess pausiert ist (miner-gate: SIGSTOP, Status "T"),
#   - der Start weniger als GRACE Sekunden her ist,
#   - die Datei $PAUSE_FLAG existiert (manuelle Wartung).
set -u
UNIT="${UNIT:-miner.service}"
PROC="${PROC:-SRBMiner-MULTI}"
API="${API:-http://127.0.0.1:21550/}"
GRACE="${GRACE:-180}"          # Sekunden nach dem Start ignorieren (DAG/Autotune)
MAX_BAD="${MAX_BAD:-3}"        # so viele schlechte Pruefungen in Folge -> Neustart
STATE="${STATE:-/run/miner-watchdog.count}"
PAUSE_FLAG="${PAUSE_FLAG:-/run/miner.paused}"
log() { logger -t miner-watchdog "$*"; echo "$*"; }

[ -e "$PAUSE_FLAG" ] && exit 0
[ "$(systemctl is-active "$UNIT")" = "active" ] || { rm -f "$STATE"; exit 0; }

pid=$(systemctl show -p MainPID --value "$UNIT")
[ -n "$pid" ] && [ "$pid" != 0 ] || exit 0
stat=$(ps -o stat= -p "$pid" 2>/dev/null | tr -d ' ')
case "$stat" in T*) rm -f "$STATE"; exit 0;; esac        # vom Gate pausiert

since=$(systemctl show -p ActiveEnterTimestampMonotonic --value "$UNIT")
now=$(awk '{printf "%d", $1*1000000}' /proc/uptime)
[ $(( (now - since) / 1000000 )) -lt "$GRACE" ] && exit 0

hr=$(curl -s -m 5 "$API" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    a = (d.get("algorithms") or [{}])[0].get("hashrate") or {}
    print(float(a.get("1min") or a.get("now") or 0))
except Exception:
    print(-1)')

if [ "${hr%.*}" -gt 0 ] 2>/dev/null; then rm -f "$STATE"; exit 0; fi

bad=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$bad" > "$STATE"
log "$UNIT: Hashrate=${hr} (API -1 = keine Antwort), schlecht $bad/$MAX_BAD"
if [ "$bad" -ge "$MAX_BAD" ]; then
  log "$UNIT: Neustart"
  rm -f "$STATE"
  systemctl restart "$UNIT"
fi
