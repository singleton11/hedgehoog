from unittest.mock import MagicMock, Mock

import pytest
from celery.exceptions import Retry
from pymongo.errors import ConnectionFailure

from celery_app import ACQUISITION_TASK, enqueue_acquisition
from config import TaskSettings
from market_data import JobBusy
from tasks import acquire_instrument_task


def test_enqueue_sends_only_job_id_to_acquisition_queue(monkeypatch):
    settings = TaskSettings(_env_file=None, marketstack_api_key="must-not-be-sent")
    producer = MagicMock()
    producer.__enter__.return_value = producer
    producer.send_task.return_value.id = "job-123"
    factory = Mock(return_value=producer)
    monkeypatch.setattr("celery_app.create_celery_app", factory)
    assert enqueue_acquisition(settings, "job-123") == "job-123"
    factory.assert_called_once_with(settings)
    producer.send_task.assert_called_once_with(ACQUISITION_TASK, args=["job-123"], task_id="job-123", queue="marketdata")
    producer.__exit__.assert_called_once()


@pytest.fixture
def worker(monkeypatch):
    settings = TaskSettings(_env_file=None, mongodb_uri="mongodb://test-worker:27017", mongodb_database="test")
    monkeypatch.setattr("tasks.TaskSettings", lambda: settings)
    mongo = MagicMock()
    mongo.__enter__.return_value = mongo
    factory = Mock(return_value=mongo)
    monkeypatch.setattr("tasks.MongoClient", factory)
    acquire = Mock(return_value={"id": "job-123", "status": "succeeded"})
    monkeypatch.setattr("tasks.run_acquisition", acquire)
    return settings, mongo, factory, acquire


def test_worker_owns_client_and_uses_durable_job(worker):
    settings, mongo, factory, acquire = worker
    result = acquire_instrument_task("job-123")
    assert result["status"] == "succeeded"
    factory.assert_called_once_with("mongodb://test-worker:27017", serverSelectionTimeoutMS=5000)
    acquire.assert_called_once_with(mongo.__getitem__.return_value, settings, "job-123")
    mongo.__exit__.assert_called_once_with(None, None, None)
    assert acquire_instrument_task.name == ACQUISITION_TASK


@pytest.mark.parametrize("error", [JobBusy(), ConnectionFailure("offline")])
def test_busy_or_database_retry_closes_client_first(worker, monkeypatch, error):
    _, mongo, _, acquire = worker
    acquire.side_effect = error

    def retry(**kwargs):
        mongo.__exit__.assert_called_once()
        assert kwargs == {"countdown": 30}
        raise Retry()

    monkeypatch.setattr(acquire_instrument_task, "retry", retry)
    with pytest.raises(Retry):
        acquire_instrument_task("job-123")
    assert acquire_instrument_task.max_retries == 12


def test_terminal_provider_failure_is_not_automatically_retried(worker, monkeypatch):
    _, mongo, _, acquire = worker
    acquire.return_value = {"id": "job-123", "status": "failed", "error": "Quota exhausted"}
    retry = Mock()
    monkeypatch.setattr(acquire_instrument_task, "retry", retry)
    assert acquire_instrument_task("job-123")["status"] == "failed"
    retry.assert_not_called()
    mongo.__exit__.assert_called_once()
