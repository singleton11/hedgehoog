# Hedgehoog

Hedgehoog is a self-hosted portfolio and goal-planning application. The frontend is a static web app; the backend is written in Rust and uses PostgreSQL for application data, durable background jobs, schedules, leases, and retries.

The runtime has no MongoDB, RabbitMQ, Celery, or Python dependency.

## Architecture

- `frontend/`: browser application.
- `backend-rust/`: Axum API plus PostgreSQL-backed worker and scheduler binaries.
- `postgres`: relational application schema and durable job queue.
- `hedgehoog-worker --queue catalogue`: serial ETF catalogue, refresh, and retention work.
- `hedgehoog-worker --queue marketdata`: concurrent Yahoo acquisition work.
- `hedgehoog-scheduler`: singleton UTC schedule producer.

Scheduled jobs:

- ETF catalogue import: 00:00 UTC.
- Market-data refresh: 02:00 UTC.
- Market-data retention: 03:00 UTC.

Jobs are claimed with PostgreSQL `FOR UPDATE SKIP LOCKED`, carry renewable leases, use bounded retries, and can be reclaimed after an expired worker lease. Deterministic deduplication keys prevent duplicate scheduled work.

## Local setup

Requirements:

- Docker with Compose.
- Node.js/npm for frontend development.
- Python 3 only for the small one-time `.env` generator used by `make setup`; it is not part of the application runtime.

Create local configuration and start PostgreSQL, the Rust API, workers, and scheduler:

```sh
make setup
make services-up
```

Start the frontend development server:

```sh
make run
```

The API listens on `http://127.0.0.1:8000`; Vite serves the frontend on its usual development port. PostgreSQL listens only on `127.0.0.1:5432` in the local Compose profile.

Stop services without deleting data:

```sh
make services-down
```

The PostgreSQL database is stored in the `postgresql_data` Docker volume. Existing MongoDB or RabbitMQ volumes from older Hedgehoog releases are not referenced or deleted by this rewrite.

## Configuration

Copy `.env.example` through `make setup`. Important variables:

- `DATABASE_URL`: PostgreSQL connection used by host-run binaries.
- `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`: local Compose database credentials.
- `JWT_SECRET`: at least 32 characters.
- `ADMIN_LOGIN`, `ADMIN_PASSWORD`: initial administrator credentials.
- `ALLOWED_ORIGINS`: comma-separated browser origins.
- `MARKET_DATA_WORKER_CONCURRENCY`: concurrent acquisition handlers.
- `JOB_LEASE_SECONDS`: renewable execution lease duration.
- `YAHOO_REQUEST_INTERVAL_SECONDS`: PostgreSQL-coordinated Yahoo request pacing.

The administrator is seeded only when absent. Passwords use Argon2 hashes. Restarting does not reset an existing account.

## Manual ETF import

Queue an immediate catalogue import:

```sh
make import-etfs
```

Watch processing:

```sh
docker compose logs -f catalogue-worker marketdata-worker scheduler
```

## Tests

Run the Rust formatting, Clippy, and test target plus frontend tests/build:

```sh
make test
```

The backend test image uses the locked Cargo dependency graph:

```sh
docker build -f backend-rust/Dockerfile --target test -t hedgehoog-rust-test .
```

PostgreSQL integration tests run when `TEST_DATABASE_URL` is set. The repository's validation workflow starts a disposable PostgreSQL 16 instance and executes those suites against a fresh migrated schema.

## Production

`deploy/compose.yaml` runs:

- PostgreSQL 16;
- a one-shot `hedgehoog-migrate` schema gate;
- `hedgehoog-api`;
- serial catalogue worker;
- concurrent market-data worker;
- singleton scheduler; and
- the static frontend web image.

Build the backend image from `backend-rust/Dockerfile`, set the required production variables (`RELEASE_SHA`, `POSTGRES_PASSWORD`, `JWT_SECRET`, `ADMIN_PASSWORD`, and `ALLOWED_ORIGINS`), then validate the rendered Compose file before deployment. Production defaults to secure cookies and rejects an insecure-HTTP configuration; only set `COOKIE_SECURE=false` together with `ALLOW_INSECURE_HTTP=true` for an explicitly accepted private HTTP-only deployment:

```sh
docker compose -f deploy/compose.yaml config
```

Production cutover is intentionally a single switch: do not route only selected endpoints to Rust. Take a PostgreSQL backup before upgrading an existing Rust deployment. This rewrite starts with a fresh PostgreSQL schema and does not migrate or delete historical MongoDB data.

## API behavior

The API preserves the existing Hedgehoog browser contract, including:

- secure session cookies and origin checks;
- customer, goal, simulation, calibration, plan, portfolio, import, performance, and recommendation routes;
- decimal strings for financial values;
- immutable market-data generations and published-history reads;
- durable acquisition lifecycle state; and
- idempotent portfolio imports and recommendation fulfillment.

See `docs/rust-backend-rewrite.md` and `backend-rust/README.md` for implementation details.
