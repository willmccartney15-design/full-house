"""Agent 2 — OTA Listing Auditor.

Scrapes the client's OTA listings, extracts what guests and OTA ranking
algorithms see, scores it 0–100 across 8 criteria and returns a ranked fix
list with effort estimates.

A deterministic rubric scores every listing first. Claude then reviews the
extracted facts and the rubric score and writes the final scores and fixes;
in dry runs the rubric result is used as is.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from sqlmodel import select

from fullhouse.agents.base import AgentContext, AgentResult
from fullhouse.core.fetch import FetchError
from fullhouse.core.models import Client, ListingAudit
from fullhouse.core.scrapers import ListingData, parse_listing

NAME = "listing_auditor"

CRITERIA = (
    "photos",
    "description",
    "amenities",
    "room_type_clarity",
    "cancellation_policy",
    "loyalty_program",
    "review_responses",
    "review_score",
)


class CriterionScore(BaseModel):
    criterion: Literal[CRITERIA]  # type: ignore[valid-type]
    score: int = Field(description="0-100")
    evidence: str = Field(description="One sentence quoting what was found on the listing")


class Fix(BaseModel):
    rank: int
    fix: str = Field(description="Specific action the owner can take, imperative mood")
    criterion: Literal[CRITERIA]  # type: ignore[valid-type]
    effort: Literal["low", "medium", "high"]
    expected_impact: str = Field(description="Why this moves bookings or ranking, one sentence")


class AuditResult(BaseModel):
    criteria: list[CriterionScore]
    fixes: list[Fix]

    @property
    def score(self) -> int:
        return round(sum(c.score for c in self.criteria) / len(self.criteria)) if self.criteria else 0


SYSTEM = """You audit OTA listings (Booking.com, HostelWorld, Expedia) for independent Australian motels and hostels.
Score each of the 8 criteria 0-100 from the extracted listing facts, and rank the fixes by expected booking impact
per hour of owner effort. Be concrete: name counts, missing items and exact settings. Do not invent facts that are not
in the data; when a fact is missing, say it could not be verified."""

RUBRIC = """Criteria and benchmarks:
- photos: 40+ photos is best practice for Booking.com ranking; under 20 is weak.
- description: 600+ characters naming location, parking, room features and nearby attractions.
- amenities: 10+ listed facilities; parking, WiFi, air conditioning matter most for motels; lockers, kitchen and
  common room for hostels.
- room_type_clarity: distinct, descriptive room names with bed configuration; each room priced.
- cancellation_policy: a free-cancellation rate option lifts conversion and ranking.
- loyalty_program: Booking.com Genius (or HostelWorld deals / Expedia member prices) participation.
- review_responses: reply to 80%+ of reviews, all negatives within 48 hours.
- review_score: 8.5+/10 is strong; under 8.0 hurts ranking."""


def rubric(listing: ListingData) -> AuditResult:
    """Deterministic baseline score; also the dry-run output."""
    scores: dict[str, tuple[int, str]] = {}
    fixes: list[tuple[float, Fix]] = []

    def add_fix(criterion, fix, effort, impact, weight):
        fixes.append((weight, Fix(rank=0, fix=fix, criterion=criterion, effort=effort, expected_impact=impact)))

    n = listing.photo_count
    scores["photos"] = (min(100, round(n / 40 * 100)), f"{n} photos on the listing.")
    if n < 40:
        add_fix("photos", f"Add {40 - n} more photos: every room type, bathroom, exterior at dusk, parking, pool, "
                "and the nearest beach or landmark.", "medium",
                "Photo count is a direct input to Booking.com search ranking and click-through.", (40 - n) / 40 * 3)

    d = len(listing.description)
    scores["description"] = (min(100, round(d / 600 * 100)), f"Description is {d} characters.")
    if d < 600:
        add_fix("description", "Rewrite the description to 600+ characters: distance to beach/CBD, free parking, "
                "room features, check-in hours and nearby attractions.", "low",
                "Fuller descriptions answer guest questions and lift conversion.", (600 - d) / 600 * 2.5)

    a = len(listing.amenities)
    scores["amenities"] = (min(100, round(a / 10 * 100)), f"{a} facilities listed.")
    if a < 10:
        add_fix("amenities", "Tick every facility you actually offer in the extranet (parking, air conditioning, "
                "fridge, kettle, laundry, BBQ, family rooms).", "low",
                "Facility filters hide listings that do not declare them.", (10 - a) / 10 * 2.5)

    rooms = listing.rooms
    priced = sum(1 for r in rooms if r.price)
    vague = sum(1 for r in rooms if r.name.lower().strip() in {"room", "standard room", "double room"})
    clarity = 0 if not rooms else round(100 * (priced / len(rooms)) * (1 - 0.3 * vague / len(rooms)))
    scores["room_type_clarity"] = (clarity, f"{len(rooms)} room types, {priced} with a visible price.")
    if not rooms or clarity < 80:
        add_fix("room_type_clarity", "Rename room types with bed configuration and view (e.g. 'Queen Room, Pool View') "
                "and make sure every room type is open for sale.", "low",
                "Clear names reduce bounce on the room table.", 1.5)

    scores["cancellation_policy"] = (90 if listing.free_cancellation else 35,
                                     "Free cancellation offered." if listing.free_cancellation
                                     else "No free-cancellation option found.")
    if not listing.free_cancellation:
        add_fix("cancellation_policy", "Add a flexible rate plan (free cancellation up to 48 hours before arrival) "
                "alongside the non-refundable rate.", "low",
                "Flexible rates convert better and are favoured in OTA sorting.", 2.8)

    scores["loyalty_program"] = (90 if listing.loyalty_program else 30,
                                 "Loyalty programme participation found." if listing.loyalty_program
                                 else "No Genius / member-price participation found.")
    if not listing.loyalty_program:
        add_fix("loyalty_program", "Join Booking.com Genius (level 1, 10% discount) and review results after 60 days.",
                "low", "Genius listings get a visibility boost and a badge for frequent travellers.", 2.6)

    rr = listing.review_response_rate
    if rr is None:
        scores["review_responses"] = (50, "Review responses could not be verified on the page.")
        add_fix("review_responses", "Check that every recent review has an owner reply, and answer negatives within "
                "48 hours.", "low", "Replies are visible to every guest reading reviews and lift conversion.", 0.5)
    else:
        scores["review_responses"] = (round(min(rr / 0.8, 1) * 100), f"{round(rr * 100)}% of visible reviews have "
                                      "a property response.")
        if rr < 0.8:
            add_fix("review_responses", "Reply to every review, negatives within 48 hours (the Review Responder agent "
                    "drafts these for approval).", "low",
                    "Responses signal an active host and lift review-driven conversion.", (0.8 - rr) * 3)

    rs = listing.review_score
    if rs is None:
        scores["review_score"] = (50, "Review score not found.")
    else:
        on_ten = rs / listing.review_scale * 10
        scores["review_score"] = (max(0, min(100, round((on_ten - 6.5) / 2.5 * 100))),
                                  f"Review score {rs}/{listing.review_scale:g} from {listing.review_count or '?'} reviews.")
        if on_ten < 8.5:
            add_fix("review_score", "Fix the two most-mentioned complaints in recent reviews, then ask happy guests "
                    "to review at checkout.", "high", "Score is a top ranking factor and sets price power.", 1.2)

    ranked = [f for _, f in sorted(fixes, key=lambda wf: -wf[0])]
    for i, f in enumerate(ranked, 1):
        f.rank = i
    return AuditResult(
        criteria=[CriterionScore(criterion=c, score=scores[c][0], evidence=scores[c][1]) for c in CRITERIA],
        fixes=ranked,
    )


def audit_listing(ctx: AgentContext, client: Client, listing: ListingData) -> AuditResult:
    baseline = rubric(listing)
    facts = listing.to_dict()
    facts["description"] = facts["description"][:2000]
    prompt = (
        f"Property: {client.name} ({client.property_type}, {client.region}).\n"
        f"OTA: {listing.ota}\n\n{RUBRIC}\n\nExtracted listing facts (JSON):\n{facts}\n\n"
        f"Baseline rubric scores (JSON):\n{baseline.model_dump_json()}\n\n"
        "Return the final 8 criterion scores and a ranked fix list (most valuable first, at most 8 fixes)."
    )
    result = ctx.claude.structured(
        SYSTEM, prompt, AuditResult, agent=NAME, client_id=client.id, stub=lambda: baseline
    )
    for i, fix in enumerate(sorted(result.fixes, key=lambda f: f.rank), 1):
        fix.rank = i
    return result


def audit_and_store(ctx: AgentContext, client: Client, listing: ListingData) -> dict:
    """Score one parsed listing and save it as today's ListingAudit for that OTA (replacing any earlier one)."""
    audit = audit_listing(ctx, client, listing)
    for old in ctx.session.exec(
        select(ListingAudit).where(
            ListingAudit.client_id == client.id, ListingAudit.date == ctx.today, ListingAudit.ota == listing.ota
        )
    ):
        ctx.session.delete(old)
    findings = {**audit.model_dump(), "listing": listing.to_dict()}
    ctx.session.add(
        ListingAudit(client_id=client.id, date=ctx.today, ota=listing.ota, score=audit.score, findings=findings)
    )
    ctx.session.commit()
    return {"score": audit.score, **findings}


def run(ctx: AgentContext, client: Client) -> AgentResult:
    result = AgentResult(agent=NAME, client_id=client.id, summary="")
    audits = {}
    for ota, url in (client.ota_urls or {}).items():
        if ota not in ("booking", "hostelworld", "expedia") or not url:
            continue
        try:
            page = ctx.fetcher.get(url, render=True)
        except FetchError as exc:
            result.errors.append(f"{ota}: {exc}")
            continue
        if page.status >= 400:
            result.errors.append(f"{ota}: HTTP {page.status}")
            continue
        listing = parse_listing(ota, page.html, url)
        audits[ota] = audit_and_store(ctx, client, listing)
    ctx.session.commit()
    result.data = {"audits": audits}
    result.summary = ", ".join(f"{ota} {a['score']}/100 ({len(a['fixes'])} fixes)" for ota, a in audits.items()) or (
        "No OTA listings audited."
    )
    return result
