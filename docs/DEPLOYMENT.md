# Deploying MyBot on a single VPS

This is the reference deployment: one Linux host running the Compose stack,
a TLS reverse proxy in front of the web console, and NapCat managed
separately for QQ. Follow it top to bottom on a clean machine and you end
at a healthy `GET /health/ready`.

## 1. Prerequisites

A VPS with 2+ vCPUs and 4 GB RAM comfortably fits the default resource
limits (roughly 2.5 GB committed across services). Install Docker Engine
with the Compose plugin, git, and open only ports 80/443 on the firewall —
every Compose port binds to loopback by design.

## 2. Clone and configure

```console
git clone <your-fork-url> mybot && cd mybot
cp .env.example .env
```

Production `.env` checklist — set every one of these, none are optional in
production:

- `POSTGRES_PASSWORD` — long random value (also flows into the app's DB URL).
- `SEARXNG_SECRET_KEY` — long random value.
- `MYBOT_OPERATOR_TOKEN` — long random value; the console is fail-closed
  without it.
- `MYBOT_LLM_BASE_URL` / `MYBOT_LLM_API_KEY` / `MYBOT_LLM_MODEL` — your
  OpenAI-compatible endpoint.
- `MYBOT_EMBEDDING_*` — an embeddings-capable endpoint, or leave both empty
  to run without memory.
- `MYBOT_MODEL_API_KEYS` — optional JSON secret map for channels managed in
  the Models panel. Channel configuration stores only reference names.
- Platform credentials: `MYBOT_TELEGRAM_BOT_TOKEN` and/or `NAPCAT_WS_URL` +
  `MYBOT_QQ_ACCESS_TOKEN`.
- `SEARXNG_IMAGE_TAG` — pin to a reviewed digest (see §6).

For image understanding, keep `MYBOT_VISION_MODE=describe` unless the selected
chat channel itself accepts OpenAI `image_url` content parts; only then use
`direct`. The channel configuration must expose a `vision` model purpose for
descriptor mode. The five `MYBOT_FALLBACK_*` values are operator-controlled
Chinese degraded-mode replies and contain no secrets.
Telegram image resolution additionally uses `MYBOT_VISION_MAX_IMAGE_BYTES`
(default 10 MB) and `MYBOT_VISION_IMAGE_DOWNLOAD_TIMEOUT_SECONDS` (default 30
seconds). The agent worker therefore needs the same `MYBOT_TELEGRAM_BOT_TOKEN`
as the gateway; the token is used only for Bot API `getFile`/download requests.

## 3. External NapCat (QQ only)

Run NapCat on the same host outside this Compose project, bind its OneBot
v11 WebSocket to a loopback or docker-network address (never the public
internet), set a narrowly scoped access token, and point `NAPCAT_WS_URL`
at it. From the gateway container the host is reachable as
`host.docker.internal` (the compose file adds the mapping on Linux).

Keep a reviewed Lagrange.OneBot configuration as a hot spare. Both endpoints
must pass the fixture-backed OneBot v11 conformance set. During a NapCat
incident, stop the old protocol endpoint, start Lagrange.OneBot with the same
access policy, update only `NAPCAT_WS_URL`, and restart `gateway`; do not
change contracts, workers, or conversation storage.

## 4. First boot

```console
docker compose build
docker compose up -d postgres redis searxng
docker compose run --rm api alembic upgrade head
docker compose up -d
curl -s http://127.0.0.1:8000/health/ready
```

Expect `{"status": "ready", ...}`. The web console is on
`http://127.0.0.1:4173` until the proxy from §5 fronts it.

After unlocking the console, use **Sandbox** for the first functional smoke:
send text, optionally add an HTTP(S) image URL, wait for the persisted reply,
then open its conversation and expand **追踪**. A sandbox session is ephemeral:
it is intentionally visible for debugging but never participates in memory or
proactive messaging.

## 5. TLS reverse proxy

Any proxy works; Caddy is the shortest path. `/etc/caddy/Caddyfile`:

```
bot.example.com {
    reverse_proxy 127.0.0.1:4173
}
```

Caddy obtains and renews certificates automatically. The web container
already proxies `/health` and `/operator` to the API, so a single upstream
suffices; do not expose 8000/5432/6379/8080 through the proxy. If you use
nginx instead, terminate TLS and `proxy_pass http://127.0.0.1:4173`,
preserving the `Authorization` header.

## 6. Pinning images

Before going to production, resolve tags to digests you have reviewed:

```console
docker pull searxng/searxng:<chosen-tag>
docker inspect --format '{{index .RepoDigests 0}}' searxng/searxng:<chosen-tag>
```

Set `SEARXNG_IMAGE_TAG=<tag>@sha256:<digest>` in `.env` (the compose file
accepts a full `tag@digest` value). PostgreSQL and Redis already use
versioned tags; treat upgrades to them as deliberate changes, and rebuild
`mybot-python`/`mybot-web` only from reviewed commits (`MYBOT_IMAGE_TAG`
names each build).

## 7. Upgrades

```console
scripts/backup.sh --mode compose            # always before an upgrade
git pull --ff-only
docker compose build
docker compose run --rm api alembic upgrade head
docker compose up -d
curl -s http://127.0.0.1:8000/health/ready
```

App containers get a 30-second graceful stop window; workers finish or
release in-flight stream entries, and anything unacked is redelivered after
restart — the pipeline is at-least-once end to end.

## 8. Rollback

```console
git checkout <previous-known-good-tag-or-commit>
docker compose build
docker compose run --rm api alembic downgrade <previous-revision>   # only if the
                                                # bad release added a migration
docker compose up -d
```

Check the migration chain with `uv run alembic history` (or
`docker compose run --rm api alembic history`). If data was damaged,
restore the pre-upgrade backup instead: see `docs/RUNBOOK.md`.

## 9. Security checklist

- Ports: only the proxy's 80/443 public; everything else loopback. Verify
  with `ss -tlnp`.
- Secrets: real values only in `.env` (mode 600) or a secret store; the
  repo history must never contain one.
- Operator token: rotate by editing `.env` and `docker compose up -d api`;
  sessions are stateless so rotation is immediate.
- Plugins: only trusted code; grant capabilities per plugin id; remember
  the runner is not a hostile-code sandbox.
- SearXNG: keep `MYBOT_SEARXNG_URL` internal; never expose 8080 publicly.
- Proactive messaging: leave `MYBOT_PROACTIVE_ENABLED=false` unless the
  affected chats have opted in through the operator API.
- Backups: schedule `scripts/backup.sh` (cron) and rehearse the restore
  drill quarterly per the runbook.
