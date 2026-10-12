"""Offline index catalogue; Yahoo identity is verified only by a published download.

An index is not an ETF and has neither an ISIN nor an inferred inception date.
The internal YAHOO listing namespace is not an exchange/MIC assertion.
"""
import re
from uuid import UUID, uuid4, uuid5

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from market_data import (DataError, expire_stalled_jobs, history_generation,
                         observation_filter, public, public_job, quote_dates, utcnow)

HISTORY_START = "1900-01-01"
SEED_NAMESPACE = UUID("da8a221b-8295-47f3-8293-cc89019dd179")
# Labels are curated conveniences, not verified financial metadata. Return variants
# must not be inferred from familiar display names.
SEEDS = (("^GSPC", "S&P 500"),
         ("^990100-USD-STRD", "MSCI WORLD"),
         ("DE000SLA1W63.SG", "Solactive European Overnight Money Market Index"),
         ("DE000SL0H423.SG", "Solactive \u20acSTR Daily Total Return Index"))


def search_indices(settings, query, limit=15):
    """Search Yahoo candidates only; never acquire history or infer return semantics."""
    from market_data import provider_client
    from market_provider import MarketDataError

    query = query.strip()
    if len(query) < 2:
        raise DataError(422, "Enter at least two characters to search indices.")
    try:
        with provider_client(settings, "yfinance", lambda: None) as client:
            rows = client.tickerslist_page(query)["data"]
    except MarketDataError:
        raise DataError(503, "Index search is unavailable. Please retry or use manual entry.") from None
    results, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get("quoteType") != "INDEX":
            continue
        symbol = row.get("symbol")
        name = row.get("longname") or row.get("shortname")
        if (not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9^][A-Z0-9._^=\-]{0,63}", symbol)
                or not isinstance(name, str) or not name.strip() or len(name.strip()) > 200 or symbol in seen):
            continue
        currency = row.get("currency")
        results.append({"symbol": symbol, "name": name.strip(),
                        "currency": currency if isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency) else None,
                        "return_variant": "unknown"})
        seen.add(symbol)
        if len(results) >= limit:
            break
    return results


def ensure_catalogue(db):
    db.indices.create_index("symbol", unique=True)
    db.indices.create_index("instrument_id", unique=True)
    db.indices.create_index([("name", ASCENDING), ("instrument_id", ASCENDING)])
    desired_symbols = {symbol for symbol, _ in SEEDS}
    # Retire removed curated defaults without deleting their identity or history.
    db.indices.update_many(
        {"provenance.source": "curated", "symbol": {"$nin": list(desired_symbols)},
         "removed_at": {"$exists": False}},
        {"$set": {"removed_at": utcnow(), "removed_by": "catalogue-maintenance"}},
    )
    for symbol, name in SEEDS:
        identifier = "idx-" + str(uuid5(SEED_NAMESPACE, symbol))
        try:
            db.indices.update_one({"symbol": symbol}, {
                "$setOnInsert": {
                    "_id": identifier, "instrument_id": identifier, "symbol": symbol,
                    "name": name, "currency": None, "return_variant": "unknown",
                    "verification_status": "unverified", "provider": "yfinance",
                    "provenance": {"source": "curated", "metadata_verified": False},
                    "created_at": utcnow(),
                },
            }, upsert=True)
        except DuplicateKeyError:
            # Another startup or a concurrent manual add won the same symbol.
            pass


def require_index(db, instrument_id):
    record = db.indices.find_one({"instrument_id": instrument_id, "removed_at": {"$exists": False}})
    if record is None:
        raise DataError(404, "Index not found in the index catalogue")
    return record


def remove_index(db, instrument_id, requested_by):
    # Keep the identity and symbol reserved so seed upserts cannot resurrect it.
    # No downloaded data or customer records are deleted.
    result = db.indices.update_one(
        {"instrument_id": instrument_id, "removed_at": {"$exists": False}},
        {"$set": {"removed_at": utcnow(), "removed_by": requested_by}},
    )
    if not result.matched_count and not db.indices.find_one({"instrument_id": instrument_id}):
        raise DataError(404, "Index not found in the index catalogue")
    return {"instrument_id": instrument_id, "removed": True}


def add_index(db, values, requested_by):
    ensure_catalogue(db)
    identifier = "idx-" + str(uuid4())
    record = {"_id": identifier, "instrument_id": identifier, **values,
              "verification_status": "unverified", "provider": "yfinance",
              "provenance": {"source": "manual", "metadata_verified": False,
                             "added_by": requested_by}, "created_at": utcnow()}
    try:
        db.indices.insert_one(record)
    except DuplicateKeyError:
        existing = db.indices.find_one({"symbol": values["symbol"]})
        if existing is None:
            raise DataError(409, "Catalogue changed; retry adding the index") from None
        # Adding an existing symbol never changes identity or metadata.
        identifier = existing["instrument_id"]
        # Explicit re-add restores the same identity and its saved history.
        db.indices.update_one({"instrument_id": identifier},
                              {"$unset": {"removed_at": "", "removed_by": ""}})
    return index_detail(db, identifier)


def index_detail(db, instrument_id, *, record=None):
    record = record if record is not None else require_index(db, instrument_id)
    expire_stalled_jobs(db, instrument_id)
    state = db.instrument_state.find_one({"_id": instrument_id}) or {}
    listing = state.get("listing")
    generation = history_generation(db, instrument_id, listing)
    dates = quote_dates(db, instrument_id, listing, generation=generation)
    profile = None
    if listing and generation != "legacy":
        observation = db.instruments.find_one(observation_filter(db, instrument_id, listing, generation=generation))
        if observation:
            profile = {key: observation[key] for key in ("collected_at", "collection_date", "data")}
    # Derive verification from the same fenced publication as quotes. A failed
    # refresh cannot promote unverified metadata or erase a previous verification.
    verified = profile is not None
    currency = record.get("currency")
    provider_currency = profile["data"].get("currency") if profile else None
    if currency is None and isinstance(provider_currency, str) and re.fullmatch(r"[A-Z]{3}", provider_currency):
        currency = provider_currency
    job = db.acquisition_jobs.find_one({"instrument_id": instrument_id}, sort=[("requested_at", DESCENDING), ("_id", DESCENDING)])
    return public({
        **{key: record.get(key) for key in ("instrument_id", "name", "symbol", "return_variant", "provider")},
        "currency": currency, "verification_status": "verified" if verified else "unverified",
        "provenance": {**{key: value for key, value in record["provenance"].items() if key != "added_by"},
                       "verification_scope": "yahoo_symbol_and_index_type" if verified else None,
                       "verified_generation": generation if verified else None,
                       "currency_source": "catalogue" if record.get("currency") else "yahoo_chart" if currency else None},
        "generation": generation, "acquisition": public_job(job), "profile": profile,
        "summary": {"quote_count": len(dates), "first_date": dates[0] if dates else None, "last_date": dates[-1] if dates else None},
        "history_request": {"requested_from": HISTORY_START, "scope": "full_available_history",
                            "warning": "Requests Yahoo history from 1900-01-01 through yesterday. This is a request boundary, not an inception date or a completeness guarantee."},
    })


def list_indices(db, limit=20, offset=0):
    ensure_catalogue(db)
    active = {"removed_at": {"$exists": False}}
    records = db.indices.find(active).sort([("name", ASCENDING), ("instrument_id", ASCENDING)]).skip(offset).limit(limit)
    # Use the selected record: concurrent removal must not turn a list into 404.
    return {"items": [index_detail(db, row["instrument_id"], record=row) for row in records],
            "total": db.indices.count_documents(active), "limit": limit, "offset": offset}
