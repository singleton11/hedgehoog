"""Preserved manual-action audit data and Yahoo's no-double-multiplication contract."""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4
from unittest.mock import Mock

import pytest

import market_data as md
from manual_splits import ManualSplitRequest, add_split, ensure_indexes, read_splits, remove_split
from return_statistics import instrument_statistics
from split_adjustments import BASIS, adjusted_rows, load_split_snapshot
from test_goal_planner import history, login, request_body
from test_market_data import ISIN, LISTING, NOW, Provider, api, bar, clock, collect, config, db, page
from test_split_adjustments import event


@pytest.fixture(autouse=True)
def indexes(db):
    ensure_indexes(db)


def body(**changes):
    return {"id": str(uuid4()), **LISTING, "date": "2026-09-15", "factor": "25", "note": "Issuer notice", **changes}


def add(db, **changes):
    return add_split(db, ISIN, ManualSplitRequest(**body(**changes)), "user-1")["item"]


def rows():
    return [{**bar(day, "10"), "price_basis": "provider_split_adjusted"} for day in
            ("2025-09-16", "2026-09-14", "2026-09-15", "2026-09-16")]


def seed(db, config):
    result, _ = collect(db, config, Provider([page(rows())]))
    assert result["status"] == "succeeded"


def legacy(db):
    """Only legacy administration is writable; it never authorizes a history read."""
    db.instrument_state.insert_one({"_id": ISIN, "listing": LISTING})
    db.quotes.insert_one({"instrument_id": ISIN, **LISTING, "date": "2026-09-15", "data": bar()})


def evidence(db):
    return {name: [{key: value for key, value in row.items() if key != "reader_leases"}
                   for row in db[name].find()] for name in
            ("quotes", "raw_eod_pages", "raw_split_pages", "split_snapshots", "marketstack_usage", "acquisition_jobs")}


@pytest.mark.parametrize("factor", ["25", "0.5"])
def test_yahoo_rejects_manual_multiplication_without_touching_statistics_or_evidence(db, config, clock, monkeypatch, factor):
    seed(db, config)
    before = instrument_statistics(db, ISIN)
    originals = evidence(db)
    provider = Mock(side_effect=AssertionError("Manual operations must not request market data"))
    monkeypatch.setattr(md, "provider_client", provider)
    clock[0] += timedelta(seconds=1)
    with pytest.raises(md.DataError, match="already split-adjusted") as error:
        add(db, factor=factor)
    assert error.value.status == 409
    after = instrument_statistics(db, ISIN)
    assert before["daily_volatility"] == after["daily_mean_log_return"] == after["daily_volatility"] == 0
    assert after["price_basis"] == BASIS
    inventory = md.acquired_instruments(db)["items"][0]
    assert inventory["historical_year_return"] == inventory["annualized_volatility"] == 0
    assert db.manual_splits.count_documents({}) == 0
    assert evidence(db) == originals
    assert all(not job.get("reader_leases") for job in db.acquisition_jobs.find())
    provider.assert_not_called()


def test_saved_manual_entries_survive_yahoo_refresh_and_conflicting_actions_without_reapplication(db, config, clock):
    legacy(db)
    saved = add(db)
    audit = deepcopy(db.manual_splits.find_one())
    for events in ([], [event(factor="25.000")], [event(factor="20")]):
        clock[0] += timedelta(seconds=1)
        result, _ = collect(db, config, Provider([page(rows())], splits=[page(events)]))
        assert result["status"] == "succeeded"
        assert instrument_statistics(db, ISIN)["daily_volatility"] == 0
        result = read_splits(db, ISIN)
        assert result["supported"] is False and result["error"] is None
        assert result["manual"][0]["id"] == saved["id"]
        assert db.manual_splits.find_one() == audit
    remove_split(db, ISIN, saved["id"], "user-1")
    assert read_splits(db, ISIN)["manual"] == []
    assert instrument_statistics(db, ISIN)["daily_volatility"] == 0


def test_duplicate_retries_removal_replacement_and_audit_time(db, config, clock):
    legacy(db)
    before = NOW
    clock[0] += timedelta(seconds=1, microseconds=123456)
    request = ManualSplitRequest(**body())
    first = add_split(db, ISIN, request, "user-1")
    assert add_split(db, ISIN, request, "user-1") == first
    assert db.manual_splits.count_documents({}) == 1
    for factor in ("25", "20"):
        with pytest.raises(md.DataError, match="already exists"):
            add(db, factor=factor)
    assert read_splits(db, ISIN, before)["manual"] == []
    learned = clock[0]
    clock[0] += timedelta(seconds=1)
    remove_split(db, ISIN, request.id, "user-2")
    removed = clock[0]
    audit = db.manual_splits.find_one()
    clock[0] += timedelta(seconds=1)
    remove_split(db, ISIN, request.id, "user-2")
    assert db.manual_splits.find_one() == audit
    with pytest.raises(md.DataError, match="already used or removed"):
        add_split(db, ISIN, request, "user-1")
    replacement = add(db, factor="20")
    remove_split(db, ISIN, request.id, "user-2")
    assert read_splits(db, ISIN)["manual"][0]["id"] == replacement["id"]
    assert read_splits(db, ISIN, learned)["manual"][0]["factor"] == "25"
    assert read_splits(db, ISIN, removed)["manual"] == []
    assert load_split_snapshot(db, ISIN, LISTING, clock[0]) is None
    assert audit["created_by"] == "user-1" and audit["removed_by"] == "user-2"
    assert md.read_quotes(db, ISIN)["items"] == []


def test_manual_only_legacy_never_authorizes_prices_after_failed_yahoo_attempt(db, config, clock):
    legacy(db)
    add(db)
    assert instrument_statistics(db, ISIN)["daily_volatility"] is None
    assert md.read_quotes(db, ISIN)["items"] == []
    clock[0] += timedelta(seconds=1)
    from market_provider import MarketDataError
    failed, _ = collect(db, config, Provider([MarketDataError("quota_exceeded", "Quota exhausted")]))
    assert failed["status"] == "failed"
    assert instrument_statistics(db, ISIN)["daily_volatility"] is None
    assert read_splits(db, ISIN)["manual"][0]["factor"] == "25"
    assert load_split_snapshot(db, ISIN, LISTING, clock[0]) is None


def test_multiple_manual_actions_and_nontrading_date_have_no_future_prefix_effect(db, config, clock):
    legacy(db)
    add(db, date="2026-09-13")
    raw = [{"date": "2026-09-11", "close": "250"}, {"date": "2026-09-14", "close": "10"}]
    events = lambda: {"_id": "audit", "date_from": "2010-05-19", "date_to": "2026-09-17",
                      "events": [{"date": item["date"], "factor": item["factor"]}
                                                   for item in read_splits(db, ISIN)["manual"]]}
    initial, _ = adjusted_rows(raw, events())
    add(db, date="2026-09-16", factor="0.5")
    later, _ = adjusted_rows(raw + [{"date": "2026-09-16", "close": "20"}], events())
    assert later[:2] == initial
    assert [float(row[BASIS]) for row in later] == [250, 250, 250]
    assert load_split_snapshot(db, ISIN, LISTING, clock[0]) is None


def test_api_auth_exact_values_scoping_and_no_provider_requests(api, db, config):
    client, publish = api
    legacy(db)
    path = f"/instruments/{ISIN}/splits"
    request = body(factor="25.000000000000000000000001", note="  issuer notice  ")
    assert client.get(path).status_code == 401
    assert client.post(path, json=request).status_code == 401
    assert client.delete(path + "/" + request["id"]).status_code == 401
    login(client)
    assert client.post(path, json=request, headers={"Origin": "https://evil.example"}).status_code == 403
    config.market_data_max_calls_per_job = 1
    used = db.marketstack_usage.find_one()
    result = client.post(path, json=request)
    assert result.status_code == 201, result.text
    assert result.json()["item"]["factor"] == request["factor"]
    assert result.json()["item"]["note"] == "issuer notice"
    assert client.post(path, json=request).json() == result.json()
    assert client.get(path).json()["manual"] == [result.json()["item"]]
    assert client.get(path + "?as_of=2026-09-17T00:00:00").status_code == 422
    assert client.get(path + "?as_of=2026-09-17T00:00:00Z").json()["manual"] == []
    assert client.delete(f"/instruments/IE00B4L5Y983/splits/{request['id']}").status_code == 404
    assert client.delete(path + "/" + request["id"]).status_code == 204
    assert client.delete(path + "/" + request["id"]).status_code == 204
    assert client.get(path).json()["manual"] == []
    assert client.post(path, json=request).status_code == 409
    assert db.marketstack_usage.find_one() == used
    publish.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"factor": 25}, {"factor": True}, {"factor": "0"}, {"factor": "1"}, {"factor": "1.00"},
    {"factor": "-0.5"}, {"factor": "NaN"}, {"factor": "Infinity"}, {"factor": "1e3"},
    {"factor": "1000000001"}, {"factor": "0.0000000001"}, {"factor": "0." + "1" * 32},
    {"date": "2026-02-30"}, {"date": "2026-09-18"}, {"date": "2026-9-01"},
    {"date": "2000-01-01"}, {"note": "x" * 501}, {"note": None}, {"id": "invalid"},
    {"exchange": "XETR;"}, {"unexpected": True},
])
def test_invalid_manual_api_inputs(api, db, config, changes):
    legacy(db)
    client, _ = api
    login(client)
    response = client.post(f"/instruments/{ISIN}/splits", json=body(**changes))
    assert response.status_code == 422, response.text
    assert db.manual_splits.count_documents({}) == 0


def test_no_quotes_wrong_listing_missing_instrument_and_yahoo_rejection(api, db, config):
    client, _ = api
    login(client)
    path = f"/instruments/{ISIN}/splits"
    assert client.post(path, json=body()).status_code == 409
    seed(db, config)
    assert client.post(path, json=body(symbol="OTHER.DE")).status_code == 409
    assert client.post(path, json=body(exchange="XLON")).status_code == 409
    assert client.post("/instruments/IE0000000000/splits", json=body()).status_code == 404
    db.split_snapshots.update_one({}, {"$set": {"events": [{"date": "2026-09-15", "factor": "20"}]}})
    for factor in ("25", "20.0"):
        response = client.post(path, json=body(factor=factor))
        assert response.status_code == 409 and "already split-adjusted" in response.json()["detail"]
    assert load_split_snapshot(db, ISIN, LISTING, NOW)["events"] == [{"date": "2026-09-15", "factor": "20"}]


def test_simulation_ignores_preserved_manual_multiplication(api, db, config):
    legacy(db)
    candles = history()
    add(db, date=candles[40]["date"][:10])
    for row in candles:
        row.update(close="10", adj_close="999", price_basis="provider_split_adjusted")
    collect(db, config, Provider([page(candles)]))
    client, _ = api
    login(client)
    result = client.post("/goals/simulate", json=request_body())
    assert result.status_code == 200, result.text
    stats = result.json()["parameter_statistics"]["instrument"]
    assert stats["daily_mean_log_return"] == stats["daily_volatility"] == 0
    assert stats["price_basis"] == BASIS
    assert read_splits(db, ISIN)["manual"]


@pytest.mark.parametrize("random", [False, True])
def test_calibration_preserves_manual_audit_without_double_adjusting_training_or_outcomes(db, config, random):
    from goal_calibration import calibrate_instrument, evaluate_history, RandomCalibrationInputs
    from random_calibration import evaluate_random_history
    from test_goal_calibration import rows_between, inputs

    legacy(db)
    add(db, date="2024-02-01")
    candles = [{**row, "close": "4", "adj_close": "999", "price_basis": "provider_split_adjusted"}
               for row in rows_between(flat=True)]
    collect(db, config, Provider([page(candles)]))
    request = (RandomCalibrationInputs(mode="random", scenario_count=12, min_horizon_months=6,
               max_horizon_months=6, lookback_months=6, seed=42) if random else inputs())
    evaluate = evaluate_random_history if random else evaluate_history
    result = calibrate_instrument(db, ISIN, request, evaluate=lambda *a, **kw: evaluate(*a, **kw, paths=100))
    expected = evaluate(rows_between(flat=True), request, seed=42, paths=100)
    assert result["sample_count"] == expected["sample_count"]
    for actual, reference in zip(result["forecasts"], expected["forecasts"]):
        assert actual["price_basis"] == BASIS
        assert {**actual, "price_basis": "adj_close"} == reference
    assert db.manual_splits.count_documents({"active": True}) == 1
