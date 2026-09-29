"""Authenticated strategy catalogue and goal simulation routes."""

from fastapi import APIRouter, Query, Request, Response

from auth import CurrentUser
from calibration_results import calculation_order, latest_result, latest_summaries, run_history, run_result, save_result
from goal_simulation import GoalInputs, contribution_dates
from goal_calibration import RandomCalibrationInputs
from goal_strategies import STRATEGIES, StrategySelection, get_strategy
import market_data as md

router = APIRouter(prefix="/goals", tags=["goals"])


class GoalRequest(GoalInputs):
    strategy: StrategySelection


class CalibrationRequest(RandomCalibrationInputs):
    strategy: StrategySelection


def simulation_cutoff(body):
    """Pin model inputs and the horizon to one UTC knowledge-time cutoff."""
    now = md.utcnow()
    cutoff = body.as_of or now
    if cutoff > now:
        raise md.DataError(422, "Simulation as_of cannot be in the future.")
    effective = body.model_copy()
    effective._point_in_time = body.as_of is not None
    effective._effective_as_of = cutoff
    contribution_dates(cutoff.date(), effective.goal_date)
    return effective, cutoff


@router.post("/calibrate")
def calibrate(body: CalibrationRequest, request: Request, user: CurrentUser, response: Response):
    response.headers["Cache-Control"] = "no-store"
    strategy = get_strategy(body.strategy.id)
    if strategy.calibrate is None:
        raise md.DataError(422, "Calibration is not implemented for this strategy.")
    order = calculation_order()
    db = request.app.state.database
    parameters = strategy.resolve_parameters(body.strategy.parameters, db)
    result = strategy.calibrate(parameters, body, db)
    result = {**result, "strategy": body.strategy.model_dump(mode="json"), "calculated_at": md.public(md.utcnow())}
    return save_result(db, user["_id"], strategy.id, result, order, strategy_name=strategy.name)


@router.get("/calibrations")
def calibrations(request: Request, user: CurrentUser, response: Response,
                 limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    response.headers["Cache-Control"] = "no-store"
    return run_history(request.app.state.database, user["_id"], limit, offset)


@router.get("/calibrations/{run_id}")
def calibration_run(run_id: str, request: Request, user: CurrentUser, response: Response):
    response.headers["Cache-Control"] = "no-store"
    result = run_result(request.app.state.database, user["_id"], run_id)
    if result is None:
        raise md.DataError(404, "Calibration run not found.")
    return {"result": result}


@router.get("/strategies")
def strategies(request: Request, user: CurrentUser, response: Response):
    response.headers["Cache-Control"] = "no-store"
    summaries = latest_summaries(request.app.state.database, user["_id"], STRATEGIES)
    return {"items": [{**strategy.public_definition(), "latest_calibration": summaries.get(strategy.id)}
                      for strategy in STRATEGIES.values()]}


@router.get("/strategies/{strategy_id}/calibration")
def saved_calibration(strategy_id: str, request: Request, user: CurrentUser, response: Response):
    response.headers["Cache-Control"] = "no-store"
    if strategy_id not in STRATEGIES:
        raise md.DataError(404, "Strategy not found.")
    return {"result": latest_result(request.app.state.database, user["_id"], strategy_id)}


@router.post("/simulate")
def simulate(body: GoalRequest, request: Request, user: CurrentUser):
    # One captured cutoff drives both historical statistics and the forecast schedule.
    body, cutoff = simulation_cutoff(body)
    strategy = get_strategy(body.strategy.id)
    parameters = strategy.resolve_parameters(body.strategy.parameters, request.app.state.database)
    result = strategy.simulate(parameters, body, request.app.state.database, cutoff.date())
    return {**result, "data_as_of": md.public(cutoff),
            "strategy": body.strategy.model_dump(mode="json")}
