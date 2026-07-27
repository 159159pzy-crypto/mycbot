import json
from pathlib import Path

from mybot.adapters.qq.translate import translate_qq_event
from mybot.contracts import AtSegment, StickerSegment, VoiceSegment

FIXTURES = Path(__file__).parent / "fixtures" / "onebot_v11"


def load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_napcat_rich_fixture_replays_through_platform_neutral_contract() -> None:
    event = translate_qq_event(
        load("napcat_group_rich.json"), connection_id="qq-main", self_id=10000
    )

    assert event is not None
    assert event.mentions_self is True
    assert any(isinstance(segment, AtSegment) for segment in event.envelope.segments)
    assert any(isinstance(segment, StickerSegment) for segment in event.envelope.segments)
    assert any(isinstance(segment, VoiceSegment) for segment in event.envelope.segments)


def test_lagrange_hot_spare_fixture_needs_no_core_contract_change() -> None:
    event = translate_qq_event(
        load("lagrange_private_text.json"),
        connection_id="qq-hot-spare",
        self_id=10000,
    )

    assert event is not None
    assert event.envelope.connection_id == "qq-hot-spare"
    assert event.envelope.chat_id == "10002"
    assert event.envelope.segments[0].type == "text"
