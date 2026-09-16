.PHONY: setup db-up db-down run run-backend run-frontend test

setup:
	cd backend && uv run python ../scripts/init_env.py

db-up: setup
	docker compose up -d --wait mongodb

db-down:
	docker compose stop mongodb

run: db-up
	cd frontend && npm ci && npm exec -- concurrently --kill-others --names backend,frontend \
		"cd ../backend && uv run uvicorn main:app --reload" \
		"npm run dev"

run-backend: db-up
	cd backend && uv run uvicorn main:app --reload

run-frontend:
	cd frontend && npm ci && npm run dev

test:
	cd backend && uv run pytest
	cd frontend && npm ci && npm test && npm run build
