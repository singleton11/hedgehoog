"""Authenticated, explicit index catalogue and acquisition API."""
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from auth import CurrentUser
from celery_app import enqueue_acquisition
from indices import add_index, index_detail, list_indices, remove_index, require_index
from instrument_routes import Generation, QuotesResponse, knowledge_time
from market_data import read_quotes, request_acquisition

router = APIRouter(prefix="/indices", tags=["indices"])
IndexId = Annotated[str, Path(pattern=r"^idx-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]


class IndexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9^][A-Z0-9._^=\-]*$")
    name: str = Field(min_length=1, max_length=200)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    return_variant: Literal["unknown", "price", "net_total_return", "gross_total_return"] = "unknown"

    @field_validator("symbol", "currency", mode="before")
    @classmethod
    def normalize_identifiers(cls, value):
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class IndexAcquisitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit acquisition authorization is required")
        return value


@router.get("")
def catalogue(request: Request, user: CurrentUser, limit: Annotated[int, Query(ge=1, le=100)] = 20, offset: Annotated[int, Query(ge=0)] = 0):
    return list_indices(request.app.state.database, limit, offset)


@router.post("", status_code=201)
def create_index(body: IndexRequest, request: Request, user: CurrentUser):
    return add_index(request.app.state.database, body.model_dump(), str(user["_id"]))


@router.get("/{instrument_id}")
def detail(instrument_id: IndexId, request: Request, user: CurrentUser):
    return index_detail(request.app.state.database, instrument_id)


@router.delete("/{instrument_id}")
def remove(instrument_id: IndexId, request: Request, user: CurrentUser):
    return remove_index(request.app.state.database, instrument_id, str(user["_id"]))


@router.post("/{instrument_id}/acquisitions", status_code=202)
def acquire(instrument_id: IndexId, body: IndexAcquisitionRequest, request: Request, user: CurrentUser):
    record = require_index(request.app.state.database, instrument_id)
    job = request_acquisition(request.app.state.database, request.app.state.settings,
                              instrument_id, {"symbol": record["symbol"], "exchange": "YAHOO"},
                              "update", str(user["_id"]), enqueue_acquisition)
    return {"job": job}


@router.get("/{instrument_id}/quotes", response_model=QuotesResponse, response_model_exclude_unset=True)
def quotes(instrument_id: IndexId, request: Request, user: CurrentUser, limit: Annotated[int, Query(ge=1, le=1000)] = 1000, offset: Annotated[int, Query(ge=0)] = 0, as_of: datetime | None = None, generation: Generation = None):
    return read_quotes(request.app.state.database, instrument_id, limit, offset, knowledge_time(as_of), generation)
