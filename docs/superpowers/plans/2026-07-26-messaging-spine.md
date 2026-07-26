# Messaging Spine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver Milestone 2 of the roadmap: an end-to-end, at-least-once message loop on QQ (external NapCat, OneBot v11 WebSocket) and Telegram (Bot API long polling) with rule-based replies and no LLM involvement — platform event → `MessageEnvelope` → Redis Streams → agent-worker → `TurnDecision` → `ReplyPlan` → outbound stream → platform delivery, with conversations and messages persisted.

**Architecture:** The gateway process owns every platform connection and all outbound delivery; the agent-worker owns computation. They communicate only through Redis Streams (`ingest`, `outbound`) using consumer groups, explicit acks, and a capped dead-letter stream, so either process can restart without losing or double-answering messages. Translators are pure functions from platform payloads to the existing frozen contracts; transports are thin cancellable IO shells around them, run under the Milestone 1 `LifecycleService` runtime. New SQLAlchemy repositories persist conversations and messages behind protocols so unit tests use fakes and the CI integration job proves the real drivers.

**Tech Stack:** Python 3.12, Pydantic v2, websockets (new dependency), httpx, redis asyncio (Streams), SQLAlchemy 2 async, Alembic, structlog, Pytest, Hypothesis, GitHub Actions.

---

## File map

- `src/mybot/adapters/identity.py`: platform-prefixed identity and connection normalization.
- `src/mybot/adapters/qq/*.py`: OneBot v11 translation, capabilities, WebSocket transport, send actions.
- `src/mybot/adapters/telegram/*.py`: Update translation, capabilities, long-poll transport, send API.
- `src/mybot/infrastructure/streams.py`: ingest/outbound stream broker with groups, acks, dedupe, DLQ.
- `src/mybot/infrastructure/database.py`: shared async engine/session factory built from settings.
- `src/mybot/repositories/*.py`: conversation and message persistence behind protocols.
- `src/mybot/engine/*.py`: rule-based turn policy and deterministic direct replies.
- `src/mybot/services/gateway.py`, `src/mybot/services/agent_worker.py`: lifecycle services replacing the idle shells for these two roles.
- `alembic/versions/20260726_0002_conversations_messages.py`: conversations/messages tables.
- `tests/adapters/*`, `tests/infrastructure/test_streams.py`, `tests/repositories/*`, `tests/engine/*`, `tests/services/*`: behavior coverage; integration-marked modules run against real PostgreSQL/Redis in CI.

**Not in this milestone:** LLM calls, memory writes, plugin execution, operator authentication, proactive messages, media download/re-upload (image and file segments carry platform URLs only), message editing, and any web UI change.

### Task 1: Inbound translation contracts for QQ and Telegram

**Files:**
- Create: `tests/adapters/platform_payloads.py`
- Create: `tests/adapters/test_qq_translate.py`
- Create: `tests/adapters/test_telegram_translate.py`
- Create: `src/mybot/adapters/__init__.py`
- Create: `src/mybot/adapters/identity.py`
- Create: `src/mybot/adapters/qq/__init__.py`
- Create: `src/mybot/adapters/qq/translate.py`
- Create: `src/mybot/adapters/telegram/__init__.py`
- Create: `src/mybot/adapters/telegram/translate.py`

- [ ] Write fixture-driven tests covering, for both platforms: private and group messages producing correct `Platform`, `ChatKind`, `chat_id`, platform-prefixed `sender_identity_id` (`qq:<user_id>`, `telegram:<user_id>`), and epoch-to-UTC `occurred_at`; text, image, file, and reply payloads mapping to the matching frozen segments with `reply_to_message_id` set; unsupported segment types degrading to a placeholder text segment rather than raising; empty-content events returning `None` instead of an envelope; the original payload preserved as frozen `raw_ref` that rejects mutation; and per-platform `PlatformCapabilities` constants (Telegram: replies/typing/editing true; QQ over OneBot v11: replies true, typing false).
- [ ] Run `uv run pytest tests/adapters -q` and retain the import failures as RED evidence.
- [ ] Implement pure translators (no IO, no clock reads) plus identity helpers and capability constants; envelope ids must derive deterministically from platform identifiers (`qq:<connection>:<message_id>`, `telegram:<connection>:<chat_id>:<message_id>`) so stream redelivery dedupes, and mention detection for group messages must recognize OneBot `at` segments targeting the bot's own id and Telegram `@username` entities, exposed as a helper the turn policy consumes.
- [ ] Run `uv run pytest tests/adapters -q` until GREEN.

### Task 2: Redis Streams broker with dedupe and dead-lettering

**Files:**
- Create: `tests/infrastructure/test_streams.py`
- Create: `src/mybot/infrastructure/streams.py`
- Modify: `src/mybot/settings.py`
- Modify: `tests/test_settings.py`
- Modify: `pyproject.toml`

- [ ] Register an `integration` pytest marker in `pyproject.toml` (markers are strict) gating tests that need real services, and add settings (with tests for defaults and bounds): `ingest_stream` = `mybot:ingest`, `outbound_stream` = `mybot:outbound`, consumer group names, a per-process consumer name derived from role + suffix, `stream_maxlen`, `stream_delivery_max_attempts`, and `stream_dedupe_ttl_seconds`.
- [ ] Write broker tests against an in-memory fake implementing the same protocol: publish appends with approximate maxlen trimming; consume yields decoded envelopes and acks only after the handler succeeds; a crashing handler leaves the entry pending and a restarted consumer reclaims it; an entry failing `stream_delivery_max_attempts` times moves to a capped dead-letter stream with the error name; duplicate envelope ids within the dedupe TTL are acked and skipped; cancellation propagates as `CancelledError` without losing unacked entries.
- [ ] Run `uv run pytest tests/infrastructure/test_streams.py tests/test_settings.py -q` and capture RED.
- [ ] Implement the broker on redis asyncio Streams (`XADD`, `XREADGROUP`, `XACK`, `XAUTOCLAIM`, `SET NX EX` dedupe) behind a protocol both the fake and the real client satisfy; payloads are contract JSON, and malformed payloads dead-letter immediately instead of crashing the consumer loop.
- [ ] Add an integration-marked test module exercising the real broker when `MYBOT_TEST_REDIS_URL` is set; run focused tests until GREEN.

### Task 3: Conversation and message persistence

**Files:**
- Create: `alembic/versions/20260726_0002_conversations_messages.py`
- Create: `src/mybot/infrastructure/database.py`
- Create: `src/mybot/repositories/__init__.py`
- Create: `src/mybot/repositories/conversations.py`
- Create: `src/mybot/repositories/messages.py`
- Create: `tests/repositories/test_repositories.py`
- Modify: `tests/test_migrations.py`

- [ ] Extend the migration source test: revision 0002 depends on 0001, creates `conversations` (uuid pk, unique `stable_key`, connection/platform/chat columns, `thread_id` nullable, timestamps) and `messages` (uuid pk, fk to conversations, `direction` check constraint in/out, nullable `platform_message_id` — assigned on delivery for outbound rows, unique `(conversation_id, direction, platform_message_id)` tolerating NULLs, `sender_identity_id`, segments JSONB, `occurred_at`, `ingested_at`, nullable raw JSONB), and downgrade drops exactly these two tables — never `system_kv`, never the `vector` extension.
- [ ] Write repository tests (integration-marked, gated on `MYBOT_TEST_DATABASE_URL`): `ConversationRepository.get_or_create` is idempotent per `ConversationKey.stable_key` and race-safe via upsert; `MessageRepository.record_inbound` maps every envelope field, returns the stored row, and reports (not raises) uniqueness conflicts so redelivered stream entries are recognized; `record_outbound` links the reply to the conversation with `platform_message_id` pending; `mark_delivered` backfills the platform-assigned message id; `recent_outbound_platform_ids(conversation)` returns the ids the turn policy needs for reply detection; a round-trip reconstructs equal frozen segment models from JSONB.
- [ ] Run the migration test RED, implement the migration and repositories (engine/session factory reuses the Milestone 1 timeout settings), and run `uv run pytest tests/test_migrations.py -q` GREEN locally plus repository tests GREEN wherever a database is available.

### Task 4: Rule-based turn policy

**Files:**
- Create: `tests/engine/test_turn_policy.py`
- Create: `src/mybot/engine/__init__.py`
- Create: `src/mybot/engine/turn_policy.py`

- [ ] Write table-driven tests over (chat kind × trigger evidence): DIRECT chats always yield `DIRECT_REPLY` with trigger `DIRECT_MESSAGE`; GROUP/CHANNEL messages yield `DIRECT_REPLY` only for a mention of the bot, a reply to one of the bot's recorded message ids, or a leading `/command`, with the matching `TurnTrigger`; everything else yields `IGNORE` with a stated reason; a message that is simultaneously mention and command prefers `COMMAND`; decisions carry confidence 1.0 (rule-based) and never raise on arbitrary envelopes (Hypothesis fuzz over segment combinations).
- [ ] Run RED, implement `decide_turn(envelope, *, self_identity, own_recent_message_ids, capabilities)` as a pure function returning `TurnDecision`, run GREEN.

### Task 5: Deterministic direct replies and the agent-worker service

**Files:**
- Create: `tests/engine/test_direct_replies.py`
- Create: `tests/services/test_agent_worker.py`
- Create: `src/mybot/engine/direct_replies.py`
- Create: `src/mybot/services/__init__.py`
- Create: `src/mybot/services/agent_worker.py`
- Modify: `src/mybot/runtime.py`
- Modify: `src/mybot/cli.py`
- Modify: `tests/test_runtime.py`

- [ ] Write reply-builder tests: `/ping` → `ReplyPlan` with text `pong`; `/status` → one-segment summary rendered from a passed-in `ReadinessResponse`; any other replied-to message → a deterministic acknowledgment template that quotes a truncated text excerpt and states that the full agent arrives in a later milestone; every plan validates against the 1–3 segment contract and sets `TypingProfile(enabled=True)` only when the platform capabilities allow typing.
- [ ] Write worker tests with fake broker/repositories: consume → get-or-create conversation → record inbound (duplicate report short-circuits to ack) → decide (feeding `recent_outbound_platform_ids` for reply detection) → on `DIRECT_REPLY` build plan, record outbound with platform id pending, publish to outbound, ack; on `IGNORE` persist then ack without publishing; handler exceptions leave the entry unacked for redelivery; the service is a `LifecycleService` that stops cleanly on the stop event mid-stream.
- [ ] Run RED; implement the reply builders and `AgentWorkerService`; extend `run_process`/`run_mode` in `runtime.py` to accept a per-mode service factory and wire `cli.py` so the `agent-worker` mode constructs the real service (other modes keep the idle shell); update the runtime test to assert the wiring.
- [ ] Run `uv run pytest tests/engine tests/services tests/test_runtime.py -q` until GREEN.

### Task 6: Gateway transports and outbound delivery

**Files:**
- Create: `tests/adapters/test_qq_transport.py`
- Create: `tests/adapters/test_telegram_transport.py`
- Create: `tests/services/test_gateway.py`
- Create: `src/mybot/adapters/qq/transport.py`
- Create: `src/mybot/adapters/telegram/transport.py`
- Create: `src/mybot/services/gateway.py`
- Modify: `src/mybot/settings.py`
- Modify: `src/mybot/cli.py`
- Modify: `tests/test_settings.py`
- Modify: `pyproject.toml`

- [ ] Add `websockets` to dependencies and settings (with tests): `napcat_ws_url` (SecretStr, validation alias `NAPCAT_WS_URL`, blank-to-None), `qq_connection_id` = `qq-main`, `telegram_connection_id` = `telegram-main`, `telegram_api_base_url` default `https://api.telegram.org`, `telegram_poll_timeout_seconds` (default 50, bounded), and gateway reconnect initial/max backoff seconds.
- [ ] Write QQ transport tests against a local test WebSocket server: connects with the access-token header, forwards translated envelopes to the ingest publisher, ignores non-message events, reconnects with capped jittered backoff after server drop, sends `send_msg` actions with an `echo` correlation id and surfaces failed action responses as logged delivery errors, and cancels cleanly mid-connection.
- [ ] Write Telegram transport tests against `httpx.MockTransport`: long-poll offset advances past processed updates, timeouts and 429/5xx responses back off without losing the offset, `sendMessage` maps reply plans (including `reply_to_message_id`) and `sendChatAction` typing fires only per the typing profile, and cancellation exits the poll loop promptly.
- [ ] Write gateway service tests: with both platforms configured it runs both adapters plus the outbound consumer concurrently under one lifecycle; with a platform unconfigured it logs one structured disable line and runs the rest; outbound entries route to the adapter matching the conversation's platform, and a successful send calls `mark_delivered` with the platform-assigned message id; typing simulation delay is computed from `TypingProfile` and is cancellable.
- [ ] Run RED, implement transports and `GatewayService`, wire the `gateway` mode in `cli.py`, and run `uv run pytest tests/adapters tests/services tests/test_settings.py -q` until GREEN.

### Task 7: Operator surface, Compose, and CI integration

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `compose.yaml`
- Modify: `.github/workflows/ci.yml`

- [ ] Add the new operator-relevant environment values to `.env.example` with placeholders and to the README settings tables; document connecting an external NapCat (token scope, loopback exposure warning) and creating a Telegram bot via BotFather, the group-reply policy, the `/ping` and `/status` commands, and the deterministic-echo placeholder behavior.
- [ ] Pass the gateway the new environment values in `compose.yaml` (keeping `NAPCAT_WS_URL` and both tokens out of the shared anchor), confirm agent-worker needs no new privileges, and validate with `docker compose --env-file .env.example config --quiet`.
- [ ] Extend the CI integration job to export `MYBOT_TEST_DATABASE_URL`/`MYBOT_TEST_REDIS_URL` and run the integration-marked stream and repository tests, then a pipeline smoke test that pushes a fixture envelope through broker → worker → outbound against the real services with fake platform transports.
- [ ] Validate the workflow schema statically; Docker execution remains CI-only when the local CLI is unavailable.

### Task 8: Final verification and commit

- [ ] Run frozen installs (`uv sync --all-groups --locked`, `pnpm --dir web install --frozen-lockfile`), then `uv run ruff check .`, `uv run pyright`, `uv run pytest -q`, `pnpm --dir web test -- --run`, and `pnpm --dir web build`.
- [ ] Generate Alembic offline upgrade SQL, confirm a single head, and confirm downgrade 0002→0001 leaves `system_kv` and the `vector` extension intact.
- [ ] Where a live NapCat or Telegram token is available locally, perform the manual acceptance pass: DM and group-mention round-trips on each configured platform, worker restart mid-conversation without duplicate replies; otherwise record the fake-transport smoke evidence as the environment limitation.
- [ ] Inspect the staged diff for secrets, conflict markers, and whitespace errors; commit with `feat: build messaging spine` and report exact test counts, evidence, and limitations.
