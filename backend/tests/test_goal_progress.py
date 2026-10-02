"""Goal list progress includes only complete, owner-scoped linked portfolio equity."""
from bson import ObjectId

from test_market_data import api, clock, config, db
from test_portfolio_tracker import cash, goal, plan, portfolio, record, trade, tracker


def test_goal_list_combines_linked_plans_without_unrelated_equity(tracker, db):
    client, cid, _, unrelated = tracker
    plan_a = plan(client, db, cid)
    target = db.planner_simulations.find_one({"_id": plan_a})["goal_id"]
    owner = db.planner_goals.find_one({"_id": target})["user_id"]
    plan_b = str(ObjectId())
    db.planner_simulations.insert_one({"_id": plan_b, "user_id": owner, "customer_id": cid,
        "goal_id": target, "saved_at": "2026-09-17T12:00:00Z", "is_plan": True})
    _, first = portfolio(client, cid, plan_id=plan_a)
    _, second = portfolio(client, cid, plan_id=plan_b)
    record(client, unrelated, cash(amount="999"))
    record(client, first, cash(amount="125.25"))
    record(client, second, cash(amount="74.75"))
    response = client.get(f"/customers/{cid}/goals")
    assert response.status_code == 200, response.text
    rows = {item["id"]: item for item in response.json()["items"]}
    assert rows[target]["linked_portfolio_count"] == 2
    assert rows[target]["linked_equity_eur"] == "200.00"
    assert len(rows) == 1


def test_goal_list_does_not_report_partial_or_unlinked_equity(tracker, db):
    client, cid, _, _ = tracker
    plan_id = plan(client, db, cid)
    target = db.planner_simulations.find_one({"_id": plan_id})["goal_id"]
    _, linked = portfolio(client, cid, plan_id=plan_id)
    record(client, linked, cash(amount="250"))
    rows = {item["id"]: item for item in client.get(f"/customers/{cid}/goals").json()["items"]}
    assert rows[target]["linked_equity_eur"] == "250.00"
    other = goal(client, cid)
    assert client.get(f"/customers/{cid}/goals").json()["items"][0]["linked_equity_eur"] is None
    assert rows.get(other) is None
    # A linked holding with no usable EUR quote invalidates the combined sum.
    record(client, linked, trade(price="100"))
    db.quotes.delete_many({})
    rows = {item["id"]: item for item in client.get(f"/customers/{cid}/goals").json()["items"]}
    assert rows[target]["linked_portfolio_count"] == 1
    assert rows[target]["linked_equity_eur"] is None
    assert rows[other]["linked_portfolio_count"] == 0
