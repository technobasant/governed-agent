SHELL := /bin/bash
.DEFAULT_GOAL := help
API ?= http://127.0.0.1:8000

help: ## List targets
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-10s %s\n", $$1, $$2}'

install: ## Install Python and web dependencies
	uv sync --all-groups
	cd web && npm ci

seed: ## Build the synthetic warehouse and the golden set from it
	uv run python -m ga_mcp.warehouse --path data/warehouse.duckdb
	uv run python evals/build_golden.py

up: ## Start PostgreSQL, MCP, API, web and Jaeger (Ollama runs on the host)
	docker compose up -d --build
	@echo "UI http://localhost:3000  API $(API)/docs  traces http://localhost:16686"

down: ## Stop the stack
	docker compose down

demo: ## Ask four questions through the API and print what happened
	uv run python scripts/demo.py --api $(API)

lint: ## Ruff, formatting and mypy
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy shared mcp agent api tests evals scripts

test: ## Unit and integration tests with coverage (integration needs Docker)
	uv run pytest --cov --cov-report=term-missing --cov-fail-under=85

eval: ## Replay the golden set and gate against the baseline
	uv run --group eval evalproof run evals/suite.yaml --out .evalproof/candidate.json --quiet
	uv run --group eval evalproof compare evals/runs/baseline.json .evalproof/candidate.json \
	  --suite evals/suite.yaml --gate

measure: ## Run the golden set against a live model and re-record the cassette
	rm -f evals/cassettes/qwen3-8b.jsonl
	GA_EVAL_MODEL=ollama:qwen3:8b GA_EVAL_RECORD=evals/cassettes/qwen3-8b.jsonl \
	  uv run --group eval evalproof run evals/suite.yaml --label "qwen3:8b (live)" \
	  --out evals/runs/qwen3-8b-live.json --no-cache
	uv run --group eval evalproof run evals/suite.yaml --out evals/runs/baseline.json --quiet
	uv run python evals/measure.py evals/runs/qwen3-8b-live.json

web-check: ## Lint, type-check and build the review UI
	cd web && npm run lint && npm run typecheck && npm run build

e2e: ## Browser test of the review flow (needs PostgreSQL on DATABASE_URL)
	cd web && npx playwright test

.PHONY: help install seed up down demo lint test eval measure web-check e2e
