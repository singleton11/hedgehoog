from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal, ROUND_CEILING
from unittest.mock import Mock

import numpy as np
import pytest

import goal_strategies as gs
import market_data as md
import random_calibration as rc
from goal_calibration import RandomCalibrationInputs, calibration_metrics, monthly_origins
from goal_simulation import GoalInputs, add_months, money_string, period_factors, required_contribution, simulate_buy_and_hold
from return_statistics import calculate_daily_statistics, calculate_statistics
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
    assert result["generation"] == {
        "method": "independent_goals_contribution_solver", "version": 2, "solver_simulation_paths": 10000,
        "capital_min_eur": "1000.00", "capital_max_eur": "100000.00",
        "goal_max_eur": "1000000.00", "goal_rule": "above_starting_capital",
        "probability_min": 0.01, "probability_max": 0.99,
    }
    assert result["simulation_paths"] == 10000
    assert result["settings"] == RandomCalibrationInputs(mode="random").public_settings()
    forecasts = result["forecasts"]
    assert len({f["scenario_id"] for f in forecasts}) == 1000
    assert len({f["horizon_months"] for f in forecasts}) > 10
    assert len({f["goal_eur"] for f in forecasts}) > 900
    assert all(Decimal("1000") <= Decimal(f["starting_capital_eur"]) <= Decimal("100000") for f in forecasts)
    assert all(Decimal(f["starting_capital_eur"]) < Decimal(f["goal_eur"]) <= Decimal("1000000") for f in forecasts)
    assert all(Decimal(f["monthly_contribution_eur"]) >= 0 for f in forecasts)
    assert any(Decimal(f["monthly_contribution_eur"]) > 1000 for f in forecasts)
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


@pytest.mark.parametrize("method,block_size", [("standard", None), ("block_bootstrap", 10)])
def test_solved_funding_matches_planner_and_prediction_uses_independent_training_draws(method, block_size):
    paths, seed = 400, 42
    rows = rows_between()
    changes = {"simulation_method": method, "block_size": block_size,
               "start_date": "2023-03-01", "end_date": "2023-03-01"}
    result = evaluate(rows, seed=seed, paths=paths, **changes)
    assert result == evaluate(rows, seed=seed, paths=paths, **changes)
    assert result["generation"]["solver_simulation_paths"] == paths
    assert result["settings"]["block_size"] == block_size
    if block_size:
        assert any("both independent contribution-solver and prediction streams" in w for w in result["warnings"])
    # Cover low, middle and high requested reliabilities, not just the first case.
    ordered = sorted(result["forecasts"], key=lambda f: f["requested_probability"])
    assert ordered[0]["requested_probability"] < 0.1
    assert ordered[-1]["requested_probability"] > 0.9
    for case in (ordered[0], ordered[len(ordered) // 2], ordered[-1]):
        origin = date.fromisoformat(case["origin_date"])
        training = [row for row in rows if case["training_start_date"] <= row["date"] <= case["training_end_date"]]
        stats = calculate_daily_statistics(training, include_return_runs=True)
        runs = stats["return_runs"] if block_size else None
        planner_inputs = GoalInputs(goal_eur=case["goal_eur"], starting_capital_eur=case["starting_capital_eur"],
                                    goal_date=case["goal_date"], reliability=case["requested_probability"],
                                    simulation_method=method, block_size=block_size)
        planner = simulate_buy_and_hold(stats, planner_inputs, origin, paths=paths, return_runs=runs,
                                       seed=np.random.SeedSequence([seed, origin.toordinal(), rc.SOLVER_STREAM]))
        assert case["monthly_contribution_eur"] == planner["monthly_contribution_eur"]
        wealth = []
        for domain in (rc.SOLVER_STREAM, rc.PREDICTION_STREAM):
            steps = [(add_months(origin, month), True) for month in range(1, case["horizon_months"] + 1)]
            growth, annuity = np.ones(paths), np.zeros(paths)
            for _, _, factors in period_factors(steps, origin, stats["daily_mean_log_return"],
                                                 stats["daily_log_return_variance"], paths,
                                                 np.random.SeedSequence([seed, origin.toordinal(), domain]),
                                                 return_runs=runs, block_size=block_size):
                growth *= factors
                annuity = annuity * factors + 1
            if domain == rc.SOLVER_STREAM:
                monthly, terminal = required_contribution(growth, annuity, case["starting_capital_eur"],
                                                         case["goal_eur"], case["requested_probability"])
                assert money_string(monthly) == case["monthly_contribution_eur"]
                assert float(np.mean(terminal >= float(case["goal_eur"]))) == planner["success_probability"]
            wealth.append(float(case["starting_capital_eur"]) * growth + float(case["monthly_contribution_eur"]) * annuity)
        assert case["predicted_probability"] == float(np.mean(wealth[1] >= float(case["goal_eur"])))
        assert case["predicted_probability"] != case["requested_probability"]
        assert not np.array_equal(wealth[0], wealth[1])
    for row in rows:
        if row["date"] > "2023-03-01":
            row["close"] *= 0.1
            row["adj_close"] *= 0.1
    changed = evaluate(rows, seed=seed, paths=paths, **changes)
    assert [{k: v for k, v in f.items() if k not in {"terminal_value_eur", "observed_success"}}
            for f in result["forecasts"]] == [
        {k: v for k, v in f.items() if k not in {"terminal_value_eur", "observed_success"}}
        for f in changed["forecasts"]]


def test_bootstrap_excludes_origins_without_a_complete_training_block():
    rows = rows_between()
    # Split each 60-day training run with a missing weekday, leaving enough
    # individual returns for fitting but no eligible 60-day bootstrap window.
    for index, row in enumerate(rows):
        if index % 45 == 0:
            row["adj_close"] = '0'
            row["close"] = '0'
    with pytest.raises(md.DataError, match="insufficient block history"):
        evaluate(rows, simulation_method="block_bootstrap", block_size=60)


def test_changing_scoring_stream_cannot_change_scenario_goals_or_observed_outcomes(monkeypatch):
    before = evaluate()
    monkeypatch.setattr(rc, "PREDICTION_STREAM", 999)
    after = evaluate()
    assert any(a["predicted_probability"] != b["predicted_probability"] for a, b in zip(before["forecasts"], after["forecasts"]))
    assert [{k: v for k, v in f.items() if k != "predicted_probability"} for f in before["forecasts"]] == [
        {k: v for k, v in f.items() if k != "predicted_probability"} for f in after["forecasts"]]


@pytest.mark.parametrize("method,block_size", [("standard", None), ("block_bootstrap", 5)])
def test_cases_share_two_streams_per_origin_and_replay_solved_funding_without_charts(monkeypatch, method, block_size):
    streams, solved, replayed = [], [], []
    original = rc.period_factors
    solve, replay = rc.required_contribution, rc.realized_value

    def capture(*args, **kwargs):
        streams.append(args[-1].entropy)
        return original(*args, **kwargs)

    def capture_solve(growth, annuity, capital, goal, reliability):
        monthly, terminal = solve(growth, annuity, capital, goal, reliability)
        solved.append((capital, goal, monthly))
        return monthly, terminal

    def capture_replay(rows, dates, index, goal_date, basis, fixed, override):
        assert solved[-1] == (fixed.starting_capital_eur, fixed.goal_eur, fixed.monthly_contribution_eur)
        replayed.append(solved[-1])
        return replay(rows, dates, index, goal_date, basis, fixed, override)

    forbidden = Mock(side_effect=AssertionError("Calibration must not generate per-case charts"))
    monkeypatch.setattr("goal_simulation.trajectory_points", forbidden)
    monkeypatch.setattr(rc, "period_factors", capture)
    monkeypatch.setattr(rc, "required_contribution", capture_solve)
    monkeypatch.setattr(rc, "realized_value", capture_replay)
    result = evaluate(scenario_count=1000, paths=100, simulation_method=method, block_size=block_size)
    assert len(streams) == 2 * result["origin_count"]
    assert {stream[-1] for stream in streams} == {rc.SOLVER_STREAM, rc.PREDICTION_STREAM}
    assert len(solved) == len(replayed) == result["sample_count"] == 1000
    assert sorted(replayed) == sorted((Decimal(f["starting_capital_eur"]), Decimal(f["goal_eur"]),
                                       Decimal(f["monthly_contribution_eur"])) for f in result["forecasts"])
    forbidden.assert_not_called()


def test_flat_prices_do_not_fabricate_probability_coverage_or_miss_cent_funding():
    result = evaluate(rows_between(flat=True))
    assert result["sample_count"] == 100
    assert result["mean_predicted_probability"] == result["observed_success_rate"] == 1
    assert result["brier_score"] == 0
    assert result["calibration_bins"][-1]["count"] == 100
    assert any("Only 1 of 10" in warning for warning in result["warnings"])
    assert len({case["goal_eur"] for case in result["forecasts"]}) == 100
    assert len({case["requested_probability"] for case in result["forecasts"]}) == 100
    for case in result["forecasts"]:
        capital, goal, monthly = map(Decimal, (case["starting_capital_eur"], case["goal_eur"], case["monthly_contribution_eur"]))
        assert goal > capital
        exact = capital + case["horizon_months"] * monthly
        assert Decimal(case["terminal_value_eur"]) == exact >= goal
        minimum = ((goal - capital) / case["horizon_months"]).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        # The planner may add one cent for strict floating-point equality.
        assert minimum <= monthly <= minimum + Decimal("0.01")
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


@pytest.mark.parametrize("seed", [0, 42, 2**32 - 1])
def test_generator_is_method_and_price_independent_with_stable_common_inputs(seed):
    settings = inputs()
    _, dates, origins = monthly_origins(rows_between(), settings)
    baseline, _ = rc.plan_scenarios(dates, origins, settings, seed)
    expected = {case.id: case for group in baseline.values() for case in group}
    for changes in ({"simulation_method": "block_bootstrap", "block_size": 60},
                    {"scenario_count": 200}, {"start_date": "2023-03-01", "end_date": "2023-06-30"}):
        variant = inputs(**changes)
        # Even unusable prices cannot affect the generator's calendar inputs.
        changed_rows = [{**row, "close": None, "adj_close": -1} for row in rows_between()]
        _, changed_dates, changed_origins = monthly_origins(changed_rows, variant)
        groups, _ = rc.plan_scenarios(changed_dates, changed_origins, variant, seed)
        actual = {case.id: case for group in groups.values() for case in group}
        common = expected.keys() & actual.keys()
        assert common and all(expected[key] == actual[key] for key in common)
    for group in baseline.values():
        for case in group:
            assert Decimal("1000") <= case.capital <= Decimal("100000")
            assert case.capital + Decimal("0.01") <= case.goal <= Decimal("1000000")
            assert case.goal * 100 == int(case.goal * 100)
            assert not hasattr(case, "monthly")


def test_forecast_inputs_are_identical_across_methods_and_training_price_values():
    changes = {"start_date": "2023-03-01", "end_date": "2023-03-01"}
    standard = evaluate(**changes)
    bootstrap = evaluate(simulation_method="block_bootstrap", block_size=10, **changes)
    flat = evaluate(rows_between(flat=True), **changes)
    keys = ("scenario_id", "origin_date", "goal_date", "starting_capital_eur", "goal_eur",
            "requested_probability", "horizon_months")
    expected = [[f[key] for key in keys] for f in standard["forecasts"]]
    for result in (bootstrap, flat):
        assert [[f[key] for key in keys] for f in result["forecasts"]] == expected
        assert any(a["monthly_contribution_eur"] != b["monthly_contribution_eur"]
                   for a, b in zip(standard["forecasts"], result["forecasts"]))


def test_changing_solver_stream_changes_funding_not_generated_inputs(monkeypatch):
    before = evaluate()
    monkeypatch.setattr(rc, "SOLVER_STREAM", 999)
    after = evaluate()
    inputs_keys = ("scenario_id", "origin_date", "goal_date", "starting_capital_eur", "goal_eur",
                   "requested_probability", "horizon_months")
    assert [[f[key] for key in inputs_keys] for f in before["forecasts"]] == [
        [f[key] for key in inputs_keys] for f in after["forecasts"]]
    assert any(a["monthly_contribution_eur"] != b["monthly_contribution_eur"]
               for a, b in zip(before["forecasts"], after["forecasts"]))


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
    {"capital_min_eur": "0"}, {"goal_max_eur": "1000"}, {"monthly_max_eur": "1000"},
    {"simulation_method": "block_bootstrap", "block_size": None}, {"block_size": 5},
    {"simulation_method": "unknown"}, {"simulation_method": "block_bootstrap", "block_size": 0},
    {"simulation_method": "block_bootstrap", "block_size": 61},
    {"simulation_method": "block_bootstrap", "block_size": 1.5},
    {"simulation_method": "block_bootstrap", "block_size": True},
])
def test_api_invalid_generated_configuration_fails_before_reading_data(api, db, monkeypatch, changes):
    client, _ = api
    login(client)
    forbidden = Mock(side_effect=AssertionError("Invalid batch must not access instrument data"))
    monkeypatch.setattr(db.instruments, "find_one", forbidden)
    assert client.post("/goals/calibrate", json={**body(), **changes}).status_code == 422
    forbidden.assert_not_called()


@pytest.mark.parametrize("method,block_size", [("standard", None), ("block_bootstrap", 5)])
def test_api_saves_solved_forecasts_and_settings_for_history_report_and_replay(api, db, method, block_size):
    client, _ = api
    seed_history(db, rows_between())
    seed_profile(db)
    login(client)
    request = body(scenario_count=10, simulation_method=method, block_size=block_size)
    first_response = client.post("/goals/calibrate", json=request)
    assert first_response.status_code == 200, first_response.text
    first = first_response.json()
    assert first["settings"]["simulation_method"] == method
    assert first["settings"]["block_size"] == block_size
    assert first["generation"]["version"] == 2
    assert first["generation"]["method"] == "independent_goals_contribution_solver"
    assert all(Decimal(f["goal_eur"]) > Decimal(f["starting_capital_eur"]) for f in first["forecasts"])
    assert any(Decimal(f["monthly_contribution_eur"]) > 1000 for f in first["forecasts"])
    assert client.get(f"/goals/calibrations/{first['id']}").json()["result"] == first
    assert client.get("/goals/calibrations").json()["items"][0]["settings"] == first["settings"]
    assert_same_calculation(first, client.post("/goals/calibrate", json=request).json())


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
