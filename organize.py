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
    from pypdf import PdfReader
except Exception:
    PdfReader = None

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
    "SONSTIGES",
]


@dataclass
class Classification:
    category: str
    title: str
    date: Optional[str]
    confidence: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Organize documents with a local Ollama model")
    parser.add_argument("--input", required=True, help="Input folder with documents")
    parser.add_argument("--model", default="qwen2.5:7b-instruct", help="Ollama model name")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434", help="Ollama base URL")
    parser.add_argument("--ollama-timeout", type=int, default=180, help="Ollama request timeout in seconds")
    parser.add_argument("--ollama-retries", type=int, default=2, help="Retries on timeout/error")
    parser.add_argument("--ollama-retry-backoff", type=float, default=1.5, help="Backoff factor between retries")
    parser.add_argument("--categories", nargs="+", default=DEFAULT_CATEGORIES, help="Allowed categories")
    parser.add_argument("--lang", default="deu", help="OCR language for Tesseract")
    parser.add_argument("--min-confidence", type=float, default=0.75, help="Confidence threshold for auto-sort")
    parser.add_argument("--max-text-chars", type=int, default=12000, help="Max chars sent to the model")
    parser.add_argument("--sorted-dir", default="_sorted", help="Folder for categorized files (relative to input)")
    parser.add_argument("--review-dir", default="_review", help="Folder for low-confidence files (relative to input)")
    parser.add_argument("--log-file", default="organize_log.jsonl", help="Path to JSONL log")
    parser.add_argument("--ocr-max-pages", type=int, default=3, help="Max pages to OCR when PDF has little text")
    parser.add_argument("--process-nice", type=int, default=5, help="Increase niceness to lower CPU priority")
    parser.add_argument("--max-cpu-threads", type=int, default=2, help="Limit CPU threads for OCR/BLAS libs (0 disables)")
    parser.add_argument("--ollama-num-thread", type=int, default=2, help="Limit Ollama model threads (0 uses model default)")
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


def extract_text(path: Path, ocr_lang: str, ocr_max_pages: int) -> str:
    ext = path.suffix.lower()

    if ext in SUPPORTED_TEXT_EXT:
        return path.read_text(encoding="utf-8", errors="ignore")

    if ext in SUPPORTED_IMAGE_EXT:
        if pytesseract is None:
            return ""
        try:
            from PIL import Image

            with Image.open(path) as img:
                return pytesseract.image_to_string(img, lang=ocr_lang)
        except Exception:
            return ""

    if ext == ".pdf":
        chunks: list[str] = []
        if PdfReader is not None:
            try:
                reader = PdfReader(str(path))
                for page in reader.pages:
                    chunks.append(page.extract_text() or "")
            except Exception:
                pass

        combined = "\n".join(chunks).strip()

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
            except Exception:
                pass

        return combined

    return ""


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
        '{"category":"...","title":"...","date":"YYYY-MM-DD oder null","confidence":0.0}\n'
        "Regeln:\n"
        "- category MUSS eine erlaubte Kategorie sein\n"
        "- title kurz und praezise\n"
        "- confidence zwischen 0 und 1\n"
        "- wenn Datum unsicher, setze null\n\n"
        "Dokumenttext:\n"
        f"{text}"
    )


def call_ollama(
    ollama_url: str,
    model: str,
    prompt: str,
    timeout_seconds: int,
    retries: int,
    backoff: float,
    ollama_num_thread: int,
) -> dict:
    endpoint = ollama_url.rstrip("/") + "/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
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


def normalize_classification(data: dict, categories: list[str]) -> Classification:
    category = str(data.get("category", "SONSTIGES")).strip().upper()
    if category not in categories:
        category = "SONSTIGES"

    title = str(data.get("title", "unbekanntes_dokument")).strip()
    if not title:
        title = "unbekanntes_dokument"

    date = data.get("date")
    if date is not None:
        date = str(date).strip() or None

    confidence = parse_confidence(data.get("confidence", 0.0))

    return Classification(category=category, title=title, date=date, confidence=confidence)


def write_log(log_path: Path, payload: dict) -> None:
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def plan_target_path(
    src: Path,
    classification: Classification,
    sorted_root: Path,
    review_root: Path,
    min_confidence: float,
) -> Path:
    date_part = ensure_date(classification.date)
    category_part = slugify(classification.category, uppercase=True)
    title_part = slugify(classification.title)
    ext = src.suffix.lower()

    filename = f"{date_part}_{category_part}_{title_part}{ext}"

    if classification.confidence < min_confidence:
        target = review_root / filename
    else:
        target = sorted_root / category_part / filename

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

    input_dir = Path(args.input).expanduser().resolve()
    if not input_dir.exists() or not input_dir.is_dir():
        print(f"[ERROR] Input folder invalid: {input_dir}")
        return 2

    sorted_root = input_dir / args.sorted_dir
    review_root = input_dir / args.review_dir
    log_path = Path(args.log_file).expanduser().resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    categories = [c.strip().upper() for c in args.categories if c.strip()]
    if "SONSTIGES" not in categories:
        categories.append("SONSTIGES")

    files = list(iter_input_files(input_dir, args.sorted_dir, args.review_dir))
    if not files:
        print("[INFO] Keine verarbeitbaren Dateien gefunden.")
        return 0

    stats = {
        "seen": 0,
        "processed": 0,
        "renamed": 0,
        "review": 0,
        "errors": 0,
        "skipped_empty": 0,
    }

    print(f"[INFO] Modus: {'APPLY' if apply_changes else 'DRY-RUN'}")
    print(f"[INFO] Dateien: {len(files)}")
    print(
        "[INFO] Limits: "
        f"nice=+{args.process_nice}, "
        f"max_cpu_threads={args.max_cpu_threads}, "
        f"ollama_num_thread={args.ollama_num_thread}, "
        f"sleep_between_files={args.sleep_between_files}s"
    )

    for src in files:
        stats["seen"] += 1
        event = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "source": str(src),
            "mode": "apply" if apply_changes else "dry-run",
        }

        try:
            text = extract_text(src, ocr_lang=args.lang, ocr_max_pages=args.ocr_max_pages)
            text = (text or "").strip()

            if not text:
                stats["skipped_empty"] += 1
                hint = build_no_text_hint(src)
                event.update({"status": "skipped", "reason": hint})
                write_log(log_path, event)
                print(f"[SKIP] {src.name}: kein Text extrahiert ({hint})")
                continue

            clipped_text = text[: args.max_text_chars]
            prompt = build_prompt(clipped_text, categories)
            model_raw = call_ollama(
                ollama_url=args.ollama_url,
                model=args.model,
                prompt=prompt,
                timeout_seconds=args.ollama_timeout,
                retries=args.ollama_retries,
                backoff=args.ollama_retry_backoff,
                ollama_num_thread=args.ollama_num_thread,
            )
            cls = normalize_classification(model_raw, categories)

            target = plan_target_path(
                src=src,
                classification=cls,
                sorted_root=sorted_root,
                review_root=review_root,
                min_confidence=args.min_confidence,
            )

            in_review = cls.confidence < args.min_confidence
            if apply_changes:
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
                    "category": cls.category,
                    "title": cls.title,
                    "date": ensure_date(cls.date),
                    "confidence": cls.confidence,
                    "review": in_review,
                    "model": args.model,
                }
            )
            write_log(log_path, event)

            flag = "REVIEW" if in_review else "SORTED"
            action = "MOVE" if apply_changes else "PLAN"
            print(
                f"[{action}/{flag}] {src.name} -> {target.name} "
                f"(cat={cls.category}, conf={cls.confidence:.2f})"
            )
        except Exception as exc:
            stats["errors"] += 1
            event.update({"status": "error", "error": str(exc)})
            write_log(log_path, event)
            print(f"[ERROR] {src.name}: {exc}")

        if args.sleep_between_files > 0:
            time.sleep(args.sleep_between_files)

    print("\n[SUMMARY]")
    for k, v in stats.items():
        print(f"- {k}: {v}")
    print(f"- log: {log_path}")

    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
