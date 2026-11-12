"""Offline audit from pages saved in a browser: Booking.com must never be contacted."""

from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fullhouse import audit
from fullhouse.core.scrapers import booking
from fullhouse.sample import SAMPLE_BOOKING_URL, SampleFetcher, load_market

LEAD = 14


@pytest.fixture
def saved(tmp_path, ctx) -> dict:
    """Save pages the way a person would: property, area search, two competitors, all for one date."""
    sample = SampleFetcher(today=ctx.today)
    checkin = ctx.today + timedelta(days=LEAD)
    comps = load_market()["competitors"]

    def save(name, url):
        path = tmp_path / name
        path.write_text(sample.get(booking.with_dates(url, checkin) if "/hotel/" in url else url).html)
        return path

    return {
        "listing": save("listing.html", SAMPLE_BOOKING_URL),
        "search": save("search.html", booking.search_url("Mooloolaba", checkin)),
        "competitors": [save(f"c{i}.html", f"https://www.booking.com/hotel/au/{c['slug']}.en-gb.html")
                        for i, c in enumerate(comps[:2])],
        "checkin": checkin,
    }


@pytest.fixture
def offline(ctx, monkeypatch):
    def refuse(url, **kw):
        raise AssertionError(f"network fetch attempted: {url}")

    monkeypatch.setattr(ctx.fetcher, "get", refuse)
    return ctx


def test_reads_url_and_dates_from_the_saved_page(saved):
    html = saved["listing"].read_text()
    assert booking.canonical_url(html) == SAMPLE_BOOKING_URL
    assert booking.detect_checkin(html) == saved["checkin"]


def test_full_saved_audit(offline, saved):
    out = audit.run_saved(offline, saved["listing"], search_html=saved["search"],
                          competitor_html=saved["competitors"])
    assert out.pages == 2 and out.pdf.exists()
    assert len(out.competitors) == 10 and "Coral Sands Motel" not in out.competitors
    assert out.notes == []
    assert "14 days out" in out.headline
    html = out.pdf.with_suffix(".html").read_text()
    assert "Cheapest room" in html and "14 days out" in html


def test_cheapest_room_row_covers_search_only_competitors(offline, saved):
    audit.run_saved(offline, saved["listing"], search_html=saved["search"], competitor_html=saved["competitors"])
    from sqlmodel import select

    from fullhouse.agents.rate_scraper import position_table
    from fullhouse.core.models import RateSnapshot

    snaps = offline.session.exec(select(RateSnapshot)).all()
    rows = {r.room_type: r for r in position_table(snaps)}
    assert rows["lowest"].competitors_priced == 10  # every competitor, from search cards or their pages
    assert rows["double"].competitors_priced >= 1  # room-level rows only from saved competitor pages
    assert {r.lead_days for r in rows.values()} == {LEAD}


def test_listing_only_audit_still_renders(offline, saved):
    out = audit.run_saved(offline, saved["listing"])
    assert out.competitors == [] and out.pages >= 1
    assert any("no market comparison" in n for n in out.notes)


def test_mismatched_dates_are_flagged(offline, saved, tmp_path):
    other = tmp_path / "other.html"
    sample = SampleFetcher(today=offline.today)
    other.write_text(sample.get(booking.with_dates(
        "https://www.booking.com/hotel/au/alex-surf-motel.en-gb.html", offline.today + timedelta(days=30))).html)
    out = audit.run_saved(offline, saved["listing"], competitor_html=[other])
    assert any("not like for like" in n for n in out.notes)


def test_clear_errors(offline, saved, tmp_path):
    bare = tmp_path / "bare.html"
    bare.write_text("<html><body><h2 data-testid='property-name'>X</h2></body></html>")
    with pytest.raises(audit.SavedPageError, match="--url"):
        audit.run_saved(offline, bare)
    with pytest.raises(audit.SavedPageError, match="--checkin"):
        audit.run_saved(offline, bare, url=SAMPLE_BOOKING_URL)
    with pytest.raises(audit.SavedPageError, match="has passed"):
        audit.run_saved(offline, saved["listing"], checkin=offline.today - timedelta(days=1))
    with pytest.raises(audit.SavedPageError, match="does not look like"):
        empty = tmp_path / "empty.html"
        empty.write_text("<html></html>")
        audit.run_saved(offline, empty, url=SAMPLE_BOOKING_URL, checkin=offline.today)
    with pytest.raises(audit.SavedPageError, match="Cannot read"):
        audit.run_saved(offline, Path(tmp_path / "missing.html"))


def test_cli_html_mode(saved, tmp_path, monkeypatch):
    from fullhouse.cli import app

    monkeypatch.setenv("FULLHOUSE_DATA_DIR", str(tmp_path / "cli"))
    args = ["audit", "--html", str(saved["listing"]), "--search-html", str(saved["search"]), "--no-claude"]
    for c in saved["competitors"]:
        args += ["--competitor-html", str(c)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "Competitors (10)" in result.output and "PDF (2 pages)" in result.output
    result = CliRunner().invoke(app, ["audit"])
    assert result.exit_code == 2
