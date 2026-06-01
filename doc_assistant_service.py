#!/usr/bin/env python3
"""Background service for automatic dry-run scans + hosted review web UI.

Behavior:
- Starts review_web.py and keeps it running.
- Triggers organize.py in dry-run mode on a fixed interval.
- Restarts the web process if it exits unexpectedly.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from assistant_config import get_section, load_config, pick, validate_config
from notification_email import send_review_notification


def ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def emit(message: str) -> None:
    print(f"[{ts()}] {message}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run review web UI and periodic organize dry-runs")
    parser.add_argument("--config-file", default="assistant_config.json", help="Path to shared JSON config file")
    parser.add_argument("--input", default=None, help="Inbox directory for organize.py")
    parser.add_argument("--output", default=None, help="Outbox directory for organize.py (sorted files)")
    parser.add_argument("--model", default=None, help="Ollama model for organize.py (optional)")
    parser.add_argument(
        "--schedule-mode",
        default=None,
        choices=["interval", "inbox-trigger", "daily"],
        help="Scheduler mode: interval, inbox-trigger, or daily",
    )
    parser.add_argument("--interval-minutes", type=int, default=None, help="Minutes between dry-run scans")
    parser.add_argument("--daily-time", default=None, help="Daily run time in local time (HH:MM)")
    parser.add_argument("--inbox-poll-seconds", type=int, default=None, help="Inbox polling interval for inbox-trigger mode")
    parser.add_argument("--host", default=None, help="Host for review_web.py")
    parser.add_argument("--port", type=int, default=None, help="Port for review_web.py")
    parser.add_argument("--state-file", default=None, help="State file for review_web.py")
    parser.add_argument("--field-aliases-file", default=None, help="Shared alias file")
    parser.add_argument("--auth-password", default=None, help="Login password for review_web.py")
    parser.add_argument("--auth-password-file", default=None, help="Password file for review_web.py")
    parser.add_argument("--session-ttl-seconds", type=int, default=None, help="Session lifetime for review_web.py")
    parser.add_argument("--python", default=None, help="Python executable")
    parser.add_argument("--project-dir", default=None, help="Project directory (default: script location)")
    parser.add_argument(
        "--organize-extra-arg",
        action="append",
        default=None,
        help="Extra argument for organize.py (repeatable, e.g. --organize-extra-arg=--ollama-timeout --organize-extra-arg=1800)",
    )
    return parser.parse_args()


def parse_daily_time(value: str) -> tuple[int, int]:
    raw = str(value or "").strip()
    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError("daily-time must be HH:MM")
    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("daily-time must be between 00:00 and 23:59")
    return hour, minute


def next_daily_run_ts(now_ts: float, hour: int, minute: int) -> float:
    from datetime import timedelta

    now = datetime.fromtimestamp(now_ts)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= now_ts:
        candidate = candidate + timedelta(days=1)
    return candidate.timestamp()


def next_interval_run_ts(now_ts: float, interval_minutes: int) -> float:
    from datetime import timedelta

    now = datetime.fromtimestamp(now_ts)
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    for slot_minute in range(0, 60, interval_minutes):
        candidate = current_hour + timedelta(minutes=slot_minute)
        if candidate.timestamp() >= now_ts:
            return candidate.timestamp()

    candidate = current_hour + timedelta(hours=1)

    return candidate.timestamp()


def inbox_snapshot(path: Path) -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    if not path.exists() or not path.is_dir():
        return snapshot
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        try:
            st = item.stat()
        except OSError:
            continue
        key = str(item.relative_to(path))
        snapshot[key] = (int(st.st_size), int(st.st_mtime_ns))
    return snapshot


def inbox_has_new_or_changed(prev: dict[str, tuple[int, int]], curr: dict[str, tuple[int, int]]) -> bool:
    if not prev and not curr:
        return False
    for key, value in curr.items():
        if key not in prev:
            return True
        if prev[key] != value:
            return True
    return False


def organize_running(project_dir: Path) -> bool:
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
        return False

    if proc.returncode != 0:
        return False

    current_pid = os.getpid()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        args = parts[1]
        if pid == current_pid:
            continue
        if "organize.py" not in args:
            continue
        if organize_path in args:
            return True
    return False


def read_last_summary(project_dir: Path) -> dict:
    summary_path = (project_dir / "logs" / "organize_summary.json").resolve()
    if not summary_path.exists() or not summary_path.is_file():
        return {}
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def build_review_cmd(args: argparse.Namespace, project_dir: Path) -> list[str]:
    cmd = [
        args.python,
        str(project_dir / "review_web.py"),
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--state-file",
        args.state_file,
        "--field-aliases-file",
        args.field_aliases_file,
        "--session-ttl-seconds",
        str(args.session_ttl_seconds),
    ]
    if args.auth_password.strip():
        cmd.extend(["--auth-password", args.auth_password.strip()])
    if args.auth_password_file.strip():
        cmd.extend(["--auth-password-file", args.auth_password_file.strip()])
    return cmd


def build_organize_cmd(args: argparse.Namespace, project_dir: Path) -> list[str]:
    cmd = [
        args.python,
        str(project_dir / "organize.py"),
        "--input",
        args.input,
        "--dry-run",
        "--review-state-file",
        args.state_file,
        "--field-aliases-file",
        args.field_aliases_file,
    ]
    if args.model.strip():
        cmd.extend(["--model", args.model.strip()])
    if args.output.strip():
        cmd.extend(["--output-root", args.output.strip()])

    # Keep this generic for future tuning without code edits.
    cmd.extend(args.organize_extra_arg)
    return cmd


def run_organize_once(args: argparse.Namespace, project_dir: Path) -> int:
    cmd = build_organize_cmd(args, project_dir)
    emit("Run dry-run scan: " + " ".join(shlex.quote(part) for part in cmd))
    proc = subprocess.run(cmd, cwd=str(project_dir), check=False)
    emit(f"Dry-run finished with exit code {proc.returncode}")
    try:
        cfg = load_config(args.config_file, project_dir)
        config = cfg.data if isinstance(cfg.data, dict) else {}
        summary = read_last_summary(project_dir)
        stats = summary.get("stats", {}) if isinstance(summary.get("stats", {}), dict) else {}
        new_review_count = int(stats.get("review", 0) or 0)
        if proc.returncode == 0 and new_review_count > 0:
            review_url = f"http://{args.host}:{args.port}"
            result = send_review_notification(
                config,
                new_review_count=new_review_count,
                scan_source=f"service:{args.schedule_mode}",
                review_url=review_url,
                input_path=args.input,
            )
            if result.sent:
                emit(f"Review notification email sent for {new_review_count} new entries")
            elif result.reason not in {"disabled", "no_new_review_entries"}:
                emit(f"[WARN] Review notification email not sent: {result.reason}")
    except Exception as exc:
        emit(f"[WARN] Review notification handling failed: {exc}")
    return proc.returncode


def terminate_process(proc: Optional[subprocess.Popen[bytes]], name: str) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        return

    emit(f"Stopping {name} (pid={proc.pid})")
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        emit(f"Force-killing {name} (pid={proc.pid})")
        proc.kill()
        proc.wait(timeout=5)


def main() -> int:
    args = parse_args()

    default_project_dir = Path(__file__).resolve().parent
    cfg = load_config(args.config_file, default_project_dir)
    if cfg.errors:
        for err in cfg.errors:
            emit(f"[ERROR] {err}")
        return 2

    config = cfg.data
    validation_errors = validate_config(config)
    if validation_errors:
        for err in validation_errors:
            emit(f"[ERROR] Invalid config: {err}")
        return 2

    section = get_section(config, "service")

    args.input = str(pick(args.input, section, "input", "")).strip()
    args.output = str(pick(args.output, section, "output", "")).strip()
    args.model = str(pick(args.model, section, "model", "")).strip()
    args.schedule_mode = str(pick(args.schedule_mode, section, "schedule_mode", "interval")).strip().lower() or "interval"
    args.interval_minutes = int(pick(args.interval_minutes, section, "interval_minutes", 5))
    args.daily_time = str(pick(args.daily_time, section, "daily_time", "02:00")).strip() or "02:00"
    args.inbox_poll_seconds = int(pick(args.inbox_poll_seconds, section, "inbox_poll_seconds", 2))
    args.host = str(pick(args.host, section, "host", "127.0.0.1")).strip()
    args.port = int(pick(args.port, section, "port", 8449))
    args.state_file = str(pick(args.state_file, section, "state_file", "review_state.json")).strip()
    args.field_aliases_file = str(pick(args.field_aliases_file, section, "field_aliases_file", "field_aliases.json")).strip()
    args.auth_password = str(pick(args.auth_password, section, "auth_password", "")).strip()
    args.auth_password_file = str(pick(args.auth_password_file, section, "auth_password_file", "")).strip()
    args.session_ttl_seconds = int(pick(args.session_ttl_seconds, section, "session_ttl_seconds", 28800))
    args.python = str(pick(args.python, section, "python", sys.executable)).strip()
    args.project_dir = str(pick(args.project_dir, section, "project_dir", "")).strip()
    if args.organize_extra_arg is None:
        cfg_extra = section.get("organize_extra_args", [])
        args.organize_extra_arg = [str(v) for v in cfg_extra] if isinstance(cfg_extra, list) else []

    if not args.input:
        emit("[ERROR] Missing input folder. Set --input or service.input in assistant_config.json")
        return 2

    if args.interval_minutes < 1:
        emit("interval-minutes too low; using minimum of 1")
        args.interval_minutes = 1

    if args.inbox_poll_seconds < 1:
        emit("inbox-poll-seconds too low; using minimum of 1")
        args.inbox_poll_seconds = 1

    if args.schedule_mode not in {"interval", "inbox-trigger", "daily"}:
        emit(f"[ERROR] Invalid schedule-mode: {args.schedule_mode}")
        return 2

    try:
        daily_hour, daily_minute = parse_daily_time(args.daily_time)
    except Exception as exc:
        emit(f"[ERROR] Invalid daily-time: {exc}")
        return 2

    project_dir = Path(args.project_dir).expanduser().resolve() if args.project_dir else default_project_dir
    if not (project_dir / "organize.py").exists() or not (project_dir / "review_web.py").exists():
        emit(f"[ERROR] project-dir invalid: {project_dir}")
        return 2

    stop = {"value": False}

    def on_signal(signum: int, _frame: object) -> None:
        emit(f"Signal {signum} received, shutting down...")
        stop["value"] = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    review_proc: Optional[subprocess.Popen[bytes]] = None

    emit(f"Service start. project_dir={project_dir}")
    emit(f"Web UI: http://{args.host}:{args.port}")
    emit(f"Schedule mode: {args.schedule_mode}")
    if args.schedule_mode == "interval":
        emit(f"Scan interval: {args.interval_minutes} min")
    elif args.schedule_mode == "inbox-trigger":
        emit(f"Inbox trigger polling: {args.inbox_poll_seconds}s")
    else:
        emit(f"Daily run time: {args.daily_time}")

    try:
        next_scan_at = next_interval_run_ts(time.time(), args.interval_minutes)
        next_daily_at = next_daily_run_ts(time.time(), daily_hour, daily_minute)
        last_inbox_snapshot = inbox_snapshot(Path(args.input).expanduser())
        if args.schedule_mode == "inbox-trigger" and last_inbox_snapshot:
            emit("Inbox contains files at startup, running initial dry-run scan.")
            if not organize_running(project_dir):
                run_organize_once(args, project_dir)
            else:
                emit("Skipping initial dry-run scan because organize.py is already running.")
            last_inbox_snapshot = inbox_snapshot(Path(args.input).expanduser())

        while not stop["value"]:
            if review_proc is None or review_proc.poll() is not None:
                if review_proc is not None:
                    emit(f"review_web.py exited with code {review_proc.returncode}, restarting...")
                review_cmd = build_review_cmd(args, project_dir)
                emit("Starting review web: " + " ".join(shlex.quote(part) for part in review_cmd))
                review_proc = subprocess.Popen(review_cmd, cwd=str(project_dir))

            now = time.time()
            if args.schedule_mode == "interval":
                if now >= next_scan_at:
                    if organize_running(project_dir):
                        emit("Skipping interval dry-run scan because organize.py is already running.")
                    else:
                        run_organize_once(args, project_dir)
                    next_scan_at = next_interval_run_ts(time.time(), args.interval_minutes)
                time.sleep(1.0)
                continue

            if args.schedule_mode == "daily":
                if now >= next_daily_at:
                    if organize_running(project_dir):
                        emit("Skipping daily dry-run scan because organize.py is already running.")
                    else:
                        run_organize_once(args, project_dir)
                    next_daily_at = next_daily_run_ts(now + 1, daily_hour, daily_minute)
                time.sleep(1.0)
                continue

            inbox_path = Path(args.input).expanduser()
            if not inbox_path.is_absolute():
                inbox_path = (project_dir / inbox_path).resolve()
            current_snapshot = inbox_snapshot(inbox_path)
            if inbox_has_new_or_changed(last_inbox_snapshot, current_snapshot):
                if organize_running(project_dir):
                    emit("Inbox change detected, but organize.py is already running. Skipping trigger.")
                else:
                    emit("Inbox change detected, running dry-run scan.")
                    run_organize_once(args, project_dir)
                current_snapshot = inbox_snapshot(inbox_path)
            last_inbox_snapshot = current_snapshot
            time.sleep(float(args.inbox_poll_seconds))
    finally:
        terminate_process(review_proc, "review_web.py")

    emit("Service stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
