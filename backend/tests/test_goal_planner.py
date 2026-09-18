from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import math
import json

import numpy as np
from statistics import mean, variance
from unittest.mock import Mock

import pytest
from pymongo.errors import ConnectionFailure

import market_data as md
from goal_planner import GoalRequest, add_months, contribution_dates, simulate_goal
from return_statistics import calculate_statistics, instrument_statistics
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


def request_body(**changes):
    return {"instrument_id": ISIN, "goal_eur": "100000.00", "starting_capital_eur": "10000.00",
            "goal_date": "2036-09-17", "reliability": 0.9, **changes}


def flat_estimates(m=0.0, v=0.0):
    return {"daily_mean_log_return": m, "daily_log_return_variance": v, "warnings": []}


def login(client):
    assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200


def test_daily_simple_mean_and_sample_variance_not_price_levels():
    rows = [{"date": f"2026-09-{day:02}", "close": raw, "adj_close": adjusted}
            for day, raw, adjusted in [(1, 100, "10"), (2, 200, "11"), (3, 100, "9.9")]]
    stats = calculate_statistics(rows)
    assert stats["price_basis"] == "adj_close"
    assert stats["return_count"] == 2
    assert stats["daily_mean_return"] == pytest.approx(0, abs=1e-15)
    assert stats["daily_return_variance"] == pytest.approx(0.02)
    logs = [math.log(1.1), math.log(0.9)]
    assert stats["daily_mean_log_return"] == pytest.approx(mean(logs))
    assert stats["daily_log_return_variance"] == pytest.approx(variance(logs))
    assert stats["annualized_volatility"] == pytest.approx(math.sqrt(variance(logs) * 252))


def test_partial_adjustments_fall_back_for_entire_series():
    stats = calculate_statistics([
        {"date": "2026-09-01", "close": "100", "adj_close": "50"},
        {"date": "2026-09-02", "close": "110", "adj_close": None},
        {"date": "2026-09-03", "close": "121", "adj_close": "60.5"},
    ])
    assert stats["price_basis"] == "close"
    assert stats["daily_mean_return"] == pytest.approx(0.1)
    assert stats["daily_return_variance"] == pytest.approx(0)
    assert "unadjusted" in stats["warnings"][0]


@pytest.mark.parametrize("invalid", [None, 0, -1, "NaN", "Infinity", True, {}, [], "missing"])
def test_invalid_prices_break_chain_and_do_not_change_raw_rows(invalid):
    rows = [{"date": f"2026-09-{i + 1:02}", "close": price} for i, price in enumerate([100, invalid, 110, 121])]
    original = deepcopy(rows)
    stats = calculate_statistics(rows)
    assert rows == original
    assert stats["invalid_price_count"] == 1
    assert stats["price_count"] == 3 and stats["return_count"] == 1
    assert stats["daily_mean_return"] == pytest.approx(0.1)
    assert stats["daily_return_variance"] is None


def test_long_gaps_are_not_daily_returns_and_weekends_are_allowed():
    stats = calculate_statistics([{"date": day, "close": price} for day, price in [
        ("2026-09-04", 100), ("2026-09-07", 110), ("2026-09-16", 200), ("2026-09-17", 220)]])
    assert stats["skipped_gap_count"] == 1
    assert stats["return_count"] == 2
    assert stats["daily_mean_return"] == pytest.approx(0.1)


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
    assert stats["daily_mean_return"] == pytest.approx(0.1)
    assert stats["daily_return_variance"] == pytest.approx(0)


def test_extreme_preserved_data_does_not_break_statistics():
    stats = calculate_statistics([{"date": f"2026-09-0{i + 1}", "close": p}
                                  for i, p in enumerate([1e-300, 1e300, 1, 1e308])])
    assert stats["daily_return_variance"] is None  # Overflow is not JSON infinity.
    assert any("numerical" in warning for warning in stats["warnings"])


def test_successful_download_persists_stats_and_updates_recompute(db, config, clock):
    rows = history()
    result, source = collect(db, config, Provider([page(rows)]))
    assert result["status"] == "succeeded"
    assert len(source.calls) == 2
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
    assert after["daily_mean_return"] != before["daily_mean_return"]


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
    assert len(source.calls) == 2
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
    assert second["daily_mean_return"] != first["daily_mean_return"]
    assert second["daily_mean_return"] == pytest.approx(calculate_statistics([
        {**row["data"], "date": row["date"]} for row in original_quotes[:-1]] +
        [{**revised["data"], "date": revised["date"]}])["daily_mean_return"])


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
    body = GoalRequest(**request_body(goal_eur="10000", starting_capital_eur="0", goal_date="2027-09-17"))
    result = simulate_goal(flat_estimates(), body, NOW.date())
    assert result["monthly_contribution_eur"] == "833.34"
    assert result["total_contributions_eur"] == "10000.08"
    assert result["terminal_percentiles_eur"] == {"p05": "10000.08", "p50": "10000.08", "p95": "10000.08"}
    assert result["contribution_count"] == 12 and result["success_probability"] == 1
    assert result["zero_contribution_success_probability"] == 0


def test_zero_monthly_only_when_initial_capital_alone_meets_probability():
    body = GoalRequest(**request_body(goal_eur="10000", starting_capital_eur="10000"))
    result = simulate_goal(flat_estimates(), body, NOW.date())
    assert result["monthly_contribution_eur"] == "0.00"
    assert result["zero_contribution_success_probability"] == 1
    # Already holding the nominal target does not guarantee preserving it.
    risky = simulate_goal(flat_estimates(m=-0.0002, v=0.0004), body, NOW.date())
    assert Decimal(risky["monthly_contribution_eur"]) > 0


def test_positive_and_negative_deterministic_growth_and_end_period_deposits():
    today = date(2026, 1, 31)
    for annual_log_growth in (-0.1, 0.1):
        body = GoalRequest(**request_body(goal_eur="10000", starting_capital_eur="1000", goal_date="2026-04-15"))
        result = simulate_goal(flat_estimates(m=annual_log_growth / 252), body, today)
        terminal_start = 1000 * math.exp(annual_log_growth * (body.goal_date - today).days / 365.25)
        annuity = sum(math.exp(annual_log_growth * (body.goal_date - d).days / 365.25)
                      for d in [date(2026, 2, 28), date(2026, 3, 31)])
        expected = math.ceil((10000 - terminal_start) / annuity * 100) / 100
        assert float(result["monthly_contribution_eur"]) == expected
        assert result["contribution_count"] == 2
        assert result["success_probability"] == 1


def test_simulation_reproducible_probability_met_and_monotone_inputs():
    estimates = flat_estimates(m=0.0001, v=0.0002)
    body = GoalRequest(**request_body())
    result = simulate_goal(estimates, body, NOW.date())
    assert simulate_goal(estimates, body, NOW.date()) == result
    amount = Decimal(result["monthly_contribution_eur"])
    assert result["success_probability"] >= 0.9
    assert result["success_probability"] < 0.905
    for changes, larger in [({"reliability": 0.99}, True), ({"goal_eur": "200000"}, True),
                            ({"starting_capital_eur": "20000"}, False), ({"reliability": 0.5}, False)]:
        changed = simulate_goal(estimates, GoalRequest(**request_body(**changes)), NOW.date())
        assert (Decimal(changed["monthly_contribution_eur"]) > amount) is larger
        assert changed["success_probability"] >= changed["target_probability"]


def test_zero_variance_trajectory_is_one_line_with_initial_capital_and_end_period_deposits():
    body = GoalRequest(**request_body(goal_eur="10000", starting_capital_eur="1000", goal_date="2027-09-17"))
    result = simulate_goal(flat_estimates(), body, NOW.date())
    assert result["goal_eur"] == "10000.00"
    assert len(result["trajectory"]) == 13
    for index, point in enumerate(result["trajectory"]):
        expected = format(Decimal("1000") + Decimal(result["monthly_contribution_eur"]) * index, ".2f")
        assert point["date"] == add_months(NOW.date(), index).isoformat()
        assert set(value for key, value in point.items() if key != "date") == {expected}


def test_trajectory_partial_month_does_not_add_an_extra_payment():
    today = date(2028, 1, 31)
    body = GoalRequest(**request_body(goal_eur="10000", starting_capital_eur="0", goal_date="2028-04-15"))
    result = simulate_goal(flat_estimates(), body, today)
    assert result["contribution_count"] == 2
    points = result["trajectory"]
    assert [point["date"] for point in points] == ["2028-01-31", "2028-02-29", "2028-03-31", "2028-04-15"]
    assert [point["mean_eur"] for point in points] == ["0.00", "5000.00", "10000.00", "10000.00"]


def test_cloud_and_mean_use_all_original_paths_with_the_recommended_contribution():
    paths, seed = 512, 123
    body = GoalRequest(**request_body(goal_date="2027-01-22"))
    estimates = flat_estimates(m=0.0001, v=0.0002)
    result = simulate_goal(estimates, body, NOW.date(), paths=paths, seed=seed)
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
    assert result == simulate_goal(estimates, body, NOW.date(), paths=paths, seed=seed)


def test_maximum_horizon_returns_bounded_monthly_summaries_not_all_traces():
    body = GoalRequest(**request_body(goal_date=add_months(NOW.date(), 600).isoformat()))
    result = simulate_goal(flat_estimates(m=0.0001, v=0.0002), body, NOW.date())
    assert result["simulation_paths"] == 10000
    assert len(result["trajectory"]) == 601
    assert result["trajectory"][0]["date"] == result["valuation_date"]
    assert result["trajectory"][-1]["date"] == result["goal_date"]
    assert len(json.dumps(result)) < 200_000
    assert all(set(point) == {"date", "mean_eur", "p05_eur", "p25_eur", "p50_eur", "p75_eur", "p95_eur"} for point in result["trajectory"])


def test_extreme_simulation_rejected_not_silently_clipped():
    with pytest.raises(md.DataError, match="unstable"):
        simulate_goal(flat_estimates(m=1000, v=1), GoalRequest(**request_body()), NOW.date())


def test_api_statistics_and_simulation_auth_origin_and_no_provider_calls(api, db, config, monkeypatch):
    client, publish = api
    seed_history(db)
    provider = Mock(side_effect=AssertionError("No provider call"))
    monkeypatch.setattr(md, "MarketstackClient", provider)
    url = f"/instruments/{ISIN}/statistics"
    assert client.get(url).status_code == 401
    assert client.post("/goals/simulate", json=request_body()).status_code == 401
    login(client)
    assert client.post("/goals/simulate", json=request_body(), headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get(url).json()["eligible"]
    response = client.post("/goals/simulate", json=request_body())
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["simulation_paths"] == 10000
    assert result["success_probability"] >= 0.9
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
    {"reliability": 0.499}, {"reliability": 1}, {"reliability": "NaN"},
    {"goal_date": "2026-10-16"}, {"goal_date": "2026-09-17"}, {"goal_date": "2076-09-18"},
    {"goal_date": "tomorrow"}, {"instrument_id": "bad"}, {"paths": 1000000},
])
def test_api_input_bounds(api, changes):
    client, _ = api
    login(client)
    assert client.post("/goals/simulate", json=request_body(**changes)).status_code == 422


@pytest.mark.parametrize("rows", [[], history(3), history(currency="USD"), history(currency=None)])
def test_api_rejects_insufficient_or_non_eur_history(api, db, rows):
    client, _ = api
    seed_history(db, rows)
    login(client)
    response = client.post("/goals/simulate", json=request_body())
    assert response.status_code == 422
    assert response.json()["detail"]
