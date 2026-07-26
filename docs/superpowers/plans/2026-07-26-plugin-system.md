# Plugin System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver Milestone 6 of the roadmap: third-party behavior loads from a `PluginManifest` and runs in the isolated plugin-runner with no new privileges. Plugin-declared tools appear in the agent's tool registry with their declared risk/approval settings, event hooks receive envelopes and decisions read-only, capability grants are enforced (with an audit entry) at the broker, one crashing plugin cannot take the stack down, and an in-repo example plugin proves the path end to end.

**Architecture:** The broker lives inside the API process — the only component on both the backend and the internal `plugin-control` network. The plugin-runner stays a pure HTTP *client* of `MYBOT_PLUGIN_BROKER_URL` (its only privilege): it imports plugins from `MYBOT_PLUGIN_CONFIG` entrypoints, registers their manifests, then long-polls `/plugin-broker/work` for tool invocations and events, executing each under a per-call timeout with crash isolation and posting `ToolResult` JSON back. On the other side, the agent-worker reaches the same broker over the backend network: a `BrokerToolCatalog` merges the static M4 registry with the broker's live plugin tool list (TTL-cached, refreshed at turn start via a new async `refresh()` on the catalog protocol), and a `PluginToolProxy` relays executions through `/plugin-broker/invoke`, which parks an asyncio future until the runner posts the result. Registration refuses any manifest whose `requested_capabilities` exceed the operator's `MYBOT_PLUGIN_CAPABILITY_GRANTS`, recording an audit entry. Broker state is in-process (one API instance), documented as such.

**Tech Stack:** Python 3.12, FastAPI, httpx (ASGITransport in tests), Pydantic v2 contracts from Milestone 1, Pytest.

---

## File map

- `src/mybot/plugins/broker.py`: broker state machine + FastAPI router (register, tools, invoke, work, result, events, health).
- `src/mybot/plugins/sdk.py`: `SimplePlugin` authoring surface (manifest + tool handlers + event handler).
- `src/mybot/plugins/examples/dice.py`: the reference dice-roller plugin with a `message` event hook.
- `src/mybot/plugins/runner.py`: `PluginRunnerService` lifecycle (load, register with retry, poll, execute, report).
- `src/mybot/tools/plugins.py`: worker-side `BrokerToolCatalog` and `PluginToolProxy`.
- `src/mybot/tools/__init__.py`: `ToolCatalog` protocol with async `refresh()`; `ToolRegistry` implements it.
- `src/mybot/api.py`, `src/mybot/services/agent_worker.py`, `src/mybot/services/__init__.py`: wiring.
- `src/mybot/settings.py`, `compose.yaml`, `.env.example`, `README.md`: operator surface.

**Not in this milestone:** interactive approval flows (M7 — `approval_required` plugin tools register but stay refused at execution), plugin-originated proactive messages, plugin persistence, hot reload (re-register by restarting the runner), multi-runner sharding, and broker authentication (M7 adds API auth; until then every surface is loopback/internal-only as documented since Milestone 1).

### Task 1: Settings and the broker

**Files:**
- Create: `src/mybot/plugins/__init__.py`
- Create: `src/mybot/plugins/broker.py`
- Modify: `src/mybot/api.py`
- Modify: `src/mybot/settings.py`
- Modify: `tests/test_settings.py`
- Create: `tests/plugins/test_broker.py`

- [ ] Add settings with tests: `plugin_broker_url` (blank→None; doubles as the worker's broker address and the runner's target), `plugin_config` (blank→None JSON), `plugin_capability_grants` (JSON object string parsed by a `plugin_grants()` helper), `plugin_invoke_timeout_seconds`, `plugin_poll_wait_seconds`, `plugin_catalog_ttl_seconds`, `plugin_result_max_chars`, all bounded.
- [ ] Implement `PluginBroker`: runner registration validating `PluginManifest` (422 on contract violations); capability review refusing any manifest whose requested capabilities exceed the operator grants with a 403 and an audit entry; a live tool catalog (plugin id, spec, runner); invoke → parked future → runner work queue; long-poll work delivery of invocations plus read-only envelope/decision events (bounded queues, drop-oldest); result posting resolving futures; health/inspection payload listing plugins, versions, and granted capabilities.
- [ ] Wire a broker instance and its router into `create_app` (exposed as `app.state.plugin_broker`), keeping existing health tests untouched.
- [ ] Write ASGI-transport tests: valid registration lists tools; oversized capability request → 403 + audit entry; invalid manifest → 422; invoke/work/result round-trip returns the tool result to a concurrent invoker; unknown tool → 404; invoke with no runner or an unanswered invocation → 504 within the timeout; posted events arrive in the next work response; re-registration after a runner restart replaces the old tools.
- [ ] Run focused tests GREEN.

### Task 2: SDK, example plugin, and the runner service

**Files:**
- Create: `src/mybot/plugins/sdk.py`
- Create: `src/mybot/plugins/examples/__init__.py`
- Create: `src/mybot/plugins/examples/dice.py`
- Create: `src/mybot/plugins/runner.py`
- Create: `tests/plugins/test_runner.py`
- Create: `tests/plugins/crash_plugin_fixture.py`

- [ ] Implement `SimplePlugin` (frozen manifest, tool handler map accepting sync or async callables, optional event handler) and the example `example.dice` plugin: a `roll_dice` tool (bounded sides/count, risk NONE, no capabilities, no approval) plus a `message` event hook counter.
- [ ] Implement `PluginRunnerService`: parse `MYBOT_PLUGIN_CONFIG` JSON entrypoints, import via `module:attr` (a failing import skips that plugin with a log, never aborts the rest), register with bounded-backoff retry, long-poll work, execute each invocation under a per-call timeout with the result JSON capped at `plugin_result_max_chars`, wrap handler crashes into structured `ToolResult` failures, deliver events read-only to subscribing plugins, and post results back; cancellation propagates cleanly.
- [ ] Write tests against the real broker over ASGI: the dice plugin registers and answers an invocation end to end; a deliberately crashing plugin (fixture module) returns a structured failure and the loop keeps serving the next invocation; a per-call timeout produces a timeout failure; an unknown-entrypoint config skips gracefully.
- [ ] Run focused tests GREEN.

### Task 3: Worker-side catalog, proxy, and event reporting

**Files:**
- Modify: `src/mybot/tools/__init__.py`
- Create: `src/mybot/tools/plugins.py`
- Create: `tests/tools/test_plugin_catalog.py`
- Modify: `src/mybot/engine/agent_turns.py`
- Modify: `src/mybot/services/agent_worker.py`
- Modify: `tests/services/test_agent_worker.py`

- [ ] Introduce the `ToolCatalog` protocol (get/available/openai_tools + async `refresh()`); `ToolRegistry.refresh()` is a no-op; the engine refreshes its catalog once per turn before offering tools; `ToolExecutor` accepts any catalog.
- [ ] Implement `BrokerToolCatalog` (static registry merged with TTL-cached broker tools; broker outages degrade to static-only with a log) and `PluginToolProxy` (relays through `/plugin-broker/invoke`, mapping transport failures, 404s, and 504s to structured `ToolResult` failures so the agent loop keeps its contract).
- [ ] Add worker event reporting: an `EventSink` posting envelope + decision to the broker after each non-duplicate turn, failure-swallowed so chat latency and delivery never depend on plugins; wire a broker-backed sink when `plugin_broker_url` is set.
- [ ] Write tests: catalog merge and TTL refresh against a scripted broker; proxy failure mapping; an end-to-end executor→broker→runner→dice-result acceptance test with the real runner service polling over ASGI; worker tests for event posting via a fake sink.
- [ ] Run focused tests GREEN.

### Task 4: Wiring, operator surface, and final verification

**Files:**
- Modify: `src/mybot/services/__init__.py`
- Modify: `tests/test_runtime.py`
- Modify: `compose.yaml`
- Modify: `.env.example`
- Modify: `README.md`

- [ ] `create_service` returns the real `PluginRunnerService` when both `plugin_broker_url` and `plugin_config` are set (idle otherwise, preserving today's default); the runtime wiring test covers both branches.
- [ ] Compose: agent-worker gains `MYBOT_PLUGIN_BROKER_URL` (default `http://api:8000`), the api service gains `MYBOT_PLUGIN_CAPABILITY_GRANTS`; the plugin-runner block keeps exactly its two environment keys so every deployment-config assertion stays green; `.env.example` documents the new values with the dice example config.
- [ ] README: plugin authoring guide (SimplePlugin, manifest, entrypoint config), the capability grant/refusal model, event hooks, resource bounds, and an honest security statement — the container boundary plus broker policy is not a hostile-code sandbox.
- [ ] Run the full verification set with and without integration URLs and commit with `feat: add manifest-driven plugin system`.
