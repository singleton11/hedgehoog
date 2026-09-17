import json
import logging
from datetime import datetime, timezone
from unittest.mock import Mock

import httpx
import mongomock
import pytest
from pymongo import ReplaceOne
from pymongo.errors import ConnectionFailure, OperationFailure

from etfs import ETF_SOURCE_URL, ETFImportError, download_etfs, import_etfs


IMPORT_DATE = "2026-09-17"


@pytest.fixture(autouse=True)
def import_clock(monkeypatch):
    clock = Mock()
    clock.now.return_value = datetime(2026, 9, 17, 23, 59, tzinfo=timezone.utc)
    monkeypatch.setattr("etfs.datetime", clock)
    return clock


@pytest.fixture
def database():
    # Keep status operations stateful; assert bulk operations separately because
    # mongomock does not support the current PyMongo ReplaceOne bulk API.
    database = Mock()
    database.imports = mongomock.MongoClient().test.imports
    database.etfs.list_indexes.return_value = []
    return database


def assert_incomplete(database):
    status = database.imports.find_one({"date": IMPORT_DATE})
    assert status is not None
    assert status["etfs"] is False


def test_download_preserves_source_fields(etf_http_get, etf_records):
    assert download_etfs() == etf_records
    etf_http_get.assert_called_once_with(
        ETF_SOURCE_URL, timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=True,
    )


def test_import_uses_unique_daily_key_and_bulk_replacements(database, etf_http_get, etf_records, caplog, import_clock):
    database.etfs.bulk_write.side_effect = lambda *args, **kwargs: assert_incomplete(database)
    with caplog.at_level(logging.INFO, logger="etfs"):
        assert import_etfs(database) == len(etf_records)
    database.etfs.create_index.assert_called_once_with([("isin", 1), ("import_date", 1)], unique=True)
    database.etfs.bulk_write.assert_called_once_with(
        [
            ReplaceOne(
                {"isin": record["isin"], "import_date": IMPORT_DATE},
                {**record, "import_date": IMPORT_DATE},
                upsert=True,
            )
            for record in etf_records
        ],
        ordered=True,
    )
    assert database.imports.find_one({"date": IMPORT_DATE})["etfs"] is True
    assert database.imports.index_information()["date_1"]["unique"] is True
    assert all("import_date" not in record for record in etf_records)
    # All documents and the completion marker use one UTC date, even at midnight.
    import_clock.now.assert_called_once_with(timezone.utc)
    assert "Imported 2 ETFs" in caplog.text


def test_import_crossing_midnight_uses_start_date(database, etf_http_get, import_clock):
    response = etf_http_get.return_value

    def download_after_midnight(*args, **kwargs):
        import_clock.now.return_value = datetime(2026, 9, 18, tzinfo=timezone.utc)
        return response

    etf_http_get.side_effect = download_after_midnight
    assert import_etfs(database) == 2
    import_clock.now.assert_called_once_with(timezone.utc)
    assert database.imports.find_one({"date": IMPORT_DATE})["etfs"] is True
    assert database.imports.find_one({"date": "2026-09-18"}) is None
    operations = database.etfs.bulk_write.call_args.args[0]
    assert all(operation._doc["import_date"] == IMPORT_DATE for operation in operations)


def test_completed_day_skips_download_and_etf_writes(database, etf_http_get, caplog):
    database.imports.insert_one({"date": IMPORT_DATE, "etfs": True, "stocks": False})
    original = database.imports.find_one()
    etf_http_get.side_effect = httpx.ConnectError("offline")
    with caplog.at_level(logging.INFO, logger="etfs"):
        assert import_etfs(database) == 0
    etf_http_get.assert_not_called()
    assert database.etfs.mock_calls == []
    assert database.imports.find_one() == original
    assert database.imports.count_documents({}) == 1
    assert "already complete; skipping" in caplog.text


@pytest.mark.parametrize("status", [{"etfs": False}, {}])
def test_incomplete_day_retries_without_resetting_other_imports(database, etf_http_get, status):
    original_id = database.imports.insert_one({"date": IMPORT_DATE, "stocks": True, **status}).inserted_id
    assert import_etfs(database) == 2
    assert database.imports.find_one() == {
        "_id": original_id, "date": IMPORT_DATE, "etfs": True, "stocks": True,
    }
    etf_http_get.assert_called_once()


def test_previous_completed_day_does_not_skip_today(database, etf_http_get):
    previous = {"date": "2026-09-16", "etfs": True}
    database.imports.insert_one(previous)
    assert import_etfs(database) == 2
    assert database.imports.find_one({"date": "2026-09-16"}) == previous
    assert database.imports.find_one({"date": IMPORT_DATE})["etfs"] is True
    assert database.imports.count_documents({}) == 2


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
    ([{"isin": "IE00B4L5Y983", "import_date": "2020-01-01"}], "reserved field import_date"),
    ([{"isin": "IE00B4L5Y983"}, {"isin": "IE00B4L5Y983"}], "duplicate ISIN"),
    ([{"isin": "IE00B4L5Y983"}, {"name": "invalid later record"}], "invalid ISIN"),
])
def test_invalid_catalogue_leaves_incomplete_status_without_etf_writes(database, etf_http_get, payload, message):
    etf_http_get.return_value = httpx.Response(
        200, content=json.dumps(payload).encode(), request=httpx.Request("GET", ETF_SOURCE_URL),
    )
    with pytest.raises(ETFImportError, match=message):
        import_etfs(database)
    assert database.etfs.mock_calls == []
    assert_incomplete(database)


@pytest.mark.parametrize("status, content", [
    (404, b"not found"), (503, b"unavailable"), (200, b"not JSON"),
])
def test_bad_http_response_aborts_import(database, etf_http_get, status, content):
    etf_http_get.return_value = httpx.Response(
        status, content=content, request=httpx.Request("GET", ETF_SOURCE_URL),
    )
    with pytest.raises(ETFImportError, match="Could not download ETF catalogue"):
        import_etfs(database)
    assert database.etfs.mock_calls == []
    assert_incomplete(database)


@pytest.mark.parametrize("error", [httpx.ConnectError("offline"), httpx.ReadTimeout("timed out")])
def test_network_failure_aborts_import(database, etf_http_get, error):
    def fail_download(*args, **kwargs):
        assert_incomplete(database)  # The marker is durable before HTTP starts.
        raise error

    etf_http_get.side_effect = fail_download
    with pytest.raises(ETFImportError, match="Could not download ETF catalogue") as raised:
        import_etfs(database)
    assert raised.value.__cause__ is error
    assert database.etfs.mock_calls == []
    assert_incomplete(database)


@pytest.mark.parametrize("collection, operation", [
    ("imports", "create_index"), ("imports", "find_one_and_update"), ("imports", "update_one"),
    ("etfs", "create_index"), ("etfs", "list_indexes"), ("etfs", "drop_index"), ("etfs", "bulk_write"),
])
def test_database_failure_is_not_silenced(database, etf_http_get, monkeypatch, collection, operation):
    database.imports.insert_one({"date": IMPORT_DATE, "etfs": False})
    database.etfs.list_indexes.return_value = [{"name": "isin_1", "key": {"isin": 1}, "unique": True}]
    error = ConnectionFailure("unavailable")
    monkeypatch.setattr(getattr(database, collection), operation, Mock(side_effect=error))
    with pytest.raises(ETFImportError, match="Could not persist ETF catalogue") as raised:
        import_etfs(database)
    assert raised.value.__cause__ is error
    assert_incomplete(database)
    if collection == "imports" and operation != "update_one":
        etf_http_get.assert_not_called()


def test_failed_completion_write_is_retried_then_skipped(database, etf_http_get, monkeypatch):
    update_one = database.imports.update_one
    monkeypatch.setattr(database.imports, "update_one", Mock(side_effect=ConnectionFailure("unavailable")))
    with pytest.raises(ETFImportError):
        import_etfs(database)
    assert_incomplete(database)
    monkeypatch.setattr(database.imports, "update_one", update_one)
    assert import_etfs(database) == 2
    assert import_etfs(database) == 0
    assert etf_http_get.call_count == 2
    assert database.etfs.bulk_write.call_count == 2
    assert database.imports.count_documents({}) == 1
    assert database.imports.find_one()["etfs"] is True


def test_migration_only_drops_old_unique_isin_index(database, etf_http_get):
    database.etfs.list_indexes.return_value = [
        {"name": "_id_", "key": {"_id": 1}},
        {"name": "legacy_isin", "key": {"isin": 1}, "unique": True},
        {"name": "isin_lookup", "key": {"isin": 1}},
        {"name": "isin_1_import_date_1", "key": {"isin": 1, "import_date": 1}, "unique": True},
    ]
    assert import_etfs(database) == 2
    database.etfs.drop_index.assert_called_once_with("legacy_isin")
    assert [call[0] for call in database.etfs.mock_calls] == [
        "create_index", "list_indexes", "drop_index", "bulk_write",
    ]


def test_migration_tolerates_already_dropped_legacy_index(database, etf_http_get):
    database.etfs.list_indexes.return_value = [{"name": "isin_1", "key": {"isin": 1}, "unique": True}]
    database.etfs.drop_index.side_effect = OperationFailure("index not found", code=27)
    assert import_etfs(database) == 2
