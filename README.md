# Ollama Document Assistant auf Proxmox (CPU-only)

Dieses Projekt beschreibt eine lokale Dokument-Automatisierung mit Ollama [https://ollama.com] unter Debian.

## Ziel

Dokumente lokal verarbeiten, um:
- Inhalte zu erkennen (inkl. OCR bei Scan-PDFs)
- Dokumente zu kategorisieren
- sinnvolle Dateinamen zu erzeugen
- sicher umzubenennen (erst Dry-Run, dann Apply)

## Hardware-Profil (dein Setup)

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

## Dateinamenschema (Vorschlag)

YYYY-MM-DD_KATEGORIE_TITEL.pdf

Beispiel:
- 2026-03-11_RECHNUNG_stadtwerke_abschlag.pdf

Regeln:
- Umlaute normalisieren (ae, oe, ue, ss)
- Sonderzeichen entfernen
- Kollisionen mit Suffix aufloesen (_1, _2, ...)

## Schneller Testplan (30-60 Minuten)

1. 20 echte Dokumente in eine Test-Inbox legen.
2. Pipeline mit 3B im Dry-Run ausfuehren.
3. Korrektheit von Kategorie und Titel bewerten.
4. Gleiches Set mit 7B testen.
5. Modell mit bester Balance aus Qualitaet und Laufzeit waehlen.

Abnahmekriterium:
- mindestens 85-90 Prozent korrekte Kategorisierung
- keine riskanten Fehlumbenennungen dank Review-Pfad

## Cron (optional)

```cron
0 2 * * * /home/USER/doc-ai/.venv/bin/python /home/USER/doc-ai/organize.py --input /srv/docs/inbox --apply >> /var/log/doc-ai.log 2>&1
```

## Nutzung von organize.py

Beispielstruktur:

- `inbox/` enthaelt neue Dokumente
- Script erzeugt bei Bedarf:
  - `_sorted/KATEGORIE/...`
  - `_review/...` (bei niedriger Confidence)

### Dry-Run (empfohlen als erster Lauf)

```bash
python3 organize.py --input ./inbox --dry-run --model qwen2.5:7b-instruct
```

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
  --ollama-timeout 420 \
  --ollama-retries 2 \
  --process-nice 5 \
  --max-cpu-threads 2 \
  --ollama-num-thread 2 \
  --sleep-between-files 0.4 \
  --min-confidence 0.75 \
  --max-text-chars 8000 \
  --sorted-dir _sorted \
  --review-dir _review \
  --log-file /var/log/doc-organize.jsonl
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
  --ollama-timeout 420 \
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
  --ollama-timeout 420 \
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
- `--ollama-retries 2`
- `--max-text-chars 6000`
- `--max-cpu-threads 4`
- `--ollama-num-thread 4`

Damit sollte auch dieser einfache Aufruf robuster laufen:

```bash
python3 organize.py --input ./inbox --dry-run --model qwen2.5:3b-instruct
```

## Naechste Schritte

2. qwen2.5:3b und qwen2.5:7b gegeneinander testen.
3. CPU-Last mit `--max-cpu-threads 4 --ollama-num-thread 4` einpegeln.
4. Wenn OPNsense/Home Assistant Lastspitzen sehen: auf `--max-cpu-threads 2 --ollama-num-thread 2` reduzieren.
