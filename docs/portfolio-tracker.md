# Portfolio tracker

Open **Customers → a customer → Portfolios**. A customer can have multiple named
portfolios. A portfolio may have no goal; create it with a goal or attach, change,
or detach a goal later in portfolio settings. A linked goal must belong to the
same customer and account. Linking is organizational: it neither changes a goal
nor funds/recalculates its simulations.

## Manual ledger

- Portfolios and cash movements are **EUR only**. Record a deposit before buying.
- Trade fields: instrument, buy/sell, quantity, average execution price per unit
  in EUR, optional EUR commission (defaults to zero), date.
- Cash fields: deposit/withdrawal, EUR amount, date.
- Dates default to **today (UTC)**, including trades and deposits; they are
  editable for historical entry. Future dates are rejected. The API also defaults
  an omitted date to today.
- Quantities and execution prices support up to eight decimal places; cash and
  commissions support cents. Prices, quantities and cash movements must be
  positive; commission may be zero. Trade notional (`quantity × price`) is rounded
  half-up to cents at settlement and must be at least EUR 0.01. Each amount/notional
  is capped at EUR 1 trillion. Sell fees cannot exceed gross sale proceeds.
- No margin or shorting: purchases plus fees and withdrawals must fit cash;
  sales cannot exceed holdings. A backdated entry replays the **whole timeline**
  and is rejected if it invalidates any later balance. On the same date, events
  run in the order they were originally entered. Enter historical deposits before
  same-day trades; entering them later cannot reorder existing events.
- Transactions are append-only in this first version: no edit/delete, transfers,
  dividends, interest, taxes, FX, splits or other corporate-action processing.
  Check the inputs before saving. Raw-price valuations across corporate actions
  can be misleading until those actions are supported.

Instrument selection uses acquired listings with confirmed EUR **price currency**,
not the fund's base currency. If provider currency is absent, use the instrument
page's manual price-currency setting. It may fill missing metadata but may never
relabel explicit USD/GBP/etc. prices as EUR. Conflicting/unknown currencies block
new purchases. Currency evidence is evaluated on latest observations per date.
A portfolio pins its instrument's symbol/exchange; it never silently switches to
a different listing. Existing EUR trade snapshots remain sellable even if their
catalogue/profile is removed. Such removal does not erase balances or history.

## Accounting and valuation

All backend ledger calculations use high-precision Decimal arithmetic; numeric
API amounts are strings, not binary floats. Cash settlement is rounded once per
trade; intermediate weighted-average cost allocations are not rounded to cents.
A full sale removes the exact remaining cost basis so there is no residual lot.
Portfolio totals are rounded after aggregation; summing individually rounded
position rows may differ by a cent.

- **Average price** = remaining fee-inclusive purchase cost / remaining quantity.
- **Position value** = remaining quantity × latest saved unadjusted EUR close.
- **Total unrealized PnL** = position value − remaining purchase cost.
- **Realized PnL** = sum of net sale proceeds (after sell fees) − weighted-average
  purchase cost allocated to the sold shares. Buy fees are capitalized in cost.
- **Portfolio cash** = deposits − withdrawals − purchases/fees + net sale proceeds.
- **Portfolio equity** = cash + open position values. Deposits and withdrawals
  are not profit or loss. A fully closed position is omitted from holdings but
  its realized profit/loss and transactions remain.

### Daily unrealized PnL

This is the closing-price movement attributable to **shares still held**, not
cash flow, realized PnL, or a change in total account equity. The interval is the
two latest distinct saved closing dates, displayed in the UI. These are saved
observations, not live prices or necessarily today/yesterday. Daily values require
adjacent weekdays (Friday → Monday is allowed); gaps spanning missing weekdays or
weekend-dated prices make daily PnL unavailable. No exchange calendar is assumed,
so a holiday gap is conservatively unavailable too. Inspect the shown dates.

For shares held at the previous closing date, the reference basis is quantity ×
previous close. Purchases after that date add their actual fee-inclusive cost to
the reference basis; partial sales remove a proportional share of the reference
basis (consistent with average-cost accounting). Daily unrealized PnL equals
current open position value minus this remaining reference basis. This avoids
counting a new contribution or an already-sold position as unrealized profit.

If a trade is newer than the latest saved close, daily PnL is unavailable until
prices catch up. Total unrealized PnL can still use the last saved close, with its
date and a warning that it is not a live price. Portfolio daily unrealized PnL is
only summed when all open positions have the same closing-date interval; mixed
intervals are reported as unavailable rather than combined into a false total.
An empty portfolio has zero position value and zero unrealized PnL.

### Customer summary PnL

The customers table counts every saved goal (including unlinked goals) and sums
all linked/unlinked portfolios. Realized and unrealized PnL use the accounting
above; total PnL is their sum **before rounding**, not a sum of rounded displays.
Customer PnL metrics retain Decimal precision through aggregation and round only
once per metric, so displayed components can differ from total by a cent.
No portfolios means zero. Any unpriced holding makes unrealized/total PnL and net
worth unavailable; realized PnL remains known, never hidden by missing marks.

**Today PnL** is distinct from portfolio **Daily unrealized PnL**: it includes
shares sold during the latest saved trading-day interval, even full liquidations
and same-day round trips. Over one common pair of adjacent closing dates it is:

`closing holdings value − opening holdings value + net sales − purchases/fees`

This is equity change excluding external deposits/withdrawals. Historical cost
realized on a sale is not counted again as daily profit; previously held shares
are measured against the preceding close. The interval is shown in the table;
it need not end today and is never a live/intraday quote. Trades on the preceding
closing date belong to the opening holdings; trades on the ending date are
inside the interval. Daily rounding happens once after customer aggregation.

Relevant positions across portfolios must have matching latest close intervals,
valid raw EUR prices, and no trades newer than the ending close. Missing prices,
currency conflicts, weekday gaps or mixed intervals return unavailable, not a
partial daily sum. Instruments fully liquidated before the interval need no
marks and their newer quotes do not choose the interval for unrelated holdings.
Cash-only customers have zero daily PnL and no date interval. Refreshing is
read-only and reuses saved prices without provider requests.

### Quote quality

Valuation is read-only: opening or refreshing a portfolio never requests provider
data or consumes quota. Update prices explicitly on instrument pages. It uses the
latest observation for each distinct date on the **recorded listing**, bounded by
the current UTC date and collection time. The last two saved dates are selected
before validating their prices: a bad latest close is not silently replaced by an
older valid close, and adjusted closes are never substituted for raw closes.

A missing, invalid or unconfirmed/non-EUR price yields **Unavailable**, not zero.
A missing prior close leaves total valuation available but daily PnL unavailable.
If any open position lacks a valuation, portfolio position value, equity and total
unrealized PnL are also unavailable; cash and realized PnL remain known. Stale-price
warnings and quote dates accompany saved historical marks.

## API and persistence

All endpoints require authentication and retain the app's same-origin mutation
protection. Browser requests use the `/api` prefix.

| Endpoint | Behavior |
| --- | --- |
| `GET /portfolio-instruments?limit=20&offset=0` | Eligible acquired EUR listing snapshots |
| `GET /customers/{cid}/portfolios?limit=20&offset=0` | Customer-scoped portfolio metadata |
| `POST /customers/{cid}/portfolios` | `{name, goal_id?: string|null}` |
| `GET /customers/{cid}/portfolios/{pid}` | Metadata, balances, positions, PnL and warnings |
| `PATCH /customers/{cid}/portfolios/{pid}` | `{name, goal_id?: string|null}`; null/omission detaches goal |
| `GET /customers/{cid}/portfolios/{pid}/transactions?limit=20&offset=0` | Ledger newest date/entry first |
| `POST /customers/{cid}/portfolios/{pid}/transactions` | Append a validated transaction |

Pagination supports `limit` 1–100 and nonnegative `offset`.

Cash transaction example:

```json
{"client_id":"85144638-d44c-4564-bb21-f7f0dd405aaa","type":"deposit","date":"2026-09-24","amount_eur":"1000.00","currency":"EUR"}
```

Trade example:

```json
{"client_id":"85144638-d44c-4564-bb21-f7f0dd405aab","type":"buy","date":"2026-09-24","instrument_id":"IE00B5BMR087","quantity":"2.5","price_eur":"100.125","commission_eur":"1.00"}
```

`client_id` is a required caller-generated UUID. Retrying an identical transaction
with the same ID returns the original event without appending or charging again.
Reusing it with different normalized inputs is a conflict (409). Keep the same
ID and effective date after a lost response; do not generate a new ID just because
a request timed out. Validation failures are 422 and write nothing. Unknown or
foreign customer/portfolio/goal IDs return 404.

The `portfolios` collection contains owner/customer metadata and a bounded embedded
ledger. An indexed owner/customer lookup protects scope. Atomic revision-checked
updates append an event only after validating that exact ledger version; a racing
writer must re-read and revalidate. This works with standalone MongoDB and does not
need replica-set transactions. Metadata edits increment the revision but never
replace the ledger. History is paginated at the API boundary.

The first version explicitly caps each portfolio at **5,000 transactions** to keep
the atomic document below MongoDB's 16 MB limit. Further additions return 409;
existing balances and history remain readable. Large-ledger storage/corporate
actions and transaction corrections need a later version.
