"""Bound payload work by page size, not by the saved ETF inventory."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import mongomock
import pytest

import market_data as md
import market_data_retention as retention
import return_statistics

NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


def seed(db, count):
    for index in range(count):
        isin = f"IE{index:010d}"
        listing = {"symbol": isin, "exchange": "TEST"}
        job = f"job-{index}"
        db.acquisition_jobs.insert_one({"_id": job, "instrument_id": isin, "listing": listing,
                                        "provider": "yfinance", "published": True,
                                        "requested_at": NOW, "published_at": NOW})
        db.instruments.insert_one({"instrument_id": isin, **listing, "job_id": job,
                                   "provider": "yfinance", "collected_at": NOW + timedelta(days=index // 2)})


@pytest.mark.parametrize("count", [20, 501])
def test_page_payload_work_is_bounded(monkeypatch, count):
    db = mongomock.MongoClient().pagination
    seed(db, count)
    leases = []
    actual_reader = retention.reader

    @contextmanager
    def track_reader(*args, **kwargs):
        leases.append(args[1])
        with actual_reader(*args, **kwargs) as generation:
            yield generation

    def statistics(database, observations, cutoff):
        return {row["_id"]: ({"saved_history": {"quote_count": 0, "first_date": None, "last_date": None}},
                              {"listing": row["listing"]}) for row in observations}

    calculate = Mock(side_effect=statistics)
    monkeypatch.setattr(retention, "reader", track_reader)
    monkeypatch.setattr(return_statistics, "acquired_statistics", calculate)
    result = md.acquired_instruments(db, limit=3, offset=2)
    expected = sorted(range(count), key=lambda index: (-(index // 2), index))[2:5]
    assert [row["instrument_id"] for row in result["items"]] == [f"IE{index:010d}" for index in expected]
    assert result["total"] == count
    assert len(leases) == 3
    assert len(calculate.call_args.args[1]) == 3
    assert all(not job.get("reader_leases") for job in db.acquisition_jobs.find())
    leases.clear()
    assert md.acquired_instruments(db, limit=3, offset=count)["items"] == []
    assert leases == []


def test_universe_has_no_payload_leases_or_per_instrument_reads(monkeypatch):
    db = mongomock.MongoClient().pagination
    seed(db, 501)
    reader = Mock(wraps=retention.reader)
    profiles = Mock(wraps=db.instruments.find_one)
    catalogue = Mock(wraps=md.latest_catalogue)
    monkeypatch.setattr(retention, "reader", reader)
    monkeypatch.setattr(db.instruments, "find_one", profiles)
    monkeypatch.setattr(md, "latest_catalogue", catalogue)
    result = md.acquired_universe(db)
    assert result["total"] == 501
    assert [row["instrument_id"] for row in result["items"]] == sorted(row["instrument_id"] for row in result["items"])
    assert reader.call_count == profiles.call_count == catalogue.call_count == 0


def test_universe_rejects_publication_during_catalogue_capture(monkeypatch):
    db = mongomock.MongoClient().pagination
    seed(db, 1)
    original = md._acquired_catalogues

    def publish(database, observations):
        job = database.acquisition_jobs.find_one()
        database.acquisition_jobs.insert_one({**job, "_id": "later", "requested_at": NOW + timedelta(seconds=1)})
        return original(database, observations)

    monkeypatch.setattr(md, "_acquired_catalogues", publish)
    with pytest.raises(md.DataError, match="superseded") as error:
        md.acquired_universe(db)
    assert error.value.status == 409


@pytest.mark.parametrize("phase", ["before_lease", "during_hydration"])
def test_page_rejects_superseded_generation_and_releases_leases(monkeypatch, phase):
    db = mongomock.MongoClient().pagination
    seed(db, 3)

    def publish():
        job = db.acquisition_jobs.find_one({"_id": "job-2"})
        job.pop("reader_leases", None)
        db.acquisition_jobs.insert_one({**job, "_id": "later", "requested_at": NOW + timedelta(seconds=1)})

    if phase == "before_lease":
        select = md._acquired_page

        def select_then_publish(*args):
            result = select(*args)
            publish()
            return result

        monkeypatch.setattr(md, "_acquired_page", select_then_publish)
    else:
        def statistics(database, observations, cutoff):
            publish()
            return {row["_id"]: ({"saved_history": {}}, {"listing": row["listing"]}) for row in observations}

        monkeypatch.setattr(return_statistics, "acquired_statistics", statistics)
    with pytest.raises(md.DataError, match="superseded") as error:
        md.acquired_instruments(db, limit=1)
    assert error.value.status == 409
    assert all(not job.get("reader_leases") for job in db.acquisition_jobs.find())


@pytest.mark.parametrize("endpoint", [md.acquired_instruments, md.acquired_universe])
def test_metadata_does_not_hide_missing_off_page_profile(endpoint):
    db = mongomock.MongoClient().pagination
    seed(db, 3)
    db.instruments.delete_one({"job_id": "job-0"})
    with pytest.raises(md.DataError, match="latest Yahoo profile") as error:
        endpoint(db, limit=1) if endpoint is md.acquired_instruments else endpoint(db)
    assert error.value.status == 409
