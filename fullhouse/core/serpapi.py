"""SerpApi Google Hotels client: licensed access to Google Hotels results (prices by booking site, ratings,
amenities) without fetching any OTA.

SerpApi takes its key as the ``api_key`` URL parameter, read here from SERPAPI_API_KEY. Every search costs one
SerpApi credit, so responses are cached on disk for 24 hours (SerpApi's own cache only lasts an hour); re-running a
prospect scan the same day is free.

Docs: https://serpapi.com/google-hotels-api and https://serpapi.com/google-hotels-property-details
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

ENDPOINT = "https://serpapi.com/search.json"
PROPERTY_TYPES = {"motel": 16, "hostel": 14, "inn": 15, "bnb": 19, "apartment": 21, "resort": 17, "boutique": 13}


class SerpApiError(RuntimeError):
    pass


class HotelsSource(Protocol):
    calls: int

    def search(self, query: str, checkin: date, checkout: date, *, property_types: list[int] | None = None,
               page_token: str | None = None) -> dict: ...

    def details(self, property_token: str, checkin: date, checkout: date) -> dict: ...


@dataclass
class SerpApiClient:
    cache_dir: Path
    api_key: str | None = field(default_factory=lambda: os.getenv("SERPAPI_API_KEY") or None)
    currency: str = "AUD"
    gl: str = "au"
    hl: str = "en"
    adults: int = 2
    cache_ttl_hours: float = 24
    calls: int = 0  # paid searches made (cache hits not counted)

    def search(self, query, checkin, checkout, *, property_types=None, page_token=None) -> dict:
        params = {"q": query, **self._stay(checkin, checkout)}
        if property_types:
            params["property_types"] = ",".join(str(t) for t in property_types)
        if page_token:
            params["next_page_token"] = page_token
        return self._get(params)

    def details(self, property_token, checkin, checkout) -> dict:
        return self._get({"property_token": property_token, **self._stay(checkin, checkout)})

    # ------------------------------------------------------------------ internals

    def _stay(self, checkin: date, checkout: date) -> dict:
        return {"check_in_date": checkin.isoformat(), "check_out_date": checkout.isoformat(), "adults": self.adults}

    def _get(self, params: dict) -> dict:
        params = {"engine": "google_hotels", "currency": self.currency, "gl": self.gl, "hl": self.hl, **params}
        key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        path = self.cache_dir / "serpapi" / f"{key}.json"
        if path.exists() and time.time() - path.stat().st_mtime < self.cache_ttl_hours * 3600:
            return json.loads(path.read_text())
        if not self.api_key:
            raise SerpApiError(
                "SERPAPI_API_KEY is not set. Add it as an environment variable (in the cloud environment's settings "
                "for this project, or in .env on your own machine), then start a new session or shell."
            )
        try:
            resp = httpx.get(ENDPOINT, params={**params, "api_key": self.api_key}, timeout=90)
        except httpx.HTTPError as exc:
            raise SerpApiError(f"SerpApi unreachable: {exc}") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise SerpApiError(f"SerpApi returned HTTP {resp.status_code} without JSON") from exc
        if resp.status_code >= 400 or data.get("error"):
            message = data.get("error") or f"HTTP {resp.status_code}"
            if "hasn't returned any results" in str(message):
                data = {"properties": [], "search_metadata": data.get("search_metadata", {})}
            else:
                raise SerpApiError(f"SerpApi: {message}")
        self.calls += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return data


# ---------------------------------------------------------------------- parsing


def _num(d: dict | None, *keys: str) -> float | None:
    for k in keys:
        v = (d or {}).get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def nightly(block: dict | None) -> float | None:
    """Per-night price including taxes and fees where Google gives it (what a guest pays)."""
    return _num(block, "extracted_lowest", "extracted_after_taxes_fees", "extracted_display_price")


@dataclass
class SourcePrice:
    source: str
    price: float | None  # per night
    official: bool  # the property's own site (direct booking)


@dataclass
class HotelData:
    name: str
    token: str | None
    type: str = ""
    price: float | None = None  # lowest per night across booking sites
    rating: float | None = None  # Google, out of 5
    reviews: int | None = None
    hotel_class: int | None = None
    amenities: list[str] = field(default_factory=list)
    excluded_amenities: list[str] = field(default_factory=list)
    images: int = 0
    website: str | None = None
    address: str | None = None
    phone: str | None = None
    prices: list[SourcePrice] = field(default_factory=list)
    other_reviews: list[dict[str, Any]] = field(default_factory=list)  # {"source", "score", "max", "reviews"}
    has_details: bool = False

    @property
    def direct_price(self) -> float | None:
        p = [s.price for s in self.prices if s.official and s.price]
        return min(p) if p else None

    @property
    def best_ota(self) -> SourcePrice | None:
        p = [s for s in self.prices if not s.official and s.price]
        return min(p, key=lambda s: s.price) if p else None

    def source_price(self, name: str) -> float | None:
        p = [s.price for s in self.prices if s.price and s.source.lower().startswith(name.lower())]
        return min(p) if p else None


def parse_property(item: dict) -> HotelData:
    return HotelData(
        name=item.get("name", "").strip(),
        token=item.get("property_token"),
        type=item.get("type", ""),
        price=nightly(item.get("rate_per_night")),
        rating=_num(item, "overall_rating"),
        reviews=int(item["reviews"]) if isinstance(item.get("reviews"), (int, float)) else None,
        hotel_class=int(item["extracted_hotel_class"]) if item.get("extracted_hotel_class") else None,
        amenities=list(item.get("amenities") or []),
        excluded_amenities=list(item.get("excluded_amenities") or []),
        images=len(item.get("images") or []),
        website=item.get("link"),
    )


def merge_details(h: HotelData, d: dict) -> HotelData:
    """Fill a search result with its property-details response."""
    h.has_details = True
    h.name = d.get("name") or h.name
    h.address, h.phone = d.get("address") or h.address, d.get("phone") or h.phone
    h.website = d.get("link") or h.website
    h.rating = _num(d, "overall_rating") or h.rating
    if isinstance(d.get("reviews"), (int, float)):
        h.reviews = int(d["reviews"])
    h.amenities = list(d.get("amenities") or h.amenities)
    h.excluded_amenities = list(d.get("excluded_amenities") or h.excluded_amenities)
    h.images = max(h.images, len(d.get("images") or []))
    h.prices = [
        SourcePrice(source=p.get("source", "?"), price=nightly(p.get("rate_per_night")), official=bool(p.get("official")))
        for p in d.get("prices") or []
    ]
    if h.prices and (low := [s.price for s in h.prices if s.price]):
        h.price = min(low)
    h.other_reviews = [
        {"source": r.get("source"), "score": _num(r.get("source_rating"), "score"),
         "max": _num(r.get("source_rating"), "max_score"), "reviews": r.get("reviews")}
        for r in d.get("other_reviews") or []
    ]
    return h
