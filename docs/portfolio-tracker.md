# Portfolio tracker

Open **Customers → a customer → Portfolios**. A customer can have multiple named
portfolios. A portfolio may have no goal; create it with a goal or attach, change,
or detach a goal later using **Assign goal** / **Change goal** beside the portfolio
name. **Unassign goal** removes the link completely. Use the pencil button beside
the name to rename the portfolio inline. A linked goal must belong to the
same customer and account. Linking is organizational: it neither changes a goal
nor funds/recalculates its simulations.

Select **Add portfolio** to open the creation form; **Cancel** discards the draft.
The portfolio list mirrors the customers table: portfolio name, linked goal,
net worth, realized, unrealized, Today and total PnL in EUR. Select a portfolio
name to open its ledger. Net worth is that portfolio's cash plus holdings.
Summary PnL follows the customer accounting below, calculated separately for each
portfolio (including its own saved-close interval); it is not live market data.

## Manual ledger

The detail view has **Positions** and **Transactions** tabs with the same portfolio
summary above both. Positions opens by default. Select **Add transaction** from
either tab to open a single dialog, then choose **Deposit**, **Withdrawal**, **Buy**
or **Sell**; only the fields for that type are shown. **Save transaction** closes
the dialog after a successful save and refreshes the server balances and history.
**Cancel** or Escape closes without submitting. Drafts and uncertain-save retry
IDs survive closing/reopening, switching types or tabs, and portfolio refreshes
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

## Import a DEGIRO account statement

Open a portfolio and select **Import DEGIRO CSV**, choose the broker's **Account**
CSV export, then **Preview statement**. Nothing is saved until you check the
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
- Dividends, interest, taxes, FX, corporate actions and any other unrecognized
  rows block the **entire** import. The preview shows line-specific issues and
  every ignored internal/reservation row. It never silently imports only the
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
| `GET /customers/{cid}/portfolios?limit=20&offset=0` | Customer-scoped portfolio metadata plus net worth, realized/unrealized/Today/total PnL and Today interval dates |
| `POST /customers/{cid}/portfolios` | `{name, goal_id?: string|null}` |
| `GET /customers/{cid}/portfolios/{pid}` | Metadata, balances, positions, PnL and warnings |
| `PATCH /customers/{cid}/portfolios/{pid}` | `{name, goal_id?: string|null}`; null/omission detaches goal |
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
