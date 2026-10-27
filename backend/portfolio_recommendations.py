"""Strategy-neutral, read-only allocation reviews and atomic ledger fulfillment.

Tokens are reproducible hashes of server state, not authorization credentials.
The bounded portfolio document retains receipts even when ledger events are removed.
"""
from datetime import date as Date, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, localcontext
from hashlib import sha256
import json
import re
from uuid import UUID, uuid5

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from auth import CurrentUser
from customer_planner import _plan
from goal_simulation import contribution_dates
from goal_strategies import get_strategy
import market_data as md
import portfolio_tracker as ledger

router = APIRouter(tags=["portfolios"])
BASE = ledger.BASE + "/{portfolio_id}/recommendations"
ZERO = Decimal(0)
UNIT = Decimal("0.00000001")
DEFAULT_POLICY = {"version": "rebalance-v1", "min_drift": "0.05", "min_trade_eur": "25",
                  "max_fee_ratio": "0.01", "cooldown_days": 30, "commission_eur": "0.00",
                  "max_price_age_days": 7}
NAMESPACE = UUID("b377ae0e-eeb4-4f0d-afb2-ff7679f91c01")


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def number(value, *, positive=False, places=None):
    if isinstance(value, bool):
        raise ValueError("Invalid decimal")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or result > ledger.MAX_AMOUNT or (positive and result <= 0):
        raise ValueError("Invalid amount")
    if places is not None and result.as_tuple().exponent < -places:
        raise ValueError("Invalid precision")
    return result


def funding_diagnostic(plan, events, today):
    """Use only immutable simulation schedule evidence, never a mutable goal."""
    saved, inputs = plan["result"], plan["inputs"]
    origin = Date.fromisoformat(saved["valuation_date"])
    end = Date.fromisoformat(inputs["goal_date"])
    if end <= origin or saved.get("goal_date", end.isoformat()) != end.isoformat():
        raise ValueError("Invalid saved horizon")
    try:
        schedule = contribution_dates(origin, end)
    except md.DataError as exc:
        raise ValueError("Invalid saved payment horizon") from exc
    dates = [Date.fromisoformat(day) for day in saved.get("payment_dates", [d.isoformat() for d in schedule])]
    # Existing simulations save anchored schedule endpoints/count instead of every date.
    if dates != schedule or saved.get("contribution_count") != len(dates):
        raise ValueError("Invalid saved payment schedule")
    if dates and (saved.get("first_contribution_date") != dates[0].isoformat()
                  or saved.get("last_contribution_date") != dates[-1].isoformat()):
        raise ValueError("Missing saved payment schedule evidence")
    initial = number(inputs["starting_capital_eur"], places=2)
    monthly = number(saved["monthly_contribution_eur"], places=2)
    count = sum(day <= today for day in dates)
    due = (initial if today >= origin else ZERO) + monthly * count
    funded = sum((Decimal(e["amount_eur"]) * (1 if e["type"] == "deposit" else -1)
                  for e in ledger.active_events(events) if e["date"] <= today.isoformat()
                  and e["type"] in ("deposit", "withdrawal")), ZERO)
    future = next((day for day in dates if day > today), None)
    return {"scheduled_due_eur": ledger.euros(due), "net_funded_eur": ledger.euros(funded),
            "shortfall_eur": ledger.euros(max(ZERO, due - funded)),
            "excess_eur": ledger.euros(max(ZERO, funded - due)), "due_count": count,
            "next_payment": {"date": future.isoformat(), "amount_eur": ledger.euros(monthly)} if future else None}, origin, end


def target_contract(plan, today, snapshot):
    selection = plan["inputs"]["strategy"]
    strategy = get_strategy(selection["id"])
    if strategy.recommendation_targets is None:
        raise ValueError("Recommendations unavailable for this strategy")
    parameters = strategy.parameters_model.model_validate(selection["parameters"])
    contract = strategy.recommendation_targets(parameters, today, snapshot)
    if not isinstance(contract.get("version"), str) or not contract["version"]:
        raise ValueError("Missing target contract version")
    targets, total = {}, ZERO
    for target in contract["targets"]:
        isin, weight = target["instrument_id"], number(target["weight"])
        if isin is not None and (not isinstance(isin, str) or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin)):
            raise ValueError("Invalid target instrument")
        if isin in targets:
            raise ValueError("Duplicate allocation target")
        targets[isin] = weight
        total += weight
    if not targets or abs(total - 1) > Decimal("0.00000001"):
        raise ValueError("Target weights must sum to one (tolerance 1e-8)")
    targets = {isin: weight / total for isin, weight in targets.items()}
    supported = contract.get("supported_actions")
    if not isinstance(supported, list) or not set(supported) <= {"deposit", "sell", "buy"}:
        raise ValueError("Invalid supported actions")
    policy = dict(DEFAULT_POLICY)
    constraints = contract.get("constraints", {})
    if set(constraints) - {"min_drift", "min_trade_eur", "max_fee_ratio", "cooldown_days"}:
        raise ValueError("Unsupported rebalance constraint")
    for key, value in constraints.items():
        if key == "cooldown_days":
            if type(value) is not int or not 0 <= value <= 36500:
                raise ValueError("Invalid cooldown")
            policy[key] = max(policy[key], value)
        else:
            amount = number(value)
            if key in ("min_drift", "max_fee_ratio") and amount > 1:
                raise ValueError("Invalid policy ratio")
            policy[key] = str(min(Decimal(policy[key]), amount) if key == "max_fee_ratio"
                              else max(Decimal(policy[key]), amount))
    return contract, targets, policy


def market_snapshot(db, isins, positions, now):
    """Raw observations only. Filled/carried marks never become trade defaults."""
    market = {}
    for isin in sorted(isins):
        item = {"instrument_id": isin, "eligible": False, "price_eur": None, "blockers": []}
        state = db.instrument_state.find_one({"_id": isin}) or {}
        item["state_token"] = digest(state)
        try:
            instrument = ledger.acquired_instrument(db, isin, now)
            pinned = positions.get(isin, {}).get("instrument")
            if pinned and any(pinned[key] != instrument[key] for key in ("symbol", "exchange", "currency")):
                raise md.DataError(422, "Pinned listing no longer matches acquired listing.")
            item.update(eligible=True, listing=instrument)
            query = {**md.observation_filter(db, isin, instrument, now),
                     "date": {"$lte": now.date().isoformat()}, "collected_at": {"$lte": now}}
            raw = db.quotes.find_one(query, sort=[("date", -1), ("collected_at", -1), ("_id", -1)])
            item["quote_token"] = digest(raw)
            data = (raw or {}).get("data", {})
            day = (raw or {}).get("date")
            price = ledger.price(data.get("close"))
            item.update(price_date=day, price_source="saved_raw_close")
            age = (now.date() - Date.fromisoformat(day)).days if day else 999999
            if (price is None or not 0 <= age <= DEFAULT_POLICY["max_price_age_days"]
                    or data.get("derived") or data.get("filled") or data.get("source") in ("derived", "filled", "carried")
                    or data.get("carried_from_date") or str(data.get("price_currency") or "").strip().upper() not in ("", "EUR")):
                item["blockers"].append("A valid recent raw EUR close or price override is required.")
            else:
                # Ledger prices have eight fractional places and an explicit maximum.
                number(price, positive=True, places=8)
                item["price_eur"] = str(price)
        except (md.DataError, ValueError, ArithmeticError) as exc:
            item["blockers"].append(str(exc))
        market[isin] = item
    return market


def balances(events):
    cash, _, holdings = ledger.replay(events)
    return {"cash_eur": ledger.euros(cash), "holdings": {isin: str(p["quantity"]) for isin, p in sorted(holdings.items())}}


def compute_actions(funding, weights, cash, positions, market, policy, today, estimated_fees=ZERO, sale_overrides=None):
    """Deterministic Decimal allocation; contributions/cash first, then sell-funded buys."""
    sale_overrides = sale_overrides or {}
    market = {isin: dict(mark) for isin, mark in market.items()}
    for isin, override in sale_overrides.items():
        if override.price_eur is not None:
            market[isin]["price_eur"] = str(override.price_eur)
    targets, actions, blockers = [], [], []
    gap = Decimal(funding["shortfall_eur"])
    if gap > 0:
        funding_blockers = (["Scheduled funding exceeds the per-deposit ledger limit."]
                            if gap > ledger.MAX_AMOUNT else [])
        actions.append({"id": "deposit", "type": "deposit", "amount_eur": str(gap), "dependencies": [],
                        "eligible": not funding_blockers, "rebalance": False, "blockers": funding_blockers})
    values = {}
    for isin, position in positions.items():
        if not position["quantity"]:
            continue
        mark = market[isin]
        if mark["price_eur"] is None or not mark["eligible"]:
            blockers.append("Incomplete valuation: " + isin)
        else:
            values[isin] = position["quantity"] * Decimal(mark["price_eur"])
    if blockers:
        return targets, actions, blockers, {"complete": False}
    equity = cash + sum(values.values(), ZERO)
    investable = max(ZERO, equity + gap - estimated_fees)
    all_isins = sorted(set(values) | {isin for isin in weights if isin is not None})
    drift = max([abs((values.get(isin, ZERO) / equity if equity else ZERO) - weights.get(isin, ZERO))
                 for isin in all_isins] + [abs((cash / equity if equity else ZERO) - weights.get(None, ZERO))]) if equity else ZERO
    for isin in [*all_isins, None]:
        actual = cash if isin is None else values.get(isin, ZERO)
        weight = weights.get(isin, ZERO)
        targets.append({"instrument_id": isin, "weight": str(weight), "actual_eur": ledger.euros(actual),
                        "price_eur": market[isin]["price_eur"] if isin is not None else None,
                        "target_eur": ledger.euros(investable * weight),
                        "deviation": str((actual / equity if equity else ZERO) - weight)})
    needed = {isin: max(ZERO, investable * weights.get(isin, ZERO) - values.get(isin, ZERO)) for isin in all_isins}
    cash_budget = max(ZERO, cash + gap - investable * weights.get(None, ZERO) - estimated_fees)
    total_need = sum(needed.values(), ZERO)
    first_buys = {isin: min(need, cash_budget * need / total_need) if total_need else ZERO for isin, need in needed.items()}
    sells = {isin: max(ZERO, values.get(isin, ZERO) - investable * weights.get(isin, ZERO)) for isin in all_isins}
    residual = {isin: max(ZERO, needed[isin] - first_buys[isin]) for isin in all_isins}
    rebalance = any(value >= ledger.CENT for value in sells.values())
    post_cash_values = {isin: values.get(isin, ZERO) + first_buys[isin] for isin in all_isins}
    post_cash = max(ZERO, cash + gap - sum(first_buys.values(), ZERO) - estimated_fees)
    remaining_drift = max([abs((post_cash_values[isin] / investable if investable else ZERO)
                              - weights.get(isin, ZERO)) for isin in all_isins]
                          + [abs((post_cash / investable if investable else ZERO) - weights.get(None, ZERO))])
    suppress = []
    if remaining_drift < Decimal(policy["min_drift"]):
        suppress.append("Allocation drift is below the rebalance threshold.")
    if policy.get("next_eligible_date") and today.isoformat() < policy["next_eligible_date"]:
        suppress.append("Rebalance cooldown is active.")

    def trade(kind, isin, amount, rebal, dependencies, suffix):
        amount = amount.quantize(ledger.CENT, rounding=ROUND_DOWN)
        if amount < Decimal("0.01"):
            return
        mark = market[isin]
        reasons = list(mark["blockers"])
        if rebal:
            reasons += suppress
            if amount < Decimal(policy["min_trade_eur"]):
                reasons.append("Rebalance trade is below the minimum gross.")
        eligible = mark["eligible"] and not (rebal and (suppress or amount < Decimal(policy["min_trade_eur"])))
        quantity = (amount / Decimal(mark["price_eur"])).quantize(UNIT, rounding=ROUND_DOWN) if mark["price_eur"] else None
        if kind == "sell" and quantity is not None:
            quantity = min(quantity, positions[isin]["quantity"])
            override = sale_overrides.get(isin)
            if override is not None and override.quantity is not None:
                quantity = override.quantity
        reviewable = eligible
        if quantity == 0:
            reasons.append("Trade rounds to zero units. Supply a valid execution price override.")
            eligible = False
        actions.append({"id": f"{kind}:{isin}:{suffix}", "type": kind, "instrument_id": isin,
                        "amount_eur": format(amount.quantize(ledger.CENT, rounding=ROUND_DOWN), ".2f"),
                        "quantity": str(quantity) if quantity is not None else None,
                        "price_eur": mark["price_eur"], "commission_eur": "0.00", "price_date": mark.get("price_date"),
                        "price_source": mark.get("price_source"), "listing": mark.get("listing"),
                        "dependencies": dependencies, "rebalance": rebal, "eligible": eligible,
                        "reviewable": reviewable, "blockers": reasons})
    for isin, amount in sells.items():
        if rebalance:
            trade("sell", isin, amount, True, [], "rebalance")
    sale_ids = [a["id"] for a in actions if a["type"] == "sell"]
    # Dependent buys can spend only executable, cent-settled sale proceeds.
    # Reserve fees and the cash target before distributing the remaining budget.
    sale_proceeds = sum(((Decimal(a["quantity"]) * Decimal(a["price_eur"])).quantize(
        ledger.CENT, rounding=ROUND_HALF_UP) for a in actions if a["type"] == "sell"), ZERO)
    sale_budget = max(ZERO, cash + gap + sale_proceeds - estimated_fees
                      - investable * weights.get(None, ZERO) - sum(first_buys.values(), ZERO))
    total_residual = sum(residual.values(), ZERO)
    if total_residual > sale_budget:
        residual = {isin: amount * sale_budget / total_residual for isin, amount in residual.items()}
    for isin in all_isins:
        # Cash/contribution buys remain independently eligible during cooldown.
        deps = ["deposit"] if gap else []
        trade("buy", isin, first_buys[isin], False, deps, "cash")
        if rebalance:
            trade("buy", isin, residual[isin], True, sale_ids + deps, "rebalance")
    # A rebalance is indivisible: no sell-only selection can evade turnover/fee checks.
    group = [a["id"] for a in actions if a["rebalance"] or a["type"] == "buy"]
    for action in actions:
        if action["rebalance"]:
            action["group"] = group
    # Preserve deterministic dependency order regardless of target iteration order.
    actions.sort(key=lambda a: ({"deposit": 0, "sell": 1, "buy": 2}[a["type"]], a["id"]))
    return targets, actions, [], {"complete": True, "equity_eur": ledger.euros(equity), "max_deviation": str(drift)}


def recommendation_snapshot(db, owner, customer_id, document, now):
    with localcontext() as context:
        context.prec = 80
        return _recommendation_snapshot(db, owner, customer_id, document, now)


def _recommendation_snapshot(db, owner, customer_id, document, now, estimated_fees=ZERO, sale_overrides=None):
    day = now.date()
    result = {"plan_id": document.get("plan_id"), "revision": document["revision"], "date": day.isoformat(),
              "status": "blocked", "funding": None, "targets": [], "actions": [], "blockers": [],
              "policy": dict(DEFAULT_POLICY), "allocation": {"complete": False},
              "forecast": {"status": "available_separately", "endpoint": f"/customers/{customer_id}/portfolios/{document['_id']}/plan-tracking"}}
    plan, market, contract = None, {}, None
    events = [e for e in document["transactions"] if e["date"] <= day.isoformat()]
    try:
        if not document.get("plan_id"):
            raise ValueError("Assign a saved plan to receive recommendations.")
        plan = _plan(db, owner, customer_id, document["plan_id"])
        funding, origin, end = funding_diagnostic(plan, events, day)
        result["funding"] = funding
        if day < origin or day >= end:
            raise ValueError("Evaluation is outside the saved plan's active horizon.")
        cash, _, positions = ledger.replay(events)
        contract, weights, policy = target_contract(plan, day, {"cash_eur": str(cash), "positions": positions})
        last = document.get("recommendation_last_rebalance")
        policy["last_rebalance_date"] = last.get("date") if last else None
        policy["next_eligible_date"] = (Date.fromisoformat(last["date"]) + timedelta(days=policy["cooldown_days"])).isoformat() if last else None
        market = market_snapshot(db, set(positions) | {isin for isin in weights if isin is not None}, positions, now)
        targets, actions, blockers, allocation = compute_actions(
            funding, weights, cash, positions, market, policy, day, estimated_fees, sale_overrides)
        for action in actions:
            if action["type"] not in contract["supported_actions"]:
                action["eligible"] = False
                action["reviewable"] = False
                action["blockers"].append("Action is unsupported by the strategy.")
        # Only independently valid groups may be fulfilled by a default all selection.
        by_id = {a["id"]: a for a in actions}
        reviewable_ids = {a["id"] for a in actions if a.get("reviewable", a["eligible"])}
        for action in actions:
            required = action.get("group", []) + action["dependencies"]
            action["reviewable"] = action["id"] in reviewable_ids and all(key in reviewable_ids for key in required)
            if any(not by_id[key]["eligible"] for key in required):
                action["eligible"] = False
                reasons = [reason for key in required for reason in by_id[key]["blockers"]]
                action["blockers"] = list(dict.fromkeys([*action["blockers"], "A dependent action is blocked.", *reasons]))
        result.update(targets=targets, actions=actions, blockers=blockers, policy=policy, allocation=allocation,
                      status="ready" if any(a["eligible"] for a in actions) else "blocked" if blockers or actions else "aligned")
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        result["status"] = "unsupported"
        result["blockers"].append(str(exc) or "Malformed saved plan or target contract.")
    result["snapshot_token"] = digest({"version": 1, "owner": owner, "customer": customer_id,
        "portfolio": document["_id"], "plan": plan, "market": market, "contract": contract, "result": result})
    return result


def projected_allocation(balance, targets, selection):
    """Project at saved marks, using an execution override only for an unpriced target."""
    cash = Decimal(balance["cash_eur"])
    weights = {t["instrument_id"]: Decimal(t["weight"]) for t in targets}
    prices = {t["instrument_id"]: t.get("price_eur") for t in targets}
    for action in selection:
        if action["type"] in ("buy", "sell") and not prices.get(action["instrument_id"]):
            prices[action["instrument_id"]] = action["price_eur"]
    if any(Decimal(quantity) and not prices.get(isin) for isin, quantity in balance["holdings"].items()):
        return {**balance, "complete": False, "equity_eur": None, "targets": [],
                "max_deviation": None, "residual_cash_eur": None}
    values = {isin: Decimal(quantity) * Decimal(prices[isin]) for isin, quantity in balance["holdings"].items()
              if Decimal(quantity) and prices.get(isin)}
    equity = cash + sum(values.values(), ZERO)
    rows = [{"instrument_id": isin, "actual_eur": ledger.euros(cash if isin is None else values.get(isin, ZERO)),
             "target_eur": ledger.euros(equity * weight), "weight": str(weight),
             "deviation": str(((cash if isin is None else values.get(isin, ZERO)) / equity if equity else ZERO) - weight)}
            for isin, weight in weights.items()]
    return {**balance, "complete": True, "equity_eur": ledger.euros(equity), "targets": rows,
            "max_deviation": str(max((abs(Decimal(t["deviation"])) for t in rows), default=ZERO)),
            "residual_cash_eur": ledger.euros(cash - equity * weights.get(None, ZERO))}


class Override(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price_eur: ledger.Units | None = None
    commission_eur: ledger.Money | None = None
    quantity: ledger.Units | None = None


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_token: str = Field(pattern=r"^[a-f0-9]{64}$")
    action_ids: list[str] = Field(min_length=1, max_length=5000)
    date: Date
    overrides: dict[str, Override] = Field(default_factory=dict, max_length=5000)

    @field_validator("action_ids")
    @classmethod
    def unique_ids(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("Select each action only once.")
        return sorted(values)

    @field_validator("date", mode="before")
    @classmethod
    def iso_date(cls, value):
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError("Use a YYYY-MM-DD date.")
        return value


class FulfillRequest(PreviewRequest):
    operation_id: UUID
    preview_token: str = Field(pattern=r"^[a-f0-9]{64}$")


def normalized_review(body):
    return {"snapshot_token": body.snapshot_token, "action_ids": body.action_ids, "date": body.date.isoformat(),
            "overrides": {key: {field: str(value.normalize()) for field, value in override
                                if value is not None} for key, override in sorted(body.overrides.items())}}


def prepare_preview(db, owner, customer_id, document, body, now):
    with localcontext() as context:
        context.prec = 80
        return _prepare_preview(db, owner, customer_id, document, body, now)


def _prepare_preview(db, owner, customer_id, document, body, now):
    snapshot = recommendation_snapshot(db, owner, customer_id, document, now)
    if body.date != now.date() or snapshot["snapshot_token"] != body.snapshot_token:
        raise md.DataError(409, "Recommendations changed. Refresh and review again.")
    original = {a["id"]: a for a in snapshot["actions"]}
    selected = set(body.action_ids)
    if selected - original.keys() or body.overrides.keys() - selected:
        raise md.DataError(422, "Unknown selection or override.")
    for action_id in selected:
        action = original[action_id]
        if set(action["dependencies"] + action.get("group", [])) - selected:
            raise md.DataError(422, "Select every prerequisite and all trades in the rebalance group.")
    estimated_fees = sum((override.commission_eur or ZERO for override in body.overrides.values()), ZERO)
    sale_overrides = {a["instrument_id"]: body.overrides[a["id"]] for a in snapshot["actions"]
                      if a["type"] == "sell" and a["id"] in selected and a["id"] in body.overrides}
    if estimated_fees or sale_overrides:
        # Revalue edited sales before sizing the group, so holdings bound sales
        # and cent-settled effective proceeds fund the dependent purchases.
        adjusted = _recommendation_snapshot(db, owner, customer_id, document, now, estimated_fees, sale_overrides)
        snapshot["actions"] = adjusted["actions"]
        saved_prices = {t["instrument_id"]: t["price_eur"] for t in snapshot["targets"]}
        snapshot["targets"] = [{**t, "price_eur": saved_prices[t["instrument_id"]]} for t in adjusted["targets"]]
    by_id = {a["id"]: a for a in snapshot["actions"]}
    omitted = sorted(selected - by_id.keys())
    resized_group = {key for a in snapshot["actions"] if a["rebalance"] and a["id"] in selected
                     for key in original[a["id"]].get("group", [])}
    for action_id in omitted:
        action = original[action_id]
        override = body.overrides.get(action_id, Override())
        # Fee sizing may consume a cash leg, but cannot silently discard an
        # explicit quantity or charge a commission for a trade that will not run.
        if (action_id not in resized_group or action["type"] != "buy" or action["rebalance"]
                or not action["eligible"] or override.quantity is not None or override.commission_eur):
            raise md.DataError(422, "Selected trade has no remaining budget after adjustments.")
    selection = [dict(a) for a in snapshot["actions"] if a["id"] in selected]
    if not selection:
        raise md.DataError(422, "No executable actions remain after adjustments.")
    for action in selection:
        if not action["eligible"] and not action.get("reviewable"):
            raise md.DataError(422, "Selected action is blocked: " + "; ".join(action["blockers"]))
        required = set(action["dependencies"] + action.get("group", []))
        if required - selected:
            raise md.DataError(422, "Select every prerequisite and all trades in the rebalance group.")
    entries, events, turnover, fees = [], list(document["transactions"]), ZERO, ZERO
    # Cash buys can be mandatory members of a rebalance group. Include every
    # grouped trade's commission and notional so its fee cannot bypass the cap.
    rebalance_ids = {key for action in selection if action["rebalance"] for key in action.get("group", [action["id"]])}
    before = balances([e for e in events if e["date"] <= body.date.isoformat()])
    cache = {}
    for action in selection:
        action_id = action["id"]
        override = body.overrides.get(action_id, Override())
        client_id = uuid5(NAMESPACE, body.snapshot_token + ":" + action_id)
        common = {"client_id": client_id, "date": body.date.isoformat(), "type": action["type"]}
        if action["type"] == "deposit":
            if action_id in body.overrides:
                raise md.DataError(422, "Funding amounts are server generated and cannot be overridden.")
            try:
                transaction = ledger.CashRequest(**common, amount_eur=action["amount_eur"])
            except ValidationError as exc:
                raise md.DataError(422, "Computed funding amount violates the deposit ledger limits.") from exc
        else:
            price = override.price_eur if override.price_eur is not None else (Decimal(action["price_eur"]) if action["price_eur"] else None)
            if price is None:
                raise md.DataError(422, "Supply a valid execution price override.")
            fee = override.commission_eur if override.commission_eur is not None else ZERO
            budget = Decimal(action["amount_eur"])
            if budget <= 0:
                raise md.DataError(422, "Fees exceed the available trade budget.")
            quantity = override.quantity if override.quantity is not None else (
                Decimal(action["quantity"]) if action["type"] == "sell"
                else (budget / price).quantize(UNIT, rounding=ROUND_DOWN))
            if quantity <= 0:
                raise md.DataError(422, "Trade rounds to zero units.")
            if quantity > ledger.MAX_AMOUNT:
                raise md.DataError(422, "Computed quantity exceeds the ledger unit limit.")
            gross = (quantity * price).quantize(ledger.CENT, rounding=ROUND_HALF_UP)
            # Quantity edits may reduce, never enlarge, the reviewed server allocation.
            if gross > budget + Decimal("0.005"):
                raise md.DataError(422, "Quantity exceeds the recommended allocation budget.")
            if action["rebalance"]:
                if gross < Decimal(snapshot["policy"]["min_trade_eur"]):
                    raise md.DataError(422, "Rebalance trade is below the minimum gross.")
            if action_id in rebalance_ids:
                turnover += gross
                fees += fee
            transaction = ledger.TradeRequest(**common, instrument_id=action["instrument_id"],
                                               quantity=quantity, price_eur=price, commission_eur=fee)
            action.update(quantity=str(quantity), price_eur=str(price), commission_eur=ledger.euros(fee), eligible=True,
                          blockers=[], amount_eur=ledger.euros(gross),
                          price_source="override" if override.price_eur is not None else action["price_source"])
        event = ledger.build_event(db, transaction, events, now, cache)
        event["recommendation"] = {"action_id": action_id, "plan_id": snapshot["plan_id"],
                                    "snapshot_token": body.snapshot_token, "price_source": action.get("price_source"),
                                    "price_date": action.get("price_date")}
        events.append(event)
        entries.append(event)
    if turnover and fees > turnover * Decimal(snapshot["policy"]["max_fee_ratio"]):
        raise md.DataError(422, "Rebalance commissions exceed the turnover cost limit.")
    if len(events) > ledger.MAX_TRANSACTIONS:
        raise md.DataError(409, "Portfolio lifetime transaction limit reached.")
    ledger.replay(events)  # Includes future history, not only today's projected balances.
    public_entries = [ledger.event_public(e) for e in entries]
    # Random generated event IDs/timestamps are not reviewed financial inputs.
    normalized_entries = [{k: v for k, v in e.items() if k not in ("id", "created_at")} for e in public_entries]
    after = balances([e for e in events if e["date"] <= body.date.isoformat()])
    projected = projected_allocation(after, snapshot["targets"], selection)
    if rebalance_ids:
        # Check edited trades against the cash/contribution-only alternative.
        ordinary_ids = {a["id"] for a in selection if not a["rebalance"]}
        baseline_events = [*document["transactions"], *[e for e in entries
            if e["recommendation"]["action_id"] in ordinary_ids]]
        baseline = projected_allocation(balances([e for e in baseline_events
            if e["date"] <= body.date.isoformat()]), snapshot["targets"], selection)
        if Decimal(projected["max_deviation"]) >= Decimal(baseline["max_deviation"]):
            raise md.DataError(422, "Rebalance must improve allocation over cash and contribution purchases alone.")
    review = normalized_review(body)
    result = {**review, "entries": public_entries,
              "before": projected_allocation(before, snapshot["targets"], selection),
              "after": projected,
              "selection": selection, "omitted_action_ids": omitted, "policy": snapshot["policy"],
              "total_commission_eur": ledger.euros(sum((Decimal(e.get("commission_eur", "0")) for e in entries), ZERO)),
              "residual_eur": after["cash_eur"]}
    result["preview_token"] = digest({"review": review, "entries": normalized_entries, "before": before,
                                      "after": after, "policy": snapshot["policy"]})
    return result, entries


@router.get(BASE)
def recommendations(customer_id: str, portfolio_id: str, request: Request, user: CurrentUser):
    db, owner = request.app.state.database, str(user["_id"])
    document = ledger.require_portfolio(db, owner, customer_id, portfolio_id)
    with localcontext() as context:
        context.prec = 80
        return recommendation_snapshot(db, owner, customer_id, document, md.utcnow())


@router.post(BASE + "/preview")
def preview(customer_id: str, portfolio_id: str, body: PreviewRequest, request: Request, user: CurrentUser):
    db, owner = request.app.state.database, str(user["_id"])
    document = ledger.require_portfolio(db, owner, customer_id, portfolio_id)
    return prepare_preview(db, owner, customer_id, document, body, md.utcnow())[0]


@router.post(BASE + "/fulfill")
def fulfill(customer_id: str, portfolio_id: str, body: FulfillRequest, request: Request, user: CurrentUser):
    db, owner, now = request.app.state.database, str(user["_id"]), md.utcnow()
    operation_id = str(body.operation_id)
    fingerprint = digest({**normalized_review(body), "preview_token": body.preview_token})
    document = ledger.require_portfolio(db, owner, customer_id, portfolio_id)

    def prior(doc):
        receipt = next((r for r in doc.get("recommendation_receipts", []) if r["operation_id"] == operation_id), None)
        if receipt:
            if receipt["fingerprint"] != fingerprint:
                raise md.DataError(409, "Operation ID was already used with different reviewed inputs.")
            return receipt["response"]
        return None

    previous = prior(document)
    if previous is not None:
        return previous  # Must precede date, plan, revision, quote and policy checks.
    try:
        reviewed, entries = prepare_preview(db, owner, customer_id, document, body, now)
    except md.DataError:
        # A concurrent writer may commit before stale-state or ledger validation fails.
        previous = prior(ledger.require_portfolio(db, owner, customer_id, portfolio_id))
        if previous is not None:
            return previous
        raise
    if body.preview_token != reviewed["preview_token"]:
        previous = prior(ledger.require_portfolio(db, owner, customer_id, portfolio_id))
        if previous is not None:
            return previous
        raise md.DataError(409, "Preview changed. Review again before confirming.")
    for event in entries:
        event["id"] = str(uuid5(body.operation_id, event["recommendation"]["action_id"]))
        event["client_id"] = event["id"]
        event["_request"]["client_id"] = event["id"]
        event["recommendation"]["operation_id"] = operation_id
    response = {"operation_id": operation_id, "plan_id": document["plan_id"], "date": body.date.isoformat(),
                "revision": document["revision"] + 1, "event_ids": [e["id"] for e in entries],
                "entries": [ledger.event_public(e) for e in entries]}
    receipt = {"operation_id": operation_id, "fingerprint": fingerprint, "response": response,
               "snapshot_token": body.snapshot_token, "action_ids": body.action_ids, "policy": reviewed["policy"]}
    update = {"$push": {"transactions": {"$each": entries}, "recommendation_receipts": receipt},
              "$inc": {"revision": 1}}
    if any(a["rebalance"] for a in reviewed["selection"]):
        update["$set"] = {"recommendation_last_rebalance": {"date": body.date.isoformat(),
                           "policy": reviewed["policy"], "operation_id": operation_id}}
    ledger.ensure_append_capacity(document, entries, receipt=receipt, fields=update.get("$set"))
    # Re-read the entire market/plan identity immediately before the document CAS.
    current = recommendation_snapshot(db, owner, customer_id, document, md.utcnow())
    if current["snapshot_token"] != body.snapshot_token:
        previous = prior(ledger.require_portfolio(db, owner, customer_id, portfolio_id))
        if previous is not None:
            return previous
        raise md.DataError(409, "Market, date, plan or policy changed. Review again.")
    changed = db.portfolios.update_one({**ledger.scope(owner, customer_id, portfolio_id),
                                       "revision": document["revision"], "plan_id": document["plan_id"],
                                       "recommendation_receipts.operation_id": {"$ne": operation_id}}, update)
    if changed.modified_count:
        return response
    previous = prior(ledger.require_portfolio(db, owner, customer_id, portfolio_id))
    if previous is not None:
        return previous
    raise md.DataError(409, "Portfolio changed concurrently. Refresh and review again.")
