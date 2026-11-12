"""Agent 4 — Google Business Profile Monitor.

Checks hours, photos (count and recency), posts in the last 30 days, unanswered
Q&A and primary category, and records each gap as a Finding.

Data sources, best first:
- Business Profile API (GBP_ACCESS_TOKEN + client.gbp_url = 'accounts/{a}/locations/{l}'):
  posts and media with dates.
- Places API (GOOGLE_PLACES_API_KEY + client.gbp_place_id): hours, category,
  website, phone, rating; photo count is capped at 10 by that API.
- Dry run: fullhouse/sample/gbp.json.
Anything no source can supply is reported as "check manually", never guessed.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

import httpx

from fullhouse.agents.base import AgentContext, AgentResult
from fullhouse.core.models import Client

NAME = "gbp_monitor"
LODGING_CATEGORIES = {"motel", "hostel", "hotel", "lodging", "motor_inn", "inn", "guest_house", "bed_and_breakfast"}


@dataclass
class GbpSnapshot:
    has_hours: bool | None = None
    photo_count: int | None = None
    photo_count_capped: bool = False
    latest_photo_date: date | None = None
    posts_last_30d: int | None = None
    unanswered_qa: int | None = None
    primary_category: str | None = None
    website: str | None = None
    phone: str | None = None
    rating: float | None = None
    review_count: int | None = None


def from_places(api_key: str, place_id: str) -> GbpSnapshot:
    resp = httpx.get(
        f"https://places.googleapis.com/v1/places/{place_id}",
        headers={
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "regularOpeningHours,photos,primaryType,websiteUri,nationalPhoneNumber,"
            "rating,userRatingCount",
        },
        timeout=30,
    )
    resp.raise_for_status()
    d = resp.json()
    photos = d.get("photos") or []
    return GbpSnapshot(
        has_hours=bool(d.get("regularOpeningHours")),
        photo_count=len(photos),
        photo_count_capped=len(photos) >= 10,
        primary_category=d.get("primaryType"),
        website=d.get("websiteUri"),
        phone=d.get("nationalPhoneNumber"),
        rating=d.get("rating"),
        review_count=d.get("userRatingCount"),
    )


def add_business_profile(snap: GbpSnapshot, token: str, resource: str, today: date) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    posts = httpx.get(f"https://mybusiness.googleapis.com/v4/{resource}/localPosts", headers=headers, timeout=30)
    posts.raise_for_status()
    cutoff = today - timedelta(days=30)
    snap.posts_last_30d = sum(
        1 for p in posts.json().get("localPosts", []) if date.fromisoformat(p.get("createTime", "1970-01-01")[:10]) >= cutoff
    )
    media = httpx.get(f"https://mybusiness.googleapis.com/v4/{resource}/media", headers=headers, timeout=30)
    media.raise_for_status()
    items = media.json().get("mediaItems", [])
    if items:
        snap.photo_count, snap.photo_count_capped = len(items), False
        snap.latest_photo_date = max(date.fromisoformat(m.get("createTime", "1970-01-01")[:10]) for m in items)


def from_sample(path: Path) -> GbpSnapshot:
    d = json.loads(Path(path).read_text())
    if d.get("latest_photo_date"):
        d["latest_photo_date"] = date.fromisoformat(d["latest_photo_date"])
    return GbpSnapshot(**d)


def gaps(snap: GbpSnapshot, client: Client, today: date) -> list[tuple[str, str]]:
    """(severity, text) for every gap found."""
    out: list[tuple[str, str]] = []
    if snap.has_hours is False:
        out.append(("high", "No opening/reception hours set. Add reception hours and holiday hours."))
    if snap.photo_count is not None and not snap.photo_count_capped and snap.photo_count < 20:
        out.append(("medium", f"Only {snap.photo_count} photos. Add at least {20 - snap.photo_count} more: rooms, "
                    "exterior, signage, parking, pool."))
    if snap.latest_photo_date and (today - snap.latest_photo_date).days > 90:
        out.append(("medium", f"Newest photo is from {snap.latest_photo_date:%d %b %Y}. Upload fresh photos this month."))
    if snap.posts_last_30d is not None and snap.posts_last_30d == 0:
        out.append(("medium", "No posts in the last 30 days. Publish one offer or update post a week."))
    if snap.unanswered_qa:
        out.append(("high", f"{snap.unanswered_qa} unanswered questions on the profile. Answer them as the owner."))
    if snap.primary_category and snap.primary_category.lower() not in LODGING_CATEGORIES:
        out.append(("high", f"Primary category is '{snap.primary_category}'. Set it to "
                    f"'{client.property_type.value.title()}'."))
    elif snap.primary_category and client.property_type.value not in snap.primary_category.lower():
        out.append(("low", f"Primary category is '{snap.primary_category}'; consider "
                    f"'{client.property_type.value.title()}' as more specific."))
    if snap.website is None:
        out.append(("high", "No website link on the profile. Link the direct-booking site."))
    elif client.website_url and client.website_url.rstrip("/") not in snap.website.rstrip("/"):
        out.append(("low", f"Profile links to {snap.website}, not {client.website_url}."))
    if snap.phone is None:
        out.append(("medium", "No phone number on the profile."))
    return out


def unknowns(snap: GbpSnapshot) -> list[str]:
    checks = {"posts_last_30d": "posts in the last 30 days", "unanswered_qa": "unanswered Q&A",
              "latest_photo_date": "photo recency"}
    return [label for key, label in checks.items() if getattr(snap, key) is None]


def run(ctx: AgentContext, client: Client) -> AgentResult:
    result = AgentResult(agent=NAME, client_id=client.id, summary="")
    snap: GbpSnapshot | None = None
    if ctx.dry_run and (sample := ctx.extras.get("sample_gbp")):
        snap = from_sample(sample)
    else:
        try:
            if ctx.settings.google_places_api_key and client.gbp_place_id:
                snap = from_places(ctx.settings.google_places_api_key, client.gbp_place_id)
            if ctx.settings.gbp_access_token and (client.gbp_url or "").startswith("accounts/"):
                snap = snap or GbpSnapshot()
                add_business_profile(snap, ctx.settings.gbp_access_token, client.gbp_url, ctx.today)
        except httpx.HTTPError as exc:
            result.errors.append(f"Google API: {exc}")
    if snap is None:
        result.errors.append("No Google data source configured (GOOGLE_PLACES_API_KEY + gbp_place_id, "
                             "or GBP_ACCESS_TOKEN + gbp_url).")
        result.summary = "Google Business Profile not checked."
        return result

    ctx.clear_findings(client, NAME)
    found = gaps(snap, client, ctx.today)
    for severity, text in found:
        ctx.record(client, NAME, text, severity)
    manual = unknowns(snap)
    ctx.session.commit()
    result.data = {"snapshot": {k: (v.isoformat() if isinstance(v, date) else v) for k, v in asdict(snap).items()},
                   "gaps": [{"severity": s, "text": t} for s, t in found], "check_manually": manual}
    result.summary = f"{len(found)} profile gaps flagged" + (f"; check manually: {', '.join(manual)}." if manual else ".")
    return result
