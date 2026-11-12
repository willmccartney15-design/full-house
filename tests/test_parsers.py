from datetime import date
from pathlib import Path

from fullhouse.core.rooms import normalise_room
from fullhouse.core.scrapers import booking, ota_for_url
from fullhouse.core.scrapers.common import parse_price

FIX = Path(__file__).parent / "fixtures"


def test_booking_listing_fallback_selectors_and_json_ld():
    listing = booking.parse_listing((FIX / "booking_listing.html").read_text(), "https://www.booking.com/hotel/au/x.html")
    assert listing.name == "Example Motor Inn"
    assert (listing.locality, listing.region) == ("St Kilda", "Victoria")
    assert listing.photo_count == 3 + 21
    assert listing.amenities == ["Free WiFi", "Parking", "24-hour front desk"]
    # The extra rate-plan row (no room name) lowers the Deluxe Queen price to its cheapest plan.
    assert [(r.name, r.price) for r in listing.rooms] == [
        ("Deluxe Queen Room", 175.0), ("Twin Room", 165.5), ("Family Room", None)
    ]
    assert listing.free_cancellation and listing.loyalty_program
    assert listing.review_score == 8.4 and listing.review_count == 1204
    assert listing.review_response_rate == 0.5
    assert "Free parking" in listing.description


def test_booking_search_results():
    results = booking.parse_search((FIX / "booking_search.html").read_text())
    assert [(r.name, r.url, r.price) for r in results] == [
        ("Alpha Motel", "https://www.booking.com/hotel/au/alpha.en-gb.html", 140.0),
        ("Beta Lodge", "https://www.booking.com/hotel/au/beta.en-gb.html", None),
    ]


def test_dated_urls():
    url = booking.with_dates("https://www.booking.com/hotel/au/x.en-gb.html?aid=1", date(2026, 12, 1))
    assert "checkin=2026-12-01" in url and "checkout=2026-12-02" in url and "aid=1" in url
    assert "ss=Mooloolaba" in booking.search_url("Mooloolaba", date(2026, 12, 1))


def test_prices_and_rooms():
    assert parse_price("AUD 1,234.50") == 1234.5
    assert parse_price("A$ 99") == 99
    assert parse_price("$149 per night") == 149
    assert parse_price("Sold out") is None
    assert normalise_room("Bed in 6-Bed Mixed Dormitory Room") == "dorm"
    assert normalise_room("Private Double Room") == "double"
    assert normalise_room("Superior Queen") == "double"
    assert normalise_room("Twin Room") == "twin"
    assert normalise_room("Family Suite") == "family"
    assert ota_for_url("https://www.hostelworld.com/pwa/hosteldetails.php/x/1") == "hostelworld"
