"""Opt-in universe capture checks against a disposable real Mongo database."""

from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4

from pymongo import MongoClient
import pytest

from market_data import acquired_universe


def test_real_mongo_universe_capture_during_publication():
    uri = os.environ.get("TEST_MONGODB_URI")
    if not uri:
        pytest.skip("Set TEST_MONGODB_URI for isolated real Mongo tests")
    name = "hedgehoog_universe_test_" + uuid4().hex
    now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    ids = [f"IE{index:010d}" for index in range(1062)]
    with MongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True) as mongo:
        db = mongo[name]
        try:
            db.instruments.create_index("job_id")

            def seed_acquisition(isin, generation, collected_at, *, published=True):
                listing = {"symbol": isin, "exchange": "TEST"}
                db.instruments.insert_one({
                    "instrument_id": isin, "job_id": generation, **listing,
                    "provider": "yfinance", "collected_at": collected_at,
                })
                db.acquisition_jobs.insert_one({
                    "_id": generation, "instrument_id": isin, "listing": listing,
                    "provider": "yfinance", "published": published,
                    "requested_at": collected_at, "published_at": collected_at if published else None,
                    "status": "succeeded" if published else "running",
                })

            for isin in reversed(ids):
                seed_acquisition(isin, f"original-{isin}", now)
            seed_acquisition(ids[0], "newer-first-etf", now + timedelta(hours=1))
            seed_acquisition("IE9999999999", "unpublished-etf", now, published=False)
            seed_acquisition("idx-test", "published-index", now)
            db.etfs.insert_many([
                {"isin": ids[0], "name": "Old label", "import_date": "2026-10-16"},
                {"isin": ids[0], "name": "Current label", "ticker": "ETF", "import_date": "2026-10-17"},
            ])

            class PublishingCatalogue:
                published = False

                def find(self, *args, **kwargs):
                    # Publish once: catalogue hydration can span several batches.
                    if not self.published:
                        seed_acquisition("IE9999999999", "newly-published-etf", now)
                        self.published = True
                    return db.etfs.find(*args, **kwargs)

            class PublishingDatabase:
                acquisition_jobs = db.acquisition_jobs
                instruments = db.instruments
                etfs = PublishingCatalogue()

            result = acquired_universe(PublishingDatabase())
            assert result["total"] == len(result["items"]) == len(ids)
            assert [item["instrument_id"] for item in result["items"]] == ids
            assert result["items"][0] == {
                "instrument_id": ids[0], "name": "Current label", "ticker": "ETF", "collected_at": "2026-10-17T13:00:00Z",
            }
            assert acquired_universe(db)["total"] == len(ids) + 1
            assert db.quotes.count_documents({}) == 0
            assert db.instrument_statistics.count_documents({}) == 0
        finally:
            mongo.drop_database(name)
