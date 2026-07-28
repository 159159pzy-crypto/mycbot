# V2-M4 Group Personality and Proactivity Implementation Plan

**Goal:** Turn MyBot from a mention-only group utility into a restrained group member
with configurable participation, conversation-specific profiles, learned local style,
subject relationships, versioned personas, and policy-gated LLM heartbeats.

**Architecture:** A profile is the single per-conversation runtime policy object. It binds
an immutable persona version, model tier, tool grants, memory policy, and willingness
policy to a conversation. The worker resolves one profile before deciding whether to
participate, then carries its ids and tier through model-call accounting. Group messages
that are not commands, mentions, or replies enter a deterministic willingness scorer;
the scorer combines configured keywords, question/request cues, persona and memory
semantic relevance, recent group activity, and recent bot presence, then persists its
components for audit and metrics. Expression examples remain CONVERSATION-scoped SHARED
memory, while relationships remain versioned SUBJECT memory, so M3 SQL privacy and
`/forget` semantics continue to be authoritative. Scheduled expression/relationship
learning and heartbeat generation are bounded, leased, failure-isolated maintenance
jobs. Heartbeats reuse profile persona, core memory, recent dialogue, existing opt-in,
quiet hours, frequency caps, and the `HEARTBEAT_OK` silent acknowledgement.

**References checked 2026-07-28:** MaiBot current `reply_necessity.py` uses explicit
relevance/content/pressure/presence components and conservative thresholding; its
expression learner stores situation/style examples per session with filtering, review,
deduplication, counts, and concurrency bounds. OpenClaw's current heartbeat runtime uses
active hours, cooldown/flood guards, compact scratch/context, cheaper model options, and
suppresses `HEARTBEAT_OK`. LangBot persists bot-to-pipeline routing rules; ChatLuna
resolves conversation constraints and preset/model bindings. Dify's current workflow
history restores by creating a new active version rather than mutating old history.
These are mechanism references only; implementation remains independent.

## Task 1: Contracts, schema, and repositories

- [x] Add RED contract tests for profile policies, persona versions, relationships, and
  willingness score components.
- [x] Add migration 0010 for profiles, persona versions, conversation bindings,
  willingness audit, heartbeat audit, relationship score, and LLM profile attribution.
- [x] Add integration repositories with atomic version creation, rollback-as-new-version,
  binding resolution, and relationship successor invalidation.

## Task 2: Profile runtime and persona versioning

- [x] Resolve a default profile without breaking the existing system-kv persona override.
- [x] Apply profile persona, model tier, tool grants, and memory policy on every turn.
- [x] Attribute model calls to profile/persona version and keep legacy channels working.

## Task 3: Reply willingness

- [x] Score unaddressed group messages from explicit cues, semantic relevance, activity,
  cooldown/presence, and profile sensitivity; keep direct/mention/reply behavior intact.
- [x] Persist decisions and expose blocked/allowed counts and component detail to metrics.
- [x] Fail closed for unsolicited participation when semantic dependencies fail.

## Task 4: Expression and relationship memory

- [x] Learn bounded, reviewed expression candidates from group dialogue into
  CONVERSATION/SHARED `EXPRESSION` memory through the M3 merge pipeline.
- [x] Inject only same-conversation top-N expression examples into the prompt.
- [x] Increment and summarize SUBJECT `RELATIONSHIP` memory, decay familiarity, inject it
  into prompts, and keep `/forget` and operator revocation authoritative.

## Task 5: LLM heartbeat

- [x] Replace the fixed proactive template with a profile/core-memory/recent-topic prompt.
- [x] Suppress `HEARTBEAT_OK`, retain opt-in/quiet/frequency/policy gates, and audit sent,
  suppressed, and failed generations with bounded model accounting.

## Task 6: Operator surface and docs

- [x] Upgrade the persona page to profile management, bindings, immutable persona history,
  diff, rollback, willingness policy, tool/memory/model settings, and relationships.
- [x] Preserve the current Apple-style hierarchy, immediate feedback, restrained motion,
  and reduced-motion behavior.
- [x] Document defaults, privacy, tuning, heartbeat cost, rollback, migration, and recovery.

## Task 7: Verification

- [x] Run focused suites, Ruff, Pyright, all Python tests, frontend tests/build, Compose
  validation, and `git diff --check`.
- [x] Run migration upgrade/downgrade/re-upgrade and all integration tests against fresh
  temporary pgvector PostgreSQL and Redis without touching the running MyBot database.
