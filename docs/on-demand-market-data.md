# On-demand market data (Yahoo Finance experiment)

## Missing-quote policy

All calculation paths apply the [global derived missing-quote policy](missing-quotes.md) before the availability/gap rules described here. Arithmetic smoothing is the default; seeded normal log-return sampling is selectable in Settings. Raw saved evidence and existing reports remain unchanged. References below to missing/invalid prices mean prices still unavailable after this bounded policy.

## Provider boundary and configuration

The default is **Yahoo Finance through pinned `yfinance==1.7.0`**, with no API key or paid monthly allowance. This is experimental, unofficial access, without an availability or completeness guarantee. Yahoo and exchange data terms still apply: free technical access is not permission for commercial redistribution. Before onboarding customers, integrate and license an appropriate provider (planned: EODHD).

`market_provider.py` defines the neutral adapter/error contract. `yahoo_finance.py` owns Yahoo search, metadata, transport, evidence and normalization. `market_data.py` owns jobs, persistence and publication. A future EODHD adapter must declare and test its own symbols, currency and split/dividend semantics rather than assume they match Yahoo.

- `MARKET_DATA_PROVIDER=yfinance` (default).
- `MARKET_DATA_MAX_CALLS_PER_JOB=40`: bounded HTTP attempts, including authentication/session requests, not a paid subscription quota.
- `MARKET_DATA_TIMEOUT_SECONDS=15`: per-request timeout; Celery also enforces task time limits.
- `MARKET_DATA_WORKER_CONCURRENCY=16`: Compose prefork acquisition processes, each with isolated Yahoo sessions. A separate concurrency-1 worker retains the `etfs` queue for catalogue imports and sweep dispatch.
- `YAHOO_REQUEST_INTERVAL_SECONDS=0.05`: Mongo-coordinated spacing between acquisition HTTP request admissions across workers, including session/bootstrap and redirects, not a per-job sleep or Celery task rate limit. Explicit interactive index searches and the standalone coverage survey use separate clients and are outside this acquisition gate. Roughly 20 request starts/second; jobs usually need multiple requests. Workers must share the database and synchronized UTC clocks. Zero disables pacing. No accumulated future slots are reserved; crashes can waste a slot and stalled processes can resume late. Mongo failures prevent unpaced requests. These settings match the successful coverage survey, not a promise that Yahoo will never throttle. Per-job caps, terminal provider errors, freshness checks and publication fencing remain unchanged.
- Marketstack is retired: its adapter, provider option and `MARKETSTACK_*` configuration are removed. Yahoo is the only supported acquisition/read provider; there is no paid or legacy fallback. Instruments with only Marketstack data require a successful Yahoo acquisition before they have usable acquired history.
- `MARKET_DATA_RETENTION_ENABLED=true` and `MARKET_DATA_RETENTION_BATCH_SIZE=1000` control automatic market-data payload retention (Settings fields `market_data_retention_enabled` and `market_data_retention_batch_size`). See [retention and maintenance](#retention-and-maintenance).
- Rebuild the Docker worker after code or dependency changes. API and worker must use the same provider, database and broker configuration.

## Manual acquisition and automatic refresh

Search, browsing, inventory, chart pagination, statistics and polling use MongoDB only. No GET route, navigation event or simulation fetches provider data. A confirmed acquisition POST queues `marketdata.acquire` on RabbitMQ. Application startup also queues a background `marketdata.refresh_stale` sweep; one Celery Beat scheduler queues the same sweep daily at **02:00 UTC**, allowing time for late-published daily closes.

The immediate startup and daily sweeps cover **saved ETFs and all active indices**, including indices without downloaded history, excluding unpublished failed Yahoo ETF profiles and removed indices. After a successful ETF catalogue import (including a same-day skip), the import worker queues an additional sweep with `include_catalogue=True`: it also includes **all catalogue ISINs**, deduplicated across historical snapshots, including unacquired ETFs and failed ETF attempts. This sequencing ensures a fresh installation downloads ETFs imported during this startup, rather than racing an empty catalogue. A failed import does not dispatch the catalogue sweep; a failed sweep dispatch retries the import task, safely reusing its completion marker. Explicit manual catalogue-import task dispatches also trigger this sweep. Initial downloads use since-inception history; existing published Yahoo histories retain incremental refreshes. Invalid metadata is skipped, unresolved listings fail safely, and catalogue-wide downloads may take a long time or encounter provider throttling. Before queuing a provider request, the sweep checks for successful selected-provider acquisition through today or today's published Yahoo quote. The acquisition must have been requested in the current freshness window: UTC midnight until 02:00, then 02:00 onward. A pre-02:00 startup acquisition (even one finishing later) therefore cannot suppress the nightly refresh. Published Yahoo success is required for the successful-job check. Durable freshness survives frequent development restarts and suppresses repeated no-new-bar checks on weekends and holidays; failures do not reset it. No exchange calendar or market-close guarantee is assumed: bars published after a successful check are picked up by the next window or manual refresh.

Each stale candidate goes through the existing acquisition request boundary, cached listing and unique active-job gate. Simultaneous startup, daily and manual requests reuse an active job. Invalid catalogue entries are logged and skipped without blocking other instruments. Sweep persistence/queue failures retry with bounded backoff; provider failures remain terminal for that acquisition and are eligible again on the next sweep. The latest complete published Yahoo history stays readable during refresh or failure. Marketstack-only history is not a fallback.

`make services-up` starts both workers and the scheduler; rebuild after backend changes. Run exactly one `celery-beat` instance and one serial `etfs` worker. `make services-down` stops all three. The scheduler continues while the Docker services run, even when the development API stops. Inspect `docker compose logs celery-beat celery-worker celery-marketdata-worker` for dispatches and outcomes. API startup waits for broker acceptance, not provider downloads; broker dispatch failure aborts startup, as for the ETF catalogue import.

ISIN remains the application instrument ID, from the local ETF catalogue. An uncached acquisition searches Yahoo for an exact catalogue ticker/ISIN candidate and a known venue, then saves the selected symbol, MIC and resolution evidence. This is a listing-selection heuristic, not independent certification of the fund's identity. Unknown/ambiguous mappings fail safely; the user can specify an exact Yahoo symbol before prices exist. Examples: `SPYI.DE`, `EUNL.DE`, `IWDA.AS`. For a symbol-only override, the worker reads that exact symbol’s chart metadata and maps the provider’s exchange identifier to a known MIC, reusing metadata for the profile. It does not search for or substitute another listing, or guess a MIC from the suffix. Exchange MIC remains an optional advanced override for unsupported venues. Saved Yahoo quotes lock the listing.

Yahoo also serves indices (for example `^990100-USD-STRD`). The separate **Indices** catalogue supports adding symbols, manually downloading history and automatic daily acquisition; indices remain excluded from ETF selectors.

Fresh installations initialize four index catalogue entries: S&P 500 (`^GSPC`), MSCI WORLD (`^990100-USD-STRD`), Solactive European Overnight Money Market Index (`DE000SLA1W63.SG`), and Solactive €STR Daily Total Return Index (`DE000SL0H423.SG`). These offline defaults contain no downloaded history or verified currency/return metadata. Startup preserves existing records and does not restore removed entries; the automatic sweep downloads active indices, while manual downloads require explicit confirmation.

**Remove from catalogue** requires UI confirmation and removes an index from the shared catalogue for all users. The authenticated `DELETE /indices/{instrument_id}` endpoint is retry-safe (unknown IDs return 404). Removal retains a tombstone so curated seeds do not reappear on reads or restarts. The latest complete published Yahoo history, acquisition audit and customer records are preserved; superseded payloads remain subject to normal retention and existing downloads are not cancelled. Removed indices cannot start new downloads or be read through index detail/quote endpoints. Explicitly adding the same Yahoo symbol restores the original identity, metadata and latest retained published history.

## Full-history refresh and atomic visibility

Every manual Yahoo refresh or older-history retry requests available daily history from catalogue inception through the current UTC date. The adapter translates the inclusive end into Yahoo's exclusive end.

Automatic startup/daily jobs with existing published Yahoo history instead request **the latest saved quote date through today**, inclusively. The overlapping bar is compared on OHLC, Adj Close, currency and price basis; changed or missing overlap, missing action evidence, or changed splits in the requested range triggers a full-history fallback before publication. Initial downloads and provider transitions remain full history. Successful empty incremental checks retain the saved history and count as checked today.

A successful diff materializes a new complete generation: new observations win on overlapping dates, unchanged observations are copied with original collection times and source-job provenance, and older split evidence is retained outside the downloaded range. The incremental base remains protected until materialization and publication finish; retention must not turn a complete history into only its downloaded tail. Persisted checkpoints support recovery of active work, not replay of superseded market-data vintages. This avoids dynamic cross-generation reads. It does **not** detect unrelated historical corrections outside the overlap; use manual full refresh to retrieve those. No extra full-history action request is made for ordinary diff jobs; actions come from the exact persisted incremental chart.

Each request creates a **generation** identified by its job UUID; payloads are immutable while retained, not permanent vintage archives:

1. Persist the chart-metadata profile, complete sanitized chart/action evidence, normalized quotes and split snapshot under that UUID. This profile is Yahoo chart metadata, not a claim to all optional quote/fund modules. Split evidence is projected from the same persisted chart response, including after process restart; it is never refetched from a different adjustment vintage.
2. Preserve nulls, unusual numeric values and all evidence within retained payloads. Index usable calendar dates; retain unindexable rows in raw evidence. Do not silently repair prices or infer missing values.
3. After the required downloads/indexing succeed, retain an immutable `market_data_publications` audit record, then atomically mark the owned/running job published. Only that owner/status-guarded job transition grants visibility; a stale worker's audit insert alone cannot publish anything.
4. Reads select **only the latest complete published Yahoo generation**, not the newest observation independently for each date and not an older vintage selected by timestamp. A shorter manual full-history response does not silently borrow dates from older generations or another provider. Automatic incremental generations explicitly materialize their pinned base history before publication.
5. A failed/empty refresh leaves the latest complete published data readable and does not authorize pruning it. Failure status and operational audit remain preserved; an initial failed attempt does not appear as an acquired instrument. Superseded successful payloads are subject to [retention](#retention-and-maintenance).

The resolved Yahoo listing and evidence are checkpointed on the job; the active listing cache changes only after publication, so failed replacements retain the previous mapping. Cache writes verify publication order and compare the observed listing/job version atomically. Redelivery of an already cached job is a no-op, preserving the completed watermark.

Publication is idempotent under database acknowledgement loss/redelivery. Quotes are immutable per job/date. The unique active-job gate, renewable worker lease and persisted checkpoints remain. Provider failures are terminal until another manual request or automatic sweep; database failures may retry from saved evidence. At-least-once execution does not guarantee exactly-once HTTP across a crash before response persistence.

`as_of` is a timezone-aware **quote-date bound**: it includes quote dates through its UTC date within the latest complete Yahoo snapshot. It does **not** select data known, collected or published by that timestamp. Later downloads can revise earlier dates, so historical-origin simulations and calibration can contain **look-ahead bias**; this is not knowledge-time or historical-vintage reconstruction.

Details, statistics and the first quote page return a `generation` token. Clients reuse both that token and the quote-date `as_of` bound for later pages, and chart/statistics requests pin the detail generation too. An explicit null detail generation pins the absence of a publication: if a first publication races those reads, the UI requires an explicit latest-snapshot refresh rather than combining empty details with new quotes or statistics. Offset-aware `as_of` values are normalized to UTC before extracting their quote-date bound. A superseded cursor returns **409 Conflict requiring refresh**, even if its payload has not yet been pruned. Clients must reload details and restart pagination; the server never silently switches to a newer generation, an older retained generation or Marketstack/`legacy` data. Reader protection allows already admitted reads to finish, not indefinite replay of old cursors. Catalogue, operational job state and manual currency metadata remain current.

Coverage is the returned range, not a guarantee of every trading session or since-inception completeness. Missing rows and unexpectedly short histories remain visible through coverage metadata. No exchange calendar is assumed.

## Prices, actions and analytics

Requests use daily `auto_adjust=False`, `back_adjust=False`, `repair=False`, `rounding=False`, `keepna=True` and actions. **Yahoo's OHLC and Close are already split-adjusted even with `auto_adjust=False`.** They are not historical as-traded raw prices. `Adj Close` is separately retained and can also reflect cash distributions.

Normalized Yahoo rows declare `price_basis=provider_split_adjusted`. Statistics, trailing-year return/volatility, simulations and both calibration modes copy valid Yahoo **Close** into the existing derived `split_adjusted_close` field. They **do not multiply split factors again**, apply legacy manual split overlays, or fall back to Adj Close when Close is invalid. Thus the existing split-only, non-dividend-reinvested analytics contract is retained. Yahoo corporate-action records are audit evidence, not instructions to adjust its Close twice.

Manual split additions are unavailable for Yahoo's already-adjusted price basis. Legacy manual records remain legacy evidence. A provider change is not permission to reinterpret old manual corrections. Charts label Yahoo prices as provider split-adjusted. Historical price revisions can alter recalculated statistics; immutable saved reports do not change.

Exact strings preserve the numeric tokens supplied in captured JSON where possible; they do not recover precision already lost upstream. Missing volume remains null, not a yfinance-filled zero. Unknown listing currency remains unknown; fund currency is not substituted. Manual currency labels do not perform FX conversion. Portfolio accounting still has no automatic corporate-action share transactions: do not assume a provider switch adds split-aware holdings/accounting support.

## Storage

- `instrument_state`: cached listing and resolution, optional manual currency label, legacy watermark.
- `acquisition_jobs`: provider, declared basis, frozen request bounds, owner/lease, counters/checkpoints, status and sanitized errors.
- `instruments`: generation-owned provider metadata; Yahoo profiles become inventory-visible only after generation publication. Superseded payloads are retention candidates.
- `raw_eod_pages`, `raw_split_pages`: sanitized original evidence plus normalized page envelopes for retained jobs. Never store cookies, crumbs, auth headers or credential-bearing URLs.
- `quotes`: dated projections, immutable while retained, with job/provider provenance and declared price basis.
- `split_snapshots`: complete normalized action evidence for a retained job.
- `market_data_publications`: retained job/provider/listing publication audit records and pruning/tombstone metadata. The owner-fenced `acquisition_jobs.published` marker is authoritative for visibility; an audit record does not imply its historical payload is still readable.
- `instrument_statistics`: derived cache; generation and model version are part of its signature. Superseded caches can be removed with their payloads.

## Retention and maintenance

The retention contract keeps **the latest complete published Yahoo snapshot per instrument**, including the full merged history of incremental publications. Superseded successful Yahoo payloads and positively identified retired Marketstack payloads can be pruned. Publication/job audit records, provenance and pruning tombstones remain; retaining audit does not promise raw payload replay. Failed refreshes do not evict the last good snapshot; failure status and sanitized errors remain available in the audit.

There is **no timed grace period** for superseded payloads. Safety comes from protecting the current publication, active readers and in-flight incremental bases, with eligibility rechecked before deletion. Physical payloads may temporarily coexist while protected or while bounded batches catch up; this does not make old generations readable through a new request. Disabling retention also does not restore historical-vintage API reads.

Retention is restricted to attributable market-data payloads and derived caches. It excludes `manual_splits`, the ETF/index catalogues and import records, users, customer/goal records, saved plans/simulations, calibrations and portfolios/transactions. It is not a customer-data reset or an instrument-removal operation. Saved reports remain immutable, but rerunning the same inputs can differ after a Yahoo revision because superseded source vintages are not retained for replay.

Run maintenance from `backend/` in the backend environment, against the intended database:

```sh
python -m market_data_retention --inventory
# No flag also defaults to inventory only; neither command deletes data.
python -m market_data_retention
# Explicit mutation after reviewing the inventory:
python -m market_data_retention --apply
```

Inventory reports eligible, protected and ambiguous records. Untagged or ambiguous legacy rows are flagged for review and left untouched unless ownership can be established safely; missing provider/job tags are **not** permission to infer Marketstack ownership and blindly delete. `MARKET_DATA_RETENTION_ENABLED=true` enables automatic retention and daily catch-up; `MARKET_DATA_RETENTION_BATCH_SIZE=1000` bounds deletion batches. Daily catch-up handles superseded payloads left behind by interrupted or deferred cleanup. The CLI remains non-destructive without explicit `--apply`.

Fewer retained documents do **not** guarantee smaller filesystem usage: MongoDB may keep freed space for reuse. Compaction or storage reclamation is a separate operator concern, not an automatic part of this maintenance command.

## HTTP contract (authenticated; browser prefix `/api`)

- `GET /instruments/search?q=...&limit=15`: local latest-catalogue autocomplete.
- `GET /instruments/acquired?limit=20&offset=0[&min_history_years=5&hide_negative_returns=true]`: published acquired ETF inventory with quote coverage and historical/statistical metrics. The optional boolean `hide_negative_returns` defaults to `false`; when `true`, it intersects the returns filter with `min_history_years` before computing `total` and applying pagination.
- `GET /instruments/acquired/universe`: complete lightweight acquired ETF universe, unaffected by acquired-list history/returns filters; bulk updates use this unfiltered membership.
- `GET /instruments/budget`: Yahoo returns `{provider:"yfinance", metered:false, month, used:0, limit:null, remaining:null}`. Null limits are not exhausted credit. The header shows the experimental provider instead of a paid quota counter.
- `GET /instruments/{isin}[?as_of=...]`: catalogue, listing, resolution, profile, acquisition, summary, manual currency, `generation`, `provider`, `price_basis`, `provider_configured:true`, `budget:null`.
- `GET /instruments/{isin}/quotes?limit=1000&offset=0[&as_of=...&generation=...]`: latest complete Yahoo generation in ascending quote-date order, bounded by the UTC date of `as_of`; `{items,total,limit,offset,as_of,generation}`. Reuse both cursor fields; superseded cursors return 409 and require restarting pagination. Numeric fields serialize as strings; nulls survive.
- `GET /instruments/{isin}/statistics[?generation=...]`: statistics for that same latest complete Yahoo generation; a superseded generation returns 409 requiring refresh, never silent fallback. Unknown/unpublished IDs fail too.
- `POST /instruments/{isin}/acquisitions`: `{confirmed:true, mode:"update"|"backfill"}` with an optional Yahoo `symbol` override and optional `exchange` MIC. Omitting MIC detects the exchange asynchronously from exact-symbol chart metadata; exchange without symbol is invalid. Returns 202 and a durable job; Yahoo jobs declare `mode:"refresh"`. Duplicate active requests return the same job.
- `PATCH /instruments/{isin}/price-currency`: existing explicit EUR/USD/CHF/GBP/RUB label contract; Mongo-only.
- `/instruments/{isin}/splits`: saved audit evidence; Yahoo manual-add attempts return 409 rather than double-adjusting prices.

## UI and verification

The acquired ETF list has an optional native **Returns** dropdown: **All** is the
default; **Hide negative/unavailable** hides the entire row when either
`historical_year_return` (historical annual return) or
`annualized_mean_log_return` (statistical annual return) is unavailable,
nonnumeric, nonfinite or negative. Both values must be finite numeric values
greater than or equal to zero. Comparisons use raw, unrounded values, not the
displayed percentage: tiny negative returns are hidden even if they round to zero,
while zero and negative zero (`-0`) are kept. Volatility availability, currency
and simulation eligibility are independent; this filter does not change their
rules or filter instrument details, the acquired universe or bulk updates.

With `hide_negative_returns=true`, the acquired-list read validates cached
statistics for every candidate that satisfies the saved-history requirement,
recomputing from saved history where necessary. A cold cache may require reading
many histories, not just the returned page. Distinct quote-date enumeration
remains page-only. With the default `false`, statistics refresh remains page-only.
These reads use saved MongoDB data and never call a provider; no performance
claim is made for the filtered path.

The header identifies Yahoo Finance as experimental. Request forms require explicit consent to fetch Yahoo data, explain full-history refresh, allow a manual Yahoo symbol with optional advanced MIC entry, explain invalid inputs, and do not estimate paid quota. Bulk updates capture the complete lightweight `/instruments/acquired/universe` response before submission and skip profiles collected less than 24 hours ago. The single-response capture avoids membership and ordering races with concurrent downloads; it does not read quote history or compute statistics. No POST is retried automatically after uncertainty.

While refresh is running or fails, previous published charts/statistics remain saved. Errors remain actionable. Provider/basis and collection metadata are available in expandable details. Yahoo's split handling is explained instead of offering a manual split form.

Offline verification should cover adapter normalization/errors, no-network reads, full and incremental complete-history publication, failed-refresh preservation, quote-date `as_of` bounds, superseded-cursor 409 responses, reader/base protection, inventory-only maintenance, ambiguous-record safety, retained audit/tombstones, same-count generation cache invalidation, no double split adjustment, separate Adj Close and nulls, and active-job database recovery. Optional tests use disposable real MongoDB databases. Real UI acceptance is performed with terminal-browser; live provider probes are explicit and bounded, never part of the default automated suite.
