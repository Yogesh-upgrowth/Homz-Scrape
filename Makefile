.PHONY: help install install-browsers db-up db-migrate lint fmt test scrape etl enrich api docker-build docker-up clean

PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

$(VENV):
	$(PY) -m venv $(VENV)

install: $(VENV) ## Create venv and install deps
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements.txt
	$(BIN)/pip install -e .

install-browsers: ## Install Playwright chromium
	$(BIN)/playwright install chromium

db-up: ## Start postgres via docker compose
	docker compose up -d postgres

db-migrate: ## Apply SQL schema (idempotent)
	docker compose exec -T postgres psql -U homz -d homz < sql/001_schema.sql
	docker compose exec -T postgres psql -U homz -d homz < sql/002_search.sql

lint: ## Ruff check
	$(BIN)/ruff check src tests

fmt: ## Ruff autofix
	$(BIN)/ruff check --fix src tests

test: ## Run unit tests (no network, no database)
	$(BIN)/pytest -q

scrape: ## Run every source once (incremental)
	$(BIN)/homz scrape all

etl: ## Post-load maintenance and rollups
	$(BIN)/homz etl run

enrich: ## AI enrichment over pending rows
	$(BIN)/homz enrich run

api: ## Serve the search API + widget
	$(BIN)/uvicorn homz.search.api:app --host 0.0.0.0 --port 8000 --reload

docker-build:
	docker compose build

docker-up:
	docker compose up -d

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache
