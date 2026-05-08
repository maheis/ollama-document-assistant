#!/usr/bin/env python3
"""Organize documents with local Ollama classification.

Features:
- Text extraction from PDF/TXT/MD and OCR fallback for scans
- Image OCR for common image types
- Local Ollama JSON classification (category/title/date/confidence)
- Safe dry-run/apply mode
- Move low-confidence files to review folder
- Rename with collision handling
"""

from __future__ import annotations

import argparse
import json
import os
import time
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

try:
    from pypdf import PdfReader, PdfWriter
except Exception:
    PdfReader = None
    PdfWriter = None

try:
    from pdf2image import convert_from_path
except Exception:
    convert_from_path = None

try:
    import pytesseract
except Exception:
    pytesseract = None


SUPPORTED_TEXT_EXT = {".txt", ".md", ".csv", ".log", ".json"}
SUPPORTED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
SUPPORTED_DOC_EXT = {".pdf"} | SUPPORTED_TEXT_EXT | SUPPORTED_IMAGE_EXT
DEFAULT_CATEGORIES = [
    "RECHNUNG",
    "VERTRAG",
    "VERSICHERUNG",
    "BANK",
    "STEUER",
    "GESUNDHEIT",
    "KINDERGARTEN",
    "SCHULE",
    "ELTERNGELD",
    "KINDERGELD",
    "GEHALT",
    "VEREIN",
    "AUTO",
    "URLAUB",
    "HAUSNEBENKOSTEN",
    "SONSTIGES",
]


@dataclass
class Classification:
    sender: str
    category: str
    customer_number: Optional[str]
    title: str
    date: Optional[str]
    confidence: float


@dataclass
class ExtractionResult:
    text: str
    has_native_pdf_text: bool
    used_ocr: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Organize documents with a local Ollama model")
    parser.add_argument("--input", required=True, help="Input folder with documents")
    parser.add_argument("--model", default="", help="Ollama model name (empty = auto if exactly one model installed)")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434", help="Ollama base URL")
    parser.add_argument("--ollama-timeout", type=int, default=900, help="Ollama request timeout in seconds")
    parser.add_argument("--ollama-retries", type=int, default=2, help="Retries on timeout/error")
    parser.add_argument("--ollama-retry-backoff", type=float, default=1.5, help="Backoff factor between retries")
    parser.add_argument("--ollama-keep-alive", default="24h", help="Keep model loaded in RAM (e.g. 24h, 30m, -1)")
    parser.add_argument("--categories", nargs="+", default=DEFAULT_CATEGORIES, help="Allowed categories")
    parser.add_argument("--lang", default="deu", help="OCR language for Tesseract")
    parser.add_argument("--min-confidence", type=float, default=0.75, help="Confidence threshold for auto-sort")
    parser.add_argument("--max-text-chars", type=int, default=6000, help="Max chars sent to the model")
    parser.add_argument("--sorted-dir", default="_sorted", help="Folder for categorized files (relative to input)")
    parser.add_argument("--review-dir", default="_review", help="Folder for low-confidence files (relative to input)")
    parser.add_argument("--log-file", default="organize_log.jsonl", help="Path to JSONL log")
    parser.add_argument("--run-log-file", default="organize_run.log", help="Path to plain-text run log (mirrors console output)")
    parser.add_argument("--category-hints-file", default="category_hints.json", help="JSON file with keywords per category")
    parser.add_argument("--keyword-fallback-min-score", type=int, default=2, help="Minimum keyword matches to apply keyword fallback")
    parser.add_argument("--customer-number-hints-file", default="customer_number_hints.json", help="JSON file with labels/patterns for customer/reference number extraction")
    parser.add_argument("--ocr-max-pages", type=int, default=3, help="Max pages to OCR when PDF has little text")
    parser.add_argument("--process-nice", type=int, default=5, help="Increase niceness to lower CPU priority")
    parser.add_argument("--max-cpu-threads", type=int, default=4, help="Limit CPU threads for OCR/BLAS libs (0 disables)")
    parser.add_argument("--ollama-num-thread", type=int, default=4, help="Limit Ollama model threads (0 uses model default)")
    parser.add_argument("--sleep-between-files", type=float, default=0.4, help="Pause between files in seconds")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Perform real file moves/renames")
    mode.add_argument("--dry-run", action="store_true", help="Preview actions without changing files")

    return parser.parse_args()


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_DOC_EXT


def strip_to_ascii(text: str) -> str:
    text = text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    text = text.replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
    text = text.replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return text


def slugify(text: str, uppercase: bool = False) -> str:
    text = strip_to_ascii(text)
    text = text.strip()
    text = re.sub(r"[^A-Za-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        text = "unbekannt"
    return text.upper() if uppercase else text.lower()


def ensure_date(date_text: Optional[str]) -> str:
    if not date_text:
        return datetime.now().strftime("%Y-%m-%d")
    date_text = date_text.strip()

    patterns = [
        (r"^(\d{4})-(\d{2})-(\d{2})$", "%Y-%m-%d"),
        (r"^(\d{2})\.(\d{2})\.(\d{4})$", "%d.%m.%Y"),
        (r"^(\d{2})/(\d{2})/(\d{4})$", "%d/%m/%Y"),
    ]

    for pattern, fmt in patterns:
        if re.match(pattern, date_text):
            try:
                return datetime.strptime(date_text, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass

    return datetime.now().strftime("%Y-%m-%d")


def parse_confidence(value: object) -> float:
    try:
        conf = float(value)
    except Exception:
        return 0.0
    return max(0.0, min(1.0, conf))


def unique_path(target: Path) -> Path:
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    i = 1
    while True:
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def extract_text(path: Path, ocr_lang: str, ocr_max_pages: int) -> ExtractionResult:
    ext = path.suffix.lower()

    if ext in SUPPORTED_TEXT_EXT:
        return ExtractionResult(
            text=path.read_text(encoding="utf-8", errors="ignore"),
            has_native_pdf_text=True,
            used_ocr=False,
        )

    if ext in SUPPORTED_IMAGE_EXT:
        if pytesseract is None:
            return ExtractionResult(text="", has_native_pdf_text=True, used_ocr=False)
        try:
            from PIL import Image

            with Image.open(path) as img:
                return ExtractionResult(
                    text=pytesseract.image_to_string(img, lang=ocr_lang),
                    has_native_pdf_text=True,
                    used_ocr=True,
                )
        except Exception:
            return ExtractionResult(text="", has_native_pdf_text=True, used_ocr=False)

    if ext == ".pdf":
        chunks: list[str] = []
        if PdfReader is not None:
            try:
                reader = PdfReader(str(path))
                for page in reader.pages:
                    chunks.append(page.extract_text() or "")
            except Exception:
                pass

        native_text = "\n".join(chunks).strip()
        combined = native_text
        used_ocr = False

        # Fallback to pdftotext for PDFs that pypdf cannot extract reliably.
        if len(combined) < 400 and shutil.which("pdftotext"):
            try:
                proc = subprocess.run(
                    ["pdftotext", "-layout", str(path), "-"],
                    capture_output=True,
                    text=True,
                    timeout=90,
                    check=False,
                )
                pdftext = (proc.stdout or "").strip()
                if len(pdftext) > len(combined):
                    combined = pdftext
                    native_text = pdftext
            except Exception:
                pass

        # OCR fallback for scanned PDFs or tiny extraction results.
        if len(combined) < 400 and convert_from_path is not None and pytesseract is not None:
            try:
                images = convert_from_path(str(path), first_page=1, last_page=max(1, ocr_max_pages))
                ocr_chunks = [pytesseract.image_to_string(img, lang=ocr_lang) for img in images]
                ocr_text = "\n".join(ocr_chunks).strip()
                if len(ocr_text) > len(combined):
                    combined = ocr_text
                    used_ocr = True
            except Exception:
                pass

        # CLI OCR fallback for environments without pdf2image/pytesseract.
        if (
            len(combined) < 400
            and shutil.which("pdftoppm")
            and shutil.which("tesseract")
        ):
            try:
                with tempfile.TemporaryDirectory(prefix="ocrpdf_") as tmpdir:
                    prefix = Path(tmpdir) / "page"
                    subprocess.run(
                        [
                            "pdftoppm",
                            "-f",
                            "1",
                            "-l",
                            str(max(1, ocr_max_pages)),
                            "-png",
                            str(path),
                            str(prefix),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=180,
                        check=False,
                    )

                    ocr_chunks: list[str] = []
                    for img_path in sorted(Path(tmpdir).glob("page-*.png")):
                        proc = subprocess.run(
                            ["tesseract", str(img_path), "stdout", "-l", ocr_lang],
                            capture_output=True,
                            text=True,
                            timeout=120,
                            check=False,
                        )
                        if proc.stdout:
                            ocr_chunks.append(proc.stdout)

                    cli_ocr_text = "\n".join(ocr_chunks).strip()
                    if len(cli_ocr_text) > len(combined):
                        combined = cli_ocr_text
                        used_ocr = True
            except Exception:
                pass

        has_native_pdf_text = len(native_text.strip()) >= 80
        return ExtractionResult(
            text=combined,
            has_native_pdf_text=has_native_pdf_text,
            used_ocr=used_ocr,
        )

    return ExtractionResult(text="", has_native_pdf_text=True, used_ocr=False)


def build_ocr_pdf(source_pdf: Path, target_pdf: Path, ocr_lang: str) -> bool:
    if not (shutil.which("pdftoppm") and shutil.which("tesseract")):
        return False

    page_pdfs: list[Path] = []
    try:
        with tempfile.TemporaryDirectory(prefix="ocr_rebuild_") as tmpdir:
            tmp_path = Path(tmpdir)
            image_prefix = tmp_path / "page"

            render = subprocess.run(
                ["pdftoppm", "-png", str(source_pdf), str(image_prefix)],
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
            if render.returncode != 0:
                return False

            images = sorted(tmp_path.glob("page-*.png"))
            if not images:
                return False

            for idx, img_path in enumerate(images, start=1):
                outbase = tmp_path / f"ocr_page_{idx:04d}"
                ocr = subprocess.run(
                    ["tesseract", str(img_path), str(outbase), "-l", ocr_lang, "pdf"],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
                page_pdf = Path(f"{outbase}.pdf")
                if ocr.returncode == 0 and page_pdf.exists():
                    page_pdfs.append(page_pdf)

            if not page_pdfs:
                return False

            if len(page_pdfs) == 1:
                shutil.copy2(page_pdfs[0], target_pdf)
                return target_pdf.exists()

            if shutil.which("pdfunite"):
                merge = subprocess.run(
                    ["pdfunite", *[str(p) for p in page_pdfs], str(target_pdf)],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
                if merge.returncode == 0 and target_pdf.exists():
                    return True

            if PdfReader is not None and PdfWriter is not None:
                writer = PdfWriter()
                for page_pdf in page_pdfs:
                    reader = PdfReader(str(page_pdf))
                    for page in reader.pages:
                        writer.add_page(page)
                with target_pdf.open("wb") as f:
                    writer.write(f)
                return target_pdf.exists()
    except Exception:
        return False

    return False


def build_no_text_hint(path: Path) -> str:
    if path.suffix.lower() != ".pdf":
        return "no_text_extracted"

    parts = [
        f"pypdf={'ok' if PdfReader is not None else 'missing'}",
        f"pdf2image={'ok' if convert_from_path is not None else 'missing'}",
        f"pytesseract={'ok' if pytesseract is not None else 'missing'}",
        f"pdftotext={'ok' if shutil.which('pdftotext') else 'missing'}",
        f"pdftoppm={'ok' if shutil.which('pdftoppm') else 'missing'}",
        f"tesseract={'ok' if shutil.which('tesseract') else 'missing'}",
    ]
    return "pdf_no_text; " + ", ".join(parts)


def build_prompt(text: str, categories: list[str]) -> str:
    categories_str = ", ".join(categories)
    return (
        "Du bist ein Dokument-Organizer. "
        "Antworte AUSSCHLIESSLICH als valides JSON ohne Markdown-Codeblock.\n"
        "Kategorisiere das Dokument in eine der erlaubten Kategorien.\n"
        f"Erlaubte Kategorien: {categories_str}\n"
        "Erzeuge exakt dieses JSON-Schema:\n"
        '{"sender":"...","category":"...","customer_number":"... oder null","title":"...","date":"YYYY-MM-DD oder null","confidence":0.0}\n'
        "Regeln:\n"
        "- sender: Firma/Institution/Absender kurz und praezise\n"
        "- category MUSS eine erlaubte Kategorie sein\n"
        "- customer_number: Kunden-/Vertrags-/Mitgliedsnummer, sonst null\n"
        "- title kurz und praezise\n"
        "- confidence zwischen 0 und 1\n"
        "- wenn Datum unsicher, setze null\n\n"
        "Dokumenttext:\n"
        f"{text}"
    )


def load_category_hints(hints_path: Path, categories: list[str]) -> dict[str, list[str]]:
    if not hints_path.exists():
        return {}

    try:
        raw = json.loads(hints_path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    if not isinstance(raw, dict):
        return {}

    normalized: dict[str, list[str]] = {}
    allowed = set(categories)
    for key, value in raw.items():
        category = str(key).strip().upper()
        if category not in allowed:
            continue
        if not isinstance(value, list):
            continue
        words = [str(v).strip().lower() for v in value if str(v).strip()]
        if words:
            normalized[category] = words

    return normalized


def add_hints_to_prompt(prompt: str, category_hints: dict[str, list[str]]) -> str:
    if not category_hints:
        return prompt

    lines = []
    for category in sorted(category_hints.keys()):
        hints = ", ".join(category_hints[category][:8])
        if hints:
            lines.append(f"- {category}: {hints}")
    if not lines:
        return prompt

    hints_block = "\nZusatz-Hinweise (Schlagwoerter pro Kategorie):\n" + "\n".join(lines) + "\n"
    return prompt + hints_block


def load_customer_number_hints(hints_path: Path) -> dict[str, list[str]]:
    if not hints_path.exists():
        return {"labels": [], "patterns": []}

    try:
        raw = json.loads(hints_path.read_text(encoding="utf-8"))
    except Exception:
        return {"labels": [], "patterns": []}

    if not isinstance(raw, dict):
        return {"labels": [], "patterns": []}

    labels_raw = raw.get("labels", [])
    patterns_raw = raw.get("patterns", [])

    labels = [str(v).strip().lower() for v in labels_raw if str(v).strip()] if isinstance(labels_raw, list) else []
    patterns = [str(v).strip() for v in patterns_raw if str(v).strip()] if isinstance(patterns_raw, list) else []
    return {"labels": labels, "patterns": patterns}


def normalize_customer_number(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    if v.lower() in {"null", "none", "n/a", "na", "keine", "ohne", "unknown"}:
        return None
    v = re.sub(r"\s+", " ", v)
    return v


def extract_customer_number_from_hints(text: str, hints: dict[str, list[str]]) -> Optional[str]:
    if not text.strip():
        return None

    # Normalize whitespace to keep regex matching predictable.
    norm_text = re.sub(r"[\t\r ]+", " ", text)

    for pattern in hints.get("patterns", []):
        try:
            m = re.search(pattern, norm_text, flags=re.IGNORECASE)
        except re.error:
            continue
        if not m:
            continue
        candidate = m.group(1) if m.groups() else m.group(0)
        normalized = normalize_customer_number(candidate)
        if normalized:
            return normalized

    for label in hints.get("labels", []):
        if not label:
            continue
        label_pattern = re.escape(label)
        m = re.search(
            rf"{label_pattern}\s*[:#\-]?\s*([A-Za-z0-9][A-Za-z0-9\-./ ]{{2,40}})",
            norm_text,
            flags=re.IGNORECASE,
        )
        if not m:
            continue
        candidate = m.group(1).strip()
        candidate = re.split(r"\s{2,}|\n", candidate)[0].strip()
        normalized = normalize_customer_number(candidate)
        if normalized:
            return normalized

    return None


def keyword_category_fallback(
    text: str,
    category_hints: dict[str, list[str]],
    categories: list[str],
    min_score: int,
) -> tuple[Optional[str], int]:
    if not category_hints:
        return None, 0

    normalized_text = " " + slugify(text).replace("_", " ") + " "
    scores: dict[str, int] = {}

    for category in categories:
        hints = category_hints.get(category, [])
        score = 0
        for hint in hints:
            h = slugify(hint).replace("_", " ").strip()
            if not h:
                continue
            if f" {h} " in normalized_text:
                score += 1
        scores[category] = score

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    if not ranked:
        return None, 0

    top_category, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0

    if top_score >= min_score and top_score >= second_score + 1:
        return top_category, top_score

    return None, top_score


def call_ollama(
    ollama_url: str,
    model: str,
    prompt: str,
    timeout_seconds: int,
    retries: int,
    backoff: float,
    ollama_num_thread: int,
    ollama_keep_alive: str,
) -> dict:
    endpoint = ollama_url.rstrip("/") + "/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "keep_alive": ollama_keep_alive,
    }
    if ollama_num_thread > 0:
        payload["options"] = {"num_thread": ollama_num_thread}

    last_exc: Optional[Exception] = None
    attempts = max(1, retries + 1)

    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(endpoint, json=payload, timeout=timeout_seconds)
            response.raise_for_status()
            body = response.json()

            raw = body.get("response", "{}")
            if isinstance(raw, dict):
                return raw
            if isinstance(raw, str):
                return json.loads(raw)
            return {}
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError, json.JSONDecodeError) as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            sleep_s = max(0.2, backoff ** (attempt - 1))
            time.sleep(sleep_s)

    if last_exc is not None:
        raise last_exc
    return {}


def get_installed_models(ollama_url: str, timeout_seconds: int) -> list[str]:
    endpoint = ollama_url.rstrip("/") + "/api/tags"
    response = requests.get(endpoint, timeout=max(5, min(timeout_seconds, 30)))
    response.raise_for_status()
    body = response.json()
    models = body.get("models", [])
    names: list[str] = []
    for model in models:
        if isinstance(model, dict):
            name = str(model.get("name", "")).strip()
            if name:
                names.append(name)
    return names


def resolve_model(selected_model: str, ollama_url: str, timeout_seconds: int) -> str:
    if selected_model.strip():
        return selected_model.strip()

    models = get_installed_models(ollama_url, timeout_seconds)
    if len(models) == 1:
        return models[0]

    if len(models) == 0:
        raise RuntimeError(
            "Kein Ollama-Modell installiert. Bitte zuerst z. B. 'ollama pull qwen2.5:3b-instruct' ausfuehren."
        )

    raise RuntimeError(
        "Mehrere Ollama-Modelle installiert. Bitte --model angeben. Verfuegbar: " + ", ".join(models)
    )


def normalize_classification(data: dict, categories: list[str]) -> Classification:
    sender = str(data.get("sender", "unbekannter_absender")).strip()
    if not sender:
        sender = "unbekannter_absender"

    category = str(data.get("category", "SONSTIGES")).strip().upper()
    if category not in categories:
        category = "SONSTIGES"

    customer_number = normalize_customer_number(data.get("customer_number"))

    title = str(data.get("title", "unbekanntes_dokument")).strip()
    if not title:
        title = "unbekanntes_dokument"

    date = data.get("date")
    if date is not None:
        date = str(date).strip() or None

    confidence = parse_confidence(data.get("confidence", 0.0))

    return Classification(
        sender=sender,
        category=category,
        customer_number=customer_number,
        title=title,
        date=date,
        confidence=confidence,
    )


def write_log(log_path: Path, payload: dict) -> None:
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def emit(line: str, run_log_path: Path) -> None:
    print(line)
    with run_log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def build_tmp_dated_log_path(file_name_or_path: str, date_prefix: str) -> Path:
    # Always write logs to /tmp and keep only the filename part from user input.
    base_name = Path(file_name_or_path).name or "organize.log"
    return Path("/tmp") / f"{date_prefix}_{base_name}"


def plan_target_path(
    src: Path,
    classification: Classification,
    sorted_root: Path,
    review_root: Path,
    min_confidence: float,
) -> Path:
    date_part = ensure_date(classification.date)
    sender_part = slugify(classification.sender)
    category_part = slugify(classification.category, uppercase=True)
    customer_number_part = slugify(classification.customer_number) if classification.customer_number else ""
    title_part = slugify(classification.title)
    ext = src.suffix.lower()
    year_part = date_part[:4]

    filename_parts = [date_part, sender_part, category_part]
    if customer_number_part:
        filename_parts.append(customer_number_part)
    filename_parts.append(title_part)
    filename = "_".join(filename_parts) + ext

    if classification.confidence < min_confidence:
        target = review_root / filename
    else:
        target = sorted_root / year_part / filename

    target.parent.mkdir(parents=True, exist_ok=True)
    return unique_path(target)


def iter_input_files(input_dir: Path, sorted_dir_name: str, review_dir_name: str):
    for p in input_dir.rglob("*"):
        if not p.is_file():
            continue

        # Avoid re-processing generated outputs and hidden files.
        if sorted_dir_name in p.parts or review_dir_name in p.parts:
            continue
        if any(part.startswith(".") for part in p.parts):
            continue

        if is_supported(p):
            yield p


def apply_runtime_limits(process_nice: int, max_cpu_threads: int) -> None:
    if process_nice > 0:
        try:
            os.nice(process_nice)
        except Exception:
            pass

    if max_cpu_threads > 0:
        for env_key in (
            "OMP_THREAD_LIMIT",
            "OMP_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
        ):
            os.environ[env_key] = str(max_cpu_threads)


def main() -> int:
    args = parse_args()
    apply_changes = args.apply and not args.dry_run
    apply_runtime_limits(args.process_nice, args.max_cpu_threads)

    date_prefix = datetime.now().strftime("%Y-%m-%d")
    run_log_path = build_tmp_dated_log_path(args.run_log_file, date_prefix)
    run_log_path.parent.mkdir(parents=True, exist_ok=True)
    emit(
        f"[RUN] {datetime.now().isoformat(timespec='seconds')} mode={'APPLY' if apply_changes else 'DRY-RUN'}",
        run_log_path,
    )

    input_dir = Path(args.input).expanduser().resolve()
    if not input_dir.exists() or not input_dir.is_dir():
        emit(f"[ERROR] Input folder invalid: {input_dir}", run_log_path)
        return 2

    sorted_root = input_dir / args.sorted_dir
    review_root = input_dir / args.review_dir
    log_path = build_tmp_dated_log_path(args.log_file, date_prefix)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    categories = [c.strip().upper() for c in args.categories if c.strip()]
    if "SONSTIGES" not in categories:
        categories.append("SONSTIGES")

    base_dir = Path(__file__).resolve().parent
    hints_path = Path(args.category_hints_file).expanduser()
    if not hints_path.is_absolute():
        hints_path = base_dir / hints_path
    category_hints = load_category_hints(hints_path, categories)

    customer_number_hints_path = Path(args.customer_number_hints_file).expanduser()
    if not customer_number_hints_path.is_absolute():
        customer_number_hints_path = base_dir / customer_number_hints_path
    customer_number_hints = load_customer_number_hints(customer_number_hints_path)

    files = list(iter_input_files(input_dir, args.sorted_dir, args.review_dir))
    if not files:
        emit("[INFO] Keine verarbeitbaren Dateien gefunden.", run_log_path)
        emit(f"[INFO] run_log: {run_log_path}", run_log_path)
        return 0

    try:
        active_model = resolve_model(args.model, args.ollama_url, args.ollama_timeout)
    except Exception as exc:
        emit(f"[ERROR] Modellauswahl fehlgeschlagen: {exc}", run_log_path)
        return 2

    stats = {
        "seen": 0,
        "processed": 0,
        "renamed": 0,
        "ocr_rebuilt": 0,
        "review": 0,
        "errors": 0,
        "skipped_empty": 0,
    }

    emit(f"[INFO] Modus: {'APPLY' if apply_changes else 'DRY-RUN'}", run_log_path)
    emit(f"[INFO] Modell: {active_model}", run_log_path)
    emit(f"[INFO] Dateien: {len(files)}", run_log_path)
    emit(f"[INFO] category_hints: {hints_path} ({len(category_hints)} categories)", run_log_path)
    emit(
        f"[INFO] customer_number_hints: {customer_number_hints_path} "
        f"(labels={len(customer_number_hints.get('labels', []))}, patterns={len(customer_number_hints.get('patterns', []))})",
        run_log_path,
    )
    emit(
        "[INFO] Limits: "
        f"nice=+{args.process_nice}, "
        f"max_cpu_threads={args.max_cpu_threads}, "
        f"ollama_num_thread={args.ollama_num_thread}, "
        f"ollama_keep_alive={args.ollama_keep_alive}, "
        f"sleep_between_files={args.sleep_between_files}s",
        run_log_path,
    )

    for src in files:
        stats["seen"] += 1
        event = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "source": str(src),
            "mode": "apply" if apply_changes else "dry-run",
        }

        try:
            extracted = extract_text(src, ocr_lang=args.lang, ocr_max_pages=args.ocr_max_pages)
            text = (extracted.text or "").strip()

            if not text:
                stats["skipped_empty"] += 1
                hint = build_no_text_hint(src)
                event.update({"status": "skipped", "reason": hint})
                write_log(log_path, event)
                emit(f"[SKIP] {src.name}: kein Text extrahiert ({hint})", run_log_path)
                continue

            clipped_text = text[: args.max_text_chars]
            prompt = build_prompt(clipped_text, categories)
            prompt = add_hints_to_prompt(prompt, category_hints)
            model_raw = call_ollama(
                ollama_url=args.ollama_url,
                model=active_model,
                prompt=prompt,
                timeout_seconds=args.ollama_timeout,
                retries=args.ollama_retries,
                backoff=args.ollama_retry_backoff,
                ollama_num_thread=args.ollama_num_thread,
                ollama_keep_alive=args.ollama_keep_alive,
            )
            cls = normalize_classification(model_raw, categories)

            keyword_fallback_applied = False
            keyword_fallback_score = 0
            fallback_category, keyword_fallback_score = keyword_category_fallback(
                text=text,
                category_hints=category_hints,
                categories=categories,
                min_score=args.keyword_fallback_min_score,
            )
            if fallback_category is not None and (
                cls.confidence < args.min_confidence or cls.category == "SONSTIGES"
            ):
                cls.category = fallback_category
                cls.confidence = max(cls.confidence, args.min_confidence)
                keyword_fallback_applied = True

            customer_number_from_hints = False
            if not cls.customer_number:
                extracted_customer_number = extract_customer_number_from_hints(text, customer_number_hints)
                if extracted_customer_number:
                    cls.customer_number = extracted_customer_number
                    customer_number_from_hints = True

            target = plan_target_path(
                src=src,
                classification=cls,
                sorted_root=sorted_root,
                review_root=review_root,
                min_confidence=args.min_confidence,
            )

            should_rebuild_ocr_pdf = (
                src.suffix.lower() == ".pdf"
                and (not extracted.has_native_pdf_text)
                and extracted.used_ocr
            )

            in_review = cls.confidence < args.min_confidence
            if apply_changes:
                if should_rebuild_ocr_pdf:
                    rebuilt = build_ocr_pdf(src, target, args.lang)
                    if rebuilt:
                        src.unlink(missing_ok=True)
                        stats["ocr_rebuilt"] += 1
                    else:
                        shutil.move(str(src), str(target))
                else:
                    shutil.move(str(src), str(target))

            stats["processed"] += 1
            stats["renamed"] += 1
            if in_review:
                stats["review"] += 1

            event.update(
                {
                    "status": "ok",
                    "source": str(src),
                    "target": str(target),
                    "sender": cls.sender,
                    "category": cls.category,
                    "customer_number": cls.customer_number,
                    "title": cls.title,
                    "date": ensure_date(cls.date),
                    "confidence": cls.confidence,
                    "review": in_review,
                    "ocr_pdf_rebuild": should_rebuild_ocr_pdf,
                    "keyword_fallback_applied": keyword_fallback_applied,
                    "keyword_fallback_score": keyword_fallback_score,
                    "customer_number_from_hints": customer_number_from_hints,
                    "model": active_model,
                }
            )
            write_log(log_path, event)

            flag = "REVIEW" if in_review else "SORTED"
            if should_rebuild_ocr_pdf:
                action = "OCR+MOVE" if apply_changes else "OCR+PLAN"
            else:
                action = "MOVE" if apply_changes else "PLAN"
            emit(
                f"[{action}/{flag}] {src.name} -> {target.name} "
                f"(cat={cls.category}, conf={cls.confidence:.2f})",
                run_log_path,
            )
        except Exception as exc:
            stats["errors"] += 1
            event.update({"status": "error", "error": str(exc)})
            write_log(log_path, event)
            emit(f"[ERROR] {src.name}: {exc}", run_log_path)

        if args.sleep_between_files > 0:
            time.sleep(args.sleep_between_files)

    emit("", run_log_path)
    emit("[SUMMARY]", run_log_path)
    for k, v in stats.items():
        emit(f"- {k}: {v}", run_log_path)
    emit(f"- log: {log_path}", run_log_path)
    emit(f"- run_log: {run_log_path}", run_log_path)
    emit(
        f"[RUN] {datetime.now().isoformat(timespec='seconds')} finished status={'ok' if stats['errors'] == 0 else 'error'}",
        run_log_path,
    )

    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
