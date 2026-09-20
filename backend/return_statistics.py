"""Derived log-return estimates; never alter or reject saved provider evidence."""

from datetime import date, timedelta
import math
from statistics import fmean, variance

from bson import Decimal128

import market_data as md

MODEL_VERSION = 4
TRADING_DAYS = 252
MIN_RETURNS = 60


def positive_price(value):
    # bools and containers are not prices, even though float(True) is legal.
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal128):
        value = value.to_decimal()
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def calculate_statistics(rows):
    """Rows are latest-per-date, ascending. Never splice adjusted/raw prices.

    Bad prices break the return chain. Only adjacent weekdays form daily
    returns: Friday -> Monday is valid, Friday -> Friday is not. Without an
    exchange calendar this conservatively skips pairs crossing missing holiday
    weekdays too, rather than annualizing sparse weekly/monthly history as daily.
    """
    quote_count = len(rows)
    rows = [row for row in rows if date.fromisoformat(row["date"]).weekday() < 5]
    weekend_count = quote_count - len(rows)
    basis = "adj_close" if rows and all(positive_price(row.get("adj_close")) is not None for row in rows) else "close"
    warnings = []
    if weekend_count:
        warnings.append(f"{weekend_count} weekend quote(s) excluded from weekday return estimation.")
    if basis == "close" and rows:
        warnings.append("Adjusted closes are incomplete; all returns use unadjusted closes. Splits and distributions can distort these estimates.")
    logs, valid_dates = [], []
    invalid_prices = skipped_gaps = invalid_returns = 0
    previous = None
    for row in rows:
        price = positive_price(row.get(basis))
        day = date.fromisoformat(row["date"])
        if price is None:
            invalid_prices += 1
            previous = None
            continue
        valid_dates.append(row["date"])
        if previous:
            previous_day, previous_price = previous
            gap = (day - previous_day).days
            weekdays = sum((previous_day + timedelta(days=i)).weekday() < 5 for i in range(1, min(gap, 7) + 1))
            if gap > 7 or weekdays != 1:
                skipped_gaps += 1
            else:
                log_return = math.log(price) - math.log(previous_price)
                if math.isfinite(log_return):
                    logs.append(log_return)
                else:
                    invalid_returns += 1
        previous = day, price
    if invalid_prices:
        warnings.append(f"{invalid_prices} non-positive, missing or non-finite price(s) excluded; returns do not bridge these rows.")
    if skipped_gaps:
        warnings.append(f"{skipped_gaps} interval(s) spanning missing weekdays excluded from daily returns. Exchange holidays may also be excluded; no exchange calendar is assumed.")
    if invalid_returns:
        warnings.append(f"{invalid_returns} numerically unrepresentable return(s) excluded.")
    if len(logs) < TRADING_DAYS:
        warnings.append("Less than one trading year of usable returns; estimates may be particularly unreliable.")
    # Preserve the +/-50% price-move warning using equivalent log thresholds.
    if any(value > math.log(1.5) or value < math.log(0.5) for value in logs):
        warnings.append("History includes daily moves greater than 50%; check corporate actions and data quality before relying on a projection.")
    metrics = {"daily_mean_log_return": None, "daily_log_return_variance": None,
               "daily_volatility": None, "annualized_mean_log_return": None,
               "annualized_volatility": None}
    try:
        if logs:
            metrics["daily_mean_log_return"] = fmean(logs)
            metrics["annualized_mean_log_return"] = metrics["daily_mean_log_return"] * TRADING_DAYS
        if len(logs) >= 2:
            metrics["daily_log_return_variance"] = variance(logs)
            metrics["daily_volatility"] = math.sqrt(metrics["daily_log_return_variance"])
            metrics["annualized_volatility"] = math.sqrt(metrics["daily_log_return_variance"] * TRADING_DAYS)
        if any(value is not None and not math.isfinite(value) for value in metrics.values()):
            raise OverflowError
    except (OverflowError, ValueError):
        metrics = dict.fromkeys(metrics)
        warnings.append("Return estimates exceed numerical limits; this history cannot be simulated.")
    return {
        "price_basis": basis, "quote_count": quote_count, "price_count": len(valid_dates),
        "return_count": len(logs), "first_date": valid_dates[0] if valid_dates else None,
        "last_date": valid_dates[-1] if valid_dates else None,
        "invalid_price_count": invalid_prices, "skipped_gap_count": skipped_gaps, "weekend_quote_count": weekend_count,
        **metrics, "warnings": warnings,
    }


def refresh_statistics(db, isin, *, force=False):
    """Persist estimates after acquisition; lazily cover pre-feature/partial data.

    Observations are append-only. Their count, listing, model version and UTC day
    invalidate the cache, including corrections to an already saved quote date.
    Currency overrides and operational eligibility are evaluated on every read.
    """
    state = db.instrument_state.find_one({"_id": isin}) or {}
    listing = state.get("listing")
    cutoff = md.utcnow()
    query = md.listing_filter(isin, listing) if listing else {"instrument_id": isin, "symbol": {"$exists": False}}
    query.update(collected_at={"$lte": cutoff}, date={"$lte": cutoff.date().isoformat()})
    count = db.quotes.count_documents(query) if listing else 0
    signature = {"version": MODEL_VERSION, "listing": listing, "observations": count, "day": cutoff.date().isoformat()}
    cached = db.instrument_statistics.find_one({"_id": isin})
    if not force and cached and cached.get("signature") == signature:
        return cached["statistics"], state
    rows = []
    if listing:
        records = db.quotes.aggregate([
            {"$match": query},
            {"$sort": {"date": 1, "collected_at": -1, "_id": -1}},
            {"$group": {"_id": "$date", "data": {"$first": "$data"}}},
            {"$sort": {"_id": 1}},
        ])
        rows = [{**record["data"], "date": record["_id"]} for record in records]
    currencies = set()
    unknown_currency = False
    for row in rows:
        currency = row.get("price_currency")
        if isinstance(currency, str) and currency.strip():
            currencies.add(currency.strip().upper())
        else:
            unknown_currency = True
    statistics = {
        **calculate_statistics(rows), "computed_at": cutoff, "as_of": cutoff,
        "provider_currencies": sorted(currencies), "unknown_currency": unknown_currency,
    }
    db.instrument_statistics.replace_one({"_id": isin}, {
        "_id": isin, "signature": signature, "statistics": statistics,
    }, upsert=True)
    return statistics, state


def instrument_statistics(db, isin):
    catalogue = md.require_catalogue(db, isin)
    md.expire_stalled_jobs(db, isin)
    estimates, state = refresh_statistics(db, isin)
    estimates = estimates.copy()
    currencies = set(estimates.pop("provider_currencies"))
    unknown = estimates.pop("unknown_currency")
    override = state.get("price_currency_override")
    warnings = list(estimates["warnings"])
    currency = source = None
    # A manual label may fill missing currency, never reinterpret explicit USD
    # observations as EUR. The fund's base currency is deliberately not used.
    if override and (not currencies or currencies == {override}):
        currency, source = override, "manual"
    elif len(currencies) == 1 and not unknown and not override:
        currency, source = next(iter(currencies)), "provider"
    if override and currencies and currencies != {override}:
        warnings.append("The manual currency conflicts with provider prices. A currency label is not an FX conversion.")
    if len(currencies) > 1:
        warnings.append("History contains mixed price currencies; it cannot be treated as one EUR return series.")
    if source == "manual":
        warnings.append("Price currency is manually specified, not verified by the provider; no FX conversion is performed.")
    last_date = estimates["last_date"]
    if last_date and (md.utcnow().date() - date.fromisoformat(last_date)).days > 14:
        warnings.append("The last usable price is more than 14 days old. Update EOD history before relying on these estimates.")
    reason = None
    if not estimates["quote_count"]:
        reason = "Download EOD history for this instrument first."
    elif currency != "EUR":
        reason = "Only EUR-priced history can be simulated. Confirm missing price currency on the instrument page; non-EUR or mixed prices require FX conversion."
    elif estimates["return_count"] < MIN_RETURNS:
        reason = f"At least {MIN_RETURNS} usable daily returns are required; download more EOD history."
    elif estimates["daily_log_return_variance"] is None:
        reason = "Return estimates are not numerically usable for simulation."
    elif db.acquisition_jobs.find_one({"instrument_id": isin, "active": True}):
        reason = "Wait for the current EOD acquisition to finish, then refresh these estimates."
    return md.public({
        "instrument_id": isin, "name": catalogue.get("name"), "listing": state.get("listing"),
        **estimates, "price_currency": currency, "currency_source": source,
        "eligible": reason is None, "ineligible_reason": reason, "warnings": warnings,
    })
