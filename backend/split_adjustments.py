"""Corporate-action evidence and analytics-only split normalization.

Marketstack split_factor is new shares / old shares (25 for a 25:1 split,
0.5 for a 1:2 reverse split). Never apply it to provider-adjusted prices.
"""

from decimal import Decimal, DecimalException, localcontext
import math

from bson import Decimal128
from pymongo import DESCENDING

import market_data as md
from market_provider import MarketDataError

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
                raise MarketDataError("invalid_split_history", "Split history contains invalid or conflicting actions. Raw evidence was saved; retry market data before using derived statistics.") from None
    return [{"date": day, "factor": str(factor)} for day, factor in sorted(events.items())]


def load_provider_split_snapshot(db, isin, listing, cutoff, generation=None):
    if not listing:
        return None
    generation = generation or md.history_generation(db, isin, listing, cutoff)
    publication = md.published_history(db, isin, listing, cutoff, generation)
    snapshot = db.split_snapshots.find_one({
        **md.observation_filter(db, isin, listing, cutoff, generation), "collected_at": {"$lte": cutoff},
    }, sort=[("requested_at", DESCENDING), ("_id", DESCENDING)])
    if publication and publication["provider"] == "yfinance":
        # Yahoo actions are audit evidence, already reflected in Yahoo Close.
        # A failed later refresh cannot invalidate the published generation.
        return snapshot
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


def load_split_snapshot(db, isin, listing, cutoff, generation=None):
    """Overlay manual evidence without mutating or replacing provider snapshots."""
    from manual_splits import manual_events

    snapshot = load_provider_split_snapshot(db, isin, listing, cutoff, generation)
    if snapshot and snapshot.get("provider") == "yfinance":
        return snapshot  # Legacy manual adjustments must not affect Yahoo prices.
    manual = manual_events(db, isin, listing, cutoff)
    if not manual:
        return snapshot
    # Immutable manual IDs make add/remove/replacement invalidate cached estimates.
    identity = {"provider": snapshot["_id"] if snapshot else None, "manual": [event["_id"] for event in manual]}
    if snapshot and snapshot.get("error"):
        return {**snapshot, "_id": identity}
    merged = dict(snapshot) if snapshot else {
        "date_from": "0001-01-01", "date_to": cutoff.date().isoformat(), "events": [],
    }
    events = {event["date"]: event["factor"] for event in merged["events"]}
    for event in manual:
        day, factor = event["date"], event["factor"]
        if day in events and Decimal(events[day]) != Decimal(factor):
            return {"_id": identity, "error": f"Manual and provider split factors conflict on {day}. Remove the manual entry or correct provider history before using derived statistics."}
        events[day] = factor  # Matching provider/manual entries apply exactly once.
    merged.update(_id=identity, events=[{"date": day, "factor": factor} for day, factor in sorted(events.items())], manual=True)
    if snapshot is None:
        merged["manual_only"] = True
    return merged


def adjusted_rows(rows, snapshot):
    """Return copies with a separate derived close, never mutate raw evidence.

    Forward normalization multiplies raw close by cumulative splits effective
    on/before that date. It has the same return ratios as backward adjustment,
    but future actions cannot change an earlier training prefix, even by
    floating-point rounding. Effective dates need not have a saved candle.
    """
    if any(row.get("price_basis") == "provider_split_adjusted" for row in rows):
        if not all(row.get("price_basis") == "provider_split_adjusted" for row in rows):
            return [{**row, BASIS: None} for row in rows], ["Mixed provider price bases cannot be used for derived returns."]
        result = []
        for row in rows:
            value = row.get("close")
            corrected = None
            try:
                if not isinstance(value, bool) and value is not None:
                    price = Decimal(str(value))
                    if price.is_finite() and price > 0 and math.isfinite(float(price)):
                        corrected = str(price)
            except (DecimalException, ValueError, TypeError, OverflowError):
                pass
            result.append({**row, BASIS: corrected})
        return result, ["Derived returns use Yahoo's already split-adjusted Close, without applying split factors again. Cash distributions are not reinvested; Adj Close is retained separately. Historical prices may be revised by Yahoo."]
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
    evidence = ("manually supplied splits" if snapshot.get("manual_only") else
                "downloaded and manually supplied splits" if snapshot.get("manual") else "downloaded splits")
    warnings = [f"Derived returns use raw closes corrected for {evidence} only; cash distributions are not reinvested. Candles and quotes remain unadjusted."]
    if snapshot.get("manual_only"):
        warnings.append("Provider split history has not been downloaded; only manually supplied splits are applied. Other actions may be missing.")
    return result, warnings
