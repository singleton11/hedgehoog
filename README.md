# Hedgehoog

React/Vite frontend and FastAPI backend, with MongoDB users, JWT authentication, ETF search, a separate index catalogue, and on-demand Yahoo Finance acquisition via `yfinance` through Celery/RabbitMQ.

## Run locally

Requires Python 3.13+, [uv](https://docs.astral.sh/uv/), Node.js 20.19+ (or 22.12+), and Docker with Compose. Start Docker first, then:

```sh
make run
```

This creates a gitignored `.env` with a random JWT secret (without overwriting existing configuration), starts MongoDB, RabbitMQ and a Dockerized Celery worker, waits for them to be healthy, installs frontend dependencies, and starts both applications. The worker image is built from `backend/Dockerfile` using the locked Python dependencies.

- Frontend: http://localhost:5173
- Backend/API docs: http://127.0.0.1:8000/docs
- RabbitMQ management: http://127.0.0.1:15672 (local default: **hedgehoog** / **hedgehoog-local**)
- Initial local login: **admin** / **admin**

Passwords are stored as Argon2 hashes, never plaintext. The admin account is created on backend startup only if its login does not already exist. Restarting never resets an existing password or role. MongoDB data lives in the `mongodb_data` Docker volume.

```sh
make setup          # Create .env only; edit it before first startup to change defaults
make services-up    # Build/start MongoDB, RabbitMQ and Celery worker
make run-backend    # Start services and backend
make run-frontend   # Start frontend only; backend must already be running
make import-etfs    # Start services and enqueue an ETF import manually
make services-down  # Stop worker, broker and MongoDB without deleting data
make db-up          # Start MongoDB only (e.g. for database tests)
make db-down        # Stop MongoDB only
```

Ctrl+C stops the application servers, not the Docker services; queued imports can continue. Use `make services-down` to stop all three services. MongoDB listens only on `127.0.0.1:27017`, with authentication enabled. Changing MongoDB initialization credentials in `.env` does not change credentials in an existing volume; update the actual database user too. Keep `MONGODB_URI` in sync with those credentials (URL-encode special characters).

RabbitMQ's authenticated AMQP port (`5672`) and management UI (`15672`) also listen only on localhost. Broker state and persistent queued tasks live in `rabbitmq_data`; its stable hostname preserves the RabbitMQ node identity across container recreation. Changing `RABBITMQ_USER`/`RABBITMQ_PASSWORD` does not rotate users in an existing volume. Keep `CELERY_BROKER_URL` in sync with the broker credentials. Existing development `.env` files work with the new defaults without being rewritten.

The host API uses localhost URLs. Compose overrides the worker's URLs to use `mongodb` and `rabbitmq` service hostnames. For custom/external services or credentials needing URL encoding, set `WORKER_MONGODB_URI` and `WORKER_CELERY_BROKER_URL` explicitly. API and worker must use the same database name and broker/vhost. The worker does not auto-reload code: rerun `make services-up` after backend changes.

MongoDB 7.0 is used because MongoDB 8's allocator is [incompatible with Linux kernels 6.19–7.0.13](https://www.mongodb.com/docs/v8.0/release-notes/8.0/), including the current local Docker Desktop kernel.

## ETF catalogue

On **backend startup** (including development reloads), after database health checks
and admin seeding, the API publishes the Celery task **`etfs.import_daily`** to
RabbitMQ's durable **`etfs`** queue. It does **not** download the feed or wait for
the import to finish. The Celery worker checks today's import status and, if
incomplete, downloads [the etfdb ETF feed](https://raw.githubusercontent.com/albertored/etfdb/refs/heads/main/json/etfs.json)
and stores a daily snapshot in MongoDB's **`etfs`** collection.

The API can serve requests while the catalogue is empty or an import is failing.
A stopped worker does not prevent startup: the task waits in RabbitMQ. A failed
publish **does** fail startup after bounded publish retries, rather than silently
losing the import request. MongoDB must still be available for API authentication.

- Each imported ETF has an **`import_date`** string (`YYYY-MM-DD`, UTC). One document per **ISIN + import date** is enforced by a unique compound index. The date is captured once at the start of each worker attempt, even if that attempt crosses midnight. A queued task or retry that starts on a later day imports that later day's snapshot, not a backdated one.
- **`imports`** tracks one document per UTC date, enforced by a unique `date` index: `{ "date": "2026-09-17", "etfs": false }`. The marker is created **before downloading**, and `etfs` becomes `true` only after all ETF writes succeed. Unrelated import-status fields are preserved.
- If today's `etfs` is **`true`**, the worker skips the download and ETF writes; GitHub need not be reachable. If the marker is missing or incomplete, the worker attempts the import. A failed/interrupted run remains incomplete, including when ETF writes succeeded but saving completion failed.
- Retries replace **today's** matching documents without changing their MongoDB `_id` or creating duplicates. Earlier days' snapshots remain untouched; a task executed the next day creates a new snapshot and status record. An unfinished prior day is not automatically backfilled.
- All source fields are preserved, including holdings, country/sector allocations, nulls and source update dates. Replacements remove fields dropped upstream. `_id` and `import_date` are reserved local fields and rejected if supplied by the feed.
- ISINs absent from a later feed are **not deleted**, including records written by an earlier partial attempt that day. No UCITS filtering or completeness checks are applied beyond importing the supplied source.
- The entire feed must be a non-empty JSON array of objects with valid, distinct ISINs before any **ETF document** writes occur. HTTP, validation or database failures fail the task, not the API; the incomplete status permits a retry.
- Existing installations automatically replace the old ISIN-only unique index with the compound index. Legacy **undated documents are retained unchanged** because their original import date is unknown; they are not treated as today's snapshot.
- HTTP timeouts are 10 seconds for connection establishment and 60 seconds for other network operations. A writable database is always required; GitHub access is needed only for an incomplete day. Bulk writes are not transactional, and the completion flag is **not an exclusive lock** between simultaneous importers. Compose runs one worker with concurrency **1**, consuming the `etfs` and `marketdata` queues; do not scale catalogue imports up without adding distributed import locking. On-demand market-data acquisitions have their own per-instrument gate and worker lease.

The task retries import/database failures up to **5 times** after its initial attempt,
with exponential backoff starting at 30 seconds, capped at 10 minutes, and jitter.
After exhaustion, it stays incomplete; fix the cause and run `make import-etfs`
or restart the API to enqueue another attempt. Messages are persistent, JSON-only,
and acknowledged after task execution; an abruptly lost worker process causes
redelivery. Delivery is **at least once**, not exactly once. A fresh MongoDB client
is opened and closed within each worker attempt, never shared across worker forks.
No Celery result backend is required: `imports` is the durable completion record,
and worker logs show task IDs, retries and failures.

```sh
make import-etfs                           # Prints the queued task ID
# Equivalent, with services already running:
docker compose exec celery-worker python -m celery -A celery_app:app call etfs.import_daily
docker compose logs -f celery-worker      # Watch imports/retries/failures
```

Startup and explicit manual dispatch are the only catalogue-import triggers. There is no Celery Beat or scheduled refresh. A continuously running server will not automatically import at midnight.

## On-demand instrument data

After sign-in, the workspace shows **Acquired ETFs** with an **Add ETF** button above the table: one entry per ETF with a saved provider profile, newest profile collection first, with the saved listing, grouped **Historical → Annual return / Annual volatility** and **Statistical → Annual return / Annual volatility** columns, distinct quote count and date range. Historical yearly return is the actual trailing 12-month price change ending at the latest saved quote (`latest / year-ago − 1`), not an annualized estimate. Hover over it for the endpoint dates and adjusted/unadjusted price basis. A year-ago quote may be up to seven days before the calendar anniversary; insufficient or incomparable endpoints show **Unavailable**. Historical annual volatility is the sample standard deviation of usable daily log returns in that trailing year, multiplied by √252. It requires year-long saved coverage and at least two usable daily returns; hover for the window, return count, and price basis. Sparse or missing-weekday intervals are excluded, not treated as daily moves. Statistical annual return/volatility retain the same all-history log-based statistics as instrument details. All values are percentages; positive returns have a green **+** sign and negative returns are red with a **−** sign. Zero returns and volatility remain neutral; missing estimates show **Unavailable**. Statistics are refreshed from saved prices for the current page only, without provider requests. Yahoo instruments become visible only after a complete history generation is published; failed refreshes retain the previous published data. Open any ETF from the list or page through results. Returning from instrument details refreshes the list.

Choose **Add ETF** to open the dedicated search page (`#/add`), then search the local ETF catalogue by name, ticker or ISIN. Selecting a result opens its details, where you can explicitly request data. Catalogue search is no longer displayed above the list or instrument details; the goal planner selects instrument parameters from the acquired universe only. Search, acquired-list reads, page reads, polling and charts use MongoDB only: browsing never contacts Yahoo.

1. The default `MARKET_DATA_PROVIDER=yfinance` needs **no API key or paid monthly budget**. `MARKET_DATA_MAX_CALLS_PER_JOB=40` and `MARKET_DATA_TIMEOUT_SECONDS=15` are safety bounds, not subscription limits. Rebuild/restart the worker with `make services-up` after this change. The header identifies Yahoo Finance as experimental.
2. Explicitly authorize acquisition. An uncached listing is resolved using Yahoo Search and known exchange identifiers; the selected symbol/MIC and evidence are cached. This heuristic is not independent ISIN certification. A Yahoo symbol override is available before quotes exist, such as `SPYI.DE`; the worker detects the exchange from that exact symbol’s Yahoo metadata. Exchange MIC is an optional advanced override, not required knowledge. Once quotes are saved, the listing is fixed.
3. **Request Data** queues `marketdata.acquire`. The worker saves metadata, daily chart evidence and split evidence from inception through the current UTC date; providers return only completed trading-day bars. Coverage is what Yahoo actually returns, not a promise of complete history.
4. **Every Yahoo update refreshes full available history**, including older dates, because Yahoo can revise historical split/dividend adjustments. A completed generation is published as a whole; reads never splice its dates with another vintage. Failed/empty refreshes leave previous published data active.
5. **Retry Older History** also performs a full refresh. If Yahoo lacks early history, it cannot invent it. Provider errors are sanitized and require an explicit retry; database interruptions resume persisted checkpoints.

`yfinance` is unofficial experimental access without an SLA. Free access does not grant commercial redistribution rights. Before onboarding customers, replace/license the provider (planned: EODHD) behind `backend/market_provider.py`. The legacy Marketstack adapter is retained only for explicit compatibility/testing; it is never an automatic paid fallback.

Storage:

- **`instruments`**: append-only tickerinfo observations, including complete nested provider `data`, `instrument_id` (ETF ISIN or separate index ID), provider symbol/exchange, job ID, exact UTC `collected_at` and UTC `collection_date`. Multiple observations in a day are retained. Point-in-time means *known when collected*, not historical fundamentals reconstructed before collection.
- **`raw_eod_pages`**: complete parsed provider envelopes, keyed by job and offset, saved before indexing. Retains all rows, including duplicates, malformed dates, unexpected shapes and missing values for later cleanup. Re-indexing a saved page after a worker/DB interruption does not consume another provider call.
- **`quotes`**: dated-object projection (first occurrence per job/date; all duplicates remain in raw pages); quote date separate from `collected_at` / `collection_date`, original raw and adjusted provider fields in `data`, listing and job ID. Observations are immutable; retries of the same job/date are idempotent. Yahoo reads select one completed published generation, optionally bounded by `as_of`, without filling its missing dates from older generations.
- **`acquisition_jobs`**: durable status, progress, sanitized errors, requested range and pagination checkpoint. A partial unique index allows only one active job per instrument. A five-minute renewable worker lease fences abandoned work; polling exposes expired jobs as retryable failures. Queued requests not started within 30 minutes are also exposed as retryable failures, covering offline workers or an interrupted Mongo-to-broker handoff; any later delivery of an expired job is skipped. Delivery is at least once: a crash between a provider response and its checkpoint can require another request, so HTTP is not exactly once.
- **`instrument_state`**: cached listing/resolution and optional manual currency label. Earlier records remain available if an update fails.
- **`market_data_publications`**: immutable generation/job/provider/listing publication audit records. Visibility requires an atomic owner/status-guarded publication marker on the job, so expired workers cannot publish late. Profile, quote and split reads share the same generation; statistics cache keys include its identity.

**Ingestion preserves provider evidence; cleanup is deferred.** Missing/null/unusual prices and inconsistent OHLC remain saved. Raw chart responses are retained before indexing; only usable dates enter dated history. Identity, transport, envelope, explicit authorization, lease and bounded-request safeguards fail safely. No silent price repair or invented null-volume zeros are applied. Provider credentials/session cookies are never published.

Yahoo charts use **provider split-adjusted OHLC**, even though requests set `auto_adjust=False`. Statistics, trailing-year metrics, simulations and calibration use Yahoo **Close**, copied into the separate `split_adjusted_close` analytics field without multiplying split factors again. `Adj Close` remains separate; cash distributions are **not** reinvested in analytics. Manual split multiplication is disabled for this already-adjusted basis. Updates publish coherent refreshed histories; saved calculation reports remain immutable.

Numeric JSON values are serialized as strings without pretending to recover precision lost upstream. Nulls are retained. Only chart rendering converts prices to JavaScript numbers. Unknown listing currency is not replaced by fund currency. **Price currency → Save currency** can label unknown prices as EUR, USD, CHF, GBP or RUB; this does not convert amounts or rewrite observations. Portfolio accounting still does not automatically adjust share quantities for corporate actions.

Authenticated endpoints (browser prefix `/api`):

| Route | Purpose |
| --- | --- |
| `GET /instruments/search?q=...` | Latest-snapshot autocomplete, no provider calls |
| `GET /instruments/acquired?limit=20&offset=0` | Paginated acquired ETFs with historical yearly return, annual return/volatility, saved profile and quote coverage, no provider calls |
| `GET /instruments/{isin}[?as_of=...Z]` | Catalogue, published profile, coverage, job and provider/basis |
| `GET /instruments/{isin}/quotes?limit=1000&offset=0[&as_of=...Z&generation=...]` | Published-generation candles, ascending and paginated |
| `PATCH /instruments/{isin}/price-currency` | `{price_currency: "EUR" \| "USD" \| "CHF" \| "GBP" \| "RUB"}`; saves a manual chart currency override |
| `POST /instruments/{isin}/acquisitions` | `{confirmed: true, mode: "update" \| "backfill"}`; optional Yahoo `symbol` manual override with auto-detected exchange; optional explicit `exchange` MIC; returns 202 and durable job |

Quote pagination reuses both the returned `generation` UUID (or explicit `legacy`) and `as_of` cutoff. The generation prevents delayed publications from changing the selected history between pages. `as_of` requires an explicit timezone and filters **collected market-data observations**; the catalogue, manual price-currency setting and operational job/provider status are still current. See [the design contract](docs/on-demand-market-data.md).

## Index catalogue

Open **Indices** (`#/indices`) for a separate, small starter catalogue stored in
MongoDB's `indices` collection. Add further indices by exact Yahoo symbol and name;
currency and price/net/gross total-return variant may remain unknown. Saving a
catalogue record does not download data or certify the supplied metadata.

Use **Download data** or **Refresh data**, then confirm, to queue an explicit Yahoo
acquisition. The table displays status and saved quote count/date range. Downloads
reuse the durable acquisition pipeline, and a failed refresh keeps the previous
published history. Reads and polling never call Yahoo. Rebuild the worker with
`make services-up` after updating backend code.

This is not a complete global index directory. Indices have separate internal IDs,
not fabricated ETF ISINs, and remain excluded from ETF inventory and portfolio
selectors. Benchmark comparisons and ETF-history proxy assignments are deferred.
Identity verification does not establish currency or total-return semantics, and
Yahoo data availability does not establish redistribution rights.

## Portfolio tracking

Open **Customers → a customer → Portfolios** to create multiple named EUR portfolios.
Link a goal now or attach/change/detach it later. Add dated deposits, withdrawals,
buys and sells manually; dates default to today (UTC). Only confirmed EUR-priced
acquired listings are supported. Cash must be funded and positions cannot be short,
including throughout a backdated transaction's full timeline.

Holdings show quantity, average cost excluding commissions, saved-price value and daily/total
unrealized PnL; portfolio totals include cash, equity and realized PnL. Buy and sell
commissions reduce realized PnL immediately on the trade date. Missing prices
remain unavailable, and quote dates/staleness are explicit. Transactions use
weighted-average cost, with idempotent retries and atomic concurrency protection.
Use **Remove** in transaction history to correct an entry after confirmation;
removal recalculates balances and is blocked if any remaining cash or holdings
would become negative. See [portfolio accounting, limits and API](docs/portfolio-tracker.md).

The **Performance** tab has a cumulative TWR chart and a separate shared Sharpe/Sortino
chart; the portfolio summary shows all three latest metrics. In **Settings**, admins
choose an app-wide saved EUR instrument as the return proxy for both ratios. No
selection or insufficient history leaves ratios unavailable. See
[performance methodology and settings](docs/portfolio-performance.md).

## Return statistics and goal planning

After EOD acquisition, Hedgehoog calculates each instrument's average daily log
return and sample log-return variance (not simple returns or price levels). The
statistics display and Monte Carlo model use these same log-return estimates.
Existing downloads are calculated lazily without spending provider quota.
The instrument page shows **Daily return**, **Daily volatility**, **Annual return**
and **Annual volatility** as percentages. Returns remain log-based; volatility
is the square root of variance, with annualization using 252 trading days.

Open **Customers** for your customer list. Create a customer (name only),
then add named goals with EUR targets and dates. Open a goal and choose **Edit
goal** to change its name, target or date. Saved simulations retain their original
settings and results. Each goal can hold multiple saved simulations.
Choose **New simulation**, configure starting
capital, reliability (50–99%), a strategy and simulation method, then calculate.
Standard Monte Carlo is the default; block bootstrap resamples contiguous observed
daily log returns. Choose a 5-, 10- (recommended), or 20-trading-day block or
enter a custom size from 1 to 60. Saved plans retain these settings for tracking. Click **Save
simulation** to keep that exact result; calculation alone does not add it to
history. Saved reports are read-only and private to your account.

The first strategy, **Buy and hold single instrument**, has one required
**Instrument** parameter selected from your acquired universe (not the full ETF
catalogue). Its saved history must be EUR-priced. A repeatable
10,000-path Monte Carlo simulation estimates the fixed monthly contribution
needed, plus model success probability and terminal-value percentiles. A projection
graph shows average portfolio value as a line, shaded 5th–95th and 25th–75th
percentile clouds across all 10,000 traces, and the goal as a dashed line. It requires
at least 60 usable daily returns; monthly payments start one month from today.
An optional timezone-aware `as_of` timestamp on simulation requests enables
historical-origin estimates using only quote observations and published history
known by that cutoff. Normal current simulations also pin statistics and their
forecast date to one captured UTC timestamp (`data_as_of` in the result).

This is a **model estimate, not a guarantee**. It uses historical log returns,
assumes fractional shares and (for standard Monte Carlo) independent normal
returns, and excludes inflation, taxes
and transaction fees. Unknown or conflicting currencies must be resolved; a EUR
label does not convert non-EUR history. See [model, data rules and API details](docs/goal-planner.md).
Rebuild the worker with `make services-up` after updating backend dependencies.

Customer persistence uses authenticated `/customers` and
`/customers/{customerId}/goals` list/create APIs, plus
`PATCH /customers/{customerId}/goals/{goalId}` for goal edits.
A goal's `/simulate` endpoint
previews a calculation; `/simulations/{simulationId}/save` explicitly retains
it without recalculating. Goal `/simulations` lists saved summaries, and
`/simulations/{simulationId}` reads a full report. Unsaved previews expire after
24 hours. See [customer, goal and simulation API details](docs/goal-planner.md#customers-goals-and-saved-simulations).

Standalone compatibility endpoints: `GET /instruments/{isin}/statistics`,
`GET /goals/strategies` (strategy catalogue and typed parameter definitions), and
`POST /goals/simulate` with `{strategy, goal_eur, starting_capital_eur,
goal_date, reliability}` and optional `simulation_method`, `block_size`. A strategy selection is
`{id: "buy_and_hold_single_instrument", parameters: {instrument: "IE00B4L5Y983"}}`.
The response echoes `strategy` and returns `parameter_statistics` keyed by
parameter name. Reliability is a fraction, e.g. `0.90`; money outputs are decimal
strings. These endpoints never call the provider or acquire missing instruments.
Existing `#/goal/{isin}` links ask you to select a customer and goal before
prefilling this instrument, subject to acquired-universe membership. See [extension points and API details](docs/goal-planner.md#strategies-and-typed-parameters).

## Calibration

Open **Calibration** (`#/calibration`) for your saved calculation history, with
**Brier score** and **calibration error** (weighted average gap from ideal
calibration, shown in percentage points). **New run** lets you choose a strategy,
set its parameters, select Standard Monte Carlo or block bootstrap with a block
size, and run the calculation. Each successful run keeps its own
report, settings, instrument and seed. Open any report and choose **Run again**
to prefill a new calculation without overwriting the original. Existing saved
latest results are preserved in the history.

Each run generates 1,000 varied financial goals (10–5,000 configurable). Targets
use training-only simulations of the selected method; independent draws of
that same method estimate probabilities before historical outcomes are read. Details include the calibration curve, origin
count and paginated audit. Provided or generated seeds support replay.

Each monthly forecast fits only its trailing historical window; later prices
are used only to score the outcome. This is walk-forward fitting on downloaded
history, **not** strict point-in-time vintages: later adjustments, present-day
instrument selection and overlapping holding periods remain limitations.
Calculations use saved data only and do not spend provider quota.

The authenticated `POST /goals/calibrate` accepts generated batches only, with
`mode: "random"`, count, horizon range, optional date bounds/uint32 seed and
simulation method/block size.
`GET /goals/calibrations` lists paginated run summaries;
`GET /goals/calibrations/{runId}` retrieves an immutable saved report. The strategy
catalogue and latest-result endpoints remain available for compatibility. See
[methodology, historical execution rules, API and limitations](docs/strategy-calibration.md).

## Authentication and authorization

The frontend sends same-origin requests through Vite's `/api` proxy, which strips `/api` before forwarding to FastAPI.

| Backend route | Access | Purpose |
| --- | --- | --- |
| `GET /health` | Public | Process liveness |
| `POST /auth/login` | Public | JSON `{ "login": "admin", "password": "admin" }`; returns public user data and sets JWT cookie |
| `GET /auth/me` | Authenticated | Current user ID, login and role |
| `POST /auth/logout` | Public/idempotent | Clears JWT cookie |
| `GET /` | Authenticated | Workspace greeting |
| `GET /admin` | Admin only | Administrator access check |

JWTs expire after 60 minutes by default and are stored in an **HttpOnly, SameSite=Strict cookie**, not local/session storage or JavaScript-visible response bodies. The frontend checks the session on startup and window focus, handles API `401` responses by returning to login, and hides admin content from non-admin users. Backend dependencies enforce authentication and admin roles independently; deleted/disabled users lose access immediately. Unsafe browser requests must have an allowed Origin, preventing cross-origin login/logout actions.

There is no public registration endpoint. There are no refresh tokens; sign in again after expiration. Logout removes the browser cookie, but an already copied JWT remains valid until its expiration (or the account is disabled/deleted or the signing secret is rotated).

## Configuration and deployment

See `.env.example` for all settings. The backend loads the root `.env` regardless of its working directory; environment variables override the file.

**`admin:admin` is for local development only. Before deploying:**

- Set `ENVIRONMENT=production`, a strong `ADMIN_PASSWORD` (at least 12 characters), and `COOKIE_SECURE=true`. HTTPS is required. Startup rejects the default password, including an existing seeded admin still using it.
- Use a fresh production database, or rotate the existing admin password securely before deployment. Changing `ADMIN_PASSWORD` alone does **not** reset an existing account.
- Keep a high-entropy, private `JWT_SECRET` of at least 32 characters, shared across backend workers. `make setup` generates one. Rotating it invalidates all sessions.
- Set `ALLOWED_ORIGINS` to a JSON array containing only your exact frontend origin, e.g. `["https://hedgehoog.example.com"]`.
- Serve the built frontend and `/api` on the same origin, with a reverse proxy forwarding `/api/*` to the backend and stripping `/api`. A static host alone does not provide Vite's development proxy.
- Use strong MongoDB credentials and a dedicated, least-privileged application database user; the local Compose root credentials are development-only. Do not expose MongoDB publicly.
- Replace the development RabbitMQ credentials, use a dedicated broker user/vhost, and keep AMQP and the management UI private. Use TLS (`amqps://`) when connecting across untrusted networks.
- Add login rate limiting at your reverse proxy before exposing the app publicly.

## Tests

```sh
make test           # Backend tests, frontend component tests, production build
```

The default backend suite uses MongoDB/broker doubles and mocked ETF HTTP responses (no GitHub access). It covers startup dispatch, worker client lifecycle and bounded retries as well as the import contract. To also run the real-MongoDB integration checks:

```sh
make db-up
cd backend
TEST_MONGODB_URI='mongodb://hedgehoog:hedgehoog-local@127.0.0.1:27017/?authSource=admin' uv run pytest
```

Use your configured URI if credentials differ. The integration tests use and remove their own uniquely named databases, checking hashed credentials, unique logins, seed idempotence, sessions surviving a backend restart, and ETF daily snapshots, interrupted-run recovery, completion skips, compound uniqueness and legacy index migration. They invoke the worker task directly with synthetic ETF HTTP responses, not the live feed.

To additionally test real RabbitMQ delivery, start the services and supply both URLs:

```sh
make services-up
cd backend
TEST_MONGODB_URI='mongodb://hedgehoog:hedgehoog-local@127.0.0.1:27017/?authSource=admin' \
TEST_CELERY_BROKER_URL='amqp://hedgehoog:hedgehoog-local@127.0.0.1:5672//' uv run pytest
```

The broker test uses its own UUID-named queue/database and a test worker. It verifies
that API startup queues while the worker is offline, a transient download failure
retries successfully, and a duplicate task skips a completed day. Test queues and
databases are removed afterward; it never imports into the application database.
