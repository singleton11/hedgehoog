"""Opt-in real Mongo split corrections and concurrent writes; no provider calls."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import Mock
from uuid import uuid4

from fastapi.testclient import TestClient

import market_data as md
from main import create_app
from manual_splits import ManualSplitRequest, add_split, ensure_indexes, remove_split
from return_statistics import instrument_statistics
from split_adjustments import load_split_snapshot
from test_market_data_integration import ISIN, LISTING, NOW, environment


def seed(db):
    ensure_indexes(db)
    db.instrument_state.insert_one({"_id": ISIN, "listing": LISTING})
    for day, close in (("2026-09-14", "250"), ("2026-09-15", "10"), ("2026-09-16", "10")):
        db.quotes.insert_one({**md.listing_filter(ISIN, LISTING), "date": day, "collected_at": NOW,
                              "data": {"close": close, "adj_close": close, "price_currency": "EUR"}})
    db.split_snapshots.insert_one({"_id": "provider-empty", **md.listing_filter(ISIN, LISTING),
                                  "collected_at": NOW, "requested_at": NOW, "date_from": "2010-05-19",
                                  "date_to": "2026-09-16", "events": []})


def request_body(**changes):
    return {"id": str(uuid4()), **LISTING, "date": "2026-09-15", "factor": "25", "note": "Issuer notice", **changes}


def test_real_mongo_api_statistics_removal_history_and_replacement(environment, monkeypatch):
    settings, db = environment
    seed(db)
    original_quotes = list(db.quotes.find())
    monkeypatch.setattr("main.enqueue_etf_import", Mock())
    with TestClient(create_app(settings)) as client:
        assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
        before = instrument_statistics(db, ISIN)
        path = f"/instruments/{ISIN}/splits"
        body = request_body()
        learned = NOW + timedelta(seconds=1)
        monkeypatch.setattr(md, "utcnow", lambda: learned)
        response = client.post(path, json=body)
        assert response.status_code == 201, response.text
        assert client.post(path, json=body).json() == response.json()
        assert instrument_statistics(db, ISIN)["daily_volatility"] == 0
        assert client.get(path).json()["manual"] == [response.json()["item"]]
        monkeypatch.setattr(md, "utcnow", lambda: learned + timedelta(seconds=1))
        assert client.delete(path + "/" + body["id"]).status_code == 204
        assert instrument_statistics(db, ISIN)["daily_volatility"] == before["daily_volatility"]
        assert client.get(path).json()["manual"] == []
        assert load_split_snapshot(db, ISIN, LISTING, learned)["events"] == [{"date": body["date"], "factor": "25"}]
        assert client.post(path, json=body).status_code == 409
        replacement = request_body()
        assert client.post(path, json=replacement).status_code == 201
        ensure_indexes(db)  # Partial unique index recreation remains idempotent.
        assert client.delete(path + "/" + body["id"]).status_code == 204
        assert client.get(path).json()["manual"][0]["id"] == replacement["id"]
    assert db.manual_splits.count_documents({}) == 2
    assert list(db.quotes.find()) == original_quotes
    assert db.marketstack_usage.count_documents({}) == db.acquisition_jobs.count_documents({}) == 0


def test_real_mongo_concurrent_same_date_and_idempotent_requests(environment, monkeypatch):
    _, db = environment
    seed(db)
    gate = Barrier(8)
    insert = db.manual_splits.insert_one

    def simultaneous_insert(*args, **kwargs):
        gate.wait(timeout=10)
        return insert(*args, **kwargs)

    monkeypatch.setattr(db.manual_splits, "insert_one", simultaneous_insert)

    def create(body):
        try:
            return add_split(db, ISIN, ManualSplitRequest(**body), "user")
        except md.DataError as error:
            assert error.status == 409
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create, [request_body() for _ in range(8)]))
    assert sum(result is not None for result in results) == 1
    winner = next(result["item"] for result in results if result)
    assert db.manual_splits.count_documents({"active": True}) == 1
    remove_split(db, ISIN, winner["id"], "user")
    same_request = request_body()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create, [same_request] * 8))
    assert all(result == results[0] for result in results)
    assert results[0] is not None
    assert db.manual_splits.count_documents({"active": True}) == 1
    assert db.manual_splits.count_documents({}) == 2
