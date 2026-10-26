# Portfolio tracker

## Missing-quote policy

All calculation paths apply the [global derived missing-quote policy](missing-quotes.md) before the availability/gap rules described here. Arithmetic smoothing is the default; seeded normal log-return sampling is selectable in Settings. Raw saved evidence and existing reports remain unchanged. References below to missing/invalid prices mean prices still unavailable after this bounded policy.

Open **Customers → a customer → Portfolios**. A customer can have multiple named
portfolios. A portfolio may have no plan; create it with a plan or attach, change,
or detach a plan later using **Assign plan** / **Change plan** beside the portfolio
name. **Unassign plan** removes the link completely. Use the pencil button beside
the name to rename the portfolio inline. First select **Make plan** on a saved
simulation, either in its goal's simulation list or on the saved report. Only
marked, saved simulations belonging to the same customer and account can be
assigned. Linking does not fund a portfolio or recalculate a simulation.

A plan is the original saved simulation with `is_plan: true`, not a copy. Multiple
simulations may be plans, and the same plan may be assigned to multiple portfolios.
Plan choices include strategy, target date and simulation ID to distinguish runs.
Existing portfolio `goal_id` values remain untouched in storage but are no longer
assignments; these portfolios expose `plan_id: null` until a plan is selected.
No simulation is automatically chosen from an old goal link.

Select **Add portfolio** to open the creation form; **Cancel** discards the draft.
The portfolio list mirrors the customers table: portfolio name, linked plan,
net worth, realized, unrealized, Today and total PnL in EUR. Select a portfolio
name to open its ledger. Net worth is that portfolio's cash plus holdings.
Summary PnL follows the customer accounting below, calculated separately for each
portfolio (including its own saved-close interval); it is not live market data.

## Plan recommendations

The **Recommendations** panel compares the linked immutable saved plan with this
portfolio. It keeps three questions separate: whether scheduled funding has been
recorded, whether holdings match the strategy allocation, and whether the existing
plan forecast reaches the target probability. Correcting allocation does not
promise to repair a forecast shortfall or guarantee a goal.

Funding due on UTC date `D` is the saved starting capital, once the simulation
origin is reached, plus the saved monthly contribution for each saved payment date
on or before `D`. Future installments are not due. Net funding is active deposits
minus withdrawals through `D`, including activity before plan assignment. Gains,
losses and sale proceeds are not contributions. The funding gap is
`max(0, due - net funding)`; market losses alone never create a deposit debt.
Each portfolio uses the whole linked plan independently. Editing the original
goal does not change these saved inputs.

Recommendations use strategy-provided weighted instrument/cash targets. Available
cash and scheduled contributions reduce underweight positions before sales are
considered. Remaining material drift can produce an ordered deposit, sell, buy
group, including sales of off-target holdings. Allocation-driven rebalancing uses
these versioned defaults, with stricter strategy constraints taking precedence:

- Maximum absolute weight deviation at least **5 percentage points**.
- At least **30 UTC calendar days** since the last recommendation rebalance.
- Each rebalance trade at least **EUR 25** gross.
- Total commissions at most **1%** of gross rebalance turnover.

Equality qualifies. Scheduled-contribution purchases are not held back by the
rebalance cooldown. Removal of transactions does not reset rebalance history.
Suppressed groups show their cost, threshold or cooldown reason; there is no force
bypass. Quantities floor to eight decimal places and settlements round half-up to
cents. Fees reduce sale proceeds and purchase budgets; residual cash is shown.
Incomplete valuations, unsupported strategies, missing or mismatched listings and
invalid saved schedules have explicit blocked/unavailable states, not an implied
aligned portfolio. Before the saved origin and at or beyond its deadline no
corrective actions are offered.

Choose **Fulfill** for one eligible action or **Fulfill all** for the eligible
group. Review the preview before confirming. Every trade starts with its latest
suitable saved raw EUR last/close, showing the listing, source and date. Derived,
stale, invalid or foreign-currency marks are not default trade prices. A raw
close is suitable for the default only within seven UTC calendar days; the
policy and price provenance are bound to the review token. Where the listing is
otherwise eligible, supply a positive price override if needed.
Prices, quantities and per-trade commissions are editable; commissions explicitly
default to **EUR 0**. Changes require a refreshed preview and must still satisfy
funding, holdings, dependency and cost rules. Every trade required by a rebalance
group contributes to its fee cap, including a cash-funded purchase in that group.
The edited batch must improve maximum allocation drift over the cash/contribution
purchases alone; reducing buy quantities cannot turn a correction into a worse
allocation and still start the cooldown. A purchase cannot silently include
an unselected prerequisite deposit or sale.

Fulfillment records transactions in the Hedgehoog ledger; it does not connect to
a bank or broker. The server appends the reviewed deposit/sell/buy batch atomically,
replaying the whole ledger and guarding portfolio revision, plan, UTC date,
market/listing evidence and policy. A stale preview requires fresh review; a
failed batch records no partial transactions or cooldown. GET and preview do not
write ledger activity or download prices. Successful fulfillment refreshes
balances, positions, history, performance, plan tracking and recommendations.

An operation UUID and durable receipt protect uncertain-response retries. Retry
the same reviewed operation rather than starting another one: identical retries
return the original result even after later edits or transaction removal; changed
inputs under that UUID conflict, and removed transactions are not resurrected.
Receipts remain bounded by the existing lifetime transaction budget. Manual
transactions and broker imports retain their own semantics and are not matched
against recommendation operation IDs.

## Manual ledger

The detail view has **Positions** and **Transactions** tabs with the same portfolio
summary above both. Positions opens by default. Select **Add transaction** from
either tab to expand an inline form above the active table, then choose **Deposit**,
**Withdrawal**, **Buy** or **Sell**; only the fields for that type are shown. The
summary and tabs remain accessible while adding a transaction. **Save transaction**
collapses the form after a successful save and refreshes the server balances and
history. **Cancel** or Escape within the form collapses it without submitting and
returns focus to **Add transaction**. Drafts and uncertain-save retry IDs survive
collapsing/reopening, switching types or tabs, and portfolio refreshes
while you remain on this portfolio; navigation or session changes reset them.
Cancellation is disabled while a save is pending. Cash and trade drafts are kept
separately so funding a purchase does not erase its inputs or retry identity.

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
- Use **Remove** in transaction history, then **Confirm removal**, to remove an
  incorrect entry. This removes it from visible history and recalculates cash,
  holdings and PnL. Removal cannot be undone and must leave cash and holdings
  non-negative throughout the **whole remaining timeline**. Remove dependent
  sales, purchases or withdrawals first if removal is blocked.
- Entries cannot be edited. Transfers, dividends, interest, taxes, FX, splits and
  other corporate actions are not processed. Check inputs before saving.
  Raw-price valuations across corporate actions can be misleading until those
  actions are supported.

Instrument selection uses acquired listings with confirmed EUR **price currency**,
not the fund's base currency. If provider currency is absent, use the instrument
page's manual price-currency setting. It may fill missing metadata but may never
relabel explicit USD/GBP/etc. prices as EUR. Conflicting/unknown currencies block
new purchases. Currency evidence is evaluated on latest observations per date.
A portfolio pins its instrument's symbol/exchange; it never silently switches to
a different listing. Existing EUR trade snapshots remain sellable even if their
catalogue/profile is removed. Such removal does not erase balances or history.

## Performance: TWR and an optional index benchmark

The **Portfolio performance** graph above the detail tabs shows cumulative
**time-weighted return (TWR)** in percent, including cash and trading commissions.
The default is **No benchmark**. Choose a downloaded index to overlay its
percentage change on the same graph; clear the choice to show only portfolio
TWR. The selection is local to the open portfolio view, not a saved portfolio
setting. Hover or tap a point for values; focus the graph and use Left/Right or
Home/End to inspect dates with the keyboard. There is no date slider or permanent
value readout. On narrow screens the graph scrolls horizontally. Benchmark
currency and return variant appear in its legend tooltip. The calculation notes
are documented below rather than repeated on screen; data-quality warnings remain
visible.

The index selector loads the complete active index catalogue. Indices without
saved history must first be explicitly downloaded on **Indices**. Opening,
refreshing or selecting a benchmark never downloads prices or consumes provider
quota. Adding, importing or removing transactions refreshes performance.

### Calculation and dates

Ledger entries have dates, not execution timestamps. This is therefore a
**daily saved-price TWR estimate**, not intraday TWR measured at each cash flow.
For each day with a transaction or saved price observation, let `V_previous` be
the preceding ending equity, `V_end` the ending cash plus marked holdings, `D`
the day's deposits, and `W` its withdrawals. The convention is deposits at the
**start** of the day and withdrawals at the **end**:

`daily growth factor = (V_end + W) / (V_previous + D)`

`cumulative TWR (%) = (product of daily growth factors − 1) × 100`

Buys and sells are internal movements, not external flows. Execution prices,
cent-rounded settlement and all commissions affect equity and therefore return;
first-day fees are not lost by starting the graph at that day's closing value.
The graph includes a zero baseline on the calendar day before the first active
funding entry (when representable), and continues through the current UTC date.
For example, +10% followed by −10% is −1% cumulative, not zero. Depositing or
withdrawing cash by itself creates no return. After a complete withdrawal, an
unfunded interval preserves the prior cumulative return; a new funded period
links onto it. A complete investment loss remains −100% under geometric linking,
even after later refunding; the graph is never silently restarted.

Holdings are replayed historically, not today's positions applied to old prices.
Each date uses the most recent **raw saved close on or before that date**, on the
trade's pinned listing and currently published history generation. Prior closes
carry forward across weekends, holidays, missing observations and after the last
saved close. Thus stale marks can defer a price movement, and newer transactions
may be valued against an older close. No exchange calendar or live price is
inferred. Historical provider revisions are reflected on refresh; this is not a
point-in-time backtest of information available when the trade occurred.

No future close or execution price fills a missing initial mark, and an invalid
latest observation is never replaced by an older valid or adjusted close. Currency is checked on each selected observation: a later currency metadata gap
cannot invalidate earlier confirmed EUR marks, and an override can fill missing
currency only on its matching pinned listing, never relabel a foreign price. If any
closing holding lacks a valid confirmed EUR mark, the portfolio return is
unavailable from that date onward, rather than reporting a partial return or
restarting at a convenient later date. Repair/download the missing history to
restore the chain. A same-day fully liquidated holding needs no closing mark.
Removed entries do not contribute; imported entries follow the same rules.

### Benchmark interpretation

The benchmark is `(saved index level / baseline index level − 1) × 100`, using
its last saved close on or before the same baseline date. It is **not rebased to
a later overlapping date** if initial history is missing; an unavailable index
leaves portfolio TWR available. Index dates are included in the graph and prior
index closes carry forward, with no use of future observations. An invalid index
observation creates a gap, which may recover at the next valid observation
because every point is normalized directly to the fixed valid baseline.

An index is compared in its **own recorded currency**, with **no FX conversion**
to EUR. Its recorded variant may be price, net total return, gross total return,
or unknown; identity verification does not establish that variant. Consequently
this is not necessarily a like-for-like EUR total-return comparison. The portfolio
ledger still does not process dividends, splits, taxes, interest or other
corporate actions, and its raw-price TWR can be misleading across them. Inspect
these limitations rather than treating the chart as a fully adjusted return.

## Import a DEGIRO account statement

Open a portfolio's **Transactions** tab and select **Import DEGIRO CSV**, choose
the broker's **Account** CSV export, then **Preview statement**. The import panel
is hidden on **Positions**; switching tabs preserves an open import draft. Nothing is saved until you check the
chosen EUR listings and transactions and select **Import transactions**. For an
initial import, use a separate empty portfolio and export the complete funding
history. Acquire each required EUR-priced instrument first; the preview reports
missing/unconfirmed listings. A CSV identifies the ISIN but not the exchange, so
verify the displayed symbol/exchange matches your holding. No provider requests
or currency relabeling are performed by an import.

Supported format and accounting:

- UTF-8 (optional BOM), comma or semicolon separator, English headers exactly:
  `Date,Time,Value date,Product,ISIN,Description,FX,Change,,Balance,,Order Id`.
  Dates are `DD-MM-YYYY`, times `HH:MM`, decimal commas with optional dot thousands
  separators. Export **newest first**; equal timestamps use reverse source-row
  order. Times order events within a broker date, not a timezone conversion.
- EUR-only `Koop`/`Buy` and `Verkoop`/`Sell` executions, one execution per Order Id.
  Fees (`DEGIRO Transactiekosten en/of kosten van derden` / `DEGIRO transaction
  and/or third party fees`) are attached by order, ISIN and timestamp. Trade cash
  changes must match rounded quantity × price. Multiple executions of one order,
  orphan fees and ambiguous duplicate rows block import rather than guessing.
- External `iDEAL Deposit`, `Deposit`, `Storting`, `Withdrawal`, `Terugstorting`.
  Cash movements use posting dates. A settled iDEAL deposit with reservation
  entries is counted once at its original funding timestamp/row position;
  its positive reservation and matching negative reversal are disclosed but not
  counted again. Incomplete or ambiguous reservation chains block import.
- `Degiro Cash Sweep Transfer` plus its matching flatex cash-account counterpart
  are internal movements, not external funding. Amount, direction, timestamp and
  value date must match. Running balances are validated as numbers but are not
  used to invent opening cash or reconcile an account-wide valuation.
- `Flatex Interest Income` with an explicit zero EUR change and no ISIN or Order
  Id is disclosed as ignored: it has no cash or position effect. Non-zero interest
  remains unsupported.
- Dividends, non-zero interest, taxes, FX, corporate actions and any other
  unrecognized rows block the **entire** import. The preview shows line-specific
  issues and every ignored zero-interest/internal/reservation row. It never silently imports only the
  recognized portion of a statement. File limit: 1,000,000 UTF-8 bytes / 10,000 rows.

The preview shows new/duplicate transactions, resulting cash and holdings/cost
basis. Monetary values use the same exact Decimal ledger rules as manual entries.
The whole existing-plus-new timeline must remain funded and long-only. Existing
same-day events keep their insertion sequence; an imported historical deposit
cannot move ahead of an already-recorded same-day trade.

Saving appends the complete batch in **one** revision-checked update. If portfolio
metadata, transactions or the chosen listing changed since preview, preview again.
If the response was lost, **Retry import** sends the identical file and token;
existing imported events are recognized without appending again. Re-uploaded and
overlapping exports deduplicate trades by Order Id and cash movements by posting
timestamp, value date, normalized description and amount. Cash rows have no broker
transaction ID: indistinguishable movements cannot be inferred to be separate.
Changed order amounts/dates and removed imported entries are conflicts, not new
entries. Manually entered transactions are **not** matched against broker imports.

The original file and raw descriptions are not saved. Imported ledger entries are
marked `source: "degiro"` and retain deterministic retry IDs and normalized ledger
inputs. They can be removed under the same funded-history rules as manual entries.
The 5,000 lifetime-transaction cap includes imports and removed transactions.

## Accounting and valuation

All backend ledger calculations use high-precision Decimal arithmetic; numeric
API amounts are strings, not binary floats. Cash settlement is rounded once per
trade; intermediate weighted-average cost allocations are not rounded to cents.
A full sale removes the exact remaining cost basis so there is no residual lot.
Portfolio totals are rounded after aggregation; summing individually rounded
position rows may differ by a cent.

- **Average price** = remaining purchase cost (excluding commissions) / remaining quantity.
  The holdings table displays Average cost rounded half-up to two decimals only;
  API values and accounting calculations retain their full precision.
- **Position value** = remaining quantity × latest saved unadjusted EUR close.
- **Total unrealized PnL** = position value − remaining purchase cost.
- **Realized PnL (incl. fees)** = gross sale proceeds − weighted-average purchase
  cost allocated to sold shares − all buy and sell commissions. Each commission
  is expensed immediately on its trade date, even if no shares have been sold;
  it is excluded from cost basis and unrealized PnL.
- **Portfolio cash** = deposits − withdrawals − purchases/fees + net sale proceeds.
- **Portfolio equity** = cash + open position values. Deposits and withdrawals
  are not profit or loss. A fully closed position is omitted from holdings but
  its realized profit/loss and transactions remain.

For example, buying €1,000 of shares with a €6 commission spends €1,006 in cash,
sets cost basis to €1,000 and immediately records −€6 realized PnL. If the market
price is unchanged, unrealized PnL is €0 and total PnL is −€6.

This policy applies to all active manual and imported transactions, including
existing history: balances are derived by replay, without rewriting stored trades.
Compared with capitalizing buy commissions, cash, equity and total PnL are unchanged;
only the realized/unrealized split and displayed purchase cost change.

### Daily unrealized PnL

This is the closing-price movement attributable to **shares still held**, not
cash flow, realized PnL, or a change in total account equity. The interval is the
two latest distinct saved closing dates; the UI keeps only the latest date in
market-price and daily-PnL value tooltips, without inline date labels.
These are saved observations, not live prices or necessarily today/yesterday. Daily values require
adjacent weekdays (Friday → Monday is allowed); gaps spanning missing weekdays or
weekend-dated prices make daily PnL unavailable. No exchange calendar is assumed,
so a holiday gap is conservatively unavailable too. Inspect the shown dates.

For shares held at the previous closing date, the reference basis is quantity ×
previous close. Purchases after that date add their actual cost excluding commissions to
the reference basis; partial sales remove a proportional share of the reference
basis (consistent with average-cost accounting). Daily unrealized PnL equals
current open position value minus this remaining reference basis. This avoids
counting a new contribution or an already-sold position as unrealized profit.
Commissions do not reduce daily unrealized PnL; they reduce realized PnL instead.
**Today PnL** below includes all commissions after its prior-close reference date.

If a trade is newer than the latest saved close, daily and total unrealized PnL
still mark the **current holdings** using that last available saved close; they do
not require today's close. New purchases retain their actual purchase cost
excluding commissions in the daily reference basis, and sales reduce it proportionally. This is a
saved-price estimate, not a historical holdings snapshot at the ending close or
a live return. The latest saved closing date is available in a tooltip, without
an inline date label or repeated saved-close notice.
Portfolio daily unrealized PnL is only summed when all open positions have the
same closing-date interval; mixed intervals are reported as unavailable rather
than combined into a false total. An empty portfolio has zero position value and
zero unrealized PnL.

### Customer summary PnL

The customers table counts every saved goal (including unlinked goals) and sums
all linked/unlinked portfolios. Realized and unrealized PnL use the accounting
above; total PnL is their sum **before rounding**, not a sum of rounded displays.
Customer PnL metrics retain Decimal precision through aggregation and round only
once per metric, so displayed components can differ from total by a cent.
No portfolios means zero. Any unpriced holding makes unrealized/total PnL and net
worth unavailable; realized PnL remains known, never hidden by missing marks.

**Today PnL** is distinct from portfolio **Daily unrealized PnL**: it includes
shares sold after the prior saved close, even full liquidations and same-day round
trips. Using one common pair of adjacent saved closing dates as price references:

`current holdings × latest saved close − prior-close holdings × prior close + net sales − purchases/fees`

This is equity change excluding external deposits/withdrawals. Historical cost
realized on a sale is not counted again as daily profit; previously held shares
are measured against the preceding close. The Today PnL value's tooltip shows
only the latest saved closing date, with no inline date or reference-date range;
it need not be today and is never a live/intraday quote. Both reference dates remain in the API response. Trades on
the preceding closing date belong to the opening holdings. **All later active
trades are included**, even when newer than the latest saved close: purchases
subtract their actual cost and fees, sales add their proceeds less fees, and
current remaining holdings use the latest saved mark. Today's close is not
required. This is a saved-price estimate, not a historical holdings snapshot at
the ending quote date. Daily rounding happens once after customer aggregation.

Relevant positions across portfolios must have matching latest close intervals
and valid raw EUR prices. Missing prices, currency conflicts, weekday gaps or
mixed intervals return unavailable, not a
partial daily sum. Instruments fully liquidated before the interval need no
marks and their newer quotes do not choose the interval for unrelated holdings.
Cash-only customers have zero daily PnL and no date interval. Refreshing is
read-only and reuses saved prices without provider requests.

### Quote quality

Valuation is read-only: opening or refreshing a portfolio never requests provider
data or consumes quota. Update prices explicitly on instrument pages. It uses the
latest observation for each distinct date on the **recorded listing**, bounded by
the current UTC date and collection time. The last two saved dates are selected
before validating their prices. In smoothing mode, an explicit trailing run of
missing weekday closes uses the last valid confirmed EUR close with a stale-price
warning. `price_date` is the actual source date, `latest_quote_date` is the latest
saved date, and `price_is_stale` identifies this fallback. Valuation never skips
invalid prices, and adjusted closes are never substituted for raw closes.

Daily PnL searches backward for the newest available valid daily interval,
including across missing quotes and weekends. It never turns a multi-weekday
gap into a daily return or uses carried prices as observed daily marks. Portfolio
and customer totals require a shared interval across exposed instruments. The
selected dates are returned in `daily_from_date`/`daily_to_date` or
`today_from_date`/`today_to_date`; positions expose `daily_price_date` separately
from their valuation date. Daily percentages use that interval's reference value.
Current-ledger trade accounting (including newer trades and fees where applicable)
is unchanged. Raw quotes, valuation, statistics and simulation fitting are unchanged.

An unresolved missing, invalid or unconfirmed/non-EUR price yields **Unavailable**, not zero.
A missing prior close leaves total valuation available but daily PnL unavailable.
If any open position lacks a valuation, portfolio position value, equity and total
unrealized PnL are also unavailable; cash and realized PnL remain known. Quote
dates remain visible for saved historical marks; warnings are reserved for
missing/invalid prices, currency conflicts and incompatible daily intervals.

## API and persistence

All endpoints require authentication and retain the app's same-origin mutation
protection. Browser requests use the `/api` prefix.

| Endpoint | Behavior |
| --- | --- |
| `GET /portfolio-instruments?limit=20&offset=0` | Eligible acquired EUR listing snapshots |
| `GET /customers/{cid}/portfolios?limit=20&offset=0` | Customer-scoped portfolio metadata plus net worth, realized/unrealized/Today/total PnL and Today interval dates |
| `POST /customers/{cid}/portfolios` | `{name, plan_id?: string|null}` |
| `GET /customers/{cid}/portfolios/{pid}` | Metadata, balances, positions, PnL and warnings |
| `GET /customers/{cid}/portfolios/{pid}/performance?benchmark_id=idx-UUID` | Read-only cumulative daily TWR and optional saved index comparison; omit `benchmark_id` for TWR only |
| `PATCH /customers/{cid}/portfolios/{pid}` | `{name, plan_id?: string|null}`; null/omission detaches plan |
| `GET /customers/{cid}/portfolios/{pid}/recommendations` | Read-only funding, weighted targets, ordered actions, blockers, effective policy and `snapshot_token`; forecast remains on `/plan-tracking` |
| `POST /customers/{cid}/portfolios/{pid}/recommendations/preview` | `{snapshot_token, action_ids, date, overrides?}`; read-only normalized entries, prices/fees, projected balances and `preview_token` |
| `POST /customers/{cid}/portfolios/{pid}/recommendations/fulfill` | Same reviewed fields plus `{operation_id: UUID, preview_token}`; atomic ledger append and durable receipt, or original response on an identical retry |
| `GET /customers/{cid}/portfolios/{pid}/transactions?limit=20&offset=0` | Ledger newest date/entry first |
| `POST /customers/{cid}/portfolios/{pid}/transactions` | Append a validated transaction |
| `DELETE /customers/{cid}/portfolios/{pid}/transactions/{tid}` | Remove one entry after validating the remaining timeline; returns `{id, removed: true}` |
| `POST /customers/{cid}/portfolios/{pid}/imports/degiro/preview` | `{csv: string}`; read-only parsed transactions, row errors/ignored reasons, resulting ledger totals and `preview_token` |
| `POST /customers/{cid}/portfolios/{pid}/imports/degiro` | `{csv: string, preview_token: string}`; atomically append reviewed new entries; returns `{imported_count, duplicate_count}` |

Pagination supports `limit` 1–100 and nonnegative `offset`. Portfolio list metrics
are calculated only for the requested page, sharing a request-local saved-quote
cache. Amounts are two-decimal strings or null when unavailable; date fields are
`today_from_date` / `today_to_date`. Creation, settings and detail metadata remain
unchanged; list summaries are derived, never stored or accepted as editable data.

Performance responses contain `portfolio_id`, `currency: "EUR"`,
`method: "daily_twr"`, `cash_flow_timing: "deposits_start_withdrawals_end"`,
`as_of`, nullable `start_date` / `end_date`, `warnings`, nullable `benchmark`
metadata, and date-ordered `points`. Each point contains `date`, `twr_percent`
and `benchmark_percent`; percentages are eight-decimal strings (`"10.00000000"`
means 10%) or null, not fractions or JSON floats. Empty portfolios have no points.
No benchmark means null benchmark metadata and values. The response is derived
from the whole active ledger, independent of transaction-history pagination, and
nothing is persisted. Unknown/removed benchmark IDs return 404; ETF ISINs or
malformed index IDs return 422. Normal portfolio owner/customer scoping applies.

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
foreign customer/portfolio/goal/transaction IDs return 404. Repeating a successful
removal returns the same acknowledgement without another write. Reposting a
removed transaction's `client_id` returns 409 and cannot restore it; recording a
new entry requires a new ID. An invalid removal returns 422 without changes.

The `portfolios` collection contains owner/customer metadata and a bounded embedded
ledger. An indexed owner/customer lookup protects scope. Atomic revision-checked
updates append or remove an event only after validating that exact ledger version;
a racing writer must re-read and revalidate. This works with standalone MongoDB
and does not need replica-set transactions. Metadata edits increment the revision
but never replace the ledger. Removed entries retain an internal removal marker
and original payload for retry safety and stable insertion sequences; they are
excluded from history, pagination totals, balances, positions and PnL. Removal is
not physical erasure from storage. History is paginated at the API boundary.

Each portfolio is capped at **5,000 recorded transactions**, including removed
entries, to keep the atomic document below MongoDB's 16 MB limit. Removal does not
free capacity. Further additions return 409; existing balances, history and
removal remain available. Large-ledger storage and corporate actions need a later
version.
