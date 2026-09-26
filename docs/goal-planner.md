# Customers, goals and return statistics

Open **Customers** in the signed-in workspace (`#/goal`) to see your
**customers** in a table with their **Net worth (EUR)**. Net worth is the sum of
cash and holdings across all of a customer's tracked portfolios, whether linked
to a goal or not, using saved prices rather than live quotes. External assets and
debts, goal targets and simulated capital are not included. Customers without
portfolios show €0.00; if any open holding cannot be valued, the total shows
**Unavailable**, never a partial sum. **Refresh customers** reloads these totals.
Create a customer with a name, then create one or more named
**goals**, each with a EUR target and goal date. Open a goal and choose **Edit
goal** to change its name, target or date, then **Save changes**. **Cancel**
discards your edits. Open **New simulation** to calculate using the current goal
settings. Starting capital, target reliability (50–99%), strategy and strategy
parameters vary per simulation; the goal's target and date are read-only within
the simulation form.

**Calculate monthly contribution** previews the result and chart without adding
a saved simulation. Click **Save simulation** to retain that exact calculation.
You can save multiple simulations per goal and reopen any read-only report,
including its inputs, statistics and chart. Saving never recalculates, and
retrying the same save cannot create duplicates. Changing inputs or refreshing
statistics discards the current preview; leaving the form discards local inputs.
Unsaved previews are saveable for 24 hours and stored server-side so that saving
uses a trusted result rather than a client-supplied report. They are excluded from
history and cannot be opened as saved reports. Editing a goal never changes an
existing preview or saved simulation: each keeps its original target, date and
result. History shows each simulation's target and date so older results remain
identifiable after an edit. Renaming also works for goals whose dates have passed.
Customer names are creation-only; deletion is not offered.

The result is a **fixed monthly contribution**, not a lump sum. The only strategy
currently offered is **Buy and hold single instrument**, with one required
parameter of type **instrument**, chosen from acquired instruments rather than
the full ETF catalogue. The instrument shortcut `#/goal/{isin}` first asks you
to choose a customer and goal, then prefills that instrument in the new simulation;
it never bypasses acquired membership or eligibility.

All customers, goals and simulation records are private to the authenticated
user. Saved reports preserve the strategy definition and calculated data and
remain readable if the strategy is retired or its instrument data is removed.

The planner keeps the inputs, monthly estimate and chart prominent. Payment dates
and supporting statistics are under **Calculation details**; refresh is under
**Saved return statistics**. Model explanations and limitations live in this
document rather than the UI. Validation errors and data-quality warnings remain
available.

Only EUR-priced history is eligible. Fund base currency is not trading currency.
A saved manual price-currency setting can fill unknown prices; it cannot convert
explicit USD (or mixed-currency) observations to EUR. Underlying FX exposure may
still exist in a EUR-traded fund. Download history and, when needed, confirm its
price currency on the instrument page before planning.

## Strategies and typed parameters

The planner separates a **strategy definition** (stable ID, display name,
description and typed parameter schema) from a **strategy selection** (ID plus
parameter values) and the common goal inputs. `GET /goals/strategies` currently
returns one definition:

```json
{
  "items": [{
    "id": "buy_and_hold_single_instrument",
    "name": "Buy and hold single instrument",
    "description": "Invest starting capital and each monthly contribution in one instrument and hold it.",
    "supports_calibration": true,
    "latest_calibration": null,
    "parameters": [{
      "key": "instrument",
      "type": "instrument",
      "label": "Instrument",
      "required": true,
      "universe": "acquired"
    }]
  }]
}
```

An acquired instrument has a saved provider profile in `instruments`, the same
membership rule as the Acquired ETFs inventory. Profile-only or partially failed
acquisitions belong to this universe, but may lack eligible history. Catalogue
entries, cached listings, queued requests and quotes without a saved profile do
not qualify. The UI reads every acquired-inventory page, rejects incomplete or
changed pagination, and never automatically acquires anything. The backend
rechecks membership and current data eligibility on every simulation, even if a
client supplies a forged parameter or an old shortcut.

Extension points:

- `backend/goal_strategies.py`: register each strategy with its typed Pydantic
  parameter model and execution callback. Reuse `AcquiredInstrument` for any
  instrument-valued field; registry parameter resolution enforces acquired
  membership for every such field before invoking the strategy. Public parameter
  definitions are derived from the model's field metadata. Unknown strategy IDs, unknown/missing
  parameters and invalid types are rejected; no strategy silently falls back to
  buy-and-hold.
- `backend/customer_planner.py`: owner-scoped customers, editable goals, preview
  calculation, explicit saving, paginated history and immutable report reads.
- `backend/goal_planner.py`: common request validation, authenticated catalogue
  and standalone simulation dispatch. There is no instrument field in the goal contract itself.
- `backend/goal_simulation.py`: the existing buy-and-hold Monte Carlo solver and
  common goal inputs. Its linear contribution formula is **not** a universal
  strategy engine. A future strategy with different trading/cash-flow behavior
  supplies a suitable simulator behind the same result contract.
- `frontend/src/StrategyParameters.jsx`: editors are keyed by parameter **type**,
  not strategy ID. Currently only acquired-universe `instrument` parameters are
  supported; new types require an editor and validation. Unsupported definitions
  disable calculation rather than guessing. Instrument controls show saved
  statistics and existing EUR/history eligibility checks.
- `frontend/src/GoalPlanner.jsx`: common goal inputs, selected strategy and
  generic result presentation. Requests/results carry the whole selection;
  statistics are keyed by instrument-parameter name rather than a single global
  instrument. Changing strategy, any parameter/input, refreshing statistics or
  navigating away aborts/discards obsolete simulations.

No speculative additional strategies, parameter types, portfolio correlations
or general-purpose backtesting engine are introduced here. Strategy selections
and definition snapshots are stored with saved simulations, not as editable
strategy definitions.
The separate **Calibration** page records runs evaluating fixed-funded goal
probabilities on historical data; see [walk-forward calibration](strategy-calibration.md).
Each strategy registers an optional generated-scenario `calibrate` callback;
the catalogue exposes `supports_calibration` and the current user's latest score
summary for compatibility. **New run** selects a strategy and parameters; every
successful calculation gets its own saved report. **Run again** copies a report's
settings into a new form without overwriting it. Calibration uses automatic
seeded scenarios only; the original goal-planner simulation contract remains
unchanged.

## Derived statistics

`backend/return_statistics.py` calculates estimates after every successful EOD
acquisition, including updates and backfills. `instrument_statistics` is a derived
MongoDB cache, with one document per ISIN. Existing and partially downloaded data
are also calculated lazily on a statistics read, without a provider request.
Cache invalidation uses model version, listing, append-only observation count and
UTC day; new observations revising an existing date invalidate it too. Current
currency settings and acquisition state are checked separately on every read.
Raw provider pages and quote observations are never rewritten or rejected by
analytics. Database failure during statistics persistence leaves acquisition
unfinished for the existing checkpoint/retry mechanism.

History selection:

- Select the latest observation per date for the saved symbol/MIC, breaking
  collection-time ties by descending MongoDB `_id`. Ignore future quote dates and
  future collection times. This is current-history analytics, not an `as_of`
  historical model or backtest.
- Exclude weekend quote dates from daily-return estimation, preserving the raw rows.
  With downloaded split history, use `split_adjusted_close`: raw close multiplied
  by cumulative effective split factors (new shares / old shares). This separate
  derived series corrects forward/reverse splits without changing candles, applying
  a split twice to provider-adjusted prices, or mixing adjustment vintages. It is
  a price return, not a dividend-reinvestment total return. Missing/invalid raw
  closes still break the chain. Legacy history without splits temporarily prefers
  complete provider `adj_close`, otherwise raw `close`, with an update warning.
- Reject missing, nonnumeric, boolean, non-finite and non-positive prices **from
  calculations only**. Invalid rows break the chain: a return never bridges them.
- Only pairs on adjacent weekdays form daily returns. Friday to Monday is valid;
  missing intervening weekdays break the chain. This prevents weekly/monthly
  prices being annualized as daily data. Without an exchange holiday calendar,
  pairs across missing holiday weekdays are conservatively excluded too. Outliers
  are retained and large moves are disclosed.
- Estimate using all available usable history with equal weighting. Sample
  variance has denominator `n - 1`; fewer than two returns has no variance.
  Simulation requires at least **60 usable returns**. Less than 252 returns and
  prices older than 14 days produce warnings, not a claim of adequate estimation.
  Active acquisitions block simulation until a fresh read after completion.

For consecutive closes `P[t]`:

```
x[t] = ln(P[t]) - ln(P[t-1])  # daily log return
m = sum(x) / n                # arithmetic average of daily log returns
v = sum((x[t] - m)^2) / (n-1) # sample log-return variance
```

All daily-return estimates and model parameters use **log returns, not simple
returns or price levels**. The statistics UI displays four percentages:

| Label | Formula | API/cache field |
| --- | --- | --- |
| Daily return | `m` | `daily_mean_log_return` |
| Daily volatility | `sqrt(v)` | `daily_volatility` |
| Annual return | `252 * m` | `annualized_mean_log_return` |
| Annual volatility | `sqrt(252 * v)` | `annualized_volatility` |

Returns are historical average log returns, displayed as percentages (`100 *
value`); annual return is annualized using 252 trading days, not an observed
calendar-year return or a simple compounded return. Volatility is the standard
deviation, not variance. The model still uses `daily_log_return_variance`
internally and in the API, but squared units are no longer displayed.
The former simple-return fields `daily_mean_return` and `daily_return_variance`
are no longer calculated or returned. Older caches are recomputed automatically
under the new model version without downloading prices again.

Log returns add across consecutive periods. Convert their sum to a monetary
growth factor with `exp(sum(x))`, not `1 + sum(x)`. The greater-than-50% price-move
warning uses the equivalent log thresholds `ln(1.5)` and `ln(0.5)`. Finite log
returns remain usable even when the direct price ratio would overflow; unstable
simulations are still rejected rather than clipped.

## Historical yearly return (inventory)

The Acquired ETFs table groups **Annual return** and **Annual volatility** under
**Historical**, separately from the all-history **Annual return** and **Annual
volatility** under **Statistical**. Both return
columns show green `+` values for growth and red negative values for decline;
zero, unavailable values and volatility stay neutral. The historical return is the
actual simple price change
`P[end] / P[start] - 1`, displayed as a percentage, not a mean, CAGR, forecast or
an input to the Monte Carlo model.

- End at the latest saved quote date in the same latest-observation, listing and
  knowledge-time selection above (not today's date or an earlier valid price).
- Start at the most recent saved quote on or before the calendar anniversary one
  year earlier, allowing at most **7 calendar days** for weekends/holidays. February
  29 clamps to February 28. Never annualize shorter coverage or use a much older
  price as if it were a year ago. Actual endpoint dates are available on hover.
- With a complete split snapshot, use split-corrected raw closes at both endpoints.
  For legacy history only, prefer provider-adjusted closes if both are valid,
  otherwise raw closes at both endpoints. Never splice bases or apply splits to
  `adj_close`. The tooltip identifies the basis. Split correction does not reinvest
  cash distributions or claim a total return.
- Endpoint prices must be positive and finite. Invalid endpoints are not silently
  replaced with earlier valid ones. Sparse or invalid intermediate prices do not
  prevent calculating an endpoint ratio; this does not imply continuous coverage.
  Known conflicting price currencies anywhere in the interval make it unavailable;
  unknown currencies remain unverified. No FX conversion or fund-currency assumption
  is applied. Non-finite results are unavailable too.
- API/cache fields are `historical_year_return` (nullable numeric fraction),
  `historical_year_start_date`, `historical_year_end_date` (ISO dates), and
  `historical_year_price_basis` (`split_adjusted_close`, `adj_close` or `close`). All four are null when
  unavailable. They share the existing cache invalidation; older cache versions
  refresh from saved data without provider requests or changes to raw observations.

### Historical annual volatility (inventory)

`historical_year_volatility` is the **annualized sample standard deviation of daily
log returns within the trailing year**: `sqrt(252 * variance(log_returns))`, not
variance of price levels or an endpoint price change. Its window follows the same
latest-saved-quote/calendar-anniversary rules above. At least two usable daily
returns and year-long saved coverage are required; insufficient coverage or known
mixed currencies produce null, not zero. Flat usable prices produce zero.

With a complete split snapshot, volatility uses split-corrected raw closes.
For legacy history without splits, unlike the historical endpoint return, it uses
provider-adjusted closes only when all weekday prices **within this window** have
usable adjustments; otherwise it uses raw closes throughout. Older prices cannot
affect either the basis or the volatility. The shared daily estimator excludes
weekends and intervals spanning missing weekdays; bad prices break the return chain. Missing data is not
interpolated. Sparse coverage can still yield an estimate from the remaining daily
pairs; small samples are unreliable and do not imply continuous year-long data.

The tooltip shows the selected window, usable return count, basis and formula.
Separate API/cache metadata fields are `historical_year_volatility_start_date`,
`historical_year_volatility_end_date`, `historical_year_volatility_price_basis`,
and `historical_year_volatility_return_count`; all five fields are null when
volatility is unavailable. Window endpoints are selected quote dates, not a claim
that every quote inside the interval is usable. These values share existing
cache/listing isolation and need no provider requests. Full-history statistical
estimates and Monte Carlo inputs are unchanged.

## Monte Carlo model and monthly amount

For buy-and-hold, `backend/goal_simulation.py` simulates 10,000 paths using NumPy's generator and fixed
seed `20260919`. Repeated requests with the same date, inputs and history are
repeatable. Path count/seed are server-controlled, not request parameters.

Starting capital is invested today (UTC). Payments occur at calendar-month
anniversaries, first one month from today, clamped to the last day of shorter
months using the original day-of-month. No payment is made today. A final partial
month grows investments to the goal date without an extra payment. Horizon must
allow at least one payment and cannot exceed 50 years (600 payments).

For each interval with `d` actual calendar days:

```
t = 252 * d / 365.25
log_growth ~ Normal(m * t, v * t)
growth_factor = exp(log_growth)
```

There is no additional `-v/2` drift correction: `m` is already an observed log
mean. Independent normal log returns imply positive lognormal prices. Payments
are added **after** each interval's growth.

Each path's final wealth is linear in the monthly contribution `c`:

```
wealth = starting_capital * cumulative_growth + c * payment_growth_sum
required_c = max((goal - starting_capital * cumulative_growth) / payment_growth_sum, 0)
```

Choose the `ceil(reliability * 10000)`-th smallest required contribution and round
**up** to EUR cents. This meets at least the requested fraction of the same
simulated paths. Report empirical success and no-contribution success rates,
5th/50th/95th terminal wealth percentiles, payment dates/count and total invested.
Zero monthly contribution is returned only if starting capital alone reaches the
requested model probability. Merely holding the nominal goal today is not enough
when its future value is risky.

Money inputs and the calculated monthly amount are limited to EUR 1 trillion;
inputs accept up to two decimal places. Non-finite estimates and numerically
unstable paths return a validation error, not clipped or discarded losing paths.
All returned monetary values are rounded decimal strings; simulation itself uses
floating-point arithmetic and is not exact financial accounting.

## Projection graph

The results graph shows the **arithmetic mean portfolio value** as a solid line,
a light **5th–95th percentile cloud**, a darker **25th–75th percentile cloud**,
and the EUR goal as a dashed reference. The slider (keyboard/touch) and pointer
hover select a date and display exact rounded amounts.

These are pointwise outcome ranges across **all 10,000 traces**, not a confidence
interval for the mean or a range containing every possible outcome. The mean
line is not an individual scenario and need not lie inside the percentile cloud
for highly skewed outcomes. Chart values include the starting capital and the
recommended monthly payment, sampled after each payment and at the goal date.

After solving the contribution, the backend replays the same seeded draws to
calculate monthly summaries. It does not create a new set of scenarios or
subsample traces. The second pass keeps working memory proportional to path
count rather than storing a 10,000-by-600 history matrix. The response contains
at most 601 points, including today, monthly dates and any final partial period.
Terminal graph percentiles match the separately reported terminal percentiles.

`POST /goals/simulate` additionally returns `goal_eur` and `trajectory`, an array
of `{date, mean_eur, p05_eur, p25_eur, p50_eur, p75_eur, p95_eur}`. All EUR values
remain decimal strings; only the frontend chart converts them to finite numbers
for SVG geometry. No chart library or provider calls are required.

## Important limitations

Reliability is an **estimated probability conditional on the model**, not a
promise or statistical confidence in the model parameters. Historical mean is
particularly uncertain. The model does not capture fat tails, regime changes,
serial dependence or parameter uncertainty. 10,000 paths also have sampling
error; a fixed seed improves repeatability, not accuracy.

Amounts are nominal EUR. There is no inflation, tax, transaction fee, withdrawal,
lot-size, liquidity or changing-contribution model. Fractional shares are assumed.
Fund expenses and reinvested distributions are reflected only insofar as they
appear in the price history. Adjusted prices are not a verified total-return
series; incremental downloads may mix corporate-action adjustment vintages.
This is an educational planning estimate, not a guaranteed investment plan.

## API and operation

All endpoints require the existing authenticated session. POST and PATCH use the same
Origin protections as other commands. None calls Marketstack or spends quota.

### Customers, goals and saved simulations

- `GET /customers`, `POST /customers` (`{name}`), `GET /customers/{customerId}`.
  Customer list items additionally include `net_worth_eur`, an exact two-decimal
  EUR string or `null` when any tracked holding cannot be valued. Totals are
  computed on read only for the current user's requested customer page, using
  the same rounded equity and saved-price rules as portfolio details. No market
  data acquisition or simulation is triggered. Creation/detail responses remain
  name-only metadata; the derived total is not stored on the customer.
- `GET /customers/{customerId}/goals`, `POST /customers/{customerId}/goals`
  (`{name, goal_eur, goal_date}`), and `GET /customers/{customerId}/goals/{goalId}`.
- `PATCH /customers/{customerId}/goals/{goalId}` updates one or more of
  `{name, goal_eur, goal_date}` and returns the updated goal. Only supplied fields
  change; nulls, empty requests and unknown fields are rejected. Names are trimmed
  and limited to 200 characters, targets use the same positive EUR validation as
  creation, and changed dates must satisfy the current simulation horizon. An
  unchanged older date does not prevent renaming. Identity, customer attachment,
  creation time, linked portfolios and simulation snapshots are preserved.
- Under `/customers/{customerId}/goals/{goalId}`:
  - `POST /simulate` accepts `{strategy, starting_capital_eur, reliability}`.
    Target/date are loaded from the saved goal, never overridden by the client.
    Returns a temporary preview envelope with `id`, `customer_id`, `goal_id`,
    `calculated_at`, `saved_at: null`, full `inputs`, `strategy_definition` and
    `result` (the existing simulation payload).
  - `POST /simulations/{simulationId}/save` takes no report body. It atomically
    saves the unexpired server preview and returns its envelope with `saved_at`.
    Repeated saves return the same record and timestamp, without recalculating.
  - `GET /simulations` lists only saved compact summaries: ID, calculation/save
    times, inputs, strategy name, monthly contribution and success probability.
  - `GET /simulations/{simulationId}` loads the full saved envelope without
    fetching current statistics, strategy definitions or provider data.

All three lists accept `limit` (default 20, maximum 100) and `offset`, returning
`{items, total, limit, offset}` in deterministic newest-first order (creation
for customers/goals, calculation for simulations, with an ID tie breaker). Unknown or
foreign customer/goal/simulation IDs are not accessible, including mismatched
parents. Money uses decimal strings. Expired unsaved previews cannot be saved;
MongoDB TTL cleanup eventually removes them. A saved record has no expiry.

The workspace routes mirror this hierarchy:
`#/goal/customers/{customerId}`, then `/goals/{goalId}`, `/new` for a new
simulation and `/simulations/{simulationId}` for a saved report. No existing
planner data migration is needed: the previous planner did not persist plans.

### Statistics and standalone calculation compatibility

- `GET /instruments/{isin}/statistics`: estimates, basis, history window/count,
  currency source, eligibility/reason and warnings. An empty history returns an
  ineligible result; an unknown catalogue instrument returns 404.
- `GET /goals/strategies`: the strategy catalogue described above.
- `POST /goals/simulate`: common goal inputs plus a strategy selection:

  ```json
  {
    "strategy": {
      "id": "buy_and_hold_single_instrument",
      "parameters": {"instrument": "IE00B4L5Y983"}
    },
    "goal_eur": "100000.00",
    "starting_capital_eur": "10000.00",
    "goal_date": "2036-09-17",
    "reliability": 0.9
  }
  ```

  Money may be decimal strings and probability is a fraction. Invalid inputs,
  unknown strategies, invalid/extra parameters, unacquired instruments and
  ineligible history return 422. The legacy top-level `instrument_id` request
  field is no longer accepted. The response echoes `strategy` and replaces the
  old single `statistics` object with `parameter_statistics: {"instrument": ...}`.
  Monetary, schedule, probability, trajectory and warning fields are unchanged.

Rebuild the Celery image after updating code/dependencies (`make services-up`)
and restart the backend. Existing history is processed on demand; no re-download
is necessary. There is no scheduled analytics or provider refresh.

Tests: `cd backend && uv run pytest tests/test_goal_planner.py`; real-Mongo
acquisition-to-planner checks are in `tests/test_goal_planner_integration.py` and
use `TEST_MONGODB_URI` with a disposable database and synthetic provider responses.
Customer/goal persistence coverage is in `backend/tests/test_customer_planner.py`;
`backend/tests/test_customer_planner_integration.py` checks real MongoDB atomic
saves, expiry and durable reports using `TEST_MONGODB_URI`.
Frontend coverage is in `frontend/src/CustomerPlanner.test.jsx`,
`frontend/src/GoalPlanner.test.jsx` and `frontend/src/GoalProjectionChart.test.jsx`.
