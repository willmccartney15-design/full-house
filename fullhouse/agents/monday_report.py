"""Agent 6 — Monday Report Generator.

Takes last week's WeeklyMetrics, compares week-on-week and to the same week
last year, and has Claude explain the movement in plain English (about 250
words) with exactly three actions for the week. Renders a PDF and drops an
email with the PDF attached into the outbox (sending is stubbed).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path

from pydantic import BaseModel, Field
from sqlmodel import select

from fullhouse.agents.base import AgentContext, AgentResult
from fullhouse.agents.rate_scraper import pivot, position_table, standard_leads
from fullhouse.core import guarantee
from fullhouse.core.metrics import WeekSummary, metrics_for, monday, summarise_week
from fullhouse.core.models import Action, ActionStatus, Client, Finding, ListingAudit, RateSnapshot
from fullhouse.reports.render import render_pdf

NAME = "monday_report"


class ActionItem(BaseModel):
    text: str = Field(description="One concrete action for this week, imperative, under 25 words")
    why: str = Field(description="The number or finding that justifies it, one sentence")


class Narrative(BaseModel):
    headline: str = Field(description="One sentence: the week's result with its main number")
    narrative: str = Field(description="About 250 words, plain English, no jargon beyond ADR/RevPAR/occupancy")
    actions: list[ActionItem] = Field(description="Exactly three actions, most valuable first")


SYSTEM = """You are the revenue manager for an independent Australian motel or hostel, writing the owner's Monday
report. Explain what moved revenue last week and why, using only the numbers provided. Compare to the same week
last year first (week-on-week moves are mostly seasonality). Name the drivers (occupancy vs rate), the market
position and any listing or funnel problems. No hedging, no filler, no headings. Then give exactly three actions for
this week, each one specific enough to do in under an hour or to hand to staff."""


def fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.1f}%"


def gather(ctx: AgentContext, client: Client, week: date) -> dict:
    s = summarise_week(ctx.session, client.id, week)
    trend = [
        {"week_start": r.week_start.isoformat(), "revenue": r.revenue, "adr": r.adr, "revpar": r.revpar}
        for r in metrics_for(ctx.session, client.id)
        if week - timedelta(weeks=5) < r.week_start <= week
    ]
    last_actions = list(
        ctx.session.exec(
            select(Action).where(Action.client_id == client.id, Action.week_start == week)
        )
    )
    latest_rates = ctx.session.exec(
        select(RateSnapshot.date).where(RateSnapshot.client_id == client.id).order_by(RateSnapshot.date.desc())
    ).first()
    position = []
    if latest_rates:
        snaps = list(
            ctx.session.exec(
                select(RateSnapshot).where(
                    RateSnapshot.client_id == client.id, RateSnapshot.date == latest_rates, RateSnapshot.ota != "direct"
                )
            )
        )
        if any(s.source == "import" for s in snaps):  # never mix an import with a scrape from the same day
            snaps = [s for s in snaps if s.source == "import"]
        position = [asdict(r) for r in position_table(standard_leads(snaps))]
    audit = ctx.session.exec(
        select(ListingAudit).where(ListingAudit.client_id == client.id).order_by(ListingAudit.date.desc())
    ).first()
    findings = list(
        ctx.session.exec(
            select(Finding).where(Finding.client_id == client.id, Finding.date >= ctx.today - timedelta(days=14))
        )
    )
    checks = []
    if client.start_date:
        for days in (60, 90):
            g = guarantee.evaluate(ctx.session, client, days, today=ctx.today)
            checks.append(asdict(g))
    return {
        "summary": s,
        "trend": trend,
        "last_week_actions": [{"text": a.text, "status": a.status.value} for a in last_actions],
        "position": position,
        "pivot": pivot(position),
        "leads": sorted({r["lead_days"] for r in position}),
        "audit": {"ota": audit.ota, "score": audit.score, "top_fixes": audit.findings.get("fixes", [])[:3]}
        if audit else None,
        "findings": [{"agent": f.agent, "severity": f.severity, "text": f.text} for f in findings],
        "guarantee": checks,
    }


def stub_narrative(client: Client, data: dict) -> Narrative:
    s: WeekSummary = data["summary"]
    driver = "occupancy" if abs(s.occupancy_yoy_pts or 0) >= abs((s.adr_yoy or 0) / 2) else "rate"
    narrative = (
        f"Revenue for the week starting {s.week_start:%d %b} was ${s.revenue:,.0f}, {fmt_pct(s.revenue_yoy)} on the "
        f"same week last year and {fmt_pct(s.revenue_wow)} on the week before. You sold {s.rooms_sold} of "
        f"{s.rooms_available} room-nights ({s.occupancy:.0%} occupancy, {s.occupancy_yoy_pts or 0:+.1f} points on last "
        f"year) at an ADR of ${s.adr:,.0f} ({fmt_pct(s.adr_yoy)} year on year). RevPAR was ${s.revpar:,.0f} "
        f"({fmt_pct(s.revpar_yoy)}). The main driver this week was {driver}."
    )
    dear = [p for p in data["position"] if p["diff_pct"] is not None and p["diff_pct"] > 10]
    cheap = [p for p in data["position"] if p["diff_pct"] is not None and p["diff_pct"] < -10]
    if dear:
        narrative += f" You are priced more than 10% above the market median on {len(dear)} room/date combinations."
    if cheap:
        narrative += f" You are more than 10% below the market median on {len(cheap)} room/date combinations."
    candidates: list[ActionItem] = []
    for f in sorted(data["findings"], key=lambda f: {"high": 0, "medium": 1, "low": 2}.get(f["severity"], 3)):
        candidates.append(ActionItem(text=f["text"], why=f"Flagged by {f['agent'].replace('_', ' ')}."))
    if data["audit"]:
        for fix in data["audit"]["top_fixes"]:
            candidates.append(ActionItem(text=fix["fix"], why=fix["expected_impact"]))
    if cheap:
        c = cheap[0]
        candidates.insert(0, ActionItem(
            text=f"Raise {c['room_type']} rates for stays {c['lead_days']} days out towards ${c['market_median']:.0f}.",
            why=f"You are {c['diff_pct']:.0f}% under the market median at that lead time."))
    candidates += [
        ActionItem(text="Reply to every review from last week.", why="Responses lift review-driven conversion."),
        ActionItem(text="Check next weekend's pickup against last year.", why="Weekends set most of the week's revenue."),
        ActionItem(text="Post one offer on Google Business Profile.", why="Keeps the profile active in local search."),
    ]
    seen, actions = set(), []
    for a in candidates:
        if a.text not in seen:
            seen.add(a.text)
            actions.append(a)
    return Narrative(
        headline=f"Revenue ${s.revenue:,.0f}, {fmt_pct(s.revenue_yoy)} on last year.",
        narrative=narrative,
        actions=actions[:3],
    )


def write(ctx: AgentContext, client: Client, data: dict) -> Narrative:
    s: WeekSummary = data["summary"]
    payload = {k: v for k, v in data.items() if k not in ("pivot", "leads")}
    payload["summary"] = asdict(s)
    prompt = (
        f"Property: {client.name}, {client.property_type} in {client.region}, {client.room_count} rooms. "
        f"Currency AUD, revenue ex-GST.\n\nData (JSON):\n{_json(payload)}\n\n"
        "Write the headline, the ~250-word narrative and exactly three actions."
    )
    stub = lambda: stub_narrative(client, data)  # noqa: E731
    result = ctx.claude.structured(SYSTEM, prompt, Narrative, agent=NAME, client_id=client.id, stub=stub)
    if len(result.actions) != 3:  # the contract is exactly three
        extra = [a for a in stub().actions if a.text not in {x.text for x in result.actions}]
        result.actions = (result.actions + extra)[:3]
    return result


def run(ctx: AgentContext, client: Client, week: date | None = None, send: bool = True) -> AgentResult:
    week = week or monday(ctx.today) - timedelta(weeks=1)
    result = AgentResult(agent=NAME, client_id=client.id, summary="")
    try:
        data = gather(ctx, client, week)
    except LookupError as exc:
        result.errors.append(str(exc))
        result.summary = f"No metrics for week starting {week}. Import the PMS CSV first."
        return result

    narrative = write(ctx, client, data)
    this_week = monday(ctx.today)
    for old in ctx.session.exec(
        select(Action).where(Action.client_id == client.id, Action.week_start == this_week)
    ):
        ctx.session.delete(old)
    for a in narrative.actions:
        ctx.session.add(Action(client_id=client.id, week_start=this_week, text=a.text, status=ActionStatus.open))
    ctx.session.commit()

    out_dir = ctx.settings.output_dir / f"client-{client.id}"
    pdf = render_pdf(
        "monday_report.html",
        {"client": client, "week": week, "s": data["summary"], "data": data, "n": narrative, "fmt_pct": fmt_pct,
         "today": ctx.today},
        out_dir / f"monday-report-{week.isoformat()}.pdf",
    )
    eml = _queue_email(ctx, client, narrative, pdf) if send else None
    result.data = {"week_start": week.isoformat(), "pdf": str(pdf), "email": str(eml) if eml else None,
                   **narrative.model_dump()}
    result.summary = narrative.headline
    return result


def _queue_email(ctx: AgentContext, client: Client, n: Narrative, pdf: Path) -> Path:
    """Email sending is stubbed: the message is written to the outbox as .eml for review."""
    msg = EmailMessage()
    msg["Subject"] = f"{client.name}: Monday report — {n.headline}"
    msg["From"] = "reports@fullhouse.local"
    msg["To"] = f"owner+client{client.id}@example.invalid"
    actions = "\n".join(f"{i}. {a.text}" for i, a in enumerate(n.actions, 1))
    msg.set_content(f"{n.narrative}\n\nThis week:\n{actions}\n")
    msg.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=pdf.name)
    path = ctx.settings.outbox_dir / f"{pdf.stem}-client{client.id}.eml"
    path.write_bytes(bytes(msg))
    return path


def _json(obj) -> str:
    import json

    return json.dumps(obj, default=str, indent=1)
