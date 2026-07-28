"""Composable inbound/outbound moderation with audited failure policy."""

import asyncio
import hashlib
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal, Protocol, cast
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from mybot.adapters.payload import as_string_mapping, get_mapping
from mybot.contracts import (
    ModerationAction,
    ModerationDecision,
    ModerationPoint,
    ModerationRequest,
)
from mybot.contracts.json import thaw_json_object

MODERATION_POLICY_KEY = "moderation.policy"


class ModerationPointPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    action: ModerationAction = ModerationAction.DIRECT_OUTPUT
    preset_response: str = "内容触发了安全策略, 已停止处理。"


class ModerationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    backends: list[Literal["local", "api", "plugin"]] = Field(
        default_factory=lambda: ["local"]
    )
    fail_mode: Literal["open", "closed"] = "open"
    keywords: list[str] = Field(default_factory=list, max_length=10_000)
    inbound: ModerationPointPolicy = Field(default_factory=ModerationPointPolicy)
    outbound: ModerationPointPolicy = Field(default_factory=ModerationPointPolicy)

    def point(self, point: ModerationPoint) -> ModerationPointPolicy:
        return self.inbound if point is ModerationPoint.INBOUND else self.outbound


class PolicyStore(Protocol):
    async def get(self, key: str) -> JsonValue | None: ...


class AuditSink(Protocol):
    async def record(
        self,
        request: ModerationRequest,
        decision: ModerationDecision,
        *,
        content_sha256: str,
        content_preview: str,
        duration_ms: int,
        conversation_stable_key: str | None = None,
        message_id: UUID | None = None,
        trace_id: str | None = None,
        error_code: str | None = None,
    ) -> UUID: ...


class ModerationBackend(Protocol):
    name: str

    async def review(
        self, request: ModerationRequest, policy: ModerationPolicy
    ) -> ModerationDecision: ...


@dataclass(slots=True)
class AhoCorasickMatcher:
    terms: Sequence[str]
    _goto: list[dict[str, int]] = field(init=False)
    _fail: list[int] = field(init=False)
    _output: list[set[str]] = field(init=False)

    def __post_init__(self) -> None:
        self._goto = [{}]
        self._fail = [0]
        self._output = [set()]
        for raw in self.terms:
            term = raw.strip().casefold()
            if not term:
                continue
            state = 0
            for character in term:
                next_state = self._goto[state].get(character)
                if next_state is None:
                    next_state = len(self._goto)
                    self._goto[state][character] = next_state
                    self._goto.append({})
                    self._fail.append(0)
                    self._output.append(set())
                state = next_state
            self._output[state].add(term)
        queue: deque[int] = deque()
        for state in self._goto[0].values():
            queue.append(state)
        while queue:
            state = queue.popleft()
            for character, target in self._goto[state].items():
                queue.append(target)
                failure = self._fail[state]
                while failure and character not in self._goto[failure]:
                    failure = self._fail[failure]
                self._fail[target] = self._goto[failure].get(character, 0)
                self._output[target].update(self._output[self._fail[target]])

    def find(self, text: str) -> tuple[str, ...]:
        state = 0
        found: set[str] = set()
        for character in text.casefold():
            while state and character not in self._goto[state]:
                state = self._fail[state]
            state = self._goto[state].get(character, 0)
            found.update(self._output[state])
        return tuple(sorted(found))


class LocalKeywordModeration:
    name = "local"

    async def review(
        self, request: ModerationRequest, policy: ModerationPolicy
    ) -> ModerationDecision:
        matches = AhoCorasickMatcher(policy.keywords).find(_request_text(request))
        point = policy.point(request.point)
        return ModerationDecision(
            flagged=bool(matches),
            action=point.action,
            preset_response=point.preset_response if matches else "",
            backend=self.name,
            reason="keyword_match" if matches else "clear",
            matched_terms=matches,
        )


@dataclass(slots=True)
class OpenAIModerationBackend:
    client: httpx.AsyncClient
    base_url: str
    api_key: str | None
    model: str = "omni-moderation-latest"
    name: str = "api"

    async def review(
        self, request: ModerationRequest, policy: ModerationPolicy
    ) -> ModerationDecision:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        response = await self.client.post(
            f"{self.base_url.rstrip('/')}/moderations",
            json={"model": self.model, "input": _request_text(request)},
            headers=headers,
        )
        response.raise_for_status()
        payload = as_string_mapping(response.json()) or {}
        results = payload.get("results")
        first = (
            as_string_mapping(cast(list[object], results)[0])
            if isinstance(results, list) and results
            else None
        )
        if first is None:
            raise ValueError("moderation response has no results")
        flagged = first.get("flagged") is True
        categories = get_mapping(first, "categories") or {}
        matched = tuple(sorted(key for key, value in categories.items() if value is True))
        point = policy.point(request.point)
        return ModerationDecision(
            flagged=flagged,
            action=point.action,
            preset_response=point.preset_response if flagged else "",
            backend=self.name,
            reason="remote_flagged" if flagged else "clear",
            matched_terms=matched,
        )


@dataclass(slots=True)
class PluginModerationBackend:
    client: httpx.AsyncClient
    broker_url: str
    name: str = "plugin"

    async def review(
        self, request: ModerationRequest, policy: ModerationPolicy
    ) -> ModerationDecision:
        response = await self.client.post(
            f"{self.broker_url.rstrip('/')}/plugin-broker/moderate",
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        payload = as_string_mapping(response.json()) or {}
        raw = as_string_mapping(payload.get("result")) or payload
        decision = ModerationDecision.model_validate(raw)
        return decision.model_copy(update={"backend": self.name})


@dataclass(slots=True)
class ModerationService:
    policies: PolicyStore
    backends: Mapping[str, ModerationBackend]
    audit: AuditSink
    timeout_seconds: float = 3.0
    clock: Callable[[], float] = monotonic

    async def moderate(
        self,
        request: ModerationRequest,
        *,
        conversation_stable_key: str | None = None,
        message_id: UUID | None = None,
        trace_id: str | None = None,
    ) -> ModerationDecision:
        policy = await self.policy()
        point = policy.point(request.point)
        text = _request_text(request)
        digest = hashlib.sha256(text.encode()).hexdigest()
        preview = text[:160]
        if not policy.enabled or not point.enabled:
            decision = ModerationDecision(backend="disabled", reason="disabled")
            await self.audit.record(
                request,
                decision,
                content_sha256=digest,
                content_preview=preview,
                duration_ms=0,
                conversation_stable_key=conversation_stable_key,
                message_id=message_id,
                trace_id=trace_id,
            )
            return decision
        for backend_name in policy.backends:
            backend = self.backends.get(backend_name)
            if backend is None:
                decision = await self._failure_decision(
                    policy, request, backend_name, "unavailable"
                )
                await self._audit(
                    request, decision, digest, preview, 0, conversation_stable_key, message_id,
                    trace_id, "backend_unavailable"
                )
                if decision.flagged:
                    return decision
                continue
            started = self.clock()
            error_code: str | None = None
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    decision = await backend.review(request, policy)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                error_code = type(error).__name__
                decision = await self._failure_decision(
                    policy, request, backend_name, error_code
                )
            duration_ms = int((self.clock() - started) * 1_000)
            await self._audit(
                request, decision, digest, preview, duration_ms, conversation_stable_key,
                message_id, trace_id, error_code
            )
            if decision.flagged:
                return decision
        return ModerationDecision(backend="chain", reason="clear")

    async def policy(self) -> ModerationPolicy:
        raw = await self.policies.get(MODERATION_POLICY_KEY)
        if raw is None:
            return ModerationPolicy()
        return ModerationPolicy.model_validate(raw)

    async def _failure_decision(
        self,
        policy: ModerationPolicy,
        request: ModerationRequest,
        backend: str,
        error: str,
    ) -> ModerationDecision:
        point = policy.point(request.point)
        closed = policy.fail_mode == "closed"
        return ModerationDecision(
            flagged=closed,
            action=point.action,
            preset_response=point.preset_response if closed else "",
            backend=backend,
            reason=f"backend_error:{error}",
        )

    async def _audit(
        self,
        request: ModerationRequest,
        decision: ModerationDecision,
        digest: str,
        preview: str,
        duration_ms: int,
        stable_key: str | None,
        message_id: UUID | None,
        trace_id: str | None,
        error_code: str | None,
    ) -> None:
        await self.audit.record(
            request,
            decision,
            content_sha256=digest,
            content_preview=preview,
            duration_ms=duration_ms,
            conversation_stable_key=stable_key,
            message_id=message_id,
            trace_id=trace_id,
            error_code=error_code,
        )


def _request_text(request: ModerationRequest) -> str:
    values: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, Mapping):
            for nested in cast(Mapping[object, object], value).values():
                visit(nested)
        elif isinstance(value, (list, tuple)):
            for nested in cast(Sequence[object], value):
                visit(nested)

    visit(thaw_json_object(request.params))
    return "\n".join(values)


__all__ = [
    "MODERATION_POLICY_KEY",
    "AhoCorasickMatcher",
    "LocalKeywordModeration",
    "ModerationPointPolicy",
    "ModerationPolicy",
    "ModerationService",
    "OpenAIModerationBackend",
    "PluginModerationBackend",
]
