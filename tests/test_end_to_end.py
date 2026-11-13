from typer.testing import CliRunner

from fullhouse import audit
from fullhouse.sample import SAMPLE_BOOKING_URL


def test_free_audit_two_pages_ten_competitors(ctx):
    out = audit.run(ctx, SAMPLE_BOOKING_URL, website="https://coralsands.example/")
    assert out.pages == 2
    assert len(out.competitors) == 10 and "Coral Sands Motel" not in out.competitors
    assert out.pdf.exists()


def test_cli_demo(tmp_path, monkeypatch):
    from fullhouse.cli import app

    monkeypatch.setenv("FULLHOUSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FULLHOUSE_MIN_SECONDS_BETWEEN_REQUESTS", "0")
    result = CliRunner().invoke(app, ["demo"])
    assert result.exit_code == 0, result.output
    assert "PDF (2 pages)" in result.output
    assert "[monday_report]" in result.output
    result = CliRunner().invoke(app, ["run", "nope", "--client", "1", "--dry-run"])
    assert result.exit_code == 2


def test_audit_reports_a_blocked_search(ctx, monkeypatch):
    import pytest

    from fullhouse.core.fetch import Page

    real_get = ctx.fetcher.get

    def get(url, **kw):
        if "searchresults" in url:  # what Booking.com serves automated clients: a shell with no results
            return Page(url="https://www.booking.com/searchresults.en-gb.html", status=202, html="<html></html>",
                        elapsed_ms=1)
        return real_get(url, **kw)

    monkeypatch.setattr(ctx.fetcher, "get", get)
    with pytest.raises(audit.BlockedError, match="no search results"):
        audit.run(ctx, SAMPLE_BOOKING_URL)
