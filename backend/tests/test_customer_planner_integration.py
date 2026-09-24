"""Opt-in disposable real Mongo preview persistence and concurrent atomic saves."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from fastapi.testclient import TestClient

import goal_strategies as gs
import market_data as md
from goal_simulation import add_months
from main import create_app
from test_customer_planner import calculate, save, selection, target
from test_goal_planner import history, login, seed_history, seed_profile
from test_market_data import NOW
from test_market_data_integration import environment


def test_real_mongo_preview_atomic_save_expiration_and_durable_snapshots(environment, monkeypatch):
    settings, db = environment
    # Unlike mongomock, Mongo's TTL monitor has a real wall clock. Freeze API
    # time near it instead of persisting already-expired historical fixtures.
    now = datetime.now(timezone.utc)
    # Existing statistics cache datetimes are BSON millisecond precision.
    now = now.replace(microsecond=(now.microsecond // 1000) * 1000)
    monkeypatch.setattr(md, "utcnow", lambda: now)
    monkeypatch.setattr("main.enqueue_etf_import", Mock())
    weeks = (now.date() - NOW.date()).days // 7
    rows = history()
    for row in rows:
        row["date"] = (datetime.fromisoformat(row["date"]).date() + timedelta(weeks=weeks)).isoformat() + "T00:00:00+0000"
    seed_history(db, rows)
    db.quotes.update_many({}, {"$set": {"collected_at": now}})
    seed_profile(db)
    goal_date = add_months(now.date(), 120).isoformat()
    with TestClient(create_app(settings)) as client:
        login(client)
        _, goal, url = target(client, goal_date=goal_date)
        preview = calculate(client, url)
        expected = client.post("/goals/simulate", json={**selection(), "goal_eur": "100000", "goal_date": goal_date})
        assert expected.status_code == 200, expected.text
        assert preview["result"] == expected.json()
        assert client.get(url + "/simulations").json()["total"] == 0
        with ThreadPoolExecutor(max_workers=8) as pool:
            reports = list(pool.map(lambda _: save(client, url, preview), range(16)))
        saved = reports[0]
        assert all(report == saved for report in reports)
        assert saved["result"] == preview["result"]
        assert db.planner_simulations.count_documents({}) == 1
        original = db.planner_simulations.find_one({"_id": saved["id"]})
        assert "expires_at" not in original
        assert db.planner_simulations.index_information()["planner_preview_expiry"]["expireAfterSeconds"] == 0
        second = calculate(client, url, starting_capital_eur="20000", reliability=0.95)
        saved_second = save(client, url, second)
        expired = calculate(client, url)
        db.planner_simulations.update_one({"_id": expired["id"]}, {"$set": {"expires_at": now - timedelta(hours=1)}})
        response = client.post(url + f"/simulations/{expired['id']}/save")
        assert response.status_code in (404, 410)
        assert "expired" in response.json()["detail"]
        assert client.get(url + "/simulations").json()["total"] == 2
        assert db.planner_simulations.find_one({"_id": saved["id"]}) == original
        changes = {"name": "Updated goal", "goal_eur": "250000.50", "goal_date": add_months(now.date(), 60).isoformat()}
        response = client.patch(url, json=changes)
        assert response.status_code == 200, response.text
        updated_goal = {**goal, **changes}
        assert response.json() == updated_goal
        assert db.planner_simulations.find_one({"_id": saved["id"]}) == original
        current = calculate(client, url)
        assert current["inputs"]["goal_eur"] == changes["goal_eur"]
        assert current["inputs"]["goal_date"] == changes["goal_date"]
    # Reopen with another MongoClient, no current source or registry definition.
    for name in ("quotes", "instruments", "instrument_statistics", "instrument_state"):
        db[name].delete_many({})
    monkeypatch.delitem(gs.STRATEGIES, selection()["strategy"]["id"])
    with TestClient(create_app(settings)) as reopened:
        login(reopened)
        assert reopened.get(url).json() == updated_goal
        for report in (saved, saved_second):
            assert reopened.get(url + f"/simulations/{report['id']}").json() == report
            assert save(reopened, url, report) == report
        page = reopened.get(url + "/simulations", params={"limit": 1, "offset": 1}).json()
        assert page["total"] == 2 and page["items"][0]["id"] == saved["id"]
