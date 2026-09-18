"""Automatic listing selection and durable cache; no live provider requests."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock

import pytest
from pymongo.errors import ConnectionFailure

import market_data as md
from marketstack import MarketstackError
from test_market_data import ISIN, LISTING, Provider, api, bar, clock, config, db, page


def candidate(ticker="SXR8.DE", name="Named ETF", mic="XETR", **extra):
    return {"ticker": ticker, "name": name, "has_eod": True,
            "stock_exchange": {"mic": mic, "name": "XETRA"}, **extra}


class SearchProvider(Provider):
    def __init__(self, search_pages=None, **kwargs):
        super().__init__(**kwargs)
        self.search_pages = iter(search_pages if search_pages is not None else [page([candidate()])])

    def tickerslist_page(self, search, offset=0):
        self.before_request()
        self.calls.append(("tickerslist", search, offset))
        result = next(self.search_pages)
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


def auto_enqueue(db, config, mode="update", publish=None):
    return md.request_acquisition(db, config, ISIN, None, mode, "user", publish or Mock())


def test_auto_queues_without_lookup_then_caches_mapping_and_updates_without_search(db, config, clock):
    publish = Mock()
    job = auto_enqueue(db, config, publish=publish)
    assert auto_enqueue(db, config, publish=publish)["id"] == job["id"]
    publish.assert_called_once()
    assert md.instrument_detail(db, config, ISIN)["listing"] is None
    assert md.budget_status(db, config)["used"] == 0
    assert db.acquisition_jobs.find_one()["lookup_ticker"] == "SXR8"
    source = SearchProvider([page([
        candidate("SXR8F.DE"), candidate("SXR8"), candidate("SXR8.DU", name=" "),
        candidate("SXR8.DE", name="First named match"), candidate("SXR8.L", mic="XLON"),
    ])])
    result = md.run_acquisition(db, config, job["id"], source.factory)
    assert result["status"] == "succeeded", result
    assert [call[0] for call in source.calls] == ["tickerslist", "tickerinfo", "eod"]
    assert source.calls[0] == ("tickerslist", "SXR8", 0)
    assert source.calls[2][1:3] == ("SXR8.DE", "XETR")
    assert result["calls"] == md.budget_status(db, config)["used"] == 3
    state = db.instrument_state.find_one()
    assert state["listing"] == LISTING
    assert state["listing_resolution"]["name"] == "First named match"
    assert state["listing_resolution"]["data"]["stock_exchange"]["mic"] == "XETR"
    assert md.instrument_detail(db, config, ISIN)["listing_resolution"]["source"] == "tickerslist"
    clock[0] += timedelta(hours=1)
    # Cache is ISIN-scoped and survives catalogue changes and fresh worker objects.
    db.etfs.update_many({"isin": ISIN}, {"$set": {"ticker": "CHANGED"}})
    next_job = auto_enqueue(db, config)
    update = Provider()  # Deliberately has no search method.
    assert md.run_acquisition(db, config, next_job["id"], update.factory)["status"] == "succeeded"
    assert [call[0] for call in update.calls] == ["tickerinfo", "eod"]
    assert md.budget_status(db, config)["used"] == 5
    assert update.calls[1][3] == "2026-09-16"


@pytest.mark.parametrize("row", [
    candidate("XSXR8.DE"), candidate("SXR8F.DE"), candidate("SXR8.DE.EXTRA"),
    candidate("SXR8."), candidate("SXR8"), candidate("SXR8.DE "),
    candidate(name=None), candidate(name=""), candidate(name=" \t "), candidate(name=123),
    candidate(mic=None), candidate(mic="GER"), candidate(mic="XETR "),
    candidate(has_eod=False), candidate(has_eod=1), candidate(stock_exchange=None),
    candidate(ticker=None),
])
def test_unusable_rows_do_not_trigger_metadata_or_eod_or_cache(db, config, row):
    job = auto_enqueue(db, config)
    source = SearchProvider([page([row])])
    result = md.run_acquisition(db, config, job["id"], source.factory)
    assert result["status"] == "failed"
    assert "No named EOD listing" in result["error"]
    assert len(source.calls) == result["calls"] == 1
    assert db.instrument_state.find_one() is None
    assert db.instruments.count_documents({}) == db.quotes.count_documents({}) == 0


def test_search_paginates_in_order_and_stops_at_first_match(db, config):
    source = SearchProvider([
        page([candidate("SXR8F")], total=3),
        page([candidate("sxr8.de", mic="xetr")], total=3, offset=1),
    ])
    job = auto_enqueue(db, config)
    result = md.run_acquisition(db, config, job["id"], source.factory)
    assert result["status"] == "succeeded"
    assert source.calls[:2] == [("tickerslist", "SXR8", 0), ("tickerslist", "SXR8", 1)]
    assert result["calls"] == 4
    assert db.instrument_state.find_one()["listing"] == LISTING


def test_pattern_escapes_catalogue_ticker_metacharacters():
    source = SearchProvider([page([candidate("ABCXDE.L", mic="XLON"), candidate("ABC.DE.L", mic="XLON")])])
    source.before_request = Mock()
    listing, _ = md.resolve_listing(source, "ABC.DE", ISIN)
    assert listing == {"symbol": "ABC.DE.L", "exchange": "XLON"}


@pytest.mark.parametrize("pages,code", [
    ([page([])], "listing_not_found"),
    ([MarketstackError("plan_restricted", "Plan restricted")], "plan_restricted"),
])
def test_failed_search_is_not_cached_and_can_be_explicitly_retried(db, config, pages, code):
    job = auto_enqueue(db, config)
    assert md.run_acquisition(db, config, job["id"], SearchProvider(pages).factory)["status"] == "failed"
    assert db.acquisition_jobs.find_one({"_id": job["id"]})["error_code"] == code
    assert db.instrument_state.find_one() is None
    retry = auto_enqueue(db, config)
    assert md.run_acquisition(db, config, retry["id"], SearchProvider().factory)["status"] == "succeeded"


def test_search_accepts_changing_totals_and_provider_isin_without_blocking(db, config):
    source = SearchProvider([
        page([candidate("WRONG")], total=2),
        page([candidate(isin="IE00B4L5Y983")], total=3, offset=1),
    ])
    job = auto_enqueue(db, config)
    assert md.run_acquisition(db, config, job["id"], source.factory)["status"] == "succeeded"
    assert db.instrument_state.find_one()["listing_resolution"]["data"]["isin"] == "IE00B4L5Y983"


def test_mapping_survives_later_eod_failure_and_retry_skips_search(db, config):
    source = SearchProvider(pages=[MarketstackError("http_error", "Provider unavailable")])
    job = auto_enqueue(db, config)
    assert md.run_acquisition(db, config, job["id"], source.factory)["status"] == "failed"
    assert db.instrument_state.find_one()["listing"] == LISTING
    assert db.instrument_state.find_one()["last_complete_quote_date"] is None
    retry = auto_enqueue(db, config)
    assert md.run_acquisition(db, config, retry["id"], Provider().factory)["status"] == "succeeded"


def test_cache_write_interruption_resumes_without_search(db, config, clock, monkeypatch):
    job = auto_enqueue(db, config)
    real_update = db.instrument_state.find_one_and_update

    def interrupted(*args, **kwargs):
        real_update(*args, **kwargs)
        raise ConnectionFailure("Cache committed but acknowledgement lost")

    monkeypatch.setattr(db.instrument_state, "find_one_and_update", interrupted)
    with pytest.raises(ConnectionFailure):
        md.run_acquisition(db, config, job["id"], SearchProvider().factory)
    assert db.acquisition_jobs.find_one()["listing"] is None
    assert db.instrument_state.find_one()["listing"] == LISTING
    monkeypatch.setattr(db.instrument_state, "find_one_and_update", real_update)
    clock[0] += timedelta(seconds=md.LEASE_SECONDS + 1)
    result = md.run_acquisition(db, config, job["id"], Provider().factory)
    assert result["status"] == "succeeded" and result["calls"] == 3


def test_stale_resolver_cannot_overwrite_replacement_mapping_or_watermark(db, config, clock, monkeypatch):
    job = auto_enqueue(db, config)
    real_update = db.instrument_state.find_one_and_update
    replacement = {"symbol": "SXR8.L", "exchange": "XLON"}
    source = SearchProvider(
        [page([candidate("SXR8.L", mic="XLON")])],
        profile={"ticker": "SXR8.L"}, pages=[page([{**bar(), **replacement}])],
    )
    takeover = []

    def paused(*args, **kwargs):
        if not takeover:
            takeover.append(True)
            # Original owner pauses just after checkpoint and before installing
            # its mapping. A replacement acquires the expired lease and finishes.
            clock[0] += timedelta(seconds=md.LEASE_SECONDS + 1)
            assert md.run_acquisition(db, config, job["id"], source.factory)["status"] == "succeeded"
        return real_update(*args, **kwargs)

    monkeypatch.setattr(db.instrument_state, "find_one_and_update", paused)
    original = SearchProvider()
    assert md.run_acquisition(db, config, job["id"], original.factory)["status"] == "succeeded"
    state = db.instrument_state.find_one()
    assert state["listing"] == replacement
    assert state["last_complete_quote_date"] == "2026-09-15"
    assert state["listing_resolution"]["data"]["ticker"] == "SXR8.L"
    assert db.acquisition_jobs.find_one()["listing"] == replacement
    assert md.read_quotes(db, ISIN)["total"] == 1
    assert len(original.calls) == 1  # Stale owner stops before tickerinfo/EOD.


@pytest.mark.parametrize("limit_kind", ["monthly", "job"])
def test_search_pages_obey_quota_and_job_limits(db, config, limit_kind):
    if limit_kind == "monthly":
        config.marketstack_monthly_budget = 1
    else:
        config.marketstack_max_calls_per_job = 1
    job = auto_enqueue(db, config)
    source = SearchProvider([page([candidate("WRONG")], total=2), page([candidate()], total=2, offset=1)])
    result = md.run_acquisition(db, config, job["id"], source.factory)
    assert result["status"] == "failed" and result["calls"] == 1
    assert len(source.calls) == 1
    assert db.instrument_state.find_one() is None


@pytest.mark.parametrize("ticker", [None, "", "  ", "A,B", "A" * 65, ["SXR8"]])
def test_missing_or_invalid_catalogue_ticker_requires_manual_override(db, config, ticker):
    db.etfs.update_many({"isin": ISIN}, {"$set": {"ticker": ticker}})
    with pytest.raises(md.DataError, match="no valid catalogue ticker"):
        auto_enqueue(db, config)
    assert md.request_acquisition(db, config, ISIN, LISTING, "update", "user", Mock())["status"] == "queued"


def test_backfill_and_duplicate_delivery_reuse_cached_mapping(db, config):
    job = auto_enqueue(db, config)
    assert md.run_acquisition(db, config, job["id"], SearchProvider().factory)["status"] == "succeeded"
    no_provider = Mock(side_effect=AssertionError("Completed delivery must not call provider"))
    assert md.run_acquisition(db, config, job["id"], no_provider)["status"] == "succeeded"
    no_provider.assert_not_called()
    backfill = auto_enqueue(db, config, mode="backfill")
    source = Provider(pages=[page([])])
    result = md.run_acquisition(db, config, backfill["id"], source.factory)
    assert result["status"] == "succeeded" and result["mode"] == "backfill"
    assert [call[0] for call in source.calls] == ["tickerinfo", "eod"]
    assert source.calls[1][3:5] == ("2010-05-19", "2026-09-14")


def test_manual_override_clears_old_resolution_evidence_before_quotes_exist(db, config):
    job = auto_enqueue(db, config)
    source = SearchProvider(pages=[MarketstackError("http_error", "Unavailable")])
    assert md.run_acquisition(db, config, job["id"], source.factory)["status"] == "failed"
    replacement = {"symbol": "CSPX.L", "exchange": "XLON"}
    md.request_acquisition(db, config, ISIN, replacement, "update", "user", Mock())
    state = db.instrument_state.find_one()
    assert state["listing"] == replacement
    assert state["listing_resolution"] is None
    assert state["last_complete_quote_date"] is None


def test_identical_catalogue_tickers_on_different_isins_do_not_share_cache(db, config):
    job = auto_enqueue(db, config)
    assert md.run_acquisition(db, config, job["id"], SearchProvider().factory)["status"] == "succeeded"
    other = "IE00B4L5Y983"
    db.etfs.update_many({"isin": other}, {"$set": {"ticker": "SXR8"}})
    other_job = md.request_acquisition(db, config, other, None, "update", "user", Mock())
    source = SearchProvider()
    assert md.run_acquisition(db, config, other_job["id"], source.factory)["status"] == "succeeded"
    assert source.calls[0] == ("tickerslist", "SXR8", 0)
    assert db.instrument_state.count_documents({}) == 2


def test_auto_api_requires_authorization_not_symbol_and_mic(api, db, config):
    client, publish = api
    path = f"/instruments/{ISIN}/acquisitions"
    assert client.post(path, json={"confirmed": True}).status_code == 401
    client.post("/auth/login", json={"login": "admin", "password": "admin"})
    for body in ({}, {"confirmed": 1}, {"confirmed": False}, {"confirmed": True, "symbol": "SXR8.DE"}, {"confirmed": True, "exchange": "XETR"}):
        assert client.post(path, json=body).status_code == 422
    response = client.post(path, json={"confirmed": True, "mode": "update"})
    assert response.status_code == 202
    job_id = response.json()["job"]["id"]
    assert client.post(path, json={"confirmed": True}).json()["job"]["id"] == job_id
    assert client.post(path, json={**LISTING, "confirmed": True}).status_code == 409
    publish.assert_called_once()
    assert md.budget_status(db, config)["used"] == 0
    assert client.get(f"/instruments/{ISIN}").json()["listing"] is None
    assert md.run_acquisition(db, config, job_id, SearchProvider().factory)["status"] == "succeeded"
    assert client.get(f"/instruments/{ISIN}").json()["listing"] == LISTING
    assert client.post(path, json={"confirmed": True}).status_code == 202
