"""Offline sample market: a fetcher that renders realistic OTA pages for fictional properties."""

from __future__ import annotations

import json
import zlib
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from jinja2 import Environment, FileSystemLoader

from fullhouse.core.fetch import Page

SAMPLE_DIR = Path(__file__).resolve().parent
SAMPLE_BOOKING_URL = "https://www.booking.com/hotel/au/coral-sands-motel.en-gb.html"

_LEAD_FACTOR = {1: 1.08, 7: 1.0, 14: 0.97, 30: 0.95}


def load_market() -> dict:
    return json.loads((SAMPLE_DIR / "market.json").read_text())


def _factor(slug: str, lead: int) -> float:
    base = min(_LEAD_FACTOR.items(), key=lambda kv: abs(kv[0] - lead))[1]
    jitter = (zlib.crc32(f"{slug}:{lead}".encode()) % 9 - 4) / 100  # deterministic +/-4%
    return base * (1 + jitter)


class SampleFetcher:
    """Implements the Fetcher interface over the fictional market in market.json."""

    def __init__(self, today: date | None = None):
        self.today = today or date.today()
        self.market = load_market()
        self.env = Environment(loader=FileSystemLoader(SAMPLE_DIR), autoescape=True)
        self.by_slug = {self.market["client"]["slug"]: self.market["client"]}
        self.by_slug.update({c["slug"]: c for c in self.market["competitors"]})

    def allowed(self, url: str) -> bool:
        return True

    def close(self) -> None:
        pass

    def get(self, url: str, *, render: bool = False, use_cache: bool = True) -> Page:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        lead = self._lead(query.get("checkin"))
        if parts.netloc.endswith("booking.com") and parts.path.startswith("/hotel/"):
            slug = parts.path.rsplit("/", 1)[-1].split(".")[0]
            prop = self.by_slug.get(slug)
            if prop is None:
                return Page(url=url, status=404, html="", elapsed_ms=0)
            return self._page(url, self._listing(prop, lead))
        if parts.netloc.endswith("booking.com") and parts.path.startswith("/searchresults"):
            props = [
                {"name": c["name"], "slug": c["slug"], "price": min(c["rooms"].values()) * _factor(c["slug"], lead)}
                for c in [self.market["client"], *self.market["competitors"]]
            ]
            html = self.env.get_template("booking_search.html.j2").render(
                region=query.get("ss", ""), props=props, **self._dates(lead)
            )
            return self._page(url, html)
        if parts.netloc == "coralsands.example":
            return self._page(url, (SAMPLE_DIR / "website.html").read_text(), elapsed=1850.0)
        if parts.netloc == "book.coralsands.example":
            client = self.market["client"]
            rooms = [(n, p * _factor("direct", lead)) for n, p in client["direct_rates"].items()]
            return self._page(url, self.env.get_template("direct_booking.html.j2").render(rooms=rooms))
        return Page(url=url, status=404, html="", elapsed_ms=0)

    def _lead(self, checkin: str | None) -> int:
        if not checkin:
            return 7
        return max((date.fromisoformat(checkin) - self.today).days, 0)

    def _listing(self, prop: dict, lead: int) -> str:
        p = {
            "photos": 32,
            "description": f"{prop['name']} offers comfortable rooms close to the beach, with free parking, "
            "free WiFi and a swimming pool. Guests love the location near cafes and the esplanade. "
            "Every room has air conditioning, a flat-screen TV and a private bathroom.",
            "amenities": ["Free WiFi", "Free parking", "Outdoor swimming pool", "Air conditioning", "Family rooms",
                          "Non-smoking rooms", "BBQ facilities", "Laundry"],
            "free_cancellation": True,
            "genius": True,
            "review_score": 8.6,
            "review_count": 640,
            "reviews_shown": 10,
            "reviews_responded": 8,
            "locality": self.market["client"]["locality"],
            "region": self.market["client"]["region"],
            **prop,
        }
        rooms = [(n, price * _factor(prop["slug"], lead)) for n, price in prop["rooms"].items()]
        shown = min(p["photos"], 6)
        ld = {
            "@context": "https://schema.org",
            "@type": "Hotel",
            "name": p["name"],
            "address": {"@type": "PostalAddress", "addressLocality": p["locality"], "addressRegion": p["region"]},
            "aggregateRating": {"ratingValue": p["review_score"], "bestRating": 10, "reviewCount": p["review_count"]},
        }
        return self.env.get_template("booking_listing.html.j2").render(
            p=p, rooms=rooms, shown_photos=shown, more_photos=p["photos"] - shown, ld=ld, **self._dates(lead)
        )

    def _dates(self, lead: int) -> dict:
        from datetime import timedelta

        checkin = self.today + timedelta(days=lead)
        return {"checkin": checkin.isoformat(), "checkout": (checkin + timedelta(days=1)).isoformat()}

    @staticmethod
    def _page(url: str, html: str, elapsed: float = 600.0) -> Page:
        return Page(url=url, status=200, html=html, elapsed_ms=elapsed)


def weekly_rows(today: date, weeks: int = 105) -> list[tuple[str, float, int, int]]:
    """Synthetic weekly metrics ending at the last complete week before ``today``.

    Seasonal (Queensland summer peak), with a ~6% occupancy and ~4% rate lift over the
    last 10 weeks to stand in for the effect of the service.
    """
    import math
    import random
    from datetime import timedelta

    rng = random.Random(7)
    last = today - timedelta(days=today.weekday()) - timedelta(weeks=1)
    first = last - timedelta(weeks=weeks - 1)
    lift_from = last - timedelta(weeks=9)
    rooms = load_market()["client"]["room_count"]
    out = []
    for i in range(weeks):
        wk = first + timedelta(weeks=i)
        season = 0.72 + 0.18 * math.cos((wk.timetuple().tm_yday - 15) / 365 * 2 * math.pi)
        lifted = wk >= lift_from
        occ = min(0.97, season * (1.06 if lifted else 1.0) * rng.uniform(0.94, 1.04))
        avail = rooms * 7
        sold = round(avail * occ)
        adr = (142 + 30 * (season - 0.72)) * (1.04 if lifted else 1.0) * rng.uniform(0.97, 1.03)
        out.append((wk.isoformat(), round(sold * adr, 2), sold, avail))
    return out


def seed(session, today: date):
    """Create the sample client, competitors and two years of metrics. Idempotent."""
    import csv
    import tempfile
    from datetime import timedelta

    from sqlmodel import select

    from fullhouse.core.metrics import import_csv
    from fullhouse.core.models import Client, Competitor, PropertyType

    m = load_market()
    c = m["client"]
    client = session.exec(select(Client).where(Client.name == c["name"])).first()
    if client is not None and not client.is_prospect:
        return client
    # New, or an audited prospect being signed (the real-world path: audit -> sign).
    client = client or Client(name=c["name"])
    client.is_prospect = False
    client.property_type = PropertyType(c["property_type"])
    client.region = f"{c['locality']}, {c['region']}"
    client.room_count = c["room_count"]
    client.ota_urls = {"booking": SAMPLE_BOOKING_URL, "direct": c["direct_url"]}
    client.website_url = c["website_url"]
    client.pms_type = "csv"
    client.start_date = today - timedelta(days=70)
    client.voice_profile = "Friendly, down-to-earth, family-run. Signs off as 'Sam, Coral Sands'."
    client.contact_name, client.contact_email, client.phone = "Sam", "sam@coralsands.example", "+61 7 5555 0100"
    session.add(client)
    session.commit()
    have = {comp.name for comp in session.exec(select(Competitor).where(Competitor.client_id == client.id))}
    for comp in m["competitors"]:
        if len(have) >= 10:
            break
        if comp["name"] not in have:
            have.add(comp["name"])
            session.add(Competitor(client_id=client.id, name=comp["name"],
                                   ota_urls={"booking": f"https://www.booking.com/hotel/au/{comp['slug']}.en-gb.html"}))
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["week_start", "revenue", "rooms_sold", "rooms_available"])
        w.writerows(weekly_rows(today))
    import_csv(session, client.id, Path(fh.name))
    Path(fh.name).unlink()
    session.commit()
    return client
