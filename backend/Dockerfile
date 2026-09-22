FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.6.8 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/backend/.venv/bin:$PATH"
WORKDIR /app/backend

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY *.py ./
RUN useradd --create-home --uid 10001 worker
USER worker

# Keep the application directory importable after Celery finishes startup.
CMD ["python", "-m", "celery", "-A", "celery_app:app", "worker", "--loglevel=info", "--queues=etfs,marketdata", "--concurrency=1"]
