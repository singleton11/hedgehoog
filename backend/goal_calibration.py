"""Walk-forward goal-probability evaluation on saved (not vintage) history.

The forecast boundary accepts only a trailing prefix. Price basis, currency,
parameters and per-origin random stream are determined before reading outcomes.
"""

from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import date
import hashlib
import json
import math
import secrets
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from goal_simulation import Money, PATHS, add_months, money_string, period_factors
import market_data as md
from return_statistics import MIN_RETURNS, calculate_statistics, positive_price

MAX_SEED = 2**32 - 1
MAX_HISTORY_ROWS = 20_000
MAX_ORIGINS = 600
MAX_FORECAST_PERIODS = 12_000
MAX_PRICE_AGE_DAYS = 7
BIN_COUNT = 10


class CalibrationWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lookback_months: int = Field(default=12, ge=3, le=120, strict=True)
    start_date: date | None = None
    end_date: date | None = None
    seed: int | None = Field(default=None, ge=0, le=MAX_SEED, strict=True)

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("Forecast start date must not be after end date")
        return self

    def public_settings(self):
        return {
            "lookback_months": self.lookback_months,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
        }


class CalibrationInputs(CalibrationWindow):
    goal_eur: Money = Field(gt=0)
    starting_capital_eur: Money
    monthly_contribution_eur: Money
    horizon_months: int = Field(default=12, ge=1, le=120, strict=True)

    def public_settings(self):
        return {**super().public_settings(), "horizon_months": self.horizon_months,
                **{key: money_string(getattr(self, key)) for key in
                   ("goal_eur", "starting_capital_eur", "monthly_contribution_eur")}}


class RandomCalibrationInputs(CalibrationWindow):
    mode: Literal["random"]
    scenario_count: int = Field(default=1000, ge=10, le=5000, strict=True)
    min_horizon_months: int = Field(default=3, ge=1, le=120, strict=True)
    max_horizon_months: int = Field(default=24, ge=1, le=120, strict=True)

    @model_validator(mode="after")
    def ordered_horizons(self):
        if self.min_horizon_months > self.max_horizon_months:
            raise ValueError("Minimum horizon must not exceed maximum horizon")
        return self

    def public_settings(self):
        return {**super().public_settings(), "mode": self.mode, "scenario_count": self.scenario_count,
                "min_horizon_months": self.min_horizon_months, "max_horizon_months": self.max_horizon_months}


def is_eur_history(rows, override):
    """A present-day manual label may fill unknowns, never relabel explicit USD."""
    if override not in (None, "EUR"):
        return False
    for row in rows:
        value = row.get("price_currency")
        currency = value.strip().upper() if isinstance(value, str) else ""
        if currency != "EUR" and not (not currency and override == "EUR"):
            return False
    return True


def reaches_goal(wealth, goal):
    # Protect equality from floating-point arithmetic, not monetary rounding.
    # Eight ULPs is < EUR 0.001 even at the maximum permitted EUR 1e12 goal;
    # a one-cent shortfall must NEVER become a success.
    target = float(goal)
    return wealth >= target - 8 * math.ulp(target)


def forecast_probability(estimates, body, origin, seed, *, paths=PATHS):
    """Same log-normal growth/payment timing as the planner, fixed funding.

    A date-derived child stream is independent of later data, omitted folds and
    the requested evaluation range. Never seed from the number of outcomes.
    """
    stream = np.random.SeedSequence([seed, origin.toordinal()])
    steps = [(add_months(origin, month), True) for month in range(1, body.horizon_months + 1)]
    growth, annuity = np.ones(paths), np.zeros(paths)
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            for _, _, factors in period_factors(steps, origin, estimates["daily_mean_log_return"],
                                               estimates["daily_log_return_variance"], paths, stream):
                growth *= factors
                annuity *= factors
                annuity += 1
            # Like the goal solver: accumulate payment growth, not repeated
            # floating-point additions of cent-valued monetary amounts.
            wealth = float(body.starting_capital_eur) * growth + float(body.monthly_contribution_eur) * annuity
            if not np.all(np.isfinite(wealth)):
                raise FloatingPointError
    except (FloatingPointError, OverflowError, ValueError):
        raise md.DataError(422, "Historical forecasts are numerically unstable. Check price quality or choose a shorter horizon.") from None
    return float(np.mean(reaches_goal(wealth, body.goal_eur)))


class UnscorableOutcome(Exception):
    pass


def realized_value(rows, dates, origin_index, goal_date, basis, body, override):
    """Replay fixed cash flows, never pick execution prices by their value.

    Buy at origin close; later anniversary deposits buy at the first saved
    weekday close on/after the due date. Cash not yet invested stays cash. Mark
    at the last close on/before the goal date, never a later closing price.
    Invalid execution/valuation prices and >7-day gaps leave the fold unscored;
    they never cause a switch to a different price basis or a favourable quote.
    """
    end_index = bisect_right(dates, goal_date) - 1
    holding = rows[origin_index:end_index + 1]
    if not is_eur_history(holding, override):
        raise UnscorableOutcome("outcome_currency")
    end_price = positive_price(rows[end_index].get(basis))
    if end_price is None or (goal_date - dates[end_index]).days > MAX_PRICE_AGE_DAYS:
        raise UnscorableOutcome("missing_outcome_price")
    start_price = positive_price(rows[origin_index].get(basis))
    try:
        # Use ratios instead of shares to avoid overflow for very small prices.
        cash_flows = [float(body.starting_capital_eur) * (end_price / start_price)]
        monthly = float(body.monthly_contribution_eur)
        if monthly:
            for month in range(1, body.horizon_months + 1):
                due = add_months(dates[origin_index], month)
                execution_index = bisect_left(dates, due)
                if execution_index > end_index:
                    if (goal_date - due).days > MAX_PRICE_AGE_DAYS:
                        raise UnscorableOutcome("missing_execution_price")
                    cash_flows.append(monthly)  # A goal-date deposit earns no return.
                    continue
                execution_price = positive_price(rows[execution_index].get(basis))
                if execution_price is None or (dates[execution_index] - due).days > MAX_PRICE_AGE_DAYS:
                    raise UnscorableOutcome("missing_execution_price")
                cash_flows.append(monthly * (end_price / execution_price))
        terminal = math.fsum(cash_flows)
        if not math.isfinite(terminal):
            raise OverflowError
    except (OverflowError, ZeroDivisionError):
        raise md.DataError(422, "Historical outcomes exceed numerical limits. Check price quality.") from None
    return terminal, dates[end_index]


def calibration_metrics(forecasts):
    """Brier = mean((p-y)^2); bins use mean p, not their midpoint."""
    bins = [[] for _ in range(BIN_COUNT)]
    for forecast in forecasts:
        p = forecast["predicted_probability"]
        bins[min(int(p * BIN_COUNT), BIN_COUNT - 1)].append(forecast)
    count = len(forecasts)
    calibration_bins = [{
        "lower": index / BIN_COUNT, "upper": (index + 1) / BIN_COUNT, "count": len(items),
        "mean_predicted_probability": math.fsum(f["predicted_probability"] for f in items) / len(items) if items else None,
        "observed_success_rate": sum(f["observed_success"] for f in items) / len(items) if items else None,
    } for index, items in enumerate(bins)]
    return {
        "sample_count": count,
        # Expected calibration error: absolute vertical gap, weighted by cases,
        # not an unweighted bin mean or signed gaps that cancel each other.
        "calibration_error": math.fsum(bin["count"] * abs(bin["mean_predicted_probability"] - bin["observed_success_rate"])
                                       for bin in calibration_bins if bin["count"]) / count,
        "brier_score": math.fsum((f["predicted_probability"] - int(f["observed_success"])) ** 2
                                 for f in forecasts) / count,
        "mean_predicted_probability": math.fsum(f["predicted_probability"] for f in forecasts) / count,
        "observed_success_rate": sum(f["observed_success"] for f in forecasts) / count,
        "calibration_bins": calibration_bins,
    }


def monthly_origins(rows, body):
    """The origin schedule uses dates only, never future price validity."""
    rows = [row for row in rows if date.fromisoformat(row["date"]).weekday() < 5]
    if not rows:
        raise md.DataError(422, "Download weekday EOD history for this instrument first.")
    dates = [date.fromisoformat(row["date"]) for row in rows]
    origins = []
    previous_month = None
    for index, day in enumerate(dates):
        month = (day.year, day.month)
        if month != previous_month and (not body.start_date or day >= body.start_date) and (not body.end_date or day <= body.end_date):
            origins.append(index)
        previous_month = month
    return rows, dates, origins


def evaluate_history(rows, body: CalibrationInputs, *, seed, override=None, paths=PATHS):
    """Fit trailing prefixes, freeze predictions, then score held-out outcomes."""
    rows, dates, origins = monthly_origins(rows, body)
    if len(origins) > MAX_ORIGINS or len(origins) * body.horizon_months > MAX_FORECAST_PERIODS:
        raise md.DataError(422, "This evaluation exceeds the calculation limit (600 origins / 12,000 monthly forecast periods). Choose a narrower forecast date range or shorter horizon.")
    forecasts, excluded = [], []
    quality_warning = raw_prices = False
    first_possible_origin = add_months(dates[0], body.lookback_months)
    for index in origins:
        origin = dates[index]

        def exclude(reason):
            excluded.append({"origin_date": origin.isoformat(), "reason": reason})

        if origin < first_possible_origin:
            exclude("insufficient_training")
            continue
        goal_date = add_months(origin, body.horizon_months)
        if goal_date > dates[-1]:
            exclude("incomplete_horizon")
            continue
        first_index = bisect_left(dates, add_months(origin, -body.lookback_months))
        # The ONLY rows the estimator sees. No full-history statistics cache,
        # future currency validation, global basis choice or outcome tuning.
        training = rows[first_index:index + 1]
        if not is_eur_history(training, override):
            exclude("training_currency")
            continue
        estimates = calculate_statistics(training)
        if estimates["return_count"] < MIN_RETURNS:
            exclude("insufficient_training")
            continue
        basis = estimates["price_basis"]
        if positive_price(rows[index].get(basis)) is None or estimates["daily_log_return_variance"] is None:
            exclude("unusable_training")
            continue
        probability = forecast_probability(estimates, body, origin, seed, paths=paths)
        # Outcomes become accessible only AFTER the probability is fixed.
        try:
            terminal, valuation_date = realized_value(rows, dates, index, goal_date, basis, body, override)
        except UnscorableOutcome as error:
            exclude(str(error))
            continue
        forecasts.append({
            "origin_date": origin.isoformat(), "goal_date": goal_date.isoformat(),
            "training_start_date": training[0]["date"], "training_end_date": training[-1]["date"],
            "training_return_count": estimates["return_count"], "price_basis": basis,
            "predicted_probability": probability, "observed_success": reaches_goal(terminal, body.goal_eur),
            "terminal_value_eur": money_string(terminal), "valuation_date": valuation_date.isoformat(),
        })
        raw_prices |= basis == "close"
        quality_warning |= bool(estimates["invalid_price_count"] or estimates["skipped_gap_count"])
    counts = dict(sorted(Counter(item["reason"] for item in excluded).items()))
    if not forecasts:
        reasons = ", ".join(f"{key.replace('_', ' ')}: {value}" for key, value in counts.items())
        raise md.DataError(422, "No evaluable historical forecasts. Require a full lookback, at least 60 usable daily returns, EUR prices and a complete goal horizon. Download more history or shorten the lookback/horizon. " + reasons)
    warnings = calibration_warnings(overlapping=body.horizon_months > 1, sample_count=len(forecasts),
                                    excluded=bool(excluded), raw_prices=raw_prices,
                                    quality_warning=quality_warning, override=override)
    return {
        "settings": body.public_settings(), "seed": seed, "simulation_paths": paths, "method": "walk_forward",
        "history_first_date": rows[0]["date"], "history_last_date": rows[-1]["date"],
        "candidate_count": len(origins), "skipped_counts": counts, "excluded_origins": excluded,
        **calibration_metrics(forecasts), "forecasts": forecasts, "warnings": warnings,
    }


def calibration_warnings(*, overlapping, sample_count, excluded, raw_prices, quality_warning, override):
    warnings = [
        "Walk-forward fitting uses only prices dated on or before each origin. This is not strict point-in-time data: downloaded history may contain later provider corrections and corporate-action adjustments.",
        "The current acquired instrument, listing and any manual currency label are selected today. This does not remove survivorship or instrument-selection bias; do not choose settings by their best historical score.",
        "A forecast starts at the first recorded weekday close of each month. Each trailing lookback is refitted independently using at least 60 usable daily log returns; no held-out outcome is used to fit or tune it.",
        "Normally distributed independent log returns, 252 trading days/year, calendar days/365.25 and fixed monthly-anniversary payments, as in the goal planner. Forecasts are model estimates, not guarantees.",
        "Historical purchases use the first saved weekday close on/after each payment date; uninvested payments stay in cash. Final valuation uses the last close on/before the goal date. Missing prices or gaps over seven days are excluded; no exchange calendar is available. Simulations approximate payments at the anniversary itself.",
        "Nominal EUR; fractional shares, no FX conversion, inflation, taxes or trading fees. Distributions and expenses are reflected only insofar as they are in the chosen price series.",
        "Brier score is the mean squared probability error (0 is best, 1 is worst). Calibration points use the mean forecast probability and actual success frequency of each nonempty bin; empty bins have no point.",
    ]
    if overlapping:
        warnings.append("Monthly forecast origins have overlapping holding periods: outcomes are correlated, not independent trials. Bin counts are not independent sample sizes; no confidence intervals are claimed.")
    if sample_count < 30:
        warnings.append("Fewer than 30 scored forecasts; the Brier score and calibration curve are particularly uncertain.")
    if excluded:
        warnings.append("Excluded origins are reported separately. Missing outcome data can bias the scored subset; unscored cases are not treated as successes or failures.")
    if raw_prices:
        warnings.append("Some forecasts use unadjusted closes because their training window lacks complete adjusted prices. Splits and distributions can distort results; each forecast retains its training-selected basis throughout the outcome.")
    if quality_warning:
        warnings.append("Some training windows contain missing weekdays or invalid prices. Returns do not bridge invalid prices or missing weekdays, matching the goal planner's conservative daily-return rules.")
    if override:
        warnings.append("Price currency is manually specified today, not independently verified; explicit conflicting currencies are still excluded.")
    return warnings


def calibrate_instrument(db, isin, body, *, evaluate):
    md.require_catalogue(db, isin)
    md.expire_stalled_jobs(db, isin)
    if db.acquisition_jobs.find_one({"instrument_id": isin, "active": True}):
        raise md.DataError(422, "Wait for the current EOD acquisition to finish before calculating calibration.")
    state = db.instrument_state.find_one({"_id": isin}) or {}
    listing = state.get("listing")
    if not listing:
        raise md.DataError(422, "Download EOD history for this instrument first.")
    cutoff = md.utcnow()
    query = {**md.listing_filter(isin, listing), "collected_at": {"$lte": cutoff},
             "date": {"$lte": cutoff.date().isoformat()}}
    records = db.quotes.aggregate([
        {"$match": query},
        {"$sort": {"date": 1, "collected_at": -1, "_id": -1}},
        {"$group": {"_id": "$date", "data": {"$first": "$data"}}},
        {"$sort": {"_id": 1}},
        {"$limit": MAX_HISTORY_ROWS + 1},
        {"$project": {"data.close": 1, "data.adj_close": 1, "data.price_currency": 1}},
    ])
    rows = [{**record.get("data", {}), "date": record["_id"]} for record in records]
    if len(rows) > MAX_HISTORY_ROWS:
        raise md.DataError(422, "Saved history exceeds the 20,000-date calculation limit; no history was silently truncated.")
    seed = body.seed if body.seed is not None else secrets.randbits(32)
    result = evaluate(rows, body, seed=seed, override=state.get("price_currency_override"))
    fingerprint = hashlib.sha256(json.dumps(md.public({"rows": rows, "listing": listing,
        "currency_override": state.get("price_currency_override")}), sort_keys=True).encode()).hexdigest()
    return {**result, "data_as_of": md.public(cutoff), "history_fingerprint": fingerprint}
