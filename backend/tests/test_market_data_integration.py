"""Opt-in disposable Mongo/Rabbit tests. Every provider response is synthetic."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
from queue import Queue
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from celery.contrib.testing.worker import start_worker
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

import market_data as md
import tasks
from celery_app import ACQUISITION_TASK, create_celery_app
from config import Settings
from main import create_app
from marketstack import MarketstackClient, MarketstackError

ISIN = "IE00B5BMR087"
LISTING = {"symbol": "SXR8.DE", "exchange": "XETR"}
NOW = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)


@pytest.fixture
def environment(monkeypatch):
    if not os.environ.get("TEST_MONGODB_URI"):
        pytest.skip("Set TEST_MONGODB_URI for isolated real Mongo tests")
    name = "hedgehoog_market_test_" + uuid4().hex
    settings = Settings(_env_file=None, mongodb_uri=os.environ["TEST_MONGODB_URI"], mongodb_database=name,
                        celery_broker_url=os.environ.get("TEST_CELERY_BROKER_URL", "amqp://unused//"),
                        jwt_secret="isolated-integration-secret-" * 3, marketstack_api_key="synthetic-only",
                        marketstack_monthly_budget=30)
    monkeypatch.setattr(md, "utcnow", lambda: NOW)
    with MongoClient(settings.mongodb_uri.get_secret_value(), serverSelectionTimeoutMS=5000, tz_aware=True) as mongo:
        db = mongo[name]
        db.etfs.insert_one({"isin": ISIN, "name": "Synthetic ETF", "ticker": "SXR8", "inception_date": "2010-05-19", "import_date": "2026-09-17"})
        try:
            yield settings, db
        finally:
            mongo.drop_database(name)


def provider_fixture():
    calls = []

    def respond(request):
        calls.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("tickerslist"):
            return httpx.Response(200, json={"pagination": {"limit": 1000, "offset": 0, "count": 2, "total": 2}, "data": [
                {"ticker": "SXR8F.DE", "name": "Wrong prefix", "has_eod": True, "stock_exchange": {"mic": "XETR"}},
                {"ticker": "SXR8.DE", "name": "Synthetic ETF", "has_eod": True, "stock_exchange": {"mic": "XETR"}},
            ]})
        if request.url.path.endswith("tickerinfo"):
            return httpx.Response(200, json={"data": {"ticker": "SXR8.DE", "name": "Synthetic ETF", "stock_exchanges": [], "unknown_nested": {"preserve": True}}})
        day = "2026-09-16" if request.url.params["date_from"] == "2026-09-16" else "2026-09-15"
        return httpx.Response(200, json={"pagination": {"limit": 1000, "offset": 0, "count": 1, "total": 1}, "data": [{"symbol": "SXR8.DE", "exchange": "XETR", "date": day + "T00:00:00+0000", "open": 100.01, "high": 103.00, "low": 99.5, "close": 102.25, "volume": 125.0, "adj_open": None, "adj_high": None, "adj_low": None, "adj_close": 102.25, "adj_volume": None, "price_currency": None}]})

    transport = httpx.MockTransport(respond)

    def factory(key, before_request):
        return MarketstackClient(key, before_request=before_request, transport=transport)

    return factory, calls


@pytest.mark.parametrize("listing", [LISTING, None])
def test_real_mongo_atomic_job_gate_budget_and_observations(environment, listing):
    settings, db = environment
    publish = Mock()
    md.ensure_indexes(db)

    def request(_):
        return md.request_acquisition(db, settings, ISIN, listing, "update", "user", publish)

    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(request, range(16)))
    assert len({job["id"] for job in jobs}) == 1
    publish.assert_called_once()
    assert db.acquisition_jobs.count_documents({"active": True}) == 1
    assert md.acquired_instruments(db)["total"] == 0
    with pytest.raises(DuplicateKeyError):
        db.acquisition_jobs.insert_one({"instrument_id": ISIN, "active": True})
    factory, calls = provider_fixture()
    result = md.run_acquisition(db, settings, jobs[0]["id"], factory)
    initial_calls = 3 if listing is None else 2
    assert result["status"] == "succeeded" and len(calls) == initial_calls
    assert db.instrument_state.find_one()["listing"] == LISTING
    assert db.instruments.find_one()["data"]["unknown_nested"]["preserve"] is True
    second = request(None)
    assert second["mode"] == "update"
    assert md.run_acquisition(db, settings, second["id"], factory)["status"] == "succeeded"
    assert db.instruments.count_documents({}) == 2
    acquired = md.acquired_instruments(db, limit=1)
    assert acquired == {"total": 1, "limit": 1, "offset": 0, "items": [{
        "instrument_id": ISIN, "isin": ISIN, "name": "Synthetic ETF", "ticker": "SXR8",
        "listing": LISTING, "collected_at": "2026-09-17T12:00:00Z", "quote_count": 2,
        "first_date": "2026-09-15", "last_date": "2026-09-16",
        "annualized_mean_log_return": 0.0, "annualized_volatility": None,
    }]}
    assert md.acquired_instruments(db, limit=1, offset=1)["items"] == []
    assert db.quotes.count_documents({}) == 2
    assert md.read_quotes(db, ISIN)["total"] == 2
    # Recreating the partial unique index is genuinely idempotent on Mongo.
    md.ensure_indexes(db)
    settings.marketstack_monthly_budget = 7

    def reserve(_):
        try:
            md.reserve_credit(db, settings)
            return True
        except MarketstackError:
            return False

    with ThreadPoolExecutor(max_workers=10) as pool:
        reservations = list(pool.map(reserve, range(20)))
    assert sum(reservations) == 7 - initial_calls - 2
    assert sum(path.endswith("tickerslist") for path, _ in calls) == (1 if listing is None else 0)
    assert md.budget_status(db, settings)["used"] == 7


def test_real_broker_request_poll_update_and_duplicate_delivery(environment, monkeypatch):
    if not os.environ.get("TEST_CELERY_BROKER_URL"):
        pytest.skip("Set TEST_CELERY_BROKER_URL for isolated RabbitMQ delivery")
    settings, db = environment
    queue = db.name
    provider, calls = provider_fixture()
    results = Queue()
    real_run = md.run_acquisition

    def observed_run(database, config, job_id):
        result = real_run(database, config, job_id, provider)
        results.put(result)
        return result

    monkeypatch.setattr(tasks, "TaskSettings", lambda: settings)
    monkeypatch.setattr(tasks, "run_acquisition", observed_run)
    monkeypatch.setattr("main.enqueue_etf_import", Mock())
    with create_celery_app(settings) as worker_app:
        worker_app.conf.task_default_queue = queue

        def publish(config, job_id):
            return worker_app.send_task(ACQUISITION_TASK, args=[job_id], task_id=job_id, queue=queue).id

        monkeypatch.setattr("instrument_routes.enqueue_acquisition", publish)
        try:
            with TestClient(create_app(settings)) as client:
                assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
                path = f"/instruments/{ISIN}"
                response = client.post(path + "/acquisitions", json={"confirmed": True})
                assert response.status_code == 202
                first_id = response.json()["job"]["id"]
                assert client.get(path).json()["acquisition"]["status"] == "queued"
                assert db.quotes.count_documents({}) == 0 and not calls
                duplicate = client.post(path + "/acquisitions", json={"confirmed": True})
                assert duplicate.json()["job"]["id"] == first_id
                with start_worker(worker_app, pool="solo", concurrency=1, perform_ping_check=False, shutdown_timeout=15):
                    assert results.get(timeout=25)["status"] == "succeeded"
                    detail = client.get(path).json()
                    assert detail["acquisition"]["status"] == "succeeded"
                    assert detail["summary"]["history_status"] == "partial"
                    assert detail["listing"] == LISTING
                    assert detail["listing_resolution"]["source"] == "tickerslist"
                    quote = client.get(path + "/quotes").json()["items"][0]
                    assert quote["date"] == "2026-09-15" and quote["open"] == "100.01"
                    assert quote["adj_open"] is None and quote["price_currency"] is None
                    updated = client.post(path + "/acquisitions", json={"confirmed": True})
                    assert updated.status_code == 202 and updated.json()["job"]["mode"] == "update"
                    assert results.get(timeout=25)["status"] == "succeeded"
                    assert calls[-1][1]["date_from"] == "2026-09-16"
                    assert client.get(path + "/quotes").json()["total"] == 2
                    assert db.instruments.count_documents({}) == 2
                    publish(settings, first_id)
                    assert results.get(timeout=25)["status"] == "succeeded"
                    assert len(calls) == 5  # Lookup once; updates/delivery reuse mapping.
                    assert sum(path.endswith("tickerslist") for path, _ in calls) == 1
        finally:
            with worker_app.connection_for_write() as connection:
                binding = worker_app.amqp.queues[queue].bind(connection)
                binding.delete()
                binding.exchange.delete()
