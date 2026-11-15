# Rust backend rewrite

## Decision

Hedgehoog replaces the Python/FastAPI/Celery backend with a Rust service in one production cutover. The Python source remains in the repository temporarily as an audit/reference implementation, but Compose, CI, release images, and deployment use only the Rust backend. Production must not route only a subset of endpoints to Rust.

The rewrite preserves the union of:

- the current `origin/main` feature set, including recommendation, retention, and saved-history behavior; and
- the PostgreSQL relational model developed on the preserved `postgres-rewrite` branch.

No MongoDB data migration is required. The target starts with an empty PostgreSQL database, and existing MongoDB data remains untouched.

## Runtime architecture

The Rust workspace contains three binaries sharing one application library:

- `hedgehoog-api`: Axum HTTP API, authentication, and request validation;
- `hedgehoog-worker`: durable background-job consumers; and
- `hedgehoog-scheduler`: singleton UTC schedule producer.

PostgreSQL is the only stateful dependency. RabbitMQ and Celery are removed.

## PostgreSQL job queue

Jobs are durable rows claimed with `FOR UPDATE SKIP LOCKED`. A claim records a worker identity and lease expiry. Workers extend leases while executing long jobs. If a process exits without completing a job, another worker can reclaim it after the lease expires.

Delivery is at least once. Handlers must remain idempotent. Acquisition work continues to use durable checkpoints and a per-instrument active-job constraint. Failed jobs use bounded exponential retry delays and preserve a sanitized terminal error. Queue names retain workload isolation:

- `catalogue`: ETF imports, refresh sweeps, and retention;
- `marketdata`: provider acquisitions.

The scheduler uses a PostgreSQL advisory lock so only one instance emits the daily refresh at 02:00 UTC and retention at 03:00 UTC. Deterministic deduplication keys prevent duplicate scheduled rows after restart.

## Compatibility boundary

The frontend contract is the existing HTTP API: routes, status codes, cookies, JSON field names, decimal strings, pagination, generation fencing, and safe error messages remain compatible. The rewrite may change internal schema and implementation details only.

## Verification gates

1. Rust formatting, linting, unit tests, and SQL migration tests pass.
2. Queue tests prove exclusive claims, lease recovery, retry bounds, scheduling deduplication, and graceful shutdown.
3. HTTP contract tests cover all existing routes and security middleware.
4. PostgreSQL HTTP integration tests cover market data plus planner, calibration, portfolio ledger, DEGIRO import, performance, recommendations, fulfillment, and plan tracking.
5. Compose starts PostgreSQL, API, workers, scheduler, and frontend without MongoDB or RabbitMQ.
6. CI and release deployment build the Rust image and run PostgreSQL integration tests. Historical Python source may be removed separately after the compatibility audit; it is not packaged or executed.

## Implemented surface

The Rust API now covers authentication, jobs, settings, index and ETF market data, acquisition and retention, customers, goals, simulations, saved plans, walk-forward calibration, portfolio ledger and valuation, plan tracking, DEGIRO preview/import, daily TWR performance, and recommendation preview/fulfillment. Financial writes use PostgreSQL transactions, row locks, unique idempotency keys, and immutable receipts where required.

Local and production Compose define PostgreSQL, a one-shot schema migration gate, the API, serial catalogue worker, concurrent market-data worker, and singleton scheduler. Long-running Rust services wait for the migration gate to succeed. MongoDB and RabbitMQ volumes are deliberately not deleted, so older data remains untouched.
