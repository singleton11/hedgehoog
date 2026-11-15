# Hedgehoog Rust backend

This crate is the Hedgehoog API and background-work runtime.

## Binaries

- `hedgehoog-api`: Axum HTTP API.
- `hedgehoog-migrate`: one-shot SQLx schema migration runner used as the Compose startup gate.
- `hedgehoog-worker --queue catalogue --concurrency 1`: serial catalogue and maintenance work.
- `hedgehoog-worker --queue marketdata --concurrency N`: concurrent provider acquisitions.
- `hedgehoog-scheduler`: singleton PostgreSQL-backed UTC scheduler.

PostgreSQL stores application data, durable jobs, leases, retries, schedules, acquisition checkpoints, and request-pacing locks. There is no Celery, RabbitMQ, MongoDB, or Python runtime dependency.

The API, workers, and scheduler defensively run idempotent SQLx migrations at startup. Compose first runs `hedgehoog-migrate` to completion and starts those long-running services only after that gate succeeds; PostgreSQL serializes any overlapping migration attempts.

## Local checks

The repository Dockerfile provides the Rust toolchain when Rust is not installed on the host:

```sh
docker build -f backend-rust/Dockerfile --target test -t hedgehoog-rust-test .
```

Run live PostgreSQL integration tests by supplying `TEST_DATABASE_URL` to the test container or Cargo process. Set `DATABASE_URL` and a 32-character `JWT_SECRET` to run a binary directly.

## Queue semantics

Workers claim jobs with `FOR UPDATE SKIP LOCKED`. Claims have renewable leases; expired claims are reclaimable. Handlers are idempotent and retries are bounded. The scheduler holds a PostgreSQL advisory lock and emits deterministic deduplication keys.

Queue isolation:

- `catalogue`: ETF imports, refresh sweeps, and retention.
- `marketdata`: Yahoo acquisition jobs and bounded statistics backfills.

The acquired ETF list reads `instrument_statistics` snapshots rather than loading quote histories. Publishing a new ETF generation and changing its price-currency override update its snapshot with the underlying change. The scheduler detects missing or outdated snapshots (including existing publications and global quote-fill policy changes) and queues backfills of at most 100 ETFs per job. Until a snapshot is ready, the list shows pending statistics and refreshes automatically; it never serves statistics for the wrong generation, currency override, or policy.
