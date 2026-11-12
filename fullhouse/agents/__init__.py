"""Agent registry: name -> module with run(ctx, client)."""

from fullhouse.agents import (
    funnel_checker,
    gbp_monitor,
    listing_auditor,
    monday_report,
    rate_scraper,
    review_responder,
)

AGENTS = {
    m.NAME: m
    for m in (rate_scraper, listing_auditor, review_responder, gbp_monitor, funnel_checker, monday_report)
}

# Order for the weekly run: data-collecting agents first, the report last.
WEEKLY_ORDER = ["rate_scraper", "listing_auditor", "review_responder", "gbp_monitor", "funnel_checker", "monday_report"]
