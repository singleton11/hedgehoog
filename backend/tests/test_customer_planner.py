from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import Mock

from bson import ObjectId
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field
from pymongo.errors import ConnectionFailure
import pytest

from auth import COOKIE_NAME, create_token
import customer_planner as planner_api
import goal_strategies as gs
import market_data as md
from goal_simulation import GoalInputs
from test_goal_planner import login, request_body, seed_history, seed_profile
from test_market_data import ISIN, NOW, api, clock, config, db, enqueue

STRATEGY = "buy_and_hold_single_instrument"
GOAL = {"name": "Retirement", "goal_eur": "100000", "goal_date": "2036-09-17"}


def empty_summary(customer, goal_count=0):
    return {**customer, "net_worth_eur": "0.00", "goal_count": goal_count,
            "realized_pnl_eur": "0.00", "unrealized_pnl_eur": "0.00", "today_pnl_eur": "0.00", "total_pnl_eur": "0.00",
            "cost_basis_eur": "0.00", "previous_value_eur": "0.00", "today_from_date": None, "today_to_date": None}


def selection(**changes):
    return {"strategy": {"id": STRATEGY, "parameters": {"instrument": ISIN}},
            "starting_capital_eur": "10000", "reliability": 0.9, **changes}


def target(client, customer_name="Customer", **goal_changes):
    response = client.post("/customers", json={"name": customer_name})
    assert response.status_code == 201, response.text
    customer = response.json()
    response = client.post(f"/customers/{customer['id']}/goals", json={**GOAL, **goal_changes})
    assert response.status_code == 201, response.text
    goal = response.json()
    return customer, goal, f"/customers/{customer['id']}/goals/{goal['id']}"


def calculate(client, url, **changes):
    response = client.post(url + "/simulate", json=selection(**changes))
    assert response.status_code == 200, response.text
    return response.json()


def save(client, url, preview):
    response = client.post(f"{url}/simulations/{preview['id']}/save")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture(autouse=True)
def ttl_clock(clock, monkeypatch):
    # mongomock expires rows on reads using its own wall clock. Keep it in sync
    # with the API's frozen clock so historical fixtures are not deleted early.
    monkeypatch.setattr("mongomock.utcnow", lambda: clock[0].replace(tzinfo=None))


@pytest.fixture
def planner(api, db):
    client, _ = api
    login(client)
    seed_history(db)
    seed_profile(db)
    return (client, *target(client))


@pytest.mark.parametrize("invalidate", ["superseded", "lease_lost"])
def test_customer_preview_rejects_history_change_without_saving_preview(planner, db, monkeypatch, invalidate):
    client, _, _, url = planner
    strategy = gs.STRATEGIES[STRATEGY]
    original = strategy.simulate

    def changed_history(*args, **kwargs):
        assert db.acquisition_jobs.find_one({"_id": "seed-history"})["reader_leases"]
        result = original(*args, **kwargs)
        if invalidate == "superseded":
            previous = db.acquisition_jobs.find_one({"_id": "seed-history"})
            db.acquisition_jobs.insert_one({**previous, "_id": "successor",
                                            "requested_at": NOW + timedelta(seconds=1), "reader_leases": {}})
        else:
            db.acquisition_jobs.update_one({"_id": "seed-history"}, {"$set": {"reader_leases": {}}})
        return result

    monkeypatch.setitem(gs.STRATEGIES, STRATEGY, replace(strategy, simulate=changed_history))
    response = client.post(url + "/simulate", json=selection())
    assert response.status_code == 409, response.text
    assert "Refresh" in response.json()["detail"]
    assert db.planner_simulations.count_documents({}) == 0
    assert not db.acquisition_jobs.find_one({"_id": "seed-history"}).get("reader_leases")


def test_creation_normalizes_and_reads_only_public_fields_with_startup_indexes(api, db):
    client, publish = api
    login(client)
    assert client.get("/customers").json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
    customer, goal, url = target(client, "  Zoë Example \n", name="  First home  ", goal_eur="123.4")
    assert customer == {"id": customer["id"], "name": "Zoë Example", "created_at": planner_api._timestamp(NOW)}
    assert ObjectId.is_valid(customer["id"])
    assert goal == {"id": goal["id"], "customer_id": customer["id"], "name": "First home", "goal_eur": "123.40",
                    "goal_date": GOAL["goal_date"], "created_at": planner_api._timestamp(NOW)}
    assert client.get(f"/customers/{customer['id']}").json() == customer
    assert client.get(url).json() == goal
    assert client.get("/customers").json()["items"] == [empty_summary(customer, 1)]
    assert client.get(f"/customers/{customer['id']}/goals").json()["items"] == [{**goal, "linked_portfolio_count": 0, "linked_equity_eur": None}]
    assert client.get(url + "/simulations").json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
    owner = str(db.users.find_one({"login": "admin"})["_id"])
    assert db.planner_customers.find_one()["user_id"] == owner
    assert db.planner_goals.find_one()["user_id"] == owner
    assert db.planner_simulations.index_information()["planner_preview_expiry"]["expireAfterSeconds"] == 0
    assert db.planner_simulations.index_information()["planner_simulations_owner_goal_order"]["key"] == [
        ("user_id", 1), ("customer_id", 1), ("goal_id", 1), ("calculated_at", -1), ("_id", -1),
    ]
    assert "planner_customers_owner_order" in db.planner_customers.index_information()
    assert "planner_goals_owner_customer_order" in db.planner_goals.index_information()
    publish.assert_not_called()


def test_goal_update_normalizes_partial_changes_and_preserves_identity(planner, db):
    client, customer, goal, url = planner
    response = client.patch(url, json={"name": "  Our first home  "})
    assert response.status_code == 200, response.text
    renamed = {**goal, "name": "Our first home"}
    assert response.json() == renamed
    assert client.get(url).json() == renamed
    response = client.patch(url, json={"goal_eur": "234.5", "goal_date": "2031-09-17"})
    assert response.status_code == 200, response.text
    updated = {**renamed, "goal_eur": "234.50", "goal_date": "2031-09-17"}
    assert response.json() == updated
    assert client.get(f"/customers/{customer['id']}/goals").json()["items"] == [{**updated, "linked_portfolio_count": 0, "linked_equity_eur": None}]
    assert db.planner_goals.count_documents({}) == 1
    assert client.patch(url, json={"name": updated["name"]}).json() == updated


@pytest.mark.parametrize("change", [
    {}, {"name": None}, {"goal_eur": None}, {"goal_date": None},
    {"customer_id": "forged"}, {"created_at": "forged"}, {"user_id": "forged"}, {"id": "forged"},
    {"name": "Valid name", "goal_eur": "0"},
])
def test_goal_update_rejects_empty_null_or_forbidden_fields_without_partial_writes(planner, db, change):
    client, _, goal, url = planner
    before = db.planner_goals.find_one({"_id": goal["id"]})
    assert client.patch(url, json=change).status_code == 422
    assert db.planner_goals.find_one({"_id": goal["id"]}) == before


def test_older_goal_can_be_renamed_without_changing_its_date(planner, clock):
    client, _, goal, url = planner
    clock[0] += timedelta(days=5000)
    response = client.patch(url, json={"name": "Completed goal"})
    assert response.status_code == 200
    assert response.json() == {**goal, "name": "Completed goal"}
    # Resubmitting an unchanged date is also harmless; new past dates are not.
    assert client.patch(url, json={"goal_date": goal["goal_date"]}).status_code == 200
    assert client.patch(url, json={"goal_date": "2030-01-01"}).status_code == 422


def test_goal_changes_preserve_saved_and_unsaved_snapshots_and_new_calculations_use_new_settings(planner, db):
    client, _, goal, url = planner
    saved = save(client, url, calculate(client, url))
    preview = calculate(client, url)
    before = list(db.planner_simulations.find())
    changes = {"name": "New target", "goal_eur": "250000.00", "goal_date": "2031-09-17"}
    assert client.patch(url, json=changes).json() == {**goal, **changes}
    assert list(db.planner_simulations.find()) == before
    assert client.get(url + f"/simulations/{saved['id']}").json() == saved
    assert save(client, url, preview) == {**preview, "saved_at": planner_api._timestamp(NOW)}
    current = calculate(client, url)
    for field in ("goal_eur", "goal_date"):
        assert current["inputs"][field] == current["result"][field] == changes[field]
        assert all(item["inputs"][field] == goal[field] for item in client.get(url + "/simulations").json()["items"])


def test_failed_goal_update_leaves_the_original_unchanged(planner, db, monkeypatch):
    client, _, goal, url = planner
    monkeypatch.setattr(db.planner_goals, "find_one_and_update", Mock(side_effect=ConnectionFailure("offline")))
    assert client.patch(url, json={"name": "Not saved"}).status_code == 503
    assert client.get(url).json() == goal


def test_every_endpoint_requires_authentication(api):
    client, _ = api
    base = f"/customers/{ObjectId()}"
    goal = base + f"/goals/{ObjectId()}"
    history = goal + "/simulations"
    assert client.patch(goal, json={"name": "Changed"}).status_code == 401
    for url in ("/customers", base, base + "/goals", goal, history, history + f"/{ObjectId()}"):
        response = client.get(url)
        assert response.status_code == 401
        assert response.headers["Cache-Control"] == "no-store"
    for url, body in (("/customers", {"name": "Name"}), (base + "/goals", GOAL),
                      (goal + "/simulate", selection()), (history + f"/{ObjectId()}/save", {})):
        assert client.post(url, json=body).status_code == 401


@pytest.mark.parametrize("headers", [{"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}])
def test_mutations_keep_origin_protection(planner, db, headers):
    client, customer, _, url = planner
    preview = calculate(client, url)
    original = client.get(url).json()
    assert client.patch(url, json={"name": "Changed"}, headers=headers).status_code == 403
    assert client.get(url).json() == original
    for endpoint, body in (("/customers", {"name": "Name"}), (f"/customers/{customer['id']}/goals", GOAL),
                           (url + "/simulate", selection()), (url + f"/simulations/{preview['id']}/save", {})):
        assert client.post(endpoint, json=body, headers=headers).status_code == 403
    assert db.planner_customers.count_documents({}) == 1
    assert db.planner_goals.count_documents({}) == 1
    assert db.planner_simulations.count_documents({"saved_at": {"$ne": None}}) == 0


@pytest.mark.parametrize("name", [None, True, 123, [], {}, "", " \t\n", "n" * 201])
def test_names_are_strict_trimmed_nonempty_and_bounded(api, db, name):
    client, _ = api
    login(client)
    customer, goal, url = target(client)
    assert client.patch(url, json={"name": name}).status_code == 422
    assert client.get(url).json() == goal
    assert client.post("/customers", json={"name": name}).status_code == 422
    assert client.post(f"/customers/{customer['id']}/goals", json={**GOAL, "name": name}).status_code == 422
    assert db.planner_customers.count_documents({}) == db.planner_goals.count_documents({}) == 1


@pytest.mark.parametrize("extra", [{"email": "ignored@example.org"}, {"user_id": "another-owner"}, {"id": "forged"}])
def test_creation_rejects_unknown_fields(api, db, extra):
    client, _ = api
    login(client)
    customer, _, _ = target(client)
    assert client.post("/customers", json={"name": "Name", **extra}).status_code == 422
    assert client.post(f"/customers/{customer['id']}/goals", json={**GOAL, **extra}).status_code == 422
    assert db.planner_customers.count_documents({}) == db.planner_goals.count_documents({}) == 1


@pytest.mark.parametrize("change", [
    {"goal_eur": value} for value in ("0", "-1", "NaN", "Infinity", "1000000000000.01", "1.001", True)
] + [{"goal_date": value} for value in ("tomorrow", "2026-09-17", "2026-10-16", "2076-09-18")]
  + [{"starting_capital_eur": "10"}, {"reliability": 0.9}, {"strategy": selection()["strategy"]}])
def test_goal_money_horizon_and_fixed_only_fields_are_validated(api, db, change):
    client, _ = api
    login(client)
    customer, goal, url = target(client)
    assert client.patch(url, json=change).status_code == 422
    assert client.get(url).json() == goal
    response = client.post(f"/customers/{customer['id']}/goals", json={**GOAL, **change})
    assert response.status_code == 422, response.text
    assert db.planner_goals.count_documents({}) == 1


@pytest.mark.parametrize("change", [
    {"starting_capital_eur": value} for value in ("-1", "NaN", "Infinity", "1000000000000.01", "1.001", True)
] + [{"reliability": value} for value in (0.009, 1, "NaN", "Infinity")]
  + [{"goal_eur": "999"}, {"goal_date": "2040-01-01"}, {"name": "Another goal"}, {"result": {}}, {"paths": 1},
     {"strategy": {"id": "unknown", "parameters": {}}},
     {"strategy": {"id": STRATEGY, "parameters": {}}},
     {"strategy": {"id": STRATEGY, "parameters": {"instrument": ISIN, "extra": 1}}},
     {"strategy": {"id": STRATEGY, "parameters": {"instrument": ISIN}, "extra": 1}}])
def test_simulation_accepts_only_varying_inputs_and_validates_before_solver(planner, db, monkeypatch, change):
    client, _, _, url = planner
    forbidden = Mock(side_effect=AssertionError("Invalid request must not resolve data or simulate"))
    monkeypatch.setattr(db.instruments, "find_one", forbidden)
    monkeypatch.setattr(gs, "simulate_buy_and_hold", forbidden)
    assert client.post(url + "/simulate", json=selection(**change)).status_code == 422
    assert db.planner_simulations.count_documents({}) == 0
    forbidden.assert_not_called()


@pytest.mark.parametrize("field", ["strategy", "starting_capital_eur", "reliability"])
def test_simulation_requires_all_varying_inputs(planner, db, field):
    client, _, _, url = planner
    body = selection()
    del body[field]
    assert client.post(url + "/simulate", json=body).status_code == 422
    assert db.planner_simulations.count_documents({}) == 0


@pytest.mark.parametrize("reliability", [0.01, 0.25, 0.49, 0.99])
def test_low_success_rates_match_standalone_and_survive_saved_plan(planner, reliability):
    client, _, _, url = planner
    standalone = client.post("/goals/simulate", json=request_body(reliability=reliability))
    assert standalone.status_code == 200, standalone.text
    preview = calculate(client, url, reliability=reliability)
    assert preview["result"] == standalone.json()
    saved = save(client, url, preview)
    planned = client.post(f"{url}/simulations/{saved['id']}/plan")
    assert planned.status_code == 200, planned.text
    assert planned.json()["inputs"]["reliability"] == reliability
    assert planned.json()["result"]["target_probability"] == reliability
    assert client.get(f"{url}/simulations/{saved['id']}").json()["inputs"]["reliability"] == reliability


def test_preview_exactly_matches_existing_simulation_and_save_is_not_recalculation(planner, db, clock, monkeypatch):
    client, customer, goal, url = planner
    expected = client.post("/goals/simulate", json=request_body()).json()
    preview = calculate(client, url)
    assert set(preview) == {"id", "customer_id", "goal_id", "calculated_at", "saved_at", "inputs", "strategy_definition", "result", "is_plan"}
    assert preview["result"] == expected
    assert preview["inputs"] == {**request_body(), "simulation_method": "standard"}
    assert preview["strategy_definition"] == gs.STRATEGIES[STRATEGY].public_definition()
    assert preview["customer_id"] == customer["id"] and preview["goal_id"] == goal["id"]
    assert preview["saved_at"] is None
    document = db.planner_simulations.find_one({"_id": preview["id"]})
    assert md.aware(document["expires_at"]) == NOW + timedelta(hours=24)
    assert document["user_id"] == str(db.users.find_one({"login": "admin"})["_id"])
    assert client.get(url + "/simulations").json()["total"] == 0
    assert client.get(url + f"/simulations/{preview['id']}").status_code == 404
    assert db.calibration_runs.count_documents({}) == 0
    forbidden = Mock(side_effect=AssertionError("Saving a preview must not recalculate or revalidate"))
    monkeypatch.setattr(db.instruments, "find_one", forbidden)
    monkeypatch.setattr(gs, "instrument_statistics", forbidden)
    monkeypatch.setitem(gs.STRATEGIES, STRATEGY, replace(gs.STRATEGIES[STRATEGY], simulate=forbidden))
    clock[0] += timedelta(minutes=1)
    saved = save(client, url, preview)
    assert saved == {**preview, "saved_at": planner_api._timestamp(clock[0])}
    stored = db.planner_simulations.find_one({"_id": preview["id"]})
    assert "expires_at" not in stored
    assert stored == {key: (saved["saved_at"] if key == "saved_at" else value)
                      for key, value in document.items() if key != "expires_at"}
    # Repeated requests, including after the original TTL and goal horizon, are
    # stable and never update saved_at or validate the old request against today.
    clock[0] += timedelta(days=5000)
    assert save(client, url, preview) == saved
    assert client.get(url + f"/simulations/{preview['id']}").json() == saved
    assert db.planner_simulations.find_one({"_id": preview["id"]}) == stored
    assert db.planner_simulations.count_documents({}) == 1
    forbidden.assert_not_called()


def test_save_and_report_survive_registry_retirement_source_removal_and_reopen(planner, db, monkeypatch):
    client, _, _, url = planner
    preview = calculate(client, url)
    for name in ("instruments", "quotes", "instrument_state", "instrument_statistics", "etfs"):
        db[name].delete_many({})
    monkeypatch.delitem(gs.STRATEGIES, STRATEGY)
    forbidden = Mock(side_effect=AssertionError("Read only the stored snapshot"))
    monkeypatch.setattr(planner_api, "get_strategy", forbidden)
    monkeypatch.setattr(gs, "instrument_statistics", forbidden)
    saved = save(client, url, preview)
    assert saved["strategy_definition"] == preview["strategy_definition"]
    with TestClient(client.app) as reopened:
        login(reopened)
        assert reopened.get(url + f"/simulations/{saved['id']}").json() == saved
        assert save(reopened, url, preview) == saved
        listing = reopened.get(url + "/simulations").json()
        assert listing["total"] == 1
        assert listing["items"][0]["strategy_name"] == preview["strategy_definition"]["name"]
    forbidden.assert_not_called()


def test_multiple_previews_and_saved_reports_are_independent(planner, db, clock):
    client, _, goal, url = planner
    first = calculate(client, url)
    clock[0] += timedelta(seconds=1)
    second = calculate(client, url, starting_capital_eur="20000", reliability=0.99)
    third = calculate(client, url)
    assert len({item["id"] for item in (first, second, third)}) == 3
    def calculation_payload(preview):
        result = deepcopy(preview["result"])
        result.pop("data_as_of")
        result["parameter_statistics"]["instrument"].pop("as_of")
        return result

    assert calculation_payload(first) == calculation_payload(third)
    assert first["result"] != second["result"]
    assert client.get(url + "/simulations").json()["total"] == 0
    saved_second = save(client, url, second)
    saved_first = save(client, url, first)
    assert client.get(url + f"/simulations/{first['id']}").json() == saved_first
    assert client.get(url + f"/simulations/{second['id']}").json() == saved_second
    assert client.get(url).json() == goal
    # An unsaved replay remains invisible, even when newer than saved reports.
    assert client.get(url + f"/simulations/{third['id']}").status_code == 404
    assert [row["id"] for row in client.get(url + "/simulations").json()["items"]] == [second["id"], first["id"]]
    assert db.planner_simulations.count_documents({}) == 3


def test_generic_strategy_dispatch_snapshots_normalized_typed_parameters(planner, db, monkeypatch):
    client, _, goal, url = planner

    class Parameters(BaseModel):
        model_config = ConfigDict(extra="forbid")
        periods: int = Field(gt=0, json_schema_extra={"parameter_type": "integer"})
        settlement: date = Field(default=NOW.date(), json_schema_extra={"parameter_type": "date"})

    expected = {"monthly_contribution_eur": "1.23", "success_probability": 0.987, "custom_result": [2, 4, 9]}

    def run(parameters, inputs, database, today):
        assert isinstance(parameters, Parameters) and parameters.periods == 3
        assert isinstance(inputs, GoalInputs) and inputs.goal_eur == Decimal(goal["goal_eur"])
        assert inputs.goal_date.isoformat() == goal["goal_date"]
        assert inputs.starting_capital_eur == Decimal("0") and inputs.reliability == 0.8
        assert database is db and today == NOW.date()
        return deepcopy(expected)

    execute = Mock(side_effect=run)
    strategy = gs.StrategyDefinition("test", "Another model", "Test generic dispatch", Parameters, execute)
    monkeypatch.setitem(gs.STRATEGIES, "test", strategy)
    forbidden = Mock(side_effect=AssertionError("Generic route must not use the buy-and-hold solver"))
    monkeypatch.setattr(gs, "simulate_buy_and_hold", forbidden)
    preview = calculate(client, url, strategy={"id": "test", "parameters": {"periods": "3"}},
                        starting_capital_eur="0", reliability=0.8)
    normalized = {"id": "test", "parameters": {"periods": 3, "settlement": NOW.date().isoformat()}}
    assert preview["inputs"]["strategy"] == normalized
    assert preview["inputs"]["starting_capital_eur"] == "0.00"
    assert preview["result"] == {**expected, "data_as_of": NOW.isoformat().replace("+00:00", "Z"),
                                  "strategy": normalized, "history_contract": "latest_complete_yahoo",
                                  "warnings": ["Uses the latest complete Yahoo published history with quote dates bounded by as_of, "
                                               "not observations known at that timestamp. Later provider corrections and corporate-action "
                                               "adjustments can revise historical prices; this is not strict point-in-time data."]}
    assert preview["strategy_definition"] == strategy.public_definition()
    assert save(client, url, preview)["result"] == preview["result"]
    execute.assert_called_once()
    forbidden.assert_not_called()


def test_foreign_owner_and_mismatched_parent_ids_never_expose_or_save(planner, db, config):
    client, customer, goal, url = planner
    saved = save(client, url, calculate(client, url))
    preview = calculate(client, url)
    _, sibling, sibling_url = target(client, "Sibling")
    wrong_customer_url = sibling_url.rsplit("/", 1)[0] + "/" + goal["id"]
    # A goal from another customer cannot be addressed under the first one.
    for parent in (wrong_customer_url, url.rsplit("/", 1)[0] + "/" + sibling["id"]):
        assert client.get(parent).status_code == 404
        assert client.patch(parent, json={"name": "Wrong parent"}).status_code == 404
        assert client.get(parent + "/simulations").status_code == 404
        assert client.post(parent + "/simulate", json=selection()).status_code == 404
    same_customer_goal = client.post(f"/customers/{customer['id']}/goals", json={**GOAL, "name": "Another goal"}).json()
    same_customer_url = f"/customers/{customer['id']}/goals/{same_customer_goal['id']}"
    for parent in (sibling_url, same_customer_url):
        for item in (saved, preview):
            assert client.get(parent + f"/simulations/{item['id']}").status_code == 404
            assert client.post(parent + f"/simulations/{item['id']}/save").status_code == 404
    other_id = db.users.insert_one({"login": "other", "role": "user", "disabled": False}).inserted_id
    with TestClient(client.app) as other:
        other.cookies.set(COOKIE_NAME, create_token(str(other_id), config))
        assert other.get("/customers").json()["total"] == 0
        other_customer, _, other_url = target(other, "Non-admin user")
        assert other.get("/customers").json()["items"] == [empty_summary(other_customer, 1)]
        assert save(other, other_url, calculate(other, other_url))["saved_at"] is not None
        for endpoint in (f"/customers/{customer['id']}", f"/customers/{customer['id']}/goals", url,
                         url + "/simulations", url + f"/simulations/{saved['id']}"):
            assert other.get(endpoint, params={"user_id": str(db.users.find_one({"login": "admin"})["_id"])}).status_code == 404
        assert other.post(f"/customers/{customer['id']}/goals", json=GOAL).status_code == 404
        assert other.post(url + "/simulate", json=selection()).status_code == 404
        assert other.patch(url, json={"name": "Foreign edit"}).status_code == 404
        assert client.get(url).json() == goal
        for item in (saved, preview):
            assert other.post(url + f"/simulations/{item['id']}/save").status_code == 404
            assert other.get(other_url + f"/simulations/{item['id']}").status_code == 404
            assert other.post(other_url + f"/simulations/{item['id']}/save").status_code == 404
    assert client.get(f"/customers/{other_customer['id']}").status_code == 404
    assert client.get(url + "/simulations").json()["total"] == 1
    assert db.planner_simulations.find_one({"_id": preview["id"]})["saved_at"] is None


@pytest.mark.parametrize("unknown", ["not-an-object-id", "unknown", str(ObjectId())])
def test_unknown_ids_are_clear_404s(planner, unknown):
    client, customer, _, url = planner
    assert client.get(f"/customers/{unknown}").status_code == 404
    assert client.get(f"/customers/{customer['id']}/goals/{unknown}").status_code == 404
    assert client.patch(f"/customers/{customer['id']}/goals/{unknown}", json={"name": "Unknown"}).status_code == 404
    assert client.patch(f"/customers/{unknown}/goals/{url.rsplit('/', 1)[1]}", json={"name": "Unknown"}).status_code == 404
    assert client.post(f"/customers/{unknown}/goals", json=GOAL).status_code == 404
    assert client.get(url + f"/simulations/{unknown}").status_code == 404
    assert client.post(url + f"/simulations/{unknown}/save").status_code == 404


def test_history_projects_compact_summaries_only_and_paginates_newest_first(planner, db, monkeypatch):
    client, _, _, url = planner
    previews = [calculate(client, url) for _ in range(3)]
    # Save in a different order: history is deterministic by calculation, then ID.
    reports = {preview["id"]: save(client, url, preview) for preview in reversed(previews)}
    calculate(client, url)  # newer unsaved preview must not affect totals/pages
    find = Mock(wraps=db.planner_simulations.find)
    monkeypatch.setattr(db.planner_simulations, "find", find)
    expected_ids = sorted(reports, reverse=True)
    for offset, expected in ((0, expected_ids[:2]), (2, expected_ids[2:]), (20, [])):
        response = client.get(url + "/simulations", params={"limit": 2, "offset": offset})
        assert response.headers["Cache-Control"] == "no-store"
        page = response.json()
        assert (page["total"], page["limit"], page["offset"]) == (3, 2, offset)
        assert [item["id"] for item in page["items"]] == expected
        for item in page["items"]:
            full = reports[item["id"]]
            assert item == {"id": full["id"], "goal_id": full["goal_id"], "is_plan": False,
                            "calculated_at": full["calculated_at"], "saved_at": full["saved_at"],
                            "inputs": full["inputs"], "strategy_name": full["strategy_definition"]["name"],
                            "monthly_contribution_eur": full["result"]["monthly_contribution_eur"],
                            "success_probability": full["result"]["success_probability"]}
    for call in find.call_args_list:
        assert call.args[1] == planner_api.SUMMARY_PROJECTION
        assert set(call.args[0]) == {"user_id", "customer_id", "goal_id", "saved_at"}


def test_customers_and_goals_pagination_defaults_bounds_and_timestamp_ties(planner, clock):
    client, customer, goal, url = planner
    customers = [customer]
    for name in ("Second", "Third"):
        customers.append(target(client, name)[0])
    goals = [goal]
    endpoint = f"/customers/{customer['id']}/goals"
    for name in ("Second", "Third"):
        goals.append(client.post(endpoint, json={**GOAL, "name": name}).json())
    customer_summaries = [empty_summary(item, 3 if item["id"] == customer["id"] else 1) for item in customers]
    for endpoint, items in (("/customers", customer_summaries), (endpoint,
                            [{**item, "linked_portfolio_count": 0, "linked_equity_eur": None} for item in goals])):
        assert client.get(endpoint).json() == {"items": list(reversed(items)), "total": 3, "limit": 20, "offset": 0}
        assert client.get(endpoint, params={"limit": 1, "offset": 1}).json() == {
            "items": [items[1]], "total": 3, "limit": 1, "offset": 1,
        }
        assert client.get(endpoint, params={"offset": 100}).json()["items"] == []
        assert client.get(endpoint, params={"limit": 100}).status_code == 200
    for endpoint in ("/customers", f"/customers/{customer['id']}/goals", url + "/simulations"):
        for params in ({"limit": 0}, {"limit": 101}, {"limit": "x"}, {"offset": -1}, {"offset": "x"}):
            assert client.get(endpoint, params=params).status_code == 422
    clock[0] += timedelta(microseconds=1)
    newer = client.post("/customers", json={"name": "Sub-millisecond newer"}).json()
    assert client.get("/customers").json()["items"][0] == empty_summary(newer)


@pytest.mark.parametrize("delta", [timedelta(hours=24), timedelta(hours=24, seconds=1)])
def test_expired_preview_cannot_be_saved_even_before_ttl_monitor_runs(planner, db, clock, delta):
    client, _, _, url = planner
    preview = calculate(client, url)
    # Keep the expired row to exercise the API predicate, not mongomock's eager TTL.
    db.planner_simulations.drop_index("planner_preview_expiry")
    clock[0] += delta
    assert db.planner_simulations.find_one({"_id": preview["id"]}) is not None
    response = client.post(url + f"/simulations/{preview['id']}/save")
    assert response.status_code == 410 and "expired" in response.json()["detail"]
    assert db.planner_simulations.find_one()["saved_at"] is None
    assert client.get(url + "/simulations").json()["total"] == 0
    assert client.get(url + f"/simulations/{preview['id']}").status_code == 404
    db.planner_simulations.delete_one({"_id": preview["id"]})  # TTL monitor removed it
    assert client.post(url + f"/simulations/{preview['id']}/save").status_code == 404


def test_preview_can_be_saved_just_before_expiration(planner, clock):
    client, _, _, url = planner
    preview = calculate(client, url)
    clock[0] += timedelta(hours=24) - timedelta(milliseconds=1)
    assert save(client, url, preview)["saved_at"] == planner_api._timestamp(clock[0])


def test_save_rejects_client_supplied_report_or_inputs(planner, db):
    client, _, _, url = planner
    preview = calculate(client, url)
    endpoint = url + f"/simulations/{preview['id']}/save"
    for body in (preview, {"result": {"monthly_contribution_eur": "0.00"}}, {"inputs": selection()},
                 {"saved_at": "2000-01-01"}, {"expires_at": "2099-01-01"}, {"id": "forged"}):
        assert client.post(endpoint, json=body).status_code == 422
    assert db.planner_simulations.find_one()["saved_at"] is None
    assert client.post(endpoint, json={}).status_code == 200


@pytest.mark.parametrize("change", ["missing-profile", "missing-quotes", "active-job", "currency", "aged-goal"])
def test_recalculate_checks_fresh_eligibility_and_failures_leave_history_unchanged(planner, db, config, clock, monkeypatch, change):
    client, _, _, url = planner
    first = save(client, url, calculate(client, url))
    original = list(db.planner_simulations.find())
    if change == "missing-profile":
        db.instruments.delete_many({})
    elif change == "missing-quotes":
        db.quotes.delete_many({})
    elif change == "active-job":
        enqueue(db, config)
    elif change == "currency":
        md.set_price_currency(db, ISIN, "USD")
    else:
        clock[0] += timedelta(days=3650)
    forbidden = Mock(side_effect=AssertionError("Invalid calculation must not run solver or provider"))
    monkeypatch.setattr(gs, "simulate_buy_and_hold", forbidden)
    monkeypatch.setattr(md, "provider_client", forbidden)
    assert client.post(url + "/simulate", json=selection()).status_code == 422
    assert list(db.planner_simulations.find()) == original
    assert client.get(url + f"/simulations/{first['id']}").json() == first
    assert client.get(url + "/simulations").json()["total"] == 1
    forbidden.assert_not_called()


def test_calculation_and_storage_failures_do_not_create_saved_entries(planner, db, monkeypatch):
    client, _, _, url = planner
    with monkeypatch.context() as patch:
        patch.setattr(gs, "simulate_buy_and_hold", Mock(side_effect=md.DataError(422, "unstable")))
        assert client.post(url + "/simulate", json=selection()).status_code == 422
    assert db.planner_simulations.count_documents({}) == 0
    with monkeypatch.context() as patch:
        patch.setattr(db.planner_simulations, "insert_one", Mock(side_effect=ConnectionFailure("offline")))
        assert client.post(url + "/simulate", json=selection()).status_code == 503
    assert db.planner_simulations.count_documents({}) == 0
    preview = calculate(client, url)
    with monkeypatch.context() as patch:
        patch.setattr(db.planner_simulations, "find_one_and_update", Mock(side_effect=ConnectionFailure("offline")))
        assert client.post(url + f"/simulations/{preview['id']}/save").status_code == 503
    assert db.planner_simulations.find_one()["saved_at"] is None
    assert client.get(url + "/simulations").json()["total"] == 0
    assert save(client, url, preview)["result"] == preview["result"]


def test_concurrent_save_returns_one_identical_report(planner, db):
    client, _, _, url = planner
    preview = calculate(client, url)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: save(client, url, preview), range(12)))
    assert all(result == results[0] for result in results)
    assert results[0]["result"] == preview["result"]
    assert db.planner_simulations.count_documents({}) == 1
    assert "expires_at" not in db.planner_simulations.find_one()
    assert client.get(url + "/simulations").json()["total"] == 1
