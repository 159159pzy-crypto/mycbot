"""OpenAI-compatible chat completion client with bounded retries."""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import httpx
import structlog

from mybot.adapters.payload import as_string_mapping, get_int, get_mapping, get_str
from mybot.contracts.common import FrozenModel, NonEmptyStr

logger = structlog.get_logger("mybot.llm")

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class ToolCall(FrozenModel):
    id: NonEmptyStr
    name: NonEmptyStr
    arguments: str  # raw JSON string exactly as the model emitted it


class ChatMessage(FrozenModel):
    role: NonEmptyStr
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: NonEmptyStr | None = None
    name: NonEmptyStr | None = None


def _serialize_message(message: ChatMessage) -> dict[str, object]:
    """Minimal OpenAI wire shape: plain messages stay exactly {role, content}."""

    if message.tool_calls:
        return {
            "role": message.role,
            "content": message.content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in message.tool_calls
            ],
        }
    serialized: dict[str, object] = {
        "role": message.role,
        "content": message.content if message.content is not None else "",
    }
    if message.tool_call_id is not None:
        serialized["tool_call_id"] = message.tool_call_id
    if message.name is not None:
        serialized["name"] = message.name
    return serialized


class LlmError(RuntimeError):
    """A chat completion failed; retryable marks transport-level trouble."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(slots=True, frozen=True)
class LlmReply:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = "stop"


@dataclass(slots=True)
class LlmClient:
    """Speaks POST {base_url}/chat/completions for any OpenAI-compatible server."""

    client: httpx.AsyncClient
    base_url: str
    api_key: str | None
    model: str
    temperature: float = 0.7
    max_output_tokens: int = 1_024
    timeout_seconds: float = 60.0
    max_retries: int = 2
    backoff_base_seconds: float = 0.5

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, object]] | None = None,
    ) -> LlmReply:
        body: dict[str, object] = {
            "model": self.model,
            "messages": [_serialize_message(message) for message in messages],
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
        }
        if tools:
            body["tools"] = list(tools)
            body["tool_choice"] = "auto"
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last_error = LlmError("chat completion was never attempted", retryable=True)
        for attempt in range(self.max_retries + 1):
            if attempt > 0 and self.backoff_base_seconds > 0:
                await asyncio.sleep(
                    min(self.backoff_base_seconds * (2 ** (attempt - 1)), 8.0)
                )
            try:
                response = await self.client.post(
                    f"{self.base_url.rstrip('/')}/chat/completions",
                    json=body,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except httpx.HTTPError as error:
                last_error = LlmError(
                    f"chat completion transport failure: {type(error).__name__}",
                    retryable=True,
                )
                logger.warning("llm_transport_failure", attempt=attempt)
                continue
            if response.status_code in _RETRYABLE_STATUS:
                last_error = LlmError(
                    f"chat completion returned HTTP {response.status_code}",
                    retryable=True,
                )
                logger.warning(
                    "llm_retryable_status",
                    status=response.status_code,
                    attempt=attempt,
                )
                continue
            if response.status_code >= 400:
                raise LlmError(
                    f"chat completion rejected with HTTP {response.status_code}",
                    retryable=False,
                )
            return self._parse(response)
        raise last_error

    def _parse(self, response: httpx.Response) -> LlmReply:
        payload = as_string_mapping(response.json()) or {}
        choices = payload.get("choices")
        first = None
        if isinstance(choices, list) and choices:
            first = as_string_mapping(cast(list[object], choices)[0])
        message = get_mapping(first, "message") if first is not None else None
        text = (get_str(message, "content") or "").strip() if message is not None else ""
        tool_calls = _parse_tool_calls(message) if message is not None else ()
        finish_reason = get_str(first, "finish_reason") or "stop" if first is not None else "stop"
        if not text and not tool_calls:
            raise LlmError("chat completion contained no message content", retryable=False)
        usage = get_mapping(payload, "usage") or {}
        return LlmReply(
            text=text,
            model=get_str(payload, "model") or self.model,
            prompt_tokens=get_int(usage, "prompt_tokens") or 0,
            completion_tokens=get_int(usage, "completion_tokens") or 0,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )


def _parse_tool_calls(message: Mapping[str, object]) -> tuple[ToolCall, ...]:
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return ()
    calls: list[ToolCall] = []
    for raw in cast(list[object], raw_calls):
        call = as_string_mapping(raw)
        if call is None:
            continue
        function = get_mapping(call, "function") or {}
        call_id = get_str(call, "id")
        name = get_str(function, "name")
        if call_id is None or name is None:
            continue
        calls.append(
            ToolCall(id=call_id, name=name, arguments=get_str(function, "arguments") or "")
        )
    return tuple(calls)
