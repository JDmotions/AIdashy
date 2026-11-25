#!/bin/bash
# Cloudflare quick tunnel for the dashboard. New URL -> tunnel.url (+ optional e-mail).
# Optional local settings in tunnel.env (not in git), e.g.:
#   MAIL_CMD=sendmail          # program called as: MAIL_CMD -s "subject" "text"
#   MAIL_PREFIX="[dash] "      # subject prefix
DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$DIR/tunnel.env" ] && . "$DIR/tunnel.env"
LOG="$DIR/tunnel.log"; URLF="$DIR/tunnel.url"
PORT=$(python3 -c "import json;print(json.load(open('$DIR/config.json')).get('port',8095))" 2>/dev/null || echo 8095)
LAN=$(hostname -I 2>/dev/null | awk '{print $1}')
: > "$LOG"
"$DIR/bin/cloudflared" tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >> "$LOG" 2>&1 &
CF=$!
trap 'kill $CF 2>/dev/null; rm -f "$URLF"' EXIT TERM INT
for i in $(seq 1 60); do
  URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" | head -1)
  [ -n "$URL" ] && break; sleep 1
done
if [ -n "$URL" ]; then
  OLD=$(cat "$URLF.last" 2>/dev/null)
  echo "$URL" > "$URLF"; echo "$URL" > "$URLF.last"; chmod 600 "$URLF.last"
  if [ "$URL" != "$OLD" ] && [ -n "$MAIL_CMD" ]; then
    PATH="$HOME/.local/bin:$PATH" $MAIL_CMD -s "${MAIL_PREFIX}Cluster dashboard reachable from outside" \
      "New tunnel URL: $URL
Login as configured. On the LAN without login: http://${LAN:-<host>}:$PORT/" >/dev/null 2>&1
  fi
fi
wait $CF
