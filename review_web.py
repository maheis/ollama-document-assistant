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
import shutil
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

    def _sync_from_log(self) -> None:
        if not self.paths.log_file.exists():
            return

        entries = self.state["entries"]
        try:
            lines = self.paths.log_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            return

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
                sorted_root = Path(*review_root.parts[:idx]) / "_sorted"
            else:
                sorted_root = src.parent / "_sorted"
            return sorted_root, review_root

        parent = original_target.parent
        if re.match(r"^\d{4}$", parent.name):
            sorted_root = parent.parent
        else:
            sorted_root = parent

        review_root = sorted_root.parent / "_review"
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
                row = {
                    "id": entry["id"],
                    "status": status_value,
                    "source": str(source),
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
    <title>Document Review Deploy</title>
    <style>
        :root {
            --bg: #f5f4ef;
            --card: #fffdf6;
            --ink: #1d1f24;
            --muted: #6d727d;
            --accent: #0f766e;
            --line: #dfdccf;
            --ok: #166534;
            --warn: #92400e;
            --err: #b91c1c;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
            color: var(--ink);
            background: radial-gradient(circle at top right, #ebe7d6 0%, var(--bg) 40%), var(--bg);
        }
        .wrap { max-width: 1460px; margin: 0 auto; padding: 18px; }
        .top {
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 12px;
            padding: 14px;
            box-shadow: 0 8px 30px rgba(30, 24, 10, 0.06);
            position: sticky;
            top: 8px;
            z-index: 2;
        }
        h1 { margin: 0 0 10px 0; font-size: 22px; letter-spacing: 0.2px; }
        .meta { color: var(--muted); font-size: 13px; margin-bottom: 10px; }
        .actions { display: flex; gap: 10px; flex-wrap: wrap; }
        .filter-box { display: inline-flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; }
        .filter-box select { width: auto; min-width: 170px; }
        button {
            border: 1px solid var(--line);
            background: white;
            color: var(--ink);
            border-radius: 10px;
            padding: 9px 12px;
            cursor: pointer;
            font-weight: 600;
        }
        button.primary { background: var(--accent); border-color: var(--accent); color: white; }
        button.secondary { background: #fff7ed; border-color: #fed7aa; color: #7c2d12; }
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
        table { width: 100%; border-collapse: collapse; min-width: 1450px; }
        th, td { border-bottom: 1px solid var(--line); padding: 8px; text-align: left; vertical-align: top; }
        th { position: sticky; top: 0; background: #f7f3e6; font-size: 12px; text-transform: uppercase; letter-spacing: 0.3px; }
        tr:hover td { background: #fffcf1; }
        input, select {
            width: 100%;
            border: 1px solid #d4cfbd;
            border-radius: 8px;
            padding: 7px 8px;
            font-size: 13px;
            background: white;
        }
        .mini { font-size: 12px; color: var(--muted); }
        .pill {
            display: inline-block;
            border: 1px solid var(--line);
            border-radius: 999px;
            padding: 2px 8px;
            font-size: 12px;
            font-weight: 600;
            background: #fff;
        }
        .review { color: var(--warn); border-color: #facc15; }
        .sorted { color: var(--ok); border-color: #86efac; }
        .missing { color: var(--err); border-color: #fecaca; }
        .status-pending { color: #334155; border-color: #cbd5e1; }
        .status-saved { color: #7c2d12; border-color: #fdba74; background: #fff7ed; }
        .status-deployed { color: #14532d; border-color: #86efac; background: #f0fdf4; }
        .status-missing { color: #991b1b; border-color: #fca5a5; background: #fef2f2; }
        a.filelink { color: #0e7490; text-decoration: none; }
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
            <h1>Review vor Deploy</h1>
            <div class=\"meta\" id=\"meta\"></div>
            <div class=\"actions\">
                <button onclick=\"reloadData()\">Neu laden</button>
                <button class=\"secondary\" onclick=\"saveEdits()\">Aenderungen speichern</button>
                <button class=\"primary\" onclick=\"deployAll()\">Deploy ausfuehren</button>
                <label class="filter-box">
                    Status-Filter
                    <select id="status-filter" onchange="applyFilter()">
                        <option value="all">Alle</option>
                        <option value="open" selected>Offen (pending/saved/missing)</option>
                        <option value="pending">Pending</option>
                        <option value="saved">Saved</option>
                        <option value="missing">Missing</option>
                        <option value="deployed">Deployed</option>
                    </select>
                </label>
            </div>
            <div class=\"status\" id=\"status\"></div>
        </div>

        <div class=\"grid\">
            <table>
                <thead>
                    <tr>
                        <th>Datei</th>
                        <th>Status</th>
                        <th>Conf</th>
                        <th>Sender</th>
                        <th>Kategorie</th>
                        <th>Kunden-Nr</th>
                        <th>Titel</th>
                        <th>Datum</th>
                        <th>Zielvorschau</th>
                        <th>Lernen</th>
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

function optionsFor(field) {
    const values = DATA.value_memory?.[field] || [];
    return values.map(v => `<option value=\"${esc(v)}\"></option>`).join('');
}

function categoryOptions(selected) {
    return DATA.categories.map(c => `<option ${c === selected ? 'selected' : ''} value=\"${esc(c)}\">${esc(c)}</option>`).join('');
}

function rowMarkup(row) {
    const badge = row.review
        ? '<span class="pill review">REVIEW</span>'
        : '<span class="pill sorted">SORTED</span>';
    const missing = row.source_exists ? '' : '<div><span class="pill missing">DATEI FEHLT</span></div>';
    const statusClass = `status-${String(row.status || 'pending')}`;
    const statusLabel = String(row.status || 'pending').toUpperCase();
    const deployDisabled = row.status === 'deployed' ? 'disabled' : '';

    return `<tr data-id=\"${esc(row.id)}\">
        <td>
            <a class=\"filelink\" target=\"_blank\" href=\"/file?id=${encodeURIComponent(row.id)}\">${esc(row.source_name)}</a>
            <div class=\"mini\">${esc(row.source)}</div>
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
                <button class=\"primary\" onclick=\"deployRow('${esc(row.id)}')\" ${deployDisabled}>Deploy Zeile</button>
            </div>
        </td>
    </tr>`;
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
    document.getElementById('meta').textContent = `Log: ${payload.log_file} | State: ${payload.state_file} | Aliases: ${payload.aliases_file} | Pending=${counts.pending || 0}, Saved=${counts.saved || 0}, Missing=${counts.missing || 0}, Deployed=${counts.deployed || 0}`;

    const shownCount = renderRows();
    const dlSender = `<datalist id=\"sender-mem\">${optionsFor('sender')}</datalist>`;
    const dlCustomer = `<datalist id=\"customer_number-mem\">${optionsFor('customer_number')}</datalist>`;
    const dlTitle = `<datalist id=\"title-mem\">${optionsFor('title')}</datalist>`;

    document.querySelectorAll('datalist').forEach(n => n.remove());
    document.body.insertAdjacentHTML('beforeend', dlSender + dlCustomer + dlTitle);
    status(`Bereit. ${openCount} offene Eintraege, ${shownCount} angezeigt.`);
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
        status('Keine offenen Eintraege.');
        return;
    }
    status('Deploy laeuft...');
    const res = await fetch('/api/deploy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rows })
    });
    const payload = await res.json();
    const msg = `Deploy fertig: moved=${payload.applied}, missing=${payload.missing}, gelernt=${payload.learned_aliases}, errors=${(payload.errors || []).length}`;
    status(msg);
    await reloadData();
}

async function deployRow(id) {
    const tr = findRow(id);
    if (!tr) {
        status('Zeile nicht gefunden.');
        return;
    }
    status('Deploy Zeile laeuft...');
    const res = await fetch('/api/deploy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rows: [rowPayload(tr)] })
    });
    const payload = await res.json();
    const msg = `Zeile deployt: moved=${payload.applied}, missing=${payload.missing}, gelernt=${payload.learned_aliases}, errors=${(payload.errors || []).length}`;
    status(msg);
    await reloadData();
}

reloadData();
</script>
</body>
</html>
"""


LOGIN_PAGE = """<!doctype html>
<html lang=\"de\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Login - Document Review Deploy</title>
    <style>
        body {
            margin: 0;
            min-height: 100vh;
            display: grid;
            place-items: center;
            font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
            background: radial-gradient(circle at top right, #ebe7d6 0%, #f5f4ef 40%), #f5f4ef;
            color: #1d1f24;
        }
        .card {
            width: min(420px, 92vw);
            background: #fffdf6;
            border: 1px solid #dfdccf;
            border-radius: 14px;
            padding: 20px;
            box-shadow: 0 8px 30px rgba(30, 24, 10, 0.08);
        }
        h1 { margin: 0 0 12px 0; font-size: 24px; }
        p { margin: 0 0 12px 0; color: #6d727d; }
        input {
            width: 100%;
            border: 1px solid #d4cfbd;
            border-radius: 10px;
            padding: 10px 12px;
            font-size: 14px;
            margin-bottom: 12px;
        }
        button {
            width: 100%;
            border: 1px solid #0f766e;
            background: #0f766e;
            color: #fff;
            border-radius: 10px;
            padding: 10px 12px;
            font-weight: 700;
            cursor: pointer;
        }
        .err { margin-top: 10px; color: #b91c1c; font-size: 13px; min-height: 18px; }
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
    store: ReviewStore
    auth: PasswordAuth

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

        if parsed.path == "/api/pending":
            if not self._require_auth(is_api=True):
                return
            self._json_response(self.store.list_entries())
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

        if parsed.path == "/api/save-edits":
            rows = payload.get("rows", []) if isinstance(payload.get("rows"), list) else []
            self._json_response(self.store.save_edits(rows))
            return

        if parsed.path == "/api/deploy":
            rows = payload.get("rows", []) if isinstance(payload.get("rows"), list) else []
            self._json_response(self.store.deploy(rows))
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

        raise FileNotFoundError(f"Log file not found: {p}")

    candidates: list[Path] = []
    for d in [base_dir / "logs", Path("/tmp")]:
        if d.exists():
            candidates.extend([p for p in d.glob("*_organize_log.jsonl") if p.is_file()])

    latest = _latest_by_mtime(candidates)
    if latest is None:
        raise FileNotFoundError("No organize_log JSONL found in ./logs or /tmp. Run organize.py --dry-run first.")
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
