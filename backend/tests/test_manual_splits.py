from datetime import timedelta
from uuid import uuid4
from unittest.mock import Mock

import pytest

import market_data as md
from manual_splits import ManualSplitRequest, add_split, ensure_indexes, read_splits, remove_split
from return_statistics import instrument_statistics
from split_adjustments import BASIS, adjusted_rows, load_split_snapshot
from marketstack import MarketstackError
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


def seed(db, config, factor="25"):
    before = "250" if factor == "25" else "5"
    rows = [bar("2025-09-16", before), bar("2026-09-14", before), bar("2026-09-15", "10"), bar("2026-09-16", "10")]
    collect(db, config, Provider([page(rows)]))


@pytest.mark.parametrize("factor", ["25", "0.5"])
def test_manual_factor_corrects_statistics_inventory_and_removal_without_touching_evidence(db, config, clock, monkeypatch, factor):
    seed(db, config, factor)
    originals = {name: list(db[name].find()) for name in ("quotes", "raw_eod_pages", "raw_split_pages", "split_snapshots", "marketstack_usage", "acquisition_jobs")}
    provider = Mock(side_effect=AssertionError("Manual operations must not request market data"))
    monkeypatch.setattr(md, "MarketstackClient", provider)
    before = instrument_statistics(db, ISIN)
    assert before["daily_volatility"] > 0
    clock[0] += timedelta(seconds=1)
    saved = add(db, factor=factor)
    after = instrument_statistics(db, ISIN)
    assert after["daily_mean_log_return"] == after["daily_volatility"] == 0
    assert after["historical_year_return"] == after["historical_year_volatility"] == 0
    assert after["price_basis"] == BASIS
    assert any("manually supplied" in warning for warning in after["warnings"])
    inventory = md.acquired_instruments(db)["items"][0]
    assert inventory["historical_year_return"] == inventory["annualized_volatility"] == 0
    clock[0] += timedelta(seconds=1)
    remove_split(db, ISIN, saved["id"], "user-2")
    assert instrument_statistics(db, ISIN)["daily_volatility"] == before["daily_volatility"]
    for name, original in originals.items():
        assert list(db[name].find()) == original
    provider.assert_not_called()


def test_manual_entries_survive_empty_and_matching_provider_updates_and_conflicts_fail_closed(db, config, clock):
    seed(db, config)
    saved = add(db)
    for events in ([], [event(factor="25.000")]):
        clock[0] += timedelta(seconds=1)
        result, _ = collect(db, config, Provider(splits=[page(events)]))
        assert result["status"] == "succeeded"
        assert instrument_statistics(db, ISIN)["daily_volatility"] == 0
        assert load_split_snapshot(db, ISIN, LISTING, clock[0])["events"] == [{"date": "2026-09-15", "factor": "25"}]
        assert read_splits(db, ISIN)["manual"][0]["id"] == saved["id"]
    clock[0] += timedelta(seconds=1)
    collect(db, config, Provider(splits=[page([event(factor="20")])]))
    assert instrument_statistics(db, ISIN)["daily_volatility"] is None
    assert "conflict" in read_splits(db, ISIN)["error"]
    remove_split(db, ISIN, saved["id"], "user-1")
    assert read_splits(db, ISIN)["error"] is None
    assert instrument_statistics(db, ISIN)["daily_volatility"] is not None


def test_duplicate_retries_removal_replacement_and_knowledge_time(db, config, clock):
    seed(db, config)
    before = NOW
    clock[0] += timedelta(seconds=1, microseconds=123456)
    request = ManualSplitRequest(**body())
    first = add_split(db, ISIN, request, "user-1")
    assert add_split(db, ISIN, request, "user-1") == first
    assert db.manual_splits.count_documents({}) == 1
    for factor in ("25", "20"):
        with pytest.raises(md.DataError, match="already exists"):
            add(db, factor=factor)
    assert load_split_snapshot(db, ISIN, LISTING, before)["events"] == []
    learned = clock[0]
    clock[0] += timedelta(seconds=1)
    remove_split(db, ISIN, request.id, "user-2")
    removed = clock[0]
    audit = db.manual_splits.find_one()
    clock[0] += timedelta(seconds=1)
    remove_split(db, ISIN, request.id, "user-2")
    assert db.manual_splits.find_one() == audit  # Repeated deletes retain first removal time.
    with pytest.raises(md.DataError, match="already used or removed"):
        add_split(db, ISIN, request, "user-1")
    replacement = add(db, factor="20")
    remove_split(db, ISIN, request.id, "user-2")  # A delayed delete cannot remove the replacement.
    assert read_splits(db, ISIN)["manual"][0]["id"] == replacement["id"]
    assert read_splits(db, ISIN, learned)["manual"][0]["factor"] == "25"
    assert read_splits(db, ISIN, removed)["manual"] == []
    assert load_split_snapshot(db, ISIN, {**LISTING, "exchange": "XLON"}, clock[0]) is None
    assert audit["created_by"] == "user-1" and audit["removed_by"] == "user-2"


def test_manual_only_legacy_is_explicit_but_does_not_hide_failed_provider_attempt(db, config, clock):
    seed(db, config)
    db.split_snapshots.delete_many({})
    db.acquisition_jobs.delete_many({})
    add(db)
    stats = instrument_statistics(db, ISIN)
    assert stats["daily_volatility"] == 0
    assert any("Other actions may be missing" in warning for warning in stats["warnings"])
    clock[0] += timedelta(seconds=1)
    collect(db, config, Provider(splits=[MarketstackError("quota_exceeded", "Quota exhausted")]))
    stats = instrument_statistics(db, ISIN)
    assert stats["daily_volatility"] is None
    assert "incomplete" in read_splits(db, ISIN)["error"]


def test_multiple_manual_actions_and_nontrading_date_have_no_future_prefix_effect(db, config, clock):
    seed(db, config)
    add(db, date="2026-09-13")  # Sunday; no candle is required on the effective date.
    rows = [{"date": "2026-09-11", "close": "250"}, {"date": "2026-09-14", "close": "10"}]
    initial, _ = adjusted_rows(rows, load_split_snapshot(db, ISIN, LISTING, clock[0]))
    add(db, date="2026-09-16", factor="0.5")
    later, _ = adjusted_rows(rows + [{"date": "2026-09-16", "close": "20"}], load_split_snapshot(db, ISIN, LISTING, clock[0]))
    assert later[:2] == initial
    assert [float(row[BASIS]) for row in later] == [250, 250, 250]


def test_api_auth_exact_values_scoping_and_no_quota(api, db, config):
    client, publish = api
    seed(db, config)
    path = f"/instruments/{ISIN}/splits"
    request = body(factor="25.000000000000000000000001", note="  issuer notice  ")
    assert client.get(path).status_code == 401
    assert client.post(path, json=request).status_code == 401
    assert client.delete(path + "/" + request["id"]).status_code == 401
    login(client)
    assert client.post(path, json=request, headers={"Origin": "https://evil.example"}).status_code == 403
    config.marketstack_monthly_budget = 0
    config.marketstack_api_key = ""
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
    seed(db, config)
    client, _ = api
    login(client)
    response = client.post(f"/instruments/{ISIN}/splits", json=body(**changes))
    assert response.status_code == 422, response.text
    assert db.manual_splits.count_documents({}) == 0


def test_no_quotes_wrong_listing_missing_instrument_and_provider_conflict(api, db, config):
    client, _ = api
    login(client)
    path = f"/instruments/{ISIN}/splits"
    assert client.post(path, json=body()).status_code == 409
    seed(db, config)
    assert client.post(path, json=body(symbol="OTHER.DE")).status_code == 409
    assert client.post(path, json=body(exchange="XLON")).status_code == 409
    assert client.post("/instruments/IE0000000000/splits", json=body()).status_code == 404
    db.split_snapshots.update_one({}, {"$set": {"events": [{"date": "2026-09-15", "factor": "20"}]}})
    assert client.post(path, json=body()).status_code == 409
    # Matching values are allowed and still apply once.
    assert client.post(path, json=body(factor="20.0")).status_code == 201
    assert load_split_snapshot(db, ISIN, LISTING, NOW)["events"] == [{"date": "2026-09-15", "factor": "20"}]


def test_simulation_uses_manual_correction(api, db, config):
    rows = history()
    for index, row in enumerate(rows):
        row.update(close="250" if index < 40 else "10", adj_close="999")
    collect(db, config, Provider([page(rows)]))
    add(db, date=rows[40]["date"][:10])
    client, _ = api
    login(client)
    result = client.post("/goals/simulate", json=request_body())
    assert result.status_code == 200, result.text
    stats = result.json()["parameter_statistics"]["instrument"]
    assert stats["daily_mean_log_return"] == stats["daily_volatility"] == 0
    assert stats["price_basis"] == BASIS


@pytest.mark.parametrize("random", [False, True])
def test_calibration_uses_manual_splits_for_training_and_outcomes(db, config, random):
    from goal_calibration import calibrate_instrument, evaluate_history, RandomCalibrationInputs
    from random_calibration import evaluate_random_history
    from test_goal_calibration import rows_between, inputs

    rows = rows_between(flat=True)
    for row in rows:
        row["close"] = "100" if row["date"] < "2024-02-01" else "4"
        row["adj_close"] = row["close"]
    collect(db, config, Provider([page(rows)]))
    add(db, date="2024-02-01")
    request = (RandomCalibrationInputs(mode="random", scenario_count=12, min_horizon_months=6,
               max_horizon_months=6, lookback_months=6, seed=42) if random else inputs())
    evaluate = evaluate_random_history if random else evaluate_history
    result = calibrate_instrument(db, ISIN, request, evaluate=lambda *a, **kw: evaluate(*a, **kw, paths=100))
    expected = evaluate(rows_between(flat=True), request, seed=42, paths=100)
    assert result["sample_count"] == expected["sample_count"]
    for actual, reference in zip(result["forecasts"], expected["forecasts"]):
        assert actual["price_basis"] == BASIS
        assert {**actual, "price_basis": "adj_close"} == reference
