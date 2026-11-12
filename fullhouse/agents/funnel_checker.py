"""Agent 5 — Direct-Booking Funnel Checker.

Checks that the direct rate undercuts every OTA by the agreed buffer on three
sample dates, that the website responds quickly, that a booking engine is
linked, and that SEO basics (title, meta description, schema.org
LodgingBusiness markup, local keywords) are present.

Rates come from today's RateSnapshot rows (the rate scraper records both the
OTA and, when configured, the direct booking engine). Speed here is server
response + download time from this machine, not a full Lighthouse run.
"""

from __future__ import annotations

from datetime import timedelta

from sqlmodel import func, select

from fullhouse.agents.base import AgentContext, AgentResult
from fullhouse.core.fetch import FetchError
from fullhouse.core.models import Client, RateSnapshot
from fullhouse.core.scrapers.common import LODGING_TYPES, json_ld, soup, text

NAME = "funnel_checker"
SAMPLE_LEADS = (7, 14, 30)
OTA_NAMES = {"booking": "Booking.com", "hostelworld": "HostelWorld", "expedia": "Expedia"}
SLOW_MS = 2500
BOOKING_ENGINES = {
    "SiteMinder (TheBookingButton)": ("thebookingbutton", "book-directonline.com", "siteminder"),
    "Cloudbeds": ("cloudbeds.com",),
    "Little Hotelier": ("littlehotelier.com",),
    "RMS Cloud": ("rmscloud.com",),
    "NewBook": ("newbook.cloud", "newbookmodern"),
    "ResBook / Resonline": ("resonline", "resbook"),
    "STAAH": ("staah.net", "staah.com"),
    "Beds24": ("beds24.com",),
    "Sirvoy": ("sirvoy.com",),
}


def check_site(ctx: AgentContext, client: Client) -> tuple[dict, list[tuple[str, str]]]:
    issues: list[tuple[str, str]] = []
    url = client.website_url
    if not url:
        return {}, [("high", "No website on file: the funnel cannot be checked.")]
    try:
        page = ctx.fetcher.get(url, use_cache=False)
    except FetchError as exc:
        return {"error": str(exc)}, [("high", f"Website did not load: {exc}")]
    if page.status >= 400:
        return {"status": page.status}, [("high", f"Website returned HTTP {page.status}.")]

    doc = soup(page.html)
    title = text(doc.title)
    meta = doc.select_one('meta[name="description"]')
    meta_desc = (meta.get("content") or "").strip() if meta else ""
    h1 = text(doc.select_one("h1"))
    viewport = doc.select_one('meta[name="viewport"]') is not None
    schema = json_ld(doc, *LODGING_TYPES)
    blob = page.html.lower()
    engines = [name for name, needles in BOOKING_ENGINES.items() if any(n in blob for n in needles)]
    keywords = [k for k in dict.fromkeys([client.region.split(",")[0].strip(), client.property_type.value]) if k]
    local_hits = [k for k in keywords if k.lower() in f"{title} {h1}".lower()]

    if not page.url.startswith("https://"):
        issues.append(("high", "Site is not served over HTTPS."))
    if page.elapsed_ms > SLOW_MS:
        issues.append(("medium", f"Homepage took {page.elapsed_ms / 1000:.1f}s to load (target under "
                       f"{SLOW_MS / 1000:.1f}s). Compress images and check hosting."))
    if not title:
        issues.append(("high", "Homepage has no <title>."))
    elif len(local_hits) < len(keywords):
        missing = [k for k in keywords if k not in local_hits]
        issues.append(("medium", f"Title/H1 do not mention {', '.join(missing)}. Use e.g. "
                       f"'{client.name} | {client.property_type.value.title()} in {client.region.split(',')[0]}'."))
    if not meta_desc:
        issues.append(("medium", "No meta description. Add 140-160 characters naming location and a reason to book direct."))
    if not h1:
        issues.append(("low", "No H1 heading on the homepage."))
    if not viewport:
        issues.append(("medium", "No mobile viewport tag: the site will render poorly on phones."))
    if not schema:
        issues.append(("medium", "No schema.org LodgingBusiness/Hotel markup. Add JSON-LD with name, address, "
                       "geo, priceRange and aggregateRating."))
    if not engines:
        issues.append(("high", "No booking engine detected on the homepage. Add a 'Book direct' button to the engine."))

    facts = {
        "url": page.url,
        "status": page.status,
        "load_ms": page.elapsed_ms,
        "title": title,
        "meta_description": meta_desc,
        "h1": h1,
        "viewport": viewport,
        "schema_type": schema.get("@type") if schema else None,
        "booking_engines": engines,
    }
    return facts, issues


def compare_rates(ctx: AgentContext, client: Client) -> tuple[list[dict], list[tuple[str, str]]]:
    latest = ctx.session.exec(
        select(func.max(RateSnapshot.date)).where(
            RateSnapshot.client_id == client.id, RateSnapshot.competitor_id.is_(None)
        )
    ).one()
    if latest is None:
        return [], [("medium", "No rate data yet: run rate_scraper first.")]
    snaps = list(
        ctx.session.exec(
            select(RateSnapshot).where(
                RateSnapshot.client_id == client.id,
                RateSnapshot.competitor_id.is_(None),
                RateSnapshot.date == latest,
                RateSnapshot.lead_days.in_(SAMPLE_LEADS),
            )
        )
    )
    direct = [s for s in snaps if s.ota == "direct"]
    if not direct:
        # Config hint for us: set ota_urls["direct"] to the engine URL with {checkin}/{checkout}.
        return [], [("high", "Direct-rate parity not checked: we need the link to your online booking page "
                     "to confirm booking direct is cheaper than the OTAs.")]

    buffer = ctx.settings.direct_rate_buffer_pct
    rows = []
    for lead in SAMPLE_LEADS:
        for room_type in sorted({s.room_type for s in direct if s.lead_days == lead}):
            d = min(s.price for s in direct if s.lead_days == lead and s.room_type == room_type)
            for ota in sorted({s.ota for s in snaps if s.ota != "direct"}):
                ota_prices = [s.price for s in snaps if s.ota == ota and s.lead_days == lead and s.room_type == room_type]
                if not ota_prices:
                    continue
                o = min(ota_prices)
                gap_pct = round((o - d) / o * 100, 1)  # how much cheaper direct is than the OTA
                status = "ok" if gap_pct >= buffer else ("ota_cheaper" if o < d else "buffer_short")
                rows.append({"lead_days": lead, "checkin": (latest + timedelta(days=lead)).isoformat(),
                             "room_type": room_type, "ota": ota, "direct": d, "ota_price": o,
                             "direct_saving_pct": gap_pct, "status": status})

    # One issue per date and OTA, listing the rooms affected.
    issues = []
    for (lead, ota, status), group in _group(rows).items():
        if status == "ok":
            continue
        when = f"{latest + timedelta(days=lead):%d %b}"
        name = OTA_NAMES.get(ota, ota)
        if status == "ota_cheaper":
            rooms = ", ".join(f"{r['room_type']} ${r['ota_price']:.0f} vs ${r['direct']:.0f} direct" for r in group)
            issues.append(("high", f"{name} is cheaper than booking direct for {when}: {rooms}. "
                           "Fix rate parity in the channel manager or lower the direct rate."))
        else:
            rooms = ", ".join(f"{r['room_type']} {r['direct_saving_pct']}%" for r in group)
            issues.append(("medium", f"Booking direct for {when} saves less than the {buffer:g}% target vs "
                           f"{name}: {rooms}."))
    return rows, issues


def _group(rows: list[dict]) -> dict[tuple, list[dict]]:
    out: dict[tuple, list[dict]] = {}
    for r in rows:
        out.setdefault((r["lead_days"], r["ota"], r["status"]), []).append(r)
    return out


def run(ctx: AgentContext, client: Client) -> AgentResult:
    result = AgentResult(agent=NAME, client_id=client.id, summary="")
    site, site_issues = check_site(ctx, client)
    rates, rate_issues = compare_rates(ctx, client)
    ctx.clear_findings(client, NAME)
    for severity, msg in site_issues + rate_issues:
        ctx.record(client, NAME, msg, severity)
    ctx.session.commit()
    issues = site_issues + rate_issues
    result.data = {"site": site, "rates": rates, "issues": [{"severity": s, "text": t} for s, t in issues]}
    breaches = len({r["checkin"] for r in rates if r["status"] == "ota_cheaper"})
    result.summary = f"{len(issues)} funnel issues; an OTA undercuts the direct rate on {breaches} of " \
                     f"{len(SAMPLE_LEADS)} sample dates."
    return result
