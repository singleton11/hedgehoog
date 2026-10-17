"""Derived return analytics; never alter or reject saved provider evidence."""

from datetime import date, timedelta
import math
from statistics import fmean, variance

from bson import Decimal128

import market_data as md
from split_adjustments import BASIS, adjusted_rows, load_split_snapshot

from quote_fill import fill_adjusted_rows, read_policy

MODEL_VERSION = 9
TRADING_DAYS = 252
MIN_RETURNS = 60
YEAR_START_MAX_GAP_DAYS = 7


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


def historical_year_rows(rows):
    """Select a comparable calendar year ending at the latest saved quote.

    Rows are latest-per-date and ascending. Use the quote on/before the calendar
    anniversary (Feb 29 clamps to Feb 28), at most seven days earlier. Never
    substitute a shorter history or skip an invalid endpoint.
    """
    if len(rows) < 2:
        return []
    end = rows[-1]
    end_date = date.fromisoformat(end["date"])
    if end_date.year == 1:
        return []
    try:
        anniversary = end_date.replace(year=end_date.year - 1)
    except ValueError:  # February 29 in a leap year.
        anniversary = end_date.replace(year=end_date.year - 1, day=28)
    start_index = next((i for i in range(len(rows) - 2, -1, -1)
                        if rows[i]["date"] <= anniversary.isoformat()), None)
    if start_index is None:
        return []
    start = rows[start_index]
    if (anniversary - date.fromisoformat(start["date"])).days > YEAR_START_MAX_GAP_DAYS:
        return []
    # A currency label is not an FX conversion. Unknown currencies are retained,
    # but known conflicting currencies cannot form a comparable price series.
    currencies = {row["price_currency"].strip().upper() for row in rows[start_index:]
                  if isinstance(row.get("price_currency"), str) and row["price_currency"].strip()}
    return rows[start_index:] if len(currencies) <= 1 else []


def calculate_historical_year_return(rows):
    """Observed simple return; interior gaps do not affect the endpoint ratio."""
    unavailable = {
        "historical_year_return": None, "historical_year_start_date": None,
        "historical_year_end_date": None, "historical_year_price_basis": None,
    }
    window = historical_year_rows(rows)
    if not window:
        return unavailable
    start, end = window[0], window[-1]
    basis = BASIS if BASIS in start and BASIS in end else (
        "adj_close" if all(positive_price(row.get("adj_close")) is not None
                           for row in (start, end)) else "close")
    start_price, end_price = (positive_price(row.get(basis)) for row in (start, end))
    if start_price is None or end_price is None:
        return unavailable
    value = end_price / start_price - 1
    if not math.isfinite(value):
        return unavailable
    return {
        "historical_year_return": value, "historical_year_start_date": start["date"],
        "historical_year_end_date": end["date"], "historical_year_price_basis": basis,
    }


def calculate_daily_statistics(rows, *, include_return_runs=False):
    """Rows are latest-per-date, ascending. Never splice adjusted/raw prices.

    Bad prices break the return chain. Only adjacent weekdays form daily
    returns: Friday -> Monday is valid, Friday -> Friday is not. Without an
    exchange calendar this conservatively skips pairs crossing missing holiday
    weekdays too, rather than annualizing sparse weekly/monthly history as daily.
    """
    quote_count = len(rows)
    rows = [row for row in rows if date.fromisoformat(row["date"]).weekday() < 5]
    weekend_count = quote_count - len(rows)
    basis = BASIS if rows and all(BASIS in row for row in rows) else (
        "adj_close" if rows and all(positive_price(row.get("adj_close")) is not None for row in rows) else "close")
    warnings = []
    if weekend_count:
        warnings.append(f"{weekend_count} weekend quote(s) excluded from weekday return estimation.")
    if basis == "close" and rows:
        warnings.append("Adjusted closes are incomplete; all returns use unadjusted closes. Splits and distributions can distort these estimates.")
    logs, valid_dates = [], []
    runs, run = [], []
    invalid_prices = skipped_gaps = invalid_returns = 0
    previous = None
    for row in rows:
        price = positive_price(row.get(basis))
        day = date.fromisoformat(row["date"])
        if price is None:
            invalid_prices += 1
            previous = None
            if run:
                runs.append(run)
                run = []
            continue
        valid_dates.append(row["date"])
        if previous:
            previous_day, previous_price = previous
            gap = (day - previous_day).days
            weekdays = sum((previous_day + timedelta(days=i)).weekday() < 5 for i in range(1, min(gap, 7) + 1))
            if gap > 7 or weekdays != 1:
                skipped_gaps += 1
                if run:
                    runs.append(run)
                    run = []
            else:
                log_return = math.log(price) - math.log(previous_price)
                if math.isfinite(log_return):
                    logs.append(log_return)
                    run.append(log_return)
                else:
                    invalid_returns += 1
                    if run:
                        runs.append(run)
                        run = []
        previous = day, price
    if run:
        runs.append(run)
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
        **({"return_runs": runs} if include_return_runs else {}),
    }


def calculate_historical_year_volatility(rows):
    """Annualized sample volatility of usable daily log returns in the last year.

    Unlike the endpoint return, every weekday price in this window determines
    the price basis. Reuse the same gap/invalid-price rules as full-history
    statistics, without letting older observations affect the estimate.
    """
    unavailable = {
        "historical_year_volatility": None,
        "historical_year_volatility_start_date": None,
        "historical_year_volatility_end_date": None,
        "historical_year_volatility_price_basis": None,
        "historical_year_volatility_return_count": None,
    }
    window = historical_year_rows(rows)
    if not window:
        return unavailable
    stats = calculate_daily_statistics(window)
    if stats["annualized_volatility"] is None:
        return unavailable
    return {
        "historical_year_volatility": stats["annualized_volatility"],
        "historical_year_volatility_start_date": window[0]["date"],
        "historical_year_volatility_end_date": window[-1]["date"],
        "historical_year_volatility_price_basis": stats["price_basis"],
        "historical_year_volatility_return_count": stats["return_count"],
    }


def calculate_statistics(rows):
    return {
        **calculate_daily_statistics(rows),
        **calculate_historical_year_return(rows),
        **calculate_historical_year_volatility(rows),
    }


def _latest_rows(db, listing, query):
    if not listing:
        return []
    records = db.quotes.aggregate([
        {"$match": query},
        {"$sort": {"date": 1, "collected_at": -1, "_id": -1}},
        {"$group": {"_id": "$date", "data": {"$first": "$data"}}},
        {"$sort": {"_id": 1}},
    ])
    return [{**record["data"], "date": record["_id"]} for record in records]


def instrument_return_runs(db, isin, *, as_of, policy=None):
    """Same published vintage, split basis and weekday/gap rules as model estimates."""
    state = db.instrument_state.find_one({"_id": isin}) or {}
    listing = state.get("listing")
    if not listing:
        return []
    generation = md.history_generation(db, isin, listing, as_of)
    query = md.observation_filter(db, isin, listing, as_of, generation)
    query.update(collected_at={"$lte": as_of}, date={"$lte": as_of.date().isoformat()})
    rows = _latest_rows(db, listing, query)
    splits = load_split_snapshot(db, isin, listing, as_of, generation)
    adjusted, _ = adjusted_rows(rows, splits)
    adjusted = fill_adjusted_rows(adjusted, isin, policy or read_policy(db),
                                  cutoff=as_of.date().isoformat(), currency_override=state.get("price_currency_override"))
    return calculate_daily_statistics(adjusted, include_return_runs=True)["return_runs"]


def refresh_statistics(db, isin, *, force=False, generation=None, as_of=None, point_in_time=False, policy=None):
    """Persist estimates after acquisition; lazily cover pre-feature/partial data.

    Observations are append-only. Their count, listing, split snapshot, model
    version and UTC day invalidate the cache, including quote/action corrections.
    Currency overrides and operational eligibility are evaluated on every read.
    """
    state = db.instrument_state.find_one({"_id": isin}) or {}
    listing = state.get("listing")
    cutoff = as_of or md.utcnow()
    historical = point_in_time
    generation = generation or md.history_generation(db, isin, listing, cutoff)
    query = md.observation_filter(db, isin, listing, cutoff, generation) if listing else {"instrument_id": isin, "symbol": {"$exists": False}}
    query.update(collected_at={"$lte": cutoff}, date={"$lte": cutoff.date().isoformat()})
    count = db.quotes.count_documents(query) if listing else 0
    splits = load_split_snapshot(db, isin, listing, cutoff, generation)
    publication = md.published_history(db, isin, listing, cutoff, generation)
    policy = policy or read_policy(db)
    signature = {"version": MODEL_VERSION, "listing": listing, "observations": count,
                 "quote_fill_policy": policy, "currency_override": state.get("price_currency_override"),
                 "generation": publication["_id"] if publication else None,
                 "splits": {"id": splits["_id"], "error": splits.get("error")} if splits else None,
                 "day": cutoff.date().isoformat()}
    cached = db.instrument_statistics.find_one({"_id": isin}) if not historical else None
    if not force and cached and cached.get("signature") == signature:
        statistics = dict(cached["statistics"])
        statistics["as_of"] = cutoff
        return statistics, state
    rows = _latest_rows(db, listing, query)
    currencies = set()
    unknown_currency = False
    for row in rows:
        currency = row.get("price_currency")
        if isinstance(currency, str) and currency.strip():
            currencies.add(currency.strip().upper())
        else:
            unknown_currency = True
    rows, split_warnings = adjusted_rows(rows, splits)
    rows = fill_adjusted_rows(rows, isin, policy, cutoff=cutoff.date().isoformat(),
                              currency_override=state.get("price_currency_override"))
    statistics = {
        "quote_fill_policy": policy, "filled_quote_count": sum(bool(row.get("filled_fields")) for row in rows),
        **calculate_statistics(rows), "computed_at": cutoff, "as_of": cutoff,
        "provider_currencies": sorted(currencies), "unknown_currency": unknown_currency,
    }
    statistics["warnings"].extend(split_warnings)
    if not historical:
        db.instrument_statistics.replace_one({"_id": isin}, {
            "_id": isin, "signature": signature, "statistics": statistics,
        }, upsert=True)
    return statistics, state


def instrument_statistics(db, isin, generation=None, *, as_of=None, point_in_time=False, policy=None):
    catalogue = md.require_catalogue(db, isin)
    if not point_in_time:
        md.expire_stalled_jobs(db, isin)
    estimates, state = refresh_statistics(db, isin, generation=generation, as_of=as_of,
                                          point_in_time=point_in_time, policy=policy)
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
    reference_date = as_of.date() if as_of is not None else md.utcnow().date()
    if last_date and (reference_date - date.fromisoformat(last_date)).days > 14:
        warnings.append("The last usable price is more than 14 days old at the selected cutoff. Update EOD history before relying on these estimates.")
    if point_in_time:
        warnings.append("Historical origin uses quote observations and a published history generation known by the selected timestamp. Present-day acquired-instrument membership and manual currency labels may still introduce selection or metadata bias.")
    reason = None
    if not estimates["quote_count"]:
        reason = "Download EOD history for this instrument first."
    elif currency != "EUR":
        reason = "Only EUR-priced history can be simulated. Confirm missing price currency on the instrument page; non-EUR or mixed prices require FX conversion."
    elif estimates["return_count"] < MIN_RETURNS:
        reason = f"At least {MIN_RETURNS} usable daily returns are required; download more EOD history."
    elif estimates["daily_log_return_variance"] is None:
        reason = "Return estimates are not numerically usable for simulation."
    elif not point_in_time and db.acquisition_jobs.find_one({"instrument_id": isin, "active": True}):
        reason = "Wait for the current EOD acquisition to finish, then refresh these estimates."
    return md.public({
        "instrument_id": isin, "name": catalogue.get("name"), "listing": state.get("listing"),
        **estimates, "price_currency": currency, "currency_source": source,
        "eligible": reason is None, "ineligible_reason": reason, "warnings": warnings,
    })
