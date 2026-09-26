"""Synthetic statements only: no customer account exports in the repository."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import csv
from io import StringIO
import json
from threading import Barrier, Lock

import pytest

import degiro_import as importer
import portfolio_tracker as ledger
from test_portfolio_tracker import (ISIN, LISTING, api, cash, clock, config, customer, db,
                                    detail, portfolio, record, tracker, trade)

ORDER_A = "11111111-1111-4111-8111-111111111111"
ORDER_B = "22222222-2222-4222-8222-222222222222"


def row(day="17-09-2026", time="09:00", description="Deposit", amount="100,00", *,
        value=None, isin="", order="", balance="100,00", currency="EUR", fx=""):
    return [day, time, value or day, "Synthetic ETF" if isin else "", isin, description, fx,
            currency if amount is not None else "", amount or "", "EUR", balance, order]


def statement(rows, delimiter=","):
    output = StringIO(newline="")
    writer = csv.writer(output, delimiter=delimiter)
    writer.writerow(importer.HEADER)
    writer.writerows(rows)
    return output.getvalue()


def sample_rows():
    # Two settled deposits, paired reservation/reversal entries, two EUR orders
    # with fees, and internal sweep/counterpart pairs. All values/IDs are synthetic.
    return [
        row(time="10:00", description="Overboeking van uw geldrekening bij flatexDEGIRO Bank 241 EUR", amount=None, balance="357,00"),
        row(time="10:00", description="Degiro Cash Sweep Transfer", amount="241,00", balance="598,00"),
        row(time="09:00", description="DEGIRO Transactiekosten en/of kosten van derden", amount="-1,00", isin=ISIN, order=ORDER_B, balance="357,00"),
        row(time="09:00", description="Koop 2 @ 120 EUR", amount="-240,00", isin=ISIN, order=ORDER_B, balance="358,00"),
        row(time="03:00", description="Overboeking van uw geldrekening bij flatexDEGIRO Bank 502 EUR", amount=None, balance="598,00"),
        row(time="03:00", description="Degiro Cash Sweep Transfer", amount="502,00", balance="1100,00"),
        row(time="01:00", value="16-09-2026", description="iDEAL Deposit", amount="300,00", balance="598,00"),
        row(time="01:00", value="16-09-2026", description="Reservation iDEAL", amount="-300,00", balance="298,00"),
        row(time="01:00", value="16-09-2026", description="iDEAL Deposit", amount="800,00", balance="598,00"),
        row(time="01:00", value="16-09-2026", description="Reservation iDEAL", amount="-800,00", balance="-202,00"),
        row("16-09-2026", "14:00", "DEGIRO Transactiekosten en/of kosten van derden", "-2,00", isin=ISIN, order=ORDER_A, balance="598,00"),
        row("16-09-2026", "14:00", "Koop 5 @ 100 EUR", "-500,00", isin=ISIN, order=ORDER_A, balance="600,00"),
        row("16-09-2026", "13:00", "Reservation iDEAL", "300,00", balance="1100,00"),
        row("16-09-2026", "09:00", "Reservation iDEAL", "800,00", balance="800,00"),
    ]


def preview(client, url, content):
    response = client.post(url + "/imports/degiro/preview", json={"csv": content})
    assert response.status_code == 200, response.text
    return response.json()


def commit(client, url, content, plan):
    return client.post(url + "/imports/degiro", json={"csv": content, "preview_token": plan["preview_token"]})


def test_preview_is_read_only_and_atomic_import_reconciles_exactly(tracker, db):
    client, _, meta, url = tracker
    content = statement(sample_rows())
    before = db.portfolios.find_one({"_id": meta["id"]})
    plan = preview(client, url, content)
    assert plan["errors"] == [] and plan["can_import"]
    assert db.portfolios.find_one({"_id": meta["id"]}) == before
    assert len(plan["ignored_rows"]) == 8
    assert [item["type"] for item in plan["transactions"]] == ["deposit", "deposit", "buy", "buy"]
    assert [item["date"] for item in plan["transactions"]] == ["2026-09-16"] * 3 + ["2026-09-17"]
    assert plan["summary"] == {"new_count": 4, "duplicate_count": 0, "cash_eur": "357.00", "deposits_eur": "1100.00",
                               "withdrawals_eur": "0.00", "commission_eur": "3.00", "positions": [
                                   {"instrument_id": ISIN, "quantity": "7", "cost_basis_eur": "740.00"}]}
    assert plan["transactions"][2]["instrument"]["symbol"] == LISTING["symbol"]
    response = commit(client, url, content, plan)
    assert response.status_code == 200, response.text
    assert response.json() == {"imported_count": 4, "duplicate_count": 0}
    stored = db.portfolios.find_one({"_id": meta["id"]})
    assert stored["revision"] == before["revision"] + 1
    assert [event["sequence"] for event in stored["transactions"]] == [1, 2, 3, 4]
    assert all(event["source"] == "degiro" for event in stored["transactions"])
    assert "csv" not in stored
    saved = detail(client, url)
    assert saved["cash_eur"] == plan["summary"]["cash_eur"]
    assert saved["positions"][0]["quantity"] == "7"
    assert saved["positions"][0]["cost_basis_eur"] == "740.00"
    assert saved["realized_pnl_eur"] == "-3.00"
    assert saved["unrealized_pnl_eur"] == "30.00"
    assert saved["equity_eur"] == "1127.00"
    # Original token retries even after revision increment; no duplicate writes.
    assert commit(client, url, content, plan).json() == {"imported_count": 0, "duplicate_count": 4}
    again = preview(client, url, "\ufeff" + statement(sample_rows(), ";"))
    assert again["summary"]["new_count"] == 0 and again["summary"]["duplicate_count"] == 4
    assert commit(client, url, content, again).status_code == 200
    assert db.portfolios.find_one({"_id": meta["id"]}) == stored


def test_overlapping_exports_deduplicate_by_order_not_row_number_and_append_new_cash(tracker):
    client, _, _, url = tracker
    content = statement(sample_rows())
    assert commit(client, url, content, preview(client, url, content)).status_code == 200
    overlap = statement([row(time="11:00", amount="20,00"), *sample_rows()[:4]])
    plan = preview(client, url, overlap)
    assert plan["can_import"]
    assert plan["summary"]["duplicate_count"] == 1
    assert plan["summary"]["new_count"] == 1
    assert commit(client, url, overlap, plan).status_code == 200
    assert detail(client, url)["cash_eur"] == "377.00"


def test_same_timestamp_uses_reverse_export_order(tracker):
    client, _, _, url = tracker
    content = statement([
        row(description="Buy 1 @ 100 EUR", amount="-100,00", isin=ISIN, order=ORDER_A),
        row(amount="100,00"),
    ])
    plan = preview(client, url, content)
    assert plan["can_import"] and plan["summary"]["cash_eur"] == "0.00"
    assert [event["type"] for event in plan["transactions"]] == ["deposit", "buy"]
    assert commit(client, url, content, plan).status_code == 200


def test_sale_fees_and_withdrawal(tracker):
    client, _, _, url = tracker
    record(client, url, cash(amount="1000"))
    record(client, url, trade(quantity="2", price="100"))
    content = statement([
        row(time="11:00", description="Withdrawal", amount="-50,00"),
        row(description="DEGIRO transaction and/or third party fees", amount="-0,75", isin=ISIN, order=ORDER_A),
        row(description="Sell 1 @ 110,50 EUR", amount="110,50", isin=ISIN, order=ORDER_A),
    ])
    plan = preview(client, url, content)
    assert plan["can_import"]
    assert commit(client, url, content, plan).status_code == 200
    result = detail(client, url)
    assert result["cash_eur"] == "859.75" and result["realized_pnl_eur"] == "9.75"


@pytest.mark.parametrize("change", [
    lambda rows: rows.append(row("15-09-2026", description="Dividend", amount="10,00")),
    lambda rows: rows[3].__setitem__(5, "Koop 2 @ 121 EUR"),
    lambda rows: rows[3].__setitem__(7, "USD"),
    lambda rows: rows[3].__setitem__(6, "1,01"),
    lambda rows: rows[3].__setitem__(11, "not-a-uuid"),
    lambda rows: rows[3].__setitem__(4, "BAD"),
    lambda rows: rows[3].__setitem__(5, "Koop 2 @ 120 USD"),
    lambda rows: rows[3].__setitem__(8, "NaN"),
    lambda rows: rows.pop(3),  # orphan fee
    lambda rows: rows.pop(7),  # missing reservation reversal
    lambda rows: rows.pop(0),  # missing sweep counterpart
    lambda rows: rows.pop(-1),  # incomplete original funding
    lambda rows: rows.insert(3, deepcopy(rows[3])),  # multiple executions of same order
    lambda rows: rows.insert(2, deepcopy(rows[2])),  # duplicated fee
    lambda rows: rows.__setitem__(0, row("31-02-2026")),
    lambda rows: rows.__setitem__(0, row("18-09-2026")),
    lambda rows: rows[0].__setitem__(5, "Overboeking van uw geldrekening bij flatexDEGIRO Bank 0,00001 EUR"),
])
def test_bad_rows_block_whole_batch_without_writes(tracker, db, change):
    client, _, meta, url = tracker
    rows = sample_rows()
    change(rows)
    content = statement(rows)
    before = db.portfolios.find_one({"_id": meta["id"]})
    plan = preview(client, url, content)
    assert not plan["can_import"] and plan["errors"]
    assert commit(client, url, content, plan).status_code == 422
    assert db.portfolios.find_one({"_id": meta["id"]}) == before


@pytest.mark.parametrize("content", ["", "wrong,header\n", 'Date,Time\n"unterminated', "x" * 1_000_001])
def test_malformed_and_oversized_requests_fail_closed(tracker, content):
    client, _, _, url = tracker
    response = client.post(url + "/imports/degiro/preview", json={"csv": content})
    assert response.status_code == 422 or not response.json()["can_import"]


def test_duplicate_cash_rows_are_ambiguous_and_thousands_are_exact(tracker):
    client, _, _, url = tracker
    item = row(amount="1.234,56")
    plan = preview(client, url, statement([item]))
    assert plan["summary"]["cash_eur"] == "1234.56"
    assert not preview(client, url, statement([item, item]))["can_import"]


def test_cash_identity_normalizes_decimal_scale_and_cannot_restore_removed_deposit(tracker):
    client, _, _, url = tracker
    first = statement([row(amount="100,0")])
    second = statement([row(amount="100,00")])
    assert commit(client, url, first, preview(client, url, first)).status_code == 200
    plan = preview(client, url, second)
    assert plan["summary"]["duplicate_count"] == 1 and plan["summary"]["new_count"] == 0
    assert detail(client, url)["cash_eur"] == "100.00"
    event = client.get(url + "/transactions").json()["items"][0]
    assert client.delete(url + "/transactions/" + event["id"]).status_code == 200
    assert not preview(client, url, second)["can_import"]


def test_reservation_funding_keeps_original_same_minute_position(tracker):
    client, _, _, url = tracker
    content = statement([
        row(description="iDEAL Deposit", amount="100,00"),
        row(description="Reservation iDEAL", amount="-100,00"),
        row("16-09-2026", "10:00", "Buy 1 @ 100 EUR", "-100,00", isin=ISIN, order=ORDER_A),
        row("16-09-2026", "10:00", "Reservation iDEAL", "100,00", value="17-09-2026"),
    ])
    plan = preview(client, url, content)
    assert plan["can_import"] and plan["summary"]["cash_eur"] == "0.00"
    assert [item["type"] for item in plan["transactions"]] == ["deposit", "buy"]


def test_preview_token_binds_cash_identity_not_just_visible_date_and_amount(tracker):
    client, _, _, url = tracker
    first = statement([row(time="10:00")])
    second = statement([row(time="11:00")])
    plan = preview(client, url, first)
    changed = preview(client, url, second)
    assert changed["transactions"] == plan["transactions"]
    assert changed["preview_token"] != plan["preview_token"]
    assert commit(client, url, second, plan).status_code == 409


def test_internal_sweeps_must_match_direction_and_magnitude(tracker):
    client, _, _, url = tracker
    rows = sample_rows()
    rows[0][5] = rows[0][5].replace("van uw", "naar uw")
    assert not preview(client, url, statement(rows))["can_import"]
    rows[1][8] = "-241,00"
    assert preview(client, url, statement(rows))["can_import"]


def test_invalid_unicode_is_a_preview_error_not_server_error(tracker):
    client, _, _, url = tracker
    # JSON can carry lone surrogates even though a UTF-8 file cannot.
    assert importer.parse_statement(chr(0xD800))[2] == [{"row": None, "message": "Use valid UTF-8 text."}]
    response = client.post(url + "/imports/degiro/preview", content=json.dumps({"csv": chr(0xD800)}), headers={"Content-Type": "application/json"})
    assert response.status_code == 422 or (response.status_code == 200 and not response.json()["can_import"])


def test_missing_or_changed_instrument_listing_blocks_import(tracker, db):
    client, _, _, url = tracker
    content = statement(sample_rows())
    plan = preview(client, url, content)
    db.instruments.delete_many({})
    missing = preview(client, url, content)
    assert not missing["can_import"]
    assert any("Acquire" in error["message"] for error in missing["errors"])
    assert commit(client, url, content, plan).status_code == 422


def test_listing_and_portfolio_changes_invalidate_preview(tracker, db):
    client, _, meta, url = tracker
    content = statement(sample_rows())
    plan = preview(client, url, content)
    # Currency remains EUR but the resolved listing changes between review/save.
    db.instrument_state.update_one({"_id": ISIN}, {"$set": {"listing": {"symbol": "NEW", "exchange": "XETR"}, "price_currency_override": "EUR"}})
    db.instruments.update_many({"instrument_id": ISIN}, {"$set": {"symbol": "NEW", "exchange": "XETR"}})
    assert commit(client, url, content, plan).status_code == 409
    plan = preview(client, url, content)
    client.patch(url, json={"name": "Changed"})
    assert commit(client, url, content, plan).status_code == 409
    assert db.portfolios.find_one({"_id": meta["id"]})["transactions"] == []


def test_changed_order_removed_events_and_changed_csv_do_not_duplicate(tracker, db):
    client, _, meta, url = tracker
    content = statement(sample_rows())
    plan = preview(client, url, content)
    changed = sample_rows()
    changed[2][8] = "-2,00"
    assert commit(client, url, statement(changed), plan).status_code == 409
    assert commit(client, url, content, plan).status_code == 200
    assert not preview(client, url, statement(changed))["can_import"]
    last = db.portfolios.find_one({"_id": meta["id"]})["transactions"][-1]
    assert client.delete(url + "/transactions/" + last["id"]).status_code == 200
    removed = preview(client, url, content)
    assert not removed["can_import"]
    assert removed["transactions"][-1]["status"] == "removed"
    assert commit(client, url, content, plan).status_code == 422


def test_no_invented_opening_balance_and_lifetime_cap(tracker, db, monkeypatch):
    client, _, _, url = tracker
    buy = statement([row(description="Buy 1 @ 10 EUR", amount="-10,00", isin=ISIN, order=ORDER_A, balance="999,00")])
    plan = preview(client, url, buy)
    assert not plan["can_import"]
    assert "no opening cash is invented" in plan["errors"][-1]["message"]
    monkeypatch.setattr(ledger, "MAX_TRANSACTIONS", 3)
    content = statement(sample_rows())
    plan = preview(client, url, content)
    assert not plan["can_import"] and "lifetime limit" in plan["errors"][-1]["message"]


def test_scope_auth_and_origin_boundaries(tracker, api, db):
    client, cid, meta, url = tracker
    content = statement(sample_rows())
    plan = preview(client, url, content)
    other = customer(client)
    _, other_url = portfolio(client, other)
    for endpoint in [url.replace(cid, other), other_url.replace(other, cid), url.replace(meta["id"], "missing")]:
        assert client.post(endpoint + "/imports/degiro/preview", json={"csv": content}).status_code == 404
        assert commit(client, endpoint, content, plan).status_code == 404
    for headers in [{"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}]:
        assert client.post(url + "/imports/degiro/preview", json={"csv": content}, headers=headers).status_code == 403
        assert client.post(url + "/imports/degiro", json={"csv": content, "preview_token": plan["preview_token"]}, headers=headers).status_code == 403
    db.portfolios.update_one({"_id": meta["id"]}, {"$set": {"user_id": "foreign"}})
    assert commit(client, url, content, plan).status_code == 404
    client.cookies.clear()
    assert client.post(url + "/imports/degiro/preview", json={"csv": content}).status_code == 401
    assert commit(client, url, content, plan).status_code == 401


def synchronize_writes(monkeypatch, collection):
    original = collection.update_one
    barrier, lock, revisions = Barrier(2), Lock(), []

    def update(query, changes, *args, **kwargs):
        with lock:
            revisions.append(query["revision"])
            wait = len(revisions) <= 2
        if wait:
            barrier.wait(timeout=10)
        return original(query, changes, *args, **kwargs)

    monkeypatch.setattr(collection, "update_one", update)
    return revisions


@pytest.mark.parametrize("race", ["identical", "different", "manual"])
def test_atomic_batch_races(tracker, db, monkeypatch, race):
    client, _, meta, url = tracker
    first = statement(sample_rows())
    second = first if race == "identical" else statement([row(amount="10,00")])
    a, b = preview(client, url, first), preview(client, url, second)
    revisions = synchronize_writes(monkeypatch, db.portfolios)
    with ThreadPoolExecutor(max_workers=2) as executor:
        one = executor.submit(commit, client, url, first, a)
        two = executor.submit(client.post, url + "/transactions", json=cash(amount="10")) if race == "manual" else executor.submit(commit, client, url, second, b)
        responses = [one.result(), two.result()]
    assert revisions[:2] == [0, 0]
    stored = db.portfolios.find_one({"_id": meta["id"]})
    if race == "identical":
        assert [response.status_code for response in responses] == [200, 200]
        assert sorted(response.json()["imported_count"] for response in responses) == [0, 4]
        assert len(stored["transactions"]) == 4 and stored["revision"] == 1
    elif race == "different":
        assert sorted(response.status_code for response in responses) == [200, 409]
        assert len(stored["transactions"]) in (1, 4) and stored["revision"] == 1
    else:
        assert responses[1].status_code == 201
        assert responses[0].status_code in (200, 409)
        assert len(stored["transactions"]) == (5 if responses[0].status_code == 200 else 1)
    ledger.replay(stored["transactions"])
