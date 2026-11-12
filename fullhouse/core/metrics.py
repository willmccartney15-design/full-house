"""Weekly KPI maths and CSV import.

Definitions (from the brief): Revenue = accommodation revenue ex-GST.
ADR = revenue / rooms sold. RevPAR = revenue / rooms available.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from sqlmodel import Session, select

from fullhouse.core.models import WeeklyMetrics

CSV_COLUMNS = ("week_start", "revenue", "rooms_sold", "rooms_available")  # ly_revenue optional


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def adr(revenue: float, rooms_sold: int) -> float:
    return round(revenue / rooms_sold, 2) if rooms_sold else 0.0


def revpar(revenue: float, rooms_available: int) -> float:
    return round(revenue / rooms_available, 2) if rooms_available else 0.0


def occupancy(rooms_sold: int, rooms_available: int) -> float:
    return round(rooms_sold / rooms_available, 4) if rooms_available else 0.0


def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or not old:
        return None
    return round((new - old) / old * 100, 1)


def import_csv(session: Session, client_id: int, path: Path) -> int:
    """Upsert weekly rows from a PMS export. Returns rows written."""
    count = 0
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in CSV_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing columns {missing}; need {CSV_COLUMNS} (+ optional ly_revenue)")
        for raw in reader:
            week = monday(date.fromisoformat(raw["week_start"].strip()))
            revenue = float(raw["revenue"])
            sold = int(float(raw["rooms_sold"]))
            available = int(float(raw["rooms_available"]))
            ly = raw.get("ly_revenue")
            row = session.exec(
                select(WeeklyMetrics).where(WeeklyMetrics.client_id == client_id, WeeklyMetrics.week_start == week)
            ).first() or WeeklyMetrics(client_id=client_id, week_start=week, revenue=0, rooms_sold=0, rooms_available=0)
            row.revenue, row.rooms_sold, row.rooms_available = revenue, sold, available
            row.adr, row.revpar = adr(revenue, sold), revpar(revenue, available)
            row.ly_revenue = float(ly) if ly not in (None, "") else row.ly_revenue
            session.add(row)
            count += 1
    session.commit()
    _backfill_ly(session, client_id)
    return count


def _backfill_ly(session: Session, client_id: int) -> None:
    """Fill ly_revenue from the row 52 weeks earlier where the CSV did not supply it."""
    rows = {r.week_start: r for r in metrics_for(session, client_id)}
    for week, row in rows.items():
        if row.ly_revenue is None and (ly := rows.get(week - timedelta(weeks=52))):
            row.ly_revenue = ly.revenue
            session.add(row)
    session.commit()


def metrics_for(session: Session, client_id: int) -> list[WeeklyMetrics]:
    return list(
        session.exec(
            select(WeeklyMetrics).where(WeeklyMetrics.client_id == client_id).order_by(WeeklyMetrics.week_start)
        )
    )


@dataclass
class WeekSummary:
    week_start: date
    revenue: float
    rooms_sold: int
    rooms_available: int
    adr: float
    revpar: float
    occupancy: float
    revenue_wow: float | None
    revenue_yoy: float | None
    adr_wow: float | None
    adr_yoy: float | None
    revpar_yoy: float | None
    occupancy_yoy_pts: float | None
    ly_revenue: float | None


def summarise_week(session: Session, client_id: int, week_start: date) -> WeekSummary:
    rows = {r.week_start: r for r in metrics_for(session, client_id)}
    this = rows.get(week_start)
    if this is None:
        raise LookupError(f"No WeeklyMetrics for client {client_id} week {week_start}")
    prev = rows.get(week_start - timedelta(weeks=1))
    ly = rows.get(week_start - timedelta(weeks=52))
    occ = occupancy(this.rooms_sold, this.rooms_available)
    ly_rev = this.ly_revenue if this.ly_revenue is not None else (ly.revenue if ly else None)
    return WeekSummary(
        week_start=week_start,
        revenue=this.revenue,
        rooms_sold=this.rooms_sold,
        rooms_available=this.rooms_available,
        adr=this.adr,
        revpar=this.revpar,
        occupancy=occ,
        revenue_wow=pct_change(this.revenue, prev.revenue if prev else None),
        revenue_yoy=pct_change(this.revenue, ly_rev),
        adr_wow=pct_change(this.adr, prev.adr if prev else None),
        adr_yoy=pct_change(this.adr, ly.adr if ly else None),
        revpar_yoy=pct_change(this.revpar, ly.revpar if ly else None),
        occupancy_yoy_pts=round((occ - occupancy(ly.rooms_sold, ly.rooms_available)) * 100, 1) if ly else None,
        ly_revenue=ly_rev,
    )
