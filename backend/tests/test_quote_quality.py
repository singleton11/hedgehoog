"""Permissive Yahoo numeric evidence, exact listing identity, and offline raw replay."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from pymongo.errors import ConnectionFailure

import market_data as md
from yahoo_finance import YahooFinanceClient
from test_market_data import api, clock, config, db
from test_yahoo_finance import chart, no_live_http, response, transport

ISIN = "IE00B4L5Y983"
LISTING = {"symbol": "EUNL.F", "exchange": "XFRA"}


def payload(rows, **metadata):
    raw = chart()
    result = raw["chart"]["result"][0]
    result["meta"].update(symbol=LISTING["symbol"], exchangeName="FRA", currency="EUR",
                          exchangeTimezoneName="UTC")
    result.pop("events")
    timestamps = []
    for row in rows:
        day = row.get("date") if isinstance(row, dict) else None
        try:
            timestamp = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
        except (ValueError, TypeError):
            timestamp = day
        timestamps.append(timestamp)
    result["timestamp"] = timestamps
    result["indicators"] = {
        "quote": [{key: [row.get(key) if isinstance(row, dict) else None for row in rows]
                   for key in ("open", "high", "low", "close", "volume")}],
        "adjclose": [{"adjclose": [row.get("adj_close") if isinstance(row, dict) else None for row in rows]}],
    }
    raw["unknown_evidence"] = deepcopy(rows)
    raw.update(metadata)
    return raw


def default_payload():
    return payload([{"date": "2026-09-15", "open": "44.213", "high": "44.213", "low": "44.213",
                     "close": "44.21", "volume": "1244.0"}])


def source(transport, pages=None):
    state, calls = transport
    state["meta"] = default_payload()
    responses = iter(pages if pages is not None else [default_payload()])

    def handler(session, method, url, kwargs):
        if "period1" in kwargs.get("params", {}):
            return response(url, deepcopy(next(responses)))

    state["handler"] = handler

    def factory(key, before_request):
        assert key == ""
        return YahooFinanceClient(before_request=before_request)

    return factory, calls


def enqueue(db, config):
    return md.request_acquisition(db, config, ISIN, LISTING, "update", "user", Mock())


def run(db, config, transport, pages=None):
    factory, calls = source(transport, pages)
    result = md.run_acquisition(db, config, enqueue(db, config)["id"], factory)
    assert result["status"] == "succeeded", result
    return result, calls


def test_inconsistent_bounds_survive_without_quality_filtering(db, config, transport):
    result, _ = run(db, config, transport)
    row = db.quotes.find_one()["data"]
    assert row["open"] == row["high"] == row["low"] == "44.213"
    assert row["close"] == "44.21" and row["volume"] == "1244.0"
    assert row["adj_close"] is None and "adj_open" not in row
    assert "quality_flags" not in row
    assert result["flagged_rows"] == 0
    assert "OHLC" not in (result["warning"] or "")
    assert db.raw_eod_pages.find_one()["data"]["data"][0] == row
    assert db.raw_eod_pages.find_one()["data"]["raw"] == default_payload()


def test_null_close_is_downloaded_and_exposed_without_repair(api, db, config, transport):
    raw = payload([{"date": "2026-09-15", "open": "12.338", "close": None}])
    result, _ = run(db, config, transport, [raw])
    assert result["rows"] == 1
    quote = db.quotes.find_one()["data"]
    assert quote["close"] is None and quote["open"] == "12.338"
    assert quote["symbol"] == LISTING["symbol"]
    client, _ = api
    client.post("/auth/login", json={"login": "admin", "password": "admin"})
    response = client.get(f"/instruments/{ISIN}/quotes")
    assert response.status_code == 200
    assert response.json()["items"][0]["close"] is None
    assert response.json()["items"][0]["open"] == "12.338"


def test_contradictory_chart_identity_fails_closed_and_publishes_nothing(db, config, transport):
    raw = default_payload()
    raw["chart"]["result"][0]["meta"]["symbol"] = "WEBN.DE"
    factory, _ = source(transport, [raw])
    result = md.run_acquisition(db, config, enqueue(db, config)["id"], factory)
    assert result["status"] == "failed"
    assert db.acquisition_jobs.find_one()["error_code"] == "identity_mismatch"
    assert db.quotes.count_documents({}) == 0
    assert md.read_quotes(db, ISIN)["items"] == []


@pytest.mark.parametrize("value", [None, 0, -1, "unexpected", "NaN", True, {"raw": [3]}, ["1", None]])
def test_price_values_do_not_block_ingestion_or_api(api, db, config, transport, value):
    row = {"date": "2026-09-15", **{key: value for key in ("open", "high", "low", "close", "volume")}}
    run(db, config, transport, [payload([row])])
    client, _ = api
    client.post("/auth/login", json={"login": "admin", "password": "admin"})
    response = client.get(f"/instruments/{ISIN}/quotes")
    assert response.status_code == 200
    expected = str(value) if type(value) is int else value
    assert response.json()["items"][0]["close"] == expected
    assert db.quotes.find_one()["data"]["close"] == value


def test_missing_fields_are_explicit_nulls_unknown_fields_remain_raw(api, db, config, transport):
    raw = payload([{"date": "2026-09-15", "unknown": {"retained": True}}])
    run(db, config, transport, [raw])
    client, _ = api
    client.post("/auth/login", json={"login": "admin", "password": "admin"})
    response = client.get(f"/instruments/{ISIN}/quotes")
    assert response.status_code == 200
    row = response.json()["items"][0]
    assert row["close"] is None and row["volume"] is None
    assert "unknown" not in row
    assert db.raw_eod_pages.find_one()["data"]["raw"] == raw


def test_raw_chart_retains_unusable_duplicate_unordered_and_out_of_range_dates(db, config, transport):
    rows = [
        {"date": "2026-09-15", "close": 1}, {"date": "2000-01-01", "close": 2},
        {"date": "2026-09-15", "close": 3}, {"date": "2099-01-01", "close": 4},
        {"date": "not-a-date", "close": 5}, {"close": 6}, None, ["unexpected"],
    ]
    raw = payload(rows)
    result, _ = run(db, config, transport, [raw])
    assert result["rows"] == 2 and result["unindexed_rows"] == 0
    saved = db.raw_eod_pages.find_one()["data"]
    assert saved["raw"] == raw and saved["raw"]["unknown_evidence"] == rows
    assert [row["close"] for row in saved["data"]] == [1, 3]
    assert db.quotes.count_documents({}) == 1
    assert db.instrument_state.find_one()["last_complete_quote_date"] == "2026-09-15"
    assert [item["date"] for item in md.read_quotes(db, ISIN)["items"]] == ["2026-09-15"]


def test_overlapping_rows_and_changed_raw_totals_are_preserved_in_one_complete_range(db, config, transport):
    raw = payload([{"date": "2026-09-15", "close": 1}, {"date": "2026-09-15", "close": 2},
                   {"date": "2026-09-14", "close": None}], pagination={"total": 999999, "offset": 1})
    result, calls = run(db, config, transport, [raw])
    assert result["pages"] == 1 and result["rows"] == 3
    assert len(calls) == result["calls"] == 4
    assert db.raw_eod_pages.count_documents({}) == 1 and db.quotes.count_documents({}) == 2
    assert db.raw_eod_pages.find_one()["data"]["raw"] == raw
    assert not any("/splits" in call[1] for call in calls)
    assert db.instrument_state.find_one()["last_complete_quote_date"] == "2026-09-15"


@pytest.mark.parametrize("pagination", [None, {}, {"limit": "1000", "count": 999, "offset": "wrong", "total": "1"}, {"limit": -1, "total": -1}])
def test_malformed_raw_pagination_metadata_does_not_block_data(db, config, transport, pagination):
    raw = payload([{"date": "2026-09-15", "close": None}], pagination=pagination)
    result, _ = run(db, config, transport, [raw])
    assert result["rows"] == 1
    assert db.raw_eod_pages.find_one()["data"]["raw"] == raw
    assert db.raw_eod_pages.find_one()["data"]["pagination"] == {"limit": 1, "offset": 0, "count": 1, "total": 1}


def test_missing_raw_pagination_needs_no_empty_probe_after_old_page_limit(db, config, transport):
    raw = payload([{"date": "2026-09-15"}] * 1000)
    result, calls = run(db, config, transport, [raw])
    assert result["pages"] == 1 and result["rows"] == 1000
    assert len(calls) == 4 and db.quotes.count_documents({}) == 1
    assert db.raw_eod_pages.find_one()["data"]["raw"] == raw


def test_saved_raw_page_resumes_without_another_provider_call(db, config, transport, clock, monkeypatch):
    factory, calls = source(transport)
    job = enqueue(db, config)
    real_update = db.quotes.update_one
    interrupted = []

    def write_then_disconnect(*args, **kwargs):
        result = real_update(*args, **kwargs)
        if not interrupted:
            interrupted.append(True)
            raise ConnectionFailure("Write committed but acknowledgement lost")
        return result

    monkeypatch.setattr(db.quotes, "update_one", write_then_disconnect)
    with pytest.raises(ConnectionFailure):
        md.run_acquisition(db, config, job["id"], factory)
    assert db.raw_eod_pages.count_documents({}) == 1
    clock[0] += timedelta(seconds=md.LEASE_SECONDS + 1)
    result = md.run_acquisition(db, config, job["id"], factory)
    assert result["status"] == "succeeded" and result["rows"] == 1
    assert len(calls) == result["calls"] == 4
    assert db.quotes.count_documents({}) == 1


def test_current_job_safety_cap_prevents_final_history_request(db, config, transport):
    config.market_data_max_calls_per_job = 3
    factory, calls = source(transport)
    result = md.run_acquisition(db, config, enqueue(db, config)["id"], factory)
    assert result["status"] == "failed" and "safety limit" in result["error"]
    assert len(calls) == result["calls"] == 3
    assert db.raw_eod_pages.count_documents({}) == 0
    assert db.quotes.count_documents({}) == 0
    assert not any("period1" in call[2].get("params", {}) for call in calls)
