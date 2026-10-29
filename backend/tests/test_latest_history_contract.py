"""Latest-only Yahoo reader contract; no provider, credentials or live MongoDB."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

mongomock = pytest.importorskip("mongomock")

import market_data as md
import return_statistics as rs
from config import TaskSettings
from goal_calibration import calibrate_instrument
from goal_planner import GoalRequest, simulate
from goal_strategies import BuyAndHoldParameters, STRATEGIES
from indices import index_detail
from instrument_routes import QuotesResponse
from portfolio_tracker import _cached_quote_rows, acquired_instrument, quote_rows, valuation
from split_adjustments import load_split_snapshot

ISIN = "IE00B3YLTY66"
LISTING = {"symbol": "SPYI.DE", "exchange": "XETR"}
NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
CUTOFF = datetime(2025, 1, 7, 12, tzinfo=timezone.utc)


@pytest.fixture
def history_db(monkeypatch):
    db = mongomock.MongoClient(tz_aware=True).latest_history_contract
    monkeypatch.setattr(md, "utcnow", lambda: NOW)
    db.etfs.insert_one({"isin": ISIN, "name": "Example ETF", "ticker": "SPYI",
                        "inception_date": "2024-01-01", "import_date": NOW})
    db.instrument_state.insert_one({"_id": ISIN, "listing": LISTING})
    return db


def publish(db, generation, *, order=0, close="100", listing=None, isin=ISIN):
    listing = listing or LISTING
    collected = NOW + timedelta(seconds=order)
    db.acquisition_jobs.insert_one({
        "_id": generation, "instrument_id": isin, "listing": listing,
        "provider": "yfinance", "published": True, "requested_at": collected,
        "published_at": collected, "status": "succeeded", "active": False,
    })
    db.instruments.insert_one({
        "instrument_id": isin, **listing, "provider": "yfinance", "published": True,
        "job_id": generation, "collected_at": collected,
        "collection_date": collected.date().isoformat(), "data": {"name": generation},
    })
    for day, value in (("2025-01-06", "100"), ("2025-01-07", close), ("2025-01-08", "999")):
        db.quotes.insert_one({
            "instrument_id": isin, **listing, "provider": "yfinance", "job_id": generation,
            "date": day, "collected_at": collected, "collection_date": collected.date().isoformat(),
            "data": {"date": day, "close": value, "price_currency": "EUR",
                     "price_basis": "provider_split_adjusted"},
        })
    db.split_snapshots.insert_one({
        "_id": generation, "job_id": generation, "instrument_id": isin, **listing,
        "provider": "yfinance", "collected_at": collected, "events": [],
        "date_from": "2024-01-01", "date_to": "2026-10-20",
    })


def assert_conflict(call):
    with pytest.raises(md.DataError) as error:
        call()
    assert error.value.status == 409


def test_as_of_bounds_dates_not_publication_or_collection_times(history_db):
    db = history_db
    publish(db, "old", close="200")
    publish(db, "latest", order=1, close="110")
    # Newer failures and another provider must never replace the complete vintage.
    db.acquisition_jobs.insert_many([
        {"_id": "failed", "instrument_id": ISIN, "listing": LISTING, "provider": "yfinance",
         "published": False, "requested_at": NOW + timedelta(days=1)},
        {"_id": "legacy", "instrument_id": ISIN, "listing": LISTING, "provider": "marketstack",
         "published": True, "requested_at": NOW + timedelta(days=2)},
    ])
    result = md.read_quotes(db, ISIN, as_of=CUTOFF, limit=1, offset=1)
    assert result["generation"] == "latest"
    assert result["total"] == 2
    assert [(row["date"], row["close"]) for row in result["items"]] == [("2025-01-07", "110")]
    assert md.published_history(db, ISIN, LISTING, CUTOFF)["_id"] == "latest"
    assert md.quote_dates(db, ISIN, LISTING, CUTOFF) == ["2025-01-06", "2025-01-07"]
    detail = md.instrument_detail(db, TaskSettings(_env_file=None), ISIN, CUTOFF)
    assert detail["profile"]["data"]["name"] == "latest"
    assert detail["summary"]["last_date"] == "2025-01-07"
    assert load_split_snapshot(db, ISIN, LISTING, CUTOFF)["_id"] == "latest"
    assert not db.acquisition_jobs.find_one({"_id": "latest"}).get("reader_leases")


@pytest.mark.parametrize("cursor", ["old", "legacy", "missing"])
def test_superseded_and_legacy_cursors_fail_every_reader(history_db, cursor):
    db = history_db
    publish(db, "old")
    publish(db, "latest", order=1)
    calls = [
        lambda: md.published_history(db, ISIN, LISTING, CUTOFF, cursor),
        lambda: md.observation_filter(db, ISIN, LISTING, CUTOFF, cursor),
        lambda: md.read_quotes(db, ISIN, as_of=CUTOFF, generation=cursor),
        lambda: md.quote_dates(db, ISIN, LISTING, CUTOFF, cursor),
        lambda: rs.refresh_statistics(db, ISIN, generation=cursor, as_of=CUTOFF),
        lambda: rs.instrument_statistics(db, ISIN, generation=cursor, as_of=CUTOFF),
        lambda: load_split_snapshot(db, ISIN, LISTING, CUTOFF, cursor),
        lambda: md.saved_history_bounds(db, [{"_id": ISIN, "listing": LISTING,
                                             "provider": "yfinance", "job_id": cursor}], CUTOFF),
    ]
    for call in calls:
        assert_conflict(call)


@pytest.mark.parametrize("retention_state", ["retiring", "pruned"])
def test_unavailable_latest_never_resurrects_previous_generation(history_db, retention_state):
    db = history_db
    publish(db, "old")
    publish(db, "latest", order=1)
    db.acquisition_jobs.update_one({"_id": "latest"}, {"$set": {"retention_state": retention_state}})
    for call in (lambda: md.published_history(db, ISIN, LISTING),
                 lambda: md.read_quotes(db, ISIN),
                 lambda: rs.refresh_statistics(db, ISIN),
                 lambda: md.acquired_instruments(db), lambda: md.acquired_universe(db)):
        assert_conflict(call)


@pytest.mark.parametrize("provider", [None, "marketstack"])
def test_untyped_and_marketstack_rows_are_not_readable(history_db, provider):
    db = history_db
    metadata = {} if provider is None else {"provider": provider}
    db.instruments.insert_one({"instrument_id": ISIN, **LISTING, **metadata,
                               "collected_at": NOW, "data": {"name": "Legacy"}})
    db.quotes.insert_one({"instrument_id": ISIN, **LISTING, **metadata,
                          "date": "2025-01-06", "collected_at": NOW,
                          "data": {"close": "100", "price_currency": "EUR"}})
    db.manual_splits.insert_one({"instrument_id": ISIN, **LISTING, "date": "2025-01-06", "factor": "2"})
    instrument = {"instrument_id": ISIN, **LISTING}
    response = QuotesResponse.model_validate(md.read_quotes(db, ISIN))
    assert response.generation is None
    assert "Yahoo acquisition" in response.warning
    assert response.items == []
    assert md.quote_dates(db, ISIN, LISTING) == []
    assert md.acquired_instruments(db)["total"] == 0
    assert md.acquired_universe(db)["total"] == 0
    with pytest.raises(md.DataError, match="Yahoo acquisition"):
        STRATEGIES["buy_and_hold_single_instrument"].resolve_parameters({"instrument": ISIN}, db)
    assert quote_rows(db, instrument, CUTOFF) == []
    assert load_split_snapshot(db, ISIN, LISTING, CUTOFF) is None
    statistics = rs.instrument_statistics(db, ISIN, as_of=CUTOFF)
    assert statistics["quote_count"] == 0
    assert not statistics["eligible"]
    assert "Yahoo acquisition" in statistics["ineligible_reason"]
    with pytest.raises(md.DataError, match="Yahoo acquisition"):
        acquired_instrument(db, ISIN, CUTOFF)
    assert_conflict(lambda: md.read_quotes(db, ISIN, generation="legacy"))
    assert_conflict(lambda: md.quote_dates(db, ISIN, LISTING, generation="legacy"))


def test_listing_change_cannot_select_older_listing_vintage(history_db):
    db = history_db
    publish(db, "old")
    new_listing = {**LISTING, "symbol": "NEW.DE"}
    publish(db, "latest", order=1, listing=new_listing)
    instrument = {"instrument_id": ISIN, **LISTING}
    for call in (lambda: md.published_history(db, ISIN, LISTING),
                 lambda: md.read_quotes(db, ISIN),
                 lambda: rs.refresh_statistics(db, ISIN),
                 lambda: quote_rows(db, instrument, CUTOFF),
                 lambda: load_split_snapshot(db, ISIN, LISTING, CUTOFF)):
        assert_conflict(call)
    db.instrument_state.update_one({"_id": ISIN}, {"$set": {"listing": new_listing}})
    assert md.read_quotes(db, ISIN)["generation"] == "latest"
    assert_conflict(lambda: md.read_quotes(db, ISIN, generation="old"))


def test_quotes_api_serializes_no_history_and_rejects_legacy_cursor(history_db):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from auth import current_user
    from instrument_routes import router

    app = FastAPI()
    app.state.database = history_db
    app.include_router(router)
    app.dependency_overrides[current_user] = lambda: {"_id": "user"}

    @app.exception_handler(md.DataError)
    async def data_error(request, error):
        return JSONResponse(status_code=error.status, content={"detail": str(error)})

    with TestClient(app) as client:
        response = client.get(f"/instruments/{ISIN}/quotes")
        assert response.status_code == 200
        assert response.json()["generation"] is None
        assert "Yahoo acquisition" in response.json()["warning"]
        assert client.get(f"/instruments/{ISIN}/quotes?generation=legacy").status_code == 409


def test_statistics_and_portfolio_use_latest_prices_at_historical_origin(history_db):
    db = history_db
    publish(db, "old", close="200")
    before, _ = rs.refresh_statistics(db, ISIN, as_of=CUTOFF)
    publish(db, "latest", order=1, close="110")
    after = rs.instrument_statistics(db, ISIN, as_of=CUTOFF, point_in_time=True)
    assert before["annualized_mean_log_return"] != after["annualized_mean_log_return"]
    assert after["quote_count"] == 2
    assert after["generation"] == "latest"
    assert any("not strict point-in-time" in warning for warning in after["warnings"])
    instrument = {"instrument_id": ISIN, "name": "Example ETF", "currency": "EUR", **LISTING}
    assert [row["close"] for row in quote_rows(db, instrument, CUTOFF)] == ["100", "110"]
    document = {"_id": "portfolio", "customer_id": "customer", "name": "Portfolio", "plan_id": None,
                "currency": "EUR", "created_at": NOW, "revision": 0, "transactions": [
                    {"type": "deposit", "date": "2025-01-06", "sequence": 1, "amount_eur": "1000"},
                    {"type": "buy", "date": "2025-01-06", "sequence": 2, "quantity": "1",
                     "price_eur": "100", "commission_eur": "0", "instrument": instrument},
                ]}
    result = valuation(db, document, now=CUTOFF)
    assert result["positions_value_eur"] == "110.00"
    assert result["positions"][0]["latest_quote_date"] == "2025-01-07"
    assert rs.instrument_return_runs(db, ISIN, as_of=CUTOFF)


def test_portfolio_request_cache_rejects_superseded_generation(history_db):
    db = history_db
    publish(db, "old")
    instrument = {"instrument_id": ISIN, **LISTING}
    cache = {}
    assert _cached_quote_rows(db, instrument, CUTOFF, cache)
    publish(db, "latest", order=1)
    assert_conflict(lambda: _cached_quote_rows(db, instrument, CUTOFF, cache))


def test_statistics_cache_is_generation_pinned(history_db):
    db = history_db
    publish(db, "old", close="200")
    before, _ = rs.refresh_statistics(db, ISIN, as_of=CUTOFF)
    assert db.instrument_statistics.find_one({"_id": ISIN})["signature"]["generation"] == "old"
    publish(db, "latest", order=1, close="110")
    after, _ = rs.refresh_statistics(db, ISIN, as_of=CUTOFF)
    assert before["daily_mean_log_return"] != after["daily_mean_log_return"]
    assert db.instrument_statistics.find_one({"_id": ISIN})["signature"]["generation"] == "latest"


def test_statistics_reader_covers_multi_query_calculation_and_detects_publication(history_db, monkeypatch):
    db = history_db
    publish(db, "old")
    calculate = rs.calculate_statistics

    def racing_calculation(*args, **kwargs):
        assert db.acquisition_jobs.find_one({"_id": "old"}).get("reader_leases")
        publish(db, "latest", order=1)
        return calculate(*args, **kwargs)

    monkeypatch.setattr(rs, "calculate_statistics", racing_calculation)
    assert_conflict(lambda: rs.refresh_statistics(db, ISIN, as_of=CUTOFF))
    assert not db.acquisition_jobs.find_one({"_id": "old"}).get("reader_leases")


def test_quote_page_detects_generation_change_during_payload_read(history_db, monkeypatch):
    db = history_db
    publish(db, "old")
    aggregate = db.quotes.aggregate

    def racing_aggregate(*args, **kwargs):
        rows = list(aggregate(*args, **kwargs))
        assert db.acquisition_jobs.find_one({"_id": "old"}).get("reader_leases")
        publish(db, "latest", order=1)
        return iter(rows)

    monkeypatch.setattr(db.quotes, "aggregate", racing_aggregate)
    assert_conflict(lambda: md.read_quotes(db, ISIN, as_of=CUTOFF))


def test_saved_bounds_detects_publication_after_endpoint_probe(history_db, monkeypatch):
    db = history_db
    publish(db, "old")
    aggregate = db.instruments.aggregate

    def racing_probe(*args, **kwargs):
        rows = list(aggregate(*args, **kwargs))
        assert db.acquisition_jobs.find_one({"_id": "old"}).get("reader_leases")
        publish(db, "latest", order=1)
        return iter(rows)

    monkeypatch.setattr(db.instruments, "aggregate", racing_probe)
    observations = [{"_id": ISIN, "listing": LISTING, "provider": "yfinance", "job_id": "old"}]
    assert_conflict(lambda: md.saved_history_bounds(db, observations, CUTOFF))


def test_reader_lease_expiry_rejects_completed_statistics(history_db, monkeypatch):
    db = history_db
    publish(db, "latest")
    calculate = rs.calculate_statistics

    def expired_calculation(*args, **kwargs):
        result = calculate(*args, **kwargs)
        monkeypatch.setattr(md, "utcnow", lambda: NOW + timedelta(minutes=6))
        return result

    monkeypatch.setattr(rs, "calculate_statistics", expired_calculation)
    assert_conflict(lambda: rs.refresh_statistics(db, ISIN, as_of=CUTOFF))
    assert not db.acquisition_jobs.find_one({"_id": "latest"}).get("reader_leases")


def test_saved_bounds_use_latest_only_and_quote_date_cutoff(history_db):
    db = history_db
    publish(db, "old")
    publish(db, "latest", order=1)
    observations = [{"_id": ISIN, "listing": LISTING, "provider": "yfinance", "job_id": "latest"}]
    bounds = md.saved_history_bounds(db, observations, CUTOFF)[ISIN]
    assert (bounds["first_date"], bounds["last_date"], bounds["generation"]) == (
        "2025-01-06", "2025-01-07", "latest")
    assert md.acquired_instruments(db)["items"][0]["quote_count"] == 3
    assert md.acquired_instruments(db, min_history_years=5)["total"] == 0
    assert md.acquired_instruments(db, hide_negative_returns=True)["total"] == 0


def test_calibration_holds_latest_lease_through_evaluation(history_db, monkeypatch):
    db = history_db
    publish(db, "latest")
    # Even a future collection timestamp is not a quote-date filter.
    db.quotes.update_many({}, {"$set": {"collected_at": NOW + timedelta(days=1)}})
    seen = []

    def evaluate(rows, body, **kwargs):
        assert db.acquisition_jobs.find_one({"_id": "latest"}).get("reader_leases")
        seen.extend(row["date"] for row in rows)
        return {"warnings": []}

    result = calibrate_instrument(db, ISIN, SimpleNamespace(seed=3), evaluate=evaluate)
    assert seen == ["2025-01-06", "2025-01-07", "2025-01-08"]
    assert result["history_fingerprint"]


def test_index_detail_keeps_profile_and_quotes_on_one_vintage(history_db):
    db = history_db
    identifier = "idx-example"
    listing = {"symbol": "^EXAMPLE", "exchange": "YAHOO"}
    db.indices.insert_one({"instrument_id": identifier, "symbol": "^EXAMPLE", "name": "Example index",
                           "provenance": {"source": "manual"}, "provider": "yfinance"})
    db.instrument_state.insert_one({"_id": identifier, "listing": listing})
    publish(db, "old-index", listing=listing, isin=identifier)
    publish(db, "latest-index", order=1, listing=listing, isin=identifier)
    result = index_detail(db, identifier)
    assert result["generation"] == "latest-index"
    assert result["provenance"]["verified_generation"] == "latest-index"
    assert result["profile"]["data"]["name"] == "latest-index"


def test_simulation_api_warns_latest_history_is_not_point_in_time(history_db, monkeypatch):
    db = history_db
    publish(db, "latest")
    body = GoalRequest(goal_eur="10000", starting_capital_eur="100", goal_date="2026-12-31",
                       reliability=0.8, as_of=CUTOFF,
                       strategy={"id": "buy_and_hold_single_instrument", "parameters": {"instrument": ISIN}})

    def simulation(parameters, body, db, today):
        assert db.acquisition_jobs.find_one({"_id": "latest"}).get("reader_leases")
        return {"warnings": []}

    strategy = SimpleNamespace(resolve_parameters=lambda values, db: BuyAndHoldParameters(**values),
                               simulate=simulation)
    monkeypatch.setattr("goal_planner.get_strategy", lambda value: strategy)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database=db)))
    result = simulate(body, request, {"_id": "user"})
    assert result["history_contract"] == "latest_complete_yahoo"
    assert any("not strict point-in-time" in warning for warning in result["warnings"])
