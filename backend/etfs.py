"""Import the ETF catalogue from etfdb before accepting API requests."""

import logging
import re
from typing import Any

import httpx
from pymongo import ReplaceOne
from pymongo.errors import PyMongoError

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

    # Validate the entire response before any database writes. Keep all other
    # fields, including nested holdings and future source attributes, unchanged.
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
        if "_id" in record:
            raise ETFImportError(f"ETF catalogue record {index} contains reserved field _id")
        seen.add(isin)
    return records


def import_etfs(database) -> int:
    logger.info("Downloading ETF catalogue from %s", ETF_SOURCE_URL)
    records = download_etfs()
    try:
        collection = database.etfs
        collection.create_index("isin", unique=True)
        # Replacement removes fields dropped upstream while preserving MongoDB
        # IDs. Missing ISINs are not deleted; a smaller feed must not erase data.
        collection.bulk_write(
            [ReplaceOne({"isin": record["isin"]}, record, upsert=True) for record in records],
            ordered=True,
        )
    except PyMongoError as exc:
        raise ETFImportError("Could not persist ETF catalogue in MongoDB collection etfs") from exc
    logger.info("Imported %d ETFs into MongoDB collection etfs", len(records))
    return len(records)
