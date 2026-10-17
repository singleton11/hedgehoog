from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import date, timedelta
from decimal import Decimal
import math
import json

import numpy as np
from statistics import mean, variance
from unittest.mock import Mock

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pymongo.errors import ConnectionFailure

import goal_strategies as gs
import market_data as md
from goal_planner import GoalRequest
from goal_simulation import GoalInputs, add_months, contribution_dates, required_contribution, simulate_buy_and_hold
from return_statistics import MODEL_VERSION, calculate_statistics, instrument_statistics
from test_market_data import ISIN, LISTING, NOW, Provider, api, bar, clock, collect, config, db, enqueue, page


def history(count=81, currency="EUR"):
    days = []
    day = NOW.date() - timedelta(days=2)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return [{**bar(day.isoformat(), str(100 * math.exp(i * 0.0001 + (i % 2) * 0.01))),
             "price_currency": currency} for i, day in enumerate(reversed(days))]


def seed_history(db, rows=None):
    rows = rows if rows is not None else history()
    db.instrument_state.update_one({"_id": ISIN}, {"$set": {"listing": LISTING}}, upsert=True)
    for index, row in enumerate(rows):
        db.quotes.insert_one({"instrument_id": ISIN, **LISTING, "date": row["date"][:10], "data": row,
                              "job_id": f"seed-{index}", "collected_at": NOW, "collection_date": NOW.date().isoformat()})


def seed_profile(db):
    db.instruments.insert_one({"instrument_id": ISIN, **LISTING, "data": {"name": "Synthetic ETF"},
                               "job_id": "seed-profile", "collected_at": NOW})


def goal_inputs(**changes):
    return {"goal_eur": "100000.00", "starting_capital_eur": "10000.00",
            "goal_date": "2036-09-17", "reliability": 0.9, **changes}


def request_body(**changes):
    return {**goal_inputs(), "strategy": {"id": "buy_and_hold_single_instrument",
                                         "parameters": {"instrument": ISIN}}, **changes}


def flat_estimates(m=0.0, v=0.0):
    return {"daily_mean_log_return": m, "daily_log_return_variance": v, "warnings": []}


def login(client):
    assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200


def test_daily_log_mean_and_sample_variance_not_simple_returns_or_price_levels():
    rows = [{"date": f"2026-09-{day:02}", "close": raw, "adj_close": adjusted}
            for day, raw, adjusted in [(1, 100, "10"), (2, 200, "11"), (3, 100, "9.9")]]
    stats = calculate_statistics(rows)
    assert stats["price_basis"] == "adj_close"
    assert stats["return_count"] == 2
    assert "daily_mean_return" not in stats
    assert "daily_return_variance" not in stats
    logs = [math.log(1.1), math.log(0.9)]
    assert stats["daily_mean_log_return"] == pytest.approx(mean(logs))
    assert stats["daily_mean_log_return"] < 0  # +10%, then -10% loses value.
    assert stats["daily_log_return_variance"] == pytest.approx(variance(logs))
    assert stats["daily_volatility"] == pytest.approx(math.sqrt(variance(logs)))
    assert stats["annualized_mean_log_return"] == pytest.approx(mean(logs) * 252)
    assert stats["annualized_volatility"] == pytest.approx(stats["daily_volatility"] * math.sqrt(252))


def test_partial_adjustments_fall_back_for_entire_series():
    stats = calculate_statistics([
        {"date": "2026-09-01", "close": "100", "adj_close": "50"},
        {"date": "2026-09-02", "close": "110", "adj_close": None},
        {"date": "2026-09-03", "close": "121", "adj_close": "60.5"},
    ])
    assert stats["price_basis"] == "close"
    assert stats["daily_mean_log_return"] == pytest.approx(math.log(1.1))
    assert stats["annualized_mean_log_return"] == pytest.approx(math.log(1.1) * 252)
    assert stats["daily_log_return_variance"] == pytest.approx(0)
    assert stats["daily_volatility"] == pytest.approx(0)
    assert "unadjusted" in stats["warnings"][0]


@pytest.mark.parametrize("invalid", [None, 0, -1, "NaN", "Infinity", True, {}, [], "missing"])
def test_invalid_prices_break_chain_and_do_not_change_raw_rows(invalid):
    rows = [{"date": f"2026-09-{i + 1:02}", "close": price} for i, price in enumerate([100, invalid, 110, 121])]
    original = deepcopy(rows)
    stats = calculate_statistics(rows)
    assert rows == original
    assert stats["invalid_price_count"] == 1
    assert stats["price_count"] == 3 and stats["return_count"] == 1
    assert stats["daily_mean_log_return"] == pytest.approx(math.log(1.1))
    assert stats["daily_log_return_variance"] is None


def test_long_gaps_are_not_daily_returns_and_weekends_are_allowed():
    stats = calculate_statistics([{"date": day, "close": price} for day, price in [
        ("2026-09-04", 100), ("2026-09-07", 110), ("2026-09-16", 200), ("2026-09-17", 220)]])
    assert stats["skipped_gap_count"] == 1
    assert stats["return_count"] == 2
    assert stats["daily_mean_log_return"] == pytest.approx(math.log(1.1))


def test_weekly_prices_are_never_annualized_as_daily_returns(db):
    first_friday = date(2025, 1, 3)
    rows = [{**bar((first_friday + timedelta(weeks=i)).isoformat(), str(100 * 1.001 ** i)),
             "price_currency": "EUR"} for i in range(61)]
    seed_history(db, rows)
    stats = instrument_statistics(db, ISIN)
    assert stats["quote_count"] == 61
    assert stats["return_count"] == 0
    assert stats["skipped_gap_count"] == 60
    assert stats["daily_mean_log_return"] is None
    assert not stats["eligible"]


def test_missing_weekdays_and_weekend_rows_do_not_distort_daily_stats():
    rows = [{"date": day, "adj_close": price} for day, price in [
        ("2026-09-04", 100), ("2026-09-05", 500), ("2026-09-06", 600),
        ("2026-09-07", 110), ("2026-09-09", 200), ("2026-09-10", 220)]]
    stats = calculate_statistics(rows)
    assert stats["weekend_quote_count"] == 2
    assert stats["skipped_gap_count"] == 1
    assert stats["return_count"] == 2
    assert stats["daily_mean_log_return"] == pytest.approx(math.log(1.1))
    assert stats["daily_log_return_variance"] == pytest.approx(0)


def test_log_returns_add_over_time_and_round_trip_has_zero_mean():
    stats = calculate_statistics([
        {"date": "2026-09-01", "adj_close": 100},
        {"date": "2026-09-02", "adj_close": 110},
        {"date": "2026-09-03", "adj_close": 100},
    ])
    assert stats["daily_mean_log_return"] == pytest.approx(0, abs=1e-15)
    assert stats["daily_log_return_variance"] == pytest.approx(2 * math.log(1.1) ** 2)
    assert math.exp(stats["daily_mean_log_return"] * stats["return_count"]) == pytest.approx(1)


@pytest.mark.parametrize("prices", [[], [100], [100, 100], [100, 100, 100]])
def test_empty_single_price_and_flat_history_log_statistics(prices):
    stats = calculate_statistics([{"date": f"2026-09-0{i + 1}", "adj_close": p}
                                  for i, p in enumerate(prices)])
    assert stats["return_count"] == max(0, len(prices) - 1)
    assert stats["daily_mean_log_return"] == (0 if len(prices) >= 2 else None)
    assert stats["annualized_mean_log_return"] == (0 if len(prices) >= 2 else None)
    assert stats["daily_log_return_variance"] == (0 if len(prices) >= 3 else None)
    assert stats["daily_volatility"] == (0 if len(prices) >= 3 else None)
    assert stats["annualized_volatility"] == (0 if len(prices) >= 3 else None)


@pytest.mark.parametrize("factor, warned", [
    (1.49, False), (1.51, True), (0.51, False), (0.49, True),
    (math.exp(0.45), True), (math.exp(-0.6), False),
])
def test_large_price_move_warning_uses_equivalent_log_thresholds(factor, warned):
    stats = calculate_statistics([
        {"date": "2026-09-01", "adj_close": 100},
        {"date": "2026-09-02", "adj_close": 100 * factor},
    ])
    assert any("greater than 50%" in warning for warning in stats["warnings"]) is warned


def test_extreme_prices_keep_finite_log_returns_even_when_price_ratios_overflow():
    prices = [1e-300, 1e300, 1e-300, 1e308]
    stats = calculate_statistics([{"date": f"2026-09-0{i + 1}", "close": p}
                                  for i, p in enumerate(prices)])
    logs = [math.log(current) - math.log(previous) for previous, current in zip(prices, prices[1:])]
    assert stats["return_count"] == 3
    assert stats["daily_mean_log_return"] == pytest.approx(mean(logs))
    assert stats["daily_log_return_variance"] == pytest.approx(variance(logs))
    assert stats["annualized_volatility"] == pytest.approx(math.sqrt(variance(logs) * 252))
    json.dumps(stats, allow_nan=False)
    assert any("greater than 50%" in warning for warning in stats["warnings"])
    with pytest.raises(md.DataError, match="unstable"):
        simulate_buy_and_hold(stats, GoalInputs(**goal_inputs()), NOW.date())


def test_successful_download_persists_stats_and_updates_recompute(db, config, clock):
    rows = history()
    result, source = collect(db, config, Provider([page(rows)]))
    assert result["status"] == "succeeded"
    assert len(source.calls) == 3
    stored = db.instrument_statistics.find_one({"_id": ISIN})
    assert stored["statistics"]["return_count"] == len(rows) - 1
    before = instrument_statistics(db, ISIN)
    assert before["eligible"] and before["price_currency"] == "EUR"  # Fund is USD.
    clock[0] += timedelta(hours=1)
    result, source = collect(db, config, Provider([page([{**bar("2026-09-16", "100"), "price_currency": "EUR"}])]))
    assert result["status"] == "succeeded"
    after = instrument_statistics(db, ISIN)
    assert after["return_count"] == before["return_count"] + 1
    assert after["computed_at"] > before["computed_at"]
    assert after["daily_mean_log_return"] != before["daily_mean_log_return"]


def test_statistics_db_failure_resumes_without_provider_calls(db, config, clock, monkeypatch):
    job = enqueue(db, config)
    source = Provider([page(history())])
    replace = db.instrument_statistics.replace_one
    monkeypatch.setattr(db.instrument_statistics, "replace_one", Mock(side_effect=ConnectionFailure("temporary")))
    with pytest.raises(ConnectionFailure):
        md.run_acquisition(db, config, job["id"], source.factory)
    assert db.instrument_state.find_one({"_id": ISIN}).get("last_complete_quote_date") is None
    monkeypatch.setattr(db.instrument_statistics, "replace_one", replace)
    clock[0] += timedelta(seconds=md.LEASE_SECONDS + 1)
    result = md.run_acquisition(db, config, job["id"], source.factory)
    assert result["status"] == "succeeded"
    assert len(source.calls) == 3
    assert instrument_statistics(db, ISIN)["return_count"] == 80


def test_lazy_stats_latest_observation_listing_and_cache_invalidation(db, clock):
    seed_history(db)
    original_quotes = list(db.quotes.find())
    first = instrument_statistics(db, ISIN)
    assert first["return_count"] == 80
    assert list(db.quotes.find()) == original_quotes
    clock[0] += timedelta(hours=1)
    assert instrument_statistics(db, ISIN)["computed_at"] == first["computed_at"]
    original = original_quotes[-1]
    revised = {**original, "_id": "revision", "job_id": "revision", "collected_at": clock[0],
               "data": {**original["data"], "close": "200", "adj_close": "200"}}
    db.quotes.insert_one(revised)
    # These must not contaminate the selected listing/history/knowledge time.
    db.quotes.insert_one({**revised, "_id": "wrong-listing", "symbol": "OTHER", "date": "2026-09-16"})
    db.quotes.insert_one({**revised, "_id": "future-date", "date": "2099-01-01"})
    db.quotes.insert_one({**revised, "_id": "future-knowledge", "collected_at": clock[0] + timedelta(days=1)})
    second = instrument_statistics(db, ISIN)
    assert second["quote_count"] == 81
    assert second["computed_at"] > first["computed_at"]
    assert second["daily_mean_log_return"] != first["daily_mean_log_return"]
    assert second["daily_mean_log_return"] == pytest.approx(calculate_statistics([
        {**row["data"], "date": row["date"]} for row in original_quotes[:-1]] +
        [{**revised["data"], "date": revised["date"]}])["daily_mean_log_return"])


@pytest.mark.parametrize("version", [2, 3, 4, 5])
def test_old_statistics_cache_is_recomputed_without_changing_quotes(db, version):
    seed_history(db)
    expected = instrument_statistics(db, ISIN)
    original_quotes = list(db.quotes.find())
    db.instrument_statistics.update_one({"_id": ISIN}, {"$set": {
        "signature.version": version,
        "statistics.daily_mean_return": 0.1,
        "statistics.daily_return_variance": 0.02,
        "statistics.daily_mean_log_return": 999,
    }, "$unset": {"statistics.daily_volatility": "", "statistics.annualized_mean_log_return": "",
                  "statistics.historical_year_return": "", "statistics.historical_year_start_date": "",
                  "statistics.historical_year_end_date": "", "statistics.historical_year_price_basis": "",
                  "statistics.historical_year_volatility": "", "statistics.historical_year_volatility_start_date": "",
                  "statistics.historical_year_volatility_end_date": "", "statistics.historical_year_volatility_price_basis": "",
                  "statistics.historical_year_volatility_return_count": ""}})
    assert instrument_statistics(db, ISIN) == expected
    saved = db.instrument_statistics.find_one({"_id": ISIN})
    assert saved["signature"]["version"] == MODEL_VERSION > version
    assert "daily_mean_return" not in saved["statistics"]
    assert "daily_return_variance" not in saved["statistics"]
    assert list(db.quotes.find()) == original_quotes


@pytest.mark.parametrize("currency,override,eligible", [
    ("EUR", None, True), ("USD", None, False), (None, None, False),
    (None, "EUR", True), ("USD", "EUR", False), ("EUR", "USD", False), ("EUR", "EUR", True),
])
def test_eur_enforcement_never_uses_fund_currency_or_relabels_usd(db, currency, override, eligible):
    seed_history(db, history(currency=currency))
    if override:
        md.set_price_currency(db, ISIN, override)
    stats = instrument_statistics(db, ISIN)
    assert stats["eligible"] is eligible
    if eligible:
        assert stats["price_currency"] == "EUR"
    else:
        assert "EUR" in stats["ineligible_reason"]


def test_currency_edits_and_active_jobs_revalidate_cached_estimates(db, config):
    seed_history(db, history(currency=None))
    assert not instrument_statistics(db, ISIN)["eligible"]
    md.set_price_currency(db, ISIN, "EUR")
    assert instrument_statistics(db, ISIN)["eligible"]
    job = enqueue(db, config)
    assert "Wait" in instrument_statistics(db, ISIN)["ineligible_reason"]
    db.acquisition_jobs.update_one({"_id": job["id"]}, {"$set": {"active": False, "status": "failed"}})
    assert instrument_statistics(db, ISIN)["eligible"]


def test_mixed_currency_and_short_or_empty_history_are_ineligible(db):
    assert "Download" in instrument_statistics(db, ISIN)["ineligible_reason"]
    rows = history(count=10)
    seed_history(db, rows)
    assert "60" in instrument_statistics(db, ISIN)["ineligible_reason"]
    db.quotes.delete_many({})
    rows = history()
    rows[-1]["price_currency"] = "USD"
    seed_history(db, rows)
    assert not instrument_statistics(db, ISIN)["eligible"]
    assert any("mixed" in message for message in instrument_statistics(db, ISIN)["warnings"])


def test_month_end_schedule_leap_years_and_partial_final_period():
    today = date(2028, 1, 31)
    assert contribution_dates(today, date(2028, 4, 15)) == [date(2028, 2, 29), date(2028, 3, 31)]
    assert add_months(date(2028, 2, 29), 12) == date(2029, 2, 28)
    assert len(contribution_dates(today, add_months(today, 600))) == 600
    for goal in (today, date(2028, 2, 28), add_months(today, 600) + timedelta(days=1)):
        with pytest.raises(md.DataError):
            contribution_dates(today, goal)


def test_zero_return_known_monthly_solution_and_rounds_up():
    body = GoalInputs(**goal_inputs(goal_eur="10000", starting_capital_eur="0", goal_date="2027-09-17"))
    result = simulate_buy_and_hold(flat_estimates(), body, NOW.date())
    assert "statistics" not in result and "parameter_statistics" not in result
    assert result["monthly_contribution_eur"] == "833.34"
    assert result["total_contributions_eur"] == "10000.08"
    assert result["terminal_percentiles_eur"] == {"p05": "10000.08", "p50": "10000.08", "p95": "10000.08"}
    assert result["contribution_count"] == 12 and result["success_probability"] == 1
    assert result["zero_contribution_success_probability"] == 0


@pytest.mark.parametrize("reliability", [0.01, 0.1, 0.499, 0.5, 0.99])
@pytest.mark.parametrize("goal,annuity,expected", [
    ("0.44", 3, "0.15"), ("0.45", 3, "0.16"),  # 0.15 * 3 < 0.45 in binary floats.
    ("0.46", 3, "0.16"), ("10000.00", 12, "833.34"),
])
def test_shared_solver_cent_rounding_and_strict_equality_at_all_reliabilities(reliability, goal, annuity, expected):
    growth, payments = np.ones(100), np.full(100, float(annuity))
    monthly, terminal = required_contribution(growth, payments, Decimal("0"), Decimal(goal), reliability)
    assert monthly == Decimal(expected)
    np.testing.assert_array_equal(terminal, float(monthly) * payments)
    assert np.all(terminal >= float(goal))
    body = GoalInputs(**goal_inputs(goal_eur=goal, starting_capital_eur="0", reliability=reliability,
                                   goal_date=add_months(NOW.date(), annuity).isoformat()))
    planner = simulate_buy_and_hold(flat_estimates(), body, NOW.date(), paths=100)
    assert planner["monthly_contribution_eur"] == expected
    assert planner["success_probability"] == 1


@pytest.mark.parametrize("reliability", [0.01, 0.02, 0.499, 0.5, 0.99])
def test_shared_solver_uses_ceil_rank_without_interpolation(reliability):
    growth, annuity = np.arange(100, dtype=float), np.ones(100)
    monthly, terminal = required_contribution(growth, annuity, 1, 100, reliability)
    assert monthly == Decimal(math.ceil(reliability * 100))
    assert np.count_nonzero(terminal >= 100) == math.ceil(reliability * 100)
    # Low requested reliability can legitimately need no contribution.
    monthly, terminal = required_contribution(growth, annuity, 1, 50, reliability)
    if reliability <= 0.5:
        assert monthly == 0


@pytest.mark.parametrize("reliability", [0.01, 0.1, 0.499, 0.99])
def test_api_accepts_full_one_to_ninety_nine_percent_reliability(api, db, reliability):
    client, _ = api
    seed_history(db)
    seed_profile(db)
    login(client)
    response = client.post("/goals/simulate", json=request_body(reliability=reliability))
    assert response.status_code == 200, response.text
    assert response.json()["target_probability"] == reliability
    assert response.json()["success_probability"] >= reliability


def test_zero_monthly_only_when_initial_capital_alone_meets_probability():
    body = GoalInputs(**goal_inputs(goal_eur="10000", starting_capital_eur="10000"))
    result = simulate_buy_and_hold(flat_estimates(), body, NOW.date())
    assert result["monthly_contribution_eur"] == "0.00"
    assert result["zero_contribution_success_probability"] == 1
    # Already holding the nominal target does not guarantee preserving it.
    risky = simulate_buy_and_hold(flat_estimates(m=-0.0002, v=0.0004), body, NOW.date())
    assert Decimal(risky["monthly_contribution_eur"]) > 0


def test_positive_and_negative_deterministic_growth_and_end_period_deposits():
    today = date(2026, 1, 31)
    for annual_log_growth in (-0.1, 0.1):
        body = GoalInputs(**goal_inputs(goal_eur="10000", starting_capital_eur="1000", goal_date="2026-04-15"))
        result = simulate_buy_and_hold(flat_estimates(m=annual_log_growth / 252), body, today)
        terminal_start = 1000 * math.exp(annual_log_growth * (body.goal_date - today).days / 365.25)
        annuity = sum(math.exp(annual_log_growth * (body.goal_date - d).days / 365.25)
                      for d in [date(2026, 2, 28), date(2026, 3, 31)])
        expected = math.ceil((10000 - terminal_start) / annuity * 100) / 100
        assert float(result["monthly_contribution_eur"]) == expected
        assert result["contribution_count"] == 2
        assert result["success_probability"] == 1


def test_simulation_reproducible_probability_met_and_monotone_inputs():
    estimates = flat_estimates(m=0.0001, v=0.0002)
    body = GoalInputs(**goal_inputs())
    result = simulate_buy_and_hold(estimates, body, NOW.date())
    assert simulate_buy_and_hold(estimates, body, NOW.date()) == result
    amount = Decimal(result["monthly_contribution_eur"])
    assert result["success_probability"] >= 0.9
    assert result["success_probability"] < 0.905
    for changes, larger in [({"reliability": 0.99}, True), ({"goal_eur": "200000"}, True),
                            ({"starting_capital_eur": "20000"}, False), ({"reliability": 0.5}, False)]:
        changed = simulate_buy_and_hold(estimates, GoalInputs(**goal_inputs(**changes)), NOW.date())
        assert (Decimal(changed["monthly_contribution_eur"]) > amount) is larger
        assert changed["success_probability"] >= changed["target_probability"]


def test_zero_variance_trajectory_is_one_line_with_initial_capital_and_end_period_deposits():
    body = GoalInputs(**goal_inputs(goal_eur="10000", starting_capital_eur="1000", goal_date="2027-09-17"))
    result = simulate_buy_and_hold(flat_estimates(), body, NOW.date())
    assert result["goal_eur"] == "10000.00"
    assert len(result["trajectory"]) == 13
    for index, point in enumerate(result["trajectory"]):
        expected = format(Decimal("1000") + Decimal(result["monthly_contribution_eur"]) * index, ".2f")
        assert point["date"] == add_months(NOW.date(), index).isoformat()
        assert set(value for key, value in point.items() if key != "date") == {expected}


def test_trajectory_partial_month_does_not_add_an_extra_payment():
    today = date(2028, 1, 31)
    body = GoalInputs(**goal_inputs(goal_eur="10000", starting_capital_eur="0", goal_date="2028-04-15"))
    result = simulate_buy_and_hold(flat_estimates(), body, today)
    assert result["contribution_count"] == 2
    points = result["trajectory"]
    assert [point["date"] for point in points] == ["2028-01-31", "2028-02-29", "2028-03-31", "2028-04-15"]
    assert [point["mean_eur"] for point in points] == ["0.00", "5000.00", "10000.00", "10000.00"]


def test_cloud_and_mean_use_all_original_paths_with_the_recommended_contribution():
    paths, seed = 512, 123
    body = GoalInputs(**goal_inputs(goal_date="2027-01-22"))
    estimates = flat_estimates(m=0.0001, v=0.0002)
    result = simulate_buy_and_hold(estimates, body, NOW.date(), paths=paths, seed=seed)
    rng = np.random.default_rng(seed)
    wealth = np.full(paths, float(body.starting_capital_eur))
    monthly = float(result["monthly_contribution_eur"])
    deposit_days = set(contribution_dates(NOW.date(), body.goal_date))
    previous = NOW.date()
    for point in result["trajectory"]:
        day = date.fromisoformat(point["date"])
        if day != previous:
            time = 252 * (day - previous).days / 365.25
            wealth *= np.exp(rng.normal(0.0001 * time, math.sqrt(0.0002 * time), paths))
            if day in deposit_days:
                wealth += monthly
        assert float(point["mean_eur"]) == pytest.approx(float(np.mean(wealth)), abs=0.0051, rel=0)
        percentiles = [float(point[f"{name}_eur"]) for name in ("p05", "p25", "p50", "p75", "p95")]
        assert percentiles == sorted(percentiles)
        assert percentiles == pytest.approx(np.quantile(wealth, [0.05, 0.25, 0.5, 0.75, 0.95], method="higher"), abs=0.0051, rel=0)
        previous = day
    terminal = result["trajectory"][-1]
    assert {key: terminal[f"{key}_eur"] for key in ("p05", "p50", "p95")} == result["terminal_percentiles_eur"]
    assert float(np.mean(wealth >= float(body.goal_eur))) == result["success_probability"]
    assert result == simulate_buy_and_hold(estimates, body, NOW.date(), paths=paths, seed=seed)


def test_maximum_horizon_returns_bounded_monthly_summaries_not_all_traces():
    body = GoalInputs(**goal_inputs(goal_date=add_months(NOW.date(), 600).isoformat()))
    result = simulate_buy_and_hold(flat_estimates(m=0.0001, v=0.0002), body, NOW.date())
    assert result["simulation_paths"] == 10000
    assert len(result["trajectory"]) == 601
    assert result["trajectory"][0]["date"] == result["valuation_date"]
    assert result["trajectory"][-1]["date"] == result["goal_date"]
    assert len(json.dumps(result)) < 200_000
    assert all(set(point) == {"date", "mean_eur", "p05_eur", "p25_eur", "p50_eur", "p75_eur", "p95_eur"} for point in result["trajectory"])


def test_extreme_simulation_rejected_not_silently_clipped():
    with pytest.raises(md.DataError, match="unstable"):
        simulate_buy_and_hold(flat_estimates(m=1000, v=1), GoalInputs(**goal_inputs()), NOW.date())


def test_historical_simulation_excludes_later_quote_publications(api, db, clock):
    client, _ = api
    seed_history(db)
    seed_profile(db)
    cutoff = NOW - timedelta(days=1)
    # The history was already known by this historical origin.
    db.quotes.update_many({}, {"$set": {"collected_at": cutoff - timedelta(hours=1)}})
    login(client)
    body = request_body(as_of=cutoff.isoformat())
    before = client.post("/goals/simulate", json=body)
    assert before.status_code == 200, before.text
    baseline = before.json()
    assert baseline["data_as_of"] == cutoff.isoformat().replace("+00:00", "Z")
    assert baseline["parameter_statistics"]["instrument"]["as_of"] == cutoff.isoformat().replace("+00:00", "Z")
    assert baseline["parameter_statistics"]["instrument"]["quote_count"] == 81
    assert db.instrument_statistics.find_one({"_id": ISIN}) is None

    latest = db.quotes.find_one(sort=[("date", -1)])
    revision = {key: value for key, value in latest.items() if key != "_id"}
    revision.update(job_id="later-revision", collected_at=NOW,
                    data={**latest["data"], "close": "999999", "adj_close": "999999"})
    db.quotes.insert_one(revision)
    future = {key: value for key, value in latest.items() if key != "_id"}
    future.update(job_id="later-date", date=(cutoff.date() + timedelta(days=1)).isoformat(),
                  collected_at=NOW, data={**latest["data"], "date": (cutoff.date() + timedelta(days=1)).isoformat()})
    db.quotes.insert_one(future)

    after = client.post("/goals/simulate", json=body)
    assert after.status_code == 200, after.text
    assert after.json() == baseline
    assert db.instrument_statistics.find_one({"_id": ISIN}) is None


def test_current_simulation_shares_one_cutoff_across_utc_midnight(api, db, clock, monkeypatch):
    client, _ = api
    seed_history(db)
    seed_profile(db)
    before_midnight = NOW.replace(hour=23, minute=59, second=59)
    after_midnight = before_midnight + timedelta(seconds=2)
    latest = db.quotes.find_one(sort=[("date", -1)])
    revision = {key: value for key, value in latest.items() if key != "_id"}
    revision.update(job_id="next-day-revision", collected_at=after_midnight,
                    data={**latest["data"], "close": "999999", "adj_close": "999999"})
    db.quotes.insert_one(revision)
    clock_calls = []

    def cross_midnight():
        clock_calls.append(None)
        return before_midnight if len(clock_calls) == 1 else after_midnight

    monkeypatch.setattr(md, "utcnow", cross_midnight)
    login(client)
    response = client.post("/goals/simulate", json=request_body())
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["valuation_date"] == before_midnight.date().isoformat()
    assert result["data_as_of"] == before_midnight.isoformat().replace("+00:00", "Z")
    assert result["parameter_statistics"]["instrument"]["as_of"] == before_midnight.isoformat().replace("+00:00", "Z")
    assert result["parameter_statistics"]["instrument"]["quote_count"] == 81
    assert len(clock_calls) > 1


def test_api_statistics_and_simulation_auth_origin_and_no_provider_calls(api, db, config, monkeypatch):
    client, publish = api
    seed_history(db)
    seed_profile(db)
    provider = Mock(side_effect=AssertionError("No provider call"))
    monkeypatch.setattr(md, "MarketstackClient", provider)
    url = f"/instruments/{ISIN}/statistics"
    assert client.get(url).status_code == 401
    assert client.post("/goals/simulate", json=request_body()).status_code == 401
    login(client)
    assert client.post("/goals/simulate", json=request_body(), headers={"Origin": "https://evil.example"}).status_code == 403
    stats = client.get(url).json()
    assert stats["eligible"]
    expected = calculate_statistics([{**row, "date": row["date"][:10]} for row in history()])
    for key in ("daily_mean_log_return", "daily_log_return_variance", "daily_volatility",
                "annualized_mean_log_return", "annualized_volatility"):
        assert stats[key] == pytest.approx(expected[key])
    assert "daily_mean_return" not in stats and "daily_return_variance" not in stats
    response = client.post("/goals/simulate", json=request_body())
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["simulation_paths"] == 10000
    assert result["success_probability"] >= 0.9
    assert result["strategy"] == request_body()["strategy"]
    assert result["parameter_statistics"] == {"instrument": stats}
    assert "statistics" not in result and "instrument_id" not in result
    assert isinstance(result["monthly_contribution_eur"], str)
    assert len(result["monthly_contribution_eur"].split(".")[1]) == 2
    assert result == client.post("/goals/simulate", json=request_body()).json()
    assert client.get("/instruments/US0378331005/statistics").status_code == 404
    assert client.get("/instruments/invalid/statistics").status_code == 422
    assert md.budget_status(db, config)["used"] == 0
    assert db.acquisition_jobs.count_documents({}) == 0
    provider.assert_not_called()
    publish.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"goal_eur": "0"}, {"goal_eur": "-1"}, {"goal_eur": "nan"}, {"goal_eur": "Infinity"},
    {"goal_eur": "1000000000001"}, {"goal_eur": "1.001"}, {"starting_capital_eur": "-1"},
    {"reliability": 0.009}, {"reliability": 0.991}, {"reliability": 1}, {"reliability": "NaN"},
    {"goal_date": "2026-10-16"}, {"goal_date": "2026-09-17"}, {"goal_date": "2076-09-18"},
    {"goal_date": "tomorrow"}, {"paths": 1000000},
    {"as_of": "2026-09-17T12:00:00"}, {"as_of": "2026-09-18T12:00:00Z"},
    {"as_of": "2026-09-16T12:00:00Z", "goal_date": "2026-09-17"},
])
def test_api_input_bounds_before_strategy_database_work(api, db, changes, monkeypatch):
    client, _ = api
    login(client)
    lookup = Mock(side_effect=AssertionError("Invalid goal must not read strategy data"))
    monkeypatch.setattr(db.instruments, "find_one", lookup)
    assert client.post("/goals/simulate", json=request_body(**changes)).status_code == 422
    lookup.assert_not_called()


@pytest.mark.parametrize("rows", [[], history(3), history(currency="USD"), history(currency=None)],
                         ids=["profile-only", "short-history", "non-eur", "unknown-currency"])
def test_api_rejects_insufficient_or_non_eur_history(api, db, rows):
    client, _ = api
    seed_history(db, rows)
    seed_profile(db)
    login(client)
    response = client.post("/goals/simulate", json=request_body())
    assert response.status_code == 422
    assert response.json()["detail"] == instrument_statistics(db, ISIN)["ineligible_reason"]


def test_strategy_catalogue_auth_field_metadata_and_no_provider_calls(api, db, config, monkeypatch):
    client, publish = api
    forbidden = Mock(side_effect=AssertionError("Catalogue must not acquire or estimate data"))
    monkeypatch.setattr(md, "MarketstackClient", forbidden)
    monkeypatch.setattr(gs, "instrument_statistics", forbidden)
    monkeypatch.setattr(db.instruments, "find_one", forbidden)
    assert client.get("/goals/strategies").status_code == 401
    login(client)
    response = client.get("/goals/strategies")
    assert response.status_code == 200
    assert response.json() == {"items": [{
        "id": "buy_and_hold_single_instrument",
        "name": "Buy and hold single instrument",
        "description": "Invest starting capital and each monthly contribution in one instrument and hold it.",
        "supports_calibration": True, "latest_calibration": None,
        "parameters": [{"key": "instrument", "type": "instrument", "label": "Instrument",
                        "required": True, "universe": "acquired"}],
    }]}
    definition = gs.get_strategy("buy_and_hold_single_instrument")
    assert definition.parameters_model is gs.BuyAndHoldParameters
    with pytest.raises(FrozenInstanceError):
        definition.name = "Changed"
    field = gs.BuyAndHoldParameters.model_fields["instrument"]
    monkeypatch.setattr(field, "title", "Updated instrument label")
    assert client.get("/goals/strategies").json()["items"][0]["parameters"][0]["label"] == field.title
    assert md.budget_status(db, config)["used"] == 0
    assert db.acquisition_jobs.count_documents({}) == 0
    assert db.instrument_statistics.count_documents({}) == 0
    forbidden.assert_not_called()
    publish.assert_not_called()


@pytest.mark.parametrize("selection", [
    None, True, [], {},
    {"id": "unknown_strategy", "parameters": {}},
    {"id": None, "parameters": {}},
    {"id": True, "parameters": {}},
    {"id": [], "parameters": {}},
    {"id": {}, "parameters": {}},
    {"parameters": {"instrument": ISIN}},
    {"id": "buy_and_hold_single_instrument"},
    *[{"id": "buy_and_hold_single_instrument", "parameters": parameters}
      for parameters in (None, True, [], "invalid", {}, {"instrument_id": ISIN},
                         {"instrument": ISIN, "unknown": 1})],
    {"id": "buy_and_hold_single_instrument", "parameters": {"instrument": ISIN}, "unknown": 1},
])
def test_strategy_selection_rejected_by_pydantic_before_database_work(api, db, monkeypatch, selection):
    client, _ = api
    login(client)
    forbidden = Mock(side_effect=AssertionError("Invalid strategy must not access data"))
    monkeypatch.setattr(db.instruments, "find_one", forbidden)
    body = request_body(strategy=selection)
    with pytest.raises(ValidationError):
        GoalRequest(**body)
    response = client.post("/goals/simulate", json=body)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    forbidden.assert_not_called()


@pytest.mark.parametrize("instrument", [None, True, False, 123, 1.2, [], [ISIN], {}, {"isin": ISIN},
                                       "", "bad", ISIN.lower(), f" {ISIN}", f"{ISIN}\n"])
def test_strategy_instrument_requires_a_strict_isin_string(api, instrument):
    client, _ = api
    login(client)
    body = request_body(strategy={"id": "buy_and_hold_single_instrument", "parameters": {"instrument": instrument}})
    with pytest.raises(ValidationError):
        GoalRequest(**body)
    response = client.post("/goals/simulate", json=body)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


@pytest.mark.parametrize("include_strategy", [False, True])
def test_legacy_top_level_instrument_is_rejected(api, include_strategy):
    client, _ = api
    login(client)
    body = request_body(instrument_id=ISIN)
    if not include_strategy:
        body.pop("strategy")
    response = client.post("/goals/simulate", json=body)
    assert response.status_code == 422
    assert any(error["loc"] == ["body", "instrument_id"] and error["type"] == "extra_forbidden"
               for error in response.json()["detail"])


@pytest.mark.parametrize("inventory", ["catalogue-only", "listing-only", "quote-only"])
def test_strategy_requires_its_own_saved_profile_even_when_quotes_are_eligible(api, db, monkeypatch, inventory):
    client, publish = api
    # A different acquired instrument must not satisfy the selected parameter.
    db.instruments.insert_one({"instrument_id": "IE00B4L5Y983", "job_id": "another-profile"})
    if inventory == "listing-only":
        db.instrument_state.insert_one({"_id": ISIN, "listing": LISTING})
    elif inventory == "quote-only":
        seed_history(db)
        assert instrument_statistics(db, ISIN)["eligible"]
    assert db.instruments.find_one({"instrument_id": ISIN}) is None
    forbidden = Mock(side_effect=AssertionError("Acquired gate must precede statistics and simulation"))
    monkeypatch.setattr(gs, "instrument_statistics", forbidden)
    monkeypatch.setattr(gs, "simulate_buy_and_hold", forbidden)
    monkeypatch.setattr(md, "MarketstackClient", forbidden)
    login(client)
    response = client.post("/goals/simulate", json=request_body())
    assert response.status_code == 422
    assert response.json()["detail"] == "Choose an instrument from the acquired universe. Acquire its data first."
    forbidden.assert_not_called()
    publish.assert_not_called()


@pytest.mark.parametrize("change", ["active-job", "price-currency"])
def test_simulation_revalidates_cached_eligibility_after_operational_changes(api, db, config, monkeypatch, change):
    client, publish = api
    seed_profile(db)
    seed_history(db, history(currency=None))
    md.set_price_currency(db, ISIN, "EUR")
    login(client)
    before = client.post("/goals/simulate", json=request_body())
    assert before.status_code == 200
    cached = db.instrument_statistics.find_one({"_id": ISIN})
    provider = Mock(side_effect=AssertionError("Simulation must not call a provider"))
    solver = Mock(side_effect=AssertionError("Ineligible history must not reach the solver"))
    monkeypatch.setattr(md, "MarketstackClient", provider)
    original_solver = gs.simulate_buy_and_hold
    monkeypatch.setattr(gs, "simulate_buy_and_hold", solver)
    if change == "active-job":
        job = enqueue(db, config)
        expected_reason = "Wait for the current EOD acquisition to finish, then refresh these estimates."
    else:
        assert client.patch(f"/instruments/{ISIN}/price-currency", json={"price_currency": "USD"}).status_code == 200
        expected_reason = instrument_statistics(db, ISIN)["ineligible_reason"]
        assert "EUR" in expected_reason
    response = client.post("/goals/simulate", json=request_body())
    assert response.status_code == 422
    assert response.json()["detail"] == expected_reason
    current_cache = db.instrument_statistics.find_one({"_id": ISIN})
    if change == "active-job":
        assert current_cache == cached
    else:
        assert current_cache["signature"]["currency_override"] == "USD"
        assert current_cache["statistics"] == cached["statistics"]
    solver.assert_not_called()
    monkeypatch.setattr(gs, "simulate_buy_and_hold", original_solver)
    if change == "active-job":
        db.acquisition_jobs.update_one({"_id": job["id"]}, {"$set": {"active": False, "status": "failed"}})
    else:
        assert client.patch(f"/instruments/{ISIN}/price-currency", json={"price_currency": "EUR"}).status_code == 200
    restored = client.post("/goals/simulate", json=request_body())
    assert restored.status_code == 200
    assert restored.json() == before.json()
    provider.assert_not_called()
    publish.assert_not_called()


def test_acquired_instrument_type_enforces_membership_for_every_strategy_parameter(api, db, monkeypatch):
    class PairParameters(BaseModel):
        model_config = ConfigDict(extra="forbid")
        primary: gs.AcquiredInstrument = Field(title="Primary")
        secondary: gs.AcquiredInstrument = Field(title="Secondary")

    execute = Mock(return_value={"parameter_statistics": {}})
    definition = gs.StrategyDefinition(
        id="test_pair", name="Test pair", description="Test-only two-instrument strategy.",
        parameters_model=PairParameters, simulate=execute,
    )
    monkeypatch.setitem(gs.STRATEGIES, definition.id, definition)
    seed_profile(db)
    second = "IE00B4L5Y983"
    body = request_body(strategy={"id": definition.id, "parameters": {"primary": ISIN, "secondary": second}})
    client, _ = api
    login(client)
    assert client.post("/goals/simulate", json=body).status_code == 422
    execute.assert_not_called()
    db.instruments.insert_one({"instrument_id": second, "job_id": "second-profile"})
    response = client.post("/goals/simulate", json=body)
    assert response.status_code == 200
    assert response.json()["strategy"] == body["strategy"]
    assert isinstance(execute.call_args.args[0], PairParameters)
    execute.assert_called_once()
    # Recheck the first parameter too, not only the most recently changed one.
    db.instruments.delete_one({"instrument_id": ISIN})
    assert client.post("/goals/simulate", json=body).status_code == 422
    execute.assert_called_once()


def test_registry_dispatches_another_typed_strategy_without_buy_and_hold_assumptions(api, db, monkeypatch):
    class SeparateParameters(BaseModel):
        model_config = ConfigDict(extra="forbid")
        periods: int = Field(gt=0, title="Periods", json_schema_extra={"parameter_type": "integer"})
        settlement_date: date = Field(default=NOW.date(), title="Settlement date",
                                      json_schema_extra={"parameter_type": "date"})

    calls = []
    output = {"monthly_contribution_eur": "12.34", "parameter_statistics": {}, "separate_projection": [7, 2, 9]}

    def separate_simulation(parameters: SeparateParameters, goals: GoalInputs, database, today):
        assert isinstance(parameters, SeparateParameters)
        assert parameters.periods == 3 and parameters.settlement_date == NOW.date()
        assert isinstance(goals, GoalInputs) and goals.goal_eur == Decimal("100000.00")
        assert database is db and today == NOW.date()
        calls.append(parameters)
        return deepcopy(output)

    definition = gs.StrategyDefinition(
        id="test_separate_strategy", name="Separate test strategy", description="A different execution model.",
        parameters_model=SeparateParameters, simulate=separate_simulation,
    )
    monkeypatch.setitem(gs.STRATEGIES, definition.id, definition)
    forbidden = Mock(side_effect=AssertionError("Generic dispatch must not use buy-and-hold data or math"))
    monkeypatch.setattr(gs, "instrument_statistics", forbidden)
    monkeypatch.setattr(gs, "simulate_buy_and_hold", forbidden)
    monkeypatch.setattr(db.instruments, "find_one", forbidden)
    client, publish = api
    login(client)
    catalogue = client.get("/goals/strategies").json()["items"]
    assert catalogue[-1] == {
        "id": definition.id, "name": definition.name, "description": definition.description,
        "supports_calibration": False, "latest_calibration": None,
        "parameters": [
            {"key": "periods", "type": "integer", "label": "Periods", "required": True, "universe": None},
            {"key": "settlement_date", "type": "date", "label": "Settlement date", "required": False, "universe": None},
        ],
    }
    for invalid in ({}, {"periods": 0}, {"periods": 3, "instrument": ISIN}):
        response = client.post("/goals/simulate", json=request_body(strategy={"id": definition.id, "parameters": invalid}))
        assert response.status_code == 422
    assert not calls
    selection = {"id": definition.id, "parameters": {"periods": "3"}}
    response = client.post("/goals/simulate", json=request_body(strategy=selection))
    assert response.status_code == 200, response.text
    assert response.json() == {**output, "data_as_of": NOW.isoformat().replace("+00:00", "Z"), "strategy": {
        "id": definition.id, "parameters": {"periods": 3, "settlement_date": NOW.date().isoformat()},
    }}
    assert len(calls) == 1
    forbidden.assert_not_called()
    publish.assert_not_called()
