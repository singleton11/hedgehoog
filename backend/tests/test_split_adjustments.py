from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import math
from statistics import variance
from unittest.mock import Mock

import pytest
from pymongo.errors import ConnectionFailure

import market_data as md
from market_provider import MarketDataError
from yahoo_finance import YahooFinanceClient
from return_statistics import calculate_statistics, instrument_statistics
from split_adjustments import BASIS, adjusted_rows, load_split_snapshot, split_events
from test_goal_planner import history, request_body, login
from test_market_data import ISIN, LISTING, NOW, Provider, api, bar, clock, collect, config, db, enqueue, page, seed_yahoo
from test_yahoo_finance import END, START, SYMBOL, no_live_http, transport


def event(day="2026-09-15", factor="25", **changes):
    return {"date": day, "split_factor": factor, "symbol": LISTING["symbol"], **changes}


def snapshot(events, **changes):
    return {"_id": "test", "date_from": "2000-01-01", "date_to": "2026-09-16",
            "events": [{"date": day, "factor": factor} for day, factor in events], **changes}


def test_client_split_chart_exact_values_and_per_http_reservation(transport):
    state, calls = transport
    reserved = Mock()
    event = next(iter(state["history"]["chart"]["result"][0]["events"]["splits"].values()))
    event.update(numerator="25.000000000000000001", denominator=1)
    with YahooFinanceClient(before_request=reserved) as client:
        result = client.splits_page(SYMBOL, START, END)
        assert client.splits_page(SYMBOL, START, END) == result
    assert result["data"][0]["split_factor"] == "25.000000000000000001"
    assert len(calls) == reserved.call_count == 4
    assert "/chart/" in calls[-1][1]
    assert calls[-1][2]["params"]["events"] == "div,splits,capitalGains"
    assert not any("/splits" in call[1] for call in calls)


@pytest.mark.parametrize("args", [("", "2020-01-01", "2026-01-01", 0),
    ("SPYI.F", "bad", "2026-01-01", 0), ("SPYI.F", "2026-01-02", "2026-01-01", 0),
    ("SPYI.F", "2020-01-01", "2026-01-01", -1)])
def test_client_rejects_bad_requests_without_quota(args):
    reserved = Mock()
    with YahooFinanceClient(before_request=reserved) as client, pytest.raises(MarketDataError):
        client.splits_page(*args)
    reserved.assert_not_called()


@pytest.mark.parametrize("factor,raw", [("25", [250, 10, 10]), ("0.5", [10, 20, 20])])
@pytest.mark.parametrize("adjusted", [None, "10", "9999"])
def test_forward_and_reverse_split_never_double_adjust_or_mutate(factor, raw, adjusted):
    rows = [{"date": f"2026-09-{day}", "close": str(price), "adj_close": adjusted}
            for day, price in zip([14, 15, 16], raw)]
    original = deepcopy(rows)
    corrected, warnings = adjusted_rows(rows, snapshot([("2026-09-15", factor)]))
    stats = calculate_statistics(corrected)
    assert rows == original
    assert stats["price_basis"] == BASIS and stats["return_count"] == 2
    assert stats["daily_mean_log_return"] == stats["daily_volatility"] == 0
    assert not any("greater than 50%" in w for w in stats["warnings"])
    assert "cash distributions" in warnings[0]


def test_multiple_splits_missing_event_candle_and_future_prefix_independence():
    rows = [{"date": "2026-09-11", "close": "250"}, {"date": "2026-09-14", "close": "5"},
            {"date": "2026-09-15", "close": "10"}]
    actions = [("2026-09-12", "25"), ("2026-09-13", "2"), ("2026-09-15", "0.5")]
    corrected, _ = adjusted_rows(rows, snapshot(actions))
    assert [Decimal(row[BASIS]) for row in corrected] == [250, 250, 250]
    prefix, _ = adjusted_rows(rows[:2], snapshot(actions[:2]))
    assert corrected[:2] == prefix  # No future-action floating point leakage.
    assert calculate_statistics(corrected)["daily_volatility"] == 0


def test_trailing_year_return_uses_split_correction():
    rows = [{"date": "2025-09-16", "close": "250", "adj_close": "250"},
            {"date": "2026-09-16", "close": "11", "adj_close": "11"}]
    corrected, _ = adjusted_rows(rows, snapshot([("2026-02-23", "25")]))
    stats = calculate_statistics(corrected)
    assert stats["historical_year_price_basis"] == BASIS
    assert stats["historical_year_return"] == pytest.approx(0.1)


@pytest.mark.parametrize("factor,post_split", [("25", "10"), ("0.5", "500")])
def test_trailing_year_volatility_uses_split_corrected_window(factor, post_split):
    rows = [{"date": day, "close": price, "adj_close": "999"} for day, price in [
        ("2025-09-12", "25"), ("2025-09-15", "500"),  # Volatile older history.
        ("2025-09-16", "250"), ("2026-09-14", "250"),
        ("2026-09-15", post_split), ("2026-09-16", str(Decimal(post_split) * Decimal("1.1"))),
    ]]
    original = deepcopy(rows)
    corrected, _ = adjusted_rows(rows, snapshot([("2026-09-15", factor)]))
    stats = calculate_statistics(corrected)
    assert stats["historical_year_return"] == pytest.approx(0.1)
    assert stats["historical_year_volatility"] == pytest.approx(math.sqrt(252 * variance([0, math.log(1.1)])))
    assert stats["historical_year_volatility"] < stats["annualized_volatility"]
    assert stats["historical_year_volatility_price_basis"] == BASIS
    assert stats["historical_year_volatility_start_date"] == "2025-09-16"
    assert stats["historical_year_volatility_end_date"] == "2026-09-16"
    assert stats["historical_year_volatility_return_count"] == 2
    assert rows == original

    # Incomplete actions must not fall back to the valid provider-adjusted prices.
    unavailable, _ = adjusted_rows(rows, {"error": "Incomplete split history"})
    unavailable_stats = calculate_statistics(unavailable)
    assert all(value is None for key, value in unavailable_stats.items() if key.startswith("historical_year_"))


@pytest.mark.parametrize("invalid", [None, 0, -1, True, "NaN", "Infinity", {}, []])
def test_invalid_raw_close_breaks_corrected_chain_without_adj_fallback(invalid):
    rows = [{"date": f"2026-09-{day}", "close": value, "adj_close": "100"}
            for day, value in zip([14, 15, 16], [100, invalid, 100])]
    corrected, _ = adjusted_rows(rows, snapshot([]))
    stats = calculate_statistics(corrected)
    assert stats["price_basis"] == BASIS and stats["return_count"] == 0
    assert stats["invalid_price_count"] == 1


def test_split_pagination_duplicates_and_raw_quotes_unchanged(db, config):
    rows = [{**bar(day, "10"), "price_basis": "provider_split_adjusted"}
            for day in ("2026-09-14", "2026-09-15", "2026-09-16")]
    splits = [page([event(), event(factor="25.0")])]
    result, source = collect(db, config, Provider([page(rows)], splits=splits))
    assert result["status"] == "succeeded"
    assert result["calls"] == 2 and md.budget_status(db, config)["used"] == 0
    assert [call[0] for call in source.calls] == ["tickerinfo", "eod"]
    assert db.split_snapshots.find_one()["events"] == [{"date": "2026-09-15", "factor": "25.0"}]
    assert [quote["data"] for quote in db.quotes.find().sort("date", 1)] == [
        {**row, "price_basis": "provider_split_adjusted"} for row in rows]
    assert [item["close"] for item in md.read_quotes(db, ISIN)["items"]] == ["10", "10", "10"]
    assert all(BASIS not in item for item in md.read_quotes(db, ISIN)["items"])
    assert instrument_statistics(db, ISIN)["daily_volatility"] == 0
    saved = list(db.raw_split_pages.find())
    md.run_acquisition(db, config, result["id"], Mock(side_effect=AssertionError("No redelivery requests")))
    assert list(db.raw_split_pages.find()) == saved


@pytest.mark.parametrize("split_rows", [[event(factor=0)], [event(factor=True)], [event(factor="NaN")],
    [event(factor=None)], [event(symbol="SPYI")], [event(exchange="XLON")], [event(day="bad")],
    [event(day="2030-01-01")], [None], [event(), event(factor="2")]])
def test_invalid_split_evidence_retained_but_never_used(db, config, split_rows):
    # Validate normalized action evidence independently of Yahoo's raw projection,
    # which drops undated events and derives symbol identity from chart metadata.
    raw = {"job_id": "invalid-actions", "data": page(split_rows)}
    db.raw_split_pages.insert_one(deepcopy(raw))
    with pytest.raises(MarketDataError, match="Split history"):
        split_events([raw], LISTING, "2010-05-19", "2026-09-17")
    assert db.raw_split_pages.find_one()["data"]["data"] == split_rows
    assert db.split_snapshots.count_documents({}) == db.quotes.count_documents({}) == 0
    assert db.instrument_state.find_one() is None


@pytest.mark.parametrize("damage", ["provider-error", "missing-chart", "missing-result", "wrong-symbol"])
def test_incomplete_chart_action_evidence_does_not_publish_or_advance(db, config, damage):
    raw = {"chart": {"result": [{"meta": {"symbol": LISTING["symbol"], "exchangeTimezoneName": "UTC"}}]}}
    if damage == "provider-error":
        raw["chart"] = {"error": {"code": "Too Many Requests"}, "result": None}
    elif damage == "missing-chart":
        raw = {}
    elif damage == "missing-result":
        raw["chart"].pop("result")
    else:
        raw["chart"]["result"][0]["meta"]["symbol"] = "OTHER"
    result, _ = collect(db, config, Provider([{**page(), "raw": raw}]))
    assert result["status"] == "failed"
    assert db.split_snapshots.count_documents({}) == 0
    assert db.instrument_state.find_one() is None
    assert md.read_quotes(db, ISIN)["items"] == []


def test_saved_split_page_replays_after_database_disconnect(db, config, clock, monkeypatch):
    source = Provider(splits=[page([event()])])
    job = enqueue(db, config)
    real = db.raw_split_pages.find_one_and_update

    def interrupted(*args, **kwargs):
        real(*args, **kwargs)
        raise ConnectionFailure("committed")

    monkeypatch.setattr(db.raw_split_pages, "find_one_and_update", interrupted)
    with pytest.raises(ConnectionFailure):
        md.run_acquisition(db, config, job["id"], source.factory)
    monkeypatch.setattr(db.raw_split_pages, "find_one_and_update", real)
    clock[0] += timedelta(seconds=md.LEASE_SECONDS + 1)
    assert md.run_acquisition(db, config, job["id"], source.factory)["status"] == "succeeded"
    assert len(source.calls) == 2  # Replay chart actions without any split HTTP request.
    assert db.raw_split_pages.count_documents({}) == db.split_snapshots.count_documents({}) == 1


def test_no_new_quote_dates_refreshes_close_vintage_and_actions_without_double_adjustment(db, config, clock):
    rows = [{**bar(day, close), "price_basis": "provider_split_adjusted"} for day, close in
            [("2026-09-14", "250"), ("2026-09-15", "10"), ("2026-09-16", "10")]]
    first, _ = collect(db, config, Provider([page(rows)], splits=[page([])]))
    before = instrument_statistics(db, ISIN)
    saved = list(db.quotes.find({"job_id": first["id"]}))
    corrected = [{**row, "close": "10"} for row in rows]
    clock[0] += timedelta(minutes=1)
    result, source = collect(db, config, Provider([page(corrected)], splits=[page([event()])]))
    assert result["status"] == "succeeded"
    assert [call[0] for call in source.calls] == ["tickerinfo", "eod"]
    assert source.calls[-1][3:5] == ("2010-05-19", "2026-09-17")
    after = instrument_statistics(db, ISIN)
    assert before["daily_volatility"] > 0 and after["daily_volatility"] == 0
    assert list(db.quotes.find({"job_id": first["id"]})) == saved
    assert md.read_quotes(db, ISIN)["total"] == 3
    # A later action retraction changes evidence, not already split-adjusted Close.
    clock[0] += timedelta(minutes=1)
    final, _ = collect(db, config, Provider([page(corrected)], splits=[page([])]))
    assert final["status"] == "succeeded"
    assert db.split_snapshots.find_one({"_id": final["id"]})["events"] == []
    assert instrument_statistics(db, ISIN)["daily_volatility"] == 0


def test_lazy_cache_invalidation_requires_coherent_close_and_action_revision(db, config, clock):
    rows = [{**bar(day, close), "price_basis": "provider_split_adjusted"} for day, close in [
        ("2025-09-16", "250"), ("2026-09-14", "250"), ("2026-09-15", "10"), ("2026-09-16", "10")]]
    result, source = collect(db, config, Provider([page(rows)]))
    before = instrument_statistics(db, ISIN)
    original = list(db.quotes.find({"job_id": result["id"]}))
    clock[0] += timedelta(seconds=1)
    seed_yahoo(db, generation="correction", collected_at=clock[0],
               rows=[{**row, "close": "10"} for row in rows])
    db.split_snapshots.update_one({"_id": "correction"}, {"$set": {
        "events": [{"date": "2026-09-15", "factor": "25"}]}})
    after = instrument_statistics(db, ISIN)
    assert before["daily_volatility"] > 0 and after["daily_volatility"] == 0
    assert before["historical_year_volatility"] > 0 and after["historical_year_volatility"] == 0
    assert after["historical_year_volatility_price_basis"] == BASIS
    assert before["historical_year_return"] == pytest.approx(-0.96)
    assert after["historical_year_return"] == 0
    assert before["quote_count"] == after["quote_count"] == 4
    assert list(db.quotes.find({"job_id": result["id"]})) == original
    with pytest.raises(md.DataError, match="superseded"):
        instrument_statistics(db, ISIN, generation=result["id"])
    item = md.acquired_instruments(db)["items"][0]
    for key, value in after.items():
        if key.startswith("historical_year_"):
            assert item[key] == value
    assert len(source.calls) == 2


def test_version_six_split_cache_refreshes_trailing_year_fields_without_provider_calls(db, config, monkeypatch):
    rows = [bar("2025-09-16", "250"), bar("2026-09-14", "250"), bar("2026-09-15", "10"), bar("2026-09-16", "10")]
    collect(db, config, Provider([page(rows)], splits=[page([event()])]))
    expected = instrument_statistics(db, ISIN)
    cached = db.instrument_statistics.find_one({"_id": ISIN})
    # Both merge parents used version 6; the split branch lacked volatility fields.
    cached["signature"]["version"] = 6
    cached["statistics"] = {key: value for key, value in cached["statistics"].items()
                            if not key.startswith("historical_year_volatility")}
    db.instrument_statistics.replace_one({"_id": ISIN}, cached)
    original_quotes = list(db.quotes.find())
    used = md.budget_status(db, config)["used"]
    provider = Mock(side_effect=AssertionError("Cache refresh must use saved evidence only"))
    monkeypatch.setattr(md, "provider_client", provider)
    assert instrument_statistics(db, ISIN) == expected
    assert db.instrument_statistics.find_one({"_id": ISIN})["signature"]["version"] > 6
    assert list(db.quotes.find()) == original_quotes
    assert md.budget_status(db, config)["used"] == used
    provider.assert_not_called()


def test_failed_action_refresh_preserves_preceding_complete_vintage(db, config, clock):
    rows = [{**bar(day, "10"), "price_basis": "provider_split_adjusted"}
            for day in ("2026-09-14", "2026-09-15")]
    first, _ = collect(db, config, Provider([page(rows)], splits=[page([event()])]))
    before = instrument_statistics(db, ISIN)
    assert before["daily_mean_log_return"] == 0
    clock[0] += timedelta(seconds=1)
    failed, _ = collect(db, config, Provider([MarketDataError("quota_exceeded", "Quota exhausted")]))
    assert failed["status"] == "failed" and db.split_snapshots.count_documents({}) == 1
    stats = instrument_statistics(db, ISIN)
    assert stats["generation"] == first["id"] and stats["daily_mean_log_return"] == 0
    assert not any("incomplete" in warning for warning in stats["warnings"])
    assert all(row["close"] == "10" for row in md.read_quotes(db, ISIN)["items"])


def test_historical_split_snapshot_ignores_job_failure_after_cutoff(db, config, clock):
    collect(db, config, Provider([page(history())], splits=[page([event(factor="2")])]))
    clock[0] += timedelta(minutes=1)
    job = enqueue(db, config)
    db.acquisition_jobs.update_one({"_id": job["id"]}, {"$set": {"calls": 1}})
    cutoff = clock[0] + timedelta(minutes=1)
    clock[0] = cutoff + timedelta(minutes=1)

    before = instrument_statistics(db, ISIN, as_of=cutoff, point_in_time=True)
    assert before["eligible"] and before["price_basis"] == BASIS
    db.acquisition_jobs.update_one({"_id": job["id"]}, {"$set": {
        "status": "failed", "active": False, "finished_at": clock[0],
    }})
    after = instrument_statistics(db, ISIN, as_of=cutoff, point_in_time=True)
    assert after == before

    # Failed attempts never replace the preceding complete Yahoo vintage.
    failed_at_cutoff = instrument_statistics(db, ISIN, as_of=clock[0], point_in_time=True)
    assert failed_at_cutoff["eligible"]
    assert failed_at_cutoff["generation"] == before["generation"]
    assert failed_at_cutoff["daily_mean_log_return"] == before["daily_mean_log_return"]
    assert not any("Split history download is incomplete" in w for w in failed_at_cutoff["warnings"])


def test_uncovered_snapshot_never_falls_back_to_adj_close():
    rows = [{"date": "2026-09-16", "close": "100", "adj_close": "100"}]
    corrected, warnings = adjusted_rows(rows, snapshot([], date_to="2026-09-15"))
    assert corrected[0][BASIS] is None and "does not cover" in warnings[0]
    assert calculate_statistics(corrected)["price_count"] == 0


def test_snapshot_listing_and_knowledge_time_isolation(db, config, clock):
    collect(db, config, Provider(splits=[page([event()])]))
    assert load_split_snapshot(db, ISIN, LISTING, NOW)["events"]
    assert load_split_snapshot(db, ISIN, LISTING, NOW - timedelta(seconds=1))["events"]
    # Collection/publication time is not a strict historical-origin cutoff.
    with pytest.raises(md.DataError, match="latest Yahoo listing changed") as error:
        load_split_snapshot(db, ISIN, {**LISTING, "exchange": "XLON"}, NOW)
    assert error.value.status == 409


def test_monte_carlo_uses_corrected_estimates_from_explicit_acquisition(api, db, config):
    rows = history()
    for i, row in enumerate(rows):
        # Leave provider adj_close deliberately stale/inconsistent.
        row.update(close="10", adj_close="250" if i < 40 else "999",
                   price_basis="provider_split_adjusted")
    split = event(day=rows[40]["date"][:10])
    collect(db, config, Provider([page(rows)], splits=[page([split])]))
    client, _ = api
    login(client)
    response = client.post("/goals/simulate", json=request_body())
    assert response.status_code == 200, response.text
    stats = response.json()["parameter_statistics"]["instrument"]
    assert stats["price_basis"] == BASIS
    assert stats["daily_mean_log_return"] == stats["daily_volatility"] == 0


def test_backfill_still_downloads_splits_through_present(db, config, clock):
    collect(db, config)
    clock[0] += timedelta(minutes=1)
    job = enqueue(db, config, mode="backfill")
    source = Provider([page([bar("2026-09-14")])], splits=[page([event()])])
    assert md.run_acquisition(db, config, job["id"], source.factory)["status"] == "succeeded"
    assert [call[0] for call in source.calls] == ["tickerinfo", "eod"]
    assert source.calls[1][3:5] == ("2010-05-19", "2026-09-17")
    assert db.split_snapshots.find_one({"_id": job["id"]})["date_to"] == "2026-09-17"


@pytest.mark.parametrize("random", [False, True])
def test_calibration_training_and_cash_flows_use_split_series(db, config, random):
    from goal_calibration import calibrate_instrument, evaluate_history, RandomCalibrationInputs
    from random_calibration import evaluate_random_history
    from test_goal_calibration import rows_between, inputs

    rows = rows_between(flat=True)
    for row in rows:
        # Both the training and outcome intervals cross this action.
        row["close"] = "4"
        row["price_basis"] = "provider_split_adjusted"
        row["adj_close"] = "100" if row["date"] < "2024-02-01" else "999"
    collect(db, config, Provider([page(rows)], splits=[page([event("2024-02-01")])]))
    body = (RandomCalibrationInputs(mode="random", scenario_count=12, min_horizon_months=6,
                                   max_horizon_months=6, lookback_months=6, seed=42)
            if random else inputs())
    evaluate = evaluate_random_history if random else evaluate_history
    result = calibrate_instrument(db, ISIN, body, evaluate=lambda *a, **kw: evaluate(*a, **kw, paths=100))
    expected = evaluate(rows_between(flat=True), body, seed=42, paths=100)
    assert result["sample_count"] == expected["sample_count"]
    for actual, reference in zip(result["forecasts"], expected["forecasts"]):
        assert actual["price_basis"] == BASIS
        assert {**actual, "price_basis": "adj_close"} == reference


def test_chart_actions_need_no_extra_request_under_current_job_limit(db, config):
    config.market_data_max_calls_per_job = 2
    result, source = collect(db, config)
    assert result["status"] == "succeeded" and result["calls"] == len(source.calls) == 2
    assert db.raw_split_pages.count_documents({}) == 1
    assert db.marketstack_usage.count_documents({}) == 0
