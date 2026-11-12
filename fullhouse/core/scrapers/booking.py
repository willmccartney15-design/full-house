"""Booking.com listing and search-result parsing.

Selectors target Booking.com's markup as of late 2026 (data-testid attributes
first, older class names as fallbacks). OTA markup changes without notice:
when a parser starts returning empty fields, save the live page into
tests/fixtures and update the selectors against it.
"""

from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from fullhouse.core.scrapers.common import (
    ListingData,
    RoomOffer,
    SearchResult,
    apply_json_ld,
    count_from_text,
    first,
    parse_price,
    select_all,
    soup,
    text,
)

OTA = "booking"


def is_booking_url(url: str) -> bool:
    return "booking.com" in urlsplit(url).netloc


def with_dates(url: str, checkin: date, nights: int = 1, adults: int = 2) -> str:
    """Hotel or search URL with check-in/out dates so the page shows bookable prices."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query.update(
        checkin=checkin.isoformat(),
        checkout=(checkin + timedelta(days=nights)).isoformat(),
        group_adults=str(adults),
        no_rooms="1",
        selected_currency="AUD",
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def search_url(region: str, checkin: date, nights: int = 1) -> str:
    base = "https://www.booking.com/searchresults.en-gb.html?" + urlencode({"ss": region})
    return with_dates(base, checkin, nights)


def parse_listing(html: str, url: str) -> ListingData:
    doc = soup(html)
    listing = ListingData(ota=OTA, url=url)

    listing.name = text(first(doc, '[data-testid="property-name"]', "h2.pp-header__title", "#hp_hotel_name"))
    listing.description = text(
        first(doc, '[data-testid="property-description"]', "#property_description_content", "#summary")
    )
    address = text(first(doc, '[data-testid="address"]', ".hp_address_subtitle"))
    if address:
        parts = [p.strip() for p in address.split(",")]
        if len(parts) >= 2:
            listing.locality = parts[-2] if len(parts) >= 3 else parts[0]
            listing.region = parts[-1]

    photos = select_all(doc, '[data-testid="gallery-image"]', ".bh-photo-grid-item", "#photo_wrapper img")
    more = count_from_text(doc, r"\+\s?(\d+)\s+photos")
    listing.photo_count = len(photos) + (more or 0)

    facilities = select_all(
        doc,
        '[data-testid="property-most-popular-facilities-wrapper"] li',
        '[data-testid="facility-list-most-popular-facilities"] li',
        ".hp_desc_important_facilities li",
    )
    listing.amenities = [t for t in (text(f) for f in facilities) if t]

    for row in select_all(doc, '[data-testid="room-row"]', "#hprt-table tbody tr"):
        name = text(first(row, '[data-testid="room-name"]', ".hprt-roomtype-icon-link", ".hprt-roomtype-link"))
        price_node = first(
            row, '[data-testid="price-and-discounted-price"]', ".bui-price-display__value", ".prco-valign-middle-helper"
        )
        price = parse_price(text(price_node))
        if name:
            listing.rooms.append(RoomOffer(name=name, price=price))
        elif price is not None and listing.rooms:  # extra rate plan row for the previous room type
            prev = listing.rooms[-1]
            if prev.price is None or price < prev.price:
                prev.price = price

    policies = first(doc, '[data-testid="property-section--policies"]', "#hotelPoliciesInc", "#policies")
    listing.policies_text = text(policies)
    page_text = doc.get_text(" ").lower()
    listing.free_cancellation = "free cancellation" in page_text
    listing.loyalty_program = "genius" in page_text

    score_node = first(doc, '[data-testid="review-score-component"] div', ".b5cd09854e.d10a6220b4")
    if score_node:
        try:
            listing.review_score = float(text(score_node).split()[0])
        except (ValueError, IndexError):
            pass
    listing.review_count = count_from_text(doc, r"([0-9][0-9,]*)\s+reviews")

    reviews = select_all(doc, '[data-testid="review-card"]', ".c-review-block")
    if reviews:
        responded = sum(1 for r in reviews if first(r, '[data-testid="review-response"]', ".c-review-block__response"))
        listing.review_response_rate = round(responded / len(reviews), 2)

    apply_json_ld(listing, doc)
    return listing


def parse_search(html: str, base_url: str = "https://www.booking.com") -> list[SearchResult]:
    doc = soup(html)
    results = []
    for card in doc.select('[data-testid="property-card"]'):
        link = first(card, 'a[data-testid="title-link"]', "a")
        name = text(first(card, '[data-testid="title"]'))
        if not link or not name:
            continue
        href = urljoin(base_url, link.get("href", "")).split("?")[0]
        price = parse_price(text(first(card, '[data-testid="price-and-discounted-price"]')))
        results.append(SearchResult(name=name, url=href, price=price))
    return results
