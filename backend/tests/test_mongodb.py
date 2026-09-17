"""Opt-in integration check; only a uniquely named test database is removed."""
import os
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

from auth import COOKIE_NAME, password_hasher
from config import Settings
from etfs import ETF_SOURCE_URL, ETFImportError
from main import create_app


@pytest.mark.skipif(not os.environ.get("TEST_MONGODB_URI"), reason="Set TEST_MONGODB_URI to run real MongoDB tests")
def test_mongodb_users_and_sessions_survive_backend_restart(etf_http_get):
    settings = Settings(
        _env_file=None,
        mongodb_uri=os.environ["TEST_MONGODB_URI"],
        mongodb_database=f"hedgehoog_test_{uuid4().hex}",
        jwt_secret="integration-test-secret-" * 3,
    )
    mongo = MongoClient(settings.mongodb_uri.get_secret_value(), serverSelectionTimeoutMS=5000)
    database = mongo[settings.mongodb_database]
    try:
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
    finally:
        mongo.drop_database(settings.mongodb_database)
        mongo.close()


@pytest.mark.skipif(not os.environ.get("TEST_MONGODB_URI"), reason="Set TEST_MONGODB_URI to run real MongoDB tests")
def test_etf_catalogue_refresh_and_failed_download(etf_http_get, etf_records):
    settings = Settings(
        _env_file=None,
        mongodb_uri=os.environ["TEST_MONGODB_URI"],
        mongodb_database=f"hedgehoog_test_{uuid4().hex}",
        jwt_secret="integration-test-secret-" * 3,
    )
    mongo = MongoClient(settings.mongodb_uri.get_secret_value(), serverSelectionTimeoutMS=5000)
    collection = mongo[settings.mongodb_database].etfs
    try:
        with TestClient(create_app(settings)) as client:
            assert client.get("/health").status_code == 200
            assert collection.count_documents({}) == 2
            for record in etf_records:
                assert collection.find_one({"isin": record["isin"]}, {"_id": 0}) == record
            original = collection.find_one({"isin": etf_records[0]["isin"]})
            with pytest.raises(DuplicateKeyError):
                collection.insert_one(dict(etf_records[0]))

        updated = dict(etf_records[0], name="Updated fund name", ter=0.15)
        del updated["future_source_field"]
        added = {"isin": "IE00B3RBWM25", "name": "New fund"}
        etf_http_get.return_value = httpx.Response(
            200, json=[updated, added], request=httpx.Request("GET", ETF_SOURCE_URL),
        )
        # Repeat the refreshed import: replacements, inserts and retries must
        # preserve IDs and never duplicate ISINs or erase absent records.
        for _ in range(2):
            with TestClient(create_app(settings)) as client:
                assert client.get("/health").status_code == 200
                assert collection.count_documents({}) == 3
                stored = collection.find_one({"isin": updated["isin"]})
                assert stored.pop("_id") == original["_id"]
                assert stored == updated
                assert collection.find_one({"isin": added["isin"]}, {"_id": 0}) == added
                assert collection.find_one({"isin": etf_records[1]["isin"]}, {"_id": 0}) == etf_records[1]
        assert etf_http_get.call_count == 3

        previous = list(collection.find().sort("isin"))
        etf_http_get.return_value = httpx.Response(
            200, json=[dict(updated, name="Must not be stored"), {}],
            request=httpx.Request("GET", ETF_SOURCE_URL),
        )
        with pytest.raises(ETFImportError, match="invalid ISIN"):
            with TestClient(create_app(settings)):
                pytest.fail("Malformed feed must prevent startup")
        assert list(collection.find().sort("isin")) == previous
    finally:
        mongo.drop_database(settings.mongodb_database)
        mongo.close()
