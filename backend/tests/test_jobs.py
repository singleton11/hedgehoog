from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock

import mongomock
import pytest
from celery.exceptions import Retry
from fastapi.testclient import TestClient
from kombu.exceptions import OperationalError
from pymongo.errors import ConnectionFailure

from celery_app import enqueue_etf_import, enqueue_market_data_refresh
from config import Settings, TaskSettings
from etfs import ETFImportError
from jobs import SAFE_ERRORS, list_jobs, queue_job
from main import create_app
from market_data import DataError
from tasks import import_etfs_task, refresh_market_data_task


@pytest.fixture
def db():
    return mongomock.MongoClient().jobs_test


@pytest.fixture
def client(db, monkeypatch):
    config = Settings(_env_file=None, jwt_secret="test-secret-" * 5, mongodb_database=db.name)
    monkeypatch.setattr("main.enqueue_etf_import", Mock())
    with TestClient(create_app(config, client_factory=lambda *a, **kw: db.client)) as client:
        yield client


def login(client):
    assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200


def seed_jobs(db):
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    for index, status in enumerate(("queued", "running", "retrying", "succeeded", "failed")):
        db.acquisition_jobs.insert_one({
            "_id": f"acquisition-{index}", "instrument_id": "IE00B4L5Y983", "status": status,
            "requested_at": now + timedelta(seconds=index),
            "error": "Traceback: mongodb://user:secret@host/?api_key=secret",
            "raw_payload": {"secret": "must never escape"},
        })
    db.acquisition_jobs.insert_one({"_id": "removal-lock", "status": "deleting", "requested_at": now})
    db.background_jobs.insert_many([
        {"_id": "import", "job_type": "etf_import", "status": "failed", "requested_at": now + timedelta(seconds=5)},
        {"_id": "refresh", "job_type": "market_data_refresh", "status": "succeeded", "requested_at": now + timedelta(seconds=6)},
    ])


def test_auth_and_regular_user_access(client, db):
    assert client.get("/jobs").status_code == 401
    login(client)
    db.users.update_one({"login": "admin"}, {"$set": {"role": "user"}})
    response = client.get("/jobs")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["items"] == []
    assert response.json()["counts"] == dict(total=0, queued=0, running=0, retrying=0, succeeded=0, failed=0)
    db.users.update_one({"login": "admin"}, {"$set": {"disabled": True}})
    assert client.get("/jobs").status_code == 401


def test_historical_acquisitions_contract_and_safe_errors(client, db):
    seed_jobs(db)
    before = list(db.acquisition_jobs.find())
    login(client)
    response = client.get("/jobs")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 7
    assert payload["counts"] == dict(total=7, queued=1, running=1, retrying=1, succeeded=2, failed=2)
    assert [row["id"] for row in payload["items"]] == ["refresh", "import", "acquisition-4", "acquisition-3", "acquisition-2", "acquisition-1", "acquisition-0"]
    assert {item["value"] for item in payload["job_types"]} == {"market_data_acquisition", "etf_import", "market_data_refresh"}
    for row in payload["items"]:
        assert set(row) == {"id", "job_type", "status", "instrument_id", "requested_at", "started_at", "finished_at", "error"}
        assert row["error"] == SAFE_ERRORS.get(row["status"])
        assert row["requested_at"].endswith("Z")
        assert row["started_at"] is None
    assert "secret" not in response.text
    assert "Traceback" not in response.text
    assert list(db.acquisition_jobs.find()) == before


@pytest.mark.parametrize("query,ids,total", [
    ("status=failed", ["import", "acquisition-4"], 2),
    ("status=&job_type=&limit=2", ["refresh", "import"], 7),
    ("job_type=market_data_acquisition&limit=2&offset=1", ["acquisition-3", "acquisition-2"], 5),
    ("job_type=etf_import&status=succeeded", [], 0),
    ("limit=2&offset=1", ["import", "acquisition-4"], 7),
    ("offset=99", [], 7),
])
def test_filters_pagination_and_global_counts(client, db, query, ids, total):
    seed_jobs(db)
    login(client)
    result = client.get(f"/jobs?{query}").json()
    assert [row["id"] for row in result["items"]] == ids
    assert result["total"] == total
    assert result["counts"]["total"] == 7


@pytest.mark.parametrize("query", ["status=deleting", "job_type=anything", "limit=0", "limit=201", "offset=-1", "offset=no"])
def test_validation(client, query):
    login(client)
    assert client.get(f"/jobs?{query}").status_code == 422


def test_default_page_and_stable_ties(db):
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    db.acquisition_jobs.insert_many([
        {"_id": f"job-{i:03d}", "status": "succeeded", "requested_at": now}
        for i in range(60)
    ])
    first = list_jobs(db)
    assert len(first["items"]) == 50
    assert first["total"] == 60
    assert first["items"][0]["id"] == "job-059"
    assert len(list_jobs(db, offset=50)["items"]) == 10


def test_database_failure_is_safe(client, db, monkeypatch):
    login(client)
    monkeypatch.setattr(db.acquisition_jobs, "aggregate", Mock(side_effect=ConnectionFailure("mongodb://user:secret@host")))
    response = client.get("/jobs")
    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}


@pytest.mark.parametrize("enqueue,job_type", [(enqueue_etf_import, "etf_import"), (enqueue_market_data_refresh, "market_data_refresh")])
def test_publish_persists_queued_before_send(db, monkeypatch, enqueue, job_type):
    producer = MagicMock()
    producer.__enter__.return_value = producer
    def send(name, **kwargs):
        row = db.background_jobs.find_one({"_id": kwargs["task_id"]})
        assert row["status"] == "queued"
        assert row["job_type"] == job_type
        assert "args" not in kwargs
        return Mock(id=kwargs["task_id"])
    producer.send_task.side_effect = send
    monkeypatch.setattr("celery_app.create_celery_app", Mock(return_value=producer))
    job_id = enqueue(TaskSettings(_env_file=None), db)
    assert db.background_jobs.find_one()["_id"] == job_id


def test_catalogue_sweep_publish_tracks_job_and_preserves_scope(db, monkeypatch):
    producer = MagicMock()
    producer.__enter__.return_value = producer
    producer.send_task.side_effect = lambda name, **kwargs: Mock(id=kwargs["task_id"])
    monkeypatch.setattr("celery_app.create_celery_app", Mock(return_value=producer))

    job_id = enqueue_market_data_refresh(TaskSettings(_env_file=None), db, include_catalogue=True)

    row = db.background_jobs.find_one({"_id": job_id})
    assert row["job_type"] == "market_data_refresh"
    assert row["status"] == "queued"
    producer.send_task.assert_called_once_with(
        refresh_market_data_task.name, queue="etfs", task_id=job_id,
        kwargs={"include_catalogue": True},
    )


def test_publish_failure_is_recorded_without_secrets(db, monkeypatch):
    producer = MagicMock()
    producer.__enter__.return_value = producer
    producer.send_task.side_effect = OperationalError("amqp://user:secret@host")
    monkeypatch.setattr("celery_app.create_celery_app", Mock(return_value=producer))
    with pytest.raises(OperationalError):
        enqueue_etf_import(TaskSettings(_env_file=None), db)
    row = db.background_jobs.find_one()
    assert row["status"] == "failed"
    assert row["finished_at"] is not None
    assert "secret" not in str(row)


@pytest.fixture
def worker(db, monkeypatch):
    monkeypatch.setattr("tasks.TaskSettings", lambda: TaskSettings(_env_file=None, mongodb_database=db.name))
    monkeypatch.setattr("tasks.MongoClient", lambda *a, **kw: db.client)


@pytest.mark.parametrize("task,target,job_type,result", [
    (import_etfs_task, "tasks.import_etfs", "etf_import", 0),
    (refresh_market_data_task, "tasks.refresh_stale_market_data", "market_data_refresh", {"requested": 2}),
])
@pytest.mark.parametrize("prequeued", [False, True])
def test_execution_tracks_scheduled_and_queued_tasks(db, worker, monkeypatch, task, target, job_type, result, prequeued):
    if prequeued:
        queue_job(db, "task-id", job_type)
    original = db.background_jobs.find_one()
    def work(*args):
        assert db.background_jobs.find_one()["status"] == "running"
        return result
    monkeypatch.setattr(target, work)
    task.push_request(id="task-id", retries=0, called_directly=False)
    try:
        assert task.run() == result
    finally:
        task.pop_request()
    row = db.background_jobs.find_one()
    assert row["_id"] == "task-id"
    assert row["job_type"] == job_type
    assert row["status"] == "succeeded"
    assert row["started_at"] <= row["finished_at"]
    assert row["error"] is None
    if original:
        assert row["requested_at"] == original["requested_at"]
    assert db.background_jobs.count_documents({}) == 1


@pytest.mark.parametrize("task,target,error,job_type", [
    (import_etfs_task, "tasks.import_etfs", ETFImportError("secret"), "etf_import"),
    (refresh_market_data_task, "tasks.refresh_stale_market_data", DataError(503, "secret"), "market_data_refresh"),
])
def test_retry_identity_and_exhaustion(db, worker, monkeypatch, task, target, error, job_type):
    monkeypatch.setattr(target, Mock(side_effect=error))
    retry = Mock(side_effect=Retry())
    monkeypatch.setattr(task, "retry", retry)
    task.push_request(id="retry-id", retries=0, called_directly=False)
    try:
        with pytest.raises(Retry):
            task.run()
    finally:
        task.pop_request()
    first = db.background_jobs.find_one()
    assert first["status"] == "retrying"
    assert first["finished_at"] is None
    task.push_request(id="retry-id", retries=task.max_retries, called_directly=False)
    try:
        with pytest.raises(Retry):
            task.run()
    finally:
        task.pop_request()
    row = db.background_jobs.find_one()
    assert row["status"] == "failed"
    assert row["requested_at"] == first["requested_at"]
    assert row["finished_at"] is not None
    assert db.background_jobs.count_documents({}) == 1
    assert "secret" not in str(row)


def test_catalogue_dispatch_failure_tracks_import_retry(db, worker, monkeypatch, automatic_refresh_producer):
    monkeypatch.setattr("tasks.import_etfs", Mock(return_value=0))
    automatic_refresh_producer.side_effect = OperationalError("amqp://user:secret@host")
    monkeypatch.setattr(import_etfs_task, "retry", Mock(side_effect=Retry()))
    for retries, expected in [(0, "retrying"), (import_etfs_task.max_retries, "failed")]:
        import_etfs_task.push_request(id="import-dispatch", retries=retries, called_directly=False)
        try:
            with pytest.raises(Retry):
                import_etfs_task.run()
        finally:
            import_etfs_task.pop_request()
        row = db.background_jobs.find_one({"_id": "import-dispatch"})
        assert row["status"] == expected
        assert (row["finished_at"] is None) == (expected == "retrying")
        assert "secret" not in str(row)
    assert db.background_jobs.count_documents({}) == 1
    automatic_refresh_producer.assert_called_with(
        TaskSettings(_env_file=None, mongodb_database=db.name), db, include_catalogue=True,
    )


def test_retry_then_success_reuses_record(db, worker, monkeypatch):
    monkeypatch.setattr("tasks.import_etfs", Mock(side_effect=[ETFImportError("temporary"), 2]))
    monkeypatch.setattr(import_etfs_task, "retry", Mock(side_effect=Retry()))
    import_etfs_task.push_request(id="same-job", retries=0, called_directly=False)
    try:
        with pytest.raises(Retry):
            import_etfs_task.run()
    finally:
        import_etfs_task.pop_request()
    first = db.background_jobs.find_one()
    import_etfs_task.push_request(id="same-job", retries=1, called_directly=False)
    try:
        assert import_etfs_task.run() == 2
    finally:
        import_etfs_task.pop_request()
    final = db.background_jobs.find_one()
    assert final["status"] == "succeeded"
    assert final["requested_at"] == first["requested_at"]
    assert final["error"] is None
    assert db.background_jobs.count_documents({}) == 1


def test_unexpected_failure_is_terminal(db, worker, monkeypatch):
    monkeypatch.setattr("tasks.import_etfs", Mock(side_effect=TypeError("secret traceback")))
    with pytest.raises(TypeError):
        import_etfs_task()
    row = db.background_jobs.find_one()
    assert row["status"] == "failed"
    assert row["error"] == SAFE_ERRORS["failed"]


def test_monitoring_write_failure_does_not_change_task_result(db, worker, monkeypatch):
    monkeypatch.setattr(db.background_jobs, "update_one", Mock(side_effect=ConnectionFailure("secret")))
    monkeypatch.setattr("tasks.import_etfs", Mock(return_value=42))
    assert import_etfs_task() == 42
