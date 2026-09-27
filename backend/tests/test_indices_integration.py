"""Opt-in disposable Mongo/Rabbit index workflow; provider data is synthetic."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from queue import Queue
from unittest.mock import Mock

from celery.contrib.testing.worker import start_worker
from fastapi.testclient import TestClient

from celery_app import ACQUISITION_TASK, create_celery_app
import indices
from main import create_app
import market_data as md
import tasks
from test_indices import IndexProvider
from test_market_data_integration import environment


def test_real_mongo_index_catalogue_and_atomic_acquisition_gate(environment):
    settings, db = environment
    indices.ensure_catalogue(db)
    values = {"symbol": "^CUSTOM", "name": "Custom index", "currency": None, "return_variant": "unknown"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: indices.add_index(db, values, "test"), range(8)))
    assert len({row["instrument_id"] for row in rows}) == 1
    assert db.indices.count_documents({"symbol": "^CUSTOM"}) == 1
    record = db.indices.find_one({"symbol": "^GSPC"})
    publish = Mock()
    md.ensure_indexes(db)
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = list(pool.map(lambda _: md.request_acquisition(db, settings, record["instrument_id"], None, "update", "test", publish), range(8)))
    assert len({job["id"] for job in jobs}) == 1
    publish.assert_called_once()
    assert md.run_acquisition(db, settings, jobs[0]["id"], IndexProvider().factory)["status"] == "succeeded"
    assert indices.index_detail(db, record["instrument_id"])["summary"]["quote_count"] == 1
    assert md.acquired_instruments(db)["total"] == 0


def test_real_broker_index_download_failed_refresh_and_duplicate_delivery(environment, monkeypatch):
    if not os.environ.get("TEST_CELERY_BROKER_URL"):
        import pytest
        pytest.skip("Set TEST_CELERY_BROKER_URL for isolated RabbitMQ delivery")
    settings, db = environment
    queue = db.name
    results = Queue()
    now = [md.utcnow()]
    monkeypatch.setattr(md, "utcnow", lambda: now[0])
    source = [IndexProvider(currency="USD")]
    real_run = md.run_acquisition

    def observed_run(database, config, job_id):
        result = real_run(database, config, job_id, source[0].factory)
        results.put(result)
        return result

    monkeypatch.setattr(tasks, "TaskSettings", lambda: settings)
    monkeypatch.setattr(tasks, "run_acquisition", observed_run)
    monkeypatch.setattr("main.enqueue_etf_import", Mock())
    with create_celery_app(settings) as worker_app:
        worker_app.conf.task_default_queue = queue

        def publish(config, job_id):
            return worker_app.send_task(ACQUISITION_TASK, args=[job_id], task_id=job_id, queue=queue).id

        monkeypatch.setattr("index_routes.enqueue_acquisition", publish)
        try:
            with TestClient(create_app(settings)) as client:
                assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200
                catalogue = client.get("/indices").json()
                record = next(row for row in catalogue["items"] if row["symbol"] == "^GSPC")
                path = "/indices/" + record["instrument_id"]
                assert source[0].calls == []
                response = client.post(path + "/acquisitions", json={"confirmed": True})
                assert response.status_code == 202
                first = response.json()["job"]["id"]
                assert client.post(path + "/acquisitions", json={"confirmed": True}).json()["job"]["id"] == first
                assert client.get(path).json()["acquisition"]["status"] == "queued"
                with start_worker(worker_app, pool="solo", concurrency=1, perform_ping_check=False, shutdown_timeout=15):
                    assert results.get(timeout=25)["status"] == "succeeded"
                    detail = client.get(path).json()
                    assert detail["verification_status"] == "verified" and detail["currency"] == "USD"
                    assert detail["summary"]["quote_count"] == 1
                    assert client.get(path + "/quotes").json()["items"][0]["close"] == "100"
                    assert client.get("/instruments/acquired").json()["total"] == 0
                    assert client.get("/portfolio-instruments").json()["total"] == 0
                    calls = len(source[0].calls)
                    publish(settings, first)
                    assert results.get(timeout=25)["status"] == "succeeded"
                    assert len(source[0].calls) == calls
                    now[0] += timedelta(seconds=1)
                    source[0] = IndexProvider(history_kind="ETF", currency="EUR", close="999")
                    assert client.post(path + "/acquisitions", json={"confirmed": True}).status_code == 202
                    assert results.get(timeout=25)["status"] == "failed"
                    after = client.get(path).json()
                    assert after["acquisition"]["status"] == "failed"
                    assert after["generation"] == first and after["currency"] == "USD"
                    assert client.get(path + "/quotes").json()["items"][0]["close"] == "100"
        finally:
            with worker_app.connection_for_write() as connection:
                binding = worker_app.amqp.queues[queue].bind(connection)
                binding.delete()
                binding.exchange.delete()
