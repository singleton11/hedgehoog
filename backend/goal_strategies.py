"""Goal strategy catalogue, parameter validation and strategy-specific execution."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pymongo.database import Database

from goal_simulation import GoalInputs, simulate_buy_and_hold
import market_data as md
from return_statistics import instrument_statistics


AcquiredInstrument = Annotated[str, Field(
    strict=True, pattern=r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$",
    json_schema_extra={"parameter_type": "instrument", "universe": "acquired"},
)]


class BuyAndHoldParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument: AcquiredInstrument = Field(title="Instrument")


Parameters = TypeVar("Parameters", bound=BaseModel)


@dataclass(frozen=True)
class StrategyDefinition(Generic[Parameters]):
    id: str
    name: str
    description: str
    parameters_model: type[Parameters]
    simulate: Callable[[Parameters, GoalInputs, Database, date], dict[str, Any]]

    def resolve_parameters(self, values: dict[str, Any], db: Database) -> Parameters:
        parameters = self.parameters_model.model_validate(values)
        for key, field in self.parameters_model.model_fields.items():
            metadata = field.json_schema_extra or {}
            value = getattr(parameters, key)
            if value is not None and metadata.get("parameter_type") == "instrument" and metadata.get("universe") == "acquired":
                # Shared by all strategies: profiles define acquired membership,
                # not catalogue records, cached listings, queued jobs or quotes.
                if db.instruments.find_one({"instrument_id": value}, {"_id": 1}) is None:
                    raise md.DataError(422, "Choose an instrument from the acquired universe. Acquire its data first.")
        return parameters

    def public_definition(self):
        parameters = []
        for key, field in self.parameters_model.model_fields.items():
            metadata = field.json_schema_extra or {}
            parameters.append({
                "key": key, "type": metadata["parameter_type"], "label": field.title or key,
                "required": field.is_required(), "universe": metadata.get("universe"),
            })
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "parameters": parameters,
        }


def buy_and_hold_single_instrument(parameters: BuyAndHoldParameters, goals: GoalInputs, db: Database, today: date):
    estimates = instrument_statistics(db, parameters.instrument)
    if not estimates["eligible"]:
        raise md.DataError(422, estimates["ineligible_reason"])
    return {
        **simulate_buy_and_hold(estimates, goals, today),
        "parameter_statistics": {"instrument": estimates},
    }


STRATEGIES = {
    "buy_and_hold_single_instrument": StrategyDefinition(
        id="buy_and_hold_single_instrument",
        name="Buy and hold single instrument",
        description="Invest starting capital and each monthly contribution in one instrument and hold it.",
        parameters_model=BuyAndHoldParameters,
        simulate=buy_and_hold_single_instrument,
    ),
}


def get_strategy(strategy_id: str) -> StrategyDefinition:
    strategy = STRATEGIES.get(strategy_id)
    if strategy is None:
        raise ValueError(f"Unknown goal strategy: {strategy_id}")
    return strategy


class StrategySelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    parameters: dict[str, Any]

    @model_validator(mode="after")
    def validate_parameters(self):
        strategy = get_strategy(self.id)
        self.parameters = strategy.parameters_model.model_validate(self.parameters).model_dump(mode="json")
        return self
