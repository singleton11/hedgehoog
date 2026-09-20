"""Opt-in real Mongo acquisition -> derived statistics -> goal API, no live provider."""

from unittest.mock import Mock
from datetime import timedelta

import httpx
from fastapi.testclient import TestClient

import market_data as md
from main import create_app
from marketstack import MarketstackClient
from test_goal_planner import history, request_body
from test_market_data_integration import ISIN, LISTING, environment


def test_real_mongo_acquisition_statistics_and_simulation(environment, monkeypatch):
    settings, db = environment
    calls = []
    rows = history()

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("tickerinfo"):
            return httpx.Response(200, json={"data": {"ticker": LISTING["symbol"], "name": "Synthetic ETF"}})
        return httpx.Response(200, json={"pagination": {"total": len(rows), "limit": 1000}, "data": rows})

    def factory(key, before_request):
        return MarketstackClient(key, before_request=before_request, transport=httpx.MockTransport(respond))

    job = md.request_acquisition(db, settings, ISIN, LISTING, "update", "synthetic-user", Mock())
    assert md.run_acquisition(db, settings, job["id"], factory)["status"] == "succeeded"
    saved = db.instrument_statistics.find_one({"_id": ISIN})
    assert saved["statistics"]["return_count"] == 80
    assert len(calls) == 2
    original_quotes = list(db.quotes.find())
    monkeypatch.setattr("main.enqueue_etf_import", Mock())
    with TestClient(create_app(settings)) as client:
        assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
        stats = client.get(f"/instruments/{ISIN}/statistics")
        assert stats.status_code == 200
        assert stats.json()["eligible"] and stats.json()["price_currency"] == "EUR"
        assert stats.json()["computed_at"] == md.public(saved["statistics"]["computed_at"])
        acquired = client.get("/instruments/acquired").json()["items"][0]
        for key in ("annualized_mean_log_return", "annualized_volatility"):
            assert acquired[key] == stats.json()[key]
        response = client.post("/goals/simulate", json=request_body())
        assert response.status_code == 200, response.text
        assert response.json()["success_probability"] >= 0.9
        assert response.json()["simulation_paths"] == 10000
        assert client.post("/goals/simulate", json=request_body()).json() == response.json()
        # Cache invalidates for a newer observation of an already stored day.
        revised = original_quotes[-1]
        revised.pop("_id")
        revised["job_id"] = "revision"
        later = md.utcnow() + timedelta(seconds=1)
        revised["collected_at"] = later
        monkeypatch.setattr(md, "utcnow", lambda: later)
        revised["data"]["adj_close"] = "200"
        db.quotes.insert_one(revised)
        refreshed = client.get(f"/instruments/{ISIN}/statistics").json()
        assert refreshed["quote_count"] == 81
        assert refreshed["daily_mean_log_return"] != stats.json()["daily_mean_log_return"]
        acquired = client.get("/instruments/acquired").json()["items"][0]
        for key in ("annualized_mean_log_return", "annualized_volatility"):
            assert acquired[key] == refreshed[key]
    assert len(calls) == 2
    assert md.budget_status(db, settings)["used"] == 2
