"""Audited, shared listing corrections; never edit provider evidence or quotes."""

from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

import market_data as md


class ManualSplitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID  # Client-generated: an uncertain POST can be retried without duplication.
    symbol: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9^][A-Z0-9._^=\-]*$")
    exchange: str = Field(pattern=r"^[A-Z0-9]{4}$")
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    factor: str = Field(max_length=32, pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
    note: str = Field(default="", max_length=500)

    @field_validator("date")
    @classmethod
    def effective_date(cls, value):
        day = date.fromisoformat(value)
        if day > md.utcnow().date():
            raise ValueError("The effective date cannot be in the future")
        return day.isoformat()

    @field_validator("factor")
    @classmethod
    def split_factor(cls, value):
        factor = Decimal(value)
        if not Decimal("0.000000001") <= factor <= Decimal("1000000000") or factor == 1:
            raise ValueError("Use new shares per old share, between 0.000000001 and 1000000000, excluding 1")
        # Avoid Decimal.normalize(): its rounding depends on the ambient context.
        return value.rstrip("0").rstrip(".") if "." in value else value

    @field_validator("note")
    @classmethod
    def trim_note(cls, value):
        return value.strip()


def ensure_indexes(db):
    db.manual_splits.create_index(
        [(field, ASCENDING) for field in ("instrument_id", "symbol", "exchange", "date")],
        unique=True, partialFilterExpression={"active": True}, name="one_manual_split_per_date",
    )


def manual_events(db, isin, listing, cutoff):
    if not listing:
        return []
    return list(db.manual_splits.find({
        **md.listing_filter(isin, listing), "created_at": {"$lte": cutoff},
        "$or": [{"removed_at": {"$exists": False}}, {"removed_at": {"$gt": cutoff}}],
    }).sort([("date", ASCENDING), ("_id", ASCENDING)]))


def public_event(record):
    return md.public({"id": record["_id"], **{field: record[field] for field in ("date", "factor", "note", "created_at")}})


def read_splits(db, isin, as_of=None):
    from split_adjustments import load_provider_split_snapshot, load_split_snapshot

    md.require_catalogue(db, isin)
    cutoff = md.aware(as_of) if as_of is not None else md.utcnow()
    listing = (db.instrument_state.find_one({"_id": isin}) or {}).get("listing")
    from market_data_retention import reader
    with reader(db, isin, listing):
        provider = load_provider_split_snapshot(db, isin, listing, cutoff)
        merged = load_split_snapshot(db, isin, listing, cutoff)
        return md.public({
            "instrument_id": isin, "listing": listing,
            "supported": False,
            "manual": [public_event(event) for event in manual_events(db, isin, listing, cutoff)],
            "provider": [event for event in (provider or {}).get("events", []) if event["date"] <= cutoff.date().isoformat()],
            "error": (merged or {}).get("error"),
        })


def add_split(db, isin, body, user_id):
    from split_adjustments import load_provider_split_snapshot

    catalogue = md.require_catalogue(db, isin)
    listing = {"symbol": body.symbol, "exchange": body.exchange}
    state = db.instrument_state.find_one({"_id": isin}) or {}
    if state.get("listing") != listing or not db.quotes.find_one(md.listing_filter(isin, listing), {"_id": 1}):
        raise md.DataError(409, "Save quotes for this listing before adding a manual split. Refresh instrument details.")
    publication = md.published_history(db, isin, listing)
    if publication and publication["provider"] == "yfinance":
        raise md.DataError(409, "Yahoo prices are already split-adjusted. Manual split multiplication is not supported for this price basis.")
    inception = md.quote_date(catalogue.get("inception_date"))
    if inception and body.date < inception:
        raise md.DataError(422, "The split date cannot be before the ETF inception date")
    payload = {**md.listing_filter(isin, listing), "date": body.date, "factor": body.factor, "note": body.note}
    existing = db.manual_splits.find_one({"_id": str(body.id)})
    if existing:
        if existing.get("active") and all(existing.get(key) == value for key, value in payload.items()):
            return {"item": public_event(existing)}
        raise md.DataError(409, "This split request was already used or removed. Refresh splits before adding a new entry.")
    now = md.utcnow()
    # BSON stores milliseconds; first and retried responses must agree exactly.
    now = now.replace(microsecond=(now.microsecond // 1000) * 1000)
    provider = load_provider_split_snapshot(db, isin, listing, now)
    for event in (provider or {}).get("events", []):
        if event["date"] == body.date and Decimal(event["factor"]) != Decimal(body.factor):
            raise md.DataError(409, "A provider split with a different factor already exists on this date")
    record = {"_id": str(body.id), **payload, "active": True, "created_at": now, "created_by": user_id}
    try:
        db.manual_splits.insert_one(record)
    except DuplicateKeyError:
        existing = db.manual_splits.find_one({"_id": str(body.id)})
        if existing and existing.get("active") and all(existing.get(key) == value for key, value in payload.items()):
            return {"item": public_event(existing)}
        raise md.DataError(409, "A manual split already exists on this date. Refresh splits; remove it first to correct it.") from None
    return {"item": public_event(record)}


def remove_split(db, isin, split_id, user_id):
    md.require_catalogue(db, isin)
    # An immutable ID prevents a delayed retry from removing a replacement entry.
    record = db.manual_splits.find_one({"_id": str(split_id), "instrument_id": isin})
    if record is None:
        raise md.DataError(404, "Manual split not found")
    db.manual_splits.update_one(
        {"_id": record["_id"], "active": True},
        {"$set": {"active": False, "removed_at": md.utcnow(), "removed_by": user_id}},
    )
