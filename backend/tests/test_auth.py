from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import jwt
import mongomock
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pymongo.errors import ConnectionFailure, DuplicateKeyError

from auth import COOKIE_NAME, JWT_AUDIENCE, JWT_ISSUER, password_hasher, seed_admin
from config import Settings
from main import create_app


@pytest.fixture(autouse=True)
def skip_etf_import(monkeypatch):
    # Auth tests do not need a broker. Task dispatch and persistence are covered
    # in test_tasks.py and test_mongodb.py.
    monkeypatch.setattr("main.enqueue_etf_import", Mock(return_value="test-task"))


@pytest.fixture
def config():
    return Settings(_env_file=None, jwt_secret="test-secret-" * 5)


@pytest.fixture
def database(config):
    client = mongomock.MongoClient()
    return client[config.mongodb_database]


@pytest.fixture
def client(config, database):
    with TestClient(create_app(config, client_factory=lambda *a, **kw: database.client)) as client:
        yield client


def login(client, password="admin", name="admin"):
    return client.post("/auth/login", json={"login": name, "password": password})


def test_seed_hashes_password_and_is_idempotent(database, config):
    seed_admin(database, config)
    user = database.users.find_one({"login": "admin"})
    assert user["role"] == "admin"
    assert user["password_hash"] != "admin"
    assert password_hasher.verify("admin", user["password_hash"])
    changed_hash = password_hasher.hash("changed-password")
    database.users.update_one({"_id": user["_id"]}, {"$set": {"password_hash": changed_hash}})
    seed_admin(database, config)
    assert database.users.count_documents({}) == 1
    assert database.users.find_one()["password_hash"] == changed_hash
    with pytest.raises(DuplicateKeyError):
        database.users.insert_one({"login": "admin"})


def test_routes_require_authentication(client):
    for path in ("/", "/auth/me", "/admin"):
        assert client.get(path).status_code == 401
    assert client.get("/health").status_code == 200


def test_login_session_and_logout(client):
    response = login(client)
    assert response.status_code == 200
    assert set(response.json()) == {"id", "login", "role"}
    assert response.json()["role"] == "admin"
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert "Max-Age=2592000" in response.headers["set-cookie"]
    assert "Domain=" not in response.headers["set-cookie"]
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/auth/me").json() == response.json()
    assert client.get("/").json() == {"message": "Hello, World!"}
    assert client.get("/admin").status_code == 200
    assert client.post("/auth/logout").status_code == 204
    assert COOKIE_NAME not in client.cookies
    assert client.get("/auth/me").status_code == 401
    assert client.post("/auth/logout").status_code == 204


@pytest.mark.parametrize("minutes", [43_200, 60, 1])
def test_session_lifetime_matches_configuration(client, config, monkeypatch, minutes):
    monkeypatch.setattr(config, "jwt_expire_minutes", minutes)
    response = login(client)
    assert response.status_code == 200
    assert f"Max-Age={minutes * 60}" in response.headers["set-cookie"]
    payload = jwt.decode(
        client.cookies[COOKIE_NAME], config.jwt_secret.get_secret_value(),
        algorithms=["HS256"], issuer=JWT_ISSUER, audience=JWT_AUDIENCE,
    )
    assert payload["exp"] - payload["iat"] == minutes * 60


def test_default_session_survives_until_30_day_expiry(client, config, monkeypatch):
    assert config.jwt_expire_minutes == 43_200
    assert login(client).status_code == 200
    payload = jwt.decode(
        client.cookies[COOKIE_NAME], config.jwt_secret.get_secret_value(),
        algorithms=["HS256"], issuer=JWT_ISSUER, audience=JWT_AUDIENCE,
    )
    issued_at = datetime.fromtimestamp(payload["iat"], timezone.utc)

    class Clock(datetime):
        current = issued_at

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz)

    # Advance JWT verification time without waiting or disabling expiry checks.
    monkeypatch.setattr("jwt.api_jwt.datetime", Clock)
    for elapsed in (timedelta(hours=2), timedelta(days=29), timedelta(days=30, seconds=-1)):
        Clock.current = issued_at + elapsed
        assert client.get("/auth/me").status_code == 200
        assert client.get("/auth/me").headers.get("set-cookie") is None
    Clock.current = issued_at + timedelta(days=30)
    assert client.get("/auth/me").status_code == 401
    Clock.current += timedelta(seconds=1)
    assert client.get("/auth/me").status_code == 401


@pytest.mark.parametrize("minutes", [1, 60, 1440, 43_200])
def test_session_lifetime_environment_override(monkeypatch, minutes):
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", str(minutes))
    assert Settings(_env_file=None, jwt_secret="test-secret-" * 5).jwt_expire_minutes == minutes


@pytest.mark.parametrize("minutes", [0, -1, 43_201])
def test_session_lifetime_rejects_out_of_range_configuration(monkeypatch, minutes):
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", str(minutes))
    with pytest.raises(ValidationError):
        Settings(_env_file=None, jwt_secret="test-secret-" * 5)


@pytest.mark.parametrize("name,password", [("admin", "wrong"), ("missing", "admin")])
def test_invalid_credentials_are_generic(client, name, password):
    response = login(client, password=password, name=name)
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid login or password"}
    assert COOKIE_NAME not in client.cookies


@pytest.mark.parametrize("credentials", [
    {}, {"login": "", "password": "admin"},
    {"login": {"$ne": None}, "password": "admin"},
    {"login": "admin", "password": "a" * 1025},
])
def test_invalid_login_payloads(client, credentials):
    assert client.post("/auth/login", json=credentials).status_code == 422


@pytest.mark.parametrize("kind", ["expired", "bad-signature", "bad-sub", "missing-exp", "wrong-aud", "wrong-iss", "wrong-alg", "garbage"])
def test_invalid_tokens(client, database, config, kind):
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(database.users.find_one()["_id"]),
        "iat": now,
        "exp": now + timedelta(minutes=1),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
    }
    key = config.jwt_secret.get_secret_value()
    algorithm = "HS256"
    if kind == "expired":
        payload["exp"] = now - timedelta(seconds=1)
    elif kind == "bad-signature":
        key = "wrong-secret-" * 5
    elif kind == "bad-sub":
        payload["sub"] = "not-an-object-id"
    elif kind == "missing-exp":
        del payload["exp"]
    elif kind == "wrong-aud":
        payload["aud"] = "another-app"
    elif kind == "wrong-iss":
        payload["iss"] = "another-issuer"
    elif kind == "wrong-alg":
        algorithm = "HS384"
    token = "garbage" if kind == "garbage" else jwt.encode(payload, key, algorithm=algorithm)
    client.cookies.set(COOKIE_NAME, token)
    assert client.get("/auth/me").status_code == 401


def test_disabled_or_deleted_user_loses_access(client, database):
    assert login(client).status_code == 200
    database.users.update_one({"login": "admin"}, {"$set": {"disabled": True}})
    assert client.get("/").status_code == 401
    assert login(client).status_code == 401
    database.users.delete_many({})
    assert client.get("/").status_code == 401


def test_role_is_checked_against_database(client, database):
    login(client)
    database.users.update_one({"login": "admin"}, {"$set": {"role": "user"}})
    assert client.get("/").status_code == 200
    assert client.get("/admin").status_code == 403
    assert client.get("/auth/me").json()["role"] == "user"


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
])
def test_cross_origin_mutations_blocked(client, headers):
    assert client.post("/auth/login", json={"login": "admin", "password": "admin"}, headers=headers).status_code == 403
    assert client.post("/auth/logout", headers=headers).status_code == 403


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://testserver"])
def test_allowed_browser_origin(client, origin):
    response = client.post("/auth/login", json={"login": "admin", "password": "admin"}, headers={"Origin": origin})
    assert response.status_code == 200


def test_database_outage_is_service_unavailable(client, database, monkeypatch):
    login(client)

    def unavailable(*args, **kwargs):
        raise ConnectionFailure("sensitive internal details")

    monkeypatch.setattr(database.users, "find_one", unavailable)
    response = client.get("/auth/me")
    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}


def test_configuration_requires_secret_and_safe_production_values():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, jwt_secret="too-short")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, jwt_secret="s" * 48, environment="production")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, jwt_secret="s" * 48, environment="production", admin_password="long-secure-password")


def test_production_rejects_previously_seeded_development_password(config, database):
    seed_admin(database, config)
    production = Settings(_env_file=None, jwt_secret="s" * 48, environment="production", admin_password="long-secure-password", cookie_secure=True)
    with pytest.raises(ValueError, match="stored admin account"):
        seed_admin(database, production)


def test_secure_cookie_in_production(config, database):
    config = Settings(_env_file=None, jwt_secret="s" * 48, environment="production", admin_password="long-secure-password", cookie_secure=True)
    with TestClient(create_app(config, client_factory=lambda *a, **kw: database.client), base_url="https://testserver") as client:
        response = login(client, password="long-secure-password")
        assert response.status_code == 200
        assert "Secure" in response.headers["set-cookie"]
        assert client.get("/auth/me").status_code == 200
