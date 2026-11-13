"""SQLModel tables. Money is stored in the client's currency (AUD by default), ex-GST."""

from __future__ import annotations

import secrets
from datetime import date, datetime, timezone
from enum import StrEnum

from sqlmodel import JSON, Column, Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_token() -> str:
    """Unguessable token for links in emails (approvals, owner page)."""
    return secrets.token_urlsafe(24)


class PropertyType(StrEnum):
    motel = "motel"
    hostel = "hostel"


class ActionStatus(StrEnum):
    open = "open"
    done = "done"
    skipped = "skipped"


class Client(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    property_type: PropertyType = PropertyType.motel
    region: str = ""
    room_count: int = 0
    ota_urls: dict = Field(default_factory=dict, sa_column=Column(JSON))  # {"booking": url, "hostelworld": url, ...}
    gbp_url: str | None = None
    gbp_place_id: str | None = None
    website_url: str | None = None
    pms_type: str | None = None
    start_date: date | None = None
    voice_profile: str | None = None  # tone notes used by review_responder
    closure_days: list = Field(default_factory=list, sa_column=Column(JSON))  # ISO dates the property was closed
    is_prospect: bool = False  # created by a free audit, not yet signed
    contact_name: str | None = None  # the owner or manager we deal with
    contact_email: str | None = None  # where reports and approval emails go
    phone: str | None = None
    owner_token: str = Field(default_factory=new_token, index=True)  # the owner's private approvals page
    created_at: datetime = Field(default_factory=utcnow)


class Competitor(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    client_id: int = Field(foreign_key="client.id", index=True)
    name: str
    ota_urls: dict = Field(default_factory=dict, sa_column=Column(JSON))


class RateSnapshot(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    client_id: int = Field(foreign_key="client.id", index=True)
    competitor_id: int | None = Field(default=None, foreign_key="competitor.id", index=True)  # None = the client
    date: date  # date the snapshot was taken
    ota: str = "booking"
    room_type: str  # normalised category, see core.rooms
    room_name: str = ""  # raw name as listed
    lead_days: int
    price: float  # per night
    source: str = "scrape"  # scrape | import (a rate-shopper export, e.g. RoomPriceGenie or Lighthouse)


class ListingAudit(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    client_id: int = Field(foreign_key="client.id", index=True)
    date: date
    ota: str
    score: int
    findings: dict = Field(default_factory=dict, sa_column=Column(JSON))


class Review(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    client_id: int = Field(foreign_key="client.id", index=True)
    source: str  # google | booking | hostelworld | expedia
    external_id: str | None = Field(default=None, index=True)
    date: date
    author: str | None = None
    rating: float | None = None
    text: str = ""
    draft_reply: str | None = None
    approved: bool = False
    posted: bool = False


class WeeklyMetrics(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    client_id: int = Field(foreign_key="client.id", index=True)
    week_start: date = Field(index=True)  # Monday
    revenue: float
    rooms_sold: int
    rooms_available: int
    adr: float = 0.0
    revpar: float = 0.0
    ly_revenue: float | None = None


class Action(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    client_id: int = Field(foreign_key="client.id", index=True)
    week_start: date
    text: str
    status: ActionStatus = ActionStatus.open


class Finding(SQLModel, table=True):
    """Generic agent output (GBP gaps, funnel issues) so every agent leaves a record."""

    id: int | None = Field(default=None, primary_key=True)
    client_id: int = Field(foreign_key="client.id", index=True)
    agent: str = Field(index=True)
    date: date
    severity: str = "medium"  # high | medium | low
    text: str
    data: dict = Field(default_factory=dict, sa_column=Column(JSON))


class ClaudeCall(SQLModel, table=True):
    """Cost log: one row per Claude API call (USD)."""

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=utcnow)
    client_id: int | None = Field(default=None, index=True)
    agent: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    attempts: int = 1
    ok: bool = True
    error: str | None = None


class ProposalKind(StrEnum):
    review_reply = "review_reply"  # post this drafted reply
    action = "action"  # one of the Monday report's three actions
    fix = "fix"  # a high-priority finding (website, Google profile, rate parity)
    listing_fix = "listing_fix"  # a ranked fix from the OTA listing audit


class ProposalStatus(StrEnum):
    pending = "pending"  # waiting for the owner
    approved = "approved"  # owner said yes; on the to-do list
    skipped = "skipped"  # owner said no
    done = "done"  # carried out
    expired = "expired"  # not answered in time


class Proposal(SQLModel, table=True):
    """Something the agents want to do, waiting for the owner's one-tap approval."""

    id: int | None = Field(default=None, primary_key=True)
    client_id: int = Field(foreign_key="client.id", index=True)
    kind: ProposalKind
    ref: str = Field(index=True)  # what it is about, e.g. "review:12"; one proposal per ref per client
    title: str
    detail: str = ""
    why: str = ""
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON))
    priority: int = 50  # higher first
    status: ProposalStatus = ProposalStatus.pending
    token: str = Field(default_factory=new_token, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    sent_at: datetime | None = None
    decided_at: datetime | None = None
    decided_note: str | None = None
    done_at: datetime | None = None


class Suppression(SQLModel, table=True):
    """Addresses that asked not to be emailed again (Spam Act unsubscribe)."""

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    reason: str = "unsubscribed"
    created_at: datetime = Field(default_factory=utcnow)
