"""Expedia listing parsing (JSON-LD first, page selectors as fallback)."""

from __future__ import annotations

from datetime import date, timedelta
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

OTA = "expedia"


def is_expedia_url(url: str) -> bool:
    return "expedia." in urlsplit(url).netloc


def with_dates(url: str, checkin: date, nights: int = 1) -> str:
    q = {"chkin": checkin.isoformat(), "chkout": (checkin + timedelta(days=nights)).isoformat(), "rm1": "a2"}
    return url.split("?")[0] + "?" + urlencode(q)


def parse_listing(html: str, url: str) -> ListingData:
    doc = soup(html)
    listing = ListingData(ota=OTA, url=url)
    listing.name = text(first(doc, '[data-stid="content-hotel-title"] h1', "h1"))
    listing.description = text(first(doc, '[data-stid="content-markup"]', "#Overview p"))
    listing.photo_count = len(select_all(doc, '[data-stid="gallery-image"]', "figure img"))
    listing.amenities = [text(n) for n in select_all(doc, '[data-stid="hotel-amenities-list"] li') if text(n)]
    for card in select_all(doc, '[data-stid="property-offer"]', '[data-stid^="property-offer-"]'):
        name = text(first(card, "h3", '[data-stid="offer-title"]'))
        if name:
            listing.rooms.append(RoomOffer(name=name, price=parse_price(text(first(card, '[data-stid="price-summary"]')))))
    listing.policies_text = text(first(doc, '[data-stid="content-item"] #Policies', "#Policies"))
    page_text = doc.get_text(" ").lower()
    listing.free_cancellation = "fully refundable" in page_text or "free cancellation" in page_text
    listing.loyalty_program = "member price" in page_text or "one key" in page_text
    apply_json_ld(listing, doc)
    return listing
