# Return statistics and EUR goal planner

Open **Goal planner** in the signed-in workspace (`#/goal`), choose an instrument,
then enter a goal amount, starting capital, goal date and target reliability
(50–99%). The result is a **fixed monthly contribution**, not a lump sum. The
instrument page also shows daily return statistics and links to its planner at
`#/goal/{isin}`.

Only EUR-priced history is eligible. Fund base currency is not trading currency.
A saved manual price-currency setting can fill unknown prices; it cannot convert
explicit USD (or mixed-currency) observations to EUR. Underlying FX exposure may
still exist in a EUR-traded fund. Download history and, when needed, confirm its
price currency on the instrument page before planning.

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
- Exclude weekend quote dates from return estimation, preserving the raw rows.
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
r[t] = P[t] / P[t-1] - 1        # simple return
x[t] = log(P[t]) - log(P[t-1])  # log return
```

The UI's daily mean and variance describe **simple returns, not price levels**.
Variance is in squared return-fraction units, not percent. The model additionally
uses daily log-return mean `m` and sample variance `v`. Annualized volatility is
`sqrt(252 * v)`.

## Monte Carlo model and monthly amount

`backend/goal_planner.py` simulates 10,000 paths using NumPy's generator and fixed
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

Both endpoints require the existing authenticated session. POST uses the same
Origin protections as other commands. Neither calls Marketstack or spends quota.

- `GET /instruments/{isin}/statistics`: estimates, basis, history window/count,
  currency source, eligibility/reason and warnings. An empty history returns an
  ineligible result; an unknown catalogue instrument returns 404.
- `POST /goals/simulate`: `{instrument_id, goal_eur, starting_capital_eur,
  goal_date, reliability}` where money may be decimal strings and probability is
  a fraction (e.g. `0.90`). Invalid inputs/ineligible instruments return 422.

Rebuild the Celery image after updating code/dependencies (`make services-up`)
and restart the backend. Existing history is processed on demand; no re-download
is necessary. There is no scheduled analytics or provider refresh.

Tests: `cd backend && uv run pytest tests/test_goal_planner.py`; real-Mongo
acquisition-to-planner checks are in `tests/test_goal_planner_integration.py` and
use `TEST_MONGODB_URI` with a disposable database and synthetic provider responses.
Frontend coverage is in `frontend/src/GoalPlanner.test.jsx` and
`frontend/src/GoalProjectionChart.test.jsx`.
