# V2-M2 Multimodal, Sandbox, and Trace Implementation Plan

**Goal:** Deliver V2-M2 so MyBot can understand inbound images, emit validated image or
sticker reply segments, exercise the production Streams/agent path from an authenticated
WebChat sandbox, and explain each reply through persisted trace spans in the operator
console.

**Architecture:** Message contracts stay additive: every envelope receives a generated
`trace_id` and an `ephemeral` bit, while `SANDBOX` becomes a first-class virtual platform.
OpenAI-compatible chat messages gain typed text/image content parts. A vision preparation
service either passes those parts directly to the chat model or asks the purpose-routed
vision model for a bounded description before the normal agent loop. The sandbox publishes
ordinary `InboundEvent` JSON to the configured ingest stream; the agent worker persists,
decides, runs tools/LLM, and publishes an ordinary `OutboundMessage`; a no-network sandbox
sender completes delivery while the authenticated console polls the persisted transcript.
Ephemeral conversations remain observable but are excluded from memory retrieval,
extraction, and proactive delivery. Completed stage spans are stored in PostgreSQL and
queried by trace or conversation.

**References:** The roadmap's Koishi Sandbox, AstrBot ChatUI, and FastGPT call-chain ideas
are used as product references only. The implementation remains independent and preserves
the existing MyBot contracts, Streams backend, operator authentication, and deployment
shape.

**Not in this milestone:** ASR/TTS, video understanding, media generation, public file
upload/storage, websocket chat streaming, public plugin market, memory 2.0 merge semantics,
or distributed conversation locks.

## Task 1: Additive contracts and migration

- [x] Add RED tests for `Platform.SANDBOX`, generated/preserved `trace_id`, `ephemeral`,
  multimodal OpenAI content serialization, and outbound trace propagation.
- [x] Add migration 0008 for conversation ephemerality, message trace ids, and bounded
  `trace_span` rows with JSON attributes and useful indexes.
- [x] Add repository methods for trace recording/query and preserve existing migrations.

## Task 2: Image understanding and prompt preservation

- [x] Add RED tests showing text plus image references survive prompt assembly and pure
  image messages no longer become the English non-text placeholder.
- [x] Add configurable `describe` and `direct` vision modes using the M1 `vision` purpose;
  failures degrade to a Chinese image-reference prompt without failing the turn.
- [x] Keep image URLs out of logs/errors and bound descriptions before adding them to the
  chat prompt.

## Task 3: Rich-media reply shaping and Chinese UX

- [x] Add validated, deliberately narrow model directives for image/sticker reply segments
  and meme intent; ignore malformed directives as plain text.
- [x] Pass supported media through `shape_reply` and leave capability fallback to the pure
  QQ/Telegram encoders.
- [x] Make empty/LLM/budget/not-configured/moderation fallbacks configurable Chinese text and
  replace the character/4 estimator with a mixed CJK/Latin heuristic.

## Task 4: Ephemeral WebChat sandbox

- [x] Add authenticated operator endpoints to create/send a sandbox turn and read its
  transcript/status.
- [x] Publish a synthetic `InboundEvent` to the real ingest stream and add a virtual sandbox
  sender to the real gateway outbound consumer.
- [x] Mark the conversation ephemeral and prove memory hooks and proactive delivery are
  skipped while normal turns, tools, persistence, and delivery still run.

## Task 5: End-to-end trace spans

- [x] Record bounded spans for ingest/decision, memory retrieval, LLM calls, tool calls,
  moderation, outbound publish, and gateway delivery without raw secrets or exception text.
- [x] Associate spans with the envelope trace id and expose authenticated trace endpoints.
- [x] Keep trace failures best-effort so observability never suppresses a reply.

## Task 6: Operator console

- [x] Add a Sandbox tab with text/image URL composition, session reset, pending state, and
  transcript polling.
- [x] Add a Chinese "追踪" disclosure to conversation details showing stage, duration,
  status, recalled-memory summary, tool result, moderation decision, and model usage.
- [x] Preserve the existing conversations/models/plugins/persona/memory workflows.

## Task 7: Documentation and verification

- [x] Document vision mode, sandbox isolation, trace retention/schema, Chinese fallback
  settings, and operator workflows in README/deployment/runbook/env examples.
- [x] Run focused RED/GREEN suites, full Python and web tests, Ruff, Pyright, web build,
  migration source/integration checks when configured, and Compose static validation.
