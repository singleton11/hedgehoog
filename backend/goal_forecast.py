"""Bounded monthly goal-date search at a fixed contribution and reliability."""
import math

import numpy as np

from goal_simulation import MAX_MONTHS, add_months, period_factors


def forecast_goal_date(estimates, body, today, monthly, *, paths, seed):
    limit = add_months(today, MAX_MONTHS)
    result = {"forecast_goal_date": None, "forecast_status": "not_reached",
              "forecast_search_end_date": limit.isoformat()}
    if body.starting_capital_eur >= body.goal_eur:
        return {**result, "forecast_goal_date": today.isoformat(), "forecast_status": "reached"}
    # Include the saved deadline so its probability exactly replays tracking.
    # Search all checkpoints in order: success probability need not be monotonic.
    steps = {add_months(today, month): True for month in range(1, MAX_MONTHS + 1)}
    steps.setdefault(body.goal_date, False)
    growth, annuity = np.ones(paths), np.zeros(paths)
    threshold = math.ceil(body.reliability * paths)
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            for day, deposit, factors in period_factors(
                sorted(steps.items()), today, estimates["daily_mean_log_return"],
                estimates["daily_log_return_variance"], paths, seed,
            ):
                growth *= factors
                annuity *= factors
                if deposit:
                    annuity += 1
                wealth = float(body.starting_capital_eur) * growth + float(monthly) * annuity
                if not np.all(np.isfinite(wealth)):
                    raise FloatingPointError
                if np.count_nonzero(wealth >= float(body.goal_eur)) >= threshold:
                    return {**result, "forecast_goal_date": day.isoformat(), "forecast_status": "reached"}
    except (FloatingPointError, OverflowError, ValueError):
        return {**result, "forecast_status": "unavailable"}
    return result


def calendar_month_difference(start, end):
    """Signed calendar months, with a fractional final anniversary interval."""
    if end < start:
        return -calendar_month_difference(end, start)
    months = (end.year - start.year) * 12 + end.month - start.month
    if add_months(start, months) > end:
        months -= 1
    lower, upper = add_months(start, months), add_months(start, months + 1)
    return months + (end - lower).days / (upper - lower).days
