"""Conservative DEGIRO Account.csv -> existing EUR funded ledger.

Preview is read-only. Commit reparses the CSV, compares the reviewed plan and
appends the entire batch with the portfolio revision CAS. No raw file is stored.
Unknown/ambiguous rows block the whole batch rather than silently losing money.
"""

import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP, localcontext
from hashlib import sha256
from io import StringIO
import json
import re
from uuid import UUID, uuid5

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from auth import CurrentUser
import market_data as md
import portfolio_tracker as ledger

router = APIRouter(tags=["portfolios"])
BASE = ledger.BASE + "/{portfolio_id}/imports/degiro"
MAX_BYTES = 1_000_000
MAX_ROWS = 10_000
NAMESPACE = UUID("a5a90a0d-f6f8-4dbc-bda3-57bc5a023ca9")
HEADER = ["Date", "Time", "Value date", "Product", "ISIN", "Description", "FX", "Change", "", "Balance", "", "Order Id"]
NUMBER = r"[+-]?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{1,8})?"
TRADE = re.compile(rf"(Koop|Verkoop|Buy|Sell) ({NUMBER}) @ ({NUMBER}) ([A-Z]{{3}})", re.I)
FEES = {"degiro transactiekosten en/of kosten van derden", "degiro transaction and/or third party fees"}
DEPOSITS = {"ideal deposit", "deposit", "storting"}
WITHDRAWALS = {"withdrawal", "terugstorting"}
TRANSFER = re.compile(rf"(?:Overboeking (?P<nl>van|naar) uw geldrekening bij|Transfer (?P<en>from|to) your cash account at) flatexDEGIRO Bank (?P<amount>{NUMBER}) EUR", re.I)


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    csv: str = Field(min_length=1, max_length=MAX_BYTES)

    @field_validator("csv", mode="before")
    @classmethod
    def bounded_utf8(cls, value):
        # Do not echo private CSV contents (or invalid Unicode) through the
        # framework's default validation-error input serialization.
        if not isinstance(value, str):
            raise md.DataError(422, "CSV must be UTF-8 text.")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise md.DataError(422, "Use valid UTF-8 text.") from exc
        if not 0 < size <= MAX_BYTES:
            raise md.DataError(422, "CSV must contain 1 to 1,000,000 UTF-8 bytes.")
        return value


class ImportRequest(PreviewRequest):
    preview_token: str = Field(pattern=r"^[a-f0-9]{64}$")


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def number(value, places=2):
    if not re.fullmatch(NUMBER, value) or len(value) > 40:
        raise ValueError("Expected a European-format decimal number (decimal comma).")
    result = Decimal(value.replace(".", "").replace(",", "."))
    if abs(result) > ledger.MAX_AMOUNT or result.as_tuple().exponent < -places:
        raise ValueError(f"Amount exceeds the ledger limit or {places} decimal places.")
    return result


@dataclass
class Row:
    line: int
    cells: list[str]
    stamp: datetime
    value_date: str
    amount: Decimal | None

    @property
    def description(self):
        return self.cells[5]

    @property
    def order(self):
        return self.cells[11]


def parse_statement(text):
    errors, ignored, candidates = [], [], []

    def error(row, message):
        errors.append({"row": row.line if isinstance(row, Row) else row, "message": message})

    def ignore(row, reason):
        ignored.append({"row": row.line, "description": row.description, "reason": reason})

    try:
        byte_length = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        error(None, "Use valid UTF-8 text.")
        return candidates, ignored, errors
    if byte_length > MAX_BYTES:
        error(None, "The CSV exceeds the 1 MB upload limit.")
        return candidates, ignored, errors
    text = text.removeprefix("\ufeff")
    delimiter = ";" if text.startswith("Date;Time;") else ","
    reader = csv.reader(StringIO(text, newline=""), delimiter=delimiter, strict=True)
    rows = []
    try:
        if next(reader, None) != HEADER:
            error(1, "Use a DEGIRO Account CSV with the English Date, Time, Value date … Order Id header.")
            return candidates, ignored, errors
        previous = None
        for index, cells in enumerate(reader):
            line = reader.line_num
            if index >= MAX_ROWS:
                error(None, f"The statement exceeds {MAX_ROWS} rows.")
                break
            if not cells or all(not cell.strip() for cell in cells):
                continue
            if len(cells) != len(HEADER) or any(len(cell) > 1000 for cell in cells):
                error(line, "Expected exactly 12 bounded columns.")
                continue
            cells = [cell.strip() for cell in cells]
            try:
                if not re.fullmatch(r"\d{2}-\d{2}-\d{4}", cells[0]) or not re.fullmatch(r"\d{2}:\d{2}", cells[1]) or not re.fullmatch(r"\d{2}-\d{2}-\d{4}", cells[2]):
                    raise ValueError("Use DD-MM-YYYY dates and HH:MM times.")
                stamp = datetime.strptime(f"{cells[0]} {cells[1]}", "%d-%m-%Y %H:%M")
                value_day = datetime.strptime(cells[2], "%d-%m-%Y").date()
                if max(stamp.date(), value_day) > md.utcnow().date():
                    raise ValueError("Future statement dates are not supported.")
                if previous and stamp > previous:
                    raise ValueError("Export the account statement newest first; row order is used to resolve equal timestamps.")
                previous = stamp
                if cells[6]:
                    raise ValueError("FX rows are not supported.")
                if cells[9] != "EUR" or (cells[7] not in ("", "EUR")):
                    raise ValueError("Only EUR account balances and EUR cash changes are supported.")
                number(cells[10])  # Validate evidence, but never interpret running balances as deposits.
                amount = number(cells[8]) if cells[8] else None
                if bool(cells[7]) != (amount is not None):
                    raise ValueError("Cash change must include both currency and amount.")
                rows.append(Row(line, cells, stamp, value_day.isoformat(), amount))
            except ValueError as exc:
                error(line, str(exc))
    except csv.Error:
        error(reader.line_num, "Malformed CSV quoting.")
    if not rows:
        error(None, "The statement contains no account rows.")

    trades, fees, reservations, deposits, sweeps, transfers = {}, {}, [], [], [], []
    identities = set()

    def candidate(row, body, source_rows, identity, effective=None):
        if identity in identities:
            error(row, "Indistinguishable duplicate cash/order rows; cannot safely identify separate transactions.")
            return
        identities.add(identity)
        body["client_id"] = str(uuid5(NAMESPACE, identity))
        funding = effective or row
        candidates.append({"body": body, "source_rows": sorted(source_rows),
                           "sort": (funding.stamp, -funding.line)})

    for row in rows:
        desc = row.description.casefold()
        match = TRADE.fullmatch(row.description)
        try:
            if match:
                if not row.order:
                    raise ValueError("A trade must have an Order Id for safe repeated imports.")
                UUID(row.order)
                if row.order in trades:
                    raise ValueError("Multiple executions for one order are not supported; import this order manually.")
                kind, quantity, unit_price, currency = match.groups()
                quantity, unit_price = number(quantity, 8), number(unit_price, 8)
                if currency.upper() != "EUR" or quantity <= 0 or unit_price <= 0:
                    raise ValueError("Only positive-quantity, positive-price EUR trades are supported.")
                kind = "buy" if kind.casefold() in ("koop", "buy") else "sell"
                with localcontext() as context:
                    context.prec = 80
                    gross = (quantity * unit_price).quantize(ledger.CENT, rounding=ROUND_HALF_UP)
                if gross <= 0 or row.amount != (-gross if kind == "buy" else gross):
                    raise ValueError("Trade cash change does not match quantity × execution price.")
                body = {"type": kind, "date": row.stamp.date().isoformat(), "instrument_id": row.cells[4],
                        "quantity": str(quantity), "price_eur": str(unit_price), "commission_eur": "0.00"}
                trades[row.order] = (row, body)
            elif desc in FEES:
                if not row.order or row.amount is None or row.amount > 0:
                    raise ValueError("Trade fees need an Order Id and a non-positive EUR change.")
                fees.setdefault(row.order, []).append(row)
            elif desc == "flatex interest income":
                if row.cells[4] or row.order or row.amount != Decimal(0):
                    raise ValueError("Only zero-value Flatex Interest Income without an ISIN or Order Id can be ignored; non-zero interest is not supported.")
                ignore(row, "Zero-value Flatex interest income; no cash or position change.")
            elif desc == "reservation ideal":
                if row.cells[4] or row.order or row.amount is None or row.amount == 0:
                    raise ValueError("Invalid iDEAL reservation.")
                reservations.append(row)
            elif desc in DEPOSITS | WITHDRAWALS:
                if row.cells[4] or row.order or row.amount is None or row.amount == 0:
                    raise ValueError("Invalid external cash movement.")
                if (desc in DEPOSITS and row.amount < 0) or (desc in WITHDRAWALS and row.amount > 0):
                    raise ValueError("Cash movement sign does not match its description.")
                deposits.append(row)
            elif desc == "degiro cash sweep transfer":
                if row.cells[4] or row.order or row.amount is None or row.amount == 0:
                    raise ValueError("Invalid internal cash sweep.")
                sweeps.append(row)
            elif TRANSFER.fullmatch(row.description):
                if number(TRANSFER.fullmatch(row.description).group("amount")) <= 0:
                    raise ValueError("Internal transfer amount must be positive; direction is recorded in its description.")
                if row.cells[4] or row.order or row.amount is not None:
                    raise ValueError("Unrecognized flatex cash-account transfer structure.")
                transfers.append(row)
            else:
                raise ValueError("Unsupported account activity. Dividends, interest, taxes, FX and corporate actions must not be silently skipped.")
        except ValueError as exc:
            error(row, str(exc))

    for order, (row, body) in trades.items():
        associated = fees.pop(order, [])
        if len({tuple(fee.cells[:9]) for fee in associated}) != len(associated):
            error(row, "Indistinguishable duplicate fee rows; review this order manually.")
        if any(fee.cells[4] != row.cells[4] or fee.stamp != row.stamp for fee in associated):
            error(row, "Fees must match the trade's ISIN and timestamp.")
        body["commission_eur"] = ledger.euros(-sum((fee.amount for fee in associated), Decimal(0)))
        candidate(row, body, [row.line, *[fee.line for fee in associated]], "order:" + str(UUID(order)))
    for group in fees.values():
        for row in group:
            error(row, "Orphan trade fee: export the associated order in the same statement.")

    used_reservations = set()
    for row in deposits:
        effective = row
        sources = [row.line]
        if row.description.casefold() == "ideal deposit":
            reversals = [item for item in reservations if item.amount == -row.amount and item.stamp == row.stamp and item.value_date == row.value_date]
            originals = [item for item in reservations if item.amount == row.amount and item.value_date == row.value_date and item.stamp <= row.stamp]
            if reversals or originals:
                if len(reversals) != 1 or len(originals) != 1 or any(item.line in used_reservations for item in [*reversals, *originals]):
                    error(row, "Ambiguous/incomplete iDEAL reservation settlement; export the complete funding period.")
                    continue
                original = originals[0]
                effective = original
                for item in [*reversals, original]:
                    used_reservations.add(item.line)
                    sources.append(item.line)
                    ignore(item, "Matched iDEAL reservation/reversal; the final deposit is counted once at funding time.")
        body = {"type": "deposit" if row.amount > 0 else "withdrawal", "date": effective.stamp.date().isoformat(),
                "amount_eur": ledger.euros(abs(row.amount)), "currency": "EUR"}
        identity = "cash:" + digest([row.stamp.isoformat(), row.value_date, row.description.casefold(), ledger.euros(row.amount)])
        candidate(row, body, sources, identity, effective)
    for row in reservations:
        if row.line not in used_reservations:
            error(row, "Unmatched iDEAL reservation: include the final deposit and its reversal in the export.")

    used_transfers = set()
    for row in sweeps:
        matches = [item for item in transfers if item.stamp == row.stamp and item.value_date == row.value_date
                   and number(TRANSFER.fullmatch(item.description).group("amount")) == abs(row.amount)
                   and ((TRANSFER.fullmatch(item.description).group("nl") or TRANSFER.fullmatch(item.description).group("en")).casefold()
                        in ("van", "from")) == (row.amount > 0)]
        if len(matches) != 1 or matches[0].line in used_transfers:
            error(row, "Unmatched internal cash sweep; include its flatex cash-account counterpart.")
            continue
        used_transfers.add(matches[0].line)
        for item in [row, matches[0]]:
            ignore(item, "Internal DEGIRO/flatex cash sweep, not an external deposit or withdrawal.")
    for row in transfers:
        if row.line not in used_transfers:
            error(row, "Unmatched flatex cash-account transfer.")

    candidates.sort(key=lambda item: item["sort"])
    if not candidates and not errors:
        error(None, "No supported external cash movements or trades were found.")
    return candidates, sorted(ignored, key=lambda item: item["row"]), errors


def prepare(db, document, parsed):
    candidates, ignored, parse_errors = parsed
    errors, transactions, additions = list(parse_errors), [], []
    events = document["transactions"]
    existing = {event["client_id"]: event for event in events}
    instrument_cache = {}
    deposits = withdrawals = commissions = Decimal(0)
    duplicates = 0
    for item in candidates:
        raw = item["body"]
        public = {**raw, "source_rows": item["source_rows"], "status": "new"}
        public.pop("client_id")
        try:
            model = ledger.TradeRequest if raw["type"] in ("buy", "sell") else ledger.CashRequest
            body = model.model_validate(raw)
            normalized = ledger.normalized_request(body)
            prior = existing.get(str(body.client_id))
            if prior:
                if prior["_request"] != normalized:
                    raise md.DataError(409, "An imported transaction has changed. Check the order/statement; it cannot be imported twice with different amounts or dates.")
                if prior.get("_removed_at"):
                    public["status"] = "removed"
                    raise md.DataError(409, "This imported transaction was removed and cannot be silently restored.")
                public["status"] = "duplicate"
                duplicates += 1
                event = prior
            else:
                event = ledger.build_event(db, body, [*events, *additions], md.utcnow(), instrument_cache)
                event["source"] = "degiro"
                additions.append(event)
                if body.type == "deposit":
                    deposits += body.amount_eur
                elif body.type == "withdrawal":
                    withdrawals += body.amount_eur
                else:
                    commissions += body.commission_eur
            if "instrument" in event:
                public["instrument"] = event["instrument"]
        except ValidationError as exc:
            errors.append({"row": item["source_rows"][0], "message": "; ".join(
                f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in exc.errors())})
        except md.DataError as exc:
            errors.append({"row": item["source_rows"][0], "message": str(exc)})
        transactions.append(public)
    if len(events) + len(additions) > ledger.MAX_TRANSACTIONS:
        errors.append({"row": None, "message": f"Import would exceed the {ledger.MAX_TRANSACTIONS}-transaction lifetime limit (including removed entries)."})
    cash, positions = None, []
    if not errors:
        try:
            cash_value, _, holdings = ledger.replay([*events, *additions])
            cash = ledger.euros(cash_value)
            positions = [{"instrument_id": isin, "quantity": str(position["quantity"]), "cost_basis_eur": ledger.euros(position["cost"])}
                         for isin, position in sorted(holdings.items()) if position["quantity"]]
        except md.DataError as exc:
            errors.append({"row": None, "message": str(exc) + " Import complete funding history; no opening cash is invented."})
    result = {"can_import": not errors, "transactions": transactions, "ignored_rows": ignored, "errors": errors,
              "warnings": ["ISINs do not identify an exchange. Verify every matched acquired EUR listing before confirming.",
                           "Only new entries are added. Existing same-day entries retain their original order; use a separate empty portfolio for initial history.",
                           "Cash entries without broker IDs are matched by timestamp, value date, description and amount. Indistinguishable movements need manual review.",
                           "Running broker balances include internal cash sweeps and are not used as opening cash or a portfolio valuation. Manual entries are not deduplicated against imports."],
              "summary": {"new_count": len(additions), "duplicate_count": duplicates, "cash_eur": cash, "positions": positions,
                          "deposits_eur": ledger.euros(deposits), "withdrawals_eur": ledger.euros(withdrawals), "commission_eur": ledger.euros(commissions)}}
    # Changes to the portfolio or resolved listing invalidate the reviewed plan.
    result["preview_token"] = digest(["degiro-v1", document["_id"], document["user_id"], document["revision"],
                                      [item["body"] for item in candidates], result])
    return result, additions


@router.post(BASE + "/preview")
def preview(customer_id: str, portfolio_id: str, body: PreviewRequest, request: Request, user: CurrentUser):
    db = request.app.state.database
    document = ledger.require_portfolio(db, str(user["_id"]), customer_id, portfolio_id)
    return prepare(db, document, parse_statement(body.csv))[0]


@router.post(BASE)
def import_statement(customer_id: str, portfolio_id: str, body: ImportRequest, request: Request, user: CurrentUser):
    db, owner = request.app.state.database, str(user["_id"])
    # Authenticate/scope before parsing private content.
    document = ledger.require_portfolio(db, owner, customer_id, portfolio_id)
    parsed = parse_statement(body.csv)
    for _ in range(8):
        result, additions = prepare(db, document, parsed)
        if not result["can_import"]:
            raise md.DataError(422, "Import blocked. Preview again and resolve every reported issue; nothing was saved.")
        counts = {"imported_count": len(additions), "duplicate_count": result["summary"]["duplicate_count"]}
        if not additions:
            return counts  # Lost response/reupload: all events already exist, no write.
        if body.preview_token != result["preview_token"]:
            raise md.DataError(409, "The portfolio or import preview changed. Preview again before confirming.")
        saved = db.portfolios.update_one(
            {**ledger.scope(owner, customer_id, portfolio_id), "revision": document["revision"]},
            {"$push": {"transactions": {"$each": additions}}, "$inc": {"revision": 1}},
        )
        if saved.modified_count:
            return counts
        document = ledger.require_portfolio(db, owner, customer_id, portfolio_id)
    raise md.DataError(409, "Portfolio changed concurrently. Preview again before confirming.")
