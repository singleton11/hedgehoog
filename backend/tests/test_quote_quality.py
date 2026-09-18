"""Permissive raw ingestion: cleanup is deferred, not an acquisition blocker."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from pymongo.errors import ConnectionFailure

import market_data as md
from marketstack import MarketstackClient
from test_market_data import api, clock, config, db

ISIN = "IE00B4L5Y983"
LISTING = {"symbol": "EUNL.F", "exchange": "XFRA"}
FIXTURES = Path(__file__).parent / "fixtures"


def source(pages=None, profile=None):
    pages = pages if pages is not None else [json.loads((FIXTURES / "eunl_inconsistent_ohlc.json").read_text())]
    profile = profile if profile is not None else {"ticker": "EUNL.F"}
    responses = iter(pages)
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("tickerinfo"):
            return httpx.Response(200, json={"data": profile})
        return httpx.Response(200, json=deepcopy(next(responses)))

    def factory(key, before_request):
        return MarketstackClient(key, before_request, httpx.MockTransport(respond))

    return factory, calls


def enqueue(db, config):
    return md.request_acquisition(db, config, ISIN, LISTING, "update", "user", Mock())


def run(db, config, pages=None):
    factory, calls = source(pages)
    result = md.run_acquisition(db, config, enqueue(db, config)["id"], factory)
    assert result["status"] == "succeeded", result
    return result, calls


def payload(rows, **metadata):
    return {"pagination": {"limit": 1000, "offset": 0, "count": len(rows), "total": len(rows), **metadata}, "data": rows}


def test_inconsistent_eunl_bounds_survive_without_quality_filtering(db, config):
    result, _ = run(db, config)
    row = db.quotes.find_one()["data"]
    assert row["open"] == row["high"] == row["low"] == "44.213"
    assert row["close"] == "44.21"
    assert row["volume"] == "1244.0"
    assert row["adj_open"] is None
    assert "quality_flags" not in row
    assert result["flagged_rows"] == 0
    assert "OHLC" not in (result["warning"] or "")
    assert db.raw_eod_pages.find_one()["data"]["data"][0] == row


def test_webn_null_close_is_downloaded_and_exposed_without_repair(api, db, config):
    raw = json.loads((FIXTURES / "webn_null_close.json").read_text())
    result, _ = run(db, config, [raw])
    assert result["rows"] == 1
    quote = db.quotes.find_one()["data"]
    assert quote["close"] is None and quote["open"] == "12.338"
    assert quote["symbol"] == "WEBN.DE"  # Even identity mismatches are preserved, not blockers.
    client, _ = api
    client.post("/auth/login", json={"login": "admin", "password": "admin"})
    response = client.get(f"/instruments/{ISIN}/quotes")
    assert response.status_code == 200
    assert response.json()["items"][0]["close"] is None
    assert response.json()["items"][0]["open"] == "12.338"


@pytest.mark.parametrize("value", [None, 0, -1, "unexpected", "NaN", True, {"raw": [3]}, ["1", None]])
def test_price_values_do_not_block_ingestion_or_api(api, db, config, value):
    row = {"date": "2026-09-15", "open": value, "high": value, "low": value, "close": value, "volume": value, "adj_open": value}
    run(db, config, [payload([row])])
    client, _ = api
    client.post("/auth/login", json={"login": "admin", "password": "admin"})
    response = client.get(f"/instruments/{ISIN}/quotes")
    assert response.status_code == 200
    expected = str(value) if type(value) is int else value
    assert response.json()["items"][0]["close"] == expected
    assert db.quotes.find_one()["data"]["close"] == value


def test_missing_fields_are_not_invented(api, db, config):
    run(db, config, [payload([{"date": "2026-09-15", "unknown": {"retained": True}}])])
    client, _ = api
    client.post("/auth/login", json={"login": "admin", "password": "admin"})
    response = client.get(f"/instruments/{ISIN}/quotes")
    assert response.status_code == 200
    row = response.json()["items"][0]
    assert "close" not in row and "volume" not in row
    assert row["unknown"] == {"retained": True}


def test_raw_pages_retain_unindexable_duplicate_unordered_and_out_of_range_rows(db, config):
    rows = [
        {"date": "2026-09-15", "close": 1},
        {"date": "2000-01-01", "close": 2},  # Before inception.
        {"date": "2026-09-15", "close": 3},  # Duplicate date, preserved in raw page.
        {"date": "2099-01-01", "close": 4},  # Future row must not poison update watermark.
        {"date": "not-a-date", "close": 5}, {"close": 6}, None, ["unexpected"],
    ]
    result, _ = run(db, config, [payload(rows)])
    assert result["rows"] == 8 and result["unindexed_rows"] == 4
    assert db.raw_eod_pages.find_one()["data"]["data"] == rows
    assert db.quotes.count_documents({}) == 3
    assert db.instrument_state.find_one()["last_complete_quote_date"] == "2026-09-15"
    assert "retained in raw EOD pages" in result["warning"]
    assert [item["date"] for item in md.read_quotes(db, ISIN)["items"]] == ["2000-01-01", "2026-09-15", "2099-01-01"]


def test_overlapping_pages_and_changing_totals_are_not_rejected(db, config):
    first = payload([{"date": "2026-09-15", "close": 1}], total=2)
    second = payload([{"date": "2026-09-15", "close": 2}, {"date": "2026-09-14", "close": None}], total=3, offset=1)
    result, calls = run(db, config, [first, second])
    assert result["pages"] == 2 and result["rows"] == 3
    assert calls == ["/v2/tickerinfo", "/v2/eod", "/v2/eod"]
    assert db.raw_eod_pages.count_documents({}) == db.quotes.count_documents({}) == 2
    assert db.raw_eod_pages.find_one({"offset": 1})["data"] == second
    assert db.instrument_state.find_one()["last_complete_quote_date"] == "2026-09-15"


@pytest.mark.parametrize("pagination", [None, {}, {"limit": "1000", "count": 999, "offset": "wrong", "total": "1"}, {"limit": -1, "total": -1}])
def test_malformed_pagination_metadata_does_not_block_data(db, config, pagination):
    page = {"data": [{"date": "2026-09-15", "close": None}], "pagination": pagination}
    result, _ = run(db, config, [page])
    assert result["rows"] == 1
    assert db.raw_eod_pages.find_one()["data"] == page


def test_missing_pagination_and_empty_page_terminate_safely(db, config):
    rows = [{"date": "2026-09-15"}] * 1000
    result, calls = run(db, config, [{"data": rows}, {"data": [], "pagination": {"total": 999999}}])
    assert result["pages"] == 2 and result["rows"] == 1000
    assert len(calls) == 3


def test_saved_raw_page_resumes_without_another_provider_call(db, config, clock, monkeypatch):
    factory, calls = source()
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
    assert len(calls) == result["calls"] == 2
    assert db.quotes.count_documents({}) == 1


def test_unbounded_provider_responses_still_obey_job_safety_cap(db, config):
    config.marketstack_max_calls_per_job = 3
    pages = [payload([{"date": "2026-09-15"}], total=999999)] * 3
    factory, calls = source(pages)
    result = md.run_acquisition(db, config, enqueue(db, config)["id"], factory)
    assert result["status"] == "failed" and "safety limit" in result["error"]
    assert len(calls) == result["calls"] == 3
    assert db.raw_eod_pages.count_documents({}) == 2
