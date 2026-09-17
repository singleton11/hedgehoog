# Hedgehoog

React/Vite frontend and FastAPI backend, with MongoDB users, JWT authentication, and Celery ETF catalogue ingestion backed by RabbitMQ.

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
- HTTP timeouts are 10 seconds for connection establishment and 60 seconds for other network operations. A writable database is always required; GitHub access is needed only for an incomplete day. Bulk writes are not transactional, and the completion flag is **not an exclusive lock** between simultaneous importers. Compose runs one worker with concurrency **1**, consuming only the `etfs` queue; do not scale it up without adding distributed import locking.

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

Startup and explicit manual dispatch are the only triggers. No Celery Beat,
ETF query endpoint or scheduled refresh is added yet. A continuously running
server will not automatically import at midnight.

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
