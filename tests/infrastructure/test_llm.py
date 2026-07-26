import json
from dataclasses import dataclass, field

import httpx
import pytest

from mybot.infrastructure.llm import ChatMessage, LlmClient, LlmError, ToolCall


@dataclass
class FakeOpenAiServer:
    responses: list[httpx.Response]
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("no scripted responses left")
        return self.responses.pop(0)


def ok_response(text: str = "hello there") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "deepseek-chat-v3",
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 34},
        },
    )


def make_client(server: FakeOpenAiServer, *, max_retries: int = 2) -> LlmClient:
    return LlmClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(server.handler)),
        base_url="https://llm.example/v1",
        api_key="llm-secret",
        model="deepseek-chat",
        temperature=0.3,
        max_output_tokens=512,
        timeout_seconds=5.0,
        max_retries=max_retries,
        backoff_base_seconds=0.0,
    )


MESSAGES = [
    ChatMessage(role="system", content="You are MyBot."),
    ChatMessage(role="user", content="hi"),
]


@pytest.mark.asyncio
async def test_success_parses_text_model_and_usage_with_exact_request_shape() -> None:
    server = FakeOpenAiServer(responses=[ok_response()])
    client = make_client(server)

    reply = await client.complete(MESSAGES)

    assert reply.text == "hello there"
    assert reply.model == "deepseek-chat-v3"
    assert reply.prompt_tokens == 120
    assert reply.completion_tokens == 34
    request = server.requests[0]
    assert str(request.url) == "https://llm.example/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer llm-secret"
    body = json.loads(request.content.decode())
    assert body == {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "You are MyBot."},
            {"role": "user", "content": "hi"},
        ],
        "temperature": 0.3,
        "max_tokens": 512,
    }


@pytest.mark.asyncio
async def test_rate_limits_and_server_errors_retry_then_succeed() -> None:
    server = FakeOpenAiServer(
        responses=[
            httpx.Response(429, json={"error": "slow down"}),
            httpx.Response(503, text="unavailable"),
            ok_response("recovered"),
        ]
    )
    client = make_client(server)

    reply = await client.complete(MESSAGES)

    assert reply.text == "recovered"
    assert len(server.requests) == 3


@pytest.mark.asyncio
async def test_exhausted_retries_raise_retryable_error() -> None:
    server = FakeOpenAiServer(
        responses=[httpx.Response(500, text="boom") for _ in range(3)]
    )
    client = make_client(server, max_retries=2)

    with pytest.raises(LlmError) as excinfo:
        await client.complete(MESSAGES)

    assert excinfo.value.retryable is True
    assert len(server.requests) == 3


@pytest.mark.asyncio
async def test_client_errors_fail_immediately_as_non_retryable() -> None:
    server = FakeOpenAiServer(
        responses=[httpx.Response(401, json={"error": "bad key"})]
    )
    client = make_client(server)

    with pytest.raises(LlmError) as excinfo:
        await client.complete(MESSAGES)

    assert excinfo.value.retryable is False
    assert len(server.requests) == 1


@pytest.mark.asyncio
async def test_empty_content_is_a_non_retryable_error() -> None:
    server = FakeOpenAiServer(
        responses=[
            httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "  "}}]},
            )
        ]
    )
    client = make_client(server)

    with pytest.raises(LlmError) as excinfo:
        await client.complete(MESSAGES)

    assert excinfo.value.retryable is False


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    }
]


@pytest.mark.asyncio
async def test_tool_call_response_parses_calls_and_finish_reason() -> None:
    server = FakeOpenAiServer(
        responses=[
            httpx.Response(
                200,
                json={
                    "model": "deepseek-chat",
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "web_search",
                                            "arguments": '{"query": "weather"}',
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 40, "completion_tokens": 12},
                },
            )
        ]
    )
    client = make_client(server)

    reply = await client.complete(MESSAGES, tools=TOOLS)

    assert reply.finish_reason == "tool_calls"
    assert reply.text == ""
    assert reply.tool_calls == (
        ToolCall(id="call_1", name="web_search", arguments='{"query": "weather"}'),
    )
    body = json.loads(server.requests[0].content.decode())
    assert body["tools"] == TOOLS
    assert body["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_assistant_tool_call_and_tool_result_messages_serialize_to_wire_shape() -> None:
    server = FakeOpenAiServer(responses=[ok_response("done")])
    client = make_client(server)
    conversation = [
        ChatMessage(role="system", content="You are MyBot."),
        ChatMessage(role="user", content="weather?"),
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=(
                ToolCall(id="call_1", name="web_search", arguments='{"query": "weather"}'),
            ),
        ),
        ChatMessage(role="tool", content="sunny", tool_call_id="call_1", name="web_search"),
    ]

    await client.complete(conversation, tools=TOOLS)

    messages = json.loads(server.requests[0].content.decode())["messages"]
    assert messages[2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"query": "weather"}'},
            }
        ],
    }
    assert messages[3] == {
        "role": "tool",
        "content": "sunny",
        "tool_call_id": "call_1",
        "name": "web_search",
    }


@pytest.mark.asyncio
async def test_no_tools_request_body_is_unchanged() -> None:
    server = FakeOpenAiServer(responses=[ok_response()])
    client = make_client(server)

    await client.complete(MESSAGES)

    body = json.loads(server.requests[0].content.decode())
    assert "tools" not in body
    assert "tool_choice" not in body
    assert body["messages"] == [
        {"role": "system", "content": "You are MyBot."},
        {"role": "user", "content": "hi"},
    ]


@pytest.mark.asyncio
async def test_transport_timeouts_are_retried_and_surface_as_retryable() -> None:
    calls = {"count": 0}

    def timing_out(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise httpx.ReadTimeout("slow upstream", request=request)

    client = LlmClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(timing_out)),
        base_url="https://llm.example/v1",
        api_key=None,
        model="deepseek-chat",
        max_retries=1,
        backoff_base_seconds=0.0,
    )

    with pytest.raises(LlmError) as excinfo:
        await client.complete(MESSAGES)

    assert excinfo.value.retryable is True
    assert calls["count"] == 2
