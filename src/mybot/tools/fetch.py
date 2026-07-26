"""SSRF-guarded URL fetch and readable-text extraction tool."""

import ipaddress
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx
import structlog
from pydantic import JsonValue

from mybot.contracts import ToolContext, ToolError, ToolResult, ToolRisk, ToolSpec

logger = structlog.get_logger("mybot.tools.fetch")

FETCH_SPEC = ToolSpec.model_validate(
    {
        "id": "fetch_url",
        "description": (
            "Fetch a public web page by URL and return its readable text. "
            "Private, loopback, and non-HTTP addresses are refused."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "An absolute http(s) URL."}
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "read_only": True,
        "idempotent": True,
        "risk": ToolRisk.MEDIUM,
        "capabilities": ("web.fetch",),
        "approval_required": False,
    }
)


def _default_resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


@dataclass(slots=True)
class UrlFetchTool:
    client: httpx.AsyncClient
    resolve: Callable[[str], list[str]] = field(default=_default_resolve)
    max_bytes: int = 2_000_000
    max_text_chars: int = 4_000

    @property
    def spec(self) -> ToolSpec:
        return FETCH_SPEC

    async def run(
        self, context: ToolContext, arguments: Mapping[str, JsonValue]
    ) -> ToolResult:
        url = arguments.get("url")
        if not isinstance(url, str) or not url.strip():
            return _fail("invalid_arguments", "the 'url' argument is required")
        parts = urlsplit(url.strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return _fail("invalid_url", "only absolute http(s) URLs are allowed")

        blocked = self._ssrf_reason(parts.hostname)
        if blocked is not None:
            return _fail("ssrf_blocked", blocked)

        truncated = False
        try:
            collected = bytearray()
            async with self.client.stream(
                "GET", url, timeout=self.client.timeout
            ) as response:
                if response.status_code >= 400:
                    return _fail(
                        "fetch_failed",
                        f"upstream returned HTTP {response.status_code}",
                        retryable=response.status_code >= 500 or response.status_code == 429,
                    )
                async for chunk in response.aiter_bytes():
                    collected.extend(chunk)
                    if len(collected) >= self.max_bytes:
                        truncated = True
                        break
        except httpx.HTTPError as error:
            logger.warning("fetch_request_failed", error=type(error).__name__)
            return _fail(
                "fetch_failed", f"fetch error: {type(error).__name__}", retryable=True
            )

        html = bytes(collected[: self.max_bytes]).decode("utf-8", errors="ignore")
        extractor = _TextExtractor()
        extractor.feed(html)
        text = extractor.text()
        if len(text) > self.max_text_chars:
            text = text[: self.max_text_chars - 1] + "…"
            truncated = True

        return ToolResult.success(
            {
                "url": url,
                "title": extractor.title or "",
                "text": text,
                "truncated": truncated,
                "sources": [{"label": extractor.title or url, "uri": url}],
            }
        )

    def _ssrf_reason(self, host: str) -> str | None:
        try:
            addresses = self.resolve(host)
        except OSError:
            return f"could not resolve host {host}"
        if not addresses:
            return f"could not resolve host {host}"
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                return f"host {host} resolved to a non-IP address"
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            ):
                return f"host {host} resolves to a non-public address"
        return None


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self.title: str | None = None

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if not stripped:
            return
        if self._in_title:
            if self.title is None:
                self.title = stripped
            return
        if self._skip_depth == 0:
            self._chunks.append(stripped)

    def text(self) -> str:
        return " ".join(self._chunks)


def _fail(code: str, message: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult.failure(ToolError(code=code, message=message, retryable=retryable))
