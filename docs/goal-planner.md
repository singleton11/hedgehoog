# Return statistics and EUR goal planner

Open **Goal planner** in the signed-in workspace (`#/goal`), choose a strategy,
configure its parameters, then enter a goal amount, starting capital, goal date
and target reliability (50–99%). The result is a **fixed monthly contribution**,
not a lump sum. The only strategy currently offered is **Buy and hold single
instrument**, with one required parameter of type **instrument**. Choose its
value from the acquired universe, not the full ETF catalogue.

The instrument page still links to `#/goal/{isin}` as a shortcut that prefills
this strategy's instrument parameter; it does not bypass acquired membership or
eligibility. Configuration changes stay local to the planner, preserve monetary
and date inputs, and invalidate previous results. Leaving the planner resets the
configuration; these are not saved plans.

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
- `backend/goal_planner.py`: common request validation, authenticated catalogue
  and dispatch. There is no instrument field in the goal contract itself.
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

No speculative additional strategies, parameter types, portfolio correlations,
backtesting engine or strategy persistence are introduced here.

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
  Use adjusted closes **only when every selected weekday row has a positive finite adjusted
  close**. Otherwise use unadjusted closes consistently across the entire series,
  with a corporate-action/distribution warning. Never splice the two bases.
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

The Acquired ETFs table groups **Annual return** under **Historical**, separately
from **Annual return** and **Annual volatility** under **Statistical**. Both return
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
- Use adjusted closes if **both endpoints** have positive finite adjusted prices;
  otherwise use raw closes at **both** endpoints. Never splice the two bases.
  The tooltip identifies the basis and warns that unadjusted splits/distributions
  can distort the price change. Provider adjustments are used as saved; this is
  not an independently verified dividend-reinvestment total return.
- Endpoint prices must be positive and finite. Invalid endpoints are not silently
  replaced with earlier valid ones. Sparse or invalid intermediate prices do not
  prevent calculating an endpoint ratio; this does not imply continuous coverage.
  Known conflicting price currencies anywhere in the interval make it unavailable;
  unknown currencies remain unverified. No FX conversion or fund-currency assumption
  is applied. Non-finite results are unavailable too.
- API/cache fields are `historical_year_return` (nullable numeric fraction),
  `historical_year_start_date`, `historical_year_end_date` (ISO dates), and
  `historical_year_price_basis` (`adj_close` or `close`). All four are null when
  unavailable. They share the existing cache invalidation; older cache versions
  refresh from saved data without provider requests or changes to raw observations.

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

All endpoints require the existing authenticated session. POST uses the same
Origin protections as other commands. None calls Marketstack or spends quota.

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
Frontend coverage is in `frontend/src/GoalPlanner.test.jsx` and
`frontend/src/GoalProjectionChart.test.jsx`.
