"""Opt-in real Mongo tests; Yahoo responses are fixtures, never live HTTP."""
from datetime import timedelta
from unittest.mock import Mock

from pydantic import SecretStr

import market_data as md
from test_market_data_integration import environment, ISIN, LISTING, NOW
from test_yfinance_acquisition import Yahoo, quote
from test_market_data import page


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
