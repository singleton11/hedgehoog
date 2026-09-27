"""Read-only progress against an immutable saved plan, using refreshed estimates."""
from decimal import Decimal
from datetime import date

from goal_forecast import calendar_month_difference

from pydantic import TypeAdapter

import market_data as md
from goal_simulation import GoalInputs, Money, money_string
from goal_strategies import get_strategy


def track_plan(db, plan, snapshot, today):
    if snapshot["equity_eur"] is None:
        raise md.DataError(422, "Plan tracking requires a complete EUR portfolio valuation.")
    try:
        inputs = GoalInputs.model_validate({
            **{key: plan["inputs"][key] for key in ("goal_eur", "goal_date", "reliability")},
            "starting_capital_eur": snapshot["equity_eur"],
        })
        saved = plan["result"]
        monthly = TypeAdapter(Money).validate_python(saved["monthly_contribution_eur"])
        selection = plan["inputs"]["strategy"]
        strategy = get_strategy(selection["id"])
        paths, seed = saved["simulation_paths"], saved["seed"]
        if type(paths) is not int or not 1 <= paths <= 100_000 or type(seed) is not int or seed < 0:
            raise ValueError("Invalid simulation configuration")
        original_probability = float(saved["success_probability"])
        if not 0 <= original_probability <= 1:
            raise ValueError("Invalid probability")
    except (KeyError, ValueError, TypeError):
        raise md.DataError(422, "The saved plan does not contain a supported simulation snapshot.") from None
    if inputs.goal_date <= today:
        raise md.DataError(422, "The plan deadline has been reached; no future monthly investment can change its outcome.")
    if strategy.track is None:
        raise md.DataError(422, "Progress calculations are unavailable for this plan's strategy.")
    parameters = strategy.resolve_parameters(selection["parameters"], db)
    result = strategy.track(parameters, inputs, db, today, paths=paths, seed=seed, fixed_monthly=monthly)
    probability = result["fixed_contribution_success_probability"]
    forecast = result["forecast_goal_date"]
    # Positive means late; negative means ahead of the saved deadline.
    months = calendar_month_difference(inputs.goal_date, date.fromisoformat(forecast)) if forecast else None
    return {
        "forecast_goal_date": forecast, "forecast_status": result["forecast_status"],
        "forecast_search_end_date": result["forecast_search_end_date"],
        "schedule_difference_months": months,
        "schedule_difference_years": months / 12 if months is not None else None,
        "plan_id": plan["_id"], "valuation_date": today.isoformat(),
        "goal_date": inputs.goal_date.isoformat(), "goal_eur": money_string(inputs.goal_eur),
        "equity_eur": snapshot["equity_eur"], "strategy_name": strategy.name,
        "monthly_contribution_eur": money_string(monthly),
        "original_probability": original_probability, "target_probability": inputs.reliability,
        "success_probability": probability,
        "probability_change_pp": (probability - original_probability) * 100,
        "target_gap_pp": (probability - inputs.reliability) * 100,
        "on_track": probability >= inputs.reliability,
        "required_monthly_contribution_eur": result["monthly_contribution_eur"],
        "monthly_contribution_change_eur": money_string(Decimal(result["monthly_contribution_eur"]) - monthly),
        "required_contribution_success_probability": result["success_probability"],
        "simulation_paths": paths, "seed": seed,
        "warnings": snapshot["warnings"] + result["warnings"],
    }
