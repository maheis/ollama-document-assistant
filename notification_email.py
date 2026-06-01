#!/usr/bin/env python3
"""Email notification helpers for completed scans."""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any


@dataclass
class EmailNotificationSettings:
    enabled: bool
    recipient: str
    sender: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_starttls: bool
    smtp_ssl: bool
    subject_prefix: str


@dataclass
class EmailNotificationResult:
    sent: bool
    reason: str = ""


def load_email_notification_settings(config: dict[str, Any]) -> EmailNotificationSettings:
    notifications = config.get("notifications", {})
    if not isinstance(notifications, dict):
        notifications = {}

    email = notifications.get("email", {})
    if not isinstance(email, dict):
        email = {}

    return EmailNotificationSettings(
        enabled=bool(email.get("enabled", False)),
        recipient=str(email.get("to", "")).strip(),
        sender=str(email.get("from", "")).strip(),
        smtp_host=str(email.get("smtp_host", "")).strip(),
        smtp_port=int(email.get("smtp_port", 587) or 587),
        smtp_username=str(email.get("smtp_username", "")).strip(),
        smtp_password=str(email.get("smtp_password", "")),
        smtp_starttls=bool(email.get("smtp_starttls", True)),
        smtp_ssl=bool(email.get("smtp_ssl", False)),
        subject_prefix=str(email.get("subject_prefix", "[ODA]")).strip() or "[ODA]",
    )


def send_review_notification(
    config: dict[str, Any],
    *,
    new_review_count: int,
    scan_source: str,
    review_url: str,
    input_path: str,
) -> EmailNotificationResult:
    if new_review_count <= 0:
        return EmailNotificationResult(sent=False, reason="no_new_review_entries")

    settings = load_email_notification_settings(config)
    if not settings.enabled:
        return EmailNotificationResult(sent=False, reason="disabled")

    missing = []
    if not settings.recipient:
        missing.append("notifications.email.to")
    if not settings.sender:
        missing.append("notifications.email.from")
    if not settings.smtp_host:
        missing.append("notifications.email.smtp_host")
    if settings.smtp_port < 1:
        missing.append("notifications.email.smtp_port")
    if missing:
        return EmailNotificationResult(sent=False, reason="missing:" + ",".join(missing))

    recipients = [part.strip() for part in settings.recipient.split(",") if part.strip()]
    if not recipients:
        return EmailNotificationResult(sent=False, reason="missing:notifications.email.to")

    subject = f"{settings.subject_prefix} {new_review_count} neue Dokumente zur Prüfung"
    body = "\n".join(
        [
            "Ein Scan wurde abgeschlossen.",
            "",
            f"Neue Dokumente zur Prüfung: {new_review_count}",
            f"Scan-Quelle: {scan_source}",
            f"Inbox: {input_path}",
            f"Weboberfläche: {review_url}",
            "",
            "Bitte die neuen Vorschläge in der Weboberfläche prüfen.",
        ]
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    try:
        if settings.smtp_ssl:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(msg, to_addrs=recipients)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
                smtp.ehlo()
                if settings.smtp_starttls:
                    smtp.starttls()
                    smtp.ehlo()
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(msg, to_addrs=recipients)
    except Exception as exc:
        return EmailNotificationResult(sent=False, reason=str(exc))

    return EmailNotificationResult(sent=True)
