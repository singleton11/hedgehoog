"""Durable, explicitly requested provider observations and acquisition jobs.

No provider calls occur in read functions. Collection times are knowledge times,
not historical effective dates. At-least-once execution is not exactly-once HTTP.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import re
from uuid import uuid4

from bson import BSON, Decimal128, ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from marketstack import MarketstackClient  # Explicit legacy adapter; never a fallback.
from market_provider import MarketDataError
from request_pacing import wait_for_yahoo_request

# Backward-compatible exception name for legacy callers and stored jobs.
MarketstackError = MarketDataError


def provider_name(settings):
    return settings.market_data_provider


def configured(settings):
    return provider_name(settings) == "yfinance" or bool(settings.marketstack_api_key.get_secret_value())


def provider_client(settings, name, before_request):
    if name == "yfinance":
        from yahoo_finance import YahooFinanceClient
        return YahooFinanceClient(before_request=before_request, timeout=settings.market_data_timeout_seconds)
    if name == "marketstack":
        return MarketstackClient(settings.marketstack_api_key.get_secret_value(), before_request=before_request)
    raise MarketDataError("unknown_provider", "This acquisition uses an unsupported provider.")

LEASE_SECONDS = 300
QUEUE_TIMEOUT_SECONDS = 1800
PRICE_FIELDS = {"open", "high", "low", "close", "volume", "adj_open", "adj_high", "adj_low", "adj_close", "adj_volume", "dividend", "split_factor"}


def utcnow():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def public(value):
    """BSON-safe, exact-number JSON boundary for explicitly selected public data."""
    if isinstance(value, datetime):
        return aware(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, (Decimal, Decimal128)):
        return str(value)
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [public(v) for v in value]
    return value


class DataError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class JobBusy(Exception):
    pass


class LeaseLost(Exception):
    pass


def ensure_indexes(db):
    db.acquisition_jobs.create_index("instrument_id", unique=True, partialFilterExpression={"active": True}, name="one_active_acquisition")
    db.acquisition_jobs.create_index([("instrument_id", ASCENDING), ("requested_at", DESCENDING)])
    db.instruments.create_index([("instrument_id", ASCENDING), ("collected_at", DESCENDING)])
    db.instruments.create_index("job_id", unique=True)
    db.quotes.create_index([("instrument_id", ASCENDING), ("symbol", ASCENDING), ("exchange", ASCENDING), ("date", ASCENDING), ("collected_at", DESCENDING)])
    db.quotes.create_index([("job_id", ASCENDING), ("date", ASCENDING)], unique=True)
    db.raw_eod_pages.create_index([("job_id", ASCENDING), ("offset", ASCENDING)], unique=True)
    db.raw_split_pages.create_index([("job_id", ASCENDING), ("offset", ASCENDING)], unique=True)
    db.split_snapshots.create_index([("instrument_id", ASCENDING), ("symbol", ASCENDING), ("exchange", ASCENDING), ("requested_at", DESCENDING), ("_id", DESCENDING)])
    db.market_data_publications.create_index([("instrument_id", ASCENDING), ("published_at", DESCENDING), ("_id", DESCENDING)])


def latest_catalogue(db, isin):
    return db.etfs.find_one({"isin": isin}, sort=[("import_date", DESCENDING)])


def require_catalogue(db, isin):
    record = latest_catalogue(db, isin)
    if record is None:
        raise DataError(404, "Instrument not found in the ETF catalogue")
    return record


def require_market_catalogue(db, instrument_id):
    """Shared acquisition/quote boundary, not an ETF selector or ledger gate."""
    if instrument_id.startswith("idx-"):
        from indices import require_index
        return require_index(db, instrument_id)
    return require_catalogue(db, instrument_id)


def search_catalogue(db, query, limit):
    # Match AFTER grouping so an old name/ticker does not resurrect an obsolete snapshot.
    expression = re.escape(query.strip())
    records = db.etfs.aggregate([
        # Do not carry large holdings/allocation arrays through autocomplete's
        # latest-snapshot grouping on every keystroke.
        {"$project": {"_id": 0, "isin": 1, "import_date": 1, "name": 1, "ticker": 1, "asset_class": 1, "currency": 1}},
        {"$sort": {"isin": 1, "import_date": -1}},
        {"$group": {"_id": "$isin", "latest": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$latest"}},
        {"$match": {"$or": [{field: {"$regex": expression, "$options": "i"}} for field in ("isin", "ticker", "name")]}},
        {"$sort": {"name": 1, "isin": 1}},
        {"$limit": limit},
        {"$project": {"_id": 0, "isin": 1, "name": 1, "ticker": 1, "asset_class": 1, "currency": 1}},
    ])
    return [{**record, "instrument_id": record["isin"]} for record in records]


def acquired_universe(db):
    """Capture selector/bulk-update membership without paginating a changing list.

    Only read saved profiles and catalogue labels, never quote histories or
    return statistics. Later acquisitions appear on the next explicit load.
    """
    profiles = list(db.instruments.aggregate([
        {"$match": {"instrument_id": {"$not": {"$regex": "^idx-"}}, "$or": [{"provider": {"$ne": "yfinance"}}, {"published": True}]}},
        {"$group": {"_id": "$instrument_id", "collected_at": {"$max": "$collected_at"}}},
        {"$sort": {"_id": 1}},
    ]))
    labels = {}
    for record in db.etfs.find(
        {"isin": {"$in": [profile["_id"] for profile in profiles]}},
        {"_id": 0, "isin": 1, "name": 1, "ticker": 1, "import_date": 1},
    ).sort("import_date", DESCENDING):
        labels.setdefault(record["isin"], record)
    items = [{
        "instrument_id": profile["_id"],
        "name": labels.get(profile["_id"], {}).get("name"),
        "ticker": labels.get(profile["_id"], {}).get("ticker"),
        "collected_at": profile.get("collected_at"),
    } for profile in profiles]
    return public({"items": items, "total": len(items)})


HISTORY_BATCH_SIZE = 50
HISTORY_COMMAND_BYTES = 512 * 1024
HISTORY_COMMAND_OVERHEAD = 4096  # Projection, sort and driver command metadata.


def _history_batches(records, command):
    """Bound literal payloads and validation queries well below Mongo's BSON cap."""
    batch = []
    for record in records:
        proposed = [*batch, record]
        if len(proposed) > HISTORY_BATCH_SIZE or len(BSON.encode(command(proposed))) + HISTORY_COMMAND_OVERHEAD > HISTORY_COMMAND_BYTES:
            if batch:
                yield batch
            batch = [record]
            if len(BSON.encode(command(batch))) + HISTORY_COMMAND_OVERHEAD > HISTORY_COMMAND_BYTES:
                raise DataError(413, "A saved history identity exceeds the filter query size limit.")
        else:
            batch = proposed
    if batch:
        yield batch


def _history_publication_query(seeds, cutoff):
    queries = []
    for seed in seeds:
        query = {"instrument_id": seed["instrument_id"],
                 "listing": {"symbol": seed["symbol"], "exchange": seed["exchange"]}}
        if seed["generation"] is not None:
            query["_id"] = seed["generation"]
        queries.append(query)
    return {"published": True, "published_at": {"$lte": cutoff}, "$or": queries}


def history_bounds_pipeline(seeds):
    """Two correlated, index-ordered endpoint probes per literal candidate.

    Separate legacy and pinned batches keep job_id equality usable by the
    existing (job_id, date) index; legacy uses the listing/date index instead.
    The initial row is only a carrier, never a mutable profile reselection.
    """
    if not seeds:
        raise ValueError("History endpoint probes require a nonempty batch")
    legacy = seeds[0]["generation"] == "legacy"
    if any((seed["generation"] == "legacy") != legacy for seed in seeds):
        raise ValueError("Legacy and pinned history probes need separate batches")
    comparisons = [{"$eq": [f"${field}", f"$${field}"]}
                   for field in ("instrument_id", "symbol", "exchange")]
    if not legacy:
        comparisons += [{"$eq": ["$job_id", "$$generation"]}, {"$eq": ["$provider", "$$provider"]}]
    match = {"$expr": {"$and": comparisons}}
    if legacy:
        # Query $ne (unlike expression $ne) also accepts missing provider metadata.
        match["provider"] = {"$ne": "yfinance"}
    pipeline = [
        {"$limit": 1},
        {"$project": {"_id": 0, "candidate": {"$literal": seeds}}},
        {"$unwind": "$candidate"},
        {"$replaceRoot": {"newRoot": "$candidate"}},
    ]
    for name, direction in (("first", ASCENDING), ("last", DESCENDING)):
        pipeline.append({"$lookup": {
            "from": "quotes", "let": {field: f"${field}" for field in seeds[0]},
            "pipeline": [{"$match": match}, {"$sort": {"date": direction}},
                         {"$limit": 1}, {"$project": {"_id": 0, "date": 1}}],
            "as": name,
        }})
    pipeline.append({"$project": {
        "_id": 0, **{field: 1 for field in seeds[0]},
        "first_date": {"$arrayElemAt": ["$first.date", 0]},
        "last_date": {"$arrayElemAt": ["$last.date", 0]},
    }})
    return pipeline


def saved_history_bounds(db, observations, as_of=None):
    """Resolve publication visibility in batches, returning only compact extrema.

    Membership never enumerates quote dates. Publication time is shared across
    batches, but legacy quote observations retain their existing live semantics.
    """
    cutoff = as_of or utcnow()
    seeds = [{"instrument_id": row["_id"], **row["listing"],
              "generation": row.get("job_id") if row.get("provider") == "yfinance" else "legacy"}
             for row in observations if row["listing"]]
    publications = {}
    needs_publication = [seed for seed in seeds if seed["generation"] != "legacy"]
    validation_command = lambda batch: {"find": "acquisition_jobs", "filter": _history_publication_query(batch, cutoff)}
    for batch in _history_batches(needs_publication, validation_command):
        records = db.acquisition_jobs.find(
            _history_publication_query(batch, cutoff),
            {"instrument_id": 1, "listing": 1, "provider": 1, "requested_at": 1, "published_at": 1},
        ).sort([("requested_at", DESCENDING), ("published_at", DESCENDING), ("_id", DESCENDING)])
        for publication in records:
            publications.setdefault(publication["instrument_id"], publication)
        for seed in batch:
            publication = publications.get(seed["instrument_id"])
            if publication is None:
                if seed["generation"] is not None:
                    raise DataError(409, "This history generation is not published for this instrument and cutoff. Refresh instrument details.")
                seed["generation"] = "legacy"
            else:
                seed.update(generation=publication["_id"], provider=publication["provider"])
    bounds = {}
    for legacy in (True, False):
        candidates = [seed for seed in seeds if (seed["generation"] == "legacy") == legacy]
        command = lambda batch: {"aggregate": "instruments", "pipeline": history_bounds_pipeline(batch), "cursor": {}}
        for batch in _history_batches(candidates, command):
            records = list(db.instruments.aggregate(history_bounds_pipeline(batch)))
            # If the carrier vanished after inventory selection, do not return a
            # plausible but incorrect empty total. The caller can retry the read.
            if len(records) != len(batch) or {row["instrument_id"] for row in records} != {seed["instrument_id"] for seed in batch}:
                raise DataError(409, "Saved instrument inventory changed during filtering. Refresh the acquired list.")
            for row in records:
                bounds[row["instrument_id"]] = row
    return bounds


def acquired_instruments(db, limit=20, offset=0, min_history_years=None):
    """One row per saved ETF profile, including partial/failed acquisitions.

    The worker saves tickerinfo before any EOD pages, so profiles are the
    acquisition inventory. A queued job or cached listing alone is not data.
    Filter saved quote spans before pagination when a minimum is requested.
    Refresh cached return statistics only for the resulting page.
    """
    from return_statistics import refresh_statistics

    pipeline = [
        {"$match": {"instrument_id": {"$not": {"$regex": "^idx-"}}, "$or": [{"provider": {"$ne": "yfinance"}}, {"published": True}]}},
        {"$project": {"instrument_id": 1, "symbol": 1, "exchange": 1, "collected_at": 1, "job_id": 1, "provider": 1, "asset_class": 1, "index": 1}},
        {"$sort": {"instrument_id": 1, "collected_at": -1, "_id": -1}},
        {"$group": {
            "_id": "$instrument_id",
            "listing": {"$first": {"symbol": "$symbol", "exchange": "$exchange"}},
            "collected_at": {"$first": "$collected_at"},
            "asset_class": {"$first": "$asset_class"},
            "index": {"$first": "$index"},
            "job_id": {"$first": "$job_id"}, "provider": {"$first": "$provider"},
        }},
        {"$sort": {"collected_at": -1, "_id": 1}},
    ]
    bounds = {}
    publication_cutoff = utcnow()
    if min_history_years is None:
        pipeline.append({"$facet": {"count": [{"$count": "total"}], "items": [{"$skip": offset}, {"$limit": limit}]}})
        result = list(db.instruments.aggregate(pipeline))[0]
    else:
        observations = list(db.instruments.aggregate(pipeline))
        bounds = saved_history_bounds(db, observations, publication_cutoff)
        matching = []
        for observation in observations:
            span = bounds.get(observation["_id"], {})
            if not span.get("first_date") or not span.get("last_date"):
                continue
            start, end = date.fromisoformat(span["first_date"]), date.fromisoformat(span["last_date"])
            # Calendar anniversaries, clamping leap day to February 28.
            anniversary_year = start.year + min_history_years
            try:
                anniversary = start.replace(year=anniversary_year)
            except ValueError:
                anniversary = date(anniversary_year, 2, 28)
            if end >= anniversary:
                matching.append(observation)
        result = {"items": matching[offset:offset + limit], "count": [{"total": len(matching)}]}
    items = []
    for observation in result["items"]:
        isin = observation["_id"]
        catalogue = db.etfs.find_one(
            {"isin": isin}, {"_id": 0, "name": 1, "ticker": 1},
            sort=[("import_date", DESCENDING)],
        ) or {}
        generation = observation.get("job_id") if observation.get("provider") == "yfinance" else "legacy"
        if min_history_years is not None:
            generation = bounds[isin]["generation"]
            dates = quote_dates(db, isin, observation["listing"], generation=generation, publication_as_of=publication_cutoff)
            statistics, state = refresh_statistics(db, isin, generation=generation, as_of=publication_cutoff)
        else:
            dates = quote_dates(db, isin, observation["listing"], generation=generation)
            statistics, state = refresh_statistics(db, isin, generation=generation)
        # Never attach another listing's returns to the displayed profile.
        if state.get("listing") != observation["listing"]:
            statistics = {}
        items.append({
            "instrument_id": isin, "isin": isin,
            "name": catalogue.get("name"), "ticker": catalogue.get("ticker"),
            "listing": observation["listing"], "collected_at": observation["collected_at"],
            "asset_class": observation.get("asset_class"), "index": observation.get("index"),
            "quote_count": len(dates), "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None,
            "annualized_mean_log_return": statistics.get("annualized_mean_log_return"),
            "annualized_volatility": statistics.get("annualized_volatility"),
            "historical_year_return": statistics.get("historical_year_return"),
            "historical_year_start_date": statistics.get("historical_year_start_date"),
            "historical_year_end_date": statistics.get("historical_year_end_date"),
            "historical_year_price_basis": statistics.get("historical_year_price_basis"),
            "historical_year_carried_from_date": statistics.get("historical_year_carried_from_date"),
            "historical_year_volatility": statistics.get("historical_year_volatility"),
            "historical_year_volatility_start_date": statistics.get("historical_year_volatility_start_date"),
            "historical_year_volatility_end_date": statistics.get("historical_year_volatility_end_date"),
            "historical_year_volatility_price_basis": statistics.get("historical_year_volatility_price_basis"),
            "historical_year_volatility_return_count": statistics.get("historical_year_volatility_return_count"),
        })
    return public({"items": items, "total": result["count"][0]["total"] if result["count"] else 0, "limit": limit, "offset": offset})


def remove_acquired_instrument(db, isin):
    """Delete shared market data, retaining catalogue and customer-owned records.

    Use the acquisition unique index as a mutex: workers cannot start a new
    acquisition during deletion, and active jobs must finish before removal.
    """
    require_catalogue(db, isin)
    ensure_indexes(db)
    lock_id = str(uuid4())
    try:
        db.acquisition_jobs.insert_one({
            "_id": lock_id, "instrument_id": isin, "active": True,
            "status": "deleting", "requested_at": utcnow(),
        })
    except DuplicateKeyError:
        raise DataError(409, "This ETF has an active acquisition or removal. Wait for it to finish and retry.") from None
    try:
        for collection in ("quotes", "raw_eod_pages", "raw_split_pages", "split_snapshots",
                           "market_data_publications", "manual_splits"):
            db[collection].delete_many({"instrument_id": isin})
        db.instrument_statistics.delete_one({"_id": isin})
        db.instrument_state.delete_one({"_id": isin})
        db.acquisition_jobs.delete_many({"instrument_id": isin, "_id": {"$ne": lock_id}})
        # Remove inventory last so a partial failure remains visible/retryable.
        db.instruments.delete_many({"instrument_id": isin})
    finally:
        db.acquisition_jobs.delete_one({"_id": lock_id})


def budget_status(db, settings):
    month = utcnow().strftime("%Y-%m")
    if provider_name(settings) == "yfinance":
        return {"provider": "yfinance", "metered": False, "month": month, "limit": None, "used": 0, "remaining": None}
    record = db.marketstack_usage.find_one({"_id": month}) or {}
    used = record.get("used", 0)
    limit = settings.marketstack_monthly_budget
    return {"month": month, "limit": limit, "used": used, "remaining": max(0, limit - used)}


def reserve_credit(db, settings):
    month = utcnow().strftime("%Y-%m")
    limit = settings.marketstack_monthly_budget
    if limit <= 0:
        raise MarketstackError("local_budget_exhausted", "Marketstack collection is disabled by the local request budget.")
    try:
        db.marketstack_usage.insert_one({"_id": month, "used": 0})
    except DuplicateKeyError:
        pass
    result = db.marketstack_usage.find_one_and_update(
        {"_id": month, "used": {"$lt": limit}}, {"$inc": {"used": 1}}, return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise MarketstackError("local_budget_exhausted", "The local monthly Marketstack request budget is exhausted.")


def quote_date(value):
    """Index usable dates; unindexable rows remain in the complete raw page."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def pagination_integer(page, key):
    metadata = page.get("pagination")
    value = metadata.get(key) if isinstance(metadata, dict) else None
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        try:
            return int(value)
        except ValueError:
            pass
    return None


def has_more_pages(page, next_offset):
    rows = page["data"]
    if not rows:
        return False  # Never loop on an empty page, whatever metadata claims.
    total = pagination_integer(page, "total")
    if total is not None and total >= next_offset:
        return next_offset < total
    limit = pagination_integer(page, "limit") or 1000
    return len(rows) >= limit


def public_job(job):
    if job is None:
        return None
    fields = ("instrument_id", "status", "phase", "mode", "requested_at", "started_at", "finished_at", "pages", "rows", "flagged_rows", "unindexed_rows", "calls", "error", "warning")
    return public({"id": job["_id"], **{field: job.get(field) for field in fields}})


def expire_stalled_jobs(db, isin):
    # A UI polling an abandoned job can recover without a Celery result backend.
    # Fencing by status/owner prevents the old process from subsequently writing.
    db.acquisition_jobs.update_many(
        {"instrument_id": isin, "status": "running", "active": True, "lease_until": {"$lte": utcnow()}},
        {"$set": {"status": "failed", "active": False, "phase": "failed", "finished_at": utcnow(), "error": "Worker stopped responding. Request data again to retry the unfinished range."}},
    )
    # Covers an API crash between durable insertion and broker publication, as
    # well as an offline/backlogged worker. Late messages see a terminal job and
    # skip; the user can retry deliberately instead of staying queued forever.
    db.acquisition_jobs.update_many(
        {"instrument_id": isin, "status": "queued", "active": True, "requested_at": {"$lte": utcnow() - timedelta(seconds=QUEUE_TIMEOUT_SECONDS)}},
        {"$set": {"status": "failed", "active": False, "phase": "failed", "finished_at": utcnow(), "error": "Acquisition did not start within 30 minutes. Check the worker and request again; any old delivery will be skipped."}},
    )


def complete_listing(listing):
    return bool(isinstance(listing, dict) and listing.get("symbol") and listing.get("exchange"))


def same_request(left, right):
    return (left or None) == (right or None)


def listing_filter(isin, listing):
    return {"instrument_id": isin, "symbol": listing["symbol"], "exchange": listing["exchange"]}


def published_history(db, isin, listing, as_of=None, generation=None):
    """Only the atomic, owner-fenced job transition makes a generation visible.

    Evidence/publication records alone do not grant visibility. A timestamp is
    a knowledge-time bound, NOT a stable cursor: callers pin the returned UUID.
    """
    if not listing or generation == "legacy":
        return None
    saved_listing = {"symbol": listing["symbol"], "exchange": listing["exchange"]}
    query = {"instrument_id": isin, "listing": saved_listing, "published": True,
             "published_at": {"$lte": as_of or utcnow()}}
    if generation is not None:
        query["_id"] = generation
    publication = db.acquisition_jobs.find_one(query, sort=[("requested_at", DESCENDING), ("published_at", DESCENDING), ("_id", DESCENDING)])
    if generation is not None and publication is None:
        raise DataError(409, "This history generation is not published for this instrument and cutoff. Refresh instrument details.")
    return publication


def history_generation(db, isin, listing, as_of=None):
    publication = published_history(db, isin, listing, as_of)
    return publication["_id"] if publication else "legacy"


def observation_filter(db, isin, listing, as_of=None, generation=None):
    query = listing_filter(isin, listing)
    publication = published_history(db, isin, listing, as_of, generation)
    if publication:
        query.update(job_id=publication["_id"], provider=publication["provider"])
    else:
        # The explicit legacy cursor also pins the absence of any publication.
        query["provider"] = {"$ne": "yfinance"}
    return query


def quote_dates(db, isin, listing, as_of=None, generation=None, *, publication_as_of=None):
    if not listing:
        return []
    query = observation_filter(db, isin, listing, publication_as_of or as_of, generation)
    if as_of:
        query["collected_at"] = {"$lte": as_of}
    return sorted(db.quotes.distinct("date", query))


def coverage(catalogue, dates):
    inception = catalogue.get("inception_date")
    result = {"quote_count": len(dates), "first_date": dates[0] if dates else None, "last_date": dates[-1] if dates else None, "requested_from": inception, "history_status": "empty", "warning": None}
    if not dates:
        return result
    try:
        missing_start = (date.fromisoformat(dates[0]) - date.fromisoformat(inception)).days > 7
    except (TypeError, ValueError):
        missing_start = True
    result["history_status"] = "partial" if missing_start else "available"
    if missing_start:
        result["warning"] = (
            f"Incomplete since-inception history: requested from {inception}, but the earliest stored quote is {dates[0]}. "
            "Provider or listing coverage may start after fund inception; a completed request does not certify complete historical coverage."
        )
    return result


def set_price_currency(db, isin, currency):
    """Save current user-supplied metadata without rewriting provider observations."""
    require_catalogue(db, isin)
    db.instrument_state.update_one(
        {"_id": isin}, {"$set": {"price_currency_override": currency}}, upsert=True,
    )
    return {"price_currency_override": currency}


def instrument_detail(db, settings, isin, as_of=None):
    catalogue = require_catalogue(db, isin)
    expire_stalled_jobs(db, isin)
    state = db.instrument_state.find_one({"_id": isin}) or {}
    listing = state.get("listing")
    cutoff = as_of or utcnow()
    generation = history_generation(db, isin, listing, cutoff)
    profile = None
    if listing:
        query = observation_filter(db, isin, listing, cutoff, generation)
        if as_of:
            query["collected_at"] = {"$lte": as_of}
        observation = db.instruments.find_one(query, sort=[("collected_at", DESCENDING), ("_id", DESCENDING)])
        if observation:
            profile = {key: observation.get(key) for key in ("collected_at", "collection_date", "data", "asset_class", "index")}
    job = db.acquisition_jobs.find_one({"instrument_id": isin}, sort=[("requested_at", DESCENDING), ("_id", DESCENDING)])
    summary = coverage(catalogue, quote_dates(db, isin, listing, cutoff, generation))
    if job is None:
        summary["history_status"] = "not_requested"
    return public({"instrument_id": isin, "generation": generation, "catalogue": {key: value for key, value in catalogue.items() if key != "_id"}, "listing": listing, "listing_resolution": state.get("listing_resolution"), "price_currency_override": state.get("price_currency_override"), "profile": profile, "acquisition": public_job(job), "summary": summary, "provider": provider_name(settings), "price_basis": "provider_split_adjusted" if provider_name(settings) == "yfinance" else "unadjusted", "provider_configured": configured(settings), "budget": None if provider_name(settings) == "yfinance" else budget_status(db, settings)})


def read_quotes(db, isin, limit=1000, offset=0, as_of=None, generation=None):
    require_market_catalogue(db, isin)
    as_of = as_of or utcnow()
    snapshot = public(as_of)
    state = db.instrument_state.find_one({"_id": isin}) or {}
    listing = state.get("listing")
    if not listing:
        return {"items": [], "total": 0, "limit": limit, "offset": offset, "as_of": snapshot, "generation": "legacy"}
    generation = generation or history_generation(db, isin, listing, as_of)
    query = observation_filter(db, isin, listing, as_of, generation)
    if as_of:
        query["collected_at"] = {"$lte": as_of}
    latest = [
        {"$match": query},
        {"$sort": {"date": 1, "collected_at": -1, "_id": -1}},
        {"$group": {"_id": "$date", "observation": {"$first": "$$ROOT"}}},
        {"$sort": {"_id": 1}},
        {"$facet": {"count": [{"$count": "total"}], "items": [{"$skip": offset}, {"$limit": limit}]}},
    ]
    result = list(db.quotes.aggregate(latest))[0]
    items = []
    for record in result["items"]:
        observation = record["observation"]
        item = {**observation["data"], "provider_date": observation["data"].get("date"), "date": observation["date"], "collected_at": observation["collected_at"], "collection_date": observation["collection_date"], "quality_flags": observation.get("quality_flags", [])}
        for field in PRICE_FIELDS:
            if type(item.get(field)) in (int, float, Decimal, Decimal128):
                item[field] = str(item[field])
        items.append(public(item))
    return {"items": items, "total": result["count"][0]["total"] if result["count"] else 0, "limit": limit, "offset": offset, "as_of": snapshot, "generation": generation}


def incremental_requires_full_history(db, job, page):
    """Validate the shared boundary before combining Yahoo adjustment vintages."""
    from split_adjustments import split_events
    from yahoo_finance import splits_from_eod_page

    if has_more_pages(page, len(page["data"])):
        raise MarketDataError("incomplete_history", "The incremental Yahoo chart must contain the complete requested range.")
    snapshot = db.split_snapshots.find_one({"_id": job["base_generation"]})
    previous = db.quotes.find_one({"job_id": job["base_generation"], "date": job["date_from"]})
    overlap = next((row for row in page["data"] if isinstance(row, dict) and quote_date(row.get("date")) == job["date_from"]), None)
    if snapshot is None or previous is None:
        return True
    actions = splits_from_eod_page(page, job["listing"]["symbol"], job["date_from"], job["date_to"])
    events = split_events([{"data": actions}], job["listing"], job["date_from"], job["date_to"])
    expected = [event for event in snapshot["events"] if job["date_from"] <= event["date"] <= job["date_to"]]
    if events != expected:
        return True
    if not page["data"]:
        return False  # Successful empty check: retain history and record freshness.
    if overlap is None:
        return True
    # Dividends may revise Adj Close even with unchanged OHLC. Quotes expose
    # it too, so do not combine incompatible dividend-adjustment vintages.
    for field in ("open", "high", "low", "close", "adj_close", "price_currency", "price_basis"):
        old, new = previous["data"].get(field), overlap.get(field)
        if field in ("price_currency", "price_basis"):
            if old != new:
                return True
        else:
            if old is None and new is None and field != "close":
                continue
            try:
                if old is None or new is None or isinstance(old, bool) or isinstance(new, bool):
                    return True
                left, right = Decimal(str(old)), Decimal(str(new))
                if not left.is_finite() or not right.is_finite() or left != right:
                    return True
            except (ArithmeticError, ValueError, TypeError):
                return True
    return False


def request_acquisition(db, settings, isin, listing, mode, requested_by, publish, *, incremental=False):
    catalogue = require_market_catalogue(db, isin)
    is_index = isin.startswith("idx-")
    if is_index:
        from indices import HISTORY_START
        inception = date.fromisoformat(HISTORY_START)  # Request boundary, not index inception.
        listing = {"symbol": catalogue["symbol"], "exchange": "YAHOO"}
    else:
        try:
            inception = date.fromisoformat(catalogue["inception_date"])
        except (KeyError, TypeError, ValueError):
            raise DataError(422, "This instrument has no valid inception date for a history request") from None
        if inception > utcnow().date():
            raise DataError(422, "The instrument inception date is in the future")
    name = "yfinance" if is_index else provider_name(settings)
    if not is_index and not configured(settings):
        raise DataError(503, "Configure the selected market-data provider on the API and worker before requesting data")
    ensure_indexes(db)
    expire_stalled_jobs(db, isin)
    requested_listing = {"symbol": listing["symbol"], **({"exchange": listing["exchange"]} if listing.get("exchange") else {})} if listing else None
    if requested_listing is not None and not requested_listing.get("exchange") and name != "yfinance":
        raise DataError(422, "Supply both symbol and exchange for a manual listing override with this provider")
    state = db.instrument_state.find_one({"_id": isin}) or {}
    listing = requested_listing if complete_listing(requested_listing) else None
    if requested_listing is None:
        listing = state.get("listing")
    existing = db.acquisition_jobs.find_one({"instrument_id": isin, "active": True})
    if existing:
        if existing.get("status") == "deleting":
            raise DataError(409, "This ETF is being removed. Please retry after removal finishes.")
        existing_request = existing.get("requested_listing") or existing.get("listing")
        if requested_listing is not None and not same_request(existing_request, requested_listing):
            raise DataError(409, "Another listing is already being collected for this instrument")
        return public_job(existing)
    if name == "marketstack" and budget_status(db, settings)["remaining"] < 1:
        raise DataError(409, "The local monthly Marketstack request budget is exhausted")
    ticker = catalogue.get("ticker")
    ticker = ticker.strip().upper() if isinstance(ticker, str) else ""
    lookup_ticker = requested_listing["symbol"] if requested_listing and not requested_listing.get("exchange") else ticker
    if not listing and requested_listing is None and (len(ticker) > 64 or not re.fullmatch(r"[A-Z0-9^][A-Z0-9._^=\-]*", ticker)):
        hint = "Supply a manual Yahoo symbol." if name == "yfinance" else "Supply a manual symbol and MIC."
        raise DataError(422, f"This instrument has no valid catalogue ticker. {hint}")
    state_listing = state.get("listing")
    if state_listing and db.quotes.count_documents({"instrument_id": isin}, limit=1):
        if requested_listing is None:
            requested_matches_saved = listing == state_listing
        elif requested_listing.get("exchange"):
            requested_matches_saved = requested_listing == state_listing
        else:
            requested_matches_saved = requested_listing["symbol"] == state_listing.get("symbol")
        if not requested_matches_saved:
            raise DataError(409, "A listing with stored quotes cannot be changed or mixed with another listing")
    same_listing = state_listing == listing
    last_complete = state.get("last_complete_quote_date") if same_listing else None
    dates = quote_dates(db, isin, listing) if listing else []
    # Yahoo can publish the current day's completed bar after the previous
    # UTC day ended. Request that inclusive boundary; the provider returns no
    # bar when the market has not closed yet. Keep the legacy Marketstack
    # boundary unchanged for compatibility with its incremental watermark.
    collection_date = utcnow().date() if name == "yfinance" else utcnow().date() - timedelta(days=1)
    end = collection_date
    start = inception
    actual_mode = "initial"
    base = published_history(db, isin, listing) if name == "yfinance" and incremental else None
    if name == "yfinance":
        actual_mode = "refresh"
        usable_dates = [day for day in dates if day <= end.isoformat()]
        if base and base.get("provider") == name and usable_dates:
            actual_mode = "incremental"
            start = date.fromisoformat(usable_dates[-1])  # Inclusive overlap detects changed adjustment vintages.
    elif mode == "backfill" and dates:
        actual_mode = "backfill"
        end = date.fromisoformat(dates[0]) - timedelta(days=1)
    elif last_complete:
        actual_mode = "update"
        start = date.fromisoformat(last_complete) + timedelta(days=1)
    job_id = str(uuid4())
    job = {
        "_id": job_id, "instrument_id": isin, "listing": listing, "requested_listing": requested_listing,
        "lookup_ticker": lookup_ticker,
        "instrument_kind": "index" if is_index else "etf",
        "history_scope": "full_available_history" if is_index else "since_inception",
        "provider": name, "price_basis": "provider_split_adjusted" if name == "yfinance" else "unadjusted",
        "requested_by": requested_by, "requested_at": utcnow(), "mode": actual_mode,
        "date_from": start.isoformat(), "date_to": end.isoformat(),
        **({"base_generation": base["_id"], "full_date_from": inception.isoformat()}
           if actual_mode == "incremental" else {}),
        # Manual Yahoo and legacy requests cover all actions. Automatic Yahoo
        # diffs reuse earlier evidence and validate actions in the overlap.
        "splits_date_from": start.isoformat() if actual_mode == "incremental" else min(inception.isoformat(), dates[0]) if dates else inception.isoformat(),
        "splits_date_to": max(inception.isoformat(), collection_date.isoformat()),
        "status": "queued", "active": True, "phase": "queued", "pages": 0, "rows": 0, "flagged_rows": 0, "unindexed_rows": 0, "calls": 0,
        "next_offset": 0, "expected_total": None, "started_at": None, "finished_at": None,
        "error": None, "warning": None,
    }
    try:
        db.acquisition_jobs.insert_one(job)
    except DuplicateKeyError:
        existing = db.acquisition_jobs.find_one({"instrument_id": isin, "active": True})
        if existing is None or existing.get("status") == "deleting":
            raise DataError(409, "Acquisition state changed; please retry") from None
        existing_request = existing.get("requested_listing") or existing.get("listing")
        if requested_listing is not None and not same_request(existing_request, requested_listing):
            raise DataError(409, "Another listing is already being collected for this instrument") from None
        return public_job(existing)
    try:
        if listing and name != "yfinance":
            change = {"listing": listing}
            if not same_listing:
                change.update(last_complete_quote_date=None, listing_resolution=None)
            db.instrument_state.update_one({"_id": isin}, {"$set": change}, upsert=True)
        publish(settings, job_id)
    except Exception:
        db.acquisition_jobs.update_one({"_id": job_id, "status": "queued"}, {"$set": {"active": False, "status": "failed", "phase": "failed", "finished_at": utcnow(), "error": "Could not queue acquisition. Check RabbitMQ and try again."}})
        raise DataError(503, "Could not queue acquisition. Check RabbitMQ and try again.") from None
    return public_job(job)


def resolve_listing(provider, ticker, isin):
    """First named exact TICKER.suffix EOD candidate, in provider result order.

    A suffix is not a MIC: only stock_exchange.mic supplies the exchange filter.
    This is the user's selection heuristic, not independently verified identity.
    Pagination remains bounded by the job's quota callback.
    """
    pattern = re.compile(re.escape(ticker) + r"\.[A-Z0-9]+", re.IGNORECASE)
    offset = 0
    while True:
        page = provider.tickerslist_page(ticker, offset=offset)
        for row in page["data"]:
            if not isinstance(row, dict):
                continue
            symbol, name = row.get("ticker"), row.get("name")
            exchange = row.get("stock_exchange")
            mic = exchange.get("mic") if isinstance(exchange, dict) else None
            if (
                not isinstance(symbol, str) or len(symbol) > 64 or not pattern.fullmatch(symbol)
                or not isinstance(name, str) or not name.strip()
                or row.get("has_eod") is not True
                or not isinstance(mic, str) or not re.fullmatch(r"[A-Z0-9]{4}", mic, re.IGNORECASE)
            ):
                continue
            return {"symbol": symbol.upper(), "exchange": mic.upper()}, {
                "source": "tickerslist", "query": ticker, "name": name.strip(),
                "collected_at": utcnow(), "data": row,
            }
        offset += len(page["data"])
        if not has_more_pages(page, offset):
            break
    raise MarketstackError("listing_not_found", "No named EOD listing matched the exact catalogue ticker.exchange pattern with a valid MIC. Supply a manual symbol and MIC to request data.")


def run_acquisition(db, settings, job_id, provider_factory=None):
    """Run/resume a job. Only a fully processed range advances the watermark."""
    ensure_indexes(db)
    now = utcnow()
    owner = str(uuid4())
    job = db.acquisition_jobs.find_one_and_update(
        {"_id": job_id, "active": True, "$or": [{"status": "queued"}, {"status": "running", "lease_until": {"$lte": now}}]},
        {"$set": {"status": "running", "phase": "tickerinfo", "owner": owner, "lease_until": now + timedelta(seconds=LEASE_SECONDS), "started_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if job is None:
        existing = db.acquisition_jobs.find_one({"_id": job_id})
        if existing and existing.get("status") == "running" and existing.get("active"):
            raise JobBusy()
        return public_job(existing)
    owned = {"_id": job_id, "owner": owner, "active": True, "status": "running"}
    # Jobs created before provider routing were explicitly Marketstack jobs.
    name = job.get("provider", "marketstack")

    def checkpoint(values=None, increments=None):
        update = {"$set": {"lease_until": utcnow() + timedelta(seconds=LEASE_SECONDS), **(values or {})}}
        if increments:
            update["$inc"] = increments
        result = db.acquisition_jobs.find_one_and_update(owned, update, return_document=ReturnDocument.AFTER)
        if result is None:
            raise LeaseLost()
        return result

    def before_request():
        current = checkpoint()
        cap = settings.market_data_max_calls_per_job if name == "yfinance" else settings.marketstack_max_calls_per_job
        if current["calls"] >= cap:
            raise MarketDataError("job_request_limit", "This job reached its request safety limit. Its unfinished range was not marked complete.")
        if name == "marketstack":
            reserve_credit(db, settings)
        else:
            wait_for_yahoo_request(db, settings.yahoo_request_interval_seconds, checkpoint)
        checkpoint(increments={"calls": 1})

    try:
        if name == "marketstack" and not settings.marketstack_api_key.get_secret_value():
            raise MarketDataError("not_configured", "The worker has no legacy provider credentials configured.")
        listing = job["listing"]
        isin = job["instrument_id"]
        requested_listing = job.get("requested_listing")
        client = (provider_factory(settings.marketstack_api_key.get_secret_value(), before_request=before_request)
                  if provider_factory else provider_client(settings, name, before_request))
        with client as provider:
            if not listing:
                checkpoint({"phase": "resolving_listing"})
                resolution = job.get("listing_resolution")
                if requested_listing and not requested_listing.get("exchange"):
                    if name != "yfinance":
                        raise MarketDataError("invalid_request", "This provider requires both symbol and exchange for manual listing overrides.")
                    listing, resolution = provider.resolve_symbol(requested_listing["symbol"])
                    if listing.get("symbol") != requested_listing["symbol"]:
                        raise MarketDataError("identity_mismatch", "Yahoo Finance returned a different instrument.")
                    resolution = {**resolution, "collected_at": utcnow()}
                else:
                    # A DB interruption can leave a cached mapping before the job
                    # checkpoint. Reuse it on redelivery rather than paying again.
                    state = db.instrument_state.find_one({"_id": isin}) or {}
                    listing = state.get("listing")
                    if not listing:
                        if name == "yfinance":
                            listing, resolution = provider.resolve_listing(job["lookup_ticker"], isin)
                            resolution = {**resolution, "collected_at": utcnow()}
                        else:
                            listing, resolution = resolve_listing(provider, job["lookup_ticker"], isin)
                        if name != "yfinance":
                            checkpoint()
                            # Checkpointing alone cannot fence a paused worker's next
                            # write. Install only into an empty cache; a replacement
                            # worker may already have stored another listing and prices.
                            try:
                                state = db.instrument_state.find_one_and_update(
                                    {"_id": isin, "listing": None}, {"$set": {
                                        "listing": listing, "listing_resolution": resolution,
                                        "last_complete_quote_date": None,
                                    }}, upsert=True, return_document=ReturnDocument.AFTER,
                                )
                            except DuplicateKeyError:
                                state = db.instrument_state.find_one({"_id": isin})
                            listing = state["listing"]
                state_listing = (db.instrument_state.find_one({"_id": isin}) or {}).get("listing")
                if state_listing and state_listing != listing and db.quotes.count_documents({"instrument_id": isin}, limit=1):
                    raise MarketDataError("listing_conflict", "A listing with stored quotes cannot be changed or mixed with another listing.")
                update = {"listing": listing}
                if resolution is not None:
                    update["listing_resolution"] = resolution
                job = checkpoint(update)
            checkpoint({"phase": "tickerinfo"})
            if db.instruments.find_one({"job_id": job_id}) is None:
                data = provider.tickerinfo(listing["symbol"])
                collected = utcnow()
                catalogue_metadata = {}
                if job.get("instrument_kind") != "index":
                    catalogue = require_catalogue(db, isin)
                    catalogue_metadata = {key: catalogue.get(key) for key in ("asset_class", "index")}
                checkpoint()
                db.instruments.update_one({"job_id": job_id}, {"$setOnInsert": {
                    "instrument_id": isin, **listing, **catalogue_metadata, "provider": name, "job_id": job_id,
                    "published": name != "yfinance",
                    "collected_at": collected, "collection_date": collected.date().isoformat(), "data": data,
                }}, upsert=True)
            if job.get("instrument_kind") == "index":
                from yahoo_finance import validate_index_identity
                # Validate persisted raw chart metadata on retries as well.
                profile = db.instruments.find_one({"job_id": job_id})
                validate_index_identity(profile["data"], listing["symbol"])
            job = checkpoint({"phase": "quotes"})
            if job.get("mode") == "incremental" and not job.get("incremental_checked"):
                # Keep the probe even when its overlap requires a full refresh.
                probe = db.raw_eod_pages.find_one({"job_id": job_id, "offset": -1})
                if probe is None:
                    page = provider.eod_page(listing["symbol"], listing["exchange"], job["date_from"], job["date_to"], offset=0)
                    collected = utcnow()
                    checkpoint()
                    probe = db.raw_eod_pages.find_one_and_update(
                        {"job_id": job_id, "offset": -1}, {"$setOnInsert": {
                            "instrument_id": isin, **listing, "provider": name, "job_id": job_id,
                            "offset": -1, "collected_at": collected,
                            "collection_date": collected.date().isoformat(), "data": page,
                        }}, upsert=True, return_document=ReturnDocument.AFTER,
                    )
                if job.get("instrument_kind") == "index":
                    from yahoo_finance import validate_index_page
                    validate_index_page(probe["data"], listing["symbol"])
                if incremental_requires_full_history(db, job, probe["data"]):
                    job = checkpoint({"mode": "refresh", "date_from": job["full_date_from"],
                                      "splits_date_from": job["full_date_from"],
                                      "incremental_checked": True, "full_refresh_reason": "Changed or missing overlap/corporate actions"})
                else:
                    checkpoint()
                    db.raw_eod_pages.update_one({"job_id": job_id, "offset": 0}, {"$setOnInsert": {
                        **{key: value for key, value in probe.items() if key != "_id"}, "offset": 0,
                    }}, upsert=True)
                    job = checkpoint({"incremental_checked": True})
            offset = job["next_offset"]
            latest_date = job.get("last_page_date")
            while job["date_from"] <= job["date_to"] and not job.get("download_complete"):
                raw = db.raw_eod_pages.find_one({"job_id": job_id, "offset": offset})
                if raw is None:
                    page = provider.eod_page(listing["symbol"], listing["exchange"], job["date_from"], job["date_to"], offset=offset)
                    collected = utcnow()
                    checkpoint()
                    raw = db.raw_eod_pages.find_one_and_update(
                        {"job_id": job_id, "offset": offset}, {"$setOnInsert": {
                            "instrument_id": isin, **listing, "provider": name,
                            "job_id": job_id, "offset": offset, "collected_at": collected,
                            "collection_date": collected.date().isoformat(), "data": page,
                        }}, upsert=True, return_document=ReturnDocument.AFTER,
                    )
                # Retry indexing the original saved response, not a new paid
                # request. All rows/fields are retained before any projection.
                page, collected = raw["data"], raw["collected_at"]
                if job.get("instrument_kind") == "index":
                    from yahoo_finance import validate_index_page
                    validate_index_page(page, listing["symbol"])
                rows = page["data"]
                unindexed = 0
                checkpoint()
                for index, row in enumerate(rows):
                    if index % 100 == 0:
                        checkpoint()
                    day = quote_date(row.get("date")) if isinstance(row, dict) else None
                    if day is None:
                        unindexed += 1
                        continue
                    db.quotes.update_one({"job_id": job_id, "date": day}, {"$setOnInsert": {
                        "instrument_id": isin, **listing, "provider": name, "job_id": job_id,
                        "date": day, "collected_at": collected, "collection_date": collected.date().isoformat(),
                        "data": row,
                    }}, upsert=True)
                    # A future provider date is still stored, but must not stop
                    # subsequent updates from requesting genuinely newer data.
                    if day <= job["date_to"] and (latest_date is None or day > latest_date):
                        latest_date = day
                offset += len(rows)
                job = checkpoint({
                    "next_offset": offset, "expected_total": pagination_integer(page, "total"),
                    "last_page_date": latest_date, "download_complete": not has_more_pages(page, offset),
                }, {"pages": 1, "rows": len(rows), "unindexed_rows": unindexed})
            from split_adjustments import split_events
            # Legacy queued jobs also gain full-range split acquisition. Freeze
            # the request range before the first call for deterministic replay.
            if "splits_date_from" not in job:
                dates = quote_dates(db, isin, listing)
                inception = require_catalogue(db, isin)["inception_date"]
                job = checkpoint({
                    "splits_date_from": min(inception, dates[0]) if dates else inception,
                    "splits_date_to": max(inception, (aware(job["requested_at"]).date() - timedelta(days=1)).isoformat()),
                })
            job = checkpoint({"phase": "splits"})
            split_offset = job.get("next_split_offset", 0)
            while not job.get("splits_complete"):
                raw = db.raw_split_pages.find_one({"job_id": job_id, "offset": split_offset})
                if raw is None:
                    if name == "yfinance":
                        from yahoo_finance import splits_from_eod_page
                        evidence = db.raw_eod_pages.find_one({"job_id": job_id, "offset": 0})
                        if evidence is None or (job["splits_date_from"], job["splits_date_to"]) != (job["date_from"], job["date_to"]):
                            raise MarketDataError("missing_action_evidence", "The saved Yahoo chart does not cover the action range. Request full history again.")
                        # Yahoo's chart includes actions. Derive from the exact
                        # persisted response, including after a worker restart.
                        page = splits_from_eod_page(evidence["data"], listing["symbol"], job["splits_date_from"], job["splits_date_to"])
                        collected = evidence["collected_at"]
                    else:
                        page = provider.splits_page(listing["symbol"], job["splits_date_from"], job["splits_date_to"], offset=split_offset)
                        collected = utcnow()
                    checkpoint()
                    raw = db.raw_split_pages.find_one_and_update(
                        {"job_id": job_id, "offset": split_offset}, {"$setOnInsert": {
                            "instrument_id": isin, **listing, "provider": name,
                            "job_id": job_id, "offset": split_offset, "collected_at": collected,
                            "collection_date": collected.date().isoformat(), "data": page,
                        }}, upsert=True, return_document=ReturnDocument.AFTER,
                    )
                page = raw["data"]
                split_offset += len(page["data"])
                total = pagination_integer(page, "total")
                expected = job.get("expected_split_total")
                if total is not None and (total < split_offset or (expected is not None and total != expected)):
                    raise MarketstackError("inconsistent_split_history", "The provider changed split pagination totals. Retry market data; the unfinished range was not marked complete.")
                total = total if total is not None else expected
                if not page["data"] and total is not None and total > split_offset:
                    raise MarketstackError("incomplete_split_history", "The provider ended split pagination before all actions were returned. Retry market data; the unfinished range was not marked complete.")
                more = split_offset < total if total is not None else has_more_pages(page, split_offset)
                job = checkpoint({"next_split_offset": split_offset, "expected_split_total": total, "splits_complete": not more})
            if db.split_snapshots.find_one({"_id": job_id}) is None:
                events = split_events(db.raw_split_pages.find({"job_id": job_id}).sort("offset", ASCENDING), listing,
                                      job["splits_date_from"], job["splits_date_to"])
                snapshot_start = job["splits_date_from"]
                if job.get("mode") == "incremental":
                    base_snapshot = db.split_snapshots.find_one({"_id": job["base_generation"]})
                    if base_snapshot is None:
                        raise MarketDataError("missing_action_evidence", "The previous history has no split snapshot. Request full history again.")
                    events = [event for event in base_snapshot["events"] if event["date"] < snapshot_start] + events
                    snapshot_start = base_snapshot["date_from"]
                checkpoint()
                db.split_snapshots.update_one({"_id": job_id}, {"$setOnInsert": {
                    "instrument_id": isin, **listing, "job_id": job_id, "provider": name,
                    "collected_at": utcnow(), "requested_at": job["requested_at"],
                    "date_from": snapshot_start, "date_to": job["splits_date_to"], "events": events,
                }}, upsert=True)
        if name == "yfinance":
            if job.get("mode") == "incremental":
                # Materialize a complete immutable generation without changing
                # previous observations or their original collection times.
                for index, observation in enumerate(db.quotes.find({
                    **listing_filter(isin, listing), "provider": name,
                    "job_id": job["base_generation"],
                })):
                    if index % 100 == 0:
                        checkpoint()
                    db.quotes.update_one({"job_id": job_id, "date": observation["date"]}, {"$setOnInsert": {
                        **{key: value for key, value in observation.items() if key != "_id"},
                        "job_id": job_id, "source_job_id": observation.get("source_job_id", observation["job_id"]),
                    }}, upsert=True)
            if not db.quotes.find_one({"job_id": job_id}):
                raise MarketDataError("empty_history", "Yahoo returned no daily historical quotes for this symbol and requested dates. A searchable symbol does not guarantee downloadable history. Previous saved history remains active.")
            checkpoint({"phase": "publishing"})
            # One immutable publication selects one generation for prices,
            # profile and splits. Retry acknowledgement loss is idempotent.
            db.market_data_publications.update_one({"_id": job_id}, {"$setOnInsert": {
                "instrument_id": isin, **listing, "provider": name,
                "price_basis": "provider_split_adjusted", "published_at": utcnow(),
            }}, upsert=True)
            # This single conditional job write is the visibility boundary.
            # If an expired/replaced worker pauses before it, the owner/status
            # predicate fails on resume; an earlier checkpoint is not enough.
            publication = db.market_data_publications.find_one({"_id": job_id})
            job = checkpoint({"published": True, "published_at": publication["published_at"]})
            db.instruments.update_one({"job_id": job_id}, {"$set": {"published": True}})
            while True:
                checkpoint({"phase": "caching_listing"})
                state = db.instrument_state.find_one({"_id": isin}) or {}
                latest = db.acquisition_jobs.find_one(
                    {"instrument_id": isin, "published": True},
                    sort=[("requested_at", DESCENDING), ("published_at", DESCENDING), ("_id", DESCENDING)],
                )
                if latest is None or latest["_id"] != job_id:
                    raise LeaseLost()
                if state.get("listing_job_id") == job_id and state.get("listing") == listing:
                    break  # Redelivery must not reset a completed watermark.
                change = {"listing": listing, "listing_job_id": job_id}
                if job.get("listing_resolution") is not None:
                    change["listing_resolution"] = job["listing_resolution"]
                elif state.get("listing") != listing:
                    change["listing_resolution"] = None
                if state.get("listing") != listing:
                    change["last_complete_quote_date"] = None
                # The latest-publication check and lease cannot fence a later
                # write to this other collection. CAS the observed version too;
                # on contention, reread and recheck ownership/publication order.
                try:
                    db.instrument_state.update_one({
                        "_id": isin, "listing": state.get("listing"),
                        "listing_job_id": state.get("listing_job_id"),
                    }, {"$set": change}, upsert=True)
                    break
                except DuplicateKeyError:
                    continue
        # Derived analytics never rewrite provider evidence. Persist them before
        # reporting success; DB retries replay saved pages without provider calls.
        from return_statistics import refresh_statistics
        checkpoint({"phase": "statistics"})
        if job.get("instrument_kind") != "index":
            refresh_statistics(db, isin, force=True)
        # Check ownership after all writes and before committing the range watermark.
        checkpoint({"phase": "finishing"})
        state = db.instrument_state.find_one({"_id": isin}) or {}
        watermark = state.get("last_complete_quote_date")
        if latest_date and (watermark is None or latest_date > watermark):
            # This write is last, never on failed/incomplete pagination. It does not
            # advance to date_to when the provider is empty or publishes late.
            db.instrument_state.update_one({"_id": isin, "listing": listing}, {"$set": {"last_complete_quote_date": latest_date}})
        if job.get("instrument_kind") == "index":
            warning = "Yahoo history was requested from 1900-01-01; this boundary is not index inception or a guarantee of complete coverage."
        else:
            warning = coverage(require_catalogue(db, isin), quote_dates(db, isin, listing))["warning"]
        if job["rows"] == 0:
            warning = "The provider returned no quotes for this range. No dates were marked as received." + (" " + warning if warning else "")
        if job.get("unindexed_rows", 0):
            warning = f"{job['unindexed_rows']} row(s) without a usable date are retained in raw EOD pages, but cannot be shown in dated quote history." + (" " + warning if warning else "")
        db.acquisition_jobs.update_one(owned, {"$set": {"status": "succeeded", "active": False, "phase": "complete", "finished_at": utcnow(), "warning": warning, "error": None}, "$unset": {"error_code": ""}})
    except LeaseLost:
        # A replacement/recovery invalidated this worker; it must not finish the job.
        pass
    except MarketstackError as exc:
        db.acquisition_jobs.update_one(owned, {"$set": {"status": "failed", "active": False, "phase": "failed", "finished_at": utcnow(), "error": str(exc), "error_code": exc.code}})
    except PyMongoError:
        # Retain the checkpoint and lease; the Celery task can retry DB failures.
        raise
    except Exception:
        db.acquisition_jobs.update_one(owned, {"$set": {"status": "failed", "active": False, "phase": "failed", "finished_at": utcnow(), "error": "Acquisition stopped unexpectedly. Check the worker and retry the unfinished range."}})
        raise
    return public_job(db.acquisition_jobs.find_one({"_id": job_id}))
