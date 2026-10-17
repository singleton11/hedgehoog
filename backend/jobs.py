"""Read-only job monitoring and best-effort task lifecycle persistence.

Acquisitions remain authoritative in acquisition_jobs. Background tasks use a
separate collection so monitoring never interferes with acquisition leases.
"""

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from pymongo.errors import PyMongoError

from auth import CurrentUser

logger = logging.getLogger(__name__)
JobStatus = Literal["queued", "running", "retrying", "succeeded", "failed"]
JobType = Literal["market_data_acquisition", "etf_import", "market_data_refresh"]
STATUSES = ("queued", "running", "retrying", "succeeded", "failed")
JOB_TYPES = [
    {"value": "market_data_acquisition", "label": "Market data acquisition"},
    {"value": "etf_import", "label": "ETF catalogue import"},
    {"value": "market_data_refresh", "label": "Market data refresh"},
]
SAFE_ERRORS = {
    "failed": "Job failed. Check the worker or service configuration.",
    "retrying": "Job interrupted; an automatic retry is pending.",
}
router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobItem(BaseModel):
    id: str
    job_type: JobType
    status: JobStatus
    instrument_id: str | None
    requested_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None


class JobCounts(BaseModel):
    total: int
    queued: int
    running: int
    retrying: int
    succeeded: int
    failed: int


class JobTypeOption(BaseModel):
    value: JobType
    label: str


class JobsResponse(BaseModel):
    items: list[JobItem]
    total: int
    counts: JobCounts
    job_types: list[JobTypeOption]


def utcnow():
    return datetime.now(timezone.utc)


def _utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def list_jobs(db, *, status=None, job_type=None, limit=50, offset=0):
    counts = {"total": 0, **dict.fromkeys(STATUSES, 0)}
    total = 0
    candidates = []
    sources = (
        (db.acquisition_jobs, "market_data_acquisition", {"status": {"$in": STATUSES}}),
        (db.background_jobs, None, {"status": {"$in": STATUSES}, "job_type": {"$in": ["etf_import", "market_data_refresh"]}}),
    )
    # Only project public metadata: provider payloads and saved exception strings
    # never enter the response. Deletion mutex records are not acquisitions.
    projection = {key: 1 for key in ("job_type", "status", "instrument_id", "requested_at", "started_at", "finished_at")}
    for collection, fixed_type, base in sources:
        for group in collection.aggregate([{"$match": base}, {"$group": {"_id": "$status", "count": {"$sum": 1}}}]):
            counts[group["_id"]] += group["count"]
            counts["total"] += group["count"]
        if fixed_type and job_type and job_type != fixed_type:
            continue
        query = dict(base)
        if status:
            query["status"] = status
        if job_type and not fixed_type:
            query["job_type"] = job_type
        total += collection.count_documents(query)
        # Each source need only contribute its first offset+limit candidates to
        # the merged newest-first page; no migration or provider calls required.
        for row in collection.find(query, projection).sort([("requested_at", -1), ("_id", -1)]).limit(offset + limit):
            row_type = fixed_type or row["job_type"]
            candidates.append({
                "id": str(row["_id"]), "job_type": row_type,
                "status": row["status"], "instrument_id": row.get("instrument_id"),
                **{key: _utc(row.get(key)) for key in ("requested_at", "started_at", "finished_at")},
                "error": SAFE_ERRORS.get(row["status"]),
            })
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    candidates.sort(key=lambda row: (row["requested_at"] or oldest, row["id"], row["job_type"]), reverse=True)
    return {"items": candidates[offset:offset + limit], "total": total, "counts": counts, "job_types": JOB_TYPES}


@router.get("", response_model=JobsResponse)
def get_jobs(
    request: Request, user: CurrentUser,
    status: JobStatus | Literal[""] | None = None,
    job_type: JobType | Literal[""] | None = None,
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
):
    return list_jobs(request.app.state.database, status=status, job_type=job_type, limit=limit, offset=offset)


def ensure_indexes(db):
    db.acquisition_jobs.create_index([("status", 1), ("requested_at", -1), ("_id", -1)])
    db.background_jobs.create_index([("status", 1), ("requested_at", -1), ("_id", -1)])
    db.background_jobs.create_index([("job_type", 1), ("status", 1), ("requested_at", -1), ("_id", -1)])


def _update(db, job_id, update, *, upsert=False):
    try:
        db.background_jobs.update_one({"_id": job_id}, update, upsert=upsert)
    except PyMongoError:
        # Observability must not turn a successful import into a failed task or
        # change its retries. Do not log driver messages containing connection URIs.
        logger.warning("Could not persist background job lifecycle")


def queue_job(db, job_id, job_type):
    _update(db, job_id, {"$setOnInsert": {
        "job_type": job_type, "status": "queued", "requested_at": utcnow(),
        "started_at": None, "finished_at": None, "error": None,
    }}, upsert=True)


def finish_job(db, job_id, status):
    _update(db, job_id, {"$set": {
        "status": status, "finished_at": utcnow() if status != "retrying" else None,
        "error": SAFE_ERRORS.get(status),
    }})


@contextmanager
def track_execution(db, task, job_type, retryable):
    # Beat has no API database connection. Its first worker attempt creates the
    # record; Celery retains request.id when retrying or redelivering a message.
    job_id = task.request.id or str(uuid4())
    now = utcnow()
    _update(db, job_id, {
        "$setOnInsert": {"job_type": job_type, "requested_at": now},
        "$set": {"status": "running", "started_at": now, "finished_at": None, "error": None},
    }, upsert=True)
    try:
        yield
    except Exception as exc:
        status = "retrying" if isinstance(exc, retryable) and task.request.retries < task.max_retries else "failed"
        finish_job(db, job_id, status)
        raise
    else:
        finish_job(db, job_id, "succeeded")
