from dataclasses import dataclass, field
from uuid import uuid4

import httpx
import pytest

from mybot.contracts import ChatKind, ConversationKey, ToolContext
from mybot.tools.fetch import UrlFetchTool


@dataclass
class FakeWeb:
    body: str = "<html><head><title>Hi</title></head><body><p>Hello world</p></body></html>"
    content_type: str = "text/html"
    status_code: int = 200
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status_code != 200:
            return httpx.Response(self.status_code, text="nope")
        return httpx.Response(
            200, text=self.body, headers={"content-type": self.content_type}
        )


def context() -> ToolContext:
    return ToolContext(
        invocation_id=uuid4(),
        conversation=ConversationKey(
            connection_id="telegram-main", chat_kind=ChatKind.DIRECT, chat_id="777"
        ),
        actor_identity_id="telegram:777",
        granted_capabilities=("web.fetch",),
        correlation_id="corr-1",
    )


def make_tool(
    api: FakeWeb,
    *,
    resolves_to: list[str] | None = None,
    max_bytes: int = 2_000_000,
    max_text_chars: int = 4_000,
) -> UrlFetchTool:
    def resolver(host: str) -> list[str]:
        return resolves_to if resolves_to is not None else ["93.184.216.34"]

    return UrlFetchTool(
        client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
        resolve=resolver,
        max_bytes=max_bytes,
        max_text_chars=max_text_chars,
    )


def test_spec_declares_fetch_capability() -> None:
    tool = make_tool(FakeWeb())

    assert tool.spec.id == "fetch_url"
    assert tool.spec.capabilities == ("web.fetch",)
    assert tool.spec.approval_required is False


@pytest.mark.asyncio
async def test_public_url_fetches_and_extracts_readable_text() -> None:
    api = FakeWeb()
    tool = make_tool(api)

    result = await tool.run(context(), {"url": "https://example.com/page"})

    assert result.ok is True
    assert result.data is not None
    assert "Hello world" in result.data["text"]
    assert "<p>" not in result.data["text"]
    assert result.data["title"] == "Hi"
    assert dict(result.data["sources"][0]) == {
        "label": "Hi",
        "uri": "https://example.com/page",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1"],
)
async def test_private_and_loopback_targets_are_blocked_before_request(ip: str) -> None:
    api = FakeWeb()
    tool = make_tool(api, resolves_to=[ip])

    result = await tool.run(context(), {"url": "https://internal.example/secret"})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "ssrf_blocked"
    assert api.requests == []


@pytest.mark.asyncio
async def test_non_http_scheme_is_refused() -> None:
    api = FakeWeb()
    tool = make_tool(api)

    result = await tool.run(context(), {"url": "file:///etc/passwd"})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_url"
    assert api.requests == []


@pytest.mark.asyncio
async def test_unresolvable_host_is_blocked() -> None:
    api = FakeWeb()
    tool = make_tool(api, resolves_to=[])

    result = await tool.run(context(), {"url": "https://ghost.example/x"})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "ssrf_blocked"


@pytest.mark.asyncio
async def test_oversized_body_is_truncated() -> None:
    big = "<html><body>" + ("A" * 5000) + "</body></html>"
    api = FakeWeb(body=big)
    tool = make_tool(api, max_text_chars=1_000)

    result = await tool.run(context(), {"url": "https://example.com/big"})

    assert result.ok is True
    assert result.data is not None
    assert len(result.data["text"]) <= 1_001  # cap plus ellipsis
    assert result.data["truncated"] is True


@pytest.mark.asyncio
async def test_upstream_error_is_structured_failure() -> None:
    api = FakeWeb(status_code=500)
    tool = make_tool(api)

    result = await tool.run(context(), {"url": "https://example.com/err"})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "fetch_failed"
