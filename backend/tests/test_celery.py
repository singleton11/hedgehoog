"""Opt-in RabbitMQ + MongoDB test with an isolated queue and synthetic ETF feed."""

import os
from datetime import datetime, timezone
from queue import Queue
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from celery.contrib.testing.worker import start_worker
from fastapi.testclient import TestClient
from pymongo import MongoClient

import tasks
from celery_app import create_celery_app, enqueue_etf_import
from config import Settings
from main import create_app

pytestmark = pytest.mark.skipif(
    not (os.environ.get("TEST_MONGODB_URI") and os.environ.get("TEST_CELERY_BROKER_URL")),
    reason="Set TEST_MONGODB_URI and TEST_CELERY_BROKER_URL to test RabbitMQ delivery",
)


def test_startup_queues_offline_worker_then_retries_and_skips_completed_day(
    monkeypatch, etf_http_get, etf_records,
):
    name = f"hedgehoog_test_{uuid4().hex}"
    settings = Settings(
        _env_file=None,
        mongodb_uri=os.environ["TEST_MONGODB_URI"],
        mongodb_database=name,
        celery_broker_url=os.environ["TEST_CELERY_BROKER_URL"],
        jwt_secret="integration-test-secret-" * 3,
    )
    monkeypatch.setattr(tasks, "TaskSettings", lambda: settings)
    monkeypatch.setattr("etfs.datetime", Mock(
        now=Mock(return_value=datetime(2026, 9, 17, 12, tzinfo=timezone.utc)),
    ))
    # Keep production retry policy, but remove the countdown for this test.
    monkeypatch.setattr("celery.app.autoretry.get_exponential_backoff_interval", lambda **kwargs: 0)
    response = etf_http_get.return_value
    etf_http_get.side_effect = [httpx.ConnectError("temporarily offline"), response]
    attempts = Queue()
    real_import = tasks.import_etfs

    def observed_import(database):
        try:
            count = real_import(database)
        except Exception as exc:
            attempts.put(exc)
            raise
        attempts.put(count)
        return count

    monkeypatch.setattr(tasks, "import_etfs", observed_import)

    def isolated_celery(config):
        app = create_celery_app(config)
        app.conf.task_default_queue = name
        return app

    # The actual producer still publishes through RabbitMQ, never eager mode.
    # Only its queue is changed so an existing Compose worker cannot steal tasks.
    monkeypatch.setattr("celery_app.create_celery_app", isolated_celery)
    with MongoClient(settings.mongodb_uri.get_secret_value(), serverSelectionTimeoutMS=5000) as mongo:
        database = mongo[name]
        with isolated_celery(settings) as worker_app:
            try:
                # No worker is running on this queue yet: startup must still work.
                with TestClient(create_app(settings)) as client:
                    assert client.get("/health").status_code == 200
                    assert database.etfs.count_documents({}) == 0
                    assert database.imports.count_documents({}) == 0
                    etf_http_get.assert_not_called()

                with start_worker(
                    worker_app, pool="solo", concurrency=1,
                    perform_ping_check=False, shutdown_timeout=15,
                ):
                    assert isinstance(attempts.get(timeout=20), tasks.ETFImportError)
                    assert attempts.get(timeout=20) == len(etf_records)
                    assert etf_http_get.call_count == 2
                    assert database.imports.find_one({"date": "2026-09-17"})["etfs"] is True
                    previous = list(database.etfs.find().sort("isin"))
                    assert len(previous) == len(etf_records)
                    for record in etf_records:
                        assert database.etfs.find_one({"isin": record["isin"]}, {"_id": 0}) == {
                            **record, "import_date": "2026-09-17",
                        }

                    # A duplicate/restart message must skip even with HTTP offline.
                    etf_http_get.side_effect = httpx.ConnectError("offline again")
                    assert enqueue_etf_import(settings)
                    assert attempts.get(timeout=20) == 0
                    assert etf_http_get.call_count == 2
                    assert list(database.etfs.find().sort("isin")) == previous
            finally:
                try:
                    with worker_app.connection_for_write() as connection:
                        queue = worker_app.amqp.queues[name].bind(connection)
                        queue.delete()
                        queue.exchange.delete()
                finally:
                    mongo.drop_database(name)
