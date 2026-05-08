#!/usr/bin/env python3
"""Shared config loader for the document assistant tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ConfigLoadResult:
    path: Path
    exists: bool
    data: dict[str, Any]
    errors: list[str]


def load_config(config_file: str, base_dir: Path) -> ConfigLoadResult:
    path = Path(config_file).expanduser()
    if not path.is_absolute():
        path = base_dir / path

    if not path.exists():
        return ConfigLoadResult(path=path, exists=False, data={}, errors=[])

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return ConfigLoadResult(
            path=path,
            exists=True,
            data={},
            errors=[f"Config parse error in {path}: {exc}"],
        )

    if not isinstance(payload, dict):
        return ConfigLoadResult(
            path=path,
            exists=True,
            data={},
            errors=[f"Config root must be a JSON object: {path}"],
        )

    return ConfigLoadResult(path=path, exists=True, data=payload, errors=[])


def validate_config(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    review_web = config.get("review_web", {})
    if review_web and not isinstance(review_web, dict):
        errors.append("review_web must be an object")
    if isinstance(review_web, dict):
        _validate_host_port(errors, review_web, scope="review_web")
        _validate_positive_int(errors, review_web, "session_ttl_seconds", scope="review_web", min_value=300)
        _validate_str(errors, review_web, "log_file", scope="review_web")
        _validate_str(errors, review_web, "state_file", scope="review_web")
        _validate_str(errors, review_web, "field_aliases_file", scope="review_web")
        _validate_str(errors, review_web, "auth_password", scope="review_web")
        _validate_str(errors, review_web, "auth_password_file", scope="review_web")
        _validate_str_list(errors, review_web, "categories", scope="review_web")

    service = config.get("service", {})
    if service and not isinstance(service, dict):
        errors.append("service must be an object")
    if isinstance(service, dict):
        _validate_host_port(errors, service, scope="service")
        _validate_positive_int(errors, service, "interval_seconds", scope="service", min_value=30)
        _validate_positive_int(errors, service, "session_ttl_seconds", scope="service", min_value=300)
        _validate_str(errors, service, "input", scope="service")
        _validate_str(errors, service, "output", scope="service")
        _validate_str(errors, service, "model", scope="service")
        _validate_str(errors, service, "state_file", scope="service")
        _validate_str(errors, service, "field_aliases_file", scope="service")
        _validate_str(errors, service, "auth_password", scope="service")
        _validate_str(errors, service, "auth_password_file", scope="service")
        _validate_str(errors, service, "python", scope="service")
        _validate_str(errors, service, "project_dir", scope="service")
        _validate_str_list(errors, service, "organize_extra_args", scope="service")

    return errors


def _validate_str(errors: list[str], section: dict[str, Any], key: str, scope: str) -> None:
    value = section.get(key)
    if value is None:
        return
    if not isinstance(value, str):
        errors.append(f"{scope}.{key} must be a string")


def _validate_positive_int(
    errors: list[str],
    section: dict[str, Any],
    key: str,
    scope: str,
    min_value: int,
) -> None:
    value = section.get(key)
    if value is None:
        return
    if not isinstance(value, int):
        errors.append(f"{scope}.{key} must be an integer")
        return
    if value < min_value:
        errors.append(f"{scope}.{key} must be >= {min_value}")


def _validate_host_port(errors: list[str], section: dict[str, Any], scope: str) -> None:
    host = section.get("host")
    if host is not None and (not isinstance(host, str) or not host.strip()):
        errors.append(f"{scope}.host must be a non-empty string")

    port = section.get("port")
    if port is None:
        return
    if not isinstance(port, int):
        errors.append(f"{scope}.port must be an integer")
        return
    if port < 1 or port > 65535:
        errors.append(f"{scope}.port must be between 1 and 65535")


def _validate_str_list(errors: list[str], section: dict[str, Any], key: str, scope: str) -> None:
    value = section.get(key)
    if value is None:
        return
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        errors.append(f"{scope}.{key} must be a list of strings")


def get_section(config: dict[str, Any], key: str) -> dict[str, Any]:
    section = config.get(key, {})
    return section if isinstance(section, dict) else {}


def pick(cli_value: Any, section: dict[str, Any], key: str, default: Any) -> Any:
    if cli_value is not None:
        return cli_value
    value = section.get(key, None)
    if value is None:
        return default
    return value
