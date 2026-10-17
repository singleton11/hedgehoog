"""Trailing stale marks restore historical annual return, never model fitting."""
from copy import deepcopy

import pytest

import market_data as md
from quote_fill import fill_adjusted_rows, read_policy
from return_statistics import (
    MODEL_VERSION,
    calculate_historical_year_return,
    calculate_statistics,
    instrument_return_runs,
    refresh_statistics,
)
from split_adjustments import adjusted_rows
from test_goal_planner import history, seed_history, seed_profile
from test_market_data import ISIN, NOW, clock, db


def rows():
    return [
        {"date": "2025-09-17", "close": "100", "price_currency": "EUR"},
        {"date": "2026-09-14", "close": "105", "price_currency": "EUR"},
        {"date": "2026-09-15", "close": "110", "price_currency": "EUR"},
        {"date": "2026-09-16", "close": None, "price_currency": "EUR"},
        {"date": "2026-09-17", "close": "NaN", "price_currency": "EUR"},
    ]


@pytest.mark.parametrize("basis", ["close", "adj_close", "split_adjusted_close"])
def test_trailing_carry_keeps_latest_endpoint_and_provenance_without_mutation(basis):
    source = rows()
    if basis != "close":
        for row in source:
            row[basis] = row["close"]
        source[0][basis] = "50"
        source[2][basis] = "55"
    before = deepcopy(source)
    actual = calculate_historical_year_return(source, carry_trailing=True)
    assert actual["historical_year_return"] == pytest.approx(0.1)
    assert actual["historical_year_start_date"] == "2025-09-17"
    assert actual["historical_year_end_date"] == "2026-09-17"
    assert actual["historical_year_carried_from_date"] == "2026-09-15"
    assert actual["historical_year_price_basis"] == basis
    assert source == before
    ordinary = calculate_statistics(source)
    carried = calculate_statistics(source, carry_trailing=True)
    for key in ordinary.keys() - actual.keys():
        assert carried[key] == ordinary[key]


@pytest.mark.parametrize("barrier", ["0", "-1", "Infinity", "garbage", True])
def test_invalid_trailing_prices_are_not_skipped(barrier):
    source = rows()
    source[-2]["close"] = barrier
    assert calculate_historical_year_return(source, carry_trailing=True)["historical_year_return"] is None


def test_failed_split_basis_is_not_rescued_or_replaced_with_adjusted_close():
    source = rows()
    for row in source:
        row["split_adjusted_close"] = None
        row["adj_close"] = "999"
    assert calculate_historical_year_return(source, carry_trailing=True)["historical_year_return"] is None
    # An invalid derived basis on a real closing price is a barrier too.
    source[0]["split_adjusted_close"] = "100"
    source[1]["split_adjusted_close"] = "105"
    assert calculate_historical_year_return(source, carry_trailing=True)["historical_year_return"] is None


@pytest.mark.parametrize("case", ["leading", "short", "weekend_source", "weekend_hole", "foreign", "currency_boundary"])
def test_carry_does_not_relax_other_availability_rules(case):
    source = rows()
    if case == "leading":
        source[0]["close"] = None
    elif case == "short":
        source[0]["date"] = "2025-09-18"
    elif case == "weekend_source":
        source[2]["date"] = "2026-09-13"
        source.pop(1)
    elif case == "weekend_hole":
        source[-1]["date"] = "2026-09-19"
    elif case == "foreign":
        for row in source:
            row["price_currency"] = "USD"
    elif case == "currency_boundary":
        source[-1]["price_currency"] = None
    assert calculate_historical_year_return(source, carry_trailing=True)["historical_year_return"] is None


def test_manual_currency_override_resolves_unknown_but_never_foreign_labels():
    source = rows()
    source[-1]["price_currency"] = None
    assert calculate_historical_year_return(source, carry_trailing=True, currency_override="EUR")["historical_year_return"] == pytest.approx(0.1)
    source[-1]["price_currency"] = "USD"
    assert calculate_historical_year_return(source, carry_trailing=True, currency_override="EUR")["historical_year_return"] is None


@pytest.mark.parametrize("enough_returns", [False, True])
def test_normal_policy_still_uses_sampled_endpoint_and_does_not_silently_carry(db, enough_returns):
    source = rows()
    if enough_returns:
        source.insert(1, {"date": "2026-09-11", "close": "101", "price_currency": "EUR"})
    seed_history(db, source)
    db.app_settings.insert_one({"_id": "market_data", "method": "normal", "seed": 7})
    stats, _ = refresh_statistics(db, ISIN)
    if enough_returns:
        expected = calculate_historical_year_return(fill_adjusted_rows(source, ISIN, read_policy(db)))
        assert stats["historical_year_return"] == expected["historical_year_return"]
        assert stats["historical_year_return"] != pytest.approx(0.1)
    else:
        assert stats["historical_year_return"] is None  # Only one observed adjacent return.
    assert stats["historical_year_carried_from_date"] is None


def test_statistics_inventory_cache_cutoff_and_bootstrap_remain_consistent(db):
    seed_profile(db)
    source = history(300)
    source.extend([
        {"date": "2026-09-16", "close": None, "adj_close": None, "price_currency": "EUR"},
        {"date": "2026-09-17", "close": None, "adj_close": None, "price_currency": "EUR"},
    ])
    for row in source:
        row["price_basis"] = "provider_split_adjusted"
    seed_history(db, source)
    original = list(db.quotes.find())
    adjusted, _ = adjusted_rows([{**row, "date": row["date"][:10]} for row in source], None)
    expected = calculate_statistics(fill_adjusted_rows(adjusted, ISIN, read_policy(db)))
    actual, _ = refresh_statistics(db, ISIN)
    year_start = next(row for row in source if row["date"][:10] == "2025-09-17")
    assert actual["historical_year_return"] == pytest.approx(float(source[-3]["close"]) / float(year_start["close"]) - 1)
    assert actual["historical_year_carried_from_date"] == "2026-09-15"
    assert actual["historical_year_end_date"] == "2026-09-17"
    assert any("stale last valid close from 2026-09-15" in warning for warning in actual["warnings"])
    for key in ("daily_mean_log_return", "annualized_mean_log_return", "daily_log_return_variance", "return_count", "historical_year_volatility"):
        assert actual[key] == expected[key]
    runs = instrument_return_runs(db, ISIN, as_of=NOW)
    assert sum(map(len, runs)) == actual["return_count"]
    assert db.instrument_statistics.find_one({"_id": ISIN})["signature"]["version"] == MODEL_VERSION
    # The previously unavailable cached result must also refresh without a provider call.
    db.instrument_statistics.update_one({"_id": ISIN}, {"$set": {
        "signature.version": MODEL_VERSION - 1, "statistics.historical_year_return": None,
    }})
    item = md.acquired_instruments(db)["items"][0]
    assert item["historical_year_return"] == actual["historical_year_return"]
    assert item["historical_year_carried_from_date"] == "2026-09-15"
    assert list(db.quotes.find()) == original
    # A later genuine quote must not repair an earlier historical cutoff.
    db.quotes.insert_one({**deepcopy(original[-1]), "_id": "future", "date": "2026-09-18",
                          "data": {"close": "999", "price_currency": "EUR", "price_basis": "provider_split_adjusted"}})
    historical, _ = refresh_statistics(db, ISIN, as_of=NOW, point_in_time=True)
    assert historical["historical_year_return"] == actual["historical_year_return"]
    assert historical["historical_year_carried_from_date"] == "2026-09-15"
    # Once the saved endpoint is observed, stale provenance disappears.
    db.quotes.update_many({"date": "2026-09-17"}, {"$set": {"data.close": "120"}})
    repaired, _ = refresh_statistics(db, ISIN, force=True)
    assert repaired["historical_year_carried_from_date"] is None
    assert not any("stale last valid close" in warning for warning in repaired["warnings"])
