"""Immutable successful calibration runs, with owner-scoped legacy import."""

import hashlib
import json

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from goal_strategies import STRATEGIES
import market_data as md


SUMMARY_FIELDS = (
    "id", "strategy_name", "strategy", "calculated_at", "settings", "seed", "brier_score",
    "calibration_error", "sample_count", "origin_count",
)
RUN_ORDER = [("order", -1), ("_id", -1)]


def ensure_indexes(db):
    db.calibration_runs.create_index([("user_id", 1), *RUN_ORDER], name="calibration_runs_owner_order")
    db.calibration_runs.create_index(
        [("user_id", 1), ("strategy_id", 1), *RUN_ORDER], name="calibration_runs_owner_strategy_order",
    )


def result_key(user_id, strategy_id):
    # Legacy documents have no owner field: only server-authenticated identity
    # and registered strategy IDs may be used to discover their exact keys.
    return hashlib.sha256(json.dumps([str(user_id), strategy_id]).encode()).hexdigest()


def calculation_order():
    # Keep microseconds as text (BSON datetimes truncate to milliseconds), with
    # a tie breaker for simultaneous starts. Allocate BEFORE doing the work.
    return f"{md.utcnow().isoformat(timespec='microseconds')}:{ObjectId()}"


def _run_document(user_id, strategy_id, result, order, run_id, strategy_name):
    full = {**result, "id": run_id, "strategy_name": strategy_name}
    return {
        "_id": run_id, "user_id": str(user_id), "strategy_id": strategy_id, "order": order,
        "result": full, "summary": {field: full[field] for field in SUMMARY_FIELDS},
    }


def import_legacy_results(db, user_id, strategy_ids=None):
    owner = str(user_id)
    strategies = STRATEGIES if strategy_ids is None else strategy_ids
    keys = {result_key(owner, strategy_id): strategy_id for strategy_id in strategies if strategy_id in STRATEGIES}
    if not keys:
        return
    # Once imported, never reload a potentially large legacy report or refresh
    # its snapshot. Newly registered strategies/users are discovered next time.
    imported = {saved["_id"] for saved in db.calibration_runs.find(
        {"user_id": owner, "_id": {"$in": [f"legacy-{key}" for key in keys]}}, {"_id": 1},
    )}
    missing = [key for key in keys if f"legacy-{key}" not in imported]
    if not missing:
        return
    for legacy in db.strategy_calibrations.find({"_id": {"$in": missing}}, {"result": 1, "order": 1}):
        strategy_id = keys[legacy["_id"]]
        document = _run_document(
            owner, strategy_id, legacy["result"],
            legacy.get("order", legacy["result"].get("calculated_at", "")),
            f"legacy-{legacy['_id']}", STRATEGIES[strategy_id].name,
        )
        query = {"_id": document["_id"], "user_id": owner}
        try:
            # Full report and summary become visible together. A concurrent
            # importer can only win the insert, never replace an existing run.
            db.calibration_runs.update_one(query, {"$setOnInsert": document}, upsert=True)
        except DuplicateKeyError:
            if db.calibration_runs.find_one(query, {"_id": 1}) is None:
                raise


def save_result(db, user_id, strategy_id, result, order, *, strategy_name=None):
    import_legacy_results(db, user_id)
    document = _run_document(
        user_id, strategy_id, result, order, str(ObjectId()),
        STRATEGIES[strategy_id].name if strategy_name is None else strategy_name,
    )
    # One immutable insert per successful calculation, including identical seed
    # replays. Failed calculations never call this function.
    db.calibration_runs.insert_one(document)
    return document["result"]


def run_history(db, user_id, limit=20, offset=0):
    import_legacy_results(db, user_id)
    query = {"user_id": str(user_id)}
    items = db.calibration_runs.find(query, {"summary": 1, "_id": 0}).sort(RUN_ORDER).skip(offset).limit(limit)
    return {"items": [saved["summary"] for saved in items], "total": db.calibration_runs.count_documents(query),
            "limit": limit, "offset": offset}


def run_result(db, user_id, run_id):
    import_legacy_results(db, user_id)
    saved = db.calibration_runs.find_one({"_id": run_id, "user_id": str(user_id)}, {"result": 1, "_id": 0})
    return saved["result"] if saved else None


def latest_result(db, user_id, strategy_id):
    import_legacy_results(db, user_id, [strategy_id])
    saved = db.calibration_runs.find_one(
        {"user_id": str(user_id), "strategy_id": strategy_id}, {"result": 1, "_id": 0}, sort=RUN_ORDER,
    )
    return saved["result"] if saved else None


def latest_summaries(db, user_id, strategy_ids):
    strategy_ids = list(strategy_ids)
    import_legacy_results(db, user_id, strategy_ids)
    summaries = {}
    # One bounded, indexed summary lookup per strategy; don't transfer all past
    # runs or their audit rows merely to display the catalogue's latest scores.
    for strategy_id in strategy_ids:
        saved = db.calibration_runs.find_one(
            {"user_id": str(user_id), "strategy_id": strategy_id}, {"summary": 1, "_id": 0}, sort=RUN_ORDER,
        )
        if saved:
            summaries[strategy_id] = saved["summary"]
    return summaries
