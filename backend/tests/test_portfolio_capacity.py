"""Real BSON capacity boundaries, including durable receipts and removal headroom."""
from copy import deepcopy
from uuid import uuid4

from bson import BSON, Int64
import pytest

import portfolio_recommendations as rec
from test_portfolio_recommendations import (saved_plan, get, review, commit, tracker, db,
                                             api, clock, config, record, cash)
from test_market_data import ISIN


BSON_LIMIT = 16 * 1024 * 1024
RESERVE = 64 * 1024
REMOVED_AT = "9999-12-31T23:59:59.999999Z"


def reserved_size(document):
    candidate = deepcopy(document)
    candidate["revision"] = Int64(candidate["revision"])
    for event in candidate["transactions"]:
        event.setdefault("_removed_at", REMOVED_AT)
    return len(BSON.encode(candidate))


@pytest.fixture
def full_receipt_ledger(tracker, db):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata)
    instrument = {"instrument_id": ISIN, "name": "\U0001f4b0" * 200,
                  "symbol": "S" * 200, "exchange": "E" * 200, "currency": "EUR"}
    listing = {key: instrument[key] for key in ("symbol", "exchange")}
    db.instrument_state.update_one({"_id": ISIN}, {"$set": {"listing": listing}})
    db.instruments.update_many({"instrument_id": ISIN}, {"$set": listing})
    db.quotes.update_many({"instrument_id": ISIN}, {"$set": listing})
    db.acquisition_jobs.update_many({"instrument_id": ISIN}, {"$set": {"listing": listing}})
    db.split_snapshots.update_many({"instrument_id": ISIN}, {"$set": listing})
    db.etfs.update_many({"isin": ISIN}, {"$set": {"name": instrument["name"]}})
    assert rec.ledger.acquired_instrument(db, ISIN, rec.md.utcnow()) == instrument
    record(client, url, cash(amount="1000000000"))
    snapshot = get(client, url)
    action = next(a for a in snapshot["actions"] if a["type"] == "buy")
    payload, preview = review(client, url, snapshot, [action["id"]],
                              {action["id"]: {"quantity": "1", "price_eur": "110"}})
    body, result = commit(client, url, payload, preview)
    seed = db.portfolios.find_one({"_id": metadata["id"]})
    event, receipt = seed["transactions"][-1], seed["recommendation_receipts"][0]

    def history(count):
        document = deepcopy(seed)
        document["transactions"] = [deepcopy(seed["transactions"][0])]
        document["recommendation_receipts"] = []
        for index in range(count):
            operation_id = str(uuid4())
            entry_id = str(uuid4())
            entry, retry = deepcopy(event), deepcopy(receipt)
            entry.update(id=entry_id, client_id=entry_id, sequence=index + 2)
            entry["_request"]["client_id"] = entry_id
            entry["recommendation"]["operation_id"] = operation_id
            retry["operation_id"] = operation_id
            retry["response"].update(operation_id=operation_id, revision=index + 2,
                                      event_ids=[entry_id], entries=[rec.ledger.event_public(entry)])
            document["transactions"].append(entry)
            document["recommendation_receipts"].append(retry)
        document["revision"] = count + 1
        return document

    assert len(BSON.encode(history(4000))) > BSON_LIMIT
    low, high = 1, 4998
    while low < high:
        middle = (low + high + 1) // 2
        if reserved_size(history(middle)) <= BSON_LIMIT - RESERVE:
            low = middle
        else:
            high = middle - 1
    # One append fits; the following append must conflict below the count cap.
    document = history(low - 1)
    assert len(document["transactions"]) < rec.ledger.MAX_TRANSACTIONS - 2
    assert reserved_size(history(low)) <= BSON_LIMIT - RESERVE
    assert reserved_size(history(low + 1)) > BSON_LIMIT - RESERVE
    db.portfolios.replace_one({"_id": metadata["id"]}, document)
    return client, url, metadata


def test_real_bson_receipt_capacity_atomic_retry_and_removal(full_receipt_ledger, db):
    client, url, metadata = full_receipt_ledger

    def reviewed_buy():
        snapshot = get(client, url)
        action = next(a for a in snapshot["actions"] if a["type"] == "buy")
        return review(client, url, snapshot, [action["id"]],
                      {action["id"]: {"quantity": "1", "price_eur": "110"}})

    payload, preview = reviewed_buy()
    body, response = commit(client, url, payload, preview)
    before = db.portfolios.find_one({"_id": metadata["id"]})
    assert reserved_size(before) <= BSON_LIMIT - RESERVE
    payload, preview = reviewed_buy()
    rejected = client.post(url + "/recommendations/fulfill", json={
        **payload, "operation_id": str(uuid4()), "preview_token": preview["preview_token"]})
    assert rejected.status_code == 409, rejected.text
    assert "capacity" in rejected.json()["detail"].lower()
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before
    assert client.post(url + "/recommendations/fulfill", json=body).json() == response
    altered = client.post(url + "/recommendations/fulfill", json={**body, "action_ids": ["deposit"]})
    assert altered.status_code == 409
    event_id = response["event_ids"][0]
    assert client.delete(url + "/transactions/" + event_id).status_code == 200
    after = db.portfolios.find_one({"_id": metadata["id"]})
    assert len(after["transactions"]) == len(before["transactions"])
    assert after["recommendation_receipts"] == before["recommendation_receipts"]
    assert client.post(url + "/recommendations/fulfill", json=body).json() == response
    assert reserved_size(after) <= BSON_LIMIT - RESERVE


def test_import_append_uses_shared_bson_budget(full_receipt_ledger, db):
    from test_degiro_import import row, statement, preview, commit

    client, url, metadata = full_receipt_ledger
    content = statement([row(time=f"09:{index:02d}", amount="1,00") for index in reversed(range(30))])
    before = db.portfolios.find_one({"_id": metadata["id"]})
    planned = preview(client, url, content)
    assert planned["can_import"], planned["errors"]
    response = commit(client, url, content, planned)
    assert response.status_code == 409, response.text
    assert "capacity" in response.json()["detail"].lower()
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before


def test_manual_append_uses_remaining_shared_bson_budget(full_receipt_ledger, db):
    client, url, metadata = full_receipt_ledger
    # A few smaller manual entries use the remaining budget, then conflict atomically.
    for _ in range(30):
        before = db.portfolios.find_one({"_id": metadata["id"]})
        response = client.post(url + "/transactions", json=cash(amount="1"))
        if response.status_code == 409:
            assert "capacity" in response.json()["detail"].lower()
            assert db.portfolios.find_one({"_id": metadata["id"]}) == before
            break
        assert response.status_code == 201, response.text
        assert reserved_size(db.portfolios.find_one({"_id": metadata["id"]})) <= BSON_LIMIT - RESERVE
    else:
        pytest.fail("Manual appends ignored the full BSON document capacity")
