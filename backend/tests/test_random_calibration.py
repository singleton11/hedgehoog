from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest.mock import Mock

import numpy as np
import pytest

import goal_strategies as gs
import market_data as md
import random_calibration as rc
from goal_calibration import RandomCalibrationInputs, calibration_metrics, monthly_origins, reaches_goal
from goal_simulation import add_months, money_string, period_factors
from return_statistics import calculate_statistics
from test_goal_calibration import assert_same_calculation, rows_between
from test_goal_planner import login, seed_history, seed_profile
from test_market_data import ISIN, api, clock, config, db


def inputs(**changes):
    return RandomCalibrationInputs(mode="random", **{"scenario_count": 100, "min_horizon_months": 3,
        "max_horizon_months": 12, "lookback_months": 12, "seed": 42, **changes})


def body(**changes):
    return {**inputs(**changes).model_dump(mode="json"), "strategy": {
        "id": "buy_and_hold_single_instrument", "parameters": {"instrument": ISIN},
    }}


def evaluate(rows=None, seed=42, paths=400, **changes):
    return rc.evaluate_random_history(rows if rows is not None else rows_between(), inputs(**changes), seed=seed, paths=paths)


def test_default_thousand_scenarios_span_probabilities_and_have_honest_origin_counts():
    result = rc.evaluate_random_history(rows_between(), RandomCalibrationInputs(mode="random"), seed=42)
    assert result["sample_count"] == result["candidate_count"] == 1000
    assert result["origin_count"] == result["available_origin_count"] == 32
    counts = Counter(f["origin_date"] for f in result["forecasts"])
    assert max(counts.values()) - min(counts.values()) <= 1
    assert all(bin["count"] > 0 for bin in result["calibration_bins"])
    assert sum(bin["count"] for bin in result["calibration_bins"]) == 1000
    assert result["generation"]["target_simulation_paths"] == result["simulation_paths"] == 10000
    assert result["settings"] == RandomCalibrationInputs(mode="random").public_settings()
    forecasts = result["forecasts"]
    assert len({f["scenario_id"] for f in forecasts}) == 1000
    assert len({f["horizon_months"] for f in forecasts}) > 10
    assert len({f["goal_eur"] for f in forecasts}) > 900
    assert all(Decimal("1000") <= Decimal(f["starting_capital_eur"]) <= Decimal("100000") for f in forecasts)
    assert all(Decimal("0") <= Decimal(f["monthly_contribution_eur"]) <= Decimal("1000") for f in forecasts)
    assert all(0.01 <= f["requested_probability"] <= 0.99 for f in forecasts)
    assert all(f["training_end_date"] == f["origin_date"] < f["goal_date"] <= result["history_last_date"] for f in forecasts)
    assert all(3 <= f["horizon_months"] <= 24 for f in forecasts)
    for key, value in calibration_metrics(forecasts).items():
        assert result[key] == value
    assert any("NOT independent" in warning for warning in result["warnings"])


def test_seed_replay_and_count_extension_preserve_existing_case_definitions():
    first = evaluate()
    assert first == evaluate()
    assert first["forecasts"] != evaluate(seed=43)["forecasts"]
    larger = evaluate(scenario_count=200)
    lookup = {f["scenario_id"]: f for f in larger["forecasts"]}
    assert all(f == lookup[f["scenario_id"]] for f in first["forecasts"])
    subset = evaluate(start_date="2023-03-01", end_date="2023-06-30")
    lookup = {f["scenario_id"]: f for f in subset["forecasts"]}
    common = [f for f in first["forecasts"] if f["scenario_id"] in lookup]
    assert common and all(f == lookup[f["scenario_id"]] for f in common)


def test_only_prefix_is_fitted_and_changing_future_prices_does_not_change_goals_or_predictions(monkeypatch):
    rows = rows_between()
    initial = deepcopy(rows)
    original = rc.calculate_statistics
    fitted = []

    def capture(training):
        assert all("2022-03-01" <= row["date"] <= "2023-03-01" for row in training)
        fitted.append(training)
        return original(training)

    monkeypatch.setattr(rc, "calculate_statistics", capture)
    baseline = evaluate(rows, start_date="2023-03-01", end_date="2023-03-01")
    assert len(fitted) == 1  # Refit per origin, not per random funding case.
    assert rows == initial
    for row in rows:
        if row["date"] > "2023-03-01":
            row["close"] *= 0.1
            row["adj_close"] *= 0.1
    changed = evaluate(rows, start_date="2023-03-01", end_date="2023-03-01")
    outcome_keys = {"terminal_value_eur", "observed_success"}
    assert [{k: v for k, v in f.items() if k not in outcome_keys} for f in baseline["forecasts"]] == [
        {k: v for k, v in f.items() if k not in outcome_keys} for f in changed["forecasts"]]
    assert [f["terminal_value_eur"] for f in baseline["forecasts"]] != [f["terminal_value_eur"] for f in changed["forecasts"]]


def test_goals_use_pilot_quantiles_and_actual_probabilities_use_independent_draws():
    paths, seed = 800, 42
    rows = rows_between()
    result = evaluate(rows, paths=paths, start_date="2023-03-01", end_date="2023-03-01")
    example = result["forecasts"][0]
    origin = date.fromisoformat(example["origin_date"])
    estimates = calculate_statistics([row for row in rows if example["training_start_date"] <= row["date"] <= example["training_end_date"]])
    wealth = []
    for domain in [rc.TARGET_STREAM, rc.PREDICTION_STREAM]:
        steps = [(add_months(origin, month), True) for month in range(1, example["horizon_months"] + 1)]
        growth, annuity = np.ones(paths), np.zeros(paths)
        stream = np.random.SeedSequence([seed, origin.toordinal(), domain])
        for _, _, factors in period_factors(steps, origin, estimates["daily_mean_log_return"], estimates["daily_log_return_variance"], paths, stream):
            growth *= factors
            annuity = annuity * factors + 1
        wealth.append(float(example["starting_capital_eur"]) * growth + float(example["monthly_contribution_eur"]) * annuity)
    expected_goal = money_string(float(np.quantile(wealth[0], 1 - example["requested_probability"])))
    assert example["goal_eur"] == expected_goal
    assert example["predicted_probability"] == float(np.mean(reaches_goal(wealth[1], Decimal(expected_goal))))
    assert example["predicted_probability"] != example["requested_probability"]
    assert not np.array_equal(wealth[0], wealth[1])


def test_changing_scoring_stream_cannot_change_scenario_goals_or_observed_outcomes(monkeypatch):
    before = evaluate()
    monkeypatch.setattr(rc, "PREDICTION_STREAM", 999)
    after = evaluate()
    assert any(a["predicted_probability"] != b["predicted_probability"] for a, b in zip(before["forecasts"], after["forecasts"]))
    assert [{k: v for k, v in f.items() if k != "predicted_probability"} for f in before["forecasts"]] == [
        {k: v for k, v in f.items() if k != "predicted_probability"} for f in after["forecasts"]]


def test_cases_share_two_streams_per_origin_not_one_simulation_per_case(monkeypatch):
    streams = []
    original = rc.period_factors

    def capture(*args):
        streams.append(args[-1].entropy)
        return original(*args)

    monkeypatch.setattr(rc, "period_factors", capture)
    result = evaluate(scenario_count=1000, paths=100)
    assert len(streams) == 2 * result["origin_count"]
    assert {stream[-1] for stream in streams} == {rc.TARGET_STREAM, rc.PREDICTION_STREAM}


def test_flat_prices_do_not_fabricate_probability_coverage_or_miss_cent_funding():
    result = evaluate(rows_between(flat=True))
    assert result["sample_count"] == 100
    assert result["mean_predicted_probability"] == result["observed_success_rate"] == 1
    assert result["brier_score"] == 0
    assert result["calibration_bins"][-1]["count"] == 100
    assert any("Only 1 of 10" in warning for warning in result["warnings"])
    for case in result["forecasts"]:
        exact = Decimal(case["starting_capital_eur"]) + case["horizon_months"] * Decimal(case["monthly_contribution_eur"])
        assert Decimal(case["goal_eur"]) == Decimal(case["terminal_value_eur"]) == exact
        assert 0.01 <= case["requested_probability"] < 1
        assert case["predicted_probability"] == 1


def test_future_invalid_adjusted_price_is_excluded_not_switched_or_replaced():
    rows = rows_between()
    settings = {"start_date": "2023-03-01", "end_date": "2023-03-01", "max_horizon_months": 6}
    before = evaluate(rows, **settings)
    last = max(f["valuation_date"] for f in before["forecasts"])
    next(row for row in rows if row["date"] == last)["adj_close"] = None
    after = evaluate(rows, **settings)
    assert 0 < after["sample_count"] < 100
    assert after["candidate_count"] == 100 == after["sample_count"] + sum(after["skipped_counts"].values())
    assert set(f["scenario_id"] for f in before["forecasts"]) == {f["scenario_id"] for f in after["forecasts"] + after["excluded_origins"]}
    assert all(f["price_basis"] == "adj_close" for f in after["forecasts"])
    lookup = {f["scenario_id"]: f for f in before["forecasts"]}
    assert all(f == lookup[f["scenario_id"]] for f in after["forecasts"])
    assert after["skipped_counts"]["missing_outcome_price"] > 0


def test_training_invalidity_does_not_reallocate_the_batch_using_future_quality():
    rows = rows_between()
    before = evaluate(rows)
    latest_origin = before["forecasts"][-1]["origin_date"]
    next(row for row in rows if row["date"] == latest_origin)["price_currency"] = "USD"
    after = evaluate(rows)
    assert after["sample_count"] < before["sample_count"]
    assert "training_currency" in after["skipped_counts"]
    assert {f["scenario_id"] for f in before["forecasts"]} == {f["scenario_id"] for f in after["forecasts"] + after["excluded_origins"]}
    assert after["forecasts"][0] == before["forecasts"][0]


def test_calendar_availability_never_assigns_incomplete_horizons():
    scenario = inputs(scenario_count=500, max_horizon_months=120)
    _, dates, origins = monthly_origins(rows_between(), scenario)
    groups, available = rc.plan_scenarios(dates, origins, scenario, 42)
    assert available == len(groups)
    assert sum(len(group) for group in groups.values()) == 500
    assert all(add_months(dates[index], case.horizon) <= dates[-1] for index, group in groups.items() for case in group)
    last = max(groups)
    assert max(case.horizon for case in groups[last]) < scenario.max_horizon_months


def test_work_cap_counts_both_streams_and_precedes_estimation(monkeypatch):
    monkeypatch.setattr(rc, "MAX_GENERATED_PERIODS", 1)
    forbidden = Mock(side_effect=AssertionError("No fitting before work limit"))
    monkeypatch.setattr(rc, "calculate_statistics", forbidden)
    with pytest.raises(md.DataError, match="30,000"):
        evaluate()
    forbidden.assert_not_called()


@pytest.mark.parametrize("rows", [[], rows_between(end=date(2022, 3, 1)),
    [{**row, "price_currency": "USD"} for row in rows_between()]])
def test_empty_short_or_ineligible_batches_have_no_invented_score(rows):
    with pytest.raises(md.DataError):
        evaluate(rows)


@pytest.mark.parametrize("level", [0, 1e12 + 1])
def test_unrepresentable_goals_are_not_silently_clipped(level):
    with pytest.raises(rc.UnscorableOutcome, match="generated_goal_out_of_bounds"):
        rc.generated_goal(np.full(100, level), 0.5)


def test_api_explicit_generated_batch_auth_seed_and_no_provider_calls(api, db, config, monkeypatch):
    client, publish = api
    seed_history(db, rows_between())
    seed_profile(db)
    assert client.post("/goals/calibrate", json=body()).status_code == 401
    login(client)
    assert client.post("/goals/calibrate", json=body(), headers={"Origin": "https://evil.example"}).status_code == 403
    provider = Mock(side_effect=AssertionError("No full-history fitting or provider requests"))
    monkeypatch.setattr(md, "MarketstackClient", provider)
    monkeypatch.setattr(gs, "instrument_statistics", provider)
    monkeypatch.setattr("goal_calibration.secrets.randbits", Mock(return_value=12345))
    generated = client.post("/goals/calibrate", json=body(seed=None))
    assert generated.status_code == 200, generated.text
    result = generated.json()
    assert result["seed"] == 12345
    assert result["strategy"] == body()["strategy"]
    assert result["settings"] == inputs().public_settings()
    assert result["sample_count"] == 100 and result["simulation_paths"] == 10000
    assert_same_calculation(result, client.post("/goals/calibrate", json=body(seed=12345)).json())
    assert client.post("/goals/calibrate", json=body(seed=0)).json()["seed"] == 0
    assert md.budget_status(db, config)["used"] == 0
    assert db.instrument_statistics.count_documents({}) == 0
    provider.assert_not_called()
    publish.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"scenario_count": 9}, {"scenario_count": 5001}, {"scenario_count": True}, {"scenario_count": "100"},
    {"scenario_count": 10.5}, {"min_horizon_months": 0}, {"max_horizon_months": 121},
    {"min_horizon_months": 12, "max_horizon_months": 6}, {"min_horizon_months": True},
    {"max_horizon_months": 6.5}, {"lookback_months": 2}, {"seed": -1}, {"seed": 2**32},
    {"seed": True}, {"start_date": "2025-01-01", "end_date": "2024-01-01"},
    {"mode": "something_else"}, {"goal_eur": "15000"}, {"simulation_paths": 10**9},
])
def test_api_invalid_generated_configuration_fails_before_reading_data(api, db, monkeypatch, changes):
    client, _ = api
    login(client)
    forbidden = Mock(side_effect=AssertionError("Invalid batch must not access instrument data"))
    monkeypatch.setattr(db.instruments, "find_one", forbidden)
    assert client.post("/goals/calibrate", json={**body(), **changes}).status_code == 422
    forbidden.assert_not_called()


def test_calibration_requires_strategy_handler_and_acquired_membership(api, db, monkeypatch):
    client, _ = api
    login(client)
    seed_history(db, rows_between())
    assert "acquired universe" in client.post("/goals/calibrate", json=body()).json()["detail"]
    seed_profile(db)
    original = gs.get_strategy("buy_and_hold_single_instrument")
    simulation = Mock(side_effect=AssertionError("Do not fall back to goal simulation"))
    monkeypatch.setitem(gs.STRATEGIES, original.id, replace(original, simulate=simulation, calibrate=None))
    assert client.get("/goals/strategies").json()["items"][0]["supports_calibration"] is False
    response = client.post("/goals/calibrate", json=body())
    assert response.status_code == 422 and "not implemented" in response.json()["detail"]
    simulation.assert_not_called()
