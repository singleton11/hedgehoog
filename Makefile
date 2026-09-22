.PHONY: setup db-up db-down services-up services-down import-etfs run run-backend run-frontend test

setup:
	cd backend && uv run python ../scripts/init_env.py

db-up: setup
	docker compose up -d --wait mongodb

db-down:
	docker compose stop mongodb

services-up: setup
	docker compose up -d --build --wait mongodb rabbitmq celery-worker

services-down:
	docker compose stop celery-worker rabbitmq mongodb

import-etfs: services-up
	docker compose exec celery-worker python -m celery -A celery_app:app call etfs.import_daily

run: services-up
	cd frontend && npm ci && npm exec -- concurrently --kill-others --names backend,frontend \
		"cd ../backend && uv run uvicorn main:app --reload" \
		"npm run dev"

run-backend: services-up
	cd backend && uv run uvicorn main:app --reload

run-frontend:
	cd frontend && npm ci && npm run dev

test:
	cd backend && uv run pytest
	cd frontend && npm ci && npm test && npm run build
