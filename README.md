# Cluster-Dashboard

Telemetrie für mehrere LLM-Server (Maya / Basalt / Strata) im LAN: Auslastung, VRAM, Leistung,
Durchsatz, letzte Requests, Miner-Steuerung. Ein Python-Prozess ohne Abhängigkeiten (nur Standardbibliothek).

## Neues System einrichten

```bash
git clone <repo> ~/tools/cluster-dashboard && cd ~/tools/cluster-dashboard
python3 server.py            # legt config.json aus config.default.json an, läuft auf :8095
```

Dann im Browser `http://<rechner>:8095/config` öffnen und eintragen:

- **Rechner** (IP, Name, Rolle) – Engines werden per Port-Scan (`scan_ports`) automatisch gefunden
- **API-Keys** bzw. Key-Dateien der Engines
- **Engines** (Name, Farbe, UI-Pfad je Diensttyp)

Alternativ eine exportierte Konfiguration über **⇧ Import** laden.

### Als Dienst (systemd, Benutzer)

```bash
cp deploy/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cluster-dashboard.service
loginctl enable-linger $USER          # läuft auch ohne Anmeldung
```

### Zugriff von außen (optional)

1. Login anlegen: `python3 server.py --set-password BENUTZER`, Dienst neu starten
2. `cloudflared` nach `bin/` legen ([Releases](https://github.com/cloudflare/cloudflared/releases))
3. `systemctl --user enable --now cluster-dashboard-tunnel.service` – die URL steht in `tunnel.url`
4. Optional Mail bei neuer URL: `tunnel.env` mit `MAIL_CMD=sendmail` und `MAIL_PREFIX="…"` anlegen

Im LAN ist kein Login nötig, über den Tunnel immer.

## Konfiguration exportieren

Unter `/config`:

| Button | Inhalt |
|---|---|
| ⇩ Export | alles außer API-Keys und Login – zum Weitergeben oder als Vorlage |
| ⇩ Export + Geheimnisse | vollständig inkl. Keys und Passwort-Hash – nur im LAN, wie ein Passwort behandeln |
| ⇧ Import | übernimmt eine Export-Datei; fehlende Keys bleiben erhalten, der Login wird nie überschrieben, Miner-Befehle nur aus dem LAN |

SSH-Schlüssel (`keys/`) sind in keinem Export enthalten und müssen separat kopiert werden.

## Miner-Steuerung (optional)

Abschnitt `miners` in `config.json`, je Rechner `kind: systemd` (lokale Unit, braucht einen
sudoers-Eintrag für `systemctl start|stop <unit>`) oder `kind: ssh` (Befehle `start`/`stop`/`status`
per SSH-Key aus `keys/`).

## Grenzen

- Rechner werden parallel abgefragt (bis 64 gleichzeitig), ein Durchlauf dauert so lange wie der
  langsamste Rechner. Das Ergebnis wird kurz zwischengespeichert, mehrere offene Browser kosten
  keine zusätzlichen Abfragen.
- Übersichtlich bis etwa 10–12 Engines; technisch sind ~50 möglich (Datenmenge ~4 KiB pro Engine und Abfrage).

## Lizenz

MIT – siehe [LICENSE](LICENSE).
