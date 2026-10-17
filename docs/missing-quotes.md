# Global missing-quote policy

Settings > Missing quotes controls a calculation-only policy shared by instrument return statistics, trailing-year return/volatility, simulation and bootstrap inputs, portfolio valuation/PnL/TWR, risk proxies, comparison indices, and both walk-forward calibration modes. Raw quotes, OHLC charts, provider envelopes and transaction records are never rewritten. No acquisition is triggered.

## Settings and reproducibility

Authenticated `GET /settings/market-data` returns `{ "version": 1, "method": "smooth", "seed": 0 }` by default. Admin-only `PUT /settings/market-data` accepts `method` (`smooth` or `normal`) and an integer `seed` from 0 through 4294967295. Booleans, strings, fractions and out-of-range seeds are rejected. The singleton `app_settings/market_data` is independent of the existing performance-proxy API. The Settings page loads it independently of the proxy list; generating a seed uses browser cryptographic randomness and does not save until Save is pressed. Non-admins can read but not edit.

New simulations/previews and calibration reports snapshot `quote_fill_policy` (including algorithm version and seed). Saved reports are not recalculated or modified when policy changes. Statistics caches include the policy and currency override in their signature. New live calculations use the current policy, including historical-origin requests; these use historical quote cutoffs, not a claim that today's policy was configured in the past. Seeds reproduce draws only with unchanged observed inputs and algorithm version.

## Eligible holes and currency

Only explicit saved weekday rows with absent/null/NaN prices are fill candidates. No rows are generated for absent dates, weekends or exchange holidays. There is no exchange calendar; an explicitly recorded weekday holiday placeholder is indistinguishable from a missing trading-day row and can be filled. Zero, negative, infinite and malformed prices are invalid, not missing, and remain unavailable. Output must be finite, strictly positive and representable as a float; extreme draws fail closed without clipping or resampling.

Currency metadata is retained. A fill cannot cross a currency boundary or reinterpret explicit foreign-currency prices as EUR. Explicit non-EUR rows are not filled. Consistently unknown metadata can be filled but stays unknown: existing consumer eligibility checks still apply. A matching manual EUR override can resolve missing labels, never explicit foreign labels. This is not FX conversion.

Split-normalized statistics are filled **after** split adjustment. A deliberately invalid split basis cannot be rescued using an adjusted-close fallback. Portfolio marks retain their existing saved-close basis and unsupported corporate-action limitations.

## Arithmetic smoothing (default)

An isolated missing close is the arithmetic mean of the previous and next valid real close. Consecutive holes linearly interpolate by saved-row position between those bounds: for example `100, null, null, 160` becomes `100, 120, 140, 160`. Both bounds must be inside the calculation cutoff and same-currency segment. Leading/trailing holes remain missing for instrument statistics, simulation fitting, calibration, risk proxies and comparison indices. Arithmetic uses fixed Decimal precision, independent of the caller's rounding context.

In smoothing mode only, portfolio valuation and TWR use the last valid confirmed EUR close for an explicit trailing run of missing weekday closes. The fallback cannot cross invalid prices, unsupported/unknown currencies or weekend endpoints. No dates or raw quotes are created. Positions expose `price_is_stale`, the source `price_date`, and `latest_quote_date`, with a visible warning including both dates. Equity, total PnL, customer/portfolio net worth and plan progress use this explicitly stale valuation. Daily PnL falls back to the newest available valid daily close interval (with its actual dates), never to a zero return from carried marks. Stale returns are excluded from expanding risk ratios (summary ratios are unavailable), rather than being treated as observed zero returns. The normal-sampling policy does not silently fall back to carrying. Carried marks never reach instrument statistics, simulation fitting or calibration.

Historical portfolio curves can therefore change when a later saved close supplies the right bound, replacing a stale mark with interpolation. These are retrospective derived marks, not claimed historical observations. Unrepaired required marks still make cumulative TWR unavailable; no partial portfolio total or silent chain restart is introduced.

## Normally sampled instrument log returns

Fit the expanding mean and sample variance of preceding **real** adjacent-weekday log returns in the currency segment. Friday-to-Monday is adjacent; returns crossing absent weekdays are excluded. At least two observed returns are required. Missing rows and synthetic values never enter the fit. Consecutive missing rows may use the preceding derived price as their anchor, but only on adjacent weekdays.

A SHA-256 domain key containing algorithm version, global seed, instrument identifier, date and price field seeds a local normal draw. The derived price is `exp(log(previous price) + sampled log return)`. No future observed price enters either the fit or draw. Different instruments/dates/fields have independent stable streams. Regeneration usually changes sampled values; zero-variance histories necessarily remain deterministic regardless of seed. This is a modeling assumption, not recovered market data, and can change estimated volatility, portfolio performance and projections.

## Calibration boundaries

Each trailing training window is sliced before filling, with the origin as its hard cutoff. A future outcome price cannot complete a training gap or influence the fit. Bootstrap blocks use that same derived training series. After predictions/funding are frozen, outcome rows are filled separately, bounded by that case's goal date. The origin execution mark is pinned to the training mark. Outcome smoothing is retrospective within the scored horizon; it never enters prediction fitting. Downloaded histories can still contain provider revisions, so this is not strict historical-vintage reconstruction.
