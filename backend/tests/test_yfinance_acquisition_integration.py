"""Opt-in real Mongo tests; Yahoo responses are fixtures, never live HTTP."""
from datetime import timedelta
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

import market_data as md
from test_market_data_integration import environment, ISIN, LISTING, NOW
from test_yfinance_acquisition import (
    Yahoo, quote,
    test_stale_listing_cache_write_cannot_replace_a_newer_job as assert_cache_takeover,
    test_cache_redelivery_does_not_reset_replacement_workers_watermark as assert_cache_redelivery,
)
from test_market_data import page


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
    settings.marketstack_api_key = SecretStr("")
    settings.marketstack_monthly_budget = 0
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
    old_second_page = md.read_quotes(db, ISIN, limit=1, offset=1, as_of=cutoff)
    assert first_page["total"] == old_second_page["total"] == 2
    assert old_second_page["items"][0]["close"] == "100"
    now[0] += timedelta(seconds=1)
    failed = collect([quote("2026-09-15", "999")], [md.MarketDataError("rate_limited", "Stopped.")])
    assert failed["status"] == "failed"
    assert md.read_quotes(db, ISIN)["items"][0]["close"] == "200"
    assert md.acquired_instruments(db)["items"][0]["quote_count"] == 1
    assert db.market_data_publications.count_documents({}) == 2
    assert db.marketstack_usage.count_documents({}) == 0
