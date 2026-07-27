from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi import FastAPI
from pydantic import JsonValue

from mybot.infrastructure.model_routing import ModelCallAttempt
from mybot.operator.api import OperatorContext, create_operator_router
from mybot.settings import Settings


@dataclass
class Config:
    values: dict[str, JsonValue] = field(default_factory=dict)

    async def get(self, key: str) -> JsonValue | None:
        return self.values.get(key)

    async def set(self, key: str, value: JsonValue) -> None:
        self.values[key] = value


@dataclass
class Audit:
    entries: list[tuple[str, Mapping[str, JsonValue]]] = field(default_factory=list)

    async def record(self, action: str, detail: Mapping[str, JsonValue]) -> None:
        self.entries.append((action, detail))


@dataclass
class Attempts:
    entries: list[ModelCallAttempt] = field(default_factory=list)

    async def record(self, attempt: ModelCallAttempt) -> None:
        self.entries.append(attempt)


class Views:
    async def model_channel_usage(self, *, days: int = 30) -> list[dict[str, JsonValue]]:
        assert days == 30
        return [
            {
                "channel": "primary",
                "calls": 3,
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cost_usd_micros": 80,
                "last_status": "success",
                "last_called_at": "2026-07-27T00:00:00+00:00",
            }
        ]

    async def model_daily_usage(self, *, days: int = 30) -> list[dict[str, JsonValue]]:
        assert days == 30
        return [
            {
                "day": "2026-07-27",
                "calls": 3,
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cost_usd_micros": 80,
            }
        ]

    async def model_conversation_usage(
        self, *, days: int = 30, limit: int = 10
    ) -> list[dict[str, JsonValue]]:
        assert days == 30
        assert limit == 10
        return [
            {
                "conversation_id": "00000000-0000-0000-0000-000000000001",
                "stable_key": "v1:qq-main:DIRECT:10001:0",
                "calls": 2,
                "prompt_tokens": 80,
                "completion_tokens": 10,
                "cost_usd_micros": 60,
            }
        ]


class Broker:
    def health(self) -> dict[str, JsonValue]:
        return {"runners": 0, "plugins": []}


class Streams:
    async def stream_len(self, stream: str) -> int:
        return 0


def channel_payload() -> dict[str, object]:
    return {
        "name": "primary",
        "base_url": "https://models.example/v1",
        "api_key_env": "TEST_MODEL_KEY",
        "priority": 0,
        "weight": 1,
        "enabled": True,
        "model_map": {
            "chat": {
                "model": "chat-model",
                "input_price_per_million": "0.50",
                "output_price_per_million": "1.50",
            },
            "embedding": {"model": "embed-model"},
        },
    }


@pytest.fixture
async def model_api(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_MODEL_KEY", "secret-key")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-key"
        if request.url.host == "fail.example":
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "model": "chat-model",
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    config = Config()
    audit = Audit()
    attempts = Attempts()
    context = OperatorContext(
        views=Views(),  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
        config=config,  # type: ignore[arg-type]
        broker=Broker(),  # type: ignore[arg-type]
        streams=Streams(),
        settings=Settings(),
        model_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        model_attempts=attempts,
    )
    app = FastAPI()
    app.include_router(create_operator_router(context))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        yield client, config, audit, attempts
    await context.model_client.aclose()


@pytest.mark.asyncio
async def test_model_channels_round_trip_without_secret_values(model_api) -> None:  # type: ignore[no-untyped-def]
    client, config, audit, _ = model_api

    saved = await client.put("/operator/models", json={"channels": [channel_payload()]})
    listed = await client.get("/operator/models")

    assert saved.status_code == 200
    assert listed.json()["source"] == "runtime"
    assert listed.json()["channels"][0]["api_key_env"] == "TEST_MODEL_KEY"
    assert listed.json()["daily_usage"][0]["day"] == "2026-07-27"
    assert listed.json()["conversation_usage"][0]["calls"] == 2
    assert "secret-key" not in listed.text
    assert config.values["models.channels"][0]["name"] == "primary"  # type: ignore[index]
    assert audit.entries[0][0] == "models.update"


@pytest.mark.asyncio
async def test_model_connectivity_test_uses_referenced_environment_secret(model_api) -> None:  # type: ignore[no-untyped-def]
    client, _, audit, attempts = model_api
    await client.put("/operator/models", json={"channels": [channel_payload()]})

    response = await client.post("/operator/models/primary/test")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["model"] == "chat-model"
    assert audit.entries[-1][0] == "models.test"
    assert attempts.entries[-1].status == "success"
    assert attempts.entries[-1].channel == "primary"


@pytest.mark.asyncio
async def test_model_connectivity_failure_is_accounted_without_secret_detail(model_api) -> None:  # type: ignore[no-untyped-def]
    client, _, audit, attempts = model_api
    payload = channel_payload()
    payload["base_url"] = "https://fail.example/v1"
    await client.put("/operator/models", json={"channels": [payload]})

    response = await client.post("/operator/models/primary/test")

    assert response.status_code == 502
    assert "secret-key" not in response.text
    assert audit.entries[-1][0] == "models.test"
    assert attempts.entries[-1].status == "retryable_error"


@pytest.mark.asyncio
async def test_model_update_rejects_mixed_embedding_models(model_api) -> None:  # type: ignore[no-untyped-def]
    client, _, _, _ = model_api
    second = channel_payload()
    second["name"] = "backup"
    second["model_map"] = {"embedding": {"model": "incompatible-embed-model"}}

    response = await client.put("/operator/models", json={"channels": [channel_payload(), second]})

    assert response.status_code == 422
    assert "embedding" in response.json()["detail"]
