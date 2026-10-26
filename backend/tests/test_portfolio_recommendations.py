"""Funding, generic allocation, review fencing and durable recommendation receipts."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

import market_data as md
import portfolio_recommendations as rec
from goal_simulation import contribution_dates
from goal_strategies import STRATEGIES
from test_market_data import ISIN, LISTING, NOW, api, clock, config, db
from test_portfolio_tracker import (tracker, plan, record, cash, trade, second_instrument,
                                    SECOND_ISIN, quote)


def saved_plan(client, db, customer_id, metadata, *, initial="1000", monthly="100", origin="2026-07-17"):
    plan_id = plan(client, db, customer_id)
    end = date(2036, 9, 17)
    dates = contribution_dates(date.fromisoformat(origin), end)
    db.planner_simulations.update_one({"_id": plan_id}, {"$set": {
        "inputs": {"starting_capital_eur": initial, "goal_date": end.isoformat(),
                   "strategy": {"id": "buy_and_hold_single_instrument", "parameters": {"instrument": ISIN}}},
        "result": {"valuation_date": origin, "goal_date": end.isoformat(), "monthly_contribution_eur": monthly,
                   "contribution_count": len(dates), "first_contribution_date": dates[0].isoformat(),
                   "last_contribution_date": dates[-1].isoformat()}}})
    db.portfolios.update_one({"_id": metadata["id"]}, {"$set": {"plan_id": plan_id}})
    return plan_id


def get(client, url):
    response = client.get(url + "/recommendations")
    assert response.status_code == 200, response.text
    return response.json()


def review(client, url, snapshot, ids=None, overrides=None):
    payload = {"snapshot_token": snapshot["snapshot_token"], "date": snapshot["date"],
               "action_ids": ids if ids is not None else [a["id"] for a in snapshot["actions"] if a["eligible"]],
               "overrides": overrides or {}}
    response = client.post(url + "/recommendations/preview", json=payload)
    assert response.status_code == 200, response.text
    return payload, response.json()


def commit(client, url, payload, preview):
    body = {**payload, "operation_id": str(uuid4()), "preview_token": preview["preview_token"]}
    response = client.post(url + "/recommendations/fulfill", json=body)
    assert response.status_code == 200, response.text
    return body, response.json()


def test_funding_immutable_schedule_withdrawals_and_no_reads_write(tracker, db):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    record(client, url, cash(amount="700", day="2026-07-01"))
    record(client, url, cash("withdrawal", "100", day="2026-08-01"))
    before = db.portfolios.find_one({"_id": metadata["id"]})
    result = get(client, url)
    assert result["funding"] == {"scheduled_due_eur": "1200.00", "net_funded_eur": "600.00",
        "shortfall_eur": "600.00", "excess_eur": "0.00", "due_count": 2,
        "next_payment": {"date": "2026-10-17", "amount_eur": "100.00"}}
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before
    assert result["actions"][0]["type"] == "deposit"
    assert result["targets"][0]["weight"] == "1"


def test_default_price_preview_atomic_fulfill_retry_after_removal_and_edit(tracker, db, monkeypatch):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    snapshot = get(client, url)
    assert [a["type"] for a in snapshot["actions"]] == ["deposit", "buy"]
    payload, preview = review(client, url, snapshot)
    assert preview["selection"][1]["price_eur"] == "110"
    assert preview["selection"][1]["price_source"] == "saved_raw_close"
    assert preview["after"]["cash_eur"] == "0.00"
    body, result = commit(client, url, payload, preview)
    document = db.portfolios.find_one({"_id": metadata["id"]})
    assert document["revision"] == 1
    assert len(document["transactions"]) == 2
    assert len(document["recommendation_receipts"]) == 1
    for event_id in reversed(result["event_ids"]):
        assert client.delete(url + "/transactions/" + event_id).status_code == 200
    assert client.patch(url, json={"name": "Edited", "plan_id": None}).status_code == 200
    monkeypatch.setattr(md, "utcnow", lambda: NOW + timedelta(days=31))
    replay = client.post(url + "/recommendations/fulfill", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == result
    assert len(db.portfolios.find_one({"_id": metadata["id"]})["transactions"]) == 2
    altered = client.post(url + "/recommendations/fulfill", json={**body, "action_ids": ["deposit"]})
    assert altered.status_code == 409


def test_explicit_dependencies_deposit_only_then_cash_buy(tracker, db):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    snapshot = get(client, url)
    buy = next(a for a in snapshot["actions"] if a["type"] == "buy")
    response = client.post(url + "/recommendations/preview", json={"snapshot_token": snapshot["snapshot_token"],
        "date": snapshot["date"], "action_ids": [buy["id"]]})
    assert response.status_code == 422
    payload, preview = review(client, url, snapshot, ["deposit"])
    commit(client, url, payload, preview)
    updated = get(client, url)
    assert updated["funding"]["shortfall_eur"] == "0.00"
    assert len(updated["actions"]) == 1
    assert updated["actions"][0]["dependencies"] == []
    payload, preview = review(client, url, updated)
    commit(client, url, payload, preview)


@pytest.mark.parametrize("change", ["quote", "plan", "revision", "policy", "day"])
def test_tokens_bind_state_without_recalculating_review(tracker, db, monkeypatch, change):
    client, cid, metadata, url = tracker
    pid = saved_plan(client, db, cid, metadata)
    snapshot = get(client, url)
    payload, preview = review(client, url, snapshot)
    if change == "quote":
        quote(db, "2026-09-17", "120")
    elif change == "plan":
        db.planner_simulations.update_one({"_id": pid}, {"$set": {"result.monthly_contribution_eur": "101"}})
    elif change == "revision":
        db.portfolios.update_one({"_id": metadata["id"]}, {"$inc": {"revision": 1}})
    elif change == "policy":
        monkeypatch.setitem(rec.DEFAULT_POLICY, "min_trade_eur", "26")
    else:
        monkeypatch.setattr(md, "utcnow", lambda: NOW + timedelta(days=1))
    response = client.post(url + "/recommendations/fulfill", json={**payload,
        "operation_id": str(uuid4()), "preview_token": preview["preview_token"]})
    assert response.status_code == 409, response.text
    assert not db.portfolios.find_one({"_id": metadata["id"]})["transactions"]


def multi_adapter(parameters, today, snapshot):
    return {"version": "test-multi-v1", "targets": [{"instrument_id": ISIN, "weight": "0.5"},
        {"instrument_id": SECOND_ISIN, "weight": "0.5"}], "supported_actions": ["deposit", "sell", "buy"], "constraints": {}}


def test_generic_multitarget_rebalance_costs_and_cooldown(tracker, db, monkeypatch):
    client, cid, metadata, url = tracker
    second_instrument(db)
    saved_plan(client, db, cid, metadata, initial="1100", monthly="0")
    monkeypatch.setitem(STRATEGIES, "buy_and_hold_single_instrument", replace(
        STRATEGIES["buy_and_hold_single_instrument"], recommendation_targets=multi_adapter))
    record(client, url, cash(amount="1100"))
    record(client, url, trade(quantity="10", price="110"))
    snapshot = get(client, url)
    assert len([t for t in snapshot["targets"] if t["instrument_id"]]) == 2
    assert [a["type"] for a in snapshot["actions"]] == ["sell", "buy"]
    sell, buy = snapshot["actions"]
    payload, preview = review(client, url, snapshot, overrides={sell["id"]: {"commission_eur": "1"},
                                                              buy["id"]: {"commission_eur": "1"}})
    assert preview["after"]["cash_eur"] == "0.00"
    assert preview["selection"][1]["quantity"] == "18.30000000"
    operation, outcome = commit(client, url, payload, preview)
    doc = db.portfolios.find_one({"_id": metadata["id"]})
    assert doc["recommendation_last_rebalance"]["date"] == NOW.date().isoformat()
    # Retained metadata is independent of the removable ledger.
    for event_id in reversed(outcome["event_ids"]):
        assert client.delete(url + "/transactions/" + event_id).status_code == 200
    blocked = get(client, url)
    assert blocked["policy"]["next_eligible_date"] == "2026-10-17"
    assert not any(a["eligible"] for a in blocked["actions"] if a["rebalance"])
    assert client.post(url + "/recommendations/fulfill", json=operation).json() == outcome


def test_fee_limit_and_partial_rebalance_rejected(tracker, db, monkeypatch):
    client, cid, metadata, url = tracker
    second_instrument(db)
    saved_plan(client, db, cid, metadata, initial="1100", monthly="0")
    monkeypatch.setitem(STRATEGIES, "buy_and_hold_single_instrument", replace(
        STRATEGIES["buy_and_hold_single_instrument"], recommendation_targets=multi_adapter))
    record(client, url, cash(amount="1100"))
    record(client, url, trade(quantity="10", price="110"))
    snapshot = get(client, url)
    base = {"snapshot_token": snapshot["snapshot_token"], "date": snapshot["date"],
            "action_ids": [a["id"] for a in snapshot["actions"]]}
    assert client.post(url + "/recommendations/preview", json={**base, "action_ids": base["action_ids"][:1]}).status_code == 422
    assert client.post(url + "/recommendations/preview", json={**base,
        "overrides": {base["action_ids"][0]: {"commission_eur": "12"}}}).status_code == 422
    assert not db.portfolios.find_one({"_id": metadata["id"]}).get("recommendation_receipts")


def test_missing_price_override_and_fee_sizing(tracker, db):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    quote(db, "2026-09-17", None)
    snapshot = get(client, url)
    buy = next(a for a in snapshot["actions"] if a["type"] == "buy")
    assert buy["price_eur"] is None
    assert buy["eligible"]  # Listing is eligible; review requires a valid override.
    payload, preview = review(client, url, snapshot, overrides={buy["id"]: {"price_eur": "100", "commission_eur": "2"}})
    assert preview["selection"][1]["quantity"] == "11.98000000"
    assert preview["after"]["cash_eur"] == "0.00"
    commit(client, url, payload, preview)


def test_bad_schedule_unsupported_adapter_and_deadline(tracker, db, monkeypatch):
    client, cid, metadata, url = tracker
    pid = saved_plan(client, db, cid, metadata)
    db.planner_simulations.update_one({"_id": pid}, {"$set": {"result.first_contribution_date": "2026-08-18"}})
    assert get(client, url)["status"] == "unsupported"
    assert get(client, url)["actions"] == []
    saved_plan(client, db, cid, metadata)
    monkeypatch.setitem(STRATEGIES, "buy_and_hold_single_instrument", replace(
        STRATEGIES["buy_and_hold_single_instrument"], recommendation_targets=None))
    assert get(client, url)["status"] == "unsupported"


@pytest.mark.parametrize("weights", [["0.4", "0.4"], ["NaN", "1"], ["-1", "2"]])
def test_malformed_target_contract(tracker, db, monkeypatch, weights):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    def invalid(parameters, today, snapshot):
        value = multi_adapter(parameters, today, snapshot)
        for target, weight in zip(value["targets"], weights):
            target["weight"] = weight
        return value
    monkeypatch.setitem(STRATEGIES, "buy_and_hold_single_instrument", replace(
        STRATEGIES["buy_and_hold_single_instrument"], recommendation_targets=invalid))
    result = get(client, url)
    assert result["status"] == "unsupported"
    assert not result["actions"]


def test_auth_origin_and_owner_scope(tracker, db):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    snapshot = get(client, url)
    base = {"snapshot_token": snapshot["snapshot_token"], "date": snapshot["date"], "action_ids": ["deposit"]}
    assert client.post(url + "/recommendations/preview", json=base, headers={"Origin": "https://evil.test"}).status_code == 403
    db.portfolios.update_one({"_id": metadata["id"]}, {"$set": {"user_id": "other-owner"}})
    assert client.get(url + "/recommendations").status_code == 404
    assert client.post(url + "/recommendations/preview", json=base).status_code == 404


def test_subminimum_allocation_correction_is_blocked_not_aligned(tracker, db):
    client, cid, metadata, url = tracker
    second_instrument(db)
    saved_plan(client, db, cid, metadata, initial="400", monthly="0")
    quote(db, "2026-09-17", "1")
    quote(db, "2026-09-17", "1", isin=SECOND_ISIN,
          listing={"symbol": "EUNL.F", "exchange": "XFRA"})
    record(client, url, cash(amount="400"))
    record(client, url, trade(quantity="380", price="1"))
    record(client, url, trade(quantity="20", price="1", instrument_id=SECOND_ISIN))
    snapshot = get(client, url)
    assert snapshot["status"] == "blocked"
    assert Decimal(snapshot["allocation"]["max_deviation"]) == Decimal("0.05")
    assert len(snapshot["actions"]) == 2
    assert all(not a["eligible"] for a in snapshot["actions"])
    assert any("minimum gross" in reason
               for a in snapshot["actions"] for reason in a["blockers"])


def test_computed_quantity_above_ledger_limit_is_422(tracker, db):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata, initial="20000", monthly="0")
    record(client, url, cash(amount="20000"))
    snapshot = get(client, url)
    buy = next(a for a in snapshot["actions"] if a["type"] == "buy")
    before = db.portfolios.find_one({"_id": metadata["id"]})
    response = client.post(url + "/recommendations/preview", json={
        "snapshot_token": snapshot["snapshot_token"], "date": snapshot["date"],
        "action_ids": [buy["id"]], "overrides": {buy["id"]: {"price_eur": "0.00000001"}}})
    assert response.status_code == 422, response.text
    assert "unit limit" in response.json()["detail"]
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before


@pytest.mark.parametrize("price", ["100", "120"])
@pytest.mark.parametrize("quantity", [None, "8"])
@pytest.mark.parametrize("fee", ["0", "1"])
def test_sale_price_edits_resize_holdings_and_dependent_buys(tracker, db, price, quantity, fee):
    client, cid, metadata, url = tracker
    second_instrument(db)
    quote(db, "2026-09-17", "110", isin=SECOND_ISIN,
          listing={"symbol": "EUNL.F", "exchange": "XFRA"})
    saved_plan(client, db, cid, metadata, initial="1100", monthly="0")
    record(client, url, cash(amount="1100"))
    record(client, url, trade(quantity="10", price="110", instrument_id=SECOND_ISIN))
    snapshot = get(client, url)
    sell = next(a for a in snapshot["actions"] if a["type"] == "sell")
    buy = next(a for a in snapshot["actions"] if a["type"] == "buy")
    override = {"price_eur": price, "commission_eur": fee}
    if quantity is not None:
        override["quantity"] = quantity
    payload, preview = review(client, url, snapshot, overrides={
        sell["id"]: override, buy["id"]: {"commission_eur": fee}})
    sale, purchase = preview["selection"]
    assert Decimal(sale["quantity"]) == Decimal(quantity or "10")
    proceeds = Decimal(quantity or "10") * Decimal(price)
    assert Decimal(sale["amount_eur"]) == proceeds
    assert Decimal(purchase["amount_eur"]) == proceeds - 2 * Decimal(fee)
    assert preview["total_commission_eur"] == str(2 * Decimal(fee)) + ".00"
    assert preview["after"]["cash_eur"] == "0.00"
    assert Decimal(preview["after"]["holdings"][SECOND_ISIN]) == 10 - Decimal(quantity or "10")
    assert sale["price_source"] == "override"
    commit(client, url, payload, preview)


def test_empty_duplicate_and_enlarged_quantity_rejected(tracker, db):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    snapshot = get(client, url)
    base = {"snapshot_token": snapshot["snapshot_token"], "date": snapshot["date"]}
    for ids in ([], ["deposit", "deposit"]):
        assert client.post(url + "/recommendations/preview", json={**base, "action_ids": ids}).status_code == 422
    ids = [a["id"] for a in snapshot["actions"]]
    assert client.post(url + "/recommendations/preview", json={**base, "action_ids": ids,
        "overrides": {ids[-1]: {"quantity": "100000"}}}).status_code == 422


@pytest.mark.parametrize("fee,status", [("10.99", 200), ("11.00", 200), ("11.01", 422)])
def test_one_percent_fee_boundary(tracker, db, monkeypatch, fee, status):
    client, cid, metadata, url = tracker
    second_instrument(db)
    saved_plan(client, db, cid, metadata, initial="1100", monthly="0")
    monkeypatch.setitem(STRATEGIES, "buy_and_hold_single_instrument", replace(
        STRATEGIES["buy_and_hold_single_instrument"], recommendation_targets=multi_adapter))
    record(client, url, cash(amount="1100"))
    record(client, url, trade(quantity="10", price="110"))
    snapshot = get(client, url)
    ids = [a["id"] for a in snapshot["actions"]]
    response = client.post(url + "/recommendations/preview", json={"snapshot_token": snapshot["snapshot_token"],
        "date": snapshot["date"], "action_ids": ids, "overrides": {ids[0]: {"commission_eur": fee}}})
    assert response.status_code == status, response.text


def allocation_fixture(a="275", b="225", cash_amount="0"):
    market = {isin: {"eligible": True, "price_eur": "1", "blockers": [], "listing": {},
                     "price_date": "2026-09-17", "price_source": "saved_raw_close"} for isin in (ISIN, SECOND_ISIN)}
    positions = {isin: {"quantity": Decimal(value)} for isin, value in ((ISIN, a), (SECOND_ISIN, b))}
    return ({"shortfall_eur": "0"}, {ISIN: Decimal("0.5"), SECOND_ISIN: Decimal("0.5")},
            Decimal(cash_amount), positions, market)


@pytest.mark.parametrize("a,b,eligible", [("274.99", "225.01", False), ("275", "225", True),
                                        ("275.01", "224.99", True), ("220", "180", False)])
def test_drift_and_minimum_trade_boundaries(a, b, eligible):
    *_, actions, blockers, allocation = rec.compute_actions(*allocation_fixture(a, b), dict(rec.DEFAULT_POLICY), NOW.date())
    assert not blockers
    assert len(actions) == 2
    assert all(a["eligible"] == eligible for a in actions)


@pytest.mark.parametrize("elapsed,eligible", [(29, False), (30, True), (31, True)])
def test_cooldown_boundary_pure(elapsed, eligible):
    policy = {**rec.DEFAULT_POLICY, "next_eligible_date": (NOW.date() + timedelta(days=30-elapsed)).isoformat()}
    _, actions, _, _ = rec.compute_actions(*allocation_fixture(), policy, NOW.date())
    assert all(a["eligible"] == eligible for a in actions)


def test_cash_first_suppresses_unnecessary_sell_and_preserves_normal_buys():
    # Cash buys remove most drift; the remaining 2.5pp must not trigger sales.
    _, actions, _, _ = rec.compute_actions(*allocation_fixture("525", "0", "475"), dict(rec.DEFAULT_POLICY), NOW.date())
    assert next(a for a in actions if a["type"] == "buy" and not a["rebalance"])["eligible"]
    assert not any(a["eligible"] for a in actions if a["rebalance"])


def test_saved_month_end_schedule_and_future_ledger_cutoff():
    origin, end = date(2024, 1, 31), date(2024, 5, 31)
    dates = contribution_dates(origin, end)
    saved = {"inputs": {"starting_capital_eur": "100", "goal_date": end.isoformat()},
             "result": {"valuation_date": origin.isoformat(), "monthly_contribution_eur": "10",
                        "contribution_count": 4, "first_contribution_date": dates[0].isoformat(),
                        "last_contribution_date": dates[-1].isoformat(), "payment_dates": [d.isoformat() for d in dates]}}
    events = [{"type": "deposit", "amount_eur": "100", "date": "2024-01-01"},
              {"type": "deposit", "amount_eur": "1000", "date": "2024-03-01"}]
    before, _, _ = rec.funding_diagnostic(saved, events, date(2024, 2, 28))
    after, _, _ = rec.funding_diagnostic(saved, events, date(2024, 2, 29))
    assert before["shortfall_eur"] == "0.00"
    assert after["shortfall_eur"] == "10.00"
    assert after["net_funded_eur"] == "100.00"


def test_current_goal_edits_do_not_change_saved_targets(tracker, db):
    client, cid, metadata, url = tracker
    pid = saved_plan(client, db, cid, metadata)
    snapshot = get(client, url)
    simulation = db.planner_simulations.find_one({"_id": pid})
    db.planner_goals.update_one({"_id": simulation["goal_id"]}, {"$set": {"goal_eur": "999", "goal_date": "2027-01-01"}})
    assert get(client, url) == snapshot


def test_forecast_shortfall_does_not_manufacture_funding(tracker, db):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata, initial="1000", monthly="0")
    record(client, url, cash(amount="1000"))
    record(client, url, trade(quantity="10", price="100"))
    quote(db, "2026-09-17", "50")
    snapshot = get(client, url)
    assert snapshot["funding"]["shortfall_eur"] == "0.00"
    assert snapshot["status"] == "aligned"
    assert snapshot["actions"] == []
    assert snapshot["allocation"]["equity_eur"] == "500.00"


def test_multi_target_cash_deposit_and_two_buys_atomic(tracker, db, monkeypatch):
    client, cid, metadata, url = tracker
    second_instrument(db)
    saved_plan(client, db, cid, metadata)
    monkeypatch.setitem(STRATEGIES, "buy_and_hold_single_instrument", replace(
        STRATEGIES["buy_and_hold_single_instrument"], recommendation_targets=multi_adapter))
    snapshot = get(client, url)
    assert [a["type"] for a in snapshot["actions"]] == ["deposit", "buy", "buy"]
    payload, preview = review(client, url, snapshot)
    assert set(preview["after"]["holdings"]) == {ISIN, SECOND_ISIN}
    _, outcome = commit(client, url, payload, preview)
    document = db.portfolios.find_one({"_id": metadata["id"]})
    assert len(outcome["event_ids"]) == 3
    assert document["revision"] == 1
    assert not document.get("recommendation_last_rebalance")


def test_incomplete_valuation_never_sells_but_retains_funding(tracker, db):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    record(client, url, cash(amount="1000"))
    record(client, url, trade(quantity="10", price="100"))
    quote(db, "2026-09-17", None)
    snapshot = get(client, url)
    assert snapshot["allocation"]["complete"] is False
    assert [a["type"] for a in snapshot["actions"]] == ["deposit"]


def test_failed_future_history_batch_has_no_receipt_or_append(tracker, db):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    record(client, url, cash(amount="1000"))
    # Future withdrawals are normally rejected on input. Seed a previously reviewed
    # future ledger event to prove full-history replay remains the atomic gate.
    document = db.portfolios.find_one({"_id": metadata["id"]})
    future = deepcopy(document["transactions"][0])
    future.update(id=str(uuid4()), client_id=str(uuid4()), sequence=2, type="withdrawal", date="2026-09-18")
    db.portfolios.update_one({"_id": metadata["id"]}, {"$push": {"transactions": future}})
    snapshot = get(client, url)
    response = client.post(url + "/recommendations/preview", json={"snapshot_token": snapshot["snapshot_token"],
        "date": snapshot["date"], "action_ids": [a["id"] for a in snapshot["actions"]]})
    assert response.status_code == 422
    result = db.portfolios.find_one({"_id": metadata["id"]})
    assert len(result["transactions"]) == 2
    assert not result.get("recommendation_receipts")


@pytest.mark.parametrize("value,options", [(True, {}), ("0.001", {"places": 2}),
    ("0", {"positive": True}), ("Infinity", {}), ("1000000000001", {})])
def test_decimal_validation_rejects_nonfinancial_values(value, options):
    with pytest.raises(ValueError):
        rec.number(value, **options)


@pytest.mark.parametrize("section,key,value", [
    ("result", "valuation_date", "not-a-date"), ("result", "valuation_date", None),
    ("inputs", "goal_date", "2026-07-01"), ("inputs", "goal_date", "2026-07-18"),
    ("inputs", "goal_date", "2080-07-17"), ("result", "goal_date", "2030-01-01"),
    ("result", "contribution_count", 1), ("result", "payment_dates", ["2026-08-18"]),
    ("result", "monthly_contribution_eur", "1.001"), ("inputs", "starting_capital_eur", True)])
def test_invalid_saved_schedule_returns_explicit_unavailable(tracker, db, section, key, value):
    client, cid, metadata, url = tracker
    pid = saved_plan(client, db, cid, metadata)
    db.planner_simulations.update_one({"_id": pid}, {"$set": {f"{section}.{key}": value}})
    result = get(client, url)
    assert result["status"] == "unsupported"
    assert result["blockers"] and result["actions"] == []


@pytest.mark.parametrize("key,value", [
    ("version", ""), ("version", None),
    ("targets", [{"instrument_id": "not-an-isin", "weight": "1"}]),
    ("targets", [{"instrument_id": ISIN, "weight": "0.5"}, {"instrument_id": ISIN, "weight": "0.5"}]),
    ("targets", []), ("supported_actions", None), ("supported_actions", "buy"),
    ("supported_actions", ["borrow"]), ("constraints", {"force": True}),
    ("constraints", {"cooldown_days": True}), ("constraints", {"cooldown_days": -1}),
    ("constraints", {"cooldown_days": 36501}), ("constraints", {"min_drift": "1.01"}),
    ("constraints", {"max_fee_ratio": "1.01"}), ("constraints", {"min_trade_eur": "NaN"})])
def test_target_contract_rejects_invalid_descriptor(monkeypatch, key, value):
    descriptor = multi_adapter(None, NOW.date(), {})
    descriptor[key] = value
    monkeypatch.setitem(STRATEGIES, "buy_and_hold_single_instrument", replace(
        STRATEGIES["buy_and_hold_single_instrument"], recommendation_targets=lambda *args: descriptor))
    saved = {"inputs": {"strategy": {"id": "buy_and_hold_single_instrument", "parameters": {"instrument": ISIN}}}}
    with pytest.raises(ValueError):
        rec.target_contract(saved, NOW.date(), {})


def test_stricter_strategy_policy_and_tolerance_normalization(monkeypatch):
    descriptor = multi_adapter(None, NOW.date(), {})
    descriptor["targets"][0]["weight"] = "0.500000001"
    descriptor["constraints"] = {"cooldown_days": 45, "min_drift": "0.1", "min_trade_eur": "50", "max_fee_ratio": "0.005"}
    monkeypatch.setitem(STRATEGIES, "buy_and_hold_single_instrument", replace(
        STRATEGIES["buy_and_hold_single_instrument"], recommendation_targets=lambda *args: descriptor))
    saved = {"inputs": {"strategy": {"id": "buy_and_hold_single_instrument", "parameters": {"instrument": ISIN}}}}
    _, weights, policy = rec.target_contract(saved, NOW.date(), {})
    assert sum(weights.values()) == Decimal(1)
    assert policy["cooldown_days"] == 45
    assert {key: Decimal(policy[key]) for key in ("min_drift", "min_trade_eur", "max_fee_ratio")} == {
        "min_drift": Decimal("0.1"), "min_trade_eur": Decimal("50"), "max_fee_ratio": Decimal("0.005")}
    descriptor["constraints"] = {"cooldown_days": 1, "min_drift": "0.01", "min_trade_eur": "1", "max_fee_ratio": "0.1"}
    _, _, policy = rec.target_contract(saved, NOW.date(), {})
    assert policy["cooldown_days"] == 30
    assert Decimal(policy["min_drift"]) == Decimal("0.05")
    assert Decimal(policy["min_trade_eur"]) == Decimal("25")
    assert Decimal(policy["max_fee_ratio"]) == Decimal("0.01")


@pytest.mark.parametrize("day,data", [("2026-09-01", {}), ("2026-09-17", {"derived": True}),
    ("2026-09-17", {"source": "filled"}), ("2026-09-17", {"carried_from_date": "2026-09-16"}),
    ("2026-09-17", {"close": "0.000000001"})])
def test_unsuitable_raw_default_requires_override_without_writes(tracker, db, day, data):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    db.quotes.delete_many({})
    observation = dict(data)
    close = observation.pop("close", "110")
    quote(db, day, close, **observation)
    before = db.portfolios.find_one({"_id": metadata["id"]})
    result = get(client, url)
    action = next(a for a in result["actions"] if a["type"] == "buy")
    assert action["price_eur"] is None and action["blockers"]
    payload = {"snapshot_token": result["snapshot_token"], "date": result["date"],
               "action_ids": [a["id"] for a in result["actions"]]}
    response = client.post(url + "/recommendations/preview", json=payload)
    assert response.status_code == 422 and "price override" in response.json()["detail"]
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before
    review(client, url, result, overrides={action["id"]: {"price_eur": "100"}})


@pytest.mark.parametrize("mode", ["foreign-currency", "missing-acquisition", "missing-listing", "pinned-mismatch"])
def test_listing_ineligibility_cannot_be_bypassed_by_price_override(tracker, db, mode):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    if mode == "pinned-mismatch":
        record(client, url, cash(amount="1000"))
        record(client, url, trade(quantity="1", price="100"))
        pinned = db.portfolios.find_one({"_id": metadata["id"]})["transactions"][1]["instrument"]
        positions = {ISIN: {"instrument": {**pinned, "symbol": "OTHER"}}}
        market = rec.market_snapshot(db, {ISIN}, positions, NOW)
        assert not market[ISIN]["eligible"]
        assert "Pinned listing" in market[ISIN]["blockers"][0]
        return
    if mode == "foreign-currency":
        quote(db, "2026-09-17", "100", currency="USD")
    elif mode == "missing-acquisition":
        db.instruments.delete_many({})
    else:
        db.instrument_state.delete_many({})
    result = get(client, url)
    action = next(a for a in result["actions"] if a["type"] == "buy")
    assert not action["eligible"] and action["blockers"]
    response = client.post(url + "/recommendations/preview", json={"snapshot_token": result["snapshot_token"],
        "date": result["date"], "action_ids": [a["id"] for a in result["actions"]],
        "overrides": {action["id"]: {"price_eur": "100"}}})
    assert response.status_code == 422 and "blocked" in response.json()["detail"]


def test_missing_foreign_plan_and_inactive_horizon(tracker, db):
    client, cid, metadata, url = tracker
    assert get(client, url)["status"] == "unsupported"
    pid = saved_plan(client, db, cid, metadata, origin="2026-09-18")
    result = get(client, url)
    assert result["actions"] == [] and "active horizon" in result["blockers"][0]
    db.planner_simulations.update_one({"_id": pid}, {"$set": {"user_id": "foreign-owner"}})
    assert client.get(url + "/recommendations").status_code == 404


def test_strategy_action_restriction_blocks_dependent_buys(tracker, db, monkeypatch):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    def adapter(*args):
        return {"version": "restricted-v1", "targets": [{"instrument_id": ISIN, "weight": "1"}],
                "supported_actions": ["buy"], "constraints": {}}
    monkeypatch.setitem(STRATEGIES, "buy_and_hold_single_instrument", replace(
        STRATEGIES["buy_and_hold_single_instrument"], recommendation_targets=adapter))
    result = get(client, url)
    assert result["status"] == "blocked"
    assert all(not a["eligible"] for a in result["actions"])
    assert "unsupported" in result["actions"][0]["blockers"][0]
    assert "dependent" in result["actions"][1]["blockers"][0]


@pytest.mark.parametrize("mutation", ["invalid-date", "numeric-date", "missing-date", "unknown-action", "unknown-override", "deposit-override"])
def test_preview_rejects_invalid_review_payload_without_writes(tracker, db, mutation):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    snapshot = get(client, url)
    payload = {"snapshot_token": snapshot["snapshot_token"], "date": snapshot["date"], "action_ids": ["deposit"]}
    if mutation == "invalid-date":
        payload["date"] = "2026-99-17"
    elif mutation == "numeric-date":
        payload["date"] = 20260917
    elif mutation == "missing-date":
        del payload["date"]
    elif mutation == "unknown-action":
        payload["action_ids"] = ["invented"]
    elif mutation == "unknown-override":
        payload["overrides"] = {"invented": {"price_eur": "10"}}
    else:
        payload["overrides"] = {"deposit": {"quantity": "1"}}
    before = db.portfolios.find_one({"_id": metadata["id"]})
    assert client.post(url + "/recommendations/preview", json=payload).status_code == 422
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before


def test_lifetime_ledger_cap_rejects_batch_before_any_append(tracker, db, monkeypatch):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    snapshot = get(client, url)
    monkeypatch.setattr(rec.ledger, "MAX_TRANSACTIONS", 1)
    before = db.portfolios.find_one({"_id": metadata["id"]})
    response = client.post(url + "/recommendations/preview", json={"snapshot_token": snapshot["snapshot_token"],
        "date": snapshot["date"], "action_ids": [a["id"] for a in snapshot["actions"]]})
    assert response.status_code == 409 and "lifetime" in response.json()["detail"]
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before


def test_valid_shape_wrong_preview_token_never_commits(tracker, db):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    snapshot = get(client, url)
    payload, preview = review(client, url, snapshot)
    response = client.post(url + "/recommendations/fulfill", json={**payload, "operation_id": str(uuid4()),
        "preview_token": "a" * 64 if preview["preview_token"] != "a" * 64 else "b" * 64})
    assert response.status_code == 409 and "Preview changed" in response.json()["detail"]
    assert db.portfolios.find_one({"_id": metadata["id"]})["transactions"] == []


def test_schedule_rejects_consistent_but_too_short_horizon(tracker, db):
    client, cid, metadata, url = tracker
    pid = saved_plan(client, db, cid, metadata)
    db.planner_simulations.update_one({"_id": pid}, {"$set": {
        "inputs.goal_date": "2026-07-18", "result.goal_date": "2026-07-18"}})
    result = get(client, url)
    assert result["actions"] == []
    assert result["blockers"] == ["Invalid saved payment horizon"]


def test_subminimum_rebalance_leg_and_unrepresentable_quantity_are_not_eligible():
    funding, _, cash_balance, positions, market = allocation_fixture("400", "100")
    weights = {ISIN: Decimal("0.75"), SECOND_ISIN: Decimal("0.24"), None: Decimal("0.01")}
    _, actions, _, _ = rec.compute_actions(funding, weights, cash_balance, positions, market,
                                           dict(rec.DEFAULT_POLICY), NOW.date())
    buy = next(a for a in actions if a["type"] == "buy")
    assert buy["amount_eur"] == "20.00" and not buy["eligible"]
    assert "minimum gross" in buy["blockers"][0]
    market[ISIN]["price_eur"] = "1000000000000"
    _, actions, _, _ = rec.compute_actions(funding, {ISIN: Decimal(1)}, Decimal("0.01"), {}, market,
                                           dict(rec.DEFAULT_POLICY), NOW.date())
    assert actions == []  # Dust must never turn into a zero-quantity ledger event.


@pytest.mark.parametrize("mode", ["zero-quantity", "all-fees"])
def test_unexecutable_price_or_fee_edit_cannot_create_events(tracker, db, mode):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    snapshot = get(client, url)
    buy = next(a for a in snapshot["actions"] if a["type"] == "buy")
    override = {"price_eur": "1000000000000"} if mode == "zero-quantity" else {"commission_eur": "1200"}
    before = db.portfolios.find_one({"_id": metadata["id"]})
    response = client.post(url + "/recommendations/preview", json={"snapshot_token": snapshot["snapshot_token"],
        "date": snapshot["date"], "action_ids": [a["id"] for a in snapshot["actions"]],
        "overrides": {buy["id"]: override}})
    assert response.status_code == 422
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before


def test_rebalance_quantity_edit_cannot_bypass_minimum_gross(tracker, db, monkeypatch):
    client, cid, metadata, url = tracker
    second_instrument(db)
    saved_plan(client, db, cid, metadata, initial="1100", monthly="0")
    monkeypatch.setitem(STRATEGIES, "buy_and_hold_single_instrument", replace(
        STRATEGIES["buy_and_hold_single_instrument"], recommendation_targets=multi_adapter))
    record(client, url, cash(amount="1100"))
    record(client, url, trade(quantity="10", price="110"))
    snapshot = get(client, url)
    sell = next(a for a in snapshot["actions"] if a["type"] == "sell")
    response = client.post(url + "/recommendations/preview", json={"snapshot_token": snapshot["snapshot_token"],
        "date": snapshot["date"], "action_ids": [a["id"] for a in snapshot["actions"]],
        "overrides": {sell["id"]: {"quantity": "0.01"}}})
    assert response.status_code == 422 and "minimum gross" in response.json()["detail"]
    assert not db.portfolios.find_one({"_id": metadata["id"]}).get("recommendation_last_rebalance")


def test_commit_rechecks_market_after_rebuilding_preview(tracker, db, monkeypatch):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    snapshot = get(client, url)
    payload, preview = review(client, url, snapshot)
    original = rec.recommendation_snapshot
    calls = 0
    def market_changes_before_final_check(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            quote(db, "2026-09-17", "111")
        return original(*args, **kwargs)
    monkeypatch.setattr(rec, "recommendation_snapshot", market_changes_before_final_check)
    response = client.post(url + "/recommendations/fulfill", json={**payload,
        "operation_id": str(uuid4()), "preview_token": preview["preview_token"]})
    assert response.status_code == 409 and "Market, date, plan or policy changed" in response.json()["detail"]
    assert calls == 2
    assert db.portfolios.find_one({"_id": metadata["id"]})["transactions"] == []


@pytest.mark.parametrize("price,quantity,initial", [
    ("1", "100.004", "300.00"),
    ("10000000.01", "0.00001001", "300.30"),
])
@pytest.mark.parametrize("fee", ["0.00", "1.00"])
def test_multi_sale_buys_use_executable_settlement(tracker, db, monkeypatch, price, quantity, initial, fee):
    client, cid, metadata, url = tracker
    second_instrument(db)
    extra = ["IE00B3XXRP09", "IE00BK5BQT80"]
    for index, isin in enumerate(extra):
        listing = {"symbol": f"ROUND{index}", "exchange": "XETR"}
        db.instrument_state.insert_one({"_id": isin, "listing": listing})
        db.instruments.insert_one({"instrument_id": isin, **listing, "collected_at": NOW,
                                   "job_id": "rounding-target", "data": {}})
        quote(db, NOW.date().isoformat(), "1", isin=isin, listing=listing)
    sellers = [ISIN, SECOND_ISIN, extra[0]]
    for isin in sellers:
        listing = db.instrument_state.find_one({"_id": isin})["listing"]
        quote(db, NOW.date().isoformat(), price, isin=isin, listing=listing)
    saved_plan(client, db, cid, metadata, initial=initial, monthly="0")
    monkeypatch.setitem(STRATEGIES, "buy_and_hold_single_instrument", replace(
        STRATEGIES["buy_and_hold_single_instrument"], recommendation_targets=lambda *_: {
            "version": "rounding-target-v1", "targets": [{"instrument_id": extra[1], "weight": "1"}],
            "supported_actions": ["deposit", "sell", "buy"], "constraints": {},
        }))
    record(client, url, cash(amount=initial))
    for isin in sellers:
        record(client, url, trade(quantity=quantity, price=price, instrument_id=isin))
    snapshot = get(client, url)
    assert len(snapshot["actions"]) == 4
    assert all(action["eligible"] for action in snapshot["actions"])
    sell = next(action for action in snapshot["actions"] if action["type"] == "sell")
    payload, preview = review(client, url, snapshot, overrides={sell["id"]: {"commission_eur": fee}})
    sales = [entry for entry in preview["entries"] if entry["type"] == "sell"]
    buy = next(entry for entry in preview["entries"] if entry["type"] == "buy")
    proceeds = sum(((Decimal(entry["quantity"]) * Decimal(entry["price_eur"])).quantize(
        rec.ledger.CENT, rounding=rec.ROUND_HALF_UP) for entry in sales), Decimal(0))
    purchase = (Decimal(buy["quantity"]) * Decimal(buy["price_eur"])).quantize(rec.ledger.CENT)
    assert purchase == proceeds - Decimal(fee)
    assert preview["after"]["cash_eur"] == "0.00"
    assert preview["total_commission_eur"] == fee
    assert all(Decimal(entry["quantity"]) <= Decimal(quantity) for entry in sales)
    _, outcome = commit(client, url, payload, preview)
    assert len(outcome["event_ids"]) == 4
    document = db.portfolios.find_one({"_id": metadata["id"]})
    cash_after, _, holdings = rec.ledger.replay(document["transactions"])
    assert cash_after == 0
    assert all(position["quantity"] >= 0 for position in holdings.values())


@pytest.mark.parametrize("race", ["committed-identical", "revision-conflict"])
def test_cas_miss_reconciles_receipt_or_requires_new_review(tracker, db, monkeypatch, race):
    from pymongo.results import UpdateResult
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    snapshot = get(client, url)
    payload, preview = review(client, url, snapshot)
    original = db.portfolios.update_one
    def competing_update(query, update, *args, **kwargs):
        if race == "committed-identical":
            # Model an identical writer having committed before this CAS loses.
            original(query, update, *args, **kwargs)
        else:
            original({"_id": metadata["id"]}, {"$inc": {"revision": 1}})
        return UpdateResult({"n": 0, "nModified": 0}, True)
    monkeypatch.setattr(db.portfolios, "update_one", competing_update)
    response = client.post(url + "/recommendations/fulfill", json={**payload,
        "operation_id": str(uuid4()), "preview_token": preview["preview_token"]})
    document = db.portfolios.find_one({"_id": metadata["id"]})
    assert document["revision"] == 1
    if race == "committed-identical":
        assert response.status_code == 200
        assert len(document["transactions"]) == 2
        assert response.json() == document["recommendation_receipts"][0]["response"]
    else:
        assert response.status_code == 409 and "concurrently" in response.json()["detail"]
        assert document["transactions"] == [] and not document.get("recommendation_receipts")


@pytest.mark.parametrize("site,snapshot_call,stale_detail", [
    ("prepare-preview", 1, "Recommendations changed. Refresh and review again."),
    ("before-cas", 2, "Market, date, plan or policy changed. Review again."),
])
@pytest.mark.parametrize("receipt", ["matching", "changed-payload", "absent"])
def test_stale_review_reconciles_concurrent_receipt(tracker, db, monkeypatch, site, snapshot_call, stale_detail, receipt):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    snapshot = get(client, url)
    payload, preview = review(client, url, snapshot)
    body = {**payload, "operation_id": str(uuid4()), "preview_token": preview["preview_token"]}
    winner_body = body
    if receipt == "changed-payload":
        winner_payload, winner_preview = review(client, url, snapshot, ["deposit"])
        winner_body = {**winner_payload, "operation_id": body["operation_id"],
                       "preview_token": winner_preview["preview_token"]}
    before = db.portfolios.find_one({"_id": metadata["id"]})
    original = rec.recommendation_snapshot
    calls, committed, after_winner = 0, None, None

    def commit_then_change_market(*args, **kwargs):
        nonlocal calls, committed, after_winner
        calls += 1
        stale_check = calls == snapshot_call
        if stale_check:
            # Complete the competing writer after this request's initial receipt
            # lookup, then dirty the market before its selected stale-state check.
            if receipt != "absent":
                winner = client.post(url + "/recommendations/fulfill", json=winner_body)
                assert winner.status_code == 200, winner.text
                committed = winner.json()
            after_winner = db.portfolios.find_one({"_id": metadata["id"]})
            quote(db, "2026-09-17", "111")
        result = original(*args, **kwargs)
        if stale_check:
            assert result["snapshot_token"] != body["snapshot_token"]
        return result

    monkeypatch.setattr(rec, "recommendation_snapshot", commit_then_change_market)
    response = client.post(url + "/recommendations/fulfill", json=body)
    assert calls == snapshot_call + (2 if receipt != "absent" else 0)
    if receipt == "matching":
        assert response.status_code == 200, response.text
        assert response.json() == committed
    else:
        assert response.status_code == 409, response.text
        expected = "Operation ID was already used with different reviewed inputs." if receipt == "changed-payload" else stale_detail
        assert response.json()["detail"] == expected
    assert db.portfolios.find_one({"_id": metadata["id"]}) == after_winner
    if receipt == "absent":
        assert after_winner == before
    else:
        assert after_winner["revision"] == before["revision"] + 1
        assert len(after_winner["recommendation_receipts"]) == 1
        assert client.post(url + "/recommendations/fulfill", json=winner_body).json() == committed
