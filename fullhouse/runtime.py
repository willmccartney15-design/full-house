"""Builds an AgentContext for live or dry runs."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

from fullhouse.agents.base import AgentContext
from fullhouse.core.claude import ClaudeClient
from fullhouse.core.config import Settings, load_settings
from fullhouse.core.db import session_scope
from fullhouse.core.fetch import Fetcher
from fullhouse.sample import SAMPLE_DIR, SampleFetcher


@contextmanager
def context(
    dry_run: bool = False, settings: Settings | None = None, today: date | None = None
) -> Iterator[AgentContext]:
    if settings is None:
        settings = load_settings()
        if dry_run:  # dry runs never touch the real database
            settings = load_settings(data_dir=settings.data_dir / "dry-run")
    today = today or date.today()
    with session_scope(settings.db_url) as session:
        fetcher = SampleFetcher(today=today) if dry_run else Fetcher(settings)
        extras = (
            {"sample_reviews": SAMPLE_DIR / "reviews.json", "sample_gbp": SAMPLE_DIR / "gbp.json"} if dry_run else {}
        )
        ctx = AgentContext(
            settings=settings,
            session=session,
            claude=ClaudeClient(settings, session=session, dry_run=dry_run),
            fetcher=fetcher,
            today=today,
            dry_run=dry_run,
            extras=extras,
        )
        try:
            yield ctx
        finally:
            fetcher.close()
