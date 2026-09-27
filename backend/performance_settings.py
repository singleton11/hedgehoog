"""App-wide, admin-managed saved EUR price-return proxy (never a yield series)."""
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from auth import AdminUser, CurrentUser
import market_data as md
import portfolio_tracker as ledger

router = APIRouter(prefix="/settings/performance", tags=["settings"])
KEY = "portfolio_performance"


class ProxySetting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument_id: str | None = Field(..., pattern=r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def read_settings(db):
    record = db.app_settings.find_one({"_id": KEY}) or {}
    return {"proxy": record.get("proxy"), "updated_at": record.get("updated_at")}


@router.get("")
def get_settings(request: Request, user: CurrentUser):
    return read_settings(request.app.state.database)


@router.put("")
def put_settings(body: ProxySetting, request: Request, user: AdminUser):
    db = request.app.state.database
    now = md.utcnow()
    proxy = ledger.acquired_instrument(db, body.instrument_id, now) if body.instrument_id else None
    db.app_settings.update_one({"_id": KEY}, {"$set": {
        "proxy": proxy, "updated_at": now.isoformat(), "updated_by": str(user["_id"]),
    }}, upsert=True)
    return read_settings(db)
