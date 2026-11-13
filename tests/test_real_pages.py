"""Regression tests on real Booking.com pages, saved from Safari on iPad on 13 Nov 2026.

Both pages are for a 2-night stay (29 Nov - 1 Dec 2026), 2 adults. When Booking.com changes its markup
and these break, save fresh pages and update the selectors and the expected values together.
"""

import gzip
from datetime import date
from pathlib import Path

import pytest

from fullhouse import audit
from fullhouse.core.scrapers import booking

REAL = Path(__file__).parent / "fixtures" / "real"
LISTING = REAL / "space_hotel_melbourne_2026-11-29.html.gz"
SEARCH = REAL / "melbourne_search_2026-11-29.html.gz"


def read(path: Path) -> str:
    return gzip.decompress(path.read_bytes()).decode("utf-8")


def test_real_property_page():
    html = read(LISTING)
    assert booking.canonical_url(html) == "https://www.booking.com/hotel/au/space-melbourne.en-gb.html"
    assert booking.detect_checkin(html) == date(2026, 11, 29)
    assert booking.stay_nights(html) == 2
    l = booking.parse_listing(html, "https://www.booking.com/hotel/au/space-melbourne.en-gb.html")
    assert (l.name, l.locality, l.region) == ("Space Hotel", "Melbourne", "Victoria")
    assert l.photo_count == 74
    assert len(l.amenities) == 10 and len(set(l.amenities)) == 10  # the page lists them twice
    assert len(l.description) > 1000
    assert len(l.rooms) == 14 and all(r.price for r in l.rooms)
    prices = {r.name: r.price for r in l.rooms}
    assert prices["Double Room with Shared Bathroom"] == 154.0  # AUD 308 for 2 nights
    assert prices["Bed in 6-Bed Mixed Dormitory Room with Shared Bathroom"] == 60.5  # per bed
    assert l.free_cancellation and l.loyalty_program
    assert (l.review_score, l.review_count) == (8.6, 1789)


def test_real_search_page():
    html = read(SEARCH)
    results = booking.parse_search(html)
    assert len(results) == 25
    first = results[0]
    assert first.name == "Space Hotel" and first.price == 154.0  # same basis as its room table
    assert all(r.url.startswith("https://www.booking.com/hotel/au/") and "?" not in r.url for r in results)
    assert sum(1 for r in results if r.price) == 25


@pytest.fixture
def real_files(tmp_path):
    out = {}
    for key, path in (("listing", LISTING), ("search", SEARCH)):
        out[key] = tmp_path / path.name.removesuffix(".gz")
        out[key].write_text(read(path))
    return out


def test_real_saved_audit(settings, real_files, monkeypatch):
    from fullhouse.runtime import context

    with context(dry_run=True, settings=settings, today=date(2026, 11, 13)) as ctx:
        monkeypatch.setattr(ctx.fetcher, "get", lambda url, **kw: pytest.fail(f"network fetch: {url}"))
        out = audit.run_saved(ctx, real_files["listing"], search_html=real_files["search"])
    assert out.pages == 2 and out.notes == []
    assert len(out.competitors) == 10 and "Space Hotel" not in out.competitors
    assert "16 days out" in out.headline and "cheapest room" in out.headline
    assert out.score >= 80  # a well-kept listing
