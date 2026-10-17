"""Historical daily TWR, saved index comparison, and owner-scoped read contracts."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction
from random import Random
from uuid import uuid4

from bson import ObjectId
import pytest

import market_data as md
from portfolio_performance import performance
from test_goal_planner import login
from test_market_data import ISIN, LISTING, NOW, api, clock, config, db
from test_portfolio_tracker import (SECOND_ISIN, SECOND_LISTING, cash, customer, portfolio,
                                    quote, record, second_instrument, trade, tracker)


def get(client, url, benchmark=None):
    response = client.get(url + "/performance", params={"benchmark_id": benchmark} if benchmark else {})
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    return response.json()


def points(result, field="twr_percent"):
    return {point["date"]: Decimal(point[field]) if point[field] is not None else None for point in result["points"]}


def index_history(db, rows, **changes):
    identifier = "idx-" + str(uuid4())
    record = {"_id": identifier, "instrument_id": identifier, "name": "Test index", "symbol": "^TEST",
              "currency": "USD", "return_variant": "price", **changes}
    db.indices.insert_one(record)
    for day, close in rows:
        quote(db, day, close, isin=identifier, listing={"symbol": record["symbol"], "exchange": "YAHOO"}, currency=record["currency"])
    return identifier


def invest(client, url, *, day="2026-09-15", amount="1000", quantity="10", price="100", fee="0"):
    record(client, url, cash(amount=amount, day=day))
    record(client, url, trade(quantity=quantity, price=price, commission_eur=fee, day=day))


def test_empty_and_cash_only_are_not_profit_and_refunding_preserves_chain(tracker):
    client, _, meta, url = tracker
    result = get(client, url)
    assert result == {"portfolio_id": meta["id"], "currency": "EUR", "method": "daily_twr",
                      "cash_flow_timing": "deposits_start_withdrawals_end", "as_of": "2026-09-17T12:00:00.000000Z",
                      "start_date": None, "end_date": None, "points": [], "benchmark": None,
                      "warnings": ["Sharpe and Sortino unavailable: select a saved EUR price-return proxy in Settings."],
                      "risk_proxy": None, "ratio_method": "since_inception_calendar_daily_log_excess_365",
                      "quote_fill_policy": {"version": 1, "method": "smooth", "seed": 0},
                      "summary": {"twr_percent": None, "sharpe": None, "sortino": None}}
    record(client, url, cash(amount="100.01", day="2026-09-01"))
    record(client, url, cash("withdrawal", amount="100.01", day="2026-09-02"))
    record(client, url, cash(amount="250.37", day="2026-09-05"))
    record(client, url, cash("withdrawal", amount="10.19", day="2026-09-05"))
    result = get(client, url)
    assert result["start_date"] == "2026-08-31" and result["end_date"] == "2026-09-17"
    assert set(points(result).values()) == {Decimal(0)}
    assert all(point["benchmark_percent"] is None for point in result["points"])


def test_deposits_and_withdrawals_are_removed_and_daily_factors_are_geometrically_linked(tracker, db):
    client, _, _, url = tracker
    quote(db, "2026-09-14", "100")
    quote(db, "2026-09-15", "110")
    quote(db, "2026-09-16", "121")
    invest(client, url, day="2026-09-14")
    record(client, url, cash(amount="1100", day="2026-09-16"))
    record(client, url, trade(quantity="10", price="110", day="2026-09-16"))
    record(client, url, trade("sell", quantity="2", price="121", day="2026-09-16"))
    record(client, url, cash("withdrawal", amount="200", day="2026-09-16"))
    result = get(client, url)
    assert points(result) == {"2026-09-13": Decimal(0), "2026-09-14": Decimal(0),
                              "2026-09-15": Decimal(10), "2026-09-16": Decimal(21), "2026-09-17": Decimal(21)}
    # Last close carries forward; no invented current-day price.
    assert result["points"][-1]["twr_percent"] == "21.00000000"


def test_positive_and_negative_returns_compound_instead_of_sum(tracker, db):
    client, _, _, url = tracker
    invest(client, url)
    quote(db, "2026-09-17", "99")
    values = points(get(client, url))
    assert values["2026-09-16"] == 10
    assert values["2026-09-17"] == -1  # +10% then -10%, not zero.


def test_first_day_fees_and_execution_prices_are_included_not_used_as_marks(tracker, db):
    client, _, _, url = tracker
    invest(client, url, quantity="9", fee="10")
    result = get(client, url)
    assert points(result)["2026-09-15"] == -1
    assert points(result)["2026-09-16"] == 8
    assert points(result)["2026-09-14"] == 0
    # Raw close, never adjusted close, and no rounding of quantities to cents.
    quote(db, "2026-09-17", "100.12345678", adj_close="99999")
    assert points(get(client, url))["2026-09-17"] == Decimal("-0.88888890")


def test_full_liquidation_withdrawal_dormancy_and_refunding(tracker, db):
    client, _, _, url = tracker
    quote(db, "2026-09-10", "100")
    invest(client, url, day="2026-09-10")
    record(client, url, trade("sell", quantity="10", price="110", day="2026-09-11", commission_eur="10"))
    record(client, url, cash("withdrawal", amount="1090", day="2026-09-11"))
    invest(client, url, day="2026-09-15", amount="500", quantity="5")
    values = points(get(client, url))
    assert values["2026-09-11"] == 9
    assert values["2026-09-15"] == 9
    assert values["2026-09-16"] == Decimal("19.9")


def test_total_loss_does_not_divide_by_zero_or_reset_on_refunding(tracker):
    client, _, _, url = tracker
    invest(client, url, day="2026-09-15", amount="1", quantity="0.01", price="1", fee="0.99")
    record(client, url, trade("sell", quantity="0.01", price="1", commission_eur="0.01", day="2026-09-15"))
    record(client, url, cash(amount="100", day="2026-09-16"))
    assert points(get(client, url))["2026-09-15"] == -100
    assert points(get(client, url))["2026-09-17"] == -100


def test_missing_history_is_not_backfilled_with_future_price_and_chain_does_not_restart(tracker, db):
    client, _, _, url = tracker
    invest(client, url, day="2026-09-14")  # First saved close is 15 September.
    result = get(client, url)
    assert points(result)["2026-09-13"] == 0
    assert all(value is None for day, value in points(result).items() if day >= "2026-09-14")
    assert any("TWR unavailable from 2026-09-14" in warning for warning in result["warnings"])
    # Correcting the historical gap repairs the derived series without writes to the ledger.
    quote(db, "2026-09-14", "100")
    assert points(get(client, url))["2026-09-17"] == 10


@pytest.mark.parametrize("bad", [None, "garbage", "0", "-1", "NaN", "Infinity", "1e99999", True])
def test_latest_invalid_observation_is_not_replaced_by_older_valid_or_adjusted_price(tracker, db, bad):
    client, _, _, url = tracker
    invest(client, url)
    quote(db, "2026-09-16", bad, adj_close="110", collected_at=NOW + timedelta(microseconds=-1))
    # This observation must be latest in collection time, even though inserted first.
    db.quotes.update_many({"date": "2026-09-16", "data.close": "110"}, {"$set": {"collected_at": NOW - timedelta(seconds=1)}})
    quote(db, "2026-09-17", "120")
    result = get(client, url)
    assert points(result)["2026-09-15"] == 0
    if bad in (None, "NaN"):
        assert points(result)["2026-09-16"] == 10
        assert points(result)["2026-09-17"] == 20
    else:
        assert points(result)["2026-09-16"] is None
        assert points(result)["2026-09-17"] is None


@pytest.mark.parametrize("failure", ["missing", "USD", "unknown"])
def test_no_partial_returns_when_one_of_multiple_positions_is_unpriced(tracker, db, failure):
    client, _, _, url = tracker
    second_instrument(db)
    invest(client, url, quantity="5")
    record(client, url, trade(instrument_id=SECOND_ISIN, quantity="5", price="20", day="2026-09-15"))
    if failure == "missing":
        db.quotes.delete_many({"instrument_id": SECOND_ISIN})
    else:
        db.quotes.update_many({"instrument_id": SECOND_ISIN}, {"$set": {"data.price_currency": "USD" if failure == "USD" else None}})
    result = get(client, url)
    assert points(result)["2026-09-15"] is None
    assert points(result)["2026-09-16"] is None
    assert any(SECOND_ISIN in warning for warning in result["warnings"])


@pytest.mark.parametrize("currency", [None, "USD"])
def test_later_currency_gaps_only_invalidate_dates_that_need_the_mark(tracker, db, currency):
    client, _, _, url = tracker
    invest(client, url)
    quote(db, "2026-09-17", "120", currency=currency)
    values = points(get(client, url))
    assert values["2026-09-15"] == 0
    assert values["2026-09-16"] == 10
    assert values["2026-09-17"] is None
    record(client, url, trade("sell", quantity="10", price="110", day="2026-09-16"))
    # No closing exposure on the bad-currency date; earlier valid history stays valid.
    assert points(get(client, url))["2026-09-17"] == 10


def test_currency_override_never_relabels_foreign_prices_or_another_listing(tracker, db):
    client, _, _, url = tracker
    invest(client, url)
    db.instrument_state.update_one({"_id": ISIN}, {"$set": {"price_currency_override": "EUR"}})
    quote(db, "2026-09-16", "110", currency="USD")
    values = points(get(client, url))
    assert values["2026-09-15"] == 0 and values["2026-09-16"] is None
    db.quotes.update_many({"date": "2026-09-16"}, {"$set": {"data.price_currency": None}})
    db.instrument_state.update_one({"_id": ISIN}, {"$set": {"listing": SECOND_LISTING}})
    assert points(get(client, url))["2026-09-16"] is None


def test_prior_close_carried_across_weekends_multi_asset_dates_and_manual_currency_confirmation(tracker, db):
    client, _, _, url = tracker
    second_instrument(db, days=("2026-09-11", "2026-09-16"))
    quote(db, "2026-09-11", "100")
    invest(client, url, day="2026-09-12", quantity="5")
    record(client, url, trade(instrument_id=SECOND_ISIN, quantity="5", price="20", day="2026-09-12"))
    db.quotes.update_many({"instrument_id": SECOND_ISIN}, {"$set": {"data.price_currency": None}})
    db.instrument_state.update_one({"_id": SECOND_ISIN}, {"$set": {"price_currency_override": "EUR"}})
    values = points(get(client, url))
    assert values["2026-09-12"] == 0
    assert values["2026-09-16"] == 10  # +50 from each holding; cash earns zero.


def test_same_day_round_trip_needs_no_closing_holdings_mark_and_fees_are_realized(tracker, db):
    client, _, _, url = tracker
    invest(client, url, day="2026-09-14", quantity="9", fee="1")
    record(client, url, trade("sell", quantity="9", price="110", commission_eur="2", day="2026-09-14"))
    db.quotes.delete_many({})
    assert points(get(client, url))["2026-09-14"] == Decimal("8.7")


def test_removed_backdated_and_imported_events_use_only_active_complete_history(tracker, db):
    client, _, meta, url = tracker
    invest(client, url)
    deposit = record(client, url, cash(amount="1000", day="2026-09-16"))
    assert points(get(client, url))["2026-09-16"] == 5
    assert client.delete(url + "/transactions/" + deposit["id"]).status_code == 200
    assert points(get(client, url))["2026-09-16"] == 10
    # An imported trade has identical semantics; source is not a performance rule.
    db.portfolios.update_one({"_id": meta["id"], "transactions.type": "buy"}, {"$set": {"transactions.$.source": "degiro"}})
    quote(db, "2026-09-10", "90")
    record(client, url, cash(amount="1000", day="2026-09-10"))
    values = points(get(client, url))
    assert values["2026-09-10"] == 0
    assert values["2026-09-16"] == 5


def test_benchmark_has_common_baseline_without_fx_and_no_benchmark_means_only_twr(tracker, db):
    client, _, _, url = tracker
    invest(client, url)
    benchmark = index_history(db, [("2026-09-14", "200"), ("2026-09-15", "220"), ("2026-09-16", "198")])
    before = {name: list(db[name].find()) for name in db.list_collection_names()}
    plain = get(client, url)
    result = get(client, url, benchmark)
    assert points(plain) == points(result)
    assert points(result, "benchmark_percent") == {"2026-09-14": Decimal(0), "2026-09-15": Decimal(10),
                                                    "2026-09-16": Decimal(-1), "2026-09-17": Decimal(-1)}
    assert result["benchmark"] == {"instrument_id": benchmark, "name": "Test index", "symbol": "^TEST",
                                    "currency": "USD", "return_variant": "price"}
    assert plain["benchmark"] is None and set(points(plain, "benchmark_percent").values()) == {None}
    assert result["warnings"] == plain["warnings"]
    assert {name: list(db[name].find()) for name in db.list_collection_names()} == before


def test_benchmark_uses_past_asof_close_not_future_baseline_and_can_have_unknown_metadata(tracker, db):
    client, _, _, url = tracker
    invest(client, url)
    benchmark = index_history(db, [("2026-09-10", "100"), ("2026-09-16", "120")], currency=None, return_variant="unknown")
    result = get(client, url, benchmark)
    assert points(result, "benchmark_percent")["2026-09-14"] == 0
    assert points(result, "benchmark_percent")["2026-09-15"] == 0
    assert points(result, "benchmark_percent")["2026-09-16"] == 20
    assert result["benchmark"]["currency"] is None
    assert result["benchmark"]["return_variant"] == "unknown"
    db.quotes.delete_many({"instrument_id": benchmark, "date": "2026-09-10"})
    result = get(client, url, benchmark)
    assert set(points(result, "benchmark_percent").values()) == {None}
    assert points(result)["2026-09-16"] == 10  # Unavailable benchmark never hides TWR.
    assert any("not silently rebased" in warning for warning in result["warnings"])


def test_missing_benchmark_date_is_smoothed_without_reusing_old_quote(tracker, db):
    client, _, _, url = tracker
    invest(client, url)
    benchmark = index_history(db, [("2026-09-14", "100"), ("2026-09-15", None), ("2026-09-16", "110")], currency="EUR")
    values = points(get(client, url, benchmark), "benchmark_percent")
    assert values["2026-09-15"] == 5
    assert values["2026-09-16"] == 10  # Normalization to a valid fixed baseline, not chaining an unknown return.


def test_benchmark_quote_dates_join_portfolio_dates_without_changing_portfolio_returns(tracker, db):
    client, _, _, url = tracker
    invest(client, url, day="2026-09-10", quantity="9")
    quote(db, "2026-09-10", "100")
    benchmark = index_history(db, [("2026-09-09", "100"), ("2026-09-12", "105")])
    plain = points(get(client, url))
    compared = points(get(client, url, benchmark))
    assert {day: compared[day] for day in plain} == plain
    assert compared["2026-09-12"] == 0


def test_read_bounds_dates_knowledge_time_and_pinned_listing_not_current_listing(tracker, db):
    client, _, _, url = tracker
    invest(client, url)
    quote(db, "2026-09-16", "99999", collected_at=NOW + timedelta(seconds=1))
    quote(db, "2026-09-18", "99999")
    quote(db, "2026-09-16", "99999", listing=SECOND_LISTING)
    db.instrument_state.update_one({"_id": ISIN}, {"$set": {"listing": SECOND_LISTING}})
    result = get(client, url)
    assert points(result)["2026-09-17"] == 10
    assert "2026-09-18" not in points(result)


def test_unpublished_generations_do_not_leak_and_refresh_does_not_mix_old_history(tracker, db):
    client, _, _, url = tracker
    invest(client, url)
    identifier = str(uuid4())
    job = {"_id": identifier, "instrument_id": ISIN, "provider": "yfinance", "listing": LISTING,
           "requested_at": NOW, "published_at": NOW, "published": False}
    db.acquisition_jobs.insert_one(job)
    for day, close in [("2026-09-15", "100"), ("2026-09-17", "120")]:
        quote(db, day, close)
        latest = db.quotes.find_one({"instrument_id": ISIN, "date": day}, sort=[("_id", -1)])
        db.quotes.update_one({"_id": latest["_id"]}, {"$set": {"provider": "yfinance", "job_id": identifier}})
    assert points(get(client, url))["2026-09-17"] == 10
    db.acquisition_jobs.update_one({"_id": identifier}, {"$set": {"published": True}})
    values = points(get(client, url))
    assert values["2026-09-17"] == 20
    assert "2026-09-16" not in values  # Not filled from the old generation.


def test_performance_endpoint_enforces_auth_owner_customer_and_index_identity(api, db):
    client, _ = api
    path = f"/customers/{ObjectId()}/portfolios/{ObjectId()}/performance"
    assert client.get(path).status_code == 401
    login(client)
    cid = customer(client)
    meta, url = portfolio(client, cid)
    other_cid = customer(client)
    assert client.get(f"/customers/{other_cid}/portfolios/{meta['id']}/performance").status_code == 404
    assert client.get(path).status_code == 404
    assert client.get(url + "/performance?benchmark_id=" + ISIN).status_code == 422
    assert client.get(url + "/performance?benchmark_id=idx-" + str(uuid4())).status_code == 404
    benchmark = index_history(db, [])
    db.indices.update_one({"instrument_id": benchmark}, {"$set": {"removed_at": NOW}})
    assert client.get(url + "/performance?benchmark_id=" + benchmark).status_code == 404
    db.portfolios.update_one({"_id": meta["id"]}, {"$set": {"user_id": "another-owner"}})
    assert client.get(url + "/performance").status_code == 404


def test_earliest_allowed_transaction_date_does_not_underflow_or_expand_calendar_days(tracker):
    client, _, _, url = tracker
    record(client, url, cash(amount="1", day="0001-01-01"))
    values = points(get(client, url))
    assert values == {"0001-01-01": Decimal(0), "2026-09-17": Decimal(0)}


def test_decimal_settlement_and_chaining_match_an_independent_fraction_oracle(db):
    rng = Random(731)
    instrument = {"instrument_id": ISIN, "name": "Test", **LISTING, "currency": "EUR"}
    for trial in range(12):
        db.quotes.delete_many({})
        events, expected = [], {}
        cash_balance, quantity, prior_value, growth = Fraction(0), Fraction(0), Fraction(0), Fraction(1)
        for offset in range(12):
            day = (NOW.date() - timedelta(days=11 - offset)).isoformat()
            close = Fraction(rng.randint(800, 1500), 100)
            quote(db, day, str(float(close)))
            inflow, outflow = Fraction(rng.randint(1500, 2500), 100), Fraction(rng.randint(0, 200), 100)
            units = Fraction(rng.randint(1, 10), 10)
            execution = Fraction(rng.randint(80000, 150000), 10000)
            fee = Fraction(rng.randint(0, 25), 100)
            # Positive half-up rounding to cents, independent of Decimal.
            gross_cents = (units * execution * 100 + Fraction(1, 2)).numerator // (units * execution * 100 + Fraction(1, 2)).denominator
            gross = Fraction(gross_cents, 100)
            for entry in [
                {"type": "deposit", "amount_eur": str(float(inflow))},
                {"type": "buy", "instrument": instrument, "quantity": str(float(units)),
                 "price_eur": str(float(execution)), "commission_eur": str(float(fee))},
                {"type": "withdrawal", "amount_eur": str(float(outflow))},
            ]:
                events.append({"date": day, "sequence": len(events) + 1, **entry})
            cash_balance += inflow - gross - fee - outflow
            quantity += units
            value = cash_balance + quantity * close
            growth *= (value + outflow) / (prior_value + inflow)
            prior_value = value
            expected[day] = (growth - 1) * 100
        result = points(performance(db, {"_id": f"oracle-{trial}", "transactions": deepcopy(events)}, now=NOW))
        for day, rational in expected.items():
            error = abs(Fraction(result[day]) - rational)
            assert error <= Fraction(1, 200000000), (trial, day, result[day], rational)
