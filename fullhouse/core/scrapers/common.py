"""Shared listing data model and HTML helpers."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from bs4 import BeautifulSoup, Tag

PRICE_RE = re.compile(r"(?:A?\$|AUD\s?)\s?([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)")


@dataclass
class RoomOffer:
    name: str
    price: float | None = None  # per night, lowest shown


@dataclass
class ListingData:
    ota: str
    url: str
    name: str = ""
    locality: str = ""
    region: str = ""
    description: str = ""
    photo_count: int = 0
    amenities: list[str] = field(default_factory=list)
    rooms: list[RoomOffer] = field(default_factory=list)
    policies_text: str = ""
    free_cancellation: bool = False
    loyalty_program: bool = False  # Genius / HostelWorld deals / Expedia member prices
    review_score: float | None = None  # on the OTA's own scale
    review_scale: float = 10.0
    review_count: int | None = None
    review_response_rate: float | None = None  # 0..1 over the reviews visible on the page
    nights: int = 1  # length of stay the page was priced for; room prices are already per night

    @property
    def room_types(self) -> list[str]:
        return sorted({r.name for r in self.rooms})

    def to_dict(self) -> dict:
        data = asdict(self)
        data["room_types"] = self.room_types
        return data


@dataclass
class SearchResult:
    name: str
    url: str
    price: float | None = None


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def text(node: Tag | None) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node else ""


def first(doc: BeautifulSoup | Tag, *selectors: str) -> Tag | None:
    for sel in selectors:
        if (node := doc.select_one(sel)) is not None:
            return node
    return None


def select_all(doc: BeautifulSoup | Tag, *selectors: str) -> list[Tag]:
    for sel in selectors:
        if nodes := doc.select(sel):
            return nodes
    return []


def parse_price(raw: str) -> float | None:
    match = PRICE_RE.search(raw.replace("\xa0", " "))
    return float(match.group(1).replace(",", "")) if match else None


def json_ld(doc: BeautifulSoup, *types: str) -> dict:
    """First JSON-LD object whose @type is one of ``types``."""
    for script in doc.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in candidates:
            if isinstance(item, dict):
                t = item.get("@type")
                t = t if isinstance(t, list) else [t]
                if any(x in types for x in t):
                    return item
    return {}


LODGING_TYPES = ("Hotel", "Motel", "Hostel", "LodgingBusiness", "BedAndBreakfast", "Resort", "Campground")


def apply_json_ld(listing: ListingData, doc: BeautifulSoup) -> None:
    ld = json_ld(doc, *LODGING_TYPES)
    if not ld:
        return
    listing.name = listing.name or ld.get("name", "")
    listing.description = listing.description or ld.get("description", "")
    addr = ld.get("address") or {}
    if isinstance(addr, dict):
        listing.locality = listing.locality or addr.get("addressLocality", "")
        listing.region = listing.region or addr.get("addressRegion", "")
    rating = ld.get("aggregateRating") or {}
    if rating:
        listing.review_score = listing.review_score or _num(rating.get("ratingValue"))
        listing.review_scale = _num(rating.get("bestRating")) or listing.review_scale
        count = rating.get("reviewCount") or rating.get("ratingCount")
        listing.review_count = listing.review_count or (int(_num(count)) if _num(count) is not None else None)
    images = ld.get("image")
    if not listing.photo_count and images:
        listing.photo_count = len(images) if isinstance(images, list) else 1
    amen = ld.get("amenityFeature") or []
    if not listing.amenities and isinstance(amen, list):
        listing.amenities = [a.get("name", "") for a in amen if isinstance(a, dict) and a.get("name")]


def _num(value) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def count_from_text(doc: BeautifulSoup, pattern: str) -> int | None:
    match = re.search(pattern, doc.get_text(" "), flags=re.I)
    return int(match.group(1).replace(",", "")) if match else None
