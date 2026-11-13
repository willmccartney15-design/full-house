from datetime import date, timedelta

import pytest
from sqlmodel import select

from fullhouse.agents import monday_report, rate_scraper
from fullhouse.core.models import Competitor, RateSnapshot
from fullhouse.core.rate_import import RateImportError, import_rates


def stay(ctx, days):
    return (ctx.today + timedelta(days=days)).isoformat()


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_wide_export_matches_names_and_buckets_leads(ctx, client, tmp_path):
    # One row per stay date, one column per property: the usual rate-shopper layout. 40 days of data.
    header = "Date,Coral Sands Motel,Harbour View Motor Inn,Seabreeze,The Esplanade Lodge,Unknown Place\n"
    rows = "".join(f"{stay(ctx, d)},$150,165,\"1,139\" ,sold out,99\n" for d in range(0, 40))
    r = import_rates(ctx.session, client, write(tmp_path, "rpg.csv", header + rows), today=ctx.today)
    assert r.rows == 40 and r.stay_dates == 40
    assert r.unmatched == {"Unknown Place": 1}  # resolved once per column
    assert r.stored == 40 * 3  # client + 2 matched competitors priced ("sold out" skipped)
    seabreeze = ctx.session.exec(select(Competitor).where(Competitor.name == "Seabreeze Motel")).one()
    assert ctx.session.exec(select(RateSnapshot).where(RateSnapshot.competitor_id == seabreeze.id)).first().price == 1139

    res = rate_scraper.run(ctx, client)  # must not fetch anything
    assert res.data["source"] == "import"
    assert {p["lead_days"] for p in res.data["position"]} == {1, 7, 14, 30}
    assert all(p["room_type"] == "lowest" for p in res.data["position"])


def test_long_export_with_rooms_nights_and_map(ctx, client, tmp_path):
    text = ("Check-in;Hotel;Rate;Room;LOS\n"
            f"{stay(ctx, 7)};self;300;Queen Room;2\n"
            f"{stay(ctx, 7)};Harbour View;330;Queen Room;2\n"
            f"{stay(ctx, 7)};Alex Surf;310;Queen Room;2\n"
            f"bad-date;Alex Surf;310;Queen Room;2\n")
    r = import_rates(ctx.session, client, write(tmp_path, "long.csv", text), today=ctx.today,
                     column_map={"Check-in": "stay_date"})
    assert (r.stored, r.skipped) == (3, 1)
    [row] = rate_scraper.position_table(rate_scraper.latest_import(ctx, client))
    assert (row.room_type, row.lead_days, row.client_price, row.market_median) == ("double", 7, 150, 160)


def test_add_competitors_and_reimport_replaces(ctx, client, tmp_path):
    path = write(tmp_path, "w.csv", f"stay_date,Coral Sands Motel,Brand New Lodge\n{stay(ctx, 7)},150,170\n")
    r = import_rates(ctx.session, client, path, today=ctx.today, add_competitors=True)
    assert r.added_competitors == ["Brand New Lodge"]
    import_rates(ctx.session, client, path, today=ctx.today)
    assert len(ctx.session.exec(select(RateSnapshot).where(RateSnapshot.source == "import")).all()) == 2


def test_stale_import_falls_back_to_scraping(ctx, client, tmp_path):
    old = (ctx.today - timedelta(days=10)).isoformat()
    path = write(tmp_path, "old.csv", "snapshot_date,stay_date,property,price\n"
                 f"{old},{stay(ctx, 5)},self,150\n")
    import_rates(ctx.session, client, path, today=ctx.today)
    assert rate_scraper.latest_import(ctx, client) == []
    assert rate_scraper.run(ctx, client).data["ota"] == "booking"  # scraped the sample market instead


def test_monday_report_uses_import_not_scrape(ctx, client, tmp_path):
    rate_scraper.run(ctx, client)  # today's scrape: double/twin/family rows
    path = write(tmp_path, "w.csv", "stay_date,Coral Sands Motel,Harbour View Motor Inn\n"
                 + "".join(f"{stay(ctx, d)},150,170\n" for d in (1, 7, 14, 30)))
    import_rates(ctx.session, client, path, today=ctx.today)
    data = monday_report.gather(ctx, client, monday_report.monday(ctx.today) - timedelta(weeks=1))
    assert {p["room_type"] for p in data["position"]} == {"lowest"}


def test_clear_errors(ctx, client, tmp_path):
    with pytest.raises(RateImportError, match="stay-date"):
        import_rates(ctx.session, client, write(tmp_path, "a.csv", "x,y\n1,2\n"), today=ctx.today)
    with pytest.raises(RateImportError, match="property column"):
        import_rates(ctx.session, client, write(tmp_path, "b.csv", "date,price\n2026-12-01,10\n"), today=ctx.today)
    with pytest.raises(RateImportError, match="Cannot read"):
        import_rates(ctx.session, client, tmp_path / "missing.csv", today=ctx.today)


def test_old_database_gets_new_column(tmp_path):
    import sqlite3

    from fullhouse.core.db import init_db

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE ratesnapshot (id INTEGER PRIMARY KEY, client_id INTEGER, competitor_id INTEGER, "
                "date DATE, ota VARCHAR, room_type VARCHAR, room_name VARCHAR, lead_days INTEGER, price FLOAT)")
    con.execute("INSERT INTO ratesnapshot VALUES (1, 1, NULL, '2026-11-01', 'booking', 'double', '', 7, 150)")
    con.commit()
    con.close()
    init_db(f"sqlite:///{db}")
    con = sqlite3.connect(db)
    assert con.execute("SELECT source FROM ratesnapshot").fetchone() == ("scrape",)


def test_cli_import_rates(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from fullhouse.cli import app

    monkeypatch.setenv("FULLHOUSE_DATA_DIR", str(tmp_path / "d"))
    today = date.today()
    path = write(tmp_path, "w.csv", "Arrival,Coral Sands Motel,Harbour View Motor Inn,Mystery Inn\n"
                 + "".join(f"{(today + timedelta(days=d)).isoformat()},150,170,1\n" for d in (1, 7)))
    result = CliRunner().invoke(app, ["import-rates", str(path), "--client", "1", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Stored 4 prices across 2 stay dates" in result.output and "Mystery Inn (1)" in result.output
