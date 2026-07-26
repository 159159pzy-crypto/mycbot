import json
from dataclasses import dataclass, field

import httpx
import pytest

from mybot.infrastructure.embeddings import EmbeddingClient, EmbeddingError


@dataclass
class FakeEmbeddingServer:
    responses: list[httpx.Response]
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


def ok_response(vectors: list[list[float]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": [
                {"index": index, "embedding": vector}
                for index, vector in reversed(list(enumerate(vectors)))
            ],
            "model": "test-embed",
        },
    )


def make_client(server: FakeEmbeddingServer, *, max_retries: int = 2) -> EmbeddingClient:
    return EmbeddingClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(server.handler)),
        base_url="https://llm.example/v1",
        api_key="embed-secret",
        model="test-embed",
        max_retries=max_retries,
        backoff_base_seconds=0.0,
    )


@pytest.mark.asyncio
async def test_batch_embeds_in_input_order_with_exact_request_shape() -> None:
    server = FakeEmbeddingServer(responses=[ok_response([[0.1, 0.2], [0.3, 0.4]])])
    client = make_client(server)

    vectors = await client.embed(["第一段", "第二段"])

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]  # ordered by index despite shuffled data
    request = server.requests[0]
    assert str(request.url) == "https://llm.example/v1/embeddings"
    assert request.headers["Authorization"] == "Bearer embed-secret"
    assert json.loads(request.content.decode()) == {
        "model": "test-embed",
        "input": ["第一段", "第二段"],
    }


@pytest.mark.asyncio
async def test_empty_input_short_circuits_without_a_request() -> None:
    server = FakeEmbeddingServer(responses=[])
    client = make_client(server)

    assert await client.embed([]) == []
    assert server.requests == []


@pytest.mark.asyncio
async def test_retryable_statuses_retry_then_surface() -> None:
    server = FakeEmbeddingServer(
        responses=[
            httpx.Response(429, json={}),
            httpx.Response(503, text="down"),
            ok_response([[1.0]]),
        ]
    )
    client = make_client(server)

    assert await client.embed(["hi"]) == [[1.0]]
    assert len(server.requests) == 3

    exhausted = FakeEmbeddingServer(responses=[httpx.Response(500)] * 3)
    with pytest.raises(EmbeddingError) as excinfo:
        await make_client(exhausted).embed(["hi"])
    assert excinfo.value.retryable is True


@pytest.mark.asyncio
async def test_client_errors_fail_fast() -> None:
    server = FakeEmbeddingServer(responses=[httpx.Response(401, json={})])
    client = make_client(server)

    with pytest.raises(EmbeddingError) as excinfo:
        await client.embed(["hi"])

    assert excinfo.value.retryable is False
    assert len(server.requests) == 1


@pytest.mark.asyncio
async def test_mismatched_vector_count_raises() -> None:
    server = FakeEmbeddingServer(responses=[ok_response([[1.0]])])
    client = make_client(server)

    with pytest.raises(EmbeddingError):
        await client.embed(["one", "two"])
