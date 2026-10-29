"""Offline CLI demonstration of acquired-return filtering with synthetic data."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import mongomock
import market_data as md


def main():
    now = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
    md.utcnow = lambda: now
    db = mongomock.MongoClient().returns_demo
    for index, (name, prices) in enumerate([
        ("Positive returns", [90, 100, 110, 121]),
        ("Negative statistical return", [90, 120, 110, 100]),
        ("Negative historical return", [150, 100, 110, 121]),
        ("Zero returns", [100, 100, 100, 100]),
        ("Profile only", []),
    ]):
        isin = f"IE{index:010d}"
        listing = {"symbol": f"DEMO{index}", "exchange": "XETR"}
        db.instruments.insert_one({"instrument_id": isin, **listing, "collected_at": now})
        db.instrument_state.insert_one({"_id": isin, "listing": listing})
        db.etfs.insert_one({"isin": isin, "name": name})
        for day, close in zip(["2025-09-16", "2026-09-14", "2026-09-15", "2026-09-16"], prices):
            db.quotes.insert_one({"instrument_id": isin, **listing, "date": day, "collected_at": now,
                                  "data": {"date": day, "close": close, "price_currency": "EUR",
                                           "price_basis": "provider_split_adjusted"}})
    all_rows = md.acquired_instruments(db)
    pages = [md.acquired_instruments(db, limit=1, offset=offset, hide_negative_returns=True)
             for offset in (0, 1, 2)]
    assert all_rows["total"] == 5
    assert all(page["total"] == 2 for page in pages)
    assert [item["name"] for page in pages for item in page["items"]] == ["Positive returns", "Zero returns"]
    assert md.acquired_universe(db)["total"] == 5
    result = {
        "scenario": "Synthetic saved ETF profiles: exact return-filtered pagination",
        "all_total": all_rows["total"],
        "all_rows": [{key: row[key] for key in ("name", "historical_year_return", "annualized_mean_log_return")}
                     for row in all_rows["items"]],
        "filtered_pages": [{"offset": page["offset"], "total": page["total"],
                            "names": [item["name"] for item in page["items"]]} for page in pages],
        "bulk_universe_total": 5,
        "limitations": "Offline mongomock demonstration; not real Mongo latency, browser integration or live product evidence.",
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
