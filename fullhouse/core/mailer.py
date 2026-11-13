"""Send email over SMTP, or write it to the outbox when SMTP isn't configured (or on dry runs).

Every message is also saved to the outbox as .eml, so there's always a record of what went out.
"""

from __future__ import annotations

import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from sqlmodel import Session, select

from fullhouse.core.config import Settings
from fullhouse.core.models import Suppression

log = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MailError(RuntimeError):
    """Sending failed. The message is still in the outbox, at ``path``."""

    def __init__(self, message: str, path: Path | None = None):
        super().__init__(message)
        self.path = path


@dataclass
class Sent:
    sent: bool  # True = handed to the SMTP server; False = outbox only
    path: Path  # the .eml copy
    reason: str = ""


def new_message(settings: Settings, to: str, subject: str, text: str, html: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((settings.mail_from_name, settings.mail_from))
    msg["To"] = to
    msg["Message-ID"] = make_msgid(domain=settings.mail_from.split("@")[-1] or "fullhouse.local")
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def is_suppressed(session: Session | None, email: str) -> bool:
    if session is None:
        return False
    return session.exec(select(Suppression).where(Suppression.email == email.strip().lower())).first() is not None


def suppress(session: Session, email: str, reason: str = "unsubscribed") -> None:
    email = email.strip().lower()
    if not is_suppressed(session, email):
        session.add(Suppression(email=email, reason=reason))
        session.commit()


def deliver(settings: Settings, msg: EmailMessage, *, send: bool = True, session: Session | None = None,
            stem: str = "message") -> Sent:
    """Save to the outbox and, when ``send`` and SMTP is configured, send it."""
    to = msg["To"] or ""
    settings.outbox_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", stem)[:80]
    path = settings.outbox_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{safe}.eml"
    path.write_bytes(bytes(msg))
    if not EMAIL_RE.match(to.split("<")[-1].rstrip(">").strip()):
        return Sent(False, path, "no valid recipient address")
    if is_suppressed(session, to.split("<")[-1].rstrip(">")):
        return Sent(False, path, f"{to} has unsubscribed")
    if not send:
        return Sent(False, path, "not sent (dry run or --no-send)")
    if not settings.smtp_host:
        return Sent(False, path, "SMTP not configured (set SMTP_HOST); saved to the outbox")
    try:
        if settings.smtp_ssl:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=ssl.create_default_context(),
                                      timeout=30)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
            server.starttls(context=ssl.create_default_context())
        with server:
            if settings.smtp_user and settings.smtp_password:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"Sending to {to} failed: {exc}", path) from exc
    log.info("sent %r to %s", msg["Subject"], to)
    return Sent(True, path)
