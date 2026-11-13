"""Prospecting pipeline: find every motel (or hostel...) in an area through Google Hotels (via SerpApi), audit
each one against the area, and rank them by how much Full House could improve them.

No OTA is fetched. Cost: one SerpApi search per results page (about 20 properties) plus one per property for
details (prices by booking site, address, phone, reviews elsewhere). ``details=False`` skips the second part.

Opportunity score (0-100, higher = better prospect) adds points for problems the service fixes:
direct booking missing or dearer than an OTA, price far from the area median, weaker rating or fewer reviews
than the area, missing amenities most of the area lists, few photos, no website.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from statistics import median

from sqlmodel import select

from fullhouse.agents.base import AgentContext
from fullhouse.core.models import Client, Finding, PropertyType
from fullhouse.core.serpapi import HotelData, HotelsSource, merge_details, parse_property
from fullhouse.reports.render import render_pdf

NAME = "prospect"
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Issue:
    severity: str
    text: str
    points: int


@dataclass
class ProspectReport:
    hotel: HotelData
    rank: int = 0
    opportunity: int = 0
    price_diff_pct: float | None = None
    issues: list[Issue] = field(default_factory=list)
    client_id: int | None = None
    pdf: Path | None = None

    @property
    def headline(self) -> str:
        top = sorted(self.issues, key=lambda i: (SEVERITY_ORDER[i.severity], -i.points))[:2]
        return " ".join(i.text for i in top) or "No major gaps found against the area."

    @property
    def direct_status(self) -> str:
        h = self.hotel
        if not h.has_details:
            return "not checked"
        if not any(s.price for s in h.prices):
            return "no prices shown"
        if h.direct_price is None:
            return "no direct price"
        ota = h.best_ota
        if ota and ota.price < h.direct_price:
            return f"{ota.source} cheaper"
        return "direct cheapest"


IMAGE_SAMPLE = 9  # SerpApi's property responses list at most this many images


@dataclass
class Area:
    query: str
    checkin: date
    nights: int
    count: int
    median_price: float | None
    median_rating: float | None
    median_reviews: float | None
    median_amenities: float | None
    common_amenities: list[str]  # listed by at least 60% of the area


@dataclass
class ProspectRun:
    area: Area
    reports: list[ProspectReport]
    searches_used: int
    shortlist_pdf: Path | None = None
    shortlist_csv: Path | None = None


# ------------------------------------------------------------------ collect


def collect(source: HotelsSource, query: str, checkin: date, checkout: date, *, property_types: list[int] | None,
            limit: int, details: bool) -> list[HotelData]:
    hotels: list[HotelData] = []
    seen: set[str] = set()
    token = None
    while len(hotels) < limit:
        page = source.search(query, checkin, checkout, property_types=property_types, page_token=token)
        for item in page.get("properties") or []:
            h = parse_property(item)
            key = h.token or h.name
            if not h.name or key in seen or h.type == "vacation rental":
                continue
            seen.add(key)
            hotels.append(h)
            if len(hotels) >= limit:
                break
        token = (page.get("serpapi_pagination") or {}).get("next_page_token")
        if not token or not page.get("properties"):
            break
    if details:
        for h in hotels:
            if h.token:
                merge_details(h, source.details(h.token, checkin, checkout))
    return hotels


# ------------------------------------------------------------------ analyse


def benchmark(hotels: list[HotelData], query: str, checkin: date, nights: int) -> Area:
    def med(values):
        values = [v for v in values if v is not None]
        return round(median(values), 2) if values else None

    counts = Counter(a.lower() for h in hotels for a in set(h.amenities))
    common = sorted(a for a, n in counts.items() if hotels and n / len(hotels) >= 0.6)
    return Area(
        query=query, checkin=checkin, nights=nights, count=len(hotels),
        median_price=med(h.price for h in hotels), median_rating=med(h.rating for h in hotels),
        median_reviews=med(h.reviews for h in hotels), median_amenities=med(len(h.amenities) for h in hotels),
        common_amenities=common,
    )


def assess(h: HotelData, area: Area, buffer_pct: float = 5.0) -> ProspectReport:
    r = ProspectReport(hotel=h)
    add = lambda sev, text, pts: r.issues.append(Issue(sev, text, pts))  # noqa: E731

    if h.has_details and any(s.price for s in h.prices):
        # Only judge direct booking when Google shows some price: with none at all (no availability, or Google
        # has no rates for these dates) there is no evidence either way.
        ota = h.best_ota
        if h.direct_price is None:
            add("high", "Google shows no price from the property's own site, so guests can only book through "
                        "OTAs paying commission.", 25)
        elif ota and ota.price < h.direct_price:
            add("high", f"{ota.source} sells it for ${ota.price:.0f} a night, ${h.direct_price - ota.price:.0f} "
                        "less than its own website.", 25)
        elif ota and (ota.price - h.direct_price) / ota.price * 100 < buffer_pct:
            add("medium", f"Booking direct saves guests only ${ota.price - h.direct_price:.0f} versus "
                          f"{ota.source}: too little to pull bookings away from OTAs.", 10)

    if h.price and area.median_price:
        r.price_diff_pct = round((h.price - area.median_price) / area.median_price * 100, 1)
        below_rating = h.rating is not None and area.median_rating is not None and h.rating < area.median_rating
        if r.price_diff_pct <= -15 and not below_rating:
            add("medium", f"Priced {abs(r.price_diff_pct):.0f}% below the area median (${h.price:.0f} vs "
                          f"${area.median_price:.0f}) despite reviews at or above the area's.", 15)
        elif r.price_diff_pct >= 15 and below_rating:
            add("medium", f"Priced {r.price_diff_pct:.0f}% above the area median with a below-median rating; "
                          "guests compare both.", 10)

    if h.rating is not None and area.median_rating is not None and h.rating < area.median_rating - 0.15:
        add("high" if h.rating < 4.0 else "medium",
            f"Google rating {h.rating:.1f} vs {area.median_rating:.1f} for the area.",
            min(20, round((area.median_rating - h.rating) * 25)))
    if h.reviews is not None and area.median_reviews and h.reviews < area.median_reviews * 0.5:
        add("medium", f"{h.reviews} Google reviews vs a median of {area.median_reviews:.0f} in the area.", 10)

    mine = {a.lower() for a in h.amenities}
    missing = [a for a in area.common_amenities if a not in mine]
    if missing:
        add("medium" if len(missing) >= 2 else "low",
            f"Missing amenities most of the area lists: {', '.join(missing[:4])}.", min(15, 4 * len(missing)))
    # SerpApi returns a sample of images (9 in real responses), not the full count: only a shorter list is real.
    if h.has_details and h.images < IMAGE_SAMPLE:
        add("low", f"Only {h.images} photos on Google.", 5)
    if not h.website:
        add("medium", "No website linked on Google.", 10)

    r.opportunity = min(100, sum(i.points for i in r.issues))
    return r


# ------------------------------------------------------------------ run


def run(ctx: AgentContext, source: HotelsSource, query: str, checkin: date, *, nights: int = 1,
        property_type: str | None = "motel", limit: int = 20, details: bool = True,
        snapshots: int = 10) -> ProspectRun:
    from fullhouse.core.serpapi import PROPERTY_TYPES

    if checkin < ctx.today:
        raise ValueError(f"Check-in {checkin} is in the past.")
    types = [PROPERTY_TYPES[property_type]] if property_type else None
    calls_before = source.calls
    hotels = collect(source, query, checkin, checkin + timedelta(days=nights), property_types=types, limit=limit,
                     details=details)
    area = benchmark(hotels, query, checkin, nights)
    reports = sorted((assess(h, area, ctx.settings.direct_rate_buffer_pct) for h in hotels),
                     key=lambda r: -r.opportunity)
    for i, r in enumerate(reports, 1):
        r.rank = i
        r.client_id = _store(ctx, r, query, property_type)
    ctx.session.commit()

    run_ = ProspectRun(area=area, reports=reports, searches_used=source.calls - calls_before)
    if not reports:
        return run_
    out = ctx.settings.output_dir / "prospects" / f"{_slug(query)}-{ctx.today.isoformat()}"
    for r in reports[:snapshots]:
        r.pdf = render_pdf("prospect_snapshot.html", {
            "r": r, "h": r.hotel, "area": area, "today": ctx.today,
            "prices": sorted((p for p in r.hotel.prices if p.price), key=lambda p: p.price)},
                           out / f"{r.rank:02d}-{_slug(r.hotel.name)}.pdf")
    run_.shortlist_pdf = render_pdf("prospect_shortlist.html", {"run": run_, "area": area, "today": ctx.today},
                                    out / "shortlist.pdf")
    run_.shortlist_csv = _write_csv(reports, out / "shortlist.csv")
    return run_


def _store(ctx: AgentContext, r: ProspectReport, area: str, property_type: str | None) -> int:
    h = r.hotel
    client = ctx.session.exec(select(Client).where(Client.name == h.name)).first()
    if client is None:
        ptype = PropertyType.hostel if property_type == "hostel" else PropertyType.motel
        client = Client(name=h.name, property_type=ptype, region=area, is_prospect=True)
    if h.website and not client.website_url:
        client.website_url = h.website
    ctx.session.add(client)
    ctx.session.flush()
    ctx.clear_findings(client, NAME)
    for issue in r.issues:
        ctx.session.add(Finding(client_id=client.id, agent=NAME, date=ctx.today, severity=issue.severity,
                                text=issue.text, data={"points": issue.points, "opportunity": r.opportunity}))
    return client.id


def _write_csv(reports: list[ProspectReport], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "name", "opportunity", "price_per_night", "price_vs_area_pct", "google_rating",
                    "google_reviews", "direct_booking", "phone", "website", "address", "headline"])
        for r in reports:
            h = r.hotel
            w.writerow([r.rank, h.name, r.opportunity, h.price, r.price_diff_pct, h.rating, h.reviews,
                        r.direct_status, h.phone or "", h.website or "", h.address or "", r.headline])
    return path


def _slug(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "area"


def as_dict(run_: ProspectRun) -> dict:
    return {"area": asdict(run_.area), "searches_used": run_.searches_used,
            "reports": [{"rank": r.rank, "name": r.hotel.name, "opportunity": r.opportunity,
                         "headline": r.headline, "direct": r.direct_status} for r in run_.reports]}
