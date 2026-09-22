"""Observed 12-month price changes are not annualized daily-return estimates."""

from copy import deepcopy
import json

from bson import Decimal128
import pytest

from return_statistics import calculate_historical_year_return, calculate_statistics


def prices(start="2025-09-16", end="2026-09-16", first=100, last=110):
    return [{"date": start, "close": first}, {"date": end, "close": last}]


@pytest.mark.parametrize("last, expected", [(110, 0.1), (90, -0.1), (100, 0), (150, 0.5)])
def test_actual_price_change_not_log_mean_or_annualized_daily_return(last, expected):
    stats = calculate_statistics(prices(last=last))
    assert stats["historical_year_return"] == pytest.approx(expected)
    assert stats["historical_year_start_date"] == "2025-09-16"
    assert stats["historical_year_end_date"] == "2026-09-16"
    assert stats["historical_year_price_basis"] == "close"
    # Endpoint prices suffice even without any adjacent daily observations.
    assert stats["annualized_mean_log_return"] is None
    assert stats["return_count"] == 0


@pytest.mark.parametrize("start, end", [
    ("2025-09-12", "2026-09-14"),  # Anniversary is Sunday; use Friday.
    ("2023-02-28", "2024-02-29"),  # Leap day clamps to February 28.
    ("2024-02-28", "2025-02-28"),  # Calendar year, not 365-day subtraction.
    ("2025-09-09", "2026-09-16"),  # Maximum permitted start-date gap.
    ("2020-01-01", "2021-01-01"),  # Anchor to saved history, not today.
    ("2024-09-14", "2025-09-14"),  # Historical endpoints aren't weekday estimates.
])
def test_calendar_anniversary_and_nearby_prior_quote(start, end):
    result = calculate_historical_year_return(prices(start, end))
    assert result["historical_year_return"] == pytest.approx(0.1)
    assert result["historical_year_start_date"] == start
    assert result["historical_year_end_date"] == end


def test_uses_latest_quote_on_or_before_anniversary_never_after_it():
    rows = [
        {"date": "2024-01-01", "close": 10},
        {"date": "2025-09-12", "close": 50},
        {"date": "2025-09-15", "close": 100},
        {"date": "2025-09-17", "close": 200},
        {"date": "2026-09-16", "close": 110},
    ]
    result = calculate_historical_year_return(rows)
    assert result["historical_year_return"] == pytest.approx(0.1)
    assert result["historical_year_start_date"] == "2025-09-15"


@pytest.mark.parametrize("rows", [
    [], [{"date": "2026-09-16", "close": 100}],
    prices(start="2025-09-17"),  # Less than a full year must not be extrapolated.
    prices(start="2025-09-08"),  # Too far before the anniversary.
    prices(start="2024-09-16"),  # Two-year growth is not a one-year return.
    prices(start="0001-01-01", end="0001-09-16"),
])
def test_unavailable_without_a_comparable_full_year(rows):
    assert all(value is None for value in calculate_historical_year_return(rows).values())


def test_prefers_adjusted_endpoints_without_mutation_or_splicing():
    rows = prices(first=200, last=110)
    rows[0]["adj_close"] = Decimal128("100")
    rows[1]["adj_close"] = "110"
    # An intermediate missing adjustment does not change an endpoint ratio.
    rows.insert(1, {"date": "2026-01-01", "close": None, "adj_close": None})
    original = deepcopy(rows)
    result = calculate_historical_year_return(rows)
    assert result["historical_year_price_basis"] == "adj_close"
    assert result["historical_year_return"] == pytest.approx(0.1)
    assert rows == original


@pytest.mark.parametrize("missing_index", [0, 1])
def test_incomplete_endpoint_adjustments_use_raw_prices_at_both_ends(missing_index):
    rows = prices(first=200, last=110)
    rows[0]["adj_close"] = 100
    rows[1]["adj_close"] = 110
    rows[missing_index]["adj_close"] = None
    result = calculate_historical_year_return(rows)
    assert result["historical_year_price_basis"] == "close"
    assert result["historical_year_return"] == pytest.approx(-0.45)


@pytest.mark.parametrize("invalid", [None, 0, -1, "NaN", "Infinity", True, {}, [], "missing"])
@pytest.mark.parametrize("index", [0, 1])
def test_invalid_endpoints_are_unavailable_and_not_replaced_by_older_valid_prices(invalid, index):
    rows = prices()
    rows[index]["close"] = invalid
    rows = [{"date": "2025-09-15", "close": 100}, rows[0],
            {"date": "2026-09-15", "close": 105}, rows[1]]
    assert calculate_historical_year_return(rows)["historical_year_return"] is None


def test_overflow_returns_null_not_nonfinite_json():
    result = calculate_historical_year_return(prices(first=1e-300, last=1e300))
    assert result["historical_year_return"] is None
    json.dumps(result, allow_nan=False)


def test_known_mixed_currencies_in_the_year_cannot_be_compared():
    rows = prices()
    rows[0]["price_currency"] = "EUR"
    rows[1]["price_currency"] = "EUR"
    rows.insert(1, {"date": "2026-01-01", "close": 105, "price_currency": "USD"})
    assert calculate_historical_year_return(rows)["historical_year_return"] is None
    rows[1]["price_currency"] = " eur "
    assert calculate_historical_year_return(rows)["historical_year_return"] == pytest.approx(0.1)
    # Currencies outside the selected interval are irrelevant to this return.
    rows.insert(0, {"date": "2020-01-01", "close": 10, "price_currency": "USD"})
    assert calculate_historical_year_return(rows)["historical_year_return"] == pytest.approx(0.1)
