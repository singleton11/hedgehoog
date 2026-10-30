"""Opt-in disposable MongoDB scale, command-count and EXPLAIN evidence.

Run with TEST_MONGODB_URI pointing only at a disposable local server.
Synthetic metadata scale is realistic; quote volume/host contention is not.
"""
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from time import perf_counter
from uuid import uuid4

import pytest
from pymongo import MongoClient, monitoring

import market_data as md

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_MONGODB_URI"), reason="Disposable TEST_MONGODB_URI required")
NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


class Commands(monitoring.CommandListener):
    def __init__(self):
        self.calls = []

    def started(self, event):
        self.calls.append((event.command_name, event.command.get(event.command_name)))

    def succeeded(self, event):
        pass

    def failed(self, event):
        pass


@pytest.fixture(params=[40, 30000])
def inventory(request, monkeypatch):
    commands = Commands()
    client = MongoClient(os.environ["TEST_MONGODB_URI"], event_listeners=[commands], tz_aware=True)
    db = client[f"pagination_scale_{uuid4().hex}"]
    monkeypatch.setattr(md, "utcnow", lambda: NOW)
    count = request.param
    try:
        md.ensure_indexes(db)
        db.etfs.create_index([("isin", 1), ("import_date", 1)], unique=True)
        jobs, profiles, states, catalogues = [], [], [], []
        for index in range(count):
            isin, generation = f"IE{index:010d}", f"current-{index}"
            listing = {"symbol": isin, "exchange": "TEST"}
            jobs.append({"_id": generation, "instrument_id": isin, "listing": listing, "provider": "yfinance",
                         "published": True, "requested_at": NOW, "published_at": NOW, "status": "succeeded"})
            # Include a nontrivial provider payload: the client must never receive it.
            profiles.append({"instrument_id": isin, **listing, "provider": "yfinance", "job_id": generation,
                             "collected_at": NOW - timedelta(days=index // 2), "data": {"synthetic": "x" * 2048}})
            states.append({"_id": isin, "listing": listing})
            catalogues.append({"isin": isin, "name": f"Fund {index}", "ticker": isin, "import_date": "2026-10-30"})
        # More than one published generation per some ETFs; no profile fallback.
        for index in range(min(count, 11628)):
            jobs.append({**jobs[index], "_id": f"older-{index}", "requested_at": NOW - timedelta(days=1)})
        db.acquisition_jobs.insert_many(jobs)
        db.instruments.insert_many(profiles)
        db.instrument_state.insert_many(states)
        db.etfs.insert_many(catalogues)
        # Real daily history for every requested nonempty page, while the rest
        # of the large inventory is metadata-only. Do not simulate calculators.
        quotes = []
        for index in range(min(count, 40)):
            for day in range(366):
                when = (NOW - timedelta(days=366 - day)).date().isoformat()
                quotes.append({"instrument_id": states[index]["_id"], **states[index]["listing"],
                               "provider": "yfinance", "job_id": f"current-{index}", "date": when,
                               "collected_at": NOW, "data": {"date": when, "close": str(100 + day / 10),
                                                            "price_basis": "provider_split_adjusted"}})
        db.quotes.insert_many(quotes)
        commands.calls.clear()
        yield db, count, commands
    finally:
        client.drop_database(db.name)
        client.close()


def test_real_page_and_universe_bounded_payload_commands(inventory):
    db, count, commands = inventory
    measurements = []
    for limit, offset in [(1, 0), (20, 20), (20, count)]:
        commands.calls.clear()
        start = perf_counter()
        result = md.acquired_instruments(db, limit=limit, offset=offset)
        elapsed = perf_counter() - start
        assert result["total"] == count
        assert [row["instrument_id"] for row in result["items"]] == [f"IE{index:010d}" for index in range(offset, min(count, offset + limit))]
        calls = Counter(name for name, _ in commands.calls)
        # Outer list, batch-statistics and fallback-calculation readers each
        # acquire/release a lease; cache and legacy-summary writes add two more.
        assert calls["update"] <= 8 * len(result["items"])
        assert calls["find"] <= 30 * len(result["items"]) + 2
        assert all("data" not in row for row in result["items"])
        measurements.append({"limit": limit, "offset": offset, "seconds": round(elapsed, 4), "commands": dict(calls)})
    commands.calls.clear()
    start = perf_counter()
    universe = md.acquired_universe(db)
    elapsed = perf_counter() - start
    assert universe["total"] == count
    assert [row["instrument_id"] for row in universe["items"]] == [f"IE{index:010d}" for index in range(count)]
    assert not any(name in ("update", "insert", "delete", "findAndModify") or collection == "quotes" for name, collection in commands.calls)
    assert sum(name == "find" for name, _ in commands.calls) == (count + md.HISTORY_BATCH_SIZE - 1) // md.HISTORY_BATCH_SIZE
    measurements.append({"universe_seconds": round(elapsed, 4), "commands": dict(Counter(name for name, _ in commands.calls))})
    explain = db.command("explain", {"aggregate": "acquisition_jobs", "pipeline": md._acquired_metadata_pipeline() + [
        {"$facet": {"count": [{"$count": "total"}], "page": [{"$sort": {"collected_at": -1, "_id": 1}}, {"$limit": 1}]}}
    ], "cursor": {}}, verbosity="executionStats")
    # Explain shape varies by Mongo's classic/SBE engine; retain actual evidence.
    lookups = [stage["$lookup"] | {key: value for key, value in stage.items() if key != "$lookup"}
               for stage in explain.get("stages", []) if "$lookup" in stage]
    assert lookups and all(stage.get("collectionScans") == 0 and "job_id_1" in stage.get("indexesUsed", []) for stage in lookups)
    print({"inventory": count, "jobs": db.acquisition_jobs.count_documents({}), "measurements": measurements,
           "lookup_explain": lookups, "server": db.client.server_info()["version"]})
