"""Disposable real MongoDB only; opt in with TEST_MONGODB_URI, never app config."""
import os
from uuid import uuid4

import pytest
from pymongo import MongoClient

import market_data as md
from config import TaskSettings
import market_data_retention as retention
from test_market_data_retention import ISIN, LISTING, seed

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_MONGODB_URI"), reason="Disposable TEST_MONGODB_URI not provided")


@pytest.fixture
def db():
    client = MongoClient(os.environ["TEST_MONGODB_URI"], tz_aware=True, serverSelectionTimeoutMS=5000)
    name = "fixture_retention_" + uuid4().hex
    database = client[name]
    md.ensure_indexes(database)
    try:
        yield database
    finally:
        client.drop_database(name)
        client.close()


def test_real_mongo_unique_gate_and_reader_retirement(db):
    seed(db, "old", 0)
    gate = retention._gate(db, ISIN)
    assert gate is not None
    assert retention._gate(db, ISIN) is None
    db.acquisition_jobs.delete_one({"_id": gate["_id"]})
    with pytest.raises(md.DataError):
        with retention.reader(db, ISIN, LISTING, "old"):
            seed(db, "new", 1)
            retention.cleanup(db, TaskSettings(_env_file=None))
            assert db.quotes.count_documents({"job_id": "old"}) == 1
    retention.cleanup(db, TaskSettings(_env_file=None))
    assert db.quotes.count_documents({"job_id": "old"}) == 0
    assert db.quotes.count_documents({"job_id": "new"}) == 1
    assert db.acquisition_jobs.find_one({"_id": "old"})["retention_state"] == "pruned"


def test_repeated_refreshes_bound_payloads_keep_dates_and_provenance(db):
    for index in range(5):
        seed(db, f"generation-{index}", index)
        db.quotes.insert_one({"instrument_id": ISIN, **LISTING, "provider": "yfinance",
                              "job_id": f"generation-{index}", "source_job_id": "generation-0",
                              "date": "2010-01-04"})
        retention.cleanup(db, TaskSettings(_env_file=None, market_data_retention_batch_size=1))
        assert db.quotes.count_documents({}) == 2
        assert set(db.quotes.distinct("date")) == {"2010-01-04", "2020-01-02"}
        assert db.quotes.find_one({"date": "2010-01-04"})["source_job_id"] == "generation-0"
    assert db.acquisition_jobs.count_documents({"retention_state": "pruned"}) == 4
