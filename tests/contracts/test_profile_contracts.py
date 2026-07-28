from uuid import uuid4

import pytest
from pydantic import ValidationError

from mybot.contracts import (
    AgentProfile,
    PersonaVersion,
    ProfileMemoryPolicy,
    RelationshipMemory,
    ReplyWillingnessPolicy,
    WillingnessComponents,
    WillingnessScore,
)


def test_profile_contract_binds_persona_model_tools_memory_and_willingness() -> None:
    persona_id = uuid4()
    profile = AgentProfile(
        name="闲聊群",
        description="更活泼但保持克制",
        active_persona_version_id=persona_id,
        model_tier="economy",
        tool_capabilities=("web.search", "memory.write"),
        memory=ProfileMemoryPolicy(
            enabled=True,
            retrieval_limit=7,
            expression_examples=4,
            relationship_enabled=True,
        ),
        willingness=ReplyWillingnessPolicy(
            enabled=True,
            threshold=0.76,
            sensitivity=0.9,
            keywords=("麦麦", "机器人"),
        ),
    )

    assert profile.active_persona_version_id == persona_id
    assert profile.model_tier == "economy"
    assert profile.tool_capabilities == ("web.search", "memory.write")
    assert profile.memory.expression_examples == 4
    assert profile.willingness.threshold == 0.76


def test_profile_policy_bounds_reject_spammy_or_unbounded_values() -> None:
    with pytest.raises(ValidationError):
        ReplyWillingnessPolicy(threshold=1.1)
    with pytest.raises(ValidationError):
        ReplyWillingnessPolicy(sensitivity=0.1)
    with pytest.raises(ValidationError):
        ProfileMemoryPolicy(retrieval_limit=0)
    with pytest.raises(ValidationError):
        ProfileMemoryPolicy(expression_examples=21)


def test_persona_versions_are_immutable_and_rollback_is_a_new_version() -> None:
    profile_id = uuid4()
    original = PersonaVersion(
        profile_id=profile_id,
        version=1,
        system_prompt="你是冷静的助手。",
    )
    rollback = PersonaVersion(
        profile_id=profile_id,
        version=3,
        system_prompt=original.system_prompt,
        parent_version_id=original.id,
        change_note="rollback to v1",
    )

    assert rollback.id != original.id
    assert rollback.version == 3
    assert rollback.parent_version_id == original.id
    with pytest.raises(ValidationError):
        original.system_prompt = "mutated"  # type: ignore[misc]


def test_relationship_and_willingness_scores_are_strictly_bounded() -> None:
    relationship = RelationshipMemory(
        subject_identity_id="telegram:777",
        familiarity=42.5,
        impression="喜欢咖啡, 沟通直接。",
        version=2,
    )
    score = WillingnessScore(
        score=0.81,
        threshold=0.75,
        allowed=True,
        reason="question and relevant topic",
        components=WillingnessComponents(
            keyword=0.4,
            question=0.8,
            persona_relevance=0.7,
            memory_relevance=0.9,
            group_heat=0.5,
            presence_penalty=0.2,
        ),
    )

    assert relationship.familiarity == 42.5
    assert score.allowed is True
    assert score.components.memory_relevance == 0.9

    with pytest.raises(ValidationError):
        RelationshipMemory(
            subject_identity_id="telegram:777",
            familiarity=101,
            impression="invalid",
        )
    with pytest.raises(ValidationError, match="allowed"):
        WillingnessScore(
            score=0.2,
            threshold=0.8,
            allowed=True,
            reason="inconsistent",
            components=WillingnessComponents(),
        )
