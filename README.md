# Hedgehoog

React/Vite frontend and FastAPI backend, with MongoDB users, JWT authentication, ETF search, and on-demand Marketstack acquisition through Celery/RabbitMQ.

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
docker compose exec celery-worker celery -A celery_app:app call etfs.import_daily
docker compose logs -f celery-worker      # Watch imports/retries/failures
```

Startup and explicit manual dispatch are the only catalogue-import triggers. There is no Celery Beat or scheduled refresh. A continuously running server will not automatically import at midnight.

## On-demand instrument data

After sign-in, the workspace shows **Acquired ETFs** beneath catalogue search: one entry per ETF with a saved provider profile, newest profile collection first, with the saved listing, **Annual return**, **Annual volatility**, distinct quote count and date range. Annual values use the same log-based statistics as instrument details and are shown as percentages; missing estimates show **Unavailable**. Statistics are refreshed from saved prices for the current page only, without provider requests. Profile-only and partial/failed downloads remain visible; catalogue-only entries and requests that have not saved data are excluded. Open any ETF from the list, page through results, or use **Refresh list** to reload saved data. Returning from instrument details also refreshes the list.

Search the local ETF catalogue by name, ticker or ISIN to find additional ETFs. Search, acquired-list reads, page reads, polling and charts use MongoDB only: browsing never spends Marketstack quota.

1. Configure `MARKETSTACK_API_KEY` in the gitignored root `.env`, and set `MARKETSTACK_MONTHLY_BUDGET` to the local monthly cap (`10000` for the current paid plan; reduce it if the key is shared with other applications). The default **0 disables acquisition**. Rebuild/restart the worker with `make services-up`; restart the API when its settings change. Both processes must use the same key, budget and database. Never use a `VITE_` variable for this key.
2. Authorize acquisition without entering a provider symbol or MIC. If no mapping is saved, the worker searches `/v2/tickerslist?search=<catalogue ticker>` and picks the first named, EOD-enabled exact `TICKER.exchange` match in provider order with a valid `stock_exchange.mic`. For example, `WEBN` skips `WEBNF` and unnamed listings, selecting `WEBN.DE` / `XETR`. The suffix is **not** the MIC. The mapping and lookup evidence are persisted per instrument and reused without searching again. This heuristic is not an independently verified ISIN match; provider identifiers are retained as supplied without identity-validation blockers. Manual symbol/MIC override remains available before quotes exist; thereafter the listing cannot change or mix with another listing.
3. **Request Data** queues `marketdata.acquire` on the durable `marketdata` queue. The worker retrieves `/v2/tickerinfo` plus paginated `/v2/eod` with `limit=1000`, from inception through yesterday UTC. The page displays queued/running progress, failures, returned coverage, all metadata and unadjusted OHLC candles.
4. **Update Data** refreshes tickerinfo and requests dates after the last quote of the last fully processed range. Empty responses do not move this watermark forward, so late publication beyond the last received date can be retrieved next time. Failed ranges do not advance it. There is no market-calendar inference or automatic repair of interior historical gaps/corrections.
5. **Retry Older History** explicitly retries the gap between inception and the earliest stored quote. Normal updates do not repeatedly request inaccessible older data. The Free plan can silently return only one year even for a since-inception request with HTTP 200; the page shows the saved coverage status and date range rather than claiming a full backfill. Provider/exchange coverage may also start later than fund inception. Historical access requires the appropriate plan.

An uncached acquisition also costs **one ticker search request per result page until a match**, using `limit=1000` and the same job/monthly limits. No match fails safely without tickerinfo/EOD and is not cached. A saved mapping survives later download failures. Each acquisition then normally costs **one tickerinfo request plus one EOD request per 1,000-row page**. Marketstack charges per symbol, not just per HTTP request. The app reserves each attempt against `marketstack_usage` atomically before HTTP and does not refund errors, timeouts or abandoned reservations. This is intentionally conservative: the displayed local budget is **not** the provider dashboard balance and does not include other applications' usage. Counters are keyed by UTC calendar month (not necessarily the provider billing cycle); recheck/set the allowance when those differ. `MARKETSTACK_MAX_CALLS_PER_JOB` defaults to 12. No provider failure is automatically retried; inspect the error before explicitly requesting again. Mongo failures can resume persisted job checkpoints.

Storage:

- **`instruments`**: append-only tickerinfo observations, including complete nested provider `data`, `instrument_id` (ISIN), provider symbol/MIC, job ID, exact UTC `collected_at` and UTC `collection_date`. Multiple observations in a day are retained. Point-in-time means *known when collected*, not historical fundamentals reconstructed before collection.
- **`raw_eod_pages`**: complete parsed provider envelopes, keyed by job and offset, saved before indexing. Retains all rows, including duplicates, malformed dates, unexpected shapes and missing values for later cleanup. Re-indexing a saved page after a worker/DB interruption does not consume another provider call.
- **`quotes`**: dated-object projection (first occurrence per job/date; all duplicates remain in raw pages); quote date separate from `collected_at` / `collection_date`, original raw and adjusted provider fields in `data`, listing and job ID. Observations are immutable; retries of the same job/date are idempotent. API reads select the latest observation per date, optionally bounded by `as_of`.
- **`acquisition_jobs`**: durable status, progress, sanitized errors, requested range and pagination checkpoint. A partial unique index allows only one active job per ISIN. A five-minute renewable worker lease fences abandoned work; polling exposes expired jobs as retryable failures. Queued requests not started within 30 minutes are also exposed as retryable failures, covering offline workers or an interrupted Mongo-to-broker handoff; any later delivery of an expired job is skipped. Delivery is at least once: a crash between a paid response and its checkpoint can require another request, so HTTP is not exactly once.
- **`instrument_state`**: cached listing, optional `listing_resolution` evidence (source, query, name, collection timestamp and complete chosen row), optional manual `price_currency_override`, and last fully processed quote watermark. Earlier records remain available if an update fails.

**Ingestion is deliberately permissive; cleanup is deferred.** Null/missing prices, zero/negative values, unexpected field types, identifier mismatches, out-of-range/unordered/duplicate dates and changing pagination metadata do not block downloads. Full EOD responses are retained in `raw_eod_pages`; rows without a usable date remain there and are counted in `unindexed_rows`, rather than crashing acquisition. No new quality flags or quality-based chart filtering are applied. Pagination advances by returned row count, stops on empty/exhausted pages and remains bounded by the per-job quota cap. Authentication, transport/JSON-envelope handling, explicit authorization, leases and budget safeguards remain.

The candlestick chart uses **unadjusted** OHLC; adjusted values remain available as returned, including nulls. Incremental updates do not recalculate older corporate-action adjustments. Numeric prices and volumes are returned as exact strings; nulls, missing fields and unexpected JSON shapes are preserved. Non-finite tokens and integers outside BSON's signed-64-bit range are represented as strings for storage. Only chart rendering converts values to JavaScript numbers. It plots finite numeric OHLC as supplied, including inconsistent bounds and zero/negative prices; rows that cannot be drawn remain saved. Unknown listing currency remains unknown; fund currency is not substituted. When price currency is unknown, use **Price currency → Save currency** on the instrument page to set **EUR, USD, CHF, GBP or RUB**. The selector is hidden when provider prices determine the currency or a manual choice is already saved. The saved manual setting is shared across users, persists through data updates, and labels chart prices and tooltips without converting amounts or rewriting raw provider observations. Saving does not consume provider quota.

Authenticated endpoints (browser prefix `/api`):

| Route | Purpose |
| --- | --- |
| `GET /instruments/search?q=...` | Latest-snapshot autocomplete, no provider calls |
| `GET /instruments/acquired?limit=20&offset=0` | Paginated acquired ETFs with annual return/volatility, saved profile and quote coverage, no provider calls |
| `GET /instruments/{isin}[?as_of=...Z]` | Catalogue, collected profile, coverage, job and local budget |
| `GET /instruments/{isin}/quotes?limit=1000&offset=0[&as_of=...Z]` | Latest-observation candles, ascending and paginated |
| `PATCH /instruments/{isin}/price-currency` | `{price_currency: "EUR" \| "USD" \| "CHF" \| "GBP" \| "RUB"}`; saves a manual chart currency override |
| `POST /instruments/{isin}/acquisitions` | `{confirmed: true, mode: "update" \| "backfill"}`; optional paired `symbol`/`exchange` manual override; returns 202 and durable job |

`as_of` requires an explicit timezone and filters **collected market-data observations**; the catalogue, manual price-currency setting and operational job/budget are still current. See [the design contract](docs/on-demand-market-data.md).

## Return statistics and goal planning

After EOD acquisition, Hedgehoog calculates each instrument's average daily log
return and sample log-return variance (not simple returns or price levels). The
statistics display and Monte Carlo model use these same log-return estimates.
Existing downloads are calculated lazily without spending provider quota.
The instrument page shows **Daily return**, **Daily volatility**, **Annual return**
and **Annual volatility** as percentages. Returns remain log-based; volatility
is the square root of variance, with annualization using 252 trading days.

Open **Goal planner** to choose one EUR-priced instrument and enter your goal in
EUR, starting capital, goal date and target reliability (50–99%). A repeatable
10,000-path Monte Carlo simulation estimates the fixed monthly contribution
needed, plus model success probability and terminal-value percentiles. A projection
graph shows average portfolio value as a line, shaded 5th–95th and 25th–75th
percentile clouds across all 10,000 traces, and the goal as a dashed line. It requires
at least 60 usable daily returns; monthly payments start one month from today.

This is a **model estimate, not a guarantee**. It uses historical log returns,
assumes fractional shares and independent returns, and excludes inflation, taxes
and transaction fees. Unknown or conflicting currencies must be resolved; a EUR
label does not convert non-EUR history. See [model, data rules and API details](docs/goal-planner.md).
Rebuild the worker with `make services-up` after updating backend dependencies.

Authenticated endpoints: `GET /instruments/{isin}/statistics` and
`POST /goals/simulate` with `{instrument_id, goal_eur, starting_capital_eur,
goal_date, reliability}`. Reliability is a fraction, e.g. `0.90`; money outputs
are decimal strings. Neither endpoint calls the provider.

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
