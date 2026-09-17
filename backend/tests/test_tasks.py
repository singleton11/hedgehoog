from unittest.mock import MagicMock, Mock

import httpx
import pytest
from celery.exceptions import Retry
from fastapi.testclient import TestClient
from kombu.exceptions import OperationalError
from pymongo.errors import ConnectionFailure

from celery_app import ETF_IMPORT_TASK, create_celery_app, enqueue_etf_import
from config import Settings, TaskSettings
from etfs import ETFImportError
from main import create_app
from tasks import import_etfs_task


@pytest.fixture
def task_mongo(monkeypatch):
    settings = TaskSettings(
        _env_file=None,
        mongodb_uri="mongodb://worker-db:27017",
        mongodb_database="task-test",
    )
    monkeypatch.setattr("tasks.TaskSettings", lambda: settings)
    mongo = MagicMock()
    mongo.__enter__.return_value = mongo
    factory = Mock(return_value=mongo)
    monkeypatch.setattr("tasks.MongoClient", factory)
    return settings, mongo, factory


def test_worker_configuration_does_not_require_api_secrets():
    settings = TaskSettings(_env_file=None, celery_broker_url="amqp://worker:secret@broker//")
    with create_celery_app(settings) as app:
        assert app.conf.broker_url == "amqp://worker:secret@broker//"
        assert app.conf.task_default_queue == "etfs"
        assert app.amqp.queues["etfs"].durable is True
        assert app.conf.task_default_delivery_mode == "persistent"
        assert app.conf.accept_content == ["json"]
        assert app.conf.task_serializer == "json"
        assert app.conf.task_ignore_result is True
        assert app.conf.result_backend is None
        assert app.conf.task_acks_late is True
        assert app.conf.task_reject_on_worker_lost is True
        assert app.conf.worker_prefetch_multiplier == 1
        assert app.conf.broker_connection_retry_on_startup is True
        assert app.conf.broker_transport_options["confirm_publish"] is True
        assert app.conf.task_publish_retry_policy["max_retries"] == 2


def test_enqueue_uses_instance_settings_without_sending_database_or_credentials(monkeypatch):
    settings = TaskSettings(_env_file=None, celery_broker_url="amqp://custom:secret@broker//")
    producer = MagicMock()
    producer.__enter__.return_value = producer
    producer.send_task.return_value.id = "queued-task-id"
    factory = Mock(return_value=producer)
    monkeypatch.setattr("celery_app.create_celery_app", factory)

    assert enqueue_etf_import(settings) == "queued-task-id"
    factory.assert_called_once_with(settings)
    producer.send_task.assert_called_once_with(ETF_IMPORT_TASK)
    producer.__exit__.assert_called_once()


@pytest.mark.parametrize("count", [2, 0])
def test_task_owns_mongo_client_and_returns_import_or_skip_count(task_mongo, monkeypatch, count):
    settings, mongo, factory = task_mongo
    importer = Mock(return_value=count)
    monkeypatch.setattr("tasks.import_etfs", importer)

    assert import_etfs_task() == count
    factory.assert_called_once_with("mongodb://worker-db:27017", serverSelectionTimeoutMS=5000)
    mongo.__getitem__.assert_called_once_with(settings.mongodb_database)
    importer.assert_called_once_with(mongo.__getitem__.return_value)
    mongo.__exit__.assert_called_once_with(None, None, None)
    assert import_etfs_task.name == ETF_IMPORT_TASK


@pytest.mark.parametrize("error", [ETFImportError("download or persistence failed"), ConnectionFailure("offline")])
def test_task_closes_client_before_scheduling_bounded_retry(task_mongo, monkeypatch, error):
    _, mongo, _ = task_mongo
    importer = Mock(side_effect=error)
    monkeypatch.setattr("tasks.import_etfs", importer)

    def retry(**kwargs):
        mongo.__exit__.assert_called_once()
        assert kwargs["exc"] is error
        assert 0 <= kwargs["countdown"] <= 30
        raise Retry(exc=error)

    retry_mock = Mock(side_effect=retry)
    monkeypatch.setattr(import_etfs_task, "retry", retry_mock)
    with pytest.raises(Retry):
        import_etfs_task()
    importer.assert_called_once()
    retry_mock.assert_called_once()
    assert import_etfs_task.max_retries == 5
    assert import_etfs_task.retry_backoff_max == 600
    assert import_etfs_task.retry_jitter is True


def test_task_stops_retrying_after_limit(task_mongo, monkeypatch):
    _, mongo, _ = task_mongo
    error = ETFImportError("still offline")
    monkeypatch.setattr("tasks.import_etfs", Mock(side_effect=error))
    # Mimic an actual worker request that has exhausted its retry budget.
    import_etfs_task.push_request(retries=5, called_directly=False)
    try:
        with pytest.raises(ETFImportError, match="still offline"):
            import_etfs_task.run()
    finally:
        import_etfs_task.pop_request()
    mongo.__exit__.assert_called_once()


def test_unexpected_programming_error_is_not_retried(task_mongo, monkeypatch):
    _, mongo, _ = task_mongo
    monkeypatch.setattr("tasks.import_etfs", Mock(side_effect=TypeError("bug")))
    retry = Mock()
    monkeypatch.setattr(import_etfs_task, "retry", retry)
    with pytest.raises(TypeError, match="bug"):
        import_etfs_task()
    retry.assert_not_called()
    mongo.__exit__.assert_called_once()


@pytest.fixture
def startup(monkeypatch):
    settings = Settings(_env_file=None, jwt_secret="test-secret-" * 5)
    mongo = MagicMock()
    enqueue = Mock(return_value="queued-task")
    seed = Mock()
    monkeypatch.setattr("main.enqueue_etf_import", enqueue)
    monkeypatch.setattr("main.seed_admin", seed)
    app = create_app(settings, client_factory=Mock(return_value=mongo))
    return settings, mongo, enqueue, seed, app


def test_startup_only_enqueues_after_seed_and_serves_without_downloading(startup, etf_http_get):
    settings, mongo, enqueue, seed, app = startup
    events = Mock()
    events.attach_mock(mongo.admin.command, "ping")
    events.attach_mock(seed, "seed")
    events.attach_mock(enqueue, "enqueue")
    etf_http_get.side_effect = httpx.ConnectError("offline")

    with TestClient(app) as client:
        assert [call[0] for call in events.mock_calls] == ["ping", "seed", "enqueue"]
        enqueue.assert_called_once_with(settings)
        assert client.get("/health").status_code == 200
        etf_http_get.assert_not_called()
        mongo.__getitem__.return_value.etfs.bulk_write.assert_not_called()
        mongo.close.assert_not_called()
    mongo.close.assert_called_once()


def test_broker_publish_failure_aborts_startup_and_closes_mongo(startup):
    _, mongo, enqueue, _, app = startup
    enqueue.side_effect = OperationalError("broker unavailable")
    with pytest.raises(OperationalError, match="broker unavailable"):
        with TestClient(app):
            pytest.fail("Startup must not silently lose the import request")
    mongo.close.assert_called_once()


def test_failed_admin_seed_does_not_queue_an_import(startup):
    _, mongo, enqueue, seed, app = startup
    seed.side_effect = ValueError("unsafe admin")
    with pytest.raises(ValueError, match="unsafe admin"):
        with TestClient(app):
            pytest.fail("Unsafe configuration must not start")
    enqueue.assert_not_called()
    mongo.close.assert_called_once()
