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
            db.instruments.insert_many([
                {"instrument_id": isin, "collected_at": now, "provider": "yfinance", "published": True}
                for isin in reversed(ids)
            ] + [
                {"instrument_id": ids[0], "collected_at": now + timedelta(hours=1), "provider": "yfinance", "published": True},
                {"instrument_id": "IE9999999999", "provider": "yfinance", "published": False},
                {"instrument_id": "idx-test", "collected_at": now},
            ])
            db.etfs.insert_many([
                {"isin": ids[0], "name": "Old label", "import_date": "2026-10-16"},
                {"isin": ids[0], "name": "Current label", "ticker": "ETF", "import_date": "2026-10-17"},
            ])

            class PublishingCatalogue:
                def find(self, *args, **kwargs):
                    # A download publishes after membership is captured, before labels load.
                    db.instruments.insert_one({
                        "instrument_id": "IE9999999999", "provider": "yfinance", "published": True, "collected_at": now,
                    })
                    return db.etfs.find(*args, **kwargs)

            class PublishingDatabase:
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
