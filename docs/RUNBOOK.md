# MyBot operations runbook

Procedures for the days after `docs/DEPLOYMENT.md`: what to back up, how
to rehearse restoring it, and what to do when queues grow, tokens leak,
memory misbehaves, or proactive messages need reining in. Commands assume
the Compose deployment; substitute your own connection details for
host-run roles.

## 1. What must survive

PostgreSQL is the only backup-critical store. It holds conversations,
messages, turns, memory items, tool invocations, plugin/tool approvals,
persona and proactive configuration (`system_kv`), and the operator audit
trail. Redis holds transient coordination state — stream entries in
flight, dedupe keys, rate/budget counters, cooldown and frequency locks —
all of which the pipeline rebuilds or ages out (losing Redis can, at
worst, drop in-flight messages within the at-least-once window or briefly
reset counters). SearXNG's volume is a cache. Back up PostgreSQL;
document, but do not back up, the other two.

## 2. Backup and restore drill

Take routine backups with `scripts/backup.sh` (custom-format `pg_dump`,
refuses suspiciously small output):

```console
scripts/backup.sh --mode compose --out backups/
```

Schedule it daily from cron and ship the newest dump off the host:

```
15 3 * * * cd /opt/mybot && scripts/backup.sh --mode compose >> backups/backup.log 2>&1
```

A backup that has never been restored is a hope, not a backup. Rehearse
the full drill quarterly, and once against a scratch host before first
production traffic. The drill was rehearsed for real against PostgreSQL 16
as part of this milestone's verification.

1. Stop writers so nothing races the restore:
   `docker compose stop gateway agent-worker maintenance-worker api`.
2. Take a fresh backup: `scripts/backup.sh --mode compose`.
3. Destroy something on purpose (scratch host only) — e.g.
   `docker compose exec postgres psql -U mybot -d mybot -c "DELETE FROM messages;"`.
4. Restore: `scripts/restore.sh --mode compose --file backups/mybot-<stamp>.dump`
   (interactive confirmation; `--yes` for automation). The restore drops
   and recreates application objects (`pg_restore --clean --if-exists`).
5. Verify: row counts match step 2 expectations, then
   `docker compose up -d` and `curl -s http://127.0.0.1:8000/health/ready`.

Restoring an older dump into a newer codebase needs one extra step:
`docker compose run --rm api alembic upgrade head` after the restore.
Never needed when dump and code are from the same version.

## 3. Queue depth triage

The operator console's Overview panel shows ingest/outbound depths and
dead-letter counts live. From the host:

```console
docker compose exec redis redis-cli XLEN mybot:ingest
docker compose exec redis redis-cli XPENDING mybot:ingest agent-workers
docker compose exec redis redis-cli XLEN mybot:outbound
docker compose exec redis redis-cli XPENDING mybot:outbound gateway
```

Healthy is near zero. Sustained growth means the consumer side is down or
slow: `mybot:ingest` growing → check `agent-worker` (crashed? LLM endpoint
timing out? token ceiling logging refusals?); `mybot:outbound` growing →
check `gateway` (platform API down? NapCat disconnected?). Start with
`docker compose ps` and `docker compose logs --tail=100 <service>`. A
large `XPENDING` count with live consumers means entries are being
retried; they either succeed, or cross five delivery attempts and move to
the dead-letter stream. Restarting a stuck worker is safe by design —
unacked entries are reclaimed and duplicates are suppressed by dedupe
keys.

## 4. Dead letters: inspection and replay

Entries that fail five deliveries, or whose payload cannot be parsed at
all, land in capped dead-letter streams (`mybot:ingest:dead`,
`mybot:outbound:dead`, 1000 entries max). Inspect them:

```console
docker compose exec redis redis-cli XLEN mybot:ingest:dead
docker compose exec redis redis-cli XRANGE mybot:ingest:dead - + COUNT 10
```

The stance is deliberate: **no automatic replay.** A dead-lettered entry
already proved it fails repeatedly; feeding it back mechanically builds a
poison loop. Instead, read the payload (it is the full JSON envelope),
fix the root cause, and then decide whether a replay still makes sense —
a chat message from hours ago usually deserves silence, not a late reply.

To replay one entry manually, delete its dedupe key first (the consumer
marked the envelope id as seen on the first attempt; within the dedupe
TTL a replay would otherwise be skipped as a duplicate), then add the
payload back to the source stream:

```console
docker compose exec redis redis-cli DEL "mybot:seen:ingest:<envelope-id>"
docker compose exec redis redis-cli XADD mybot:ingest '*' payload '<json-payload>'
```

The outbound equivalents are `mybot:seen:outbound:<key>` and
`mybot:outbound`. Trim a fully handled dead-letter stream with
`XTRIM mybot:ingest:dead MAXLEN 0`.

## 5. Token rotation

- **Operator token** (`MYBOT_OPERATOR_TOKEN`): edit `.env`, then
  `docker compose up -d api`. Auth is stateless, so rotation is
  immediate; operators re-enter the token in the console (it is held in
  memory only, so a reload already forgot the old one). Rotate on any
  suspicion of exposure and whenever an operator leaves.
- **LLM / embedding keys** (`MYBOT_LLM_API_KEY`,
  `MYBOT_EMBEDDING_API_KEY`): issue the new key at the provider, update
  `.env`, then `docker compose up -d agent-worker maintenance-worker`,
  and revoke the old key at the provider afterwards.
- **Telegram bot token** (`MYBOT_TELEGRAM_BOT_TOKEN`): revoke via
  BotFather (`/revoke`), which invalidates the old token instantly;
  update `.env` and `docker compose up -d gateway`.
- **QQ / NapCat access token** (`MYBOT_QQ_ACCESS_TOKEN`): change it in
  the NapCat configuration and `.env` together, restart NapCat, then
  `docker compose up -d gateway`.

After any rotation, confirm recovery: `/health/ready`, one test message
per platform, and no auth errors in `docker compose logs gateway`.

## 6. Memory hygiene

Users own the fastest path: `/forget` in chat immediately revokes the
sender's subject-scoped memories (and, in a direct chat, that
conversation's memories) and reports the count. Operators work in the
console's Memories panel — filter by scope or revocation state, revoke
any item one click at a time; every revocation is written to the
`operator_audit` table.

The maintenance worker automates the rest on
`MYBOT_MEMORY_MAINTENANCE_INTERVAL_SECONDS`: items past `valid_until` are
revoked, items untouched for `MYBOT_MEMORY_DECAY_DAYS` lose confidence
(falling below the floor revokes them), and rows revoked more than
`MYBOT_MEMORY_REVOKED_RETENTION_DAYS` ago are deleted permanently.
Confirm it is running from `docker compose logs maintenance-worker`.

Two practices worth keeping: review the Memories panel after enabling a
new group (extraction is conservative, but groups produce more marginal
candidates), and remember that switching `MYBOT_EMBEDDING_MODEL` starts
retrieval fresh — old vectors are never compared across models and age
out via decay rather than needing manual deletion.

## 7. Proactive messaging controls

Three nested controls, all of which must agree before the bot ever
speaks unprompted:

1. **Kill switch:** `MYBOT_PROACTIVE_ENABLED` (default `false`). Nothing
   proactive happens while it is false. Flip in `.env` and
   `docker compose up -d maintenance-worker`.
2. **Per-conversation opt-in:** only stable keys listed by
   `GET/PUT /operator/config/proactive` receive check-ins; updates are
   audited. Remove a key to silence that conversation immediately.
3. **Pacing:** `MYBOT_PROACTIVE_QUIET_START_HOUR`/`..._END_HOUR` define a
   UTC quiet window (equal values disable it; it may wrap midnight), and
   `MYBOT_PROACTIVE_MIN_INTERVAL_HOURS` caps frequency per conversation
   via a Redis lock (`mybot:proactive:<stable-key>`).

Every proactive send is recorded as a `PROACTIVE`-triggered turn and an
outbound message, so the Conversations panel shows exactly what was sent
where. If proactive messages misfire, the order of response is: remove
the conversation from the opt-in list (surgical), or set
`MYBOT_PROACTIVE_ENABLED=false` (global), then read the recorded turns to
understand what happened. To make one conversation eligible again sooner
than its frequency cap allows, delete its `mybot:proactive:<stable-key>`
Redis key.

## 8. When the bot loops or gets flooded

The guards refuse once at a rate-limit crossing, then go silent — a
flooding user sees one notice, not a notice per message. Bot senders and
echoes of the bot's own recent replies are ignored outright. If a flood
is ongoing, lower `MYBOT_RATE_LIMIT_USER_PER_MINUTE` /
`MYBOT_RATE_LIMIT_CHAT_PER_MINUTE` or raise
`MYBOT_GROUP_COOLDOWN_SECONDS` in `.env` and
`docker compose up -d agent-worker`; counters live in Redis with
minute-level TTLs, so changes take effect immediately and nothing needs
cleanup afterwards. Token spend has its own ceilings
(`MYBOT_AGENT_DAILY_TOKEN_CEILING`,
`MYBOT_AGENT_CONVERSATION_DAILY_TOKEN_CEILING`); a crossed ceiling
degrades to a deterministic budget notice, visible in the turns record.
