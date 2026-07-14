# Engineering Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish a testable Python/React monorepo baseline and stable public contracts for a self-hosted QQ/Telegram agent bot without platform business behavior.

**Architecture:** A Python package under `src/mybot` owns immutable Pydantic contracts, environment settings, dependency probes, observability, FastAPI lifecycle, and cancellable process-mode runners. A separate Vite/React application consumes the public health endpoint. PostgreSQL/pgvector, Redis, SearXNG, and isolated application roles are composed around one reusable Python image.

**Tech Stack:** Python 3.12, uv, FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic, psycopg 3, Redis asyncio, structlog, OpenTelemetry, Pytest, Hypothesis, Ruff, Pyright, React, TypeScript, Vite, Vitest, Testing Library, Docker Compose.

---

## File map

- `src/mybot/contracts/*.py`: immutable transport, conversation, reply, tool, memory, and plugin schemas.
- `src/mybot/settings.py`: environment parsing and secret-safe representation.
- `src/mybot/infrastructure/*.py`: DB/Redis clients, health probe interfaces, logging, tracing, correlation IDs.
- `src/mybot/api.py`: FastAPI app factory and health endpoints.
- `src/mybot/runtime.py`, `src/mybot/cli.py`, `src/mybot/__main__.py`: real cancellable process-mode entrypoints.
- `alembic/*`: async migration environment and initial pgvector/system table migration.
- `tests/*`: contract, settings, readiness, and runtime behavior tests.
- `web/src/*`: accessible responsive application shell and API health display.
- `web/src/*.test.tsx`: accessibility and health rendering tests.
- `Dockerfile`, `web/Dockerfile`, `compose.yaml`: reusable application image and deployment topology.
- `.env.example`, `.gitignore`, `Makefile`, `README.md`, `.github/workflows/ci.yml`: operator and CI surface.

### Task 1: Python project and contract tests

**Files:**
- Create: `pyproject.toml`
- Create: `tests/contracts/test_messages.py`
- Create: `tests/contracts/test_decisions.py`
- Create: `tests/contracts/test_tools_memory_plugins.py`
- Create: `tests/test_settings.py`

- [ ] Write tests that import the desired public schemas and assert immutability, UTC validation, typed segment discrimination, deterministic `ConversationKey.stable_key`, bounded decisions, non-empty 1..3 segment reply plans, structured tool outcomes, scoped memory, plugin schema validation, and secret redaction.
- [ ] Run `uv run pytest tests/contracts tests/test_settings.py -q` and retain the expected import-failure output as RED evidence.

### Task 2: Python contracts and settings

**Files:**
- Create: `src/mybot/contracts/__init__.py`
- Create: `src/mybot/contracts/common.py`
- Create: `src/mybot/contracts/messages.py`
- Create: `src/mybot/contracts/conversation.py`
- Create: `src/mybot/contracts/replies.py`
- Create: `src/mybot/contracts/tools.py`
- Create: `src/mybot/contracts/memory.py`
- Create: `src/mybot/contracts/plugins.py`
- Create: `src/mybot/settings.py`

- [ ] Implement frozen Pydantic v2 models with strict enums, normalized identifiers, UTC-aware timestamps, JSON-schema-compatible plugin/tool schemas, and explicit validators for every tested invariant.
- [ ] Implement `SecretStr` settings loaded from the environment with non-secret development defaults and sanitized repr/model dumps.
- [ ] Run `uv run pytest tests/contracts tests/test_settings.py -q` until GREEN.

### Task 3: Health, observability, and runtime tests

**Files:**
- Create: `tests/test_health.py`
- Create: `tests/test_runtime.py`

- [ ] Write small fake dependency probes and assert `/health/live` is unconditional, `/health/ready` reports database and Redis separately, returns 200 only when both are healthy, returns 503 otherwise, and preserves a supplied/request correlation ID.
- [ ] Assert each non-API runtime delegates to a cancellable lifecycle service rather than a print-only branch.
- [ ] Run `uv run pytest tests/test_health.py tests/test_runtime.py -q` and retain expected import failures as RED evidence.

### Task 4: API, probes, telemetry, and process modes

**Files:**
- Create: `src/mybot/__init__.py`
- Create: `src/mybot/api.py`
- Create: `src/mybot/cli.py`
- Create: `src/mybot/runtime.py`
- Create: `src/mybot/__main__.py`
- Create: `src/mybot/infrastructure/health.py`
- Create: `src/mybot/infrastructure/logging.py`
- Create: `src/mybot/infrastructure/telemetry.py`
- Create: `src/mybot/infrastructure/correlation.py`

- [ ] Implement dependency-injected async DB/Redis probes, readiness aggregation, JSON logging, OpenTelemetry bootstrap, and correlation ID middleware.
- [ ] Implement API/gateway/agent-worker/maintenance-worker/plugin-runner module entrypoints with signal-aware cancellation and shared lifecycle interfaces.
- [ ] Run focused tests until GREEN, then run `uv run pytest -q`, `uv run ruff check .`, and `uv run pyright`.

### Task 5: Database migration and container topology

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako`
- Create: `alembic/versions/20260714_0001_foundation.py`
- Create: `Dockerfile`
- Create: `web/Dockerfile`
- Create: `compose.yaml`
- Create: `searxng/settings.yml`

- [ ] Wire Alembic to async SQLAlchemy settings and create a reversible migration that enables `vector` and creates `system_kv`.
- [ ] Define PostgreSQL+pgvector, Redis AOF, SearXNG, web, and five application roles using healthchecks and shared images; keep NapCat optional/documented rather than required.
- [ ] Validate YAML statically and run `docker compose config` only if Docker is available.

### Task 6: Frontend tests and shell

**Files:**
- Create: `web/package.json`
- Create: `web/tsconfig.json`
- Create: `web/tsconfig.app.json`
- Create: `web/vite.config.ts`
- Create: `web/index.html`
- Create: `web/src/main.tsx`
- Create: `web/src/App.tsx`
- Create: `web/src/App.css`
- Create: `web/src/index.css`
- Create: `web/src/test/setup.ts`
- Create: `web/src/App.test.tsx`

- [ ] Write Testing Library tests for landmark/heading/status accessibility, loading state, healthy dependency rendering, and degraded/error rendering; run `pnpm --dir web test -- --run` and capture expected RED import failure.
- [ ] Implement a responsive graphite/ink mission-control shell with amber accents, restrained healthy cyan, condensed headings, readable body typography, subtle grid/noise, visible focus styles, and live-region health status.
- [ ] Run `pnpm --dir web test -- --run` and `pnpm --dir web build` until GREEN.

### Task 7: Operator surface and CI

**Files:**
- Create: `.env.example`
- Create: `.gitignore`
- Create: `Makefile`
- Create: `README.md`
- Create: `.github/workflows/ci.yml`

- [ ] Document uv/pnpm setup, migrations, local commands, Docker Compose startup, health endpoints, role commands, and external/optional NapCat connectivity without including live secrets.
- [ ] Add CI jobs for Python tests/lint/type checking, frontend tests/build, and Compose validation.
- [ ] Run every command documented for local verification that the current machine supports.

### Task 8: Final verification and commit

- [ ] Inspect `git diff --check`, the complete file list, dependency locks, and the milestone requirements line by line.
- [ ] Run fresh full Python and frontend verification commands and record exact counts/output.
- [ ] If Docker is unavailable, report compose validation as a documented environment limitation after static YAML validation.
- [ ] Commit all milestone files with `feat: establish engineering foundation` and report the resulting SHA.
