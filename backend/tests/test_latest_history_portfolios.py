"""Synthetic latest-Yahoo portfolio reads; no provider, credentials or live DB."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

import goal_strategies as strategies
import market_data as md
import plan_tracking
import portfolio_performance as performance
import portfolio_recommendations as recommendations
import portfolio_tracker as ledger
from goal_simulation import contribution_dates
from test_latest_history_contract import CUTOFF, ISIN, LISTING, NOW, history_db, publish

SECOND_ISIN = "IE00B4L5Y983"
SECOND_LISTING = {"symbol": "EUNL.DE", "exchange": "XETR"}
BENCHMARK = "idx-00000000-0000-4000-8000-000000000001"
PROXY = "idx-00000000-0000-4000-8000-000000000002"
OWNER = "synthetic-owner"
CUSTOMER = "synthetic-customer"


def instrument(isin=ISIN, listing=None):
    return {"instrument_id": isin, "name": "Synthetic instrument", "currency": "EUR", **(listing or LISTING)}


def add_source(db, isin, listing, generation, *, currency="EUR"):
    if isin != ISIN:
        db.instrument_state.insert_one({"_id": isin, "listing": listing})
        if not isin.startswith("idx-"):
            db.etfs.insert_one({"isin": isin, "name": "Synthetic ETF", "import_date": NOW})
    if isin.startswith("idx-"):
        db.indices.insert_one({"instrument_id": isin, "symbol": listing["symbol"],
                               "name": "Synthetic index", "currency": currency, "return_variant": "price"})
    publish(db, generation, isin=isin, listing=listing, close="110")
    db.instruments.update_one({"job_id": generation}, {"$set": {"data.currency": currency}})
    return instrument(isin, listing)


def portfolio(*sources):
    return {"_id": "synthetic-portfolio", "customer_id": CUSTOMER, "name": "Synthetic portfolio",
            "plan_id": "synthetic-plan", "currency": "EUR", "created_at": NOW, "revision": 0,
            "transactions": [
                {"type": "deposit", "date": "2025-01-06", "sequence": 1, "amount_eur": "1000"},
                *[{"type": "buy", "date": "2025-01-06", "sequence": index + 2,
                   "quantity": "1", "price_eur": "100", "commission_eur": "0", "instrument": source}
                  for index, source in enumerate(sources)],
            ]}


def assert_leased(db, *generations):
    for generation in generations:
        job = db.acquisition_jobs.find_one({"_id": generation})
        assert job and job.get("reader_leases"), generation


def assert_released(db):
    assert all(not job.get("reader_leases") for job in db.acquisition_jobs.find())


def assert_conflict(db, call):
    with pytest.raises(md.DataError) as error:
        call()
    assert error.value.status == 409
    assert_released(db)


def save_plan(db, document, *, target=ISIN):
    origin, end = date(2025, 1, 6), date(2028, 1, 6)
    schedule = contribution_dates(origin, end)
    plan = {"_id": document["plan_id"], "user_id": OWNER, "customer_id": CUSTOMER,
            "is_plan": True, "saved_at": md.public(NOW),
            "inputs": {"goal_eur": "10000", "starting_capital_eur": "1000", "goal_date": end.isoformat(),
                       "reliability": 0.8, "strategy": {"id": "buy_and_hold_single_instrument",
                                                       "parameters": {"instrument": target}}},
            "result": {"valuation_date": origin.isoformat(), "goal_date": end.isoformat(),
                       "monthly_contribution_eur": "100", "simulation_paths": 16, "seed": 3,
                       "success_probability": 0.8, "contribution_count": len(schedule),
                       "first_contribution_date": schedule[0].isoformat(),
                       "last_contribution_date": schedule[-1].isoformat()}}
    db.planner_simulations.insert_one(plan)
    return plan


@pytest.mark.parametrize("changed", ["holding", "benchmark", "proxy"])
def test_performance_keeps_all_sources_leased_through_derived_ratios(history_db, monkeypatch, changed):
    db = history_db
    holding = add_source(db, ISIN, LISTING, "holding-old")
    benchmark_listing = {"symbol": "^BENCHMARK", "exchange": "YAHOO"}
    proxy_listing = {"symbol": "^PROXY", "exchange": "YAHOO"}
    add_source(db, BENCHMARK, benchmark_listing, "benchmark-old")
    proxy = add_source(db, PROXY, proxy_listing, "proxy-old")
    db.app_settings.insert_one({"_id": "portfolio_performance", "proxy": proxy})
    selected = {"holding": (ISIN, LISTING), "benchmark": (BENCHMARK, benchmark_listing),
                "proxy": (PROXY, proxy_listing)}
    original = performance.RiskRatios.values
    reached = []

    def race(ratios):
        if not reached:
            reached.append(True)
            assert_leased(db, "holding-old", "benchmark-old", "proxy-old")
            isin, listing = selected[changed]
            publish(db, "replacement", order=1, isin=isin, listing=listing, close="150")
        return original(ratios)

    monkeypatch.setattr(performance.RiskRatios, "values", race)
    assert_conflict(db, lambda: performance.performance(db, portfolio(holding), BENCHMARK, now=CUTOFF))
    assert reached


def test_performance_rejects_proxy_prices_relabelled_by_new_profile(history_db, monkeypatch):
    db = history_db
    listing = {"symbol": "^PROXY", "exchange": "YAHOO"}
    proxy = add_source(db, PROXY, listing, "proxy-old", currency=None)
    db.app_settings.insert_one({"_id": "portfolio_performance", "proxy": proxy})
    db.instruments.update_one({"job_id": "proxy-old"}, {"$set": {"data.currency": "USD"}})
    db.quotes.update_many({"job_id": "proxy-old"}, {"$unset": {"data.price_currency": ""}})
    original = ledger.quote_rows
    reached = []

    def race(*args, **kwargs):
        rows = original(*args, **kwargs)
        reached.append(True)
        assert_leased(db, "proxy-old")
        publish(db, "proxy-new", order=1, isin=PROXY, listing=listing, close="150")
        db.instruments.update_one({"job_id": "proxy-new"}, {"$set": {"data.currency": "EUR"}})
        return rows

    monkeypatch.setattr(ledger, "quote_rows", race)
    assert_conflict(db, lambda: performance.performance(db, portfolio(), now=CUTOFF))
    assert reached


def test_performance_detects_publication_between_holdings(history_db, monkeypatch):
    db = history_db
    first = add_source(db, ISIN, LISTING, "first-old")
    second = add_source(db, SECOND_ISIN, SECOND_LISTING, "second-current")
    original = ledger.quote_rows
    reached = []

    def race(db, source, *args, **kwargs):
        if source["instrument_id"] == SECOND_ISIN:
            reached.append(True)
            assert_leased(db, "first-old", "second-current")
            publish(db, "first-new", order=1, close="150")
        return original(db, source, *args, **kwargs)

    monkeypatch.setattr(ledger, "quote_rows", race)
    assert_conflict(db, lambda: performance.performance(db, portfolio(first, second), now=CUTOFF))
    assert reached


def test_performance_valid_proxy_ratios_still_warn_about_look_ahead(history_db):
    db = history_db
    listing = {"symbol": "^PROXY", "exchange": "YAHOO"}
    proxy = add_source(db, PROXY, listing, "proxy-current")
    db.app_settings.insert_one({"_id": "portfolio_performance", "proxy": proxy})
    document = portfolio()
    document["transactions"][0]["date"] = "2025-01-07"
    result = performance.performance(db, document, now=NOW)
    assert result["summary"]["sharpe"] is not None
    assert result["summary"]["sortino"] is not None
    assert len(result["warnings"]) == 1
    assert "look-ahead bias" in result["warnings"][0]
    assert_released(db)


def test_performance_latest_prices_warning_and_lease_cleanup(history_db):
    db = history_db
    holding = add_source(db, ISIN, LISTING, "old")
    publish(db, "latest", order=1, close="150")
    document = portfolio(holding)
    before = deepcopy(document)
    result = performance.performance(db, document, now=CUTOFF)
    assert result["summary"]["twr_percent"] == "5.00000000"
    assert any("latest complete Yahoo" in warning and "look-ahead bias" in warning
               and "not strict point-in-time" in warning for warning in result["warnings"])
    assert any("select a saved EUR" in warning for warning in result["warnings"])
    assert document == before
    assert_released(db)


def test_performance_rejects_lease_expiry_during_derived_calculation(history_db, monkeypatch):
    db = history_db
    holding = add_source(db, ISIN, LISTING, "latest")
    original = performance.RiskRatios.values
    reached = []

    def expire(ratios):
        result = original(ratios)
        if not reached:
            reached.append(True)
            assert_leased(db, "latest")
            monkeypatch.setattr(md, "utcnow", lambda: NOW + timedelta(minutes=6))
        return result

    monkeypatch.setattr(performance.RiskRatios, "values", expire)
    assert_conflict(db, lambda: performance.performance(db, portfolio(holding), now=CUTOFF))
    assert reached


def test_market_snapshot_rejects_refresh_of_previous_instrument(history_db, monkeypatch):
    db = history_db
    add_source(db, ISIN, LISTING, "first-old")
    add_source(db, SECOND_ISIN, SECOND_LISTING, "second-current")
    original = ledger.acquired_instrument
    reached = []

    def race(db, isin, now):
        if isin == SECOND_ISIN:
            reached.append(True)
            assert_leased(db, "first-old")
            publish(db, "first-new", order=1, close="150")
        return original(db, isin, now)

    monkeypatch.setattr(ledger, "acquired_instrument", race)
    assert_conflict(db, lambda: recommendations.market_snapshot(db, {ISIN, SECOND_ISIN}, {}, CUTOFF))
    assert reached


def test_recommendations_hold_all_sources_through_allocation(history_db, monkeypatch):
    db = history_db
    first = add_source(db, ISIN, LISTING, "first-old")
    add_source(db, SECOND_ISIN, SECOND_LISTING, "target-current")
    document = portfolio(first)
    save_plan(db, document, target=SECOND_ISIN)
    original = recommendations.compute_actions
    reached = []

    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        reached.append(True)
        assert_leased(db, "first-old", "target-current")
        publish(db, "first-new", order=1, close="150")
        return result

    monkeypatch.setattr(recommendations, "compute_actions", race)
    assert_conflict(db, lambda: recommendations.recommendation_snapshot(db, OWNER, CUSTOMER, document, CUTOFF))
    assert reached


@pytest.mark.parametrize("stage", ["fee_adjustment", "projected_allocation"])
def test_preview_holds_original_vintage_through_derived_outcome(history_db, monkeypatch, stage):
    db = history_db
    add_source(db, ISIN, LISTING, "old")
    document = portfolio()
    save_plan(db, document)
    snapshot = recommendations.recommendation_snapshot(db, OWNER, CUSTOMER, document, CUTOFF)
    buy = next(action for action in snapshot["actions"] if action["type"] == "buy")
    body = recommendations.PreviewRequest(
        snapshot_token=snapshot["snapshot_token"], date=CUTOFF.date().isoformat(),
        action_ids=[action["id"] for action in snapshot["actions"] if action["eligible"]],
        overrides={buy["id"]: {"commission_eur": "1"}} if stage == "fee_adjustment" else {},
    )
    reached = []
    if stage == "fee_adjustment":
        original = recommendations._recommendation_snapshot

        def race(*args, **kwargs):
            if len(args) > 5:
                reached.append(True)
                assert_leased(db, "old")
                publish(db, "new", order=1, close="150")
            return original(*args, **kwargs)

        monkeypatch.setattr(recommendations, "_recommendation_snapshot", race)
    else:
        original = recommendations.projected_allocation

        def race(*args, **kwargs):
            result = original(*args, **kwargs)
            if not reached:
                reached.append(True)
                assert_leased(db, "old")
                publish(db, "new", order=1, close="150")
            return result

        monkeypatch.setattr(recommendations, "projected_allocation", race)
    before = deepcopy(document)
    assert_conflict(db, lambda: recommendations.prepare_preview(db, OWNER, CUSTOMER, document, body, CUTOFF))
    assert reached
    assert document == before
    assert db.portfolios.count_documents({}) == 0


def test_recommendation_preview_success_keeps_raw_prices_and_releases_leases(history_db):
    db = history_db
    add_source(db, ISIN, LISTING, "latest")
    document = portfolio()
    save_plan(db, document)
    snapshot = recommendations.recommendation_snapshot(db, OWNER, CUSTOMER, document, CUTOFF)
    assert_released(db)
    buy = next(action for action in snapshot["actions"] if action["type"] == "buy")
    assert buy["price_eur"] == "110"
    body = recommendations.PreviewRequest(
        snapshot_token=snapshot["snapshot_token"], date=CUTOFF.date().isoformat(),
        action_ids=[action["id"] for action in snapshot["actions"] if action["eligible"]],
        overrides={buy["id"]: {"commission_eur": "1"}},
    )
    review, entries = recommendations.prepare_preview(db, OWNER, CUSTOMER, document, body, CUTOFF)
    assert review["preview_token"]
    assert [entry["type"] for entry in entries] == ["buy"]
    assert entries[0]["price_eur"] == "110"
    assert_released(db)


@pytest.mark.parametrize("stage", ["after_valuation", "forecast_outcome"])
@pytest.mark.parametrize("changed", ["holding", "strategy"])
def test_plan_tracking_protects_equity_and_strategy_through_forecast(history_db, monkeypatch, stage, changed):
    db = history_db
    holding = add_source(db, ISIN, LISTING, "holding-old")
    add_source(db, SECOND_ISIN, SECOND_LISTING, "strategy-old")
    document = portfolio(holding)
    save_plan(db, document, target=SECOND_ISIN)
    monkeypatch.setattr(ledger, "require_portfolio", lambda *args: document)
    reached = []

    def replace_history():
        reached.append(True)
        assert_leased(db, "holding-old", "strategy-old")
        isin, listing = (ISIN, LISTING) if changed == "holding" else (SECOND_ISIN, SECOND_LISTING)
        publish(db, "replacement", order=1, isin=isin, listing=listing, close="150")

    if stage == "after_valuation":
        original = ledger.valuation

        def race(*args, **kwargs):
            result = original(*args, **kwargs)
            replace_history()
            return result

        monkeypatch.setattr(ledger, "valuation", race)

    def track(parameters, inputs, db, today, **options):
        assert parameters.instrument == SECOND_ISIN
        assert inputs.starting_capital_eur == Decimal("1899.00")
        assert options == {"paths": 16, "seed": 3, "fixed_monthly": Decimal("100")}
        if stage == "forecast_outcome":
            replace_history()
        return {"fixed_contribution_success_probability": 0.8, "forecast_goal_date": "2027-12-01",
                "forecast_status": "reached", "forecast_search_end_date": "2030-01-01",
                "monthly_contribution_eur": "100", "success_probability": 0.8, "warnings": []}

    monkeypatch.setitem(strategies.STRATEGIES, "buy_and_hold_single_instrument",
                        replace(strategies.STRATEGIES["buy_and_hold_single_instrument"], track=track))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database=db)))
    assert_conflict(db, lambda: ledger.portfolio_plan_tracking(CUSTOMER, document["_id"], request, {"_id": OWNER}))
    assert reached


@pytest.mark.parametrize("endpoint", ["performance", "recommendations", "plan-tracking"])
def test_portfolio_routes_preserve_publication_conflicts_as_http_409(history_db, monkeypatch, endpoint):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from auth import current_user

    db = history_db
    source = add_source(db, ISIN, LISTING, "old")
    document = portfolio(source)
    save_plan(db, document)
    monkeypatch.setattr(ledger, "require_portfolio", lambda *args: document)
    app = FastAPI()
    app.state.database = db
    app.include_router(ledger.router)
    app.include_router(performance.router)
    app.include_router(recommendations.router)
    app.dependency_overrides[current_user] = lambda: {"_id": OWNER}

    @app.exception_handler(md.DataError)
    async def data_error(request, error):
        return JSONResponse(status_code=error.status, content={"detail": str(error)})

    reached = []

    def publish_replacement():
        reached.append(True)
        assert_leased(db, "old")
        publish(db, "new", order=1, close="150")

    if endpoint == "performance":
        original = performance.RiskRatios.values

        def race(ratios):
            result = original(ratios)
            if not reached:
                publish_replacement()
            return result

        monkeypatch.setattr(performance.RiskRatios, "values", race)
    elif endpoint == "recommendations":
        original = recommendations.compute_actions

        def race(*args, **kwargs):
            result = original(*args, **kwargs)
            publish_replacement()
            return result

        monkeypatch.setattr(recommendations, "compute_actions", race)
    else:
        def race(db, plan, snapshot, today):
            publish_replacement()
            return {"equity_eur": snapshot["equity_eur"]}

        monkeypatch.setattr(plan_tracking, "track_plan", race)
    with TestClient(app) as client:
        response = client.get(f"/customers/{CUSTOMER}/portfolios/{document['_id']}/{endpoint}")
    assert response.status_code == 409, response.text
    assert reached
    assert_released(db)


def test_plan_tracking_success_releases_all_sources(history_db, monkeypatch):
    db = history_db
    holding = add_source(db, ISIN, LISTING, "holding-current")
    add_source(db, SECOND_ISIN, SECOND_LISTING, "strategy-current")
    document = portfolio(holding)
    save_plan(db, document, target=SECOND_ISIN)
    monkeypatch.setattr(ledger, "require_portfolio", lambda *args: document)
    reached = []

    def track(db, plan, snapshot, today):
        reached.append(True)
        assert_leased(db, "holding-current", "strategy-current")
        return {"equity_eur": snapshot["equity_eur"], "valuation_date": today.isoformat()}

    monkeypatch.setattr(plan_tracking, "track_plan", track)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database=db)))
    result = ledger.portfolio_plan_tracking(CUSTOMER, document["_id"], request, {"_id": OWNER})
    assert result["equity_eur"] == "1899.00"
    assert reached
    assert_released(db)
