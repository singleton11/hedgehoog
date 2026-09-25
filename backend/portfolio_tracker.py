"""EUR-only, funded, long-only portfolios with a dated, removable ledger.

One bounded Mongo document is the atomic write boundary. A revision CAS protects
cash/holdings across processes without requiring a replica set or transactions.
All accounting uses Decimal; rounded EUR settlement amounts, never JSON floats.
"""

from datetime import date as Date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Annotated, Literal
from uuid import UUID

from bson import Decimal128, ObjectId
from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from auth import CurrentUser
from customer_planner import Name, Limit, Offset, CREATED_ORDER, _customer, _goal, _page, _timestamp
import market_data as md

router = APIRouter(tags=["portfolios"])
BASE = "/customers/{customer_id}/portfolios"
MAX_TRANSACTIONS = 5000  # Keeps the single-document atomic ledger below BSON's 16 MB limit.
MAX_AMOUNT = Decimal("1000000000000")
CENT = Decimal("0.01")
ZERO = Decimal(0)
Money = Annotated[Decimal, Field(ge=0, le=MAX_AMOUNT, decimal_places=2, allow_inf_nan=False)]
Units = Annotated[Decimal, Field(gt=0, le=MAX_AMOUNT, decimal_places=8, allow_inf_nan=False)]
META_FIELDS = ("customer_id", "name", "goal_id", "currency", "created_at", "revision")


class PortfolioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    goal_id: str | None = Field(default=None, min_length=1, max_length=100)


class DatedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: UUID
    date: Date = Field(default_factory=lambda: md.utcnow().date())

    @field_validator("date", mode="before")
    @classmethod
    def iso_day(cls, value):
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError("Use a YYYY-MM-DD date.")
        return value

    @field_validator("date")
    @classmethod
    def past_or_today(cls, value):
        if value > md.utcnow().date():
            raise ValueError("Transaction date cannot be in the future (UTC).")
        return value


class CashRequest(DatedRequest):
    type: Literal["deposit", "withdrawal"]
    amount_eur: Money = Field(gt=0)
    currency: Literal["EUR"] = "EUR"


class TradeRequest(DatedRequest):
    type: Literal["buy", "sell"]
    instrument_id: Annotated[str, Field(strict=True, pattern=r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")]
    quantity: Units
    price_eur: Units
    commission_eur: Money = Decimal(0)


TransactionRequest = Annotated[CashRequest | TradeRequest, Body(discriminator="type")]


def ensure_indexes(db):
    db.portfolios.create_index([("user_id", 1), ("customer_id", 1), *CREATED_ORDER], name="portfolios_owner_customer_order")


def decimal_string(value):
    return format(value, "f")


def euros(value):
    if value is None:
        return None
    rounded = value.quantize(CENT, rounding=ROUND_HALF_UP)
    return format(abs(rounded) if rounded == 0 else rounded, ".2f")


def metadata(document):
    return {"id": document["_id"], **{key: document[key] for key in META_FIELDS}}


def event_public(event):
    return {key: value for key, value in event.items() if not key.startswith("_")}


def active_events(events):
    # Keep removed entries internally to preserve insertion order, idempotency
    # keys and the document-size cap. They never contribute to history or value.
    return [event for event in events if not event.get("_removed_at")]


def scope(owner, customer_id, portfolio_id=None):
    return {"user_id": owner, "customer_id": customer_id, **({"_id": portfolio_id} if portfolio_id else {})}


def require_portfolio(db, owner, customer_id, portfolio_id):
    _customer(db, owner, customer_id)
    document = db.portfolios.find_one(scope(owner, customer_id, portfolio_id))
    if document is None:
        raise md.DataError(404, "Portfolio not found.")
    return document


def quote_rows(db, instrument, now):
    """Latest observation per distinct effective date, on the pinned listing only."""
    query = {**md.listing_filter(instrument["instrument_id"], instrument),
             "date": {"$lte": now.date().isoformat()}, "collected_at": {"$lte": now}}
    rows = db.quotes.aggregate([
        {"$match": query}, {"$sort": {"date": 1, "collected_at": -1, "_id": -1}},
        {"$group": {"_id": "$date", "data": {"$first": {
            "close": {"$ifNull": ["$data.close", None]},
            "price_currency": {"$ifNull": ["$data.price_currency", None]},
        }}}}, {"$sort": {"_id": 1}},
    ])
    return [{**row["data"], "date": row["_id"]} for row in rows if md.quote_date(row["_id"]) == row["_id"]]


def currency_supported(db, instrument, rows):
    # Fund base currency is NOT listing price currency. An override can fill
    # missing metadata but never relabel explicit foreign-currency observations.
    currencies = {row["price_currency"].strip().upper() for row in rows
                  if isinstance(row.get("price_currency"), str) and row["price_currency"].strip()}
    state = db.instrument_state.find_one({"_id": instrument["instrument_id"]}) or {}
    listing = {key: instrument[key] for key in ("symbol", "exchange")}
    override = state.get("price_currency_override") if state.get("listing") == listing else None
    if currencies - {"EUR"} or (override and override != "EUR"):
        return False
    return override == "EUR" or (bool(rows) and all(
        isinstance(row.get("price_currency"), str) and row["price_currency"].strip().upper() == "EUR" for row in rows))


def acquired_instrument(db, isin, now):
    state = db.instrument_state.find_one({"_id": isin}) or {}
    listing = state.get("listing") or {}
    if not all(isinstance(listing.get(key), str) and listing[key].strip() and len(listing[key].encode("utf-8")) <= 200
               for key in ("symbol", "exchange")):
        raise md.DataError(422, "Acquire a EUR instrument listing before trading.")
    if not db.instruments.find_one(md.listing_filter(isin, listing)):
        raise md.DataError(422, "Acquire the selected instrument listing before trading.")
    catalogue = md.latest_catalogue(db, isin) or {}
    instrument = {"instrument_id": isin, "name": str(catalogue.get("name") or isin)[:200],
                  "symbol": listing["symbol"], "exchange": listing["exchange"], "currency": "EUR"}
    if not currency_supported(db, instrument, quote_rows(db, instrument, now)):
        raise md.DataError(422, "Only confirmed EUR-priced instruments are supported. Check price currency in instrument details.")
    return instrument


def price(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal128):
        value = value.to_decimal()
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    # Preserve provider evidence, but do not let pathological exponents reach
    # monetary rendering. No fallback to adjusted or older valid prices.
    return number if number.is_finite() and Decimal("1e-18") <= number <= Decimal("1e18") else None


def daily_interval(start, end):
    """Conservative weekday adjacency: do not label a data gap as daily PnL."""
    first, last = Date.fromisoformat(start), Date.fromisoformat(end)
    gap = (last - first).days
    return first.weekday() < 5 and last.weekday() < 5 and 0 < gap <= 3 and sum(
        (first + timedelta(days=step)).weekday() < 5 for step in range(1, gap + 1)
    ) == 1


def replay(events, anchors=None):
    """Average cost; optional prior-close reference basis for remaining shares.

    Each same-day transaction follows its immutable insertion sequence. Backdated
    insertion or removal replays ALL active events and fails at the first negative balance.
    Sell allocation uses the same average-cost proportion for the daily reference.
    A full liquidation removes the exact remaining basis (no rounding residual).
    """
    with localcontext() as context:
        context.prec = 80
        cash, realized = ZERO, ZERO
        positions = {}
        anchors = anchors or {}
        for event in sorted(active_events(events), key=lambda item: (item["date"], item["sequence"])):
            kind = event["type"]
            if kind in ("deposit", "withdrawal"):
                amount = Decimal(event["amount_eur"])
                cash += amount if kind == "deposit" else -amount
            else:
                instrument = event["instrument"]
                isin = instrument["instrument_id"]
                position = positions.setdefault(isin, {"instrument": instrument, "quantity": ZERO, "cost": ZERO,
                                                        "reference": ZERO, "last_trade_date": event["date"]})
                quantity, unit_price = Decimal(event["quantity"]), Decimal(event["price_eur"])
                commission = Decimal(event["commission_eur"])
                gross = (quantity * unit_price).quantize(CENT, rounding=ROUND_HALF_UP)
                position["last_trade_date"] = event["date"]
                anchor_date, anchor_price = anchors.get(isin, (None, None))
                if kind == "buy":
                    cost = gross + commission
                    cash -= cost
                    position["quantity"] += quantity
                    position["cost"] += cost
                    position["reference"] += (quantity * anchor_price if anchor_date and event["date"] <= anchor_date else cost)
                else:
                    if quantity > position["quantity"]:
                        raise md.DataError(422, f"Insufficient holdings on {event['date']} for {isin}; short positions are not supported.")
                    all_shares = quantity == position["quantity"]
                    cost = position["cost"] if all_shares else position["cost"] * quantity / position["quantity"]
                    reference = position["reference"] if all_shares else position["reference"] * quantity / position["quantity"]
                    cash += gross - commission
                    realized += gross - commission - cost
                    position["quantity"] -= quantity
                    position["cost"] -= cost
                    position["reference"] -= reference
            if cash < 0:
                raise md.DataError(422, f"Insufficient EUR cash on {event['date']}. Add a deposit before this transaction.")
        return cash, realized, positions


def valuation(db, document):
    with localcontext() as context:
        context.prec = 80
        cash, realized, positions = replay(document["transactions"])
        now, quotes, anchors = md.utcnow(), {}, {}
        for isin, position in positions.items():
            if not position["quantity"]:
                continue
            rows = quote_rows(db, position["instrument"], now)
            quotes[isin] = rows if currency_supported(db, position["instrument"], rows) else []
            if len(quotes[isin]) >= 2 and (previous := price(quotes[isin][-2].get("close"))) is not None:
                anchors[isin] = (quotes[isin][-2]["date"], previous)
        _, _, marked = replay(document["transactions"], anchors)
        items, warnings = [], []
        for isin, position in marked.items():
            quantity = position["quantity"]
            if not quantity:
                continue
            rows = quotes[isin]
            latest = rows[-1] if rows else {}
            market = price(latest.get("close"))
            value = quantity * market if market is not None else None
            unrealized = value - position["cost"] if value is not None else None
            daily = None
            warning = None
            if market is None:
                warning = "EUR valuation unavailable: latest saved raw close is missing, invalid, or currency is unconfirmed/conflicting."
            elif isin not in anchors:
                warning = "Daily unrealized PnL unavailable: two distinct valid EUR closing dates are required."
            elif not daily_interval(anchors[isin][0], latest["date"]):
                warning = "Daily unrealized PnL unavailable: saved closes span missing weekdays or non-trading dates."
            elif position["last_trade_date"] > latest["date"]:
                warning = "Daily unrealized PnL unavailable: a trade is newer than the latest saved close."
            else:
                daily = value - position["reference"]
            if latest.get("date") and latest["date"] < now.date().isoformat():
                stale = f"Uses saved close from {latest['date']}, not a live price."
                warning = f"{warning} {stale}" if warning else stale
            if warning:
                warnings.append(f"{isin}: {warning}")
            items.append({"instrument": position["instrument"], "quantity": decimal_string(quantity),
                          "average_price_eur": format(position["cost"] / quantity, ".8f"),
                          "cost_basis_eur": euros(position["cost"]), "market_price_eur": decimal_string(market) if market is not None else None,
                          "price_date": latest.get("date"), "previous_price_date": anchors.get(isin, (None,))[0],
                          "position_value_eur": euros(value), "unrealized_pnl_eur": euros(unrealized),
                          "daily_unrealized_pnl_eur": euros(daily), "warning": warning,
                          "_value": value, "_unrealized": unrealized, "_daily": daily})
        items.sort(key=lambda item: (item["instrument"]["name"], item["instrument"]["instrument_id"]))
        def total(key):
            return sum((item[key] for item in items), ZERO) if all(item[key] is not None for item in items) else None
        value, unrealized, daily = total("_value"), total("_unrealized"), total("_daily")
        periods = {(item["previous_price_date"], item["price_date"]) for item in items}
        if len(periods) > 1:
            daily = None
            warnings.append("Portfolio daily unrealized PnL unavailable: positions have different closing-date intervals. Update their market data.")
        period = next(iter(periods)) if len(periods) == 1 and daily is not None else (None, None)
        return {"portfolio": metadata(document), "cash_eur": euros(cash), "positions_value_eur": euros(value),
                "equity_eur": euros(cash + value) if value is not None else None, "realized_pnl_eur": euros(realized),
                "unrealized_pnl_eur": euros(unrealized), "daily_unrealized_pnl_eur": euros(daily),
                "daily_from_date": period[0], "daily_to_date": period[1],
                "positions": [event_public(item) for item in items], "warnings": warnings}


def customer_net_worth(db, owner, customer_ids):
    """Sum tracked portfolio equity for one customer page, never partial totals.

    Use the same rounded EUR equity shown on each portfolio. Goals and simulated
    capital are not assets. No portfolios means zero; any unpriced holding means
    the customer's total is unavailable, not just the sum of known assets.
    """
    if not customer_ids:
        return {}
    with localcontext() as context:
        context.prec = 80
        totals = dict.fromkeys(customer_ids, ZERO)
        documents = db.portfolios.find(
            {"user_id": owner, "customer_id": {"$in": customer_ids}},
            {field: 1 for field in (*META_FIELDS, "transactions")},
        )
        for document in documents:
            customer_id = document["customer_id"]
            if totals[customer_id] is None:
                continue
            equity = valuation(db, document)["equity_eur"]
            totals[customer_id] = totals[customer_id] + Decimal(equity) if equity is not None else None
        return {customer_id: euros(total) for customer_id, total in totals.items()}


@router.get("/portfolio-instruments")
def instruments(request: Request, user: CurrentUser, limit: Limit = 20, offset: Offset = 0):
    db, now = request.app.state.database, md.utcnow()
    items = []
    for isin in sorted(db.instruments.distinct("instrument_id")):
        try:
            items.append(acquired_instrument(db, isin, now))
        except md.DataError:
            continue
    return {"items": items[offset:offset + limit], "total": len(items), "limit": limit, "offset": offset}


@router.get(BASE)
def portfolios(customer_id: str, request: Request, user: CurrentUser, limit: Limit = 20, offset: Offset = 0):
    db, owner = request.app.state.database, str(user["_id"])
    _customer(db, owner, customer_id)
    return _page(db.portfolios, scope(owner, customer_id), {key: 1 for key in META_FIELDS}, CREATED_ORDER, metadata, limit, offset)


@router.post(BASE, status_code=201)
def create_portfolio(customer_id: str, body: PortfolioRequest, request: Request, user: CurrentUser):
    db, owner = request.app.state.database, str(user["_id"])
    _customer(db, owner, customer_id)
    if body.goal_id:
        _goal(db, owner, customer_id, body.goal_id)
    document = {"_id": str(ObjectId()), **scope(owner, customer_id), "name": body.name, "goal_id": body.goal_id,
                "currency": "EUR", "created_at": _timestamp(md.utcnow()), "revision": 0, "transactions": []}
    db.portfolios.insert_one(document)
    return metadata(document)


@router.get(BASE + "/{portfolio_id}")
def portfolio(customer_id: str, portfolio_id: str, request: Request, user: CurrentUser):
    db = request.app.state.database
    return valuation(db, require_portfolio(db, str(user["_id"]), customer_id, portfolio_id))


@router.patch(BASE + "/{portfolio_id}")
def update_portfolio(customer_id: str, portfolio_id: str, body: PortfolioRequest, request: Request, user: CurrentUser):
    from pymongo import ReturnDocument
    db, owner = request.app.state.database, str(user["_id"])
    require_portfolio(db, owner, customer_id, portfolio_id)
    if body.goal_id:
        _goal(db, owner, customer_id, body.goal_id)
    document = db.portfolios.find_one_and_update(scope(owner, customer_id, portfolio_id),
        {"$set": {"name": body.name, "goal_id": body.goal_id}, "$inc": {"revision": 1}}, return_document=ReturnDocument.AFTER)
    return metadata(document)


@router.get(BASE + "/{portfolio_id}/transactions")
def transactions(customer_id: str, portfolio_id: str, request: Request, user: CurrentUser, limit: Limit = 20, offset: Offset = 0):
    document = require_portfolio(request.app.state.database, str(user["_id"]), customer_id, portfolio_id)
    events = sorted(active_events(document["transactions"]), key=lambda item: (item["date"], item["sequence"]), reverse=True)
    return {"items": [event_public(event) for event in events[offset:offset + limit]], "total": len(events), "limit": limit, "offset": offset}


@router.delete(BASE + "/{portfolio_id}/transactions/{transaction_id}")
def remove_transaction(customer_id: str, portfolio_id: str, transaction_id: str, request: Request, user: CurrentUser):
    db, owner = request.app.state.database, str(user["_id"])
    for _ in range(8):
        document = require_portfolio(db, owner, customer_id, portfolio_id)
        events = document["transactions"]
        event = next((item for item in events if item["id"] == transaction_id), None)
        if event is None:
            raise md.DataError(404, "Transaction not found.")
        result = {"id": transaction_id, "removed": True}
        if event.get("_removed_at"):
            return result  # Safe to retry after a lost response; no extra revision.
        try:
            replay([item for item in events if item["id"] != transaction_id])
        except md.DataError as error:
            raise md.DataError(error.status, f"Cannot remove this transaction: {error} Remove dependent transactions first.") from error
        updated = db.portfolios.update_one(
            {**scope(owner, customer_id, portfolio_id), "revision": document["revision"], "transactions.id": transaction_id},
            {"$set": {"transactions.$._removed_at": _timestamp(md.utcnow())}, "$inc": {"revision": 1}},
        )
        if updated.modified_count:
            return result
    raise md.DataError(409, "Portfolio changed concurrently. Retry removing the transaction.")


@router.post(BASE + "/{portfolio_id}/transactions", status_code=201)
def add_transaction(customer_id: str, portfolio_id: str, body: TransactionRequest, request: Request, user: CurrentUser):
    db, owner, now = request.app.state.database, str(user["_id"]), md.utcnow()
    normalized = body.model_dump(mode="json")
    # Normalize numeric strings so equivalent retries have the same identity.
    for key in ("amount_eur", "quantity", "price_eur", "commission_eur"):
        if key in normalized:
            normalized[key] = format(Decimal(normalized[key]).normalize(), "f")
    with localcontext() as context:
        context.prec = 80
        for _ in range(8):
            document = require_portfolio(db, owner, customer_id, portfolio_id)
            events = document["transactions"]
            existing = next((event for event in events if event["client_id"] == normalized["client_id"]), None)
            if existing:
                if existing["_request"] != normalized:
                    raise md.DataError(409, "This transaction ID was already used with different inputs.")
                if existing.get("_removed_at"):
                    raise md.DataError(409, "This transaction was removed. Use a new transaction ID to record a new entry.")
                return event_public(existing)
            if len(events) >= MAX_TRANSACTIONS:
                raise md.DataError(409, f"This portfolio has reached the {MAX_TRANSACTIONS}-transaction limit.")
            event = {**normalized, "id": str(ObjectId()), "sequence": len(events) + 1,
                     "created_at": _timestamp(now), "_request": normalized}
            if isinstance(body, TradeRequest):
                gross = (body.quantity * body.price_eur).quantize(CENT, rounding=ROUND_HALF_UP)
                if gross <= 0 or gross > MAX_AMOUNT:
                    raise md.DataError(422, "Trade value must round to at least EUR 0.01 and be no more than EUR 1 trillion.")
                if body.type == "sell" and body.commission_eur > gross:
                    raise md.DataError(422, "Sell commission cannot exceed the sale proceeds.")
                prior = next((item["instrument"] for item in active_events(events) if item.get("instrument", {}).get("instrument_id") == body.instrument_id), None)
                if body.type == "sell":
                    if not prior:
                        raise md.DataError(422, "Insufficient holdings; short positions are not supported.")
                    # Selling a recorded EUR position must not depend on catalogue
                    # availability or the acquired universe's current listing.
                    instrument = prior
                else:
                    instrument = acquired_instrument(db, body.instrument_id, now)
                    if prior and any(prior[key] != instrument[key] for key in ("symbol", "exchange", "currency")):
                        raise md.DataError(422, "This portfolio already records a different listing for this instrument.")
                    instrument = prior or instrument
                event.pop("instrument_id")
                event["instrument"] = instrument
                event["commission_eur"] = euros(body.commission_eur)
            else:
                event["amount_eur"] = euros(body.amount_eur)
            replay([*events, event])
            result = db.portfolios.update_one({**scope(owner, customer_id, portfolio_id), "revision": document["revision"]},
                                               {"$push": {"transactions": event}, "$inc": {"revision": 1}})
            if result.modified_count:
                return event_public(event)
    raise md.DataError(409, "Portfolio changed concurrently. Retry with the same transaction ID.")
