# Ollama Document Assistant (Debian, CPU-only)

Lokale Dokument-Automatisierung mit [Ollama](https://ollama.com), OCR und Web-Review.
Dieses Projekt ist in Zusammenarbeit mit dem Copilot entstanden.

Wichtiges Prinzip:

- `organize.py` erzeugt immer nur Vorschlaege (Dry-Run)
- finales Umbenennen passiert ausschliesslich per Deploy in der Weboberflaeche

## Quickstart (Debian)

```bash
curl -fsSL https://raw.githubusercontent.com/maheis/ollama-document-assistant/main/install.sh | bash -s -- --full-setup
```

## Passwort auslesen

```bash
head -n 1 ~/.local/share/ollama-document-assistant/.review_web_password
```

## Was das Projekt macht

- Text aus PDF/TXT/MD/Bildern extrahieren (inkl. OCR-Fallback)
- Dokumente mit lokalem LLM klassifizieren
- saubere Zielnamen vorschlagen
- Vorschlaege im Browser pruefen/anpassen
- erst nach Freigabe deployen

## Workflow

1) Inbox fuellen (`./inbox`)
2) Dry-Run erzeugt Vorschlaege
3) Vorschlaege in der Web-UI pruefen
4) einzelne oder alle Eintraege deployen

## Dateinamenschema

`YYYY-MM-DD_ABSENDER_KATEGORIE_[KUNDENNUMMER]_TITEL.ext`

Beispiele:

- `2026-03-11_stadtwerke_RECHNUNG_123456_abschlag_april.pdf`
- `2026-03-11_stadtwerke_RECHNUNG_abschlag_april.pdf`

## Zentrale Konfiguration

Zentrale Defaults liegen in `assistant_config.json`.

Gesteuert werden u. a.:

- Host/Port der Weboberflaeche
- Login-Passwortdatei
- Scan-Intervall
- Modell sowie Inbox- und Ausgabepfad fuer den Dienst

Beispiel (service-Block):

```json
"service": {
  "input": "./inbox",
  "output": "./output",
  "model": "qwen2.5:7b-instruct"
}
```

Start mit Config:

```bash
python3 review_web.py --config-file ./assistant_config.json
python3 doc_assistant_service.py --config-file ./assistant_config.json
```

Prioritaet:

- CLI-Flag > `assistant_config.json` > Script-Default

Fuer den Dienst `doc_assistant_service.py` sind vor allem diese Werte relevant:

- `service.input`: Inbox-Pfad
- `service.output`: Outbox-Basis (Sortierung direkt dort, Review in `_review`)
- `service.model`: Ollama-Modell
- `service.schedule_mode`: `interval`, `inbox-trigger` oder `daily`
- `service.interval_seconds`: Scan-Intervall
- `service.daily_time`: Uhrzeit fuer den taeglichen Lauf (`HH:MM`, nur bei `daily`)
- `service.inbox_poll_seconds`: Polling-Intervall fuer Inbox-Trigger (nur bei `inbox-trigger`)

Scheduler-Modi:

1. `interval` (wie bisher): alle `service.interval_seconds` Sekunden
2. `inbox-trigger`: prueft die Inbox dauerhaft und startet bei neuen/geaenderten Dateien sofort
3. `daily`: startet einmal pro Tag zur konfigurierten `service.daily_time`

Beispiele:

```json
"service": {
  "schedule_mode": "interval",
  "interval_seconds": 300
}
```

```json
"service": {
  "schedule_mode": "inbox-trigger",
  "inbox_poll_seconds": 2
}
```

```json
"service": {
  "schedule_mode": "daily",
  "daily_time": "06:30"
}
```

Validierung:

- `review_web.py` und `doc_assistant_service.py` validieren die Config beim Start
- bei JSON- oder Schema-Fehlern: klare Meldung und Exit-Code `2`

## Installation per Script

Basis-Setup (venv, Python-Dependencies, Inbox, Passwortdatei, optionale user-systemd Unit):

```bash
bash ./install.sh
```

Standard-Installationspfad:

```text
~/.local/share/ollama-document-assistant
```

Vollsetup fuer Debian:

```bash
bash ./install.sh --full-setup
```

Optionen:

- `--no-systemd`: keine user-systemd Unit installieren
- `--no-start`: Unit installieren, aber nicht starten
- `--full-setup`: aktiviert `--install-system-deps`, `--install-ollama`, `--pull-models`
- `--install-system-deps`: apt-Pakete installieren (Debian/Ubuntu)
- `--install-ollama`: Ollama installieren und starten
- `--pull-models`: Modell(e) mit `ollama pull` laden
- `--model <name>`: Modell explizit setzen (mehrere per Komma)
- `--install-dir <pfad>`: Installationspfad ueberschreiben
- `--repo-url <url>`: alternatives Git-Repository fuer Bootstrap-Checkout
- `--repo-ref <ref>`: Branch/Tag/Commit fuer Bootstrap-Checkout

Hinweise:

- kein `git clone`/Checkout im Skript
- fuer apt-Schritte sind root/sudo Rechte noetig
- wenn `install.sh` als einzelnes File (z. B. per wget/curl gespeichert) ausserhalb eines Repos ausgefuehrt wird, loescht es sich nach erfolgreicher Installation selbst
- bei erneutem Ausfuehren von `install.sh` bleibt eine vorhandene Passwortdatei (`.review_web_password`) unveraendert

## Deinstallation

Mit Skript (empfohlen):

```bash
bash ~/.local/share/ollama-document-assistant/uninstall.sh
```

Wenn du im geklonten Repository arbeitest:

```bash
bash ./uninstall.sh
```

Optional (zusaetzlich Modell und Ollama entfernen):

```bash
bash ~/.local/share/ollama-document-assistant/uninstall.sh --remove-models --remove-ollama
```

Wenn bei der Installation ein eigener `--install-dir` verwendet wurde, rufe das Skript aus genau diesem Verzeichnis auf.

Manuell deinstallieren:

```bash
# 1) Service stoppen/entfernen
systemctl --user stop ollama-document-assistant.service
systemctl --user disable ollama-document-assistant.service
rm -f ~/.config/systemd/user/ollama-document-assistant.service
systemctl --user daemon-reload
systemctl --user reset-failed

# 2) Installationsverzeichnis entfernen
rm -rf ~/.local/share/ollama-document-assistant

# 3) Optional: Modell entfernen
ollama rm qwen2.5:7b-instruct

# 4) Optional: Ollama selbst entfernen (Debian/Ubuntu)
sudo systemctl stop ollama
sudo systemctl disable ollama
sudo apt-get remove -y ollama
sudo apt-get autoremove -y
```

## Update einer bestehenden Installation

Wenn du bereits per `install.sh` installiert hast, update so:

```bash
cd ~/.local/share/ollama-document-assistant
git pull --ff-only
bash ./install.sh --install-dir ~/.local/share/ollama-document-assistant
systemctl --user restart ollama-document-assistant.service
systemctl --user status ollama-document-assistant.service
```

Hinweis:

- `assistant_config.json` bleibt erhalten (wird nicht ueberschrieben, wenn vorhanden)

Falls im Installationsordner kein `.git` vorhanden ist:

```bash
git clone https://github.com/maheis/ollama-document-assistant.git /tmp/oda-update
bash /tmp/oda-update/install.sh --install-dir ~/.local/share/ollama-document-assistant
systemctl --user restart ollama-document-assistant.service
```

## Web-Review und Deploy

Start (manuell):

```bash
python3 review_web.py --config-file ./assistant_config.json
```

UI-Funktionen:

1. Spaltenweise bearbeiten (Sender, Kategorie, Kunden-Nr, Titel, Datum)
2. Dokument direkt aus der Zeile oeffnen
3. global oder pro Zeile speichern
4. global oder pro Zeile deployen
5. Status je Eintrag: `PENDING`, `SAVED`, `MISSING`, `DEPLOYED`
6. Status-Filter: `Alle`, `Offen`, `Pending`, `Saved`, `Missing`, `Deployed`
7. Lern-Haken pro Feld (Alias-Lernen)
8. Manuellen Dry-Run-Scan direkt im UI starten (`Scan jetzt starten`)

Hinweis:

- deployte Dokumente bleiben sichtbar und sind filterbar

## Sicherheit

Login empfohlen:

```bash
printf '%s\n' 'DEIN_STARKES_PASSWORT' > ./.review_web_password
chmod 600 ./.review_web_password
```

Verhalten:

- mit `--auth-password` oder `--auth-password-file`: Login aktiv (Session-Cookie)
- Remote-Bind ohne Login wird blockiert

## Dienstbetrieb (auto scan + web host)

Der Dienst `doc_assistant_service.py`:

- startet `review_web.py` dauerhaft
- startet `organize.py` periodisch im Dry-Run
- restartet Web-Prozess bei Absturz

Wann der Dienst laeuft:

- nach `install.sh` standardmaessig als user-systemd Unit `ollama-document-assistant.service`
- beim Login des Users (WantedBy `default.target`)
- dauerhaft auch ohne Login nur wenn User-Lingering aktiv ist (`loginctl enable-linger <user>`)

Manuell starten:

```bash
python3 doc_assistant_service.py --config-file ./assistant_config.json
```

User-systemd Unit:

- Datei: `systemd/ollama-document-assistant.service`

Installation:

```bash
mkdir -p ~/.config/systemd/user
cp ./systemd/ollama-document-assistant.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ollama-document-assistant.service
systemctl --user status ollama-document-assistant.service
```

Pfade konfigurieren:

1. In `assistant_config.json` unter `service.input` und `service.output`
2. Optional per CLI beim Start von `doc_assistant_service.py` (`--input`, `--output`)
3. In der Web-UI ueber `/config` (Button `Konfiguration`)

Hinweis zur Web-Konfigurationsseite:

- speichert in `assistant_config.json` (service.input/output/model/schedule_mode/interval_seconds/daily_time/inbox_poll_seconds)
- Web-Passwort kann dort ebenfalls geaendert werden (wird in die Passwortdatei geschrieben)
- Update kann dort geprueft und gestartet werden (`Auf Update pruefen`, `Update durchfuehren`)
- `Update durchfuehren` ist nur aktiv, wenn wirklich ein neuer Stand im Git-Remote verfuegbar ist
- Dienst kann dort direkt neu gestartet werden (`Dienst neu starten`)
- danach Dienst neu starten, damit die neuen Werte aktiv werden:

```bash
systemctl --user restart ollama-document-assistant.service
```

## Modell-Standard

Es wird durchgaengig `qwen2.5:7b-instruct` verwendet.

- Default in `assistant_config.json`
- Default in `organize.py`
- Default fuer `install.sh --pull-models`

## Wichtige organize.py Optionen

```bash
python3 organize.py \
  --input ./inbox \
  --dry-run \
  --model qwen2.5:7b-instruct \
  --min-confidence 0.85 \
  --max-text-chars 8000 \
  --ollama-timeout 1800 \
  --ollama-retries 0 \
  --field-aliases-file field_aliases.json
```

Wichtige Hinweise:

- `--apply` ist deaktiviert
- Logs landen persistent in `./logs` (mit Datums-Prefix), z. B. `logs/2026-05-08_organize_log.jsonl`
- bei wenig PDF-Text greift OCR

Log-Dateien aufraeumen:

```bash
# Alle Organize-Logs anzeigen
ls -lah ./logs/

# Logs aelter als 30 Tage loeschen
find ./logs -type f -name '*_organize_*.log*' -mtime +30 -delete
find ./logs -type f -name '*_organize_*.jsonl' -mtime +30 -delete

# Alle Logs komplett loeschen (nur wenn gewollt)
rm -f ./logs/*_organize_*.log* ./logs/*_organize_*.jsonl
```

## Troubleshooting

### Kein Text extrahiert

```bash
which pdftotext && pdftotext -v
which tesseract && tesseract --list-langs | grep -E 'deu|eng'
python3 -c "import pypdf,pdf2image,pytesseract; print('python deps ok')"
```

### Ollama Timeout

```bash
python3 organize.py --input ./inbox --dry-run --model qwen2.5:7b-instruct --ollama-timeout 1800 --ollama-retries 0
```

Wenn noetig:

1. `--max-text-chars` reduzieren
2. Ollama API pruefen: `curl http://127.0.0.1:11434/api/tags`

### CPU drosseln

```bash
python3 organize.py \
  --input ./inbox \
  --dry-run \
  --model qwen2.5:7b-instruct \
  --process-nice 8 \
  --max-cpu-threads 2 \
  --ollama-num-thread 2 \
  --sleep-between-files 0.5
```

## Lizenz

MIT, siehe [LICENSE](LICENSE).
