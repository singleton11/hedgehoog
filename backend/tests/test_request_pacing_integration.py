"""Real Mongo, separate processes, no provider calls or application collections."""
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from pymongo import MongoClient

from request_pacing import wait_for_yahoo_request


def _reserve_requests(uri, name):
    with MongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True) as client:
        collection = client[name].provider_request_pacing
        original = collection.find_one_and_update
        grants = []

        def record(*args, **kwargs):
            result = original(*args, **kwargs)
            if result is not None:
                grants.append(result["next_request_at"])
            return result

        with patch.object(collection, "find_one_and_update", side_effect=record):
            for _ in range(4):
                wait_for_yahoo_request(SimpleNamespace(provider_request_pacing=collection), 0.05, Mock())
        return grants


def test_real_mongo_serializes_independent_processes():
    uri = os.environ.get("TEST_MONGODB_URI")
    if not uri:
        pytest.skip("Set TEST_MONGODB_URI for isolated real Mongo tests")
    name = "hedgehoog_pacing_test_" + uuid4().hex
    with MongoClient(uri, serverSelectionTimeoutMS=5000) as client:
        client.admin.command("ping")
        try:
            with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
                futures = [pool.submit(_reserve_requests, uri, name) for _ in range(4)]
                grants = sorted(value for future in futures for value in future.result(timeout=30))
            assert len(grants) == len(set(grants)) == 16
            assert all((b - a).total_seconds() >= 0.05 for a, b in zip(grants, grants[1:]))
            assert client[name].provider_request_pacing.count_documents({}) == 1
        finally:
            client.drop_database(name)
