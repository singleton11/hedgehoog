from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock

import httpx
import pytest
from pymongo.errors import ConnectionFailure

import market_data as md
from marketstack import MarketstackClient, MarketstackError
from return_statistics import calculate_statistics, instrument_statistics
from split_adjustments import BASIS, adjusted_rows, load_split_snapshot
from test_goal_planner import history, request_body, login
from test_market_data import ISIN, LISTING, NOW, Provider, api, bar, clock, collect, config, db, enqueue, page


def event(day="2026-09-15", factor="25", **changes):
    return {"date": day, "split_factor": factor, "symbol": LISTING["symbol"], **changes}


def snapshot(events, **changes):
    return {"_id": "test", "date_from": "2000-01-01", "date_to": "2026-09-16",
            "events": [{"date": day, "factor": factor} for day, factor in events], **changes}


def test_client_split_endpoint_exact_values_and_quota():
    requests, reserved = [], Mock()

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=b'{"data":[{"symbol":"SPYI.F","date":"2026-02-23","split_factor":25.000000000000000001}]}')

    with MarketstackClient("secret", reserved, transport=httpx.MockTransport(respond)) as client:
        result = client.splits_page("SPYI.F", "2011-01-01", "2026-09-16", offset=1000)
    assert result["data"][0]["split_factor"] == "25.000000000000000001"
    assert requests[0].url.path == "/v2/splits"
    assert dict(requests[0].url.params) == {"access_key": "secret", "symbols": "SPYI.F", "date_from": "2011-01-01",
        "date_to": "2026-09-16", "limit": "1000", "sort": "ASC", "offset": "1000"}
    reserved.assert_called_once()


@pytest.mark.parametrize("args", [("", "2020-01-01", "2026-01-01", 0),
    ("SPYI.F", "bad", "2026-01-01", 0), ("SPYI.F", "2026-01-02", "2026-01-01", 0),
    ("SPYI.F", "2020-01-01", "2026-01-01", -1)])
def test_client_rejects_bad_requests_without_quota(args):
    reserved = Mock()
    with MarketstackClient("secret", reserved) as client, pytest.raises(MarketstackError):
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


@pytest.mark.parametrize("invalid", [None, 0, -1, True, "NaN", "Infinity", {}, []])
def test_invalid_raw_close_breaks_corrected_chain_without_adj_fallback(invalid):
    rows = [{"date": f"2026-09-{day}", "close": value, "adj_close": "100"}
            for day, value in zip([14, 15, 16], [100, invalid, 100])]
    corrected, _ = adjusted_rows(rows, snapshot([]))
    stats = calculate_statistics(corrected)
    assert stats["price_basis"] == BASIS and stats["return_count"] == 0
    assert stats["invalid_price_count"] == 1


def test_split_pagination_duplicates_and_raw_quotes_unchanged(db, config):
    rows = [bar("2026-09-14", "250"), bar("2026-09-15", "10"), bar("2026-09-16", "10")]
    splits = [page([event()], total=2), page([event(factor="25.0")], total=2, offset=1)]
    result, source = collect(db, config, Provider([page(rows)], splits=splits))
    assert result["status"] == "succeeded"
    assert result["calls"] == md.budget_status(db, config)["used"] == 4
    assert source.calls[-1][-1] == 1
    assert db.split_snapshots.find_one()["events"] == [{"date": "2026-09-15", "factor": "25.0"}]
    assert [quote["data"] for quote in db.quotes.find().sort("date", 1)] == rows
    assert [item["close"] for item in md.read_quotes(db, ISIN)["items"]] == ["250", "10", "10"]
    assert all(BASIS not in item for item in md.read_quotes(db, ISIN)["items"])
    assert instrument_statistics(db, ISIN)["daily_volatility"] == 0
    saved = list(db.raw_split_pages.find())
    md.run_acquisition(db, config, result["id"], Mock(side_effect=AssertionError("No redelivery requests")))
    assert list(db.raw_split_pages.find()) == saved


@pytest.mark.parametrize("split_rows", [[event(factor=0)], [event(factor=True)], [event(factor="NaN")],
    [event(factor=None)], [event(symbol="SPYI")], [event(exchange="XLON")], [event(day="bad")],
    [event(day="2030-01-01")], [None], [event(), event(factor="2")]])
def test_invalid_split_evidence_retained_but_never_used(db, config, split_rows):
    result, _ = collect(db, config, Provider(splits=[page(split_rows)]))
    assert result["status"] == "failed" and "Split history" in result["error"]
    assert db.raw_split_pages.find_one()["data"]["data"] == split_rows
    assert db.split_snapshots.count_documents({}) == 0
    assert db.quotes.count_documents({}) == 1
    assert db.instrument_state.find_one().get("last_complete_quote_date") is None
    stats = instrument_statistics(db, ISIN)
    assert stats["daily_mean_log_return"] is None
    assert any("incomplete" in w for w in stats["warnings"])


@pytest.mark.parametrize("last", [MarketstackError("quota_exceeded", "Quota exhausted"),
    page([], total=2, offset=1), {"data": []}, page([], total=1, offset=1)])
def test_incomplete_split_pagination_does_not_publish_or_advance(db, config, last):
    result, _ = collect(db, config, Provider(splits=[page([event()], total=2), last]))
    assert result["status"] == "failed"
    assert db.split_snapshots.count_documents({}) == 0
    assert db.instrument_state.find_one().get("last_complete_quote_date") is None


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
    assert len(source.calls) == 3  # No second split request.
    assert db.raw_split_pages.count_documents({}) == db.split_snapshots.count_documents({}) == 1


def test_no_new_quotes_update_reloads_all_splits_and_invalidates_cache(db, config, clock):
    rows = [bar("2026-09-14", "250"), bar("2026-09-15", "10"), bar("2026-09-16", "10")]
    collect(db, config, Provider([page(rows)], splits=[page([])]))
    before = instrument_statistics(db, ISIN)
    saved = list(db.quotes.find())
    clock[0] += timedelta(minutes=1)
    result, source = collect(db, config, Provider(splits=[page([event()])]))
    assert result["status"] == "succeeded"
    assert [call[0] for call in source.calls] == ["tickerinfo", "splits"]
    assert source.calls[-1][2:4] == ("2010-05-19", "2026-09-16")
    after = instrument_statistics(db, ISIN)
    assert before["daily_volatility"] > 0 and after["daily_volatility"] == 0
    assert list(db.quotes.find()) == saved
    # A later complete empty snapshot removes a provider-retracted action.
    clock[0] += timedelta(minutes=1)
    collect(db, config, Provider(splits=[page([])]))
    assert instrument_statistics(db, ISIN)["daily_volatility"] == before["daily_volatility"]


def test_lazy_cache_invalidation_on_split_only_revision(db, config, clock):
    rows = [bar("2026-09-14", "250"), bar("2026-09-15", "10"), bar("2026-09-16", "10")]
    result, source = collect(db, config, Provider([page(rows)]))
    before = instrument_statistics(db, ISIN)
    original = db.split_snapshots.find_one()
    clock[0] += timedelta(seconds=1)
    # Publish a correction from a newer completed job without touching quotes
    # or invoking the worker's force=True refresh.
    revision = {**db.acquisition_jobs.find_one(), "_id": "correction", "requested_at": clock[0]}
    db.acquisition_jobs.insert_one(revision)
    db.split_snapshots.insert_one({**original, "_id": "correction", "collected_at": clock[0],
        "requested_at": clock[0], "events": [{"date": "2026-09-15", "factor": "25"}]})
    after = instrument_statistics(db, ISIN)
    assert before["daily_volatility"] > 0 and after["daily_volatility"] == 0
    assert before["quote_count"] == after["quote_count"] == 3
    assert len(source.calls) == 3


def test_failed_split_refresh_does_not_reuse_an_old_successful_snapshot(db, config, clock):
    collect(db, config, Provider([page([bar("2026-09-14", "250"), bar("2026-09-15", "10")])],
                                 splits=[page([event()])]))
    assert instrument_statistics(db, ISIN)["daily_mean_log_return"] == 0
    clock[0] += timedelta(seconds=1)
    failed, _ = collect(db, config, Provider(splits=[MarketstackError("quota_exceeded", "Quota exhausted")]))
    assert failed["status"] == "failed" and db.split_snapshots.count_documents({}) == 1
    stats = instrument_statistics(db, ISIN)
    assert stats["daily_mean_log_return"] is None
    assert any("Split history download is incomplete" in w for w in stats["warnings"])


def test_uncovered_snapshot_never_falls_back_to_adj_close():
    rows = [{"date": "2026-09-16", "close": "100", "adj_close": "100"}]
    corrected, warnings = adjusted_rows(rows, snapshot([], date_to="2026-09-15"))
    assert corrected[0][BASIS] is None and "does not cover" in warnings[0]
    assert calculate_statistics(corrected)["price_count"] == 0


def test_snapshot_listing_and_knowledge_time_isolation(db, config, clock):
    collect(db, config, Provider(splits=[page([event()])]))
    assert load_split_snapshot(db, ISIN, LISTING, NOW)["events"]
    assert load_split_snapshot(db, ISIN, LISTING, NOW - timedelta(seconds=1)) is None
    assert load_split_snapshot(db, ISIN, {**LISTING, "exchange": "XLON"}, NOW) is None


def test_monte_carlo_uses_corrected_estimates_from_explicit_acquisition(api, db, config):
    rows = history()
    for i, row in enumerate(rows):
        # Leave provider adj_close deliberately stale/inconsistent.
        row.update(close="250" if i < 40 else "10", adj_close="250" if i < 40 else "10")
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
    assert source.calls[1][4] == "2026-09-14"
    assert source.calls[2][3] == "2026-09-16"


@pytest.mark.parametrize("random", [False, True])
def test_calibration_training_and_cash_flows_use_split_series(db, config, random):
    from goal_calibration import calibrate_instrument, evaluate_history, RandomCalibrationInputs
    from random_calibration import evaluate_random_history
    from test_goal_calibration import rows_between, inputs

    rows = rows_between(flat=True)
    for row in rows:
        # Both the training and outcome intervals cross this action.
        row["close"] = "100" if row["date"] < "2024-02-01" else "4"
        row["adj_close"] = row["close"]
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


def test_split_requests_share_job_limit(db, config):
    config.marketstack_max_calls_per_job = 2
    result, source = collect(db, config)
    assert result["status"] == "failed" and "safety limit" in result["error"]
    assert len(source.calls) == 2 and db.raw_split_pages.count_documents({}) == 0
