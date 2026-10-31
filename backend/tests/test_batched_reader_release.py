"""Offline release-failure semantics; no Mongo query-plan or timing claims."""
from datetime import datetime, timedelta, timezone

import mongomock
import pytest

import market_data as md
from market_data_retention import READER_SECONDS, reader_many


@pytest.mark.parametrize("write_completed", [False, True])
def test_release_failure_propagates_after_releasing_remaining_chunks(monkeypatch, write_completed):
    now = datetime(2026, 10, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(md, "utcnow", lambda: now)
    db = mongomock.MongoClient(tz_aware=True).db
    foreign_expiry = now + timedelta(seconds=600)
    observations = []
    for index in range(51):
        isin, job_id = f"IE{index:010d}", f"job-{index}"
        listing = {"symbol": isin, "exchange": "TEST"}
        db.acquisition_jobs.insert_one({
            "_id": job_id, "instrument_id": isin, "listing": listing,
            "provider": "yfinance", "published": True,
            "requested_at": now, "published_at": now,
            "reader_leases": {"foreign": foreign_expiry},
        })
        observations.append({"_id": isin, "job_id": job_id, "listing": listing, "provider": "yfinance"})

    original = mongomock.collection.Collection.update_many
    releases = []
    failure = RuntimeError("release acknowledgement lost")

    def update_many(collection, query, update, *args, **kwargs):
        if "$unset" not in update:
            return original(collection, query, update, *args, **kwargs)
        releases.append(query["_id"]["$in"])
        if len(releases) == 1:
            if write_completed:
                original(collection, query, update, *args, **kwargs)
            raise failure
        return original(collection, query, update, *args, **kwargs)

    monkeypatch.setattr(mongomock.collection.Collection, "update_many", update_many)
    with pytest.raises(RuntimeError) as error:
        with reader_many(db, observations):
            assert all(len(job["reader_leases"]) == 2 for job in db.acquisition_jobs.find())
    assert error.value is failure
    assert releases == [[f"job-{index}" for index in range(50)], ["job-50"]]
    for job in db.acquisition_jobs.find():
        leases = job["reader_leases"]
        assert leases["foreign"] == foreign_expiry
        if job["_id"] == "job-50" or write_completed:
            assert leases == {"foreign": foreign_expiry}
        else:
            # A failed write may leave our token, but never beyond its original TTL.
            assert len(leases) == 2
            assert [expiry for token, expiry in leases.items() if token != "foreign"] == [
                now + timedelta(seconds=READER_SECONDS)
            ]
