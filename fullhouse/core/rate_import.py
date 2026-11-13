"""Competitor-rate import from a rate-shopper export (RoomPriceGenie, PriceLabs, Lighthouse, a spreadsheet).

Two layouts are read:

Long: one row per price.
    stay_date,property,price[,room_type][,nights][,snapshot_date][,ota]
Wide: one row per stay date, one column per property (how rate shoppers usually export).
    stay_date,<Your property>,<Competitor A>,<Competitor B>,...

Column names are matched case-insensitively with common aliases ("date", "arrival", "hotel", "rate"...);
``column_map`` renames anything else, e.g. {"Check-in": "stay_date"}. Prices are per night unless a
``nights`` column says the price covers a longer stay. Property names are matched to the client and its
competitors ignoring case, punctuation and words like "hotel"/"motel"; names that match nobody are either
added as competitors (``add_competitors``) or reported back.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlmodel import Session, select

from fullhouse.core.models import Client, Competitor, RateSnapshot
from fullhouse.core.rooms import normalise_room
from fullhouse.core.scrapers.common import parse_price

LOWEST = "lowest"
SOURCE = "import"

ALIASES = {
    "stay_date": ("stay_date", "date", "stay date", "arrival", "arrival_date", "check-in", "checkin", "check_in",
                  "night", "day"),
    "property": ("property", "hotel", "property_name", "hotel_name", "competitor", "name"),
    "price": ("price", "rate", "amount", "lowest_rate", "lowest rate", "best_rate", "price_aud"),
    "room_type": ("room_type", "room", "room type", "room_name"),
    "nights": ("nights", "los", "length_of_stay"),
    "snapshot_date": ("snapshot_date", "extract_date", "shop_date", "captured", "updated", "as_of"),
    "ota": ("ota", "channel", "source"),
}
_SELF = {"self", "you", "my property", "your property", "our property"}
_NOISE = r"\b(the|hotel|motel|motor inn|inn|hostel|apartments?|suites?|resort|lodge|backpackers)\b"


class RateImportError(ValueError):
    pass


@dataclass
class ImportResult:
    rows: int = 0
    stored: int = 0
    stay_dates: int = 0
    added_competitors: list[str] = field(default_factory=list)
    unmatched: dict[str, int] = field(default_factory=dict)  # name -> rows skipped
    skipped: int = 0  # rows without a usable date or price


def _key(name: str) -> str:
    text = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    return re.sub(r"\s+", " ", re.sub(_NOISE, " ", text)).strip()


def _parse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%a %d %b %Y",
                "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _price(raw: str) -> float | None:
    raw = (raw or "").strip()
    if not raw or raw.lower() in {"sold out", "n/a", "na", "-", "–", "closed"}:
        return None
    try:
        return float(raw.replace(",", "").replace("$", "").replace("AUD", "").strip())
    except ValueError:
        return parse_price(raw)


def _canonical_headers(headers: list[str], column_map: dict[str, str]) -> dict[str, str]:
    """original header -> canonical field (only for recognised fields)."""
    renamed = {h: column_map.get(h, h) for h in headers}
    out = {}
    for original, name in renamed.items():
        low = name.strip().lower()
        for canonical, aliases in ALIASES.items():
            if low == canonical or low in aliases:
                out.setdefault(original, canonical)
                break
    return out


class _Matcher:
    def __init__(self, session: Session, client: Client, add_competitors: bool, result: ImportResult):
        self.session, self.client, self.add, self.result = session, client, add_competitors, result
        self.by_key: dict[str, int | None] = {_key(client.name): None}
        for c in session.exec(select(Competitor).where(Competitor.client_id == client.id)):
            self.by_key.setdefault(_key(c.name), c.id)

    def resolve(self, name: str) -> tuple[bool, int | None]:
        """(matched, competitor_id); competitor_id None means the client itself."""
        name = name.strip()
        if name.lower() in _SELF:
            return True, None
        key = _key(name)
        if key in self.by_key:
            return True, self.by_key[key]
        for known, cid in self.by_key.items():  # "Seabreeze" vs "Seabreeze Motel Mooloolaba"
            if known and key and (known in key or key in known) and min(len(known), len(key)) >= 5:
                self.by_key[key] = cid
                return True, cid
        if self.add:
            comp = Competitor(client_id=self.client.id, name=name, ota_urls={})
            self.session.add(comp)
            self.session.flush()
            self.by_key[key] = comp.id
            self.result.added_competitors.append(name)
            return True, comp.id
        self.result.unmatched[name] = self.result.unmatched.get(name, 0) + 1
        return False, None


def import_rates(
    session: Session,
    client: Client,
    path: Path,
    *,
    today: date,
    column_map: dict[str, str] | None = None,
    add_competitors: bool = False,
    ota: str = "booking",
) -> ImportResult:
    column_map = column_map or {}
    result = ImportResult()
    try:
        fh = open(path, newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise RateImportError(f"Cannot read {path}: {exc.strerror}") from exc
    with fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(fh, dialect=dialect)
        headers = reader.fieldnames or []
        fields = _canonical_headers(headers, column_map)
        col = {v: k for k, v in fields.items()}  # canonical -> original
        if "stay_date" not in col:
            raise RateImportError(f"{path}: no stay-date column. Found {headers}; map one with "
                                  "--map 'Your column=stay_date'.")
        wide = "price" not in col
        if not wide and "property" not in col:
            raise RateImportError(f"{path}: has a price column but no property column; map one with "
                                  "--map 'Your column=property'.")
        property_cols = [h for h in headers if h not in fields] if wide else []
        if wide and not property_cols:
            raise RateImportError(f"{path}: no price column and no property columns found.")

        matcher = _Matcher(session, client, add_competitors, result)
        snapshot_dates: set[date] = set()
        stays: set[date] = set()
        snaps: list[RateSnapshot] = []
        rows = list(reader)
        result.rows = len(rows)
        if wide:  # resolve each column once, not once per row
            resolved = {h: matcher.resolve(h) for h in property_cols}
        for row in rows:
            stay = _parse_date(row.get(col["stay_date"], ""))
            snap_day = _parse_date(row.get(col["snapshot_date"], "")) if "snapshot_date" in col else None
            snap_day = snap_day or today
            nights = 1
            if "nights" in col:
                try:
                    nights = max(int(float(row.get(col["nights"]) or 1)), 1)
                except ValueError:
                    nights = 1
            if stay is None or stay < snap_day:
                result.skipped += 1
                continue
            cells = (
                [(h, row.get(h, "")) for h in property_cols] if wide
                else [(row.get(col["property"], ""), row.get(col["price"], ""))]
            )
            for name, raw_price in cells:
                price = _price(raw_price)
                if price is None:
                    continue
                matched, cid = resolved[name] if wide else matcher.resolve(name)
                if not matched:
                    continue
                room = (row.get(col["room_type"], "") if "room_type" in col else "").strip()
                snaps.append(RateSnapshot(
                    client_id=client.id, competitor_id=cid, date=snap_day,
                    ota=(row.get(col["ota"], "") if "ota" in col else "").strip().lower() or ota,
                    room_type=normalise_room(room) if room else LOWEST, room_name=room or "Cheapest room",
                    lead_days=(stay - snap_day).days, price=round(price / nights, 2), source=SOURCE,
                ))
                snapshot_dates.add(snap_day)
                stays.add(stay)

    # Re-importing the same extract replaces it.
    for day in snapshot_dates:
        for old in session.exec(select(RateSnapshot).where(
            RateSnapshot.client_id == client.id, RateSnapshot.date == day, RateSnapshot.source == SOURCE
        )):
            session.delete(old)
    session.add_all(snaps)
    session.commit()
    result.stored, result.stay_dates = len(snaps), len(stays)
    return result
