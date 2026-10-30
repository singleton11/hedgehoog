"""Latest-vintage protections for proxy, benchmark, recommendations and splits."""
from datetime import timedelta

import pytest

import market_data as md
from manual_splits import read_splits
from performance_settings import acquired_proxy_index
from portfolio_performance import benchmark_history
from portfolio_recommendations import market_snapshot
from test_latest_history_contract import CUTOFF, ISIN, LISTING, NOW, history_db, publish

INDEX = "idx-00000000-0000-4000-8000-000000000001"


def test_proxy_and_benchmark_use_latest_vintage_before_collection_time(history_db):
    db = history_db
    listing = {"symbol": "^FIXTURE", "exchange": "YAHOO"}
    db.indices.insert_one({"instrument_id": INDEX, "symbol": "^FIXTURE", "name": "Synthetic index", "currency": "EUR"})
    db.instrument_state.insert_one({"_id": INDEX, "listing": listing})
    publish(db, "latest", isin=INDEX, listing=listing, close="110")
    assert acquired_proxy_index(db, INDEX, CUTOFF)["instrument_id"] == INDEX
    benchmark, marks = benchmark_history(db, INDEX, CUTOFF)
    assert benchmark["name"] == "Synthetic index"
    assert str(marks.at("2025-01-07")) == "110"


def test_recommendation_raw_close_ignores_collection_time(history_db):
    db = history_db
    publish(db, "latest", close="110")
    result = market_snapshot(db, {ISIN}, {}, CUTOFF)[ISIN]
    assert result["eligible"]
    assert result["price_eur"] == "110"
    assert result["price_date"] == "2025-01-07"


def test_split_api_filters_effective_dates_and_keeps_manual_audit(history_db):
    db = history_db
    publish(db, "latest")
    db.split_snapshots.update_one({"_id": "latest"}, {"$set": {"events": [
        {"date": "2025-01-06", "factor": "2"}, {"date": "2025-01-08", "factor": "3"},
    ]}})
    db.manual_splits.insert_one({"_id": "excluded-manual", "instrument_id": ISIN, **LISTING,
                                  "date": "2025-01-06", "factor": "7", "note": "Synthetic preserved audit", "created_at": CUTOFF})
    result = read_splits(db, ISIN, CUTOFF)
    assert result["supported"] is False
    assert result["provider"] == [{"date": "2025-01-06", "factor": "2"}]
    assert result["manual"][0]["factor"] == "7"
    assert db.manual_splits.count_documents({}) == 1


def test_latest_published_history_still_fills_derived_gaps_without_mutating_evidence(history_db):
    from return_statistics import refresh_statistics

    db = history_db
    publish(db, "latest")
    db.quotes.update_one({"job_id": "latest", "date": "2025-01-07"}, {"$set": {"data.close": None}})
    original = list(db.quotes.find())
    result, _ = refresh_statistics(db, ISIN)
    assert result["filled_quote_count"] == 1
    assert result["quote_count"] == 3
    assert result["price_count"] == 3
    assert result["return_count"] == 2
    assert list(db.quotes.find()) == original


def test_split_api_conflicts_if_refresh_between_two_snapshot_reads(history_db, monkeypatch):
    import split_adjustments
    db = history_db
    publish(db, "old")
    original = split_adjustments.load_provider_split_snapshot
    called = []

    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        if not called:
            called.append(True)
            publish(db, "new", order=1)
        return result

    monkeypatch.setattr(split_adjustments, "load_provider_split_snapshot", race)
    with pytest.raises(md.DataError) as exc:
        read_splits(db, ISIN, CUTOFF)
    assert exc.value.status == 409
