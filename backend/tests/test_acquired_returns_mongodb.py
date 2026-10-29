"""Opt-in disposable real-Mongo return membership and read-cost regression."""
import os
from datetime import date, datetime, timedelta, timezone
from time import perf_counter
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pymongo import MongoClient

import market_data as md
import return_statistics

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_MONGODB_URI"), reason="Set TEST_MONGODB_URI for disposable real MongoDB tests")


def test_return_membership_cold_warm_and_combined_history(monkeypatch):
    mongo = MongoClient(os.environ["TEST_MONGODB_URI"], serverSelectionTimeoutMS=5000)
    db = mongo[f"returns_filter_test_{uuid4().hex}"]
    cutoff = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(md, "utcnow", lambda: cutoff)
    monkeypatch.setattr(md, "provider_client", Mock(side_effect=AssertionError("No provider reads")))
    try:
        md.ensure_indexes(db)
        for index in range(60):
            isin = f"IE{index:010d}"
            listing = {"symbol": f"DEMO{index}", "exchange": "XETR"}
            generation = f"synthetic-{index}"
            metadata = {"provider": "yfinance", "job_id": generation}
            db.instruments.insert_one({"instrument_id": isin, **listing, **metadata, "published": True, "collected_at": cutoff})
            db.instrument_state.insert_one({"_id": isin, "listing": listing})
            db.acquisition_jobs.insert_one({"_id": generation, "instrument_id": isin, "listing": listing,
                                            "provider": "yfinance", "published": True, "published_at": cutoff})
            days = [date(2025, 9, 16) + timedelta(days=offset) for offset in range(366)]
            if index % 3 == 0:
                days.insert(0, date(2010, 1, 1))
            quotes = []
            for position, day in enumerate(days):
                close = 100 + position / 10 if index % 2 else 200 - position / 10
                quotes.append({"instrument_id": isin, **listing, **metadata, "date": day.isoformat(), "collected_at": cutoff,
                               "data": {"date": day.isoformat(), "close": close,
                                        "price_currency": "EUR", "price_basis": "provider_split_adjusted"}})
            db.quotes.insert_many(quotes)
        histories = Mock(wraps=return_statistics._latest_rows)
        dates = Mock(wraps=md.quote_dates)
        refresh = Mock(wraps=return_statistics.refresh_statistics)
        monkeypatch.setattr(return_statistics, "_latest_rows", histories)
        monkeypatch.setattr(return_statistics, "refresh_statistics", refresh)
        monkeypatch.setattr(md, "quote_dates", dates)
        metrics = []
        for years, expected, candidates in [(None, 30, 60), (5, 10, 20)]:
            db.instrument_statistics.delete_many({})
            previous = None
            for cache in ("cold", "warm"):
                histories.reset_mock()
                refresh.reset_mock()
                dates.reset_mock()
                start = perf_counter()
                page = md.acquired_instruments(db, limit=7, offset=7, min_history_years=years, hide_negative_returns=True)
                elapsed = perf_counter() - start
                assert page["total"] == expected
                assert len(page["items"]) == min(7, expected - 7)
                assert all(item["historical_year_return"] >= 0 and item["annualized_mean_log_return"] >= 0 for item in page["items"])
                assert histories.call_count == (candidates if cache == "cold" else 0)
                assert refresh.call_count == candidates
                assert dates.call_count == len(page["items"])
                if previous is not None:
                    assert page == previous
                previous = page
                metrics.append({"history_years": years, "cache": cache, "seconds": round(elapsed, 4),
                                "candidate_statistics_validations": refresh.call_count, "full_history_reads": histories.call_count,
                                "page_date_enumerations": dates.call_count, "filtered_total": page["total"]})
        print({"scenario": "real Mongo synthetic 60 ETFs with 366 daily quotes each", "reads": metrics,
               "limitations": "Single local disposable Mongo, synthetic sparse long-history endpoints; not production sizing or transactional cross-page snapshot."})
    finally:
        mongo.drop_database(db.name)
        mongo.close()
