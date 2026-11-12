"""Agent 3 — Review Responder.

Collects new reviews (Google Business Profile API where configured, OTA pages
otherwise, or a JSON import), and drafts a reply to each in the property's
voice. Drafts land in Review.draft_reply; nothing is posted without approval.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import httpx
from sqlmodel import select

from fullhouse.agents.base import AgentContext, AgentResult, log
from fullhouse.core.models import Client, Review

NAME = "review_responder"

DEFAULT_VOICE = "Warm, brief and specific. Australian English. Sign off with the owner's first name if known."

SYSTEM = """You write replies to guest reviews for an independent Australian motel or hostel, in the owner's voice.
Rules: under 90 words; thank the guest by name if given; mention one specific thing they raised; for complaints,
apologise once, say what has been done or will be done (only if the review or owner notes support it; otherwise
invite them to get in touch directly), and never argue or share private details; never offer refunds or discounts;
no emoji; no hashtags. Return only the reply text."""


def import_reviews(ctx: AgentContext, client: Client, path: Path) -> int:
    """Load reviews from a JSON list of {source, external_id, date, author, rating, text}."""
    return _store(ctx, client, json.loads(Path(path).read_text()))


def fetch_gbp_reviews(ctx: AgentContext, client: Client) -> list[dict]:
    """Google Business Profile reviews via the My Business v4 API.

    Needs GBP_ACCESS_TOKEN (OAuth, scope business.manage) and client.gbp_url set to the
    resource name 'accounts/{account_id}/locations/{location_id}'.
    """
    token, resource = ctx.settings.gbp_access_token, client.gbp_url or ""
    if not token or not resource.startswith("accounts/"):
        return []
    resp = httpx.get(
        f"https://mybusiness.googleapis.com/v4/{resource}/reviews",
        headers={"Authorization": f"Bearer {token}"},
        params={"pageSize": 50, "orderBy": "updateTime desc"},
        timeout=30,
    )
    resp.raise_for_status()
    stars = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
    out = []
    for r in resp.json().get("reviews", []):
        if r.get("reviewReply"):
            continue  # already answered on Google
        out.append(
            {
                "source": "google",
                "external_id": r.get("reviewId"),
                "date": (r.get("createTime") or date.today().isoformat())[:10],
                "author": (r.get("reviewer") or {}).get("displayName"),
                "rating": stars.get(r.get("starRating")),
                "text": r.get("comment", ""),
            }
        )
    return out


def _store(ctx: AgentContext, client: Client, items: list[dict]) -> int:
    added = 0
    for item in items:
        ext = item.get("external_id") or hashlib.sha1(
            f"{item.get('source')}:{item.get('author')}:{item.get('text')}".encode()
        ).hexdigest()[:16]
        exists = ctx.session.exec(
            select(Review).where(Review.client_id == client.id, Review.external_id == ext)
        ).first()
        if exists:
            continue
        ctx.session.add(
            Review(
                client_id=client.id,
                source=item.get("source", "unknown"),
                external_id=ext,
                date=date.fromisoformat(str(item.get("date"))[:10]) if item.get("date") else ctx.today,
                author=item.get("author"),
                rating=item.get("rating"),
                text=item.get("text", ""),
            )
        )
        added += 1
    ctx.session.commit()
    return added


def is_negative(review: Review) -> bool:
    """Google rates out of 5; the OTAs out of 10."""
    if review.rating is None:
        return False
    return review.rating <= (3 if review.source == "google" else 6)


def stub_reply(client: Client, review: Review) -> str:
    name = (review.author or "").split()[0] if review.author else ""
    greeting = f"Hi {name}, thanks" if name else "Thanks"
    if is_negative(review):
        return (f"{greeting} for taking the time to write this. We're sorry your stay at {client.name} fell short. "
                "We've passed your comments to the team and would like to hear more, so please get in touch with us "
                "directly.")
    return f"{greeting} for staying with us at {client.name} and for the kind words. We hope to see you again soon."


def draft(ctx: AgentContext, client: Client, review: Review) -> str:
    scale = "/5" if review.source == "google" else "/10"
    prompt = (
        f"Property: {client.name}, {client.property_type} in {client.region}.\n"
        f"Owner voice notes: {client.voice_profile or DEFAULT_VOICE}\n\n"
        f"Review on {review.source}, rating {review.rating}{scale}, by {review.author or 'a guest'}:\n"
        f"<review>{review.text}</review>\n\nWrite the reply."
    )
    return ctx.claude.text(SYSTEM, prompt, agent=NAME, client_id=client.id, stub=lambda: stub_reply(client, review),
                           max_tokens=1000)


def run(ctx: AgentContext, client: Client) -> AgentResult:
    result = AgentResult(agent=NAME, client_id=client.id, summary="")
    try:
        added = _store(ctx, client, fetch_gbp_reviews(ctx, client))
    except httpx.HTTPError as exc:
        added = 0
        result.errors.append(f"Google Business Profile: {exc}")
    if ctx.dry_run and (sample := ctx.extras.get("sample_reviews")):
        added += import_reviews(ctx, client, sample)

    pending = list(
        ctx.session.exec(select(Review).where(Review.client_id == client.id, Review.draft_reply.is_(None)))
    )
    for review in pending:
        try:
            review.draft_reply = draft(ctx, client, review)
            ctx.session.add(review)
        except Exception as exc:  # one bad review should not stop the rest
            log.exception("draft failed")
            result.errors.append(f"review {review.id}: {exc}")
    ctx.session.commit()
    result.data = {
        "new_reviews": added,
        "drafted": [{"id": r.id, "source": r.source, "rating": r.rating, "draft": r.draft_reply} for r in pending],
    }
    result.summary = f"{added} new reviews collected, {len(pending)} replies drafted for approval."
    return result
