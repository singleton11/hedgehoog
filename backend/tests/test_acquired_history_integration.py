"""Opt-in endpoint probes/executionStats on a disposable, unauthenticated Mongo.

These tests intentionally execute the actual correlated aggregation, not an
explained standalone find. They do not establish wall-clock acceptance targets.
"""

from datetime import datetime, timezone
import json
import os
from uuid import uuid4

from pymongo import MongoClient
import pytest

import market_data as md


@pytest.fixture
def history_db():
    uri = os.environ.get("TEST_MONGODB_URI")
    if not uri:
        pytest.skip("Not run: TEST_MONGODB_URI for disposable real Mongo is unavailable")
    name = "hedgehoog_history_test_" + uuid4().hex
    with MongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True) as mongo:
        db = mongo[name]
        try:
            md.ensure_indexes(db)
            yield db
        finally:
            mongo.drop_database(name)


def _lookup_stats(explanation):
    result = []

    def visit(value):
        if isinstance(value, dict):
            if "$lookup" in value and "totalKeysExamined" in value:
                result.append(value)
            for key, child in value.items():
                # Explain's command embeds the pipeline but contains no counters.
                if key != "command":
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(explanation)
    return result


@pytest.mark.parametrize("legacy", [False, True])
def test_correlated_endpoints_use_existing_indexes_in_both_directions(history_db, legacy):
    db = history_db
    listing = {"symbol": "$SHARED", "exchange": "XETR"}
    db.instruments.insert_one({"instrument_id": "carrier"})
    seeds = []
    rows = []
    for index in range(4):
        isin = f"IE{index:010d}"
        seed = {"instrument_id": isin, **listing,
                "generation": "legacy" if legacy else f"pinned-{index}"}
        if not legacy:
            seed["provider"] = "yfinance"
        seeds.append(seed)
        # Two sparse endpoints, one date, no dates, or a denser history.
        days = ["2000-01-01", "2030-01-01"] if index in (0, 3) else ["2020-01-01"] if index == 1 else []
        if index == 3:
            days += [f"2010-{month:02d}-{day:02d}" for month in range(1, 13) for day in range(1, 29)]
        for day in days:
            row = {"instrument_id": isin, **listing, "date": day, "job_id": f"pinned-{index}"}
            if not legacy:
                row["provider"] = "yfinance"
            rows.append(row)
        # Residual provider/listing predicates and many superseded generations
        # must not extend extrema, even with index access. Legacy necessarily
        # traverses some of these keys; do not assert constant key reads.
        for generation in range(80):
            for day in ("1900-01-01", "2100-01-01"):
                rows.append({"instrument_id": isin, **listing, "date": day,
                             "job_id": f"old-{index}-{generation}", "provider": "yfinance"})
        for symbol, exchange, provider, day in [
            ("OTHER", "XETR", "marketstack" if not legacy else "yfinance", "1800-01-01"),
            (listing["symbol"], "OTHER", "marketstack" if not legacy else "yfinance", "2200-01-01"),
        ]:
            rows.append({"instrument_id": isin, "symbol": symbol, "exchange": exchange,
                         "provider": provider, "job_id": f"pinned-{index}", "date": day})
    db.quotes.insert_many(rows)
    pipeline = md.history_bounds_pipeline(seeds)
    actual = list(db.instruments.aggregate(pipeline))
    extrema = [(row.get("first_date"), row.get("last_date")) for row in actual]
    assert extrema == [("2000-01-01", "2030-01-01"), ("2020-01-01", "2020-01-01"),
                       (None, None), ("2000-01-01", "2030-01-01")]
    explanation = db.command("explain", {"aggregate": "instruments", "pipeline": pipeline, "cursor": {}},
                             verbosity="executionStats")
    probes = _lookup_stats(explanation)
    assert len(probes) == 2, "Server must expose counters for the actual two correlated lookups"
    expected = "instrument_id_1_symbol_1_exchange_1_date_1_collected_at_-1" if legacy else "job_id_1_date_1"
    for probe in probes:
        assert probe["collectionScans"] == 0
        assert expected in probe["indexesUsed"]
        assert 0 < probe["totalKeysExamined"] < len(rows)
        assert probe["totalDocsExamined"] < len(rows)
    # Explain may hide inner plans on some supported servers. Counter evidence
    # alone cannot establish absence of a blocking inner sort; report that gap.
    # The carrier legitimately reads one instruments row via COLLSCAN. Only
    # lookup collectionScans describe the quotes access we need to exclude.
    encoded = json.dumps(explanation, default=str)
    assert '"stage": "SORT"' not in encoded
    print(json.dumps({"kind": "actual_correlated_lookup", "legacy": legacy,
                      "candidates": len(seeds), "internal_probes": 2 * len(seeds),
                      "endpoint_command_count": 1, "seeded_quotes": len(rows), "extrema": extrema,
                      "lookup_counters": [{key: probe.get(key) for key in (
                          "totalKeysExamined", "totalDocsExamined", "collectionScans", "indexesUsed")}
                          for probe in probes],
                      "limitation": "No wall-clock acceptance; inner sort visibility is server-dependent."}))


def test_literal_seeds_survive_profile_replacement_and_detect_missing_carrier(history_db):
    db = history_db
    listing = {"symbol": "ORIGINAL", "exchange": "XETR"}
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    candidate = {"_id": "IE0000000001", "listing": listing, "provider": "yfinance", "job_id": "pinned"}
    db.instruments.insert_one({"instrument_id": candidate["_id"], "symbol": "CHANGED", "exchange": "OTHER"})
    db.acquisition_jobs.insert_one({"_id": "pinned", "instrument_id": candidate["_id"], "listing": listing,
                                    "published": True, "published_at": now, "provider": "yfinance"})
    db.quotes.insert_many([{"instrument_id": candidate["_id"], **listing, "job_id": "pinned",
                            "provider": "yfinance", "date": day} for day in ("2000-01-01", "2030-01-01")])
    assert md.saved_history_bounds(db, [candidate], now)[candidate["_id"]]["first_date"] == "2000-01-01"
    db.instruments.delete_many({})
    assert md.saved_history_bounds(db, [], now) == {}
    with pytest.raises(md.DataError, match="inventory changed"):
        md.saved_history_bounds(db, [candidate], now)
