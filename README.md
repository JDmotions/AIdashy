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

## Sprachen

Oberfläche, Konfiguration, Login und Server-Meldungen sind übersetzt (Deutsch, Englisch).
Umschalter `🌐` oben; die Wahl wird im Browser gespeichert, `?lang=en` erzwingt eine Sprache.
Ohne Wahl gilt die Browsersprache, sonst Englisch.

Texte liegen in `i18n/<code>.json` (flach, `"schluessel": "Text mit {platzhalter}"`).
Neue Sprache: `i18n/en.json` kopieren, z. B. nach `i18n/fr.json`, `_meta.name`/`_meta.locale`
anpassen und übersetzen – sie erscheint automatisch im Umschalter. Fehlende Schlüssel fallen auf Englisch zurück.

## Konfiguration exportieren

Unter `/config`:

| Button | Inhalt |
|---|---|
| ⇩ Export | alles außer API-Keys und Login – zum Weitergeben oder als Vorlage |
| ⇩ Export + Geheimnisse | vollständig inkl. Keys und Passwort-Hash – nur im LAN, wie ein Passwort behandeln |
| ⇧ Import | übernimmt eine Export-Datei; fehlende Keys bleiben erhalten, der Login wird nie überschrieben, Miner-Befehle nur aus dem LAN |

SSH-Schlüssel (`keys/`) sind in keinem Export enthalten und müssen separat kopiert werden.

## Miner-Steuerung (optional)

Das Dashboard kann pro Rechner einen GPU-Miner anzeigen (Hashrate, Shares, Temperatur) und per
Schalter starten/stoppen. Vorgesehen ist SRBMiner-MULTI mit eingeschalteter API
(`--api-enable --api-port 21550`); andere Miner funktionieren, solange sich Start, Stopp und Status
per Befehl abbilden lassen.

Die Miner stehen im Abschnitt `miners` der `config.json` (nicht über die Weboberfläche, weil
die Einträge Befehle ausführen). Schlüssel ist die IP aus `machines`.

### Linux-Rechner, auf dem das Dashboard läuft (`kind: systemd`)

```json
"miners": {
  "192.168.0.10": { "kind": "systemd", "unit": "miner.service", "api": "http://127.0.0.1:21550/" }
}
```

1. Miner als systemd-Unit einrichten: Vorlage `deploy/miner/linux/miner.service.example`.
   Als normaler Benutzer (`User=`) laufen lassen – dann kann ein Proxy wie miner-gate ihn per
   SIGSTOP pausieren, und das Dashboard zeigt „pausiert“.
2. Dashboard-Benutzer darf genau diese Unit schalten: `deploy/miner/linux/sudoers.example`
   nach `/etc/sudoers.d/` (mit `visudo -f`). Das Dashboard ruft `sudo -n /usr/bin/systemctl start|stop <unit>`.
3. Status kommt aus der Miner-API (`api`) und `systemctl is-active`.

### Entfernte Rechner per SSH (`kind: ssh`, Windows oder Linux)

```json
"miners": {
  "192.168.0.20": {
    "kind": "ssh", "user": "miner", "key": "keys/miner_ed25519",
    "start":  "del /q \"C:\\ProgramData\\miner\\paused.flag\" 2>nul & schtasks /run /tn MinerStart",
    "stop":   "type nul > \"C:\\ProgramData\\miner\\paused.flag\" & taskkill /im SRBMiner-MULTI.exe /f",
    "status": "tasklist /FI \"IMAGENAME eq SRBMiner-MULTI.exe\" /NH & curl.exe -s -m 3 http://127.0.0.1:21550/",
    "process": "SRBMiner-MULTI.exe"
  }
}
```

- **SSH-Zugang:** Schlüsselpaar erzeugen (`ssh-keygen -t ed25519 -f keys/miner_ed25519 -N ""`),
  den öffentlichen Teil auf dem Zielrechner eintragen. Windows: OpenSSH-Server aktivieren
  (Einstellungen → Optionale Features), Schlüssel in `C:\Users\<user>\.ssh\authorized_keys`, bei
  Administratoren stattdessen in `C:\ProgramData\ssh\administrators_authorized_keys`.
- **Befehle** laufen unter Windows in `cmd.exe`. `status` muss den Prozessnamen (`process`)
  ausgeben, wenn der Miner läuft; folgt danach JSON der Miner-API, zeigt das Dashboard Hashrate und Shares.
- **Start über eine geplante Aufgabe:** GPU-Miner müssen in der angemeldeten Benutzersitzung
  laufen, nicht in der SSH-Sitzung. Deshalb startet `start` eine Aufgabe (`schtasks /run`), die
  „nur ausführen, wenn der Benutzer angemeldet ist“ eingestellt ist. Anlegen mit
  `deploy/miner/windows/install-miner-tasks.ps1` (siehe unten).
- **Pausen-Datei:** `stop` legt `paused.flag` an, `start` löscht sie. Daran erkennt der Watchdog,
  dass der Miner absichtlich aus ist (Dashboard-Schalter oder miner-gate), und startet ihn nicht neu.

`"enabled": false` und `"note": "…"` blenden einen Miner aus bzw. zeigen einen Hinweis statt des Schalters.

### Watchdogs

Ein Watchdog startet den Miner neu, wenn er abstürzt oder hängt (Prozess läuft, aber API stumm
oder Hashrate 0). Er lässt den Miner in Ruhe, wenn er absichtlich gestoppt oder pausiert ist.

**Linux** (`deploy/miner/linux/`):

```bash
sudo install -m 755 miner-watchdog.sh /usr/local/sbin/
sudo cp miner-watchdog.service miner-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now miner-watchdog.timer
journalctl -t miner-watchdog          # was er getan hat
```

- Abstürze fängt schon `Restart=on-failure` der Miner-Unit ab; der Timer prüft alle 2 min die API.
- Neustart nach 3 schlechten Prüfungen in Folge, nicht in den ersten 180 s nach dem Start.
- Keine Aktion, wenn die Unit gestoppt ist, der Prozess pausiert ist (SIGSTOP) oder `/run/miner.paused` existiert.
- Einstellbar über `Environment=` in `miner-watchdog.service`: `UNIT`, `API`, `GRACE`, `MAX_BAD`, `PAUSE_FLAG`.

**Windows** (`deploy/miner/windows/`, PowerShell als Administrator, als der Benutzer, unter dem der Miner laufen soll):

```powershell
powershell -ExecutionPolicy Bypass -File install-miner-tasks.ps1 `
  -MinerExe "D:\miner\SRBMiner-MULTI.exe" `
  -MinerArgs "--algorithm <algo> --pool <pool:port> --wallet <wallet> --api-enable --api-port 21550"
```

- Legt `MinerStart` an (bei Anmeldung, in der Benutzersitzung, Neustart bei Fehler, nie doppelt)
  und `MinerWatchdog` (alle 2 min, unsichtbar).
- Statt der exe kann `-MinerExe` auch ein eigenes Start-Programm oder eine `.bat` sein.
- Watchdog: startet `MinerStart`, wenn der Prozess fehlt; beendet und startet neu nach 3
  Prüfungen ohne Hashrate. Nichts, solange `C:\ProgramData\miner\paused.flag` existiert.
  Protokoll: `C:\ProgramData\miner\watchdog.log`.
- Damit der Miner nach einem Neustart des PCs ohne Eingriff läuft, braucht Windows eine
  automatische Anmeldung (z. B. Sysinternals Autologon) – sonst startet `MinerStart` erst bei der Anmeldung.

> Die Windows-Skripte sind nicht auf einem Windows-Rechner getestet worden; vor dem
> Produktiveinsatz einmal von Hand prüfen (`schtasks /query /tn MinerWatchdog`, Log ansehen).

## Grenzen

- Rechner werden parallel abgefragt (bis 64 gleichzeitig), ein Durchlauf dauert so lange wie der
  langsamste Rechner. Das Ergebnis wird kurz zwischengespeichert, mehrere offene Browser kosten
  keine zusätzlichen Abfragen.
- Übersichtlich bis etwa 10–12 Engines; technisch sind ~50 möglich (Datenmenge ~4 KiB pro Engine und Abfrage).

## Lizenz

MIT – siehe [LICENSE](LICENSE).
