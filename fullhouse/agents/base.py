"""What every agent receives, and the shared result shape."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from sqlmodel import Session, select

from fullhouse.core.claude import ClaudeClient
from fullhouse.core.config import Settings
from fullhouse.core.fetch import Page
from fullhouse.core.models import Client, Competitor, Finding

log = logging.getLogger("fullhouse.agents")


class PageSource(Protocol):
    def get(self, url: str, *, render: bool = False, use_cache: bool = True) -> Page: ...
    def allowed(self, url: str) -> bool: ...
    def close(self) -> None: ...


@dataclass
class AgentContext:
    settings: Settings
    session: Session
    claude: ClaudeClient
    fetcher: PageSource
    today: date = field(default_factory=date.today)
    dry_run: bool = False
    extras: dict[str, Any] = field(default_factory=dict)  # e.g. sample data paths in dry runs

    def competitors(self, client: Client) -> list[Competitor]:
        return list(self.session.exec(select(Competitor).where(Competitor.client_id == client.id)))

    def clear_findings(self, client: Client, agent: str) -> None:
        """Re-running an agent on the same day replaces that day's findings rather than duplicating them."""
        for old in self.session.exec(
            select(Finding).where(Finding.client_id == client.id, Finding.agent == agent, Finding.date == self.today)
        ):
            self.session.delete(old)

    def record(self, client: Client, agent: str, text: str, severity: str = "medium", **data) -> Finding:
        finding = Finding(client_id=client.id, agent=agent, date=self.today, severity=severity, text=text, data=data)
        self.session.add(finding)
        return finding


@dataclass
class AgentResult:
    agent: str
    client_id: int
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors
