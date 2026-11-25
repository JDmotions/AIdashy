#!/usr/bin/env bash
# Watchdog for a miner under systemd: restarts the unit when it is running but the miner API
# does not answer or the hashrate is 0 several times in a row.
# Runs as root via miner-watchdog.timer (every 2 min).
#
# Does NOT act when
#   - the unit was stopped on purpose (dashboard switch, miner-gate: systemctl stop),
#   - the process is paused (miner-gate: SIGSTOP, state "T"),
#   - the start was less than GRACE seconds ago,
#   - the file $PAUSE_FLAG exists (manual maintenance).
set -u
UNIT="${UNIT:-miner.service}"
PROC="${PROC:-SRBMiner-MULTI}"
API="${API:-http://127.0.0.1:21550/}"
GRACE="${GRACE:-180}"          # ignore the first seconds after a start (DAG/autotune)
MAX_BAD="${MAX_BAD:-3}"        # this many bad checks in a row -> restart
STATE="${STATE:-/run/miner-watchdog.count}"
PAUSE_FLAG="${PAUSE_FLAG:-/run/miner.paused}"
log() { logger -t miner-watchdog "$*"; echo "$*"; }

[ -e "$PAUSE_FLAG" ] && exit 0
[ "$(systemctl is-active "$UNIT")" = "active" ] || { rm -f "$STATE"; exit 0; }

pid=$(systemctl show -p MainPID --value "$UNIT")
[ -n "$pid" ] && [ "$pid" != 0 ] || exit 0
stat=$(ps -o stat= -p "$pid" 2>/dev/null | tr -d ' ')
case "$stat" in T*) rm -f "$STATE"; exit 0;; esac        # paused by the gate

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
log "$UNIT: hashrate=${hr} (-1 = API not answering), bad $bad/$MAX_BAD"
if [ "$bad" -ge "$MAX_BAD" ]; then
  log "$UNIT: restarting"
  rm -f "$STATE"
  systemctl restart "$UNIT"
fi
