from dataclasses import dataclass, field

import httpx
import pytest

from mybot.contracts import ModerationPoint, ModerationRequest
from mybot.security.moderation import (
    AhoCorasickMatcher,
    LocalKeywordModeration,
    ModerationPolicy,
    ModerationService,
    OpenAIModerationBackend,
)


def test_aho_corasick_reports_overlapping_casefolded_terms_once() -> None:
    matcher = AhoCorasickMatcher(["Bad", "bad word", "word", "bad"])

    assert matcher.find("A BAD WORD and bad") == ("bad", "bad word", "word")


@dataclass
class Config:
    value: object

    async def get(self, _key: str):  # type: ignore[no-untyped-def]
        return self.value


@dataclass
class Audit:
    rows: list[tuple[object, object, dict[str, object]]] = field(default_factory=list)

    async def record(self, request, decision, **kwargs):  # type: ignore[no-untyped-def]
        self.rows.append((request, decision, kwargs))


@pytest.mark.asyncio
async def test_local_moderation_flags_and_audits_inbound_text() -> None:
    audit = Audit()
    service = ModerationService(
        policies=Config({"keywords": ["禁词"], "backends": ["local"]}),
        backends={"local": LocalKeywordModeration()},
        audit=audit,
    )

    decision = await service.moderate(
        ModerationRequest(point=ModerationPoint.INBOUND, params={"text": "这里有禁词"}),
        conversation_stable_key="conv",
        trace_id="trace",
    )

    assert decision.flagged is True
    assert decision.matched_terms == ("禁词",)
    assert audit.rows[0][2]["content_sha256"]
    assert audit.rows[0][2]["conversation_stable_key"] == "conv"


@pytest.mark.asyncio
async def test_fail_closed_turns_backend_errors_into_a_flagged_decision() -> None:
    class Broken:
        name = "api"

        async def review(self, request, policy):  # type: ignore[no-untyped-def]
            raise TimeoutError

    service = ModerationService(
        policies=Config({"backends": ["api"], "fail_mode": "closed"}),
        backends={"api": Broken()},
        audit=Audit(),
        timeout_seconds=0.01,
    )

    decision = await service.moderate(
        ModerationRequest(point=ModerationPoint.OUTBOUND, params={"text": "answer"})
    )

    assert decision.flagged is True
    assert decision.reason.startswith("backend_error:")


@pytest.mark.asyncio
async def test_openai_compatible_moderation_response_is_normalized() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/moderations"
        return httpx.Response(
            200,
            json={"results": [{"flagged": True, "categories": {"violence": True}}]},
        )

    backend = OpenAIModerationBackend(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        base_url="https://moderation.test/v1",
        api_key="secret",
    )

    decision = await backend.review(
        ModerationRequest(point=ModerationPoint.OUTBOUND, params={"text": "x"}),
        ModerationPolicy(),
    )

    assert decision.flagged is True
    assert decision.matched_terms == ("violence",)
