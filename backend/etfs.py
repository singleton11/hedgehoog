"""Import the ETF catalogue from etfdb before accepting API requests."""

import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from pymongo import ReplaceOne, ReturnDocument
from pymongo.errors import OperationFailure, PyMongoError

ETF_SOURCE_URL = "https://raw.githubusercontent.com/albertored/etfdb/refs/heads/main/json/etfs.json"
logger = logging.getLogger(__name__)


class ETFImportError(RuntimeError):
    """The startup catalogue download, validation or persistence failed."""


def download_etfs() -> list[dict[str, Any]]:
    try:
        response = httpx.get(
            ETF_SOURCE_URL,
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=True,
        )
        response.raise_for_status()
        records = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ETFImportError(f"Could not download ETF catalogue from {ETF_SOURCE_URL}") from exc

    # Validate the entire response before writing any ETF documents. Keep all
    # source fields, including nested holdings and future attributes, unchanged.
    if not isinstance(records, list) or not records:
        raise ETFImportError("ETF catalogue must be a non-empty JSON array")
    seen = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ETFImportError(f"ETF catalogue record {index} must be an object")
        isin = record.get("isin")
        if not isinstance(isin, str) or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin):
            raise ETFImportError(f"ETF catalogue record {index} has an invalid ISIN")
        if isin in seen:
            raise ETFImportError(f"ETF catalogue contains duplicate ISIN {isin}")
        for field in ("_id", "import_date"):
            if field in record:
                raise ETFImportError(f"ETF catalogue record {index} contains reserved field {field}")
        seen.add(isin)
    return records


def import_etfs(database) -> int:
    """Import a UTC daily snapshot; return the record count, or zero if complete."""
    # Capture once so a download crossing midnight stays in the same daily run.
    import_date = datetime.now(timezone.utc).date().isoformat()
    try:
        imports = database.imports
        imports.create_index("date", unique=True)
        status = imports.find_one_and_update(
            {"date": import_date},
            {"$setOnInsert": {"etfs": False}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        if status.get("etfs") is True:
            logger.info("ETF import for %s is already complete; skipping", import_date)
            return 0

        logger.info("Downloading ETF catalogue for %s from %s", import_date, ETF_SOURCE_URL)
        records = download_etfs()
        collection = database.etfs
        collection.create_index([("isin", 1), ("import_date", 1)], unique=True)
        # Migrate the old ISIN-only constraint after establishing the new one.
        # Preserve legacy undated documents: their actual import date is unknown.
        for index in collection.list_indexes():
            if index.get("unique") and list(index["key"].items()) == [("isin", 1)]:
                try:
                    collection.drop_index(index["name"])
                except OperationFailure as exc:
                    if exc.code != 27:  # Another startup may have dropped it already.
                        raise

        # A retry replaces today's records (including removed source fields),
        # preserving their IDs and all earlier snapshots. Missing ISINs remain.
        collection.bulk_write(
            [
                ReplaceOne(
                    {"isin": record["isin"], "import_date": import_date},
                    {**record, "import_date": import_date},
                    upsert=True,
                )
                for record in records
            ],
            ordered=True,
        )
        # Never mark completion until every ETF write has been acknowledged.
        # Failures leave the marker false so the next startup retries the day.
        imports.update_one({"date": import_date}, {"$set": {"etfs": True}})
    except PyMongoError as exc:
        raise ETFImportError("Could not persist ETF catalogue or import status in MongoDB") from exc
    logger.info("Imported %d ETFs for %s into MongoDB collection etfs", len(records), import_date)
    return len(records)
