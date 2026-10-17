"""Calculation-only missing closes. Raw observations are never changed.

The caller supplies ascending, latest-per-date rows bounded by its knowledge
cutoff. Explicit invalid prices (zero/negative/infinite) are not missing prices.
"""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
import random
from typing import Literal

from bson import Decimal128
from pydantic import BaseModel, ConfigDict, Field

VERSION = 1
KEY = "market_data"


class QuoteFillSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["smooth", "normal"] = "smooth"
    seed: int = Field(default=0, ge=0, le=2**32 - 1, strict=True)


def read_policy(db):
    record = db.app_settings.find_one({"_id": KEY}) or {}
    return {"version": VERSION, **QuoteFillSettings.model_validate(
        {key: record[key] for key in ("method", "seed") if key in record}).model_dump()}


def currency(row, override=None):
    value = row.get("price_currency")
    return value.strip().upper() if isinstance(value, str) and value.strip() else override


def positive(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal128):
        value = value.to_decimal()
    try:
        value = Decimal(str(value))
        return value if value.is_finite() and value > 0 and math.isfinite(float(value)) and float(value) > 0 else None
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None


def missing(value):
    if value is None:
        return True
    if isinstance(value, Decimal128):
        value = value.to_decimal()
    try:
        return not isinstance(value, bool) and Decimal(str(value)).is_nan()
    except (InvalidOperation, ValueError, TypeError):
        return False


def adjacent(a, b):
    gap = (b - a).days
    return 0 < gap <= 3 and sum((a + timedelta(days=i)).weekday() < 5 for i in range(1, gap + 1)) == 1


def fill_rows(rows, instrument_id, policy=None, *, fields=("close",), cutoff=None,
              currency_override=None, required_currency=None, pinned_rows=None):
    """Fill explicit weekday holes within same-currency segments, returning copies.

    Smooth uses linear arithmetic interpolation between real endpoints; an
    isolated hole is their arithmetic mean. Normal uses a date-keyed stream
    fitted to at least two preceding real adjacent-weekday log returns. Filled
    prices can anchor consecutive draws but NEVER enter the fit. Unrepresentable
    draws remain missing, without clipping or retrying to obtain a nicer price.
    Date-keyed pinned rows freeze previously derived execution marks before later
    draws. Observed values and fit eligibility still come only from input rows.
    """
    policy = policy or {"method": "smooth", "seed": 0, "version": VERSION}
    result = [{**row, **({"filled_fields": list(row["filled_fields"])} if "filled_fields" in row else {})}
              for row in rows if cutoff is None or row["date"] <= cutoff]
    for field in fields:
        values = [positive(row.get(field)) for row in result]
        holes = [missing(row.get(field)) for row in result]
        days = [date.fromisoformat(row["date"]) for row in result]
        currencies = [currency(row, currency_override) for row in result]
        next_endpoint = [None] * len(result)
        endpoint = None
        for i in range(len(result) - 1, -1, -1):
            next_endpoint[i] = endpoint
            if values[i] is not None or not holes[i] or days[i].weekday() >= 5:
                endpoint = i
        segment_end = [0] * len(result)
        end = len(result) - 1
        for i in range(len(result) - 1, -1, -1):
            if i == len(result) - 1 or currencies[i] != currencies[i + 1] or days[i].weekday() >= 5:
                end = i
            segment_end[i] = end
        previous_endpoint = None
        count, mean, m2 = 0, 0.0, 0.0
        for i, row in enumerate(result):
            left = previous_endpoint
            if values[i] is not None or not holes[i] or days[i].weekday() >= 5:
                previous_endpoint = i
            if i and currencies[i] != currencies[i - 1]:
                count, mean, m2 = 0, 0.0, 0.0
                left = None
                previous_endpoint = i if values[i] is not None or not holes[i] else None
            if (i and days[i].weekday() < 5 and days[i - 1].weekday() < 5
                    and currencies[i] == currencies[i - 1] and adjacent(days[i - 1], days[i])
                    and values[i] is not None and values[i - 1] is not None):
                observed = math.log(float(values[i])) - math.log(float(values[i - 1]))
                count += 1
                delta = observed - mean
                mean += delta / count
                m2 += delta * (observed - mean)
            pinned = (pinned_rows or {}).get(row["date"])
            if pinned is not None and field in pinned:
                # Arrays above retain raw observations: this anchor must never
                # become an observed return, including on the following day.
                row[field] = pinned[field]
                if field in pinned.get("filled_fields", []):
                    row.setdefault("filled_fields", []).append(field)
                continue
            if (values[i] is not None or not missing(row.get(field)) or days[i].weekday() >= 5
                    or (field == "split_adjusted_close" and not missing(row.get("close")))
                    or currencies[i] not in (None, "EUR")
                    or currency_override not in (None, "EUR")
                    or (required_currency is not None and currencies[i] != required_currency)):
                continue
            value = None
            if policy["method"] == "smooth":
                right = next_endpoint[i]
                if (left is not None and right is not None and values[left] is not None and values[right] is not None
                        and segment_end[left] >= right and days[left].weekday() < 5 and days[right].weekday() < 5):
                    # Fix precision across portfolio Decimal and float analytics callers.
                    with localcontext() as context:
                        context.prec = 50
                        weight = Decimal(i - left) / Decimal(right - left)
                        value = values[left] * (1 - weight) + values[right] * weight
            elif (count >= 2 and i and days[i - 1].weekday() < 5
                  and currencies[i] == currencies[i - 1] and adjacent(days[i - 1], days[i])):
                anchor = positive(result[i - 1].get(field))
                if anchor is not None:
                    identity = json.dumps([VERSION, policy["seed"], instrument_id, row["date"], field], separators=(",", ":"))
                    rng = random.Random(int.from_bytes(hashlib.sha256(identity.encode()).digest(), "big"))
                    draw = rng.normalvariate(mean, math.sqrt(max(0.0, m2 / (count - 1))))
                    try:
                        value = positive(math.exp(math.log(float(anchor)) + draw))
                    except OverflowError:
                        pass
            if positive(value) is not None:
                row[field] = str(value)
                row.setdefault("filled_fields", []).append(field)
    return result


def fill_adjusted_rows(rows, instrument_id, policy, *, cutoff=None, currency_override=None, pinned_rows=None):
    # A deliberately unavailable split basis must not fall back to another field.
    fields = ("split_adjusted_close",) if any("split_adjusted_close" in row for row in rows) else ("close", "adj_close")
    return fill_rows(rows, instrument_id, policy, fields=fields, cutoff=cutoff,
                     currency_override=currency_override, pinned_rows=pinned_rows)
