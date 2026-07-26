# Production Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver Milestone 8, the final roadmap stop: close the gap between "works" and "runs unattended for months". Abuse and cost guards bound every answerable turn, policy-gated proactive messages ship from the maintenance worker, backup/restore is scripted and rehearsed, containers get log rotation, resource limits, and graceful-stop windows, third-party image tags become pinnable, CI gains a dependency audit, a load smoke test proves the spine under volume, and a deployment guide plus runbook let an operator go from a clean VPS to a healthy `/health/ready` and survive the bad days after.

**Architecture:** Guards are a small pure-ish `TurnGuards` component consulted by the worker after the turn decision and before any reply work: bot senders and echo loops are ignored outright (loop safety), per-user and per-chat Redis counters (reusing the budget `increment` backend) allow a single deterministic refusal at the threshold then go silent, group agent turns respect a cooldown via `SET NX EX`, and oversized inputs get a deterministic refusal. A `ModerationHook` protocol runs over the final reply text ahead of delivery — shipped as a wiring point with a documented no-op default. Proactive messaging is the maintenance worker's second job: a `ProactivePass` reads the operator-managed opt-in list from `system_kv`, honors a global kill switch, UTC quiet hours (midnight-wrap capable), and a per-conversation frequency cap implemented as `SET NX EX`, then records and publishes a template check-in as a `PROACTIVE`-triggered turn through the normal outbound stream. Operations hardening lands in Compose (json-file log rotation, memory/cpu limits, `stop_grace_period`) and in `scripts/backup.sh`/`restore.sh`, which support both compose-exec and direct-connection modes; the runbook's backup→destroy→restore drill is rehearsed for real against PostgreSQL in this repo's verification. Supply-chain work parameterizes the SearXNG image tag, documents digest pinning, and adds a `pip-audit` job to CI.

**Tech Stack:** Python 3.12, Redis counters, Bash + pg_dump/pg_restore, Docker Compose spec fields, GitHub Actions, pip-audit.

---

## File map

- `src/mybot/engine/guards.py`: rate limits, cooldown, size cap, bot/echo loop detection, verdicts.
- `src/mybot/services/agent_worker.py`: guard + moderation wiring ahead of delivery.
- `src/mybot/adapters/__init__.py` + `telegram/translate.py`: `sender_is_bot` on `InboundEvent`.
- `src/mybot/services/proactive.py` + `services/maintenance.py`: the proactive pass.
- `src/mybot/repositories/conversations.py`: `by_stable_key` detail lookup.
- `src/mybot/operator/api.py`: proactive opt-in config endpoints with audit.
- `scripts/backup.sh`, `scripts/restore.sh`, `docs/RUNBOOK.md`, `docs/DEPLOYMENT.md`.
- `compose.yaml`, `.env.example`, `.github/workflows/ci.yml`, `README.md`, `docs/ROADMAP.md`.
- `tests/engine/test_guards.py`, `tests/services/test_proactive.py`, `tests/services/test_pipeline_load.py`, deployment/config test extensions.

**Not in this milestone:** a real moderation backend (the hook is the contract), LLM-authored proactive content (template v1; the agent path can replace it later), multi-node scaling, and automated offsite backup shipping (the runbook covers the manual pattern).

### Task 1: Abuse and cost guards

- [ ] Extend `InboundEvent` with `sender_is_bot` (default false; Telegram sets it from `from.is_bot`) and add translator coverage.
- [ ] Settings with tests: `rate_limit_user_per_minute`, `rate_limit_chat_per_minute` (0 disables either), `group_cooldown_seconds`, `input_max_chars`, `loop_guard_enabled`, all bounded.
- [ ] Implement `TurnGuards.check(...) -> GuardVerdict` (allow / ignore-with-reason / refuse-with-text): bot senders ignored; inbound text equal to a recent outbound text ignored (echo loop); user and chat counters incremented per answerable turn with exactly one refusal at the threshold crossing and silence beyond; group AGENT turns blocked while the cooldown key is live (commands always pass); AGENT inputs over the size cap refused deterministically.
- [ ] Wire guards into the worker after the decision and before reply work; a refusal becomes a normal deterministic reply (recorded + delivered); add the `ModerationHook` protocol applied to final reply text before `record_outbound`, replacing rejected text with a deterministic notice and an audit-style log.
- [ ] Unit tests with injected clocks/backends for every verdict, plus worker tests for guard and moderation wiring.

### Task 2: Proactive messaging

- [ ] `ConversationRepository.by_stable_key` returning delivery details (integration-tested).
- [ ] Settings with tests: `proactive_enabled` (default false), `proactive_min_interval_hours`, `proactive_quiet_start_hour`/`proactive_quiet_end_hour` (UTC, equal values meaning no quiet window, wrap supported), `proactive_message` template.
- [ ] `ProactivePass.run_pass`: skip when disabled or inside quiet hours; read the opt-in stable-key list from `system_kv` (`proactive.enabled_conversations`); for each, `SET NX EX` the frequency key and, when acquired, record the outbound row, publish the `OutboundMessage`, and record a `DIRECT_REPLY`/`PROACTIVE` turn; unknown stable keys skip with a log.
- [ ] Run the pass from the maintenance worker alongside the memory lifecycle; failures in one job never block the other.
- [ ] `GET/PUT /operator/config/proactive` managing the opt-in list with an `operator_audit` entry; API test coverage.
- [ ] Unit tests with fakes and injected now for quiet hours (both wrapped and plain windows), frequency capping, opt-in filtering, and payload shape.

### Task 3: Operations hardening, supply chain, and load smoke

- [ ] `scripts/backup.sh` and `scripts/restore.sh` supporting `--mode compose|direct` (pg_dump custom format; restore into a clean database with confirmation), plus documented Redis stance: streams and counters are transient coordination state, safe to lose, so PostgreSQL is the only backup-critical store.
- [ ] Rehearse the runbook drill for real in verification: back up the integration database, drop and recreate it, restore, and prove row counts survive.
- [ ] Compose: json-file log rotation on every service, memory/cpu limits, `stop_grace_period` on app roles, `SEARXNG_IMAGE_TAG` parameterization; deployment-config tests assert rotation, limits, grace, and that every third-party image tag is env-pinnable; README documents digest pinning with `docker inspect`.
- [ ] CI: a `pip-audit` job over the exported lockfile; keep every existing job intact.
- [ ] Load smoke (integration-marked): push 100 distinct `/ping` envelopes through real Redis + PostgreSQL, one worker; assert all 100 replies arrive inside a bounded window and nothing dead-letters.

### Task 4: Deployment guide, runbook, and the final docs pass

- [ ] `docs/DEPLOYMENT.md`: single-VPS reference — prerequisites, clone, production `.env` checklist (every secret), Caddy TLS reverse-proxy example in front of the web container, external NapCat placement, first boot with migrations, upgrade procedure (pull → build → migrate → restart) and rollback (previous tag + `alembic downgrade`), and a security checklist.
- [ ] `docs/RUNBOOK.md`: backup/restore, dead-letter inspection and replay stance, token rotation (operator/LLM/platform), queue depth triage, memory hygiene, proactive controls.
- [ ] Final docs pass: README feature map brought current, `docs/ROADMAP.md` marked with per-milestone status and a definition-of-done checklist review.
- [ ] Full verification with and without integration URLs, frontend tests and build, Compose static validation; commit with `feat: production polish`.
