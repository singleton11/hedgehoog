"""Customer-wide lifetime PnL and dated, cash-flow-neutral daily PnL."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

import portfolio_tracker as tracker_api
from test_market_data import ISIN, api, clock, config, db
from test_portfolio_tracker import (
    SECOND_ISIN, cash, customer, detail, goal, portfolio, quote, record, second_instrument, trade, tracker,
)


def summary(client):
    response = client.get("/customers")
    assert response.status_code == 200, response.text
    return response.json()["items"][0]


def test_all_portfolios_include_losses_sales_fees_and_ignore_external_flows(tracker, db):
    client, cid, _, url = tracker
    target = goal(client, cid)
    _, other = portfolio(client, cid, goal_id=target)
    record(client, url, cash())
    record(client, url, trade(quantity="4", price="80", commission_eur="4"))
    record(client, url, trade("sell", quantity="1", price="105", day="2026-09-16", commission_eur="1"))
    record(client, url, cash(amount="500", day="2026-09-16"))
    record(client, url, cash("withdrawal", amount="200", day="2026-09-16"))
    record(client, other, cash(amount="200"))
    record(client, other, trade(price="120", day="2026-09-16", commission_eur="2"))
    before = deepcopy(list(db.portfolios.find()))
    result = summary(client)
    assert result["realized_pnl_eur"] == "23.00"
    assert result["unrealized_pnl_eur"] == "75.00"
    assert result["total_pnl_eur"] == "98.00"
    # First portfolio: 3*110 - 4*100 + 104 = 34; second: 110 - 122 = -12.
    assert result["today_pnl_eur"] == "22.00"
    assert (result["today_from_date"], result["today_to_date"]) == ("2026-09-15", "2026-09-16")
    assert result["net_worth_eur"] == "1598.00"
    assert list(db.portfolios.find()) == before
    assert set(db.planner_customers.find_one({"_id": cid})) == {"_id", "user_id", "name", "created_at"}


@pytest.mark.parametrize("sell_price,realized,daily", [("105", "48.00", "8.00"), ("95", "28.00", "-12.00")])
def test_daily_pnl_includes_fully_liquidated_positions(tracker, sell_price, realized, daily):
    client, _, _, url = tracker
    record(client, url, cash())
    record(client, url, trade(quantity="2", price="80"))
    record(client, url, trade("sell", quantity="2", price=sell_price, day="2026-09-16", commission_eur="2"))
    result = summary(client)
    assert detail(client, url)["daily_unrealized_pnl_eur"] == "0.00"
    assert result["realized_pnl_eur"] == result["total_pnl_eur"] == realized
    assert result["unrealized_pnl_eur"] == "0.00"
    assert result["today_pnl_eur"] == daily
    assert result["today_to_date"] == "2026-09-16"


def test_intraday_round_trip_counts_fees_but_not_deposits(tracker):
    client, _, _, url = tracker
    record(client, url, cash(day="2026-09-16"))
    record(client, url, trade(quantity="2", price="100", day="2026-09-16", commission_eur="1"))
    record(client, url, trade("sell", quantity="2", price="108", day="2026-09-16", commission_eur="2"))
    record(client, url, cash("withdrawal", amount="1013", day="2026-09-16"))
    result = summary(client)
    assert result["today_pnl_eur"] == result["realized_pnl_eur"] == result["total_pnl_eur"] == "13.00"
    assert result["net_worth_eur"] == result["unrealized_pnl_eur"] == "0.00"


@pytest.mark.parametrize("failure", ["missing-prior", "invalid-prior", "invalid-latest", "currency", "gap", "weekend", "newer-trade"])
def test_incomplete_daily_prices_never_return_partial_totals(tracker, db, failure):
    client, cid, _, url = tracker
    record(client, url, cash())
    record(client, url, trade(quantity="2", price="80"))
    record(client, url, trade("sell", day="2026-09-16", price="90"))
    _, other = portfolio(client, cid)
    record(client, other, cash(amount="300"))
    if failure == "missing-prior":
        db.quotes.delete_many({"date": "2026-09-15"})
    elif failure == "invalid-prior":
        db.quotes.update_many({"date": "2026-09-15"}, {"$set": {"data.close": "bad"}})
    elif failure == "invalid-latest":
        db.quotes.update_many({"date": "2026-09-16"}, {"$set": {"data.close": None}})
    elif failure == "currency":
        db.quotes.update_many({}, {"$set": {"data.price_currency": "USD"}})
    elif failure in ("gap", "weekend"):
        db.quotes.delete_many({})
        quote(db, "2026-09-14" if failure == "gap" else "2026-09-13", "100")
        quote(db, "2026-09-16", "110")
    else:
        record(client, url, trade(day="2026-09-17"))
    result = summary(client)
    assert result["today_pnl_eur"] is result["today_from_date"] is result["today_to_date"] is None
    assert result["realized_pnl_eur"] == "10.00"
    if failure not in ("invalid-latest", "currency"):
        assert result["unrealized_pnl_eur"] is not None
        assert result["total_pnl_eur"] is not None


def test_missing_marks_do_not_hide_other_portfolios_realized_pnl(tracker, db):
    client, cid, _, url = tracker
    record(client, url, cash())
    record(client, url, trade())
    second_instrument(db)
    _, other = portfolio(client, cid)
    record(client, other, cash())
    record(client, other, trade(instrument_id=SECOND_ISIN, price="10"))
    record(client, other, trade("sell", instrument_id=SECOND_ISIN, price="25", day="2026-09-16", commission_eur="1"))
    db.quotes.delete_many({"instrument_id": ISIN})
    result = summary(client)
    assert result["realized_pnl_eur"] == "14.00"
    assert all(result[field] is None for field in ("net_worth_eur", "unrealized_pnl_eur", "total_pnl_eur", "today_pnl_eur"))


def test_cross_portfolio_mixed_intervals_are_unavailable_then_refresh_together(tracker, db):
    client, cid, _, url = tracker
    second_instrument(db, days=("2026-09-14", "2026-09-15"))
    _, other = portfolio(client, cid)
    for path, isin in ((url, ISIN), (other, SECOND_ISIN)):
        record(client, path, cash())
        record(client, path, trade(instrument_id=isin, day="2026-09-14", price="10"))
    result = summary(client)
    assert result["today_pnl_eur"] is None
    assert result["total_pnl_eur"] == "120.00"
    quote(db, "2026-09-16", "40", isin=SECOND_ISIN, listing={"symbol": "EUNL.F", "exchange": "XFRA"})
    assert summary(client)["today_pnl_eur"] == "20.00"


def test_old_liquidations_do_not_require_prices_in_current_interval(tracker, db):
    client, cid, _, url = tracker
    second_instrument(db, days=("2026-09-10", "2026-09-11"))
    _, old = portfolio(client, cid)
    record(client, old, cash())
    record(client, old, trade(instrument_id=SECOND_ISIN, day="2026-09-10", price="10"))
    record(client, old, trade("sell", instrument_id=SECOND_ISIN, day="2026-09-14", price="20"))
    db.quotes.update_many({"instrument_id": SECOND_ISIN}, {"$set": {"data.close": None, "data.price_currency": "USD"}})
    record(client, url, cash())
    record(client, url, trade())
    result = summary(client)
    assert result["today_pnl_eur"] == "10.00"
    assert result["realized_pnl_eur"] == "10.00"
    assert result["total_pnl_eur"] == "20.00"


def test_fresh_quotes_for_old_liquidations_do_not_choose_the_daily_interval(tracker, db):
    client, cid, _, url = tracker
    second_instrument(db, days=("2026-09-16", "2026-09-17"))
    _, old = portfolio(client, cid)
    record(client, old, cash())
    record(client, old, trade(instrument_id=SECOND_ISIN, day="2026-09-01", price="10"))
    record(client, old, trade("sell", instrument_id=SECOND_ISIN, day="2026-09-02", price="20"))
    record(client, url, cash())
    record(client, url, trade())
    result = summary(client)
    assert result["today_pnl_eur"] == "10.00"
    assert (result["today_from_date"], result["today_to_date"]) == ("2026-09-15", "2026-09-16")


def test_pnl_rounds_only_after_aggregation_without_double_counting_half_cents(tracker, db):
    client, cid, _, url = tracker
    quote(db, "2026-09-17", "0.51")
    record(client, url, cash())
    record(client, url, trade(quantity="2", price="0.505"))
    record(client, url, trade("sell", quantity="1", price="0.51", day="2026-09-17"))
    result = summary(client)
    assert result["realized_pnl_eur"] == result["unrealized_pnl_eur"] == "0.01"
    assert result["total_pnl_eur"] == "0.01"  # 0.005 + 0.005, not rounded components.
    assert result["net_worth_eur"] == "1000.01"
    _, other = portfolio(client, cid)
    record(client, other, cash())
    record(client, other, trade(quantity="2", price="0.505"))
    record(client, other, trade("sell", quantity="1", price="0.51", day="2026-09-17"))
    result = summary(client)
    assert result["realized_pnl_eur"] == result["unrealized_pnl_eur"] == "0.01"
    assert result["total_pnl_eur"] == "0.02"


def test_friday_to_monday_is_one_trading_interval(tracker, db):
    client, _, _, url = tracker
    db.quotes.delete_many({})
    quote(db, "2026-09-11", "100")
    quote(db, "2026-09-14", "110")
    record(client, url, cash())
    record(client, url, trade(day="2026-09-10"))
    result = summary(client)
    assert result["today_pnl_eur"] == "10.00"
    assert (result["today_from_date"], result["today_to_date"]) == ("2026-09-11", "2026-09-14")


def test_zero_goals_and_portfolios_and_more_than_a_page_of_each(tracker, db, monkeypatch):
    client, cid, _, url = tracker
    empty = customer(client, "Empty")
    result = summary(client)
    assert result["id"] == empty and result["goal_count"] == 0
    assert all(result[field] == "0.00" for field in ("net_worth_eur", "realized_pnl_eur", "unrealized_pnl_eur", "today_pnl_eur", "total_pnl_eur"))
    assert result["today_from_date"] is result["today_to_date"] is None
    for _ in range(21):
        goal(client, cid)
        _, path = portfolio(client, cid)
        record(client, path, cash())
        record(client, path, trade())
    record(client, url, cash())
    record(client, url, trade())
    fetch_quotes = Mock(wraps=tracker_api.quote_rows)
    monkeypatch.setattr(tracker_api, "quote_rows", fetch_quotes)
    result = {item["id"]: item for item in client.get("/customers").json()["items"]}[cid]
    assert result["goal_count"] == 21
    assert result["today_pnl_eur"] == result["total_pnl_eur"] == "220.00"
    assert fetch_quotes.call_count == 1  # Same saved listing reused within this page read.
