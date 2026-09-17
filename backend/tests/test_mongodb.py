"""Opt-in real MongoDB checks using mocked HTTP and disposable UUID databases."""
import os
from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import ConnectionFailure, DuplicateKeyError

from auth import COOKIE_NAME, password_hasher
from config import Settings
from etfs import ETF_SOURCE_URL, ETFImportError
from main import create_app


pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_MONGODB_URI"), reason="Set TEST_MONGODB_URI to run real MongoDB tests",
)
IMPORT_DATE = "2026-09-17"
PREVIOUS_DATE = "2026-09-16"
NEXT_DATE = "2026-09-18"


@pytest.fixture
def etf_clock(monkeypatch):
    clock = Mock(now=Mock(return_value=datetime(2026, 9, 17, 12, tzinfo=timezone.utc)))
    monkeypatch.setattr("etfs.datetime", clock)
    return clock


@pytest.fixture
def mongodb(etf_clock):
    settings = Settings(
        _env_file=None,
        mongodb_uri=os.environ["TEST_MONGODB_URI"],
        mongodb_database=f"hedgehoog_test_{uuid4().hex}",
        jwt_secret="integration-test-secret-" * 3,
    )
    mongo = MongoClient(settings.mongodb_uri.get_secret_value(), serverSelectionTimeoutMS=5000)
    try:
        yield settings, mongo[settings.mongodb_database]
    finally:
        try:
            mongo.drop_database(settings.mongodb_database)
        finally:
            mongo.close()


def feed_response(records):
    return httpx.Response(200, json=records, request=httpx.Request("GET", ETF_SOURCE_URL))


def test_mongodb_users_and_sessions_survive_backend_restart(mongodb, etf_http_get):
    settings, database = mongodb
    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 401
        response = client.post("/auth/login", json={"login": "admin", "password": "admin"})
        assert response.status_code == 200
        token = client.cookies[COOKIE_NAME]
        stored = database.users.find_one({"login": "admin"})
        assert password_hasher.verify("admin", stored["password_hash"])
        assert "password" not in stored
        with pytest.raises(DuplicateKeyError):
            database.users.insert_one({"login": "admin"})

    with TestClient(create_app(settings)) as client:
        client.cookies.set(COOKIE_NAME, token)
        assert client.get("/auth/me").json()["id"] == str(stored["_id"])
        assert client.get("/admin").status_code == 200
        assert database.users.count_documents({}) == 1
        assert database.users.find_one()["password_hash"] == stored["password_hash"]
        assert client.post("/auth/logout").status_code == 204


def test_initial_startup_stores_daily_snapshot_status_and_unique_indexes(
    mongodb, etf_http_get, etf_records, etf_clock,
):
    settings, database = mongodb

    def download(*args, **kwargs):
        # The false marker must already exist when the HTTP request starts.
        status = database.imports.find_one({}, {"_id": 0})
        assert status == {"date": IMPORT_DATE, "etfs": False}
        assert status["etfs"] is False
        assert database.etfs.count_documents({}) == 0
        return feed_response(etf_records)

    etf_http_get.side_effect = download
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200

    etf_clock.now.assert_called_once_with(timezone.utc)
    etf_http_get.assert_called_once()
    assert database.etfs.count_documents({}) == len(etf_records)
    for record in etf_records:
        assert database.etfs.find_one({"isin": record["isin"]}, {"_id": 0}) == {
            **record, "import_date": IMPORT_DATE,
        }
    statuses = list(database.imports.find({}, {"_id": 0}))
    assert statuses == [{"date": IMPORT_DATE, "etfs": True}]
    assert statuses[0]["etfs"] is True

    assert any(
        index["key"] == [("date", 1)] and index.get("unique") is True
        for index in database.imports.index_information().values()
    )
    assert any(
        index["key"] == [("isin", 1), ("import_date", 1)] and index.get("unique") is True
        for index in database.etfs.index_information().values()
    )
    with pytest.raises(DuplicateKeyError):
        database.imports.insert_one({"date": IMPORT_DATE, "etfs": False})
    with pytest.raises(DuplicateKeyError):
        database.etfs.insert_one({**etf_records[0], "import_date": IMPORT_DATE})


def test_completed_same_day_startup_skips_offline_http(mongodb, etf_http_get):
    settings, database = mongodb
    with TestClient(create_app(settings)):
        pass
    database.imports.update_one({"date": IMPORT_DATE}, {"$set": {"prices": False}})
    previous_etfs = list(database.etfs.find().sort("isin"))
    previous_status = database.imports.find_one()
    etf_http_get.reset_mock()
    etf_http_get.side_effect = httpx.ConnectError("offline")

    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200

    etf_http_get.assert_not_called()
    assert list(database.etfs.find().sort("isin")) == previous_etfs
    assert list(database.imports.find()) == [previous_status]


def test_interrupted_day_replaces_partial_records_and_preserves_status_fields(
    mongodb, etf_http_get, etf_records,
):
    settings, database = mongodb
    database.imports.insert_one({
        "date": IMPORT_DATE, "etfs": False, "prices": True, "source_status": {"profiles": False},
    })
    previous_status = database.imports.find_one()
    original_id = database.etfs.insert_one({
        **etf_records[0], "import_date": IMPORT_DATE, "obsolete_source_field": "remove me",
    }).inserted_id
    database.etfs.insert_one({**etf_records[1], "import_date": IMPORT_DATE})
    absent_record = database.etfs.find_one({"isin": etf_records[1]["isin"]})
    updated = dict(etf_records[0], name="Updated fund name", ter=0.15)
    del updated["future_source_field"]
    added = {"isin": "IE00B3RBWM25", "name": "New fund"}

    def download(*args, **kwargs):
        # $setOnInsert must not reset flags belonging to another importer.
        assert database.imports.find_one() == previous_status
        return feed_response([updated, added])

    etf_http_get.side_effect = download
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200

    etf_http_get.assert_called_once()
    assert database.etfs.count_documents({}) == 3
    assert database.etfs.find_one({"isin": updated["isin"]}) == {
        **updated, "import_date": IMPORT_DATE, "_id": original_id,
    }
    assert database.etfs.find_one({"isin": added["isin"]}, {"_id": 0}) == {
        **added, "import_date": IMPORT_DATE,
    }
    assert database.etfs.find_one({"isin": absent_record["isin"]}) == absent_record
    assert list(database.imports.find()) == [{**previous_status, "etfs": True}]


def test_new_utc_day_retains_previous_snapshots(mongodb, etf_http_get, etf_records, etf_clock):
    settings, database = mongodb
    with TestClient(create_app(settings)):
        pass
    previous_etfs = list(database.etfs.find().sort("isin"))
    previous_status = database.imports.find_one()
    updated = dict(etf_records[0], name="Next day's fund name")
    del updated["future_source_field"]
    added = {"isin": "IE00B3RBWM25", "name": "New fund"}
    etf_http_get.return_value = feed_response([updated, added])
    etf_clock.now.return_value = datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc)

    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200

    assert etf_http_get.call_count == 2
    assert database.etfs.count_documents({}) == 4
    assert list(database.etfs.find({"import_date": IMPORT_DATE}).sort("isin")) == previous_etfs
    for record in (updated, added):
        assert database.etfs.find_one({"isin": record["isin"], "import_date": NEXT_DATE}, {"_id": 0}) == {
            **record, "import_date": NEXT_DATE,
        }
    old_id = database.etfs.find_one({"isin": updated["isin"], "import_date": IMPORT_DATE})["_id"]
    assert database.etfs.find_one({"isin": updated["isin"], "import_date": NEXT_DATE})["_id"] != old_id
    assert database.etfs.find_one({"isin": etf_records[1]["isin"], "import_date": NEXT_DATE}) is None
    assert database.imports.find_one({"date": IMPORT_DATE}) == previous_status
    assert database.imports.find_one({"date": NEXT_DATE}, {"_id": 0}) == {"date": NEXT_DATE, "etfs": True}
    assert database.imports.count_documents({}) == 2


@pytest.mark.parametrize("failure, message", [
    ("offline", "Could not download ETF catalogue"),
    ("malformed-json", "Could not download ETF catalogue"),
    ("invalid-record", "invalid ISIN"),
    ("reserved-import-date", "reserved field import_date"),
])
def test_failed_download_or_validation_leaves_false_marker_and_etfs_untouched(
    mongodb, etf_http_get, etf_records, failure, message,
):
    settings, database = mongodb
    database.etfs.insert_many([
        {**etf_records[0], "import_date": PREVIOUS_DATE},
        {**etf_records[0], "import_date": IMPORT_DATE},
    ])
    database.imports.insert_one({"date": PREVIOUS_DATE, "etfs": True, "prices": True})
    previous_status = database.imports.find_one()
    previous_etfs = list(database.etfs.find().sort("import_date"))
    if failure == "offline":
        etf_http_get.side_effect = httpx.ConnectError("offline")
    elif failure == "malformed-json":
        etf_http_get.return_value = httpx.Response(
            200, content=b"not JSON", request=httpx.Request("GET", ETF_SOURCE_URL),
        )
    else:
        invalid = {} if failure == "invalid-record" else {**etf_records[1], "import_date": PREVIOUS_DATE}
        etf_http_get.return_value = feed_response([
            dict(etf_records[0], name="Must not be stored"), invalid,
        ])

    with pytest.raises(ETFImportError, match=message):
        with TestClient(create_app(settings)):
            pytest.fail("An incomplete import must prevent startup")

    etf_http_get.assert_called_once()
    assert list(database.etfs.find().sort("import_date")) == previous_etfs
    assert database.imports.find_one({"date": PREVIOUS_DATE}) == previous_status
    assert database.imports.find_one({"date": IMPORT_DATE}, {"_id": 0}) == {"date": IMPORT_DATE, "etfs": False}
    assert database.imports.count_documents({}) == 2
    assert database.users.count_documents({}) == 0


def test_partial_real_bulk_failure_is_recovered_on_next_startup(
    mongodb, etf_http_get, etf_records, monkeypatch,
):
    settings, database = mongodb
    real_bulk_write = Collection.bulk_write
    failure = ConnectionFailure("connection lost after first ETF write")

    def partially_write(collection, requests, *args, **kwargs):
        if collection.full_name != database.etfs.full_name:
            return real_bulk_write(collection, requests, *args, **kwargs)
        assert database.imports.find_one()["etfs"] is False
        # Commit an actual MongoDB replacement, not a mocked success result.
        real_bulk_write(collection, requests[:1], *args, **kwargs)
        assert database.imports.find_one()["etfs"] is False
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(Collection, "bulk_write", partially_write)
        with pytest.raises(ETFImportError, match="Could not persist ETF catalogue") as raised:
            with TestClient(create_app(settings)):
                pytest.fail("A partial bulk write must prevent startup")
    assert raised.value.__cause__ is failure
    assert database.etfs.count_documents({}) == 1
    partial = database.etfs.find_one()
    assert partial == {**etf_records[0], "import_date": IMPORT_DATE, "_id": partial["_id"]}
    previous_status = database.imports.find_one()
    assert previous_status == {"date": IMPORT_DATE, "etfs": False, "_id": previous_status["_id"]}
    assert database.users.count_documents({}) == 0

    updated = dict(etf_records[0], name="Recovered fund")
    del updated["future_source_field"]
    etf_http_get.return_value = feed_response([updated, etf_records[1]])
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200

    assert etf_http_get.call_count == 2
    assert database.etfs.count_documents({}) == 2
    assert database.etfs.find_one({"isin": updated["isin"]}) == {
        **updated, "import_date": IMPORT_DATE, "_id": partial["_id"],
    }
    assert database.etfs.find_one({"isin": etf_records[1]["isin"]}, {"_id": 0}) == {
        **etf_records[1], "import_date": IMPORT_DATE,
    }
    assert list(database.imports.find()) == [{**previous_status, "etfs": True}]


@pytest.mark.parametrize("legacy_index_name", ["isin_1", "legacy_unique_isin"])
def test_migrates_unique_isin_index_without_changing_undated_rows(
    mongodb, etf_http_get, etf_records, monkeypatch, legacy_index_name,
):
    settings, database = mongodb
    database.etfs.create_index("isin", unique=True, name=legacy_index_name)
    database.etfs.create_index("ticker", name="ticker_lookup")
    database.etfs.insert_many([dict(record) for record in etf_records])
    legacy_rows = list(database.etfs.find().sort("isin"))
    updated = dict(etf_records[0], name="Dated version")
    added = {"isin": "IE00B3RBWM25", "name": "New fund"}
    etf_http_get.return_value = feed_response([updated, added])
    real_drop_index = Collection.drop_index
    dropped_indexes = []

    def drop_index(collection, index_name, *args, **kwargs):
        if collection.full_name == database.etfs.full_name:
            # The compound constraint must be established before removing the old one.
            assert any(
                index["key"] == [("isin", 1), ("import_date", 1)] and index.get("unique") is True
                for index in collection.index_information().values()
            )
            dropped_indexes.append(index_name)
        return real_drop_index(collection, index_name, *args, **kwargs)

    monkeypatch.setattr(Collection, "drop_index", drop_index)
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200

    etf_http_get.assert_called_once()
    assert dropped_indexes == [legacy_index_name]
    assert legacy_index_name not in database.etfs.index_information()
    assert "ticker_lookup" in database.etfs.index_information()
    assert database.etfs.count_documents({}) == 4
    assert list(database.etfs.find({"import_date": {"$exists": False}}).sort("isin")) == legacy_rows
    for record in (updated, added):
        assert database.etfs.find_one({"isin": record["isin"], "import_date": IMPORT_DATE}, {"_id": 0}) == {
            **record, "import_date": IMPORT_DATE,
        }
    assert list(database.imports.find({}, {"_id": 0})) == [{"date": IMPORT_DATE, "etfs": True}]
