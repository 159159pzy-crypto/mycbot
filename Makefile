.DEFAULT_GOAL := help

UV ?= uv
PNPM ?= pnpm
COMPOSE ?= docker compose

.PHONY: help install install-python install-web format lint typecheck test \
	test-python test-web build-web migrate run-api run-gateway \
	run-agent-worker run-maintenance-worker run-plugin-runner compose-build \
	compose-infra compose-migrate compose-up compose-down compose-logs \
	compose-config verify

help:
	@echo "Targets: install format lint typecheck test build-web migrate"
	@echo "         run-api run-gateway run-agent-worker run-maintenance-worker"
	@echo "         run-plugin-runner compose-up compose-down compose-config verify"

install: install-python install-web

install-python:
	$(UV) sync --all-groups --locked

install-web:
	$(PNPM) --dir web install --frozen-lockfile

format:
	$(UV) run ruff format .

lint:
	$(UV) run ruff check .

typecheck:
	$(UV) run pyright

test: test-python test-web

test-python:
	$(UV) run pytest -q

test-web:
	$(PNPM) --dir web test -- --run

build-web:
	$(PNPM) --dir web build

migrate:
	$(UV) run alembic upgrade head

run-api:
	$(UV) run python -m mybot api

run-gateway:
	$(UV) run python -m mybot gateway

run-agent-worker:
	$(UV) run python -m mybot agent-worker

run-maintenance-worker:
	$(UV) run python -m mybot maintenance-worker

run-plugin-runner:
	$(UV) run python -m mybot plugin-runner

compose-build:
	$(COMPOSE) build

compose-infra:
	$(COMPOSE) up -d postgres redis searxng

compose-migrate:
	$(COMPOSE) run --rm api alembic upgrade head

compose-up: compose-build compose-infra compose-migrate
	$(COMPOSE) up -d

compose-down:
	$(COMPOSE) down

compose-logs:
	$(COMPOSE) logs --follow --tail=200

compose-config:
	$(COMPOSE) --env-file .env.example config --quiet

verify: lint typecheck test build-web compose-config
