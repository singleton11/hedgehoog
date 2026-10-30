"""UTC quote-date bounds are identical for equivalent offset-aware instants."""
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from auth import current_user
from instrument_routes import router
from manual_splits import read_splits
import market_data as md
import return_statistics as rs
from test_latest_history_contract import ISIN, LISTING, history_db, publish


@pytest.mark.parametrize("timestamp,days", [
    ("2025-01-08T00:30:00+14:00", ["2025-01-06", "2025-01-07"]),
    ("2025-01-07T23:30:00-12:00", ["2025-01-06", "2025-01-07", "2025-01-08"]),
])
def test_latest_readers_bound_utc_dates(history_db, timestamp, days):
    db = history_db
    publish(db, "latest")
    cutoff = datetime.fromisoformat(timestamp)
    db.split_snapshots.update_one({"_id": "latest"}, {"$set": {"events": [
        {"date": "2025-01-07", "factor": "2"}, {"date": "2025-01-08", "factor": "3"},
    ]}})
    assert [row["date"] for row in md.read_quotes(db, ISIN, as_of=cutoff)["items"]] == days
    assert md.quote_dates(db, ISIN, LISTING, cutoff) == days
    bounds = md.saved_history_bounds(db, [{"_id": ISIN, "listing": LISTING,
                                          "provider": "yfinance", "job_id": "latest"}], cutoff)
    assert bounds[ISIN]["last_date"] == days[-1]
    statistics, _ = rs.refresh_statistics(db, ISIN, as_of=cutoff)
    assert statistics["quote_count"] == len(days)
    assert statistics["last_date"] == days[-1]
    assert sum(map(len, rs.instrument_return_runs(db, ISIN, as_of=cutoff))) == len(days) - 1
    assert read_splits(db, ISIN, cutoff)["provider"][-1]["date"] == days[-1]


@pytest.mark.parametrize("timestamp,total", [
    ("2025-01-08T00:30:00+14:00", 2), ("2025-01-07T23:30:00-12:00", 3),
])
def test_quotes_http_uses_utc_quote_date(history_db, timestamp, total):
    publish(history_db, "latest")
    app = FastAPI()
    app.state.database = history_db
    app.include_router(router)
    app.dependency_overrides[current_user] = lambda: {"_id": "synthetic-user"}

    @app.exception_handler(md.DataError)
    async def data_error(request, error):
        return JSONResponse(status_code=error.status, content={"detail": str(error)})

    with TestClient(app) as client:
        result = client.get(f"/instruments/{ISIN}/quotes", params={"as_of": timestamp})
        assert result.status_code == 200
        assert result.json()["total"] == total
        assert result.json()["as_of"].endswith("Z")
        assert client.get(f"/instruments/{ISIN}/quotes", params={"as_of": "2025-01-07T12:00:00"}).status_code == 422
