# MyBot engineering foundation

MyBot is a self-hosted QQ and Telegram agent-bot foundation. Milestone 1
establishes typed contracts, dependency health, cancellable process roles, a
small operator UI, migrations, and a container topology. It intentionally does
not include platform business behavior or bundle a NapCat deployment.

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
| API | `uv run python -m mybot api` | FastAPI lifecycle and public health endpoints |
| Gateway | `uv run python -m mybot gateway` | Platform adapter process boundary |
| Agent worker | `uv run python -m mybot agent-worker` | Agent-work lifecycle boundary |
| Maintenance worker | `uv run python -m mybot maintenance-worker` | Scheduled/maintenance lifecycle boundary |
| Plugin runner | `uv run python -m mybot plugin-runner` | Isolated plugin lifecycle boundary |

The non-API modes are deliberately idle lifecycle services until later
milestones add platform and agent behavior; they are not print-only stubs.
In Compose, `plugin-runner` receives only `MYBOT_PLUGIN_BROKER_URL` and
`MYBOT_PLUGIN_CONFIG`. It shares the internal-only `plugin-control` network with
the API and is not attached to the general backend network.

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

## Optional external NapCat connection

NapCat is intentionally not a required Compose service. Manage it separately,
keep its WebSocket endpoint off the public internet, and give it a narrowly
scoped access token. A host-managed instance is reachable from the gateway
container as `host.docker.internal`; set the reserved `NAPCAT_WS_URL` and
`MYBOT_QQ_ACCESS_TOKEN` values in `.env` when the QQ adapter milestone consumes
them. On Linux, Compose adds the `host-gateway` mapping for that hostname.

The current gateway establishes the process boundary only; it does not yet
claim an active NapCat protocol connection.

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

CI runs Python lint/type/tests, frontend tests/build, Compose validation, both
container image builds, and an integration job backed by pgvector PostgreSQL
and Redis. The integration job applies an Alembic upgrade, downgrade, and
re-upgrade before starting the API and checking liveness and readiness.

## Deployment notes

- Replace every placeholder credential and keep production secrets outside the
  repository, preferably in the deployment platform's secret store.
- Pin reviewed container digests for production. The local SearXNG service uses
  `latest` to keep this foundation runnable against its evolving settings
  schema.
- Put TLS and authentication in a reverse proxy before exposing the API, web,
  or SearXNG surfaces beyond loopback.
- `plugin-runner` drops Linux capabilities, uses a read-only root filesystem,
  enables `no-new-privileges`, receives no database or Redis credentials, and
  has only the internal `plugin-control` network. A container is still not a
  complete hostile code sandbox; add stronger isolation before accepting
  untrusted plugins.
- Back up the named PostgreSQL volume before migrations or upgrades.
