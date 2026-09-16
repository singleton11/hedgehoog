.PHONY: run run-backend run-frontend

run:
	cd frontend && npm ci && npm exec -- concurrently --kill-others --names backend,frontend \
		"cd ../backend && uv run uvicorn main:app --reload" \
		"npm run dev"

run-backend:
	cd backend && uv run uvicorn main:app --reload

run-frontend:
	cd frontend && npm ci && npm run dev
