"""Cross-process Yahoo request pacing using the application's shared MongoDB."""

from datetime import datetime, timedelta, timezone
import time

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError


_KEY = "yahoo_http"


def utcnow():
    return datetime.now(timezone.utc)


def wait_for_yahoo_request(db, interval, heartbeat):
    """Grant a slot only when due; never prebook slots that can bunch after a stall.

    Workers must share the database and synchronized UTC clocks (as Compose
    containers do). A process-local lock or Celery task rate limit would not
    coordinate all outbound HTTP calls across workers.
    """
    if interval == 0:
        return
    collection = db.provider_request_pacing
    try:
        collection.update_one(
            {"_id": _KEY},
            {"$setOnInsert": {"next_request_at": datetime(1970, 1, 1, tzinfo=timezone.utc)}},
            upsert=True,
        )
    except DuplicateKeyError:
        # Another process initialized the same singleton document.
        pass
    while True:
        heartbeat()
        now = utcnow()
        next_at = now + timedelta(seconds=interval)
        # BSON truncates dates to milliseconds; round up to preserve the minimum.
        remainder = next_at.microsecond % 1000
        if remainder:
            next_at += timedelta(microseconds=1000 - remainder)
        granted = collection.find_one_and_update(
            {"_id": _KEY, "next_request_at": {"$lte": now}},
            {"$set": {"next_request_at": next_at}},
            return_document=ReturnDocument.AFTER,
        )
        if granted is not None:
            return
        state = collection.find_one({"_id": _KEY})
        # Never bypass pacing on an unexpected missing state or DB failure.
        next_at = state["next_request_at"] if state else now + timedelta(seconds=interval)
        if next_at.tzinfo is None:
            next_at = next_at.replace(tzinfo=timezone.utc)
        # Renew/fence job ownership even with a long interval or clock adjustment.
        time.sleep(min(1.0, max(0.001, (next_at - utcnow()).total_seconds())))
