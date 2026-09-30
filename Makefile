# ViralClip AI - common commands. Run `make help`.
SHELL := /bin/bash
PY := backend/.venv/bin/python
.DEFAULT_GOAL := help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Install backend (venv) + frontend dependencies
	cd backend && python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -e ".[dev,s3]"
	cd frontend && npm install
	@test -f .env || cp .env.example .env
	@mkdir -p data/inbox
	@echo "✔ Setup klaar. Vul je API keys in .env in (of later via Settings) en start met: make dev"

dev: ## Run API + worker + dashboard locally (Ctrl+C stops all)
	./scripts/dev.sh

api: ## Run only the API (http://localhost:8000/docs)
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000

worker: ## Run only the worker
	cd backend && .venv/bin/python -m app.worker.runner

web: ## Run only the dashboard (http://localhost:3000)
	cd frontend && npm run dev

demo: ## Load a demo video + clips (no API keys needed)
	cd backend && .venv/bin/python -m app.demo

migrate: ## Apply database migrations
	cd backend && .venv/bin/python -m app.migrate

test: ## Backend tests + frontend lint/build
	cd backend && .venv/bin/ruff check app tests && .venv/bin/pytest -q
	cd frontend && npx eslint . && npm run build

up: ## Start the full stack with Docker Compose
	@test -f .env || cp .env.example .env
	@mkdir -p data/inbox
	docker compose up -d --build
	@echo "✔ Dashboard: http://localhost:3000"

down: ## Stop Docker Compose
	docker compose down

logs: ## Follow Docker Compose logs
	docker compose logs -f --tail=100

.PHONY: help setup dev api worker web demo migrate test up down logs
