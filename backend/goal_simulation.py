"""Bounded, reproducible buy-and-hold Monte Carlo savings projections."""

from calendar import monthrange
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP, localcontext
import math
from typing import Annotated

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

import market_data as md
from return_statistics import TRADING_DAYS

PATHS = 10_000
SEED = 20260919
MAX_MONTHS = 600
MAX_MONEY = Decimal("1000000000000")
CENT = Decimal("0.01")
Money = Annotated[Decimal, Field(ge=0, le=MAX_MONEY, decimal_places=2, allow_inf_nan=False)]


class GoalInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_eur: Money = Field(gt=0)
    starting_capital_eur: Money
    goal_date: date
    reliability: float = Field(ge=0.5, le=0.99, allow_inf_nan=False)


def add_months(day, months):
    year, month = divmod(day.year * 12 + day.month - 1 + months, 12)
    month += 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def contribution_dates(today, goal_date):
    if goal_date < add_months(today, 1):
        raise md.DataError(422, "Goal date must allow at least one monthly contribution, starting one month from today (UTC).")
    if goal_date > add_months(today, MAX_MONTHS):
        raise md.DataError(422, "Goal date must be within 50 years of today (UTC).")
    dates = []
    for month in range(1, MAX_MONTHS + 1):
        candidate = add_months(today, month)
        if candidate > goal_date:
            break
        dates.append(candidate)
    return dates


def money_string(value):
    # Simulation outputs are approximate; serialize the rounded display amount
    # as a string, just like other monetary values at the API boundary.
    with localcontext() as context:
        context.prec = 330
        return format(Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP), "f")


def period_factors(steps, today, mean, variance, paths, seed):
    """Replaying this stream reuses every draw, not a second set of scenarios."""
    rng = np.random.default_rng(seed)
    previous = today
    for day, deposit in steps:
        trading_time = TRADING_DAYS * (day - previous).days / 365.25
        yield day, deposit, np.exp(rng.normal(mean * trading_time, math.sqrt(variance * trading_time), paths))
        previous = day


def trajectory_points(steps, today, mean, variance, paths, seed, capital, monthly):
    """Monthly summaries of ALL paths, with O(paths) working memory.

    Replay after solving the contribution so the graph describes the exact same
    funded scenarios as the reported success probability. No trace subsampling.
    """
    growth, annuity = np.ones(paths), np.zeros(paths)

    def point(day):
        wealth = capital * growth + monthly * annuity
        if not np.all(np.isfinite(wealth)):
            raise FloatingPointError
        quantiles = np.quantile(wealth, [0.05, 0.25, 0.5, 0.75, 0.95], method="higher")
        return {
            "date": day.isoformat(),
            # Divide first to avoid overflowing a sum of large finite values.
            "mean_eur": money_string(float(np.sum(wealth / paths))),
            **{f"{key}_eur": money_string(value) for key, value in zip(("p05", "p25", "p50", "p75", "p95"), quantiles)},
        }

    points = [point(today)]
    for day, deposit, factors in period_factors(steps, today, mean, variance, paths, seed):
        growth *= factors
        annuity *= factors
        if deposit:
            annuity += 1.0
        points.append(point(day))
    return points


def simulate_buy_and_hold(estimates, body: GoalInputs, today, *, paths=PATHS, seed=SEED):
    deposits = contribution_dates(today, body.goal_date)
    steps = [(day, True) for day in deposits]
    if deposits[-1] < body.goal_date:
        steps.append((body.goal_date, False))
    growth = np.ones(paths)
    annuity = np.zeros(paths)
    mean = estimates["daily_mean_log_return"]
    variance = estimates["daily_log_return_variance"]
    # If log returns have mean m and variance v, a period's log return is
    # Normal(m*t, v*t). Do NOT subtract v/2 again: m is already a log mean.
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            for _, deposit, factors in period_factors(steps, today, mean, variance, paths, seed):
                growth *= factors
                annuity *= factors
                if deposit:
                    annuity += 1.0  # A payment at period end earns only subsequent returns.
            initial_wealth = float(body.starting_capital_eur) * growth
            required = np.maximum((float(body.goal_eur) - initial_wealth) / annuity, 0)
            if not np.all(np.isfinite(required)):
                raise FloatingPointError
            # Each path ends at initial*growth + contribution*annuity. Its
            # required contribution is solved exactly. The empirical quantile
            # meets at least ceil(reliability*N) paths using the SAME draws.
            rank = math.ceil(body.reliability * paths) - 1
            needed = float(np.partition(required, rank)[rank])
            if needed > float(MAX_MONEY):
                raise md.DataError(422, "The required monthly amount exceeds the EUR 1 trillion calculation limit. Try a longer horizon or a smaller goal.")
            monthly = Decimal(str(needed)).quantize(CENT, rounding=ROUND_CEILING)
            terminal = initial_wealth + float(monthly) * annuity
            if np.count_nonzero(terminal >= float(body.goal_eur)) < rank + 1:
                # Protect the empirical threshold against floating-point equality.
                monthly += CENT
                terminal = initial_wealth + float(monthly) * annuity
            if not np.all(np.isfinite(terminal)):
                raise FloatingPointError
            trajectory = trajectory_points(steps, today, mean, variance, paths, seed,
                                           float(body.starting_capital_eur), float(monthly))
    except (FloatingPointError, OverflowError, ValueError):
        raise md.DataError(422, "This history and horizon produce numerically unstable projections. Check price quality or choose a shorter horizon.") from None
    success = float(np.mean(terminal >= float(body.goal_eur)))
    quantiles = np.quantile(terminal, [0.05, 0.5, 0.95], method="higher")
    return {
        "monthly_contribution_eur": money_string(monthly),
        "target_probability": body.reliability, "success_probability": success,
        "zero_contribution_success_probability": float(np.mean(initial_wealth >= float(body.goal_eur))),
        "simulation_paths": paths, "seed": seed,
        "valuation_date": today.isoformat(), "goal_date": body.goal_date.isoformat(),
        "goal_eur": money_string(body.goal_eur), "trajectory": trajectory,
        "contribution_count": len(deposits), "first_contribution_date": deposits[0].isoformat(),
        "last_contribution_date": deposits[-1].isoformat(),
        "total_contributions_eur": money_string(monthly * len(deposits)),
        "total_invested_eur": money_string(body.starting_capital_eur + monthly * len(deposits)),
        "terminal_percentiles_eur": {key: money_string(value) for key, value in zip(("p05", "p50", "p95"), quantiles)},
        "warnings": list(estimates.get("warnings", [])) + [
            "Model-estimated success is not a guarantee. Historical parameters and 10,000 simulated paths are uncertain; real markets have fat tails and changing regimes.",
            "Independent normally distributed log returns, 252 trading days/year and actual calendar days/365.25. All available usable history is equally weighted.",
            "Fixed payments at each monthly anniversary (clamped to month end), first payment one month from today, with fractional shares and no withdrawals.",
            "Nominal EUR: inflation, taxes and transaction fees are not modelled. Fund expenses and distributions are reflected only insofar as they are in the price history. Adjusted histories may mix adjustment vintages after incremental downloads.",
            "No FX conversion is modelled. A EUR-traded instrument may still carry underlying foreign-currency exposure.",
        ],
    }
