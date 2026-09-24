"""Independent rational-number oracle for cumulative average-cost accounting."""

from decimal import Decimal
from fractions import Fraction
from random import Random

from portfolio_tracker import euros, replay


def test_random_fractional_ledger_reconciles_against_rational_oracle():
    random = Random(23491)
    snapshots = [{"instrument_id": f"instrument-{index}", "name": f"ETF {index}", "currency": "EUR",
                  "symbol": str(index), "exchange": "XETR"} for index in range(3)]
    events = []
    cash = Fraction(1_000_000)
    flows, realized = cash, Fraction(0)
    holdings = {item["instrument_id"]: [Fraction(0), Fraction(0)] for item in snapshots}

    def append(kind, **values):
        events.append({"type": kind, "date": "2026-09-01", "sequence": len(events) + 1, **values})

    def exact_decimal(value):
        # Event quantities/prices have terminating decimal expansions. The
        # oracle's allocated cost may recur and is never converted this way.
        return str(Decimal(value.numerator) / Decimal(value.denominator))

    def trade(instrument, quantity, cents, fee, sell=False):
        nonlocal cash, realized
        held, basis = holdings[instrument["instrument_id"]]
        raw_cents = quantity * cents
        # All gross prices are positive: implement half-up independently using
        # integer rational arithmetic, not the production Decimal helper.
        gross = Fraction((2 * raw_cents.numerator + raw_cents.denominator) // (2 * raw_cents.denominator), 100)
        fee_amount = Fraction(fee, 100)
        if sell:
            allocation = basis * quantity / held
            realized += gross - fee_amount - allocation
            cash += gross - fee_amount
            holdings[instrument["instrument_id"]] = [held - quantity, basis - allocation]
        else:
            cash -= gross + fee_amount
            holdings[instrument["instrument_id"]] = [held + quantity, basis + gross + fee_amount]
        append("sell" if sell else "buy", instrument=instrument, quantity=exact_decimal(quantity),
               price_eur=exact_decimal(Fraction(cents, 100)), commission_eur=exact_decimal(fee_amount))

    def compare():
        actual_cash, actual_realized, actual_positions = replay(events)
        assert Fraction(actual_cash) == cash
        assert abs(Fraction(actual_realized) - realized) < Fraction(1, 10**60)
        for isin, (quantity, basis) in holdings.items():
            position = actual_positions.get(isin, {"quantity": Decimal(0), "cost": Decimal(0)})
            assert Fraction(position["quantity"]) == quantity
            assert abs(Fraction(position["cost"]) - basis) < Fraction(1, 10**60)
        total_basis = sum((position["cost"] for position in actual_positions.values()), Decimal(0))
        # Test the displayed monetary reconciliation too, without assuming
        # finite precision can represent rational allocations exactly.
        assert abs(Fraction(actual_cash) + Fraction(total_basis) - flows - Fraction(actual_realized)) < Fraction(1, 10**18)

    append("deposit", amount_eur=exact_decimal(cash))
    for step in range(350):
        instrument = random.choice(snapshots)
        held, _ = holdings[instrument["instrument_id"]]
        if step % 31 == 0:
            amount = Fraction(random.randint(1, 50000), 100)
            withdrawal = step % 62 == 0
            cash += -amount if withdrawal else amount
            flows += -amount if withdrawal else amount
            append("withdrawal" if withdrawal else "deposit", amount_eur=exact_decimal(amount))
        else:
            sell = held > 0 and random.random() < 0.55
            # Hundredth-share units preserve exact submitted quantities while
            # average-cost allocations exercise recurring rational values.
            quantity = Fraction(random.randint(1, int(held * 100) if sell else 500), 100)
            trade(instrument, quantity, random.randint(1000, 20000), random.randint(0, 5), sell=sell)
        if step % 35 == 0:
            compare()
    compare()
    for instrument in snapshots:
        quantity, _ = holdings[instrument["instrument_id"]]
        if quantity:
            trade(instrument, quantity, 12567, 1, sell=True)
    compare()
    actual_cash, actual_realized, positions = replay(events)
    assert all(position["quantity"] == position["cost"] == 0 for position in positions.values())
    assert euros(actual_cash - Decimal(flows.numerator) / Decimal(flows.denominator)) == euros(actual_realized)
