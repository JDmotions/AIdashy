#!/bin/bash
# Cloudflare-Quick-Tunnel fuer das Cluster-Dashboard. Neue URL -> tunnel.url (+ optional Mail).
# Optionale lokale Einstellungen in tunnel.env (nicht im Git), z. B.:
#   MAIL_CMD=sendmail          # Programm mit Aufruf: MAIL_CMD -s "Betreff" "Text"
#   MAIL_PREFIX="opencode: "   # Praefix fuer den Betreff
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
    PATH="$HOME/.local/bin:$PATH" $MAIL_CMD -s "${MAIL_PREFIX}Cluster-Dashboard extern erreichbar" \
      "Neue Tunnel-URL: $URL
Login wie eingerichtet. Im LAN ohne Login: http://${LAN:-<rechner>}:$PORT/" >/dev/null 2>&1
  fi
fi
wait $CF
