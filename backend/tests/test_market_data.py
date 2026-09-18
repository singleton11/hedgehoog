from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import mongomock
import pytest
from fastapi.testclient import TestClient
from pymongo.errors import ConnectionFailure, DuplicateKeyError

import market_data as md
from config import Settings
from main import create_app
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
    """No network; service tests exercise quota callbacks and all Mongo writes."""
    def __init__(self, pages=None, profile=None):
        self.pages = iter(pages or [page()])
        self.profile = profile if profile is not None else {"ticker": "SXR8.DE", "name": "Example", "address": {"city": ""}, "stock_exchanges": [], "future": {"value": None}}
        self.calls = []

    def factory(self, key, before_request):
        assert key == "test-marketstack-key"
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

    def eod_page(self, symbol, exchange, date_from, date_to, offset=0):
        self.before_request()
        self.calls.append(("eod", symbol, exchange, date_from, date_to, offset))
        result = next(self.pages)
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


@pytest.fixture
def clock(monkeypatch):
    current = [NOW]
    monkeypatch.setattr(md, "utcnow", lambda: current[0])
    return current


@pytest.fixture
def config():
    return Settings(_env_file=None, jwt_secret="test-secret-" * 5, marketstack_api_key="test-marketstack-key", marketstack_monthly_budget=30)


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


def test_search_latest_snapshot_and_escaped_literals(db):
    result = md.search_catalogue(db, "sxR8", 15)
    assert len(result) == 1
    assert result[0]["instrument_id"] == ISIN
    assert result[0]["ticker"] == "SXR8"
    assert "_id" not in result[0]
    assert md.search_catalogue(db, "Obsolete", 15) == []
    assert md.search_catalogue(db, ".*", 15) == []
    assert len(md.search_catalogue(db, "iShares", 1)) == 1


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
    assert provider.calls[1][3:] == ("2010-05-19", "2026-09-16", 0)
    assert md.budget_status(db, config)["used"] == 2
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
    assert result["mode"] == "update"
    assert source.calls[1][3:] == ("2026-09-16", "2026-09-16", 0)
    assert db.instruments.count_documents({}) == 2
    assert md.read_quotes(db, ISIN)["total"] == 2
    assert md.instrument_detail(db, config, ISIN)["profile"]["data"]["new_field"] == 123
    assert md.instrument_detail(db, config, ISIN, NOW + timedelta(minutes=30))["profile"]["data"]["name"] == "Example"


def test_empty_increment_does_not_skip_late_published_quotes(db, config):
    collect(db, config)
    result, source = collect(db, config, Provider([page([])]))
    assert result["status"] == "succeeded" and "no quotes" in result["warning"]
    assert db.instrument_state.find_one()["last_complete_quote_date"] == "2026-09-15"
    new = enqueue(db, config)
    assert db.acquisition_jobs.find_one({"_id": new["id"]})["date_from"] == "2026-09-16"


def test_explicit_backfill_queries_only_the_older_gap(db, config):
    collect(db, config)
    job = enqueue(db, config, mode="backfill")
    provider = Provider([page([bar("2020-01-02")])])
    result = md.run_acquisition(db, config, job["id"], provider.factory)
    assert result["mode"] == "backfill"
    assert provider.calls[1][3:5] == ("2010-05-19", "2026-09-14")
    assert db.instrument_state.find_one()["last_complete_quote_date"] == "2026-09-15"
    assert md.read_quotes(db, ISIN)["items"][0]["date"] == "2020-01-02"


def test_failed_pagination_does_not_advance_watermark(db, config):
    provider = Provider([page([bar("2020-01-02")], total=2), MarketstackError("quota", "Quota exhausted")])
    result, _ = collect(db, config, provider)
    assert result["status"] == "failed"
    assert db.quotes.count_documents({}) == 1
    assert db.instrument_state.find_one()["last_complete_quote_date"] is None
    new = enqueue(db, config)
    assert db.acquisition_jobs.find_one({"_id": new["id"]})["date_from"] == "2010-05-19"
    assert new["mode"] == "initial"


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
    assert provider.calls == []  # Re-index the persisted page without another HTTP call.
    assert db.instruments.count_documents({}) == db.quotes.count_documents({}) == 1
    assert result["calls"] == 2


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


def test_budget_and_job_limits_stop_before_http(db, config):
    config.marketstack_monthly_budget = 1
    result, source = collect(db, config)
    assert result["status"] == "failed"
    assert len(source.calls) == 1
    assert result["calls"] == 1
    assert md.budget_status(db, config)["remaining"] == 0
    assert db.quotes.count_documents({}) == 0
    with pytest.raises(md.DataError, match="budget"):
        enqueue(db, config)
    config.marketstack_monthly_budget = 30
    config.marketstack_max_calls_per_job = 1
    result, source = collect(db, config)
    assert result["status"] == "failed" and "safety limit" in result["error"]
    assert len(source.calls) == 1


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
    original = db.quotes.find_one()
    revised = {**original, "_id": "revision", "job_id": "revision", "collected_at": NOW + timedelta(hours=1), "data": bar(close="100.5")}
    db.quotes.insert_one(revised)
    clock[0] += timedelta(hours=2)
    assert md.read_quotes(db, ISIN)["items"][0]["close"] == "100.5"
    assert md.read_quotes(db, ISIN, as_of=NOW + timedelta(minutes=30))["items"][0]["close"] == "101.1234567890123456789"
    assert md.read_quotes(db, ISIN, as_of=NOW - timedelta(seconds=1))["total"] == 0
    assert md.read_quotes(db, ISIN, limit=1, offset=1)["items"] == []
    assert md.read_quotes(db, ISIN)["total"] == 1
    assert md.instrument_detail(db, config, ISIN, NOW - timedelta(seconds=1))["profile"] is None


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
    for url in ("/instruments/search?q=SXR8", base, base + "/quotes"):
        assert client.get(url).status_code == 401
    assert client.post(base + "/acquisitions", json={**LISTING, "confirmed": True}).status_code == 401
    assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
    assert client.get("/instruments/search?q=SXR8").json()["items"][0]["instrument_id"] == ISIN
    assert client.get(base).json()["summary"]["history_status"] == "not_requested"
    assert client.get(base + "/quotes").json()["items"] == []
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
    assert config.marketstack_api_key.get_secret_value() not in response.text


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
