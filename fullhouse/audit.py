"""Free Revenue Audit: a 2-page PDF for one Booking.com property.

Two ways in:

``run`` (live): fetch the listing, auto-pick 10 competitors from the same
Booking.com search, run rate_scraper + listing_auditor + funnel_checker.
Booking.com often turns away automated clients; then BlockedError says so.

``run_saved`` (from pages you saved in your own browser): the property page,
optionally a search results page for the area (competitors and their lowest
prices) and optionally individual competitor pages (per-room prices). No
automated requests go to Booking.com. Prices are for the dates the pages were
saved with.

Either way the property is stored as a prospect Client, so a signed client
keeps its history.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

from sqlmodel import select

from fullhouse.agents import funnel_checker, listing_auditor, rate_scraper
from fullhouse.agents.base import AgentContext
from fullhouse.core.models import Client, Competitor, PropertyType, RateSnapshot
from fullhouse.core.rooms import normalise_room
from fullhouse.core.scrapers import ListingData, booking
from fullhouse.reports.render import render_pdf

N_COMPETITORS = 10
LOWEST = "lowest"  # pseudo room type: each property's cheapest room, comparable even when room names differ


class BlockedError(RuntimeError):
    """The OTA served an empty or challenge page instead of real content."""


class SavedPageError(ValueError):
    """A saved page is missing something the audit needs."""


@dataclass
class AuditOutput:
    client_id: int
    pdf: Path
    pages: int
    score: int
    competitors: list[str]
    headline: str
    notes: list[str]


def _canonical(url: str) -> str:
    return url.split("?")[0]


def guess_type(name: str, listing) -> PropertyType:
    text = f"{name} {' '.join(listing.room_types)}".lower()
    return PropertyType.hostel if any(k in text for k in ("hostel", "backpack", "dorm")) else PropertyType.motel


# ---------------------------------------------------------------------- live


def run(ctx: AgentContext, url: str, website: str | None = None) -> AuditOutput:
    if not booking.is_booking_url(url):
        raise ValueError("The free audit takes a Booking.com property URL.")
    url = _canonical(url)
    page = ctx.fetcher.get(booking.with_dates(url, ctx.today + timedelta(days=7)), render=True)
    if page.status >= 400:
        raise RuntimeError(f"Could not load {url} (HTTP {page.status}).")
    listing = booking.parse_listing(page.html, url)
    if not listing.name and not listing.rooms:
        raise BlockedError(
            f"Booking.com served a page with no property details for {url} (HTTP {page.status}); it is probably "
            "turning away automated clients. Save the page in your browser and use `fullhouse audit --html`."
        )
    client = _upsert_client(ctx, listing, url, website)
    competitors = pick_competitors(ctx, client, listing.locality or client.region)
    position = rate_scraper.run(ctx, client).data.get("position", [])
    audit = listing_auditor.run(ctx, client).data["audits"].get("booking")
    if audit is None:
        raise RuntimeError("Listing audit failed; see logs.")
    issues = funnel_checker.run(ctx, client).data["issues"]
    return _report(ctx, client, listing, audit, position, issues, [c.name for c in competitors], lead=7, notes=[])


def pick_competitors(ctx: AgentContext, client: Client, area: str) -> list[Competitor]:
    existing = list(ctx.session.exec(select(Competitor).where(Competitor.client_id == client.id)))
    if len(existing) >= N_COMPETITORS:
        return existing[:N_COMPETITORS]
    page = ctx.fetcher.get(booking.search_url(area, ctx.today + timedelta(days=7)), render=True)
    results = booking.parse_search(page.html)
    if not results:
        raise BlockedError(
            f"Booking.com returned no search results for '{area}' (HTTP {page.status}). It usually does this when it "
            "turns away automated clients. Save the pages in your browser and use `fullhouse audit --html`, or add "
            "competitors by hand with `fullhouse client add-competitor`."
        )
    return _add_competitors(ctx, client, existing, [(r.name, r.url) for r in results])


# ---------------------------------------------------------------- saved pages


def run_saved(
    ctx: AgentContext,
    listing_html: Path,
    *,
    url: str | None = None,
    search_html: Path | None = None,
    competitor_html: list[Path] | None = None,
    checkin: date | None = None,
    website: str | None = None,
) -> AuditOutput:
    """Audit from pages saved in a browser. Booking.com is never contacted."""
    notes: list[str] = []
    html = _read(listing_html)
    url = _canonical(url) if url else booking.canonical_url(html)
    if not url:
        raise SavedPageError(f"{listing_html}: no Booking.com property URL found in the page; pass --url.")
    checkin = checkin or booking.detect_checkin(html)
    if checkin is None:
        raise SavedPageError(f"{listing_html}: no check-in date found in the page; pass --checkin YYYY-MM-DD "
                             "(the dates you searched with when you saved it).")
    lead = (checkin - ctx.today).days
    if lead < 0:
        raise SavedPageError(f"The saved prices are for {checkin}, which has passed. Save the pages again with "
                             "future dates.")

    listing = booking.parse_listing(html, url)
    if not listing.name and not listing.rooms:
        raise SavedPageError(f"{listing_html} does not look like a Booking.com property page (no name or rooms).")
    if not any(r.price for r in listing.rooms):
        notes.append("The property page shows no room prices (sold out, or saved without dates); price position "
                     "is left out.")
    client = _upsert_client(ctx, listing, url, website)

    # Competitors: individual pages first (room-level prices), then search results (lowest price only).
    priced: list[tuple[str, str, ListingData | None, float | None]] = []  # name, url, listing, lowest
    for path in competitor_html or []:
        chtml = _read(path)
        curl = booking.canonical_url(chtml) or f"file://{Path(path).resolve()}"
        comp = booking.parse_listing(chtml, curl)
        if not comp.name:
            notes.append(f"Skipped {Path(path).name}: not a Booking.com property page.")
            continue
        _check_dates(path, chtml, checkin, notes)
        priced.append((comp.name, curl, comp, _lowest(comp)))
    if search_html:
        shtml = _read(search_html)
        _check_dates(search_html, shtml, checkin, notes)
        results = booking.parse_search(shtml)
        if not results:
            notes.append(f"No properties found in {Path(search_html).name}; save the results list once it has loaded.")
        names = {n for n, *_ in priced}
        for r in results:
            if r.name not in names and _canonical(r.url) != url and r.name != client.name:
                priced.append((r.name, r.url, None, r.price))
                names.add(r.name)
    priced = priced[:N_COMPETITORS]
    if not priced:
        notes.append("No competitor pages or search results given, so there is no market comparison.")

    existing = list(ctx.session.exec(select(Competitor).where(Competitor.client_id == client.id)))
    competitors = _add_competitors(ctx, client, existing, [(n, u) for n, u, *_ in priced])
    by_name = {c.name: c for c in competitors}

    position = _store_saved_rates(ctx, client, listing, priced, by_name, lead)
    audit = listing_auditor.audit_and_store(ctx, client, listing)

    ctx.clear_findings(client, funnel_checker.NAME)
    _, issues = funnel_checker.check_site(ctx, client)
    if not client.website_url:
        issues = [("medium", "No website given, so the direct-booking site was not checked (use --website).")]
    for severity, text in issues:
        ctx.record(client, funnel_checker.NAME, text, severity)
    ctx.session.commit()
    issues = [{"severity": s, "text": t} for s, t in issues]
    issues.append({"severity": "medium", "text": "Direct-rate parity not checked in this audit: it needs the link "
                   "to your online booking page."})
    return _report(ctx, client, listing, audit, position, issues, [n for n, *_ in priced], lead=lead, notes=notes)


def _store_saved_rates(ctx, client, listing, priced, by_name, lead) -> list[dict]:
    for old in ctx.session.exec(
        select(RateSnapshot).where(RateSnapshot.client_id == client.id, RateSnapshot.date == ctx.today)
    ):
        ctx.session.delete(old)
    snaps: list[RateSnapshot] = []

    def add(competitor_id, room_type, room_name, price):
        snaps.append(RateSnapshot(client_id=client.id, competitor_id=competitor_id, date=ctx.today, ota="booking",
                                  room_type=room_type, room_name=room_name, lead_days=lead, price=price))

    def add_listing(competitor_id, data: ListingData):
        for room in data.rooms:
            if room.price:
                add(competitor_id, normalise_room(room.name), room.name, room.price)

    add_listing(None, listing)
    if (low := _lowest(listing)) is not None:
        add(None, LOWEST, "Cheapest room", low)
    for name, _, data, low in priced:
        cid = by_name[name].id
        if data is not None:
            add_listing(cid, data)
        if low is not None:
            add(cid, LOWEST, "Cheapest room", low)
    ctx.session.add_all(snaps)
    ctx.session.commit()
    rows = [asdict(r) for r in rate_scraper.position_table(snaps)]
    # Cheapest-room comparison first: it covers every competitor, including search-only ones.
    return sorted(rows, key=lambda r: (r["room_type"] != LOWEST, r["room_type"]))


def _lowest(data: ListingData) -> float | None:
    prices = [r.price for r in data.rooms if r.price]
    return min(prices) if prices else None


def _check_dates(path, html: str, checkin: date, notes: list[str]) -> None:
    found = booking.detect_checkin(html)
    if found and found != checkin:
        notes.append(f"{Path(path).name} was saved for {found}, not {checkin}; its prices are not like for like.")


def _read(path: Path) -> str:
    """HTML from a saved page: plain .html, or a Safari Web Archive (.webarchive, what iPhone/iPad Safari saves)."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise SavedPageError(f"Cannot read {path}: {exc.strerror}") from exc
    if data.startswith(b"bplist"):
        return _from_webarchive(path, data)
    return data.decode("utf-8", errors="replace")


def _from_webarchive(path: Path, data: bytes) -> str:
    import plistlib

    try:
        main = plistlib.loads(data)["WebMainResource"]
        encoding = main.get("WebResourceTextEncodingName") or "utf-8"
        return main["WebResourceData"].decode(encoding, errors="replace")
    except (plistlib.InvalidFileException, KeyError, LookupError, AttributeError) as exc:
        raise SavedPageError(f"{path}: not a readable Safari Web Archive ({exc}).") from exc


# ------------------------------------------------------------------- shared


def _upsert_client(ctx: AgentContext, listing: ListingData, url: str, website: str | None) -> Client:
    name = listing.name or url.rsplit("/", 1)[-1].split(".")[0].replace("-", " ").title()
    region = ", ".join(p for p in (listing.locality, listing.region) if p)
    client = ctx.session.exec(select(Client).where(Client.name == name)).first() or Client(name=name, is_prospect=True)
    client.region = region or client.region
    client.property_type = guess_type(name, listing)
    client.ota_urls = {**(client.ota_urls or {}), "booking": url}
    if website:
        client.website_url = website
    ctx.session.add(client)
    ctx.session.commit()
    return client


def _add_competitors(ctx, client: Client, existing: list[Competitor], candidates) -> list[Competitor]:
    own = _canonical(client.ota_urls["booking"])
    have_urls = {_canonical(c.ota_urls.get("booking", "")) for c in existing}
    have_names = {c.name for c in existing}
    for name, url in candidates:
        if len(existing) >= N_COMPETITORS:
            break
        url = _canonical(url)
        if url == own or name == client.name or url in have_urls or name in have_names:
            continue
        comp = Competitor(client_id=client.id, name=name, ota_urls={"booking": url})
        ctx.session.add(comp)
        existing.append(comp)
        have_urls.add(url)
        have_names.add(name)
    ctx.session.commit()
    return existing


def _lead_label(lead: int) -> str:
    return {0: "tonight", 1: "tomorrow", 7: "a week out"}.get(lead, f"{lead} days out")


def _report(ctx, client, listing, audit, position, issues, competitor_names, *, lead, notes) -> AuditOutput:
    pos = next((r for r in position if r["lead_days"] == lead and r["diff_pct"] is not None), None)
    headline = _headline(client, audit, pos, lead)
    issues = sorted(issues, key=lambda i: {"high": 0, "medium": 1, "low": 2}[i["severity"]])
    rr = listing.review_response_rate
    out = ctx.settings.output_dir / f"client-{client.id}" / f"free-audit-{ctx.today.isoformat()}.pdf"
    pdf = render_pdf(
        "free_audit.html",
        {
            "client": client,
            "today": ctx.today,
            "ota_label": "Booking.com",
            "headline": headline,
            "audit": listing_auditor.AuditResult.model_validate({"criteria": audit["criteria"], "fixes": audit["fixes"]}),
            "position": position,
            "pivot": rate_scraper.pivot(position),
            "leads": sorted({r["lead_days"] for r in position}),
            "pos": pos,
            "pos_label": _lead_label(lead),
            "rr": "–" if rr is None else f"{rr:.0%}",
            "funnel_issues": issues,
            "funnel_high": sum(1 for i in issues if i["severity"] == "high"),
            "competitor_names": competitor_names,
            "notes": notes,
        },
        out,
    )
    return AuditOutput(client_id=client.id, pdf=out, pages=pdf.pages, score=audit["score"],
                       competitors=competitor_names, headline=headline, notes=notes)


def _headline(client: Client, audit: dict, pos: dict | None, lead: int = 7) -> str:
    parts = [f"{client.name}'s Booking.com listing scores {audit['score']}/100"]
    if pos:
        diff = pos["diff_pct"]
        where = "in line with" if abs(diff) < 2 else f"{abs(diff):.0f}% {'above' if diff > 0 else 'below'}"
        room = "cheapest room" if pos["room_type"] == LOWEST else f"{pos['room_type']} rate"
        parts.append(f"and its {room} {_lead_label(lead)} is {where} the local market median")
    top = audit["fixes"][0]["fix"] if audit["fixes"] else None
    sentence = " ".join(parts) + "."
    return sentence + (f" Biggest quick win: {top[0].lower() + top[1:]}" if top else "")
