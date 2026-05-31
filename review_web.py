#!/usr/bin/env python3
"""Review and deploy document rename suggestions from organize.py logs.

Workflow:
1) Run organize.py in dry-run mode.
2) Start this web app and review/edit suggestions.
3) Click Deploy to apply file moves/renames.

The app persists:
- pending/deployed state in review_state.json
- learned aliases in field_aliases.json
"""

from __future__ import annotations

import argparse
import hmac
import hashlib
import json
import mimetypes
import os
import re
import secrets
import signal
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from assistant_config import get_section, load_config, pick, validate_config


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


def strip_to_ascii(text: str) -> str:
    text = text.replace("\u00e4", "ae").replace("\u00f6", "oe").replace("\u00fc", "ue")
    text = text.replace("\u00c4", "Ae").replace("\u00d6", "Oe").replace("\u00dc", "Ue")
    text = text.replace("\u00df", "ss")
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


def ensure_date(date_text: Optional[str], source_name: str = "") -> str:
    date_text = (date_text or "").strip()

    def parse_direct(value: str) -> Optional[str]:
        patterns = [
            (r"^(\d{4})-(\d{2})-(\d{2})$", "%Y-%m-%d"),
            (r"^(\d{2})\.(\d{2})\.(\d{4})$", "%d.%m.%Y"),
            (r"^(\d{2})/(\d{2})/(\d{4})$", "%d/%m/%Y"),
        ]
        for pattern, fmt in patterns:
            if re.match(pattern, value):
                try:
                    return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
                except ValueError:
                    return None
        return None

    parsed = parse_direct(date_text) if date_text else None
    if parsed:
        return parsed

    stem = Path(source_name).stem

    m = re.search(r"\b(\d{4})[-_.](\d{2})[-_.](\d{2})\b", stem)
    if m:
        parsed = parse_direct(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
        if parsed:
            return parsed

    m = re.search(r"\b(\d{2})\.(\d{2})\.(\d{4})\b", stem)
    if m:
        parsed = parse_direct(f"{m.group(1)}.{m.group(2)}.{m.group(3)}")
        if parsed:
            return parsed

    m = re.search(r"\b(\d{2})(\d{2})(\d{2})\b", stem)
    if m:
        yy = int(m.group(1))
        mm = int(m.group(2))
        dd = int(m.group(3))
        current_yy = int(datetime.now().strftime("%y"))
        century = 2000 if yy <= current_yy else 1900
        try:
            return datetime(century + yy, mm, dd).strftime("%Y-%m-%d")
        except ValueError:
            pass

    return datetime.now().strftime("%Y-%m-%d")


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


def entry_id_for_event(event: dict[str, Any]) -> str:
    base = f"{event.get('timestamp','')}|{event.get('source','')}|{event.get('target','')}"
    return hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()[:16]


@dataclass
class Paths:
    log_file: Path
    state_file: Path
    aliases_file: Path


class PasswordAuth:
    def __init__(self, password: str, session_ttl_seconds: int) -> None:
        self.password = password
        self.session_ttl_seconds = max(300, session_ttl_seconds)
        self.sessions: dict[str, float] = {}
        self.lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def create_session(self, password_attempt: str) -> Optional[str]:
        if not self.enabled:
            return ""
        if not hmac.compare_digest(password_attempt, self.password):
            return None
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions[token] = time.time() + float(self.session_ttl_seconds)
        return token

    def is_valid(self, token: str) -> bool:
        if not self.enabled:
            return True
        if not token:
            return False

        now = time.time()
        with self.lock:
            expiry = self.sessions.get(token)
            if expiry is None:
                return False
            if expiry < now:
                self.sessions.pop(token, None)
                return False
            # Sliding expiration while actively used.
            self.sessions[token] = now + float(self.session_ttl_seconds)
            return True

    def clear_session(self, token: str) -> None:
        if not token:
            return
        with self.lock:
            self.sessions.pop(token, None)


class ReviewStore:
    def __init__(self, paths: Paths, categories: list[str]) -> None:
        self.paths = paths
        self.categories = categories
        self.lock = threading.Lock()
        self.state = self._load_state()
        self.aliases = self._load_aliases()
        self._sync_from_log()

    def _load_json_file(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _write_json_file(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_state(self) -> dict[str, Any]:
        state = self._load_json_file(
            self.paths.state_file,
            {
                "entries": {},
                "value_memory": {
                    "sender": [],
                    "category": [],
                    "customer_number": [],
                    "title": [],
                },
            },
        )
        if not isinstance(state, dict):
            state = {}
        state.setdefault("entries", {})
        state.setdefault("value_memory", {})
        for key in ("sender", "category", "customer_number", "title"):
            values = state["value_memory"].get(key, [])
            if not isinstance(values, list):
                values = []
            state["value_memory"][key] = [str(v) for v in values if str(v).strip()]
        return state

    def _load_aliases(self) -> dict[str, dict[str, str]]:
        aliases = self._load_json_file(
            self.paths.aliases_file,
            {"sender": {}, "category": {}, "customer_number": {}, "title": {}},
        )
        if not isinstance(aliases, dict):
            aliases = {}
        normalized: dict[str, dict[str, str]] = {}
        for key in ("sender", "category", "customer_number", "title"):
            raw = aliases.get(key, {})
            if not isinstance(raw, dict):
                raw = {}
            normalized[key] = {str(k): str(v) for k, v in raw.items() if str(k).strip() and str(v).strip()}
        return normalized

    def _log_files_for_sync(self) -> list[Path]:
        base = self.paths.log_file
        candidates: list[Path] = []

        if base.exists() and base.is_file():
            candidates.append(base)

        if base.parent.exists():
            candidates.extend([p for p in base.parent.glob("*_organize_log.jsonl") if p.is_file()])

        # Deduplicate while preserving deterministic ordering by file path.
        unique = sorted({p.resolve() for p in candidates})
        return unique

    def _sync_from_log(self) -> None:
        entries = self.state["entries"]
        log_files = self._log_files_for_sync()
        if not log_files:
            return

        for log_file in log_files:
            try:
                lines = log_file.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue

            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue

                if event.get("status") != "ok":
                    continue
                if not event.get("source") or not event.get("target"):
                    continue

                item_id = entry_id_for_event(event)
                if item_id in entries:
                    continue

                default_fields = {
                    "sender": str(event.get("sender", "")).strip(),
                    "category": str(event.get("category", "SONSTIGES")).strip().upper() or "SONSTIGES",
                    "customer_number": str(event.get("customer_number", "")).strip(),
                    "title": str(event.get("title", "")).strip(),
                    "date": str(event.get("date", "")).strip(),
                }
                if default_fields["category"] not in self.categories:
                    default_fields["category"] = "SONSTIGES"

                entries[item_id] = {
                    "id": item_id,
                    "status": "pending",
                    "source": str(event.get("source")),
                    "target": str(event.get("target")),
                    "default": default_fields,
                    "edited": dict(default_fields),
                    "confidence": float(event.get("confidence", 0.0) or 0.0),
                    "review": bool(event.get("review", False)),
                    "created_at": str(event.get("timestamp", "")),
                    "deployed_at": "",
                    "deployed_target": "",
                }

        self._save_state()

    def _save_state(self) -> None:
        self._write_json_file(self.paths.state_file, self.state)

    def _save_aliases(self) -> None:
        self._write_json_file(self.paths.aliases_file, self.aliases)

    def _edited_differs_from_default(self, entry: dict[str, Any]) -> bool:
        default = entry.get("default", {})
        edited = entry.get("edited", {})
        for field in ("sender", "category", "customer_number", "title", "date"):
            if str(default.get(field, "")).strip() != str(edited.get(field, "")).strip():
                return True
        return False

    def _remember_values(self, fields: dict[str, str]) -> None:
        memory = self.state["value_memory"]
        for key in ("sender", "category", "customer_number", "title"):
            value = str(fields.get(key, "")).strip()
            if not value:
                continue
            values = memory.get(key, [])
            if value not in values:
                values.append(value)
            memory[key] = values[-500:]

    def _infer_roots(self, entry: dict[str, Any]) -> tuple[Path, Path]:
        src = Path(entry["source"]).resolve()
        original_target = Path(entry["target"]).resolve()

        if entry.get("review"):
            review_root = original_target.parent
            if "_review" in review_root.parts:
                idx = review_root.parts.index("_review")
                sorted_root = Path(*review_root.parts[:idx]) if idx > 0 else src.parent
            else:
                sorted_root = src.parent
            return sorted_root, review_root

        parent = original_target.parent
        if re.match(r"^\d{4}$", parent.name):
            sorted_root = parent.parent
        else:
            sorted_root = parent

        # Backward compatibility for historic targets under outbox/_sorted/YYYY.
        if sorted_root.name == "_sorted":
            review_root = sorted_root.parent / "_review"
        else:
            review_root = sorted_root / "_review"
        return sorted_root, review_root

    def _build_target(self, entry: dict[str, Any]) -> Path:
        fields = entry["edited"]
        src = Path(entry["source"]).resolve()
        sorted_root, review_root = self._infer_roots(entry)

        date_part = ensure_date(fields.get("date", ""), src.name)
        sender_part = slugify(fields.get("sender", ""))
        category_part = slugify(fields.get("category", "SONSTIGES"), uppercase=True)
        customer = fields.get("customer_number", "").strip()
        title_part = slugify(fields.get("title", ""))
        ext = src.suffix.lower()

        parts = [date_part, sender_part, category_part]
        if customer:
            parts.append(slugify(customer))
        parts.append(title_part)
        name = "_".join(parts) + ext

        if entry.get("review"):
            base_target = review_root / name
        else:
            base_target = sorted_root / date_part[:4] / name

        return unique_path(base_target)

    def _format_target_preview(self, target_preview: str) -> str:
        raw = str(target_preview or "").strip()
        if not raw:
            return ""

        p = Path(raw)
        parts = p.parts
        for marker in ("_review", "_sorted", "outbox", "output"):
            if marker in parts:
                idx = parts.index(marker)
                return str(Path(*parts[idx:]))

        return raw

    def _format_source_preview(self, source_path: str) -> str:
        raw = str(source_path or "").strip()
        if not raw:
            return ""

        p = Path(raw)
        parts = p.parts
        for marker in ("inbox", "_review", "_sorted", "outbox", "output"):
            if marker in parts:
                idx = parts.index(marker)
                return str(Path(*parts[idx:]))

        return raw

    def list_entries(self) -> dict[str, Any]:
        with self.lock:
            self._sync_from_log()
            rows: list[dict[str, Any]] = []
            for entry in self.state["entries"].values():
                source = Path(entry.get("source", ""))
                status_value = str(entry.get("status", "pending") or "pending")
                target_preview = str(entry.get("deployed_target", "")).strip()
                if status_value != "deployed" or not target_preview:
                    target_preview = str(self._build_target(entry))
                target_preview = self._format_target_preview(target_preview)
                row = {
                    "id": entry["id"],
                    "status": status_value,
                    "source": str(source),
                    "source_preview": self._format_source_preview(str(source)),
                    "source_name": source.name,
                    "source_exists": source.exists(),
                    "confidence": float(entry.get("confidence", 0.0) or 0.0),
                    "review": bool(entry.get("review", False)),
                    "target_preview": target_preview,
                    "default": entry.get("default", {}),
                    "edited": entry.get("edited", {}),
                }
                rows.append(row)

            status_order = {"pending": 0, "saved": 1, "missing": 2, "deployed": 3}
            rows.sort(key=lambda r: (status_order.get(r.get("status", "pending"), 9), r["source_name"].lower()))
            counts = {"pending": 0, "saved": 0, "missing": 0, "deployed": 0}
            for row in rows:
                key = row.get("status", "pending")
                if key in counts:
                    counts[key] += 1
            return {
                "rows": rows,
                "categories": self.categories,
                "value_memory": self.state.get("value_memory", {}),
                "aliases": self.aliases,
                "log_file": str(self.paths.log_file),
                "state_file": str(self.paths.state_file),
                "aliases_file": str(self.paths.aliases_file),
                "counts": counts,
            }

    def save_edits(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        with self.lock:
            updated = 0
            entries = self.state["entries"]
            for row in rows:
                item_id = str(row.get("id", "")).strip()
                if item_id not in entries:
                    continue
                entry = entries[item_id]
                if entry.get("status") == "deployed":
                    continue

                edited = entry.get("edited", {}).copy()
                incoming = row.get("edited", {}) if isinstance(row.get("edited", {}), dict) else {}

                for field in ("sender", "category", "customer_number", "title", "date"):
                    value = str(incoming.get(field, "")).strip()
                    if field == "category":
                        value = value.upper() if value else "SONSTIGES"
                        if value not in self.categories:
                            value = "SONSTIGES"
                    edited[field] = value

                entry["edited"] = edited
                self._remember_values(edited)
                if Path(entry.get("source", "")).exists():
                    entry["status"] = "saved" if self._edited_differs_from_default(entry) else "pending"
                else:
                    entry["status"] = "missing"
                updated += 1

            self._save_state()
            return {"updated": updated}

    def _learn_aliases(self, entry: dict[str, Any], learn_fields: dict[str, bool]) -> int:
        count = 0
        default = entry.get("default", {})
        edited = entry.get("edited", {})

        for field in ("sender", "category", "customer_number", "title"):
            if not bool(learn_fields.get(field, False)):
                continue
            src_value = str(default.get(field, "")).strip()
            dst_value = str(edited.get(field, "")).strip()
            if not src_value or not dst_value or src_value == dst_value:
                continue

            key = slugify(src_value)
            self.aliases.setdefault(field, {})[key] = dst_value
            count += 1

        return count

    def deploy(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        with self.lock:
            applied = 0
            missing = 0
            learned = 0
            errors: list[str] = []

            entries = self.state["entries"]
            for row in rows:
                item_id = str(row.get("id", "")).strip()
                if item_id not in entries:
                    continue
                entry = entries[item_id]
                if entry.get("status") == "deployed":
                    continue

                incoming = row.get("edited", {}) if isinstance(row.get("edited", {}), dict) else {}
                learn_fields = row.get("learn", {}) if isinstance(row.get("learn", {}), dict) else {}

                for field in ("sender", "category", "customer_number", "title", "date"):
                    value = str(incoming.get(field, "")).strip()
                    if field == "category":
                        value = value.upper() if value else "SONSTIGES"
                        if value not in self.categories:
                            value = "SONSTIGES"
                    entry["edited"][field] = value

                self._remember_values(entry["edited"])
                learned += self._learn_aliases(entry, learn_fields)

                src = Path(entry["source"])
                if not src.exists():
                    entry["status"] = "missing"
                    missing += 1
                    continue

                try:
                    target = self._build_target(entry)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(target))
                    entry["status"] = "deployed"
                    entry["deployed_at"] = datetime.now().isoformat(timespec="seconds")
                    entry["deployed_target"] = str(target)
                    applied += 1
                except Exception as exc:
                    entry["status"] = "saved" if self._edited_differs_from_default(entry) else "pending"
                    errors.append(f"{src.name}: {exc}")

            self._save_state()
            self._save_aliases()
            return {
                "applied": applied,
                "missing": missing,
                "learned_aliases": learned,
                "errors": errors,
            }

    def file_path_for_id(self, item_id: str) -> Optional[Path]:
        with self.lock:
            entry = self.state["entries"].get(item_id)
            if not entry:
                return None
            candidate = Path(entry.get("source", ""))
            if candidate.exists() and candidate.is_file():
                return candidate
            deployed = Path(entry.get("deployed_target", ""))
            if deployed.exists() and deployed.is_file():
                return deployed
            return None


HTML_PAGE = """<!doctype html>
<html lang=\"de\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Dokumente zur Prüfung</title>
    <style>
        :root {
            --bg: #0f1117;
            --bg2: #151926;
            --card: #1c2233;
            --ink: #e8edf7;
            --muted: #9aa6bd;
            --accent: #23c4a8;
            --line: #2b3449;
            --ok: #55d187;
            --warn: #f7b955;
            --err: #ff6b6b;
            --field: #111827;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
            color: var(--ink);
            background: radial-gradient(circle at top right, #20263a 0%, var(--bg) 42%), var(--bg2);
        }
        .wrap { width: 100vw; max-width: 100vw; margin: 0; padding: 18px; }
        .top {
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 12px;
            padding: 14px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.35);
            position: sticky;
            top: 8px;
            z-index: 2;
        }
        h1 { margin: 0 0 10px 0; font-size: 22px; letter-spacing: 0.2px; }
        .meta { color: var(--muted); font-size: 13px; margin-bottom: 10px; }
        .activity {
            display: inline-block;
            margin-bottom: 10px;
            border: 1px solid var(--line);
            border-radius: 999px;
            padding: 3px 10px;
            font-size: 12px;
            font-weight: 600;
            background: #1a2131;
        }
        .activity.busy { color: #f7b955; border-color: #7a5a2a; background: #2a1f1a; }
        .activity.idle { color: #55d187; border-color: #2f6b4d; background: #16271f; }
        .actions { display: flex; gap: 10px; flex-wrap: wrap; }
        .filter-box { display: inline-flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; }
        .filter-box select { width: auto; min-width: 170px; }
        button {
            border: 1px solid var(--line);
            background: #1a2131;
            color: var(--ink);
            border-radius: 10px;
            padding: 9px 12px;
            cursor: pointer;
            font-weight: 600;
        }
        button.primary { background: var(--accent); border-color: var(--accent); color: #071a18; }
        button.secondary { background: #2a1f1a; border-color: #5a3a2a; color: #f7b955; }
        button.danger { background: #2a1717; border-color: #7a2a2a; color: #ff9b9b; }
        button:disabled { opacity: 0.55; cursor: not-allowed; }
        .status { margin-top: 10px; font-size: 14px; }
        .grid {
            margin-top: 14px;
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 12px;
            overflow: auto;
            max-height: calc(100vh - 190px);
        }
        table { width: 100%; border-collapse: collapse; min-width: 1450px; table-layout: fixed; }
        th, td { border-bottom: 1px solid var(--line); padding: 8px; text-align: left; vertical-align: top; overflow: hidden; text-overflow: ellipsis; }
        th { position: sticky; top: 0; background: #20283a; font-size: 12px; text-transform: uppercase; letter-spacing: 0.3px; }
        tr:hover td { background: #232c40; }
        th.col-file, td.col-file { width: 120px; max-width: 180px; white-space: nowrap; }
        th:nth-child(4), td:nth-child(4) { min-width: 160px; max-width: 320px; width: 18vw; } /* Sender */
        th:nth-child(5), td:nth-child(5) { min-width: 120px; max-width: 200px; width: 10vw; } /* Kategorie */
        th:nth-child(6), td:nth-child(6) { min-width: 110px; max-width: 180px; width: 9vw; } /* Kunden-Nr */
        th:nth-child(7), td:nth-child(7) { min-width: 180px; max-width: 340px; width: 20vw; } /* Titel */
        th:nth-child(8), td:nth-child(8) { min-width: 110px; max-width: 160px; width: 8vw; } /* Datum */
        th:nth-child(9), td:nth-child(9) { min-width: 180px; max-width: 340px; width: 18vw; } /* Zielvorschau */
        th:nth-child(2), td:nth-child(2) { width: 70px; max-width: 90px; }
        th:nth-child(3), td:nth-child(3) { width: 60px; max-width: 80px; }
        th:nth-child(10), td:nth-child(10) { width: 120px; max-width: 160px; }
        th:nth-child(11), td:nth-child(11) {
            min-width: 170px;
            max-width: 260px;
            width: 13vw;
        }
        td.col-file .mini {
            white-space: nowrap;
        }
        td.col-file a.filelink {
            display: inline-block;
            white-space: nowrap;
        }
        input, select {
            width: 100%;
            border: 1px solid #3a445f;
            border-radius: 8px;
            padding: 7px 8px;
            font-size: 13px;
            background: var(--field);
            color: var(--ink);
        }
        input::placeholder { color: #7f8aa3; }
        .mini { font-size: 12px; color: var(--muted); }
        .pill {
            display: inline-block;
            border: 1px solid var(--line);
            border-radius: 999px;
            padding: 2px 8px;
            font-size: 12px;
            font-weight: 600;
            background: #1a2131;
        }
        .review { color: var(--warn); border-color: #f7b955; }
        .sorted { color: var(--ok); border-color: #55d187; }
        .missing { color: var(--err); border-color: #ff6b6b; }
        .status-pending { color: #c9d4e8; border-color: #4a5b7a; }
        .status-saved { color: #f7b955; border-color: #7a5a2a; background: #2a1f1a; }
        .status-deployed { color: #55d187; border-color: #2f6b4d; background: #16271f; }
        .status-missing { color: #ff9b9b; border-color: #7a2a2a; background: #2a1717; }
        a.filelink { color: #67c7ff; text-decoration: none; }
        a.filelink:hover { text-decoration: underline; }
        .learn { display: flex; gap: 8px; font-size: 12px; color: var(--muted); flex-wrap: wrap; }
        .learn label { display: inline-flex; align-items: center; gap: 4px; }
        .row-actions { display: flex; gap: 8px; margin-top: 8px; }
        .row-actions button { padding: 6px 8px; font-size: 12px; border-radius: 8px; }
    </style>
</head>
<body>
    <div class=\"wrap\">
        <div class=\"top\">
            <h1>Dokumente zur Prüfung</h1>
            <div class=\"meta\" id=\"meta\"></div>
            <div class="activity idle" id="activity-indicator">Systemstatus: Leerlauf</div>

            </div>



                <button class=\"primary\" onclick=\"deployAll()\">Ausführung starten</button>
                <button onclick=\"window.location.href='/config'\">Konfiguration</button>
                <label class="filter-box">
                    Status-Filter
                    <select id="status-filter" onchange="applyFilter()">
                        <option value="all">Alle</option>
                        <option value="open" selected>Offen (pending/saved/missing)</option>
                        <option value="pending">Pending</option>
                        <option value="saved">Saved</option>
                        <option value="missing">Missing</option>
                        <option value="deployed">Ausgeführt</option>
                    </select>
                </label>
            </div>
            <div class=\"status\" id=\"status\"></div>
        </div>

        <div class=\"grid\">
            <table>
                <thead>
                    <tr>
                        <th class="col-file">Datei</th>
                        <th>Status</th>
                        <th>Conf</th>
                        <th>Sender</th>
                        <th>Kategorie</th>
                        <th>Kunden-Nr</th>
                        <th>Titel</th>
                        <th>Datum</th>
                        <th>Zielvorschau</th>
                        <th>Aktion</th>
                    </tr>
                </thead>
                <tbody id=\"rows\"></tbody>
            </table>
        </div>
    </div>

<script>
let DATA = { rows: [], categories: [], value_memory: {} };
let CURRENT_FILTER = 'open';

function esc(v) {
    return String(v ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
}

function status(text, cls = '') {
    const el = document.getElementById('status');
    el.textContent = text;
    el.className = 'status ' + cls;
}

function setActivityIndicator(payload) {
    const el = document.getElementById('activity-indicator');
    if (!el) {
        return;
    }
    const busy = !!payload?.activity_running;
    const label = busy ? 'Dokumentprüfung läuft' : 'Leerlauf';
    el.textContent = `Systemstatus: ${label}`;
    el.className = 'activity ' + (busy ? 'busy' : 'idle');

    const triggerBtn = document.getElementById('trigger-scan-btn');
    const stopBtn = document.getElementById('stop-scan-btn');
    if (triggerBtn) {
        triggerBtn.disabled = busy;
    }
    if (stopBtn) {
        stopBtn.disabled = !busy;
    }
}

function optionsFor(field) {
    const values = DATA.value_memory?.[field] || [];
    return values.map(v => `<option value=\"${esc(v)}\"></option>`).join('');
}

function categoryOptions(selected) {
    return DATA.categories.map(c => `<option ${c === selected ? 'selected' : ''} value=\"${esc(c)}\">${esc(c)}</option>`).join('');
}

function uiStatusLabel(status) {
    const key = String(status || 'pending').toLowerCase();
    if (key === 'pending') return 'OFFEN';
    if (key === 'saved') return 'GESPEICHERT';
    if (key === 'missing') return 'FEHLEND';
    if (key === 'deployed') return 'AUSGEFÜHRT';
    return key.toUpperCase();
}

function rowMarkup(row) {
    const badge = row.review
        ? '<span class="pill review">PRÜFEN</span>'
        : '<span class="pill sorted">BEREIT</span>';
    const missing = row.source_exists ? '' : '<div><span class="pill missing">DATEI FEHLT</span></div>';
    const statusClass = `status-${String(row.status || 'pending')}`;
    const statusLabel = uiStatusLabel(row.status);
    const deployDisabled = row.status === 'deployed' ? 'disabled' : '';

    return `<tr data-id=\"${esc(row.id)}\">
        <td class=\"col-file\">
            <a class=\"filelink\" target=\"_blank\" href=\"/file?id=${encodeURIComponent(row.id)}\">${esc(row.source_name)}</a>
            <div class=\"mini\">${esc(row.source_preview || row.source)}</div>
            <div>${badge}</div>
            ${missing}
        </td>
        <td><span class=\"pill ${statusClass}\">${esc(statusLabel)}</span></td>
        <td>${Number(row.confidence || 0).toFixed(2)}</td>
        <td>
            <input list=\"sender-mem\" name=\"sender\" value=\"${esc(row.edited.sender || '')}\" />
            <div class=\"mini\">LLM: ${esc(row.default.sender || '')}</div>
        </td>
        <td>
            <select name=\"category\">${categoryOptions(row.edited.category || 'SONSTIGES')}</select>
            <div class=\"mini\">LLM: ${esc(row.default.category || '')}</div>
        </td>
        <td>
            <input list=\"customer_number-mem\" name=\"customer_number\" value=\"${esc(row.edited.customer_number || '')}\" />
            <div class=\"mini\">LLM: ${esc(row.default.customer_number || '')}</div>
        </td>
        <td>
            <input list=\"title-mem\" name=\"title\" value=\"${esc(row.edited.title || '')}\" />
            <div class=\"mini\">LLM: ${esc(row.default.title || '')}</div>
        </td>
        <td>
            <input name=\"date\" value=\"${esc(row.edited.date || '')}\" placeholder=\"YYYY-MM-DD\" />
            <div class=\"mini\">LLM: ${esc(row.default.date || '')}</div>
        </td>
        <td class=\"mini\">${esc(row.target_preview || '')}</td>
        <td>
            <div class=\"learn\">
                <label><input type=\"checkbox\" name=\"learn_sender\" /> Sender</label>
                <label><input type=\"checkbox\" name=\"learn_category\" /> Kategorie</label>
                <label><input type=\"checkbox\" name=\"learn_customer_number\" /> Kunden-Nr</label>
                <label><input type=\"checkbox\" name=\"learn_title\" /> Titel</label>
            </div>
        </td>
        <td>
            <div class=\"row-actions\">
                <button onclick=\"saveRow('${esc(row.id)}')\" ${deployDisabled}>Speichern</button>
                <button class=\"primary\" onclick=\"deployRow('${esc(row.id)}')\" ${deployDisabled}>Ausführen</button>
                <button class=\"danger\" onclick=\"deleteRow('${esc(row.id)}')\">Löschen</button>
            </div>
        </td>
    </tr>`;
// Einzellöschfunktion
async function deleteRow(id) {
    if (!confirm('Eintrag wirklich löschen?')) return;
    status('Lösche Eintrag...');
    const res = await fetch('/api/delete-entry', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id })
    });
    const payload = await res.json();
    if (!res.ok || payload.ok === false) {
        status(payload.error || 'Löschen fehlgeschlagen', 'err');
        return;
    }
    status('Eintrag gelöscht.', 'ok');
    await reloadData();
}
}

function rowPayload(tr) {
    return {
        id: tr.dataset.id,
        edited: {
            sender: tr.querySelector('[name="sender"]').value,
            category: tr.querySelector('[name="category"]').value,
            customer_number: tr.querySelector('[name="customer_number"]').value,
            title: tr.querySelector('[name="title"]').value,
            date: tr.querySelector('[name="date"]').value,
        },
        learn: {
            sender: tr.querySelector('[name="learn_sender"]').checked,
            category: tr.querySelector('[name="learn_category"]').checked,
            customer_number: tr.querySelector('[name="learn_customer_number"]').checked,
            title: tr.querySelector('[name="learn_title"]').checked,
        }
    };
}

function collectRows() {
    const trs = [...document.querySelectorAll('#rows tr')];
    return trs.map(rowPayload);
}

function findRow(id) {
    return document.querySelector(`#rows tr[data-id="${CSS.escape(id)}"]`);
}

function matchesFilter(row) {
    const status = String(row.status || 'pending').toLowerCase();
    if (CURRENT_FILTER === 'all') {
        return true;
    }
    if (CURRENT_FILTER === 'open') {
        return status === 'pending' || status === 'saved' || status === 'missing';
    }
    return status === CURRENT_FILTER;
}

function renderRows() {
    const shownRows = (DATA.rows || []).filter(matchesFilter);
    document.getElementById('rows').innerHTML = shownRows.map(rowMarkup).join('');
    return shownRows.length;
}

function applyFilter() {
    const select = document.getElementById('status-filter');
    CURRENT_FILTER = (select?.value || 'all').toLowerCase();
    const shownCount = renderRows();
    status(`Filter aktiv: ${CURRENT_FILTER}. Angezeigt: ${shownCount}.`);
}

async function reloadData() {
    status('Lade Daten...');
    const res = await fetch('/api/pending');
    const payload = await res.json();
    DATA = payload;

    const counts = payload.counts || {};
    const openCount = (counts.pending || 0) + (counts.saved || 0) + (counts.missing || 0);
    document.getElementById('meta').textContent = `Log: ${payload.log_file} | Zustand: ${payload.state_file} | Aliase: ${payload.aliases_file} | Offen=${counts.pending || 0}, Gespeichert=${counts.saved || 0}, Fehlend=${counts.missing || 0}, Ausgeführt=${counts.deployed || 0}`;

    const shownCount = renderRows();
    const dlSender = `<datalist id=\"sender-mem\">${optionsFor('sender')}</datalist>`;
    const dlCustomer = `<datalist id=\"customer_number-mem\">${optionsFor('customer_number')}</datalist>`;
    const dlTitle = `<datalist id=\"title-mem\">${optionsFor('title')}</datalist>`;

    document.querySelectorAll('datalist').forEach(n => n.remove());
    document.body.insertAdjacentHTML('beforeend', dlSender + dlCustomer + dlTitle);
    status(`Bereit. ${openCount} offene Einträge, ${shownCount} angezeigt.`);
    await refreshScanStatus();
}

async function refreshScanStatus() {
    const res = await fetch('/api/scan-status');
    const payload = await res.json();
    if (!res.ok) {
        return;
    }
    setActivityIndicator(payload);
    if (payload.running) {
        status('Scan läuft gerade im Hintergrund...');
        return;
    }
    if (typeof payload.last_exit_code === 'number') {
        if (payload.last_exit_code === 0) {
            status('Letzter Scan erfolgreich abgeschlossen.', 'ok');
        } else {
            status(`Letzter Scan fehlgeschlagen (exit=${payload.last_exit_code}).`, 'err');
        }
    }
}

async function triggerScan() {
    status('Starte Scan...');
    const res = await fetch('/api/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({})
    });
    const payload = await res.json();

    if (!res.ok) {
        const errors = (payload.errors || []).join(' | ');
        status(errors || payload.error || 'Scan konnte nicht gestartet werden.', 'err');
        return;
    }

    if (payload.running) {
        status('Ein Scan läuft bereits.', 'warn');
        return;
    }

    status('Scan gestartet. Ergebnis erscheint nach Abschluss im Status.', 'ok');
    setTimeout(reloadData, 2000);
}

async function stopScan() {
    status('Stoppe laufende Überprüfung...');
    const res = await fetch('/api/scan-stop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({})
    });
    const payload = await res.json();

    if (!res.ok || payload.ok === false) {
        const details = payload.details ? ` (${payload.details})` : '';
        status((payload.error || payload.message || 'Stop fehlgeschlagen.') + details, 'err');
        return;
    }

    if (payload.stopped) {
        status(payload.message || 'Überprüfung wurde gestoppt.', 'ok');
    } else {
        status(payload.message || 'Keine laufende Überprüfung gefunden.', 'warn');
    }
    await refreshScanStatus();
}

async function saveEdits() {
    const rows = collectRows();
    const res = await fetch('/api/save-edits', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rows })
    });
    const payload = await res.json();
    status(`Gespeichert: ${payload.updated}`);
    await reloadData();
}

async function saveRow(id) {
    const tr = findRow(id);
    if (!tr) {
        status('Zeile nicht gefunden.');
        return;
    }
    const res = await fetch('/api/save-edits', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rows: [rowPayload(tr)] })
    });
    const payload = await res.json();
    status(`Zeile gespeichert: ${payload.updated}`);
    await reloadData();
}

async function deployAll() {
    const rows = collectRows();
    if (!rows.length) {
        status('Keine offenen Einträge.');
        return;
    }
    status('Ausführung läuft...');
    const res = await fetch('/api/deploy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rows })
    });
    const payload = await res.json();
    const msg = `Ausführung fertig: verschoben=${payload.applied}, fehlend=${payload.missing}, gelernt=${payload.learned_aliases}, fehler=${(payload.errors || []).length}`;
    status(msg);
    await reloadData();
}

async function deployRow(id) {
    const tr = findRow(id);
    if (!tr) {
        status('Zeile nicht gefunden.');
        return;
    }
    status('Ausführung für Zeile läuft...');
    const res = await fetch('/api/deploy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rows: [rowPayload(tr)] })
    });
    const payload = await res.json();
    const msg = `Zeile ausgeführt: verschoben=${payload.applied}, fehlend=${payload.missing}, gelernt=${payload.learned_aliases}, fehler=${(payload.errors || []).length}`;
    status(msg);
    await reloadData();
}

reloadData();
setInterval(refreshScanStatus, 4000);
</script>
</body>
</html>
"""


CONFIG_PAGE = """<!doctype html>
<html lang=\"de\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Konfiguration - Dokumente zur Prüfung</title>
    <style>
        :root {
            --bg: #0f1117;
            --bg2: #151926;
            --card: #1c2233;
            --card2: #1a2030;
            --ink: #e8edf7;
            --muted: #9aa6bd;
            --accent: #23c4a8;
            --line: #2b3449;
            --ok: #55d187;
            --warn: #f7b955;
            --err: #ff6b6b;
            --field: #111827;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: \"IBM Plex Sans\", \"Segoe UI\", sans-serif;
            color: var(--ink);
            background: radial-gradient(circle at top right, #20263a 0%, var(--bg) 42%), var(--bg2);
        }
        .wrap { max-width: 900px; margin: 0 auto; padding: 20px; }
        .card {
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 12px;
            padding: 16px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.35);
        }
        h1 { margin: 0 0 8px 0; font-size: 24px; }
        p.meta { margin: 0 0 14px 0; color: var(--muted); font-size: 13px; }
        .section {
            border: 1px solid var(--line);
            border-radius: 10px;
            padding: 12px;
            margin-top: 12px;
            background: var(--card2);
        }
        .section h2 {
            margin: 0 0 10px 0;
            font-size: 14px;
            letter-spacing: 0.2px;
            color: #c4d1eb;
            text-transform: uppercase;
        }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
        .grid.stack { grid-template-columns: 1fr; }
        .field { display: flex; flex-direction: column; gap: 6px; }
        .field.wide { grid-column: 1 / -1; }
        label { font-size: 13px; color: #cad5ea; font-weight: 600; }
        input, select {
            width: 100%;
            border: 1px solid #3a445f;
            border-radius: 8px;
            padding: 9px 10px;
            font-size: 14px;
            background: var(--field);
            color: var(--ink);
        }
        input::placeholder { color: #7f8aa3; }
        .actions { margin-top: 14px; display: flex; gap: 10px; flex-wrap: wrap; }
        button {
            border: 1px solid var(--line);
            background: #1a2131;
            color: var(--ink);
            border-radius: 10px;
            padding: 9px 12px;
            cursor: pointer;
            font-weight: 600;
        }
        button.primary { background: var(--accent); border-color: var(--accent); color: #071a18; }
        .status { margin-top: 12px; font-size: 14px; min-height: 18px; }
        .status.ok { color: var(--ok); }
        .status.warn { color: var(--warn); }
        .status.err { color: var(--err); }
        @media (max-width: 760px) {
            .grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div class=\"wrap\">
        <div class=\"card\">
            <h1>Konfiguration</h1>
            <p class=\"meta\" id=\"meta\"></p>

            <div class=\"section\">
                <h2>In-/Outbox</h2>
                <div class=\"grid\">
                    <div class=\"field wide\">
                        <label for=\"input\">Inbox (service.input)</label>
                        <input id=\"input\" placeholder=\"./inbox\" />
                    </div>

                    <div class=\"field wide\">
                        <label for=\"output\">Outbox (service.output)</label>
                        <input id=\"output\" placeholder=\"./output\" />
                    </div>
                </div>
            </div>

            <div class=\"section\">
                <h2>Ausführungplan</h2>
                <div class=\"grid stack\">
                    <div class=\"field\">
                        <label for=\"model\">Modell (service.model)</label>
                        <input id=\"model\" placeholder=\"qwen2.5:7b-instruct\" />
                    </div>

                    <div class=\"field\">
                        <label for=\"schedule-mode\">Scheduler-Modus (service.schedule_mode)</label>
                        <select id=\"schedule-mode\">
                            <option value=\"interval\">interval</option>
                            <option value=\"inbox-trigger\">inbox-trigger</option>
                            <option value=\"daily\">daily</option>
                        </select>
                    </div>

                    <div class=\"field\">
                        <label for=\"interval\">Scan-Intervall Sekunden (service.interval_seconds)</label>
                        <input id=\"interval\" type=\"number\" min=\"30\" step=\"1\" placeholder=\"300\" />
                    </div>

                    <div class=\"field\">
                        <label for=\"inbox-poll\">Inbox Poll Sekunden (service.inbox_poll_seconds)</label>
                        <input id=\"inbox-poll\" type=\"number\" min=\"1\" step=\"1\" placeholder=\"2\" />
                    </div>

                    <div class=\"field\">
                        <label for=\"daily-time\">Tägliche Startzeit (service.daily_time, HH:MM)</label>
                        <input id=\"daily-time\" placeholder=\"02:00\" />
                    </div>
                </div>
            </div>

            <div class=\"section\">
                <h2>Sicherheit</h2>
                <div class=\"grid\">
                    <div class=\"field\">
                        <label for=\"new-password\">Neues Web-Passwort</label>
                        <input id=\"new-password\" type=\"password\" autocomplete=\"new-password\" placeholder=\"leer lassen = unverändert\" />
                    </div>

                    <div class=\"field\">
                        <label for=\"new-password-confirm\">Neues Passwort bestätigen</label>
                        <input id=\"new-password-confirm\" type=\"password\" autocomplete=\"new-password\" placeholder=\"Wiederholen\" />
                    </div>
                </div>
            </div>

            <div class=\"section\">
                <h2>Ollama</h2>
                <div class=\"grid\">
                    <div class=\"field\">
                        <label for=\"ollama-timeout\">Ollama Timeout Sekunden</label>
                        <input id=\"ollama-timeout\" type=\"number\" min=\"1\" step=\"1\" placeholder=\"1800\" />
                    </div>

                    <div class=\"field\">
                        <label for=\"ollama-retries\">Ollama Retries</label>
                        <input id=\"ollama-retries\" type=\"number\" min=\"0\" step=\"1\" placeholder=\"0\" />
                    </div>

                    <div class=\"field\">
                        <label for=\"max-text-chars\">Max. Textzeichen pro Anfrage</label>
                        <input id=\"max-text-chars\" type=\"number\" min=\"100\" step=\"100\" placeholder=\"6000\" />
                    </div>

                    <div class=\"field\">
                        <label for=\"process-nice\">Process Nice</label>
                        <input id=\"process-nice\" type=\"number\" min=\"0\" step=\"1\" placeholder=\"5\" />
                    </div>

                    <div class=\"field\">
                        <label for=\"max-cpu-threads\">Max CPU Threads (0 = kein Limit)</label>
                        <input id=\"max-cpu-threads\" type=\"number\" min=\"0\" step=\"1\" placeholder=\"4\" />
                    </div>

                    <div class=\"field\">
                        <label for=\"ollama-num-thread\">Ollama Num Thread (0 = Default)</label>
                        <input id=\"ollama-num-thread\" type=\"number\" min=\"0\" step=\"1\" placeholder=\"4\" />
                    </div>

                    <div class=\"field\">
                        <label for=\"sleep-between-files\">Pause zwischen Dateien (Sekunden)</label>
                        <input id=\"sleep-between-files\" type=\"number\" min=\"0\" step=\"0.1\" placeholder=\"0.4\" />
                    </div>
                </div>
            </div>

            <div class=\"section\">
                <h2>Update</h2>
                <div class=\"grid\">
                    <div class=\"field wide\">
                        <label for=\"update-info\">Repository-Status</label>
                        <input id=\"update-info\" readonly value=\"Noch nicht geprüft\" />
                    </div>
                </div>
                <div class=\"actions\">
                    <button onclick=\"checkForUpdate()\">Auf Update prüfen</button>
                    <button id=\"run-update-btn\" class=\"primary\" onclick=\"runUpdate()\" disabled>Update durchführen</button>
                    <button onclick=\"restartService()\">Dienst neu starten</button>
                </div>
            </div>


            <div class="actions">
                <button class="danger" style="background:#ff6b6b; color:#fff; border-color:#ff6b6b" onclick="resetReviewState()">Review zurücksetzen</button>
            </div>

            <script>
            async function resetReviewState() {
                if (!confirm('Wirklich alle Review-Einträge löschen?')) return;
                status('Setze Review-State zurück...');
                const res = await fetch('/api/reset-review-state', { method: 'POST' });
                const payload = await res.json();
                if (!res.ok || payload.ok === false) {
                    status(payload.error || 'Reset fehlgeschlagen', 'err');
                    return;
                }
                status('Review-State geleert.', 'ok');
            }
            </script>
                <button class=\"primary\" onclick=\"deployAll()\">Ausführung starten</button>
                <button onclick=\"window.location.href='/config'\">Konfiguration</button>
                <label class="filter-box">
                    Status-Filter
                    <select id="status-filter" onchange="applyFilter()">
                        <option value="all">Alle</option>
                        <option value="open" selected>Offen (pending/saved/missing)</option>
                        <option value="pending">Pending</option>
                        <option value="saved">Saved</option>
                        <option value="missing">Missing</option>
                        <option value="deployed">Ausgeführt</option>
                    </select>
                </label>
            </div>
            <div class=\"status\" id=\"status\"></div>
        </div>
    </div>

<script>
function status(text, cls = '') {
    const el = document.getElementById('status');
    el.textContent = text;
    el.className = 'status ' + cls;
}

function byId(id) {
    return document.getElementById(id);
}

function setUpdateUI(payload) {
    const info = byId('update-info');
    const btn = byId('run-update-btn');
    if (!info || !btn) {
        return;
    }
    if (!payload || payload.supported === false) {
        info.value = payload?.message || 'Update-Prüfung nicht verfügbar';
        btn.disabled = true;
        return;
    }

    const behind = Number(payload.behind || 0);
    const ahead = Number(payload.ahead || 0);
    const upstream = payload.upstream || '-';
    const text = `${payload.message || 'Status unbekannt'} | Upstream=${upstream} | behind=${behind} | ahead=${ahead}`;
    info.value = text;
    btn.disabled = !payload.available;
}

async function checkForUpdate() {
    const info = byId('update-info');
    if (info) {
        info.value = 'Prüfe auf Updates...';
    }
    const res = await fetch('/api/update-status');
    const payload = await res.json();
    setUpdateUI(payload);
    if (!res.ok) {
        status(payload.error || payload.message || 'Update-Prüfung fehlgeschlagen.', 'err');
        return;
    }
    status(payload.message || 'Update-Status geprüft.', payload.available ? 'warn' : 'ok');
}

async function runUpdate() {
    const btn = byId('run-update-btn');
    if (btn) {
        btn.disabled = true;
    }
    status('Führe Update durch...');
    const res = await fetch('/api/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({})
    });
    const payload = await res.json();
    setUpdateUI(payload);

    if (!res.ok || payload.ok === false) {
        const details = payload.details ? ` (${payload.details})` : '';
        status((payload.message || payload.error || 'Update fehlgeschlagen') + details, 'err');
        return;
    }

    if (payload.updated) {
        status('Update erfolgreich. Bitte Dienst neu starten.', 'ok');
    } else {
        status(payload.message || 'Bereits aktuell.', 'ok');
    }
    await checkForUpdate();
}

async function restartService() {
    status('Starte Dienst neu...');
    const res = await fetch('/api/service-restart', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({})
    });
    const payload = await res.json();
    if (!res.ok || payload.ok === false) {
        const details = payload.details ? ` (${payload.details})` : '';
        status((payload.error || payload.message || 'Dienstneustart fehlgeschlagen') + details, 'err');
        return;
    }
    status(payload.message || 'Dienst wurde neu gestartet. Seite wird neu geladen...', 'ok');
    setTimeout(() => window.location.reload(), 1200);
}

async function loadConfig() {
    status('Lade Konfiguration...');
    const res = await fetch('/api/config');
    const payload = await res.json();

    if (!res.ok) {
        status(payload.error || 'Konfiguration konnte nicht geladen werden.', 'err');
        return;
    }

    const service = payload.service || {};
    const organize = service.organize_options || {};
    byId('input').value = service.input || '';
    byId('output').value = service.output || '';
    byId('model').value = service.model || '';
    byId('schedule-mode').value = service.schedule_mode || 'interval';
    byId('interval').value = service.interval_seconds || 300;
    byId('daily-time').value = service.daily_time || '02:00';
    byId('inbox-poll').value = service.inbox_poll_seconds || 2;
    byId('ollama-timeout').value = organize.ollama_timeout ?? 1800;
    byId('ollama-retries').value = organize.ollama_retries ?? 0;
    byId('max-text-chars').value = organize.max_text_chars ?? 6000;
    byId('process-nice').value = organize.process_nice ?? 5;
    byId('max-cpu-threads').value = organize.max_cpu_threads ?? 4;
    byId('ollama-num-thread').value = organize.ollama_num_thread ?? 4;
    byId('sleep-between-files').value = organize.sleep_between_files ?? 0.4;
    byId('new-password').value = '';
    byId('new-password-confirm').value = '';
    byId('meta').textContent = `Config-Datei: ${payload.config_path} | Passwortdatei: ${payload.auth_password_file || '-'}`;
    status('Konfiguration geladen.', 'ok');
    await checkForUpdate();
}

async function saveConfig() {
    const newPassword = byId('new-password').value;
    const newPasswordConfirm = byId('new-password-confirm').value;

    const body = {
        service: {
            input: byId('input').value,
            output: byId('output').value,
            model: byId('model').value,
            schedule_mode: byId('schedule-mode').value,
            interval_seconds: byId('interval').value,
            daily_time: byId('daily-time').value,
            inbox_poll_seconds: byId('inbox-poll').value
        },
        organize_options: {
            ollama_timeout: byId('ollama-timeout').value,
            ollama_retries: byId('ollama-retries').value,
            max_text_chars: byId('max-text-chars').value,
            process_nice: byId('process-nice').value,
            max_cpu_threads: byId('max-cpu-threads').value,
            ollama_num_thread: byId('ollama-num-thread').value,
            sleep_between_files: byId('sleep-between-files').value
        },
        auth: {
            new_password: newPassword,
            new_password_confirm: newPasswordConfirm
        }
    };

    status('Speichere Konfiguration...');
    const res = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    });
    const payload = await res.json();

    if (!res.ok) {
        const errors = (payload.errors || []).join(' | ');
        status(errors || payload.error || 'Speichern fehlgeschlagen.', 'err');
        return;
    }

    const note = payload.restart_required ? ' Bitte Dienst neu starten.' : '';
    status('Gespeichert.' + note, 'ok');
    byId('new-password').value = '';
    byId('new-password-confirm').value = '';
    byId('meta').textContent = `Config-Datei: ${payload.config_path} | Passwortdatei: ${payload.auth_password_file || '-'}`;
}

loadConfig();
</script>
</body>
</html>
"""


LOGIN_PAGE = """<!doctype html>
<html lang=\"de\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Login - Dokumente zur Prüfung</title>
    <style>
        :root {
            --bg: #0f1320;
            --bg2: #111827;
            --card: #131b2d;
            --line: #2b3550;
            --ink: #eef3ff;
            --muted: #9aa7c0;
            --accent: #34d399;
            --err: #ff6b6b;
            --field: #0e1526;
        }
        body {
            margin: 0;
            min-height: 100vh;
            display: grid;
            place-items: center;
            font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
            background: radial-gradient(circle at top right, #20263a 0%, var(--bg) 42%), var(--bg2);
            color: var(--ink);
        }
        .card {
            width: min(420px, 92vw);
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 14px;
            padding: 20px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.35);
        }
        h1 { margin: 0 0 12px 0; font-size: 24px; }
        p { margin: 0 0 12px 0; color: var(--muted); }
        input {
            width: 100%;
            border: 1px solid #3a445f;
            border-radius: 10px;
            padding: 10px 12px;
            font-size: 14px;
            margin-bottom: 12px;
            background: var(--field);
            color: var(--ink);
        }
        input::placeholder {
            color: #7f8aa3;
        }
        button {
            width: 100%;
            border: 1px solid var(--accent);
            background: var(--accent);
            color: #071a18;
            border-radius: 10px;
            padding: 10px 12px;
            font-weight: 700;
            cursor: pointer;
        }
        .err { margin-top: 10px; color: var(--err); font-size: 13px; min-height: 18px; }
    </style>
</head>
<body>
    <form class=\"card\" onsubmit=\"return doLogin(event)\">
        <h1>Login</h1>
        <p>Bitte Passwort eingeben.</p>
        <input id=\"pw\" type=\"password\" autocomplete=\"current-password\" placeholder=\"Passwort\" required />
        <button type=\"submit\">Anmelden</button>
        <div class=\"err\" id=\"err\"></div>
    </form>

<script>
async function doLogin(event) {
    event.preventDefault();
    const pw = document.getElementById('pw').value;
    const err = document.getElementById('err');
    err.textContent = '';

    const res = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: pw })
    });

    if (res.ok) {
        window.location.href = '/';
        return false;
    }

    err.textContent = 'Login fehlgeschlagen.';
    return false;
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/reset-review-state":
            # Leere review_state.json
            try:
                state_file = self.store.paths.state_file
                empty = {"entries": {}, "value_memory": {"sender": [], "category": [], "customer_number": [], "title": []}}
                with open(state_file, "w", encoding="utf-8") as f:
                    json.dump(empty, f, ensure_ascii=False, indent=2)
                self._json_response({"ok": True})
            except Exception as exc:
                self._json_response({"ok": False, "error": str(exc)}, status=500)
            return
        # ...existing code for other POST endpoints...

    store: ReviewStore
    auth: PasswordAuth
    config_path: Path
    scan_lock = threading.Lock()
    scan_proc: Optional[subprocess.Popen[bytes]] = None
    scan_last_exit_code: Optional[int] = None
    scan_last_finished_at: float = 0.0
    update_lock = threading.Lock()

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cache-Control", "no-store")

    def _parse_cookies(self) -> dict[str, str]:
        raw = self.headers.get("Cookie", "")
        cookies: dict[str, str] = {}
        for chunk in raw.split(";"):
            if "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            cookies[key.strip()] = value.strip()
        return cookies

    def _session_token(self) -> str:
        return self._parse_cookies().get("oda_session", "")

    def _is_authenticated(self) -> bool:
        if not self.auth.enabled:
            return True
        return self.auth.is_valid(self._session_token())

    def _redirect(self, path: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self._send_security_headers()
        self.send_header("Location", path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _require_auth(self, is_api: bool) -> bool:
        if self._is_authenticated():
            return True
        if is_api:
            self._json_response({"error": "unauthorized"}, status=401)
        else:
            self._redirect("/login")
        return False

    def _json_response(self, payload: Any, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _text_response(self, payload: str, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        raw = payload.encode("utf-8")
        self.send_response(status)
        self._send_security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        data = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(data.decode("utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _load_runtime_config(self) -> tuple[dict[str, Any], list[str]]:
        cfg = load_config(str(self.config_path), self.config_path.parent)
        if cfg.errors:
            return {}, cfg.errors
        return cfg.data if isinstance(cfg.data, dict) else {}, []

    def _write_runtime_config(self, config: dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _resolve_auth_password_file(self, config: dict[str, Any]) -> Path:
        service = get_section(config, "service")
        review = get_section(config, "review_web")

        raw = str(service.get("auth_password_file", "")).strip()
        if not raw:
            raw = str(review.get("auth_password_file", "")).strip()
        if not raw:
            raw = ".review_web_password"

        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.config_path.parent / p
        return p.resolve()

    def _resolve_project_dir(self, config: dict[str, Any]) -> Path:
        service = get_section(config, "service")
        project_dir_raw = str(service.get("project_dir", "")).strip()
        project_dir = Path(project_dir_raw).expanduser() if project_dir_raw else self.config_path.parent
        if not project_dir.is_absolute():
            project_dir = self.config_path.parent / project_dir
        return project_dir.resolve()

    def _run_git(self, project_dir: Path, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(project_dir)] + args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _restart_user_service(self) -> dict[str, Any]:
        if shutil.which("systemctl") is None:
            return {
                "ok": False,
                "message": "systemctl nicht gefunden",
            }

        cmd = ["systemctl", "--user", "restart", "ollama-document-assistant.service"]
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception as exc:
            return {
                "ok": False,
                "message": f"Dienstneustart fehlgeschlagen: {exc}",
            }

        if proc.returncode != 0:
            return {
                "ok": False,
                "message": "Dienstneustart fehlgeschlagen",
                "details": (proc.stderr or proc.stdout).strip(),
            }

        return {
            "ok": True,
            "message": "Dienst wurde neu gestartet",
        }

    def _update_status_payload(self) -> dict[str, Any]:
        config, load_errors = self._load_runtime_config()
        if load_errors:
            return {"supported": False, "available": False, "error": "config_load_failed", "errors": load_errors}

        project_dir = self._resolve_project_dir(config)
        if shutil.which("git") is None:
            return {
                "supported": False,
                "available": False,
                "message": "git nicht gefunden",
                "project_dir": str(project_dir),
            }

        probe = self._run_git(project_dir, ["rev-parse", "--is-inside-work-tree"])
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            return {
                "supported": False,
                "available": False,
                "message": "Kein Git-Repository im Projektordner",
                "project_dir": str(project_dir),
            }

        fetch = self._run_git(project_dir, ["fetch", "--prune"], timeout=60)
        if fetch.returncode != 0:
            return {
                "supported": True,
                "available": False,
                "message": "Fetch fehlgeschlagen",
                "project_dir": str(project_dir),
                "details": (fetch.stderr or fetch.stdout).strip(),
            }

        upstream = self._run_git(project_dir, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
        if upstream.returncode != 0:
            return {
                "supported": True,
                "available": False,
                "message": "Kein Upstream-Branch konfiguriert",
                "project_dir": str(project_dir),
            }

        upstream_name = upstream.stdout.strip()
        counts = self._run_git(project_dir, ["rev-list", "--left-right", "--count", "HEAD...@{u}"])
        if counts.returncode != 0:
            return {
                "supported": True,
                "available": False,
                "message": "Vergleich mit Upstream fehlgeschlagen",
                "project_dir": str(project_dir),
                "upstream": upstream_name,
                "details": (counts.stderr or counts.stdout).strip(),
            }

        parts = counts.stdout.strip().split()
        ahead = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
        behind = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        available = behind > 0
        message = "Update verfügbar" if available else "Bereits aktuell"

        return {
            "supported": True,
            "available": available,
            "message": message,
            "project_dir": str(project_dir),
            "upstream": upstream_name,
            "ahead": ahead,
            "behind": behind,
        }

    def _build_scan_command(self, config: dict[str, Any]) -> tuple[list[str], Path]:
        service = get_section(config, "service")
        project_dir_raw = str(service.get("project_dir", "")).strip()
        project_dir = Path(project_dir_raw).expanduser() if project_dir_raw else self.config_path.parent
        if not project_dir.is_absolute():
            project_dir = self.config_path.parent / project_dir
        project_dir = project_dir.resolve()

        input_path = str(service.get("input", "")).strip()
        if not input_path:
            raise ValueError("service.input is required to run scan")

        field_aliases_file = str(service.get("field_aliases_file", "")).strip() or "field_aliases.json"
        python_bin = str(service.get("python", "")).strip() or sys.executable
        model = str(service.get("model", "")).strip()
        output = str(service.get("output", "")).strip()
        extra_args_raw = service.get("organize_extra_args", [])
        extra_args = [str(v) for v in extra_args_raw] if isinstance(extra_args_raw, list) else []

        cmd = [
            python_bin,
            str(project_dir / "organize.py"),
            "--input",
            input_path,
            "--dry-run",
            "--field-aliases-file",
            field_aliases_file,
        ]
        if model:
            cmd.extend(["--model", model])
        if output:
            cmd.extend(["--output-root", output])
        cmd.extend(extra_args)
        return cmd, project_dir

    def _organize_options_from_args(self, extra_args: list[str]) -> dict[str, Any]:
        values: dict[str, Any] = {
            "ollama_timeout": 1800,
            "ollama_retries": 0,
            "max_text_chars": 6000,
            "process_nice": 5,
            "max_cpu_threads": 4,
            "ollama_num_thread": 4,
            "sleep_between_files": 0.4,
        }

        mapping = {
            "--ollama-timeout": ("ollama_timeout", int),
            "--ollama-retries": ("ollama_retries", int),
            "--max-text-chars": ("max_text_chars", int),
            "--process-nice": ("process_nice", int),
            "--max-cpu-threads": ("max_cpu_threads", int),
            "--ollama-num-thread": ("ollama_num_thread", int),
            "--sleep-between-files": ("sleep_between_files", float),
        }

        args = [str(v) for v in extra_args]
        i = 0
        while i < len(args):
            flag = args[i]
            spec = mapping.get(flag)
            if spec and i + 1 < len(args):
                key, caster = spec
                raw = args[i + 1]
                try:
                    values[key] = caster(raw)
                except Exception:
                    pass
                i += 2
                continue
            i += 1

        return values

    def _organize_args_from_options(self, options: dict[str, Any]) -> list[str]:
        return [
            "--ollama-timeout",
            str(int(options.get("ollama_timeout", 1800))),
            "--ollama-retries",
            str(int(options.get("ollama_retries", 0))),
            "--max-text-chars",
            str(int(options.get("max_text_chars", 6000))),
            "--process-nice",
            str(int(options.get("process_nice", 5))),
            "--max-cpu-threads",
            str(int(options.get("max_cpu_threads", 4))),
            "--ollama-num-thread",
            str(int(options.get("ollama_num_thread", 4))),
            "--sleep-between-files",
            str(float(options.get("sleep_between_files", 0.4))),
        ]

    def _detect_organize_activity(self) -> bool:
        config, load_errors = self._load_runtime_config()
        if load_errors:
            return False

        project_dir = self._resolve_project_dir(config)
        organize_path = str((project_dir / "organize.py").resolve())
        try:
            proc = subprocess.run(
                ["ps", "-eo", "args"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            return False

        if proc.returncode != 0:
            return False

        for line in proc.stdout.splitlines():
            if "organize.py" not in line:
                continue
            if organize_path in line:
                return True
        return False

    def _find_external_organize_pids(self) -> list[int]:
        config, load_errors = self._load_runtime_config()
        if load_errors:
            return []

        project_dir = self._resolve_project_dir(config)
        organize_path = str((project_dir / "organize.py").resolve())
        try:
            proc = subprocess.run(
                ["ps", "-eo", "pid=,args="],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            return []

        if proc.returncode != 0:
            return []

        current_pid = os.getpid()
        pids: list[int] = []
        for line in proc.stdout.splitlines():
            match = re.match(r"\s*(\d+)\s+(.*)", line)
            if not match:
                continue
            pid = int(match.group(1))
            args = match.group(2)
            if pid == current_pid:
                continue
            if "organize.py" not in args:
                continue
            if organize_path in args:
                pids.append(pid)
        return pids

    def _stop_scan_process(self) -> dict[str, Any]:
        manual_pid: Optional[int] = None
        manual_stopped = False
        with self.scan_lock:
            proc = self.scan_proc
            if proc is not None:
                rc = proc.poll()
                if rc is None:
                    manual_pid = proc.pid
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                    except Exception as exc:
                        return {"ok": False, "stopped": False, "error": "manual_stop_failed", "details": str(exc)}
                    self.scan_last_exit_code = proc.returncode if proc.returncode is not None else -signal.SIGTERM
                    self.scan_last_finished_at = time.time()
                    self.scan_proc = None
                    manual_stopped = True
                else:
                    self.scan_last_exit_code = rc
                    self.scan_last_finished_at = time.time()
                    self.scan_proc = None

        external_pids = self._find_external_organize_pids()
        external_stopped: list[int] = []
        for pid in external_pids:
            try:
                os.kill(pid, signal.SIGTERM)
                external_stopped.append(pid)
            except ProcessLookupError:
                continue
            except PermissionError:
                return {
                    "ok": False,
                    "stopped": manual_stopped,
                    "error": "permission_denied",
                    "details": f"Keine Berechtigung zum Stoppen von PID {pid}",
                }
            except Exception as exc:
                return {"ok": False, "stopped": manual_stopped, "error": "external_stop_failed", "details": str(exc)}

        if manual_stopped or external_stopped:
            message = "Überprüfung wurde gestoppt"
            payload: dict[str, Any] = {
                "ok": True,
                "stopped": True,
                "message": message,
                "manual_stopped": manual_stopped,
                "external_stopped": len(external_stopped),
            }
            if manual_pid is not None:
                payload["manual_pid"] = manual_pid
            if external_stopped:
                payload["external_pids"] = external_stopped
            return payload

        return {"ok": True, "stopped": False, "message": "Keine laufende Überprüfung gefunden"}

    def _scan_status_payload(self) -> dict[str, Any]:
        manual_running = False
        with self.scan_lock:
            proc = self.scan_proc
            if proc is not None:
                rc = proc.poll()
                if rc is None:
                    manual_running = True
                    manual_pid = proc.pid
                else:
                    self.scan_last_exit_code = rc
                    self.scan_last_finished_at = time.time()
                    self.scan_proc = None
                    manual_pid = None
            else:
                manual_pid = None

        external_running = self._detect_organize_activity()
        activity_running = manual_running or external_running

        payload = {
            "running": manual_running,
            "external_running": external_running,
            "last_exit_code": self.scan_last_exit_code,
            "last_finished_at": self.scan_last_finished_at,
            "activity_running": activity_running,
            "activity_state": "busy" if activity_running else "idle",
        }
        if manual_pid is not None and manual_running:
            payload["pid"] = manual_pid

        return payload

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/login":
            if not self.auth.enabled:
                self._redirect("/")
                return
            if self._is_authenticated():
                self._redirect("/")
                return
            self._text_response(LOGIN_PAGE)
            return

        if parsed.path == "/":
            if not self._require_auth(is_api=False):
                return
            self._text_response(HTML_PAGE)
            return

        if parsed.path == "/config":
            if not self._require_auth(is_api=False):
                return
            self._text_response(CONFIG_PAGE)
            return

        if parsed.path == "/api/pending":
            if not self._require_auth(is_api=True):
                return
            self._json_response(self.store.list_entries())
            return

        if parsed.path == "/api/scan-status":
            if not self._require_auth(is_api=True):
                return
            self._json_response(self._scan_status_payload())
            return

        if parsed.path == "/api/update-status":
            if not self._require_auth(is_api=True):
                return
            self._json_response(self._update_status_payload())
            return

        if parsed.path == "/api/config":
            if not self._require_auth(is_api=True):
                return
            config, errors = self._load_runtime_config()
            if errors:
                self._json_response({"error": "config_load_failed", "errors": errors}, status=500)
                return
            service = get_section(config, "service")
            organize_extra_args = [str(v) for v in service.get("organize_extra_args", [])] if isinstance(service.get("organize_extra_args", []), list) else []
            organize_options = self._organize_options_from_args(organize_extra_args)
            self._json_response(
                {
                    "config_path": str(self.config_path),
                    "auth_password_file": str(self._resolve_auth_password_file(config)),
                    "service": {
                        "input": str(service.get("input", "")).strip(),
                        "output": str(service.get("output", "")).strip(),
                        "model": str(service.get("model", "")).strip(),
                        "schedule_mode": str(service.get("schedule_mode", "interval")).strip() or "interval",
                        "interval_seconds": int(service.get("interval_seconds", 300) or 300),
                        "daily_time": str(service.get("daily_time", "02:00")).strip() or "02:00",
                        "inbox_poll_seconds": int(service.get("inbox_poll_seconds", 2) or 2),
                        "organize_options": organize_options,
                    },
                }
            )
            return

        if parsed.path == "/api/logout":
            token = self._session_token()
            self.auth.clear_session(token)
            self.send_response(HTTPStatus.SEE_OTHER)
            self._send_security_headers()
            self.send_header("Set-Cookie", "oda_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if parsed.path == "/file":
            if not self._require_auth(is_api=False):
                return
            params = parse_qs(parsed.query)
            item_id = (params.get("id") or [""])[0]
            path = self.store.file_path_for_id(item_id)
            if path is None:
                self._text_response("Not found", status=404, content_type="text/plain; charset=utf-8")
                return

            try:
                data = path.read_bytes()
            except Exception:
                self._text_response("Read error", status=500, content_type="text/plain; charset=utf-8")
                return

            content_type, _ = mimetypes.guess_type(str(path))
            self.send_response(HTTPStatus.OK)
            self._send_security_headers()
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f"inline; filename={path.name}")
            self.end_headers()
            self.wfile.write(data)
            return

        self._text_response("Not found", status=404, content_type="text/plain; charset=utf-8")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        payload = self._read_json()

        if parsed.path == "/api/login":
            if not self.auth.enabled:
                self._json_response({"ok": True, "auth": "disabled"})
                return
            password = str(payload.get("password", ""))
            token = self.auth.create_session(password)
            if not token:
                self._json_response({"error": "invalid_credentials"}, status=401)
                return

            self.send_response(HTTPStatus.OK)
            self._send_security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", "oda_session=" + token + "; Path=/; HttpOnly; SameSite=Strict")
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if not self._require_auth(is_api=True):
            return


        if parsed.path == "/api/delete-entry":
            entry_id = str(payload.get("id", "")).strip()
            if not entry_id:
                self._json_response({"ok": False, "error": "Kein Eintrag angegeben"}, status=400)
                return
            with self.store.lock:
                if entry_id in self.store.state["entries"]:
                    del self.store.state["entries"][entry_id]
                    self.store._save_state()
                    self._json_response({"ok": True})
                else:
                    self._json_response({"ok": False, "error": "Eintrag nicht gefunden"}, status=404)
            return

        if parsed.path == "/api/save-edits":
            rows = payload.get("rows", []) if isinstance(payload.get("rows"), list) else []
            self._json_response(self.store.save_edits(rows))
            return

        if parsed.path == "/api/deploy":
            rows = payload.get("rows", []) if isinstance(payload.get("rows"), list) else []
            self._json_response(self.store.deploy(rows))
            return

        if parsed.path == "/api/scan":
            status_payload = self._scan_status_payload()
            if status_payload.get("running"):
                self._json_response(status_payload)
                return

            config, load_errors = self._load_runtime_config()
            if load_errors:
                self._json_response({"error": "config_load_failed", "errors": load_errors}, status=500)
                return

            try:
                cmd, project_dir = self._build_scan_command(config)
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(project_dir),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:
                self._json_response({"error": f"scan_start_failed: {exc}"}, status=500)
                return

            with self.scan_lock:
                self.scan_proc = proc

            self._json_response({"ok": True, "running": False, "pid": proc.pid})
            return

        if parsed.path == "/api/scan-stop":
            result = self._stop_scan_process()
            self._json_response(result, status=200 if result.get("ok") else 500)
            return

        if parsed.path == "/api/update":
            with self.update_lock:
                status_payload = self._update_status_payload()
                if not status_payload.get("supported"):
                    self._json_response(status_payload, status=400)
                    return
                if not status_payload.get("available"):
                    self._json_response({"ok": True, "updated": False, **status_payload})
                    return

                project_dir = Path(str(status_payload.get("project_dir", "")).strip() or self.config_path.parent)
                pull = self._run_git(project_dir, ["pull", "--ff-only"], timeout=120)
                if pull.returncode != 0:
                    self._json_response(
                        {
                            "ok": False,
                            "updated": False,
                            "error": "update_failed",
                            "message": "Update konnte nicht durchgefuehrt werden",
                            "details": (pull.stderr or pull.stdout).strip(),
                        },
                        status=500,
                    )
                    return

                self._json_response(
                    {
                        "ok": True,
                        "updated": True,
                        "message": "Update erfolgreich eingespielt",
                        "restart_required": True,
                        "details": pull.stdout.strip(),
                    }
                )
            return

        if parsed.path == "/api/service-restart":
            result = self._restart_user_service()
            self._json_response(result, status=200 if result.get("ok") else 500)
            return

        if parsed.path == "/api/config":
            service_payload = payload.get("service", {}) if isinstance(payload.get("service"), dict) else {}
            auth_payload = payload.get("auth", {}) if isinstance(payload.get("auth"), dict) else {}
            organize_payload = payload.get("organize_options", {}) if isinstance(payload.get("organize_options"), dict) else {}
            input_path = str(service_payload.get("input", "")).strip()
            output_path = str(service_payload.get("output", "")).strip()
            model = str(service_payload.get("model", "")).strip()
            schedule_mode = str(service_payload.get("schedule_mode", "interval")).strip().lower() or "interval"
            interval_raw = str(service_payload.get("interval_seconds", "")).strip()
            daily_time = str(service_payload.get("daily_time", "02:00")).strip() or "02:00"
            inbox_poll_raw = str(service_payload.get("inbox_poll_seconds", "2")).strip() or "2"
            new_password = str(auth_payload.get("new_password", ""))
            new_password_confirm = str(auth_payload.get("new_password_confirm", ""))

            if not input_path:
                self._json_response({"error": "Inbox ist erforderlich (service.input)"}, status=400)
                return

            try:
                interval = int(interval_raw)
            except ValueError:
                self._json_response({"error": "service.interval_seconds must be an integer"}, status=400)
                return

            if interval < 30:
                self._json_response({"error": "service.interval_seconds must be >= 30"}, status=400)
                return

            try:
                inbox_poll_seconds = int(inbox_poll_raw)
            except ValueError:
                self._json_response({"error": "service.inbox_poll_seconds must be an integer"}, status=400)
                return

            if inbox_poll_seconds < 1:
                self._json_response({"error": "service.inbox_poll_seconds must be >= 1"}, status=400)
                return

            if schedule_mode not in {"interval", "inbox-trigger", "daily"}:
                self._json_response({"error": "service.schedule_mode must be one of: interval, inbox-trigger, daily"}, status=400)
                return

            try:
                organize_options = {
                    "ollama_timeout": int(organize_payload.get("ollama_timeout", 1800)),
                    "ollama_retries": int(organize_payload.get("ollama_retries", 0)),
                    "max_text_chars": int(organize_payload.get("max_text_chars", 6000)),
                    "process_nice": int(organize_payload.get("process_nice", 5)),
                    "max_cpu_threads": int(organize_payload.get("max_cpu_threads", 4)),
                    "ollama_num_thread": int(organize_payload.get("ollama_num_thread", 4)),
                    "sleep_between_files": float(organize_payload.get("sleep_between_files", 0.4)),
                }
            except Exception:
                self._json_response({"error": "organize options have invalid numeric values"}, status=400)
                return

            if organize_options["ollama_timeout"] < 1:
                self._json_response({"error": "ollama_timeout must be >= 1"}, status=400)
                return
            if organize_options["ollama_retries"] < 0:
                self._json_response({"error": "ollama_retries must be >= 0"}, status=400)
                return
            if organize_options["max_text_chars"] < 100:
                self._json_response({"error": "max_text_chars must be >= 100"}, status=400)
                return
            if organize_options["max_cpu_threads"] < 0:
                self._json_response({"error": "max_cpu_threads must be >= 0"}, status=400)
                return
            if organize_options["ollama_num_thread"] < 0:
                self._json_response({"error": "ollama_num_thread must be >= 0"}, status=400)
                return
            if organize_options["sleep_between_files"] < 0:
                self._json_response({"error": "sleep_between_files must be >= 0"}, status=400)
                return

            if new_password or new_password_confirm:
                if new_password != new_password_confirm:
                    self._json_response({"error": "password confirmation does not match"}, status=400)
                    return
                if len(new_password.strip()) < 8:
                    self._json_response({"error": "password must be at least 8 characters"}, status=400)
                    return

            config, load_errors = self._load_runtime_config()
            if load_errors:
                self._json_response({"error": "config_load_failed", "errors": load_errors}, status=500)
                return

            if not isinstance(config, dict):
                config = {}
            service = config.get("service")
            if not isinstance(service, dict):
                service = {}

            service["input"] = input_path
            service["output"] = output_path
            service["model"] = model
            service["schedule_mode"] = schedule_mode
            service["interval_seconds"] = interval
            service["daily_time"] = daily_time
            service["inbox_poll_seconds"] = inbox_poll_seconds
            service["organize_extra_args"] = self._organize_args_from_options(organize_options)

            review = config.get("review_web")
            if not isinstance(review, dict):
                review = {}

            auth_password_file = self._resolve_auth_password_file(config)
            auth_password_file_rel = os.path.relpath(auth_password_file, self.config_path.parent)
            service["auth_password_file"] = auth_password_file_rel
            service["auth_password"] = ""
            review["auth_password_file"] = auth_password_file_rel
            review["auth_password"] = ""
            config["service"] = service
            config["review_web"] = review

            validation_errors = validate_config(config)
            if validation_errors:
                self._json_response({"error": "invalid_config", "errors": validation_errors}, status=400)
                return

            try:
                self._write_runtime_config(config)
            except Exception as exc:
                self._json_response({"error": f"config_write_failed: {exc}"}, status=500)
                return

            if new_password:
                try:
                    auth_password_file.parent.mkdir(parents=True, exist_ok=True)
                    auth_password_file.write_text(new_password.strip() + "\n", encoding="utf-8")
                    os.chmod(auth_password_file, 0o600)
                except Exception as exc:
                    self._json_response({"error": f"password_write_failed: {exc}"}, status=500)
                    return

            self._json_response(
                {
                    "ok": True,
                    "config_path": str(self.config_path),
                    "auth_password_file": str(auth_password_file),
                    "restart_required": True,
                }
            )
            return

        self._text_response("Not found", status=404, content_type="text/plain; charset=utf-8")


def _latest_by_mtime(paths: list[Path]) -> Optional[Path]:
    if not paths:
        return None
    return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def find_latest_log_file(explicit: str, base_dir: Path) -> Path:
    if explicit.strip():
        p = Path(explicit).expanduser()
        if not p.is_absolute():
            p = base_dir / p
        if p.exists():
            return p.resolve()

        # If explicit points to the non-dated base filename, accept dated variants.
        dated = list(p.parent.glob(f"*_{p.name}")) if p.parent.exists() else []
        latest_dated = _latest_by_mtime([c for c in dated if c.is_file()])
        if latest_dated is not None:
            return latest_dated.resolve()

        # Fresh installs may configure a log file that does not exist yet.
        # Return the configured path and let sync start with an empty state.
        return p.resolve()

    candidates: list[Path] = []
    for d in [base_dir / "logs", Path("/tmp")]:
        if d.exists():
            candidates.extend([p for p in d.glob("*_organize_log.jsonl") if p.is_file()])

    latest = _latest_by_mtime(candidates)
    if latest is None:
        # Start with an empty state when no scan has run yet; sync picks up new logs later.
        return (base_dir / "logs" / "organize_log.jsonl").resolve()
    return latest.resolve()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review and deploy organize.py suggestions")
    parser.add_argument("--config-file", default="assistant_config.json", help="Path to shared JSON config file")
    parser.add_argument("--log-file", default=None, help="Path to JSONL log file (default: latest from ./logs, fallback /tmp)")
    parser.add_argument("--state-file", default=None, help="State file path")
    parser.add_argument("--field-aliases-file", default=None, help="Aliases file shared with organize.py")
    parser.add_argument("--auth-password", default=None, help="Login password for web UI/API")
    parser.add_argument("--auth-password-file", default=None, help="File containing login password (first line)")
    parser.add_argument("--session-ttl-seconds", type=int, default=None, help="Session lifetime in seconds")
    parser.add_argument("--categories", nargs="+", default=None, help="Allowed categories")
    parser.add_argument("--host", default=None, help="Bind host")
    parser.add_argument("--port", type=int, default=None, help="Bind port")
    return parser.parse_args()


def resolve_auth_password(args: argparse.Namespace, base_dir: Path) -> str:
    auth_password_file = (args.auth_password_file or "").strip()
    if auth_password_file:
        p = Path(auth_password_file).expanduser()
        if not p.is_absolute():
            p = base_dir / p
        if p.exists():
            try:
                return p.read_text(encoding="utf-8").splitlines()[0].strip()
            except Exception:
                return ""
    auth_password = (args.auth_password or "").strip()
    if auth_password:
        return auth_password
    return os.environ.get("REVIEW_WEB_PASSWORD", "").strip()


def main() -> int:
    args = parse_args()
    base_dir = Path(__file__).resolve().parent

    cfg = load_config(args.config_file, base_dir)
    if cfg.errors:
        for err in cfg.errors:
            print(f"[ERROR] {err}")
        return 2

    config = cfg.data
    validation_errors = validate_config(config)
    if validation_errors:
        for err in validation_errors:
            print(f"[ERROR] Invalid config: {err}")
        return 2

    section = get_section(config, "review_web")

    log_file_arg = str(pick(args.log_file, section, "log_file", "")).strip()
    state_file_arg = str(pick(args.state_file, section, "state_file", "review_state.json")).strip()
    aliases_file_arg = str(pick(args.field_aliases_file, section, "field_aliases_file", "field_aliases.json")).strip()
    session_ttl_seconds = int(pick(args.session_ttl_seconds, section, "session_ttl_seconds", 28800))
    host = str(pick(args.host, section, "host", "127.0.0.1")).strip()
    port = int(pick(args.port, section, "port", 8449))

    categories_raw = pick(args.categories, section, "categories", DEFAULT_CATEGORIES)
    if not isinstance(categories_raw, list):
        categories_raw = DEFAULT_CATEGORIES

    log_file = find_latest_log_file(log_file_arg, base_dir)

    state_file = Path(state_file_arg).expanduser()
    if not state_file.is_absolute():
        state_file = base_dir / state_file

    aliases_file = Path(aliases_file_arg).expanduser()
    if not aliases_file.is_absolute():
        aliases_file = base_dir / aliases_file

    categories = [str(c).strip().upper() for c in categories_raw if str(c).strip()]
    if "SONSTIGES" not in categories:
        categories.append("SONSTIGES")

    auth_password = resolve_auth_password(args, base_dir)
    if not auth_password:
        auth_password = str(section.get("auth_password", "")).strip() or os.environ.get("REVIEW_WEB_PASSWORD", "").strip()

    auth_password_file_cfg = str(section.get("auth_password_file", "")).strip()
    if not auth_password and auth_password_file_cfg:
        p = Path(auth_password_file_cfg).expanduser()
        if not p.is_absolute():
            p = base_dir / p
        if p.exists():
            try:
                auth_password = p.read_text(encoding="utf-8").splitlines()[0].strip()
            except Exception:
                auth_password = ""

    auth = PasswordAuth(auth_password, session_ttl_seconds)

    if not auth.enabled and host not in {"127.0.0.1", "localhost", "::1"}:
        print("[ERROR] Remote bind without login is blocked. Set --auth-password or --auth-password-file.")
        return 2

    store = ReviewStore(Paths(log_file=log_file, state_file=state_file, aliases_file=aliases_file), categories=categories)

    Handler.store = store
    Handler.auth = auth
    Handler.config_path = cfg.path
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Review app listening on http://{host}:{port}")
    print(f"Log file: {log_file}")
    print(f"State file: {state_file}")
    print(f"Aliases file: {aliases_file}")
    print(f"Auth: {'enabled' if auth.enabled else 'disabled (localhost only recommended)'}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
