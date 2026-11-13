from datetime import date

import pytest

from fullhouse.core.config import load_settings
from fullhouse.runtime import context
from fullhouse.sample import seed

TODAY = date(2026, 11, 12)  # a Thursday

# Live credentials and mail settings that must never leak into tests (a real SMTP_HOST would make
# the mailer tests connect to a real server, and could send mail to the sample addresses).
LIVE_ENV = (
    "FULLHOUSE_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY", "SERPAPI_API_KEY", "GOOGLE_PLACES_API_KEY",
    "GBP_ACCESS_TOKEN", "SMTP_HOST", "SMTP_PORT", "SMTP_SSL", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM",
    "MAIL_FROM_NAME", "FULLHOUSE_SENDER_NAME", "FULLHOUSE_SENDER_BUSINESS", "FULLHOUSE_SENDER_CONTACT",
    "FULLHOUSE_TIMEZONE", "FULLHOUSE_DATA_DIR",
)


@pytest.fixture(autouse=True)
def _isolate_from_live_env(monkeypatch):
    for name in LIVE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("fullhouse.core.config.load_dotenv", lambda *a, **k: False)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("FULLHOUSE_DB_URL", raising=False)
    return load_settings(data_dir=tmp_path / "data", min_seconds_between_requests=0)


@pytest.fixture
def ctx(settings):
    with context(dry_run=True, settings=settings, today=TODAY) as c:
        yield c


@pytest.fixture
def client(ctx):
    return seed(ctx.session, ctx.today)
