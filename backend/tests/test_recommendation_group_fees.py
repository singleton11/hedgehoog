"""Commissions on a mandatory cash buy belong to the whole rebalance group."""
from dataclasses import replace
from copy import deepcopy

from goal_strategies import STRATEGIES
from test_portfolio_recommendations import saved_plan, get, multi_adapter
from test_portfolio_tracker import api, clock, config, db, tracker, cash, trade, record, second_instrument


def test_group_fee_cap_includes_mandatory_cash_buy_commission(tracker, db, monkeypatch):
    client, cid, metadata, url = tracker
    second_instrument(db)
    saved_plan(client, db, cid, metadata, initial="1000", monthly="0")
    monkeypatch.setitem(STRATEGIES, "buy_and_hold_single_instrument", replace(
        STRATEGIES["buy_and_hold_single_instrument"], recommendation_targets=multi_adapter))
    record(client, url, cash(amount="1000"))
    record(client, url, trade(quantity="8.18181818", price="110"))
    snapshot = get(client, url)
    cash_buy = next(action for action in snapshot["actions"] if action["type"] == "buy" and not action["rebalance"])
    assert any(action["rebalance"] and cash_buy["id"] in action["group"] for action in snapshot["actions"])
    before = deepcopy(db.portfolios.find_one({"_id": metadata["id"]}))
    request = {"snapshot_token": snapshot["snapshot_token"], "date": snapshot["date"],
               "action_ids": [action["id"] for action in snapshot["actions"]],
               "overrides": {cash_buy["id"]: {"commission_eur": "20"}}}
    rejected = client.post(url + "/recommendations/preview", json=request)
    assert rejected.status_code == 422, rejected.text
    assert "turnover cost limit" in rejected.json()["detail"]
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before
    request["overrides"][cash_buy["id"]]["commission_eur"] = "1"
    accepted = client.post(url + "/recommendations/preview", json=request)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["total_commission_eur"] == "1.00"
