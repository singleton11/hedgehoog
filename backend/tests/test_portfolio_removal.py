"""Removal must preserve funding, ownership, retry safety and atomicity."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from bson import ObjectId
from pymongo.results import UpdateResult
import pytest

import portfolio_tracker as tracker_api
from test_portfolio_tracker import (
    api, cash, clock, config, db, detail, portfolio, record,
    synchronize_first_two_writes, trade, tracker,
)


def remove(client, url, event):
    response = client.delete(url + "/transactions/" + event["id"])
    assert response.status_code == 200, response.text
    assert response.json() == {"id": event["id"], "removed": True}
    return response


def test_remove_all_transaction_types_recalculates_balances_and_net_worth(tracker, db):
    client, customer_id, metadata, url = tracker
    deposit = record(client, url, cash())
    buy = record(client, url, trade(quantity="2", commission_eur="2"))
    sell = record(client, url, trade("sell", price="120", day="2026-09-16", commission_eur="1"))
    withdrawal = record(client, url, cash("withdrawal", "50", "2026-09-17"))
    assert detail(client, url)["cash_eur"] == "867.00"
    assert detail(client, url)["realized_pnl_eur"] == "17.00"
    for event, expected_cash, quantity, realized, equity in [
        (withdrawal, "917.00", "1", "17.00", "1027.00"),
        (sell, "798.00", "2", "-2.00", "1018.00"),
        (buy, "1000.00", None, "0.00", "1000.00"),
        (deposit, "0.00", None, "0.00", "0.00"),
    ]:
        before = db.portfolios.find_one({"_id": metadata["id"]})
        response = remove(client, url, event)
        assert response.headers["Cache-Control"] == "no-store"
        current = detail(client, url)
        assert current["cash_eur"] == expected_cash
        assert current["realized_pnl_eur"] == realized
        assert current["equity_eur"] == equity
        assert [item["quantity"] for item in current["positions"]] == ([quantity] if quantity else [])
        assert client.get("/customers").json()["items"][0]["net_worth_eur"] == equity
        stored = db.portfolios.find_one({"_id": metadata["id"]})
        assert stored["revision"] == before["revision"] + 1
        assert len(stored["transactions"]) == 4  # Internal retry/order evidence retained.
        assert next(item for item in stored["transactions"] if item["id"] == event["id"])["_removed_at"]
    assert client.get(url + "/transactions").json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
    assert detail(client, url)["daily_unrealized_pnl_eur"] == "0.00"


def test_remove_middle_buy_replays_weighted_cost_and_daily_pnl(tracker):
    client, _, _, url = tracker
    record(client, url, cash())
    record(client, url, trade(quantity="2", price="100"))
    middle = record(client, url, trade(price="50", day="2026-09-16", commission_eur="1"))
    record(client, url, trade("sell", price="120", day="2026-09-16", commission_eur="1"))
    remove(client, url, middle)
    result = detail(client, url)
    assert result["cash_eur"] == "919.00"
    assert result["realized_pnl_eur"] == "19.00"
    assert result["unrealized_pnl_eur"] == result["daily_unrealized_pnl_eur"] == "10.00"
    assert result["positions"][0]["cost_basis_eur"] == "100.00"
    assert result["positions"][0]["quantity"] == "1"


@pytest.mark.parametrize("kind", ["deposit", "buy", "sell"])
def test_removal_rejects_historical_negative_balances_without_writing(tracker, db, kind):
    client, _, metadata, url = tracker
    deposit = record(client, url, cash(amount="100"))
    buy = record(client, url, trade())
    sell = record(client, url, trade("sell", day="2026-09-15"))
    record(client, url, cash("withdrawal", "100", "2026-09-16"))
    # A later deposit makes the final cash positive but cannot fund earlier events.
    record(client, url, cash(amount="1000", day="2026-09-17"))
    event = {"deposit": deposit, "buy": buy, "sell": sell}[kind]
    before = db.portfolios.find_one({"_id": metadata["id"]})
    response = client.delete(url + "/transactions/" + event["id"])
    assert response.status_code == 422
    assert "Cannot remove this transaction" in response.json()["detail"]
    assert ("holdings" if kind == "buy" else "cash") in response.json()["detail"]
    assert "Remove dependent transactions first" in response.json()["detail"]
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before


def test_retry_is_idempotent_and_old_post_cannot_resurrect_removed_event(tracker, db):
    client, _, metadata, url = tracker
    body = cash()
    event = record(client, url, body)
    remove(client, url, event)
    before = db.portfolios.find_one({"_id": metadata["id"]})
    remove(client, url, event)
    response = client.post(url + "/transactions", json=body)
    assert response.status_code == 409 and "removed" in response.json()["detail"]
    assert client.post(url + "/transactions", json={**body, "amount_eur": "999"}).status_code == 409
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before
    assert record(client, url, cash())["sequence"] == 2


def test_unknown_or_wrong_portfolio_transaction_is_not_removed(tracker, db):
    client, customer_id, _, url = tracker
    event = record(client, url, cash())
    _, other_url = portfolio(client, customer_id)
    before = list(db.portfolios.find())
    assert client.delete(url + f"/transactions/{ObjectId()}").status_code == 404
    assert client.delete(other_url + "/transactions/" + event["id"]).status_code == 404
    assert list(db.portfolios.find()) == before


def test_removed_events_are_excluded_before_pagination_and_do_not_reuse_sequence(tracker):
    client, _, _, url = tracker
    events = [record(client, url, cash(amount="10", day="2026-09-14")) for _ in range(4)]
    remove(client, url, events[1])
    remove(client, url, events[3])
    new = record(client, url, cash(amount="10", day="2026-09-14"))
    assert new["sequence"] == 5
    assert client.get(url + "/transactions?limit=2&offset=1").json() == {
        "items": [events[2], events[0]], "total": 3, "limit": 2, "offset": 1,
    }


def test_removal_does_not_bypass_document_size_cap(tracker, monkeypatch):
    client, _, _, url = tracker
    monkeypatch.setattr(tracker_api, "MAX_TRANSACTIONS", 1)
    event = record(client, url, cash())
    remove(client, url, event)
    assert client.post(url + "/transactions", json=cash()).status_code == 409
    remove(client, url, event)  # Deletion retries remain allowed at capacity.


@pytest.mark.parametrize("race", ["two-deposits", "remove-and-buy", "same-event"])
def test_removal_cas_revalidates_against_concurrent_writes(tracker, db, monkeypatch, race):
    client, _, metadata, url = tracker
    first = record(client, url, cash(amount="100"))
    second = record(client, url, cash(amount="100"))
    if race == "two-deposits":
        record(client, url, trade())
    before = db.portfolios.find_one({"_id": metadata["id"]})
    revisions = synchronize_first_two_writes(monkeypatch, db.portfolios)
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(client.delete, url + "/transactions/" + first["id"])]
        if race == "remove-and-buy":
            futures.append(workers.submit(client.post, url + "/transactions", json=trade(price="150")))
        else:
            futures.append(workers.submit(client.delete, url + "/transactions/" + (first if race == "same-event" else second)["id"]))
        responses = [future.result(timeout=15) for future in futures]
    assert revisions[:2] == [before["revision"], before["revision"]]
    statuses = sorted(response.status_code for response in responses)
    assert statuses in ([[200, 422], [201, 422]] if race == "remove-and-buy" else
                        [[200, 200]] if race == "same-event" else [[200, 422]])
    stored = db.portfolios.find_one({"_id": metadata["id"]})
    assert stored["revision"] == before["revision"] + 1
    tracker_api.replay(stored["transactions"])  # Full timeline remains valid.
    if race == "same-event":
        assert responses[0].json() == responses[1].json()


def test_removal_cas_retries_are_bounded_without_writes(tracker, db, monkeypatch):
    client, _, metadata, url = tracker
    event = record(client, url, cash())
    before = db.portfolios.find_one({"_id": metadata["id"]})
    write = Mock(return_value=UpdateResult({"n": 0, "nModified": 0}, acknowledged=True))
    monkeypatch.setattr(db.portfolios, "update_one", write)
    response = client.delete(url + "/transactions/" + event["id"])
    assert response.status_code == 409
    assert write.call_count == 8
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before
