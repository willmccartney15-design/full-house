"""OTA parsers, dispatched by URL."""

from __future__ import annotations

from datetime import date

from fullhouse.core.scrapers import booking, expedia, hostelworld
from fullhouse.core.scrapers.common import ListingData, RoomOffer, SearchResult

PARSERS = {"booking": booking, "hostelworld": hostelworld, "expedia": expedia}


def ota_for_url(url: str) -> str:
    if booking.is_booking_url(url):
        return "booking"
    if hostelworld.is_hostelworld_url(url):
        return "hostelworld"
    if expedia.is_expedia_url(url):
        return "expedia"
    raise ValueError(f"Not a supported OTA URL: {url}")


def parse_listing(ota: str, html: str, url: str) -> ListingData:
    return PARSERS[ota].parse_listing(html, url)


def dated_url(ota: str, url: str, checkin: date) -> str:
    return PARSERS[ota].with_dates(url, checkin)


__all__ = ["ListingData", "RoomOffer", "SearchResult", "ota_for_url", "parse_listing", "dated_url", "booking"]
