"""Background catalogue tasks. MongoDB's imports collection records completion."""

from pymongo import MongoClient
from pymongo.errors import PyMongoError

from celery_app import ACQUISITION_TASK, ETF_IMPORT_TASK, app
from config import TaskSettings
from etfs import ETFImportError, import_etfs
from market_data import JobBusy, run_acquisition


@app.task(
    name=ETF_IMPORT_TASK,
    autoretry_for=(ETFImportError, PyMongoError),
    retry_backoff=30,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def import_etfs_task() -> int:
    # Create after the worker forks, and close on success, skip, or failure.
    # Each attempt captures its own UTC date in import_etfs; no past-day backfill.
    config = TaskSettings()
    with MongoClient(
        config.mongodb_uri.get_secret_value(), serverSelectionTimeoutMS=5000,
    ) as client:
        return import_etfs(client[config.mongodb_database])


@app.task(name=ACQUISITION_TASK, bind=True, max_retries=12, soft_time_limit=600, time_limit=660)
def acquire_instrument_task(self, job_id: str):
    config = TaskSettings()
    try:
        with MongoClient(
            config.mongodb_uri.get_secret_value(), serverSelectionTimeoutMS=5000,
        ) as client:
            return run_acquisition(client[config.mongodb_database], config, job_id)
    except JobBusy:
        # A redelivered message waits for a live lease rather than doing paid work.
        raise self.retry(countdown=30) from None
    except PyMongoError:
        # Provider failures are terminal and explicit. Only persistence failures
        # retry automatically, resuming already-written profile/page checkpoints.
        raise self.retry(countdown=30) from None
