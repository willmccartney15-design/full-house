"""Onboarding checklist: which data each client has connected, and how to connect the rest."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlmodel import func, select

from fullhouse.core.config import Settings
from fullhouse.core.models import Client, Competitor, RateSnapshot, Review, WeeklyMetrics


@dataclass
class Check:
    item: str
    done: bool
    detail: str
    how: str
    needed_for: str


def checklist(session, settings: Settings, client: Client, today: date) -> list[Check]:
    urls = client.ota_urls or {}
    comps = session.exec(select(func.count()).where(Competitor.client_id == client.id)).one()
    weeks = session.exec(select(WeeklyMetrics.week_start).where(WeeklyMetrics.client_id == client.id)
                         .order_by(WeeklyMetrics.week_start)).all()
    last_import = session.exec(select(func.max(RateSnapshot.date)).where(
        RateSnapshot.client_id == client.id, RateSnapshot.source == "import")).one()
    reviews = session.exec(select(func.count()).where(Review.client_id == client.id)).one()
    history_ok = bool(weeks) and client.start_date is not None and weeks[0] <= client.start_date - timedelta(days=358)
    fresh = bool(weeks) and weeks[-1] >= today - timedelta(days=14)
    gbp = (client.gbp_url or "").startswith("accounts/") and bool(settings.gbp_access_token)
    places = bool(client.gbp_place_id) and bool(settings.google_places_api_key)
    return [
        Check("Owner contact email", bool(client.contact_email), client.contact_email or "missing",
              "fullhouse client contact --client ID --email ... --name ...", "Monday report, approval emails"),
        Check("Service start date", client.start_date is not None, str(client.start_date or "missing"),
              "fullhouse client sign --client ID --start-date YYYY-MM-DD --rooms N", "guarantee"),
        Check("OTA listing", any(urls.get(o) for o in ("booking", "hostelworld", "expedia")),
              ", ".join(k for k in urls if k != "direct") or "none",
              "fullhouse client add ... --booking-url", "listing audit"),
        Check("Website", bool(client.website_url), client.website_url or "missing", "--website on client add",
              "direct-booking checker"),
        Check("Booking-engine link", bool(urls.get("direct")), urls.get("direct") or "missing",
              "--direct-url 'https://...?checkin={checkin}&checkout={checkout}'", "direct vs OTA price"),
        Check("Competitor set", comps >= 5, f"{comps} competitors", "fullhouse client add-competitor ...",
              "rate position"),
        Check("Competitor rates (PriceLabs export)", last_import is not None and last_import >= today - timedelta(
            days=7), f"last import {last_import}" if last_import else "never imported",
              "Export comp-set rates from PriceLabs weekly; fullhouse import-rates FILE --client ID", "rate position"),
        Check("Weekly PMS figures, current", fresh, f"latest week {weeks[-1]}" if weeks else "none",
              "Export weekly revenue/rooms from the PMS; fullhouse import-metrics FILE --client ID", "Monday report"),
        Check("12 months of PMS history", history_ok, f"{len(weeks)} weeks on file",
              "Import the last 12+ months once at onboarding", "guarantee eligibility"),
        Check("Google Business Profile access", gbp or places,
              "Business Profile API" if gbp else ("Places API (read-only)" if places else "not connected"),
              "Owner adds us as a manager; set GBP_ACCESS_TOKEN and --gbp accounts/X/locations/Y",
              "Google profile monitor, posting review replies"),
        Check("Reviews flowing", reviews > 0, f"{reviews} reviews on file",
              "Connect Google (above) or fullhouse import-reviews FILE --client ID", "review responder"),
        Check("Owner's voice for replies", bool(client.voice_profile), "set" if client.voice_profile else "default",
              "--voice 'Friendly, signs off as Sam' on client add", "review responder"),
    ]
