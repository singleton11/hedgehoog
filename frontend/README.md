# Frontend

A minimal React app built with Vite. It calls the FastAPI backend and displays
its hello-world message, with loading and error states.

## Run locally

Requires Node.js 20.19+ or 22.12+ (newer supported releases also work) and npm.

Start both the backend and frontend from the repository root (requires
[uv](https://docs.astral.sh/uv/) for the backend):

```sh
make run
```

This installs frontend dependencies and starts both development servers with
automatic reload. Press `Ctrl+C` to stop both; if either server exits, the other
is stopped too.

To run only the frontend, from the repository root:

```sh
make run-frontend
```

Start the backend separately with `make run-backend` in another terminal.
Alternatively, start the frontend directly:

```sh
cd frontend
npm ci
npm run dev
```

Open http://localhost:5173. The page fetches `/api/`; Vite proxies that request
to `http://127.0.0.1:8000/`, which returns `{"message":"Hello, World!"}`.
The browser uses the same origin, so no backend CORS configuration is needed.
If the backend is unavailable, the page displays an error. Start the backend
and refresh to try again.

## Build

From `frontend/`:

```sh
npm run build
npm run preview
```

The production build is written to `dist/`. Vite's local preview server also
uses the proxy, so keep the backend running when testing the build.

For deployment, configure your web server to forward `/api/` to the backend's
`/` endpoint. The Vite proxy is only for local development and preview; it is
not included in the built static files.
