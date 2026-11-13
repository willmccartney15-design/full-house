"""The owner approval loop.

Each week the agents' output becomes Proposals: a drafted review reply to post, the Monday report's three
actions, high-priority website / Google-profile / rate-parity findings, and the top listing fixes. The owner gets
one email with an Approve and a Skip button per proposal (see ``digest``), or works through them on their private
page in the approvals web app (``fullhouse.web``). Approved proposals become the to-do list (``fullhouse todo``);
an approved review reply is posted automatically when Google Business Profile access is configured, otherwise it
waits on the to-do list to be pasted in.

A proposal is created once per subject (``ref``): re-running the week doesn't re-ask about something the owner
already answered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlmodel import Session, select

from fullhouse.core.config import Settings
from fullhouse.core.metrics import monday
from fullhouse.core.models import (
    Action,
    ActionStatus,
    Client,
    Finding,
    ListingAudit,
    Proposal,
    ProposalKind,
    ProposalStatus,
    Review,
)

log = logging.getLogger(__name__)

MAX_PER_DIGEST = 10
SEVERITY_PRIORITY = {"high": 80, "medium": 50, "low": 20}
FIX_AGENTS = ("funnel_checker", "gbp_monitor")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BuildResult:
    created: list[Proposal] = field(default_factory=list)
    expired: int = 0


def build(session: Session, client: Client, today: date, ttl_days: int = 14) -> BuildResult:
    """Create proposals for anything new since last week; expire unanswered old ones."""
    result = BuildResult()
    existing = {p.ref for p in session.exec(select(Proposal).where(Proposal.client_id == client.id))}

    asked = {_norm(p.title) for p in session.exec(select(Proposal).where(Proposal.client_id == client.id))}

    def add(kind: ProposalKind, ref: str, title: str, detail: str, why: str, priority: int, **payload):
        # One question per subject: skip a ref already asked, or the same words already asked as another kind
        # (a finding that also became one of the Monday report's three actions).
        if ref in existing or _norm(title) in asked or _norm(detail) in asked:
            return
        asked.add(_norm(title))
        p = Proposal(client_id=client.id, kind=kind, ref=ref, title=title, detail=detail, why=why,
                     priority=priority, payload=payload)
        session.add(p)
        existing.add(ref)
        result.created.append(p)

    for r in session.exec(select(Review).where(
        Review.client_id == client.id, Review.draft_reply.is_not(None), Review.approved == False,  # noqa: E712
        Review.posted == False,  # noqa: E712
    )):
        scale = "/5" if r.source == "google" else "/10"
        negative = r.rating is not None and r.rating <= (3 if r.source == "google" else 6)
        share = (r.rating / (5 if r.source == "google" else 10)) if r.rating is not None else 0.7
        priority = 90 + round((1 - share) * 9) if negative else 60  # the worst reviews first
        add(ProposalKind.review_reply, f"review:{r.id}",
            f"Reply to {r.author or 'a guest'}'s {r.rating:g}{scale} review on {r.source.title()}"
            if r.rating is not None else f"Reply to a review on {r.source.title()}",
            r.draft_reply or "", f"“{_clip(r.text, 220)}”", priority, review_id=r.id)

    week = monday(today)
    for a in session.exec(select(Action).where(
        Action.client_id == client.id, Action.week_start == week, Action.status == ActionStatus.open
    )):
        add(ProposalKind.action, f"action:{a.id}", a.text, "", "One of this week's three actions from the "
            "Monday report.", 70, action_id=a.id)

    since = today - timedelta(days=7)
    for f in session.exec(select(Finding).where(
        Finding.client_id == client.id, Finding.agent.in_(FIX_AGENTS), Finding.date >= since,
        Finding.severity == "high",
    )):
        add(ProposalKind.fix, f"finding:{f.agent}:{_key(f.text)}", _clip(f.text, 160), f.text,
            f"Flagged by the {f.agent.replace('_', ' ')}.", SEVERITY_PRIORITY.get(f.severity, 50), finding_id=f.id)

    audit = session.exec(select(ListingAudit).where(ListingAudit.client_id == client.id)
                         .order_by(ListingAudit.date.desc())).first()
    if audit:
        for fx in (audit.findings or {}).get("fixes", [])[:3]:
            add(ProposalKind.listing_fix, f"listing:{audit.ota}:{fx.get('criterion')}", _clip(fx.get("fix", ""), 160),
                fx.get("fix", ""), fx.get("expected_impact", ""), 55 - int(fx.get("rank", 1)), effort=fx.get("effort"))

    cutoff = _now() - timedelta(days=ttl_days)
    for p in session.exec(select(Proposal).where(Proposal.client_id == client.id,
                                                 Proposal.status == ProposalStatus.pending)):
        if _aware(p.created_at) < cutoff:
            p.status, p.decided_at = ProposalStatus.expired, _now()
            session.add(p)
            result.expired += 1
    session.commit()
    return result


def pending(session: Session, client: Client, limit: int | None = None) -> list[Proposal]:
    rows = session.exec(select(Proposal).where(Proposal.client_id == client.id,
                                               Proposal.status == ProposalStatus.pending)).all()
    rows = sorted(rows, key=lambda p: (-p.priority, p.id or 0))
    return rows[:limit] if limit else rows


def by_token(session: Session, token: str) -> Proposal | None:
    return session.exec(select(Proposal).where(Proposal.token == token)).first() if token else None


class DecisionError(ValueError):
    pass


def decide(session: Session, settings: Settings, proposal: Proposal, approve: bool, *, note: str | None = None,
           edited_text: str | None = None) -> Proposal:
    """Record the owner's answer and apply its effects."""
    if proposal.status != ProposalStatus.pending:
        raise DecisionError(f"This was already {proposal.status.value}.")
    proposal.status = ProposalStatus.approved if approve else ProposalStatus.skipped
    proposal.decided_at, proposal.decided_note = _now(), (note or None)
    if proposal.kind == ProposalKind.review_reply:
        review = session.get(Review, proposal.payload.get("review_id"))
        if review is not None and approve:
            if edited_text and edited_text.strip():
                review.draft_reply = proposal.detail = edited_text.strip()
            review.approved = True
            session.add(review)
            client = session.get(Client, proposal.client_id)
            if _post_google_reply(settings, client, review):
                review.posted = True
                proposal.status, proposal.done_at = ProposalStatus.done, _now()
    elif proposal.kind == ProposalKind.action and not approve:
        action = session.get(Action, proposal.payload.get("action_id"))
        if action is not None:
            action.status = ActionStatus.skipped
            session.add(action)
    session.add(proposal)
    session.commit()
    return proposal


def mark_done(session: Session, proposal: Proposal) -> Proposal:
    if proposal.status not in (ProposalStatus.approved, ProposalStatus.pending):
        raise DecisionError(f"This is {proposal.status.value}; only approved items can be marked done.")
    proposal.status, proposal.done_at = ProposalStatus.done, _now()
    if proposal.kind == ProposalKind.action and (action := session.get(Action, proposal.payload.get("action_id"))):
        action.status = ActionStatus.done
        session.add(action)
    if proposal.kind == ProposalKind.review_reply and (review := session.get(Review, proposal.payload.get("review_id"))):
        review.posted = True
        session.add(review)
    session.add(proposal)
    session.commit()
    return proposal


def todo(session: Session, client_id: int | None = None) -> list[Proposal]:
    q = select(Proposal).where(Proposal.status == ProposalStatus.approved)
    if client_id is not None:
        q = q.where(Proposal.client_id == client_id)
    return sorted(session.exec(q).all(), key=lambda p: (p.client_id, -p.priority))


def _post_google_reply(settings: Settings, client: Client | None, review: Review) -> bool:
    """Post an approved reply to Google (Business Profile API v4) when access is configured."""
    resource = (client.gbp_url or "") if client else ""
    if review.source != "google" or not settings.gbp_access_token or not resource.startswith("accounts/") \
            or not review.external_id:
        return False
    try:
        resp = httpx.put(
            f"https://mybusiness.googleapis.com/v4/{resource}/reviews/{review.external_id}/reply",
            headers={"Authorization": f"Bearer {settings.gbp_access_token}"},
            json={"comment": review.draft_reply}, timeout=30,
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.warning("posting Google reply for review %s failed: %s", review.id, exc)
        return False


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split()).rstrip("…").strip()[:150]


def _key(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode()).hexdigest()[:12]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
