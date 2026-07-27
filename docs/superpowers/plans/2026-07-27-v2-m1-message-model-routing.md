# V2-M1 Message Contracts and Model Routing Implementation Plan

**Goal:** Deliver V2-M1: additive rich-message contracts, forward-compatible plugin
transport negotiation, purpose-aware OpenAI-compatible model channels with failover and
Redis cooldowns, per-attempt usage/cost accounting, an authenticated model-management
console, and replay fixtures that keep the QQ core independent from NapCat-specific drift.

**Architecture:** Public message contracts remain strict and additive. Platform adapters
translate between QQ/Telegram wire shapes and typed segments, with unsupported outbound
media rendered as readable text. Plugin runner registration negotiates a transport version
before future envelope fields are emitted. Model channels are non-secret JSON stored under
`models.channels` in `system_kv`; each entry refers to an environment-variable name for its
API key and maps chat, memory, embedding, and vision purposes to models and price snapshots.
A cached router selects priority groups with weighted rotation, skips Redis-cooled channels,
fails over only on retryable failures, and records every attempt in `llm_call_log`.

**References checked 2026-07-27:** Satori standard elements (`at`, `emoji`, `audio`,
`quote`), OneBot v12 message segments and `alt_message`, current NapCat README, current
Lagrange.Core V2 notice, and the current Dify/FastGPT repositories' provider-management
surfaces. These are design references only; implementation remains independent.

**Not in this milestone:** image understanding, ASR/TTS, media generation or download/CAS
integration, WebChat sandbox, trace spans, memory merge semantics, public plugin market,
or multi-worker distributed conversation locks.

## Task 1: Contract compatibility and typed segments

- [x] Add RED contract tests for `at`, `sticker`, and `voice`, additive capability bits,
  `ReplyPlan.media_segments`, and strict validation.
- [x] Add a plugin transport version to registration; old requests without the field remain
  version 1, unsupported versions fail before registration, and health exposes the negotiated
  version.
- [x] Export every new public type from `mybot.contracts` and keep existing JSON round trips.

## Task 2: QQ and Telegram translation/replay

- [x] Add RED fixture tests for OneBot `at/face/record`, Telegram entity mentions,
  sticker/voice input, and pure outbound encoders.
- [x] Add representative recorded OneBot v11 fixtures and replay them through the translator;
  keep NapCat-only normalization in `adapters/qq`.
- [x] Update transports to consume encoder output while preserving current text behavior and
  readable fallback for unsupported capability bits.

## Task 3: Model channel contracts and router

- [x] Add RED tests for channel validation, env-only secret references, cached `system_kv`
  configuration, priority plus weighted rotation, purpose-specific model selection, Redis
  cooldown, retryable failover, and permanent-error stop.
- [x] Preserve legacy `MYBOT_LLM_*`/`MYBOT_EMBEDDING_*` settings as the fallback channel when
  no runtime channel list exists.
- [x] Route chat, memory extraction, and embeddings through purpose-bound clients; expose a
  vision-bound client for M2 without using it yet.

## Task 4: Attempt ledger and migration

- [x] Add migration 0007 and source tests for `llm_call_log`, including channel, purpose,
  model, status, usage, latency, nullable conversation, price snapshots, computed cost, and
  error code.
- [x] Add repository tests and record success, retryable failure, and permanent failure
  without storing URLs, API keys, response bodies, or raw exception text.
- [x] Add aggregate queries by day, conversation, and channel.

## Task 5: Operator API and web console

- [x] Add authenticated GET/PUT channel endpoints, a connectivity-test endpoint, health and
  monthly-usage views, validation that API-key references are environment variable names,
  and operator audit entries without secrets.
- [x] Add a Models tab that edits non-secret channel fields, shows channel health and monthly
  cost/usage, and never accepts or renders API-key values.
- [x] Keep legacy Overview token cards and all existing operator behavior unchanged.

## Task 6: Documentation and verification

- [x] Document runtime channel JSON, secret env references, fallback precedence, cooldown,
  cost semantics, and the Lagrange.OneBot hot-spare procedure.
- [x] Run focused RED/GREEN suites, then the commands behind `make verify`; run migration integration checks when
  configured and report any external-service blocker exactly.

Verification note (2026-07-27): Windows has no `make` executable, so the five commands in
the `verify` target were run directly. Docker CLI/Compose static validation works, but the
Docker Desktop Linux engine is absent/unavailable; PostgreSQL/Redis integration tests are
therefore collected and skipped rather than claimed as live-verified.
