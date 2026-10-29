"""Background catalogue tasks. MongoDB's imports collection records completion."""

from kombu.exceptions import OperationalError
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from celery_app import (
    ACQUISITION_TASK, ETF_IMPORT_TASK, MARKET_DATA_REFRESH_TASK, MARKET_DATA_RETENTION_TASK, app,
    enqueue_market_data_refresh,
)
from config import TaskSettings
from etfs import ETFImportError, import_etfs
from market_data import DataError, JobBusy, run_acquisition
from market_data_refresh import refresh_stale_market_data
from jobs import track_execution


@app.task(
    name=ETF_IMPORT_TASK,
    bind=True,
    autoretry_for=(ETFImportError, PyMongoError, OperationalError),
    retry_backoff=30,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def import_etfs_task(self) -> int:
    # Create after the worker forks, and close on success, skip, or failure.
    # Each attempt captures its own UTC date in import_etfs; no past-day backfill.
    config = TaskSettings()
    with MongoClient(
        config.mongodb_uri.get_secret_value(), serverSelectionTimeoutMS=5000,
    ) as client:
        db = client[config.mongodb_database]
        with track_execution(db, self, "etf_import", (ETFImportError, PyMongoError, OperationalError)):
            count = import_etfs(db)
            # Even a same-day skipped import must acquire missing catalogue history.
            # Retrying a failed dispatch safely reuses the completed import marker.
            enqueue_market_data_refresh(config, db, include_catalogue=True)
            return count


@app.task(
    name=MARKET_DATA_REFRESH_TASK,
    bind=True,
    autoretry_for=(PyMongoError, DataError),
    retry_backoff=30,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def refresh_market_data_task(self, include_catalogue=False):
    config = TaskSettings()
    with MongoClient(
        config.mongodb_uri.get_secret_value(), serverSelectionTimeoutMS=5000,
    ) as client:
        db = client[config.mongodb_database]
        options = {"include_catalogue": True} if include_catalogue else {}
        with track_execution(db, self, "market_data_refresh", (PyMongoError, DataError)):
            return refresh_stale_market_data(db, config, **options)


@app.task(name=MARKET_DATA_RETENTION_TASK, bind=True,
          autoretry_for=(PyMongoError, JobBusy), retry_backoff=30,
          retry_backoff_max=600, retry_jitter=True, max_retries=12)
def retain_market_data_task(self, instrument_id=None):
    from market_data_retention import cleanup
    config = TaskSettings()
    with MongoClient(config.mongodb_uri.get_secret_value(), serverSelectionTimeoutMS=5000) as client:
        result = cleanup(client[config.mongodb_database], config, instrument_id)
        if any(item.get("skipped") for item in result.get("items", [])):
            raise JobBusy()
        return result


@app.task(name=ACQUISITION_TASK, bind=True, max_retries=12, soft_time_limit=600, time_limit=660)
def acquire_instrument_task(self, job_id: str):
    config = TaskSettings()
    try:
        with MongoClient(
            config.mongodb_uri.get_secret_value(), serverSelectionTimeoutMS=5000,
        ) as client:
            result = run_acquisition(client[config.mongodb_database], config, job_id)
            if result and result.get("status") in ("succeeded", "failed", "retired"):
                # Cleanup dispatch cannot change the acquisition outcome. Daily
                # catch-up covers a broker failure after durable completion.
                try:
                    app.send_task(MARKET_DATA_RETENTION_TASK,
                                  args=[result["instrument_id"]], queue="etfs")
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning("Retention dispatch failed; daily catch-up will retry")
            return result
    except JobBusy:
        # A redelivered message waits for a live lease rather than doing paid work.
        raise self.retry(countdown=30) from None
    except PyMongoError:
        # Provider failures are terminal and explicit. Only persistence failures
        # retry automatically, resuming already-written profile/page checkpoints.
        raise self.retry(countdown=30) from None
