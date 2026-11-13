"""The weekly approval email: every pending proposal for a client, each with Approve and Skip links."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session

from fullhouse.core import mailer, proposals
from fullhouse.core.config import Settings
from fullhouse.core.models import Client, ProposalKind
from fullhouse.reports.render import render_html

LABELS = {
    ProposalKind.review_reply: "Review reply",
    ProposalKind.action: "This week's action",
    ProposalKind.fix: "Fix",
    ProposalKind.listing_fix: "Listing fix",
}


def compose(settings: Settings, client: Client, items) -> tuple[str, str, str]:
    """(subject, text, html)"""
    n = len(items)
    subject = f"{client.name}: {n} thing{'s' if n != 1 else ''} to approve this week"
    base = settings.approval_base_url
    lines = [f"{client.contact_name or 'Hi'},", "",
             f"{n} thing{'s' if n != 1 else ''} to approve for {client.name}. Nothing happens without your yes.", ""]
    for i, p in enumerate(items, 1):
        lines += [f"{i}. {p.title}"]
        if p.kind == ProposalKind.review_reply:
            lines += [f"   Review: {p.why}", f"   Our reply: {p.detail}"]
        elif p.why:
            lines += [f"   {p.why}"]
        lines += [f"   Approve: {base}/p/{p.token}?d=approve", f"   Skip:    {base}/p/{p.token}?d=skip", ""]
    lines += [f"All of them: {base}/o/{client.owner_token}",
              f"Unanswered items expire after {settings.proposal_ttl_days} days.", "",
              f"{settings.sender_name}, {settings.sender_business}"]
    html = render_html("email/approvals.html", {
        "subject": subject, "client": client, "items": items, "labels": LABELS, "base": base,
        "ttl": settings.proposal_ttl_days, "sender": f"{settings.sender_name}, {settings.sender_business}",
    })
    return subject, "\n".join(lines), html


def send(session: Session, settings: Settings, client: Client, *, send: bool = True) -> mailer.Sent | None:
    """Email the client's pending proposals. Returns None when there is nothing to ask."""
    items = proposals.pending(session, client, limit=proposals.MAX_PER_DIGEST)
    if not items:
        return None
    subject, text, html = compose(settings, client, items)
    msg = mailer.new_message(settings, client.contact_email or "", subject, text, html)
    result = mailer.deliver(settings, msg, send=send, session=session, stem=f"approvals-client{client.id}")
    now = datetime.now(timezone.utc)
    for p in items:
        p.sent_at = now
        session.add(p)
    session.commit()
    return result
