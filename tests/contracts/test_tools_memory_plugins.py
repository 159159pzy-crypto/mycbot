from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mybot.contracts import (
    ChatKind,
    ConversationKey,
    MemoryItem,
    MemoryPrivacy,
    MemoryScope,
    Platform,
    PluginManifest,
    ToolContext,
    ToolError,
    ToolResult,
    ToolRisk,
    ToolSpec,
)


def test_tool_contracts_express_policy_context_and_structured_outcomes() -> None:
    spec = ToolSpec(
        id="web.search",
        description="Search configured web sources",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        read_only=True,
        idempotent=True,
        risk=ToolRisk.LOW,
        capabilities=("network.http",),
        approval_required=False,
    )
    context = ToolContext(
        invocation_id=uuid4(),
        conversation=ConversationKey(
            connection_id="tg-main",
            chat_kind=ChatKind.DIRECT,
            chat_id="42",
        ),
        actor_identity_id="identity-1",
        granted_capabilities=("network.http",),
        correlation_id="trace-123",
    )
    success = ToolResult.success({"items": [{"title": "result"}]})
    failure = ToolResult.failure(
        ToolError(code="upstream_timeout", message="Search timed out", retryable=True)
    )

    assert spec.read_only and spec.idempotent
    assert context.correlation_id == "trace-123"
    assert success.ok is True and success.data is not None and success.error is None
    assert failure.ok is False and failure.data is None and failure.error is not None


def test_tool_and_plugin_json_fields_are_deeply_immutable() -> None:
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    spec = ToolSpec(
        id="web.search",
        description="Search the web",
        input_schema=schema,
        read_only=True,
        idempotent=True,
        risk=ToolRisk.LOW,
        approval_required=False,
    )
    manifest = PluginManifest(
        id="com.example.search",
        version="1.0.0",
        entrypoint="example.plugin:create",
        config_schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
    )
    success = ToolResult.success({"items": [{"title": "result"}]})
    failure = ToolResult.failure(
        ToolError(
            code="upstream",
            message="failed",
            details={"attempt": {"delays": [1, 2]}},
        )
    )

    with pytest.raises(TypeError):
        cast(dict[str, Any], spec.input_schema["properties"])["other"] = {}
    with pytest.raises((AttributeError, TypeError)):
        cast(list[str], spec.input_schema["required"]).append("other")
    with pytest.raises(TypeError):
        cast(dict[str, Any], manifest.config_schema["properties"])["other"] = {}
    with pytest.raises(TypeError):
        cast(dict[str, Any], cast(list[Any], success.data["items"])[0])["title"] = "changed"  # type: ignore[index]
    with pytest.raises((AttributeError, TypeError)):
        cast(
            list[int],
            cast(dict[str, Any], failure.error.details["attempt"])["delays"],  # type: ignore[union-attr]
        ).append(3)

    assert spec.model_dump(mode="json")["input_schema"] == schema
    assert success.model_dump(mode="json")["data"] == {"items": [{"title": "result"}]}


@pytest.mark.parametrize(
    ("ok", "data", "error"),
    [
        (True, None, None),
        (True, {"value": 1}, ToolError(code="bad", message="bad")),
        (False, None, None),
        (False, {"value": 1}, ToolError(code="bad", message="bad")),
    ],
)
def test_tool_result_rejects_ambiguous_payloads(
    ok: bool, data: dict[str, object] | None, error: ToolError | None
) -> None:
    with pytest.raises(ValidationError):
        ToolResult(ok=ok, data=data, error=error)


def test_memory_item_enforces_scope_validity_and_revocation_rules() -> None:
    memory_id = uuid4()
    source_id = "message-123"
    item = MemoryItem(
        id=memory_id,
        scope=MemoryScope.SUBJECT,
        subject_identity_id="identity-1",
        kind="preference",
        content="Prefers compact technical answers",
        source_message_ids=(source_id,),
        confidence=0.88,
        privacy=MemoryPrivacy.PRIVATE,
        valid_from=datetime(2026, 7, 14, tzinfo=UTC),
    )

    assert item.id == memory_id
    assert item.source_message_ids == (source_id,)

    with pytest.raises(ValidationError, match="subject_identity_id"):
        MemoryItem(
            scope=MemoryScope.SUBJECT,
            kind="preference",
            content="Missing subject",
            source_message_ids=(source_id,),
            confidence=0.5,
            privacy=MemoryPrivacy.PRIVATE,
        )

    with pytest.raises(ValidationError, match="valid_until"):
        MemoryItem(
            scope=MemoryScope.GLOBAL,
            kind="fact",
            content="Invalid interval",
            source_message_ids=(source_id,),
            confidence=0.5,
            privacy=MemoryPrivacy.PUBLIC,
            valid_from=datetime(2026, 7, 15, tzinfo=UTC),
            valid_until=datetime(2026, 7, 14, tzinfo=UTC),
        )

    with pytest.raises(ValidationError, match="revoked_reason"):
        MemoryItem(
            scope=MemoryScope.GLOBAL,
            kind="fact",
            content="Revoked without reason",
            source_message_ids=(source_id,),
            confidence=0.5,
            privacy=MemoryPrivacy.PUBLIC,
            revoked_at=datetime.now(UTC) + timedelta(seconds=1),
        )


def test_plugin_manifest_is_frozen_and_validates_requested_surfaces() -> None:
    manifest = PluginManifest(
        id="com.example.search",
        version="1.2.3",
        entrypoint="example_search.plugin:create_plugin",
        event_hooks=("message.received",),
        tools=(
            ToolSpec(
                id="web.search",
                description="Search the web",
                input_schema={"type": "object"},
                read_only=True,
                idempotent=True,
                risk=ToolRisk.LOW,
                capabilities=("network.http",),
                approval_required=False,
            ),
        ),
        tasks=("search.refresh_index",),
        config_schema={"type": "object", "additionalProperties": False},
        platforms=frozenset({Platform.QQ, Platform.TELEGRAM}),
        requested_capabilities=("network.http",),
    )

    assert manifest.version == "1.2.3"
    with pytest.raises(ValidationError):
        manifest.version = "2.0.0"  # type: ignore[misc]

    with pytest.raises(ValidationError, match="semantic version"):
        PluginManifest(
            id="com.example.bad",
            version="latest",
            entrypoint="bad.plugin:create",
        )
