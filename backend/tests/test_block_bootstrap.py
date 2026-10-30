"""Empirical block draws and immutable planner replay contracts."""
from copy import deepcopy
from datetime import date, timedelta
import math
from statistics import fmean, variance

import numpy as np
import pytest

import market_data as md
from goal_simulation import GoalInputs, period_factors, simulate_buy_and_hold
from plan_tracking import track_plan
from return_statistics import calculate_daily_statistics, instrument_return_runs
from test_customer_planner import planner, calculate, save, ttl_clock
from test_goal_planner import goal_inputs, login, request_body, seed_history
from test_market_data import ISIN, NOW, api, clock, config, db, seed_yahoo


def test_blocks_preserve_contiguous_pairs_and_seeded_replay():
    rows = [{"date": day, "close": price} for day, price in [
        ("2026-09-01", 100), ("2026-09-02", 110), ("2026-09-03", 100),
        ("2026-09-04", 110), ("2026-09-07", 100),
        ("2026-09-09", 1000), ("2026-09-10", 1100), ("2026-09-11", 1000),
    ]]
    stats = calculate_daily_statistics(rows, include_return_runs=True)
    assert stats["return_count"] == 6
    assert [len(run) for run in stats["return_runs"]] == [4, 2]
    assert stats["skipped_gap_count"] == 1
    today = date(2026, 10, 1)
    steps = [(date(2026, 11, 1), True), (date(2026, 12, 1), True)]
    args = (steps, today, 0, 0, 32, 42)
    first = list(period_factors(*args, return_runs=stats["return_runs"], block_size=2))
    second = list(period_factors(*args, return_runs=stats["return_runs"], block_size=2))
    for (_, _, factors), (_, _, replay) in zip(first, second):
        np.testing.assert_array_equal(factors, replay)
        assert np.all(np.isfinite(factors))
    changed = list(period_factors(*args[:-1], 43, return_runs=stats["return_runs"], block_size=2))
    assert not np.array_equal(first[0][2], changed[0][2])
    with pytest.raises(md.DataError, match="uninterrupted"):
        list(period_factors(*args, return_runs=stats["return_runs"], block_size=5))


def test_bootstrap_solver_replays_trajectory_and_fixed_contribution():
    rows = [0.02, -0.01, 0.025, -0.03, 0.005] * 12
    estimates = {"daily_mean_log_return": fmean(rows), "daily_log_return_variance": variance(rows),
                 "return_count": len(rows), "warnings": []}
    today = date(2026, 9, 17)
    body = GoalInputs(**goal_inputs(goal_date="2027-09-17", simulation_method="block_bootstrap", block_size=5))
    first = simulate_buy_and_hold(estimates, body, today, paths=128, seed=41, return_runs=[rows])
    replay = simulate_buy_and_hold(estimates, body, today, paths=128, seed=41, return_runs=[rows],
                                   fixed_monthly=first["monthly_contribution_eur"])
    assert first["trajectory"] == replay["trajectory"]
    assert first["success_probability"] == replay["success_probability"]
    assert replay["fixed_contribution_success_probability"] == first["success_probability"]
    assert first["simulation_method"] == "block_bootstrap" and first["block_size"] == 5
    assert first["trajectory"][-1]["date"] == first["goal_date"]
    assert first["trajectory"][-1]["p50_eur"] == first["terminal_percentiles_eur"]["p50"]
    with pytest.raises(md.DataError, match="changed"):
        simulate_buy_and_hold(estimates, body, today, paths=8, return_runs=[[0.01]])
    with pytest.raises(md.DataError, match="changed"):
        simulate_buy_and_hold(estimates, body, today, paths=8, return_runs=[[0.01] * len(rows)])


@pytest.mark.parametrize("settings", [
    {"simulation_method": "block_bootstrap"}, {"block_size": 10},
    {"simulation_method": "standard", "block_size": 5},
    {"simulation_method": "block_bootstrap", "block_size": 0},
    {"simulation_method": "block_bootstrap", "block_size": 61},
    {"simulation_method": "block_bootstrap", "block_size": True},
    {"simulation_method": "block_bootstrap", "block_size": 1.5},
    {"simulation_method": "unknown", "block_size": 5},
])
def test_invalid_settings_rejected_before_data_access(api, db, settings):
    client, _ = api
    login(client)
    assert client.post("/goals/simulate", json=request_body(**settings)).status_code == 422
    assert not db.instrument_statistics.count_documents({})


def test_bootstrap_uses_split_corrected_eligible_history_and_snapshot_tracks(planner, db):
    client, customer, _, url = planner
    preview = calculate(client, url, simulation_method="block_bootstrap", block_size=10)
    assert preview["inputs"]["simulation_method"] == "block_bootstrap"
    assert preview["inputs"]["block_size"] == 10
    assert preview["result"]["simulation_method"] == "block_bootstrap"
    saved = save(client, url, preview)
    assert saved["result"] == preview["result"]
    assert client.post(f"{url}/simulations/{saved['id']}/plan").status_code == 200
    plan = db.planner_simulations.find_one({"_id": saved["id"]})
    before = deepcopy(plan)
    tracked = track_plan(db, plan, {"equity_eur": saved["inputs"]["starting_capital_eur"], "warnings": []}, NOW.date())
    assert tracked["simulation_method"] == "block_bootstrap" and tracked["block_size"] == 10
    assert tracked["success_probability"] == saved["result"]["success_probability"]
    assert tracked["required_monthly_contribution_eur"] == saved["result"]["monthly_contribution_eur"]
    assert db.planner_simulations.find_one({"_id": saved["id"]}) == before
    assert client.get(f"{url}/simulations/{saved['id']}").json()["inputs"]["block_size"] == 10
    assert client.get(url + "/simulations").json()["items"][0]["inputs"]["block_size"] == 10


def test_return_runs_use_latest_complete_history_with_quote_date_cutoff(db):
    seed_history(db)
    runs = instrument_return_runs(db, ISIN, as_of=NOW)
    assert sum(map(len, runs)) == 80
    assert len(runs) == 1
    assert all(math.isfinite(value) for value in runs[0])
    saved = list(db.quotes.find({"job_id": "seed-history"}).sort("date", 1))
    revised = {**saved[-1], "_id": "later-revision", "job_id": "unpublished-revision",
               "provider": "yfinance", "collected_at": NOW + timedelta(hours=1),
               "data": {**saved[-1]["data"], "close": "250", "adj_close": "250"}}
    db.quotes.insert_one(revised)
    assert instrument_return_runs(db, ISIN, as_of=NOW) == runs
    assert instrument_return_runs(db, ISIN, as_of=NOW + timedelta(hours=2)) == runs

    # Later publication revises past prices even for an earlier as_of.
    rows = [row["data"] for row in saved[:-1]] + [revised["data"]]
    rows.append({**revised["data"], "date": "2099-01-01", "close": "0.01", "adj_close": "0.01"})
    seed_yahoo(db, generation="published-revision", collected_at=NOW + timedelta(hours=1), rows=rows)
    latest = instrument_return_runs(db, ISIN, as_of=NOW)
    assert latest != runs
    assert sum(map(len, latest)) == 80
    assert instrument_return_runs(db, ISIN, as_of=NOW + timedelta(hours=2)) == latest
