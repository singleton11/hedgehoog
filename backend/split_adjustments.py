"""Corporate-action evidence and analytics-only split normalization.

Marketstack split_factor is new shares / old shares (25 for a 25:1 split,
0.5 for a 1:2 reverse split). Never apply it to provider-adjusted prices.
"""

from decimal import Decimal, DecimalException, localcontext
import math

from bson import Decimal128
from pymongo import DESCENDING

import market_data as md
from marketstack import MarketstackError

BASIS = "split_adjusted_close"


def split_events(pages, listing, start, end):
    """Validate derived inputs only; the complete raw pages are already saved.

    Duplicate date/factor rows represent the same action, not extra splits.
    Conflicting duplicates and malformed/mismatched actions fail closed rather
    than silently producing a plausible but wrong return series.
    """
    events = {}
    for page in pages:
        for row in page["data"]["data"]:
            try:
                if not isinstance(row, dict):
                    raise ValueError
                day = md.quote_date(row.get("date"))
                if day is None or row.get("symbol") != listing["symbol"]:
                    raise ValueError
                if row.get("exchange") not in (None, listing["exchange"]):
                    raise ValueError
                if not start <= day <= end:
                    raise ValueError
                raw = row.get("split_factor")
                if isinstance(raw, bool) or raw is None:
                    raise ValueError
                factor = Decimal(str(raw))
                if not factor.is_finite() or factor <= 0:
                    raise ValueError
                if day in events and events[day] != factor:
                    raise ValueError
                events[day] = factor
            except (ValueError, TypeError, DecimalException):
                raise MarketstackError("invalid_split_history", "Split history contains invalid or conflicting actions. Raw evidence was saved; retry market data before using derived statistics.") from None
    return [{"date": day, "factor": str(factor)} for day, factor in sorted(events.items())]


def load_split_snapshot(db, isin, listing, cutoff):
    if not listing:
        return None
    snapshot = db.split_snapshots.find_one({
        **md.listing_filter(isin, listing), "collected_at": {"$lte": cutoff},
    }, sort=[("requested_at", DESCENDING), ("_id", DESCENDING)])
    # New acquisitions must not silently fall back to uncorrected prices after
    # a failed/unfinished split download. Legacy data remains readable with a
    # warning until the first explicit refresh (no provider calls from reads).
    attempt = db.acquisition_jobs.find_one({
        "instrument_id": isin, "listing": listing, "splits_date_from": {"$exists": True},
        "requested_at": {"$lte": cutoff},
    }, sort=[("requested_at", DESCENDING), ("_id", DESCENDING)])
    if attempt and attempt.get("calls", 0) > 0 and not attempt.get("active") and (snapshot is None or attempt["_id"] != snapshot["_id"]):
        return {"_id": attempt["_id"], "error": "Split history download is incomplete. Request market data again before using derived statistics."}
    return snapshot


def adjusted_rows(rows, snapshot):
    """Return copies with a separate derived close, never mutate raw evidence.

    Forward normalization multiplies raw close by cumulative splits effective
    on/before that date. It has the same return ratios as backward adjustment,
    but future actions cannot change an earlier training prefix, even by
    floating-point rounding. Effective dates need not have a saved candle.
    """
    if snapshot is None:
        return rows, ["Split history has not been downloaded. Update market data to correct derived returns for splits."]
    error = snapshot.get("error")
    if rows and not error and (snapshot["date_from"] > rows[0]["date"] or snapshot["date_to"] < rows[-1]["date"]):
        error = "Saved split history does not cover these quotes. Update market data before using derived statistics."
    if error:
        return [{**row, BASIS: None} for row in rows], [error]
    events = iter(snapshot["events"])
    event = next(events, None)
    result = []
    with localcontext() as context:
        context.prec = 50
        factor = Decimal(1)
        for row in rows:
            while event is not None and event["date"] <= row["date"]:
                try:
                    factor *= Decimal(event["factor"])
                except DecimalException:
                    factor = Decimal("NaN")
                event = next(events, None)
            value = row.get("close")
            if isinstance(value, Decimal128):
                value = value.to_decimal()
            price = None
            try:
                if not isinstance(value, bool) and value is not None:
                    raw = Decimal(str(value))
                    corrected = raw * factor
                    if corrected.is_finite() and corrected > 0:
                        number = float(corrected)
                        if math.isfinite(number) and number > 0:
                            price = str(corrected)
            except (DecimalException, ValueError, TypeError, OverflowError):
                pass
            result.append({**row, BASIS: price})
    return result, ["Derived returns use raw closes corrected for downloaded splits only; cash distributions are not reinvested. Candles and quotes remain unadjusted."]
