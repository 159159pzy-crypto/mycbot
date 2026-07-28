from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import JsonValue

from mybot.contracts import PluginManifest
from mybot.operator.api import OperatorContext, create_operator_router
from mybot.plugins.broker import PluginBroker
from mybot.plugins.control import PluginControlStore
from mybot.plugins.tooling import InstalledPlugin
from mybot.security.pairing import PairingPolicy, PairingRequest
from mybot.settings import Settings
from mybot.skills import SkillStore


class Views:
    pass


@dataclass
class Audit:
    entries: list[tuple[str, dict[str, JsonValue]]] = field(default_factory=list)

    async def record(self, action: str, detail: Mapping[str, JsonValue]) -> None:
        self.entries.append((action, dict(detail)))


class Config:
    async def get(self, key: str) -> JsonValue | None:
        del key
        return None


class Streams:
    async def stream_len(self, stream: str) -> int:
        del stream
        return 0


class Attempts:
    async def record(self, attempt: object) -> None:
        del attempt


@dataclass
class Pairing:
    policies: dict[tuple[str, str], PairingPolicy] = field(default_factory=dict)
    requests: dict[UUID, PairingRequest] = field(default_factory=dict)
    approved_ids: set[UUID] = field(default_factory=set)
    dismissed_ids: set[UUID] = field(default_factory=set)

    async def policy(self, platform: str, connection_id: str) -> PairingPolicy:
        return self.policies.get((platform, connection_id), PairingPolicy("open"))

    async def set_policy(
        self, platform: str, connection_id: str, policy: str, allowlist: list[str]
    ) -> PairingPolicy:
        saved = PairingPolicy(policy, tuple(allowlist))
        self.policies[(platform, connection_id)] = saved
        return saved

    async def pending(self) -> list[dict[str, object]]:
        return [
            {
                "id": str(request_id),
                "platform": request.platform,
                "connection_id": request.connection_id,
                "subject_identity_id": request.subject_identity_id,
                "code": request.code,
                "expires_at": request.expires_at,
            }
            for request_id, request in self.requests.items()
            if request_id not in self.approved_ids and request_id not in self.dismissed_ids
        ]

    async def request(
        self, platform: str, connection_id: str, subject_identity_id: str
    ) -> tuple[PairingRequest, bool]:
        request_id = uuid4()
        request = PairingRequest(
            id=str(request_id),
            platform=platform,
            connection_id=connection_id,
            subject_identity_id=subject_identity_id,
            code="ABCDEFGH",
            expires_at="2026-07-28T10:00:00+00:00",
        )
        self.requests[request_id] = request
        return request, True

    async def approve(self, request_id: UUID) -> bool:
        if request_id not in self.requests:
            return False
        self.approved_ids.add(request_id)
        return True

    async def dismiss(self, request_id: UUID) -> bool:
        if request_id not in self.requests:
            return False
        self.dismissed_ids.add(request_id)
        return True


@dataclass
class Installer:
    manifest: PluginManifest
    install_dir: Path

    def registry(self) -> list[dict[str, str]]:
        return [
            {
                "name": self.manifest.id,
                "version": self.manifest.version,
                "source_url": "https://plugins.example/test.zip",
                "manifest_sha256": "a" * 64,
            }
        ]

    async def install(self, name: str) -> InstalledPlugin:
        if name != self.manifest.id:
            raise KeyError(name)
        return InstalledPlugin(self.manifest, self.install_dir)


@pytest.fixture
async def m6_api(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, OperatorContext]]:
    skills = SkillStore(tmp_path / "skills")
    skills.save(
        "daily-summary",
        (
            "---\nname: daily-summary\ndescription: Daily format\n"
            "trigger: Daily summaries\n---\n\nUse bullets.\n"
        ),
    )
    manifest = PluginManifest.model_validate(
        {
            "id": "test.configurable",
            "version": "1.0.0",
            "entrypoint": "mybot.plugins.examples.dice:PLUGIN",
            "config_schema": {
                "type": "object",
                "properties": {"enabled": {"type": "boolean"}},
                "required": ["enabled"],
                "additionalProperties": False,
            },
        }
    )
    broker = PluginBroker(grants={})
    broker.register(
        "runner-1", [cast(JsonValue, manifest.model_dump(mode="json"))]
    )
    control = PluginControlStore(tmp_path / "plugins-data")
    pairing = Pairing()
    context = OperatorContext(
        views=cast(object, Views()),  # type: ignore[arg-type]
        audit=cast(object, Audit()),  # type: ignore[arg-type]
        config=cast(object, Config()),  # type: ignore[arg-type]
        broker=broker,
        streams=Streams(),
        settings=Settings(),
        model_client=httpx.AsyncClient(),
        model_attempts=cast(object, Attempts()),  # type: ignore[arg-type]
        skills=skills,
        plugin_control=control,
        plugin_installer=cast(object, Installer(manifest, tmp_path / "installed")),  # type: ignore[arg-type]
        pairing=cast(object, pairing),  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.include_router(create_operator_router(context))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        yield client, context
    await context.model_client.aclose()


@pytest.mark.asyncio
async def test_skills_can_be_listed_edited_and_disabled(
    m6_api: tuple[httpx.AsyncClient, OperatorContext],
) -> None:
    client, _context = m6_api

    listed = (await client.get("/operator/skills")).json()["skills"]
    assert listed[0]["name"] == "daily-summary"
    saved = await client.put(
        "/operator/skills/daily-summary",
        json={
            "content": (
                "---\nname: daily-summary\ndescription: New format\n"
                "trigger: Daily summaries\n---\n\nUse a table.\n"
            )
        },
    )
    disabled = await client.put(
        "/operator/skills/daily-summary/enabled", json={"enabled": False}
    )

    assert saved.status_code == 200
    assert saved.json()["skill"]["description"] == "New format"
    assert disabled.json()["skill"]["enabled"] is False


@pytest.mark.asyncio
async def test_plugin_action_and_config_validate_persist_and_notify(
    m6_api: tuple[httpx.AsyncClient, OperatorContext],
) -> None:
    client, context = m6_api

    invalid = await client.put(
        "/operator/plugins/test.configurable/config", json={"config": {}}
    )
    valid = await client.put(
        "/operator/plugins/test.configurable/config",
        json={"config": {"enabled": True}},
    )
    reloaded = await client.post(
        "/operator/plugins/test.configurable/action", json={"action": "reload"}
    )
    listed = (await client.get("/operator/plugins")).json()["plugins"]
    work = await context.broker.work("runner-1", wait_seconds=0)

    assert invalid.status_code == 422
    assert valid.status_code == 200
    assert reloaded.json()["generation"] == 1
    assert listed[0]["config"] == {"enabled": True}
    assert work["events"][0]["kind"] == "plugin.config.changed"  # type: ignore[index]


@pytest.mark.asyncio
async def test_registry_listing_and_install_are_exposed(
    m6_api: tuple[httpx.AsyncClient, OperatorContext],
) -> None:
    client, _context = m6_api

    registry = await client.get("/operator/plugins/registry")
    installed = await client.post(
        "/operator/plugins/install", json={"name": "test.configurable"}
    )

    assert registry.json()["plugins"][0]["name"] == "test.configurable"
    assert installed.status_code == 200
    assert installed.json()["plugin"]["version"] == "1.0.0"


@pytest.mark.asyncio
async def test_pairing_policy_generation_approval_and_dismissal(
    m6_api: tuple[httpx.AsyncClient, OperatorContext],
) -> None:
    client, _context = m6_api

    policy = await client.put(
        "/operator/pairing/policies/telegram",
        json={
            "connection_id": "telegram-main",
            "policy": "paired",
            "allowlist": [],
        },
    )
    first = await client.post(
        "/operator/pairing/requests",
        json={
            "platform": "telegram",
            "connection_id": "telegram-main",
            "subject_identity_id": "telegram:42",
        },
    )
    second = await client.post(
        "/operator/pairing/requests",
        json={
            "platform": "telegram",
            "connection_id": "telegram-main",
            "subject_identity_id": "telegram:43",
        },
    )
    approved = await client.post(
        f"/operator/pairing/requests/{first.json()['request']['id']}/approve"
    )
    dismissed = await client.post(
        f"/operator/pairing/requests/{second.json()['request']['id']}/dismiss"
    )
    overview = await client.get("/operator/pairing")

    assert policy.json()["policy"] == "paired"
    assert approved.json() == {"approved": True}
    assert dismissed.json() == {"dismissed": True}
    assert overview.json()["requests"] == []
