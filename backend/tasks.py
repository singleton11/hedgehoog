"""Background catalogue tasks. MongoDB's imports collection records completion."""

from pymongo import MongoClient
from pymongo.errors import PyMongoError

from celery_app import ETF_IMPORT_TASK, app
from config import TaskSettings
from etfs import ETFImportError, import_etfs


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
