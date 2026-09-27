"""Opt-in historical return reads against disposable real MongoDB databases."""

import test_portfolio_performance as contracts
from test_portfolio_removal_integration import db
from test_portfolio_tracker import api, clock, config, tracker


def test_real_mongo_geometric_returns_and_external_flows(tracker, db):
    contracts.test_deposits_and_withdrawals_are_removed_and_daily_factors_are_geometrically_linked(tracker, db)


def test_real_mongo_missing_currency_does_not_erase_liquidated_history(tracker, db):
    contracts.test_later_currency_gaps_only_invalidate_dates_that_need_the_mark(tracker, db, None)


def test_real_mongo_benchmark_and_read_only_snapshot(tracker, db):
    contracts.test_benchmark_has_common_baseline_without_fx_and_no_benchmark_means_only_twr(tracker, db)


def test_real_mongo_invalid_selected_endpoint(tracker, db):
    contracts.test_latest_invalid_observation_is_not_replaced_by_older_valid_or_adjusted_price(tracker, db, None)


def test_real_mongo_published_generation_isolation(tracker, db):
    contracts.test_unpublished_generations_do_not_leak_and_refresh_does_not_mix_old_history(tracker, db)
