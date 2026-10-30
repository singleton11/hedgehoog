"""Synthetic bounded-work contracts, not production latency measurements."""
from datetime import timedelta
from unittest.mock import Mock

import pytest
from pymongo.errors import ConnectionFailure

import market_data as md
import return_statistics as rs
from test_market_data import ISIN, LISTING, NOW, Provider, bar, collect, config, db, clock, page, seed_yahoo


def positive_rows():
    return [bar("1990-01-01", "80"), bar("2025-09-16", "90"), bar("2026-09-14", "100"),
            bar("2026-09-15", "110"), bar("2026-09-16", "121")]


def attach_summary(db, isin, generation):
    dates = sorted({row["date"] for row in db.quotes.find({"job_id": generation})})
    summary = {"version": 1, "instrument_id": isin, "listing": LISTING, "generation": generation,
               "observations": len(dates), "quote_count": len(dates), "first_date": dates[0], "last_date": dates[-1]}
    db.acquisition_jobs.update_one({"_id": generation}, {"$set": {"history_summary": summary}})


@pytest.mark.parametrize("years", [None, 5, 15, 30])
@pytest.mark.parametrize("returns", [False, True])
def test_warm_summaries_skip_all_quote_reads_and_calculations(db, monkeypatch, years, returns):
    for index in range(53):
        isin = f"IE{index:010d}"
        seed_yahoo(db, isin, rows=positive_rows())
        attach_summary(db, isin, isin)
    cold = md.acquired_instruments(db, limit=2, offset=2, min_history_years=years, hide_negative_returns=returns)
    assert cold["total"] == 53
    assert [row["instrument_id"] for row in cold["items"]] == ["IE0000000002", "IE0000000003"]
    refresh = Mock(side_effect=AssertionError("Valid summaries must not recalculate"))
    monkeypatch.setattr(rs, "refresh_statistics", refresh)
    for operation in ("aggregate", "count_documents", "distinct", "find", "find_one"):
        monkeypatch.setattr(db.quotes, operation, Mock(side_effect=AssertionError("Warm summary scanned quotes")))
    profiles = Mock(wraps=db.instruments.find)
    caches = Mock(wraps=db.instrument_statistics.find)
    monkeypatch.setattr(db.instruments, "find", profiles)
    monkeypatch.setattr(db.instrument_statistics, "find", caches)
    warm = md.acquired_instruments(db, limit=2, offset=2, min_history_years=years, hide_negative_returns=returns)
    assert warm == cold
    # mongomock implements server-side $lookup via internal find() calls;
    # count only the application's batched payload hydration commands.
    hydration = [call for call in profiles.call_args_list if "$or" in call.args[0]]
    assert len(hydration) == (2 if years is not None or returns else 1)
    assert caches.call_count == (2 if returns else 1)
    refresh.assert_not_called()
    assert all(not job.get("reader_leases") for job in db.acquisition_jobs.find())


def test_legacy_cache_validation_is_batched_and_hydration_reuses_coverage(db, monkeypatch):
    for index in range(51):
        seed_yahoo(db, f"IE{index:010d}", rows=positive_rows())
    cold = md.acquired_instruments(db, limit=1, hide_negative_returns=True)
    db.acquisition_jobs.update_many({}, {"$unset": {"history_summary": ""}})  # Simulate pre-summary publications.
    aggregates = Mock(wraps=db.quotes.aggregate)
    monkeypatch.setattr(db.quotes, "aggregate", aggregates)
    monkeypatch.setattr(db.quotes, "count_documents", Mock(side_effect=AssertionError("Per-candidate count")))
    monkeypatch.setattr(md, "quote_dates", Mock(side_effect=AssertionError("Full page dates")))
    monkeypatch.setattr(rs, "_latest_rows", Mock(side_effect=AssertionError("Full history read")))
    assert md.acquired_instruments(db, limit=1, hide_negative_returns=True) == cold
    assert aggregates.call_count == 2
    assert all(job.get("history_summary") for job in db.acquisition_jobs.find())


@pytest.mark.parametrize("change", ["instrument", "listing", "generation", "cutoff", "currency", "fill", "splits", "version", "coverage", "missing"])
def test_invalid_summaries_use_original_calculation(db, config, monkeypatch, clock, change):
    collect(db, config, Provider([page(positive_rows())]))
    before = md.acquired_instruments(db, hide_negative_returns=True)
    cached = db.instrument_statistics.find_one({"_id": ISIN})
    if change in ("instrument", "listing", "generation", "version"):
        value = {"instrument": "OTHER", "listing": {**LISTING, "symbol": "OTHER"}, "generation": "OTHER", "version": 0}[change]
        field = "instrument_id" if change == "instrument" else change
        db.instrument_statistics.update_one({"_id": ISIN}, {"$set": {f"signature.{field}": value}})
    elif change == "cutoff":
        clock[0] -= timedelta(days=2)
    elif change == "currency":
        md.set_price_currency(db, ISIN, "USD")
    elif change == "fill":
        db.app_settings.insert_one({"_id": "market_data", "method": "normal", "seed": 42})
    elif change == "splits":
        db.split_snapshots.update_one({"_id": cached["signature"]["generation"]}, {"$set": {"error": "Synthetic revised split evidence"}})
    elif change == "coverage":
        db.instrument_statistics.update_one({"_id": ISIN}, {"$unset": {"statistics.saved_history": ""}})
    else:
        db.instrument_statistics.delete_one({"_id": ISIN})
    refresh = Mock(wraps=rs.refresh_statistics)
    monkeypatch.setattr(rs, "refresh_statistics", refresh)
    actual = md.acquired_instruments(db, hide_negative_returns=True)
    assert refresh.call_count == 1
    if change == "cutoff":
        assert actual["total"] == 0  # No comparable year-ago endpoint at this earlier cutoff.
        earlier = md.acquired_instruments(db)["items"][0]
        assert earlier["last_date"] == "2026-09-15"
        assert earlier["quote_count"] == 4
    else:
        assert actual == before
    assert all(not job.get("reader_leases") for job in db.acquisition_jobs.find())


def test_batch_cache_database_failure_and_warm_publication_race_are_not_hidden(db, monkeypatch):
    seed_yahoo(db, rows=positive_rows())
    attach_summary(db, ISIN, ISIN)
    md.acquired_instruments(db, hide_negative_returns=True)
    find = db.instrument_statistics.find
    monkeypatch.setattr(db.instrument_statistics, "find", Mock(side_effect=ConnectionFailure("synthetic offline")))
    with pytest.raises(ConnectionFailure, match="synthetic offline"):
        md.acquired_instruments(db, hide_negative_returns=True)
    def publish_after_cache(*args, **kwargs):
        result = list(find(*args, **kwargs))
        seed_yahoo(db, generation="new", collected_at=NOW + timedelta(seconds=1), rows=positive_rows())
        return iter(result)
    monkeypatch.setattr(db.instrument_statistics, "find", publish_after_cache)
    with pytest.raises(md.DataError, match="superseded"):
        md.acquired_instruments(db, hide_negative_returns=True)
    assert all(not job.get("reader_leases") for job in db.acquisition_jobs.find())


def test_writer_attaches_exact_coverage_only_to_published_generation(db, config):
    result, _ = collect(db, config, Provider([page(positive_rows())]))
    job = db.acquisition_jobs.find_one({"_id": result["id"]})
    summary = md.published_history_summary({"_id": ISIN, "listing": LISTING, "job_id": job["_id"],
                                            "history_summary": job["history_summary"]}, NOW)
    assert summary["quote_count"] == summary["observations"] == 5
    assert (summary["first_date"], summary["last_date"]) == ("1990-01-01", "2026-09-16")
    assert job["published"]


@pytest.mark.parametrize("bad", ["not-an-object", {"first_date": "invalid"}, {"version": 1, "first_date": "2026-01-01", "last_date": "2026-02-30"}])
def test_malformed_publication_summaries_fall_back_to_bounded_probes(db, bad):
    seed_yahoo(db, rows=positive_rows())
    db.acquisition_jobs.update_one({"_id": ISIN}, {"$set": {"history_summary": bad}})
    item = md.acquired_instruments(db, min_history_years=30, hide_negative_returns=True)["items"][0]
    assert item["quote_count"] == 5 and item["first_date"] == "1990-01-01"


def test_raw_coverage_not_usable_prices_and_duplicate_dates_count_once(db):
    seed_yahoo(db, rows=[bar("1990-01-01", None), *positive_rows()[1:], bar("2026-09-17", None)])
    row = db.quotes.find_one({"date": "2026-09-16"})
    row.pop("_id")
    db.quotes.insert_one(row)
    item = md.acquired_instruments(db, min_history_years=30)["items"][0]
    assert (item["quote_count"], item["first_date"], item["last_date"]) == (6, "1990-01-01", "2026-09-17")
    assert db.acquisition_jobs.find_one({"_id": ISIN}).get("history_summary") is None


def test_legacy_upgrade_stores_full_generation_not_earlier_cutoff(db, clock):
    seed_yahoo(db, rows=[*positive_rows(), bar("2026-09-18", "125")])
    earlier = md.acquired_instruments(db)["items"][0]
    assert earlier["quote_count"] == 5
    assert db.acquisition_jobs.find_one({"_id": ISIN})["history_summary"]["last_date"] == "2026-09-18"
    clock[0] += timedelta(days=1)
    later = md.acquired_instruments(db)["items"][0]
    assert (later["quote_count"], later["last_date"]) == (6, "2026-09-18")
