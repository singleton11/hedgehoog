# Backend

FastAPI, MongoDB and Celery backend, managed with [uv](https://docs.astral.sh/uv/).

## Install uv

On macOS with Homebrew:

```sh
brew install uv
```

For other platforms, see the [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).

## Run locally

From the repository root:

```sh
make run
```

This starts both the backend and frontend development servers with automatic
reload. It requires uv, Node.js (see `frontend/package.json` for supported
versions), and npm. Frontend dependencies are installed automatically with
`npm ci`; uv installs backend dependencies as needed. Press `Ctrl+C` to stop
both servers; if either server exits, the other is stopped too. MongoDB, RabbitMQ
and the Dockerized Celery worker remain running; `make services-down` stops them.

- Frontend: http://localhost:5173

To run only the backend, from the repository root:

```sh
make run-backend
```

Or run the commands directly:

```sh
make services-up  # From the repository root: MongoDB, RabbitMQ and worker
cd backend
uv sync --locked
uv run uvicorn main:app --reload
```

uv uses Python 3.13 (pinned in `.python-version`), downloading it if needed.
`uv sync --locked` creates `.venv` and installs dependencies from `uv.lock`,
checking that the lockfile matches `pyproject.toml`. No manual environment
activation is needed. `uv run` also syncs dependencies automatically, so on
subsequent runs you can just use the last command.

Stop the server with `Ctrl+C`.

After database checks and admin seeding, startup queues `etfs.import_daily` via
RabbitMQ and starts serving requests without waiting for ingestion. A failed broker
publish fails startup; an import failure does not. The Celery worker checks MongoDB's
`imports` collection for today's UTC `{date: "YYYY-MM-DD", etfs: true}` and skips
completed days. Otherwise, it records `etfs: false`, downloads the
[etfdb ETF catalogue](https://raw.githubusercontent.com/albertored/etfdb/refs/heads/main/json/etfs.json),
and writes `etfs` documents keyed by unique `(isin, import_date)`. Only after all
writes succeed is the day's flag set to `true`. Failures retry up to five times
with exponential backoff and jitter; each attempt uses its execution day's UTC
date. Previous snapshots, legacy undated records and missing ISINs are retained.
This is not a scheduled midnight refresh; Celery Beat is not configured.

From the repository root:

```sh
make import-etfs                       # Manually queue an import/retry
make services-up                       # Rebuild worker after backend code changes
docker compose logs -f celery-worker  # Task IDs, skips, retries and failures
```

The worker uses `python -m celery`, which keeps the backend directory on Python's
import path for modules loaded during task execution (including return statistics).
Use this module launcher rather than the `celery` console script; rebuilding alone
does not fix an incorrect launcher. Run `make services-up` to rebuild and recreate
the worker after changes.

The `celery-worker` service consumes only `etfs`, with concurrency 1 to serialize
imports and refresh sweeps. Do not run extra workers on the `etfs` queue without
adding distributed locking. The separate `celery-marketdata-worker` consumes
`marketdata` with 16 isolated prefork processes by default; set
`MARKET_DATA_WORKER_CONCURRENCY` to tune it. Both start/stop with `make services-up`
and `make services-down`. RabbitMQ persists queued
messages; MongoDB's `imports` collection tracks completion (no result backend).
`CELERY_BROKER_URL` configures the host producer. Compose overrides worker URLs to
use service hostnames; use `WORKER_MONGODB_URI` / `WORKER_CELERY_BROKER_URL` for
custom services and keep the API/worker database and broker/vhost aligned.
See the [root README](../README.md#etf-catalogue) for the full import contract.

- Hello world: http://127.0.0.1:8000/
- Interactive API docs: http://127.0.0.1:8000/docs

`GET /` returns:

```json
{"message": "Hello, World!"}
```

Try it in another terminal:

```sh
curl http://127.0.0.1:8000/
```

## Standalone ETF history coverage CSV

Survey the entire ETF reference catalogue without MongoDB, Celery, API keys, or
changes to application data. From the repository root, try ten instruments first:

```sh
cd backend
uv run python etf_history_coverage.py --output ../etf-coverage-sample.csv --limit 10
```

Run the full catalogue (currently about 4,500 ETFs) into a separate CSV:

```sh
uv run python etf_history_coverage.py --output ../etf-coverage.csv
# Continue an interrupted run, using the SAME inclusive end date as the original:
uv run python etf_history_coverage.py --output ../etf-coverage.csv --resume --end YYYY-MM-DD
```

The CSV starts with `ticker,day_closes` and includes ISIN, resolved Yahoo symbol,
status/error code, first/last valid close dates, requested dates and UTC check time.
`day_closes` counts distinct trading dates with finite positive **Close**, not
Adj Close or calendar days. Full history starts at 1970-01-01 and ends yesterday
UTC by default (avoiding the current partial daily candle); `--start` / `--end`
change the inclusive range. Failed requests have a blank count, not a misleading
zero; a successful empty response has `status=no_history` and count zero.

Catalogue tickers are not necessarily Yahoo symbols. The script reuses the
project's conservative ticker/ISIN listing resolver; ambiguous or unsupported
listings are recorded as errors rather than guessing exchange suffixes. Use
`--catalogue /path/to/etfs.json` for a local etfdb JSON snapshot. For reproducible
multi-day runs, keep that snapshot and specify a fixed `--end` from the first run.

Each row is flushed immediately. `--resume` skips all recorded ISINs, including
errors; use a new output file (optionally a catalogue subset) to retry them.
Existing files are never overwritten. Downloads use **four isolated worker
processes** by default; `--workers 8` changes concurrency and `--workers 1`
restores sequential execution. Only the parent writes CSV rows, in completion
order. Per-instrument START/DONE logs may appear out of catalogue order.
At least one second separates actual Yahoo HTTP calls **across all workers**;
`--delay 2` slows them further and `--timeout 30` changes the per-request timeout.
Parallel workers overlap network latency and processing, not the global rate
limit. Each instrument can require multiple HTTP calls, so a full run may take
many hours. Rate limits/access denials stop new requests after saving the error
row; in-flight responses may finish and unsaved instruments are retried on resume.
Shutdown can wait for an in-flight request's timeout. Yahoo is unofficial
and may throttle or deny bulk access; this does not guarantee full coverage.
Only counts and metadata are saved, not the downloaded price history.

## Instrument acquisition

The acquired ETF list offers a confirmed **Remove → Delete saved data** action.
Authenticated `DELETE /instruments/{isin}` deletes that ETF's shared profiles,
prices, provider evidence, splits, cached statistics, listing settings and acquisition
history. It retains the searchable catalogue, provider usage counters, portfolios,
and saved calculations. Live valuations may become unavailable until reacquisition.
Deletion is retry-safe and returns 204; active acquisitions/removals return 409.
An acquisition-slot mutex prevents new acquisition jobs during deletion. If the API
process is killed during deletion, its `status: deleting` job deliberately keeps
that slot blocked; an operator must verify the process has stopped before deleting
that lock record and retrying removal. Normal exceptions release the lock.

Authenticated `/instruments/search`, `/instruments/{isin}` and `/instruments/{isin}/quotes` routes read the catalogue and collected observations from MongoDB. Explicit `POST /instruments/{isin}/acquisitions` requests queue `marketdata.acquire`; the serial catalogue worker consumes `etfs` and the parallel acquisition worker consumes `marketdata`.

The default provider is Yahoo Finance via pinned `yfinance==1.7.0`; no API key or paid quota is required. `MARKET_DATA_PROVIDER=yfinance`, `MARKET_DATA_MAX_CALLS_PER_JOB=40` and `MARKET_DATA_TIMEOUT_SECONDS=15` configure the experimental adapter and safety bounds.
`YAHOO_REQUEST_INTERVAL_SECONDS=0.05` sets shared minimum spacing between Yahoo
HTTP request admissions across acquisition workers (roughly 20 starts/second, not
20 completed jobs/second). MongoDB coordinates the gate, including cookie/crumb,
redirect, listing and history calls; workers need synchronized UTC clocks and the
same database/configuration. Explicit interactive index searches and the standalone
coverage survey are outside this acquisition-only gate. A stalled process can resume late, so this is pacing,
not a strict wire-level rate guarantee. Zero disables pacing. Database failures
fail closed; job call caps and terminal provider-denial behavior are unchanged.
The successful standalone survey used the same 16-process/50-ms settings, but
Yahoo can still throttle future traffic; lower concurrency or increase the
interval if that happens. This is not a guaranteed speedup or a provider quota.
Use `docker compose logs -f celery-marketdata-worker` for download task logs.
Rebuild both workers with `make services-up` after code/dependency changes. Browsing never calls the provider. Errors are sanitized; provider failures can be retried manually or by the next automatic sweep, while database recovery replays saved checkpoints.

The worker resolves a Yahoo listing from the catalogue ticker/ISIN and known venues, saving symbol/MIC and lookup evidence. POST accepts `{confirmed:true}` and an optional Yahoo `symbol` override (for example `SPYI.DE`). Without `exchange`, the worker detects its MIC from exact-symbol Yahoo chart metadata; it never substitutes a different symbol. An explicit paired `symbol`/`exchange` override remains available, and the legacy Marketstack provider still requires the pair. Every Yahoo acquisition refreshes full available history from inception through the current UTC date, then publishes a coherent immutable generation. Failed refreshes keep previous published data readable. `as_of` quote pagination pins the publication cutoff so updates cannot splice different adjusted-price vintages.

Yahoo OHLC/Close are already split-adjusted with `auto_adjust=False`. Analytics use Close without applying split factors again or substituting dividend-adjusted Adj Close; manual split multiplication is unavailable for this basis. Preserve raw evidence and nulls. The neutral adapter contract in `market_provider.py` is the replacement point for a future licensed EODHD provider. Yahoo is unofficial experimental access, not a commercial data licence. See the [full storage and API contract](../docs/on-demand-market-data.md).

```sh
uv run pytest  # Includes provider MockTransport, acquisition and API tests; no live API calls
# Optional: set TEST_MONGODB_URI and TEST_CELERY_BROKER_URL for isolated real-service tests.
```

## Saved-history filtering

`GET /instruments/acquired?min_history_years=5|15|30` preserves latest-profile
ordering and exact totals before pagination. `saved_history_bounds` validates
publications at one request cutoff, separates legacy/pinned seeds, and runs two
correlated endpoint lookups (date ascending/descending, limit 1) per candidate.
Each aggregation carries literal selected identities through a single instruments
row; it does not reselect profiles. An inventory emptied during the read returns
409 rather than an incorrect total. Batches cap at 50 candidates and 512 KiB BSON
including conservative command overhead; oversized identities return 413.

The query reuses `(job_id,date)` and
`(instrument_id,symbol,exchange,date,collected_at)` indexes without new storage or
write costs. Provider/listing predicates remain mandatory residual filters, so
internal probes can examine extra keys, especially legacy histories surrounded by
retained Yahoo generations. Client batch count is not total internal probe count.
No history grouping, durable coverage cache, acquisition change or provider call
is introduced. Full distinct dates/counts and statistics remain page-only.

Focused offline verification (using preinstalled dependencies, no downloads):

```sh
python -m pytest tests/test_market_data.py tests/test_yfinance_acquisition.py tests/test_incremental_acquisition.py
python -m pytest tests/test_acquired_history_integration.py
```

For opt-in real-Mongo verification, set `TEST_MONGODB_URI` to an isolated
disposable test server, then run:

```sh
python -m pytest -s tests/test_acquired_history_integration.py
```

The opt-in tests run the actual correlated lookup and print endpoint results,
keys/documents examined, indexes used, batch commands and internal probe counts
for pinned and adversarial legacy histories in both directions. Server-dependent
inner-plan visibility can limit blocking-sort evidence. Mongomock's explicitly
labelled test emulator verifies payload semantics only, never index correctness.
The supplied environment has no Mongo server/container runtime, so real-Mongo 7.0
query compatibility, selective index access and timings are **not verified**.

The historical checkpoints/benchmark script referenced by issue 40 are absent
from this source snapshot; they were not reconstructed or replaced with a weaker
baseline. No new real-Mongo benchmark ran. Warm p95 <= 2 seconds and membership
speedup >= 5x versus the original pre-optimization baseline remain **unverified
targets**, not results. Before accepting the optimization, compare the original,
grouped and endpoint variants on identical deterministic 1,000-ETF fixtures,
including 5/15/30 presets, first/next pages and empty 30-year results, with at least
20 alternating warm samples per case, initial cold latency, full payload equality,
and actual per-lookup executionStats. If residual filters prevent selective access
or either target fails, return evidence for plan review; do not add indexes/caches
or widen the acquisition scope without a revised bounded task.

## Automatic market-data refresh

Startup queues `marketdata.refresh_stale` for saved ETFs and all active indices; the API does not wait for downloads. Once the catalogue import completes (including a same-day skip), its worker queues an additional sweep covering **every catalogue ETF**, deduplicated by ISIN across snapshots. Unacquired ETFs download since-inception history and existing Yahoo histories refresh incrementally. Invalid metadata is skipped and failed mappings remain failed jobs; the initial catalogue-wide download may take a long time or encounter provider throttling. Failed import/sweep dispatches retry with bounded backoff.

One Compose `celery-beat` scheduler refreshes saved ETFs and active indices daily at **02:00 UTC**. Successful selected-provider checks through today or published Yahoo quotes for today suppress repeated requests within the current UTC freshness window: midnight until 02:00, then 02:00 onward, based on acquisition request time. Yahoo success must have published its generation. Failed attempts do not reset freshness, and removed indices remain excluded.

The sweep uses the existing per-instrument active-job gate and saved listing, preserving full-history generation publication and manual acquisitions. Invalid entries are logged without blocking other candidates. Provider failures become eligible again on the next sweep; database/queue failures receive bounded retries. Run exactly one scheduler; `make services-up` rebuilds/starts it and `make services-down` stops it. Scheduled refreshes continue while Docker services run, even after stopping the development API. See `docker compose logs celery-beat celery-worker celery-marketdata-worker` and [the design contract](../docs/on-demand-market-data.md).

## Index catalogue and explicit Yahoo history

Indices use their own MongoDB `indices` catalogue and stable `idx-<UUID>` IDs,
never fabricated ISINs or fund inception dates. Startup and lazy list/add seeding
are offline and idempotent. The small curated symbol set (`^GSPC`,
`^990100-USD-STRD`, `DE000SLA1W63.SG`) starts unverified with unknown currency and return
variant. No benchmark selection, ETF proxies, or portfolio index holdings are
included; index profiles are excluded from ETF acquired inventory and selectors.

All routes require authentication:

- `GET /indices?limit=20&offset=0` → `{items,total,limit,offset}` (limit 1–100).
- `GET /indices/search?q=MSCI%20World%20net&limit=15` → `{items}` with
  `{symbol,name,currency,return_variant}` candidates. Query: 2–100 characters;
  limit: 1–20. Performs an explicit Yahoo search using the bounded provider client,
  keeps only `quoteType=INDEX`, deduplicates symbols, and does not persist records
  or enqueue history downloads. Currency may be null; return variant is always
  `unknown` (never inferred). Provider failures return a safe retryable 503.
- `POST /indices` → record (201), body `{symbol,name,currency:null,return_variant:"unknown"}`.
  Currency is optional/null or three letters. Return variant defaults to `unknown`;
  other accepted values are `price`, `net_total_return`, `gross_total_return`.
  Symbols are trimmed/uppercased. Re-adding a symbol returns its existing record
  without overwriting metadata, including concurrent adds.
- `GET /indices/{id}` → record.
- `POST /indices/{id}/acquisitions` with **only** `{confirmed:true}` → `{job}` (202).
  Active requests deduplicate. This explicitly selects Yahoo even when ETF
  acquisitions are configured to use legacy Marketstack.
- `GET /indices/{id}/quotes` → existing `{items,total,limit,offset,as_of,generation}`
  quote contract, with exact numeric strings/nulls; supports `limit` (1–1000),
  `offset`, timezone-aware `as_of`, and stable `generation` pagination.

Records include `instrument_id,name,symbol,currency,return_variant,
verification_status,acquisition,summary,provider`; summary contains
`quote_count,first_date,last_date`. Additional fields are `provenance`, `profile`,
`generation`, and `history_request`. Verification is `unverified` until a fenced
published download establishes the exact symbol and Yahoo `instrumentType=INDEX`
in both chart profile and raw historical response. `verified` means **identity
only**, not verification of a user-supplied name/return variant/currency. Unknown
currency remains null unless the published Yahoo chart supplies a three-letter
currency; no return variant is inferred. Provenance states this verification scope.

Downloads reuse `marketdata.acquire`, per-job call limits, raw evidence/checkpoint
replay, lease fencing, and atomic Yahoo history generations. Index listing
`exchange=YAHOO` is an internal provider namespace, **not** an asserted exchange
or MIC. Full-history requests use an explicit **1900-01-01 through yesterday**
boundary, not a claimed inception or a guarantee of complete coverage. Requests
are split into non-overlapping ranges of at most 99 × 365 days to stay below
Yahoo's 100-year daily-data limit. Each response is retained separately under
`raw.history_chunks` in one logical page; split replay uses those same responses
without additional provider calls. Short ranges retain the original raw format.
A pre-listing chunk may return Yahoo HTTP 400 with an exact, request-bound
“Data doesn't exist” response. Only when its exclusive end is at or before the
same symbol's reported `firstTradeDate` is it retained as `raw.empty_history`
(metadata, date bounds, and original error response), allowing later chunks to
proceed. This is provider-reported absence, not proof of actual index inception.
Unknown inception, overlapping no-data errors, other HTTP failures and access
restrictions remain failures; no retries or fabricated bars are introduced.
A searchable symbol can still have no downloadable daily history. Failed
refreshes preserve the previous published quotes and metadata. Close-only Yahoo
indices retain null OHLC/volume/adjusted-close fields; nothing is synthesized.
Yahoo availability is not guaranteed; no network verification occurs during
seeding, browsing, or adding records.

Offline coverage: `uv run pytest tests/test_indices.py tests/test_yahoo_finance.py
 tests/test_yfinance_acquisition.py`.

## Manage dependencies

Run these commands from `backend/`:

```sh
uv add <package>          # Add a runtime dependency
uv add --dev <package>    # Add a development dependency
uv remove <package>       # Remove a dependency
uv lock --upgrade         # Update locked dependency versions
uv sync --locked          # Install the updated lockfile
```

Dependencies live in `pyproject.toml`; exact resolved versions live in `uv.lock`.
Commit both files along with `.python-version`. Do not commit `.venv`.
