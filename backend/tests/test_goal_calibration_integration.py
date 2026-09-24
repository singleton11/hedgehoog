"""Opt-in real Mongo calibration using only a disposable, synthetic history."""

from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from bson import Decimal128
from fastapi.testclient import TestClient

import calibration_results as saved
import market_data as md
from main import create_app
from goal_calibration import MAX_HISTORY_ROWS
from test_goal_calibration import assert_same_calculation, body, rows_between
from test_calibration_results import seed_legacy
from test_goal_planner import seed_history, seed_profile
from test_market_data_integration import ISIN, LISTING, NOW, environment


def test_real_mongo_snapshot_projection_and_seeded_calibration(environment, monkeypatch):
    settings, db = environment
    md.ensure_indexes(db)
    seed_history(db, [{**row, "adj_close": Decimal128(str(row["adj_close"])),
                       "close": Decimal128(str(row["close"]))} for row in rows_between()])
    seed_profile(db)
    monkeypatch.setattr("main.enqueue_etf_import", Mock())
    provider = Mock(side_effect=AssertionError("A calibration read must not call Marketstack"))
    monkeypatch.setattr(md, "MarketstackClient", provider)
    with TestClient(create_app(settings)) as client:
        assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
        response = client.post("/goals/calibrate", json=body(seed=4294967295))
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["sample_count"] == result["candidate_count"] == 100
        assert result["origin_count"] == 35
        assert sum(bin["count"] for bin in result["calibration_bins"]) == result["sample_count"]
        repeated = client.post("/goals/calibrate", json=body(seed=4294967295)).json()
        assert_same_calculation(result, repeated)
        assert db.calibration_runs.count_documents({}) == 2
        assert client.get(f"/goals/calibrations/{result['id']}").json() == {"result": result}
        # A latest observation containing no projected price/currency fields
        # must remain an invalid row, not cause a KeyError or revive the old one.
        original = db.quotes.find_one({"date": "2023-01-02"})
        original.pop("_id")
        original.update(job_id="empty-revision", data={"date": "2023-01-02"})
        db.quotes.insert_one(original)
        updated = client.post("/goals/calibrate", json=body(seed=4294967295))
        assert updated.status_code == 200, updated.text
        assert updated.json()["history_fingerprint"] != result["history_fingerprint"]
        assert updated.json()["skipped_counts"]["training_currency"] > 0
    provider.assert_not_called()
    assert md.budget_status(db, settings)["used"] == 0


def test_real_mongo_concurrent_runs_and_legacy_import_are_atomic_and_start_ordered(environment):
    from calibration_results import latest_result, latest_summaries, save_result
    _, db = environment
    saved.ensure_indexes(db)
    saved.ensure_indexes(db)
    strategy = "buy_and_hold_single_instrument"
    legacy = seed_legacy(db, "owner", order="-old")
    other_legacy = seed_legacy(db, "another-owner", seed=100, order="-old")

    def complete(number):
        result = {"strategy": {"id": strategy, "parameters": {"instrument": ISIN}},
                  "seed": number, "calculated_at": md.public(NOW), "settings": {"mode": "random"},
                  "brier_score": number / 100, "calibration_error": number / 100,
                  "sample_count": 10, "origin_count": 2}
        return save_result(db, "owner", strategy, result, f"{number:04d}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(complete, [6, 4, 1, 9, 2, 7, 0, 5, 3, 8]))
    assert len({item["id"] for item in results}) == 10
    assert db.calibration_runs.count_documents({}) == 11
    assert db.calibration_runs.count_documents({"user_id": "another-owner"}) == 0
    assert db.strategy_calibrations.find_one({"_id": legacy["_id"]}) == legacy
    for document in db.calibration_runs.find():
        assert document["summary"] == {key: document["result"][key] for key in saved.SUMMARY_FIELDS}
        assert saved.run_result(db, "owner", document["_id"]) == document["result"]
    assert [item["seed"] for item in saved.run_history(db, "owner", limit=3, offset=2)["items"]] == [7, 6, 5]
    # A slow older success finishing after all parallel writes remains visible.
    slow = complete(-1)
    assert saved.run_result(db, "owner", slow["id"]) == slow
    assert db.calibration_runs.count_documents({}) == 12
    assert latest_result(db, "owner", strategy)["seed"] == 9
    assert latest_summaries(db, "owner", [strategy])[strategy]["seed"] == 9
    assert latest_result(db, "unrelated-owner", strategy) is None
    assert latest_summaries(db, "unrelated-owner", [strategy]) == {}
    assert latest_result(db, "another-owner", strategy)["id"] == f"legacy-{other_legacy['_id']}"
    assert db.calibration_runs.count_documents({}) == 13
    assert db.strategy_calibrations.count_documents({}) == 2


def test_real_mongo_twenty_thousand_date_guard_is_not_silent_truncation(environment, monkeypatch):
    settings, db = environment
    seed_profile(db)
    db.instrument_state.insert_one({"_id": ISIN, "listing": LISTING})
    rows = [{"instrument_id": ISIN, **LISTING, "date": (date(1940, 1, 1) + timedelta(days=index)).isoformat(),
             "collected_at": NOW, "job_id": f"synthetic-{index}",
             "data": {"close": Decimal128("100.00"), "adj_close": Decimal128("100.00"), "price_currency": "EUR"}}
            for index in range(MAX_HISTORY_ROWS + 1)]
    db.quotes.insert_many(rows)
    monkeypatch.setattr("main.enqueue_etf_import", Mock())
    with TestClient(create_app(settings)) as client:
        assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
        response = client.post("/goals/calibrate", json=body())
        assert response.status_code == 422
        assert "20,000-date" in response.json()["detail"]
        assert "silently truncated" in response.json()["detail"]
    assert db.quotes.count_documents({}) == MAX_HISTORY_ROWS + 1


def test_real_mongo_default_generated_batch_replays_seed_and_separates_origin_count(environment, monkeypatch):
    settings, db = environment
    seed_history(db, [{**row, "adj_close": Decimal128(str(row["adj_close"])),
                       "close": Decimal128(str(row["close"]))} for row in rows_between()])
    seed_profile(db)
    monkeypatch.setattr("main.enqueue_etf_import", Mock())
    provider = Mock(side_effect=AssertionError("Generated batches must not acquire anything"))
    monkeypatch.setattr(md, "MarketstackClient", provider)
    request = {"mode": "random", "seed": 42, "strategy": body()["strategy"]}
    with TestClient(create_app(settings)) as client:
        assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
        response = client.post("/goals/calibrate", json=request)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["settings"]["mode"] == "random"
        assert result["candidate_count"] == result["sample_count"] == 1000
        assert result["origin_count"] == 32
        assert len(result["forecasts"]) == 1000
        assert len({case["scenario_id"] for case in result["forecasts"]}) == 1000
        assert all(bin["count"] > 0 for bin in result["calibration_bins"])
        repeated = client.post("/goals/calibrate", json=request).json()
        assert_same_calculation(result, repeated)
        assert 0 <= result["calibration_error"] <= 1
        endpoint = f"/goals/strategies/{request['strategy']['id']}/calibration"
        assert client.get(endpoint).json()["result"] == repeated
        history = client.get("/goals/calibrations", params={"limit": 1, "offset": 1}).json()
        assert history["total"] == 2 and history["items"][0]["id"] == result["id"]
        assert client.get(f"/goals/calibrations/{result['id']}").json() == {"result": result}
        summary = client.get("/goals/strategies").json()["items"][0]["latest_calibration"]
        assert summary["brier_score"] == result["brier_score"]
        assert summary["calibration_error"] == result["calibration_error"]
        assert "forecasts" not in summary
    # A new app/client sees both immutable reports after source data removal.
    db.quotes.delete_many({})
    db.instruments.delete_many({})
    with TestClient(create_app(settings)) as restarted:
        assert restarted.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
        assert restarted.get(endpoint).json()["result"] == repeated
        assert restarted.get(f"/goals/calibrations/{result['id']}").json() == {"result": result}
        assert restarted.get("/goals/calibrations").json()["total"] == 2
    provider.assert_not_called()
    assert md.budget_status(db, settings)["used"] == 0
