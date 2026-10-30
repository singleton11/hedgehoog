from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import math
from unittest.mock import Mock

import numpy as np
import pytest

import goal_calibration as gc
import goal_strategies as gs
import market_data as md
from goal_calibration import CalibrationInputs, RandomCalibrationInputs, calibration_metrics, evaluate_history, forecast_probability, realized_value
from goal_simulation import add_months
from return_statistics import calculate_statistics
from test_goal_planner import login, seed_history, seed_profile
from test_market_data import ISIN, LISTING, NOW, api, clock, config, db, seed_yahoo


def rows_between(start=date(2022, 1, 3), end=date(2025, 12, 31), *, flat=False):
    rows = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            index = len(rows)
            price = 100 if flat else 100 * math.exp(0.0001 * index + 0.035 * math.sin(index * 0.7))
            rows.append({"date": day.isoformat(), "adj_close": price, "close": price, "price_currency": "EUR"})
        day += timedelta(days=1)
    return rows


def inputs(**changes):
    return CalibrationInputs(**{
        "goal_eur": "10700", "starting_capital_eur": "10000", "monthly_contribution_eur": "50",
        "lookback_months": 6, "horizon_months": 6, "seed": 42, **changes,
    })


def body(**changes):
    settings = RandomCalibrationInputs(mode="random", **{
        "scenario_count": 100, "min_horizon_months": 6, "max_horizon_months": 6,
        "lookback_months": 6, "seed": 42, **changes,
    })
    return {**settings.model_dump(mode="json"), "strategy": {
        "id": "buy_and_hold_single_instrument", "parameters": {"instrument": ISIN},
    }}


def assert_same_calculation(first, second):
    # Replays reproduce the numerical report, not the identity/timestamp of a run.
    assert first["id"] != second["id"]
    metadata = {"id", "calculated_at"}
    assert {key: value for key, value in first.items() if key not in metadata} == {
        key: value for key, value in second.items() if key not in metadata
    }


def evaluate(rows=None, **changes):
    return evaluate_history(rows if rows is not None else rows_between(), inputs(**changes), seed=42, paths=400)


def test_brier_formula_bins_mean_probabilities_and_probability_one():
    forecasts = [{"predicted_probability": p, "observed_success": y} for p, y in
                 [(0, False), (0.1, True), (0.11, False), (0.25, True), (0.8, False), (1, True)]]
    result = calibration_metrics(forecasts)
    assert result["sample_count"] == 6
    assert result["brier_score"] == pytest.approx((0 + 0.9**2 + 0.11**2 + 0.75**2 + 0.8**2 + 0) / 6)
    assert result["mean_predicted_probability"] == pytest.approx(2.26 / 6)
    assert result["observed_success_rate"] == 0.5
    bins = result["calibration_bins"]
    assert len(bins) == 10 and sum(item["count"] for item in bins) == 6
    assert bins[1] == {"lower": 0.1, "upper": 0.2, "count": 2,
                       "mean_predicted_probability": 0.10500000000000001, "observed_success_rate": 0.5}
    assert bins[9]["count"] == 1 and bins[9]["mean_predicted_probability"] == 1
    assert bins[3] == {"lower": 0.3, "upper": 0.4, "count": 0,
                       "mean_predicted_probability": None, "observed_success_rate": None}
    assert calibration_metrics([{"predicted_probability": 0, "observed_success": True}])["brier_score"] == 1


def test_fixed_contribution_forecast_matches_independent_planner_log_process():
    scenario = inputs()
    origin = date(2024, 2, 29)
    estimates = {"daily_mean_log_return": 0.0001, "daily_log_return_variance": 0.0002}
    seed, paths = 123, 1024
    rng = np.random.default_rng(np.random.SeedSequence([seed, origin.toordinal()]))
    wealth = np.full(paths, float(scenario.starting_capital_eur))
    previous = origin
    for month in range(1, scenario.horizon_months + 1):
        day = add_months(origin, month)
        time = 252 * (day - previous).days / 365.25
        wealth *= np.exp(rng.normal(0.0001 * time, math.sqrt(0.0002 * time), paths))
        wealth += float(scenario.monthly_contribution_eur)
        previous = day
    actual = forecast_probability(estimates, scenario, origin, seed, paths=paths)
    assert actual == float(np.mean(wealth >= float(scenario.goal_eur)))
    assert actual == forecast_probability(estimates, scenario, origin, seed, paths=paths)
    assert actual != forecast_probability(estimates, scenario, origin, seed + 1, paths=paths)


def test_flat_history_exact_funding_and_zero_one_probabilities():
    rows = rows_between(flat=True)
    result = evaluate(rows, starting_capital_eur="100", monthly_contribution_eur="10", goal_eur="160")
    assert result["sample_count"] > 20
    assert result["brier_score"] == 0
    assert result["observed_success_rate"] == result["mean_predicted_probability"] == 1
    assert all(f["terminal_value_eur"] == "160.00" and f["observed_success"] for f in result["forecasts"])
    missed = evaluate(rows, starting_capital_eur="100", monthly_contribution_eur="10", goal_eur="160.01")
    assert missed["brier_score"] == 0
    assert missed["observed_success_rate"] == missed["mean_predicted_probability"] == 0
    assert all(not f["observed_success"] for f in missed["forecasts"])


@pytest.mark.parametrize("capital,monthly,horizon,target", [
    ("10000", "100.05", 12, "11200.60"),
    ("0", "0.01", 12, "0.12"),
    ("999999987993.99", "100.05", 120, "999999999999.99"),
])
def test_exact_cent_funding_meets_goal_but_one_cent_short_does_not(capital, monthly, horizon, target):
    origin = date(2010, 1, 4)
    rows = rows_between(origin, add_months(origin, horizon), flat=True)
    dates = [date.fromisoformat(row["date"]) for row in rows]
    estimates = {"daily_mean_log_return": 0.0, "daily_log_return_variance": 0.0}
    scenario = inputs(starting_capital_eur=capital, monthly_contribution_eur=monthly,
                      horizon_months=horizon, goal_eur=target)
    value, _ = realized_value(rows, dates, 0, add_months(origin, horizon), "adj_close", scenario, None)
    assert gc.money_string(value) == target
    assert gc.reaches_goal(value, scenario.goal_eur)
    assert forecast_probability(estimates, scenario, origin, 42, paths=50) == 1
    higher_goal = scenario.model_copy(update={"goal_eur": Decimal(target) + Decimal("0.01")})
    assert not gc.reaches_goal(value, higher_goal.goal_eur)
    assert forecast_probability(estimates, higher_goal, origin, 42, paths=50) == 0
    if horizon == 12:
        result = evaluate(rows_between(flat=True), starting_capital_eur=capital,
                          monthly_contribution_eur=monthly, horizon_months=horizon, goal_eur=target)
        assert result["brier_score"] == 0
        assert result["observed_success_rate"] == result["mean_predicted_probability"] == 1


def test_goal_equality_tolerance_is_subcent_not_display_rounding():
    for goal in [0.01, 11200.60, 1e12]:
        assert gc.reaches_goal(np.nextafter(goal, -np.inf), goal)
        assert not gc.reaches_goal(goal - 0.004, goal)  # May round to goal on display.


def test_only_trailing_prefix_is_fitted_and_predictions_exist_before_outcome_replay(monkeypatch):
    rows = rows_between()
    original = deepcopy(rows)
    estimates_by_origin = {}
    estimate = gc.calculate_statistics
    replay = gc.realized_value

    def capture(training):
        result = estimate(training)
        origin = date.fromisoformat(training[-1]["date"])
        assert all(add_months(origin, -6).isoformat() <= row["date"] <= origin.isoformat() for row in training)
        estimates_by_origin[origin] = training
        return result

    predicted = set()
    predict = gc.forecast_probability

    def capture_prediction(estimates, scenario, origin, seed, **kwargs):
        assert origin in estimates_by_origin
        predicted.add(origin)
        return predict(estimates, scenario, origin, seed, **kwargs)

    def capture_replay(rows, dates, index, *args):
        assert dates[index] in predicted
        return replay(rows, dates, index, *args)

    monkeypatch.setattr(gc, "calculate_statistics", capture)
    monkeypatch.setattr(gc, "forecast_probability", capture_prediction)
    monkeypatch.setattr(gc, "realized_value", capture_replay)
    result = evaluate(rows)
    assert result["sample_count"] == len(predicted)
    for forecast in result["forecasts"]:
        training = estimates_by_origin[date.fromisoformat(forecast["origin_date"])]
        assert forecast["training_end_date"] == forecast["origin_date"] < forecast["goal_date"]
        assert forecast["training_return_count"] == calculate_statistics(training)["return_count"]
    assert rows == original


def test_perturbing_all_future_prices_cannot_change_earlier_forecast_or_its_basis():
    rows = rows_between()
    baseline = evaluate(rows, start_date="2023-01-01", end_date="2023-01-31")
    expected = baseline["forecasts"][0]
    changed = deepcopy(rows)
    for row in changed:
        if row["date"] > expected["origin_date"]:
            row["close"] *= 0.001
            row["adj_close"] *= 0.001
    future_crash = evaluate(changed, start_date="2023-01-01", end_date="2023-01-31")["forecasts"][0]
    assert future_crash["predicted_probability"] == expected["predicted_probability"]
    assert future_crash["price_basis"] == expected["price_basis"] == "adj_close"
    assert future_crash["terminal_value_eur"] != expected["terminal_value_eur"]
    assert future_crash["observed_success"] != expected["observed_success"]


def test_incomplete_future_adjustments_do_not_reselect_training_basis():
    rows = rows_between()
    baseline = evaluate(rows)
    origin = baseline["forecasts"][0]["origin_date"]
    # A missing adjusted price during the hold, not an execution or valuation,
    # must not make the already-issued forecast use raw prices.
    later = next(row for row in rows if row["date"] > origin)
    later["adj_close"] = None
    later["close"] = 90000
    changed = evaluate(rows)
    assert changed["forecasts"][0] == baseline["forecasts"][0]


def test_future_raw_price_must_not_replace_missing_adjusted_terminal():
    rows = rows_between()
    baseline = evaluate(rows)
    first = baseline["forecasts"][0]
    next(row for row in rows if row["date"] == first["valuation_date"])["adj_close"] = None
    result = evaluate(rows)
    assert {"origin_date": first["origin_date"], "reason": "missing_outcome_price"} in result["excluded_origins"]
    assert not any(f["origin_date"] == first["origin_date"] for f in result["forecasts"])
    assert result["skipped_counts"]["missing_outcome_price"] >= 1


def test_streams_stable_when_evaluation_range_or_later_history_changes():
    rows = rows_between()
    full = evaluate(rows)
    filtered = evaluate(rows, start_date="2023-01-01", end_date="2023-12-31")
    assert filtered["forecasts"] == [f for f in full["forecasts"] if "2023-01-01" <= f["origin_date"] <= "2023-12-31"]
    truncated = evaluate([row for row in rows if row["date"] <= "2024-12-31"])
    lookup = {f["origin_date"]: f for f in full["forecasts"]}
    assert all(f == lookup[f["origin_date"]] for f in truncated["forecasts"])
    assert full == evaluate(rows)
    another = evaluate_history(rows, inputs(), seed=43, paths=400)
    assert [f["predicted_probability"] for f in full["forecasts"]] != [f["predicted_probability"] for f in another["forecasts"]]
    assert [f["terminal_value_eur"] for f in full["forecasts"]] == [f["terminal_value_eur"] for f in another["forecasts"]]


def test_realized_cashflow_execution_rolls_forward_valuation_never_looks_forward():
    rows = [{"date": day, "adj_close": price, "price_currency": "EUR"} for day, price in [
        ("2024-01-05", 100), ("2024-02-02", 110), ("2024-02-06", 120),
        ("2024-03-04", 130), ("2024-03-06", 99999),
    ]]
    dates = [date.fromisoformat(row["date"]) for row in rows]
    scenario = inputs(starting_capital_eur="1000", monthly_contribution_eur="100", horizon_months=2)
    value, valuation = realized_value(rows, dates, 0, date(2024, 3, 5), "adj_close", scenario, None)
    # Feb5 deposit buys Feb6, not Feb2. Mar5 deposit remains cash; Mar6 unused.
    assert value == pytest.approx(1000 * 130 / 100 + 100 * 130 / 120 + 100)
    assert valuation == date(2024, 3, 4)
    rows[2]["adj_close"] = None
    with pytest.raises(gc.UnscorableOutcome, match="missing_execution_price"):
        realized_value(rows, dates, 0, date(2024, 3, 5), "adj_close", scenario, None)
    # No contribution means no need to price a nonexistent intermediate trade.
    realized_value(rows, dates, 0, date(2024, 3, 5), "adj_close", inputs(monthly_contribution_eur="0", horizon_months=2), None)


def test_stale_execution_and_valuation_are_not_cherry_picked():
    rows = [{"date": day, "close": 100, "price_currency": "EUR"} for day in [
        "2024-01-05", "2024-02-14", "2024-03-05",
    ]]
    dates = [date.fromisoformat(row["date"]) for row in rows]
    with pytest.raises(gc.UnscorableOutcome, match="missing_execution_price"):
        realized_value(rows, dates, 0, dates[-1], "close", inputs(horizon_months=2), None)
    with pytest.raises(gc.UnscorableOutcome, match="missing_outcome_price"):
        realized_value(rows, dates, 0, date(2024, 3, 15), "close", inputs(horizon_months=2), None)


def test_weekends_gaps_invalid_origin_and_incomplete_horizons_are_explicit():
    rows = rows_between()
    original = evaluate(rows)
    origin = original["forecasts"][0]["origin_date"]
    next(row for row in rows if row["date"] == origin).update(adj_close=None, close=None)
    rows += [{"date": "2025-12-27", "adj_close": 10000000, "close": 10000000, "price_currency": "USD"}]
    rows.sort(key=lambda row: row["date"])
    result = evaluate(rows)
    assert {"origin_date": origin, "reason": "unusable_training"} in result["excluded_origins"]
    assert not any(f["origin_date"] == origin for f in result["forecasts"])
    assert result["candidate_count"] == result["sample_count"] + sum(result["skipped_counts"].values())
    assert result["skipped_counts"]["insufficient_training"] > 0
    assert result["skipped_counts"]["incomplete_horizon"] > 0
    assert all(f["goal_date"] <= result["history_last_date"] for f in result["forecasts"])
    assert any("overlapping" in warning for warning in result["warnings"])


@pytest.mark.parametrize("currency,override,works", [("EUR", None, True), (None, "EUR", True),
    ("USD", "EUR", False), (None, None, False), ("EUR", "USD", False)])
def test_currency_eligibility_is_resolved_per_training_window(currency, override, works):
    rows = [{**row, "price_currency": currency} for row in rows_between()]
    if works:
        assert evaluate_history(rows, inputs(), seed=42, override=override, paths=50)["sample_count"] > 0
    else:
        with pytest.raises(md.DataError, match="EUR"):
            evaluate_history(rows, inputs(), seed=42, override=override, paths=50)


def test_future_currency_conflict_does_not_reject_earlier_valid_folds():
    rows = rows_between()
    before = evaluate(rows)
    rows[-1]["price_currency"] = "USD"
    after = evaluate(rows)
    assert after["forecasts"][0] == before["forecasts"][0]
    assert after["sample_count"] > 0


@pytest.mark.parametrize("rows", [[], rows_between(end=date(2022, 2, 1)),
    rows_between()[::5], [{**row, "close": None, "adj_close": None} for row in rows_between()]])
def test_no_fabricated_score_for_empty_short_sparse_or_invalid_history(rows):
    with pytest.raises(md.DataError):
        evaluate(rows)


def test_limits_fail_explicitly_rather_than_silent_sampling(monkeypatch):
    monkeypatch.setattr(gc, "MAX_ORIGINS", 2)
    with pytest.raises(md.DataError, match="calculation limit"):
        evaluate()
    monkeypatch.setattr(gc, "MAX_ORIGINS", 600)
    monkeypatch.setattr(gc, "MAX_FORECAST_PERIODS", 10)
    with pytest.raises(md.DataError, match="calculation limit"):
        evaluate()


def test_extreme_model_is_rejected_not_probability_clipped():
    with pytest.raises(md.DataError, match="unstable"):
        forecast_probability({"daily_mean_log_return": 1000, "daily_log_return_variance": 1},
                             inputs(), date(2024, 1, 1), 42, paths=100)


def test_api_auth_origin_saved_history_only_and_reproducible_seed(api, db, config, monkeypatch):
    client, publish = api
    seed_history(db, rows_between())
    seed_profile(db)
    provider = Mock(side_effect=AssertionError("Calibration must never acquire or use full-history estimates"))
    monkeypatch.setattr(md, "provider_client", provider)
    monkeypatch.setattr(gs, "instrument_statistics", provider)
    assert client.post("/goals/calibrate", json=body()).status_code == 401
    login(client)
    assert client.post("/goals/calibrate", json=body(), headers={"Origin": "https://evil.example"}).status_code == 403
    quotes = list(db.quotes.find())
    response = client.post("/goals/calibrate", json=body(seed=0))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["seed"] == 0
    assert result["strategy"] == body()["strategy"]
    assert result["settings"] == {k: v for k, v in body().items() if k not in ("strategy", "seed")}
    assert result["simulation_paths"] == 10000
    assert result["method"] == "walk_forward"
    assert result["sample_count"] > 0
    assert result["data_as_of"] == md.public(NOW)
    assert len(result["history_fingerprint"]) == 64
    assert_same_calculation(result, client.post("/goals/calibrate", json=body(seed=0)).json())
    assert list(db.quotes.find()) == quotes
    assert db.instrument_statistics.count_documents({}) == 0
    assert md.budget_status(db, config)["used"] == 0
    assert db.acquisition_jobs.count_documents({}) == 1
    assert db.acquisition_jobs.find_one({"_id": "seed-history"})["status"] == "succeeded"
    provider.assert_not_called()
    publish.assert_not_called()


def test_generated_seed_is_returned_and_can_be_replayed(api, db, monkeypatch):
    client, _ = api
    login(client)
    seed_history(db, rows_between())
    seed_profile(db)
    generate = Mock(return_value=987654321)
    monkeypatch.setattr(gc.secrets, "randbits", generate)
    response = client.post("/goals/calibrate", json=body(seed=None))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["seed"] == 987654321
    generate.assert_called_once_with(32)
    assert_same_calculation(result, client.post("/goals/calibrate", json=body(seed=987654321)).json())
    generate.assert_called_once_with(32)


@pytest.mark.parametrize("changes", [
    {"goal_eur": "0"}, {"goal_eur": "NaN"}, {"goal_eur": "1.001"}, {"goal_eur": "1000000000001"},
    {"starting_capital_eur": "-1"}, {"monthly_contribution_eur": "-1"}, {"monthly_contribution_eur": "Infinity"},
    {"horizon_months": 0}, {"horizon_months": 121}, {"horizon_months": True}, {"horizon_months": 1.5},
    {"lookback_months": 2}, {"lookback_months": 121}, {"lookback_months": "12"},
    {"seed": -1}, {"seed": 2**32}, {"seed": True}, {"seed": 1.2}, {"seed": "42"},
    {"start_date": "2025-01-01", "end_date": "2024-01-01"}, {"start_date": "tomorrow"},
    {"simulation_paths": 100000000}, {"strategy": {"id": "unknown", "parameters": {}}},
])
def test_api_rejects_invalid_inputs_before_data_access(api, db, monkeypatch, changes):
    client, _ = api
    login(client)
    forbidden = Mock(side_effect=AssertionError("Invalid request must not access instrument data"))
    monkeypatch.setattr(db.instruments, "find_one", forbidden)
    response = client.post("/goals/calibrate", json={**body(), **changes})
    assert response.status_code == 422, response.text
    forbidden.assert_not_called()


def test_api_rechecks_acquired_membership_missing_history_and_active_acquisition(api, db, config):
    client, _ = api
    login(client)
    seed_history(db, rows_between())
    assert "published Yahoo history" in client.post("/goals/calibrate", json=body()).json()["detail"]
    seed_profile(db)
    db.acquisition_jobs.insert_one({"instrument_id": ISIN, "active": True, "status": "running", "heartbeat_at": NOW})
    assert "Wait" in client.post("/goals/calibrate", json=body()).json()["detail"]
    db.acquisition_jobs.delete_many({})
    db.quotes.delete_many({})
    assert client.post("/goals/calibrate", json=body()).status_code == 422


def test_saved_snapshot_filters_unpublished_rows_and_uses_latest_complete_revision(api, db, clock):
    client, _ = api
    login(client)
    seed_history(db, rows_between())
    seed_profile(db)
    request = body(start_date="2023-01-01", end_date="2023-12-31")
    before = client.post("/goals/calibrate", json=request).json()
    saved = list(db.quotes.find({"job_id": "seed-history"}).sort("date", 1))
    original = next(row for row in saved if row["date"] == "2023-04-03")
    for name, changes in [
        ("wrong-listing", {"symbol": "OTHER", "job_id": "seed-history"}),
        ("future-date", {"date": "2099-01-01", "job_id": "seed-history"}),
        ("unpublished", {"collected_at": NOW + timedelta(days=1)}),
    ]:
        db.quotes.insert_one({**original, "_id": name, "job_id": name,
                             "data": {**original["data"], "close": "0.01", "adj_close": "0.01"}, **changes})
    assert_same_calculation(before, client.post("/goals/calibrate", json=request).json())
    seed_yahoo(db, generation="revision", collected_at=NOW + timedelta(hours=1), rows=[
        {**row["data"], **({"close": "0.01", "adj_close": "0.01"} if row["date"] == original["date"] else {})}
        for row in saved
    ])
    after = client.post("/goals/calibrate", json=request).json()
    assert after["history_fingerprint"] != before["history_fingerprint"]
    assert after["forecasts"] != before["forecasts"]
    assert after["data_as_of"] == before["data_as_of"] == md.public(NOW)
    assert any("not strict point-in-time" in warning for warning in after["warnings"])
    clock[0] += timedelta(hours=2)
    replay = client.post("/goals/calibrate", json=request).json()
    assert_same_calculation({**after, "data_as_of": md.public(clock[0])}, replay)
    assert replay["history_fingerprint"] == after["history_fingerprint"]


def test_strategy_specific_calibration_dispatch_and_unsupported_strategies(api, db, monkeypatch):
    client, _ = api
    login(client)
    definition = gs.get_strategy("buy_and_hold_single_instrument")
    seed_history(db, rows_between())
    seed_profile(db)
    unsupported = replace(definition, id="unsupported", calibrate=None)
    monkeypatch.setitem(gs.STRATEGIES, unsupported.id, unsupported)
    assert client.get("/goals/strategies").json()["items"][-1]["supports_calibration"] is False
    response = client.post("/goals/calibrate", json={**body(), "strategy": {
        "id": "unsupported", "parameters": {"instrument": ISIN},
    }})
    assert response.status_code == 422 and "not implemented" in response.json()["detail"]
    from random_calibration import evaluate_random_history
    calibrated = evaluate_random_history(rows_between(), RandomCalibrationInputs(mode="random"), seed=42)
    handler = Mock(return_value=calibrated)
    monkeypatch.setitem(gs.STRATEGIES, unsupported.id, replace(unsupported, calibrate=handler))
    request = {**body(), "strategy": {"id": "unsupported", "parameters": {"instrument": ISIN}}}
    response = client.post("/goals/calibrate", json=request)
    assert response.status_code == 200, response.text
    assert response.json() == {**calibrated, "strategy": request["strategy"], "calculated_at": md.public(NOW),
                               "id": response.json()["id"], "strategy_name": unsupported.name}
    parameters, scenario, database = handler.call_args.args
    assert isinstance(parameters, gs.BuyAndHoldParameters)
    assert isinstance(scenario, RandomCalibrationInputs) and database is db
