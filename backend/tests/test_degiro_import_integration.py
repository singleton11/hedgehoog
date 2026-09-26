"""Opt-in atomic import checks in disposable real MongoDB databases."""

import pytest

import test_degiro_import as contracts
from test_portfolio_removal_integration import db, synchronize_mongo_writes
from test_portfolio_tracker import api, clock, config, tracker


def test_real_mongo_degiro_import_and_retries(tracker, db):
    contracts.test_preview_is_read_only_and_atomic_import_reconciles_exactly(tracker, db)


@pytest.mark.parametrize("race", ["identical", "different", "manual"])
def test_real_mongo_degiro_batch_cas(tracker, db, monkeypatch, race):
    monkeypatch.setattr(contracts, "synchronize_writes", synchronize_mongo_writes)
    contracts.test_atomic_batch_races(tracker, db, monkeypatch, race)
