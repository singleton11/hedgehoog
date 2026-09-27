"""Saved-price daily time-weighted returns for funded EUR portfolios.

Date-only cash flows use deposits at the start and withdrawals at the end of
that day's valuation period. Trades/fees are internal; chain the daily factors,
not PnL divided by net contributions. This is not an intraday/execution-time TWR.
"""

from bisect import bisect_right
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Annotated

from fastapi import APIRouter, Query, Request

from auth import CurrentUser
from customer_planner import _timestamp
from indices import require_index
import market_data as md
import portfolio_tracker as ledger

router = APIRouter(tags=["portfolios"])
BenchmarkId = Annotated[str | None, Query(pattern=r"^idx-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]
ONE = Decimal(1)
# Keep pathological compounded returns bounded and renderable by the browser.
MAX_GROWTH = Decimal("1e300")


class SavedMarks:
    """Last saved raw close on/before a date, including an invalid latest row.

    Never backfill from the future or skip an invalid endpoint. quote_rows pins
    the published generation and listing and bounds effective/knowledge times.
    """

    def __init__(self, rows, *, required_currency=None, currency_override=None):
        self.dates = [row["date"] for row in rows]
        self.values = []
        for row in rows:
            currency = row.get("price_currency")
            currency = currency.strip().upper() if isinstance(currency, str) and currency.strip() else None
            supported = required_currency is None or (
                currency_override in (None, required_currency)
                and (currency == required_currency or (currency is None and currency_override == required_currency)))
            self.values.append(ledger.price(row.get("close")) if supported else None)

    def at(self, day):
        index = bisect_right(self.dates, day) - 1
        return self.values[index] if index >= 0 else None


def percent(growth):
    if growth is None:
        return None
    result = format((growth - ONE) * 100, ".8f")
    return "0.00000000" if Decimal(result) == 0 else result


def benchmark_history(db, identifier, now):
    record = require_index(db, identifier)
    instrument = {"instrument_id": identifier, "symbol": record["symbol"], "exchange": "YAHOO"}
    rows = ledger.quote_rows(db, instrument, now)
    # Read only: index_detail also expires jobs, which is inappropriate here.
    profile = db.instruments.find_one({**md.observation_filter(db, identifier, instrument, now),
                                      "collected_at": {"$lte": now}}, sort=[("collected_at", -1), ("_id", -1)])
    currency = record.get("currency") or (profile or {}).get("data", {}).get("currency")
    return {"instrument_id": identifier, "name": record["name"], "symbol": record["symbol"],
            "currency": currency, "return_variant": record.get("return_variant", "unknown")}, SavedMarks(rows)


def performance(db, document, benchmark_id=None, *, now=None):
    now = now or md.utcnow()
    today = now.date().isoformat()
    events = sorted((event for event in ledger.active_events(document["transactions"]) if event["date"] <= today),
                    key=lambda item: (item["date"], item["sequence"]))
    benchmark, benchmark_marks = benchmark_history(db, benchmark_id, now) if benchmark_id else (None, None)
    result = {"portfolio_id": document["_id"], "currency": "EUR", "method": "daily_twr",
              "cash_flow_timing": "deposits_start_withdrawals_end", "as_of": _timestamp(now),
              "start_date": None, "end_date": None, "points": [], "benchmark": benchmark, "warnings": []}
    if not events:
        return result

    first = events[0]["date"]
    first_date = date.fromisoformat(first)
    baseline = (first_date - timedelta(days=1)).isoformat() if first_date > date.min else first
    instruments = {event["instrument"]["instrument_id"]: event["instrument"] for event in events if "instrument" in event}
    marks = {}
    dates = {first, today, *(event["date"] for event in events)}
    for isin, instrument in instruments.items():
        rows = ledger.quote_rows(db, instrument, now)
        state = db.instrument_state.find_one({"_id": isin}) or {}
        listing = {key: instrument[key] for key in ("symbol", "exchange")}
        override = state.get("price_currency_override") if state.get("listing") == listing else None
        # Confirm the observation actually used on each historical date. A later
        # metadata gap (especially after liquidation) must not erase earlier EUR
        # marks. Overrides fill absent currency only, on the pinned listing.
        marks[isin] = SavedMarks(rows, required_currency="EUR", currency_override=override)
        dates.update(day for day in marks[isin].dates if first <= day <= today)
    if benchmark_marks:
        dates.update(day for day in benchmark_marks.dates if first <= day <= today)
    result.update(start_date=baseline, end_date=today)
    with localcontext() as context:
        context.prec = 80
        benchmark_base = benchmark_marks.at(baseline) if benchmark_marks else None
        if benchmark_marks and benchmark_base is None:
            result["warnings"].append(
                f"Benchmark unavailable: a valid saved close on or before {baseline} is required. "
                "Download sufficient index history; the benchmark is not silently rebased to a later date.")
        if baseline < first:
            result["points"].append({"date": baseline, "twr_percent": percent(ONE),
                                     "benchmark_percent": percent(ONE) if benchmark_base is not None else None})
        cash, previous_equity, growth = ledger.ZERO, ledger.ZERO, ONE
        holdings = {}
        event_index = 0
        benchmark_gap = False
        for day in sorted(dates):
            deposits, withdrawals = ledger.ZERO, ledger.ZERO
            while event_index < len(events) and events[event_index]["date"] == day:
                event = events[event_index]
                event_index += 1
                kind = event["type"]
                if kind in ("deposit", "withdrawal"):
                    amount = Decimal(event["amount_eur"])
                    if kind == "deposit":
                        deposits += amount
                        cash += amount
                    else:
                        withdrawals += amount
                        cash -= amount
                else:
                    isin = event["instrument"]["instrument_id"]
                    quantity = Decimal(event["quantity"])
                    gross = (quantity * Decimal(event["price_eur"])).quantize(ledger.CENT, rounding=ROUND_HALF_UP)
                    direction = ONE if kind == "buy" else -ONE
                    cash -= direction * gross + Decimal(event["commission_eur"])
                    holdings[isin] = holdings.get(isin, ledger.ZERO) + direction * quantity
                    if not holdings[isin]:
                        del holdings[isin]

            equity = cash
            missing = []
            for isin, quantity in holdings.items():
                close = marks[isin].at(day)
                if close is None:
                    missing.append(isin)
                else:
                    equity += quantity * close
            if missing:
                equity = None
                if growth is not None:
                    result["warnings"].append(
                        f"TWR unavailable from {day}: missing, invalid or unconfirmed EUR saved close for {', '.join(missing)}. "
                        "The cumulative chain cannot resume until this history is repaired; no partial portfolio return is shown.")
                growth = None
            if growth is not None:
                denominator = previous_equity + deposits
                numerator = equity + withdrawals
                if denominator > 0:
                    growth *= numerator / denominator
                    if growth > MAX_GROWTH:
                        growth = None
                        result["warnings"].append(f"TWR unavailable from {day}: compounded return exceeds the supported display range.")
                elif numerator != 0:
                    growth = None
                    result["warnings"].append(f"TWR unavailable from {day}: no opening capital for a nonzero ending value.")
                # Zero capital and no exposure: preserve the chain across a full
                # withdrawal/dormant interval, then link the next funded period.
            previous_equity = equity
            benchmark_growth = None
            if benchmark_base is not None:
                close = benchmark_marks.at(day)
                if close is not None:
                    benchmark_growth = close / benchmark_base
                elif not benchmark_gap:
                    result["warnings"].append(f"Benchmark has an invalid saved close from {day}; unavailable observations are not replaced by older valid prices.")
                    benchmark_gap = True
            result["points"].append({"date": day, "twr_percent": percent(growth),
                                     "benchmark_percent": percent(benchmark_growth)})
    return result


@router.get(ledger.BASE + "/{portfolio_id}/performance")
def portfolio_performance(customer_id: str, portfolio_id: str, request: Request, user: CurrentUser,
                          benchmark_id: BenchmarkId = None):
    db = request.app.state.database
    document = ledger.require_portfolio(db, str(user["_id"]), customer_id, portfolio_id)
    return performance(db, document, benchmark_id)
