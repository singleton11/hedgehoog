"""Retention reader/ownership guards and CLI boundaries, with synthetic storage."""
from copy import deepcopy
from datetime import timedelta
import json
import sys
from unittest.mock import Mock

import pytest

import market_data as md
import market_data_retention as retention
from test_market_data_retention import ISIN, LISTING, NOW, db, seed, settings


def test_explicit_generation_without_publication_never_leases_or_yields(db):
    with pytest.raises(md.DataError) as error:
        with retention.reader(db, ISIN, LISTING, "synthetic-missing"):
            pytest.fail("Missing generation cannot be read")
    assert error.value.status == 409
    assert db.acquisition_jobs.count_documents({}) == 0


def test_first_publication_during_empty_read_conflicts_on_completion(db):
    with pytest.raises(md.DataError) as error:
        with retention.reader(db, ISIN, LISTING) as generation:
            assert generation is None
            seed(db, "synthetic-new", 0)
    assert error.value.status == 409
    assert db.quotes.count_documents({"job_id": "synthetic-new"}) == 1


def test_reader_removes_only_expired_leases_and_releases_its_own_lease(db):
    seed(db, "latest", 0)
    leases = {"expired": NOW - timedelta(seconds=1), "live": NOW + timedelta(seconds=60)}
    db.acquisition_jobs.update_one({"_id": "latest"}, {"$set": {"reader_leases": leases}})
    with retention.reader(db, ISIN, LISTING, "latest") as generation:
        assert generation == "latest"
        active = db.acquisition_jobs.find_one({"_id": generation})["reader_leases"]
        assert "expired" not in active
        assert "live" in active and len(active) == 2
    assert db.acquisition_jobs.find_one({"_id": "latest"})["reader_leases"] == {"live": leases["live"]}


def test_unknown_terminal_state_is_not_pruned(db):
    seed(db, "pending-review", 0, published=False, status="synthetic-unknown")
    seed(db, "latest", 1)
    result = retention.cleanup(db, settings(), ISIN)
    assert result["items"][0]["counts"]["quotes"] == 0
    assert db.quotes.count_documents({"job_id": "pending-review"}) == 1
    assert "retention_state" not in db.acquisition_jobs.find_one({"_id": "pending-review"})


def test_missing_job_id_on_tagged_orphan_is_not_guessed(db):
    db.quotes.insert_one({"_id": "synthetic-orphan", "instrument_id": ISIN, "provider": "marketstack", "date": "2020-01-01"})
    db.quotes.insert_one({"_id": "ambiguous", "instrument_id": ISIN, "date": "2020-01-02"})
    result = retention.cleanup(db, settings(), ISIN)
    assert result["items"][0]["counts"]["quotes"] == 1
    assert db.market_data_retention.count_documents({}) == 0
    assert list(db.quotes.find()) == [{"_id": "ambiguous", "instrument_id": ISIN, "date": "2020-01-02"}]


@pytest.mark.parametrize("collision_collection", ["acquisition_jobs", "market_data_publications"])
@pytest.mark.parametrize("collision", ["provider", "instrument"])
def test_legacy_identity_collisions_never_prune_other_owner(db, collision_collection, collision):
    job = seed(db, "synthetic-collision", 0, provider="marketstack", published=False)
    foreign = {**job, **({"provider": "yfinance", "published": True} if collision == "provider" else {"instrument_id": "OTHER-FIXTURE"})}
    db[collision_collection].replace_one({"_id": job["_id"]}, foreign)
    # Existing audit must be marked as conflicting, not replayed against another owner.
    db.market_data_retention.insert_one({"_id": job["_id"], "instrument_id": ISIN,
                                        "provider": "marketstack", "state": "retiring"})
    before = deepcopy(db[collision_collection].find_one({"_id": job["_id"]}))
    retention.cleanup(db, settings(), ISIN)
    assert db[collision_collection].find_one({"_id": job["_id"]}) == before
    assert db.market_data_retention.find_one({"_id": job["_id"]})["state"] == "ownership_conflict"
    assert db.acquisition_jobs.count_documents({"status": "maintenance"}) == 0


@pytest.mark.parametrize("apply", [False, True])
def test_retention_cli_uses_selected_database_and_default_inventory(db, monkeypatch, capsys, apply):
    seed(db, "older", 0)
    seed(db, "latest", 1)
    cfg = settings(mongodb_uri="mongodb://synthetic.invalid:27017", mongodb_database=db.name)
    monkeypatch.setattr("config.TaskSettings", Mock(return_value=cfg))
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.__getitem__ = Mock(return_value=db)
    factory = Mock(return_value=client)
    monkeypatch.setattr("pymongo.MongoClient", factory)
    monkeypatch.setattr(sys, "argv", ["retention-fixture", *(["--apply"] if apply else [])])
    retention.main()
    result = json.loads(capsys.readouterr().out)
    factory.assert_called_once_with("mongodb://synthetic.invalid:27017", serverSelectionTimeoutMS=5000)
    client.__getitem__.assert_called_once_with(db.name)
    client.__exit__.assert_called_once()
    assert db.quotes.count_documents({"job_id": "older"}) == (0 if apply else 1)
    assert db.quotes.count_documents({"job_id": "latest"}) == 1
    if apply:
        assert result["items"][0]["counts"]["quotes"] == 1
    else:
        assert result["mode"] == "inventory" and result["payloads"]["quotes"]["yahoo"] == 2


def test_retention_cli_refuses_conflicting_modes_before_constructing_client(monkeypatch, capsys):
    client = Mock(side_effect=AssertionError("Client must never be created"))
    monkeypatch.setattr("pymongo.MongoClient", client)
    monkeypatch.setattr(sys, "argv", ["retention-fixture", "--apply", "--inventory"])
    with pytest.raises(SystemExit) as error:
        retention.main()
    assert error.value.code == 2
    assert "Choose inventory or apply, not both" in capsys.readouterr().err
    client.assert_not_called()
