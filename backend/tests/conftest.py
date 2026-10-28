from unittest.mock import Mock

import httpx
import pytest

from etfs import ETF_SOURCE_URL


@pytest.fixture(autouse=True)
def automatic_refresh_producer(monkeypatch):
    producer = Mock(return_value="test-refresh-task")
    monkeypatch.setattr("main.enqueue_market_data_refresh", producer)
    monkeypatch.setattr("tasks.enqueue_market_data_refresh", producer)
    return producer


@pytest.fixture(autouse=True)
def history_lookup_calls(monkeypatch):
    """Emulate the small endpoint pipeline unsupported by mongomock.

    This checks result semantics only, never Mongo index use or performance.
    Real Mongo tests run the unmodified aggregation implementation.
    """
    from copy import deepcopy
    import mongomock

    original = mongomock.collection.Collection.aggregate
    calls = []

    def aggregate(collection, pipeline, *args, **kwargs):
        if not (len(pipeline) == 7 and pipeline[0] == {"$limit": 1}
                and "candidate" in pipeline[1].get("$project", {})):
            return original(collection, pipeline, *args, **kwargs)
        calls.append(deepcopy(pipeline))
        if not collection.find_one():
            return iter([])
        results = []
        for seed in pipeline[1]["$project"]["candidate"]["$literal"]:
            row = dict(seed)
            for stage, field in zip(pipeline[4:6], ("first_date", "last_date")):
                probe = stage["$lookup"]["pipeline"]
                match = probe[0]["$match"]
                query = {key: value for key, value in match.items() if key != "$expr"}
                for comparison in match["$expr"]["$and"]:
                    left, right = comparison["$eq"]
                    query[left[1:]] = seed[right[2:]]
                endpoint = next(collection.database.quotes.find(query).sort(list(probe[1]["$sort"].items())).limit(1), None)
                if endpoint:
                    row[field] = endpoint["date"]
            results.append(row)
        return iter(results)

    monkeypatch.setattr(mongomock.collection.Collection, "aggregate", aggregate)
    return calls


@pytest.fixture
def etf_records():
    return [
        {
            "isin": "IE00B4L5Y983",
            "name": "Example World UCITS ETF",
            "ticker": "EXAMPLE",
            "update_date": "2026-09-12",
            "ter": 0.2,
            "hedged": False,
            "number_of_holdings": None,
            "holdings": [{"name": "Example holding", "percentage": 5.25, "isin": "US0378331005"}],
            "countries": [{"name": "United States", "percentage": 70.5}],
            "sectors": [],
            "future_source_field": {"values": [1, None, "value"]},
        },
        {"isin": "IE00B3XXRP09", "name": "Example S&P 500 UCITS ETF", "ticker": "EXAMPLE"},
    ]


@pytest.fixture
def etf_http_get(monkeypatch, etf_records):
    get = Mock(return_value=httpx.Response(
        200, json=etf_records, request=httpx.Request("GET", ETF_SOURCE_URL),
    ))
    monkeypatch.setattr("etfs.httpx.get", get)
    return get
