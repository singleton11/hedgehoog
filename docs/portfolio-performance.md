# Portfolio performance

The portfolio detail summary shows cumulative TWR and the latest Sharpe and Sortino ratios. The Performance tab contains a TWR/index comparison chart and a separate, unitless chart containing both ratios. All three tabs retain their state while hidden; refreshes and ledger edits invalidate performance and summary values together.

## App-wide proxy setting

`GET /settings/performance` is authenticated. `PUT /settings/performance` requires the existing admin role and an explicit `{ "instrument_id": "ISIN" }` or `{ "instrument_id": null }` body. The singleton `app_settings/portfolio_performance` stores the selected listing, timestamp and last editor. All portfolios, regardless of owner, use this setting. It does not grant access to other users' portfolios.

The Settings page offers acquired, confirmed EUR-priced ETF listings from the complete paginated `/portfolio-instruments` list. It does not download prices. The setting is a **price-return proxy**, not an annual-yield quote or an inflation rate. For example, an acquired EUR overnight ETF can be selected; a Treasury yield such as `^IRX` cannot. Cash distributions are not reinvested. No selection means unavailable ratios, not a zero-rate fallback. TWR remains available independently.

Changing the setting affects historical ratios on the next portfolio read/refresh; it does not mutate transactions or acquired observations. Reads use the pinned listing and currently published saved generation, bounded by the request's observation time. A removed/relisted proxy or missing/invalid EUR history makes ratios unavailable. Missing price currency may be filled by the matching listing's explicit EUR override, never by relabelling foreign observations.

## Calculation contract

`GET /customers/{customer}/portfolios/{portfolio}/performance` retains the existing daily TWR cash-flow timing: deposits at period start, withdrawals at period end, trades/fees internal. Each point additionally contains nullable eight-decimal `sharpe` and `sortino` strings. The response adds `risk_proxy`, `ratio_method` and `summary: { twr_percent, sharpe, sortino }`.

Ratios are expanding **since-inception daily log excess-return ratios**, not rolling-year or conventional simple-return ratios:

- Portfolio daily factor: `(ending equity + withdrawals) / (previous equity + deposits)`.
- Proxy daily factor: current saved close / saved close on or before the previous calendar day.
- Daily excess: `x = ln(portfolio factor) - ln(proxy factor)`.
- Sharpe: `sqrt(365) * mean(x) / sample_standard_deviation(x)`.
- Sortino: `sqrt(365) * mean(x) / sqrt(sum(min(x, 0)^2) / n)`; the same proxy is the minimum-return target.

Calendar-day sampling uses carried-forward closes, including weekends. Unchanged intervals are accumulated as repeated zero excess returns without expanding the response or iterating across every date. Saved proxy dates are included in the valuation timeline; choosing a comparison index does not change the ratio sample. Days without opening capital or deposits and without exposure are excluded; full-withdrawal days are included. There must be at least two funded observations. Zero deviation is null, not infinity. A non-positive factor (including total loss) or missing required history invalidates the expanding ratios until history is repaired; no partial-history ratio is shown.

The daily log convention aligns with the application's return statistics, but 365 calendar-day annualization differs from the 252-trading-day convention used for instrument quote statistics. These ratios can therefore differ from fund factsheet Sharpe/Sortino values.

## Verification

Backend tests cover an independent numerical oracle, compressed versus expanded flat intervals, permissions, input validation, listing/currency fencing, missing history, future observations, summary consistency and benchmark independence. Frontend tests cover unitless shared lines and gaps, summary values, tab keyboard navigation and state retention, explicit settings saves, read-only users, pagination failures and stale authentication responses.
