"""Real query plans on synthetic raw pages; never use application Mongo config."""
import os
from uuid import uuid4

import pytest
from pymongo import MongoClient

import market_data as md
import market_data_retention as retention
from config import TaskSettings

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_MONGODB_URI"),
    reason="Disposable TEST_MONGODB_URI not provided",
)

RAW_COLLECTIONS = ("raw_eod_pages", "raw_split_pages")
RETENTION_KEY = {"provider": 1, "instrument_id": 1, "job_id": 1}


@pytest.fixture(params=RAW_COLLECTIONS)
def raw_db(request):
    client = MongoClient(os.environ["TEST_MONGODB_URI"], serverSelectionTimeoutMS=5000)
    name = "fixture_retention_discovery_" + uuid4().hex
    db = client[name]
    collection = db[request.param]
    try:
        # Match the pre-fix raw-page indexes and keep payloads large enough that
        # a collection scan must read unrelated provider data, even on a miss.
        collection.create_index([("job_id", 1), ("offset", 1)], unique=True)
        for batch in range(8):
            collection.insert_many([
                {"provider": "yfinance", "instrument_id": f"YAHOO-{index % 100}",
                 "job_id": f"yahoo-{index}", "offset": 0, "payload": "x" * 4096}
                for index in range(batch * 1000, (batch + 1) * 1000)
            ])
        yield db, collection
    finally:
        client.drop_database(name)
        client.close()


def plan_nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from plan_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from plan_nodes(child)


def explain(db, command):
    return db.command("explain", command, verbosity="executionStats")


def assert_indexed(result, max_docs):
    nodes = list(plan_nodes(result["queryPlanner"]["winningPlan"]))
    assert not any(node.get("stage") == "COLLSCAN" for node in nodes), result
    assert any(node.get("stage") in ("IXSCAN", "DISTINCT_SCAN") for node in nodes), result
    assert result["executionStats"]["totalDocsExamined"] <= max_docs, result
    return nodes


def test_raw_retention_discovery_and_purge_plans(raw_db):
    db, collection = raw_db
    query = {"instrument_id": "YAHOO-0", "provider": "marketstack"}
    command = {"find": collection.name, "filter": query, "projection": {"job_id": 1}}
    before = explain(db, command)
    assert any(node.get("stage") == "COLLSCAN"
               for node in plan_nodes(before["queryPlanner"]["winningPlan"]))
    assert before["executionStats"]["totalDocsExamined"] == 8000

    md.ensure_indexes(db)
    assert_indexed(explain(db, command), max_docs=0)
    md.ensure_indexes(db)  # Startup and each retention pass are idempotent.
    indexes = list(collection.list_indexes())
    assert any(index["key"] == {"job_id": 1, "offset": 1} and index["unique"]
               for index in indexes)
    assert any(index["key"] == RETENTION_KEY for index in indexes)
    assert not any(index["key"] == RETENTION_KEY for index in db.quotes.list_indexes())

    # The reported regression: no legacy rows, but many large Yahoo pages.
    for instrument in ("YAHOO-0", "NONEXISTENT"):
        missing = {"instrument_id": instrument, "provider": "marketstack"}
        for projection in ({"job_id": 1}, {"_id": 1}):
            result = explain(db, {"find": collection.name, "filter": missing,
                                  "projection": projection})
            nodes = assert_indexed(result, max_docs=0)
            assert any(node.get("keyPattern") == RETENTION_KEY for node in nodes)
            assert result["executionStats"]["nReturned"] == 0
        assert_indexed(explain(db, {"delete": collection.name,
                                   "deletes": [{"q": missing, "limit": 0}]}), max_docs=0)

    legacy_id = collection.insert_one({"instrument_id": "LEGACY", "provider": "marketstack",
                                       "job_id": "legacy", "offset": 0}).inserted_id
    collection.insert_one({"instrument_id": "LEGACY", "job_id": "untagged", "offset": 0})
    legacy = {"instrument_id": "LEGACY", "provider": "marketstack"}
    assert_indexed(explain(db, {"find": collection.name, "filter": legacy,
                               "projection": {"job_id": 1}}), max_docs=1)
    # cleanup() uses the provider prefix before per-instrument processing.
    result = explain(db, {"distinct": collection.name, "key": "instrument_id",
                          "query": {"provider": "marketstack"}})
    assert_indexed(result, max_docs=1)
    assert collection.distinct("instrument_id", {"provider": "marketstack"}) == ["LEGACY"]

    # _batches() selects IDs then deletes the same fenced identity plus IDs.
    for identity in (legacy, {"instrument_id": "LEGACY", "job_id": "legacy",
                              "provider": {"$in": ["marketstack", None]}},
                     {"instrument_id": "LEGACY", "job_id": "untagged",
                      "provider": {"$in": ["marketstack", None]}}):
        assert_indexed(explain(db, {"find": collection.name, "filter": identity,
                                   "projection": {"_id": 1}, "limit": 1}), max_docs=1)
        assert_indexed(explain(db, {"delete": collection.name, "deletes": [
            {"q": {"$and": [identity, {"_id": {"$in": [legacy_id]}}]}, "limit": 0},
        ]}), max_docs=1)

    result = retention.cleanup(db, TaskSettings(_env_file=None, market_data_retention_batch_size=1))
    assert result["items"][0]["counts"][collection.name] == 1
    assert collection.count_documents({"provider": "marketstack"}) == 0
    assert collection.count_documents({"provider": "yfinance"}) == 8000
    assert collection.count_documents({"job_id": "untagged"}) == 1
    # Catch-up's terminal empty scan must remain indexed after all legacy rows go.
    assert_indexed(explain(db, {"find": collection.name, "filter": legacy,
                               "projection": {"_id": 1}}), max_docs=0)


def test_cleanup_installs_indexes_before_global_legacy_discovery(raw_db, monkeypatch):
    db, collection = raw_db
    collection.insert_one({"instrument_id": "ORPHAN", "provider": "marketstack",
                           "job_id": "orphan", "offset": 0})
    original = type(collection).distinct
    checked = []

    def checked_distinct(self, *args, **kwargs):
        if self.database.name == db.name and self.name in RAW_COLLECTIONS:
            assert self.index_information().get("provider_1_instrument_id_1_job_id_1", {}).get("key") == list(RETENTION_KEY.items())
            checked.append(self.name)
        return original(self, *args, **kwargs)

    # Inspect the indexes at the actual distinct boundary, not after cleanup's
    # later retain_instrument() call has already installed them.
    monkeypatch.setattr(type(collection), "distinct", checked_distinct)
    result = retention.cleanup(db, TaskSettings(_env_file=None))
    assert set(checked) == set(RAW_COLLECTIONS)
    assert result["items"][0]["instrument_id"] == "ORPHAN"
    assert collection.count_documents({"provider": "marketstack"}) == 0
    assert collection.count_documents({"provider": "yfinance"}) == 8000
