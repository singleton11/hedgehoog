"""Trailing-year volatility shares the daily estimator, not its full-history window."""

from copy import deepcopy
from datetime import date, timedelta
import json
import math
from statistics import variance

import pytest

from return_statistics import calculate_historical_year_volatility, calculate_statistics


def year_with_daily_tail(start="2025-09-16", end="2026-09-16"):
    end_day = date.fromisoformat(end)
    days = [end_day - timedelta(days=i) for i in range(8, -1, -1)]
    return [{"date": start, "close": 100}] + [
        {"date": day.isoformat(), "close": 100 * math.exp(0.01 * (i % 2))}
        for i, day in enumerate(days) if day.weekday() < 5
    ]


def test_trailing_year_uses_sample_log_volatility_not_older_history():
    rows = []
    day, price = date(2024, 9, 16), 100
    while day <= date(2026, 9, 16):
        if day.weekday() < 5:
            scale = 0.1 if day <= date(2025, 9, 16) else 0.01
            price *= math.exp(scale if len(rows) % 2 else -scale)
            rows.append({"date": day.isoformat(), "adj_close": price})
        day += timedelta(days=1)
    original = deepcopy(rows)
    stats = calculate_statistics(rows)
    window = [row for row in rows if row["date"] >= "2025-09-16"]
    logs = [math.log(b["adj_close"]) - math.log(a["adj_close"])
            for a, b in zip(window, window[1:])]
    assert stats["historical_year_volatility"] == pytest.approx(math.sqrt(variance(logs) * 252))
    assert stats["historical_year_volatility"] < stats["annualized_volatility"] / 2
    assert stats["historical_year_volatility_return_count"] == len(logs)
    assert stats["historical_year_volatility_start_date"] == "2025-09-16"
    assert stats["historical_year_volatility_end_date"] == "2026-09-16"
    assert stats["historical_year_volatility_price_basis"] == "adj_close"
    assert rows == original


@pytest.mark.parametrize("start, end", [
    ("2025-09-12", "2026-09-14"),  # Sunday anniversary uses prior Friday.
    ("2023-02-28", "2024-02-29"),  # Leap anniversary clamps to February 28.
    ("2025-09-09", "2026-09-16"),  # Seven-day boundary.
    ("2020-01-01", "2021-01-01"),  # Saved year, not wall-clock year.
    ("2024-09-13", "2025-09-14"),  # Latest saved quote on a weekend.
])
def test_same_calendar_window_as_historical_return(start, end):
    rows = year_with_daily_tail(start, end)
    if rows[-1]["date"] != end:
        rows.append({"date": end, "close": 110})
    stats = calculate_statistics(rows)
    assert stats["historical_year_volatility"] is not None
    assert stats["historical_year_volatility_start_date"] == stats["historical_year_start_date"] == start
    assert stats["historical_year_volatility_end_date"] == stats["historical_year_end_date"] == end


@pytest.mark.parametrize("rows", [
    [], [{"date": "2026-09-16", "close": 100}],
    year_with_daily_tail(start="2025-09-17"),  # Shorter than one year.
    year_with_daily_tail(start="2025-09-08"),  # Stale year-ago boundary.
    year_with_daily_tail(start="0001-01-01", end="0001-09-16"),
    [{"date": "2025-09-16", "close": 100}, {"date": "2026-09-16", "close": 110}],
    [{"date": "2025-09-16", "close": 100}, {"date": "2026-09-15", "close": 100},
     {"date": "2026-09-16", "close": 110}],  # Only one daily return.
    [{"date": (date(2025, 1, 3) + timedelta(weeks=i)).isoformat(), "close": 100 + i}
     for i in range(54)],  # Weekly history is never daily volatility.
])
def test_insufficient_history_is_unavailable(rows):
    assert all(value is None for value in calculate_historical_year_volatility(rows).values())


def test_flat_prices_are_zero_not_unavailable():
    rows = year_with_daily_tail()
    for row in rows:
        row["close"] = 100
    assert calculate_historical_year_volatility(rows)["historical_year_volatility"] == 0


def test_window_price_basis_is_independent_of_endpoints_and_older_adjustments():
    rows = year_with_daily_tail()
    for row in rows:
        row["adj_close"] = 50
    rows.insert(0, {"date": "2024-01-01", "close": 200, "price_currency": "USD"})
    stats = calculate_statistics(rows)
    assert stats["price_basis"] == "close"
    assert stats["historical_year_volatility_price_basis"] == "adj_close"
    assert stats["historical_year_volatility"] == 0
    rows[3]["adj_close"] = None  # Missing interior adjustment, not an endpoint.
    stats = calculate_statistics(rows)
    assert stats["historical_year_price_basis"] == "adj_close"
    assert stats["historical_year_volatility_price_basis"] == "close"
    assert stats["historical_year_volatility"] > 0


@pytest.mark.parametrize("invalid", [None, 0, -1, "NaN", "Infinity", True, {}, []])
def test_invalid_prices_and_missing_weekdays_break_the_daily_chain(invalid):
    rows = [{"date": day, "close": price} for day, price in [
        ("2025-09-16", 100), ("2026-09-04", 100), ("2026-09-05", 999),
        ("2026-09-07", 110), ("2026-09-08", invalid), ("2026-09-09", 100),
        ("2026-09-10", 90), ("2026-09-14", 200), ("2026-09-16", 300),
    ]]
    original = deepcopy(rows)
    result = calculate_historical_year_volatility(rows)
    assert result["historical_year_volatility_return_count"] == 2
    assert result["historical_year_volatility"] == pytest.approx(math.sqrt(252 * variance([math.log(1.1), math.log(0.9)])))
    assert rows == original


def test_known_mixed_window_currencies_are_unavailable_but_unknown_is_not_fx_converted():
    rows = year_with_daily_tail()
    rows[0]["price_currency"] = "EUR"
    rows[-1]["price_currency"] = "USD"
    assert all(value is None for value in calculate_historical_year_volatility(rows).values())
    rows[-1]["price_currency"] = " eur "
    assert calculate_historical_year_volatility(rows)["historical_year_volatility"] is not None


def test_extreme_finite_prices_do_not_overflow_log_volatility():
    rows = year_with_daily_tail()
    for i, row in enumerate(rows):
        row["close"] = 1e300 if i % 2 else 1e-300
    result = calculate_historical_year_volatility(rows)
    assert math.isfinite(result["historical_year_volatility"])
    json.dumps(result, allow_nan=False)
