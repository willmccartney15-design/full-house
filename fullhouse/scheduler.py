"""Weekly runner: every Monday morning, run all agents for every signed client."""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import select

from fullhouse.agents import AGENTS, WEEKLY_ORDER
from fullhouse.agents.base import AgentResult
from fullhouse.core.config import Settings, load_settings
from fullhouse.core.models import Client
from fullhouse.runtime import context

log = logging.getLogger("fullhouse.scheduler")


def run_weekly(dry_run: bool = False, settings: Settings | None = None, client_id: int | None = None,
               agents: list[str] | None = None) -> dict[int, list[AgentResult]]:
    """Run the weekly agent sequence. One client's failure never stops the others."""
    results: dict[int, list[AgentResult]] = {}
    with context(dry_run=dry_run, settings=settings) as ctx:
        if dry_run:
            from fullhouse.sample import seed

            seed(ctx.session, ctx.today)
        query = select(Client).where(Client.is_prospect == False)  # noqa: E712
        if client_id is not None:
            query = query.where(Client.id == client_id)
        for client in ctx.session.exec(query):
            results[client.id] = []
            for name in agents or WEEKLY_ORDER:
                try:
                    res = AGENTS[name].run(ctx, client)
                except Exception as exc:  # keep going: log and move to the next agent
                    log.exception("%s failed for client %s", name, client.id)
                    ctx.session.rollback()
                    res = AgentResult(agent=name, client_id=client.id, summary="failed", errors=[repr(exc)])
                results[client.id].append(res)
                log.info("client %s %s: %s", client.id, name, res.summary)
    return results


def start(day_of_week: str = "mon", hour: int = 6, minute: int = 0, dry_run: bool = False) -> None:
    settings = load_settings()
    scheduler = BlockingScheduler(timezone=settings.timezone)
    scheduler.add_job(
        run_weekly,
        CronTrigger(day_of_week=day_of_week, hour=hour, minute=minute, timezone=settings.timezone),
        kwargs={"dry_run": dry_run},
        id="weekly",
        misfire_grace_time=3600,
        coalesce=True,
    )
    log.info("Weekly run scheduled %s %02d:%02d %s", day_of_week, hour, minute, settings.timezone)
    scheduler.start()
