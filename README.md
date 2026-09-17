# Hedgehoog

React/Vite frontend and FastAPI backend, with MongoDB users, JWT authentication, and startup ETF catalogue ingestion.

## Run locally

Requires Python 3.13+, [uv](https://docs.astral.sh/uv/), Node.js 20.19+ (or 22.12+), and Docker with Compose. Start Docker first, then:

```sh
make run
```

This creates a gitignored `.env` with a random JWT secret (without overwriting existing configuration), starts MongoDB and waits for it to be healthy, installs frontend dependencies, and starts both applications.

- Frontend: http://localhost:5173
- Backend/API docs: http://127.0.0.1:8000/docs
- Initial local login: **admin** / **admin**

Passwords are stored as Argon2 hashes, never plaintext. The admin account is created on backend startup only if its login does not already exist. Restarting never resets an existing password or role. MongoDB data lives in the `mongodb_data` Docker volume.

```sh
make setup          # Create .env only; edit it before first startup to change defaults
make db-up          # Start MongoDB only
make run-backend    # Start MongoDB and backend
make run-frontend   # Start frontend only; backend must already be running
make db-down       # Stop MongoDB without deleting data
```

Ctrl+C stops the application servers, not MongoDB. `make db-down` stops the database. MongoDB listens only on `127.0.0.1:27017`, with authentication enabled. Changing MongoDB initialization credentials in `.env` does not change credentials in an existing volume; update the actual database user too. Keep `MONGODB_URI` in sync with those credentials (URL-encode special characters).

MongoDB 7.0 is used because MongoDB 8's allocator is [incompatible with Linux kernels 6.19–7.0.13](https://www.mongodb.com/docs/v8.0/release-notes/8.0/), including the current local Docker Desktop kernel.

## ETF catalogue

On **every backend startup** (including development reloads), the server downloads
[the etfdb ETF feed](https://raw.githubusercontent.com/albertored/etfdb/refs/heads/main/json/etfs.json)
and imports it into the configured MongoDB database's **`etfs`** collection before
accepting requests. Startup requires internet access to GitHub and a writable database.

- One document per **ISIN**, enforced by a unique index. Repeated imports update existing documents without changing their MongoDB `_id` or creating duplicates.
- All source fields are preserved, including holdings, country/sector allocations, nulls and source update dates. Imported documents are replaced on refresh, so fields removed upstream are removed locally too.
- ISINs absent from a later feed are **not deleted**. No UCITS filtering or completeness checks are applied beyond importing the supplied source.
- The entire feed must be a non-empty JSON array of objects with valid, distinct ISINs before any database writes occur. HTTP, validation or database failures abort startup rather than serving an uninitialized catalogue.
- HTTP timeouts are 10 seconds for connection establishment and 60 seconds for other network operations. Database bulk writes are not transactional: a write failure may leave some records refreshed; restarting safely reapplies the feed.

This imports the source only; no ETF query endpoint or scheduled refresh is added yet.

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
- Add login rate limiting at your reverse proxy before exposing the app publicly.

## Tests

```sh
make test           # Backend tests, frontend component tests, production build
```

The default backend suite uses MongoDB doubles and mocked ETF HTTP responses (no GitHub access). To also run the real-MongoDB integration checks:

```sh
make db-up
cd backend
TEST_MONGODB_URI='mongodb://hedgehoog:hedgehoog-local@127.0.0.1:27017/?authSource=admin' uv run pytest
```

Use your configured URI if credentials differ. The integration tests use and remove their own uniquely named databases, checking hashed credentials, unique logins, seed idempotence, sessions surviving a backend restart, and ETF persistence, refreshes, unique ISINs and restart idempotence. They use synthetic ETF HTTP responses, not the live feed.
