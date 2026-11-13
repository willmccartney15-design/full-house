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


FREE_MAIL = ("gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "icloud.com",
             "me.com", "bigpond.com", "bigpond.net.au", "optusnet.com.au")
RESEND_ENDPOINT = "https://api.resend.com/emails"
RESEND_TEST_SENDER = "onboarding@resend.dev"


def transport(settings: Settings) -> str:
    """resend | smtp | outbox"""
    if settings.email_provider in ("resend", "smtp", "outbox"):
        return settings.email_provider
    if settings.resend_api_key:
        return "resend"
    if settings.smtp_host:
        return "smtp"
    return "outbox"


def resend_sender(settings: Settings) -> str:
    """The From address Resend will accept: RESEND_FROM, else MAIL_FROM on your own domain, else Resend's test sender
    (a free-mail address like Gmail can't be verified as a Resend sending domain)."""
    if settings.resend_from:
        return settings.resend_from
    domain = settings.mail_from.rsplit("@", 1)[-1].lower()
    address = RESEND_TEST_SENDER if domain in FREE_MAIL or domain.endswith(".local") else settings.mail_from
    return formataddr((settings.mail_from_name, address))


def resend_payload(settings: Settings, msg: EmailMessage) -> dict:
    """Resend's POST /emails body for an EmailMessage: text, html and attachments, with replies to MAIL_FROM."""
    import base64

    text = html = None
    attachments = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if filename:
            attachments.append({"filename": filename,
                                "content": base64.b64encode(part.get_payload(decode=True)).decode()})
        elif part.get_content_type() == "text/plain" and text is None:
            text = part.get_content()
        elif part.get_content_type() == "text/html" and html is None:
            html = part.get_content()
    payload = {"from": resend_sender(settings), "to": [msg["To"]], "subject": msg["Subject"] or "",
               "reply_to": [settings.mail_from]}
    if text:
        payload["text"] = text
    if html:
        payload["html"] = html
    if attachments:
        payload["attachments"] = attachments
    return payload


def _send_resend(settings: Settings, msg: EmailMessage, path: Path) -> Sent:
    import httpx

    # Without RESEND_API_KEY the header carries a placeholder: in hosted sessions the network proxy replaces it with
    # the key saved for api.resend.com, so the key never sits in the environment.
    key = settings.resend_api_key or "proxy-injected"
    headers = {"Authorization": f"Bearer {key}", "Idempotency-Key": (msg["Message-ID"] or "").strip("<>")[:256]}
    try:
        resp = httpx.post(RESEND_ENDPOINT, json=resend_payload(settings, msg), headers=headers, timeout=30)
    except httpx.HTTPError as exc:
        raise MailError(f"Sending to {msg['To']} through Resend failed: {exc}", path) from exc
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("message") or resp.text
        except ValueError:
            detail = resp.text
        raise MailError(f"Resend refused the email to {msg['To']} (HTTP {resp.status_code}): {detail}", path)
    log.info("sent %r to %s via Resend (%s)", msg["Subject"], msg["To"], resp.json().get("id"))
    return Sent(True, path)


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
    how = transport(settings)
    if how == "resend":
        return _send_resend(settings, msg, path)
    if how != "smtp":
        return Sent(False, path, "no email provider configured (set RESEND_API_KEY or SMTP_HOST); "
                                 "saved to the outbox")
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
