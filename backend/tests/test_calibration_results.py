from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from threading import Event
from unittest.mock import Mock

from bson import ObjectId
import pytest
from fastapi.testclient import TestClient
from pymongo.errors import ConnectionFailure

import calibration_results as saved
import goal_strategies as gs
import market_data as md
from auth import COOKIE_NAME, create_token
from goal_calibration import calibration_metrics
from test_goal_calibration import assert_same_calculation, body, inputs, rows_between
from test_goal_planner import login, seed_history, seed_profile
from test_market_data import ISIN, NOW, api, clock, config, db

STRATEGY = "buy_and_hold_single_instrument"
URL = f"/goals/strategies/{STRATEGY}/calibration"
HISTORY = "/goals/calibrations"


def result(seed=42, strategy=STRATEGY):
    return {"strategy": {"id": strategy, "parameters": {"instrument": ISIN}}, "seed": seed,
            "calculated_at": md.public(NOW), "settings": {"mode": "random"},
            "brier_score": 0.2, "calibration_error": 0.1, "sample_count": 10, "origin_count": 5,
            "forecasts": [{"predicted_probability": 0.2}], "calibration_bins": [{"count": 10}]}


def seed_legacy(db, user_id, strategy=STRATEGY, seed=42, order="0001"):
    legacy = {"_id": saved.result_key(user_id, strategy), "order": order, "result": result(seed, strategy)}
    legacy["summary"] = {key: value for key, value in legacy["result"].items() if key in saved.SUMMARY_FIELDS}
    db.strategy_calibrations.insert_one(legacy)
    return legacy


def test_calibration_error_weights_absolute_bin_gaps_by_samples():
    forecasts = [{"predicted_probability": p, "observed_success": y}
                 for p, y in [(0.1, False), (0.11, True), (0.19, False), (0.95, False)]]
    metrics = calibration_metrics(forecasts)
    assert metrics["calibration_error"] == pytest.approx((3 * abs(0.4 / 3 - 1 / 3) + 0.95) / 4)
    assert metrics["calibration_error"] == pytest.approx(0.3875)
    assert metrics["calibration_error"] != metrics["brier_score"]


@pytest.mark.parametrize("cases,expected", [
    ([(0.5, False), (0.5, True)], 0), ([(0, True), (1, False)], 1),
    ([(0, False), (1, True)], 0), ([(0.25, False)], 0.25),
])
def test_calibration_error_perfect_extreme_and_empty_bins(cases, expected):
    assert calibration_metrics([{"predicted_probability": p, "observed_success": y}
                                for p, y in cases])["calibration_error"] == expected


@pytest.mark.parametrize("endpoint", [URL, HISTORY, f"{HISTORY}/{ObjectId()}"])
def test_saved_endpoints_require_auth_and_no_store(api, endpoint):
    client, _ = api
    response = client.get(endpoint)
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"


def test_empty_history_and_unknown_detail_are_distinct(api):
    client, _ = api
    login(client)
    assert client.get(URL).json() == {"result": None}
    assert client.get(HISTORY).json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
    for run_id in (str(ObjectId()), "not-an-object-id", "legacy-unknown"):
        response = client.get(f"{HISTORY}/{run_id}")
        assert response.status_code == 404
        assert response.headers["Cache-Control"] == "no-store"
    assert client.get("/goals/strategies/no-such-strategy/calibration").status_code == 404
    assert client.get("/goals/strategies").json()["items"][0]["latest_calibration"] is None


def test_calculation_persists_and_reads_use_saved_reports_and_projected_summaries(api, db, monkeypatch):
    client, _ = api
    seed_history(db, rows_between())
    seed_profile(db)
    login(client)
    response = client.post("/goals/calibrate", json=body(seed=0))
    assert response.status_code == 200, response.text
    full = response.json()
    assert response.headers["Cache-Control"] == "no-store"
    assert full["calculated_at"] == md.public(NOW)
    assert ObjectId.is_valid(full["id"]) and isinstance(full["id"], str)
    assert full["strategy_name"] == gs.STRATEGIES[STRATEGY].name
    assert 0 <= full["calibration_error"] <= 1
    document = db.calibration_runs.find_one({"_id": full["id"]})
    assert document["user_id"] == str(db.users.find_one({"login": "admin"})["_id"])
    assert document["strategy_id"] == STRATEGY
    assert document["result"] == full
    summary = {**{key: full[key] for key in saved.SUMMARY_FIELDS}, "generation": full["generation"]}
    assert document["summary"] == summary
    assert db.strategy_calibrations.count_documents({}) == 0
    forbidden = Mock(side_effect=AssertionError("Saved reads must not simulate or load instrument data"))
    monkeypatch.setattr(db.quotes, "aggregate", forbidden)
    monkeypatch.setattr(db.instruments, "find_one", forbidden)
    monkeypatch.setattr(gs, "instrument_statistics", forbidden)
    definition = gs.STRATEGIES[STRATEGY]
    monkeypatch.setitem(gs.STRATEGIES, STRATEGY, replace(definition, calibrate=forbidden))
    loaded = client.get(URL)
    assert loaded.headers["Cache-Control"] == "no-store"
    assert loaded.json() == {"result": full}
    assert client.get(f"{HISTORY}/{full['id']}").json() == {"result": full}
    # Every collection read while listing is projected: only migration IDs or
    # lightweight summaries, never the reports' thousands of forecast rows.
    find = Mock(wraps=db.calibration_runs.find)
    monkeypatch.setattr(db.calibration_runs, "find", find)
    catalogue = client.get("/goals/strategies")
    assert catalogue.headers["Cache-Control"] == "no-store"
    assert catalogue.json()["items"][0]["latest_calibration"] == summary
    history = client.get(HISTORY)
    assert history.headers["Cache-Control"] == "no-store"
    assert history.json() == {"items": [summary], "total": 1, "limit": 20, "offset": 0}
    assert find.call_count >= 2
    for call in find.call_args_list:
        assert call.args[0]["user_id"] == document["user_id"]
        assert call.args[1] in ({"_id": 1}, {"summary": 1, "_id": 0})
    assert "forecasts" not in summary and "calibration_bins" not in summary
    assert "_id" not in summary and "order" not in summary
    with TestClient(client.app) as reopened:
        login(reopened)
        assert reopened.get(URL).json()["result"] == full
        assert reopened.get(f"{HISTORY}/{full['id']}").json()["result"] == full
    forbidden.assert_not_called()


def test_identical_seed_runs_have_unique_ids_and_do_not_mutate_previous_reports(api, db):
    client, _ = api
    login(client)
    seed_history(db, rows_between())
    seed_profile(db)
    first = client.post("/goals/calibrate", json=body(seed=42)).json()
    original = db.calibration_runs.find_one({"_id": first["id"]})
    second = client.post("/goals/calibrate", json=body(seed=42)).json()
    assert_same_calculation(first, second)
    assert db.calibration_runs.count_documents({}) == 2
    assert db.calibration_runs.find_one({"_id": first["id"]}) == original
    assert client.get(URL).json()["result"] == second
    assert client.get(f"{HISTORY}/{first['id']}").json()["result"] == first
    assert [item["id"] for item in client.get(HISTORY).json()["items"]] == [second["id"], first["id"]]


def test_failed_recalculation_keeps_all_history_and_reports_survive_instrument_removal(api, db, clock):
    client, _ = api
    login(client)
    seed_history(db, rows_between())
    seed_profile(db)
    first = client.post("/goals/calibrate", json=body(seed=42)).json()
    clock[0] += timedelta(seconds=1)
    second = client.post("/goals/calibrate", json=body(seed=43, scenario_count=25)).json()
    assert second["seed"] == 43 and second["calculated_at"] != first["calculated_at"]
    assert second["sample_count"] == 25
    documents = list(db.calibration_runs.find())
    db.quotes.delete_many({})
    assert client.post("/goals/calibrate", json=body(seed=44)).status_code == 422
    db.instruments.delete_many({})
    db.instrument_state.delete_many({})
    assert client.post("/goals/calibrate", json=body(seed=44)).status_code == 422
    assert client.get(URL).json()["result"] == second
    assert client.get(f"{HISTORY}/{first['id']}").json()["result"] == first
    assert client.get(f"{HISTORY}/{second['id']}").json()["result"] == second
    assert client.get(HISTORY).json()["total"] == 2
    assert list(db.calibration_runs.find()) == documents


def test_saved_results_are_isolated_including_guessed_ids_and_query_owner(api, db, config):
    client, _ = api
    login(client)
    seed_history(db, rows_between())
    seed_profile(db)
    admin_result = client.post("/goals/calibrate", json=body(seed=42)).json()
    admin_id = str(db.users.find_one({"login": "admin"})["_id"])
    other_id = db.users.insert_one({"login": "another", "role": "user", "disabled": False}).inserted_id
    with TestClient(client.app) as other:
        other.cookies.set(COOKIE_NAME, create_token(str(other_id), config))
        assert other.get(URL).json() == {"result": None}
        assert other.get("/goals/strategies").json()["items"][0]["latest_calibration"] is None
        assert other.get(HISTORY, params={"user_id": admin_id}).json()["total"] == 0
        for params in ({}, {"user_id": admin_id}):
            response = other.get(f"{HISTORY}/{admin_result['id']}", params=params)
            assert response.status_code == 404
            assert response.headers["Cache-Control"] == "no-store"
        response = other.post("/goals/calibrate", json=body(seed=43))
        assert response.status_code == 200, response.text
        other_result = response.json()
        assert other.get(URL).json()["result"] == other_result
        assert other.get(HISTORY).json()["total"] == 1
    assert client.get(URL).json()["result"] == admin_result
    assert client.get(f"{HISTORY}/{other_result['id']}").status_code == 404
    assert client.get(HISTORY).json()["total"] == 1
    assert db.calibration_runs.count_documents({}) == 2


def test_old_slow_run_is_retained_but_latest_uses_start_order_and_owner_strategy(db, clock):
    old_order = saved.calculation_order()
    clock[0] += timedelta(microseconds=1)
    new_order = saved.calculation_order()
    latest = saved.save_result(db, "user-a", STRATEGY, result(2), new_order)
    clock[0] += timedelta(seconds=1)
    earlier = saved.save_result(db, "user-a", STRATEGY, result(1), old_order)
    assert saved.latest_result(db, "user-a", STRATEGY) == latest
    saved.save_result(db, "user-a", "another-strategy", result(3, "another-strategy"), new_order,
                      strategy_name="Another strategy")
    saved.save_result(db, "user-b", STRATEGY, result(4), new_order)
    assert db.calibration_runs.count_documents({}) == 4
    assert saved.run_result(db, "user-a", earlier["id"]) == earlier
    assert saved.latest_summaries(db, "user-a", [STRATEGY])[STRATEGY]["seed"] == 2
    assert saved.latest_summaries(db, "user-b", [STRATEGY])[STRATEGY]["seed"] == 4
    assert saved.latest_summaries(db, "user-c", [STRATEGY]) == {}


def test_concurrent_requests_allocate_start_order_before_work_and_keep_both_successes(api, db, monkeypatch):
    client, _ = api
    login(client)
    seed_profile(db)
    started, release = Event(), Event()

    def calculate(parameters, settings, database):
        if settings.seed == 1:
            started.set()
            assert release.wait(timeout=10), "Newer request did not complete"
        return result(settings.seed)

    monkeypatch.setitem(gs.STRATEGIES, STRATEGY, replace(gs.STRATEGIES[STRATEGY], calibrate=calculate))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, "/goals/calibrate", json=body(seed=1))
        try:
            assert started.wait(timeout=10), "Older request did not start"
            newer = client.post("/goals/calibrate", json=body(seed=2))
            assert newer.status_code == 200, newer.text
        finally:
            release.set()
        older = future.result(timeout=10)
    assert older.status_code == 200, older.text
    assert older.json()["id"] > newer.json()["id"]  # ID allocated at completion, not start.
    assert client.get(URL).json()["result"] == newer.json()
    assert [item["id"] for item in client.get(HISTORY).json()["items"]] == [
        newer.json()["id"], older.json()["id"],
    ]
    assert client.get(f"{HISTORY}/{older.json()['id']}").json()["result"] == older.json()
    assert db.calibration_runs.count_documents({}) == 2


def test_history_pagination_and_equal_order_tie_break_use_run_id_descending(api, db, monkeypatch):
    client, _ = api
    login(client)
    owner = db.users.find_one({"login": "admin"})["_id"]
    ids = iter(ObjectId(value) for value in ["f" * 24, "0" * 23 + "1", "a" * 24, "b" * 24])
    monkeypatch.setattr(saved, "ObjectId", lambda: next(ids))
    for seed in range(3):
        saved.save_result(db, owner, STRATEGY, result(seed), "same-order")
    assert saved.latest_result(db, owner, STRATEGY)["id"] == "f" * 24
    assert saved.latest_summaries(db, owner, [STRATEGY])[STRATEGY]["id"] == "f" * 24
    newest = saved.save_result(db, owner, STRATEGY, result(3), "z-newer-order")
    expected_ids = [newest["id"], "f" * 24, "a" * 24, "0" * 23 + "1"]
    first = client.get(HISTORY, params={"limit": 2, "offset": 0}).json()
    second = client.get(HISTORY, params={"limit": 2, "offset": 2}).json()
    assert first["total"] == second["total"] == 4
    assert first["limit"] == second["limit"] == 2
    assert first["offset"] == 0 and second["offset"] == 2
    assert [item["id"] for item in first["items"] + second["items"]] == expected_ids
    assert client.get(HISTORY, params={"limit": 100, "offset": 4}).json() == {
        "items": [], "total": 4, "limit": 100, "offset": 4,
    }
    assert client.get(HISTORY, params={"limit": 1}).json()["items"][0]["id"] == newest["id"]
    assert saved.latest_result(db, owner, STRATEGY) == newest


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": -1}, {"limit": 101}, {"offset": -1},
                                    {"limit": "bad"}, {"offset": "bad"}, {"limit": 1.5}, {"offset": 0.5}])
def test_invalid_history_pagination_is_rejected_before_reads(api, db, monkeypatch, params):
    client, _ = api
    login(client)
    forbidden = Mock(side_effect=AssertionError("Validate before reading runs"))
    monkeypatch.setattr(db.calibration_runs, "find", forbidden)
    response = client.get(HISTORY, params=params)
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    forbidden.assert_not_called()


def test_new_strategy_snapshots_name_and_retired_runs_remain_readable(api, db, monkeypatch):
    client, _ = api
    login(client)
    seed_profile(db)
    definition = replace(gs.STRATEGIES[STRATEGY], id="future-strategy", name="Original future name",
                         calibrate=Mock(return_value=result(9)))
    monkeypatch.setitem(gs.STRATEGIES, definition.id, definition)
    selection = {"id": definition.id, "parameters": {"instrument": ISIN}}
    first = client.post("/goals/calibrate", json={**body(), "strategy": selection}).json()
    assert first["strategy"] == selection and first["strategy_name"] == definition.name
    monkeypatch.setitem(gs.STRATEGIES, definition.id, replace(definition, name="Renamed strategy"))
    second = client.post("/goals/calibrate", json={**body(), "strategy": selection}).json()
    assert second["strategy_name"] == "Renamed strategy"
    monkeypatch.delitem(gs.STRATEGIES, definition.id)
    db.instruments.delete_many({})
    assert client.get(f"{HISTORY}/{first['id']}").json() == {"result": first}
    assert client.get(f"{HISTORY}/{second['id']}").json() == {"result": second}
    assert [item["strategy_name"] for item in client.get(HISTORY).json()["items"]] == [
        "Renamed strategy", "Original future name",
    ]
    assert client.get(f"/goals/strategies/{definition.id}/calibration").status_code == 404


@pytest.mark.parametrize("first_read", ["history", "detail", "latest", "catalogue", "save"])
def test_legacy_import_precedes_every_entrypoint_is_once_and_preserves_source(api, db, monkeypatch, first_read):
    client, _ = api
    login(client)
    owner = db.users.find_one({"login": "admin"})["_id"]
    legacy = seed_legacy(db, owner)
    original = deepcopy(legacy)
    legacy_id = f"legacy-{legacy['_id']}"
    expected = {**legacy["result"], "id": legacy_id, "strategy_name": gs.STRATEGIES[STRATEGY].name}
    upsert = Mock(wraps=db.calibration_runs.update_one)
    monkeypatch.setattr(db.calibration_runs, "update_one", upsert)
    if first_read == "save":
        seed_profile(db)
        monkeypatch.setitem(gs.STRATEGIES, STRATEGY, replace(gs.STRATEGIES[STRATEGY], calibrate=Mock(return_value=result(7))))
        assert client.post("/goals/calibrate", json=body()).status_code == 200
    else:
        endpoint = {"history": HISTORY, "detail": f"{HISTORY}/{legacy_id}", "latest": URL,
                    "catalogue": "/goals/strategies"}[first_read]
        assert client.get(endpoint).status_code == 200
    assert client.get(f"{HISTORY}/{legacy_id}").json() == {"result": expected}
    assert client.get(HISTORY).json()["total"] == (2 if first_read == "save" else 1)
    if first_read != "save":
        assert client.get(URL).json() == {"result": expected}
        assert client.get("/goals/strategies").json()["items"][0]["latest_calibration"] == {
            key: expected[key] for key in saved.SUMMARY_FIELDS
        }
    imported = db.calibration_runs.find_one({"_id": legacy_id})
    assert imported["user_id"] == str(owner) and imported["strategy_id"] == STRATEGY
    assert imported["order"] == legacy["order"]
    assert db.strategy_calibrations.find_one({"_id": legacy["_id"]}) == original
    upsert.assert_called_once()
    assert set(upsert.call_args.args[1]) == {"$setOnInsert"}
    assert upsert.call_args.args[0] == {"_id": legacy_id, "user_id": str(owner)}
    # A legacy writer or renamed definition cannot mutate an imported snapshot.
    db.strategy_calibrations.update_one({"_id": legacy["_id"]}, {"$set": {"result.seed": 999}})
    monkeypatch.setitem(gs.STRATEGIES, STRATEGY, replace(gs.STRATEGIES[STRATEGY], name="Changed"))
    assert client.get(f"{HISTORY}/{legacy_id}").json() == {"result": expected}
    assert db.calibration_runs.find_one({"_id": legacy_id}) == imported
    upsert.assert_called_once()


def test_legacy_discovery_is_owner_and_registered_strategy_scoped_without_global_scan(api, db, config, monkeypatch):
    client, _ = api
    login(client)
    owner = db.users.find_one({"login": "admin"})["_id"]
    other = db.users.insert_one({"login": "other", "role": "user", "disabled": False}).inserted_id
    own_legacy = seed_legacy(db, owner)
    other_legacy = seed_legacy(db, other)
    future_legacy = seed_legacy(db, owner, strategy="future")
    find = Mock(wraps=db.strategy_calibrations.find)
    monkeypatch.setattr(db.strategy_calibrations, "find", find)
    assert client.get(HISTORY).json()["total"] == 1
    assert db.calibration_runs.count_documents({}) == 1
    assert client.get(f"{HISTORY}/legacy-{other_legacy['_id']}").status_code == 404
    assert client.get(f"{HISTORY}/legacy-{future_legacy['_id']}").status_code == 404
    for call in find.call_args_list:
        assert call.args[0] == {"_id": {"$in": [own_legacy["_id"]]}}
    # Newly registered strategies remain importable even after previous reads.
    monkeypatch.setitem(gs.STRATEGIES, "future", replace(gs.STRATEGIES[STRATEGY], id="future", name="Future"))
    assert client.get(HISTORY).json()["total"] == 2
    assert client.get(f"{HISTORY}/legacy-{future_legacy['_id']}").json()["result"]["strategy_name"] == "Future"
    with TestClient(client.app) as another:
        another.cookies.set(COOKIE_NAME, create_token(str(other), config))
        history = another.get(HISTORY).json()
        assert history["total"] == 1
        assert history["items"][0]["id"] == f"legacy-{other_legacy['_id']}"
        assert another.get(f"{HISTORY}/legacy-{own_legacy['_id']}").status_code == 404
    assert db.calibration_runs.count_documents({}) == db.strategy_calibrations.count_documents({}) == 3


def test_failed_atomic_insert_never_changes_an_existing_report(api, db, monkeypatch):
    client, _ = api
    login(client)
    seed_profile(db)
    monkeypatch.setitem(gs.STRATEGIES, STRATEGY, replace(gs.STRATEGIES[STRATEGY], calibrate=Mock(return_value=result())))
    first = client.post("/goals/calibrate", json=body()).json()
    documents = list(db.calibration_runs.find())
    monkeypatch.setattr(db.calibration_runs, "insert_one", Mock(side_effect=ConnectionFailure("temporary")))
    assert client.post("/goals/calibrate", json=body()).status_code == 503
    assert list(db.calibration_runs.find()) == documents
    assert client.get(URL).json()["result"] == first
    assert client.get(HISTORY).json()["total"] == 1


def test_indexes_cover_owner_order_and_owner_strategy_order_and_are_idempotent(api, db):
    # The app lifespan, not the first query, installs the indexes.
    indexes = db.calibration_runs.index_information()
    saved.ensure_indexes(db)
    saved.ensure_indexes(db)
    assert db.calibration_runs.index_information() == indexes
    assert indexes["calibration_runs_owner_order"]["key"] == [("user_id", 1), *saved.RUN_ORDER]
    assert indexes["calibration_runs_owner_strategy_order"]["key"] == [
        ("user_id", 1), ("strategy_id", 1), *saved.RUN_ORDER,
    ]
    assert indexes["_id_"]["key"] == [("_id", 1)]


def test_single_scenario_is_removed_from_public_api(api, db, monkeypatch):
    client, _ = api
    login(client)
    forbidden = Mock(side_effect=AssertionError("Manual scenarios must fail before data access"))
    monkeypatch.setattr(db.instruments, "find_one", forbidden)
    request = {**inputs().model_dump(mode="json"), "strategy": body()["strategy"]}
    assert client.post("/goals/calibrate", json=request).status_code == 422
    assert client.get(URL).json() == {"result": None}
    assert client.get(HISTORY).json()["total"] == 0
    forbidden.assert_not_called()
