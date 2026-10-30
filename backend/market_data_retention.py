"""Latest-only quote retention. Inventory is non-deleting; cleanup is retryable.

Only provider-owned payloads with attributable job IDs are removed. Ambiguous
pre-routing records are reported, not guessed from customer/catalogue data.
"""
from contextlib import contextmanager
from datetime import timedelta
import argparse
import json
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

import market_data as md

PAYLOADS = ("quotes", "raw_eod_pages", "raw_split_pages", "split_snapshots", "instruments")
READER_SECONDS = 300
GATE_SECONDS = 300


def latest(db, isin, listing=None):
    query = {"instrument_id": isin, "provider": "yfinance", "published": True}
    selected = db.acquisition_jobs.find_one(query, sort=[("requested_at", -1), ("published_at", -1), ("_id", -1)])
    if selected and listing and selected.get("listing") != {key: listing[key] for key in ("symbol", "exchange")}:
        raise conflict()
    return selected


def conflict():
    return md.DataError(409, "This history generation is unavailable or superseded. Refresh instrument details and retry with the latest Yahoo history.")


@contextmanager
def reader(db, isin, listing, generation=None):
    """Lease before reading payloads; expired/racing readers fail at completion."""
    selected = latest(db, isin, listing)
    if selected is None:
        if generation is not None:
            raise conflict()
        # Never use legacy provider rows, including on Marketstack-only instruments.
        yield None
        if latest(db, isin, listing) is not None:
            raise conflict()
        return
    job_id = selected["_id"]
    if generation is not None and generation != job_id:
        raise conflict()
    for stale_token, expiry in selected.get("reader_leases", {}).items():
        if md.aware(expiry) <= md.utcnow():
            db.acquisition_jobs.update_one({"_id": job_id, f"reader_leases.{stale_token}": expiry},
                                           {"$unset": {f"reader_leases.{stale_token}": ""}})
    token = str(uuid4())
    expires = md.utcnow() + timedelta(seconds=READER_SECONDS)
    lease_field = f"reader_leases.{token}"
    acquired = db.acquisition_jobs.update_one({
        "_id": job_id, "published": True, "provider": "yfinance",
        "retention_state": {"$nin": ["retiring", "pruned"]},
    }, {"$set": {lease_field: expires}})
    if not acquired.matched_count:
        raise conflict()
    try:
        yield job_id
        current = latest(db, isin, listing)
        saved = db.acquisition_jobs.find_one({"_id": job_id}) or {}
        if (current is None or current["_id"] != job_id
                or saved.get("retention_state") in ("retiring", "pruned")
                or md.utcnow() >= expires or token not in saved.get("reader_leases", {})):
            raise conflict()
    finally:
        db.acquisition_jobs.update_one({"_id": job_id}, {"$unset": {lease_field: ""}})


@contextmanager
def reader_many(db, observations):
    """Lease explicit Yahoo identities in bounded commands, never reselect payloads.

    Validate latest ordering after acquisition and again at completion. Register
    each attempted chunk before writing so partial/ambiguous writes are released.
    """
    token = str(uuid4())
    now = md.utcnow()
    expires = now + timedelta(seconds=READER_SECONDS)
    field = f"reader_leases.{token}"

    def identity(row):
        if row.get("provider") != "yfinance" or row.get("job_id") in (None, "legacy") or not row.get("listing"):
            raise conflict()
        return {"_id": row["job_id"], "instrument_id": row["_id"], "listing": row["listing"]}

    def query(batch):
        return {"$or": [identity(row) for row in batch], "provider": "yfinance", "published": True,
                "retention_state": {"$nin": ["retiring", "pruned"]}}

    def pipeline(batch):
        return [
            {"$match": {"instrument_id": {"$in": [row["_id"] for row in batch]},
                        "provider": "yfinance", "published": True}},
            {"$project": {"instrument_id": 1, "listing": 1, "retention_state": 1,
                          "requested_at": 1, "published_at": 1, field: 1}},
            {"$sort": {"requested_at": -1, "published_at": -1, "_id": -1}},
            {"$group": {"_id": "$instrument_id", "job": {"$first": "$$ROOT"}}},
        ]

    # Remove only expired tokens, atomically preserving other admitted readers.
    update = [{"$set": {"reader_leases": {"$arrayToObject": {"$concatArrays": [
        {"$filter": {"input": {"$objectToArray": {"$ifNull": ["$reader_leases", {}]}},
                     "as": "lease", "cond": {"$gt": ["$$lease.v", now]}}},
        [{"k": token, "v": expires}],
    ]}}}}]
    command = lambda batch: {"update": "acquisition_jobs", "updates": [{"q": query(batch), "u": update, "multi": True}],
                             "validation": pipeline(batch)}
    batches = list(md._history_batches(observations, command))
    attempted = []

    def validate():
        for batch in batches:
            current = {row["_id"]: row["job"] for row in db.acquisition_jobs.aggregate(pipeline(batch))}
            for row in batch:
                job = current.get(row["_id"], {})
                expiry = job.get("reader_leases", {}).get(token)
                if (job.get("_id") != row["job_id"] or job.get("listing") != row["listing"]
                        or job.get("retention_state") in ("retiring", "pruned")
                        or expiry is None or md.aware(expiry) <= md.utcnow() or expires <= md.utcnow()):
                    raise conflict()

    try:
        for batch in batches:
            attempted.append(batch)
            if db.acquisition_jobs.update_many(query(batch), update).matched_count != len(batch):
                raise conflict()
        validate()
        yield
        validate()
    finally:
        # Try every chunk even if releasing an earlier one fails. A failed DB
        # release remains bounded by the same expiry used by the single reader.
        release_error = None
        for batch in attempted:
            try:
                db.acquisition_jobs.update_many({"_id": {"$in": [row["job_id"] for row in batch]}},
                                                {"$unset": {field: ""}})
            except Exception as exc:
                release_error = exc
        if release_error is not None:
            raise release_error


def inventory(db):
    """Report attributable data and ambiguity without deleting any records."""
    jobs = list(db.acquisition_jobs.find({"status": {"$nin": ["maintenance", "deleting"]}}))
    legacy_ids = [job["_id"] for job in jobs if job.get("provider", "marketstack") == "marketstack"]
    yahoo_ids = [job["_id"] for job in jobs if job.get("provider") == "yfinance"]
    return {
        "mode": "inventory", "marketstack_jobs": len(legacy_ids),
        "payloads": {name: {
            "marketstack": db[name].count_documents({"$or": [{"provider": "marketstack"}, {"job_id": {"$in": legacy_ids}}]}),
            "yahoo": db[name].count_documents({"provider": "yfinance"}),
            "unattributed": db[name].count_documents({"provider": {"$nin": ["marketstack", "yfinance"]}, "job_id": {"$nin": legacy_ids + yahoo_ids}}),
        } for name in PAYLOADS},
        "limitation": "Unattributed records are not deleted; investigate ownership before purging. MongoDB reuses deleted storage; filesystem size need not shrink.",
    }


def _gate(db, isin):
    owner = str(uuid4())
    now = md.utcnow()
    # Expired maintenance can resume without blocking acquisitions indefinitely.
    gate = db.acquisition_jobs.find_one_and_update({
        "instrument_id": isin, "active": True, "status": "maintenance",
        "lease_until": {"$lte": now},
    }, {"$set": {"owner": owner, "lease_until": now + timedelta(seconds=GATE_SECONDS)}}, return_document=ReturnDocument.AFTER)
    if gate is None:
        gate = {"_id": str(uuid4()), "instrument_id": isin, "active": True,
                "status": "maintenance", "owner": owner, "requested_at": now,
                "lease_until": now + timedelta(seconds=GATE_SECONDS)}
        try:
            db.acquisition_jobs.insert_one(gate)
        except DuplicateKeyError:
            return None
    return gate


def _heartbeat(db, gate):
    now = md.utcnow()
    result = db.acquisition_jobs.update_one({
        "_id": gate["_id"], "owner": gate["owner"], "active": True,
        "status": "maintenance", "lease_until": {"$gt": now},
    }, {
        "$set": {"lease_until": now + timedelta(seconds=GATE_SECONDS)},
    })
    if not result.matched_count:
        raise md.LeaseLost()


def _batches(db, collection, query, size, gate):
    removed = 0
    while True:
        _heartbeat(db, gate)
        ids = [row["_id"] for row in db[collection].find(query, {"_id": 1}).limit(size)]
        if not ids:
            return removed
        removed += db[collection].delete_many({"$and": [query, {"_id": {"$in": ids}}]}).deleted_count


def _prune_job(db, job, gate, size, counts):
    for name in PAYLOADS:
        identity = {"instrument_id": job["instrument_id"], "job_id": job["_id"],
                    "provider": {"$in": [job.get("provider", "marketstack"), None]}}
        if name == "split_snapshots":
            # Pre-routing snapshots used the job ID as their sole identity.
            identity.pop("job_id")
            identity["$or"] = [{"job_id": job["_id"]},
                               {"_id": job["_id"], "job_id": {"$exists": False}}]
        counts[name] = counts.get(name, 0) + _batches(db, name, identity, size, gate)
        _heartbeat(db, gate)
        db.market_data_retention.update_one({"_id": job["_id"]}, {"$set": {"checkpoint": name}}, upsert=True)


def _legacy_identity_conflict(db, job_id, isin):
    for name in ("acquisition_jobs", "market_data_publications"):
        record = db[name].find_one({"_id": job_id})
        if record and (record.get("provider", "marketstack") != "marketstack"
                       or record.get("instrument_id") != isin):
            return True
    return False


def retain_instrument(db, settings, isin):
    md.ensure_indexes(db)
    counts = {}
    waiting_for_readers = False
    # Compare against the pre-gate state; a stale cleaner cannot erase a newer
    # publication even if it resumes after another owner has released the gate.
    initial_state = db.instrument_state.find_one({"_id": isin}) or {}
    # Tagged orphan rows have explicit ownership even if their old job vanished.
    for name in (*PAYLOADS, "market_data_publications"):
        for row in db[name].find({"instrument_id": isin, "provider": "marketstack"}, {"job_id": 1}):
            job_id = row.get("job_id", row["_id"] if name == "market_data_publications" else None)
            if job_id is not None and not _legacy_identity_conflict(db, job_id, isin):
                db.market_data_retention.update_one({"_id": job_id}, {"$setOnInsert": {
                    "instrument_id": isin, "provider": "marketstack", "state": "retiring",
                }}, upsert=True)
    # Fence queued/running legacy work before acquiring the shared gate. Old
    # deliveries cannot claim these jobs again; their IDs remain in purge audit.
    legacy = list(db.acquisition_jobs.find({"instrument_id": isin, "status": {"$nin": ["maintenance", "deleting"]},
                                          "$or": [{"provider": "marketstack"}, {"provider": {"$exists": False}}]}))
    for job in legacy:
        if _legacy_identity_conflict(db, job["_id"], isin):
            continue
        db.market_data_retention.update_one({"_id": job["_id"]}, {"$setOnInsert": {
            "instrument_id": isin, "provider": "marketstack", "state": "retiring",
        }}, upsert=True)
        db.acquisition_jobs.update_one({"_id": job["_id"]}, {"$set": {"active": False, "status": "retired", "retention_state": "retiring"}})
    gate = _gate(db, isin)
    if gate is None:
        return {"instrument_id": isin, "skipped": "active acquisition/removal", "counts": counts}
    try:
        selected = latest(db, isin)  # Reselect only after holding the acquisition gate.
        selected_id = selected["_id"] if selected else None
        jobs = list(db.acquisition_jobs.find({"instrument_id": isin, "provider": "yfinance"}))
        protected = {job.get("base_generation") for job in jobs if job.get("active") or job.get("status") in ("queued", "running")}
        for job in jobs:
            if (job["_id"] == selected_id or job["_id"] in protected
                    or job.get("active") or job.get("status") in ("queued", "running")):
                continue
            if not job.get("published") and job.get("status") not in ("failed", "succeeded", "retired"):
                continue
            _heartbeat(db, gate)
            db.acquisition_jobs.update_one({"_id": job["_id"]}, {"$set": {"retention_state": "retiring"}})
            current = db.acquisition_jobs.find_one({"_id": job["_id"]})
            leases = current.get("reader_leases", {})
            if any(md.aware(expiry) > md.utcnow() for expiry in leases.values()):
                waiting_for_readers = True
                continue
            _prune_job(db, job, gate, settings.market_data_retention_batch_size, counts)
            db.acquisition_jobs.update_one({"_id": job["_id"]}, {
                "$set": {"retention_state": "pruned", "pruned_at": md.utcnow()}, "$unset": {"reader_leases": ""},
            })
            db.market_data_publications.update_one({"_id": job["_id"]}, {"$set": {"retention_state": "pruned"}})
            db.market_data_retention.update_one({"_id": job["_id"]}, {"$set": {
                "instrument_id": isin, "provider": "yfinance", "state": "pruned",
            }}, upsert=True)
        # Purge audit IDs are replayed on catch-up, including late writes from a
        # fenced, paused pre-retirement worker. No broad instrument deletions.
        for audit in db.market_data_retention.find({"instrument_id": isin, "provider": "marketstack"}):
            _heartbeat(db, gate)
            if _legacy_identity_conflict(db, audit["_id"], isin):
                db.market_data_retention.update_one({"_id": audit["_id"]}, {"$set": {"state": "ownership_conflict"}})
                continue
            _prune_job(db, audit, gate, settings.market_data_retention_batch_size, counts)
            for name in ("market_data_publications", "acquisition_jobs"):
                _heartbeat(db, gate)
                db[name].delete_many({"_id": audit["_id"], "instrument_id": isin,
                                      "provider": {"$in": ["marketstack", None]}})
            db.market_data_retention.update_one({"_id": audit["_id"]}, {"$set": {"state": "pruned"}})
        for name in (*PAYLOADS, "market_data_publications"):
            counts[name] = counts.get(name, 0) + _batches(
                db, name, {"instrument_id": isin, "provider": "marketstack"},
                settings.market_data_retention_batch_size, gate,
            )
        # Clear only retired provider mapping fields; preserve manual currency
        # metadata and every excluded user-owned record.
        _heartbeat(db, gate)
        if selected is None and (legacy or db.market_data_retention.find_one({"instrument_id": isin, "provider": "marketstack"})):
            db.instrument_state.update_one({"_id": isin, **{
                key: initial_state.get(key) for key in ("listing", "listing_job_id", "last_complete_quote_date")
            }}, {"$unset": {
                key: "" for key in ("listing", "listing_resolution", "listing_job_id", "last_complete_quote_date")
            }})
        _heartbeat(db, gate)
        db.instrument_statistics.delete_one({"_id": isin})
        _heartbeat(db, gate)
        db.market_data_retention_runs.update_one({"_id": isin}, {"$set": {
            "finished_at": md.utcnow(), "counts": counts, "error": None,
        }}, upsert=True)
        return {"instrument_id": isin, "counts": counts,
                **({"skipped": "reader leases still active"} if waiting_for_readers else {})}
    except Exception as exc:
        db.market_data_retention_runs.update_one({"_id": isin}, {"$set": {"error": type(exc).__name__}}, upsert=True)
        raise
    finally:
        db.acquisition_jobs.delete_one({"_id": gate["_id"], "owner": gate["owner"]})


def cleanup(db, settings, isin=None):
    if not settings.market_data_retention_enabled:
        return {"disabled": True, "items": []}
    # Global provider discovery runs before retain_instrument's index setup.
    md.ensure_indexes(db)
    identifiers = {isin} if isin else set(db.acquisition_jobs.distinct("instrument_id")) | set(db.market_data_retention.distinct("instrument_id"))
    if isin is None:
        for name in (*PAYLOADS, "market_data_publications"):
            identifiers.update(db[name].distinct("instrument_id", {"provider": "marketstack"}))
    results = [retain_instrument(db, settings, item) for item in sorted(value for value in identifiers if value)]
    if isin is None:
        # Usage is exclusively owned by the removed provider.
        db.marketstack_usage.delete_many({})
    return {"items": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", action="store_true", help="Non-deleting inventory (default)")
    parser.add_argument("--apply", action="store_true", help="Apply retention to the configured local database")
    args = parser.parse_args()
    if args.inventory and args.apply:
        parser.error("Choose inventory or apply, not both")
    from config import TaskSettings
    from pymongo import MongoClient
    settings = TaskSettings()
    with MongoClient(settings.mongodb_uri.get_secret_value(), serverSelectionTimeoutMS=5000) as client:
        db = client[settings.mongodb_database]
        print(json.dumps(cleanup(db, settings) if args.apply else inventory(db)))


if __name__ == "__main__":
    main()
