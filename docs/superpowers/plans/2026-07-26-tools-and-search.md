# Tools and Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver Milestone 4 of the roadmap: the agent can decide to use tools mid-turn, execute them under the existing policy contracts, and cite results. Built-in tools are SearXNG web search and a safe URL fetch/reader. The tool loop is bounded, every invocation is recorded, and sources flow into `ReplyPlan.citations` and into the delivered reply text.

**Architecture:** A `ToolRegistry` holds `Tool`s, each pairing a frozen `ToolSpec` with an async `run(context, arguments) -> ToolResult`. The registry converts specs into the OpenAI tool-calling schema for the LLM and a `ToolExecutor` enforces capability grants (`ToolContext.granted_capabilities`), blocks `approval_required` tools with an operator-facing message, and wraps each call in a per-tool timeout. The `AgentTurnEngine` gains a bounded tool loop: it offers tools to the model, executes requested calls, feeds results back, and is cut off by a per-turn call ceiling and total deadline into a forced final answer. Sources returned by tools are collected, deduped, rendered into the reply, and persisted; each tool invocation is written to a `tool_invocations` audit table linked to the turn.

**Tech Stack:** Python 3.12, httpx, Pydantic v2, SQLAlchemy async, Alembic, stdlib `ipaddress`/`html.parser`, Pytest.

---

## File map

- `src/mybot/infrastructure/llm.py`: tool-calling request/response support (`tools`, `tool_choice`, `tool_calls`, `finish_reason`).
- `src/mybot/tools/__init__.py`: `Tool` protocol, `ToolRegistry`, `ToolExecutor`, OpenAI schema conversion, source extraction.
- `src/mybot/tools/search.py`: SearXNG JSON search tool.
- `src/mybot/tools/fetch.py`: SSRF-guarded URL fetch/reader with size/time caps and HTML text extraction.
- `src/mybot/engine/agent_turns.py`: bounded tool loop, citation collection, invocation recording.
- `src/mybot/engine/reply_shaping.py`: citation rendering into reply text and `ReplyPlan.citations`.
- `alembic/versions/20260726_0004_tool_invocations.py`, `src/mybot/repositories/tool_invocations.py`: tool audit table and repository.
- `src/mybot/settings.py`, `compose.yaml`, `.env.example`, `README.md`: SearXNG URL, tool bounds, capability grants, operator docs.

**Not in this milestone:** interactive approval for `approval_required` tools (M7), plugin-provided tools (M6), memory retrieval as a tool (M5), streaming tool calls, and non-text media returned by tools.

### Task 1: LLM tool-calling support

**Files:**
- Modify: `tests/infrastructure/test_llm.py`
- Modify: `src/mybot/infrastructure/llm.py`

- [ ] Add a `ToolCall` model and extend `ChatMessage` with optional `tool_calls`, `tool_call_id`, and `name`; serialize messages minimally so a plain system/user message still emits exactly `{role, content}`.
- [ ] Extend `LlmReply` with `tool_calls` and `finish_reason`, both defaulted so existing constructions are unchanged.
- [ ] Write tests: with `tools` provided the request body carries `tools` and `tool_choice="auto"` and the response's `tool_calls` (id/name/arguments) and `finish_reason="tool_calls"` parse out; an assistant tool-call message plus a `tool` role result message round-trip into the exact OpenAI wire shape; a response with tool calls and null content does not raise; the no-tools body remains byte-for-byte as before.
- [ ] Implement and run `uv run pytest tests/infrastructure/test_llm.py -q` until GREEN.

### Task 2: Tool registry, policy, and executor

**Files:**
- Create: `tests/tools/test_registry.py`
- Create: `src/mybot/tools/__init__.py`

- [ ] Write tests: the registry converts specs into OpenAI function tools and lists only capability-satisfied tools; `ToolExecutor.execute` returns the tool's `ToolResult` on success; a tool whose required capabilities are not in `ToolContext.granted_capabilities` yields a `capability_denied` failure without running; an `approval_required` tool yields an `approval_required` operator-facing failure without running; a tool that exceeds the timeout yields a retryable `timeout` failure; an unexpected exception becomes a `tool_error` failure; cancellation propagates.
- [ ] Implement the `Tool` protocol, `ToolRegistry` (schema conversion, capability filtering), `ToolExecutor`, and a `collect_sources` helper that reads a `sources` list from `ToolResult.data`.
- [ ] Run `uv run pytest tests/tools/test_registry.py -q` until GREEN.

### Task 3: Built-in search and fetch tools

**Files:**
- Create: `tests/tools/test_search.py`
- Create: `tests/tools/test_fetch.py`
- Create: `src/mybot/tools/search.py`
- Create: `src/mybot/tools/fetch.py`

- [ ] Write search tests against `httpx.MockTransport`: a query hits `{searxng_url}/search?format=json`, results normalize to title/url/snippet, the top-N cap applies, sources are emitted for citations, and an upstream error becomes a structured failure.
- [ ] Write fetch tests with an injectable resolver: public hosts fetch and extract readable text from HTML with a size cap and truncation marker; loopback, private, link-local, and non-http schemes are refused with an `ssrf_blocked` (or `invalid_url`) failure before any request; an oversized body truncates; an upstream error is structured.
- [ ] Implement both tools (search requires capability `web.search`, fetch requires `web.fetch`; both read-only, low risk, no approval) and run `uv run pytest tests/tools -q` until GREEN.

### Task 4: Tool invocation audit table

**Files:**
- Create: `alembic/versions/20260726_0004_tool_invocations.py`
- Create: `src/mybot/repositories/tool_invocations.py`
- Modify: `src/mybot/repositories/__init__.py`
- Modify: `tests/test_migrations.py`
- Modify: `tests/repositories/test_repositories.py`

- [ ] Extend the migration source test: revision 0004 chains from 0003, creates `tool_invocations` (uuid pk, turn fk cascade, tool_id, ok bool, error_code nullable, latency_ms, created_at) plus a turn index, and downgrade drops exactly that index and table.
- [ ] Add an integration-marked repository test: `ToolInvocationRepository.record_many` writes one row per invocation linked to a turn and reads back tool_id/ok/error_code.
- [ ] Implement the migration and repository and run the migration test GREEN plus the repository test where a database is available.

### Task 5: Agent tool loop, citations, and wiring

**Files:**
- Modify: `tests/engine/test_agent_turns.py`
- Modify: `tests/engine/test_reply_shaping.py`
- Modify: `src/mybot/engine/reply_shaping.py`
- Modify: `src/mybot/engine/agent_turns.py`
- Modify: `src/mybot/services/agent_worker.py`

- [ ] Add citation rendering: `shape_reply` accepts optional citations, sets `ReplyPlan.citations`, and appends a compact sources footer to the reply text (respecting the segment cap); with no citations the output is unchanged.
- [ ] Add engine tests with a scripted fake LLM: a turn where the model requests a search then answers produces a cited reply, records a `replied` turn, and records one tool invocation; capability-denied and approval-required tool calls feed a structured error back to the model rather than crashing; the call ceiling forces a final answer after the configured number of tool calls; the total deadline yields the fallback reply and an `error` turn; command turns still never build a tool loop.
- [ ] Implement the bounded loop (offer tools, execute via `ToolExecutor`, feed results back, cut off at ceiling/deadline, force a final tools-disabled answer), collect and dedupe sources into citations, sum token usage across calls, and persist invocations after the turn row; build the registry, executor, and grants in `create_agent_worker_service`.
- [ ] Run `uv run pytest tests/engine tests/services -q` until GREEN.

### Task 6: Operator surface and final verification

**Files:**
- Modify: `src/mybot/settings.py`
- Modify: `tests/test_settings.py`
- Modify: `compose.yaml`
- Modify: `.env.example`
- Modify: `README.md`

- [ ] Add and test settings: `searxng_url`, `agent_granted_capabilities`, `tool_max_calls_per_turn`, `tool_timeout_seconds`, `turn_deadline_seconds`, `tool_fetch_max_bytes`; expose `MYBOT_SEARXNG_URL` through the Compose app environment and `.env.example`, keeping deployment-config assertions green.
- [ ] Document the tool system in the README: the two built-in tools, the SSRF stance, capability/approval policy, the bound settings, and how citations appear in replies.
- [ ] Run the full verification set (`ruff`, `pyright`, `pytest` with and without integration URLs, frontend build unchanged, Compose static validation) and commit with `feat: add tools and search`.
