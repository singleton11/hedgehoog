"""Durable, explicitly requested provider observations and acquisition jobs.

No provider calls occur in read functions. Collection times are knowledge times,
not historical effective dates. At-least-once execution is not exactly-once HTTP.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import logging
import math
import re
from time import perf_counter
from uuid import uuid4

from bson import BSON, Decimal128, ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from market_provider import MarketDataError
from request_pacing import wait_for_yahoo_request

# Backward-compatible exception name for legacy callers and stored jobs.
MarketstackError = MarketDataError


def provider_name(settings):
    return settings.market_data_provider


def configured(settings):
    return provider_name(settings) == "yfinance"


def provider_client(settings, name, before_request):
    if name == "yfinance":
        from yahoo_finance import YahooFinanceClient
        return YahooFinanceClient(before_request=before_request, timeout=settings.market_data_timeout_seconds)
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


def _acquired_publication_pipeline():
    return [
        {"$match": {"instrument_id": {"$not": {"$regex": "^idx-"}}, "provider": "yfinance", "published": True}},
        {"$sort": {"requested_at": -1, "published_at": -1, "_id": -1}},
        {"$group": {"_id": "$instrument_id", "publication": {"$first": "$$ROOT"}}},
        {"$sort": {"_id": 1}},
    ]


def _acquired_publications(db):
    return db.acquisition_jobs.aggregate(_acquired_publication_pipeline())


def _acquired_metadata_pipeline():
    # Join only the chosen generation. No histories, caches or reader leases are
    # needed to select a page; payload hydration still runs inside reader().
    pipeline = _acquired_publication_pipeline()
    pipeline.insert(1, {"$project": {"instrument_id": 1, "listing": 1, "requested_at": 1,
                                     "published_at": 1, "history_summary": 1}})
    return pipeline + [
        {"$project": {"publication._id": 1, "publication.listing": 1, "publication.history_summary": 1}},
        {"$lookup": {"from": "instruments", "localField": "publication._id",
                     "foreignField": "job_id", "as": "profiles"}},
        {"$project": {"listing": "$publication.listing", "job_id": "$publication._id",
                      "history_summary": "$publication.history_summary", "profiles": {"$filter": {"input": "$profiles", "as": "profile", "cond": {"$and": [
                          {"$eq": ["$$profile.instrument_id", "$_id"]},
                          {"$eq": ["$$profile.provider", "yfinance"]},
                          {"$eq": ["$$profile.symbol", "$publication.listing.symbol"]},
                          {"$eq": ["$$profile.exchange", "$publication.listing.exchange"]},
                      ]}}}}},
        {"$project": {"listing": 1, "job_id": 1, "history_summary": 1, "profile_count": {"$size": "$profiles"},
                      "collected_at": {"$arrayElemAt": ["$profiles.collected_at", 0]}}},
    ]


def _acquired_page(db, limit, offset):
    result = next(db.acquisition_jobs.aggregate(_acquired_metadata_pipeline() + [
        {"$facet": {
            "count": [{"$count": "total"}],
            "invalid": [{"$match": {"profile_count": {"$ne": 1}}}, {"$limit": 1}],
            "page": [{"$sort": {"collected_at": -1, "_id": 1}}, {"$skip": offset}, {"$limit": limit}],
        }},
    ]))
    if result["invalid"]:
        raise DataError(409, "The latest Yahoo profile is unavailable. Request Yahoo history again.")
    return (result["count"][0]["total"] if result["count"] else 0), result["page"]


def _acquired_catalogues(db, observations):
    catalogues = {}
    command = lambda batch: {"find": "etfs", "filter": {"isin": {"$in": [row["_id"] for row in batch]}}}
    for batch in _history_batches(observations, command):
        for record in db.etfs.find({"isin": {"$in": [row["_id"] for row in batch]}},
                                   {"isin": 1, "name": 1, "ticker": 1, "import_date": 1}).sort("import_date", DESCENDING):
            catalogues.setdefault(record["isin"], record)
    return catalogues


def acquired_universe(db):
    """Capture compact published metadata, without leasing any quote payloads."""
    observations = list(db.acquisition_jobs.aggregate(_acquired_metadata_pipeline()))
    if any(row["profile_count"] != 1 for row in observations):
        raise DataError(409, "The latest Yahoo profile is unavailable. Request Yahoo history again.")
    catalogues = _acquired_catalogues(db, observations)
    # Metadata has no payload lifetime to protect. Revalidate the captured
    # identities in one read instead of acquiring N write-backed payload leases.
    current = {row["_id"]: row["publication"] for row in db.acquisition_jobs.aggregate(
        _acquired_publication_pipeline() + [{"$project": {
            "publication._id": 1, "publication.listing": 1, "publication.retention_state": 1,
        }}])}
    if any(current.get(row["_id"], {}).get("_id") != row["job_id"]
           or current[row["_id"]].get("listing") != row["listing"]
           or current[row["_id"]].get("retention_state") in ("retiring", "pruned") for row in observations):
        from market_data_retention import conflict
        raise conflict()
    items = [{"instrument_id": row["_id"], "name": catalogues.get(row["_id"], {}).get("name"),
              "ticker": catalogues.get(row["_id"], {}).get("ticker"), "collected_at": row.get("collected_at")}
             for row in observations]
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


def history_bounds_pipeline(seeds):
    """Two generation-pinned, index-ordered endpoint probes per candidate."""
    if not seeds:
        raise ValueError("History endpoint probes require a nonempty batch")
    if any(seed["generation"] in (None, "legacy") or seed.get("provider") != "yfinance" for seed in seeds):
        raise DataError(409, "History bounds require the latest published Yahoo generation.")
    comparisons = [{"$eq": [f"${field}", f"$${field}"]}
                   for field in ("instrument_id", "symbol", "exchange")]
    comparisons += [{"$eq": ["$job_id", "$$generation"]}, {"$eq": ["$provider", "$$provider"]}]
    match = {"$expr": {"$and": comparisons}}
    if "date_to" in seeds[0]:
        match["date"] = {"$lte": seeds[0]["date_to"]}
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


def saved_history_bounds(db, observations, as_of=None, *, leases_held=False):
    """Probe exact endpoints under the caller's leases, or acquire a batch lease."""
    from contextlib import nullcontext
    from market_data_retention import reader_many

    cutoff = aware(as_of) if as_of is not None else utcnow()
    bounds = {}
    selected = [row for row in observations if row["listing"]]
    with nullcontext() if leases_held else reader_many(db, selected):
        seeds = [{"instrument_id": row["_id"], **row["listing"], "generation": row["job_id"],
                  "provider": "yfinance", "date_to": cutoff.date().isoformat()} for row in selected]
        command = lambda batch: {"aggregate": "instruments", "pipeline": history_bounds_pipeline(batch), "cursor": {}}
        for batch in _history_batches(seeds, command):
            records = list(db.instruments.aggregate(history_bounds_pipeline(batch)))
            if len(records) != len(batch) or {row["instrument_id"] for row in records} != {seed["instrument_id"] for seed in batch}:
                raise DataError(409, "Saved instrument inventory changed during filtering. Refresh the acquired list.")
            for row in records:
                bounds[row["instrument_id"]] = row
        return bounds


def published_history_summary(observation, cutoff):
    """Only writer-produced, fully covered immutable generations can skip probes."""
    summary = observation.get("history_summary")
    if not isinstance(summary, dict):
        return None
    try:
        for field in ("first_date", "last_date"):
            if date.fromisoformat(summary[field]).isoformat() != summary[field]:
                return None
    except (KeyError, TypeError, ValueError):
        return None
    if (summary.get("version") == 1 and summary.get("instrument_id") == observation["_id"]
            and summary.get("listing") == observation["listing"] and summary.get("generation") == observation["job_id"]
            and type(summary.get("quote_count")) is int and summary["quote_count"] > 0
            and type(summary.get("observations")) is int and summary["observations"] == summary["quote_count"]
            and isinstance(summary.get("first_date"), str) and isinstance(summary.get("last_date"), str)
            and summary["first_date"] <= summary["last_date"] <= cutoff.date().isoformat()):
        return summary
    return None


def _nonnegative_returns(statistics):
    for field in ("historical_year_return", "annualized_mean_log_return"):
        value = statistics.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        try:
            if not math.isfinite(value) or value < 0:
                return False
        except OverflowError:
            return False
    return True


def acquired_instruments(db, limit=20, offset=0, min_history_years=None, *, hide_negative_returns=False):
    """Latest complete Yahoo profiles with generation-consistent history/returns."""
    from contextlib import ExitStack
    from market_data_retention import reader, reader_many
    from return_statistics import acquired_statistics

    cutoff = utcnow()
    started = phase = perf_counter()
    timings = {}

    def mark(name):
        nonlocal phase
        now = perf_counter()
        timings[name] = round((now - phase) * 1000, 2)
        phase = now

    observations, page_statistics = [], {}
    with ExitStack() as readers:
        selected = []
        unfiltered = min_history_years is None and not hide_negative_returns
        total = None
        if unfiltered:
            total, candidates = _acquired_page(db, limit, offset)
            publications = ({"_id": row["_id"], "publication": {"_id": row["job_id"], "listing": row["listing"],
                                                             "history_summary": row.get("history_summary")}}
                            for row in candidates)
        else:
            publications = _acquired_publications(db)
        for row in publications:
            job = row["publication"]
            generation = (readers.enter_context(reader(db, row["_id"], job["listing"], job["_id"]))
                          if unfiltered else job["_id"])
            selected.append({"_id": row["_id"], "listing": job["listing"], "job_id": generation,
                             "provider": "yfinance", "history_summary": job.get("history_summary")})
        if not unfiltered:
            readers.enter_context(reader_many(db, selected))
        mark("publications_and_leases_ms")
        identity = lambda row: {**listing_filter(row["_id"], row["listing"]), "provider": "yfinance", "job_id": row["job_id"]}
        command = lambda batch: {"find": "instruments", "filter": {"$or": [identity(row) for row in batch]}}
        for batch in _history_batches(selected, command):
            profiles = {profile["instrument_id"]: profile for profile in db.instruments.find(
                {"$or": [identity(row) for row in batch]},
                {"instrument_id": 1, "collected_at": 1, "asset_class": 1, "index": 1})}
            for row in batch:
                profile = profiles.get(row["_id"])
                if profile is None:
                    raise DataError(409, "The latest Yahoo profile is unavailable. Request Yahoo history again.")
                observations.append({**row, **{key: profile.get(key) for key in ("collected_at", "asset_class", "index")}})
        mark("profiles_ms")
        inventory_count = len(observations)
        observations.sort(key=lambda row: row["_id"])
        observations.sort(key=lambda row: aware(row["collected_at"]), reverse=True)
        if min_history_years is not None:
            bounds = {row["_id"]: summary for row in observations
                      if (summary := published_history_summary(row, cutoff)) is not None}
            bounds.update(saved_history_bounds(db, [row for row in observations if row["_id"] not in bounds], cutoff, leases_held=True))
            matching = []
            for observation in observations:
                span = bounds.get(observation["_id"], {})
                if not span.get("first_date") or not span.get("last_date"):
                    continue
                start, end = date.fromisoformat(span["first_date"]), date.fromisoformat(span["last_date"])
                try:
                    anniversary = start.replace(year=start.year + min_history_years)
                except ValueError:
                    anniversary = date(start.year + min_history_years, 2, 28)
                if end >= anniversary:
                    matching.append(observation)
            observations = matching
        mark("bounds_ms")
        if hide_negative_returns:
            page_statistics = acquired_statistics(db, observations, cutoff)
            observations = [row for row in observations
                            if page_statistics[row["_id"]][1].get("listing") == row["listing"]
                            and _nonnegative_returns(page_statistics[row["_id"]][0])]
        page = observations if unfiltered else observations[offset:offset + limit]
        if not hide_negative_returns:
            page_statistics = acquired_statistics(db, page, cutoff)
        mark("statistics_ms")
        catalogues = _acquired_catalogues(db, page)
        items = []
        for observation in page:
            isin = observation["_id"]
            catalogue = catalogues.get(isin, {})
            statistics, state = page_statistics[isin]
            saved_history = statistics.get("saved_history")
            if saved_history is None:
                # Compatibility for older/custom calculators; normal caches use
                # compact coverage, not a second full distinct-date response.
                dates = quote_dates(db, isin, observation["listing"], as_of=cutoff, generation=observation["job_id"])
                saved_history = {"quote_count": len(dates), "first_date": dates[0] if dates else None,
                                 "last_date": dates[-1] if dates else None}
            if state.get("listing") != observation["listing"]:
                statistics = {}
            items.append({
                "instrument_id": isin, "isin": isin,
                "name": catalogue.get("name"), "ticker": catalogue.get("ticker"),
                "listing": observation["listing"], "collected_at": observation["collected_at"],
                "asset_class": observation.get("asset_class"), "index": observation.get("index"),
                **saved_history,
                **{key: statistics.get(key) for key in (
                    "annualized_mean_log_return", "annualized_volatility", "historical_year_return",
                    "historical_year_start_date", "historical_year_end_date", "historical_year_price_basis",
                    "historical_year_carried_from_date", "historical_year_volatility",
                    "historical_year_volatility_start_date", "historical_year_volatility_end_date",
                    "historical_year_volatility_price_basis", "historical_year_volatility_return_count",
                )},
            })
        mark("page_hydration_ms")
        logging.getLogger(__name__).debug("acquired_filter phases_ms=%s inventory=%d matched=%d page=%d history_years=%s returns=%s total_ms=%.2f",
                                         timings, inventory_count, len(observations), len(items), min_history_years,
                                         hide_negative_returns, (perf_counter() - started) * 1000)
        return public({"items": items, "total": total if unfiltered else len(observations), "limit": limit, "offset": offset})


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
    return {"provider": "yfinance", "metered": False, "month": utcnow().strftime("%Y-%m"),
            "limit": None, "used": 0, "remaining": None}


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
    """Select the latest complete Yahoo vintage, never a knowledge-time snapshot.

    as_of remains a compatibility argument; only callers' quote-date predicates
    bound history. Retired job tombstones must not resurrect an older vintage.
    """
    publication = db.acquisition_jobs.find_one(
        {"instrument_id": isin, "provider": "yfinance", "published": True},
        sort=[("requested_at", DESCENDING), ("published_at", DESCENDING), ("_id", DESCENDING)],
    )
    saved_listing = {key: listing[key] for key in ("symbol", "exchange")} if listing else None
    if publication and publication.get("listing") != saved_listing:
        raise DataError(409, "The latest Yahoo listing changed. Refresh instrument details before reading history.")
    if generation is not None and (publication is None or generation != publication["_id"]):
        raise DataError(409, "This history generation is unavailable or superseded. Refresh instrument details and retry using the latest Yahoo history.")
    if publication and publication.get("retention_state") in ("retiring", "pruned"):
        raise DataError(409, "The latest Yahoo history generation is unavailable. Request Yahoo history again.")
    return publication


def history_generation(db, isin, listing, as_of=None):
    publication = published_history(db, isin, listing, as_of)
    return publication["_id"] if publication else None


def observation_filter(db, isin, listing, as_of=None, generation=None):
    publication = published_history(db, isin, listing, as_of, generation)
    query = listing_filter(isin, listing) if listing else {"instrument_id": isin}
    # Match nothing when Yahoo has never published; legacy evidence is not history.
    query.update(provider="yfinance", job_id=publication["_id"] if publication else {"$in": []})
    return query


def quote_dates(db, isin, listing, as_of=None, generation=None, *, publication_as_of=None):
    from market_data_retention import reader

    published_history(db, isin, listing, generation=generation)
    with reader(db, isin, listing, generation) as generation:
        if generation is None:
            return []
        query = observation_filter(db, isin, listing, generation=generation)
        if as_of:
            query["date"] = {"$lte": aware(as_of).date().isoformat()}
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
    from market_data_retention import reader

    catalogue = require_catalogue(db, isin)
    expire_stalled_jobs(db, isin)
    state = db.instrument_state.find_one({"_id": isin}) or {}
    listing = state.get("listing")
    cutoff = as_of or utcnow()
    with reader(db, isin, listing) as generation:
        profile = None
        if generation is not None:
            observation = db.instruments.find_one(observation_filter(db, isin, listing, generation=generation))
            if observation:
                profile = {key: observation.get(key) for key in ("collected_at", "collection_date", "data", "asset_class", "index")}
        job = db.acquisition_jobs.find_one({"instrument_id": isin}, sort=[("requested_at", DESCENDING), ("_id", DESCENDING)])
        summary = coverage(catalogue, quote_dates(db, isin, listing, cutoff, generation))
        if generation is None:
            summary.update(history_status="not_requested" if job is None else "empty",
                           warning="No published Yahoo history is available. Request Yahoo acquisition before reading history.")
        return public({"instrument_id": isin, "generation": generation, "catalogue": {key: value for key, value in catalogue.items() if key != "_id"}, "listing": listing, "listing_resolution": state.get("listing_resolution"), "price_currency_override": state.get("price_currency_override"), "profile": profile, "acquisition": public_job(job), "summary": summary, "provider": provider_name(settings), "price_basis": "provider_split_adjusted", "provider_configured": configured(settings), "budget": None})


def read_quotes(db, isin, limit=1000, offset=0, as_of=None, generation=None):
    from market_data_retention import reader

    require_market_catalogue(db, isin)
    as_of = as_of or utcnow()
    snapshot = public(as_of)
    state = db.instrument_state.find_one({"_id": isin}) or {}
    listing = state.get("listing")
    published_history(db, isin, listing, generation=generation)
    with reader(db, isin, listing, generation) as generation:
        if generation is None:
            return {"items": [], "total": 0, "limit": limit, "offset": offset, "as_of": snapshot, "generation": None,
                    "warning": "No published Yahoo history is available. Request Yahoo acquisition first."}
        query = observation_filter(db, isin, listing, generation=generation)
        query["date"] = {"$lte": aware(as_of).date().isoformat()}
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
    if name != "yfinance":
        raise DataError(422, "Marketstack is retired. Request a Yahoo acquisition instead.")
    ensure_indexes(db)
    expire_stalled_jobs(db, isin)
    requested_listing = {"symbol": listing["symbol"], **({"exchange": listing["exchange"]} if listing.get("exchange") else {})} if listing else None
    if requested_listing is not None and not requested_listing.get("exchange") and name != "yfinance":
        raise DataError(422, "Supply both symbol and exchange for a manual listing override with this provider")
    state = db.instrument_state.find_one({"_id": isin}) or {}
    if not db.acquisition_jobs.find_one({"instrument_id": isin, "provider": "yfinance", "published": True}):
        # A retired provider's cached mapping is not a Yahoo symbol identity.
        state = {key: value for key, value in state.items() if key not in ("listing", "listing_resolution", "last_complete_quote_date")}
    listing = requested_listing if complete_listing(requested_listing) else None
    if requested_listing is None:
        listing = state.get("listing")
    existing = db.acquisition_jobs.find_one({"instrument_id": isin, "active": True})
    if existing:
        if existing.get("status") in ("deleting", "maintenance"):
            raise DataError(409, "This instrument is undergoing removal or retention maintenance. Retry after it finishes.")
        existing_request = existing.get("requested_listing") or existing.get("listing")
        if requested_listing is not None and not same_request(existing_request, requested_listing):
            raise DataError(409, "Another listing is already being collected for this instrument")
        return public_job(existing)
    ticker = catalogue.get("ticker")
    ticker = ticker.strip().upper() if isinstance(ticker, str) else ""
    lookup_ticker = requested_listing["symbol"] if requested_listing and not requested_listing.get("exchange") else ticker
    if not listing and requested_listing is None and (len(ticker) > 64 or not re.fullmatch(r"[A-Z0-9^][A-Z0-9._^=\-]*", ticker)):
        hint = "Supply a manual Yahoo symbol." if name == "yfinance" else "Supply a manual symbol and MIC."
        raise DataError(422, f"This instrument has no valid catalogue ticker. {hint}")
    state_listing = state.get("listing")
    if state_listing and db.quotes.count_documents({"instrument_id": isin, "provider": "yfinance"}, limit=1):
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
    # Request the inclusive current UTC day; the provider returns no bar when
    # the market has not closed yet.
    collection_date = utcnow().date()
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
        if existing is None or existing.get("status") in ("deleting", "maintenance"):
            raise DataError(409, "Acquisition state changed; please retry") from None
        existing_request = existing.get("requested_listing") or existing.get("listing")
        if requested_listing is not None and not same_request(existing_request, requested_listing):
            raise DataError(409, "Another listing is already being collected for this instrument") from None
        return public_job(existing)
    try:
        if job.get("base_generation"):
            # Selection preceded the unique gate. Revalidate after insertion so
            # maintenance cannot prune a base between selection and ownership.
            current_base = published_history(db, isin, listing, generation=job["base_generation"])
            if current_base is None:
                raise DataError(409, "History changed during acquisition setup. Refresh and retry.")
        if listing and name != "yfinance":
            change = {"listing": listing}
            if not same_listing:
                change.update(last_complete_quote_date=None, listing_resolution=None)
            db.instrument_state.update_one({"_id": isin}, {"$set": change}, upsert=True)
        publish(settings, job_id)
    except DataError:
        db.acquisition_jobs.update_one({"_id": job_id, "status": "queued"}, {"$set": {
            "active": False, "status": "failed", "phase": "failed", "finished_at": utcnow(),
            "error": "History changed during acquisition setup. Refresh and retry.",
        }})
        raise
    except Exception:
        db.acquisition_jobs.update_one({"_id": job_id, "status": "queued"}, {"$set": {"active": False, "status": "failed", "phase": "failed", "finished_at": utcnow(), "error": "Could not queue acquisition. Check RabbitMQ and try again."}})
        raise DataError(503, "Could not queue acquisition. Check RabbitMQ and try again.") from None
    return public_job(job)


def run_acquisition(db, settings, job_id, provider_factory=None):
    """Run/resume a job. Only a fully processed range advances the watermark."""
    ensure_indexes(db)
    # Reject pre-routing/retired deliveries before constructing any provider.
    saved = db.acquisition_jobs.find_one({"_id": job_id})
    if saved and saved.get("provider") != "yfinance":
        db.acquisition_jobs.update_one({"_id": job_id}, {"$set": {
            "active": False, "status": "retired", "phase": "retired",
            "error": "Marketstack is retired. Request Yahoo history instead.",
        }})
        return public_job(db.acquisition_jobs.find_one({"_id": job_id}))
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
    name = "yfinance"

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
        cap = settings.market_data_max_calls_per_job
        if current["calls"] >= cap:
            raise MarketDataError("job_request_limit", "This job reached its request safety limit. Its unfinished range was not marked complete.")
        wait_for_yahoo_request(db, settings.yahoo_request_interval_seconds, checkpoint)
        checkpoint(increments={"calls": 1})

    try:
        listing = job["listing"]
        isin = job["instrument_id"]
        requested_listing = job.get("requested_listing")
        client = (provider_factory("", before_request=before_request)
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
                    saved_yahoo = db.acquisition_jobs.find_one({"instrument_id": isin, "provider": "yfinance", "published": True})
                    listing = state.get("listing") if saved_yahoo else None
                    if not listing:
                        if name == "yfinance":
                            listing, resolution = provider.resolve_listing(job["lookup_ticker"], isin)
                            resolution = {**resolution, "collected_at": utcnow()}
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
                if state_listing and state_listing != listing and db.quotes.count_documents({"instrument_id": isin, "provider": "yfinance"}, limit=1):
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
            summary = list(db.quotes.aggregate([
                {"$match": {**listing_filter(isin, listing), "provider": name, "job_id": job_id}},
                {"$group": {"_id": "$date", "observations": {"$sum": 1}}},
                {"$group": {"_id": None, "observations": {"$sum": "$observations"}, "quote_count": {"$sum": 1},
                            "first_date": {"$min": "$_id"}, "last_date": {"$max": "$_id"}}},
            ]))[0]
            summary.pop("_id")
            job = checkpoint({"published": True, "published_at": publication["published_at"],
                              "history_summary": {"version": 1, "instrument_id": isin, "listing": listing,
                                                  "generation": job_id, **summary}})
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
