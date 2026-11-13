"""Agent 1 — Competitor Rate Scraper.

For the client and each competitor, fetch prices for check-in 1, 7, 14 and 30
days out, store RateSnapshot rows, and build a position table: the client's
lowest price per room category vs the market median.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import timedelta
from statistics import median

from bs4 import BeautifulSoup
from sqlmodel import select

from fullhouse.agents.base import AgentContext, AgentResult, log
from fullhouse.core.fetch import FetchError
from fullhouse.core.models import Client, RateSnapshot
from fullhouse.core.rooms import normalise_room
from fullhouse.core.scrapers import dated_url, parse_listing
from fullhouse.core.scrapers.common import parse_price

NAME = "rate_scraper"
LEAD_DAYS = (1, 7, 14, 30)
OTA_PREFERENCE = ("booking", "hostelworld", "expedia")


@dataclass
class PositionRow:
    room_type: str
    lead_days: int
    client_price: float | None
    market_median: float | None
    market_low: float | None
    market_high: float | None
    competitors_priced: int
    diff_pct: float | None  # client vs median; + means client is dearer


def primary_ota(ota_urls: dict) -> str | None:
    return next((o for o in OTA_PREFERENCE if ota_urls.get(o)), None)


IMPORT_MAX_AGE_DAYS = 7


def latest_import(ctx: AgentContext, client: Client) -> list[RateSnapshot]:
    """Imported rate-shopper snapshots from the last week (newest extract only), if any."""
    newest = ctx.session.exec(
        select(RateSnapshot.date).where(RateSnapshot.client_id == client.id, RateSnapshot.source == "import")
        .order_by(RateSnapshot.date.desc())
    ).first()
    if newest is None or (ctx.today - newest).days > IMPORT_MAX_AGE_DAYS:
        return []
    return list(ctx.session.exec(select(RateSnapshot).where(
        RateSnapshot.client_id == client.id, RateSnapshot.source == "import", RateSnapshot.date == newest
    )))


def standard_leads(snaps: list[RateSnapshot], leads: tuple[int, ...] = LEAD_DAYS,
                   tolerance: int = 3) -> list[RateSnapshot]:
    """Keep the stay dates closest to 1/7/14/30 days out (within ``tolerance``), so a 90-day export reads
    like a scrape. Lead values are kept as they are; the table shows the real distance."""
    available = {s.lead_days for s in snaps if s.competitor_id is None}
    chosen = set()
    for target in leads:
        near = [d for d in available if abs(d - target) <= tolerance]
        if near:
            chosen.add(min(near, key=lambda d: (abs(d - target), d)))
    return [s for s in snaps if s.lead_days in chosen]


def run(ctx: AgentContext, client: Client, lead_days: tuple[int, ...] = LEAD_DAYS) -> AgentResult:
    result = AgentResult(agent=NAME, client_id=client.id, summary="")
    imported = latest_import(ctx, client)
    if imported:  # a licensed rate-shopper export beats scraping: use it and touch no OTA
        table = position_table(standard_leads(imported, lead_days))
        result.data = {"ota": "import", "snapshots": len(imported), "source": "import",
                       "extract_date": imported[0].date.isoformat(), "position": [asdict(r) for r in table]}
        if not table:
            result.errors.append(f"The imported rates from {imported[0].date} have no prices for {client.name} "
                                 "near 1/7/14/30 days out; check its name in the file.")
        dear = sum(1 for r in table if r.diff_pct is not None and r.diff_pct > 10)
        cheap = sum(1 for r in table if r.diff_pct is not None and r.diff_pct < -10)
        result.summary = (f"Using imported rates from {imported[0].date:%d %b} ({len(imported)} prices). "
                          f"{dear} room/lead combinations more than 10% above market median, {cheap} more than "
                          "10% below.")
        return result
    ota = primary_ota(client.ota_urls or {})
    if ota is None:
        result.errors.append("Client has no OTA URL (booking / hostelworld / expedia).")
        result.summary = "No OTA listing to compare."
        return result

    # Re-running on the same day replaces that day's snapshots.
    for old in ctx.session.exec(
        select(RateSnapshot).where(RateSnapshot.client_id == client.id, RateSnapshot.date == ctx.today)
    ):
        ctx.session.delete(old)

    targets = [(None, client.name, client.ota_urls or {})]
    targets += [(c.id, c.name, c.ota_urls or {}) for c in ctx.competitors(client)]
    snapshots: list[RateSnapshot] = []
    for competitor_id, name, urls in targets:
        url = urls.get(ota)
        if not url:
            result.errors.append(f"{name}: no {ota} URL")
            continue
        for lead in lead_days:
            checkin = ctx.today + timedelta(days=lead)
            try:
                page = ctx.fetcher.get(dated_url(ota, url, checkin), render=True)
            except FetchError as exc:
                result.errors.append(f"{name} +{lead}d: {exc}")
                continue
            if page.status >= 400:
                result.errors.append(f"{name} +{lead}d: HTTP {page.status}")
                continue
            listing = parse_listing(ota, page.html, url)
            priced = [r for r in listing.rooms if r.price]
            if not priced:
                log.info("%s +%dd: no prices (sold out or markup changed)", name, lead)
            for room in priced:
                snapshots.append(
                    RateSnapshot(
                        client_id=client.id,
                        competitor_id=competitor_id,
                        date=ctx.today,
                        ota=ota,
                        room_type=normalise_room(room.name),
                        room_name=room.name,
                        lead_days=lead,
                        price=room.price,
                    )
                )

    snapshots += _direct_rates(ctx, client, lead_days, result)
    ctx.session.add_all(snapshots)
    ctx.session.commit()

    table = position_table([s for s in snapshots if s.ota == ota])
    result.data = {"ota": ota, "snapshots": len(snapshots), "position": [asdict(r) for r in table]}
    dear = [r for r in table if r.diff_pct is not None and r.diff_pct > 10]
    cheap = [r for r in table if r.diff_pct is not None and r.diff_pct < -10]
    result.summary = (
        f"{len(snapshots)} prices from {len(targets)} properties on {ota} (incl. direct). "
        f"{len(dear)} room/lead combinations more than 10% above market median, {len(cheap)} more than 10% below."
    )
    return result


def position_table(snapshots: list[RateSnapshot]) -> list[PositionRow]:
    """Client's lowest price vs competitors' lowest prices, per room category and lead time."""
    lowest: dict[tuple[str, int, int | None], float] = {}
    for s in snapshots:
        key = (s.room_type, s.lead_days, s.competitor_id)
        lowest[key] = min(lowest.get(key, s.price), s.price)

    rows = []
    for room_type, lead in sorted({(k[0], k[1]) for k in lowest if k[2] is None}):
        market = [p for (rt, ld, cid), p in lowest.items() if rt == room_type and ld == lead and cid is not None]
        client_price = lowest[(room_type, lead, None)]
        med = round(median(market), 2) if market else None
        rows.append(
            PositionRow(
                room_type=room_type,
                lead_days=lead,
                client_price=client_price,
                market_median=med,
                market_low=min(market) if market else None,
                market_high=max(market) if market else None,
                competitors_priced=len(market),
                diff_pct=round((client_price - med) / med * 100, 1) if med else None,
            )
        )
    return rows


def pivot(position: list[dict]) -> dict[str, dict[int, dict]]:
    """Position rows regrouped as {room_type: {lead_days: row}} for compact tables."""
    out: dict[str, dict[int, dict]] = {}
    for r in position:
        out.setdefault(r["room_type"], {})[r["lead_days"]] = r
    return out


def _direct_rates(ctx: AgentContext, client: Client, lead_days, result: AgentResult) -> list[RateSnapshot]:
    """Direct-booking prices, when ota_urls['direct'] is a booking-engine URL with {checkin}/{checkout}."""
    template = (client.ota_urls or {}).get("direct")
    if not template or "{checkin}" not in template:
        return []
    out = []
    for lead in lead_days:
        checkin = ctx.today + timedelta(days=lead)
        url = template.format(checkin=checkin.isoformat(), checkout=(checkin + timedelta(days=1)).isoformat())
        try:
            page = ctx.fetcher.get(url, render=True)
        except FetchError as exc:
            result.errors.append(f"direct +{lead}d: {exc}")
            continue
        for name, price in parse_direct(page.html):
            out.append(
                RateSnapshot(
                    client_id=client.id,
                    competitor_id=None,
                    date=ctx.today,
                    ota="direct",
                    room_type=normalise_room(name),
                    room_name=name,
                    lead_days=lead,
                    price=price,
                )
            )
    return out


def parse_direct(html: str) -> list[tuple[str, float]]:
    """Best-effort parser for booking-engine result pages: elements tagged as rooms that carry a price.

    Works with engines that mark rooms with a 'room' class or data attribute. For an engine this misses,
    add a dedicated parser here and a fixture test.
    """
    doc = BeautifulSoup(html, "html.parser")
    found = []
    for node in doc.select("[data-room-name], .room, .room-type, .rate-plan"):
        name = node.get("data-room-name") or ""
        if not name and (title := node.select_one(".room-name, h2, h3, h4")):
            name = title.get_text(" ", strip=True)
        price = parse_price(node.get_text(" ", strip=True))
        if name and price:
            found.append((re.sub(r"\s+", " ", name).strip(), price))
    return found
