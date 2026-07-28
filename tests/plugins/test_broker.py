import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from mybot.api import create_app
from mybot.infrastructure.health import ReadinessService
from mybot.plugins.broker import PluginBroker, PluginService
from mybot.settings import Settings


class AlwaysUpProbe:
    async def check(self) -> None:
        return None


def manifest_payload(
    *,
    plugin_id: str = "example.dice",
    requested: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": plugin_id,
        "version": "1.0.0",
        "entrypoint": "mybot.plugins.examples.dice:PLUGIN",
        "event_hooks": ["message"],
        "tools": [
            {
                "id": "roll_dice",
                "description": "Roll dice",
                "input_schema": {"type": "object", "properties": {}},
                "read_only": True,
                "idempotent": False,
                "risk": "NONE",
                "capabilities": [],
                "approval_required": False,
            }
        ],
        "requested_capabilities": requested or [],
    }


def make_app(*, grants: str = "{}"):  # type: ignore[no-untyped-def]
    settings = Settings(
        plugin_capability_grants=grants,
        plugin_invoke_timeout_seconds=0.5,
    )
    return create_app(
        settings=settings,
        readiness=ReadinessService(database=AlwaysUpProbe(), redis=AlwaysUpProbe()),
        bootstrap=False,
    )


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = make_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as instance:
        instance.app = app  # type: ignore[attr-defined]
        yield instance


@pytest.mark.asyncio
async def test_registration_lists_tools_and_health_surfaces_review_data(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/plugin-broker/register",
        json={"runner_id": "runner-1", "manifests": [manifest_payload()]},
    )

    assert response.status_code == 200
    assert response.json() == {"accepted": ["example.dice"]}
    tools = (await client.get("/plugin-broker/tools")).json()["tools"]
    assert tools[0]["plugin_id"] == "example.dice"
    assert tools[0]["spec"]["id"] == "roll_dice"
    health = (await client.get("/plugin-broker/health")).json()
    assert health["runners"] == 1
    assert health["protocol_version"] == 2
    assert health["runner_protocol_versions"] == {"runner-1": 1}
    assert health["plugins"][0]["version"] == "1.0.0"
    assert health["plugins"][0]["granted_capabilities"] == []


@pytest.mark.asyncio
async def test_registration_negotiates_plugin_transport_version(
    client: httpx.AsyncClient,
) -> None:
    current = await client.post(
        "/plugin-broker/register",
        json={
            "runner_id": "runner-current",
            "protocol_version": 1,
            "manifests": [manifest_payload()],
        },
    )
    unsupported = await client.post(
        "/plugin-broker/register",
        json={
            "runner_id": "runner-future",
            "protocol_version": 99,
            "manifests": [manifest_payload(plugin_id="future.plugin")],
        },
    )

    assert current.status_code == 200
    assert unsupported.status_code == 409
    assert "protocol" in unsupported.json()["detail"]


@pytest.mark.asyncio
async def test_ungranted_capability_request_is_refused_with_audit_entry(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/plugin-broker/register",
        json={
            "runner_id": "runner-1",
            "manifests": [manifest_payload(requested=["memory.write"])],
        },
    )

    assert response.status_code == 403
    broker: PluginBroker = client.app.state.plugin_broker  # type: ignore[attr-defined]
    assert broker.audit_log[0].plugin_id == "example.dice"
    assert broker.audit_log[0].reason == "capability_refused"
    assert (await client.get("/plugin-broker/tools")).json()["tools"] == []


@pytest.mark.asyncio
async def test_granted_capabilities_allow_registration() -> None:
    app = make_app(grants='{"example.dice": ["memory.write"]}')
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        response = await client.post(
            "/plugin-broker/register",
            json={
                "runner_id": "runner-1",
                "manifests": [manifest_payload(requested=["memory.write"])],
            },
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_invalid_manifest_is_rejected_with_422(client: httpx.AsyncClient) -> None:
    bad = manifest_payload()
    bad["version"] = "not-semver"

    response = await client.post(
        "/plugin-broker/register", json={"runner_id": "runner-1", "manifests": [bad]}
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_invoke_work_result_round_trip(client: httpx.AsyncClient) -> None:
    await client.post(
        "/plugin-broker/register",
        json={"runner_id": "runner-1", "manifests": [manifest_payload()]},
    )

    async def fake_runner() -> None:
        work = (
            await client.get(
                "/plugin-broker/work",
                params={"runner_id": "runner-1", "wait_seconds": 2.0},
            )
        ).json()
        invocation = work["invocations"][0]
        assert invocation["tool_id"] == "roll_dice"
        assert invocation["arguments"] == {"sides": 6}
        await client.post(
            "/plugin-broker/result",
            json={
                "invocation_id": invocation["invocation_id"],
                "result": {"ok": True, "data": {"value": 4}, "error": None},
            },
        )

    runner_task = asyncio.create_task(fake_runner())
    response = await client.post(
        "/plugin-broker/invoke",
        json={"tool_id": "roll_dice", "arguments": {"sides": 6}, "context": {}},
    )
    await runner_task

    assert response.status_code == 200
    assert response.json()["result"]["data"] == {"value": 4}


@pytest.mark.asyncio
async def test_unknown_tool_is_404_and_unanswered_invoke_times_out(
    client: httpx.AsyncClient,
) -> None:
    assert (
        await client.post(
            "/plugin-broker/invoke", json={"tool_id": "ghost", "arguments": {}}
        )
    ).status_code == 404

    await client.post(
        "/plugin-broker/register",
        json={"runner_id": "runner-1", "manifests": [manifest_payload()]},
    )
    response = await client.post(
        "/plugin-broker/invoke", json={"tool_id": "roll_dice", "arguments": {}}
    )

    assert response.status_code == 504


@pytest.mark.asyncio
async def test_events_fan_out_to_hooked_runners(client: httpx.AsyncClient) -> None:
    await client.post(
        "/plugin-broker/register",
        json={"runner_id": "runner-1", "manifests": [manifest_payload()]},
    )

    posted = await client.post(
        "/plugin-broker/events",
        json={"kind": "message", "payload": {"envelope_id": "qq:qq-main:1"}},
    )
    work = (
        await client.get(
            "/plugin-broker/work",
            params={"runner_id": "runner-1", "wait_seconds": 0.05},
        )
    ).json()

    assert posted.json()["delivered"] == 1
    assert work["events"] == [
        {"kind": "message", "payload": {"envelope_id": "qq:qq-main:1"}}
    ]


@pytest.mark.asyncio
async def test_legacy_runner_receives_new_segments_as_readable_text(
    client: httpx.AsyncClient,
) -> None:
    await client.post(
        "/plugin-broker/register",
        json={"runner_id": "legacy", "manifests": [manifest_payload()]},
    )
    await client.post(
        "/plugin-broker/register",
        json={
            "runner_id": "current",
            "protocol_version": 2,
            "manifests": [manifest_payload(plugin_id="current.plugin")],
        },
    )
    await client.post(
        "/plugin-broker/events",
        json={
            "kind": "message",
            "payload": {
                "envelope": {
                    "trace_id": "trace-1",
                    "ephemeral": True,
                    "segments": [
                        {"type": "sticker", "id": "14", "name": "smile"}
                    ]
                }
            },
        },
    )

    legacy = (
        await client.get(
            "/plugin-broker/work",
            params={"runner_id": "legacy", "wait_seconds": 0.05},
        )
    ).json()
    current = (
        await client.get(
            "/plugin-broker/work",
            params={"runner_id": "current", "wait_seconds": 0.05},
        )
    ).json()

    assert legacy["events"][0]["payload"]["envelope"]["segments"] == [
        {"type": "text", "text": "[sticker: smile]"}
    ]
    assert "trace_id" not in legacy["events"][0]["payload"]["envelope"]
    assert "ephemeral" not in legacy["events"][0]["payload"]["envelope"]
    assert current["events"][0]["payload"]["envelope"]["segments"][0]["type"] == "sticker"
    assert current["events"][0]["payload"]["envelope"]["trace_id"] == "trace-1"


@pytest.mark.asyncio
async def test_reregistration_replaces_a_restarted_runners_tools(
    client: httpx.AsyncClient,
) -> None:
    await client.post(
        "/plugin-broker/register",
        json={"runner_id": "runner-1", "manifests": [manifest_payload()]},
    )
    replacement = manifest_payload(plugin_id="example.coin")
    replacement["tools"][0]["id"] = "flip_coin"  # type: ignore[index]
    replacement["entrypoint"] = "mybot.plugins.examples.coin:PLUGIN"

    await client.post(
        "/plugin-broker/register",
        json={"runner_id": "runner-1", "manifests": [replacement]},
    )
    tools = (await client.get("/plugin-broker/tools")).json()["tools"]

    assert [tool["spec"]["id"] for tool in tools] == ["flip_coin"]


@pytest.mark.asyncio
async def test_manifest_service_requirements_are_negotiated_authorized_and_quoted() -> None:
    async def handler(plugin_id: str, arguments: dict[str, object]) -> dict[str, object]:
        return {"plugin_id": plugin_id, "value": arguments["value"]}

    broker = PluginBroker(
        grants={"example.dice": ("kv.store",)},
        services=(
            PluginService(
                name="kv.store",
                version=1,
                capability="kv.store",
                handler=handler,
            ),
        ),
        service_quota_per_minute=1,
    )
    manifest = manifest_payload(requested=["kv.store"])
    manifest["requires"] = {"kv.store": "1"}
    broker.register("runner-1", [manifest])

    assert broker.service_catalog() == [
        {"name": "kv.store", "version": 1, "capability": "kv.store"}
    ]
    result = await broker.invoke_service(
        runner_id="runner-1",
        plugin_id="example.dice",
        service="kv.store",
        version="1",
        arguments={"value": "ok"},
    )
    assert result == {"plugin_id": "example.dice", "value": "ok"}
    with pytest.raises(Exception, match="quota"):
        await broker.invoke_service(
            runner_id="runner-1",
            plugin_id="example.dice",
            service="kv.store",
            version="1",
            arguments={"value": "again"},
        )
    assert [entry.reason for entry in broker.audit_log][-2:] == [
        "service_invoked",
        "service_quota_exceeded",
    ]

    unsupported = manifest_payload(plugin_id="example.unsupported")
    unsupported["requires"] = {"memory.search": "2"}
    with pytest.raises(Exception, match=r"memory\.search"):
        broker.register("runner-2", [unsupported])
    assert broker.health()["load_errors"]


@pytest.mark.asyncio
async def test_config_and_tasks_are_validated_and_delivered_only_to_target_runner() -> None:
    broker = PluginBroker(grants={})
    target = manifest_payload()
    target["config_schema"] = {
        "type": "object",
        "properties": {"endpoint": {"type": "string"}},
        "required": ["endpoint"],
        "additionalProperties": False,
    }
    target["tasks"] = [{"id": "refresh", "interval_seconds": 60}]
    broker.register("runner-1", [target])
    broker.register("runner-2", [manifest_payload(plugin_id="example.other")])

    assert broker.update_config("example.dice", {"endpoint": "https://example.test"}) == 1
    assert broker.dispatch_task("example.dice", "refresh") == 1
    work = await broker.work("runner-1", wait_seconds=0)
    other = await broker.work("runner-2", wait_seconds=0)

    assert [event["kind"] for event in work["events"]] == [
        "plugin.config.changed",
        "plugin.task",
    ]
    assert other["events"] == []
    with pytest.raises(Exception, match="required"):
        broker.update_config("example.dice", {})
    assert broker.task_catalog()[0]["interval_seconds"] == 60

    assert broker.unregister("runner-1") is True
    assert all(tool["plugin_id"] != "example.dice" for tool in broker.tool_catalog())
