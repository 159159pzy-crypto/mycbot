from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def indented_block(document: str, header: str) -> str:
    lines = document.splitlines()
    try:
        start = lines.index(header)
    except ValueError as error:
        raise AssertionError(f"missing configuration block: {header.strip()}") from error

    indentation = len(header) - len(header.lstrip())
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        line_indentation = len(line) - len(line.lstrip())
        if line_indentation <= indentation:
            end = index
            break
    return "\n".join(lines[start:end])


def test_web_health_is_independent_from_api_readiness() -> None:
    compose = source("compose.yaml")
    nginx = source("web/nginx.conf")
    web = indented_block(compose, "  web:")

    assert "depends_on:" not in web
    assert '"127.0.0.1:${WEB_PORT:-4173}:8080"' in web
    assert "http://127.0.0.1:8080/static-health" in web
    assert "location = /static-health {" in nginx
    assert "return 200" in nginx


def test_web_container_runs_unprivileged_with_restricted_capabilities() -> None:
    dockerfile = source("web/Dockerfile")
    compose = source("compose.yaml")
    nginx = source("web/nginx.conf")
    web = indented_block(compose, "  web:")

    assert "FROM nginxinc/nginx-unprivileged:1.29-alpine" in dockerfile
    assert "USER nginx" in dockerfile
    assert "EXPOSE 8080" in dockerfile
    assert "http://127.0.0.1:8080/static-health" in dockerfile
    assert "listen 8080;" in nginx
    assert "read_only: true" in web
    assert "cap_drop:\n      - ALL" in web
    assert "no-new-privileges:true" in web


def test_nginx_resolves_api_only_when_a_proxy_request_arrives() -> None:
    nginx = source("web/nginx.conf")

    assert "resolver 127.0.0.11" in nginx
    assert "set $api_upstream api:8000;" in nginx
    assert "proxy_pass http://$api_upstream;" in nginx
    assert "proxy_pass http://api:8000;" not in nginx


def test_plugin_runner_has_only_internal_control_plane_access() -> None:
    compose = source("compose.yaml")
    environment = source(".env.example")
    plugin = indented_block(compose, "  plugin-runner:")
    api = indented_block(compose, "  api:")
    plugin_network = indented_block(compose, "  plugin-control:")

    assert "<<: *app-service" not in plugin
    assert "*app-environment" not in plugin
    assert "MYBOT_DATABASE_URL" not in plugin
    assert "MYBOT_REDIS_URL" not in plugin
    assert "MYBOT_PLUGIN_BROKER_URL:" in plugin
    assert "MYBOT_PLUGIN_CONFIG:" in plugin
    assert "volumes:" not in plugin
    assert "ports:" not in plugin
    assert "extra_hosts:" not in plugin
    assert "- backend" not in plugin
    assert "- plugin-control" in plugin
    assert "- plugin-control" in api
    assert "internal: true" in plugin_network

    for service in (
        "postgres",
        "redis",
        "searxng",
        "gateway",
        "agent-worker",
        "maintenance-worker",
        "web",
    ):
        assert "plugin-control" not in indented_block(compose, f"  {service}:")

    assert "MYBOT_PLUGIN_BROKER_URL=" in environment
    assert "MYBOT_PLUGIN_CONFIG=" in environment


def test_ci_builds_images_and_exercises_migrations_and_health() -> None:
    workflow = source(".github/workflows/ci.yml")
    images = indented_block(workflow, "  images:")
    integration = indented_block(workflow, "  integration:")

    assert "docker build --tag mybot-python:ci ." in images
    assert "docker build --tag mybot-web:ci web" in images

    assert "postgres:" in integration
    assert "pgvector/pgvector:pg16" in integration
    assert "redis:" in integration
    assert "redis:7.4-alpine" in integration
    first_upgrade = integration.index("uv run alembic upgrade head")
    downgrade = integration.index("uv run alembic downgrade base", first_upgrade + 1)
    second_upgrade = integration.index("uv run alembic upgrade head", downgrade + 1)
    assert first_upgrade < downgrade < second_upgrade
    assert "uv run python -m mybot api" in integration
    assert "curl --fail --silent --show-error http://127.0.0.1:8000/health/live" in integration
    assert "curl --fail --silent --show-error http://127.0.0.1:8000/health/ready" in integration


def test_ci_smoke_runs_the_hardened_web_image_and_always_removes_it() -> None:
    workflow = source(".github/workflows/ci.yml")
    images = indented_block(workflow, "  images:")

    build = images.index("docker build --tag mybot-web:ci web")
    run = images.index("docker run --detach", build + 1)
    assert build < run
    assert "--read-only" in images
    assert "--cap-drop ALL" in images
    assert "--tmpfs /tmp:rw,noexec,nosuid,size=16m" in images
    assert "--publish 127.0.0.1:18080:8080" in images
    assert "http://127.0.0.1:18080/static-health" in images
    assert "trap cleanup EXIT" in images
    assert "docker rm --force mybot-web-smoke" in images


def test_operator_docs_describe_the_new_isolation_and_health_boundaries() -> None:
    readme = source("README.md")
    normalized = " ".join(readme.split())

    assert "GET /static-health" in normalized
    assert "does not wait for API readiness" in normalized
    assert "MYBOT_PLUGIN_BROKER_URL" in normalized
    assert "plugin-control" in normalized
    assert "container image builds" in normalized
    assert "upgrade, downgrade, and re-upgrade" in normalized


def test_dependency_timeout_defaults_are_exposed_to_operators() -> None:
    compose = source("compose.yaml")
    environment = source(".env.example")
    readme = source("README.md")
    app_environment = indented_block(compose, "x-app-environment: &app-environment")
    defaults = {
        "MYBOT_HEALTH_PROBE_TIMEOUT_SECONDS": "2",
        "MYBOT_DATABASE_CONNECT_TIMEOUT_SECONDS": "3",
        "MYBOT_DATABASE_READ_TIMEOUT_SECONDS": "3",
        "MYBOT_REDIS_CONNECT_TIMEOUT_SECONDS": "2",
        "MYBOT_REDIS_READ_TIMEOUT_SECONDS": "2",
    }

    for name, value in defaults.items():
        assert f"{name}: ${{{name}:-{value}}}" in app_environment
        assert f"{name}={value}" in environment
        assert name in readme
