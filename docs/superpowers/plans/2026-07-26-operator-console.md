# Operator Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver Milestone 7 of the roadmap: the web shell becomes a real, authenticated operations console. A static bearer token guards every operator API (health probes stay open), the operator can browse conversations with turn and tool detail, filter and revoke memories, review plugins, edit the persona, and pre-approve `approval_required` tools — all without touching the database — and a minimal metrics surface (token usage, turn latency, queue depths, dead letters) renders with sparklines in the existing mission-control visual language.

**Architecture:** An `OperatorAuthMiddleware` on the API requires `Authorization: Bearer` matching `MYBOT_OPERATOR_TOKEN` for everything except `/health/*` and `/plugin-broker/*` (the latter stays an internal-network control plane, documented; M8 can add a service token). Auth fails closed when no token is configured, and repeated failures are rate limited per client. Read endpoints under `/operator/*` are thin SQL views (`operator_views.py`) over the existing tables; usage and latency aggregate from `turns`, queue depth and dead-letter counts read Redis stream lengths. Mutations — persona, tool approvals, memory revocation — write through `system_kv`/repositories and each records a row in a new `operator_audit` table (migration 0006). Standing tool approvals live in `system_kv` under `tools.approved_ids`; the worker's `ToolExecutor` gains an `ApprovalSource` (TTL-cached) so an approval flipped in the UI makes an `approval_required` tool callable within the cache window. The frontend keeps the graphite mission-control shell: an in-memory token gate (no browser storage), then tabbed panels — overview/usage, conversations, memories, plugins, persona — each Vitest-covered; nginx and the Vite dev server proxy `/operator` same-origin so no CORS surface appears.

**Tech Stack:** FastAPI middleware + routers, SQLAlchemy text views, Alembic, Redis XLEN, React 19 + TypeScript + Vitest, nginx.

---

## File map

- `src/mybot/operator/auth.py`: bearer middleware, fail-closed policy, failure rate limiter.
- `src/mybot/operator/api.py`: `/operator` router (reads, mutations, metrics).
- `src/mybot/repositories/operator_views.py`: conversation/message/turn/memory/usage read queries.
- `src/mybot/repositories/audit.py` + `alembic/versions/20260726_0006_operator_audit.py`: mutation audit trail.
- `src/mybot/tools/__init__.py` + `src/mybot/services/agent_worker.py`: approval source consumed by the executor.
- `web/src/operator/*`: token gate, API client, panels, styles; `web/nginx.conf`, `web/vite.config.ts` proxy `/operator`.

**Not in this milestone:** multi-operator accounts or sessions (one static token), interactive per-invocation approval prompts (approvals are standing grants), broker/service-to-service auth (M8), OTLP dashboards themselves (wiring + docs only), and websocket live tails.

### Task 1: Authentication middleware and settings

- [ ] Settings with tests: `operator_token` (SecretStr, blank→None), `operator_auth_max_failures`, `operator_auth_window_seconds`, `tool_approvals_cache_ttl_seconds`.
- [ ] Middleware tests over ASGI: `/health/*` and `/plugin-broker/*` pass untouched; `/operator/*` without or with a wrong token → 401 and a failure recorded; correct token → 200; token unset → 403 fail-closed; more than the allowed failures inside the window → 429 even before token checking; failures expire with an injected clock.
- [ ] Implement and wire into `create_app`; run GREEN.

### Task 2: Operator API, audit trail, approvals, and metrics

- [ ] Migration 0006 `operator_audit` (uuid pk, action, detail JSONB, created_at) with source test chaining from 0005; `AuditRepository.record`.
- [ ] `operator_views.py`: conversation search/list (newest activity first), message texts, turns joined with their tool invocations, memory listing with scope/privacy/revoked filters, daily usage aggregates (14 days), and a 24h turn metrics rollup (counts by outcome, average and p95 latency).
- [ ] `/operator` endpoints: `GET conversations`, `GET conversations/{id}/messages`, `GET conversations/{id}/turns`, `GET memories`, `POST memories/{id}/revoke`, `GET plugins` (broker health view), `GET usage`, `GET metrics` (usage rollup + Redis queue and dead-letter depths), `GET/PUT config/persona`, `GET/PUT config/approvals`; every mutation writes an audit row.
- [ ] Executor approvals: `ApprovalSource` protocol on `ToolExecutor`; approval-required tools execute when their id is in the approved set; `SystemKvApprovals` caches `tools.approved_ids` with a TTL; worker wiring; registry tests updated.
- [ ] Integration-marked API tests over ASGI against real PostgreSQL/Redis: seed a conversation with turns and invocations and read it back; revoke a memory and see it flagged; persona round-trip plus audit rows; approvals round-trip; usage/metrics shapes.
- [ ] Run GREEN.

### Task 3: The console frontend

- [ ] `web/src/operator/api.ts` typed client attaching the bearer token; token lives only in memory.
- [ ] Panels in the existing visual language: overview usage sparkline + queue/dead-letter tiles; conversation browser (search → list → messages and turns with tool detail); memory browser (scope/privacy filters, revoke buttons with optimistic refresh); plugins view (manifest/version/grants + approval toggles for approval-required tools); persona editor (load, edit, save with confirmation).
- [ ] Token gate renders before any operator fetch; a 401 drops back to the gate with a message.
- [ ] Vitest coverage for the gate and every panel (mocked fetch), keeping the existing App accessibility tests green; nginx adds a `/operator/` proxy location alongside `/health`; the Vite dev proxy mirrors it.
- [ ] `pnpm --dir web test -- --run` and `pnpm --dir web build` GREEN.

### Task 4: Operator surface and final verification

- [ ] Compose passes `MYBOT_OPERATOR_TOKEN` to the api service; `.env.example` documents it (empty = operator API disabled, fail closed); README gains the console guide, the auth model, the approvals flow, and an end-to-end OTLP wiring example (collector endpoint env, what gets traced today).
- [ ] Full verification with and without integration URLs, frontend tests and build, Compose static validation, deployment-config assertions green; commit with `feat: add authenticated operator console`.
