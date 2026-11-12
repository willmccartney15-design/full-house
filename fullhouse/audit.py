"""Free Revenue Audit: one Booking.com URL in, a 2-page PDF out.

1. Fetch and parse the listing; infer the property name and region.
2. Auto-pick 10 competitors from the same Booking.com search results.
3. Run rate_scraper, listing_auditor and funnel_checker.
4. Render free_audit.html to PDF.
The property is stored as a prospect Client so a signed client keeps its history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlmodel import select

from fullhouse.agents import funnel_checker, listing_auditor, rate_scraper
from fullhouse.agents.base import AgentContext
from fullhouse.core.models import Client, Competitor, PropertyType
from fullhouse.core.scrapers import booking
from fullhouse.reports.render import render_pdf

N_COMPETITORS = 10


@dataclass
class AuditOutput:
    client_id: int
    pdf: Path
    pages: int
    score: int
    competitors: list[str]
    headline: str


def _canonical(url: str) -> str:
    return url.split("?")[0]


def guess_type(name: str, listing) -> PropertyType:
    text = f"{name} {' '.join(listing.room_types)}".lower()
    return PropertyType.hostel if any(k in text for k in ("hostel", "backpack", "dorm")) else PropertyType.motel


def run(ctx: AgentContext, url: str, website: str | None = None) -> AuditOutput:
    if not booking.is_booking_url(url):
        raise ValueError("The free audit takes a Booking.com property URL.")
    url = _canonical(url)
    page = ctx.fetcher.get(booking.with_dates(url, ctx.today + timedelta(days=7)), render=True)
    if page.status >= 400:
        raise RuntimeError(f"Could not load {url} (HTTP {page.status}).")
    listing = booking.parse_listing(page.html, url)
    name = listing.name or url.rsplit("/", 1)[-1].split(".")[0].replace("-", " ").title()
    region = ", ".join(p for p in (listing.locality, listing.region) if p)

    client = ctx.session.exec(select(Client).where(Client.name == name)).first()
    if client is None:
        client = Client(name=name, is_prospect=True)
    client.region = region or client.region
    client.property_type = guess_type(name, listing)
    client.ota_urls = {**(client.ota_urls or {}), "booking": url}
    if website:
        client.website_url = website
    ctx.session.add(client)
    ctx.session.commit()

    competitors = pick_competitors(ctx, client, listing.locality or region)
    rates = rate_scraper.run(ctx, client)
    audit = listing_auditor.run(ctx, client).data["audits"].get("booking")
    if audit is None:
        raise RuntimeError("Listing audit failed; see logs.")
    funnel = funnel_checker.run(ctx, client)

    position = rates.data.get("position", [])
    pos7 = next((r for r in position if r["lead_days"] == 7 and r["diff_pct"] is not None), None)
    rr = listing.review_response_rate
    headline = _headline(client, audit, pos7)
    issues = sorted(funnel.data["issues"], key=lambda i: {"high": 0, "medium": 1, "low": 2}[i["severity"]])
    out = ctx.settings.output_dir / f"client-{client.id}" / f"free-audit-{ctx.today.isoformat()}.pdf"
    pdf = render_pdf(
        "free_audit.html",
        {
            "client": client,
            "today": ctx.today,
            "ota_label": "Booking.com",
            "headline": headline,
            "audit": listing_auditor.AuditResult.model_validate(
                {"criteria": audit["criteria"], "fixes": audit["fixes"]}
            ),
            "position": position,
            "pivot": rate_scraper.pivot(position),
            "leads": sorted({r["lead_days"] for r in position}),
            "pos7": pos7,
            "rr": "–" if rr is None else f"{rr:.0%}",
            "funnel_issues": issues,
            "funnel_high": sum(1 for i in issues if i["severity"] == "high"),
            "competitor_names": [c.name for c in competitors],
        },
        out,
    )
    return AuditOutput(
        client_id=client.id,
        pdf=out,
        pages=pdf.pages,
        score=audit["score"],
        competitors=[c.name for c in competitors],
        headline=headline,
    )


def pick_competitors(ctx: AgentContext, client: Client, area: str) -> list[Competitor]:
    existing = list(ctx.session.exec(select(Competitor).where(Competitor.client_id == client.id)))
    if len(existing) >= N_COMPETITORS:
        return existing[:N_COMPETITORS]
    page = ctx.fetcher.get(booking.search_url(area, ctx.today + timedelta(days=7)), render=True)
    own = _canonical(client.ota_urls["booking"])
    have = {_canonical(c.ota_urls.get("booking", "")) for c in existing}
    for r in booking.parse_search(page.html):
        if len(existing) >= N_COMPETITORS:
            break
        if r.url in (own, *have) or r.name == client.name:
            continue
        comp = Competitor(client_id=client.id, name=r.name, ota_urls={"booking": r.url})
        ctx.session.add(comp)
        existing.append(comp)
        have.add(r.url)
    ctx.session.commit()
    return existing


def _headline(client: Client, audit: dict, pos7: dict | None) -> str:
    parts = [f"{client.name}'s Booking.com listing scores {audit['score']}/100"]
    if pos7:
        diff = pos7["diff_pct"]
        where = "in line with" if abs(diff) < 2 else f"{abs(diff):.0f}% {'above' if diff > 0 else 'below'}"
        parts.append(f"and its {pos7['room_type']} rate a week out is {where} the local market median")
    top = audit["fixes"][0]["fix"] if audit["fixes"] else None
    sentence = " ".join(parts) + "."
    return sentence + (f" Biggest quick win: {top[0].lower() + top[1:]}" if top else "")
