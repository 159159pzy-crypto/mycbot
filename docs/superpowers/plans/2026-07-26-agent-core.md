# Agent Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver Milestone 3 of the roadmap: answerable non-command messages become real agent turns — an OpenAI-compatible LLM produces the reply with persona and bounded conversation history — while commands stay deterministic, failures degrade to explicit fallback replies, every turn is recorded for audit, and daily token ceilings bound spend.

**Architecture:** A typed `LlmClient` speaks the OpenAI-compatible `/chat/completions` protocol behind httpx with bounded timeouts and retry. The turn policy now routes non-command answerable messages to `TurnAction.AGENT`; the worker serializes turns per conversation and delegates them to an `AgentTurnEngine` that assembles prompts from persona (`system_kv` override, settings default) plus a token-budgeted history window, shapes model output into a validated `ReplyPlan`, records a `turns` row for every outcome, and enforces Redis-backed daily token ceilings. When no LLM endpoint is configured the engine answers with an explicit deterministic fallback, so the spine remains fully operable.

**Tech Stack:** Python 3.12, httpx, Pydantic v2, SQLAlchemy 2 async, Alembic, Redis (token counters), structlog, Pytest.

---

## File map

- `src/mybot/infrastructure/llm.py`: OpenAI-compatible chat client, typed errors, usage accounting.
- `src/mybot/engine/prompt.py`: token estimation, history windowing, prompt assembly.
- `src/mybot/engine/reply_shaping.py`: model text → validated 1–3 segment `ReplyPlan`.
- `src/mybot/engine/agent_turns.py`: the agent turn engine — budget gate, LLM call, fallback, audit.
- `src/mybot/infrastructure/budget.py`: daily token ledger over an increment-capable backend.
- `src/mybot/repositories/turns.py`, `src/mybot/repositories/system_kv.py`: turn audit and persona storage.
- `alembic/versions/20260726_0003_turns.py`: the turns audit table.
- `src/mybot/engine/turn_policy.py`, `src/mybot/services/agent_worker.py`: AGENT routing and wiring.

**Not in this milestone:** tool calling, memory, streaming responses, multi-provider abstraction, operator UI for persona editing (the `system_kv` key is documented and editable by SQL), and cross-process conversation serialization (per-conversation ordering is in-process per worker).

### Task 1: LLM client and settings

**Files:**
- Create: `tests/infrastructure/test_llm.py`
- Create: `src/mybot/infrastructure/llm.py`
- Modify: `src/mybot/settings.py`
- Modify: `tests/test_settings.py`

- [ ] Add settings with bounds and tests: `llm_base_url` (blank→None disables the agent), `llm_api_key` SecretStr blank→None, `llm_model`, `llm_temperature`, `llm_max_output_tokens`, `llm_timeout_seconds`, `llm_max_retries`, `agent_system_prompt` default persona, `agent_history_max_messages`, `agent_history_token_budget`, `agent_daily_token_ceiling`, `agent_conversation_daily_token_ceiling` (0 disables a ceiling).
- [ ] Write `LlmClient` tests against `httpx.MockTransport`: success parses text and prompt/completion usage; 429 and 5xx retry up to `max_retries` then raise retryable `LlmError`; other 4xx raise non-retryable immediately; timeouts raise retryable; empty content raises; the auth header and request body (model, messages, temperature, max_tokens) are exactly as configured.
- [ ] Run RED, implement the client with zero-base backoff injectable for tests, run GREEN.

### Task 2: Turn audit and persona storage

**Files:**
- Create: `alembic/versions/20260726_0003_turns.py`
- Create: `src/mybot/repositories/turns.py`
- Create: `src/mybot/repositories/system_kv.py`
- Modify: `src/mybot/repositories/__init__.py`
- Modify: `tests/test_migrations.py`
- Modify: `tests/repositories/test_repositories.py`

- [ ] Extend the migration source test: revision 0003 chains from 0002, creates `turns` (uuid pk, conversation fk, nullable inbound message fk, action, trigger, nullable model, token counts, latency, outcome constrained to replied/fallback/budget_exceeded/error, nullable error, created_at) plus a conversation/created index, and downgrade drops exactly that index and table.
- [ ] Add integration-marked repository tests: `TurnRepository.record_turn` round-trips every field; `SystemKvRepository.get` returns None for missing keys and `set` upserts JSON values read back intact.
- [ ] Run RED, implement the migration and repositories, run GREEN where a database is available.

### Task 3: Prompt assembly and reply shaping

**Files:**
- Create: `tests/engine/test_prompt.py`
- Create: `tests/engine/test_reply_shaping.py`
- Create: `src/mybot/engine/prompt.py`
- Create: `src/mybot/engine/reply_shaping.py`
- Modify: `src/mybot/repositories/messages.py`

- [ ] Add `MessageRepository.recent_texts` returning newest-first (direction, sender, text) rows extracted from segment JSON, with an integration-marked test.
- [ ] Write prompt tests: the system message contains the persona and platform/chat context and states the plain-text reply constraints; history is included oldest-first, attributes group speakers, maps outbound rows to assistant turns; a small token budget drops the oldest history first and never the inbound message; token estimation is monotonic in text length.
- [ ] Write shaping tests: whitespace-only model output falls back to an explicit apology text; long output splits on paragraph boundaries into at most 3 segments with a hard per-segment character cap and ellipsis truncation; typing follows platform capabilities; the result always validates as a `ReplyPlan`.
- [ ] Run RED, implement both modules as pure functions, run GREEN.

### Task 4: Token budget ledger

**Files:**
- Create: `tests/infrastructure/test_budget.py`
- Create: `src/mybot/infrastructure/budget.py`
- Modify: `src/mybot/infrastructure/streams.py`

- [ ] Add an `increment(key, amount, ttl_seconds) -> int` method to both `MemoryStreamBackend` (clock-aware expiry) and `RedisStreamBackend` (INCRBY + first-write EXPIRE).
- [ ] Write ledger tests: global and per-conversation daily counters accumulate under date-scoped keys; `allows` turns False only when a configured ceiling is crossed; a ceiling of 0 never blocks; keys carry a TTL so counters expire; an integration-marked test exercises the real Redis increment.
- [ ] Run RED, implement `TokenBudget`, run GREEN.

### Task 5: Agent turn engine and worker integration

**Files:**
- Create: `tests/engine/test_agent_turns.py`
- Create: `src/mybot/engine/agent_turns.py`
- Modify: `src/mybot/engine/turn_policy.py`
- Modify: `tests/engine/test_turn_policy.py`
- Modify: `src/mybot/services/agent_worker.py`
- Modify: `tests/services/test_agent_worker.py`

- [ ] Change the policy so answerable non-command messages yield `TurnAction.AGENT` (commands remain `DIRECT_REPLY`); update policy tests accordingly.
- [ ] Write engine tests with a fake LLM: a successful turn returns the shaped reply and records a `replied` turn with model, usage, and latency; an `LlmError` returns the apologetic fallback and records `error` with the error name; an unconfigured LLM returns the not-configured fallback recording `fallback`; a crossed ceiling returns the budget refusal recording `budget_exceeded` without calling the LLM; token usage is consumed into the ledger after replies.
- [ ] Update worker tests: AGENT decisions flow through the engine and publish the engine's plan; per-conversation serialization keeps two concurrent messages for one conversation in order while different conversations proceed concurrently; command messages still bypass the engine deterministically.
- [ ] Run RED, implement the engine and worker wiring (per-`stable_key` in-process locks, engine built in `create_agent_worker_service` from settings), run GREEN.

### Task 6: Operator surface and final verification

**Files:**
- Modify: `.env.example`
- Modify: `compose.yaml`
- Modify: `README.md`

- [ ] Document the agent: LLM env values with placeholders (endpoint, key, model), the persona `system_kv` override key, budget ceilings, fallback behavior when unconfigured or failing, and the token-estimation caveat; pass the LLM environment to the agent-worker service only, keeping it out of the shared anchor.
- [ ] Validate Compose statically and keep every deployment-config assertion green.
- [ ] Run the full verification set (`ruff`, `pyright`, `pytest` with and without integration URLs, frontend tests/build, Compose static validation) and commit with `feat: add agent core`.
