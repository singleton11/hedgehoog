"""Authenticated strategy catalogue and goal simulation routes."""

from fastapi import APIRouter, Request

from auth import CurrentUser
from goal_simulation import GoalInputs, contribution_dates
from goal_strategies import STRATEGIES, StrategySelection, get_strategy
import market_data as md

router = APIRouter(prefix="/goals", tags=["goals"])


class GoalRequest(GoalInputs):
    strategy: StrategySelection


@router.get("/strategies")
def strategies(user: CurrentUser):
    return {"items": [strategy.public_definition() for strategy in STRATEGIES.values()]}


@router.post("/simulate")
def simulate(body: GoalRequest, request: Request, user: CurrentUser):
    today = md.utcnow().date()
    # Validate the horizon before doing database work or allocating paths.
    contribution_dates(today, body.goal_date)
    strategy = get_strategy(body.strategy.id)
    parameters = strategy.resolve_parameters(body.strategy.parameters, request.app.state.database)
    result = strategy.simulate(parameters, body, request.app.state.database, today)
    return {**result, "strategy": body.strategy.model_dump(mode="json")}
