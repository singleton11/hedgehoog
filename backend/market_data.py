"""Durable, explicitly requested Marketstack observations and acquisition jobs.

No provider calls occur in read functions. Collection times are knowledge times,
not historical effective dates. At-least-once execution is not exactly-once HTTP.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import re
from uuid import uuid4

from bson import Decimal128, ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from marketstack import MarketstackClient, MarketstackError

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


def latest_catalogue(db, isin):
    return db.etfs.find_one({"isin": isin}, sort=[("import_date", DESCENDING)])


def require_catalogue(db, isin):
    record = latest_catalogue(db, isin)
    if record is None:
        raise DataError(404, "Instrument not found in the ETF catalogue")
    return record


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


def acquired_instruments(db, limit=20, offset=0):
    """One row per saved ETF profile, including partial/failed acquisitions.

    The worker saves tickerinfo before any EOD pages, so profiles are the
    acquisition inventory. A queued job or cached listing alone is not data.
    Paginate lightweight observations before fetching catalogue/quote summaries
    and refreshing cached return statistics from saved prices only.
    """
    from return_statistics import refresh_statistics

    result = list(db.instruments.aggregate([
        {"$project": {"instrument_id": 1, "symbol": 1, "exchange": 1, "collected_at": 1}},
        {"$sort": {"instrument_id": 1, "collected_at": -1, "_id": -1}},
        {"$group": {
            "_id": "$instrument_id",
            "listing": {"$first": {"symbol": "$symbol", "exchange": "$exchange"}},
            "collected_at": {"$first": "$collected_at"},
        }},
        {"$sort": {"collected_at": -1, "_id": 1}},
        {"$facet": {"count": [{"$count": "total"}], "items": [{"$skip": offset}, {"$limit": limit}]}},
    ]))[0]
    items = []
    for observation in result["items"]:
        isin = observation["_id"]
        catalogue = db.etfs.find_one(
            {"isin": isin}, {"_id": 0, "name": 1, "ticker": 1},
            sort=[("import_date", DESCENDING)],
        ) or {}
        dates = quote_dates(db, isin, observation["listing"])
        statistics, state = refresh_statistics(db, isin)
        # Never attach another listing's returns to the displayed profile.
        if state.get("listing") != observation["listing"]:
            statistics = {}
        items.append({
            "instrument_id": isin, "isin": isin,
            "name": catalogue.get("name"), "ticker": catalogue.get("ticker"),
            "listing": observation["listing"], "collected_at": observation["collected_at"],
            "quote_count": len(dates), "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None,
            "annualized_mean_log_return": statistics.get("annualized_mean_log_return"),
            "annualized_volatility": statistics.get("annualized_volatility"),
            "historical_year_return": statistics.get("historical_year_return"),
            "historical_year_start_date": statistics.get("historical_year_start_date"),
            "historical_year_end_date": statistics.get("historical_year_end_date"),
            "historical_year_price_basis": statistics.get("historical_year_price_basis"),
        })
    return public({"items": items, "total": result["count"][0]["total"] if result["count"] else 0, "limit": limit, "offset": offset})


def budget_status(db, settings):
    month = utcnow().strftime("%Y-%m")
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


def listing_filter(isin, listing):
    return {"instrument_id": isin, "symbol": listing["symbol"], "exchange": listing["exchange"]}


def quote_dates(db, isin, listing, as_of=None):
    if not listing:
        return []
    query = listing_filter(isin, listing)
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
            "Marketstack may silently limit history to your plan or listing coverage. Normal updates only fetch newer dates; use Retry Older History after checking or upgrading your plan."
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
    profile = None
    if listing:
        query = listing_filter(isin, listing)
        if as_of:
            query["collected_at"] = {"$lte": as_of}
        observation = db.instruments.find_one(query, sort=[("collected_at", DESCENDING), ("_id", DESCENDING)])
        if observation:
            profile = {key: observation[key] for key in ("collected_at", "collection_date", "data")}
    job = db.acquisition_jobs.find_one({"instrument_id": isin}, sort=[("requested_at", DESCENDING), ("_id", DESCENDING)])
    summary = coverage(catalogue, quote_dates(db, isin, listing, as_of))
    if job is None:
        summary["history_status"] = "not_requested"
    return public({"instrument_id": isin, "catalogue": {key: value for key, value in catalogue.items() if key != "_id"}, "listing": listing, "listing_resolution": state.get("listing_resolution"), "price_currency_override": state.get("price_currency_override"), "profile": profile, "acquisition": public_job(job), "summary": summary, "provider_configured": bool(settings.marketstack_api_key.get_secret_value()), "budget": budget_status(db, settings)})


def read_quotes(db, isin, limit=1000, offset=0, as_of=None):
    require_catalogue(db, isin)
    as_of = as_of or utcnow()
    snapshot = public(as_of)
    state = db.instrument_state.find_one({"_id": isin}) or {}
    listing = state.get("listing")
    if not listing:
        return {"items": [], "total": 0, "limit": limit, "offset": offset, "as_of": snapshot}
    query = listing_filter(isin, listing)
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
    return {"items": items, "total": result["count"][0]["total"] if result["count"] else 0, "limit": limit, "offset": offset, "as_of": snapshot}


def request_acquisition(db, settings, isin, listing, mode, requested_by, publish):
    catalogue = require_catalogue(db, isin)
    try:
        inception = date.fromisoformat(catalogue["inception_date"])
    except (KeyError, TypeError, ValueError):
        raise DataError(422, "This instrument has no valid inception date for a history request") from None
    if inception > utcnow().date():
        raise DataError(422, "The instrument inception date is in the future")
    if not settings.marketstack_api_key.get_secret_value():
        raise DataError(503, "Configure MARKETSTACK_API_KEY on the API and worker before requesting data")
    ensure_indexes(db)
    expire_stalled_jobs(db, isin)
    explicit_listing = listing
    state = db.instrument_state.find_one({"_id": isin}) or {}
    listing = listing or state.get("listing")
    existing = db.acquisition_jobs.find_one({"instrument_id": isin, "active": True})
    if existing:
        if explicit_listing is not None and existing["listing"] != explicit_listing:
            raise DataError(409, "Another listing is already being collected for this instrument")
        return public_job(existing)
    if budget_status(db, settings)["remaining"] < 1:
        raise DataError(409, "The local monthly Marketstack request budget is exhausted")
    ticker = catalogue.get("ticker")
    ticker = ticker.strip().upper() if isinstance(ticker, str) else ""
    if not listing and (len(ticker) > 64 or not re.fullmatch(r"[A-Z0-9^][A-Z0-9._^=\-]*", ticker)):
        raise DataError(422, "This instrument has no valid catalogue ticker. Supply a manual symbol and MIC.")
    if state.get("listing") and state["listing"] != listing and db.quotes.count_documents({"instrument_id": isin}, limit=1):
        raise DataError(409, "A listing with stored quotes cannot be changed or mixed with another listing")
    same_listing = state.get("listing") == listing
    last_complete = state.get("last_complete_quote_date") if same_listing else None
    dates = quote_dates(db, isin, listing)
    end = utcnow().date() - timedelta(days=1)
    start = inception
    actual_mode = "initial"
    if mode == "backfill" and dates:
        actual_mode = "backfill"
        end = date.fromisoformat(dates[0]) - timedelta(days=1)
    elif last_complete:
        actual_mode = "update"
        start = date.fromisoformat(last_complete) + timedelta(days=1)
    job_id = str(uuid4())
    job = {
        "_id": job_id, "instrument_id": isin, "listing": listing, "lookup_ticker": ticker,
        "requested_by": requested_by, "requested_at": utcnow(), "mode": actual_mode,
        "date_from": start.isoformat(), "date_to": end.isoformat(),
        "status": "queued", "active": True, "phase": "queued", "pages": 0, "rows": 0, "flagged_rows": 0, "unindexed_rows": 0, "calls": 0,
        "next_offset": 0, "expected_total": None, "started_at": None, "finished_at": None,
        "error": None, "warning": None,
    }
    try:
        db.acquisition_jobs.insert_one(job)
    except DuplicateKeyError:
        existing = db.acquisition_jobs.find_one({"instrument_id": isin, "active": True})
        if existing is None:
            raise DataError(409, "Acquisition state changed; please retry") from None
        if explicit_listing is not None and existing["listing"] != explicit_listing:
            raise DataError(409, "Another listing is already being collected for this instrument") from None
        return public_job(existing)
    try:
        if listing:
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


def run_acquisition(db, settings, job_id, provider_factory=MarketstackClient):
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
        if current["calls"] >= settings.marketstack_max_calls_per_job:
            raise MarketstackError("job_request_limit", "This job reached its request safety limit. Its unfinished range was not marked complete.")
        reserve_credit(db, settings)
        checkpoint(increments={"calls": 1})

    try:
        if not settings.marketstack_api_key.get_secret_value():
            raise MarketstackError("not_configured", "The worker has no MARKETSTACK_API_KEY configured.")
        listing = job["listing"]
        isin = job["instrument_id"]
        with provider_factory(settings.marketstack_api_key.get_secret_value(), before_request=before_request) as provider:
            if not listing:
                checkpoint({"phase": "resolving_listing"})
                # A DB interruption can leave a cached mapping before the job
                # checkpoint. Reuse it on redelivery rather than paying again.
                state = db.instrument_state.find_one({"_id": isin}) or {}
                listing = state.get("listing")
                if not listing:
                    listing, resolution = resolve_listing(provider, job["lookup_ticker"], isin)
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
                job = checkpoint({"listing": listing})
            checkpoint({"phase": "tickerinfo"})
            if db.instruments.find_one({"job_id": job_id}) is None:
                data = provider.tickerinfo(listing["symbol"])
                collected = utcnow()
                checkpoint()
                db.instruments.update_one({"job_id": job_id}, {"$setOnInsert": {
                    "instrument_id": isin, **listing, "provider": "marketstack", "job_id": job_id,
                    "collected_at": collected, "collection_date": collected.date().isoformat(), "data": data,
                }}, upsert=True)
            job = checkpoint({"phase": "quotes"})
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
                            "instrument_id": isin, **listing, "provider": "marketstack",
                            "job_id": job_id, "offset": offset, "collected_at": collected,
                            "collection_date": collected.date().isoformat(), "data": page,
                        }}, upsert=True, return_document=ReturnDocument.AFTER,
                    )
                # Retry indexing the original saved response, not a new paid
                # request. All rows/fields are retained before any projection.
                page, collected = raw["data"], raw["collected_at"]
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
                        "instrument_id": isin, **listing, "provider": "marketstack", "job_id": job_id,
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
        # Derived analytics never rewrite provider evidence. Persist them before
        # reporting success; DB retries replay saved pages without provider calls.
        from return_statistics import refresh_statistics
        checkpoint({"phase": "statistics"})
        refresh_statistics(db, isin, force=True)
        # Check ownership after all writes and before committing the range watermark.
        checkpoint({"phase": "finishing"})
        state = db.instrument_state.find_one({"_id": isin}) or {}
        watermark = state.get("last_complete_quote_date")
        if latest_date and (watermark is None or latest_date > watermark):
            # This write is last, never on failed/incomplete pagination. It does not
            # advance to date_to when the provider is empty or publishes late.
            db.instrument_state.update_one({"_id": isin, "listing": listing}, {"$set": {"last_complete_quote_date": latest_date}})
        summary = coverage(require_catalogue(db, isin), quote_dates(db, isin, listing))
        warning = summary["warning"]
        if job["rows"] == 0:
            warning = "Marketstack returned no quotes for this range. No dates were marked as received." + (" " + warning if warning else "")
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
