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
from customer_planner import Name, Limit, Offset, CREATED_ORDER, _customer, _plan, _page, _timestamp
import market_data as md

router = APIRouter(tags=["portfolios"])
BASE = "/customers/{customer_id}/portfolios"
MAX_TRANSACTIONS = 5000  # Keeps the single-document atomic ledger below BSON's 16 MB limit.
MAX_AMOUNT = Decimal("1000000000000")
CENT = Decimal("0.01")
ZERO = Decimal(0)
Money = Annotated[Decimal, Field(ge=0, le=MAX_AMOUNT, decimal_places=2, allow_inf_nan=False)]
Units = Annotated[Decimal, Field(gt=0, le=MAX_AMOUNT, decimal_places=8, allow_inf_nan=False)]
META_FIELDS = ("customer_id", "name", "plan_id", "currency", "created_at", "revision")


class PortfolioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    plan_id: str | None = Field(default=None, min_length=1, max_length=100)


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
    return {"id": document["_id"], **{key: document.get(key) if key == "plan_id" else document[key] for key in META_FIELDS}}


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


def quote_rows(db, instrument, now, *, policy=None, carry_trailing=False):
    """Latest observation per distinct effective date, on the pinned listing only."""
    query = {**md.observation_filter(db, instrument["instrument_id"], instrument, now),
             "date": {"$lte": now.date().isoformat()}, "collected_at": {"$lte": now}}
    rows = db.quotes.aggregate([
        {"$match": query}, {"$sort": {"date": 1, "collected_at": -1, "_id": -1}},
        {"$group": {"_id": "$date", "data": {"$first": {
            "close": {"$ifNull": ["$data.close", None]},
            "price_currency": {"$ifNull": ["$data.price_currency", None]},
        }}}}, {"$sort": {"_id": 1}},
    ])
    from quote_fill import fill_rows, read_policy

    rows = [{**row["data"], "date": row["_id"]} for row in rows if md.quote_date(row["_id"]) == row["_id"]]
    state = db.instrument_state.find_one({"_id": instrument["instrument_id"]}) or {}
    listing = {key: instrument[key] for key in ("symbol", "exchange")}
    override = state.get("price_currency_override") if state.get("listing") == listing else None
    policy = policy or read_policy(db)
    rows = fill_rows(rows, instrument["instrument_id"], policy,
                     cutoff=now.date().isoformat(), currency_override=override)
    if carry_trailing and policy["method"] == "smooth":
        rows = carry_trailing_closes(db, instrument, rows)
    return rows


def carry_trailing_closes(db, instrument, rows):
    """Portfolio-only stale marks; do not feed carried prices to return fitting."""
    from quote_fill import missing

    start = len(rows)
    while start and missing(rows[start - 1].get("close")) and Date.fromisoformat(rows[start - 1]["date"]).weekday() < 5:
        start -= 1
    if start == 0 or start == len(rows):
        return rows
    source = rows[start - 1]
    if (price(source.get("close")) is None or Date.fromisoformat(source["date"]).weekday() >= 5
            or not currency_supported(db, instrument, rows[start - 1:])):
        return rows
    return [*rows[:start], *[{**row, "close": source["close"], "carried_from_date": source["date"]}
                            for row in rows[start:]]]


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
    if isin.startswith("idx-"):
        raise md.DataError(422, "Indices are reference data, not tradable portfolio instruments.")
    state = db.instrument_state.find_one({"_id": isin}) or {}
    listing = state.get("listing") or {}
    if not all(isinstance(listing.get(key), str) and listing[key].strip() and len(listing[key].encode("utf-8")) <= 200
               for key in ("symbol", "exchange")):
        raise md.DataError(422, "Acquire a EUR instrument listing before trading.")
    if not db.instruments.find_one(md.observation_filter(db, isin, listing, now)):
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


def daily_marks(rows):
    """Newest usable daily intervals, never treating carried marks as observations."""
    for previous, latest in reversed(list(zip(rows, rows[1:]))):
        if (daily_interval(previous["date"], latest["date"])
                and not previous.get("carried_from_date") and not latest.get("carried_from_date")
                and price(previous.get("close")) is not None and price(latest.get("close")) is not None):
            yield previous, latest


def replay(events, anchors=None):
    """Average cost; optional prior-close reference basis for remaining shares.

    Each same-day transaction follows its immutable insertion sequence. Backdated
    insertion or removal replays ALL active events and fails at the first negative balance.
    Commissions are realized expenses on the trade date, never part of cost or
    daily reference basis. Sell allocation uses the same average-cost proportion
    for the daily reference. A full liquidation removes the exact remaining basis.
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
                                                        "reference": ZERO})
                quantity, unit_price = Decimal(event["quantity"]), Decimal(event["price_eur"])
                commission = Decimal(event["commission_eur"])
                gross = (quantity * unit_price).quantize(CENT, rounding=ROUND_HALF_UP)
                anchor_date, anchor_price = anchors.get(isin, (None, None))
                realized -= commission
                if kind == "buy":
                    cash -= gross + commission
                    position["quantity"] += quantity
                    position["cost"] += gross
                    position["reference"] += (quantity * anchor_price if anchor_date and event["date"] <= anchor_date else gross)
                else:
                    if quantity > position["quantity"]:
                        raise md.DataError(422, f"Insufficient holdings on {event['date']} for {isin}; short positions are not supported.")
                    all_shares = quantity == position["quantity"]
                    cost = position["cost"] if all_shares else position["cost"] * quantity / position["quantity"]
                    reference = position["reference"] if all_shares else position["reference"] * quantity / position["quantity"]
                    cash += gross - commission
                    realized += gross - cost
                    position["quantity"] -= quantity
                    position["cost"] -= cost
                    position["reference"] -= reference
            if cash < 0:
                raise md.DataError(422, f"Insufficient EUR cash on {event['date']}. Add a deposit before this transaction.")
        return cash, realized, positions


def _cached_quote_rows(db, instrument, now, cache):
    key = tuple(instrument[field] for field in ("instrument_id", "symbol", "exchange"))
    if "quote_fill_policy" not in cache:
        from quote_fill import read_policy
        cache["quote_fill_policy"] = read_policy(db)
    if key not in cache:
        cache[key] = quote_rows(db, instrument, now, policy=cache["quote_fill_policy"], carry_trailing=True)
    return cache[key]


def valuation(db, document, *, now=None, quote_cache=None, exact_totals=False):
    """Public rounded totals by default; retain Decimal totals for aggregation."""
    with localcontext() as context:
        context.prec = 80
        cash, realized, positions = replay(document["transactions"])
        now, quotes, anchors, daily_quotes = now or md.utcnow(), {}, {}, {}
        quote_cache = {} if quote_cache is None else quote_cache
        for isin, position in positions.items():
            if not position["quantity"]:
                continue
            rows = _cached_quote_rows(db, position["instrument"], now, quote_cache)
            quotes[isin] = rows if currency_supported(db, position["instrument"], rows) else []
            pair = next(daily_marks(quotes[isin]), None)
            if pair:
                previous, latest = pair
                anchors[isin] = (previous["date"], price(previous["close"]))
                daily_quotes[isin] = latest
        if len(quotes) > 1:
            intervals = [{(previous["date"], latest["date"]): (previous, latest)
                          for previous, latest in daily_marks(rows)} for rows in quotes.values()]
            common = set.intersection(*(set(values) for values in intervals))
            if common:
                interval = max(common)
                for isin, values in zip(quotes, intervals):
                    previous, latest = values[interval]
                    anchors[isin] = (previous["date"], price(previous["close"]))
                    daily_quotes[isin] = latest
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
            elif latest.get("carried_from_date"):
                warning = (f"Stale valuation: using last valid close from {latest['carried_from_date']}; "
                           f"saved close missing through {latest['date']}.")
            elif isin not in anchors:
                warning = ("Daily unrealized PnL unavailable: saved closes span missing weekdays or non-trading dates."
                           if len(rows) >= 2 and not daily_interval(rows[-2]["date"], rows[-1]["date"])
                           else "Daily unrealized PnL unavailable: two distinct valid EUR closing dates are required.")
            if isin in daily_quotes:
                # Keep the current-ledger accounting, but use the selected historical close.
                daily = quantity * price(daily_quotes[isin]["close"]) - position["reference"]
            if warning:
                warnings.append(f"{isin}: {warning}")
            items.append({"instrument": position["instrument"], "quantity": decimal_string(quantity),
                          "average_price_eur": format(position["cost"] / quantity, ".8f"),
                          "cost_basis_eur": euros(position["cost"]), "market_price_eur": decimal_string(market) if market is not None else None,
                          "price_date": latest.get("carried_from_date", latest.get("date")),
                          "price_is_stale": bool(latest.get("carried_from_date")),
                          "latest_quote_date": latest.get("date"), "previous_price_date": anchors.get(isin, (rows[-2]["date"] if len(rows) >= 2 else None,))[0],
                          "daily_price_date": daily_quotes.get(isin, {}).get("date"),
                          "position_value_eur": euros(value), "unrealized_pnl_eur": euros(unrealized),
                          "daily_unrealized_pnl_eur": euros(daily), "warning": warning,
                          "daily_previous_value_eur": euros(quantity * price(daily_quotes[isin]["close"]) - daily) if daily is not None else None,
                          "_daily_value": quantity * price(daily_quotes[isin]["close"]) if daily is not None else None,
                          "_value": value, "_unrealized": unrealized, "_daily": daily, "_cost_basis": position["cost"]})
        items.sort(key=lambda item: (item["instrument"]["name"], item["instrument"]["instrument_id"]))
        def total(key):
            return sum((item[key] for item in items), ZERO) if all(item[key] is not None for item in items) else None
        value, unrealized, daily = total("_value"), total("_unrealized"), total("_daily")
        cost_basis = total("_cost_basis")
        periods = {(item["previous_price_date"], item["daily_price_date"]) for item in items}
        if len(periods) > 1:
            daily = None
            warnings.append("Portfolio daily unrealized PnL unavailable: positions have different closing-date intervals. Update their market data.")
        period = next(iter(periods)) if len(periods) == 1 and daily is not None else (None, None)
        amount = (lambda number: number) if exact_totals else euros
        return {"portfolio": metadata(document), "cash_eur": amount(cash), "positions_value_eur": amount(value),
                "equity_eur": amount(cash + value) if value is not None else None, "realized_pnl_eur": amount(realized),
                "unrealized_pnl_eur": amount(unrealized), "daily_unrealized_pnl_eur": amount(daily),
                "cost_basis_eur": amount(cost_basis),
                "previous_value_eur": amount(total("_daily_value") - daily) if daily is not None else None,
                "daily_from_date": period[0], "daily_to_date": period[1],
                "positions": [event_public(item) for item in items], "warnings": warnings}


def _daily_pnl(db, documents, now, quote_cache):
    """Current equity movement from a shared prior close, using latest saved marks.

    Include all trades after the prior close, even newer than the latest mark,
    with sales, round trips and fees, but without external cash flows. Select dates
    by searching backward for a usable common daily interval. Instruments
    liquidated before the interval need no marks and cannot poison the total.
    The caller supplies an 80-digit Decimal context and a request-local cache.
    """
    trades = [event for document in documents for event in active_events(document["transactions"])
              if event["type"] in ("buy", "sell")]
    if not trades:
        return {"today_pnl_eur": "0.00", "today_from_date": None, "today_to_date": None}
    unavailable = {"today_pnl_eur": None, "today_from_date": None, "today_to_date": None}
    instruments = {tuple(event["instrument"][field] for field in ("instrument_id", "symbol", "exchange")):
                   event["instrument"] for event in trades}
    rows = {key: _cached_quote_rows(db, instrument, now, quote_cache) for key, instrument in instruments.items()}
    last_trades = {key: max(event["date"] for event in trades if tuple(
        event["instrument"][field] for field in ("instrument_id", "symbol", "exchange")) == key) for key in instruments}
    open_keys = {tuple(position["instrument"][field] for field in ("instrument_id", "symbol", "exchange"))
                 for document in documents for position in replay(document["transactions"])[2].values() if position["quantity"]}
    open_dates = sorted({row["date"] for key in open_keys for row in rows[key]})
    # A long-liquidated listing's fresh (or malformed) quotes must not choose
    # the interval for unrelated holdings. Recent liquidations still participate.
    candidates = {key for key, values in rows.items() if key in open_keys or (
        (len(values) < 2 or last_trades[key] > values[-2]["date"])
        and (len(open_dates) < 2 or last_trades[key] > open_dates[-2]))}
    if not candidates:
        return {"today_pnl_eur": "0.00", "today_from_date": None, "today_to_date": None}
    dates = sorted({row["date"] for key in candidates for row in rows[key]})
    if len(dates) < 2:
        return unavailable
    pairs = {key: {(previous["date"], latest["date"]): (previous, latest)
                   for previous, latest in daily_marks(marks)} for key, marks in rows.items()}
    for start, end in reversed(list(zip(dates, dates[1:]))):
        if not daily_interval(start, end):
            continue
        result = _daily_pnl_interval(db, documents, rows, pairs, start, end)
        if result is not None:
            return result
    return unavailable


def _daily_pnl_interval(db, documents, rows, pairs, start, end):
    total, exposed = ZERO, False
    for document in documents:
        events = active_events(document["transactions"])
        _, _, before = replay([event for event in events if event["date"] <= start])
        _, _, after = replay(events)
        # `after` is the current ledger, not a snapshot at the saved close.
        # Include matching trade cash flows through the current ledger too;
        # omitting newer trades would turn their principal into apparent PnL.
        period_trades = [event for event in events if event["type"] in ("buy", "sell") and start < event["date"]]
        traded = {event["instrument"]["instrument_id"] for event in period_trades}
        for isin, position in after.items():
            opening = before.get(isin, {}).get("quantity", ZERO)
            closing = position["quantity"]
            if not opening and not closing and isin not in traded:
                continue
            exposed = True
            instrument = position["instrument"]
            key = tuple(instrument[field] for field in ("instrument_id", "symbol", "exchange"))
            marks = rows[key]
            pair = pairs[key].get((start, end))
            if pair is None or not currency_supported(db, instrument, marks):
                return None
            previous, latest = (price(row["close"]) for row in pair)
            total += closing * latest - opening * previous
        for event in period_trades:
            gross = (Decimal(event["quantity"]) * Decimal(event["price_eur"])).quantize(CENT, rounding=ROUND_HALF_UP)
            total += (gross if event["type"] == "sell" else -gross) - Decimal(event["commission_eur"])
    return {"today_pnl_eur": euros(total), "today_from_date": start if exposed else None,
            "today_to_date": end if exposed else None}


def customer_portfolio_summaries(db, owner, customer_ids):
    """All portfolios for one owner/customer page; exact totals, never partials.

    Net worth preserves the sum of displayed portfolio equities. PnL totals
    retain exact Decimal amounts until customer aggregation, then round once;
    rounded realized and unrealized displays may differ from total by a cent.
    Daily PnL uses a common close interval across all relevant positions.
    Goals and simulated capital are not assets; deposits are not profit.
    """
    if not customer_ids:
        return {}
    with localcontext() as context:
        context.prec = 80
        fields = {"net_worth_eur": "equity_eur", "realized_pnl_eur": "realized_pnl_eur",
                  "unrealized_pnl_eur": "unrealized_pnl_eur", "cost_basis_eur": "cost_basis_eur",
                  "previous_value_eur": "previous_value_eur"}
        totals = {customer_id: dict.fromkeys(fields, ZERO) for customer_id in customer_ids}
        grouped = {customer_id: [] for customer_id in customer_ids}
        now, quote_cache = md.utcnow(), {}
        documents = db.portfolios.find(
            {"user_id": owner, "customer_id": {"$in": customer_ids}},
            {field: 1 for field in (*META_FIELDS, "transactions")},
        )
        for document in documents:
            customer_id = document["customer_id"]
            grouped[customer_id].append(document)
            snapshot = valuation(db, document, now=now, quote_cache=quote_cache, exact_totals=True)
            for field, source in fields.items():
                amount = snapshot[source]
                if field == "net_worth_eur" and amount is not None:
                    amount = Decimal(euros(amount))
                current = totals[customer_id][field]
                totals[customer_id][field] = current + amount if current is not None and amount is not None else None
        result = {}
        for customer_id, amounts in totals.items():
            unrealized = amounts["unrealized_pnl_eur"]
            amounts["total_pnl_eur"] = amounts["realized_pnl_eur"] + unrealized if unrealized is not None else None
            result[customer_id] = {**{field: euros(amount) for field, amount in amounts.items()},
                                   **_daily_pnl(db, grouped[customer_id], now, quote_cache)}
        return result


def portfolio_summary(db, document, now, quote_cache):
    """List-only metrics with the same accounting as customer summaries."""
    with localcontext() as context:
        context.prec = 80
        snapshot = valuation(db, document, now=now, quote_cache=quote_cache, exact_totals=True)
        unrealized = snapshot["unrealized_pnl_eur"]
        total = snapshot["realized_pnl_eur"] + unrealized if unrealized is not None else None
        return {**metadata(document), "net_worth_eur": euros(snapshot["equity_eur"]),
                "realized_pnl_eur": euros(snapshot["realized_pnl_eur"]),
                "unrealized_pnl_eur": euros(unrealized), "total_pnl_eur": euros(total),
                "cost_basis_eur": euros(snapshot["cost_basis_eur"]),
                "previous_value_eur": euros(snapshot["previous_value_eur"]),
                **_daily_pnl(db, [document], now, quote_cache)}


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
    now, quote_cache = md.utcnow(), {}
    return _page(db.portfolios, scope(owner, customer_id), {key: 1 for key in (*META_FIELDS, "transactions")},
                 CREATED_ORDER, lambda document: portfolio_summary(db, document, now, quote_cache), limit, offset)


@router.post(BASE, status_code=201)
def create_portfolio(customer_id: str, body: PortfolioRequest, request: Request, user: CurrentUser):
    db, owner = request.app.state.database, str(user["_id"])
    _customer(db, owner, customer_id)
    if body.plan_id:
        _plan(db, owner, customer_id, body.plan_id)
    document = {"_id": str(ObjectId()), **scope(owner, customer_id), "name": body.name, "plan_id": body.plan_id,
                "currency": "EUR", "created_at": _timestamp(md.utcnow()), "revision": 0, "transactions": []}
    db.portfolios.insert_one(document)
    return metadata(document)


@router.get(BASE + "/{portfolio_id}")
def portfolio(customer_id: str, portfolio_id: str, request: Request, user: CurrentUser):
    db = request.app.state.database
    return valuation(db, require_portfolio(db, str(user["_id"]), customer_id, portfolio_id))


@router.get(BASE + "/{portfolio_id}/plan-tracking")
def portfolio_plan_tracking(customer_id: str, portfolio_id: str, request: Request, user: CurrentUser):
    from plan_tracking import track_plan

    db, owner = request.app.state.database, str(user["_id"])
    document = require_portfolio(db, owner, customer_id, portfolio_id)
    if not document.get("plan_id"):
        raise md.DataError(422, "Assign a saved plan to this portfolio to track its progress.")
    plan = _plan(db, owner, customer_id, document["plan_id"])
    now = md.utcnow()
    return track_plan(db, plan, valuation(db, document, now=now), now.date())


@router.patch(BASE + "/{portfolio_id}")
def update_portfolio(customer_id: str, portfolio_id: str, body: PortfolioRequest, request: Request, user: CurrentUser):
    from pymongo import ReturnDocument
    db, owner = request.app.state.database, str(user["_id"])
    require_portfolio(db, owner, customer_id, portfolio_id)
    if body.plan_id:
        _plan(db, owner, customer_id, body.plan_id)
    document = db.portfolios.find_one_and_update(scope(owner, customer_id, portfolio_id),
        {"$set": {"name": body.name, "plan_id": body.plan_id}, "$inc": {"revision": 1}}, return_document=ReturnDocument.AFTER)
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


def normalized_request(body):
    normalized = body.model_dump(mode="json")
    # normalize() uses the current context; ledger values have at most 21 digits.
    for key in ("amount_eur", "quantity", "price_eur", "commission_eur"):
        if key in normalized:
            normalized[key] = format(Decimal(normalized[key]).normalize(), "f")
    return normalized


def build_event(db, body, events, now, instrument_cache=None):
    """Shared manual/batch validation. Caller replays and atomically saves events."""
    normalized = normalized_request(body)
    event = {**normalized, "id": str(ObjectId()), "sequence": len(events) + 1,
             "created_at": _timestamp(now), "_request": normalized}
    if isinstance(body, TradeRequest):
        with localcontext() as context:
            context.prec = 80
            gross = (body.quantity * body.price_eur).quantize(CENT, rounding=ROUND_HALF_UP)
        if gross <= 0 or gross > MAX_AMOUNT:
            raise md.DataError(422, "Trade value must round to at least EUR 0.01 and be no more than EUR 1 trillion.")
        if body.type == "sell" and body.commission_eur > gross:
            raise md.DataError(422, "Sell commission cannot exceed the sale proceeds.")
        prior = next((item["instrument"] for item in active_events(events) if item.get("instrument", {}).get("instrument_id") == body.instrument_id), None)
        if body.type == "sell":
            if not prior:
                raise md.DataError(422, "Insufficient holdings; short positions are not supported.")
            instrument = prior
        else:
            cache = instrument_cache if instrument_cache is not None else {}
            if body.instrument_id not in cache:
                cache[body.instrument_id] = acquired_instrument(db, body.instrument_id, now)
            instrument = cache[body.instrument_id]
            if prior and any(prior[key] != instrument[key] for key in ("symbol", "exchange", "currency")):
                raise md.DataError(422, "This portfolio already records a different listing for this instrument.")
            instrument = prior or instrument
        event.pop("instrument_id")
        event["instrument"] = instrument
        event["commission_eur"] = euros(body.commission_eur)
    else:
        event["amount_eur"] = euros(body.amount_eur)
    return event


@router.post(BASE + "/{portfolio_id}/transactions", status_code=201)
def add_transaction(customer_id: str, portfolio_id: str, body: TransactionRequest, request: Request, user: CurrentUser):
    db, owner, now = request.app.state.database, str(user["_id"]), md.utcnow()
    normalized = normalized_request(body)
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
            event = build_event(db, body, events, now)
            replay([*events, event])
            result = db.portfolios.update_one({**scope(owner, customer_id, portfolio_id), "revision": document["revision"]},
                                               {"$push": {"transactions": event}, "$inc": {"revision": 1}})
            if result.modified_count:
                return event_public(event)
    raise md.DataError(409, "Portfolio changed concurrently. Retry with the same transaction ID.")
