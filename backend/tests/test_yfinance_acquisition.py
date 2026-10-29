"""Offline Yahoo service contracts: generations, refreshes and price semantics."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pymongo.errors import ConnectionFailure

import market_data as md
from config import Settings, TaskSettings
from main import create_app
from manual_splits import ManualSplitRequest, add_split
from return_statistics import refresh_statistics
from split_adjustments import BASIS, adjusted_rows
from test_market_data import ISIN, LISTING, Provider, bar, clock, db, page


@pytest.fixture
def config():
    return Settings(_env_file=None, market_data_provider="yfinance", jwt_secret="test-secret-" * 5)


class Yahoo(Provider):
    def eod_page(self, *args, **kwargs):
        result = super().eod_page(*args, **kwargs)
        splits = next(self.splits)
        if isinstance(splits, Exception):
            result["raw"] = {"chart": {"error": {"code": "Synthetic action failure"}}}
        else:
            events = {}
            for index, event in enumerate(splits["data"]):
                timestamp = int(datetime.fromisoformat(event["date"]).replace(tzinfo=timezone.utc).timestamp())
                events[str(index)] = {"date": timestamp, "numerator": event["split_factor"], "denominator": "1"}
            result["raw"] = {"chart": {"result": [{"meta": {"symbol": args[0], "exchangeTimezoneName": "UTC"}, "events": {"splits": events}}]}}
        return result

    def factory(self, key, before_request):
        self.before_request = before_request
        return self

    def resolve_listing(self, ticker, isin):
        self.before_request()
        self.calls.append(("search", ticker, isin))
        return LISTING.copy(), {"source": "yahoo_search", "query": ticker, "name": "Test ETF", "data": {"symbol": LISTING["symbol"]}}

    def resolve_symbol(self, symbol):
        self.before_request()
        self.calls.append(("resolve_symbol", symbol))
        if symbol == "UNKNOWN.DE":
            raise md.MarketDataError("unknown_exchange", "The Yahoo exchange is not mapped.")
        return {"symbol": symbol, "exchange": "XETR"}, {"source": "yahoo_chart_metadata", "query": symbol, "exchangeName": "GER", "data": {"symbol": symbol}}


def quote(day="2026-09-15", close="100", adj="90"):
    return {**bar(day, close), "price_currency": "EUR", "price_basis": "provider_split_adjusted", "adj_close": adj}


def run(db, config, rows=None, splits=None, listing=LISTING, source=None):
    job = md.request_acquisition(db, config, ISIN, listing, "update", "user", Mock())
    provider = source or Yahoo([page(rows if rows is not None else [quote()])], splits=splits)
    return md.run_acquisition(db, config, job["id"], provider.factory), provider


def test_every_yahoo_request_is_globally_paced_and_counted(db, config, monkeypatch):
    pacer = Mock()
    monkeypatch.setattr(md, "wait_for_yahoo_request", pacer)
    result, provider = run(db, config, listing=None)
    assert result["status"] == "succeeded"
    calls = db.acquisition_jobs.find_one({"_id": result["id"]})["calls"]
    assert pacer.call_count == calls == len(provider.calls)
    for call in pacer.call_args_list:
        assert call.args[:2] == (db, 0.05)
        assert callable(call.args[2])


@pytest.mark.parametrize("settings_class", [TaskSettings, Settings])
def test_settings_only_allow_yahoo_without_marketstack_fields(settings_class):
    settings = settings_class(_env_file=None, jwt_secret="s" * 48)
    assert settings.market_data_provider == "yfinance"
    assert not any(field.startswith("marketstack_") for field in settings_class.model_fields)
    with pytest.raises(ValidationError):
        settings_class(_env_file=None, jwt_secret="s" * 48, market_data_provider="marketstack")


def test_marketstack_is_retired_not_configured(db, config, monkeypatch):
    from marketstack import MarketstackClient

    retired = config.model_copy(update={"market_data_provider": "marketstack"})
    assert md.configured(retired) is False
    assert md.instrument_detail(db, retired, ISIN)["provider_configured"] is False
    pacer, publish = Mock(), Mock()
    monkeypatch.setattr(md, "wait_for_yahoo_request", pacer)
    with pytest.raises(md.DataError, match="retired") as error:
        md.request_acquisition(db, retired, ISIN, LISTING, "update", "user", publish)
    assert error.value.status == 422
    publish.assert_not_called()
    pacer.assert_not_called()
    assert db.acquisition_jobs.count_documents({}) == 0
    assert db.marketstack_usage.count_documents({}) == 0
    with pytest.raises(md.MarketDataError, match="retired") as error:
        MarketstackClient()
    assert error.value.code == "provider_retired"


@pytest.mark.parametrize("stored_provider", [None, "marketstack"])
def test_retired_job_deliveries_do_not_construct_provider_or_use_yahoo_pacing(db, config, monkeypatch, stored_provider):
    job = {"_id": "retired-job", "instrument_id": ISIN, "active": True, "status": "queued"}
    if stored_provider is not None:
        job["provider"] = stored_provider
    db.acquisition_jobs.insert_one(job)
    factory, pacer = Mock(side_effect=AssertionError("Retired provider must not run")), Mock()
    monkeypatch.setattr(md, "wait_for_yahoo_request", pacer)
    result = md.run_acquisition(db, config, job["_id"], factory)
    assert result["status"] == result["phase"] == "retired"
    assert "retired" in result["error"]
    assert db.acquisition_jobs.find_one({"_id": job["_id"]})["active"] is False
    factory.assert_not_called()
    pacer.assert_not_called()
    assert db.marketstack_usage.count_documents({}) == 0
    assert db.market_data_publications.count_documents({}) == 0


def test_default_free_provider_requires_no_key_or_monthly_budget(db, config):
    default = Settings(_env_file=None, jwt_secret="s" * 48)
    assert default.market_data_provider == "yfinance"
    budget = md.budget_status(db, config)
    assert budget["metered"] is False and budget["remaining"] is None
    detail = md.instrument_detail(db, config, ISIN)
    assert detail["provider"] == "yfinance" and detail["provider_configured"] is True
    assert detail["budget"] is None
    result, provider = run(db, config, listing=None)
    assert result["status"] == "succeeded"
    assert provider.calls[0] == ("search", "SXR8", ISIN)
    assert db.marketstack_usage.count_documents({}) == 0
    assert db.instrument_state.find_one({"_id": ISIN})["listing_resolution"]["source"] == "yahoo_search"


def test_symbol_only_manual_override_resolves_mic_in_worker_and_caches_after_success(db, config):
    job = md.request_acquisition(db, config, ISIN, {"symbol": "SPYI.DE"}, "update", "user", Mock())
    assert job["status"] == "queued"
    assert db.instrument_state.find_one({"_id": ISIN}) is None
    source = Yahoo([page([quote()])])
    result = md.run_acquisition(db, config, job["id"], source.factory)
    assert result["status"] == "succeeded"
    assert source.calls[0] == ("resolve_symbol", "SPYI.DE")
    assert not any(call[0] == "search" for call in source.calls)
    state = db.instrument_state.find_one({"_id": ISIN})
    assert state["listing"] == {"symbol": "SPYI.DE", "exchange": "XETR"}
    assert state["listing_resolution"]["source"] == "yahoo_chart_metadata"
    assert db.acquisition_jobs.find_one({"_id": job["id"]})["requested_listing"] == {"symbol": "SPYI.DE"}


def test_symbol_only_api_accepts_yfinance_without_http_in_request(db, config, monkeypatch):
    config.mongodb_database = db.name
    monkeypatch.setattr("main.enqueue_etf_import", Mock())
    publish = Mock()
    monkeypatch.setattr("instrument_routes.enqueue_acquisition", publish)
    with TestClient(create_app(config, client_factory=lambda *a, **kw: db.client)) as client:
        client.post("/auth/login", json={"login": "admin", "password": "admin"})
        path = f"/instruments/{ISIN}/acquisitions"
        assert client.post(path, json={"confirmed": True, "exchange": "XETR"}).status_code == 422
        response = client.post(path, json={"confirmed": True, "symbol": "spyi.de"})
        assert response.status_code == 202
        job_id = response.json()["job"]["id"]
    publish.assert_called_once()
    saved = db.acquisition_jobs.find_one({"_id": job_id})
    assert saved["requested_listing"] == {"symbol": "SPYI.DE"}
    assert saved["listing"] is None
    assert db.instrument_state.find_one({"_id": ISIN}) is None


def test_symbol_only_active_job_is_idempotent_after_resolution(db, config, clock, monkeypatch):
    job = md.request_acquisition(db, config, ISIN, {"symbol": "SPYI.DE"}, "update", "user", Mock())
    original = db.raw_eod_pages.find_one
    interrupted = [False]

    def fail_after_resolution(*args, **kwargs):
        if not interrupted[0]:
            interrupted[0] = True
            raise ConnectionFailure("restart after symbol resolution")
        return original(*args, **kwargs)

    monkeypatch.setattr(db.raw_eod_pages, "find_one", fail_after_resolution)
    source = Yahoo([page([quote()])])
    with pytest.raises(ConnectionFailure):
        md.run_acquisition(db, config, job["id"], source.factory)
    assert db.acquisition_jobs.find_one({"_id": job["id"]})["listing"] == {"symbol": "SPYI.DE", "exchange": "XETR"}
    assert md.request_acquisition(db, config, ISIN, {"symbol": "SPYI.DE"}, "update", "user", Mock())["id"] == job["id"]
    clock[0] += timedelta(seconds=md.LEASE_SECONDS + 1)
    fresh = Yahoo([page([quote()])])
    fresh.resolve_symbol = Mock(side_effect=AssertionError("resolved listing should be durable"))
    result = md.run_acquisition(db, config, job["id"], fresh.factory)
    assert result["status"] == "succeeded"
    fresh.resolve_symbol.assert_not_called()


def test_symbol_only_failure_does_not_replace_saved_listing_before_quotes(db, config):
    db.instrument_state.insert_one({"_id": ISIN, "listing": LISTING, "listing_resolution": {"source": "old"}})
    job = md.request_acquisition(db, config, ISIN, {"symbol": "SPYI.DE"}, "update", "user", Mock())
    result = md.run_acquisition(db, config, job["id"], Yahoo([md.MarketDataError("http_error", "failed")]).factory)
    assert result["status"] == "failed"
    assert db.instrument_state.find_one({"_id": ISIN}) == {"_id": ISIN, "listing": LISTING, "listing_resolution": {"source": "old"}}


def test_symbol_only_rejects_mismatch_unknown_exchange_and_quote_lock(db, config):
    class Mismatch(Yahoo):
        def resolve_symbol(self, symbol):
            self.before_request()
            self.calls.append(("resolve_symbol", symbol))
            return {"symbol": "OTHER.DE", "exchange": "XETR"}, {"source": "yahoo_chart_metadata"}

    job = md.request_acquisition(db, config, ISIN, {"symbol": "SPYI.DE"}, "update", "user", Mock())
    assert md.run_acquisition(db, config, job["id"], Mismatch().factory)["status"] == "failed"
    assert db.acquisition_jobs.find_one({"_id": job["id"]})["error_code"] == "identity_mismatch"
    assert db.instrument_state.find_one({"_id": ISIN}) is None
    unknown = md.request_acquisition(db, config, ISIN, {"symbol": "UNKNOWN.DE"}, "update", "user", Mock())
    assert md.run_acquisition(db, config, unknown["id"], Yahoo().factory)["status"] == "failed"
    assert db.acquisition_jobs.find_one({"_id": unknown["id"]})["error_code"] == "unknown_exchange"
    acquired, _ = run(db, config)
    assert acquired["status"] == "succeeded"
    with pytest.raises(md.DataError, match="cannot be changed"):
        md.request_acquisition(db, config, ISIN, {"symbol": "SPYI.DE"}, "update", "user", Mock())


@pytest.mark.parametrize("symbol", ["SPYI.DE", LISTING["symbol"]])
@pytest.mark.parametrize("newer_first", [True, False])
def test_stale_listing_cache_write_cannot_replace_a_newer_job(db, config, clock, monkeypatch, symbol, newer_first):
    job = md.request_acquisition(db, config, ISIN, {"symbol": LISTING["symbol"]}, "update", "user", Mock())
    collection = db.instrument_state
    original_update = type(collection).update_one
    replacement = {"symbol": symbol, "exchange": "XETR"}
    takeover = []

    def paused(self, query, update, **kwargs):
        if self.full_name == collection.full_name and update.get("$set", {}).get("listing") == LISTING and not takeover:
            takeover.append(True)
            # Publication locks the listing even before its cache write lands.
            clock[0] += timedelta(seconds=md.LEASE_SECONDS + 1)
            if replacement != LISTING:
                with pytest.raises(md.DataError) as error:
                    md.request_acquisition(db, config, ISIN, replacement, "update", "user", Mock())
                assert error.value.status == 409
                return original_update(self, query, update, **kwargs)
            next_job = md.request_acquisition(db, config, ISIN, replacement, "update", "user", Mock())
            takeover.append(next_job["id"])
            if newer_first:
                assert md.run_acquisition(db, config, next_job["id"], Yahoo().factory)["status"] == "succeeded"
            else:
                # The old write can also land after the new request has queued
                # but before it caches. The newer worker must still make progress.
                result = original_update(self, query, update, **kwargs)
                assert md.run_acquisition(db, config, next_job["id"], Yahoo().factory)["status"] == "succeeded"
                return result
        return original_update(self, query, update, **kwargs)

    monkeypatch.setattr(type(collection), "update_one", paused)
    md.run_acquisition(db, config, job["id"], Yahoo().factory)
    assert takeover
    cached = db.instrument_state.find_one({"_id": ISIN})
    if replacement != LISTING:
        assert len(takeover) == 1
        assert db.acquisition_jobs.count_documents({"provider": "yfinance"}) == 1
        assert cached["listing"] == LISTING
        assert cached["listing_job_id"] == job["id"]
        assert cached["last_complete_quote_date"] is None
        assert db.acquisition_jobs.find_one({"_id": job["id"]})["status"] == "failed"
    else:
        assert cached["listing"] == replacement
        assert cached["listing_job_id"] == takeover[1]
        assert cached["last_complete_quote_date"] == "2026-09-15"
    assert md.read_quotes(db, ISIN)["total"] == 1


def test_cache_redelivery_does_not_reset_replacement_workers_watermark(db, config, clock, monkeypatch):
    job = md.request_acquisition(db, config, ISIN, {"symbol": LISTING["symbol"]}, "update", "user", Mock())
    collection = db.instrument_state
    original_update = type(collection).update_one
    takeover = []

    def paused(self, query, update, **kwargs):
        if self.full_name == collection.full_name and update.get("$set", {}).get("listing") == LISTING and not takeover:
            takeover.append(True)
            clock[0] += timedelta(seconds=md.LEASE_SECONDS + 1)
            assert md.run_acquisition(db, config, job["id"], Yahoo().factory)["status"] == "succeeded"
        return original_update(self, query, update, **kwargs)

    monkeypatch.setattr(type(collection), "update_one", paused)
    assert md.run_acquisition(db, config, job["id"], Yahoo().factory)["status"] == "succeeded"
    assert takeover
    assert db.instrument_state.find_one({"_id": ISIN})["last_complete_quote_date"] == "2026-09-15"


def test_every_update_requests_full_history_and_publishes_one_generation(db, config, clock):
    first, _ = run(db, config, [quote("2026-09-14", "100"), quote("2026-09-15", "110")])
    cutoff = clock[0]
    clock[0] += timedelta(seconds=2)
    second, source = run(db, config, [quote("2026-09-15", "55")])
    assert second["mode"] == "refresh"
    assert next(call for call in source.calls if call[0] == "eod")[3:5] == ("2010-05-19", "2026-09-17")
    latest = md.read_quotes(db, ISIN)
    assert [(row["date"], row["close"]) for row in latest["items"]] == [("2026-09-15", "55")]
    historical = md.read_quotes(db, ISIN, as_of=cutoff)
    assert historical["generation"] == latest["generation"] == second["id"]
    assert [row["close"] for row in historical["items"]] == ["55"]
    with pytest.raises(md.DataError) as error:
        md.read_quotes(db, ISIN, as_of=cutoff, generation=first["id"])
    assert error.value.status == 409
    # Publication changes visibility; the scheduled retention task deletes payloads separately.
    assert db.quotes.count_documents({}) == 3
    assert db.market_data_publications.count_documents({}) == 2
    assert md.published_history(db, ISIN, LISTING)["_id"] == second["id"]
    assert md.published_history(db, ISIN, LISTING, cutoff)["_id"] == second["id"]


def test_historical_as_of_filters_quote_dates_from_the_latest_generation(db, config, clock):
    cutoff = datetime(2026, 9, 14, 23, 59, tzinfo=timezone.utc)
    run(db, config, [quote("2026-09-14", "100"), quote("2026-09-15", "110")])
    clock[0] += timedelta(seconds=2)
    latest, _ = run(db, config, [quote("2026-09-14", "50"), quote("2026-09-15", "55")])
    historical = md.read_quotes(db, ISIN, as_of=cutoff)
    assert historical["generation"] == latest["id"]
    assert [(row["date"], row["close"]) for row in historical["items"]] == [("2026-09-14", "50")]
    assert md.quote_dates(db, ISIN, LISTING, as_of=cutoff) == ["2026-09-14"]
    detail = md.instrument_detail(db, config, ISIN, as_of=cutoff)
    assert detail["generation"] == latest["id"]
    assert detail["summary"]["quote_count"] == 1
    assert detail["summary"]["last_date"] == "2026-09-14"


def test_failed_refresh_preserves_latest_published_prices_profile_statistics(db, config, clock):
    first, _ = run(db, config, [quote("2026-09-14"), quote("2026-09-15")])
    clock[0] += timedelta(seconds=2)
    latest, _ = run(db, config, source=Yahoo(
        [page([quote("2026-09-14", "200"), quote("2026-09-15", "200")])],
        profile={"name": "Latest published profile"},
    ))
    before = md.instrument_detail(db, config, ISIN)
    clock[0] += timedelta(seconds=2)
    source = Yahoo([page([quote("2026-09-15", "999")])], profile={"name": "Unpublished profile"},
                   splits=[md.MarketDataError("rate_limited", "Yahoo rate limited the request.")])
    failed, _ = run(db, config, source=source)
    assert failed["status"] == "failed"
    detail = md.instrument_detail(db, config, ISIN)
    assert detail["generation"] == before["generation"] == latest["id"]
    assert detail["profile"] == before["profile"]
    assert [row["close"] for row in md.read_quotes(db, ISIN)["items"]] == ["200", "200"]
    assert md.acquired_instruments(db)["items"][0]["quote_count"] == 2
    stats, _ = refresh_statistics(db, ISIN)
    assert stats["daily_mean_log_return"] == 0
    assert db.instrument_statistics.find_one({"_id": ISIN})["signature"]["generation"] == latest["id"]
    assert md.published_history(db, ISIN, LISTING)["_id"] == latest["id"]
    with pytest.raises(md.DataError) as error:
        md.read_quotes(db, ISIN, generation=first["id"])
    assert error.value.status == 409
    assert db.quotes.count_documents({"job_id": failed["id"]}) == 1
    assert db.quotes.count_documents({"job_id": latest["id"]}) == 2


def test_initial_failed_or_empty_history_is_not_acquired_inventory(db, config):
    failed, _ = run(db, config, source=Yahoo([page([quote()])], splits=[md.MarketDataError("rate_limited", "Stopped.")]))
    assert failed["status"] == "failed"
    assert md.acquired_instruments(db)["total"] == 0
    assert md.read_quotes(db, ISIN)["total"] == 0
    empty, _ = run(db, config, [])
    assert empty["status"] == "failed"
    assert md.acquired_instruments(db)["total"] == 0


def test_split_adjusted_close_is_not_adjusted_again_or_replaced_with_adj_close(db, config):
    rows = [quote("2026-09-14", "100", "80"), quote("2026-09-15", "100", "90")]
    split = {"symbol": LISTING["symbol"], "date": "2026-09-15", "split_factor": "25"}
    result, _ = run(db, config, rows, splits=[page([split])])
    assert result["status"] == "succeeded"
    stats, _ = refresh_statistics(db, ISIN)
    assert stats["daily_mean_log_return"] == 0
    assert stats["price_basis"] == BASIS
    assert any("already split-adjusted" in message for message in stats["warnings"])
    # Legacy corrections are not applied to this provider's adjusted prices.
    db.manual_splits.insert_one({"_id": "legacy", "instrument_id": ISIN, **LISTING, "date": "2026-09-15",
                                "factor": "25", "created_at": md.utcnow(), "active": True})
    assert refresh_statistics(db, ISIN, force=True)[0]["daily_mean_log_return"] == 0
    with pytest.raises(md.DataError, match="already split-adjusted"):
        add_split(db, ISIN, ManualSplitRequest(id=uuid4(), **LISTING, date="2026-09-15", factor="25"), "user")


def test_invalid_close_never_falls_back_to_dividend_adjusted_price():
    rows, warnings = adjusted_rows([quote(close=None, adj="100")], None)
    assert rows[0][BASIS] is None
    mixed, _ = adjusted_rows([quote(), bar("2026-09-16")], None)
    assert all(row[BASIS] is None for row in mixed)


def test_generation_change_invalidates_statistics_even_with_equal_row_counts(db, config, clock):
    first, _ = run(db, config, [quote("2026-09-14", "100"), quote("2026-09-15", "100")])
    old = refresh_statistics(db, ISIN)[0]
    clock[0] += timedelta(seconds=2)
    second, _ = run(db, config, [quote("2026-09-14", "100"), quote("2026-09-15", "110")])
    new = refresh_statistics(db, ISIN)[0]
    assert new["daily_mean_log_return"] > old["daily_mean_log_return"]
    assert db.instrument_statistics.find_one({"_id": ISIN})["signature"]["generation"] == second["id"]


def test_legacy_rows_never_fill_holes_in_a_yahoo_generation(db, config):
    db.quotes.insert_one({"instrument_id": ISIN, **LISTING, "provider": "marketstack", "job_id": "legacy",
                          "date": "2015-01-05", "data": bar("2015-01-05"), "collected_at": md.utcnow()})
    result, _ = run(db, config)
    assert result["status"] == "succeeded"
    assert md.quote_dates(db, ISIN, LISTING) == ["2026-09-15"]


def test_per_job_limit_stops_yahoo_without_spending_legacy_credits(db, config):
    config.market_data_max_calls_per_job = 1
    result, source = run(db, config)
    assert result["status"] == "failed"
    assert len(source.calls) == 1
    assert db.marketstack_usage.count_documents({}) == 0
    assert db.market_data_publications.count_documents({}) == 0


def test_database_retry_reuses_raw_pages_and_publication(db, config, clock, monkeypatch):
    publish = db.market_data_publications.update_one
    failed_once = [False]

    def lose_ack(*args, **kwargs):
        result = publish(*args, **kwargs)
        if not failed_once[0]:
            failed_once[0] = True
            raise ConnectionFailure("lost publication acknowledgement")
        return result

    monkeypatch.setattr(db.market_data_publications, "update_one", lose_ack)
    job = md.request_acquisition(db, config, ISIN, LISTING, "update", "user", Mock())
    source = Yahoo([page([quote()])])
    with pytest.raises(ConnectionFailure):
        md.run_acquisition(db, config, job["id"], source.factory)
    count = len(source.calls)
    clock[0] += timedelta(seconds=md.LEASE_SECONDS + 1)
    result = md.run_acquisition(db, config, job["id"], source.factory)
    assert result["status"] == "succeeded"
    assert len(source.calls) == count
    assert db.market_data_publications.count_documents({}) == 1
    assert md.read_quotes(db, ISIN)["total"] == 1


def test_published_calibration_and_portfolio_queries_use_same_generation(db, config, clock):
    from goal_calibration import CalibrationInputs, calibrate_instrument
    from portfolio_tracker import acquired_instrument, quote_rows
    run(db, config, [quote("2026-09-14"), quote("2026-09-15")])
    clock[0] += timedelta(seconds=2)
    run(db, config, [quote("2026-09-15", "200")])
    instrument = acquired_instrument(db, ISIN, md.utcnow())
    assert [row["close"] for row in quote_rows(db, instrument, md.utcnow())] == ["200"]
    captured = []

    def evaluate(rows, body, **kwargs):
        captured.extend(rows)
        return {"warnings": []}

    result = calibrate_instrument(db, ISIN, CalibrationInputs(goal_eur="1000", starting_capital_eur="0", monthly_contribution_eur="10", seed=1), evaluate=evaluate)
    assert [row[BASIS] for row in captured] == ["200"]
    assert len(result["history_fingerprint"]) == 64


def test_expired_worker_cannot_publish_over_a_new_success(db, config, clock, monkeypatch):
    original = db.market_data_publications.update_one
    replacement = []

    def interrupt_publication(*args, **kwargs):
        if not replacement:
            replacement.append(None)
            clock[0] += timedelta(seconds=md.LEASE_SECONDS + 1)
            md.expire_stalled_jobs(db, ISIN)
            fresh, _ = run(db, config, [quote(close="200")])
            replacement[0] = fresh
        return original(*args, **kwargs)

    monkeypatch.setattr(db.market_data_publications, "update_one", interrupt_publication)
    expired, _ = run(db, config, [quote(close="100")])
    assert expired["status"] == "failed"
    assert replacement[0]["status"] == "succeeded"
    assert md.published_history(db, ISIN, LISTING)["_id"] == replacement[0]["id"]
    assert md.read_quotes(db, ISIN)["items"][0]["close"] == "200"
    assert db.acquisition_jobs.find_one({"_id": expired["id"]}).get("published") is not True
    assert md.acquired_instruments(db)["items"][0]["quote_count"] == 1


def test_delayed_publication_invalidates_superseded_and_legacy_generation_cursors(db, config, clock, monkeypatch):
    first, _ = run(db, config, [quote("2026-09-14", "100"), quote("2026-09-15", "110")])
    clock[0] += timedelta(seconds=1)
    original = db.market_data_publications.update_one
    pages = []

    def delayed_publication(*args, **kwargs):
        clock[0] += timedelta(seconds=1)
        pages.append(md.read_quotes(db, ISIN, limit=1))
        return original(*args, **kwargs)

    monkeypatch.setattr(db.market_data_publications, "update_one", delayed_publication)
    second, _ = run(db, config, [quote("2026-09-14", "50"), quote("2026-09-15", "55")])
    first_page = pages[0]
    cutoff = datetime.fromisoformat(first_page["as_of"].replace("Z", "+00:00"))
    assert first_page["generation"] == first["id"]
    assert first_page["items"][0]["close"] == "100"
    for generation in (first_page["generation"], "legacy", str(uuid4())):
        with pytest.raises(md.DataError) as error:
            md.read_quotes(db, ISIN, limit=1, offset=1, as_of=cutoff, generation=generation)
        assert error.value.status == 409
    second_page = md.read_quotes(db, ISIN, limit=1, offset=1, as_of=cutoff, generation=second["id"])
    assert second_page["generation"] == second["id"]
    assert second_page["items"][0]["close"] == "55"
    assert md.read_quotes(db, ISIN)["generation"] == second["id"]


def test_fresh_worker_replays_split_actions_from_persisted_chart_without_http(db, config, clock, monkeypatch):
    original = db.raw_split_pages.find_one
    failed = [False]

    def database_interrupt(*args, **kwargs):
        if not failed[0]:
            failed[0] = True
            raise ConnectionFailure("worker restarted after durable chart")
        return original(*args, **kwargs)

    monkeypatch.setattr(db.raw_split_pages, "find_one", database_interrupt)
    job = md.request_acquisition(db, config, ISIN, LISTING, "update", "user", Mock())
    source = Yahoo([page([quote()])], splits=[page([{"date": "2026-09-15", "split_factor": "4"}])])
    with pytest.raises(ConnectionFailure):
        md.run_acquisition(db, config, job["id"], source.factory)
    # A new process/client has no old instance cache. Any provider method would
    # now return newer factor=8 evidence, so forbid ALL provider operations.
    fresh = Yahoo()
    fresh.eod_page = fresh.splits_page = fresh.tickerinfo = Mock(side_effect=AssertionError("Unexpected HTTP on DB replay"))
    clock[0] += timedelta(seconds=md.LEASE_SECONDS + 1)
    result = md.run_acquisition(db, config, job["id"], fresh.factory)
    assert result["status"] == "succeeded"
    assert db.split_snapshots.find_one({"_id": job["id"]})["events"] == [{"date": "2026-09-15", "factor": "4"}]
    fresh.eod_page.assert_not_called()
