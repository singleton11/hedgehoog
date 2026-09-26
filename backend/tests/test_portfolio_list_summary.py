"""Page-scoped portfolio list metrics reuse customer summary accounting."""

from copy import deepcopy
from unittest.mock import Mock

from bson import ObjectId
import pytest

import portfolio_tracker as tracker_api
from test_market_data import ISIN, api, clock, config, db
from test_portfolio_tracker import (
    SECOND_ISIN, cash, customer, detail, goal, portfolio, quote, record, second_instrument, trade, tracker,
)

FIELDS = {"net_worth_eur", "realized_pnl_eur", "unrealized_pnl_eur", "today_pnl_eur", "total_pnl_eur",
          "today_from_date", "today_to_date"}


def summaries(client, cid, query=""):
    response = client.get(f"/customers/{cid}/portfolios{query}")
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    return response.json()


def test_list_shows_separate_linked_and_unlinked_totals_and_is_read_only(tracker, db):
    client, cid, first, url = tracker
    target = goal(client, cid)
    second, other = portfolio(client, cid, goal_id=target)
    record(client, url, cash())
    record(client, url, trade(quantity="4", price="80", commission_eur="4"))
    record(client, url, trade("sell", quantity="1", price="105", day="2026-09-16", commission_eur="1"))
    record(client, url, cash(amount="500", day="2026-09-16"))
    record(client, url, cash("withdrawal", amount="200", day="2026-09-16"))
    record(client, other, cash(amount="200"))
    record(client, other, trade(price="120", day="2026-09-16", commission_eur="2"))
    before = deepcopy(list(db.portfolios.find()))
    result = summaries(client, cid)
    assert (result["total"], result["limit"], result["offset"]) == (2, 20, 0)
    rows = {item["id"]: item for item in result["items"]}
    assert rows[first["id"]]["goal_id"] is None
    assert rows[second["id"]]["goal_id"] == target
    assert {key: rows[first["id"]][key] for key in FIELDS} == {
        "net_worth_eur": "1410.00", "realized_pnl_eur": "20.00", "unrealized_pnl_eur": "90.00",
        "today_pnl_eur": "34.00", "total_pnl_eur": "110.00",
        "today_from_date": "2026-09-15", "today_to_date": "2026-09-16",
    }
    assert {key: rows[second["id"]][key] for key in FIELDS} == {
        "net_worth_eur": "188.00", "realized_pnl_eur": "-2.00", "unrealized_pnl_eur": "-10.00",
        "today_pnl_eur": "-12.00", "total_pnl_eur": "-12.00",
        "today_from_date": "2026-09-15", "today_to_date": "2026-09-16",
    }
    assert list(db.portfolios.find()) == before
    assert not FIELDS.intersection(detail(client, url)["portfolio"])
    quote(db, "2026-09-17", "120")
    refreshed = {item["id"]: item for item in summaries(client, cid)["items"]}
    assert refreshed[first["id"]]["net_worth_eur"] == "1440.00"


@pytest.mark.parametrize("amount", [None, "123.45"])
def test_empty_and_cash_only_portfolios_have_zero_pnl_without_dates(tracker, amount):
    client, cid, _, url = tracker
    if amount:
        record(client, url, cash(amount=amount))
    result = summaries(client, cid)["items"][0]
    assert result["net_worth_eur"] == (amount or "0.00")
    assert all(result[key] == "0.00" for key in FIELDS if "pnl" in key)
    assert result["today_from_date"] is result["today_to_date"] is None


@pytest.mark.parametrize("failure", ["missing", "invalid", "currency", "prior", "newer-trade"])
def test_missing_marks_never_hide_realized_pnl_or_return_partial_values(tracker, db, failure):
    client, cid, _, url = tracker
    record(client, url, cash())
    record(client, url, trade(quantity="2", price="80"))
    record(client, url, trade("sell", price="90", day="2026-09-16"))
    if failure == "missing":
        db.quotes.delete_many({})
    elif failure == "invalid":
        db.quotes.update_many({"date": "2026-09-16"}, {"$set": {"data.close": None}})
    elif failure == "currency":
        db.quotes.update_many({}, {"$set": {"data.price_currency": "USD"}})
    elif failure == "prior":
        db.quotes.delete_many({"date": "2026-09-15"})
    else:
        record(client, url, trade(day="2026-09-17"))
    result = summaries(client, cid)["items"][0]
    assert result["realized_pnl_eur"] == "10.00"
    assert result["today_pnl_eur"] is result["today_from_date"] is result["today_to_date"] is None
    if failure in ("missing", "invalid", "currency"):
        assert result["net_worth_eur"] is result["unrealized_pnl_eur"] is result["total_pnl_eur"] is None
    else:
        assert all(result[key] is not None for key in ("net_worth_eur", "unrealized_pnl_eur", "total_pnl_eur"))
    customer_summary = client.get("/customers").json()["items"][0]
    assert {key: result[key] for key in FIELDS} == {key: customer_summary[key] for key in FIELDS}


@pytest.mark.parametrize("buy_day,expected_daily", [("2026-09-14", "8.00"), ("2026-09-16", "48.00")])
def test_daily_includes_full_sales_and_round_trips_not_only_remaining_holdings(tracker, buy_day, expected_daily):
    client, cid, _, url = tracker
    record(client, url, cash())
    record(client, url, trade(quantity="2", price="80", day=buy_day))
    record(client, url, trade("sell", quantity="2", price="105", day="2026-09-16", commission_eur="2"))
    result = summaries(client, cid)["items"][0]
    assert result["today_pnl_eur"] == expected_daily
    assert result["realized_pnl_eur"] == result["total_pnl_eur"] == "48.00"
    assert result["unrealized_pnl_eur"] == "0.00"
    assert detail(client, url)["daily_unrealized_pnl_eur"] == "0.00"


def test_total_pnl_uses_exact_components_before_rounding_and_preserves_large_values(tracker, db):
    client, cid, _, url = tracker
    quote(db, "2026-09-17", "0.51")
    record(client, url, cash())
    record(client, url, trade(quantity="2", price="0.505"))
    record(client, url, trade("sell", price="0.51", day="2026-09-17"))
    result = summaries(client, cid)["items"][0]
    assert result["realized_pnl_eur"] == result["unrealized_pnl_eur"] == result["total_pnl_eur"] == "0.01"
    assert result["net_worth_eur"] == "1000.01"
    db.quotes.update_many({"date": "2026-09-17"}, {"$set": {"data.close": "9007199254740993.01"}})
    result = summaries(client, cid)["items"][0]
    assert result["net_worth_eur"] == "9007199254741992.51"
    assert result["unrealized_pnl_eur"] == "9007199254740992.51"
    assert result["total_pnl_eur"] == "9007199254740992.51"


def test_portfolios_choose_independent_intervals_and_removed_trades_do_not_contribute(tracker, db):
    client, cid, first, url = tracker
    second_instrument(db, days=("2026-09-14", "2026-09-15"))
    second, other = portfolio(client, cid)
    for path, isin in ((url, ISIN), (other, SECOND_ISIN)):
        record(client, path, cash())
        record(client, path, trade(instrument_id=isin, day="2026-09-14", price="10"))
    removed = record(client, url, trade(day="2026-09-17"))
    assert client.delete(url + "/transactions/" + removed["id"]).status_code == 200
    rows = {item["id"]: item for item in summaries(client, cid)["items"]}
    assert rows[first["id"]]["today_to_date"] == "2026-09-16"
    assert rows[second["id"]]["today_to_date"] == "2026-09-15"
    assert rows[first["id"]]["today_pnl_eur"] == rows[second["id"]]["today_pnl_eur"] == "10.00"
    assert client.get("/customers").json()["items"][0]["today_pnl_eur"] is None


def test_valuation_is_scoped_to_the_owner_customer_and_page_with_shared_quotes(tracker, db, monkeypatch):
    client, cid, first, url = tracker
    record(client, url, cash())
    record(client, url, trade())
    for _ in range(21):
        _, path = portfolio(client, cid)
        record(client, path, cash())
        record(client, path, trade())
    other_customer = customer(client, "Off-page customer")
    portfolio(client, other_customer)
    foreign = db.portfolios.find_one({"_id": first["id"]})
    foreign.update({"_id": str(ObjectId()), "user_id": "another-owner"})
    db.portfolios.insert_one(foreign)
    fetch_quotes = Mock(wraps=tracker_api.quote_rows)
    value = Mock(wraps=tracker_api.valuation)
    find = Mock(wraps=db.portfolios.find)
    monkeypatch.setattr(tracker_api, "quote_rows", fetch_quotes)
    monkeypatch.setattr(tracker_api, "valuation", value)
    monkeypatch.setattr(db.portfolios, "find", find)
    result = summaries(client, cid, "?limit=20&offset=20")
    assert (result["total"], result["limit"], result["offset"]) == (22, 20, 20)
    assert len(result["items"]) == value.call_count == 2
    assert fetch_quotes.call_count == 1
    assert {call.args[1]["_id"] for call in value.call_args_list} == {item["id"] for item in result["items"]}
    assert all(item["net_worth_eur"] == "1010.00" for item in result["items"])
    owner = str(db.users.find_one({"login": "admin"})["_id"])
    assert find.call_args.args[0] == {"user_id": owner, "customer_id": cid}
    assert summaries(client, cid, "?offset=100")["items"] == []
    assert value.call_count == 2 and fetch_quotes.call_count == 1
