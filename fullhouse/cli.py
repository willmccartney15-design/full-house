"""Command line: `fullhouse --help`."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Optional

import typer
from sqlmodel import func, select

from fullhouse.agents import AGENTS
from fullhouse.core import guarantee as guarantee_mod
from fullhouse.core.metrics import import_csv
from fullhouse.core.models import ClaudeCall, Client, Competitor, PropertyType, Review
from fullhouse.runtime import context

app = typer.Typer(help="Full House: revenue-operations agents for independent motels and hostels.",
                  no_args_is_help=True)
client_app = typer.Typer(help="Manage clients and competitors.", no_args_is_help=True)
reviews_app = typer.Typer(help="Review drafts awaiting approval.", no_args_is_help=True)
app.add_typer(client_app, name="client")
app.add_typer(reviews_app, name="reviews")

DRY = typer.Option(False, "--dry-run", help="Offline: sample market, no Claude calls, separate database.")


@app.callback()
def main(verbose: bool = typer.Option(False, "-v", "--verbose")) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def _client(ctx, client_id: int) -> Client:
    client = ctx.session.get(Client, client_id)
    if client is None:
        typer.secho(f"No client with id {client_id}. See `fullhouse client list`.", fg="red")
        raise typer.Exit(1)
    return client


def _seed_if_dry(ctx, dry_run: bool) -> None:
    if dry_run:
        from fullhouse.sample import seed

        seed(ctx.session, ctx.today)


def _print_result(res) -> None:
    colour = "green" if res.ok else "yellow"
    typer.secho(f"[{res.agent}] {res.summary}", fg=colour)
    for err in res.errors[:10]:
        typer.secho(f"  ! {err}", fg="yellow")


# ------------------------------------------------------------------ core verbs


@app.command()
def audit(
    property_url: Optional[str] = typer.Argument(None, help="Booking.com property URL (live mode)"),
    html: Optional[Path] = typer.Option(None, "--html", help="Property page saved from your browser (offline mode)"),
    search_html: Optional[Path] = typer.Option(None, "--search-html",
                                               help="Saved Booking.com search results for the area: competitors"),
    competitor_html: Optional[list[Path]] = typer.Option(None, "--competitor-html",
                                                         help="Saved competitor property page; repeat for each"),
    checkin: Optional[str] = typer.Option(None, help="YYYY-MM-DD the pages were saved for (read from the page if omitted)"),
    url: Optional[str] = typer.Option(None, "--url", help="Property URL, if the saved page does not include it"),
    website: Optional[str] = typer.Option(None, help="Property website, for the direct-booking checks"),
    no_claude: bool = typer.Option(False, "--no-claude", help="Score with the built-in rubric only; no Claude calls"),
    dry_run: bool = DRY,
) -> None:
    """Free Revenue Audit: a 2-page PDF, from a live URL or from pages saved in your browser (--html)."""
    _audit(property_url, html=html, search_html=search_html, competitor_html=competitor_html or [],
           checkin=checkin, url=url, website=website, no_claude=no_claude, dry_run=dry_run)


def _audit(property_url, *, html=None, search_html=None, competitor_html=(), checkin=None, url=None, website=None,
           no_claude=False, dry_run=False) -> None:
    from fullhouse import audit as audit_mod
    from fullhouse.sample import SAMPLE_BOOKING_URL

    if html is None and property_url is None:
        typer.secho("Give a Booking.com URL, or --html with a property page saved from your browser.", fg="red")
        raise typer.Exit(2)
    if html is None and dry_run and not property_url.startswith(SAMPLE_BOOKING_URL):
        typer.secho(f"--dry-run uses the offline sample market; using {SAMPLE_BOOKING_URL}", fg="yellow")
        property_url, website = SAMPLE_BOOKING_URL, website or "https://coralsands.example/"
    with context(dry_run=dry_run) as ctx:
        if no_claude:
            ctx.claude.dry_run = True
        try:
            if html is not None:
                out = audit_mod.run_saved(
                    ctx, html, url=url or property_url, search_html=search_html, competitor_html=list(competitor_html),
                    checkin=date.fromisoformat(checkin) if checkin else None, website=website,
                )
            else:
                out = audit_mod.run(ctx, property_url, website=website)
        except (audit_mod.BlockedError, audit_mod.SavedPageError) as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(1)
    typer.secho(out.headline, bold=True)
    typer.echo(f"Competitors ({len(out.competitors)}): {', '.join(out.competitors) or 'none'}")
    for note in out.notes:
        typer.secho(f"Note: {note}", fg="yellow")
    typer.secho(f"PDF ({out.pages} pages): {out.pdf}", fg="green")


@app.command()
def run(
    agent: str = typer.Argument(..., help=f"One of: {', '.join(AGENTS)}"),
    client: int = typer.Option(..., "--client", help="Client id"),
    dry_run: bool = DRY,
    as_json: bool = typer.Option(False, "--json", help="Print the full result as JSON"),
) -> None:
    """Run one agent for one client."""
    if agent not in AGENTS:
        typer.secho(f"Unknown agent '{agent}'. Choose from: {', '.join(AGENTS)}", fg="red")
        raise typer.Exit(2)
    with context(dry_run=dry_run) as ctx:
        _seed_if_dry(ctx, dry_run)
        res = AGENTS[agent].run(ctx, _client(ctx, client))
    _print_result(res)
    if as_json:
        typer.echo(json.dumps(res.data, indent=2, default=str))


@app.command()
def weekly(
    dry_run: bool = DRY,
    client: Optional[int] = typer.Option(None, "--client", help="Only this client"),
) -> None:
    """Run every agent for every signed client (what the scheduler does each Monday)."""
    from fullhouse.scheduler import run_weekly

    results = run_weekly(dry_run=dry_run, client_id=client)
    if not results:
        typer.echo("No signed clients. Add one with `fullhouse client add`.")
    for client_id, items in results.items():
        typer.secho(f"Client {client_id}", bold=True)
        for res in items:
            _print_result(res)


@app.command()
def schedule(
    day: str = typer.Option("mon", help="Day of week (mon..sun)"),
    hour: int = typer.Option(6), minute: int = typer.Option(0), dry_run: bool = DRY,
) -> None:
    """Start the blocking weekly scheduler (FULLHOUSE_TIMEZONE, default Australia/Sydney)."""
    from fullhouse.scheduler import start

    start(day, hour, minute, dry_run=dry_run)


@app.command()
def demo() -> None:
    """5-minute offline demo: free audit, then the full weekly run, on the sample motel."""
    _audit("https://www.booking.com/hotel/au/coral-sands-motel.en-gb.html",
           website="https://coralsands.example/", dry_run=True)
    typer.echo("")
    weekly(dry_run=True, client=None)


# ------------------------------------------------------------------ data in / out


@app.command("import-metrics")
def import_metrics(csv_path: Path, client: int = typer.Option(..., "--client")) -> None:
    """Import weekly PMS metrics: week_start,revenue,rooms_sold,rooms_available[,ly_revenue]."""
    with context() as ctx:
        n = import_csv(ctx.session, _client(ctx, client).id, csv_path)
    typer.secho(f"Imported {n} weeks.", fg="green")


@app.command()
def prospect(
    area: str = typer.Argument(..., help="Where to search, e.g. 'Mooloolaba QLD'"),
    checkin: Optional[str] = typer.Option(None, help="YYYY-MM-DD; default 14 days from today"),
    nights: int = typer.Option(1),
    property_type: Optional[str] = typer.Option("motel", "--type",
                                                help="motel, hostel, inn, bnb, apartment, resort, boutique, or 'any'"),
    limit: int = typer.Option(20, help="Properties to analyse (each costs one SerpApi search for details)"),
    no_details: bool = typer.Option(False, "--no-details", help="Search results only: cheaper, no prices by site"),
    snapshots: int = typer.Option(10, help="One-page PDF snapshots for the top N"),
    max_class: int = typer.Option(3, help="Skip hotels rated above this many stars (0 = no limit)"),
    dry_run: bool = DRY,
) -> None:
    """Find and rank prospects in an area from Google Hotels (via SerpApi). No OTA is fetched.

    Google tags few independent motels as "motels"; for a fuller list use --type any with a query such as
    "motels in Mooloolaba QLD" (the 3-star cap keeps big hotels and resorts out).
    """
    from datetime import timedelta

    from fullhouse import prospect as prospect_mod
    from fullhouse.core.serpapi import PROPERTY_TYPES, SerpApiClient, SerpApiError
    from fullhouse.sample.serpapi import SampleHotels

    ptype = None if property_type in (None, "any") else property_type
    if ptype and ptype not in PROPERTY_TYPES:
        typer.secho(f"Unknown --type {property_type}; choose from {', '.join(PROPERTY_TYPES)} or any.", fg="red")
        raise typer.Exit(2)
    if not no_details:
        typer.echo(f"Up to {limit + 1 + limit // 20} SerpApi searches (cached results are free).")
    with context(dry_run=dry_run) as ctx:
        start = date.fromisoformat(checkin) if checkin else ctx.today + timedelta(days=14)
        source = SampleHotels() if dry_run else SerpApiClient(cache_dir=ctx.settings.cache_dir)
        try:
            result = prospect_mod.run(ctx, source, area, start, nights=nights, property_type=ptype, limit=limit,
                                      details=not no_details, snapshots=snapshots,
                                      max_class=max_class or None)
        except (SerpApiError, ValueError) as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(1)
    a = result.area
    if not result.reports:
        typer.echo(f"No properties found for '{area}'.")
        return
    typer.secho(f"{a.count} properties · area median {f'${a.median_price:.0f}/night' if a.median_price else 'price n/a'} · "
                f"{result.searches_used} SerpApi searches used", bold=True)
    for r in result.reports[:10]:
        typer.echo(f"{r.rank:>2}. [{r.opportunity:>3}] {r.hotel.name}: {r.headline}")
    typer.secho(f"Shortlist: {result.shortlist_pdf}\nCSV: {result.shortlist_csv}", fg="green")


@app.command("import-rates")
def import_rates(
    csv_path: Path,
    client: int = typer.Option(..., "--client"),
    map_: Optional[list[str]] = typer.Option(None, "--map", help="Rename a column: 'Their column=stay_date'. Repeat."),
    add_competitors: bool = typer.Option(False, "--add-competitors",
                                         help="Add properties the file names that are not competitors yet"),
    dry_run: bool = DRY,
) -> None:
    """Import competitor rates from a rate-shopper export (RoomPriceGenie, PriceLabs, Lighthouse, spreadsheet).

    Long layout: stay_date,property,price[,room_type,nights,snapshot_date,ota]. Wide layout: stay_date then
    one column per property. The rate agent uses an import from the last 7 days instead of fetching OTAs.
    """
    from fullhouse.core.rate_import import RateImportError
    from fullhouse.core.rate_import import import_rates as do_import

    column_map = {}
    for item in map_ or []:
        if "=" not in item:
            typer.secho(f"--map needs 'Column=field', got {item!r}", fg="red")
            raise typer.Exit(2)
        k, v = item.split("=", 1)
        column_map[k.strip()] = v.strip()
    with context(dry_run=dry_run) as ctx:
        _seed_if_dry(ctx, dry_run)
        try:
            r = do_import(ctx.session, _client(ctx, client), csv_path, today=ctx.today, column_map=column_map,
                          add_competitors=add_competitors)
        except RateImportError as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(1)
    typer.secho(f"Stored {r.stored} prices across {r.stay_dates} stay dates from {r.rows} rows.", fg="green")
    if r.added_competitors:
        typer.echo(f"Added competitors: {', '.join(r.added_competitors)}")
    if r.unmatched:
        names = ", ".join(f"{n} ({c})" for n, c in r.unmatched.items())
        typer.secho(f"Not matched to the client or a competitor, skipped: {names}. Re-run with --add-competitors "
                    "to add them, or rename them in the file.", fg="yellow")
    if r.skipped:
        typer.secho(f"{r.skipped} rows had no usable stay date (or a date before the extract) and were skipped.",
                    fg="yellow")


@app.command("import-reviews")
def import_reviews(json_path: Path, client: int = typer.Option(..., "--client")) -> None:
    """Import reviews from JSON: [{source, external_id, date, author, rating, text}]."""
    from fullhouse.agents.review_responder import import_reviews as do_import

    with context() as ctx:
        n = do_import(ctx, _client(ctx, client), json_path)
    typer.secho(f"Imported {n} new reviews.", fg="green")


@app.command()
def guarantee(client: int = typer.Option(..., "--client"), dry_run: bool = DRY) -> None:
    """Day-60 and day-90 guarantee check against the same period last year."""
    with context(dry_run=dry_run) as ctx:
        _seed_if_dry(ctx, dry_run)
        c = _client(ctx, client)
        for days in (60, 90):
            g = guarantee_mod.evaluate(ctx.session, c, days, today=ctx.today)
            if g.passed is None:
                typer.echo(f"Day {days} ({g.checkpoint}): {g.note}")
            else:
                verdict = "PASS" if g.passed else "FAIL - fees waived"
                typer.secho(
                    f"Day {days} ({g.checkpoint}): {verdict}. Revenue ${g.revenue:,.0f} vs last year adjusted "
                    f"${g.ly_revenue_adjusted:,.0f} ({g.delta:+,.0f}) over {g.weeks} weeks, capacity factor "
                    f"{g.capacity_factor}.", fg="green" if g.passed else "red")


@app.command()
def costs(month: Optional[str] = typer.Option(None, help="YYYY-MM, default this month"), dry_run: bool = DRY) -> None:
    """Claude spend per client for a month (USD), against the < $40/client target."""
    month = month or date.today().strftime("%Y-%m")
    with context(dry_run=dry_run) as ctx:
        rows = ctx.session.exec(
            select(ClaudeCall.client_id, func.count(), func.sum(ClaudeCall.cost_usd))
            .where(func.strftime("%Y-%m", ClaudeCall.created_at) == month)
            .group_by(ClaudeCall.client_id)
        ).all()
    if not rows:
        typer.echo(f"No Claude calls logged in {month}.")
    for client_id, calls, cost in rows:
        typer.echo(f"client {client_id}: {calls} calls, ${cost or 0:.2f} USD")


# ------------------------------------------------------------------ clients


@client_app.command("add")
def client_add(
    name: str = typer.Option(...),
    property_type: PropertyType = typer.Option(PropertyType.motel, "--type"),
    region: str = typer.Option("", help="e.g. 'Mooloolaba, Queensland'"),
    rooms: int = typer.Option(0, help="Room count (beds for hostels are still counted by room)"),
    booking_url: Optional[str] = typer.Option(None), hostelworld_url: Optional[str] = typer.Option(None),
    expedia_url: Optional[str] = typer.Option(None),
    direct_url: Optional[str] = typer.Option(None, help="Booking-engine URL with {checkin} and {checkout}"),
    website: Optional[str] = typer.Option(None), gbp: Optional[str] = typer.Option(None, help="accounts/x/locations/y"),
    place_id: Optional[str] = typer.Option(None, help="Google place id"),
    pms: Optional[str] = typer.Option(None),
    start_date: Optional[str] = typer.Option(None, help="YYYY-MM-DD service start"),
    voice: Optional[str] = typer.Option(None, help="Tone notes for review replies"),
) -> None:
    """Add a signed client."""
    urls = {k: v for k, v in {"booking": booking_url, "hostelworld": hostelworld_url, "expedia": expedia_url,
                              "direct": direct_url}.items() if v}
    with context() as ctx:
        c = Client(name=name, property_type=property_type, region=region, room_count=rooms, ota_urls=urls,
                   website_url=website, gbp_url=gbp, gbp_place_id=place_id, pms_type=pms,
                   start_date=date.fromisoformat(start_date) if start_date else None, voice_profile=voice)
        ctx.session.add(c)
        ctx.session.commit()
        typer.secho(f"Client {c.id}: {c.name}", fg="green")


@client_app.command("list")
def client_list(dry_run: bool = DRY) -> None:
    with context(dry_run=dry_run) as ctx:
        for c in ctx.session.exec(select(Client).order_by(Client.id)):
            n = ctx.session.exec(select(func.count()).where(Competitor.client_id == c.id)).one()
            tag = " (prospect)" if c.is_prospect else ""
            typer.echo(f"{c.id:>3}  {c.name}{tag} · {c.property_type.value} · {c.region} · {n} competitors")


@client_app.command("sign")
def client_sign(client: int = typer.Option(..., "--client"), start_date: str = typer.Option(...),
                rooms: int = typer.Option(...)) -> None:
    """Convert an audited prospect into a signed client."""
    with context() as ctx:
        c = _client(ctx, client)
        c.is_prospect, c.start_date, c.room_count = False, date.fromisoformat(start_date), rooms
        ctx.session.add(c)
        ctx.session.commit()
    typer.secho(f"{c.name} signed from {start_date}.", fg="green")


@client_app.command("add-competitor")
def add_competitor(client: int = typer.Option(..., "--client"), name: str = typer.Option(...),
                   booking_url: Optional[str] = typer.Option(None),
                   hostelworld_url: Optional[str] = typer.Option(None),
                   expedia_url: Optional[str] = typer.Option(None)) -> None:
    urls = {k: v for k, v in {"booking": booking_url, "hostelworld": hostelworld_url,
                              "expedia": expedia_url}.items() if v}
    with context() as ctx:
        _client(ctx, client)
        ctx.session.add(Competitor(client_id=client, name=name, ota_urls=urls))
        ctx.session.commit()
    typer.secho(f"Added competitor {name}.", fg="green")


# ------------------------------------------------------------------ reviews


@reviews_app.command("pending")
def reviews_pending(client: int = typer.Option(..., "--client"), dry_run: bool = DRY) -> None:
    """Drafted replies waiting for approval."""
    with context(dry_run=dry_run) as ctx:
        rows = ctx.session.exec(
            select(Review).where(Review.client_id == client, Review.approved == False,  # noqa: E712
                                 Review.draft_reply.is_not(None))
        ).all()
        for r in rows:
            typer.secho(f"#{r.id} {r.source} {r.rating} — {r.author or 'guest'}", bold=True)
            typer.echo(f"  Review: {r.text}")
            typer.echo(f"  Draft:  {r.draft_reply}\n")
        if not rows:
            typer.echo("Nothing waiting.")


@reviews_app.command("approve")
def reviews_approve(review_id: int, reply: Optional[str] = typer.Option(None, help="Edited reply text")) -> None:
    """Approve a draft (optionally edited). Posting to the platform is done by hand for now."""
    with context() as ctx:
        r = ctx.session.get(Review, review_id)
        if r is None:
            typer.secho("No such review.", fg="red")
            raise typer.Exit(1)
        r.draft_reply, r.approved = reply or r.draft_reply, True
        ctx.session.add(r)
        ctx.session.commit()
    typer.secho(f"Approved #{review_id}. Paste the reply on {r.source}.", fg="green")


if __name__ == "__main__":
    app()
