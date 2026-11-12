"""HostelWorld listing parsing (JSON-LD first, page selectors as fallback)."""

from __future__ import annotations

from datetime import date
from urllib.parse import urlencode, urlsplit

from fullhouse.core.scrapers.common import (
    ListingData,
    RoomOffer,
    apply_json_ld,
    first,
    parse_price,
    select_all,
    soup,
    text,
)

OTA = "hostelworld"


def is_hostelworld_url(url: str) -> bool:
    return "hostelworld.com" in urlsplit(url).netloc


def with_dates(url: str, checkin: date, nights: int = 1) -> str:
    return url.split("?")[0] + "?" + urlencode({"from": checkin.isoformat(), "nights": nights, "guests": 1})


def parse_listing(html: str, url: str) -> ListingData:
    doc = soup(html)
    listing = ListingData(ota=OTA, url=url)
    listing.name = text(first(doc, "h1.property-name", "h1"))
    listing.description = text(first(doc, ".property-description", ".description", "#description"))
    listing.photo_count = len(select_all(doc, ".gallery img", ".property-gallery img", "[data-gallery-image]"))
    listing.amenities = [text(n) for n in select_all(doc, ".facilities li", ".amenities li") if text(n)]
    for row in select_all(doc, ".room-row", ".rate-type", ".room-item"):
        name = text(first(row, ".room-title", ".title", "h3"))
        if name:
            listing.rooms.append(RoomOffer(name=name, price=parse_price(text(first(row, ".price", ".rate")))))
    listing.policies_text = text(first(doc, ".house-rules", ".policies"))
    page_text = doc.get_text(" ").lower()
    listing.free_cancellation = "free cancellation" in page_text
    listing.loyalty_program = "hostelworld deal" in page_text or "member price" in page_text
    reviews = select_all(doc, ".review-item", ".review")
    if reviews:
        responded = sum(1 for r in reviews if first(r, ".property-reply", ".owner-response"))
        listing.review_response_rate = round(responded / len(reviews), 2)
    apply_json_ld(listing, doc)
    return listing
