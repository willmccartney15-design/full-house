"""No-ROI-no-fee guarantee calculator.

Rule (from the brief): if trailing revenue at day 60 and day 90 is not above
the same period last year, adjusted for room-count changes and closure days,
fees for that period are refunded or waived. Month-on-month comparisons are
never used (seasonality). The property needs >= 12 months of PMS data.

Method, on weekly data:
- Window = the full weeks (Monday starts) between start_date and the checkpoint
  (start_date + 60 or + 90 days).
- This-year revenue = sum of revenue over those weeks.
- Last-year revenue = the same weeks 52 weeks earlier (or ly_revenue when the
  52-weeks-earlier row is missing).
- Capacity adjustment: LY revenue is scaled by this-year room-nights / LY
  room-nights. This-year room-nights = reported rooms_available minus
  room_count x each closure day (Client.closure_days) in the window. Record
  rooms_available as full capacity (room_count x 7) and closures as closure
  days, so they are not counted twice.
- Pass when this-year revenue > adjusted LY revenue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlmodel import Session

from fullhouse.core.metrics import metrics_for, monday
from fullhouse.core.models import Client


@dataclass
class GuaranteeResult:
    checkpoint_days: int
    checkpoint: date
    eligible: bool
    passed: bool | None
    revenue: float
    ly_revenue_adjusted: float
    delta: float  # this year minus adjusted LY; negative = shortfall
    weeks: int
    capacity_factor: float
    note: str = ""


def evaluate(session: Session, client: Client, days: int, today: date | None = None) -> GuaranteeResult:
    if client.start_date is None:
        raise ValueError(f"{client.name}: start_date is not set")
    checkpoint = client.start_date + timedelta(days=days)
    rows = {r.week_start: r for r in metrics_for(session, client.id)}

    first_week = monday(client.start_date)
    if first_week < client.start_date:
        first_week += timedelta(weeks=1)  # only full weeks inside the window
    weeks = []
    w = first_week
    while w + timedelta(days=7) <= checkpoint:
        weeks.append(w)
        w += timedelta(weeks=1)

    earliest = min(rows) if rows else None
    has_history = earliest is not None and earliest <= client.start_date - timedelta(days=358)
    has_ly_field = bool(weeks) and all(rows.get(wk) and rows[wk].ly_revenue is not None for wk in weeks)
    eligible = has_history or has_ly_field

    def result(passed, revenue=0.0, ly_adj=0.0, factor=1.0, note=""):
        return GuaranteeResult(
            checkpoint_days=days,
            checkpoint=checkpoint,
            eligible=eligible,
            passed=passed,
            revenue=round(revenue, 2),
            ly_revenue_adjusted=round(ly_adj, 2),
            delta=round(revenue - ly_adj, 2),
            weeks=len(weeks),
            capacity_factor=round(factor, 4),
            note=note,
        )

    if not eligible:
        return result(None, note="Not eligible: needs 12 months of PMS data before start_date.")
    if today is not None and today < checkpoint:
        return result(None, note=f"Checkpoint not reached (due {checkpoint.isoformat()}).")
    missing = [wk for wk in weeks if wk not in rows]
    if missing:
        return result(None, note=f"Missing weekly data for {', '.join(m.isoformat() for m in missing)}.")

    closures = {date.fromisoformat(d) for d in (client.closure_days or [])}
    revenue = ty_nights = ly_revenue = ly_nights = 0.0
    for wk in weeks:
        row = rows[wk]
        closed = sum(1 for i in range(7) if wk + timedelta(days=i) in closures)
        revenue += row.revenue
        ty_nights += max(row.rooms_available - closed * client.room_count, 0)
        ly = rows.get(wk - timedelta(weeks=52))
        if ly is not None:
            ly_revenue += ly.revenue
            ly_nights += ly.rooms_available
        else:
            ly_revenue += row.ly_revenue or 0.0
            ly_nights += row.rooms_available  # no LY capacity on record: assume unchanged
    factor = ty_nights / ly_nights if ly_nights else 1.0
    ly_adj = ly_revenue * factor
    return result(revenue > ly_adj, revenue, ly_adj, factor)
