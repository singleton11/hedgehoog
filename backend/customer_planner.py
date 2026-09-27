"""Owner-scoped customers, editable goals and explicitly saved simulation snapshots."""

from datetime import date, timedelta
from typing import Annotated

from bson import ObjectId
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pymongo import ReturnDocument

from auth import CurrentUser
from goal_planner import GoalRequest
from goal_simulation import Money, contribution_dates, money_string
from goal_strategies import StrategySelection, get_strategy
import market_data as md

router = APIRouter(prefix="/customers", tags=["customer-planner"])
PREVIEW_TTL = timedelta(hours=24)
Name = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=200)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]
CREATED_ORDER = [("created_at", -1), ("_id", -1)]
SIMULATION_ORDER = [("calculated_at", -1), ("_id", -1)]
CUSTOMER_FIELDS = ("name", "created_at")
GOAL_FIELDS = ("customer_id", "name", "goal_eur", "goal_date", "created_at")
SIMULATION_FIELDS = ("customer_id", "goal_id", "calculated_at", "saved_at", "inputs", "strategy_definition", "result", "is_plan")
SUMMARY_PROJECTION = {
    "calculated_at": 1, "saved_at": 1, "inputs": 1, "strategy_definition.name": 1,
    "is_plan": 1, "goal_id": 1,
    "result.monthly_contribution_eur": 1, "result.success_probability": 1,
}


class CustomerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name


class CustomerGoalRequest(CustomerRequest):
    goal_eur: Money = Field(gt=0)
    goal_date: date


class CustomerGoalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name | None = None
    goal_eur: Money | None = Field(default=None, gt=0)
    goal_date: date | None = None

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set or any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Supply at least one goal field; fields cannot be null.")
        return self


class SimulationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: StrategySelection
    starting_capital_eur: Money
    reliability: float = Field(ge=0.5, le=0.99, allow_inf_nan=False)


class SaveRequest(BaseModel):
    # The client can identify a preview, never supply or replace its results.
    model_config = ConfigDict(extra="forbid")


def ensure_indexes(db):
    db.planner_customers.create_index([("user_id", 1), *CREATED_ORDER], name="planner_customers_owner_order")
    db.planner_goals.create_index(
        [("user_id", 1), ("customer_id", 1), *CREATED_ORDER], name="planner_goals_owner_customer_order",
    )
    db.planner_simulations.create_index(
        [("user_id", 1), ("customer_id", 1), ("goal_id", 1), *SIMULATION_ORDER],
        name="planner_simulations_owner_goal_order",
    )
    db.planner_simulations.create_index(
        [("user_id", 1), ("customer_id", 1), ("is_plan", 1), *SIMULATION_ORDER],
        name="planner_simulations_owner_plans_order",
    )
    db.planner_simulations.create_index("expires_at", expireAfterSeconds=0, name="planner_preview_expiry")


def _timestamp(now):
    # Fixed precision sorts chronologically and survives BSON without losing
    # sub-millisecond identity; _id resolves simultaneous timestamps.
    return md.aware(now).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _public(document, fields):
    # Old snapshots are simulations until explicitly promoted; no read-time migration.
    return {"id": document["_id"], **{
        field: document.get(field, False) if field == "is_plan" else document[field] for field in fields
    }}


def _page(collection, query, projection, order, serialize, limit, offset):
    rows = collection.find(query, projection).sort(order).skip(offset).limit(limit)
    return {"items": [serialize(row) for row in rows], "total": collection.count_documents(query),
            "limit": limit, "offset": offset}


def _customer(db, owner, customer_id):
    document = db.planner_customers.find_one({"_id": customer_id, "user_id": owner})
    if document is None:
        raise md.DataError(404, "Customer not found.")
    return document


def _goal(db, owner, customer_id, goal_id):
    _customer(db, owner, customer_id)
    document = db.planner_goals.find_one({"_id": goal_id, "user_id": owner, "customer_id": customer_id})
    if document is None:
        raise md.DataError(404, "Goal not found.")
    return document


def _simulation_scope(owner, customer_id, goal_id):
    return {"user_id": owner, "customer_id": customer_id, "goal_id": goal_id}


def _summary(document):
    return {"id": document["_id"], "is_plan": document.get("is_plan", False), "goal_id": document["goal_id"],
            "calculated_at": document["calculated_at"], "saved_at": document["saved_at"],
            "inputs": document["inputs"], "strategy_name": document["strategy_definition"]["name"],
            "monthly_contribution_eur": document["result"]["monthly_contribution_eur"],
            "success_probability": document["result"]["success_probability"]}


@router.get("")
def customers(request: Request, user: CurrentUser, limit: Limit = 20, offset: Offset = 0):
    # Portfolio routes use this module's ownership helpers; defer the reverse
    # dependency until request time to reuse the same valuation rules.
    from portfolio_tracker import customer_portfolio_summaries

    db, owner = request.app.state.database, str(user["_id"])
    page = _page(db.planner_customers, {"user_id": owner},
                 {field: 1 for field in CUSTOMER_FIELDS}, CREATED_ORDER,
                 lambda row: _public(row, CUSTOMER_FIELDS), limit, offset)
    customer_ids = [item["id"] for item in page["items"]]
    totals = customer_portfolio_summaries(db, owner, customer_ids)
    counts = {row["_id"]: row["count"] for row in db.planner_goals.aggregate([
        {"$match": {"user_id": owner, "customer_id": {"$in": customer_ids}}},
        {"$group": {"_id": "$customer_id", "count": {"$sum": 1}}},
    ])} if customer_ids else {}
    for item in page["items"]:
        item.update(totals[item["id"]], goal_count=counts.get(item["id"], 0))
    return page


@router.post("", status_code=201)
def create_customer(body: CustomerRequest, request: Request, user: CurrentUser):
    document = {"_id": str(ObjectId()), "user_id": str(user["_id"]), "name": body.name,
                "created_at": _timestamp(md.utcnow())}
    request.app.state.database.planner_customers.insert_one(document)
    return _public(document, CUSTOMER_FIELDS)


@router.get("/{customer_id}")
def customer(customer_id: str, request: Request, user: CurrentUser):
    return _public(_customer(request.app.state.database, str(user["_id"]), customer_id), CUSTOMER_FIELDS)


def _plan(db, owner, customer_id, plan_id):
    document = db.planner_simulations.find_one({
        "_id": plan_id, "user_id": owner, "customer_id": customer_id,
        "is_plan": True, "saved_at": {"$ne": None},
    })
    if document is None:
        raise md.DataError(404, "Plan not found.")
    return document


@router.get("/{customer_id}/plans")
def plans(customer_id: str, request: Request, user: CurrentUser, limit: Limit = 20, offset: Offset = 0):
    db, owner = request.app.state.database, str(user["_id"])
    _customer(db, owner, customer_id)
    return _page(db.planner_simulations,
                 {"user_id": owner, "customer_id": customer_id, "is_plan": True, "saved_at": {"$ne": None}},
                 SUMMARY_PROJECTION, SIMULATION_ORDER, _summary, limit, offset)


@router.get("/{customer_id}/goals")
def goals(customer_id: str, request: Request, user: CurrentUser, limit: Limit = 20, offset: Offset = 0):
    db, owner = request.app.state.database, str(user["_id"])
    _customer(db, owner, customer_id)
    return _page(db.planner_goals, {"user_id": owner, "customer_id": customer_id},
                 {field: 1 for field in GOAL_FIELDS}, CREATED_ORDER,
                 lambda row: _public(row, GOAL_FIELDS), limit, offset)


@router.post("/{customer_id}/goals", status_code=201)
def create_goal(customer_id: str, body: CustomerGoalRequest, request: Request, user: CurrentUser):
    db, owner = request.app.state.database, str(user["_id"])
    _customer(db, owner, customer_id)
    now = md.utcnow()
    contribution_dates(now.date(), body.goal_date)
    document = {"_id": str(ObjectId()), "user_id": owner, "customer_id": customer_id, "name": body.name,
                "goal_eur": money_string(body.goal_eur), "goal_date": body.goal_date.isoformat(),
                "created_at": _timestamp(now)}
    db.planner_goals.insert_one(document)
    return _public(document, GOAL_FIELDS)


@router.get("/{customer_id}/goals/{goal_id}")
def goal(customer_id: str, goal_id: str, request: Request, user: CurrentUser):
    return _public(_goal(request.app.state.database, str(user["_id"]), customer_id, goal_id), GOAL_FIELDS)


@router.patch("/{customer_id}/goals/{goal_id}")
def update_goal(customer_id: str, goal_id: str, body: CustomerGoalUpdate, request: Request, user: CurrentUser):
    db, owner = request.app.state.database, str(user["_id"])
    target = _goal(db, owner, customer_id, goal_id)
    changes = body.model_dump(exclude_unset=True)
    if "goal_eur" in changes:
        changes["goal_eur"] = money_string(body.goal_eur)
    if "goal_date" in changes:
        changes["goal_date"] = body.goal_date.isoformat()
        # Renaming an older goal must not require moving its date. Only a
        # changed date is validated against today's simulation horizon.
        if changes["goal_date"] != target["goal_date"]:
            contribution_dates(md.utcnow().date(), body.goal_date)
    document = db.planner_goals.find_one_and_update(
        {"_id": goal_id, "user_id": owner, "customer_id": customer_id},
        {"$set": changes}, return_document=ReturnDocument.AFTER,
    )
    if document is None:
        raise md.DataError(404, "Goal not found.")
    # Simulation inputs/results and portfolio plan attachments are untouched.
    return _public(document, GOAL_FIELDS)


@router.post("/{customer_id}/goals/{goal_id}/simulate")
def simulate(customer_id: str, goal_id: str, body: SimulationRequest, request: Request, user: CurrentUser):
    db, owner = request.app.state.database, str(user["_id"])
    target = _goal(db, owner, customer_id, goal_id)
    inputs = GoalRequest(goal_eur=target["goal_eur"], goal_date=target["goal_date"], **body.model_dump())
    today = md.utcnow().date()
    contribution_dates(today, inputs.goal_date)
    strategy = get_strategy(inputs.strategy.id)
    parameters = strategy.resolve_parameters(inputs.strategy.parameters, db)
    # Dispatch through the same fresh eligibility checks and exact solver as
    # /goals/simulate. Nothing here changes the numerical model.
    result = strategy.simulate(parameters, inputs, db, today)
    now = md.utcnow()
    normalized_inputs = {**inputs.model_dump(mode="json"), "goal_eur": money_string(inputs.goal_eur),
                         "starting_capital_eur": money_string(inputs.starting_capital_eur)}
    document = {
        "_id": str(ObjectId()), **_simulation_scope(owner, customer_id, goal_id),
        "calculated_at": _timestamp(now), "saved_at": None, "expires_at": now + PREVIEW_TTL,
        "inputs": normalized_inputs, "strategy_definition": md.public(strategy.public_definition()),
        "result": md.public({**result, "strategy": inputs.strategy.model_dump(mode="json")}),
    }
    # Only successful calculations create previews. Previews never enter history.
    db.planner_simulations.insert_one(document)
    return _public(document, SIMULATION_FIELDS)


@router.post("/{customer_id}/goals/{goal_id}/simulations/{simulation_id}/save")
def save_simulation(customer_id: str, goal_id: str, simulation_id: str, request: Request, user: CurrentUser,
                    body: SaveRequest | None = None):
    db, owner = request.app.state.database, str(user["_id"])
    _goal(db, owner, customer_id, goal_id)
    scope = {"_id": simulation_id, **_simulation_scope(owner, customer_id, goal_id)}
    now = md.utcnow()
    document = db.planner_simulations.find_one_and_update(
        {**scope, "saved_at": None, "expires_at": {"$gt": now}},
        {"$set": {"saved_at": _timestamp(now)}, "$unset": {"expires_at": ""}},
        return_document=ReturnDocument.AFTER,
    )
    if document is None:
        # Another save may have won; return that immutable snapshot verbatim.
        # Expiration is enforced above even while Mongo's TTL monitor is delayed.
        document = db.planner_simulations.find_one(scope)
        if document is None:
            raise md.DataError(404, "Simulation preview not found or expired.")
        if document["saved_at"] is None:
            raise md.DataError(410, "Simulation preview expired. Calculate a new preview before saving.")
    return _public(document, SIMULATION_FIELDS)


@router.post("/{customer_id}/goals/{goal_id}/simulations/{simulation_id}/plan")
def make_plan(customer_id: str, goal_id: str, simulation_id: str, request: Request, user: CurrentUser,
              body: SaveRequest | None = None):
    db, owner = request.app.state.database, str(user["_id"])
    _goal(db, owner, customer_id, goal_id)
    document = db.planner_simulations.find_one_and_update(
        {"_id": simulation_id, **_simulation_scope(owner, customer_id, goal_id), "saved_at": {"$ne": None}},
        {"$set": {"is_plan": True}}, return_document=ReturnDocument.AFTER,
    )
    if document is None:
        raise md.DataError(404, "Saved simulation not found.")
    return _public(document, SIMULATION_FIELDS)


@router.get("/{customer_id}/goals/{goal_id}/simulations")
def simulations(customer_id: str, goal_id: str, request: Request, user: CurrentUser,
                limit: Limit = 20, offset: Offset = 0):
    db, owner = request.app.state.database, str(user["_id"])
    _goal(db, owner, customer_id, goal_id)
    query = {**_simulation_scope(owner, customer_id, goal_id), "saved_at": {"$ne": None}}
    return _page(db.planner_simulations, query, SUMMARY_PROJECTION, SIMULATION_ORDER, _summary, limit, offset)


@router.get("/{customer_id}/goals/{goal_id}/simulations/{simulation_id}")
def simulation(customer_id: str, goal_id: str, simulation_id: str, request: Request, user: CurrentUser):
    db, owner = request.app.state.database, str(user["_id"])
    _goal(db, owner, customer_id, goal_id)
    document = db.planner_simulations.find_one({"_id": simulation_id, **_simulation_scope(owner, customer_id, goal_id),
                                              "saved_at": {"$ne": None}})
    if document is None:
        raise md.DataError(404, "Saved simulation not found.")
    # No registry validation or market-data reads: stored snapshots outlive both.
    return _public(document, SIMULATION_FIELDS)
