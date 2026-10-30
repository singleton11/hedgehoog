"""Automatic Yahoo listing selection and publication fencing, entirely offline."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock

import pytest
from pymongo.errors import ConnectionFailure

import market_data as md
from yahoo_finance import YahooFinanceClient
from test_market_data import ISIN, LISTING, api, clock, config, db
from test_yahoo_finance import chart, listing, no_live_http, response, transport


def candidate(symbol="SXR8.DE", name="Named ETF", exchange="GER", **extra):
    return listing(symbol, exchange, longname=name, **extra)


@pytest.fixture
def yahoo(transport):
    state, calls = transport
    for phase in ("meta", "history"):
        state[phase]["chart"]["result"][0]["meta"].update(symbol=LISTING["symbol"], exchangeName="GER", currency="EUR")
    state["search"] = {"quotes": [candidate()]}

    def factory(key, before_request):
        assert key == ""
        return YahooFinanceClient(before_request=before_request)

    return state, calls, factory


def auto_enqueue(db, config, mode="update", publish=None):
    return md.request_acquisition(db, config, ISIN, None, mode, "user", publish or Mock())


def searches(calls):
    return [call[2]["params"]["q"] for call in calls if call[1].endswith("/search")]


def test_auto_queues_without_lookup_then_caches_mapping_and_updates_without_search(db, config, clock, yahoo):
    state, calls, factory = yahoo
    publish = Mock()
    job = auto_enqueue(db, config, publish=publish)
    assert auto_enqueue(db, config, publish=publish)["id"] == job["id"]
    publish.assert_called_once()
    assert calls == [] and md.instrument_detail(db, config, ISIN)["listing"] is None
    assert db.acquisition_jobs.find_one()["lookup_ticker"] == "SXR8"
    state["search"] = {"quotes": [candidate("SXR8F.DE"), candidate("XSXR8.DE"), candidate()]}
    result = md.run_acquisition(db, config, job["id"], factory)
    assert result["status"] == "succeeded", result
    assert searches(calls) == ["SXR8"]
    assert result["calls"] == len(calls) == 5
    state_doc = db.instrument_state.find_one()
    assert state_doc["listing"] == LISTING
    assert state_doc["listing_resolution"]["name"] == "Named ETF"
    assert state_doc["listing_resolution"]["data"]["exchange"] == "GER"
    assert md.instrument_detail(db, config, ISIN)["listing_resolution"]["source"] == "yahoo_search"
    clock[0] += timedelta(hours=1)
    db.etfs.update_many({"isin": ISIN}, {"$set": {"ticker": "CHANGED"}})
    next_job = auto_enqueue(db, config)
    result = md.run_acquisition(db, config, next_job["id"], factory)
    assert result["status"] == "succeeded" and result["calls"] == 4
    assert searches(calls) == ["SXR8"]
    assert md.budget_status(db, config)["used"] == db.marketstack_usage.count_documents({}) == 0
    assert db.acquisition_jobs.find_one({"_id": next_job["id"]})["date_from"] == "2010-05-19"


@pytest.mark.parametrize("row,code", [
    (candidate("XSXR8.DE"), "listing_not_found"), (candidate("SXR8F.DE"), "listing_not_found"),
    (candidate("SXR8.DE.EXTRA"), "listing_not_found"), (candidate("SXR8."), "listing_not_found"),
    (candidate("SXR8.DE "), "listing_not_found"), (candidate(symbol=None), "listing_not_found"),
    (candidate(exchange=None), "unknown_exchange"), (candidate(exchange="XETR"), "unknown_exchange"),
    (candidate(exchange="GER "), "unknown_exchange"), (candidate(quoteType="OPTION"), "listing_not_found"),
    (candidate(quoteType=True), "listing_not_found"), (None, "listing_not_found"),
    ({}, "listing_not_found"), ([], "listing_not_found"), (True, "listing_not_found"),
    (candidate(isin="IE00B4L5Y983"), "listing_not_found"),
])
def test_unusable_rows_do_not_trigger_metadata_or_eod_or_cache(db, config, yahoo, row, code):
    state, calls, factory = yahoo
    state["handler"] = lambda session, method, url, kwargs: (
        response(url, {"quotes": [row] if kwargs["params"]["q"] == "SXR8" else []})
        if url.endswith("/search") else None)
    job = auto_enqueue(db, config)
    result = md.run_acquisition(db, config, job["id"], factory)
    assert result["status"] == "failed"
    assert db.acquisition_jobs.find_one({"_id": job["id"]})["error_code"] == code
    assert not any("/chart/" in call[1] for call in calls)
    assert result["calls"] == len(calls) == (3 if code == "unknown_exchange" else 4)
    assert db.instrument_state.find_one() is None
    assert db.instruments.count_documents({}) == db.quotes.count_documents({}) == 0


@pytest.mark.parametrize("name", [None, "", " \t ", 123])
def test_optional_display_name_does_not_replace_exact_symbol_identity(db, config, yahoo, name):
    state, _, factory = yahoo
    state["search"] = {"quotes": [candidate(name=name)]}
    job = auto_enqueue(db, config)
    assert md.run_acquisition(db, config, job["id"], factory)["status"] == "succeeded"
    assert db.instrument_state.find_one()["listing"] == LISTING


def test_search_uses_explicit_isin_fallback_not_retired_pagination(db, config, yahoo):
    state, calls, factory = yahoo

    def handler(session, method, url, kwargs):
        if url.endswith("/search"):
            rows = [candidate("SXR8F.DE")] if kwargs["params"]["q"] == "SXR8" else [candidate(isin=ISIN)]
            return response(url, {"quotes": rows, "pagination": {"total": 1000}})

    state["handler"] = handler
    job = auto_enqueue(db, config)
    result = md.run_acquisition(db, config, job["id"], factory)
    assert result["status"] == "succeeded" and result["calls"] == 6
    assert searches(calls) == ["SXR8", ISIN]
    assert db.instrument_state.find_one()["listing"] == LISTING


def test_pattern_escapes_catalogue_ticker_metacharacters(yahoo):
    state, _, factory = yahoo
    state["search"] = {"quotes": [candidate("ABCXDE", exchange="LSE"), candidate("ABC.DE", exchange="GER")]}
    with factory("", Mock()) as client:
        selected, _ = client.resolve_listing("ABC.DE", ISIN)
    assert selected == {"symbol": "ABC.DE", "exchange": "XETR"}


@pytest.mark.parametrize("status,code", [(200, "listing_not_found"), (403, "plan_restricted")])
def test_failed_search_is_not_cached_and_can_be_explicitly_retried(db, config, yahoo, status, code):
    state, _, factory = yahoo
    state["search"] = {"quotes": []}
    if status != 200:
        state["handler"] = lambda session, method, url, kwargs: response(url, {}, status) if url.endswith("/search") else None
    job = auto_enqueue(db, config)
    assert md.run_acquisition(db, config, job["id"], factory)["status"] == "failed"
    assert db.acquisition_jobs.find_one({"_id": job["id"]})["error_code"] == code
    assert db.instrument_state.find_one() is None
    state["handler"] = None
    state["search"] = {"quotes": [candidate()]}
    retry = auto_enqueue(db, config)
    assert md.run_acquisition(db, config, retry["id"], factory)["status"] == "succeeded"


def test_contradictory_provider_isin_blocks_selection_regardless_of_raw_totals(db, config, yahoo):
    state, calls, factory = yahoo
    state["search"] = {"quotes": [candidate(isin="IE00B4L5Y983")], "pagination": {"total": 999}}
    job = auto_enqueue(db, config)
    assert md.run_acquisition(db, config, job["id"], factory)["status"] == "failed"
    assert searches(calls) == ["SXR8", ISIN]
    assert db.instrument_state.find_one() is None
    assert db.instruments.count_documents({}) == 0


def test_mapping_is_not_published_after_eod_failure_and_retry_resolves_again(db, config, yahoo):
    state, calls, factory = yahoo
    state["handler"] = lambda session, method, url, kwargs: response(url, {}, 500) if "period1" in kwargs.get("params", {}) else None
    job = auto_enqueue(db, config)
    assert md.run_acquisition(db, config, job["id"], factory)["status"] == "failed"
    assert db.instrument_state.find_one() is None
    saved = db.acquisition_jobs.find_one({"_id": job["id"]})
    assert saved["listing"] == LISTING and saved["listing_resolution"]["source"] == "yahoo_search"
    state["handler"] = None
    retry = auto_enqueue(db, config)
    assert md.run_acquisition(db, config, retry["id"], factory)["status"] == "succeeded"
    assert searches(calls) == ["SXR8", "SXR8"]


def test_cache_write_interruption_resumes_without_search(db, config, clock, yahoo, monkeypatch):
    _, calls, factory = yahoo
    job = auto_enqueue(db, config)
    real_update = db.instrument_state.update_one

    def interrupted(*args, **kwargs):
        real_update(*args, **kwargs)
        raise ConnectionFailure("Cache committed but acknowledgement lost")

    monkeypatch.setattr(db.instrument_state, "update_one", interrupted)
    with pytest.raises(ConnectionFailure):
        md.run_acquisition(db, config, job["id"], factory)
    assert db.acquisition_jobs.find_one()["listing"] == LISTING
    assert db.instrument_state.find_one()["listing"] == LISTING
    monkeypatch.setattr(db.instrument_state, "update_one", real_update)
    clock[0] += timedelta(seconds=md.LEASE_SECONDS + 1)
    count = len(calls)
    result = md.run_acquisition(db, config, job["id"], factory)
    assert result["status"] == "succeeded" and result["calls"] == 5
    assert len(calls) == count


def test_stale_resolver_cannot_overwrite_replacement_mapping_or_watermark(db, config, clock, yahoo, monkeypatch):
    state, calls, factory = yahoo
    job = auto_enqueue(db, config)
    real_checkpoint = db.acquisition_jobs.find_one_and_update
    replacement = {"symbol": "SXR8.L", "exchange": "XLON"}
    takeover = []

    def paused(query, update, *args, **kwargs):
        if not takeover and update.get("$set", {}).get("listing") == LISTING:
            takeover.append(True)
            clock[0] += timedelta(seconds=md.LEASE_SECONDS + 1)
            state["search"] = {"quotes": [candidate("SXR8.L", exchange="LSE")]}
            for phase in ("meta", "history"):
                state[phase]["chart"]["result"][0]["meta"].update(symbol="SXR8.L", exchangeName="LSE")
            assert md.run_acquisition(db, config, job["id"], factory)["status"] == "succeeded"
        return real_checkpoint(query, update, *args, **kwargs)

    monkeypatch.setattr(db.acquisition_jobs, "find_one_and_update", paused)
    assert md.run_acquisition(db, config, job["id"], factory)["status"] == "succeeded"
    assert takeover == [True]
    saved = db.instrument_state.find_one()
    assert saved["listing"] == replacement and saved["last_complete_quote_date"] == "2024-04-02"
    assert saved["listing_resolution"]["data"]["symbol"] == "SXR8.L"
    assert db.acquisition_jobs.find_one()["listing"] == replacement
    assert md.read_quotes(db, ISIN)["total"] == 2
    assert not any("/chart/SXR8.DE" in call[1] for call in calls)


@pytest.mark.parametrize("limit_kind", ["retired-monthly", "job"])
def test_search_obeys_current_job_limit_not_retired_budget(db, config, yahoo, limit_kind):
    _, calls, factory = yahoo
    db.marketstack_usage.insert_one({"_id": "2026-09", "used": 10**9})
    if limit_kind == "job":
        config.market_data_max_calls_per_job = 1
    job = auto_enqueue(db, config)
    result = md.run_acquisition(db, config, job["id"], factory)
    if limit_kind == "job":
        assert result["status"] == "failed" and result["calls"] == len(calls) == 1
        assert db.instrument_state.find_one() is None
    else:
        assert result["status"] == "succeeded" and result["calls"] == len(calls) == 5
    assert db.marketstack_usage.find_one()["used"] == 10**9


@pytest.mark.parametrize("ticker", [None, "", "  ", "A,B", "A" * 65, ["SXR8"]])
def test_missing_or_invalid_catalogue_ticker_requires_manual_override(db, config, ticker):
    db.etfs.update_many({"isin": ISIN}, {"$set": {"ticker": ticker}})
    with pytest.raises(md.DataError, match="no valid catalogue ticker"):
        auto_enqueue(db, config)
    assert md.request_acquisition(db, config, ISIN, LISTING, "update", "user", Mock())["status"] == "queued"


def test_backfill_and_duplicate_delivery_reuse_published_mapping(db, config, yahoo, clock):
    _, calls, factory = yahoo
    job = auto_enqueue(db, config)
    assert md.run_acquisition(db, config, job["id"], factory)["status"] == "succeeded"
    no_provider = Mock(side_effect=AssertionError("Completed delivery must not call provider"))
    assert md.run_acquisition(db, config, job["id"], no_provider)["status"] == "succeeded"
    no_provider.assert_not_called()
    clock[0] += timedelta(seconds=1)
    backfill = auto_enqueue(db, config, mode="backfill")
    result = md.run_acquisition(db, config, backfill["id"], factory)
    assert result["status"] == "succeeded" and result["mode"] == "refresh"
    assert searches(calls) == ["SXR8"]
    saved = db.acquisition_jobs.find_one({"_id": backfill["id"]})
    assert (saved["date_from"], saved["date_to"]) == ("2010-05-19", "2026-09-17")


def test_manual_override_does_not_publish_failed_resolution_evidence(db, config, yahoo):
    state, _, factory = yahoo
    state["handler"] = lambda session, method, url, kwargs: response(url, {}, 500) if "period1" in kwargs.get("params", {}) else None
    job = auto_enqueue(db, config)
    assert md.run_acquisition(db, config, job["id"], factory)["status"] == "failed"
    replacement = {"symbol": "CSPX.L", "exchange": "XLON"}
    manual = md.request_acquisition(db, config, ISIN, replacement, "update", "user", Mock())
    assert db.instrument_state.find_one() is None
    saved = db.acquisition_jobs.find_one({"_id": manual["id"]})
    assert saved["listing"] == replacement and saved.get("listing_resolution") is None


def test_identical_catalogue_tickers_on_different_isins_do_not_share_cache(db, config, yahoo):
    _, calls, factory = yahoo
    job = auto_enqueue(db, config)
    assert md.run_acquisition(db, config, job["id"], factory)["status"] == "succeeded"
    other = "IE00B4L5Y983"
    db.etfs.update_many({"isin": other}, {"$set": {"ticker": "SXR8"}})
    other_job = md.request_acquisition(db, config, other, None, "update", "user", Mock())
    assert md.run_acquisition(db, config, other_job["id"], factory)["status"] == "succeeded"
    assert searches(calls) == ["SXR8", "SXR8"]
    assert db.instrument_state.count_documents({}) == 2


def test_auto_api_requires_authorization_and_accepts_manual_yahoo_symbol(api, db, config, yahoo, clock):
    _, _, factory = yahoo
    client, publish = api
    path = f"/instruments/{ISIN}/acquisitions"
    assert client.post(path, json={"confirmed": True}).status_code == 401
    client.post("/auth/login", json={"login": "admin", "password": "admin"})
    for body in ({}, {"confirmed": 1}, {"confirmed": False}, {"confirmed": True, "exchange": "XETR"}):
        assert client.post(path, json=body).status_code == 422
    response = client.post(path, json={"confirmed": True, "mode": "update"})
    assert response.status_code == 202
    job_id = response.json()["job"]["id"]
    assert client.post(path, json={"confirmed": True}).json()["job"]["id"] == job_id
    assert client.post(path, json={**LISTING, "confirmed": True}).status_code == 409
    publish.assert_called_once()
    assert md.budget_status(db, config)["used"] == 0
    assert client.get(f"/instruments/{ISIN}").json()["listing"] is None
    assert md.run_acquisition(db, config, job_id, factory)["status"] == "succeeded"
    assert client.get(f"/instruments/{ISIN}").json()["listing"] == LISTING
    clock[0] += timedelta(seconds=1)
    assert client.post(path, json={"confirmed": True, "symbol": "SXR8.DE"}).status_code == 202
