# Full House

AI revenue-operations agents for independent motels and hostels in Australia. Six agents do the
"glue layer" that pricing software leaves undone (OTA listings, reviews, Google profile,
direct-booking funnel and a weekly plain-English report), and a free-audit flow turns one
Booking.com URL into a 2-page sales PDF.

## 5-minute demo (offline, no API key)

```bash
uv sync
uv run fullhouse demo
```

The demo runs against a fictional market of 12 Sunshine Coast properties
(`fullhouse/sample/market.json`). It writes to `.fullhouse/dry-run/`:

- `output/client-1/free-audit-<date>.pdf`: the 2-page Free Revenue Audit
- `output/client-1/monday-report-<week>.pdf`: the Monday report
- `outbox/*.eml`: the Monday email with the PDF attached (sending is stubbed)

Then try `uv run fullhouse reviews pending --client 1 --dry-run` and
`uv run fullhouse guarantee --client 1 --dry-run`.

## Setup for live runs

```bash
uv sync
uv run playwright install chromium     # or set FULLHOUSE_CHROMIUM_PATH
cp .env.example .env                    # add ANTHROPIC_API_KEY
```

## Free audit from saved pages (recommended)

Booking.com usually turns away automated clients, so the reliable way to run a free audit is from
pages you save in your own browser. Nothing is fetched from Booking.com.

1. Open the property on Booking.com with dates selected (say two weeks out, one night, two adults) and
   wait for the room table to load. Save it: Chrome **File → Save Page As → Webpage, Complete**.
2. Optional, for competitors: search the town for the same dates, scroll so the list loads, and save
   that page too. The first 10 other properties become the competitor set, compared on their cheapest room.
3. Optional, for room-level comparison: save a few competitors' property pages for the same dates.

```bash
uv run fullhouse audit --html listing.html --search-html search.html \
    --competitor-html rival1.html --competitor-html rival2.html \
    --website https://their-site.com.au
```

The property URL and dates are read from the saved page (`--url` and `--checkin` override them). Pages
saved for different dates are flagged in the PDF. Add `--no-claude` to score with the built-in rubric
only. Only `--website` is fetched live.

## Commands

| Command | What it does |
| --- | --- |
| `fullhouse audit --html saved.html [--search-html ...] [--competitor-html ...]` | Free audit from pages saved in your browser (see above). |
| `fullhouse audit <booking_url> [--website URL]` | Free audit: parses the listing, auto-picks 10 competitors from the same Booking.com search, runs rate_scraper + listing_auditor + funnel_checker, renders a 2-page PDF. Stores the property as a prospect. |
| `fullhouse run <agent> --client <id>` | Runs one agent for one client. |
| `fullhouse weekly` | Runs all six agents for every signed client, report last. |
| `fullhouse schedule [--day mon --hour 6]` | Blocking scheduler that runs `weekly` every week (timezone `FULLHOUSE_TIMEZONE`). |
| `fullhouse client add / list / sign / add-competitor` | Client and competitor records. |
| `fullhouse import-metrics file.csv --client <id>` | Weekly PMS export: `week_start,revenue,rooms_sold,rooms_available[,ly_revenue]`. |
| `fullhouse import-reviews file.json --client <id>` | Reviews from any source (see `fullhouse/sample/reviews.json`). |
| `fullhouse reviews pending / approve` | Approve drafted replies. Nothing is ever posted automatically. |
| `fullhouse guarantee --client <id>` | Day-60 and day-90 guarantee check. |
| `fullhouse costs [--month YYYY-MM]` | Claude spend per client from the cost log. |

Every command that runs agents takes `--dry-run`: offline sample market, deterministic stubs instead
of Claude, and a separate database, so a dry run never touches real client data.

## The agents

| Agent | Input | Output |
| --- | --- | --- |
| `rate_scraper` | Client + competitor OTA URLs; optional direct booking-engine URL | `RateSnapshot` rows for check-in 1/7/14/30 days out; position table (client vs market median per room category) |
| `listing_auditor` | Client OTA listing pages | Score 0-100 over 8 criteria; ranked fix list with effort. A deterministic rubric scores first; Claude reviews it and writes the final scores and fixes |
| `review_responder` | Google Business Profile API, or imported JSON | Draft replies in the client's voice in `Review.draft_reply`; never posted without approval |
| `gbp_monitor` | Places API and/or Business Profile API | Gaps in hours, photos, posts, Q&A, category, as `Finding` rows. Anything no API can supply is listed as "check manually" |
| `funnel_checker` | Client website + today's rate snapshots | Direct vs OTA price on 3 sample dates against the agreed buffer, load time, booking engine, title/meta/H1, schema.org markup |
| `monday_report` | `WeeklyMetrics` + everything above | WoW and YoY deltas, ~250-word narrative, exactly three `Action` rows, PDF, queued email |

## Layout

```
fullhouse/
  core/        config, models, db, Claude wrapper, fetcher, OTA parsers, metrics, guarantee
  agents/      the six agents + shared context
  reports/     Jinja2 templates + WeasyPrint renderer
  sample/      fictional market, page templates and sample data for dry runs
  audit.py     free audit flow
  cli.py       Typer CLI
  scheduler.py APScheduler weekly runner
tests/         pytest (offline; parser fixtures, guarantee maths, agents, end to end)
```

## Claude usage

All calls go through `core/claude.py`: one wrapper with retries (backoff, honours `retry-after`),
refusal handling, structured JSON output validated against Pydantic models, and a cost log
(`ClaudeCall` table, USD). Defaults: `claude-opus-5-5`, effort `medium`, server-side refusal
fallback on. Change the model or effort in `.env`. Each agent makes one small call per listing,
review or report, so monthly spend per client should sit far below the $40 target; check real
figures with `fullhouse costs`.

## Guarantee method (`core/guarantee.py`)

Full weeks between `start_date` and day 60 (or 90) are compared with the same weeks 52 weeks earlier.
Last year's revenue is scaled by this year's available room-nights over last year's, so added rooms
raise the bar and closure days (`Client.closure_days`) lower it. Pass = this year's revenue is above
the adjusted figure. Properties need 12 months of history before the start date. Record
`rooms_available` as full capacity and closures as closure days, so closures are not counted twice.

## Limits to know before going live

- **OTA terms.** Booking.com, HostelWorld and Expedia restrict automated access in their terms, and
  the fetcher skips any page their robots.txt disallows. Run audits at low volume, and confirm the
  legal position (or use a licensed rate-data provider) before scaling.
- **Selectors drift.** The OTA parsers target current markup with fallbacks, and are tested
  against fixtures, not live pages. When a field comes back empty, save the live page into
  `tests/fixtures/` and update the selectors.
- **Direct rates** need the client's booking-engine URL with `{checkin}` and `{checkout}`
  placeholders (`--direct-url`). The generic parser reads engines that mark rooms with a `room`
  class or `data-room-name`; others need a dedicated parser.
- **Google.** Places API returns at most 10 photos and no posts or Q&A; posts and media dates need
  the Business Profile API (OAuth). Q&A has no API source here and is reported as "check manually".
- **Email** is written to the outbox as `.eml`; wire up SMTP or a provider when ready.
- **PMS/channel-manager APIs** (SiteMinder, RoomPriceGenie) are not integrated yet; metrics come
  in by CSV.

## Tests

```bash
uv run pytest
```
