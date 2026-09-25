"""Opt-in real Mongo acquisition -> derived statistics -> goal API, no live provider."""

from unittest.mock import Mock
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

import market_data as md
from main import create_app
from marketstack import MarketstackClient
from test_goal_planner import history, request_body
from test_market_data import bar
from test_market_data_integration import ISIN, LISTING, environment


def test_real_mongo_acquisition_statistics_and_simulation(environment, monkeypatch):
    settings, db = environment
    calls = []
    rows = [{**bar("2025-09-15", "90"), "price_currency": "EUR"}] + history()
    split_date = rows[40]["date"][:10]
    for row in rows[40:]:
        row["close"] = str(Decimal(row["close"]) / 25)
    # Provider adjusted closes already include the split: never adjust them twice.

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("tickerinfo"):
            return httpx.Response(200, json={"data": {"ticker": LISTING["symbol"], "name": "Synthetic ETF"}})
        if request.url.path.endswith("splits"):
            return httpx.Response(200, json={"pagination": {"total": 1, "limit": 1000}, "data": [
                {"symbol": LISTING["symbol"], "date": split_date, "split_factor": 25}]})
        return httpx.Response(200, json={"pagination": {"total": len(rows), "limit": 1000}, "data": rows})

    def factory(key, before_request):
        return MarketstackClient(key, before_request=before_request, transport=httpx.MockTransport(respond))

    job = md.request_acquisition(db, settings, ISIN, LISTING, "update", "synthetic-user", Mock())
    assert md.run_acquisition(db, settings, job["id"], factory)["status"] == "succeeded"
    assert db.instruments.find_one({"instrument_id": ISIN}) is not None
    saved = db.instrument_statistics.find_one({"_id": ISIN})
    assert saved["statistics"]["return_count"] == 80
    assert len(calls) == 3
    assert saved["statistics"]["price_basis"] == "split_adjusted_close"
    assert db.raw_split_pages.count_documents({}) == db.split_snapshots.count_documents({}) == 1
    assert [item["close"] for item in md.read_quotes(db, ISIN)["items"]] == [row["close"] for row in rows]
    original_quotes = list(db.quotes.find())
    monkeypatch.setattr("main.enqueue_etf_import", Mock())
    with TestClient(create_app(settings)) as client:
        assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
        stats = client.get(f"/instruments/{ISIN}/statistics")
        assert stats.status_code == 200
        assert stats.json()["eligible"] and stats.json()["price_currency"] == "EUR"
        assert stats.json()["computed_at"] == md.public(saved["statistics"]["computed_at"])
        acquired = client.get("/instruments/acquired").json()["items"][0]
        for key in ("annualized_mean_log_return", "annualized_volatility", "historical_year_return",
                    "historical_year_start_date", "historical_year_end_date", "historical_year_price_basis"):
            assert acquired[key] == stats.json()[key]
        assert acquired["historical_year_return"] == pytest.approx(float(rows[-1]["adj_close"]) / 90 - 1)
        response = client.post("/goals/simulate", json=request_body())
        assert response.status_code == 200, response.text
        assert response.json()["success_probability"] >= 0.9
        assert response.json()["simulation_paths"] == 10000
        assert response.json()["strategy"] == request_body()["strategy"]
        assert response.json()["parameter_statistics"] == {"instrument": stats.json()}
        assert "statistics" not in response.json() and "instrument_id" not in response.json()
        assert client.post("/goals/simulate", json=request_body()).json() == response.json()
        # Cache invalidates for a newer observation of an already stored day.
        revised = original_quotes[-1]
        revised.pop("_id")
        revised["job_id"] = "revision"
        later = md.utcnow() + timedelta(seconds=1)
        revised["collected_at"] = later
        monkeypatch.setattr(md, "utcnow", lambda: later)
        revised["data"]["close"] = "8"  # 200 in the split-normalized series.
        db.quotes.insert_one(revised)
        refreshed = client.get(f"/instruments/{ISIN}/statistics").json()
        assert refreshed["quote_count"] == 82
        assert refreshed["daily_mean_log_return"] != stats.json()["daily_mean_log_return"]
        acquired = client.get("/instruments/acquired").json()["items"][0]
        for key in ("annualized_mean_log_return", "annualized_volatility", "historical_year_return",
                    "historical_year_start_date", "historical_year_end_date", "historical_year_price_basis"):
            assert acquired[key] == refreshed[key]
        assert acquired["historical_year_return"] == pytest.approx(200 / 90 - 1)
    assert len(calls) == 3
    assert md.budget_status(db, settings)["used"] == 3
