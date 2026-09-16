"""Opt-in integration check; only a uniquely named test database is removed."""
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

from auth import COOKIE_NAME, password_hasher
from config import Settings
from main import create_app


@pytest.mark.skipif(not os.environ.get("TEST_MONGODB_URI"), reason="Set TEST_MONGODB_URI to run real MongoDB tests")
def test_mongodb_users_and_sessions_survive_backend_restart():
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
