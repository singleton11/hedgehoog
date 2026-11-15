.PHONY: setup db-up db-down services-up services-down import-etfs run run-backend run-frontend test test-coverage

setup:
	python3 scripts/init_env.py

db-up: setup
	docker compose up -d --wait postgres

db-down:
	docker compose stop postgres

services-up: setup
	docker compose up -d --build --wait postgres api catalogue-worker marketdata-worker scheduler

services-down:
	docker compose stop scheduler marketdata-worker catalogue-worker api postgres

import-etfs: services-up
	docker compose exec -T postgres sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -c "INSERT INTO job_queue (id, kind, queue, payload, status, run_at, requested_at, max_attempts, deduplication_key) VALUES (md5(random()::text || clock_timestamp()::text), '\''etf_import'\'', '\''catalogue'\'', '\''{}'\''::jsonb, '\''queued'\'', now(), now(), 3, '\''manual-etf-import:'\'' || clock_timestamp()::text);"'

run: services-up
	cd frontend && npm ci && npm run dev

run-backend: services-up
	docker compose logs -f api catalogue-worker marketdata-worker scheduler

run-frontend:
	cd frontend && npm ci && npm run dev

test:
	docker build -f backend-rust/Dockerfile --target test -t hedgehoog-rust-test .
	cd frontend && npm ci && npm test && npm run build

test-coverage: test
