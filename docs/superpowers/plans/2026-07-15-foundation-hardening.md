# Platform Foundation Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the Milestone 1 quality review by making health checks bounded and cancellable, public JSON deeply immutable, the console resilient and fresh, and deployment/CI least-privilege and integration-tested.

**Architecture:** Keep the existing public contracts and visual language, adding focused adapters instead of product behavior. Backend changes remain under settings/contracts/health/API; frontend data acquisition moves into a dedicated hook while presentational sections stay stateless; deployment changes isolate plugin-runner and make the web container independently healthy.

**Tech Stack:** Python 3.12, Pydantic v2, FastAPI, SQLAlchemy async, redis asyncio, Alembic, React 19, TypeScript, Vitest, Nginx unprivileged, Docker Compose, GitHub Actions.

---

### Task 1: Bounded readiness and settings semantics

**Files:**
- Modify: `tests/test_health.py`
- Modify: `tests/test_settings.py`
- Modify: `src/mybot/settings.py`
- Modify: `src/mybot/infrastructure/health.py`
- Modify: `src/mybot/infrastructure/telemetry.py`
- Modify: `src/mybot/api.py`

- [ ] Add tests proving a blocking dependency becomes `down` within a configured timeout, cancellation propagates as `CancelledError`, DB/Redis clients receive connect/read timeouts, blank optional secret environment values become `None`, blank OTLP never creates an exporter, and OpenAPI documents typed 200/503 readiness payloads.
- [ ] Run focused tests and capture failures caused by missing timeout/normalization/response behavior.
- [ ] Add `health_probe_timeout_seconds`, database connect timeout, Redis socket connect/read timeout, a blank-to-None optional secret validator, and typed health response models.
- [ ] Use `asyncio.timeout` per probe while explicitly re-raising cancellation; pass driver-specific timeout options when constructing clients.
- [ ] Run focused tests until green.

### Task 2: Recursively immutable JSON contracts

**Files:**
- Modify: `tests/contracts/test_messages.py`
- Modify: `tests/contracts/test_tools_memory_plugins.py`
- Create: `src/mybot/contracts/json.py`
- Modify: `src/mybot/contracts/messages.py`
- Modify: `src/mybot/contracts/tools.py`
- Modify: `src/mybot/contracts/plugins.py`

- [ ] Add tests that attempt nested mapping assignment, list append, and mutation inside raw refs, schemas, result data, and result error details; also assert JSON serialization preserves ordinary object/array output.
- [ ] Run focused contract tests and capture the current successful mutations as failures.
- [ ] Introduce recursive `FrozenJsonObject`/tuple conversion with Pydantic validation and serialization hooks.
- [ ] Replace mutable public JSON fields without changing their JSON wire shape.
- [ ] Run focused contract tests until green.

### Task 3: Runtime and migration regressions

**Files:**
- Modify: `tests/test_runtime.py`
- Create: `tests/test_migrations.py`
- Modify: `alembic/versions/20260714_0001_foundation.py`

- [ ] Change the runtime test to start a lifecycle task, observe it running, then signal cancellation and await clean exit.
- [ ] Add a migration source test proving downgrade drops `system_kv` but never drops the shared `vector` extension.
- [ ] Run both tests and capture the migration failure.
- [ ] Remove extension destruction from downgrade and run both tests green.

### Task 4: Fresh, recoverable frontend health state

**Files:**
- Modify: `web/src/App.test.tsx`
- Create: `web/src/health/types.ts`
- Create: `web/src/health/useReadiness.ts`
- Create: `web/src/components/ReadinessConsole.tsx`
- Modify: `web/src/App.tsx`
- Modify: `web/src/App.css`

- [ ] Add fake-timer tests for fetch timeout, healthy-to-degraded polling, network-error-to-recovered polling, hidden-document reduced polling, and stale-data announcement.
- [ ] Run Vitest and capture failures because the current effect fetches only once and never marks stale.
- [ ] Implement a bounded aborting fetch loop with normal interval, error backoff, hidden interval, stale threshold, and cleanup-safe scheduling.
- [ ] Keep `App` as composition, move health acquisition to the hook, and move the existing industrial markup into focused stateless components without changing accessible landmarks or visual direction.
- [ ] Run Vitest and the production build until green.

### Task 5: Independent web health, plugin isolation, and unprivileged image

**Files:**
- Modify: `web/Dockerfile`
- Modify: `web/nginx.conf`
- Modify: `compose.yaml`
- Modify: `.env.example`
- Modify: `README.md`

- [ ] Run static assertions showing web currently waits for API readiness, uses proxied API health, runs privileged Nginx on port 80, and plugin-runner inherits database/Redis secrets plus the general backend network.
- [ ] Switch to the unprivileged Nginx image/high port, add `/static-health`, run as non-root with all capabilities dropped, and remove the web dependency on API health.
- [ ] Define plugin-runner without the shared app environment or backend network; give it only a placeholder broker URL/config and an internal control network shared with API.
- [ ] Validate Compose schema and documented environment names.

### Task 6: Container and integration CI

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] Add image build coverage for Python and web Dockerfiles.
- [ ] Add an integration job that starts PostgreSQL and Redis services, runs Alembic upgrade/downgrade/upgrade, starts the API, and curls live/ready endpoints.
- [ ] Validate workflow schema; Docker execution remains CI-only when the local CLI is unavailable.

### Task 7: Final verification and commit

- [ ] Run frozen uv and pnpm installs.
- [ ] Run Python tests, Ruff, Pyright, Vitest, and frontend build.
- [ ] Generate Alembic offline upgrade SQL and confirm one head.
- [ ] Validate Compose and GitHub workflow schemas statically.
- [ ] Inspect staged diff for secrets, conflict markers, and whitespace errors.
- [ ] Commit with `fix: harden platform foundation` and report exact results and limitations.
