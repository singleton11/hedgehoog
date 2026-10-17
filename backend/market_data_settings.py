"""Global calculation policy; independent of portfolio risk-proxy settings."""
from fastapi import APIRouter, Request
from pydantic import Field
from typing import Literal

from auth import AdminUser, CurrentUser
import market_data as md
from quote_fill import KEY, QuoteFillSettings, read_policy

router = APIRouter(prefix="/settings/market-data", tags=["settings"])


class PolicyUpdate(QuoteFillSettings):
    method: Literal["smooth", "normal"]
    seed: int = Field(ge=0, le=2**32 - 1, strict=True)


@router.get("")
def get_settings(request: Request, user: CurrentUser):
    return read_policy(request.app.state.database)


@router.put("")
def put_settings(body: PolicyUpdate, request: Request, user: AdminUser):
    db = request.app.state.database
    db.app_settings.update_one({"_id": KEY}, {"$set": {
        **body.model_dump(), "updated_at": md.utcnow().isoformat(), "updated_by": str(user["_id"]),
    }}, upsert=True)
    return read_policy(db)
