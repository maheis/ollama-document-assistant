# Ollama Document Assistant auf Proxmox (CPU-only)

Dieses Projekt beschreibt eine lokale Dokument-Automatisierung mit Ollama unter Debian in einer Proxmox-VM.

## Ziel

Dokumente lokal verarbeiten, um:
- Inhalte zu erkennen (inkl. OCR bei Scan-PDFs)
- Dokumente zu kategorisieren
- sinnvolle Dateinamen zu erzeugen
- sicher umzubenennen (erst Dry-Run, dann Apply)

## Hardware-Profil (dein Setup)

- Host CPU: Intel Core i5-10210U (4C/8T)
- Host RAM: 32 GB
- GPU: keine dedizierte GPU
- VMs:
  - Home Assistant: 4 GB
  - OPNsense: 4 GB
  - Debian (Ollama + Pipeline): aktuell 6 GB, empfohlen 16 GB

## Machbarkeit

Ja, das Setup ist fuer den Anwendungsfall geeignet.

- CPU-only reicht fuer Batch-Verarbeitung von Dokumenten.
- Interaktiv ist es langsamer als mit GPU, fuer Kategorisierung und Benennung aber gut nutzbar.
- 16 GB RAM in der Debian-VM sind ein sinnvoller Zielwert.

## Proxmox Zielkonfiguration (konkret)

Fuer die Debian-VM:
- RAM: 16 GB
- vCPU: 4 (Start), bei Bedarf auf 6 erhoehen
- CPU Type: host
- Ballooning:
  - stabilster Betrieb: aus
  - falls aktiv: Minimum 8-10 GB setzen
- Disk: SSD/NVMe, mindestens 40 GB (bei vielen Dokumenten eher 80+ GB)
- SCSI Controller: VirtIO SCSI

Warum:
- Genug Reserve fuer OCR + LLM im selben Lauf
- Bei 4 GB + 4 GB + 16 GB bleiben auf dem Host ca. 8 GB fuer Proxmox und Cache

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
  --min-confidence 0.75 \
  --sorted-dir _sorted \
  --review-dir _review \
  --log-file /var/log/doc-organize.jsonl
```

Hinweise:

- Standard ohne `--apply` ist Dry-Run
- Nur unterstuetzte Dateitypen werden verarbeitet (`.pdf`, Bilder, `.txt`, `.md`, ...)
- Bei PDF ohne brauchbaren Text greift OCR (wenn Tesseract + pdf2image installiert sind)

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

## Naechste Schritte

1. Debian-VM auf 16 GB RAM setzen und mit 4 vCPU starten.
2. qwen2.5:3b und qwen2.5:7b gegeneinander testen.
3. Danach organize.py fertigstellen (OCR + LLM + Dry-Run/Apply + Logging).
