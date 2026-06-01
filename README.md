# Ollama Document Assistant

Lokale Dokument-Automatisierung mit [Ollama](https://ollama.com), OCR und Web-Review.
Dieses Projekt ist in Zusammenarbeit mit dem Copilot entstanden.

Kurz gesagt:

- für Menschen, die eingehende Dokumente lokal vorsortieren und erst nach manueller Prüfung final ablegen wollen
- das Tool extrahiert Text, nutzt ein lokales LLM für Vorschläge und lässt die finale Entscheidung immer in der Weboberfläche
- für den Start reicht in der Regel: installieren, `http://127.0.0.1:8449` öffnen, Datei in die Inbox legen, `Scan starten`, prüfen, deployen

Wichtiges Prinzip:

- `organize.py` erzeugt immer nur Vorschläge (Dry-Run)
- finales Umbenennen passiert ausschließlich per Deploy in der Weboberfläche
- PDFs ohne eingebetteten Text werden bereits im Dry-Run lokal durch eine OCR-PDF ersetzt

## Schnellstart

```bash
curl -fsSL https://raw.githubusercontent.com/maheis/ollama-document-assistant/main/install.sh | bash -s -- --full-setup
```

Danach typischer Ablauf:

1. Weboberfläche öffnen: `http://127.0.0.1:8449`
2. Eine oder mehrere Dateien in die Inbox legen
3. In der Weboberfläche `Scan starten` klicken oder auf den nächsten automatischen Lauf warten
4. Vorschläge prüfen und danach deployen

Zum Passwort:

- die Datei `.review_web_password` enthält einen Passwort-Hash, nicht das Klartext-Passwort
- bei einer Neuinstallation zeigt `install.sh` das erzeugte Passwort einmalig am Ende an
- ein neues Passwort kann später in der Web-UI gesetzt werden
- die Passwortdatei ist nur für das Programm gedacht, nicht zum Ablesen des Passworts

## Alltag

Das Projekt macht im Alltag genau diesen Ablauf:

1. Dateien in die Inbox legen
2. `organize.py` erzeugt immer nur Vorschläge (Dry-Run)
3. die Weboberfläche zeigt offene Vorschläge an
4. du passt bei Bedarf Felder an
5. erst `Deploy` verschiebt oder benennt Dateien final um

Wichtige Punkte:

- PDFs ohne eingebetteten Text werden bereits im Dry-Run lokal durch eine OCR-PDF ersetzt
- manuelle Änderungen an Sender, Kategorie, Kunden-Nr und Titel werden beim Deploy automatisch als Alias gelernt
- deployte Dokumente bleiben sichtbar und sind filterbar

## Dateinamenschema

`YYYY-MM-DD_ABSENDER_KATEGORIE_[KUNDENNUMMER]_TITEL.ext`

Beispiele:

- `2026-03-11_stadtwerke_RECHNUNG_123456_abschlag_april.pdf`
- `2026-03-11_stadtwerke_RECHNUNG_abschlag_april.pdf`

## Konfiguration

Zentrale Defaults liegen in `assistant_config.json`.

Gesteuert werden u. a.:

- Host/Port der Weboberfläche
- Login-Passwortdatei
- Scan-Intervall
- Modell sowie Inbox- und Ausgabepfad für den Dienst
- E-Mail-Benachrichtigungen nach abgeschlossenen Scans mit neuen Prüffällen

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

Für den Dienst `doc_assistant_service.py` sind vor allem diese Werte relevant:

- `service.input`: Inbox-Pfad
- `service.output`: Outbox-Basis für die final sortierten Dateien
- `service.model`: Ollama-Modell
- `service.schedule_mode`: `interval`, `inbox-trigger` oder `daily`
- `service.interval_minutes`: Scan-Intervall in Minuten, immer ab voller Stunde auf festen Slots (`*/N`)
- `service.daily_time`: Uhrzeit für den täglichen Lauf (`HH:MM`, nur bei `daily`)
- `service.inbox_poll_seconds`: Polling-Intervall für Inbox-Trigger (nur bei `inbox-trigger`)

Für Benachrichtigungen gibt es zusätzlich:

- `notifications.email.enabled`: E-Mail-Versand aktivieren/deaktivieren
- `notifications.email.to`: Empfänger, mehrere Adressen kommasepariert
- `notifications.email.from`: Absenderadresse
- `notifications.email.smtp_host`: SMTP-Server
- `notifications.email.smtp_port`: SMTP-Port
- `notifications.email.smtp_username`: optionaler SMTP-Benutzer
- `notifications.email.smtp_password`: optionales SMTP-Passwort
- `notifications.email.smtp_starttls`: STARTTLS verwenden
- `notifications.email.smtp_ssl`: direktes SMTP-SSL verwenden
- `notifications.email.subject_prefix`: Präfix für den Betreff

Eine Mail wird verschickt, wenn ein Scan erfolgreich abgeschlossen wurde und dabei neue Dokumente zur Prüfung entstanden sind. Das gilt sowohl für automatische Läufe des Dienstes als auch für manuell gestartete Scans über die Weboberfläche.

Scheduler-Modi:

1. `interval`: feste Minuten-Slots ab voller Stunde, z. B. bei `15` immer um `:00`, `:15`, `:30`, `:45`
2. `inbox-trigger`: prüft die Inbox dauerhaft und startet bei neuen/geänderten Dateien sofort
3. `daily`: startet einmal pro Tag zur konfigurierten `service.daily_time`

Beispiele:

```json
"service": {
  "schedule_mode": "interval",
  "interval_minutes": 5
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

## Installation

Basis-Setup (venv, Python-Dependencies, Inbox, Passwortdatei, optionale user-systemd Unit):

```bash
bash ./install.sh
```

Standard-Installationspfad:

```text
~/.local/share/ollama-document-assistant
```

Vollsetup für Debian:

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
- `--install-dir <pfad>`: Installationspfad überschreiben
- `--repo-url <url>`: alternatives Git-Repository für Bootstrap-Checkout
- `--repo-ref <ref>`: Branch/Tag/Commit für Bootstrap-Checkout

Hinweise:

- kein `git clone`/Checkout im Skript
- für apt-Schritte sind root/sudo Rechte nötig
- wenn `install.sh` als einzelnes File (z. B. per wget/curl gespeichert) außerhalb eines Repos ausgeführt wird, löscht es sich nach erfolgreicher Installation selbst
- bei erneutem Ausführen von `install.sh` bleibt eine vorhandene Passwortdatei (`.review_web_password`) unverändert

## Update

Wenn du bereits per `install.sh` installiert hast, update so:

```bash
cd ~/.local/share/ollama-document-assistant
systemctl --user stop ollama-document-assistant.service
git pull --ff-only
bash ./install.sh --install-dir ~/.local/share/ollama-document-assistant
systemctl --user start ollama-document-assistant.service
systemctl --user status ollama-document-assistant.service
```

Hinweis:

- `assistant_config.json` bleibt erhalten (wird nicht überschrieben, wenn vorhanden)

Falls im Installationsordner kein `.git` vorhanden ist:

```bash
git clone https://github.com/maheis/ollama-document-assistant.git /tmp/oda-update
bash /tmp/oda-update/install.sh --install-dir ~/.local/share/ollama-document-assistant
systemctl --user restart ollama-document-assistant.service
```

## Weboberfläche

Start (manuell):

```bash
python3 review_web.py --config-file ./assistant_config.json
```

Die Weboberfläche kann:

1. Spaltenweise bearbeiten (Sender, Kategorie, Kunden-Nr, Titel, Datum)
2. Dokument direkt aus der Zeile öffnen
3. global oder pro Zeile speichern
4. global oder pro Zeile deployen
5. Status je Eintrag: `PENDING`, `SAVED`, `MISSING`, `DEPLOYED`
6. Status-Filter: `Alle`, `Offen`, `Pending`, `Saved`, `Missing`, `Deployed`
7. Manuelle Änderungen an Sender, Kategorie, Kunden-Nr und Titel werden beim Deploy automatisch als Alias gelernt
8. Manuellen Dry-Run-Scan direkt im UI starten (`Scan starten`) und bei Bedarf stoppen
9. Logfiles direkt in der Weboberfläche ansehen
10. Anzeige von Systemstatus und noch zu scannenden Dateien

## Sicherheit

Login empfohlen:

```bash
printf '%s\n' 'DEIN_STARKES_PASSWORT' > ./.review_web_password
chmod 600 ./.review_web_password
```

Verhalten:

- mit `--auth-password` oder `--auth-password-file`: Login aktiv (Session-Cookie)
- Remote-Bind ohne Login wird blockiert

## Dienst

Der Dienst `doc_assistant_service.py`:

- startet `review_web.py` dauerhaft
- startet `organize.py` periodisch im Dry-Run
- restartet Web-Prozess bei Absturz

Wann der Dienst laeuft:

- nach `install.sh` standardmäßig als user-systemd Unit `ollama-document-assistant.service`
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
3. In der Web-UI über `/config` (Button `Konfiguration`)

Hinweis zur Web-Konfigurationsseite:

- speichert in `assistant_config.json` (service.input/output/model/schedule_mode/interval_minutes/daily_time/inbox_poll_seconds)
- Web-Passwort kann dort ebenfalls geändert werden (wird in die Passwortdatei geschrieben)
- SMTP-Daten und Empfänger für Prüfungs-Benachrichtigungen können dort ebenfalls gepflegt werden
- Logfiles können dort komplett zurückgesetzt werden
- Update kann dort geprüft und gestartet werden (`Auf Update prüfen`, `Update durchführen`)
- `Update durchfuehren` ist nur aktiv, wenn wirklich ein neuer Stand im Git-Remote verfuegbar ist
- Dienst kann dort direkt neu gestartet werden (`Dienst neu starten`)
- danach Dienst neu starten, damit die neuen Werte aktiv werden:

```bash
systemctl --user restart ollama-document-assistant.service
```

## Modell

Es wird durchgängig `qwen2.5:7b-instruct` verwendet.

- Default in `assistant_config.json`
- Default in `organize.py`
- Default für `install.sh --pull-models`

## Wartung und Troubleshooting

### Wichtige organize.py Optionen

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
- PDFs ohne OCR-Text werden trotzdem bereits während des Dry-Runs lokal in eine durchsuchbare OCR-PDF umgewandelt
- Logs landen persistent in `./logs` (mit Datums-Prefix), z. B. `logs/2026-05-08_organize_log.jsonl`
- bei wenig PDF-Text greift OCR

Log-Dateien aufraeumen:

```bash
# Alle Organize-Logs anzeigen
ls -lah ./logs/

# Logs älter als 30 Tage löschen
find ./logs -type f -name '*_organize_*.log*' -mtime +30 -delete
find ./logs -type f -name '*_organize_*.jsonl' -mtime +30 -delete

# Alle Logs komplett löschen (nur wenn gewollt)
rm -f ./logs/*_organize_*.log* ./logs/*_organize_*.jsonl
```

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

Wenn nötig:

1. `--max-text-chars` reduzieren
2. Ollama API prüfen: `curl http://127.0.0.1:11434/api/tags`

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

## Deinstallation

Mit Skript (empfohlen):

```bash
bash ~/.local/share/ollama-document-assistant/uninstall.sh
```

Wenn du im geklonten Repository arbeitest:

```bash
bash ./uninstall.sh
```

Optional (zusätzlich Modell und Ollama entfernen):

```bash
bash ~/.local/share/ollama-document-assistant/uninstall.sh --remove-ollama --remove-models
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

## Lizenz

MIT, siehe [LICENSE](LICENSE).
