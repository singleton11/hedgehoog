"""App-wide, admin-managed saved EUR price-return proxy (never a yield series)."""
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from auth import AdminUser, CurrentUser
import market_data as md
import portfolio_tracker as ledger
from indices import require_index

router = APIRouter(prefix="/settings/performance", tags=["settings"])
KEY = "portfolio_performance"


class ProxySetting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument_id: str | None = Field(..., pattern=r"^idx-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


def acquired_proxy_index(db, identifier, now):
    record = require_index(db, identifier)
    listing = {"symbol": record["symbol"], "exchange": "YAHOO"}
    state = db.instrument_state.find_one({"_id": identifier}) or {}
    if state.get("listing") != listing or not db.instruments.find_one({
        **md.observation_filter(db, identifier, listing, now), "collected_at": {"$lte": now},
    }):
        raise md.DataError(422, "Acquire the selected index before using it as a proxy.")
    instrument = {"instrument_id": identifier, "name": record["name"], **listing, "currency": "EUR"}
    rows = ledger.quote_rows(db, instrument, now)
    if not rows or not ledger.currency_supported(db, instrument, rows):
        raise md.DataError(422, "Only acquired indices with confirmed EUR price history are supported.")
    return instrument


def read_settings(db):
    record = db.app_settings.find_one({"_id": KEY}) or {}
    return {"proxy": record.get("proxy"), "updated_at": record.get("updated_at")}


@router.get("")
def get_settings(request: Request, user: CurrentUser):
    return read_settings(request.app.state.database)


@router.get("/indices")
def proxy_indices(request: Request, user: CurrentUser,
                  limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0)):
    db, now = request.app.state.database, md.utcnow()
    items = []
    for record in db.indices.find({"removed_at": {"$exists": False}}).sort([("name", 1), ("instrument_id", 1)]):
        try:
            items.append(acquired_proxy_index(db, record["instrument_id"], now))
        except md.DataError as error:
            if error.status not in (404, 422):
                raise
    return {"items": items[offset:offset + limit], "total": len(items), "limit": limit, "offset": offset}


@router.put("")
def put_settings(body: ProxySetting, request: Request, user: AdminUser):
    db = request.app.state.database
    now = md.utcnow()
    proxy = acquired_proxy_index(db, body.instrument_id, now) if body.instrument_id else None
    db.app_settings.update_one({"_id": KEY}, {"$set": {
        "proxy": proxy, "updated_at": now.isoformat(), "updated_by": str(user["_id"]),
    }}, upsert=True)
    return read_settings(db)
