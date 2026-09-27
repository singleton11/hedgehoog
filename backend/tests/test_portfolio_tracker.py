"""API contracts and exact accounting for the funded EUR ledger."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Lock
from unittest.mock import Mock
from uuid import uuid4

from bson import Decimal128, ObjectId
from pymongo.results import UpdateResult
import pytest

import market_data as md
import portfolio_tracker as tracker_api
from test_goal_planner import login, seed_history, seed_profile
from test_market_data import ISIN, LISTING, NOW, api, bar, clock, config, db

SECOND_ISIN = "IE00B4L5Y983"
SECOND_LISTING = {"symbol": "EUNL.F", "exchange": "XFRA"}


def cash(kind="deposit", amount="1000", day="2026-09-01", **changes):
    return {"type": kind, "amount_eur": amount, "date": day, "client_id": str(uuid4()), **changes}


def trade(kind="buy", quantity="1", price="100", day="2026-09-14", **changes):
    return {"type": kind, "instrument_id": ISIN, "quantity": quantity, "price_eur": price,
            "date": day, "client_id": str(uuid4()), **changes}


def record(client, url, body):
    response = client.post(url + "/transactions", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def customer(client, name="Customer"):
    response = client.post("/customers", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def goal(client, customer_id):
    response = client.post(f"/customers/{customer_id}/goals", json={
        "name": "Retirement", "goal_eur": "100000", "goal_date": "2036-09-17",
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def plan(client, db, customer_id):
    goal_id = goal(client, customer_id)
    target = db.planner_goals.find_one({"_id": goal_id})
    plan_id = str(ObjectId())
    db.planner_simulations.insert_one({"_id": plan_id, "user_id": target["user_id"],
                                      "customer_id": customer_id, "goal_id": goal_id,
                                      "saved_at": "2026-09-17T12:00:00Z", "is_plan": True})
    return plan_id


def portfolio(client, customer_id, **changes):
    response = client.post(f"/customers/{customer_id}/portfolios", json={"name": "Investments", **changes})
    assert response.status_code == 201, response.text
    result = response.json()
    return result, f"/customers/{customer_id}/portfolios/{result['id']}"


def detail(client, url):
    response = client.get(url)
    assert response.status_code == 200, response.text
    return response.json()


def quote(db, day, close, *, isin=ISIN, listing=None, collected_at=NOW, currency="EUR", **data):
    listing = listing or LISTING
    db.quotes.insert_one({
        "instrument_id": isin, **listing, "date": day, "job_id": str(uuid4()),
        "collected_at": collected_at, "collection_date": collected_at.date().isoformat(),
        "data": {**bar(day, close), **listing, "price_currency": currency, **data},
    })


def second_instrument(db, days=("2026-09-15", "2026-09-16")):
    db.instrument_state.insert_one({"_id": SECOND_ISIN, "listing": SECOND_LISTING})
    db.instruments.insert_one({"instrument_id": SECOND_ISIN, **SECOND_LISTING,
                               "collected_at": NOW, "job_id": "second-profile", "data": {}})
    for day, close in zip(days, ("20", "30")):
        quote(db, day, close, isin=SECOND_ISIN, listing=SECOND_LISTING)


@pytest.fixture
def tracker(api, db):
    client, _ = api
    login(client)
    seed_history(db, [{**bar(day, close), "price_currency": "EUR"}
                      for day, close in [("2026-09-15", "100"), ("2026-09-16", "110")]])
    seed_profile(db)
    customer_id = customer(client)
    metadata, url = portfolio(client, customer_id)
    return client, customer_id, metadata, url


def test_all_portfolio_routes_require_authentication(api):
    client, _ = api
    base = f"/customers/{ObjectId()}/portfolios"
    url = base + f"/{ObjectId()}"
    for endpoint in ("/portfolio-instruments", base, url, url + "/transactions"):
        response = client.get(endpoint)
        assert response.status_code == 401
        assert response.headers["Cache-Control"] == "no-store"
    assert client.post(base, json={"name": "Portfolio"}).status_code == 401
    assert client.patch(url, json={"name": "Renamed"}).status_code == 401
    assert client.delete(url + f"/transactions/{ObjectId()}").status_code == 401
    for body in (cash(), trade()):
        assert client.post(url + "/transactions", json=body).status_code == 401


@pytest.mark.parametrize("headers", [{"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}])
def test_portfolio_mutations_preserve_origin_protection(tracker, db, headers):
    client, customer_id, metadata, url = tracker
    stored = db.portfolios.find_one({"_id": metadata["id"]})
    assert client.post(f"/customers/{customer_id}/portfolios", json={"name": "Bad"}, headers=headers).status_code == 403
    assert client.patch(url, json={"name": "Bad"}, headers=headers).status_code == 403
    assert client.post(url + "/transactions", json=cash(), headers=headers).status_code == 403
    assert client.delete(url + f"/transactions/{ObjectId()}", headers=headers).status_code == 403
    assert db.portfolios.count_documents({}) == 1
    assert db.portfolios.find_one({"_id": metadata["id"]}) == stored


def test_named_multiple_portfolios_attach_detach_and_independent_balances(tracker, db):
    client, customer_id, first, url = tracker
    assert first == {"id": first["id"], "customer_id": customer_id, "name": "Investments", "plan_id": None,
                     "currency": "EUR", "created_at": "2026-09-17T12:00:00.000000Z", "revision": 0}
    target = plan(client, db, customer_id)
    second, other_url = portfolio(client, customer_id, name="  Zoë's house \n", plan_id=target)
    assert second["name"] == "Zoë's house" and second["plan_id"] == target
    assert second["id"] != first["id"]
    record(client, url, cash(amount="12.34"))
    for plan_id, name in [(target, "Attached"), (None, "Detached")]:
        response = client.patch(url, json={"name": name, "plan_id": plan_id})
        assert response.status_code == 200, response.text
        assert response.json()["plan_id"] == plan_id
        assert response.json()["name"] == name
        assert response.json()["created_at"] == first["created_at"]
        assert detail(client, url)["cash_eur"] == "12.34"
    assert detail(client, other_url)["cash_eur"] == "0.00"
    assert detail(client, other_url)["portfolio"] == second
    assert db.portfolios.find_one({"_id": first["id"]})["revision"] == 3
    assert db.portfolios.index_information()["portfolios_owner_customer_order"]["key"] == [
        ("user_id", 1), ("customer_id", 1), ("created_at", -1), ("_id", -1),
    ]
    listed = client.get(f"/customers/{customer_id}/portfolios").json()["items"][0]
    assert {key: listed[key] for key in second} == second
    assert set(listed) == set(first) | {"net_worth_eur", "realized_pnl_eur", "unrealized_pnl_eur", "today_pnl_eur",
                                        "total_pnl_eur", "today_from_date", "today_to_date"}


def test_customer_net_worth_sums_cash_and_holdings_across_linked_and_unlinked_portfolios(tracker, db):
    client, customer_id, _, url = tracker
    target = plan(client, db, customer_id)
    _, other_url = portfolio(client, customer_id, plan_id=target)
    record(client, url, cash(amount="1000"))
    record(client, url, trade(quantity="2", price="100", commission_eur="1.23"))
    record(client, url, cash("withdrawal", amount="50", day="2026-09-16"))
    record(client, other_url, cash(amount="250.45"))
    before = list(db.portfolios.find())
    response = client.get("/customers")
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    summary = response.json()["items"][0]
    # 1000 - 201.23 - 50 cash + 220 saved holdings + 250.45 other cash.
    assert summary["net_worth_eur"] == "1219.22"
    assert Decimal(summary["net_worth_eur"]) == sum(Decimal(detail(client, path)["equity_eur"]) for path in (url, other_url))
    assert {key: summary[key] for key in ("goal_count", "realized_pnl_eur", "unrealized_pnl_eur", "today_pnl_eur", "total_pnl_eur",
                                        "today_from_date", "today_to_date")} == {
        "goal_count": 1, "realized_pnl_eur": "-1.23", "unrealized_pnl_eur": "20.00", "today_pnl_eur": "20.00",
        "total_pnl_eur": "18.77", "today_from_date": "2026-09-15", "today_to_date": "2026-09-16",
    }
    assert list(db.portfolios.find()) == before
    assert "net_worth_eur" not in db.planner_customers.find_one({"_id": customer_id})
    # A refreshed list reflects saved market data without any provider request.
    quote(db, "2026-09-17", "120")
    assert client.get("/customers").json()["items"][0]["net_worth_eur"] == "1239.22"


def test_customer_net_worth_is_exact_and_includes_more_than_one_portfolio_page(tracker, db):
    client, customer_id, _, url = tracker
    record(client, url, cash())
    record(client, url, trade())
    quote(db, "2026-09-17", "9007199254740993.01")
    for _ in range(21):
        _, other_url = portfolio(client, customer_id)
        record(client, other_url, cash(amount="0.01"))
    summary = client.get("/customers").json()["items"][0]
    assert summary["net_worth_eur"] == "9007199254741893.22"
    assert summary["unrealized_pnl_eur"] == summary["total_pnl_eur"] == "9007199254740893.01"
    assert summary["today_pnl_eur"] == "9007199254740883.01"


@pytest.mark.parametrize("failure", ["missing", "invalid", "currency"])
def test_customer_net_worth_is_unavailable_rather_than_partial_when_any_holding_is_unpriced(tracker, db, failure):
    client, customer_id, _, url = tracker
    record(client, url, cash())
    record(client, url, trade())
    _, other_url = portfolio(client, customer_id)
    record(client, other_url, cash(amount="250"))
    if failure == "missing":
        db.quotes.delete_many({})
    elif failure == "invalid":
        quote(db, "2026-09-17", "not-a-price")
    else:
        quote(db, "2026-09-17", "110", currency="USD")
    summary = client.get("/customers").json()["items"][0]
    assert all(summary[key] is None for key in ("net_worth_eur", "unrealized_pnl_eur", "today_pnl_eur", "total_pnl_eur"))
    assert summary["realized_pnl_eur"] == "0.00"
    # Once liquidated, there is no missing holding value to conceal the cash.
    record(client, url, trade("sell", day="2026-09-17"))
    assert client.get("/customers").json()["items"][0]["net_worth_eur"] == "1250.00"


def test_customer_net_worth_distinguishes_empty_and_cash_only_customers(tracker):
    client, customer_id, _, url = tracker
    empty_id = customer(client, "No portfolios")
    goal(client, empty_id)  # A goal target is not an asset.
    summaries = {item["id"]: item["net_worth_eur"] for item in client.get("/customers").json()["items"]}
    assert summaries == {customer_id: "0.00", empty_id: "0.00"}
    record(client, url, cash(amount="0.10"))
    record(client, url, cash(amount="0.20"))
    summaries = {item["id"]: item["net_worth_eur"] for item in client.get("/customers").json()["items"]}
    assert summaries == {customer_id: "0.30", empty_id: "0.00"}


def test_customer_net_worth_reads_only_the_current_owners_customer_page(tracker, db, monkeypatch):
    client, customer_id, first, url = tracker
    record(client, url, cash(amount="12.34"))
    other_customer = customer(client, "Off-page customer")
    _, other_url = portfolio(client, other_customer)
    record(client, other_url, cash(amount="999.99"))
    goal(client, customer_id)
    goal(client, other_customer)
    foreign_goal = db.planner_goals.find_one({"customer_id": customer_id})
    foreign_goal.update({"_id": str(ObjectId()), "user_id": "another-owner"})
    db.planner_goals.insert_one(foreign_goal)
    goal_counts = Mock(wraps=db.planner_goals.aggregate)
    monkeypatch.setattr(db.planner_goals, "aggregate", goal_counts)
    foreign = db.portfolios.find_one({"_id": first["id"]})
    foreign.update({"_id": str(ObjectId()), "user_id": "another-owner"})
    db.portfolios.insert_one(foreign)
    find = Mock(wraps=db.portfolios.find)
    value = Mock(wraps=tracker_api.valuation)
    monkeypatch.setattr(db.portfolios, "find", find)
    monkeypatch.setattr(tracker_api, "valuation", value)
    response = client.get("/customers?limit=1&offset=1")
    assert response.status_code == 200, response.text
    page = response.json()
    assert (page["total"], page["limit"], page["offset"]) == (2, 1, 1)
    assert [(item["id"], item["net_worth_eur"]) for item in page["items"]] == [(customer_id, "12.34")]
    assert find.call_count == value.call_count == 1
    owner = str(db.users.find_one({"login": "admin"})["_id"])
    assert find.call_args.args[0] == {"user_id": owner, "customer_id": {"$in": [customer_id]}}
    assert value.call_args.args[1]["_id"] == first["id"]
    assert page["items"][0]["goal_count"] == 1
    assert goal_counts.call_args.args[0][0] == {"$match": {"user_id": owner, "customer_id": {"$in": [customer_id]}}}
    assert client.get("/customers?offset=100").json()["items"] == []
    assert find.call_count == value.call_count == goal_counts.call_count == 1


def test_owner_customer_and_goal_scoping_cannot_be_crossed(tracker, db):
    client, customer_id, first, url = tracker
    event = record(client, url, cash())
    own_goal = plan(client, db, customer_id)
    other_customer = customer(client, "Second customer, same owner")
    other_goal = plan(client, db, other_customer)
    other, _ = portfolio(client, other_customer)
    admin = db.users.find_one({"login": "admin"})
    db.users.insert_one({"login": "reader", "password_hash": admin["password_hash"], "role": "user"})
    assert client.post("/auth/logout").status_code == 204
    assert client.post("/auth/login", json={"login": "reader", "password": "admin"}).status_code == 200
    foreign_customer = customer(client, "Another owner")
    foreign_goal = plan(client, db, foreign_customer)
    foreign, foreign_url = portfolio(client, foreign_customer)
    # Admin is not an ownership bypass, nor can another user read admin's data.
    for endpoint in (f"/customers/{customer_id}/portfolios", url, url + "/transactions"):
        assert client.get(endpoint).status_code == 404
    assert client.post(f"/customers/{customer_id}/portfolios", json={"name": "Bad"}).status_code == 404
    assert client.patch(url, json={"name": "Bad"}).status_code == 404
    assert client.post(url + "/transactions", json=cash()).status_code == 404
    assert client.delete(url + "/transactions/" + event["id"]).status_code == 404
    assert client.post("/auth/logout").status_code == 204
    login(client)
    before = list(db.portfolios.find())
    for endpoint in (foreign_url, f"/customers/{customer_id}/portfolios/{other['id']}",
                     f"/customers/{other_customer}/portfolios/{first['id']}",
                     f"/customers/{customer_id}/portfolios/{foreign['id']}", url + "-missing"):
        assert client.get(endpoint).status_code == 404
        assert client.get(endpoint + "/transactions").status_code == 404
        assert client.patch(endpoint, json={"name": "Bad", "plan_id": own_goal}).status_code == 404
        assert client.post(endpoint + "/transactions", json=cash()).status_code == 404
        assert client.delete(endpoint + "/transactions/" + event["id"]).status_code == 404
    for invalid_goal in (other_goal, foreign_goal, str(ObjectId())):
        body = {"name": "Bad attachment", "plan_id": invalid_goal}
        assert client.post(f"/customers/{customer_id}/portfolios", json=body).status_code == 404
        assert client.patch(url, json=body).status_code == 404
    assert client.get(f"/customers/{foreign_customer}/portfolios").status_code == 404
    assert list(db.portfolios.find()) == before
    assert client.get(f"/customers/{customer_id}/portfolios").json()["total"] == 1


@pytest.mark.parametrize("change", [
    {"name": value} for value in (None, True, 12, [], "", " \n", "x" * 201)
] + [{"plan_id": ""}, {"currency": "USD"}, {"user_id": "foreign"}, {"transactions": []}, {"revision": 99}])
def test_portfolio_metadata_rejects_invalid_or_server_owned_fields(tracker, db, change):
    client, customer_id, metadata, url = tracker
    before = db.portfolios.find_one({"_id": metadata["id"]})
    body = {"name": "Portfolio", **change}
    assert client.post(f"/customers/{customer_id}/portfolios", json=body).status_code == 422
    assert client.patch(url, json=body).status_code == 422
    assert db.portfolios.count_documents({}) == 1
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before


@pytest.mark.parametrize("inventory", ["catalogue-only", "listing-only", "quote-only", "wrong-profile-listing"])
def test_buys_and_instrument_picker_require_acquired_selected_listing(api, db, inventory):
    client, _ = api
    login(client)
    if inventory == "listing-only":
        db.instrument_state.insert_one({"_id": ISIN, "listing": LISTING, "price_currency_override": "EUR"})
    elif inventory in ("quote-only", "wrong-profile-listing"):
        seed_history(db, [{**bar(), "price_currency": "EUR"}])
        if inventory == "wrong-profile-listing":
            seed_profile(db)
            db.instruments.update_many({}, {"$set": {"symbol": "OTHER"}})
    _, url = portfolio(client, customer(client))
    record(client, url, cash())
    assert client.get("/portfolio-instruments").json()["items"] == []
    response = client.post(url + "/transactions", json=trade())
    assert response.status_code == 422
    assert "Acquire" in response.json()["detail"]
    assert detail(client, url)["cash_eur"] == "1000.00"
    assert client.get(url + "/transactions").json()["total"] == 1


@pytest.mark.parametrize("currencies,override,eligible", [
    (["EUR", "EUR"], None, True), ([" eur ", "EUR"], None, True),
    ([None, None], None, False), ([None, None], "EUR", True),
    (["USD", "USD"], "EUR", False), (["EUR", "USD"], "EUR", False),
    (["EUR", None], None, False), (["EUR", None], "EUR", True),
    (["EUR", "EUR"], "USD", False),
])
def test_currency_confirmation_never_relabels_explicit_usd_or_uses_fund_currency(tracker, db, currencies, override, eligible):
    client, _, _, url = tracker
    for day, currency in zip(("2026-09-15", "2026-09-16"), currencies):
        db.quotes.update_one({"date": day}, {"$set": {"data.price_currency": currency}})
    if override:
        md.set_price_currency(db, ISIN, override)
    record(client, url, cash())
    # The catalogue's fund base currency is USD; only the acquired listing counts.
    assert md.latest_catalogue(db, ISIN)["currency"] == "USD"
    assert client.get("/portfolio-instruments").json()["total"] == int(eligible)
    response = client.post(url + "/transactions", json=trade())
    assert response.status_code == (201 if eligible else 422), response.text
    assert client.get(url + "/transactions").json()["total"] == (2 if eligible else 1)


def test_listing_is_pinned_for_valuation_and_sells_even_after_universe_changes(tracker, db):
    client, _, _, url = tracker
    record(client, url, cash())
    original = record(client, url, trade(quantity="2"))
    new_listing = {"symbol": "NEW.DE", "exchange": "XFRA"}
    db.instrument_state.update_one({"_id": ISIN}, {"$set": {"listing": new_listing, "price_currency_override": "EUR"}})
    db.instruments.insert_one({"instrument_id": ISIN, **new_listing, "collected_at": NOW, "job_id": "new"})
    quote(db, "2026-09-17", "999", listing=new_listing)
    assert client.get("/portfolio-instruments").json()["items"][0]["symbol"] == "NEW.DE"
    rejected = client.post(url + "/transactions", json=trade())
    assert rejected.status_code == 422
    assert "different listing" in rejected.json()["detail"]
    position = detail(client, url)["positions"][0]
    assert position["market_price_eur"] == "110" and position["position_value_eur"] == "220.00"
    assert position["instrument"] == original["instrument"]
    db.instruments.delete_many({})
    db.etfs.delete_many({})
    db.instrument_state.delete_many({})
    sold = record(client, url, trade("sell", quantity="1", price="120", day="2026-09-16"))
    assert sold["instrument"] == original["instrument"]
    assert detail(client, url)["realized_pnl_eur"] == "20.00"


def test_override_for_a_new_listing_does_not_confirm_the_pinned_listing(tracker, db):
    client, _, _, url = tracker
    db.quotes.update_many({}, {"$set": {"data.price_currency": None}})
    md.set_price_currency(db, ISIN, "EUR")
    record(client, url, cash())
    record(client, url, trade())
    db.instrument_state.update_one({"_id": ISIN}, {"$set": {"listing": SECOND_LISTING}})
    result = detail(client, url)
    assert result["positions_value_eur"] is None
    assert result["positions"][0]["market_price_eur"] is None
    assert "currency" in result["positions"][0]["warning"]


def test_backdating_replays_later_cash_balances_not_just_final_balance(tracker, db):
    client, _, metadata, url = tracker
    record(client, url, cash(amount="100"))
    record(client, url, trade(day="2026-09-10"))
    record(client, url, cash(amount="100", day="2026-09-15"))
    before = db.portfolios.find_one({"_id": metadata["id"]})
    # Final cash would be +50, but the intervening buy would leave -50.
    response = client.post(url + "/transactions", json=cash("withdrawal", "50", "2026-09-05"))
    assert response.status_code == 422
    assert "cash on 2026-09-10" in response.json()["detail"]
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before
    # A deposit later on the same date cannot retroactively fund an earlier day.
    assert client.post(url + "/transactions", json=trade(day="2026-08-31")).status_code == 422
    record(client, url, cash(amount="50", day="2026-09-05"))
    record(client, url, cash("withdrawal", "50", "2026-09-05"))
    assert detail(client, url)["cash_eur"] == "100.00"


def test_backdating_replays_later_holdings_and_same_day_sequence(tracker, db):
    client, _, metadata, url = tracker
    record(client, url, cash())
    record(client, url, trade(quantity="2", day="2026-09-02"))
    record(client, url, trade("sell", quantity="2", day="2026-09-10"))
    record(client, url, trade(quantity="2", day="2026-09-15"))
    before = db.portfolios.find_one({"_id": metadata["id"]})
    # Positive final holdings cannot hide the later oversell on September 10.
    response = client.post(url + "/transactions", json=trade("sell", day="2026-09-05"))
    assert response.status_code == 422
    assert "holdings on 2026-09-10" in response.json()["detail"]
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before
    assert client.post(url + "/transactions", json=trade("sell", day="2026-09-01")).status_code == 422
    record(client, url, trade("sell", quantity="2", day="2026-09-15"))
    assert detail(client, url)["positions"] == []
    assert client.post(url + "/transactions", json=trade("sell", day="2026-09-15")).status_code == 422


@pytest.mark.parametrize("commission", ["0.00", "6.00"])
@pytest.mark.parametrize("day", ["2026-09-14", "2026-09-16"])
def test_buy_commission_is_realized_immediately_without_changing_equity_or_total_pnl(tracker, db, commission, day):
    client, customer_id, metadata, url = tracker
    db.quotes.update_many({}, {"$set": {"data.close": "100"}})
    record(client, url, cash(amount="1006"))
    purchase = trade(quantity="10", price="100", commission_eur=commission, day=day)
    bought = record(client, url, purchase)
    before = db.portfolios.find_one({"_id": metadata["id"]})
    # Stored historical trades are replayed, not rewritten; a retry cannot expense the fee twice.
    assert record(client, url, purchase) == bought
    fee = Decimal(commission)
    realized = tracker_api.euros(-fee)
    equity = tracker_api.euros(Decimal("1006") - fee)
    result = detail(client, url)
    assert result["cash_eur"] == tracker_api.euros(Decimal("6") - fee)
    assert result["equity_eur"] == equity
    assert result["positions_value_eur"] == result["positions"][0]["cost_basis_eur"] == "1000.00"
    assert result["positions"][0]["average_price_eur"] == "100.00000000"
    assert result["realized_pnl_eur"] == realized
    assert result["unrealized_pnl_eur"] == result["daily_unrealized_pnl_eur"] == "0.00"
    assert result == tracker_api.valuation(db, before)
    for endpoint in ("/customers", f"/customers/{customer_id}/portfolios"):
        summary = client.get(endpoint).json()["items"][0]
        assert summary["net_worth_eur"] == equity
        assert summary["realized_pnl_eur"] == summary["total_pnl_eur"] == realized
        assert summary["unrealized_pnl_eur"] == "0.00"
        # Daily total includes the commission exactly once, only in the trade's interval.
        assert summary["today_pnl_eur"] == (realized if day == "2026-09-16" else "0.00")
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before
    db.quotes.delete_many({})
    assert detail(client, url)["realized_pnl_eur"] == realized
    for endpoint in ("/customers", f"/customers/{customer_id}/portfolios"):
        summary = client.get(endpoint).json()["items"][0]
        assert summary["realized_pnl_eur"] == realized
        assert summary["net_worth_eur"] is summary["unrealized_pnl_eur"] is summary["total_pnl_eur"] is None


def test_average_cost_commissions_partial_sale_rebuy_and_full_liquidation(tracker, db):
    client, _, _, url = tracker
    record(client, url, cash())
    record(client, url, trade(quantity="2", commission_eur="2"))
    record(client, url, trade(price="160", commission_eur="1"))
    result = detail(client, url)
    assert result["cash_eur"] == "637.00"
    assert result["positions"][0]["cost_basis_eur"] == "360.00"
    assert result["positions"][0]["average_price_eur"] == "120.00000000"
    assert result["realized_pnl_eur"] == "-3.00"
    record(client, url, trade("sell", price="150", commission_eur="2", day="2026-09-16"))
    result = detail(client, url)
    assert result["cash_eur"] == "785.00"
    assert result["realized_pnl_eur"] == "25.00"
    assert result["positions"][0]["quantity"] == "2"
    assert result["positions"][0]["cost_basis_eur"] == "240.00"
    assert result["daily_unrealized_pnl_eur"] == "20.00"  # Remaining two shares only.
    record(client, url, trade(price="130", commission_eur="1", day="2026-09-16"))
    result = detail(client, url)
    assert result["cash_eur"] == "654.00"
    assert result["positions"][0]["cost_basis_eur"] == "370.00"
    assert result["positions"][0]["average_price_eur"] == "123.33333333"
    assert result["realized_pnl_eur"] == "24.00"
    assert result["unrealized_pnl_eur"] == "-40.00"
    assert result["daily_unrealized_pnl_eur"] == "0.00"  # 330 - (200 prior-close basis + 130 new cost).
    record(client, url, trade("sell", quantity="3", price="120", commission_eur="1", day="2026-09-16"))
    db.quotes.delete_many({})  # Closed positions do not depend on market data.
    result = detail(client, url)
    assert result["positions"] == [] and result["warnings"] == []
    assert result["realized_pnl_eur"] == "13.00"
    assert result["cash_eur"] == result["equity_eur"] == "1013.00"
    for field in ("positions_value_eur", "unrealized_pnl_eur", "daily_unrealized_pnl_eur"):
        assert result[field] == "0.00"
    assert result["daily_from_date"] is result["daily_to_date"] is None


def test_fractional_quantities_round_half_up_and_clear_exact_remaining_basis(tracker):
    client, _, _, url = tracker
    record(client, url, cash(amount="1"))
    record(client, url, trade(quantity="0.1", price="0.1"))
    record(client, url, trade(quantity="0.2", price="0.1"))
    result = detail(client, url)
    assert result["positions"][0]["quantity"] == "0.3"
    assert result["positions"][0]["cost_basis_eur"] == "0.03"
    record(client, url, trade("sell", quantity="0.1", price="0.15", day="2026-09-16"))
    result = detail(client, url)
    assert result["positions"][0]["quantity"] == "0.2"
    assert result["positions"][0]["cost_basis_eur"] == "0.02"
    assert result["realized_pnl_eur"] == "0.01"  # 0.015 gross settles as 0.02, not banker's rounding.
    record(client, url, trade("sell", quantity="0.2", price="0.15", day="2026-09-16"))
    result = detail(client, url)
    assert result["positions"] == []
    assert result["realized_pnl_eur"] == "0.02" and result["cash_eur"] == "1.02"
    record(client, url, trade(quantity="0.00000001", price="1000000", day="2026-09-16"))
    result = detail(client, url)
    assert result["positions"][0]["quantity"] == "0.00000001"
    assert result["positions"][0]["cost_basis_eur"] == "0.01"
    assert result["positions"][0]["average_price_eur"] == "1000000.00000000"
    assert result["realized_pnl_eur"] == "0.02"


def test_latest_two_distinct_raw_closes_not_adjusted_revisions_or_future_evidence(tracker, db, api, monkeypatch):
    client, _, _, url = tracker
    record(client, url, cash())
    record(client, url, trade(quantity="2"))
    db.quotes.update_many({}, {"$set": {"collected_at": NOW - timedelta(hours=2), "data.adj_close": "999"}})
    quote(db, "2026-09-16", "111", collected_at=NOW - timedelta(hours=1), adj_close="888")
    quote(db, "2026-09-16", Decimal128("112.1234567890123456789"), adj_close="777")
    quote(db, "2026-09-16", "900", collected_at=NOW + timedelta(seconds=1))
    quote(db, "2026-09-18", "901")
    quote(db, "2026-09-17", "902", listing=SECOND_LISTING)
    quote(db, "2026-09-17", "903", isin=SECOND_ISIN)
    before = list(db.quotes.find())
    provider = Mock(side_effect=AssertionError("Portfolio reads must not acquire market data"))
    monkeypatch.setattr(md, "MarketstackClient", provider)
    result = detail(client, url)
    position = result["positions"][0]
    assert position["market_price_eur"] == "112.1234567890123456789"
    assert position["price_date"] == "2026-09-16" and position["previous_price_date"] == "2026-09-15"
    assert result["positions_value_eur"] == "224.25"
    assert result["unrealized_pnl_eur"] == result["daily_unrealized_pnl_eur"] == "24.25"
    assert result["equity_eur"] == "1024.25"
    assert result["daily_from_date"] == "2026-09-15" and result["daily_to_date"] == "2026-09-16"
    assert position["warning"] is None
    assert result["warnings"] == []
    assert list(db.quotes.find()) == before
    assert db.marketstack_usage.count_documents({}) == 0
    api[1].assert_not_called()
    provider.assert_not_called()


@pytest.mark.parametrize("invalid", [None, True, "bad", "NaN", "Infinity", "0", "-1", "1e-1000", "1e1000", {}, []])
def test_invalid_latest_raw_close_never_falls_back_to_adjusted_or_older_close(tracker, db, invalid):
    client, _, _, url = tracker
    record(client, url, cash())
    record(client, url, trade())
    quote(db, "2026-09-16", invalid, adj_close="999")  # Newest observation wins even when unusable.
    result = detail(client, url)
    position = result["positions"][0]
    assert position["price_date"] == "2026-09-16"
    for field in ("market_price_eur", "position_value_eur", "unrealized_pnl_eur", "daily_unrealized_pnl_eur"):
        assert position[field] is None
    for field in ("positions_value_eur", "equity_eur", "unrealized_pnl_eur", "daily_unrealized_pnl_eur"):
        assert result[field] is None
    assert result["cash_eur"] == "900.00" and result["realized_pnl_eur"] == "0.00"
    assert position["warning"] == "EUR valuation unavailable: latest saved raw close is missing, invalid, or currency is unconfirmed/conflicting."
    assert result["warnings"] == [f"{ISIN}: {position['warning']}"]


@pytest.mark.parametrize("mode", ["no-quotes", "missing-close", "one-date", "invalid-previous"])
def test_missing_quote_or_prior_close_keeps_unavailable_values_explicit(tracker, db, mode):
    client, _, _, url = tracker
    record(client, url, cash())
    record(client, url, trade())
    if mode == "no-quotes":
        db.quotes.delete_many({})
    elif mode == "missing-close":
        db.quotes.update_one({"date": "2026-09-16"}, {"$unset": {"data.close": ""}})
    elif mode == "one-date":
        db.quotes.delete_many({"date": "2026-09-15"})
        quote(db, "2026-09-16", "110")  # Repeated observations are not two closing days.
    else:
        quote(db, "2026-09-14", "99")  # Do not skip the invalid prior day to use this older price.
        db.quotes.update_one({"date": "2026-09-15"}, {"$set": {"data.close": None, "data.adj_close": "100"}})
    result = detail(client, url)
    assert result["daily_unrealized_pnl_eur"] is None
    assert result["daily_from_date"] is result["daily_to_date"] is None
    if mode in ("no-quotes", "missing-close"):
        assert result["positions_value_eur"] is result["equity_eur"] is None
    else:
        assert result["positions_value_eur"] == "110.00"
        assert result["unrealized_pnl_eur"] == "10.00"
        assert result["positions"][0]["previous_price_date"] is None


@pytest.mark.parametrize("trade_day", ["2026-09-16", "2026-09-17"])
def test_daily_reference_allocates_new_buys_and_sales_to_remaining_shares(tracker, trade_day):
    client, _, _, url = tracker
    record(client, url, cash())
    record(client, url, trade(quantity="2", price="90", commission_eur="2"))
    record(client, url, trade(quantity="2", price="105", commission_eur="2", day=trade_day))
    record(client, url, trade("sell", quantity="1", price="120", commission_eur="1", day=trade_day))
    result = detail(client, url)
    # Reference: (2 * prior close 100 + new purchase 210) * 3/4 = 307.50.
    # Average-cost basis: (180 + 210) * 3/4 = 292.50. Realized: 120 - 97.50 - all 5 in fees.
    assert result["positions"][0]["quantity"] == "3"
    assert result["positions"][0]["cost_basis_eur"] == "292.50"
    assert result["realized_pnl_eur"] == "17.50"
    assert result["unrealized_pnl_eur"] == "37.50"
    assert result["daily_unrealized_pnl_eur"] == "22.50"


@pytest.mark.parametrize("kind,expected", [("buy", "10.00"), ("sell", "10.00")])
def test_trade_newer_than_latest_quote_uses_last_saved_close(tracker, db, api, monkeypatch, kind, expected):
    client, _, _, url = tracker
    record(client, url, cash())
    record(client, url, trade(quantity="2"))
    record(client, url, trade(kind, price="120", commission_eur="2", day="2026-09-17"))
    before = list(db.quotes.find())
    provider = Mock(side_effect=AssertionError("Portfolio reads must not acquire market data"))
    monkeypatch.setattr(md, "MarketstackClient", provider)
    result = detail(client, url)
    position = result["positions"][0]
    assert result["positions_value_eur"] == ("330.00" if kind == "buy" else "110.00")
    # New purchases use actual cost (120), with the fee expensed in realized PnL;
    # sales only reduce the reference for the shares still held.
    assert result["daily_unrealized_pnl_eur"] == position["daily_unrealized_pnl_eur"] == expected
    assert result["realized_pnl_eur"] == ("-2.00" if kind == "buy" else "18.00")
    assert position["market_price_eur"] == "110"
    assert result["daily_from_date"] == position["previous_price_date"] == "2026-09-15"
    assert result["daily_to_date"] == position["price_date"] == "2026-09-16"
    assert position["warning"] is None
    assert result["warnings"] == []
    assert list(db.quotes.find()) == before
    assert db.marketstack_usage.count_documents({}) == 0
    api[1].assert_not_called()
    provider.assert_not_called()


@pytest.mark.parametrize("previous_position", [False, True])
def test_new_position_after_latest_close_uses_actual_cost_and_saved_price(tracker, previous_position):
    client, _, _, url = tracker
    record(client, url, cash())
    if previous_position:
        record(client, url, trade(quantity="2"))
        record(client, url, trade("sell", quantity="2", day="2026-09-17"))
    record(client, url, trade(quantity="2", price="115", commission_eur="2", day="2026-09-17"))
    result = detail(client, url)
    position = result["positions"][0]
    assert position["quantity"] == "2"
    assert position["cost_basis_eur"] == "230.00"
    assert result["positions_value_eur"] == "220.00"
    assert result["unrealized_pnl_eur"] == result["daily_unrealized_pnl_eur"] == "-10.00"
    assert position["daily_unrealized_pnl_eur"] == "-10.00"
    assert result["realized_pnl_eur"] == "-2.00"
    assert result["daily_from_date"] == "2026-09-15"
    assert result["daily_to_date"] == "2026-09-16"


def test_mixed_quote_periods_preserve_position_daily_pnl_but_not_portfolio_total(tracker, db):
    client, _, _, url = tracker
    second_instrument(db, days=("2026-09-14", "2026-09-15"))
    record(client, url, cash())
    record(client, url, trade())
    record(client, url, trade(instrument_id=SECOND_ISIN, price="20"))
    result = detail(client, url)
    assert len(result["positions"]) == 2
    assert {item["daily_unrealized_pnl_eur"] for item in result["positions"]} == {"10.00"}
    assert result["positions_value_eur"] == "140.00" and result["equity_eur"] == "1020.00"
    assert result["unrealized_pnl_eur"] == "20.00"
    assert result["daily_unrealized_pnl_eur"] is None
    assert result["daily_from_date"] is result["daily_to_date"] is None
    assert any("different closing-date intervals" in warning for warning in result["warnings"])
    # One unavailable position makes value/unrealized totals unavailable, not a misleading partial sum.
    db.quotes.update_one({"instrument_id": SECOND_ISIN, "date": "2026-09-15"}, {"$set": {"data.close": None}})
    result = detail(client, url)
    assert result["positions_value_eur"] is result["equity_eur"] is result["unrealized_pnl_eur"] is None
    first = next(item for item in result["positions"] if item["instrument"]["instrument_id"] == ISIN)
    assert first["position_value_eur"] == "110.00" and first["daily_unrealized_pnl_eur"] == "10.00"


@pytest.mark.parametrize("start,end,available", [
    ("2026-09-15", "2026-09-16", True),   # Consecutive weekdays.
    ("2026-09-11", "2026-09-14", True),   # Friday to Monday.
    ("2026-09-14", "2026-09-16", False),  # Missing Tuesday.
    ("2026-09-10", "2026-09-14", False),  # Missing Friday, despite the weekend.
    ("2026-09-11", "2026-09-15", False),  # Missing Monday.
    ("2026-09-11", "2026-09-12", False),  # Saturday latest close.
    ("2026-09-12", "2026-09-14", False),  # Saturday previous close.
    ("2026-09-13", "2026-09-14", False),  # Sunday previous close.
    ("2026-09-12", "2026-09-13", False),  # Both endpoints on a weekend.
    ("2026-09-01", "2026-09-16", False),  # Long stale interval.
])
def test_daily_pnl_requires_adjacent_weekdays_or_friday_monday(tracker, db, start, end, available):
    client, _, _, url = tracker
    db.quotes.delete_many({})
    seed_history(db, [{**bar(day, close), "price_currency": "EUR"}
                      for day, close in ((start, "100"), (end, "110"))])
    record(client, url, cash())
    record(client, url, trade(day="2026-09-01"))
    result = detail(client, url)
    position = result["positions"][0]
    assert result["positions_value_eur"] == "110.00" and result["unrealized_pnl_eur"] == "10.00"
    assert position["previous_price_date"] == start and position["price_date"] == end
    assert position["daily_unrealized_pnl_eur"] == result["daily_unrealized_pnl_eur"] == ("10.00" if available else None)
    assert result["daily_from_date"] == (start if available else None)
    assert result["daily_to_date"] == (end if available else None)
    if not available:
        assert "missing weekdays or non-trading dates" in position["warning"]


def test_missing_currency_requires_override_without_losing_raw_close(tracker, db):
    client, _, _, url = tracker
    db.quotes.update_many({}, {"$unset": {"data.price_currency": ""}})
    record(client, url, cash())
    assert client.get("/portfolio-instruments").json()["total"] == 0
    assert client.post(url + "/transactions", json=trade()).status_code == 422
    md.set_price_currency(db, ISIN, "EUR")
    record(client, url, trade())
    result = detail(client, url)
    assert result["positions"][0]["market_price_eur"] == "110"
    assert result["daily_unrealized_pnl_eur"] == "10.00"


def test_omitted_dates_default_to_frozen_utc_today_for_cash_and_trade(tracker, clock):
    client, _, _, url = tracker
    assert clock[0] == NOW
    deposit = cash()
    deposit.pop("date")
    purchase = trade()
    purchase.pop("date")
    for body in (deposit, purchase):
        event = record(client, url, body)
        assert event["date"] == "2026-09-17"
        assert event["created_at"] == "2026-09-17T12:00:00.000000Z"
    assert detail(client, url)["cash_eur"] == "900.00"


@pytest.mark.parametrize("day", ["2026-09-18", "2026-02-30", "2026-9-1", "2026-09-17T00:00:00Z", 0, None])
def test_invalid_and_future_dates_rejected_for_cash_and_trades(tracker, db, day):
    client, _, metadata, url = tracker
    before = db.portfolios.find_one({"_id": metadata["id"]})
    for body in (cash(day=day), trade(day=day)):
        response = client.post(url + "/transactions", json=body)
        assert response.status_code == 422, response.text
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before


@pytest.mark.parametrize("field,bad", [
    (field, bad) for field in ("amount_eur", "quantity", "price_eur", "commission_eur")
    for bad in ("-1", "NaN", "Infinity", True, None, "1000000000001")
] + [("amount_eur", "0"), ("amount_eur", "0.001"), ("commission_eur", "0.001"),
     ("quantity", "0"), ("price_eur", "0"), ("quantity", "0.000000001"), ("price_eur", "0.000000001")])
def test_bad_numbers_are_rejected_before_writing(tracker, db, field, bad):
    client, _, metadata, url = tracker
    before = db.portfolios.find_one({"_id": metadata["id"]})
    body = cash(**{field: bad}) if field == "amount_eur" else trade(**{field: bad})
    response = client.post(url + "/transactions", json=body)
    assert response.status_code == 422, response.text
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before


@pytest.mark.parametrize("change", [
    {"quantity": "0.00000001", "price_eur": "0.01"},
    {"quantity": "1000000000000", "price_eur": "2"},
    {"type": "sell", "price_eur": "1", "commission_eur": "1.01"},
])
def test_trade_settlement_must_be_at_least_a_cent_bounded_and_cover_sell_fees(tracker, db, change):
    client, _, metadata, url = tracker
    record(client, url, cash())
    record(client, url, trade())
    before = db.portfolios.find_one({"_id": metadata["id"]})
    assert client.post(url + "/transactions", json=trade(**change)).status_code == 422
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before


def test_cash_and_buy_fees_are_fully_funded_and_short_sales_rejected(tracker):
    client, _, _, url = tracker
    assert client.post(url + "/transactions", json=cash("withdrawal", "0.01")).status_code == 422
    assert client.post(url + "/transactions", json=trade("sell")).status_code == 422
    record(client, url, cash(amount="100"))
    assert client.post(url + "/transactions", json=trade(commission_eur="0.01")).status_code == 422
    record(client, url, trade())
    assert detail(client, url)["cash_eur"] == "0.00"
    assert client.post(url + "/transactions", json=trade("sell", quantity="1.00000001")).status_code == 422
    record(client, url, trade("sell", commission_eur="100"))  # Net proceeds may be zero, never negative.
    assert detail(client, url)["cash_eur"] == "0.00"
    assert detail(client, url)["realized_pnl_eur"] == "-100.00"


def test_client_id_retries_are_normalized_idempotent_and_conflicts_do_not_write(tracker, db):
    client, _, metadata, url = tracker
    deposit = cash(amount="1000.00")
    first = record(client, url, deposit)
    assert record(client, url, {**deposit, "amount_eur": "1e3", "currency": "EUR"}) == first
    purchase = trade(quantity="1.00", price="100.000", commission_eur="0.00")
    bought = record(client, url, purchase)
    assert record(client, url, {**purchase, "quantity": "1", "price_eur": "1e2"}) == bought
    before = db.portfolios.find_one({"_id": metadata["id"]})
    for body in ({**deposit, "amount_eur": "999"}, {**deposit, "type": "withdrawal"},
                 {**purchase, "quantity": "2"}, {**purchase, "date": "2026-09-13"},
                 {**purchase, "commission_eur": "1"}):
        response = client.post(url + "/transactions", json=body)
        assert response.status_code == 409
        assert "different inputs" in response.json()["detail"]
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before
    db.instruments.delete_many({})  # Retries must not re-resolve an acquired instrument.
    assert record(client, url, purchase) == bought
    assert before["revision"] == 2 and len(before["transactions"]) == 2
    assert "_request" not in bought and "user_id" not in bought
    _, second_url = portfolio(client, metadata["customer_id"])
    assert record(client, second_url, deposit)["id"] != first["id"]  # Identity is scoped to one portfolio.


@pytest.mark.parametrize("change", [
    {"id": "forged"}, {"sequence": 99}, {"created_at": "2000-01-01"}, {"user_id": "foreign"},
    {"_request": {}}, {"_removed_at": "2026-09-17"}, {"instrument": {"currency": "USD"}}, {"currency": "USD"},
    {"client_id": "not-a-uuid"}, {"type": "dividend"},
])
def test_transaction_server_fields_and_invalid_identity_are_rejected(tracker, db, change):
    client, _, metadata, url = tracker
    record(client, url, cash())
    before = db.portfolios.find_one({"_id": metadata["id"]})
    for body in (cash(**change), trade(**change)):
        assert client.post(url + "/transactions", json=body).status_code == 422
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before


def test_transactions_cannot_be_edited_or_bulk_deleted(tracker, db):
    client, _, metadata, url = tracker
    event = record(client, url, cash())
    before = db.portfolios.find_one({"_id": metadata["id"]})
    assert client.delete(url + "/transactions").status_code == 405
    for method in ("patch", "put"):
        for endpoint in (url + "/transactions", url + "/transactions/" + event["id"]):
            response = client.request(method, endpoint, json={"amount_eur": "9999"})
            assert response.status_code in (404, 405)
    assert client.patch(url, json={"name": "Changed", "transactions": []}).status_code == 422
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before


def test_portfolio_transaction_and_instrument_pagination_are_stable(tracker, db):
    client, customer_id, first, url = tracker
    second, _ = portfolio(client, customer_id, name="Second")
    third, _ = portfolio(client, customer_id, name="Third")
    base = f"/customers/{customer_id}/portfolios"
    # Equal created_at timestamps must still have a deterministic ID tie-breaker.
    for offset, item in enumerate((third, second, first)):
        assert client.get(base, params={"limit": 1, "offset": offset}).json() == {
            "items": [{**item, "net_worth_eur": "0.00", "realized_pnl_eur": "0.00", "unrealized_pnl_eur": "0.00",
                       "today_pnl_eur": "0.00", "total_pnl_eur": "0.00", "today_from_date": None, "today_to_date": None}],
            "total": 3, "limit": 1, "offset": offset,
        }
    events = [record(client, url, cash(day=day, amount="10")) for day in
              ("2026-09-15", "2026-09-01", "2026-09-15", "2026-09-14")]
    ordered = [events[2], events[0], events[3], events[1]]
    assert client.get(url + "/transactions", params={"limit": 2, "offset": 1}).json() == {
        "items": ordered[1:3], "total": 4, "limit": 2, "offset": 1,
    }
    assert [event["sequence"] for event in events] == [1, 2, 3, 4]
    second_instrument(db)
    picker = client.get("/portfolio-instruments").json()
    assert [item["instrument_id"] for item in picker["items"]] == sorted([ISIN, SECOND_ISIN])
    assert client.get("/portfolio-instruments?limit=1&offset=1").json() == {
        "items": picker["items"][1:], "total": 2, "limit": 1, "offset": 1,
    }
    for endpoint in (base, url + "/transactions", "/portfolio-instruments"):
        assert client.get(endpoint + "?offset=100").json()["items"] == []
        for query in ("limit=0", "limit=101", "offset=-1", "offset=abc"):
            assert client.get(endpoint + "?" + query).status_code == 422
    for event in ordered:
        assert all(not key.startswith("_") for key in event)


def synchronize_first_two_writes(monkeypatch, collection):
    """Force both requests to validate the same revision before either CAS writes."""
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


@pytest.mark.parametrize("race", ["buy", "withdrawal", "sell", "same-client-id", "cap"])
def test_atomic_revision_cas_rechecks_cash_holdings_idempotency_and_cap(tracker, db, monkeypatch, race):
    client, _, metadata, url = tracker
    record(client, url, cash(amount="100"))
    if race == "sell":
        record(client, url, trade())
        bodies = [trade("sell", quantity="0.75") for _ in range(2)]
    elif race == "withdrawal":
        bodies = [cash("withdrawal", "75", "2026-09-14") for _ in range(2)]
    elif race == "cap":
        monkeypatch.setattr(tracker_api, "MAX_TRANSACTIONS", 2)
        bodies = [cash(amount="1") for _ in range(2)]
    else:
        bodies = [trade(price="75") for _ in range(2)]
        if race == "same-client-id":
            bodies[1] = deepcopy(bodies[0])
    before = db.portfolios.find_one({"_id": metadata["id"]})
    revisions = synchronize_first_two_writes(monkeypatch, db.portfolios)
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(client.post, url + "/transactions", json=body) for body in bodies]
        responses = [future.result(timeout=15) for future in futures]
    assert revisions[:2] == [before["revision"], before["revision"]]
    expected_error = 409 if race == "cap" else 422
    assert sorted(response.status_code for response in responses) == (
        [201, 201] if race == "same-client-id" else [201, expected_error]
    ), [response.text for response in responses]
    after = db.portfolios.find_one({"_id": metadata["id"]})
    assert after["revision"] == before["revision"] + 1
    assert len(after["transactions"]) == len(before["transactions"]) + 1
    assert after["transactions"][:-1] == before["transactions"]
    result = detail(client, url)
    assert Decimal(result["cash_eur"]) >= 0
    if race == "sell":
        assert result["positions"][0]["quantity"] == "0.25"
    elif race != "cap":
        assert result["cash_eur"] == "25.00"
    if race == "same-client-id":
        assert responses[0].json() == responses[1].json()


def test_transaction_cap_allows_existing_retry_but_no_additional_append(tracker, db, monkeypatch):
    client, _, metadata, url = tracker
    monkeypatch.setattr(tracker_api, "MAX_TRANSACTIONS", 2)
    first = cash()
    saved = record(client, url, first)
    record(client, url, cash(amount="1"))
    before = db.portfolios.find_one({"_id": metadata["id"]})
    assert record(client, url, first) == saved
    response = client.post(url + "/transactions", json=cash())
    assert response.status_code == 409 and "2-transaction limit" in response.json()["detail"]
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before


def test_contended_cas_is_bounded_and_returns_retryable_conflict_without_writes(tracker, db, monkeypatch):
    client, _, metadata, url = tracker
    before = db.portfolios.find_one({"_id": metadata["id"]})
    write = Mock(return_value=UpdateResult({"n": 0, "nModified": 0}, acknowledged=True))
    monkeypatch.setattr(db.portfolios, "update_one", write)
    response = client.post(url + "/transactions", json=cash())
    assert response.status_code == 409
    assert "Retry with the same transaction ID" in response.json()["detail"]
    assert write.call_count == 8
    assert db.portfolios.find_one({"_id": metadata["id"]}) == before
