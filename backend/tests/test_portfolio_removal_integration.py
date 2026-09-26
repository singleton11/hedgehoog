"""Opt-in removal contracts against disposable real MongoDB databases."""

import os
from threading import Barrier, Lock
from uuid import uuid4

from pymongo import MongoClient
from pymongo.collection import Collection
import pytest

import test_portfolio_removal as contracts
from test_portfolio_tracker import api, clock, config, tracker


@pytest.fixture
def db(clock):
    uri = os.environ.get("TEST_MONGODB_URI")
    if not uri:
        pytest.skip("Set TEST_MONGODB_URI for isolated real Mongo tests")
    name = "hedgehoog_removal_test_" + uuid4().hex
    mongo = MongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    try:
        mongo.admin.command("ping")
        yield mongo[name]
    finally:
        # The API lifespan may already have closed the injected client.
        mongo.close()
        with MongoClient(uri, serverSelectionTimeoutMS=5000) as cleanup:
            cleanup.drop_database(name)
            assert name not in cleanup.list_database_names()


def synchronize_mongo_writes(monkeypatch, collection):
    original = Collection.update_one
    barrier, lock, revisions = Barrier(2), Lock(), []

    def update(current, query, changes, *args, **kwargs):
        if current.full_name == collection.full_name:
            with lock:
                revisions.append(query["revision"])
                wait = len(revisions) <= 2
            if wait:
                barrier.wait(timeout=10)
        return original(current, query, changes, *args, **kwargs)

    # Real PyMongo returns fresh collection objects, so patch at class level.
    monkeypatch.setattr(Collection, "update_one", update)
    return revisions


@pytest.mark.parametrize("race", ["two-deposits", "remove-and-buy", "same-event"])
def test_real_mongo_removal_races(tracker, db, monkeypatch, race):
    monkeypatch.setattr(contracts, "synchronize_first_two_writes", synchronize_mongo_writes)
    contracts.test_removal_cas_revalidates_against_concurrent_writes(tracker, db, monkeypatch, race)


def test_real_mongo_removal_accounting(tracker, db):
    contracts.test_remove_all_transaction_types_recalculates_balances_and_net_worth(tracker, db)


def test_real_mongo_removal_pagination(tracker):
    contracts.test_removed_events_are_excluded_before_pagination_and_do_not_reuse_sequence(tracker)


def test_real_mongo_removal_retry_identity(tracker, db):
    contracts.test_retry_is_idempotent_and_old_post_cannot_resurrect_removed_event(tracker, db)
