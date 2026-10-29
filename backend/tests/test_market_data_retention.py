"""Synthetic retention fixtures: no provider, broker or production access."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import mongomock
import pytest
from pymongo.errors import ConnectionFailure

import market_data as md
import market_data_retention as retention
from config import TaskSettings

ISIN = "FIXTURE-ETF"
LISTING = {"symbol": "FIXTURE.DE", "exchange": "XETR"}
NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


@pytest.fixture
def db(monkeypatch):
    database = mongomock.MongoClient(tz_aware=True).retention_fixture
    md.ensure_indexes(database)
    # mongomock incorrectly recreates existing partial indexes on every call.
    monkeypatch.setattr(md, "ensure_indexes", lambda db: None)
    monkeypatch.setattr(md, "utcnow", lambda: NOW)
    return database


def seed(db, job_id, order, *, provider="yfinance", published=True, status="succeeded", active=False):
    job = {"_id": job_id, "instrument_id": ISIN, "listing": LISTING,
           "requested_at": NOW + timedelta(seconds=order), "published_at": NOW + timedelta(seconds=order),
           "provider": provider, "published": published, "status": status, "active": active}
    db.acquisition_jobs.insert_one(job)
    db.market_data_publications.insert_one({"_id": job_id, "instrument_id": ISIN, "provider": provider})
    for collection in retention.PAYLOADS:
        db[collection].insert_one({"_id": job_id, "job_id": job_id, "instrument_id": ISIN,
                                   "provider": provider, **LISTING, "date": "2020-01-02"})
    return job


def settings(**kwargs):
    return TaskSettings(_env_file=None, **kwargs)


def test_latest_only_cleanup_is_idempotent_bounded_and_preserves_exclusions(db):
    seed(db, "old", 0)
    seed(db, "latest", 1)
    db.quotes.insert_one({"instrument_id": ISIN, "provider": "yfinance", "job_id": "old", "date": "2020-01-03"})
    for name in ("etfs", "manual_splits", "customer_plans", "calibration_results", "portfolios"):
        db[name].insert_one({"_id": "excluded", "instrument_id": ISIN, "payload": [1, 2, 3]})
    db.instrument_statistics.insert_one({"_id": ISIN, "stale": True})
    before = retention.inventory(db)
    assert before["payloads"]["quotes"]["yahoo"] == 3
    assert db.quotes.count_documents({}) == 3  # Inventory never deletes.
    result = retention.cleanup(db, settings(market_data_retention_batch_size=1))
    assert result["items"][0]["counts"]["quotes"] == 2
    for name in retention.PAYLOADS:
        assert db[name].count_documents({"job_id": "old"}) == 0
        assert db[name].count_documents({"job_id": "latest"}) == 1
    assert db.acquisition_jobs.find_one({"_id": "old"})["retention_state"] == "pruned"
    assert db.market_data_publications.find_one({"_id": "old"})["retention_state"] == "pruned"
    assert db.instrument_statistics.find_one({"_id": ISIN}) is None
    for name in ("etfs", "manual_splits", "customer_plans", "calibration_results", "portfolios"):
        assert db[name].find_one({"_id": "excluded"})["payload"] == [1, 2, 3]
    assert retention.cleanup(db, settings())["items"][0]["counts"]["quotes"] == 0


def test_retention_off_and_active_incremental_base_are_protected(db):
    seed(db, "base", 0)
    seed(db, "new", 1, active=True, status="running", published=False)
    db.acquisition_jobs.update_one({"_id": "new"}, {"$set": {"base_generation": "base"}})
    assert retention.cleanup(db, settings(market_data_retention_enabled=False))["disabled"]
    assert retention.cleanup(db, settings())["items"][0]["skipped"]
    assert db.quotes.count_documents({"job_id": "base"}) == 1


def test_reader_protection_and_completion_conflict_on_refresh(db):
    seed(db, "old", 0)
    with pytest.raises(md.DataError) as error:
        with retention.reader(db, ISIN, LISTING) as generation:
            assert generation == "old"
            seed(db, "new", 1)
            retention.cleanup(db, settings())
            assert db.quotes.count_documents({"job_id": "old"}) == 1
            assert db.acquisition_jobs.find_one({"_id": "old"})["retention_state"] == "retiring"
    assert error.value.status == 409
    retention.cleanup(db, settings())
    assert db.quotes.count_documents({"job_id": "old"}) == 0
    with pytest.raises(md.DataError):
        with retention.reader(db, ISIN, LISTING, "old"):
            pytest.fail("Superseded cursor must not acquire a lease")


def test_expired_reader_is_pruned_and_must_fail_completion(db, monkeypatch):
    seed(db, "old", 0)
    with pytest.raises(md.DataError):
        with retention.reader(db, ISIN, LISTING):
            seed(db, "new", 1)
            monkeypatch.setattr(md, "utcnow", lambda: NOW + timedelta(seconds=retention.READER_SECONDS + 1))
            retention.cleanup(db, settings())
            assert db.quotes.count_documents({"job_id": "old"}) == 0


def test_partial_cleanup_retry_preserves_durable_tombstone(db, monkeypatch):
    seed(db, "old", 0)
    seed(db, "latest", 1)
    original = db.raw_eod_pages.delete_many
    failure = Mock(side_effect=ConnectionFailure("synthetic restart"))
    monkeypatch.setattr(db.raw_eod_pages, "delete_many", failure)
    with pytest.raises(ConnectionFailure):
        retention.cleanup(db, settings())
    assert db.acquisition_jobs.find_one({"_id": "old"})["retention_state"] == "retiring"
    assert db.quotes.count_documents({"job_id": "old"}) == 0
    monkeypatch.setattr(db.raw_eod_pages, "delete_many", original)
    retention.cleanup(db, settings())
    assert db.acquisition_jobs.find_one({"_id": "old"})["retention_state"] == "pruned"
    assert db.raw_eod_pages.count_documents({"job_id": "old"}) == 0


def test_legacy_owned_jobs_payloads_usage_purged_without_ambiguous_or_customer_deletion(db):
    seed(db, "legacy", 0, provider="marketstack", published=False, active=True, status="queued")
    seed(db, "untagged-job", 1, provider="marketstack", published=False)
    db.acquisition_jobs.update_one({"_id": "untagged-job"}, {"$unset": {"provider": ""}})
    for name in retention.PAYLOADS:
        db[name].update_one({"job_id": "untagged-job"}, {"$unset": {"provider": ""}})
    db.quotes.insert_one({"_id": "ambiguous", "instrument_id": ISIN, "date": "1999-01-01"})
    db.marketstack_usage.insert_one({"_id": "fixture-month", "used": 100})
    report = retention.inventory(db)
    assert report["payloads"]["quotes"]["unattributed"] == 1
    retention.cleanup(db, settings())
    assert db.acquisition_jobs.count_documents({}) == 0
    assert db.marketstack_usage.count_documents({}) == 0
    assert list(db.quotes.find({}, {"_id": 1})) == [{"_id": "ambiguous"}]
    assert db.market_data_retention.count_documents({"provider": "marketstack", "state": "pruned"}) == 2


def test_terminal_unpublished_cleanup_does_not_remove_latest_or_source_provenance(db):
    seed(db, "latest", 0)
    seed(db, "failed", 1, published=False, status="failed")
    db.quotes.update_one({"job_id": "latest"}, {"$set": {"source_job_id": "historical-source"}})
    retention.cleanup(db, settings())
    assert db.quotes.find_one({"job_id": "latest"})["source_job_id"] == "historical-source"
    assert db.quotes.count_documents({"job_id": "failed"}) == 0


def test_retired_worker_delivery_never_constructs_provider(db):
    seed(db, "retired", 0, provider="marketstack", published=False, active=True, status="queued")
    factory = Mock(side_effect=AssertionError("Provider calls forbidden"))
    assert md.run_acquisition(db, settings(), "retired", factory)["status"] == "retired"
    factory.assert_not_called()


def test_maintenance_request_conflict_and_expired_gate_resume(db):
    db.etfs.insert_one({"isin": ISIN, "inception_date": "2020-01-01", "ticker": "FIXTURE"})
    gate = retention._gate(db, ISIN)
    with pytest.raises(md.DataError) as exc:
        md.request_acquisition(db, settings(), ISIN, LISTING, "update", "fixture", Mock())
    assert exc.value.status == 409
    db.acquisition_jobs.update_one({"_id": gate["_id"]}, {"$set": {"lease_until": NOW - timedelta(seconds=1)}})
    resumed = retention._gate(db, ISIN)
    assert resumed["_id"] == gate["_id"] and resumed["owner"] != gate["owner"]
    with pytest.raises(md.LeaseLost):
        retention._heartbeat(db, gate)
