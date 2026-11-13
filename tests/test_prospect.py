import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from sqlmodel import select

from fullhouse import prospect
from fullhouse.core.models import Client, Finding
from fullhouse.core.serpapi import SerpApiClient, SerpApiError, merge_details, parse_property

FIX = Path(__file__).parent / "fixtures" / "serpapi"


class FixtureHotels:
    """Serves the documented-shape fixtures and records what was asked."""

    def __init__(self):
        self.calls, self.requests = 0, []
        self.p1, self.p2 = (json.loads((FIX / f"search_p{i}.json").read_text()) for i in (1, 2))
        self.details_by_token = json.loads((FIX / "details.json").read_text())

    def search(self, query, checkin, checkout, *, property_types=None, page_token=None):
        self.calls += 1
        self.requests.append(("search", query, property_types, page_token))
        return self.p2 if page_token else self.p1

    def details(self, token, checkin, checkout):
        self.calls += 1
        self.requests.append(("details", token))
        return self.details_by_token[token]


def test_parsing_documented_shapes():
    item = json.loads((FIX / "search_p1.json").read_text())["properties"][0]
    h = parse_property(item)
    assert (h.name, h.token, h.price, h.rating, h.reviews, h.hotel_class) == ("Alpha Motel", "TOK_A", 150, 4.6, 812, 3)
    merge_details(h, json.loads((FIX / "details.json").read_text())["TOK_A"])
    assert h.direct_price == 138 and h.best_ota.source == "Booking.com" and h.price == 138
    assert h.images == 25 and h.phone == "+61 7 5555 0001"
    assert h.other_reviews == [{"source": "Tripadvisor", "score": 4.5, "max": 5, "reviews": 300}]


def test_pipeline_ranks_and_explains(ctx):
    src = FixtureHotels()
    run = prospect.run(ctx, src, "Mooloolaba QLD", ctx.today + timedelta(days=14), limit=10)
    names = [r.hotel.name for r in run.reports]
    assert "Beach House" not in names and len(names) == 4  # vacation rental dropped; page 2 fetched
    assert ("search", "Mooloolaba QLD", [16], None) in src.requests  # motels only
    assert run.searches_used == 2 + 4
    top = run.reports[0]
    assert top.hotel.name == "Beta Motor Inn" and top.rank == 1
    assert "Booking.com sells it for $120" in top.headline
    assert top.direct_status == "Booking.com cheaper"
    gamma = next(r for r in run.reports if r.hotel.name == "Gamma Lodge")
    assert gamma.direct_status == "no direct price"
    alpha = next(r for r in run.reports if r.hotel.name == "Alpha Motel")
    assert alpha.direct_status == "direct cheapest" and alpha.opportunity < top.opportunity
    assert run.area.common_amenities == ["free parking", "free wi-fi"]
    assert run.shortlist_pdf.exists() and run.shortlist_csv.read_text().count("\n") == 5
    assert top.pdf.exists()
    # Every property is stored as a prospect with its findings.
    beta = ctx.session.exec(select(Client).where(Client.name == "Beta Motor Inn")).one()
    assert beta.is_prospect
    assert ctx.session.exec(select(Finding).where(Finding.client_id == beta.id, Finding.agent == "prospect")).all()


def test_limit_and_no_details(ctx):
    src = FixtureHotels()
    run = prospect.run(ctx, src, "Mooloolaba", ctx.today + timedelta(days=7), limit=2, details=False)
    assert len(run.reports) == 2 and src.calls == 1
    assert all(r.direct_status == "not checked" for r in run.reports)


def test_rerun_replaces_findings(ctx):
    for _ in range(2):
        prospect.run(ctx, FixtureHotels(), "Mooloolaba", ctx.today + timedelta(days=7), limit=10, snapshots=0)
    beta = ctx.session.exec(select(Client).where(Client.name == "Beta Motor Inn")).one()
    texts = [f.text for f in ctx.session.exec(select(Finding).where(Finding.client_id == beta.id))]
    assert len(texts) == len(set(texts))


def test_past_checkin_rejected(ctx):
    with pytest.raises(ValueError, match="past"):
        prospect.run(ctx, FixtureHotels(), "x", ctx.today - timedelta(days=1))


def test_client_requires_key_and_caches(tmp_path, monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    with pytest.raises(SerpApiError, match="SERPAPI_API_KEY"):
        SerpApiClient(cache_dir=tmp_path).search("x", date(2026, 12, 1), date(2026, 12, 2))

    seen = []

    def fake_get(url, params, timeout):
        seen.append(params)
        return httpx.Response(200, json={"properties": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    client = SerpApiClient(cache_dir=tmp_path, api_key="k")
    for _ in range(2):
        client.search("Mooloolaba", date(2026, 12, 1), date(2026, 12, 2), property_types=[16])
    assert len(seen) == 1 and client.calls == 1  # second call served from cache
    p = seen[0]
    assert (p["engine"], p["api_key"], p["currency"], p["gl"], p["property_types"]) == (
        "google_hotels", "k", "AUD", "au", "16")
    assert "api_key" not in json.dumps(list((tmp_path / "serpapi").glob("*.json"))[0].read_text())


def test_client_reports_api_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, params, timeout: httpx.Response(
        401, json={"error": "Invalid API key."}, request=httpx.Request("GET", url)))
    with pytest.raises(SerpApiError, match="Invalid API key"):
        SerpApiClient(cache_dir=tmp_path, api_key="bad").details("T", date(2026, 12, 1), date(2026, 12, 2))


def test_cli_prospect_dry_run(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from fullhouse.cli import app

    monkeypatch.setenv("FULLHOUSE_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(app, ["prospect", "Mooloolaba QLD", "--dry-run", "--snapshots", "2"])
    assert result.exit_code == 0, result.output
    assert "1. [" in result.output and "Coral Sands Motel" in result.output and "shortlist.pdf" in result.output
    assert CliRunner().invoke(app, ["prospect", "x", "--type", "castle", "--dry-run"]).exit_code == 2


class RealMooloolaba(FixtureHotels):
    """Trimmed real SerpApi responses (Mooloolaba motels, 2026-11-27): Google showed no price for any of them."""

    def __init__(self):
        self.calls, self.requests = 0, []
        real = FIX / "real"
        self.p1 = self.p2 = json.loads((real / "mooloolaba_search.json").read_text())
        self.details_by_token = json.loads((real / "mooloolaba_details.json").read_text())


def test_real_response_without_prices(ctx):
    run = prospect.run(ctx, RealMooloolaba(), "Mooloolaba QLD", ctx.today + timedelta(days=14), limit=20, snapshots=0)
    by_name = {r.hotel.name: r for r in run.reports}
    assert set(by_name) == {"Central Motel Mooloolaba", "Kyamba Court Motel", "River Esplanade Motel Mooloolaba"}
    assert run.area.median_price is None
    kyamba = by_name["Kyamba Court Motel"].hotel
    assert (kyamba.rating, kyamba.reviews, kyamba.phone, kyamba.website) == (4.2, 158, "+61 7 5444 0202", None)
    assert by_name["Central Motel Mooloolaba"].hotel.website == "http://www.centralmotelmooloolaba.com.au/"
    for r in run.reports:
        assert r.hotel.price is None and r.direct_status == "no prices shown"
        # No price from anyone is not evidence of OTA-only booking, and 9 images is SerpApi's sample, not a count.
        assert "OTAs" not in r.headline and "photos" not in r.headline


def _no_prices(src: "FixtureHotels", token: str) -> None:
    src.details_by_token[token]["prices"] = []


def test_no_prices_on_google_is_a_top_finding_when_the_area_is_priced(ctx):
    src = FixtureHotels()
    _no_prices(src, "TOK_G")  # Gamma has no price anywhere; the other three do
    run = prospect.run(ctx, src, "Mooloolaba", ctx.today + timedelta(days=21), limit=10, snapshots=0)
    gamma = next(r for r in run.reports if r.hotel.name == "Gamma Lodge")
    assert run.area.priced_share >= 0.5
    assert gamma.direct_status == "no prices shown"
    assert gamma.headline.startswith("Google Hotels shows no bookable price")
    assert any(i.severity == "high" and i.points == 25 for i in gamma.issues)


def test_no_prices_everywhere_is_flagged_softly(ctx):
    src = FixtureHotels()
    for token in src.details_by_token:
        _no_prices(src, token)
    for page in (src.p1, src.p2):  # search results without prices too
        for item in page["properties"]:
            item.pop("rate_per_night", None)
    run = prospect.run(ctx, src, "Mooloolaba", ctx.today + timedelta(days=21), limit=10, snapshots=0)
    assert run.area.priced_share == 0
    for r in run.reports:
        no_price = [i for i in r.issues if "no bookable price" in i.text]
        assert no_price and no_price[0].severity == "medium" and "another date" in no_price[0].text


def test_max_class_skips_upmarket_hotels(ctx):
    src = FixtureHotels()
    src.p1["properties"][0]["extracted_hotel_class"] = 5  # Alpha becomes a 5-star resort
    run = prospect.run(ctx, src, "Mooloolaba", ctx.today + timedelta(days=21), limit=10, snapshots=0)
    assert "Alpha Motel" not in [r.hotel.name for r in run.reports]
    run = prospect.run(ctx, FixtureHotels(), "Mooloolaba", ctx.today + timedelta(days=21), limit=10, snapshots=0,
                       max_class=None)
    assert "Alpha Motel" in [r.hotel.name for r in run.reports]
