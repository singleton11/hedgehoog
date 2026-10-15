# On-demand market data (Yahoo Finance experiment)

## Provider boundary and configuration

The default is **Yahoo Finance through pinned `yfinance==1.7.0`**, with no API key or paid monthly allowance. This is experimental, unofficial access, without an availability or completeness guarantee. Yahoo and exchange data terms still apply: free technical access is not permission for commercial redistribution. Before onboarding customers, integrate and license an appropriate provider (planned: EODHD).

`market_provider.py` defines the neutral adapter/error contract. `yahoo_finance.py` owns Yahoo search, metadata, transport, evidence and normalization. `market_data.py` owns jobs, persistence and publication. A future EODHD adapter must declare and test its own symbols, currency and split/dividend semantics rather than assume they match Yahoo.

- `MARKET_DATA_PROVIDER=yfinance` (default).
- `MARKET_DATA_MAX_CALLS_PER_JOB=40`: bounded HTTP attempts, including authentication/session requests, not a paid subscription quota.
- `MARKET_DATA_TIMEOUT_SECONDS=15`: per-request timeout; Celery also enforces task time limits.
- The legacy Marketstack adapter remains available for explicit compatibility/testing. There is **no automatic fallback** to it, and Compose no longer injects Marketstack credentials. Existing `MARKETSTACK_*` variables do not gate Yahoo acquisition.
- Rebuild the Docker worker after code or dependency changes. API and worker must use the same provider, database and broker configuration.

## Manual acquisition and automatic refresh

Search, browsing, inventory, chart pagination, statistics and polling use MongoDB only. No GET route, navigation event or simulation fetches provider data. A confirmed acquisition POST queues `marketdata.acquire` on RabbitMQ. Application startup also queues a background `marketdata.refresh_stale` sweep; one Celery Beat scheduler queues the same sweep daily at **02:00 UTC**, allowing time for late-published daily closes.

The sweep covers **saved ETFs and all active indices**, including indices without downloaded history. It does not download the entire ETF reference catalogue, include unpublished failed Yahoo ETF profiles, or resurrect removed indices. Before queuing a provider request, the sweep checks for successful selected-provider acquisition through today or today's published Yahoo quote. The acquisition must have been requested in the current freshness window: UTC midnight until 02:00, then 02:00 onward. A pre-02:00 startup acquisition (even one finishing later) therefore cannot suppress the nightly refresh. Published Yahoo success is required for the successful-job check. Durable freshness survives frequent development restarts and suppresses repeated no-new-bar checks on weekends and holidays; failures do not reset it. No exchange calendar or market-close guarantee is assumed: bars published after a successful check are picked up by the next window or manual refresh.

Each stale candidate goes through the existing acquisition request boundary, cached listing and unique active-job gate. Simultaneous startup, daily and manual requests reuse an active job. Invalid catalogue entries and unavailable legacy budgets are logged and skipped without blocking other instruments. Sweep persistence/queue failures retry with bounded backoff; provider failures remain terminal for that acquisition and are eligible again on the next sweep. Previous published history stays readable. No automatic fallback to a paid provider occurs.

`make services-up` starts the worker and scheduler; rebuild after backend changes. Run exactly one `celery-beat` instance. `make services-down` stops both. The scheduler continues while the Docker services run, even when the development API stops. Inspect `docker compose logs celery-beat celery-worker` for dispatches and outcomes. API startup waits for broker acceptance, not provider downloads; broker dispatch failure aborts startup, as for the ETF catalogue import.

ISIN remains the application instrument ID, from the local ETF catalogue. An uncached acquisition searches Yahoo for an exact catalogue ticker/ISIN candidate and a known venue, then saves the selected symbol, MIC and resolution evidence. This is a listing-selection heuristic, not independent certification of the fund's identity. Unknown/ambiguous mappings fail safely; the user can specify an exact Yahoo symbol before prices exist. Examples: `SPYI.DE`, `EUNL.DE`, `IWDA.AS`. For a symbol-only override, the worker reads that exact symbol’s chart metadata and maps the provider’s exchange identifier to a known MIC, reusing metadata for the profile. It does not search for or substitute another listing, or guess a MIC from the suffix. Exchange MIC remains an optional advanced override for unsupported venues; the legacy Marketstack provider still requires both symbol and MIC. Saved quotes lock the listing.

Yahoo also serves indices (for example `^990100-USD-STRD`). The separate **Indices** catalogue supports adding symbols, manually downloading history and automatic daily acquisition; indices remain excluded from ETF selectors.

Fresh installations initialize four index catalogue entries: S&P 500 (`^GSPC`), MSCI WORLD (`^990100-USD-STRD`), Solactive European Overnight Money Market Index (`DE000SLA1W63.SG`), and Solactive €STR Daily Total Return Index (`DE000SL0H423.SG`). These offline defaults contain no downloaded history or verified currency/return metadata. Startup preserves existing records and does not restore removed entries; the automatic sweep downloads active indices, while manual downloads require explicit confirmation.

**Remove from catalogue** requires UI confirmation and removes an index from the shared catalogue for all users. The authenticated `DELETE /indices/{instrument_id}` endpoint is retry-safe (unknown IDs return 404). Removal retains a tombstone so curated seeds do not reappear on reads or restarts. Downloaded history, acquisition jobs and customer records are preserved; existing downloads are not cancelled. Removed indices cannot start new downloads or be read through index detail/quote endpoints. Explicitly adding the same Yahoo symbol restores the original identity, metadata and saved history.

## Full-history refresh and atomic visibility

Every manual Yahoo refresh or older-history retry requests available daily history from catalogue inception through the current UTC date. The adapter translates the inclusive end into Yahoo's exclusive end.

Automatic startup/daily jobs with existing published Yahoo history instead request **the latest saved quote date through today**, inclusively. The overlapping bar is compared on OHLC, Adj Close, currency and price basis; changed or missing overlap, missing action evidence, or changed splits in the requested range triggers a full-history fallback before publication. Initial downloads and provider transitions remain full history. Successful empty incremental checks retain the saved history and count as checked today.

A successful diff materializes a new complete immutable generation: new observations win on overlapping dates, unchanged observations are copied with original collection times and source-job provenance, and older split evidence is retained outside the downloaded range. The diff and full-fallback probe are persisted for replay. This avoids dynamic cross-generation reads and preserves pinned reports. It does **not** detect unrelated historical corrections outside the overlap; use manual full refresh to retrieve those. No extra full-history action request is made for ordinary diff jobs; actions come from the exact persisted incremental chart.

Each request creates an immutable **generation** identified by its job UUID:

1. Persist the chart-metadata profile, complete sanitized chart/action evidence, normalized quotes and split snapshot under that UUID. This profile is Yahoo chart metadata, not a claim to all optional quote/fund modules. Split evidence is projected from the same persisted chart response, including after process restart; it is never refetched from a different adjustment vintage.
2. Preserve nulls, unusual numeric values and all evidence. Index usable calendar dates; retain unindexable rows in raw evidence. Do not silently repair prices or infer missing values.
3. After the required downloads/indexing succeed, retain an immutable `market_data_publications` audit record, then atomically mark the owned/running job published. Only that owner/status-guarded job transition grants visibility; a stale worker's audit insert alone cannot publish anything.
4. Reads select **one published generation**, not the newest observation independently for each date. A shorter manual full-history response does not silently borrow dates from older generations or another provider. Automatic incremental generations explicitly materialize their pinned base history before publication.
5. A failed/empty refresh leaves the previous published data readable. Failed raw evidence remains retained; an initial failed attempt does not appear as an acquired instrument.

The resolved Yahoo listing and evidence are checkpointed on the job; the active listing cache changes only after publication, so failed replacements retain the previous mapping. Cache writes verify publication order and compare the observed listing/job version atomically. Redelivery of an already cached job is a no-op, preserving the completed watermark.

Publication is idempotent under database acknowledgement loss/redelivery. Quotes are immutable per job/date. The unique active-job gate, renewable worker lease and persisted checkpoints remain. Provider failures are terminal until another manual request or automatic sweep; database failures may retry from saved evidence. At-least-once execution does not guarantee exactly-once HTTP across a crash before response persistence.

`as_of` selects publications known at the supplied timezone-aware cutoff as well as collected observations. Details and the first quote page return an immutable `generation` token (or explicit `legacy` when no Yahoo publication exists). Clients reuse both generation and server cutoff for later pages; timestamp alone is not a safe cursor when publication writes are delayed. Instrument chart/statistics requests pin the detail generation too. Catalogue, operational job state and manual currency metadata remain current, not historical snapshots.

Coverage is the returned range, not a guarantee of every trading session or since-inception completeness. Missing rows and unexpectedly short histories remain visible through coverage metadata. No exchange calendar is assumed.

## Prices, actions and analytics

Requests use daily `auto_adjust=False`, `back_adjust=False`, `repair=False`, `rounding=False`, `keepna=True` and actions. **Yahoo's OHLC and Close are already split-adjusted even with `auto_adjust=False`.** They are not historical as-traded raw prices. `Adj Close` is separately retained and can also reflect cash distributions.

Normalized Yahoo rows declare `price_basis=provider_split_adjusted`. Statistics, trailing-year return/volatility, simulations and both calibration modes copy valid Yahoo **Close** into the existing derived `split_adjusted_close` field. They **do not multiply split factors again**, apply legacy manual split overlays, or fall back to Adj Close when Close is invalid. Thus the existing split-only, non-dividend-reinvested analytics contract is retained. Yahoo corporate-action records are audit evidence, not instructions to adjust its Close twice.

Manual split additions are unavailable for Yahoo's already-adjusted price basis. Legacy manual records remain legacy evidence. A provider change is not permission to reinterpret old manual corrections. Charts label Yahoo prices as provider split-adjusted. Historical price revisions can alter recalculated statistics; immutable saved reports do not change.

Exact strings preserve the numeric tokens supplied in captured JSON where possible; they do not recover precision already lost upstream. Missing volume remains null, not a yfinance-filled zero. Unknown listing currency remains unknown; fund currency is not substituted. Manual currency labels do not perform FX conversion. Portfolio accounting still has no automatic corporate-action share transactions: do not assume a provider switch adds split-aware holdings/accounting support.

## Storage

- `instrument_state`: cached listing and resolution, optional manual currency label, legacy watermark.
- `acquisition_jobs`: provider, declared basis, frozen request bounds, owner/lease, counters/checkpoints, status and sanitized errors.
- `instruments`: append-only provider metadata; Yahoo profiles become inventory-visible only after generation publication.
- `raw_eod_pages`, `raw_split_pages`: sanitized original evidence plus normalized page envelopes. Never store cookies, crumbs, auth headers or credential-bearing URLs.
- `quotes`: immutable dated projections with job/provider provenance and declared price basis.
- `split_snapshots`: complete normalized action evidence for a job.
- `market_data_publications`: immutable job/provider/listing publication audit records. The owner-fenced `acquisition_jobs.published` marker is authoritative for visibility.
- `instrument_statistics`: derived cache; generation and model version are part of its signature.

The experimental transition resets acquired data and experimental customers, goals, portfolios and reports **only after explicit authorization**; users, ETF catalogue/import markers, source and configuration are retained. There is no automatic destructive reset at startup. Old saved data must not be mixed into a fresh provider dataset.

## HTTP contract (authenticated; browser prefix `/api`)

- `GET /instruments/search?q=...&limit=15`: local latest-catalogue autocomplete.
- `GET /instruments/acquired?limit=20&offset=0`: published inventory with quote coverage and cached historical/statistical metrics.
- `GET /instruments/budget`: Yahoo returns `{provider:"yfinance", metered:false, month, used:0, limit:null, remaining:null}`. Null limits are not exhausted credit. The header shows the experimental provider instead of a paid quota counter.
- `GET /instruments/{isin}[?as_of=...]`: catalogue, listing, resolution, profile, acquisition, summary, manual currency, `generation`, `provider`, `price_basis`, `provider_configured:true`, `budget:null`.
- `GET /instruments/{isin}/quotes?limit=1000&offset=0[&as_of=...&generation=...]`: one published generation in ascending date order; `{items,total,limit,offset,as_of,generation}`. Reuse both snapshot fields. Numeric fields serialize as strings; nulls survive.
- `GET /instruments/{isin}/statistics[?generation=...]`: statistics for that same published generation; unknown/unpublished generation IDs fail rather than selecting a newer one.
- `POST /instruments/{isin}/acquisitions`: `{confirmed:true, mode:"update"|"backfill"}` with an optional Yahoo `symbol` override and optional `exchange` MIC. Omitting MIC detects the exchange asynchronously from exact-symbol chart metadata; exchange without symbol is invalid, and legacy Marketstack still requires both. Returns 202 and a durable job; Yahoo jobs declare `mode:"refresh"`. Duplicate active requests return the same job.
- `PATCH /instruments/{isin}/price-currency`: existing explicit EUR/USD/CHF/GBP/RUB label contract; Mongo-only.
- `/instruments/{isin}/splits`: saved audit evidence; Yahoo manual-add attempts return 409 rather than double-adjusting prices.

## UI and verification

The header identifies Yahoo Finance as experimental. Request forms require explicit consent to fetch Yahoo data, explain full-history refresh, allow a manual Yahoo symbol with optional advanced MIC entry, explain invalid inputs, and do not estimate paid quota. Bulk updates still capture all acquired pages before submission and skip profiles collected less than 24 hours ago. No POST is retried automatically after uncertainty.

While refresh is running or fails, previous published charts/statistics remain saved. Errors remain actionable. Provider/basis and collection metadata are available in expandable details. Yahoo's split handling is explained instead of offering a manual split form.

Offline tests cover adapter normalization/errors, no-network reads, full refresh, publication isolation, failed-refresh preservation, stable `as_of` pagination, same-count generation cache invalidation, no double split adjustment, separate Adj Close and nulls, and database replay. Optional tests use disposable real MongoDB databases. Real UI acceptance is performed with terminal-browser; live provider probes are explicit and bounded, never part of the default automated suite.
