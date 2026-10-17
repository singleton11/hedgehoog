"""Independent seeded goals, training-only funding solves and held-out replay."""

from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal

import numpy as np

import market_data as md
from goal_calibration import (
    CalibrationInputs, RandomCalibrationInputs, MAX_ORIGINS, UnscorableOutcome,
    calibration_metrics, calibration_warnings, is_eur_history, monthly_origins,
    reaches_goal, realized_value,
)
from goal_simulation import PATHS, add_months, money_string, period_factors, required_contribution
from return_statistics import MIN_RETURNS, calculate_daily_statistics, calculate_statistics, positive_price
from quote_fill import fill_adjusted_rows

# Disjoint domains: scheduling, scenario parameters, funding solver and validation.
SCHEDULE_STREAM, SCENARIO_STREAM, SOLVER_STREAM, PREDICTION_STREAM = 149, 150, 151, 152
MAX_GENERATED_PERIODS = 30_000  # Includes BOTH independent Monte Carlo streams.
GENERATION = {
    "method": "independent_goals_contribution_solver", "version": 2, "solver_simulation_paths": PATHS,
    "capital_min_eur": "1000.00", "capital_max_eur": "100000.00",
    "goal_max_eur": "1000000.00", "goal_rule": "above_starting_capital",
    "probability_min": 0.01, "probability_max": 0.99,
}


@dataclass(frozen=True)
class GeneratedScenario:
    id: str
    capital: Decimal
    goal: Decimal
    horizon: int
    requested_probability: float


def plan_scenarios(dates, origins, body: RandomCalibrationInputs, seed):
    """Allocate evenly across dates, with a seeded random remainder/subset.

    Availability depends ONLY on calendar dates, never price values, fit quality
    or later successes. Invalid assigned cases are excluded, never replaced.
    Each date/local-index identifies a stable parameter and probability stream.
    """
    first_possible = add_months(dates[0], body.lookback_months)
    available = [index for index in origins if dates[index] >= first_possible
                 and add_months(dates[index], body.min_horizon_months) <= dates[-1]]
    if not available:
        raise md.DataError(422, "No historical origins fit a full training lookback and minimum forecast horizon. Download more history or shorten those windows.")
    if len(available) > MAX_ORIGINS:
        raise md.DataError(422, "Generated calibration supports at most 600 historical origins. Choose a narrower forecast date range.")
    rng = np.random.default_rng(np.random.SeedSequence([seed, SCHEDULE_STREAM]))
    order = rng.permutation(available)
    groups = defaultdict(list)
    max_horizons = {}
    for index in available:
        # Recent origins can use shorter horizons, but never an immature goal.
        max_horizons[index] = max(month for month in range(body.min_horizon_months, body.max_horizon_months + 1)
                                  if add_months(dates[index], month) <= dates[-1])
    for number in range(body.scenario_count):
        index = int(order[number % len(order)])
        local_index = number // len(order)
        day = dates[index]
        draws = np.random.default_rng(np.random.SeedSequence([seed, day.toordinal(), SCENARIO_STREAM, local_index]))
        # Cycle across ten probability strata at each historical origin. These
        # are requested design rates, not fabricated forecast probabilities.
        bucket = (day.toordinal() + local_index) % 10
        capital_cents = int(draws.integers(100_000, 10_000_001))
        groups[index].append(GeneratedScenario(
            id=f"{day.isoformat()}:{local_index + 1}",
            capital=Decimal(capital_cents) / 100,
            goal=Decimal(int(draws.integers(capital_cents + 1, 100_000_001))) / 100,
            horizon=int(draws.integers(body.min_horizon_months, max_horizons[index] + 1)),
            requested_probability=float(draws.uniform(max(bucket / 10, 0.01), min((bucket + 1) / 10, 0.99))),
        ))
    periods = 2 * sum(max(scenario.horizon for scenario in group) for group in groups.values())
    if periods > MAX_GENERATED_PERIODS:
        raise md.DataError(422, "Generated calibration exceeds 30,000 simulated monthly periods across both streams. Reduce the scenario count, maximum horizon or forecast date range.")
    return groups, len(available)


def evaluate_random_history(rows, body: RandomCalibrationInputs, *, seed, override=None, paths=PATHS,
                            quote_fill_policy=None, instrument_id=""):
    rows, dates, origins = monthly_origins(rows, body)
    groups, available_origins = plan_scenarios(dates, origins, body, seed)
    forecasts, excluded = [], []
    raw_prices = quality_warning = False

    for index, group in sorted(groups.items()):
        origin = dates[index]

        def exclude(scenarios, reason):
            excluded.extend({"scenario_id": scenario.id, "origin_date": origin.isoformat(), "reason": reason}
                            for scenario in scenarios)

        first_index = bisect_left(dates, add_months(origin, -body.lookback_months))
        training = fill_adjusted_rows(rows[first_index:index + 1], instrument_id, quote_fill_policy,
                                      cutoff=origin.isoformat(), currency_override=override)
        if not is_eur_history(training, override):
            exclude(group, "training_currency")
            continue
        estimates = calculate_statistics(training)
        basis = estimates["price_basis"]
        if estimates["return_count"] < MIN_RETURNS:
            exclude(group, "insufficient_training")
            continue
        if positive_price(training[-1].get(basis)) is None or estimates["daily_log_return_variance"] is None:
            exclude(group, "unusable_training")
            continue
        return_runs = None
        if body.simulation_method == "block_bootstrap":
            # Only the trailing training prefix can supply blocks. Never bridge
            # gaps or use returns from a held-out goal outcome.
            return_runs = calculate_daily_statistics(training, include_return_runs=True)["return_runs"]
            if not any(len(run) >= body.block_size for run in return_runs):
                exclude(group, "insufficient_block_history")
                continue
        raw_prices |= basis == "close"
        quality_warning |= bool(estimates["invalid_price_count"] or estimates["skipped_gap_count"])
        outcomes = {}
        by_horizon = defaultdict(list)
        for scenario in group:
            by_horizon[scenario.horizon].append(scenario)
        steps = [(add_months(origin, month), True) for month in range(1, max(by_horizon) + 1)]
        streams = [period_factors(steps, origin, estimates["daily_mean_log_return"],
                                  estimates["daily_log_return_variance"], paths,
                                  np.random.SeedSequence([seed, origin.toordinal(), domain]),
                                  **({"return_runs": return_runs, "block_size": body.block_size}
                                     if body.simulation_method == "block_bootstrap" else {}))
                   for domain in (SOLVER_STREAM, PREDICTION_STREAM)]
        solver_growth, solver_annuity = np.ones(paths), np.zeros(paths)
        prediction_growth, prediction_annuity = np.ones(paths), np.zeros(paths)
        try:
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                # Reuse the two date-local growth streams for every funding
                # combination, with O(paths) working memory, not N full runs.
                for month, (solver_step, prediction_step) in enumerate(zip(*streams), 1):
                    solver_growth *= solver_step[2]
                    solver_annuity = solver_annuity * solver_step[2] + 1
                    prediction_growth *= prediction_step[2]
                    prediction_annuity = prediction_annuity * prediction_step[2] + 1
                    for scenario in by_horizon[month]:
                        goal = scenario.goal
                        monthly, _ = required_contribution(
                            solver_growth, solver_annuity, scenario.capital, goal, scenario.requested_probability)
                        predicted_wealth = float(scenario.capital) * prediction_growth + float(monthly) * prediction_annuity
                        if not np.all(np.isfinite(predicted_wealth)):
                            raise FloatingPointError
                        # Match the planner's strict simulated-wealth threshold;
                        # never substitute the requested rate for this estimate.
                        probability = float(np.mean(predicted_wealth >= float(goal)))
                        fixed = CalibrationInputs(goal_eur=goal, starting_capital_eur=scenario.capital,
                                                  monthly_contribution_eur=monthly, horizon_months=month,
                                                  lookback_months=body.lookback_months)
                        goal_date = add_months(origin, month)
                        # Goal, solved funding and validation probability are frozen
                        # before the first read of this case's held-out outcome.
                        try:
                            if month not in outcomes:
                                outcome = fill_adjusted_rows(rows, instrument_id, quote_fill_policy,
                                                             cutoff=goal_date.isoformat(), currency_override=override,
                                                             pinned_rows={origin.isoformat(): training[-1]})
                                outcomes[month] = outcome
                            outcome = outcomes[month]
                            terminal, valuation_date = realized_value(outcome, dates[:len(outcome)], index, goal_date, basis, fixed, override)
                        except UnscorableOutcome as error:
                            exclude([scenario], str(error))
                            continue
                        forecasts.append({
                            "scenario_id": scenario.id, "origin_date": origin.isoformat(), "goal_date": goal_date.isoformat(),
                            "training_start_date": training[0]["date"], "training_end_date": training[-1]["date"],
                            "training_return_count": estimates["return_count"], "price_basis": basis,
                            "goal_eur": money_string(goal), "starting_capital_eur": money_string(scenario.capital),
                            "monthly_contribution_eur": money_string(monthly), "horizon_months": month,
                            "requested_probability": scenario.requested_probability, "predicted_probability": probability,
                            "observed_success": reaches_goal(terminal, goal), "terminal_value_eur": money_string(terminal),
                            "valuation_date": valuation_date.isoformat(),
                        })
        except (FloatingPointError, OverflowError, ValueError):
            raise md.DataError(422, "Generated forecasts are numerically unstable. Check price quality or reduce the maximum horizon.") from None

    counts = dict(sorted(Counter(item["reason"] for item in excluded).items()))
    if not forecasts:
        reasons = ", ".join(f"{key.replace('_', ' ')}: {value}" for key, value in counts.items())
        raise md.DataError(422, "No evaluable generated scenarios. Require EUR prices, at least 60 usable daily training returns, and valid historical execution prices. " + reasons)
    # Present chronologically, then by the date-local case number, not by the
    # order in which shared horizon computations happened to finish.
    key = lambda case: (case["origin_date"], int(case["scenario_id"].rsplit(":", 1)[1]))
    forecasts.sort(key=key)
    excluded.sort(key=key)
    origin_count = len({forecast["origin_date"] for forecast in forecasts})
    warnings = calibration_warnings(overlapping=body.max_horizon_months > 1, sample_count=len(forecasts),
                                    excluded=bool(excluded), raw_prices=raw_prices,
                                    quality_warning=quality_warning, override=override,
                                    simulation_method=body.simulation_method, block_size=body.block_size)
    warnings.extend([
        "Generated scenarios are allocated evenly across calendar-eligible origins with a seeded random remainder. Starting capital is uniform in cents from EUR 1,000 to EUR 100,000; goals are uniform in cents from starting capital plus EUR 0.01 to EUR 1,000,000. Horizons are uniform over the requested range that fits saved calendar coverage. Scenario inputs are independent of the simulation method and price values or outcomes; unavailable cases are not replaced.",
        "Requested success rates cycle across ten strata spanning 1%–99%. The selected method solves EUR/month with the planner's contribution quantile and upward cent rounding on one training-only simulation stream. A second independent stream estimates success with that frozen funding before historical replay. Rounding and flat histories can prevent actual probabilities from spanning all bins; requested rates are not reported as predictions.",
        "Many scenarios reuse the same historical paths and date-local simulations. Scored scenarios are NOT independent market observations. More scenarios improve probability-range coverage, not the amount of historical evidence; inspect the distinct historical-origin count.",
        "Scores weight each scored generated scenario equally and describe this generator's mix of targets, funding and horizons; they are not directly comparable across different generation policies.",
    ])
    if origin_count < 30:
        warnings.append("Fewer than 30 distinct scored historical origins; generating more scenarios does not fix this limited historical evidence.")
    metrics = calibration_metrics(forecasts)
    populated = sum(bin["count"] > 0 for bin in metrics["calibration_bins"])
    if populated < 10:
        warnings.append(f"Only {populated} of 10 probability bins contain scored scenarios. The system does not force probabilities into empty bins.")
    return {
        "settings": body.public_settings(), "seed": seed, "simulation_paths": paths, "method": "walk_forward",
        "history_first_date": rows[0]["date"], "history_last_date": rows[-1]["date"],
        "candidate_count": body.scenario_count, "available_origin_count": available_origins, "origin_count": origin_count,
        "generation": {**GENERATION, "solver_simulation_paths": paths},
        "skipped_counts": counts, "excluded_origins": excluded, **metrics,
        "forecasts": forecasts, "warnings": warnings,
    }
