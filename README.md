# Ollama Document Assistant auf Proxmox (CPU-only)

Dieses Projekt beschreibt eine lokale Dokument-Automatisierung mit Ollama [https://ollama.com] unter Debian und ist mit dem Copilot entstanden.

## Ziel

Dokumente lokal verarbeiten, um:
- Inhalte zu erkennen (inkl. OCR bei Scan-PDFs)
- Dokumente zu kategorisieren
- sinnvolle Dateinamen zu erzeugen
- sicher umzubenennen (erst Dry-Run, dann Apply)

## Hardware-Profil (mein/dein Setup)

- Host CPU: Intel Core i5-10210U (4C/8T)
- Host RAM: 16 GB

## Modell-Empfehlung (Testreihenfolge)

Teste in dieser Reihenfolge:

1. qwen2.5:3b-instruct
2. qwen2.5:7b-instruct
3. llama3.1:8b-instruct (optional Vergleich)

Empfehlung fuer Dauerbetrieb:
- Standard: qwen2.5:7b-instruct
- Fallback bei Last: qwen2.5:3b-instruct

Hinweis:
- 14B-Modelle sind auf dieser CPU meist zu traege fuer den praktischen Mehrwert in dieser Pipeline.

## Erwartbare Performance

CPU-only auf i5-10210U:
- kurze, textbasierte Dokumente: meist in Sekundenbereich bis niedriger zweistelliger Bereich
- lange OCR-Scans: deutlich langsamer

Wichtig:
- OCR (Tesseract) ist haeufig der groesste Zeitanteil, nicht das LLM selbst.

## Ist Performance

qwen2.5:3b-instruct 
6-7 Minuten pro Dokument 

qwen2.5:7b-instruct
15-20Minuten pro Dokument

## Debian Setup

### 1) Systempakete

```bash
sudo apt update
sudo apt install -y curl python3 python3-venv python3-pip tesseract-ocr tesseract-ocr-deu poppler-utils
```

### 2) Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable ollama
sudo systemctl start ollama
```

### 3) Modelle laden

```bash
ollama pull qwen2.5:3b-instruct
ollama pull qwen2.5:7b-instruct
```

### 3a) Installierte Modelle anzeigen

```bash
ollama list
```

Alternativ per API:

```bash
curl -s http://127.0.0.1:11434/api/tags
```

### 3b) Modelle aktualisieren

Modelle werden durch erneutes Pull aktualisiert:

```bash
ollama pull qwen2.5:3b-instruct
ollama pull qwen2.5:7b-instruct
```

### 4) Python-Umgebung

```bash
git clone https://github.com/maheis/ollama-document-assistant.git
cd ollama-document-assistant
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## Betriebsprofil (sicher)

- Immer zuerst mit --dry-run testen
- Confidence-Schwelle setzen (z. B. 0.75)
- Unterhalb Schwelle in Review-Ordner statt Auto-Rename
- Alle Umbenennungen protokollieren (CSV/JSON)
- Produktivlauf nachts einplanen, um andere VMs nicht zu stoeren

## Dateinamenschema

YYYY-MM-DD_ABSENDER_FIRMA_KATEGORIE_[KUNDENNUMMER]_TITEL.pdf

Beispiel:
- 2026-03-11_stadtwerke_RECHNUNG_123456_abschlag_april.pdf
- 2026-03-11_stadtwerke_RECHNUNG_abschlag_april.pdf (wenn keine Kundennummer gefunden wird)

Regeln:
- Umlaute normalisieren (ae, oe, ue, ss)
- Sonderzeichen entfernen
- Kollisionen mit Suffix aufloesen (_1, _2, ...)

## Cron (optional)

```cron
0 2 * * * /home/USER/doc-ai/.venv/bin/python /home/USER/doc-ai/organize.py --input /srv/docs/inbox --apply >> /var/log/doc-ai.log 2>&1

35       20       *       *       *       python3 ~/ollama-document-assistant/organize.py --input ~/ollama-document-assistant/inbox --dry-run --model qwen2.5:3b-instruct
35       22       *       *       *       python3 ~/ollama-document-assistant/organize.py --input ~/ollama-document-assistant/inbox --dry-run --model qwen2.5:7b-instruct
# *       *       *       *       *       Befehl der ausgeführt werden soll
# -       -       -       -       -
# |       |       |       |       |
# |       |       |       |       +----- Wochentag (0 - 7) (Sonntag ist 0 und 7; oder Namen, siehe unten)
# |       |       |       +------- Monat (1 - 12)
# |       |       +--------- Tag (1 - 31)
# |       +----------- Stunde (0 - 23)
# +------------- Minute (0 - 59; oder Namen, siehe unten)

```

## Nutzung von organize.py

Beispielstruktur:

- `inbox/` enthaelt neue Dokumente
- Script erzeugt bei Bedarf:
  - `_sorted/YYYY/...`
  - `_review/...` (bei niedriger Confidence)

### Dry-Run (empfohlen als erster Lauf)

```bash
python3 organize.py --input ./inbox --dry-run --model qwen2.5:7b-instruct
```

Hinweis zur Modellauswahl:

- Wenn `--model` gesetzt ist, wird genau dieses Modell verwendet.
- Wenn `--model` leer bleibt und genau ein Modell installiert ist, wird dieses automatisch genutzt.
- Bei mehreren installierten Modellen ohne `--model` bricht das Script mit Hinweis ab.

### Echter Lauf (Dateien werden verschoben/umbenannt)

```bash
python3 organize.py --input ./inbox --apply --model qwen2.5:7b-instruct
```

### Wichtige Optionen

```bash
python3 organize.py \
  --input /srv/docs/inbox \
  --apply \
  --model qwen2.5:7b-instruct \
  --ollama-timeout 900 \
  --ollama-retries 2 \
  --ollama-keep-alive 24h \
  --process-nice 5 \
  --max-cpu-threads 2 \
  --ollama-num-thread 2 \
  --sleep-between-files 0.4 \
  --min-confidence 0.75 \
  --max-text-chars 8000 \
  --sorted-dir _sorted \
  --review-dir _review \
  --log-file doc-organize.jsonl
```

Hinweise:

- Standard ohne `--apply` ist Dry-Run
- Nur unterstuetzte Dateitypen werden verarbeitet (`.pdf`, Bilder, `.txt`, `.md`, ...)
- Bei PDF ohne brauchbaren Text greift OCR (wenn Tesseract + pdf2image installiert sind)
- PDF ohne Textlayer werden im `--apply` Lauf als OCR-PDF neu erzeugt und dann im Ziel abgelegt
- Im Log steht dafuer `ocr_pdf_rebuild: true`
- Es werden immer zwei Logs geschrieben (Dry-Run und Apply):
  - JSONL Event-Log: `--log-file` (Standard `organize_log.jsonl`)
  - Konsolen-Spiegel als Textlog: `--run-log-file` (Standard `organize_run.log`)
  - Beide Logs werden immer unter `/tmp` geschrieben und mit Datum gepraefixt, z. B. `/tmp/2026-05-08_organize_log.jsonl`
  - Bei den Optionen `--log-file` und `--run-log-file` wird nur der Dateiname verwendet
- Das LLM bleibt per Default im RAM: `--ollama-keep-alive 24h`
- Optionale Kategorie-Schlagworte aus `category_hints.json` werden verwendet
- Bei unsicheren Faellen (`confidence` niedrig oder `SONSTIGES`) kann ein Keyword-Fallback die Kategorie korrigieren

### Kategorie-Schlagworte (optional, empfohlen)

Datei:

- `category_hints.json` (liegt im Projektordner)

Neue Optionen:

- `--category-hints-file category_hints.json`
- `--keyword-fallback-min-score 2`

Verhalten:

1. Schlagworte werden als Zusatz-Hinweis in den Prompt gegeben.
2. Ist das LLM unsicher oder liefert `SONSTIGES`, wird ein Keyword-Fallback geprueft.
3. Bei eindeutigem Treffer (Score hoch genug) wird die Kategorie gesetzt.

Beispiel:

```bash
python3 organize.py \
  --input ./inbox \
  --dry-run \
  --model qwen2.5:3b-instruct \
  --category-hints-file ./category_hints.json \
  --keyword-fallback-min-score 2
```

### Kundennummer/Referenznummer-Hinweise (optional, empfohlen)

Datei:

- `customer_number_hints.json` (liegt im Projektordner)

Option:

- `--customer-number-hints-file ./customer_number_hints.json`

Verhalten:

1. Wenn das LLM keine Kundennummer liefert, durchsucht das Script den Text nach Labels/Patterns.
2. Treffer werden als `customer_number` uebernommen (z. B. Kundennummer, Vertragsnummer, Konto, IBAN).
3. Ohne Treffer wird kein Platzhalter in den Dateinamen geschrieben.

### Fehlerbild: "[SKIP] ... kein Text extrahiert"

Wenn diese Meldung bei PDF-Dateien auftaucht, ist es meist ein Scan-PDF ohne eingebetteten Text und OCR konnte nicht greifen.

Hinweis: Das Script nutzt jetzt auch einen CLI-Fallback (`pdftoppm` + `tesseract`), falls `pdf2image`/`pytesseract` im Python-Environment fehlen.

Schnellchecks:

```bash
which pdftotext && pdftotext -v
which tesseract && tesseract --list-langs | grep -E 'deu|eng'
python3 -c "import pypdf,pdf2image,pytesseract; print('python deps ok')"
```

Test direkt auf eine betroffene Datei:

```bash
pdftotext -layout "2022-03-31 - Raiffeisenbank AGB Leana.pdf" - | head -n 20
```

Wenn dort nichts kommt, ist OCR erforderlich. Stelle sicher, dass `tesseract-ocr` und `tesseract-ocr-deu` installiert sind.

Wenn im Log `pypdf=missing` / `pdf2image=missing` / `pytesseract=missing` steht, ist meist die falsche oder keine venv aktiv:

```bash
source .venv/bin/activate
pip install -r requirements.txt
python3 -c "import pypdf,pdf2image,pytesseract; print('python deps ok')"
```

### Fehlerbild: "Read timed out (timeout=180)" bei Ollama

Auf CPU-only Systemen kann ein Modellaufruf bei langen Dokumenten laenger dauern als der Standard-Timeout.

Empfohlener Lauf:

```bash
python3 organize.py \
  --input ./inbox \
  --dry-run \
  --model qwen2.5:7b-instruct \
  --ollama-timeout 900 \
  --ollama-retries 2 \
  --max-text-chars 8000
```

Wenn es weiter zu Timeouts kommt:

1. Modell auf `qwen2.5:3b-instruct` wechseln
2. `--max-text-chars` weiter reduzieren (z. B. 5000)
3. Sicherstellen, dass Ollama lokal laeuft: `curl http://127.0.0.1:11434/api/tags`

### CPU-Nutzung begrenzen (Host entlasten)

Das Script unterstuetzt eingebaute CPU-Drosselung:

- `--process-nice 5`: Prozess laeuft mit niedrigerer Prioritaet
- `--max-cpu-threads 4`: begrenzt Threads fuer OCR/BLAS-lastige Teile
- `--ollama-num-thread 4`: begrenzt Threads fuer den Modellaufruf
- `--sleep-between-files 0.4`: kurze Pause zwischen Dokumenten

Empfehlung fuer deinen Host:

```bash
python3 organize.py \
  --input ./inbox \
  --dry-run \
  --model qwen2.5:3b-instruct \
  --ollama-timeout 900 \
  --ollama-retries 2 \
  --max-text-chars 6000 \
  --process-nice 8 \
  --max-cpu-threads 2 \
  --ollama-num-thread 2 \
  --sleep-between-files 0.5
```

Wenn der Host trotzdem noch zu stark ausgelastet ist:

1. `--max-cpu-threads 1`
2. `--ollama-num-thread 1`
3. auf `qwen2.5:3b-instruct` bleiben

### Standard-Defaults (ohne Zusatzflags)

Aktuelle Defaults sind CPU-sicher gesetzt:

- `--ollama-timeout 420`
- `--ollama-timeout 900`
- `--ollama-retries 2`
- `--ollama-keep-alive 24h`
- `--max-text-chars 6000`
- `--max-cpu-threads 4`
- `--ollama-num-thread 4`

Damit sollte auch dieser einfache Aufruf robuster laufen:

```bash
python3 organize.py --input ./inbox --dry-run --model qwen2.5:3b-instruct
```

## Lizenz

MIT, siehe [LICENSE](LICENSE).
