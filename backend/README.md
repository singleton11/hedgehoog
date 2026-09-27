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

The worker runs with concurrency 1 to serialize imports. Do not run extra workers
on the `etfs` queue without adding distributed locking. RabbitMQ persists queued
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

Authenticated `/instruments/search`, `/instruments/{isin}` and `/instruments/{isin}/quotes` routes read the catalogue and collected observations from MongoDB. Explicit `POST /instruments/{isin}/acquisitions` requests queue `marketdata.acquire`; the worker consumes both `etfs` and `marketdata`.

The default provider is Yahoo Finance via pinned `yfinance==1.7.0`; no API key or paid quota is required. `MARKET_DATA_PROVIDER=yfinance`, `MARKET_DATA_MAX_CALLS_PER_JOB=40` and `MARKET_DATA_TIMEOUT_SECONDS=15` configure the experimental adapter and safety bounds. Rebuild the worker after code/dependency changes. Browsing never calls the provider. Errors are sanitized; provider failures require an explicit retry, while database recovery replays saved checkpoints.

The worker resolves a Yahoo listing from the catalogue ticker/ISIN and known venues, saving symbol/MIC and lookup evidence. POST accepts `{confirmed:true}` and an optional Yahoo `symbol` override (for example `SPYI.DE`). Without `exchange`, the worker detects its MIC from exact-symbol Yahoo chart metadata; it never substitutes a different symbol. An explicit paired `symbol`/`exchange` override remains available, and the legacy Marketstack provider still requires the pair. Every explicit Yahoo acquisition refreshes full available history from inception through yesterday, then publishes a coherent immutable generation. Failed refreshes keep previous published data readable. `as_of` quote pagination pins the publication cutoff so updates cannot splice different adjusted-price vintages.

Yahoo OHLC/Close are already split-adjusted with `auto_adjust=False`. Analytics use Close without applying split factors again or substituting dividend-adjusted Adj Close; manual split multiplication is unavailable for this basis. Preserve raw evidence and nulls. The neutral adapter contract in `market_provider.py` is the replacement point for a future licensed EODHD provider. Yahoo is unofficial experimental access, not a commercial data licence. See the [full storage and API contract](../docs/on-demand-market-data.md).

```sh
uv run pytest  # Includes provider MockTransport, acquisition and API tests; no live API calls
# Optional: set TEST_MONGODB_URI and TEST_CELERY_BROKER_URL for isolated real-service tests.
```

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
