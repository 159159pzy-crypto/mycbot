# MyBot

MyBot is a complete, self-hosted QQ and Telegram agent bot. On typed,
frozen contracts it layers a real messaging spine (external NapCat over
OneBot v11, Telegram long polling, an at-least-once Redis Streams
pipeline), an LLM agent with persona and bounded history behind any
OpenAI-compatible endpoint, policy-gated tools (SearXNG search and safe
URL fetch with citations), scoped pgvector long-term memory with privacy
enforced in code, a hierarchical pgvector knowledge base with annotation
replies, an isolated plugin system, an authenticated operator
console, and production polish: abuse guards, opt-in proactive check-ins,
rehearsed backups, resource-limited containers, and a dependency-audited
CI. NapCat is intentionally not bundled. The full milestone sequence and
its final status live in `docs/ROADMAP.md`; `docs/DEPLOYMENT.md` takes a
clean VPS to a healthy deployment and `docs/RUNBOOK.md` covers the days
after.

## Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Node.js 22 and [pnpm](https://pnpm.io/) 11
- Docker Engine or Docker Desktop with Docker Compose v2 for the container path
- GNU Make is optional; every Make target expands to the commands shown below

Copy the local-only example environment before starting services:

```powershell
Copy-Item .env.example .env
```

On macOS or Linux, use `cp .env.example .env`. The checked-in values are only
development placeholders. Do not commit real bot tokens, database passwords,
or search secrets.

## Quick start with Docker Compose

Build the two application images, start stateful dependencies, apply the
database migration, and then start every role:

```console
docker compose build
docker compose up -d postgres redis searxng
docker compose run --rm api alembic upgrade head
docker compose up -d
```

The same sequence is available as `make compose-up`. All published ports bind
to `127.0.0.1` by default:

| Surface | Local URL or port |
| --- | --- |
| Web operator shell | <http://127.0.0.1:4173> |
| API | <http://127.0.0.1:8000> |
| SearXNG | <http://127.0.0.1:8080> |
| PostgreSQL | `127.0.0.1:5432` |
| Redis | `127.0.0.1:6379` |

Useful operator commands:

```console
docker compose ps
docker compose logs --follow --tail=200
docker compose --env-file .env.example config --quiet
docker compose down
```

Named volumes preserve PostgreSQL, Redis AOF, and SearXNG cache data across a
normal `docker compose down`. Adding `--volumes` permanently removes that data.

## Local development with uv and pnpm

Install both dependency sets and start the backing services:

```console
uv sync --all-groups --locked
pnpm --dir web install --frozen-lockfile
docker compose up -d postgres redis searxng
uv run alembic upgrade head
```

Run the API and web development server in separate terminals:

```console
uv run python -m mybot api
pnpm --dir web dev
```

The Vite server proxies relative `/health` requests to the API at
`http://127.0.0.1:8000`. The production web container uses the same relative
path and proxies it to the Compose service `api:8000`, avoiding a cross-origin
browser dependency. It runs as an unprivileged nginx user on container port
8080. `GET /static-health` checks only the static web server, so the web
container does not wait for API readiness and remains observable while the API
or its dependencies recover.

## Process roles

Every Python role is a real, signal-aware process entrypoint built from the
same image:

| Role | Local command | Responsibility in this milestone |
| --- | --- | --- |
| API | `uv run python -m mybot api` | FastAPI lifecycle, health endpoints, operator API, plugin broker |
| Gateway | `uv run python -m mybot gateway` | Owns the NapCat WebSocket and Telegram long polling, translates inbound events, delivers replies |
| Agent worker | `uv run python -m mybot agent-worker` | Consumes the ingest stream, persists conversations/messages, applies guards, runs agent turns with tools and memory |
| Knowledge worker | `uv run python -m mybot knowledge-worker` | Parses Markdown/TXT/PDF tasks, builds parent/child chunks, and embeds child chunks |
| Maintenance worker | `uv run python -m mybot maintenance-worker` | Memory lifecycle (expiry, decay, purge) and the proactive messaging pass |
| Plugin runner | `uv run python -m mybot plugin-runner` | Loads manifested plugins and executes their tools in isolation |

In Compose, `plugin-runner` receives only `MYBOT_PLUGIN_BROKER_URL` and
`MYBOT_PLUGIN_CONFIG`. It shares the internal-only `plugin-control` network with
the API and is not attached to the general backend network.

## Messaging spine behavior

The gateway and agent worker communicate only through Redis Streams
(`mybot:ingest` and `mybot:outbound`) using consumer groups, explicit acks,
per-envelope dedupe keys, and capped dead-letter streams, so either process
can restart without losing or double-answering a message. Replies are
delivered at-least-once by design.

Knowledge ingestion uses a separate `mybot:knowledge` stream and the
`knowledge-workers` consumer group. Upload requests only validate, persist,
and enqueue; `document_id + generation` is the idempotency boundary.

## Knowledge base and annotation replies

The operator console's **知识库** page accepts Markdown, TXT, and PDF. A
document is global or bound to one conversation. Only smaller child chunks are
ranked by pgvector; the matching parent chunk is returned as model context.
SQL filters embedding model and scope before distance ordering, and ephemeral
sandbox sessions can only see global knowledge.

`kb_search` is a built-in read-only `knowledge.read` tool. Its `sources` use
`kb://<document>/<parent>` URIs, so hits enter the existing
`ReplyPlan.citations` and reference-footer renderer. The console exposes the
same retrieval path as a query/top-k/threshold/scope hit-testing panel.

Operator-reviewed question/answer pairs live in `annotation`. Before a normal
agent turn, top-1 must exceed its threshold and beat top-2 by the configured
margin to bypass the chat model. MISS or embedding failure falls back to the
ordinary agent, while a HIT still passes through outbound moderation. Attempts
are recorded in `annotation_match_audit`; Bot replies in the conversation view
can be saved as conversation-scoped annotations.

The turn policy answers every direct chat, and answers group or channel
messages only when the message mentions the bot, replies to one of the
bot's own messages, or starts with a `/command`. Commands are built in and
deterministic: `/ping` answers `pong`, `/status` reports API dependency
readiness, and `/forget` revokes the sender's memories. Every other
answerable message becomes an agent turn (next section).

Enable a platform by setting its credential in `.env`:

- **Telegram:** create a bot with [BotFather](https://t.me/BotFather) and set
  `MYBOT_TELEGRAM_BOT_TOKEN`. Long polling starts on the next gateway start;
  no inbound port or webhook is required.
- **QQ:** run NapCat separately, expose its OneBot v11 WebSocket only to a
  network the gateway can reach (never the public internet), give it a
  narrowly scoped access token, and set `NAPCAT_WS_URL` plus
  `MYBOT_QQ_ACCESS_TOKEN`.

An unset credential simply disables that adapter; the gateway logs the
disabled platform once and keeps running the rest.

## Agent replies

Answerable non-command messages are agent turns: the worker assembles a
prompt from the persona, a token-budgeted window of recent conversation
history, and the inbound message, then asks an OpenAI-compatible LLM for the
reply. Commands (`/ping`, `/status`) always stay deterministic and never
reach the model. Configure any OpenAI-compatible endpoint in `.env`:

| Setting | Purpose |
| --- | --- |
| `MYBOT_LLM_BASE_URL` | Legacy fallback channel, e.g. `https://api.deepseek.com/v1`; empty is allowed when runtime channels are configured |
| `MYBOT_LLM_API_KEY` | Bearer token for that endpoint (optional for local servers) |
| `MYBOT_LLM_MODEL` | Model name the endpoint expects (default `deepseek-chat`) |
| `MYBOT_AGENT_DAILY_TOKEN_CEILING` | Global daily token budget, 0 = unlimited |
| `MYBOT_AGENT_CONVERSATION_DAILY_TOKEN_CEILING` | Per-conversation daily budget, 0 = unlimited |
| `MYBOT_MODEL_API_KEYS` | Optional JSON secret map used by runtime channels; never stored in `system_kv` |
| `MYBOT_MODEL_CHANNEL_COOLDOWN_SECONDS` | Redis cooldown after a retryable channel failure (default 60) |
| `MYBOT_MODEL_CHANNELS_CACHE_TTL_SECONDS` | Worker refresh interval for runtime channel configuration (default 30) |

The operator console's **Models** panel manages a purpose-aware channel
list stored at `models.channels` in `system_kv`. Each channel maps one or
more purposes (`chat`, `memory`, `embedding`, `vision`) to models, carries
priority and weight, and stores price-per-million snapshots for cost
accounting. Secrets stay env-only: `api_key_env` is a reference name, never
the key value. For example:

```json
[
  {
    "name": "primary",
    "base_url": "https://models.example/v1",
    "api_key_env": "PRIMARY_MODEL_KEY",
    "priority": 0,
    "weight": 2,
    "enabled": true,
    "model_map": {
      "chat": {
        "model": "chat-model",
        "input_price_per_million": "0.50",
        "output_price_per_million": "1.50"
      },
      "memory": {"model": "small-model"},
      "embedding": {"model": "text-embedding-3-small"}
    }
  }
]
```

Set `MYBOT_MODEL_API_KEYS={"PRIMARY_MODEL_KEY":"..."}` outside version
control. Channels at the same priority use deterministic weighted rotation;
retryable transport/429/5xx failures put that channel into Redis cooldown and
continue to the next channel. Permanent 4xx errors stop immediately. Every
attempt is written to `llm_call_log` without URLs, keys, response bodies, or
exception text. Enabled embedding channels must use the same model so vector
spaces are never mixed. The Models panel summarizes the last 30 days by day,
conversation, and channel, including token totals and price-snapshot cost.

The first default profile is seeded from `MYBOT_AGENT_SYSTEM_PROMPT`. During
upgrade, an existing `agent.system_prompt` value in `system_kv` is preserved
as that profile's first immutable persona version. The operator console's
**档位与人设** page is authoritative for subsequent edits; saving or rolling
back creates a new persona version instead of overwriting history.

```sql
INSERT INTO system_kv (key, value)
VALUES ('agent.system_prompt', '"你是一只高冷但热心的猫娘助手。"')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
```

Degradation is explicit by design: with neither runtime nor legacy channels
the bot answers with an unavailable-model fallback; an LLM
outage produces an apologetic fallback; a crossed token ceiling produces a
budget notice. Every agent turn — replied, fallback, budget_exceeded, or
error — is recorded in the `turns` table with model, token usage, and
latency. History budgeting uses a characters/4 token estimate, a safety
margin rather than exact tokenizer accounting.

## Agent tools

During an agent turn the model may call tools. Two are built in: `web_search`
queries the bundled SearXNG over its JSON API, and `fetch_url` retrieves a
public web page and extracts readable text. Sources returned by tools are
deduplicated into `ReplyPlan.citations` and rendered as a compact `Sources:`
footer in the delivered reply, so answers about current events arrive cited.

Tool execution is policy-gated by the Milestone 1 contracts: a tool runs only
when every capability it declares is present in
`MYBOT_AGENT_GRANTED_CAPABILITIES` (clearing the list disables tools), tools
marked `approval_required` are refused until the operator console grants
them a standing approval, and every call is bounded by
`MYBOT_TOOL_TIMEOUT_SECONDS`, `MYBOT_TOOL_MAX_CALLS_PER_TURN`, and the
overall `MYBOT_TURN_DEADLINE_SECONDS` (deadline overrun degrades to the
fallback reply). Each invocation is audited in the `tool_invocations` table
linked to its turn.

`fetch_url` refuses non-HTTP schemes and any host that resolves to a
private, loopback, link-local, reserved, or multicast address, caps download
size via `MYBOT_TOOL_FETCH_MAX_BYTES`, and truncates extracted text. This
guards the backend network (PostgreSQL, Redis, SearXNG) from being probed
through the bot; keep it in mind before granting `web.fetch` in sensitive
deployments.

## Memory

After each agent reply the worker extracts at most three durable candidates.
Each candidate first retrieves active memories under the same scope, owner,
conversation and privacy boundary; the memory-purpose model then chooses
`ADD`, `UPDATE`, `DELETE`, or `NOOP`. `UPDATE` inserts a new row and marks its
predecessor with `invalid_at`/`invalidated_by`; `DELETE` only invalidates the
old row. `/forget` and operator revocation remain separate privacy actions via
`revoked_at`, so a forgotten row can never be selected as an update target.

Recall is hybrid: pgvector cosine rank and PostgreSQL `pg_trgm` text rank are
computed after the same SQL scope/privacy/time filters, then fused with RRF.
This keeps semantic matches while improving exact QQ names, slang and
abbreviations. Recall audit stores only a SHA-256 query hash plus vector/text/
selected memory IDs, never the raw query. `MYBOT_MEMORY_TOKEN_BUDGET` still
bounds the rendered recall block.

Privacy is enforced in SQL and code, never delegated to the prompt: facts
about a person are stored subject-scoped and PRIVATE and are only ever
retrieved in that person's own direct chat; group turns can see only
SHARED/PUBLIC conversation- or global-scoped memories, and extraction can
never produce SENSITIVE items. `/forget` immediately revokes the sender's
subject memories (in a direct chat it also clears that conversation's
memories) and reports the count. The console's Memories page can include
invalidated/revoked versions, follow predecessor/successor chains, inspect
operation audit, and edit bounded persona/user-profile core blocks.

Persona and per-subject user-profile core blocks are injected on every
non-ephemeral turn without relying on retrieval. `memory_append` and
`memory_replace` require the `memory.write` capability and, by default, a
standing operator approval (`MYBOT_MEMORY_CORE_TOOLS_APPROVAL_REQUIRED`).
Each save enforces its block token budget and increments a version.

Before count/token pressure removes history, the worker sends only the rows
about to be dropped to the memory-purpose model, under hard message/token caps
and a Redis per-conversation debounce. The maintenance worker also runs an
optional low-frequency consolidation pass over bounded recent dialogue and
active memories; its candidates re-enter the normal merge pipeline. Lifecycle
expiry/decay uses `invalid_at`, while only privacy-revoked rows older than
`MYBOT_MEMORY_REVOKED_RETENTION_DAYS` are physically purged. Embeddings remain
model-tagged, so incompatible vectors are never compared.

## Group personality and profiles

Every conversation resolves one profile before participation is decided. A
profile binds an immutable persona version, model tier, tool grants, memory
policy, same-conversation expression example count, relationship switch, and
group reply-willingness policy. Unbound conversations use the conservative
default profile; its willingness switch is off, so upgrading cannot make the
bot start talking in groups unexpectedly.

Unaddressed group messages are scored before any chat-model call. Explicit
keywords, question/request cues, persona and visible-memory relevance, recent
group activity, and recent bot presence produce deterministic components that
are written to `reply_willingness_audit`. Semantic dependency failures fail
closed. Mentions, replies, commands, existing rate limits, and group cooldowns
keep their previous behavior. The Overview page reports the last 24 hours of
allowed and blocked willingness decisions.

Optional background personality learning is group-only and off by default.
It extracts short style patterns into active
`CONVERSATION/SHARED/EXPRESSION` memory, so prompt injection can never cross a
conversation boundary. Relationship summaries are
`SUBJECT/PRIVATE/RELATIONSHIP` memories with a 0–100 familiarity score; every
learned or operator edit appends a successor and invalidates the old version,
so `/forget`, revocation, history, and decay use the same M3 lifecycle.

| Setting | Default | Purpose |
| --- | --- | --- |
| `MYBOT_PERSONALITY_LEARNING_ENABLED` | false | Global cost/privacy switch for expression and relationship learning |
| `MYBOT_PERSONALITY_LEARNING_LOOKBACK_HOURS` | 24 | Recent group dialogue window |
| `MYBOT_PERSONALITY_LEARNING_CONVERSATION_LIMIT` | 10 | Maximum groups per maintenance pass |
| `MYBOT_PERSONALITY_LEARNING_MESSAGE_LIMIT` | 80 | Maximum inbound messages per group |
| `MYBOT_PERSONALITY_LEARNING_TOKEN_BUDGET` | 3000 | Hard prompt budget per learned group context |
| `MYBOT_PERSONALITY_LEARNING_MIN_CONFIDENCE` | 0.7 | Minimum expression confidence |

## Plugins

M6 provides two extension levels. Markdown skills live under
`skills/<name>/SKILL.md` with `name`, `description`, and `trigger` frontmatter.
The agent prompt receives only a bounded catalog; the model loads full
instructions through the read-only `load_skill` tool when needed. The operator
console can edit and enable or disable skills, and the next turn sees the
updated catalog.

Code plugins are a `PluginManifest` plus `SimplePlugin` handlers. Create a local
plugin skeleton with:

```powershell
uv run mybot plugin new weather_digest
```

The scaffold includes `plugin.json`, a Python entrypoint, a scheduled task,
configuration schema, and a pytest skeleton. `config_schema`, structured
`tasks`, and `requires` are enforced at runtime; legacy string task ids remain
accepted. The first injected service versions are deliberately limited to
`memory.search@1`, `llm.complete@1`, and `kv.store@1`. Registration fails if a
required service version is unavailable or requested capabilities exceed
`MYBOT_PLUGIN_CAPABILITY_GRANTS`; calls are identity checked, rate limited by
`MYBOT_PLUGIN_SERVICE_QUOTA_PER_MINUTE`, and audited.

`plugin-runner` now supervises one child process per plugin. Source changes or
an operator reload restart only that plugin. Repeated crashes use exponential
backoff and then open a circuit until manual reload. Control, status, managed
installs, and per-plugin configuration are written atomically under
`MYBOT_PLUGIN_DATA_DIR`. The container receives no database or Redis
credentials, exposes no ports, and reaches the API only through the internal
`plugin-control` network.

`registry.json` is a trusted operator-maintained index, not a public market.
Installation accepts HTTPS archives only, enforces
`MYBOT_PLUGIN_INSTALL_MAX_BYTES`, rejects path traversal and symbolic links,
checks the indexed `plugin.json` SHA-256, installs into a versioned directory,
and updates `managed.json` only after a complete install. The operator console
exposes plugin status, configuration forms, reload/enable/disable, and registry
installation.

Unknown direct messages can be gated per QQ or Telegram connection with
`open`, `paired`, or `allowlist`. Under `paired`, an unknown sender is stopped
before conversation/message persistence and LLM use, receives one one-hour
eight-character pairing prompt, and can continue only after approval. Pending
requests are capped at three per connection. Group chats remain outside this
direct-message gate.

Useful M6 endpoints are `GET /operator/skills`, `GET /operator/plugins`,
`GET /operator/plugins/registry`, and `GET /operator/pairing`. The broker's
`GET /plugin-broker/health` response includes loaded plugins, services, tasks,
grants, and load errors.

| Setting | Default | Purpose |
| --- | ---: | --- |
| `MYBOT_SKILLS_PROMPT_MAX_CHARS` | 4000 | Maximum skill catalog characters injected per turn |
| `MYBOT_PLUGIN_TASK_POLL_SECONDS` | 5 | Maintenance scheduler poll interval |
| `MYBOT_PLUGIN_SUPERVISOR_POLL_SECONDS` | 1 | Source and control reconciliation interval |
| `MYBOT_PLUGIN_CRASH_LIMIT` | 5 | Crashes inside the rolling window before circuit open |
| `MYBOT_PLUGIN_CRASH_WINDOW_SECONDS` | 60 | Crash circuit rolling window |
| `MYBOT_PLUGIN_SERVICE_QUOTA_PER_MINUTE` | 30 | Per-plugin, per-service call quota |
| `MYBOT_PLUGIN_INSTALL_MAX_BYTES` | 10000000 | Maximum trusted registry archive size |

Plugins, skills, remote archives, plugin outputs, and stranger messages are all
treated as untrusted input. The process and broker boundaries limit mistakes
and resource runaway; they are not a sandbox for deliberately hostile code.

### Historical M1 broker behavior (superseded by M6)

<!-- Kept only as source-level migration context for older deployments.
Plugins are ordinary Python objects: a frozen `PluginManifest` plus handler
callables, wrapped in `SimplePlugin` from `mybot.plugins.sdk`. The isolated
plugin-runner imports them from `MYBOT_PLUGIN_CONFIG` entrypoints
(`{"plugins": ["mybot.plugins.examples.dice:PLUGIN"]}` enables the in-repo
dice example), registers their manifests with the broker inside the API —
the only component on both the backend and `plugin-control` networks — and
then long-polls for work. The runner remains a pure HTTP client of
`MYBOT_PLUGIN_BROKER_URL`; it still receives no database or Redis
credentials and no new networks.

Registration is where policy bites: manifests are validated against the
`PluginManifest` contract, and any plugin whose `requested_capabilities`
exceed its entry in `MYBOT_PLUGIN_CAPABILITY_GRANTS` is refused with an
audit entry. Accepted plugin tools appear in the agent's tool registry with
their declared risk and approval settings (approval-required plugin tools
stay refused until approved in the operator console), and built-in tool ids
cannot be shadowed. The agent worker reaches the broker over the backend
network, refreshing its plugin tool view on a TTL; broker or runner outages
degrade to built-in tools only. Plugins declaring the `message` event hook
receive read-only envelope/decision notifications after each turn.
`GET /plugin-broker/health` lists loaded plugins, versions, tools, and
grants for operator review. Broker state is held in the single API process.

Execution is bounded — per-call timeouts in both the runner and the worker,
a result size cap, and crash isolation so one failing plugin cannot take
the runner loop or the stack down. Be clear about what this is not: the
container boundary plus broker policy protects against mistakes and
resource runaway, not against deliberately hostile plugin code. Only run
plugins you trust, grant capabilities narrowly, and treat anything more as
requiring real sandboxing. -->

## Health and migrations

- `GET /health/live` proves the API process is alive and does not probe a
  dependency.
- `GET /health/ready` reports PostgreSQL and Redis separately. It returns HTTP
  200 only when both probes pass, otherwise HTTP 503.
- Both endpoints return or preserve `X-Correlation-ID`.
- Web `GET /static-health` returns independently of API and database readiness.

Try them from PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/live
Invoke-RestMethod http://127.0.0.1:8000/health/ready
```

Apply pending migrations locally with `uv run alembic upgrade head`, or against
Compose with `docker compose run --rm api alembic upgrade head`. The initial
migration enables pgvector and creates the foundation system table.

Dependency probes use bounded fail-fast defaults that can be overridden in
`.env`:

| Setting | Default seconds | Purpose |
| --- | ---: | --- |
| `MYBOT_HEALTH_PROBE_TIMEOUT_SECONDS` | 2 | Overall timeout for each readiness probe |
| `MYBOT_DATABASE_CONNECT_TIMEOUT_SECONDS` | 3 | PostgreSQL connection timeout |
| `MYBOT_DATABASE_READ_TIMEOUT_SECONDS` | 3 | PostgreSQL statement timeout |
| `MYBOT_REDIS_CONNECT_TIMEOUT_SECONDS` | 2 | Redis connection timeout |
| `MYBOT_REDIS_READ_TIMEOUT_SECONDS` | 2 | Redis command timeout |

Messaging spine settings, all optional with safe defaults:

| Setting | Default | Purpose |
| --- | --- | --- |
| `MYBOT_QQ_CONNECTION_ID` | `qq-main` | Namespace for QQ conversation identities |
| `MYBOT_TELEGRAM_CONNECTION_ID` | `telegram-main` | Namespace for Telegram conversation identities |
| `MYBOT_TELEGRAM_API_BASE_URL` | `https://api.telegram.org` | Override for Bot API proxies or a local test server |
| `MYBOT_TELEGRAM_POLL_TIMEOUT_SECONDS` | 50 | Long-poll duration for `getUpdates` |
| `MYBOT_INGEST_STREAM` / `MYBOT_OUTBOUND_STREAM` | `mybot:ingest` / `mybot:outbound` | Redis stream names |
| `MYBOT_STREAM_DELIVERY_MAX_ATTEMPTS` | 5 | Redeliveries before an entry dead-letters |
| `MYBOT_STREAM_DEDUPE_TTL_SECONDS` | 3600 | Window for suppressing duplicate envelope ids |
| `MYBOT_GATEWAY_RECONNECT_INITIAL_SECONDS` | 1 | First reconnect backoff for platform connections |
| `MYBOT_GATEWAY_RECONNECT_MAX_SECONDS` | 30 | Reconnect backoff ceiling |

## Optional external NapCat connection

NapCat is intentionally not a required Compose service. Manage it separately,
keep its WebSocket endpoint off the public internet, and give it a narrowly
scoped access token. A host-managed instance is reachable from the gateway
container as `host.docker.internal`; set the reserved `NAPCAT_WS_URL` and
`MYBOT_QQ_ACCESS_TOKEN` values in `.env` when the QQ adapter milestone consumes
them. On Linux, Compose adds the `host-gateway` mapping for that hostname.

When `NAPCAT_WS_URL` is set, the gateway maintains that WebSocket connection
with authenticated headers and bounded reconnect backoff, translating OneBot
v11 message events into the platform-neutral envelope contract.

That contract now represents text, image, file, reference, `at`, sticker, and
voice segments. QQ and Telegram encoders are pure functions covered by replay
fixtures; unsupported outbound media becomes readable text instead of being
dropped. Voice is representable and forwardable, but M1 does not perform ASR,
TTS, or voice understanding. Plugin runner registration negotiates protocol
version 2; legacy version-1 runners remain accepted and receive new segment
types projected to readable text so strict older SDK models do not fail.

## Operator console

The web shell is now an authenticated operations console. Set
`MYBOT_OPERATOR_TOKEN` to a long random value and open the web UI: below the
readiness deck, the console unlocks with that token (held in memory only —
never in browser storage). Overview shows a 14-day
token-usage sparkline, turn outcomes, average/p95 turn latency, and live
queue and dead-letter depths. Conversations lists chats by latest activity
and opens any of them into full message and turn detail, including per-turn
tool invocations. Memories filters by scope and revocation state and revokes
any item with one click. Plugins mirrors the broker view and edits the
standing tool approvals. Persona edits the agent's system prompt; workers
pick the change up on their next turn without a restart. Safety & Evaluation
edits moderation policy, resolves pending per-call approvals, runs shadow
regression suites, and shows assertion-level diffs.

The auth model is deliberately small: every `/operator/*` request requires
`Authorization: Bearer $MYBOT_OPERATOR_TOKEN`; with the token unset the
operator API refuses everything (fail closed); repeated failures from one
client are rate limited; health endpoints stay unauthenticated so container
probes keep working; and `/plugin-broker/*` remains reachable only on
internal networks as documented since Milestone 1. Every mutation — persona
change, approvals change, memory revocation — is recorded in the
`operator_audit` table and visible via `GET /operator/audit`.

Approval-required tools first honor standing grants stored in `system_kv`
under `tools.approved_ids`. Without a standing grant, each invocation is
persisted in the Safety & Evaluation queue and the worker pauses until an
operator approves or rejects that one call. `MYBOT_TOOL_APPROVAL_TIMEOUT_SECONDS`
defaults to 60 seconds; timeout is an audited automatic rejection and the
agent receives the normal tool-error result so its existing fallback path can
continue. `MYBOT_TOOL_APPROVAL_POLL_SECONDS` controls database polling.

## Abuse guards and moderation

Every answerable turn passes guard checks before any reply work. Messages
from bot senders and messages that echo one of the bot's own recent
replies are ignored outright, which prevents two bots from talking each
other into a loop. Per-user and per-chat rate limits (Redis counters with
minute TTLs) issue exactly one deterministic refusal at the threshold and
then go silent — a flood produces one notice, not a notice per message.
Group agent replies respect a cooldown so the bot cannot dominate a busy
chat, while explicit `/commands` always pass. Oversized inputs are
refused deterministically without reaching the model.

| Setting | Default | Purpose |
| --- | --- | --- |
| `MYBOT_RATE_LIMIT_USER_PER_MINUTE` | 20 | Answerable turns per sender per minute (0 disables) |
| `MYBOT_RATE_LIMIT_CHAT_PER_MINUTE` | 30 | Answerable turns per conversation per minute (0 disables) |
| `MYBOT_GROUP_COOLDOWN_SECONDS` | 3 | Minimum spacing between agent replies in one group |
| `MYBOT_INPUT_MAX_CHARS` | 4000 | Size cap on agent-turn input |
| `MYBOT_LOOP_GUARD_ENABLED` | true | Ignore bot senders and echoed replies |

Both inbound user content and outbound reply plans pass the single-method
`ModerationHook` contract. The policy in `system_kv` under
`moderation.policy` selects an editable Aho-Corasick keyword backend, an
optional OpenAI-compatible `/moderations` backend, and/or a plugin tool whose
capabilities include `moderation`. Every backend decision is audited. Backend
errors follow the policy's explicit `open` or `closed` fail mode.

| Setting | Default | Purpose |
| --- | --- | --- |
| `MYBOT_MODERATION_ENABLED` | true | Global worker switch for inbound/outbound moderation |
| `MYBOT_MODERATION_API_BASE_URL` | empty | Optional OpenAI-compatible API root ending before `/moderations` |
| `MYBOT_MODERATION_API_KEY` | empty | Bearer token for the moderation endpoint; never stored in the database |
| `MYBOT_MODERATION_API_MODEL` | `omni-moderation-latest` | Remote moderation model name |
| `MYBOT_MODERATION_TIMEOUT_SECONDS` | 3 | Per-backend timeout before fail-mode handling |

## Regression evaluation and feedback

Evaluation cases are JSON files under `MYBOT_EVALUATION_CASES_DIR` (default
`evals`). Each case carries question/history plus deterministic
`must_contain`, `must_not_contain`, `must_call_tool`, and `must_cite`
assertions. `mybot eval --offline --cases evals` runs only cases with an
`offline_response`, so CI does not spend model tokens. Console runs use an
ephemeral sandbox conversation, traverse the real agent/tool/moderation
pipeline, and intercept the reply before the outbound platform stream.

Run and result records retain the persona version, selected profile tier,
actual model channel/model, tool calls, citations, assertions, and optional
judge score. LLM judging requires both `MYBOT_EVALUATION_JUDGE_ENABLED=true`
and the per-run checkbox. Bot messages accept positive/negative feedback and
notes; negative replies can be converted into repo regression cases or
refined into annotation replies, and the 30-day negative rate appears in
Overview metrics.

## Proactive check-ins

Off by default, and triple-gated when on: `MYBOT_PROACTIVE_ENABLED` is
the global kill switch, only conversations opted in through
`GET/PUT /operator/config/proactive` (audited) are ever considered, and
each send must clear UTC quiet hours plus a per-conversation frequency
cap before the maintenance worker runs the bound persona against a bounded
recent transcript plus durable core memory. The background model must return
exactly `HEARTBEAT_OK` when there is no concrete reason to interrupt; that
sentinel is silently suppressed. Sent, suppressed, and failed generations are
written to `proactive_generation_audit`, including persona version, model, and
token usage. Actual sends still use the normal outbound stream and are
recorded as `PROACTIVE` turns.

| Setting | Default | Purpose |
| --- | --- | --- |
| `MYBOT_PROACTIVE_ENABLED` | false | Global kill switch |
| `MYBOT_PROACTIVE_MIN_INTERVAL_HOURS` | 24 | Frequency cap per conversation |
| `MYBOT_PROACTIVE_QUIET_START_HOUR` / `MYBOT_PROACTIVE_QUIET_END_HOUR` | 22 / 8 | UTC quiet window; equal values disable it, and it may wrap midnight |
| `MYBOT_PROACTIVE_CONTEXT_MESSAGES` | 12 | Maximum recent messages supplied to heartbeat generation |
| `MYBOT_PROACTIVE_MESSAGE` | built-in fallback | Used only when no heartbeat LLM is wired |

## Backups and day-2 operations

PostgreSQL is the only backup-critical store; Redis holds transient
stream coordination and counters, and SearXNG holds a cache.
`scripts/backup.sh --mode compose` writes a custom-format `pg_dump` and
`scripts/restore.sh` restores it with confirmation; the
backup→destroy→restore drill is written out in `docs/RUNBOOK.md` and was
rehearsed for real against PostgreSQL as part of this milestone's
verification. The runbook also covers queue depth triage, dead-letter
inspection and the manual replay stance, token rotation, memory hygiene,
and proactive controls.

Every container runs with json-file log rotation, a memory limit, and a
CPU limit; application roles get a 30-second `stop_grace_period` so
workers drain gracefully, and anything unacked at shutdown is redelivered
after restart. Third-party images are version-pinned in `compose.yaml`,
and the SearXNG tag is operator-pinnable via `SEARXNG_IMAGE_TAG` — pin a
reviewed digest in production (`docker inspect --format
'{{index .RepoDigests 0}}' searxng/searxng:<tag>`). CI audits production
dependencies with pip-audit on every push.

## Observability (OTLP)

Structured JSON logs and correlation IDs are always on. To export traces,
point `MYBOT_OTEL_EXPORTER_OTLP_ENDPOINT` at an OTLP/HTTP collector (for
example `http://otel-collector:4318`); the API bootstraps the OpenTelemetry
SDK with FastAPI instrumentation, so every request — including operator and
broker calls — emits spans carrying the correlation ID. Leaving the value
empty (the default) creates no exporter and adds no overhead. A minimal
local pipeline is the `otel/opentelemetry-collector` image with an OTLP
receiver and a logging or Prometheus exporter; the operator console's
metrics panel complements this with turn latency, token usage, and queue
depth without requiring any collector at all.

## V2-M2: image understanding, sandbox, and reply traces

Inbound `image` segments are no longer collapsed into a generic non-text
placeholder. `MYBOT_VISION_MODE=describe` (the default) sends OpenAI-compatible
text/image content parts through the M1 `vision` model purpose and adds the
bounded Chinese description to the normal chat prompt. `direct` keeps the
image parts in the main chat request for a multimodal chat model; `off` keeps a
readable image reference without making a vision call. A vision failure never
drops the turn: it degrades to the configured Chinese reference text. ASR/TTS
and video understanding remain outside M2.

Telegram photos and image documents retain a secret-free `tg-file://` reference
in Streams and PostgreSQL. Immediately before a vision request, the agent uses
the configured bot token to call `getFile`, downloads the image with bounded
size/time limits, and sends a temporary `data:image/...;base64` part to the
model. Bot tokens and token-bearing Telegram download URLs are never persisted
or included in trace attributes.

The authenticated console now has a **Sandbox** tab. A submitted text and
optional HTTP(S) image URL becomes a normal `InboundEvent` on `mybot:ingest`,
is consumed by the production agent worker, and leaves through the normal
outbound stream. The gateway's virtual `SANDBOX` sender performs no network
delivery; the console reads the persisted transcript. Sandbox envelopes and
conversations are marked `ephemeral`, so memory retrieval/extraction and
proactive delivery are skipped, while tools, model routing, turn persistence,
moderation, and accounting remain identical to QQ/TG.

Every envelope carries a `trace_id`. Completed spans for vision preparation,
memory retrieval, model calls, tool calls, moderation, outbound publication,
and gateway delivery are stored in `trace_spans`. Open any conversation and
expand **追踪** to see stage status, duration, bounded memory recall context,
tool outcome, moderation decision, and model token use. Trace writes are
best-effort and store stable error codes rather than raw exception text.

Model replies can deliberately emit standalone validated directives such as
`[[media:{"type":"image","url":"https://example/image.png"}]]`,
`[[media:{"type":"sticker","id":"14","name":"smile"}]]`, and
`[[meme:acknowledge]]`. `shape_reply` removes valid directives from visible
text and carries the typed media through the existing QQ/Telegram capability
encoders; malformed or unsafe image directives remain readable text.

## Verification

Run the complete local verification set with `make verify`, or run each command
directly:

```console
uv run ruff check .
uv run pyright
uv run pytest -q
pnpm --dir web test -- --run
pnpm --dir web build
docker compose --env-file .env.example config --quiet
```

Integration tests that need real services are marked `integration` and skip
themselves unless `MYBOT_TEST_DATABASE_URL` and `MYBOT_TEST_REDIS_URL` are
set. With both running locally you can exercise the full spine, including a
smoke test that pushes a `/ping` envelope through the real streams and
database:

```console
MYBOT_TEST_DATABASE_URL=postgresql+psycopg://mybot:password@127.0.0.1:5432/mybot_test \
MYBOT_TEST_REDIS_URL=redis://127.0.0.1:6379/9 \
uv run pytest -m integration -q
```

CI runs Python lint/type/tests, frontend tests/build, Compose validation, a
production dependency audit (pip-audit over the exported lockfile), both
container image builds, and an integration job backed by pgvector PostgreSQL
and Redis. The integration job applies an Alembic upgrade, downgrade, and
re-upgrade, runs the integration-marked stream/repository/pipeline tests
(including a 100-message load smoke), and then starts the API and checks
liveness and readiness.

## Deployment notes

The complete single-VPS walkthrough — production `.env` checklist, first
boot, Caddy TLS example, upgrade and rollback procedure, and a security
checklist — is `docs/DEPLOYMENT.md`. The short version:

- Replace every placeholder credential and keep production secrets outside the
  repository, preferably in the deployment platform's secret store.
- Pin reviewed container digests for production. Locally SearXNG defaults to
  `latest` to stay runnable against its evolving settings schema; in
  production set `SEARXNG_IMAGE_TAG` to a reviewed `tag@sha256:digest`.
- Put TLS and authentication in a reverse proxy before exposing the API, web,
  or SearXNG surfaces beyond loopback.
- `plugin-runner` drops Linux capabilities, uses a read-only root filesystem,
  enables `no-new-privileges`, receives no database or Redis credentials, and
  has only the internal `plugin-control` network. A container is still not a
  complete hostile code sandbox; add stronger isolation before accepting
  untrusted plugins.
- Run `scripts/backup.sh` before migrations or upgrades, and rehearse the
  restore drill in `docs/RUNBOOK.md` before you need it.
