"""Both summary tables mark current ledgers with the latest saved close."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock

import pytest

import market_data as md
import portfolio_tracker as tracker_api
from test_market_data import ISIN, NOW, api, clock, config, db
from test_portfolio_tracker import cash, portfolio, quote, record, trade, tracker


def table_summaries(client, cid, pid):
    customers = client.get("/customers")
    portfolios = client.get(f"/customers/{cid}/portfolios")
    assert customers.status_code == portfolios.status_code == 200
    return [next(row for row in customers.json()["items"] if row["id"] == cid),
            next(row for row in portfolios.json()["items"] if row["id"] == pid)]


@pytest.mark.parametrize("stale_days", [0, 9])
@pytest.mark.parametrize("held,transactions,expected", [
    (True, [("buy", "1", "120", "3")], "7.00"),
    (True, [("sell", "1", "105", "2")], "13.00"),
    (True, [("sell", "2", "105", "2")], "8.00"),
    (True, [("sell", "2", "105", "2"), ("buy", "1", "120", "3")], "-5.00"),
    (False, [("buy", "2", "120", "3")], "-23.00"),
    (False, [("buy", "2", "100", "1"), ("sell", "2", "108", "2")], "13.00"),
    (False, [("buy", "2", "110", "0")], "0.00"),
])
def test_tables_use_latest_saved_close_for_newer_trades(
        tracker, db, api, clock, monkeypatch, stale_days, held, transactions, expected):
    client, cid, metadata, url = tracker
    clock[0] = NOW + timedelta(days=stale_days)
    day = clock[0].date().isoformat()
    record(client, url, cash())
    if held:
        record(client, url, trade(quantity="2", price="80", commission_eur="4"))
    for kind, quantity, unit_price, fee in transactions:
        record(client, url, trade(kind, quantity=quantity, price=unit_price, day=day, commission_eur=fee))
    # External funding after the saved close is never profit or loss.
    record(client, url, cash(amount="500", day=day))
    record(client, url, cash("withdrawal", amount="200", day=day))
    before = deepcopy(list(db.portfolios.find()))
    saved_quotes = deepcopy(list(db.quotes.find()))
    provider = Mock(side_effect=AssertionError("Summary reads must not fetch prices"))
    monkeypatch.setattr(md, "provider_client", provider)
    for result in table_summaries(client, cid, metadata["id"]):
        assert result["today_pnl_eur"] == expected
        assert (result["today_from_date"], result["today_to_date"]) == ("2026-09-15", "2026-09-16")
    assert list(db.portfolios.find()) == before
    assert list(db.quotes.find()) == saved_quotes
    assert db.marketstack_usage.count_documents({}) == 0
    api[1].assert_not_called()
    provider.assert_not_called()


def test_tables_refresh_when_a_new_close_arrives_without_rewriting_trades(tracker, db):
    client, cid, metadata, url = tracker
    record(client, url, cash())
    record(client, url, trade(quantity="2", price="80"))
    record(client, url, trade(price="120", day="2026-09-17", commission_eur="3"))
    before = deepcopy(list(db.portfolios.find()))
    for result in table_summaries(client, cid, metadata["id"]):
        assert result["today_pnl_eur"] == "7.00"
    quote(db, "2026-09-17", "130")
    for result in table_summaries(client, cid, metadata["id"]):
        assert result["today_pnl_eur"] == "47.00"  # 3*130 - 2*110 - 120 - 3
        assert (result["today_from_date"], result["today_to_date"]) == ("2026-09-16", "2026-09-17")
    assert list(db.portfolios.find()) == before


def test_customer_aggregates_all_newer_trades_and_portfolios_with_one_quote_cache(tracker, db, clock, monkeypatch):
    client, cid, metadata, url = tracker
    clock[0] = NOW + timedelta(days=2)
    _, other = portfolio(client, cid)
    record(client, url, cash())
    record(client, url, trade(quantity="2", price="80"))
    record(client, url, trade(price="120", day="2026-09-17", commission_eur="3"))
    record(client, url, trade("sell", price="115", day="2026-09-18", commission_eur="1"))
    record(client, other, cash())
    record(client, other, trade(quantity="2", price="100", day="2026-09-18", commission_eur="1"))
    record(client, other, trade("sell", quantity="2", price="108", day="2026-09-19", commission_eur="2"))
    fetch_quotes = Mock(wraps=tracker_api.quote_rows)
    monkeypatch.setattr(tracker_api, "quote_rows", fetch_quotes)
    customer, holding = table_summaries(client, cid, metadata["id"])
    assert holding["today_pnl_eur"] == "11.00"  # 2*110 - 2*100 - 123 + 114
    assert customer["today_pnl_eur"] == "24.00"  # Includes other portfolio's 13 EUR round trip.
    assert fetch_quotes.call_count == 2  # Once per table request, not per portfolio.


@pytest.mark.parametrize("failure", ["prior", "invalid", "currency"])
def test_newer_trades_still_require_valid_saved_marks(tracker, db, failure):
    client, cid, metadata, url = tracker
    record(client, url, cash())
    record(client, url, trade(day="2026-09-17"))
    if failure == "prior":
        db.quotes.delete_many({"date": "2026-09-15"})
    else:
        field, value = ("data.close", None) if failure == "invalid" else ("data.price_currency", "USD")
        db.quotes.update_many({"instrument_id": ISIN, "date": "2026-09-16"}, {"$set": {field: value}})
    for result in table_summaries(client, cid, metadata["id"]):
        assert result["today_pnl_eur"] is result["today_from_date"] is result["today_to_date"] is None
