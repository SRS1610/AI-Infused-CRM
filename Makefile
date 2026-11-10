# Tessera developer entry points. Every target works from a fresh clone.
SHELL := /bin/bash
COMPOSE := docker compose -f deploy/compose/docker-compose.yml --env-file .env
BACKEND := cd backend && uv run
FRONTEND := cd frontend && pnpm

.PHONY: help setup up down logs ps db-init db-migrate seed serve worker scheduler gateway web \
        test test-backend test-frontend e2e lint format typecheck ci clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Install backend and frontend dependencies, copy .env
	@test -f .env || cp .env.example .env
	cd backend && uv sync
	@test -d frontend && (cd frontend && pnpm install) || true

up: ## Start the full stack with Docker Compose
	$(COMPOSE) up -d --build

down: ## Stop the stack
	$(COMPOSE) down

logs: ## Tail stack logs
	$(COMPOSE) logs -f --tail=200

ps: ## Stack status
	$(COMPOSE) ps

db-init: ## Initialise the database and install the base modules
	$(BACKEND) tessera db init

db-migrate: ## Apply pending module upgrades
	$(BACKEND) tessera db migrate

seed: ## Load demo data for the installed modules
	$(BACKEND) tessera seed demo

serve: ## Run the API server locally (reload)
	$(BACKEND) tessera serve --reload

worker: ## Run a Celery worker locally
	$(BACKEND) tessera worker

scheduler: ## Run the scheduler locally
	$(BACKEND) tessera scheduler

gateway: ## Run the AI gateway locally
	cd gateway && uv run gateway serve --reload

web: ## Run the frontend dev server
	$(FRONTEND) dev

test: test-backend test-frontend ## Run all tests

test-backend: ## Backend tests with coverage (needs TESSERA_TEST_DATABASE_URL)
	$(BACKEND) pytest --cov --cov-report=term-missing --cov-report=xml

test-frontend: ## Frontend unit tests
	@test -d frontend && $(FRONTEND) test || echo "frontend not present yet"

e2e: ## Playwright end-to-end tests
	$(FRONTEND) e2e

lint: ## Lint backend and frontend
	cd backend && uv run ruff check . && uv run ruff format --check .
	@test -d frontend && $(FRONTEND) lint || true

format: ## Format backend and frontend
	cd backend && uv run ruff format . && uv run ruff check --fix .
	@test -d frontend && $(FRONTEND) format || true

typecheck: ## Static type checks
	cd backend && uv run mypy tessera
	@test -d frontend && $(FRONTEND) typecheck || true

ci: lint typecheck test ## What CI runs

clean: ## Remove build artefacts and caches
	rm -rf backend/.venv backend/.pytest_cache backend/.mypy_cache backend/.ruff_cache backend/htmlcov backend/coverage.xml
	rm -rf frontend/node_modules frontend/dist
