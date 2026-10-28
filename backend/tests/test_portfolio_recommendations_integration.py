"""Opt-in recommendation atomicity contracts against disposable real MongoDB.

Run with TEST_MONGODB_URI pointing at a MongoDB instance on which this test may
create and drop databases. Only the write scheduling is intercepted in races;
all reads, compare-and-swap updates, ledger validation and receipts are real.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, timedelta
from threading import Event, Lock
from uuid import uuid4

from pymongo.collection import Collection
import pytest

from goal_simulation import contribution_dates

from test_portfolio_removal_integration import db, synchronize_mongo_writes
from test_portfolio_tracker import (
    ISIN, SECOND_ISIN, api, cash, clock, config, detail, plan, record,
    second_instrument, tracker, trade,
)


@pytest.fixture
def recommended(tracker, db):
    client, customer_id, metadata, url = tracker
    plan_id = plan(client, db, customer_id)
    payments = [day.isoformat() for day in contribution_dates(date(2026, 9, 17), date(2036, 9, 17))]
    db.planner_simulations.update_one({"_id": plan_id}, {"$set": {
        "inputs": {
            "strategy": {"id": "buy_and_hold_single_instrument", "parameters": {"instrument": ISIN}},
            "starting_capital_eur": "1000.00", "goal_eur": "100000.00",
            "goal_date": "2036-09-17", "reliability": 0.9,
        },
        "result": {
            "valuation_date": "2026-09-17", "monthly_contribution_eur": "100.00",
            "payment_dates": payments, "contribution_count": len(payments),
            "first_contribution_date": payments[0], "last_contribution_date": payments[-1],
        },
    }})
    response = client.patch(url, json={"name": metadata["name"], "plan_id": plan_id})
    assert response.status_code == 200, response.text
    return client, customer_id, metadata, url


def stored(db, metadata):
    return db.portfolios.find_one({"_id": metadata["id"]})


def recommendations(client, url):
    response = client.get(url + "/recommendations")
    assert response.status_code == 200, response.text
    return response.json()


def reviewed(client, url):
    snapshot = recommendations(client, url)
    selected = [action["id"] for action in snapshot["actions"] if action["eligible"]]
    assert selected, snapshot
    request = {"snapshot_token": snapshot["snapshot_token"], "action_ids": selected,
               "date": snapshot["date"], "overrides": {}}
    response = client.post(url + "/recommendations/preview", json=request)
    assert response.status_code == 200, response.text
    preview = response.json()
    return snapshot, preview, {**request, "preview_token": preview["preview_token"],
                               "operation_id": str(uuid4())}


def fulfill(client, url, request):
    return client.post(url + "/recommendations/fulfill", json=request)


def seed_rebalance(client, db, url):
    second_instrument(db)
    record(client, url, cash(amount="1000"))
    record(client, url, trade(quantity="50", price="20", instrument_id=SECOND_ISIN))


def test_real_mongo_read_preview_and_batch_are_atomic(recommended, db):
    client, _, metadata, url = recommended
    before = stored(db, metadata)
    snapshot, preview, request = reviewed(client, url)
    assert [item["type"] for item in preview["entries"]] == ["deposit", "buy"]
    assert stored(db, metadata) == before
    response = fulfill(client, url, request)
    assert response.status_code == 200, response.text
    result = response.json()
    after = stored(db, metadata)
    assert after["revision"] == before["revision"] + 1
    assert len(after["transactions"]) == len(preview["entries"]) == 2
    assert [event["sequence"] for event in after["transactions"]] == [1, 2]
    assert [event["id"] for event in after["transactions"]] == result["event_ids"]
    assert detail(client, url)["cash_eur"] == preview["after"]["cash_eur"]
    assert snapshot["funding"]["shortfall_eur"] == "1000.00"
    assert fulfill(client, url, request).json() == result
    assert stored(db, metadata) == after


@pytest.mark.parametrize("race", ["identical", "competing"])
def test_real_mongo_concurrent_fulfillment_uses_one_cas(recommended, db, monkeypatch, race):
    client, _, metadata, url = recommended
    seed_rebalance(client, db, url)
    _, preview, request = reviewed(client, url)
    assert {entry["type"] for entry in preview["entries"]} == {"sell", "buy"}
    other = deepcopy(request)
    if race == "competing":
        other["operation_id"] = str(uuid4())
    before = stored(db, metadata)
    revisions = synchronize_mongo_writes(monkeypatch, db.portfolios)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(fulfill, client, url, body) for body in (request, other)]
        responses = [future.result(timeout=20) for future in futures]
    assert revisions[:2] == [before["revision"]] * 2
    assert sorted(response.status_code for response in responses) == ([200, 200] if race == "identical" else [200, 409])
    if race == "identical":
        assert responses[0].json() == responses[1].json()
    after = stored(db, metadata)
    assert after["revision"] == before["revision"] + 1
    assert len(after["transactions"]) == len(before["transactions"]) + len(preview["entries"])
    assert recommendations(client, url)["policy"]["last_rebalance_date"] == request["date"]


def test_real_mongo_manual_write_wins_and_invalidates_review(recommended, db, monkeypatch):
    client, _, metadata, url = recommended
    _, _, request = reviewed(client, url)
    before = stored(db, metadata)
    original = Collection.update_one
    reached, release, lock = Event(), Event(), Lock()
    intercepted = False

    def update(collection, query, changes, *args, **kwargs):
        nonlocal intercepted
        wait = False
        if collection.full_name == db.portfolios.full_name and "revision" in query:
            with lock:
                if not intercepted:
                    intercepted = wait = True
        if wait:
            reached.set()
            assert release.wait(timeout=10), "Manual writer did not release fulfillment"
        return original(collection, query, changes, *args, **kwargs)

    monkeypatch.setattr(Collection, "update_one", update)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(fulfill, client, url, request)
        try:
            assert reached.wait(timeout=10), "Fulfillment never attempted its Mongo CAS"
            record(client, url, cash(amount="12.34", day=request["date"]))
            manual = stored(db, metadata)
        finally:
            release.set()
        response = pending.result(timeout=20)
    assert response.status_code == 409, response.text
    assert manual["revision"] == before["revision"] + 1
    assert stored(db, metadata) == manual
    assert detail(client, url)["cash_eur"] == "12.34"


@pytest.mark.parametrize("later_change", ["metadata", "removal", "day-rollover"])
def test_real_mongo_lost_response_retry_returns_original_without_resurrection(recommended, db, clock, later_change):
    client, _, metadata, url = recommended
    _, _, request = reviewed(client, url)
    response = fulfill(client, url, request)
    assert response.status_code == 200, response.text
    original = response.json()  # Treat this response as lost by the caller.
    if later_change == "metadata":
        changed = client.patch(url, json={"name": "After uncertain fulfillment", "plan_id": None})
        assert changed.status_code == 200, changed.text
        record(client, url, cash(amount="5", day=request["date"]))
    elif later_change == "removal":
        for event_id in reversed(original["event_ids"]):
            removed = client.delete(url + "/transactions/" + event_id)
            assert removed.status_code == 200, removed.text
        assert client.get(url + "/transactions").json()["total"] == 0
    else:
        clock[0] += timedelta(days=1)
    before_retry = stored(db, metadata)
    retry = fulfill(client, url, request)
    assert retry.status_code == 200, retry.text
    assert retry.json() == original
    assert stored(db, metadata) == before_retry
    if later_change == "removal":
        assert all(event.get("_removed_at") for event in before_retry["transactions"])
        assert detail(client, url)["cash_eur"] == "0.00"


@pytest.mark.parametrize("change", ["selection", "date", "override", "preview"])
def test_real_mongo_operation_uuid_cannot_be_reused_with_changed_payload(recommended, db, change):
    client, _, metadata, url = recommended
    _, _, request = reviewed(client, url)
    response = fulfill(client, url, request)
    assert response.status_code == 200, response.text
    before = stored(db, metadata)
    altered = deepcopy(request)
    if change == "selection":
        altered["action_ids"] = altered["action_ids"][:1]
    elif change == "date":
        altered["date"] = "2026-09-18"
    elif change == "override":
        altered["overrides"] = {altered["action_ids"][-1]: {"commission_eur": "1.00"}}
    else:
        altered["preview_token"] = ("0" if altered["preview_token"][0] != "0" else "1") + altered["preview_token"][1:]
    conflict = fulfill(client, url, altered)
    assert conflict.status_code == 409, conflict.text
    assert stored(db, metadata) == before
    assert fulfill(client, url, request).json() == response.json()


def test_real_mongo_failed_dependent_trade_preserves_history_receipts_and_cooldown(recommended, db, clock):
    client, _, metadata, url = recommended
    seed_rebalance(client, db, url)
    snapshot, _, request = reviewed(client, url)
    assert {action["type"] for action in snapshot["actions"] if action["eligible"]} == {"sell", "buy"}
    # A future sale is valid now, but selling these same shares today would make
    # the later historical replay short. The entire recommendation must fail.
    # Persist through the real endpoint while its date is legal, then evaluate
    # the earlier UTC snapshot (also representative of legacy future entries).
    clock[0] += timedelta(days=1)
    record(client, url, trade("sell", quantity="50", price="30", day="2026-09-18", instrument_id=SECOND_ISIN))
    clock[0] -= timedelta(days=1)
    snapshot = recommendations(client, url)
    selected = [action["id"] for action in snapshot["actions"] if action["eligible"]]
    assert len(selected) == 2, snapshot
    before = stored(db, metadata)
    response = client.post(url + "/recommendations/preview", json={
        "snapshot_token": snapshot["snapshot_token"], "action_ids": selected,
        "date": snapshot["date"], "overrides": {},
    })
    assert response.status_code == 422, response.text
    assert stored(db, metadata) == before
    stale_commit = fulfill(client, url, request)
    assert stale_commit.status_code == 409, stale_commit.text
    assert stored(db, metadata) == before
    assert recommendations(client, url)["policy"]["last_rebalance_date"] is None


def test_real_mongo_removal_does_not_reset_rebalance_cooldown(recommended, db):
    client, _, metadata, url = recommended
    seed_rebalance(client, db, url)
    _, _, request = reviewed(client, url)
    response = fulfill(client, url, request)
    assert response.status_code == 200, response.text
    committed = response.json()
    policy = recommendations(client, url)["policy"]
    assert policy["last_rebalance_date"] == request["date"]
    assert policy["next_eligible_date"] == "2026-10-17"
    for event_id in reversed(committed["event_ids"]):
        removed = client.delete(url + "/transactions/" + event_id)
        assert removed.status_code == 200, removed.text
    snapshot = recommendations(client, url)
    assert snapshot["policy"]["last_rebalance_date"] == policy["last_rebalance_date"]
    assert snapshot["policy"]["next_eligible_date"] == policy["next_eligible_date"]
    rebalances = [action for action in snapshot["actions"] if action["rebalance"]]
    assert rebalances and all(not action["eligible"] and action["blockers"] for action in rebalances)
    before_retry = stored(db, metadata)
    assert fulfill(client, url, request).json() == committed
    assert stored(db, metadata) == before_retry
