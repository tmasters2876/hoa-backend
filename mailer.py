"""Outbound email for hoa-admin.

Backends (MAIL_BACKEND):
  off   — default. Nothing is sent; every call is logged as skipped. Production
          stays here until SMTP is configured.
  file  — DEV. Each message is written to dev/mail/ as .eml + .json and shown on
          the console's DEV-only Outbox page. Nothing leaves the machine.
  smtp  — SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM / SMTP_TLS.
          Works with Gmail app passwords, Resend, SendGrid, Postmark.

Every attempt is recorded in email_log (sql/005_email.sql) regardless of backend.
This module never touches Flask's request or session: callers pass everything
in, so sending can run on a background thread.
"""
from __future__ import annotations

import json
import os
import smtplib
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

REPO = Path(__file__).resolve().parent
FILE_OUTBOX = REPO / "dev" / "mail"


def backend() -> str:
    return os.getenv("MAIL_BACKEND", "off").strip().lower() or "off"


def enabled() -> bool:
    return backend() in ("file", "smtp")


def from_address() -> str:
    return os.getenv("SMTP_FROM", "PLCA Console <no-reply@plantationlakes.local>")


def subject_prefix() -> str:
    dev = os.getenv("HOA_ENV", "").strip().lower() == "dev"
    return "[DEV] [PLCA Console] " if dev else "[PLCA Console] "


def _log(to: str, subject: str, kind: str | None, related_id: str | None, status: str, error: str | None = None) -> None:
    try:
        from services import get_supabase_client

        get_supabase_client().from_("email_log").insert({
            "to_address": to, "subject": subject, "kind": kind, "related_id": related_id,
            "status": status, "error": (error or None) and str(error)[:500],
        }).execute()
    except Exception as e:  # logging must never break the caller
        print(f"[mail] WARNING: email_log write failed: {e}", flush=True)


def _write_file(msg: EmailMessage, kind: str | None, related_id: str | None) -> Path:
    FILE_OUTBOX.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    stem = FILE_OUTBOX / f"{stamp}_{(kind or 'mail')}_{uuid.uuid4().hex[:6]}"
    stem.with_suffix(".eml").write_bytes(bytes(msg))
    stem.with_suffix(".json").write_text(json.dumps({
        "to": msg["To"], "from": msg["From"], "subject": msg["Subject"], "kind": kind,
        "related_id": related_id, "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "text": msg.get_content(),
    }, indent=1))
    return stem


def _send_smtp(msg: EmailMessage) -> None:
    host = os.getenv("SMTP_HOST", "")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")
    use_tls = os.getenv("SMTP_TLS", "true").strip().lower() != "false"
    if not host:
        raise RuntimeError("MAIL_BACKEND=smtp but SMTP_HOST is not set")
    with smtplib.SMTP(host, port, timeout=20) as s:
        if use_tls:
            s.starttls()
        if user:
            s.login(user, password)
        s.send_message(msg)


def send_email(to: str, subject: str, text: str, *, kind: str | None = None, related_id: str | None = None) -> str:
    """Send one plain-text email. Returns the email_log status: sent | failed | skipped."""
    to = (to or "").strip()
    full_subject = subject_prefix() + subject
    if not to or not enabled():
        _log(to or "(none)", full_subject, kind, related_id, "skipped", None if to else "no address")
        return "skipped"
    msg = EmailMessage()
    msg["From"] = from_address()
    msg["To"] = to
    msg["Subject"] = full_subject
    msg.set_content(text)
    try:
        if backend() == "file":
            _write_file(msg, kind, related_id)
        else:
            _send_smtp(msg)
    except Exception as e:
        print(f"[mail] ERROR sending to {to}: {e}", flush=True)
        _log(to, full_subject, kind, related_id, "failed", str(e))
        return "failed"
    _log(to, full_subject, kind, related_id, "sent")
    return "sent"


def outbox() -> list[dict]:
    """DEV file backend: captured messages, newest first."""
    if not FILE_OUTBOX.exists():
        return []
    items = []
    for p in sorted(FILE_OUTBOX.glob("*.json"), reverse=True):
        try:
            d = json.loads(p.read_text())
            d["id"] = p.stem
            items.append(d)
        except Exception:
            continue
    return items


def outbox_message(item_id: str) -> dict | None:
    p = FILE_OUTBOX / f"{item_id}.json"
    if not p.exists() or "/" in item_id or ".." in item_id:
        return None
    d = json.loads(p.read_text())
    d["id"] = item_id
    return d


def clear_outbox() -> int:
    n = 0
    for p in FILE_OUTBOX.glob("*"):
        if p.suffix in (".eml", ".json"):
            p.unlink()
            n += 1
    return n
