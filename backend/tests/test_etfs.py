import json
import logging
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from pymongo import ReplaceOne
from pymongo.errors import ConnectionFailure

from config import Settings
from etfs import ETF_SOURCE_URL, ETFImportError, download_etfs, import_etfs
from main import create_app


def test_download_preserves_source_fields(etf_http_get, etf_records):
    assert download_etfs() == etf_records
    etf_http_get.assert_called_once_with(
        ETF_SOURCE_URL, timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=True,
    )


def test_import_uses_unique_isin_and_bulk_replacements(etf_http_get, etf_records, caplog):
    database = Mock()
    with caplog.at_level(logging.INFO, logger="etfs"):
        assert import_etfs(database) == len(etf_records)
    database.etfs.create_index.assert_called_once_with("isin", unique=True)
    database.etfs.bulk_write.assert_called_once_with(
        [ReplaceOne({"isin": record["isin"]}, record, upsert=True) for record in etf_records],
        ordered=True,
    )
    assert "Imported 2 ETFs" in caplog.text


@pytest.mark.parametrize("payload, message", [
    ([], "non-empty JSON array"),
    ({"etfs": []}, "non-empty JSON array"),
    (None, "non-empty JSON array"),
    (["invalid"], "must be an object"),
    ([{}], "invalid ISIN"),
    ([{"isin": None}], "invalid ISIN"),
    ([{"isin": 123}], "invalid ISIN"),
    ([{"isin": " IE00B4L5Y983"}], "invalid ISIN"),
    ([{"isin": "IE00B4L5Y983", "_id": "source-id"}], "reserved field _id"),
    ([{"isin": "IE00B4L5Y983"}, {"isin": "IE00B4L5Y983"}], "duplicate ISIN"),
    ([{"isin": "IE00B4L5Y983"}, {"name": "invalid later record"}], "invalid ISIN"),
])
def test_invalid_catalogue_never_touches_database(etf_http_get, payload, message):
    etf_http_get.return_value = httpx.Response(
        200, content=json.dumps(payload).encode(), request=httpx.Request("GET", ETF_SOURCE_URL),
    )
    database = Mock()
    with pytest.raises(ETFImportError, match=message):
        import_etfs(database)
    assert database.mock_calls == []


@pytest.mark.parametrize("status, content", [
    (404, b"not found"), (503, b"unavailable"), (200, b"not JSON"),
])
def test_bad_http_response_aborts_import(etf_http_get, status, content):
    etf_http_get.return_value = httpx.Response(
        status, content=content, request=httpx.Request("GET", ETF_SOURCE_URL),
    )
    database = Mock()
    with pytest.raises(ETFImportError, match="Could not download ETF catalogue"):
        import_etfs(database)
    assert database.mock_calls == []


@pytest.mark.parametrize("error", [httpx.ConnectError("offline"), httpx.ReadTimeout("timed out")])
def test_network_failure_aborts_import(etf_http_get, error):
    etf_http_get.side_effect = error
    database = Mock()
    with pytest.raises(ETFImportError, match="Could not download ETF catalogue") as raised:
        import_etfs(database)
    assert raised.value.__cause__ is error
    assert database.mock_calls == []


@pytest.mark.parametrize("operation", ["create_index", "bulk_write"])
def test_database_failure_is_not_silenced(etf_http_get, operation):
    database = Mock()
    getattr(database.etfs, operation).side_effect = ConnectionFailure("unavailable")
    with pytest.raises(ETFImportError, match="Could not persist ETF catalogue"):
        import_etfs(database)


def test_startup_imports_before_serving_requests(monkeypatch, etf_http_get):
    config = Settings(_env_file=None, jwt_secret="test-secret-" * 5)
    mongo = Mock()
    database = Mock()
    mongo.__getitem__ = Mock(return_value=database)
    seed_admin = Mock()
    monkeypatch.setattr("main.seed_admin", seed_admin)
    events = Mock()
    events.attach_mock(mongo.admin.command, "ping")
    events.attach_mock(database.etfs.bulk_write, "import_records")
    events.attach_mock(seed_admin, "seed_admin")

    with TestClient(create_app(config, client_factory=Mock(return_value=mongo))) as client:
        assert [call[0] for call in events.mock_calls] == ["ping", "import_records", "seed_admin"]
        assert client.get("/health").status_code == 200
        etf_http_get.assert_called_once()
        mongo.close.assert_not_called()
    mongo.close.assert_called_once()


@pytest.mark.parametrize("failure", ["download", "database"])
def test_import_failure_prevents_startup_and_closes_mongo(monkeypatch, etf_http_get, failure):
    config = Settings(_env_file=None, jwt_secret="test-secret-" * 5)
    mongo = Mock()
    database = Mock()
    mongo.__getitem__ = Mock(return_value=database)
    if failure == "download":
        etf_http_get.side_effect = httpx.ConnectError("offline")
    else:
        database.etfs.bulk_write.side_effect = ConnectionFailure("unavailable")
    seed_admin = Mock()
    monkeypatch.setattr("main.seed_admin", seed_admin)
    with pytest.raises(ETFImportError):
        with TestClient(create_app(config, client_factory=Mock(return_value=mongo))):
            pytest.fail("The app must not accept requests after an import failure")
    seed_admin.assert_not_called()
    mongo.close.assert_called_once()
