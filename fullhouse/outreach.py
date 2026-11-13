"""First-contact emails to prospects, written from what the scan and audit found.

Claude writes the body from the property's own findings (a deterministic template in dry runs). The sender
identity and unsubscribe line are always added by code, never by the model: Australia's Spam Act 2003 requires
commercial email to identify the sender, say how to contact them, and offer a working way to opt out. Addresses
that opt out are suppressed and never emailed again (``fullhouse outreach --unsubscribe``).

Cold email to a business is only permitted when the address is conspicuously published for that business and the
message is relevant to the recipient's role; use the property's published contact address, never a guessed one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlmodel import select

from fullhouse.agents.base import AgentContext
from fullhouse.core import mailer
from fullhouse.core.models import Client, Finding, ListingAudit

NAME = "outreach"
SEVERITY = {"high": 0, "medium": 1, "low": 2}

SYSTEM = """You write a short first email from a revenue consultant to the owner of an independent Australian motel
or hostel. Rules: 90-130 words; plain, friendly Australian English; open with the single most concrete finding about
THEIR property, with its number; mention one more finding in one sentence; offer the free 2-page audit (attached if
the facts say so) and a 15-minute call; one question at the end; no hype, no exclamation marks, no buzzwords, no
emoji; never invent facts beyond the list given; do not add a signature or unsubscribe line (they are appended).
Return only the email body."""


@dataclass
class Draft:
    client: Client
    subject: str
    body: str
    findings: list[str]
    attachment: Path | None


def gather(ctx: AgentContext, client: Client) -> tuple[list[str], Path | None, int | None]:
    findings = ctx.session.exec(select(Finding).where(
        Finding.client_id == client.id, Finding.agent.in_(("prospect", "funnel_checker", "gbp_monitor"))
    )).all()
    facts = [f.text for f in sorted(findings, key=lambda f: (SEVERITY.get(f.severity, 3), -(f.data or {}).get(
        "points", 0)))]
    audit = ctx.session.exec(select(ListingAudit).where(ListingAudit.client_id == client.id)
                             .order_by(ListingAudit.date.desc())).first()
    score = audit.score if audit else None
    if audit:
        facts += [f"Booking.com listing scores {audit.score}/100 on our audit."]
        facts += [f"Top listing fix: {fx['fix']}" for fx in (audit.findings or {}).get("fixes", [])[:1]]
    pdfs = sorted((ctx.settings.output_dir / f"client-{client.id}").glob("free-audit-*.pdf"))
    return list(dict.fromkeys(facts)), (pdfs[-1] if pdfs else None), score


def stub_body(client: Client, facts: list[str], attached: bool) -> str:
    first = facts[0] if facts else "I had a look at how it shows up on Google and Booking.com."
    second = f" I also noticed: {facts[1][0].lower() + facts[1][1:]}" if len(facts) > 1 else ""
    audit = ("I've attached a free 2-page audit with the fixes ranked." if attached
             else "I can send you a free 2-page audit with the fixes ranked.")
    return (f"Hi,\n\nI work with independent motels on the Sunshine Coast on bookings and pricing, and I had a look at "
            f"{client.name}. {first}{second}\n\n{audit} Most of it takes an owner under an hour to fix, and I only "
            "charge if revenue beats the same period last year.\n\nWould a 15-minute call next week be useful?")


def draft(ctx: AgentContext, client: Client) -> Draft:
    facts, pdf, _ = gather(ctx, client)
    prompt = (f"Property: {client.name} ({client.property_type}, {client.region}).\n"
              f"Free audit attached: {'yes' if pdf else 'no'}.\nFindings, most important first:\n"
              + "\n".join(f"- {f}" for f in facts[:6]))
    body = ctx.claude.text(SYSTEM, prompt, agent=NAME, client_id=client.id,
                           stub=lambda: stub_body(client, facts, pdf is not None), max_tokens=1200)
    subject = f"A quick look at {client.name} on Google and Booking.com"
    return Draft(client=client, subject=subject, body=body.strip(), findings=facts, attachment=pdf)


def footer(ctx: AgentContext) -> str:
    s = ctx.settings
    contact = f" · {s.sender_contact}" if s.sender_contact else ""
    return (f"\n\n{s.sender_name}\n{s.sender_business}{contact}\n\n"
            "If you'd rather not hear from me again, reply \"unsubscribe\" and I won't email you again.")


def deliver(ctx: AgentContext, d: Draft, to: str, *, send: bool) -> mailer.Sent:
    msg = mailer.new_message(ctx.settings, to, d.subject, d.body + footer(ctx))
    if d.attachment and d.attachment.exists():
        msg.add_attachment(d.attachment.read_bytes(), maintype="application", subtype="pdf",
                           filename=f"{d.client.name} - free revenue audit.pdf")
    return mailer.deliver(ctx.settings, msg, send=send, session=ctx.session, stem=f"outreach-client{d.client.id}")
