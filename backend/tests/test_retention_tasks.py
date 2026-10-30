"""Retention scheduling is separate from acquisition success and broker failures."""
from pathlib import Path
from unittest.mock import MagicMock, Mock

import mongomock
import pytest
import yaml
from pymongo.errors import ConnectionFailure

from celery_app import MARKET_DATA_RETENTION_TASK, create_celery_app
from config import TaskSettings
import market_data_retention as retention
import tasks


def client_for(monkeypatch):
    client = MagicMock()
    client.__enter__.return_value = mongomock.MongoClient()
    monkeypatch.setattr(tasks, "MongoClient", Mock(return_value=client))
    return client


def test_compose_workers_forward_retention_controls():
    root = Path(__file__).resolve().parents[2]
    for path in (root / "compose.yaml", root / "deploy" / "compose.yaml"):
        services = yaml.safe_load(path.read_text())["services"]
        for name in ("celery-worker", "celery-marketdata-worker"):
            environment = services[name]["environment"]
            assert environment["MARKET_DATA_RETENTION_ENABLED"] == "${MARKET_DATA_RETENTION_ENABLED:-true}"
            assert environment["MARKET_DATA_RETENTION_BATCH_SIZE"] == "${MARKET_DATA_RETENTION_BATCH_SIZE:-1000}"


def test_daily_catchup_and_success_dispatch(monkeypatch):
    app = create_celery_app(TaskSettings(_env_file=None))
    assert app.conf.beat_schedule["daily-market-data-retention"]["task"] == MARKET_DATA_RETENTION_TASK
    client_for(monkeypatch)
    monkeypatch.setattr(tasks, "run_acquisition", Mock(return_value={"status": "succeeded", "instrument_id": "FIXTURE"}))
    send = Mock()
    monkeypatch.setattr(tasks.app, "send_task", send)
    assert tasks.acquire_instrument_task.run("fixture-job")["status"] == "succeeded"
    send.assert_called_once_with(MARKET_DATA_RETENTION_TASK, args=["FIXTURE"], queue="etfs")


def test_dispatch_failure_does_not_change_acquisition_success(monkeypatch):
    client_for(monkeypatch)
    monkeypatch.setattr(tasks, "run_acquisition", Mock(return_value={"status": "succeeded", "instrument_id": "FIXTURE"}))
    monkeypatch.setattr(tasks.app, "send_task", Mock(side_effect=RuntimeError("synthetic offline broker")))
    assert tasks.acquire_instrument_task.run("fixture-job")["status"] == "succeeded"


def test_cleanup_failure_retries_separately(monkeypatch):
    client_for(monkeypatch)
    cleanup = Mock(side_effect=ConnectionFailure("synthetic database restart"))
    monkeypatch.setattr(retention, "cleanup", cleanup)
    retry = Mock(side_effect=RuntimeError("retry scheduled"))
    monkeypatch.setattr(tasks.retain_market_data_task, "retry", retry)
    with pytest.raises(RuntimeError, match="retry scheduled"):
        tasks.retain_market_data_task.run("FIXTURE")
    retry.assert_called_once()
