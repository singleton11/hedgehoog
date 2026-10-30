"""Opt-in real Mongo tests; Yahoo responses are fixtures, never live HTTP."""
from datetime import timedelta
import os
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pymongo import MongoClient

import market_data as md
from config import Settings
from test_market_data import ISIN, LISTING, NOW
from test_yfinance_acquisition import (
    Yahoo, quote,
    test_stale_listing_cache_write_cannot_replace_a_newer_job as assert_cache_takeover,
    test_cache_redelivery_does_not_reset_replacement_workers_watermark as assert_cache_redelivery,
)
from test_market_data import page


@pytest.fixture
def environment(monkeypatch):
    uri = os.environ.get("TEST_MONGODB_URI")
    if not uri:
        pytest.skip("Set TEST_MONGODB_URI for isolated real Mongo tests")
    name = "fixture_yahoo_" + uuid4().hex
    settings = Settings(_env_file=None, mongodb_uri=uri, mongodb_database=name,
                        celery_broker_url="amqp://unused//", jwt_secret="fixture-only-" * 4)
    monkeypatch.setattr(md, "utcnow", lambda: NOW)
    with MongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True) as client:
        db = client[name]
        db.etfs.insert_one({"isin": ISIN, "name": "Synthetic ETF", "ticker": "SXR8",
                            "inception_date": "2010-05-19", "import_date": "2026-09-17"})
        try:
            yield settings, db
        finally:
            client.drop_database(name)


@pytest.mark.parametrize("symbol", ["SPYI.DE", LISTING["symbol"]])
@pytest.mark.parametrize("newer_first", [True, False])
def test_real_mongo_listing_cache_compare_and_set(environment, monkeypatch, symbol, newer_first):
    settings, db = environment
    settings.market_data_provider = "yfinance"
    now = [NOW]
    monkeypatch.setattr(md, "utcnow", lambda: now[0])
    # Run the same forced interleaving against real Mongo's conditional upsert
    # and duplicate-key behavior, not just mongomock's approximation.
    assert_cache_takeover(db, settings, now, monkeypatch, symbol, newer_first)


def test_real_mongo_listing_cache_redelivery(environment, monkeypatch):
    settings, db = environment
    settings.market_data_provider = "yfinance"
    now = [NOW]
    monkeypatch.setattr(md, "utcnow", lambda: now[0])
    assert_cache_redelivery(db, settings, now, monkeypatch)


def test_real_mongo_published_refresh_and_failed_attempt_isolation(environment, monkeypatch):
    settings, db = environment
    settings.market_data_provider = "yfinance"
    now = [NOW]
    monkeypatch.setattr(md, "utcnow", lambda: now[0])

    def collect(rows, splits=None):
        job = md.request_acquisition(db, settings, ISIN, LISTING, "update", "test", Mock())
        provider = Yahoo([page(rows)], splits=splits)
        return md.run_acquisition(db, settings, job["id"], provider.factory)

    first = collect([quote("2026-09-14"), quote("2026-09-15")])
    assert first["status"] == "succeeded"
    first_page = md.read_quotes(db, ISIN, limit=1)
    cutoff = md.aware(NOW)
    now[0] += timedelta(seconds=1)
    second = collect([quote("2026-09-15", "200")])
    assert second["status"] == "succeeded"
    assert md.read_quotes(db, ISIN)["total"] == 1
    # as_of bounds quote dates in the latest vintage, never collection time.
    latest_page = md.read_quotes(db, ISIN, as_of=cutoff)
    assert first_page["total"] == 2
    assert latest_page["total"] == 1
    assert latest_page["items"][0]["close"] == "200"
    with pytest.raises(md.DataError) as error:
        md.read_quotes(db, ISIN, limit=1, offset=1, as_of=cutoff, generation=first_page["generation"])
    assert error.value.status == 409
    now[0] += timedelta(seconds=1)
    failed = collect([quote("2026-09-15", "999")], [md.MarketDataError("rate_limited", "Stopped.")])
    assert failed["status"] == "failed"
    assert md.read_quotes(db, ISIN)["items"][0]["close"] == "200"
    assert md.acquired_instruments(db)["items"][0]["quote_count"] == 1
    assert db.market_data_publications.count_documents({}) == 2
    assert db.marketstack_usage.count_documents({}) == 0


def test_real_incremental_materialization_survives_repeated_pruning(environment, monkeypatch):
    from market_data_retention import cleanup

    settings, db = environment
    now = [NOW]
    monkeypatch.setattr(md, "utcnow", lambda: now[0])
    expected = {"2026-09-14", "2026-09-15"}
    previous = None
    origin = None
    for index in range(4):
        # No network: Yahoo chart/action responses are explicit synthetic fixtures.
        last = max(expected)
        day = f"2026-09-{15 + index:02d}"
        rows = [quote(day) for day in sorted(expected)] if index == 0 else [quote(last), quote(day)]
        job = md.request_acquisition(db, settings, ISIN, LISTING, "update", "synthetic-test", Mock(), incremental=True)
        source = Yahoo([page(rows)])
        result = md.run_acquisition(db, settings, job["id"], source.factory)
        assert result["status"] == "succeeded"
        assert result["mode"] == ("refresh" if index == 0 else "incremental")
        origin = origin or result["id"]
        expected.add(day)
        cleanup(db, settings, ISIN)
        assert set(db.quotes.distinct("date")) == expected
        assert db.quotes.count_documents({}) == len(expected)
        assert md.read_quotes(db, ISIN)["total"] == len(expected)
        if previous:
            assert db.quotes.count_documents({"job_id": previous}) == 0
            assert db.acquisition_jobs.find_one({"_id": previous})["retention_state"] == "pruned"
            assert db.quotes.find_one({"date": "2026-09-14"})["source_job_id"] == origin
        previous = result["id"]
        now[0] += timedelta(days=1)
