"""Offline stand-in for SerpApi Google Hotels, built from the fictional market in market.json.

Responses follow SerpApi's documented shapes (properties[], rate_per_night, prices[] with ``official``,
other_reviews[]), so the same parsing runs in dry runs, tests and live.
"""

from __future__ import annotations

import zlib
from datetime import date

from fullhouse.sample import load_market

AMENITIES = ["Free Wi-Fi", "Free parking", "Air conditioning", "Outdoor pool", "Kid-friendly", "Accessible",
             "Laundry service", "Kitchen", "Restaurant", "Pet-friendly"]


def _h(text: str, mod: int) -> int:
    return zlib.crc32(text.encode()) % mod


def _rate(amount: float) -> dict:
    a = round(amount)
    return {"lowest": f"${a}", "extracted_lowest": a, "before_taxes_fees": f"${a}", "extracted_before_taxes_fees": a}


class SampleHotels:
    """Implements the HotelsSource protocol offline. ``calls`` counts what would have been paid searches."""

    def __init__(self):
        m = load_market()
        self.props = [{**m["client"], "is_client": True}] + [
            {**c, "locality": m["client"]["locality"], "region": m["client"]["region"]} for c in m["competitors"]
        ]
        self.calls = 0

    def _profile(self, p: dict) -> dict:
        slug = p["slug"]
        base = min(p["rooms"].values())
        if p.get("is_client"):  # deliberately weak: dearer direct than Booking.com, thin listing
            return {"price": base, "rating": 3.9, "reviews": 142, "amenities": AMENITIES[:3], "images": 6,
                    "website": p.get("website_url"), "direct": base * 1.06, "otas": {"Booking.com": base,
                                                                                    "Expedia": base * 1.02}}
        rating = round(4.0 + _h(slug, 8) / 10, 1)
        n_amen = 4 + _h(slug + "a", 6)
        no_site = slug in {"seabreeze-motel", "buderim-gateway-motel"}
        no_direct = slug in {"point-cartwright-motel"}
        return {
            "price": base, "rating": rating, "reviews": 120 + _h(slug + "r", 700),
            "amenities": AMENITIES[:n_amen], "images": 12 + _h(slug + "i", 40),
            "website": None if no_site else f"https://{slug}.example/",
            "direct": None if (no_site or no_direct) else base * (0.97 if _h(slug + "p", 3) == 0 else 0.92),
            "otas": {"Booking.com": base, "Expedia": base * 1.03, "Agoda": base * 0.99},
        }

    def _item(self, p: dict, details: bool) -> dict:
        f = self._profile(p)
        lowest = min([v for v in [f["direct"], *f["otas"].values()] if v])
        item = {
            "type": "hotel", "name": p["name"], "property_token": f"tok-{p['slug']}",
            "link": f["website"], "rate_per_night": _rate(lowest), "overall_rating": f["rating"],
            "reviews": f["reviews"], "extracted_hotel_class": 3, "amenities": f["amenities"],
            "images": [{"thumbnail": f"https://img.example/{p['slug']}/{i}.jpg"} for i in range(min(f["images"], 8))],
        }
        if details:
            item["address"] = f"1 Example St, {p['locality']} QLD 4557, Australia"
            item["phone"] = "+61 7 5555 0" + str(100 + _h(p["slug"], 899))
            item["images"] = [{"thumbnail": f"https://img.example/{p['slug']}/{i}.jpg"} for i in range(f["images"])]
            prices = [{"source": src, "official": False, "rate_per_night": _rate(v)} for src, v in f["otas"].items()]
            if f["direct"]:
                prices.insert(0, {"source": p["name"], "official": True, "rate_per_night": _rate(f["direct"])})
            item["prices"] = prices
            item["other_reviews"] = [{"source": "Tripadvisor", "source_rating": {"score": round(f["rating"] - 0.1, 1),
                                                                                "max_score": 5}, "reviews": 80}]
        return item

    def search(self, query, checkin: date, checkout: date, *, property_types=None, page_token=None) -> dict:
        self.calls += 1
        items = [self._item(p, details=False) for p in self.props]
        if page_token is None:
            return {"properties": items[:8], "serpapi_pagination": {"next_page_token": "page-2"}}
        return {"properties": items[8:]}

    def details(self, property_token: str, checkin: date, checkout: date) -> dict:
        self.calls += 1
        slug = property_token.removeprefix("tok-")
        p = next(p for p in self.props if p["slug"] == slug)
        return self._item(p, details=True)
