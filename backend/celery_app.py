"""Celery configuration shared by the API producer and ETF worker."""

import logging

from celery import Celery
from celery.schedules import crontab

from config import TaskSettings

ETF_IMPORT_TASK = "etfs.import_daily"
ACQUISITION_TASK = "marketdata.acquire"
MARKET_DATA_REFRESH_TASK = "marketdata.refresh_stale"
MARKET_DATA_REFRESH_HOUR = 2  # UTC; allow time for late-published daily closes.
logger = logging.getLogger(__name__)


def create_celery_app(settings: TaskSettings | None = None) -> Celery:
    config = settings or TaskSettings()
    celery = Celery(
        "hedgehoog",
        broker=config.celery_broker_url.get_secret_value(),
        include=["tasks"],
        set_as_current=False,
    )
    celery.conf.update(
        task_default_queue="etfs",
        task_default_delivery_mode="persistent",
        task_serializer="json",
        accept_content=["json"],
        task_ignore_result=True,
        # A killed worker must not acknowledge an unfinished snapshot.
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        broker_connection_retry_on_startup=True,
        broker_connection_timeout=5,
        broker_transport_options={
            "confirm_publish": True,
            "read_timeout": 5,
            "write_timeout": 5,
        },
        # Keep API startup bounded if RabbitMQ cannot accept the task.
        task_publish_retry_policy={
            "max_retries": 2,
            "interval_start": 0,
            "interval_step": 1,
            "interval_max": 1,
        },
        beat_schedule={
            "daily-market-data-refresh": {
                "task": MARKET_DATA_REFRESH_TASK,
                "schedule": crontab(hour=MARKET_DATA_REFRESH_HOUR, minute=0),
                "options": {"queue": "etfs"},
            },
        },
        timezone="UTC",
        enable_utc=True,
    )
    return celery


app = create_celery_app()


def enqueue_etf_import(settings: TaskSettings) -> str:
    # Use this API instance's settings, not process-global defaults. No database
    # client or credentials are sent in the message; the worker owns its client.
    with create_celery_app(settings) as producer:
        result = producer.send_task(ETF_IMPORT_TASK)
    logger.info("Queued ETF import task %s", result.id)
    return result.id


def enqueue_market_data_refresh(settings: TaskSettings) -> str:
    with create_celery_app(settings) as producer:
        result = producer.send_task(MARKET_DATA_REFRESH_TASK, queue="etfs")
    logger.info("Queued market-data refresh sweep %s", result.id)
    return result.id


def enqueue_acquisition(settings: TaskSettings, job_id: str) -> str:
    # The message contains only a durable job ID, never credentials or payloads.
    with create_celery_app(settings) as producer:
        result = producer.send_task(ACQUISITION_TASK, args=[job_id], task_id=job_id, queue="marketdata")
    logger.info("Queued acquisition job %s", job_id)
    return result.id
