from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import math
from statistics import mean, variance
from unittest.mock import Mock

import mongomock
import pytest
from fastapi.testclient import TestClient
from pymongo.errors import ConnectionFailure, DuplicateKeyError

import market_data as md
from config import Settings
from main import create_app
from market_provider import MarketDataError
from marketstack import MarketstackError

ISIN = "IE00B5BMR087"
LISTING = {"symbol": "SXR8.DE", "exchange": "XETR"}
NOW = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)


def bar(day="2026-09-15", close="101.1234567890123456789"):
    return {"date": day + "T00:00:00+0000", "symbol": "SXR8.DE", "exchange": "XETR", "open": "100", "high": "102", "low": "99", "close": close, "volume": None, "adj_open": None, "adj_high": None, "adj_low": None, "adj_close": close, "adj_volume": None, "price_currency": None, "future_field": {"preserved": True}}


def page(rows=None, total=None, offset=0):
    rows = [bar()] if rows is None else rows
    return {"pagination": {"limit": 1000, "offset": offset, "count": len(rows), "total": len(rows) if total is None else total}, "data": rows}


class Provider:
    """Synthetic Yahoo chart/profile adapter; never constructs a network client."""
    def __init__(self, pages=None, profile=None, splits=None):
        self.pages = iter(pages or [page()])
        self.splits = iter(splits if splits is not None else [page([])])
        self.profile = profile if profile is not None else {"ticker": "SXR8.DE", "name": "Example", "address": {"city": ""}, "stock_exchanges": [], "future": {"value": None}}
        self.calls = []

    def factory(self, key, before_request):
        assert key == ""
        self.before_request = before_request
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def tickerinfo(self, symbol):
        self.before_request()
        self.calls.append(("tickerinfo", symbol))
        return deepcopy(self.profile)

    def splits_page(self, symbol, date_from, date_to, offset=0):
        self.before_request()
        self.calls.append(("splits", symbol, date_from, date_to, offset))
        result = next(self.splits)
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)

    def eod_page(self, symbol, exchange, date_from, date_to, offset=0):
        self.before_request()
        self.calls.append(("eod", symbol, exchange, date_from, date_to, offset))
        result = next(self.pages)
        if isinstance(result, Exception):
            raise result
        result = deepcopy(result)
        # Yahoo supplies split actions with the chart, not a separate request.
        if "raw" not in result:
            actions = next(self.splits, page([]))
            if isinstance(actions, Exception):
                result["raw"] = {"chart": {"error": {"code": "Synthetic action failure"}}}
            else:
                events = {}
                for index, event in enumerate(actions["data"]):
                    timestamp = int(datetime.fromisoformat(event["date"]).replace(tzinfo=timezone.utc).timestamp())
                    events[str(index)] = {"date": timestamp, "numerator": event["split_factor"], "denominator": "1"}
                result["raw"] = {"chart": {"result": [{"meta": {"symbol": symbol, "exchangeTimezoneName": "UTC"}, "events": {"splits": events}}]}}
        return result


@pytest.fixture
def clock(monkeypatch):
    current = [NOW]
    monkeypatch.setattr(md, "utcnow", lambda: current[0])
    return current


@pytest.fixture
def config():
    return Settings(_env_file=None, market_data_provider="yfinance", jwt_secret="test-secret-" * 5)


@pytest.fixture
def db(clock, monkeypatch):
    db = mongomock.MongoClient().market_data_test
    # mongomock erroneously revalidates all rows (ignoring partialFilterExpression)
    # when an existing partial index is recreated. Match Mongo's idempotent call.
    create_index = db.acquisition_jobs.create_index

    def idempotent_index(keys, **kwargs):
        name = kwargs.get("name")
        existing = db.acquisition_jobs.index_information().get(name)
        if existing is not None:
            assert existing["unique"] == kwargs["unique"]
            assert existing["partialFilterExpression"] == kwargs["partialFilterExpression"]
            return name
        return create_index(keys, **kwargs)

    monkeypatch.setattr(db.acquisition_jobs, "create_index", idempotent_index)
    db.etfs.insert_many([
        {"isin": ISIN, "ticker": "OLD", "name": "Obsolete title", "inception_date": "2010-05-19"},
        {"isin": ISIN, "ticker": "SXR8", "name": "iShares Core S&P 500 UCITS ETF", "inception_date": "2010-05-19", "import_date": "2026-09-17", "currency": "USD", "holdings": [{"isin": "US0378331005"}]},
        {"isin": "IE00B4L5Y983", "ticker": "EUNL", "name": "iShares Core MSCI World", "inception_date": "2009-09-25", "import_date": "2026-09-17"},
    ])
    return db


def enqueue(db, config, mode="update", listing=None, publish=None):
    return md.request_acquisition(db, config, ISIN, listing or LISTING, mode, "user-id", publish or Mock())


def collect(db, config, provider=None):
    job = enqueue(db, config)
    source = provider or Provider()
    result = md.run_acquisition(db, config, job["id"], source.factory)
    return result, source


def seed_yahoo(db, isin=ISIN, generation=None, listing=None, collected_at=NOW, rows=(), profile=None):
    """Publish an explicit synthetic Yahoo generation, never legacy fallback data."""
    generation = generation or isin
    listing = listing or LISTING
    db.instrument_state.update_one({"_id": isin}, {"$set": {"listing": listing}}, upsert=True)
    db.acquisition_jobs.insert_one({"_id": generation, "instrument_id": isin, "listing": listing,
                                    "provider": "yfinance", "published": True, "status": "succeeded",
                                    "active": False, "requested_at": collected_at, "published_at": collected_at})
    db.instruments.insert_one({"instrument_id": isin, **listing, "provider": "yfinance", "published": True,
                               "job_id": generation, "collected_at": collected_at, "data": profile or {}})
    db.split_snapshots.insert_one({"_id": generation, "instrument_id": isin, **listing, "job_id": generation,
                                   "provider": "yfinance", "collected_at": collected_at, "events": []})
    for row in rows:
        db.quotes.insert_one({"instrument_id": isin, **listing, "provider": "yfinance", "job_id": generation,
                              "collected_at": collected_at, "collection_date": collected_at.date().isoformat(),
                              "date": row["date"][:10],
                              "data": {**row, "price_basis": "provider_split_adjusted"}})
    return generation


@pytest.mark.parametrize("metadata", [
    {"asset_class": "equity", "index": "S&P 500"},
    {"asset_class": "commodities", "index": "Bloomberg Commodity"},
    {},
])
def test_acquisition_snapshots_latest_catalogue_metadata(db, config, metadata):
    db.etfs.update_one(
        {"isin": ISIN, "import_date": {"$exists": False}},
        {"$set": {"asset_class": "obsolete", "index": "Obsolete index"}},
    )
    db.etfs.update_one({"isin": ISIN, "import_date": "2026-09-17"}, {"$set": metadata or {"currency": "USD"}})
    result, source = collect(db, config)
    snapshot = db.instruments.find_one({"job_id": result["id"]})
    assert snapshot["asset_class"] == metadata.get("asset_class")
    assert snapshot["index"] == metadata.get("index")
    assert snapshot["data"] == source.profile
    row = md.acquired_instruments(db)["items"][0]
    profile = md.instrument_detail(db, config, ISIN)["profile"]
    for key in ("asset_class", "index"):
        assert row[key] == metadata.get(key)
        assert profile[key] == metadata.get(key)

    db.etfs.update_one({"isin": ISIN, "import_date": "2026-09-17"}, {"$set": {"index": "Changed index"}})
    md.run_acquisition(db, config, result["id"], source.factory)
    assert db.instruments.find_one({"job_id": result["id"]}) == snapshot

    refresh = enqueue(db, config, mode="refresh")
    md.run_acquisition(db, config, refresh["id"], Provider().factory)
    assert db.instruments.find_one({"job_id": refresh["id"]})["index"] == "Changed index"
    assert db.instruments.find_one({"job_id": result["id"]}) == snapshot


def test_removal_deletes_market_data_only_and_allows_reacquisition(db, config):
    job, _ = collect(db, config)
    collections = ("instruments", "quotes", "raw_eod_pages", "raw_split_pages",
                   "split_snapshots", "market_data_publications", "manual_splits")
    for name in collections:
        db[name].insert_one({"instrument_id": "OTHER", "job_id": "other", "date": "other", "offset": 999})
    db.manual_splits.insert_one({"instrument_id": ISIN})
    db.customers.insert_one({"_id": "customer", "instrument_id": ISIN})
    db.marketstack_usage.insert_one({"_id": "sentinel", "used": 10})
    md.remove_acquired_instrument(db, ISIN)
    md.remove_acquired_instrument(db, ISIN)  # Retry is harmless.
    for name in collections:
        assert db[name].count_documents({"instrument_id": ISIN}) == 0
        assert db[name].count_documents({"instrument_id": "OTHER"}) == 1
    assert db.instrument_state.find_one({"_id": ISIN}) is None
    assert db.instrument_statistics.find_one({"_id": ISIN}) is None
    assert db.acquisition_jobs.count_documents({"instrument_id": ISIN}) == 0
    assert db.etfs.count_documents({"isin": ISIN}) == 2
    assert db.customers.find_one({"_id": "customer"})
    assert db.marketstack_usage.find_one({"_id": "sentinel"})["used"] == 10
    assert md.run_acquisition(db, config, job["id"]) is None
    assert enqueue(db, config)["status"] == "queued"


@pytest.mark.parametrize("status", ["queued", "running", "deleting"])
def test_removal_rejects_active_jobs_without_deleting(db, config, status):
    job = enqueue(db, config)
    db.acquisition_jobs.update_one({"_id": job["id"]}, {"$set": {"status": status}})
    db.instruments.insert_one({"instrument_id": ISIN, "job_id": "saved"})
    with pytest.raises(md.DataError) as error:
        md.remove_acquired_instrument(db, ISIN)
    assert error.value.status == 409
    assert db.instruments.count_documents({"instrument_id": ISIN}) == 1


def test_removal_mutex_blocks_acquisition_and_releases_on_failure(db, config, monkeypatch):
    def fail_delete(query):
        with pytest.raises(md.DataError) as error:
            enqueue(db, config)
        assert error.value.status == 409
        raise RuntimeError("storage unavailable")
    monkeypatch.setattr(db.quotes, "delete_many", fail_delete)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        md.remove_acquired_instrument(db, ISIN)
    assert not db.acquisition_jobs.find_one({"instrument_id": ISIN, "active": True})


def test_removal_route_requires_authentication_and_valid_identifier(api, db, config):
    client, _ = api
    assert client.delete(f"/instruments/{ISIN}").status_code == 401
    client.post("/auth/login", json={"login": "admin", "password": "admin"})
    assert client.delete("/instruments/invalid").status_code == 422
    assert client.delete("/instruments/IE0000000000").status_code == 404
    job = enqueue(db, config)
    assert client.delete(f"/instruments/{ISIN}").status_code == 409
    db.acquisition_jobs.update_one({"_id": job["id"]}, {"$set": {"active": False}})
    assert client.delete(f"/instruments/{ISIN}").status_code == 204
    assert client.delete(f"/instruments/{ISIN}").status_code == 204


def test_search_latest_snapshot_and_escaped_literals(db):
    result = md.search_catalogue(db, "sxR8", 15)
    assert len(result) == 1
    assert result[0]["instrument_id"] == ISIN
    assert result[0]["ticker"] == "SXR8"
    assert "_id" not in result[0]
    assert md.search_catalogue(db, "Obsolete", 15) == []
    assert md.search_catalogue(db, ".*", 15) == []
    assert len(md.search_catalogue(db, "iShares", 1)) == 1


def test_acquired_list_excludes_catalogue_only_cached_and_unsaved_jobs(db, config):
    empty = {"items": [], "total": 0, "limit": 20, "offset": 0}
    assert md.acquired_instruments(db) == empty
    job = enqueue(db, config)
    assert db.acquisition_jobs.find_one({"_id": job["id"]})["listing"] == LISTING
    assert db.instrument_state.find_one() is None  # No published history yet.
    assert md.acquired_instruments(db) == empty
    db.acquisition_jobs.update_one({"_id": job["id"]}, {"$set": {"status": "failed", "active": False}})
    assert md.acquired_instruments(db) == empty
    assert md.budget_status(db, config)["used"] == 0


def test_acquired_universe_is_complete_lightweight_and_uses_saved_profiles(db, monkeypatch):
    import return_statistics

    def unexpected_read(*args, **kwargs):
        pytest.fail("Universe loading must not read quotes or compute statistics")

    monkeypatch.setattr(md, "quote_dates", unexpected_read)
    monkeypatch.setattr(return_statistics, "refresh_statistics", unexpected_read)
    assert md.acquired_universe(db) == {"items": [], "total": 0}
    ids = [f"IE{index:010d}" for index in range(105)]
    for isin in reversed(ids):
        seed_yahoo(db, isin)
    seed_yahoo(db, ids[0], generation="newest", collected_at=NOW + timedelta(hours=1))
    seed_yahoo(db, ISIN)
    db.instruments.insert_many([
        {"instrument_id": "IE8888888888", "collected_at": NOW + timedelta(hours=1)},
        {"instrument_id": ids[0], "collected_at": NOW + timedelta(hours=2), "provider": "yfinance", "published": False},
        {"instrument_id": "IE9999999999", "provider": "yfinance", "published": False},
        {"instrument_id": "idx-test", "collected_at": NOW},
        {"instrument_id": ISIN, "collected_at": NOW, "provider": "yfinance", "published": True},
    ])
    db.etfs.insert_one({"isin": ISIN, "name": "Latest name", "ticker": "NEW", "import_date": "2026-09-18"})
    universe = md.acquired_universe(db)
    assert universe["total"] == len(ids) + 1 == len(universe["items"])
    assert [item["instrument_id"] for item in universe["items"]] == sorted(ids + [ISIN])
    assert universe["items"][0] == {
        "instrument_id": ids[0], "name": None, "ticker": None, "collected_at": "2026-09-17T13:00:00Z",
    }
    assert universe["items"][-1] == {
        "instrument_id": ISIN, "name": "Latest name", "ticker": "NEW", "collected_at": "2026-09-17T12:00:00Z",
    }
    # A later publication changes only a subsequent capture, never a paginated read.
    seed_yahoo(db, "IE9999999999")
    assert md.acquired_universe(db)["total"] == universe["total"] + 1
    assert len(universe["items"]) == len(ids) + 1


def test_acquired_universe_route_is_authenticated_and_db_only(api, db, config):
    client, publish = api
    assert client.get("/instruments/acquired/universe").status_code == 401
    client.post("/auth/login", json={"login": "admin", "password": "admin"})
    response = client.get("/instruments/acquired/universe")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}
    collect(db, config)
    publish.reset_mock()
    used = md.budget_status(db, config)["used"]
    response = client.get("/instruments/acquired/universe")
    assert response.status_code == 200
    assert response.json() == {"items": [{
        "instrument_id": ISIN, "name": "iShares Core S&P 500 UCITS ETF", "ticker": "SXR8", "collected_at": "2026-09-17T12:00:00Z",
    }], "total": 1}
    publish.assert_not_called()
    assert md.budget_status(db, config)["used"] == used


def test_acquired_list_excludes_failed_unpublished_profile_then_includes_complete_history(db, config):
    result, _ = collect(db, config, Provider([MarketDataError("transient", "Temporary failure")]))
    assert result["status"] == "failed"
    assert db.instruments.count_documents({"job_id": result["id"]}) == 1
    assert md.acquired_instruments(db) == {"items": [], "total": 0, "limit": 20, "offset": 0}
    complete, _ = collect(db, config)
    assert complete["status"] == "succeeded"
    page = md.acquired_instruments(db)
    assert page["total"] == 1
    assert page["items"] == [{
        "instrument_id": ISIN, "isin": ISIN, "name": "iShares Core S&P 500 UCITS ETF",
        "ticker": "SXR8", "listing": LISTING, "collected_at": "2026-09-17T12:00:00Z",
        "asset_class": None, "index": None,
        "quote_count": 1, "first_date": "2026-09-15", "last_date": "2026-09-15",
        "annualized_mean_log_return": None, "annualized_volatility": None,
        "historical_year_return": None, "historical_year_start_date": None,
        "historical_year_end_date": None, "historical_year_price_basis": None,
        "historical_year_carried_from_date": None,
        "historical_year_volatility": None, "historical_year_volatility_start_date": None,
        "historical_year_volatility_end_date": None, "historical_year_volatility_price_basis": None,
        "historical_year_volatility_return_count": None,
    }]


def test_acquired_list_deduplicates_profiles_and_quotes_and_paginates(db, config, clock):
    collect(db, config)
    clock[0] += timedelta(hours=1)
    collect(db, config, Provider([page([bar("2026-09-15"), bar("2026-09-16")])]))
    original = db.quotes.find_one({"date": "2026-09-15"})
    db.quotes.insert_one({**original, "_id": "revision", "job_id": "revision", "collected_at": clock[0]})
    db.quotes.insert_one({**original, "_id": "different-listing", "job_id": "different-listing", "symbol": "OTHER.L", "date": "2000-01-01"})
    db.etfs.insert_one({"isin": ISIN, "name": "Updated catalogue name", "ticker": "NEW", "import_date": "2026-09-18"})
    # Same collection time: ISIN is the deterministic tie-breaker. This ETF has
    # only a profile, not quotes. Multiple observations still count as one ETF.
    second = "IE00B4L5Y983"
    seed_yahoo(db, second, generation="second-profile", listing={"symbol": "EUNL.F", "exchange": "XFRA"},
               collected_at=clock[0], profile={"private_payload": "not in list"})
    first_page = md.acquired_instruments(db, limit=1)
    assert first_page["total"] == 2
    assert first_page["items"][0]["instrument_id"] == second
    assert first_page["items"][0]["quote_count"] == 0
    next_page = md.acquired_instruments(db, limit=1, offset=1)
    assert next_page == {"total": 2, "limit": 1, "offset": 1, "items": [{
        "instrument_id": ISIN, "isin": ISIN, "name": "Updated catalogue name", "ticker": "NEW",
        "listing": LISTING, "collected_at": "2026-09-17T13:00:00Z", "quote_count": 2,
        "asset_class": None, "index": None,
        "first_date": "2026-09-15", "last_date": "2026-09-16",
        "annualized_mean_log_return": 0.0, "annualized_volatility": None,
        "historical_year_return": None, "historical_year_start_date": None,
        "historical_year_end_date": None, "historical_year_price_basis": None,
        "historical_year_carried_from_date": None,
        "historical_year_volatility": None, "historical_year_volatility_start_date": None,
        "historical_year_volatility_end_date": None, "historical_year_volatility_price_basis": None,
        "historical_year_volatility_return_count": None,
    }]}
    assert md.acquired_instruments(db, limit=1, offset=2) == {"items": [], "total": 2, "limit": 1, "offset": 2}
    # Later failed updates do not remove already acquired observations.
    db.acquisition_jobs.update_many({}, {"$set": {"status": "failed", "active": False}})
    assert md.acquired_instruments(db)["total"] == 2
    seed_yahoo(db, ISIN, generation="newest", collected_at=clock[0] + timedelta(hours=1))
    assert md.acquired_instruments(db)["items"][0]["instrument_id"] == ISIN


@pytest.mark.parametrize("historical,statistical,keep", [
    (0.1, 0.2, True), (0.1, -0.2, False), (-0.1, 0.2, False), (-0.1, -0.2, False),
    (0, 0, True), (-0.0, 0.0, True), (0.1, -0.0, True), (-1e-12, 0.2, False), (0.1, -1e-12, False),
    (None, 0.2, False), (0.1, None, False), (None, None, False),
    (float("nan"), 0.2, False), (0.1, float("inf"), False), (float("-inf"), 0.2, False),
    pytest.param(10**400, 0.2, False, id="oversized-historical-return"),
    pytest.param(0.1, 10**400, False, id="oversized-statistical-return"),
    ("0", 0.2, False), (0.1, "0.2", False), (True, 0.2, False), (0.1, False, False),
    ({}, 0.2, False), (0.1, [], False),
])
def test_acquired_return_filter_requires_two_finite_nonnegative_numbers(db, monkeypatch, historical, statistical, keep):
    import return_statistics

    seed_yahoo(db)
    statistics = {"historical_year_return": historical, "annualized_mean_log_return": statistical,
                  "annualized_volatility": None, "historical_year_volatility": None}
    refresh = Mock(return_value=(statistics, {"listing": LISTING}))
    monkeypatch.setattr(return_statistics, "refresh_statistics", refresh)
    assert md.acquired_instruments(db, hide_negative_returns=True)["total"] == int(keep)
    refresh.assert_called_once_with(db, ISIN, generation=ISIN, as_of=NOW)
    # All remains a display-only path: no return membership rule, even with unavailable volatility.
    assert md.acquired_instruments(db, hide_negative_returns=False)["total"] == 1
    statistics.clear()  # Missing keys must not coerce to zero.
    assert md.acquired_instruments(db, hide_negative_returns=True)["total"] == 0


@pytest.mark.parametrize("years", [None, 5, 15, 30])
def test_acquired_return_filter_counts_all_candidates_before_paging(db, monkeypatch, years):
    import return_statistics

    values = {}
    for index in range(12):
        isin = f"IE{index:010d}"
        values[isin] = {"historical_year_return": 0.1, "annualized_mean_log_return": 0.2 if index % 2 else -0.2}
        seed_yahoo(db, isin, rows=[bar(day) for day in
                   ("1990-01-01", "2020-01-01" if index != 11 else "1991-01-01")])
    # A newer duplicate must not affect one-row membership or deterministic tie ordering.
    db.instruments.insert_one({"instrument_id": "IE0000000001", **LISTING, "collected_at": NOW, "job_id": "duplicate"})
    db.instruments.insert_one({"instrument_id": "idx-excluded", **LISTING, "collected_at": NOW})
    refresh = Mock(side_effect=lambda db, isin, **kwargs: (values[isin], {"listing": LISTING}))
    dates = Mock(wraps=md.quote_dates)
    labels = Mock(wraps=db.etfs.find)
    monkeypatch.setattr(return_statistics, "refresh_statistics", refresh)
    monkeypatch.setattr(md, "quote_dates", dates)
    monkeypatch.setattr(db.etfs, "find", labels)
    matches = [f"IE{index:010d}" for index in range(1, 12 if years is None else 11, 2)]
    for offset in (0, 2, 4, 6, 99):
        refresh.reset_mock()
        dates.reset_mock()
        labels.reset_mock()
        result = md.acquired_instruments(db, limit=2, offset=offset, min_history_years=years, hide_negative_returns=True)
        expected = matches[offset:offset + 2]
        assert result["total"] == len(matches)
        assert [item["instrument_id"] for item in result["items"]] == expected
        assert result["limit"] == 2 and result["offset"] == offset
        assert dates.call_count == len(expected)  # Custom calculator omits compact coverage.
        assert labels.call_count == int(bool(expected))
        if expected:
            assert labels.call_args.args[0] == {"isin": {"$in": expected}}
        assert refresh.call_count == (12 if years is None else 11)
        assert len({call.args[1] for call in refresh.call_args_list}) == refresh.call_count
        assert all(call.kwargs == {"generation": call.args[1], "as_of": NOW} for call in refresh.call_args_list)
    assert md.acquired_universe(db)["total"] == 12


@pytest.mark.parametrize("missing_state", [True, False])
def test_acquired_return_filter_treats_listing_mismatch_as_unavailable(db, config, monkeypatch, missing_state):
    import return_statistics

    collect(db, config, Provider([page([bar("2025-09-16", "90"), bar("2026-09-14", "100"), bar("2026-09-15", "110"), bar("2026-09-16", "121")])]))
    assert md.acquired_instruments(db, hide_negative_returns=True)["total"] == 1
    if missing_state:
        db.instrument_state.delete_one({"_id": ISIN})
    else:
        db.instrument_state.update_one({"_id": ISIN}, {"$set": {"listing": {**LISTING, "symbol": "OTHER"}}})
    refresh = Mock(wraps=return_statistics.refresh_statistics)
    monkeypatch.setattr(return_statistics, "refresh_statistics", refresh)
    with pytest.raises(md.DataError, match="latest Yahoo listing changed") as error:
        md.acquired_instruments(db, hide_negative_returns=True)
    assert error.value.status == 409
    assert refresh.call_count == 1  # A stale state cannot silently remove a published candidate.


@pytest.mark.parametrize("invalid", ["unpublished", "instrument", "listing", "cutoff"])
def test_acquired_return_filter_propagates_invalid_publication(db, invalid):
    db.instruments.insert_one({"instrument_id": ISIN, **LISTING, "provider": "yfinance", "published": True,
                               "job_id": "pinned", "collected_at": NOW})
    publication = {"_id": "pinned", "instrument_id": ISIN, "listing": LISTING, "provider": "yfinance",
                   "published": True, "published_at": NOW}
    if invalid == "unpublished":
        publication["published"] = False
    elif invalid == "instrument":
        publication["instrument_id"] = "OTHER"
    elif invalid == "listing":
        publication["listing"] = {**LISTING, "exchange": "OTHER"}
    else:
        publication["published_at"] = NOW + timedelta(seconds=1)
    db.acquisition_jobs.insert_one(publication)
    if invalid == "unpublished":
        assert md.acquired_instruments(db, hide_negative_returns=True)["total"] == 0
    elif invalid == "cutoff":
        db.instrument_state.insert_one({"_id": ISIN, "listing": LISTING})
        assert md.acquired_instruments(db, hide_negative_returns=True)["total"] == 0  # Empty returns, not a time cutoff.
        assert md.acquired_instruments(db)["total"] == 1
    else:
        with pytest.raises(md.DataError, match="latest Yahoo profile") as error:
            md.acquired_instruments(db, hide_negative_returns=True)
        assert error.value.status == 409


@pytest.mark.parametrize("published", [True, False])
def test_acquired_return_filter_requires_attributable_published_profile_no_legacy_fallback(db, monkeypatch, published):
    import return_statistics
    from bson import ObjectId

    old_listing = {**LISTING, "symbol": "OLD"}
    for identity, listing in [(ObjectId("000000000000000000000001"), old_listing),
                              (ObjectId("000000000000000000000002"), LISTING)]:
        db.instruments.insert_one({"_id": identity, "instrument_id": ISIN, **listing,
                                   "provider": "yfinance", "published": True, "collected_at": NOW})
    db.instrument_state.insert_one({"_id": ISIN, "listing": LISTING})
    if published:
        db.acquisition_jobs.insert_one({"_id": "resolved", "instrument_id": ISIN, "listing": LISTING,
                                        "provider": "yfinance", "published": True, "published_at": NOW})
    # An unpublished Yahoo quote must never contribute to the legacy fallback.
    db.quotes.insert_one({"instrument_id": ISIN, **LISTING, "date": "1800-01-01", "provider": "yfinance", "job_id": "hidden"})
    refresh = Mock(return_value=({"historical_year_return": 0, "annualized_mean_log_return": 0}, {"listing": LISTING}))
    monkeypatch.setattr(return_statistics, "refresh_statistics", refresh)
    if published:
        with pytest.raises(md.DataError, match="latest Yahoo profile") as error:
            md.acquired_instruments(db, hide_negative_returns=True)
        assert error.value.status == 409
        # Only an explicitly attributed profile makes this published history readable.
        db.instruments.update_one({"_id": ObjectId("000000000000000000000002")}, {"$set": {"job_id": "resolved"}})
        result = md.acquired_instruments(db, hide_negative_returns=True)
        assert result["total"] == 1 and result["items"][0]["listing"] == LISTING
        assert result["items"][0]["quote_count"] == 0
        refresh.assert_called_once_with(db, ISIN, generation="resolved", as_of=NOW)
    else:
        assert md.acquired_instruments(db, hide_negative_returns=True)["total"] == 0
        refresh.assert_not_called()


def test_acquired_return_filter_does_not_hide_database_failures(db, monkeypatch):
    import return_statistics

    seed_yahoo(db)
    monkeypatch.setattr(return_statistics, "refresh_statistics", Mock(side_effect=ConnectionFailure("offline")))
    with pytest.raises(ConnectionFailure, match="offline"):
        md.acquired_instruments(db, hide_negative_returns=True)


@pytest.mark.parametrize("years", [None, 5])
def test_acquired_return_filter_pins_publication_during_and_after_profile_selection(db, clock, monkeypatch, years):
    import return_statistics

    def publish(generation, close, published=True):
        db.instruments.insert_one({"instrument_id": ISIN, **LISTING, "collected_at": clock[0],
                                   "job_id": generation, "provider": "yfinance", "published": published})
        db.acquisition_jobs.insert_one({"_id": generation, "instrument_id": ISIN, "listing": LISTING,
                                        "provider": "yfinance", "published": published, "published_at": clock[0]})
        for day, value in [("2020-01-01", "90"), ("2025-09-16", "90"), ("2026-09-14", "100"),
                           ("2026-09-15", "110"), ("2026-09-16", close)]:
            db.quotes.insert_one({"instrument_id": ISIN, **LISTING, "job_id": generation, "provider": "yfinance",
                                  "date": day, "collected_at": clock[0],
                                  "data": {"date": day, "close": value, "price_basis": "provider_split_adjusted"}})

    publish("previous", "50")
    db.instrument_state.insert_one({"_id": ISIN, "listing": LISTING})
    publications = md._acquired_publications

    def select_with_publication(database):
        clock[0] += timedelta(seconds=1)
        publish("selected", "121")
        yield from publications(database)

    refresh = return_statistics.refresh_statistics
    calls = []

    def refresh_then_publish(*args, **kwargs):
        calls.append(kwargs)
        result = refresh(*args, **kwargs)
        clock[0] += timedelta(seconds=1)
        publish("later", "10")
        publish("failed", "1", published=False)
        return result

    monkeypatch.setattr(md, "_acquired_publications", select_with_publication)
    monkeypatch.setattr(return_statistics, "refresh_statistics", refresh_then_publish)
    dates = Mock(wraps=md.quote_dates)
    monkeypatch.setattr(md, "quote_dates", dates)
    with pytest.raises(md.DataError, match="superseded") as error:
        md.acquired_instruments(db, min_history_years=years, hide_negative_returns=True)
    assert error.value.status == 409
    assert calls == [{"generation": "selected", "as_of": NOW}]
    assert all(not job.get("reader_leases") for job in db.acquisition_jobs.find())
    assert dates.call_count <= 1
    if dates.called:
        dates.assert_called_once_with(db, ISIN, LISTING, as_of=NOW, generation="selected")


@pytest.mark.parametrize("change", ["prices", "fill", "splits", "generation"])
def test_acquired_return_filter_validates_cached_statistics(db, config, clock, monkeypatch, change):
    import return_statistics

    collect(db, config, Provider([page([bar("2025-09-16", "90"), bar("2026-09-14", "100"), bar("2026-09-15", "110"), bar("2026-09-16", "121")])]))
    assert md.acquired_instruments(db, hide_negative_returns=True)["total"] == 1
    cached = db.instrument_statistics.find_one({"_id": ISIN})
    clock[0] += timedelta(hours=1)
    if change == "prices":
        from test_portfolio_tracker import quote
        quote(db, "2026-09-16", "50")
    elif change == "fill":
        # Reuse the real policy shape, changing one validated signature input.
        policy = dict(cached["signature"]["quote_fill_policy"])
        policy["method"] = "none" if policy["method"] != "none" else "smooth"
        monkeypatch.setattr(return_statistics, "read_policy", lambda db: policy)
    else:
        rows = [row["data"] for row in db.quotes.find({"job_id": cached["signature"]["generation"]})]
        seed_yahoo(db, generation="new-generation", rows=rows if change == "splits" else (), collected_at=clock[0])
        if change == "splits":
            db.split_snapshots.update_one({"_id": "new-generation"}, {"$set": {"error": "Conflicting split evidence"}})
    latest_rows = Mock(wraps=return_statistics._latest_rows)
    monkeypatch.setattr(return_statistics, "_latest_rows", latest_rows)
    filtered = md.acquired_instruments(db, hide_negative_returns=True)
    assert latest_rows.call_count == 1
    updated = db.instrument_statistics.find_one({"_id": ISIN})
    assert updated["signature"] != cached["signature"]
    if change in ("prices", "generation"):
        assert filtered["total"] == 0
    else:
        # Already adjusted Yahoo Close does not depend on reapplying split evidence.
        assert filtered["total"] == 1
    latest_rows.reset_mock()
    assert md.acquired_instruments(db, hide_negative_returns=True) == filtered
    latest_rows.assert_not_called()


def test_acquired_return_filter_api_validation_and_defaults(api, db, config, monkeypatch):
    client, _ = api
    assert client.get("/instruments/acquired?hide_negative_returns=true").status_code == 401
    client.post("/auth/login", json={"login": "admin", "password": "admin"})
    collect(db, config)  # Profile and insufficient history are retained by All.
    monkeypatch.setattr(md, "provider_client", Mock(side_effect=AssertionError("Read must not call provider")))
    assert client.get("/instruments/acquired").json()["total"] == 1
    assert client.get("/instruments/acquired?hide_negative_returns=false").json()["total"] == 1
    for query in ("hide_negative_returns=true", "hide_negative_returns=true&min_history_years=5"):
        assert client.get("/instruments/acquired?" + query).json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
    for value in ("maybe", "2", "", "null"):
        assert client.get("/instruments/acquired?hide_negative_returns=" + value).status_code == 422
    assert client.get("/instruments/acquired/universe").json()["total"] == 1


@pytest.mark.parametrize("years", [5, 15, 30])
def test_acquired_history_filter_precedes_pagination(db, monkeypatch, years):
    import return_statistics

    refresh = Mock(return_value=({}, {"listing": LISTING}))
    monkeypatch.setattr(return_statistics, "refresh_statistics", refresh)
    ids = ["IE0000000001", "IE0000000002", "IE0000000003", "IE0000000004"]
    for index, isin in enumerate(ids):
        # First row is just short; second and third match exactly; last is profile-only.
        end = f"{1990 + years}-01-01" if index else f"{1989 + years}-12-31"
        seed_yahoo(db, isin, rows=[] if index == 3 else [bar(day) for day in ("1990-01-01", end)])
    # Neither other listings nor unpublished Yahoo history may extend coverage.
    db.quotes.insert_one({"instrument_id": ids[0], **LISTING, "symbol": "OTHER", "date": "1900-01-01", "collected_at": NOW})
    db.instruments.insert_one({"instrument_id": "IE0000000005", **LISTING, "collected_at": NOW, "provider": "yfinance", "published": False})
    result = md.acquired_instruments(db, limit=1, offset=1, min_history_years=years)
    assert result["total"] == 2
    assert result["items"][0]["instrument_id"] == ids[2]
    assert result["offset"] == 1
    assert result["items"][0]["quote_count"] == 2
    assert refresh.call_count == 1
    assert md.acquired_instruments(db, offset=2, min_history_years=years)["items"] == []


def test_acquired_history_filter_uses_published_yahoo_generation(db, monkeypatch):
    import return_statistics

    monkeypatch.setattr(return_statistics, "refresh_statistics", Mock(return_value=({}, {"listing": LISTING})))
    db.instruments.insert_one({"instrument_id": ISIN, **LISTING, "collected_at": NOW, "job_id": "current", "provider": "yfinance", "published": True})
    db.acquisition_jobs.insert_one({"_id": "current", "instrument_id": ISIN, "listing": LISTING,
                                     "published": True, "published_at": NOW, "provider": "yfinance"})
    for generation, days in [("current", ["2020-01-01", "2026-01-01"]), ("old", ["1990-01-01"])]:
        for day in days:
            db.quotes.insert_one({"instrument_id": ISIN, **LISTING, "date": day, "collected_at": NOW, "job_id": generation, "provider": "yfinance"})
    assert md.acquired_instruments(db, min_history_years=5)["total"] == 1
    assert md.acquired_instruments(db, min_history_years=15)["total"] == 0


def test_history_filter_accepts_publication_during_profile_selection(db, clock, monkeypatch):
    import return_statistics

    def publish(generation, days):
        db.instruments.insert_one({"instrument_id": ISIN, **LISTING, "collected_at": clock[0],
                                   "job_id": generation, "provider": "yfinance", "published": True})
        db.acquisition_jobs.insert_one({"_id": generation, "instrument_id": ISIN, "listing": LISTING,
                                        "provider": "yfinance", "published": True, "published_at": clock[0]})
        for day in days:
            db.quotes.insert_one({"instrument_id": ISIN, **LISTING, "job_id": generation,
                                  "provider": "yfinance", "date": day, "collected_at": clock[0],
                                  "data": {"close": "100", "price_basis": "provider_split_adjusted"}})

    publish("previous", ("2024-01-01", "2025-01-01"))
    db.instrument_state.insert_one({"_id": ISIN, "listing": LISTING})
    publications = md._acquired_publications

    def select_with_refresh(database):
        # A refresh becomes visible while the publication cursor is consumed.
        clock[0] += timedelta(seconds=1)
        publish("refreshed", ("2020-01-01", "2025-01-01"))
        yield from publications(database)

    bounds = Mock(wraps=md.saved_history_bounds)
    dates = Mock(wraps=md.quote_dates)
    statistics = Mock(wraps=return_statistics.refresh_statistics)
    monkeypatch.setattr(md, "_acquired_publications", select_with_refresh)
    monkeypatch.setattr(md, "saved_history_bounds", bounds)
    monkeypatch.setattr(md, "quote_dates", dates)
    monkeypatch.setattr(return_statistics, "refresh_statistics", statistics)

    result = md.acquired_instruments(db, min_history_years=5)
    assert result["total"] == len(result["items"]) == 1
    item = result["items"][0]
    assert item["instrument_id"] == ISIN
    assert item["collected_at"] == md.public(clock[0])
    assert (item["first_date"], item["last_date"], item["quote_count"]) == ("2020-01-01", "2025-01-01", 2)
    assert bounds.call_args.args[2] == NOW
    assert clock[0] == NOW + timedelta(seconds=1)
    dates.assert_not_called()  # Coverage reuses the raw-row summary, not usable-price endpoints.
    statistics.assert_called_once_with(db, ISIN, generation="refreshed", as_of=NOW)
    print({"scenario": "publication during saved-profile selection", "total": result["total"],
           "instrument_id": item["instrument_id"], "generation": "refreshed",
           "publication_cutoff": md.public(clock[0]), "first_date": item["first_date"],
           "last_date": item["last_date"], "quote_count": item["quote_count"],
           "limitation": "synthetic mongomock data; no real-Mongo concurrency or performance evidence"})


def test_history_endpoint_pipeline_and_batch_operation_bounds(db, monkeypatch, history_lookup_calls):
    from bson import BSON
    import return_statistics

    for index in range(123):
        isin = f"IE{index:010d}"
        seed_yahoo(db, isin, rows=[bar(day) for day in ("1990-01-01", "2020-01-01")])
    dates = Mock(wraps=md.quote_dates)
    distinct = Mock(wraps=db.quotes.distinct)
    refresh = Mock(return_value=({}, {"listing": LISTING}))
    monkeypatch.setattr(md, "quote_dates", dates)
    monkeypatch.setattr(db.quotes, "distinct", distinct)
    monkeypatch.setattr(return_statistics, "refresh_statistics", refresh)
    result = md.acquired_instruments(db, limit=2, offset=100, min_history_years=30)
    assert result["total"] == 123
    assert [item["instrument_id"] for item in result["items"]] == ["IE0000000100", "IE0000000101"]
    assert dates.call_count == distinct.call_count == refresh.call_count == 2
    assert [len(p[1]["$project"]["candidate"]["$literal"]) for p in history_lookup_calls] == [50, 50, 23]
    probes = 0
    for pipeline in history_lookup_calls:
        assert len(BSON.encode({"aggregate": "instruments", "pipeline": pipeline, "cursor": {}})) < md.HISTORY_COMMAND_BYTES
        for stage, direction in zip(pipeline[4:6], (1, -1)):
            lookup = stage["$lookup"]
            assert lookup["from"] == "quotes"
            assert lookup["pipeline"][1:] == [{"$sort": {"date": direction}}, {"$limit": 1}, {"$project": {"_id": 0, "date": 1}}]
            assert lookup["pipeline"][0]["$match"]["date"] == {"$lte": NOW.date().isoformat()}
            assert len(lookup["pipeline"][0]["$match"]["$expr"]["$and"]) == 5
            assert all(seed["provider"] == "yfinance" and seed["generation"] == seed["instrument_id"]
                       for seed in pipeline[1]["$project"]["candidate"]["$literal"])
            probes += len(pipeline[1]["$project"]["candidate"]["$literal"])
    assert probes == 246  # Internal probes, not client commands or examined keys.
    print({"scenario": "synthetic saved-history 30y filter, page after 100 matches",
           "total": result["total"], "items": [item["instrument_id"] for item in result["items"]],
           "endpoint_batch_commands": len(history_lookup_calls), "internal_endpoint_probes": probes,
           "page_date_calls": dates.call_count, "page_statistics_calls": refresh.call_count,
           "limitation": "mongomock endpoint emulator; no real-Mongo index or latency evidence"})
    for offset, years in ((123, 30), (0, 40)):
        dates.reset_mock()
        distinct.reset_mock()
        refresh.reset_mock()
        assert md.acquired_instruments(db, offset=offset, min_history_years=years)["items"] == []
        dates.assert_not_called()
        distinct.assert_not_called()
        refresh.assert_not_called()


def test_history_seed_size_bound_and_disappearing_carrier(db, monkeypatch):
    from bson import BSON

    observations = [{"_id": f"IE{index:010d}", "listing": {"symbol": "$" + "X" * 2000, "exchange": "XETR"},
                     "provider": "yfinance", "job_id": f"generation-{index}"} for index in range(5)]
    seeds = [{"instrument_id": row["_id"], **row["listing"], "generation": row["job_id"], "provider": "yfinance"}
             for row in observations]
    command = lambda batch: {"aggregate": "instruments", "pipeline": md.history_bounds_pipeline(batch), "cursor": {}}
    # A deliberately low cap exercises BSON splitting before the count cap.
    monkeypatch.setattr(md, "HISTORY_COMMAND_BYTES", len(BSON.encode(command(seeds[:2]))) + md.HISTORY_COMMAND_OVERHEAD)
    batches = list(md._history_batches(seeds, command))
    assert [len(batch) for batch in batches] == [2, 2, 1]
    assert all(len(BSON.encode(command(batch))) + md.HISTORY_COMMAND_OVERHEAD <= md.HISTORY_COMMAND_BYTES for batch in batches)
    monkeypatch.setattr(md, "HISTORY_COMMAND_BYTES", 100)
    with pytest.raises(md.DataError, match="size limit") as error:
        list(md._history_batches(seeds, command))
    assert error.value.status == 413
    monkeypatch.setattr(md, "HISTORY_COMMAND_BYTES", 512 * 1024)
    assert md.saved_history_bounds(db, []) == {}
    assert md.acquired_instruments(db, min_history_years=30)["total"] == 0
    for row in observations:
        seed_yahoo(db, row["_id"], generation=row["job_id"], listing=row["listing"])
    original = db.instruments.aggregate
    monkeypatch.setattr(db.instruments, "aggregate", lambda pipeline: iter([]) if pipeline[0] == {"$limit": 1} else original(pipeline))
    with pytest.raises(md.DataError, match="inventory changed") as error:
        md.saved_history_bounds(db, observations)
    assert error.value.status == 409
    assert all(not job.get("reader_leases") for job in db.acquisition_jobs.find())


@pytest.mark.parametrize("years", [5, 15, 30])
@pytest.mark.parametrize("provider", ["legacy", "yfinance"])
def test_history_filter_full_payload_equivalence(db, years, provider):
    # The previous distinct-date implementation is the fixed-fixture oracle.
    leap_end = date(1992 + years, 2, 28).isoformat()
    cases = [("1990-01-01", f"{1990 + years}-01-01"),
             ("1990-01-01", f"{1989 + years}-12-31"),
             ("1992-02-29", leap_end), ("2020-01-01",), ()]
    for index, days in enumerate(cases):
        isin = f"IE{index:010d}"
        metadata = {"provider": "yfinance", "job_id": isin} if provider == "yfinance" else {}
        db.instruments.insert_one({"instrument_id": isin, **LISTING, "collected_at": NOW, "published": True, **metadata})
        if metadata:
            db.acquisition_jobs.insert_one({"_id": isin, "instrument_id": isin, "listing": LISTING,
                                            "published": True, "published_at": NOW, "provider": "yfinance"})
        db.instrument_state.insert_one({"_id": isin, "listing": LISTING})
        for day in days:
            # Sparse, unusable prices count; duplicate observations do not inflate count.
            for close in (None, "100"):
                db.quotes.insert_one({"instrument_id": isin, **LISTING, "date": day, "collected_at": NOW,
                                      **metadata, "data": {"date": day, "close": close}})
        db.quotes.insert_one({"instrument_id": isin, **LISTING, "symbol": "OLD", "date": "1800-01-01"})
        db.quotes.insert_one({"instrument_id": isin, **LISTING, "provider": "yfinance", "date": "1800-01-01"})
    db.instruments.insert_one({"instrument_id": "idx-excluded", **LISTING, "collected_at": NOW})
    all_items = md.acquired_instruments(db)["items"]
    matching = []
    for item in all_items:
        if item["first_date"] is None:
            continue
        start, end = date.fromisoformat(item["first_date"]), date.fromisoformat(item["last_date"])
        try:
            anniversary = start.replace(year=start.year + years)
        except ValueError:
            anniversary = date(start.year + years, 2, 28)
        if end >= anniversary:
            matching.append(item)
    assert len(matching) == (2 if provider == "yfinance" else 0)
    if provider == "legacy":
        assert all_items == []  # Legacy profiles and quotes never enter the Yahoo inventory.
    for offset in (0, 1, 2, 20):
        assert md.acquired_instruments(db, limit=1, offset=offset, min_history_years=years) == {
            "items": matching[offset:offset + 1], "total": len(matching), "limit": 1, "offset": offset,
        }


@pytest.mark.parametrize("invalid", ["unpublished", "instrument", "listing", "cutoff"])
def test_history_filter_rejects_invalid_pinned_publication(db, invalid):
    db.instruments.insert_one({"instrument_id": ISIN, **LISTING, "provider": "yfinance", "published": True,
                               "job_id": "pinned", "collected_at": NOW})
    publication = {"_id": "pinned", "instrument_id": ISIN, "listing": LISTING, "provider": "yfinance",
                   "published": True, "published_at": NOW}
    if invalid == "unpublished":
        publication["published"] = False
    elif invalid == "instrument":
        publication["instrument_id"] = "OTHER"
    elif invalid == "listing":
        publication["listing"] = {**LISTING, "exchange": "OTHER"}
    else:
        publication["published_at"] = NOW + timedelta(seconds=1)
    db.acquisition_jobs.insert_one(publication)
    if invalid == "unpublished":
        assert md.acquired_instruments(db, min_history_years=5)["total"] == 0
    elif invalid == "cutoff":
        db.instrument_state.insert_one({"_id": ISIN, "listing": LISTING})
        for day in ("2020-01-01", "2025-01-01"):
            db.quotes.insert_one({"instrument_id": ISIN, **LISTING, "provider": "yfinance", "job_id": "pinned", "date": day,
                                  "data": {**bar(day), "price_basis": "provider_split_adjusted"}})
        assert md.acquired_instruments(db, min_history_years=5)["total"] == 1
    else:
        with pytest.raises(md.DataError, match="latest Yahoo profile") as error:
            md.acquired_instruments(db, min_history_years=5)
        assert error.value.status == 409


def test_history_publications_are_batched_pinned_and_keep_request_cutoff(db, monkeypatch, history_lookup_calls):
    import return_statistics

    observations = []
    for index in range(105):
        isin, generation = f"IE{index:010d}", f"current-{index}"
        observations.append({"_id": isin, "listing": LISTING, "provider": "yfinance", "job_id": generation})
        db.instruments.insert_one({"instrument_id": isin, **LISTING, "collected_at": NOW,
                                   "provider": "yfinance", "published": True, "job_id": generation})
        db.acquisition_jobs.insert_one({"_id": generation, "instrument_id": isin, "listing": LISTING,
                                        "published": True, "published_at": NOW, "provider": "yfinance"})
        for job, provider, symbol, exchange, days in [
            (generation, "yfinance", LISTING["symbol"], LISTING["exchange"], ("2020-01-01", "2025-01-01")),
            (generation, "marketstack", LISTING["symbol"], LISTING["exchange"], ("1800-01-01",)),
            (generation, "yfinance", "OTHER", LISTING["exchange"], ("1800-01-01",)),
            (generation, "yfinance", LISTING["symbol"], "OTHER", ("1800-01-01",)),
            ("superseded", "yfinance", LISTING["symbol"], LISTING["exchange"], ("1800-01-01", "2100-01-01")),
        ]:
            for day in days:
                db.quotes.insert_one({"instrument_id": isin, "symbol": symbol, "exchange": exchange,
                                      "job_id": job, "provider": provider, "date": day})
    aggregates = Mock(wraps=db.acquisition_jobs.aggregate)
    monkeypatch.setattr(db.acquisition_jobs, "aggregate", aggregates)
    bounds = md.saved_history_bounds(db, observations, NOW)
    # Inspect application commands, not mongomock's internal find() calls.
    assert aggregates.call_count == 6  # Three bounded chunks, admission and completion.
    assert all("published_at" not in call.args[0][0]["$match"] for call in aggregates.call_args_list)
    assert len(history_lookup_calls) == 3
    assert all((row["first_date"], row["last_date"]) == ("2020-01-01", "2025-01-01") for row in bounds.values())
    for pipeline in history_lookup_calls:
        assert len(pipeline[4]["$lookup"]["pipeline"][0]["$match"]["$expr"]["$and"]) == 5
    refresh = Mock(return_value=({}, {"listing": LISTING}))
    monkeypatch.setattr(return_statistics, "refresh_statistics", refresh)
    quote_dates = Mock(wraps=md.quote_dates)
    monkeypatch.setattr(md, "quote_dates", quote_dates)
    original = md.saved_history_bounds

    def after_membership(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(md, "utcnow", lambda: NOW + timedelta(seconds=1))
        return result

    monkeypatch.setattr(md, "saved_history_bounds", after_membership)
    page = md.acquired_instruments(db, limit=1, min_history_years=5)
    assert page["items"][0]["quote_count"] == 2
    assert quote_dates.call_args.kwargs == {"generation": "current-0", "as_of": NOW}
    assert refresh.call_args.kwargs == {"generation": "current-0", "as_of": NOW}


def test_history_filter_keeps_previous_publication_after_failed_refresh(db):
    db.instrument_state.insert_one({"_id": ISIN, "listing": LISTING})
    db.instruments.insert_many([
        {"instrument_id": ISIN, **LISTING, "job_id": "current", "provider": "yfinance",
         "published": True, "collected_at": NOW},
        {"instrument_id": ISIN, **LISTING, "job_id": "failed", "provider": "yfinance",
         "published": False, "collected_at": NOW + timedelta(hours=1)},
    ])
    db.acquisition_jobs.insert_many([
        {"_id": "current", "instrument_id": ISIN, "listing": LISTING,
         "published": True, "published_at": NOW, "provider": "yfinance"},
        {"_id": "failed", "instrument_id": ISIN, "listing": LISTING, "status": "failed"},
    ])
    for isin, generation, days in [
        (ISIN, "current", ("2020-01-01", "2025-01-01")),
        (ISIN, "failed", ("1800-01-01", "2200-01-01")),
        ("IE9999999999", "current", ("1800-01-01", "2200-01-01")),
    ]:
        for day in days:
            db.quotes.insert_one({"instrument_id": isin, **LISTING, "job_id": generation,
                                  "provider": "yfinance", "date": day,
                                  "data": {**bar(day), "price_basis": "provider_split_adjusted"}})
    item = md.acquired_instruments(db, min_history_years=5)["items"][0]
    assert (item["first_date"], item["last_date"], item["quote_count"]) == ("2020-01-01", "2025-01-01", 2)
    assert md.acquired_instruments(db, min_history_years=15)["total"] == 0


def test_history_latest_profile_ties_missing_job_and_legacy_fallback(db):
    from bson import ObjectId

    old, new = {**LISTING, "symbol": "OLD"}, {**LISTING, "symbol": "$NEW"}
    for identity, listing in [(ObjectId("000000000000000000000001"), old),
                              (ObjectId("000000000000000000000002"), new)]:
        db.instruments.insert_one({"_id": identity, "instrument_id": ISIN, **listing,
                                   "provider": "yfinance", "published": True, "collected_at": NOW})
    # Unattributed profiles/quotes are never a substitute for a complete publication.
    for listing, days in [(old, ("1800-01-01", "2025-01-01")), (new, ("2000-01-01", "2020-01-01"))]:
        for day in days:
            db.quotes.insert_one({"instrument_id": ISIN, **listing, "date": day})
    assert md.acquired_instruments(db, min_history_years=30)["total"] == 0
    assert md.acquired_instruments(db, min_history_years=15)["total"] == 0
    db.acquisition_jobs.insert_one({"_id": "resolved", "instrument_id": ISIN, "listing": new,
                                    "provider": "yfinance", "published": True, "published_at": NOW})
    with pytest.raises(md.DataError, match="latest Yahoo profile"):
        md.acquired_instruments(db, min_history_years=5)
    db.instruments.update_one({"_id": ObjectId("000000000000000000000002")}, {"$set": {"job_id": "resolved"}})
    db.instrument_state.insert_one({"_id": ISIN, "listing": new})
    for day in ("2020-01-01", "2025-01-01"):
        db.quotes.insert_one({"instrument_id": ISIN, **new, "job_id": "resolved", "provider": "yfinance", "date": day,
                              "data": {**bar(day), "price_basis": "provider_split_adjusted"}})
    assert md.acquired_instruments(db, min_history_years=15)["total"] == 0
    assert md.acquired_instruments(db, min_history_years=5)["items"][0]["quote_count"] == 2


def test_acquired_annual_statistics_refresh_saved_history_only_for_current_page(db, config, clock, monkeypatch):
    import return_statistics

    rows = [bar("2025-09-16", "90")] + [bar(f"2026-09-{day}", close) for day, close in [(14, "100"), (15, "110"), (16, "99")]]
    initial, _ = collect(db, config, Provider([page(rows)]))
    seed_yahoo(db, "IE00B4L5Y983", generation="other-profile", collected_at=NOW - timedelta(hours=1))
    db.instrument_statistics.delete_many({})  # Previously acquired, not yet calculated.
    original_quotes = list(db.quotes.find())
    refresh = Mock(wraps=return_statistics.refresh_statistics)
    monkeypatch.setattr(return_statistics, "refresh_statistics", refresh)
    item = md.acquired_instruments(db, limit=1)["items"][0]
    refresh.assert_called_once_with(db, ISIN, generation=initial["id"], as_of=NOW)
    assert db.instrument_statistics.count_documents({}) == 1  # Not the off-page ETF.
    logs = [math.log(1.1), math.log(0.9)]
    assert item["annualized_mean_log_return"] == pytest.approx(mean(logs) * 252)
    assert item["annualized_volatility"] == pytest.approx(math.sqrt(variance(logs) * 252))
    assert item["historical_year_return"] == pytest.approx(0.1)
    assert item["historical_year_start_date"] == "2025-09-16"
    assert item["historical_year_end_date"] == "2026-09-16"
    assert item["historical_year_price_basis"] == "split_adjusted_close"
    assert item["historical_year_volatility"] == pytest.approx(math.sqrt(variance(logs) * 252))
    assert item["historical_year_volatility_start_date"] == "2025-09-16"
    assert item["historical_year_volatility_end_date"] == "2026-09-16"
    assert item["historical_year_volatility_price_basis"] == "split_adjusted_close"
    assert item["historical_year_volatility_return_count"] == 2
    assert list(db.quotes.find()) == original_quotes
    cached = db.instrument_statistics.find_one({"_id": ISIN})
    clock[0] += timedelta(hours=1)
    assert md.acquired_instruments(db, limit=1)["items"][0] == item
    assert db.instrument_statistics.find_one({"_id": ISIN}) == cached
    from test_portfolio_tracker import quote
    quote(db, "2026-09-16", "121")
    updated = md.acquired_instruments(db, limit=1)["items"][0]
    assert updated["quote_count"] == 4
    assert updated["annualized_mean_log_return"] == pytest.approx(math.log(1.1) * 252)
    assert updated["annualized_volatility"] == pytest.approx(0)
    assert updated["historical_year_volatility"] == pytest.approx(0)
    assert updated["historical_year_return"] == pytest.approx(121 / 90 - 1)
    # Revising the year-ago endpoint also invalidates the cached actual return.
    quote(db, "2025-09-16", "100")
    assert md.acquired_instruments(db, limit=1)["items"][0]["historical_year_return"] == pytest.approx(0.21)


@pytest.mark.parametrize("missing_state", [True, False])
def test_acquired_annual_statistics_never_use_a_different_listing(db, config, missing_state):
    collect(db, config, Provider([page([bar("2025-09-16", "90"), bar("2026-09-14", "100"), bar("2026-09-15", "110"), bar("2026-09-16", "121")])]))
    assert md.acquired_instruments(db)["items"][0]["historical_year_return"] is not None
    db.instruments.update_many({}, {"$set": {"symbol": "PROFILE.DE"}})
    if missing_state:
        db.instrument_state.delete_one({"_id": ISIN})
    with pytest.raises(md.DataError, match="latest Yahoo profile") as error:
        md.acquired_instruments(db)
    assert error.value.status == 409
    assert all(not job.get("reader_leases") for job in db.acquisition_jobs.find())


def test_request_queues_only_once_and_does_not_collect(db, config):
    publish = Mock()
    first = enqueue(db, config, publish=publish)
    second = enqueue(db, config, publish=publish)
    assert first["id"] == second["id"]
    assert first["status"] == "queued"
    publish.assert_called_once_with(config, first["id"])
    assert db.instruments.count_documents({}) == db.quotes.count_documents({}) == 0
    assert md.budget_status(db, config)["used"] == 0
    with pytest.raises(md.DataError, match="already being collected"):
        enqueue(db, config, listing={"symbol": "CSPX.L", "exchange": "XLON"})


def test_publish_failure_is_sanitized_and_retriable(db, config):
    with pytest.raises(md.DataError) as caught:
        enqueue(db, config, publish=Mock(side_effect=RuntimeError("secret broker URL")))
    assert caught.value.status == 503
    assert "secret" not in str(caught.value)
    job = db.acquisition_jobs.find_one()
    assert job["status"] == "failed" and job["active"] is False
    assert enqueue(db, config)["id"] != job["_id"]


def test_initial_history_snapshots_exact_numbers_and_no_duplicate_redelivery(db, config):
    result, provider = collect(db, config)
    assert result["status"] == "succeeded"
    assert result["calls"] == 2 and result["rows"] == 1
    assert provider.calls[1][3:] == ("2010-05-19", "2026-09-17", 0)
    assert md.budget_status(db, config)["used"] == 0
    assert db.marketstack_usage.count_documents({}) == 0
    snapshot = db.instruments.find_one()
    assert snapshot["collection_date"] == "2026-09-17"
    assert snapshot["data"] == provider.profile
    quote = db.quotes.find_one()
    assert quote["data"]["close"] == "101.1234567890123456789"
    assert quote["data"]["adj_open"] is None
    assert quote["data"]["future_field"] == {"preserved": True}
    detail = md.instrument_detail(db, config, ISIN)
    assert detail["summary"]["history_status"] == "partial"
    assert "Incomplete" in detail["summary"]["warning"]
    assert detail["catalogue"]["holdings"] == [{"isin": "US0378331005"}]
    assert detail["profile"]["collected_at"] == "2026-09-17T12:00:00Z"
    assert md.read_quotes(db, ISIN)["items"][0]["volume"] is None
    assert md.run_acquisition(db, config, result["id"], Mock(side_effect=AssertionError("must not run")))["status"] == "succeeded"
    assert db.instruments.count_documents({}) == db.quotes.count_documents({}) == 1


def test_incremental_only_after_last_quote_and_fresh_profile(db, config, clock):
    collect(db, config)
    clock[0] += timedelta(hours=1)
    source = Provider([page([bar("2026-09-16")])], {"ticker": "SXR8.DE", "name": "Updated", "new_field": 123})
    result, source = collect(db, config, source)
    assert result["mode"] == "refresh"
    assert source.calls[1][3:] == ("2010-05-19", "2026-09-17", 0)
    assert db.instruments.count_documents({}) == 2
    assert md.read_quotes(db, ISIN)["total"] == 1  # A full replacement never mixes the preceding vintage.
    assert md.instrument_detail(db, config, ISIN)["profile"]["data"]["new_field"] == 123
    assert md.instrument_detail(db, config, ISIN, NOW + timedelta(minutes=30))["profile"]["data"]["name"] == "Updated"


def test_empty_increment_does_not_skip_late_published_quotes(db, config):
    collect(db, config)
    result, source = collect(db, config, Provider([page([])]))
    assert result["status"] == "failed"  # Empty replacement must not erase readable history.
    assert db.instrument_state.find_one()["last_complete_quote_date"] == "2026-09-15"
    assert md.read_quotes(db, ISIN)["total"] == 1
    new = enqueue(db, config)
    assert db.acquisition_jobs.find_one({"_id": new["id"]})["date_from"] == "2010-05-19"


def test_explicit_backfill_is_full_yahoo_refresh_with_monotone_watermark(db, config, clock):
    collect(db, config)
    clock[0] += timedelta(seconds=1)
    job = enqueue(db, config, mode="backfill")
    provider = Provider([page([bar("2020-01-02")])])
    result = md.run_acquisition(db, config, job["id"], provider.factory)
    assert result["mode"] == "refresh"
    assert provider.calls[1][3:5] == ("2010-05-19", "2026-09-17")
    assert db.instrument_state.find_one()["last_complete_quote_date"] == "2026-09-15"
    assert result["status"] == "succeeded"
    assert md.read_quotes(db, ISIN)["items"][0]["date"] == "2020-01-02"


def test_failed_pagination_does_not_advance_watermark(db, config):
    provider = Provider([page([bar("2020-01-02")], total=2), MarketstackError("quota", "Quota exhausted")])
    result, _ = collect(db, config, provider)
    assert result["status"] == "failed"
    assert db.quotes.count_documents({}) == 1
    assert db.instrument_state.find_one() is None  # Failed first download never publishes listing/watermark.
    assert md.read_quotes(db, ISIN)["items"] == []
    new = enqueue(db, config)
    assert db.acquisition_jobs.find_one({"_id": new["id"]})["date_from"] == "2010-05-19"
    assert new["mode"] == "refresh"


def test_database_retry_resumes_profile_and_idempotent_page_writes(db, config, clock, monkeypatch):
    job = enqueue(db, config)
    real = db.quotes.update_one
    calls = []

    def interrupted(*args, **kwargs):
        result = real(*args, **kwargs)
        if not calls:
            calls.append(True)
            raise ConnectionFailure("write acknowledged but connection lost")
        return result

    monkeypatch.setattr(db.quotes, "update_one", interrupted)
    with pytest.raises(ConnectionFailure):
        md.run_acquisition(db, config, job["id"], Provider().factory)
    assert db.instruments.count_documents({}) == db.quotes.count_documents({}) == 1
    clock[0] += timedelta(seconds=md.LEASE_SECONDS + 1)
    provider = Provider()
    result = md.run_acquisition(db, config, job["id"], provider.factory)
    assert result["status"] == "succeeded"
    assert provider.calls == []  # Reuse saved EOD and its exact split actions, entirely offline.
    assert db.instruments.count_documents({}) == db.quotes.count_documents({}) == 1
    assert result["calls"] == 2


def test_completed_download_can_be_finalized_offline_after_statistics_failure(db, config, monkeypatch):
    import return_statistics

    job = enqueue(db, config)
    with monkeypatch.context() as patch:
        patch.setattr(return_statistics, "refresh_statistics", Mock(side_effect=ModuleNotFoundError("return_statistics")))
        with pytest.raises(ModuleNotFoundError):
            md.run_acquisition(db, config, job["id"], Provider().factory)
    failed = db.acquisition_jobs.find_one({"_id": job["id"]})
    assert failed["status"] == "failed" and failed["active"] is False
    assert failed["download_complete"] is True
    assert db.instrument_state.find_one()["last_complete_quote_date"] is None
    evidence = {name: list(db[name].find()) for name in ("quotes", "raw_eod_pages", "instruments", "marketstack_usage")}

    # Operator recovery, not a fresh acquisition: explicitly reactivate only the
    # completed checkpoint. Every provider method is forbidden during finalization.
    db.acquisition_jobs.update_one({"_id": job["id"]}, {"$set": {"status": "queued", "active": True}})
    offline = Mock()
    offline.__enter__ = Mock(return_value=offline)
    offline.__exit__ = Mock(return_value=False)
    for name in ("tickerinfo", "eod_page", "tickerslist"):
        setattr(offline, name, Mock(side_effect=AssertionError("recovery must not request provider data")))
    result = md.run_acquisition(db, config, job["id"], Mock(return_value=offline))
    assert result["status"] == "succeeded"
    assert result["error"] is None
    assert result["calls"] == failed["calls"]
    assert db.instrument_state.find_one()["last_complete_quote_date"] == "2026-09-15"
    assert db.instrument_statistics.find_one()["statistics"]["quote_count"] == 1
    for name, documents in evidence.items():
        assert list(db[name].find()) == documents
    for name in ("tickerinfo", "eod_page", "tickerslist"):
        getattr(offline, name).assert_not_called()
    assert md.instrument_detail(db, config, ISIN)["acquisition"]["error"] is None


def test_successful_page_checkpoint_resumes_at_next_offset(db, config, clock):
    job = enqueue(db, config)
    provider = Provider([page([bar("2020-01-02")], total=2), ConnectionFailure("database offline")])
    with pytest.raises(ConnectionFailure):
        md.run_acquisition(db, config, job["id"], provider.factory)
    assert db.acquisition_jobs.find_one()["next_offset"] == 1
    clock[0] += timedelta(seconds=md.LEASE_SECONDS + 1)
    provider = Provider([page([bar("2020-01-03")], total=2, offset=1)])
    assert md.run_acquisition(db, config, job["id"], provider.factory)["status"] == "succeeded"
    assert provider.calls[0][-1] == 1
    assert md.read_quotes(db, ISIN)["total"] == 2


def test_retired_budget_is_ignored_but_current_job_limits_stop_before_http(db, config):
    db.marketstack_usage.insert_one({"_id": "2026-09", "used": 10**9})
    result, source = collect(db, config)
    assert result["status"] == "succeeded" and result["calls"] == len(source.calls) == 2
    assert md.budget_status(db, config)["remaining"] is None
    assert db.marketstack_usage.find_one()["used"] == 10**9
    config.market_data_max_calls_per_job = 1
    result, source = collect(db, config)
    assert result["status"] == "failed" and "safety limit" in result["error"]
    assert result["calls"] == len(source.calls) == 1
    assert md.read_quotes(db, ISIN)["total"] == 1  # Retain the preceding complete vintage.


def test_provider_isin_mismatch_is_preserved_without_blocking_download(db, config):
    result, source = collect(db, config, Provider(profile={"ticker": "SXR8.DE", "isin": "IE00B4L5Y983"}))
    assert result["status"] == "succeeded"
    assert len(source.calls) == 2
    assert db.instruments.find_one()["data"]["isin"] == "IE00B4L5Y983"
    assert db.quotes.count_documents({}) == 1


def test_listing_cannot_change_after_prices_exist(db, config):
    collect(db, config)
    with pytest.raises(md.DataError, match="cannot be changed"):
        enqueue(db, config, listing={"symbol": "CSPX.L", "exchange": "XLON"})


def test_stalled_job_is_exposed_as_retryable_failure(db, config, clock):
    job = enqueue(db, config)
    db.acquisition_jobs.update_one({"_id": job["id"]}, {"$set": {"status": "running", "lease_until": NOW + timedelta(seconds=1)}})
    with pytest.raises(md.JobBusy):
        md.run_acquisition(db, config, job["id"], Mock(side_effect=AssertionError("no provider")))
    clock[0] += timedelta(seconds=2)
    assert md.instrument_detail(db, config, ISIN)["acquisition"]["status"] == "failed"
    assert enqueue(db, config)["id"] != job["id"]


def test_orphaned_queued_request_can_be_retried_and_late_delivery_skips(db, config, clock):
    job = enqueue(db, config)
    clock[0] += timedelta(seconds=md.QUEUE_TIMEOUT_SECONDS + 1)
    detail = md.instrument_detail(db, config, ISIN)
    assert detail["acquisition"]["status"] == "failed"
    assert "did not start" in detail["acquisition"]["error"]
    provider = Mock(side_effect=AssertionError("late delivery must not consume quota"))
    assert md.run_acquisition(db, config, job["id"], provider)["status"] == "failed"
    provider.assert_not_called()
    assert enqueue(db, config)["id"] != job["id"]


def test_quote_observations_as_of_and_pagination(db, config, clock):
    first, _ = collect(db, config)
    from test_portfolio_tracker import quote
    quote(db, "2026-09-15", "999", publish=False)
    assert md.read_quotes(db, ISIN)["items"][0]["close"] == "101.1234567890123456789"
    clock[0] += timedelta(hours=1)
    quote(db, "2026-09-15", "100.5")
    clock[0] += timedelta(hours=1)
    assert md.read_quotes(db, ISIN)["items"][0]["close"] == "100.5"
    assert md.read_quotes(db, ISIN, as_of=NOW + timedelta(minutes=30))["items"][0]["close"] == "100.5"
    assert md.read_quotes(db, ISIN, as_of=NOW - timedelta(days=3))["total"] == 0
    assert md.read_quotes(db, ISIN, limit=1, offset=1)["items"] == []
    assert md.read_quotes(db, ISIN)["total"] == 1
    assert md.instrument_detail(db, config, ISIN, NOW - timedelta(seconds=1))["profile"] is not None
    with pytest.raises(md.DataError, match="superseded"):
        md.read_quotes(db, ISIN, generation=first["id"])


@pytest.fixture
def api(db, config, monkeypatch):
    config.mongodb_database = db.name
    monkeypatch.setattr("main.enqueue_etf_import", Mock())
    publish = Mock()
    monkeypatch.setattr("instrument_routes.enqueue_acquisition", publish)
    with TestClient(create_app(config, client_factory=lambda *a, **kw: db.client)) as client:
        yield client, publish


def test_routes_auth_validation_and_db_only_reads(api, db, config):
    client, publish = api
    base = f"/instruments/{ISIN}"
    for url in ("/instruments/search?q=SXR8", "/instruments/acquired", "/instruments/budget", base, base + "/quotes"):
        assert client.get(url).status_code == 401
    assert client.post(base + "/acquisitions", json={**LISTING, "confirmed": True}).status_code == 401
    assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
    assert client.get("/instruments/search?q=SXR8").json()["items"][0]["instrument_id"] == ISIN
    assert client.get(base).json()["summary"]["history_status"] == "not_requested"
    assert client.get(base + "/quotes").json()["items"] == []
    acquired = client.get("/instruments/acquired")
    assert acquired.status_code == 200  # Static route must precede /{isin}.
    assert acquired.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
    for query in ("limit=0", "limit=101", "offset=-1", "offset=abc", "min_history_years=0", "min_history_years=10", "min_history_years=abc"):
        assert client.get("/instruments/acquired?" + query).status_code == 422
    for years in (5, 15, 30):
        assert client.get(f"/instruments/acquired?min_history_years={years}").status_code == 200
    assert client.get(base + "?as_of=2026-09-17T00:00:00").status_code == 422
    assert client.get("/instruments/search?q=.*").json()["items"] == []
    assert client.get("/instruments/search?q=%20%20").status_code == 422
    publish.assert_not_called()
    assert md.budget_status(db, config)["used"] == 0
    for body in ({**LISTING, "confirmed": False}, {**LISTING, "confirmed": 1}, {**LISTING}, {**LISTING, "symbol": "AAPL,MSFT", "confirmed": True}):
        assert client.post(base + "/acquisitions", json=body).status_code == 422
    assert client.post(base + "/acquisitions", json={**LISTING, "confirmed": True}, headers={"Origin": "https://evil.example"}).status_code == 403
    response = client.post(base + "/acquisitions", json={**LISTING, "confirmed": True})
    assert response.status_code == 202
    assert response.json()["job"]["status"] == "queued"
    publish.assert_called_once()
    assert "access_key" not in response.text and "private-crumb" not in response.text


def test_budget_route_reads_current_global_usage_without_consuming_quota(api, db, config, clock):
    client, publish = api
    assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
    db.etfs.delete_many({})  # Available without an instrument or acquired data.
    db.marketstack_usage.insert_one({"_id": "2026-09", "used": 7})
    for _ in range(2):
        response = client.get("/instruments/budget")
        assert response.status_code == 200
        assert response.json() == {"provider": "yfinance", "metered": False, "month": "2026-09", "limit": None, "used": 0, "remaining": None}
    assert db.marketstack_usage.find_one({"_id": "2026-09"})["used"] == 7
    clock[0] = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert client.get("/instruments/budget").json() == {"provider": "yfinance", "metered": False, "month": "2026-10", "limit": None, "used": 0, "remaining": None}
    publish.assert_not_called()


@pytest.mark.parametrize("currency", ["EUR", "USD", "CHF", "GBP", "RUB"])
def test_price_currency_api_persists_supported_values_without_acquisition(api, db, config, monkeypatch, currency):
    client, publish = api
    provider = Mock(side_effect=AssertionError("Currency edits must not call Marketstack"))
    monkeypatch.setattr(md, "provider_client", provider)
    config.market_data_max_calls_per_job = 1  # Manual metadata does not reserve any provider requests.
    catalogue = list(db.etfs.find())
    base = f"/instruments/{ISIN}"
    assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
    assert client.get(base).json()["price_currency_override"] is None
    assert db.instrument_state.find_one({"_id": ISIN}) is None

    response = client.patch(base + "/price-currency", json={"price_currency": currency})
    assert response.status_code == 200
    assert response.json() == {"price_currency_override": currency}
    assert db.instrument_state.find_one({"_id": ISIN}) == {"_id": ISIN, "price_currency_override": currency}
    fresh = client.get(base)
    assert fresh.status_code == 200
    assert fresh.json()["price_currency_override"] == currency
    assert client.get("/instruments/IE00B4L5Y983").json()["price_currency_override"] is None
    assert db.instrument_state.count_documents({}) == 1
    assert list(db.etfs.find()) == catalogue
    for collection in (db.instruments, db.quotes, db.raw_eod_pages, db.acquisition_jobs, db.marketstack_usage):
        assert collection.count_documents({}) == 0
    assert md.budget_status(db, config)["used"] == 0
    provider.assert_not_called()
    publish.assert_not_called()


def test_price_currency_api_requires_auth_and_same_origin(api, db, config):
    client, publish = api
    state = {"_id": ISIN, "price_currency_override": "GBP"}
    db.instrument_state.insert_one(deepcopy(state))
    url = f"/instruments/{ISIN}/price-currency"
    body = {"price_currency": "EUR"}
    assert client.patch(url, json=body).status_code == 401
    assert db.instrument_state.find_one({"_id": ISIN}) == state
    assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
    for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
        assert client.patch(url, json=body, headers=headers).status_code == 403
        assert db.instrument_state.find_one({"_id": ISIN}) == state
    assert md.budget_status(db, config)["used"] == 0
    assert db.acquisition_jobs.count_documents({}) == 0
    publish.assert_not_called()


@pytest.mark.parametrize("body", [
    {}, {"currency": "EUR"}, {"price_currency": "CAD"}, {"price_currency": "eur"},
    {"price_currency": " EUR "}, {"price_currency": ""}, {"price_currency": None},
    {"price_currency": True}, {"price_currency": 1}, {"price_currency": ["EUR"]},
    {"price_currency": {"$set": "EUR"}}, {"price_currency": "EUR", "extra": True}, [],
])
def test_price_currency_api_rejects_invalid_bodies_without_writing(api, db, config, body):
    client, publish = api
    state = {"_id": ISIN, "price_currency_override": "GBP"}
    db.instrument_state.insert_one(deepcopy(state))
    assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
    assert client.patch(f"/instruments/{ISIN}/price-currency", json=body).status_code == 422
    assert list(db.instrument_state.find()) == [state]
    assert md.budget_status(db, config)["used"] == 0
    assert db.acquisition_jobs.count_documents({}) == 0
    publish.assert_not_called()


def test_price_currency_api_requires_catalogue_instrument(api, db, config):
    client, publish = api
    assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
    response = client.patch("/instruments/US0378331005/price-currency", json={"price_currency": "EUR"})
    assert response.status_code == 404
    assert response.json() == {"detail": "Instrument not found in the ETF catalogue"}
    assert db.instrument_state.count_documents({}) == 0
    assert db.acquisition_jobs.count_documents({}) == 0
    assert md.budget_status(db, config)["used"] == 0
    publish.assert_not_called()


def test_price_currency_updates_preserve_state_and_raw_observations_including_as_of(api, db, config, clock, monkeypatch):
    source = Provider([page([bar(), {**bar("2026-09-16"), "price_currency": "GBP"}])],
                      profile={"ticker": "SXR8.DE", "price_currency": "USD", "currency": "USD"})
    result, _ = collect(db, config, source)
    assert result["status"] == "succeeded"
    db.instrument_state.update_one({"_id": ISIN}, {"$set": {
        "listing_resolution": {"ticker": "SXR8", "source": "cached", "future": None},
        "future_metadata": {"preserved": [1, None]},
    }})
    other = {"_id": "IE00B4L5Y983", "listing": {"symbol": "EUNL.F", "exchange": "XFRA"},
             "price_currency_override": "RUB", "last_complete_quote_date": "2026-09-14"}
    db.instrument_state.insert_one(deepcopy(other))
    state = db.instrument_state.find_one({"_id": ISIN})
    assert state["last_complete_quote_date"] == "2026-09-16"
    collections = ("etfs", "instruments", "quotes", "raw_eod_pages", "acquisition_jobs", "marketstack_usage")
    stored = {name: list(db[name].find()) for name in collections}
    provider = Mock(side_effect=AssertionError("Currency edits and reads must not call Marketstack"))
    monkeypatch.setattr(md, "provider_client", provider)
    client, publish = api
    base = f"/instruments/{ISIN}"
    historical = {"as_of": NOW.isoformat()}
    assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
    detail = client.get(base).json()
    assert detail["price_currency_override"] is None
    quotes = client.get(base + "/quotes", params=historical).json()
    assert [item["price_currency"] for item in quotes["items"]] == [None, "GBP"]

    for currency in ("CHF", "EUR"):
        clock[0] += timedelta(hours=1)
        response = client.patch(base + "/price-currency", json={"price_currency": currency})
        assert response.status_code == 200
        assert response.json() == {"price_currency_override": currency}
        assert db.instrument_state.find_one({"_id": ISIN}) == {**state, "price_currency_override": currency}
        # The override is current shared metadata, not an as-of observation.
        for params in ({}, historical):
            fresh = client.get(base, params=params)
            assert fresh.status_code == 200
            assert fresh.json() == {**detail, "price_currency_override": currency}
        assert client.get(base + "/quotes", params=historical).json() == quotes
        assert {name: list(db[name].find()) for name in collections} == stored
        assert db.instrument_state.find_one({"_id": other["_id"]}) == other
        assert client.get(f"/instruments/{other['_id']}").json()["price_currency_override"] == "RUB"
    provider.assert_not_called()
    publish.assert_not_called()


@pytest.mark.parametrize("already_acquired", [False, True], ids=["initial", "update"])
def test_price_currency_override_survives_provider_acquisition(api, db, config, clock, already_acquired):
    if already_acquired:
        result, _ = collect(db, config)
        assert result["status"] == "succeeded"
    client, publish = api
    assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
    base = f"/instruments/{ISIN}"
    assert client.patch(base + "/price-currency", json={"price_currency": "CHF"}).status_code == 200
    used = md.budget_status(db, config)["used"]
    clock[0] += timedelta(seconds=1)
    source = Provider([page([{**bar("2026-09-16"), "price_currency": "GBP"}])],
                      profile={"ticker": "SXR8.DE", "price_currency": "USD"})
    result, source = collect(db, config, source)
    assert result["status"] == "succeeded"
    assert result["mode"] == "refresh"
    assert len(source.calls) == result["calls"] == 2
    assert md.budget_status(db, config)["used"] == used == 0
    state = db.instrument_state.find_one({"_id": ISIN})
    assert state["price_currency_override"] == "CHF"
    assert state["listing"] == LISTING
    assert state["last_complete_quote_date"] == "2026-09-16"
    fresh = client.get(base)
    assert fresh.status_code == 200
    assert fresh.json()["price_currency_override"] == "CHF"
    assert db.instruments.find_one({"job_id": result["id"]})["data"] == source.profile
    assert db.quotes.find_one({"job_id": result["id"]})["data"]["price_currency"] == "GBP"
    publish.assert_not_called()


def test_price_currency_override_is_shared_between_authenticated_users(api, db):
    client, publish = api
    admin = db.users.find_one({"login": "admin"})
    db.users.insert_one({"login": "reader", "password_hash": admin["password_hash"], "role": "user"})
    base = f"/instruments/{ISIN}"
    assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
    assert client.patch(base + "/price-currency", json={"price_currency": "EUR"}).status_code == 200
    assert client.post("/auth/logout").status_code == 204
    assert client.post("/auth/login", json={"login": "reader", "password": "admin"}).status_code == 200
    assert client.get(base).json()["price_currency_override"] == "EUR"
    assert client.patch(base + "/price-currency", json={"price_currency": "USD"}).status_code == 200
    assert client.post("/auth/logout").status_code == 204
    assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
    assert client.get(base).json()["price_currency_override"] == "USD"
    assert list(db.instrument_state.find()) == [{"_id": ISIN, "price_currency_override": "USD"}]
    publish.assert_not_called()


def test_acquired_api_returns_saved_data_without_provider_calls(api, db, config, monkeypatch):
    collect(db, config, Provider([page([bar("2025-09-16", "90"), bar("2026-09-14", "100"), bar("2026-09-15", "110"), bar("2026-09-16", "99")])]))
    provider = Mock(side_effect=AssertionError("Browsing must not call Marketstack"))
    monkeypatch.setattr(md, "provider_client", provider)
    used = md.budget_status(db, config)["used"]
    client, publish = api
    client.post("/auth/login", json={"login": "admin", "password": "admin"})
    response = client.get("/instruments/acquired?limit=1&offset=0")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["quote_count"] == 4
    stats = client.get(f"/instruments/{ISIN}/statistics").json()
    for key in ("annualized_mean_log_return", "annualized_volatility", "historical_year_return", "historical_year_volatility"):
        assert item[key] == stats[key]
        assert isinstance(item[key], float)
    assert item["historical_year_return"] == pytest.approx(0.1)
    for key in ("historical_year_start_date", "historical_year_end_date", "historical_year_price_basis",
                "historical_year_volatility_start_date", "historical_year_volatility_end_date",
                "historical_year_volatility_price_basis", "historical_year_volatility_return_count"):
        assert item[key] == stats[key]
    assert response.json()["total"] == 1
    assert "data" not in response.json()["items"][0]
    assert "access_key" not in response.text and "private-crumb" not in response.text
    assert md.budget_status(db, config)["used"] == used
    provider.assert_not_called()
    publish.assert_not_called()


def test_quote_api_has_exact_string_contract(api, db, config):
    client, _ = api
    collect(db, config)
    client.post("/auth/login", json={"login": "admin", "password": "admin"})
    response = client.get(f"/instruments/{ISIN}/quotes")
    assert response.status_code == 200
    assert response.json()["items"][0]["close"] == "101.1234567890123456789"
    assert response.json()["items"][0]["volume"] is None
    schema = client.get("/openapi.json").json()["components"]["schemas"]["QuoteResponse"]
    assert "type" not in schema["properties"]["close"]  # Permissive provider field, numeric values still exact strings.
