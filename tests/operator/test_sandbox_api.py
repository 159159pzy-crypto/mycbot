from dataclasses import dataclass, field

import httpx
import pytest
from fastapi import FastAPI
from pydantic import JsonValue

from mybot.adapters import InboundEvent
from mybot.operator.api import OperatorContext, create_operator_router
from mybot.settings import Settings


class Views:
    async def conversation_by_stable_key(self, stable_key: str):  # type: ignore[no-untyped-def]
        return None


@dataclass
class Publisher:
    payloads: list[str] = field(default_factory=list)

    async def publish(self, payload: str) -> str:
        self.payloads.append(payload)
        return "1-0"


@dataclass
class Audit:
    entries: list[tuple[str, dict[str, JsonValue]]] = field(default_factory=list)

    async def record(self, action: str, detail):  # type: ignore[no-untyped-def]
        self.entries.append((action, dict(detail)))


class Config:
    async def get(self, key: str):  # type: ignore[no-untyped-def]
        return None


class Broker:
    def health(self):  # type: ignore[no-untyped-def]
        return {"runners": 0, "plugins": []}


class Streams:
    async def stream_len(self, stream: str) -> int:
        return 0


class Attempts:
    async def record(self, attempt):  # type: ignore[no-untyped-def]
        return None


@pytest.fixture
async def sandbox_api():  # type: ignore[no-untyped-def]
    publisher = Publisher()
    audit = Audit()
    context = OperatorContext(
        views=Views(),  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
        config=Config(),  # type: ignore[arg-type]
        broker=Broker(),  # type: ignore[arg-type]
        streams=Streams(),
        settings=Settings(),
        model_client=httpx.AsyncClient(),
        model_attempts=Attempts(),  # type: ignore[arg-type]
        sandbox=publisher,
    )
    app = FastAPI()
    app.include_router(create_operator_router(context))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        yield client, publisher, audit
    await context.model_client.aclose()


@pytest.mark.asyncio
async def test_sandbox_submission_publishes_ephemeral_multimodal_inbound(sandbox_api) -> None:  # type: ignore[no-untyped-def]
    client, publisher, audit = sandbox_api

    response = await client.post(
        "/operator/sandbox/messages",
        json={
            "session_id": "debug-1",
            "text": "这是什么?",
            "image_urls": ["https://img.example/cat.png"],
        },
    )

    assert response.status_code == 200
    event = InboundEvent.model_validate_json(publisher.payloads[0])
    assert event.envelope.platform.value == "SANDBOX"
    assert event.envelope.ephemeral is True
    assert [segment.type for segment in event.envelope.segments] == ["text", "image"]
    assert response.json()["trace_id"] == event.envelope.trace_id
    assert audit.entries[0][0] == "sandbox.message"


@pytest.mark.asyncio
async def test_sandbox_rejects_unsafe_image_scheme_and_reports_pending_session(sandbox_api) -> None:  # type: ignore[no-untyped-def]
    client, publisher, _ = sandbox_api

    rejected = await client.post(
        "/operator/sandbox/messages",
        json={"image_urls": ["file:///etc/passwd"]},
    )
    pending = await client.get("/operator/sandbox/debug-2")

    assert rejected.status_code == 422
    assert publisher.payloads == []
    assert pending.json() == {"status": "pending", "session_id": "debug-2"}
