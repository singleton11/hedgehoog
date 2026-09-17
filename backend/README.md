# Backend

A minimal FastAPI app, managed with [uv](https://docs.astral.sh/uv/).

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
both servers; if either server exits, the other is stopped too.

- Frontend: http://localhost:5173

To run only the backend, from the repository root:

```sh
make run-backend
```

Or run the commands directly:

```sh
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

Before accepting requests, every startup downloads the
[etfdb ETF catalogue](https://raw.githubusercontent.com/albertored/etfdb/refs/heads/main/json/etfs.json)
into MongoDB's `etfs` collection, keyed by unique ISIN. Existing records are
refreshed without duplicates; missing ISINs are retained. Internet access and a
writable MongoDB database are required, and import failures abort startup.
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
