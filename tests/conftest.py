from datetime import date

import pytest

from fullhouse.core.config import load_settings
from fullhouse.runtime import context
from fullhouse.sample import seed

TODAY = date(2026, 11, 12)  # a Thursday


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
