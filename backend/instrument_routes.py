"""Authenticated catalogue reads and explicit acquisition commands."""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from auth import CurrentUser
from celery_app import enqueue_acquisition
from market_data import instrument_detail, read_quotes, request_acquisition, search_catalogue

router = APIRouter(prefix="/instruments", tags=["instruments"])
InstrumentId = Annotated[str, Path(pattern=r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")]


class AcquisitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Z0-9^][A-Z0-9._^=\-]*$")
    exchange: str | None = Field(default=None, pattern=r"^[A-Z0-9]{4}$")
    confirmed: Literal[True]
    mode: Literal["update", "backfill"] = "update"

    @field_validator("symbol", "exchange", mode="before")
    @classmethod
    def normalize_identifier(cls, value):
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def complete_listing_override(self):
        if (self.symbol is None) != (self.exchange is None):
            raise ValueError("Supply both symbol and exchange for a manual listing override, or omit both")
        return self

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit acquisition authorization is required")
        return value


class QuoteResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    date: str
    collected_at: datetime
    collection_date: str
    # Provider fields are deliberately unconstrained. Numeric values are
    # serialized exactly by read_quotes; nulls and unexpected shapes survive.
    open: Any = None
    high: Any = None
    low: Any = None
    close: Any = None
    volume: Any = None
    quality_flags: list[str] = Field(default_factory=list)
    adj_open: Any = None
    adj_high: Any = None
    adj_low: Any = None
    adj_close: Any = None
    adj_volume: Any = None


class QuotesResponse(BaseModel):
    as_of: datetime
    items: list[QuoteResponse]
    total: int
    limit: int
    offset: int


def knowledge_time(value):
    if value is not None and value.tzinfo is None:
        raise HTTPException(422, "as_of must include a timezone, for example 2026-09-17T12:00:00Z")
    return value


@router.get("/search")
def search(request: Request, user: CurrentUser, q: Annotated[str, Query(min_length=2, max_length=100)], limit: Annotated[int, Query(ge=1, le=50)] = 15):
    if len(q.strip()) < 2:
        raise HTTPException(422, "Enter at least two non-whitespace characters")
    return {"items": search_catalogue(request.app.state.database, q, limit)}


@router.get("/{isin}")
def detail(isin: InstrumentId, request: Request, user: CurrentUser, as_of: datetime | None = None):
    return instrument_detail(request.app.state.database, request.app.state.settings, isin, knowledge_time(as_of))


@router.get("/{isin}/quotes", response_model=QuotesResponse, response_model_exclude_unset=True)
def quotes(isin: InstrumentId, request: Request, user: CurrentUser, limit: Annotated[int, Query(ge=1, le=1000)] = 1000, offset: Annotated[int, Query(ge=0)] = 0, as_of: datetime | None = None):
    return read_quotes(request.app.state.database, isin, limit, offset, knowledge_time(as_of))


@router.post("/{isin}/acquisitions", status_code=202)
def acquire(isin: InstrumentId, body: AcquisitionRequest, request: Request, user: CurrentUser):
    job = request_acquisition(
        request.app.state.database, request.app.state.settings, isin,
        {"symbol": body.symbol, "exchange": body.exchange} if body.symbol is not None else None, body.mode,
        str(user["_id"]), enqueue_acquisition,
    )
    return {"job": job}
