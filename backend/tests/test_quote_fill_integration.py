"""Opt-in derived-fill contracts against a disposable real MongoDB database."""
from test_portfolio_removal_integration import db
from test_portfolio_tracker import api, clock, config, tracker
import test_quote_fill as contracts


def test_real_mongo_fill_preserves_raw_rows_and_repairs_twr(tracker, db):
    contracts.test_portfolio_twr_repairs_derived_gap_not_saved_evidence(tracker, db)


def test_real_mongo_policy_invalidates_statistics(db):
    contracts.test_statistics_policy_cache_and_bootstrap_consistency(db)
