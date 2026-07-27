from datetime import UTC, datetime

import pytest

from mybot.contracts import ChatKind, ImageSegment, MessageEnvelope, Platform, TextSegment
from mybot.engine.vision import VisionMode, VisionService
from mybot.infrastructure.llm import ImageContentPart, LlmError, LlmReply


class FakeVisionLlm:
    def __init__(self, *, error: LlmError | None = None) -> None:
        self.error = error
        self.calls = []

    async def complete(self, messages, *, tools=None):  # type: ignore[no-untyped-def]
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return LlmReply(
            text="一只橘猫趴在键盘旁边",
            model="vision",
            prompt_tokens=12,
            completion_tokens=8,
        )


def image_envelope() -> MessageEnvelope:
    return MessageEnvelope(
        id="qq:main:1",
        connection_id="main",
        platform=Platform.QQ,
        chat_kind=ChatKind.DIRECT,
        chat_id="42",
        sender_identity_id="qq:7",
        occurred_at=datetime.now(tz=UTC),
        segments=(
            TextSegment(text="它在干嘛?"),
            ImageSegment(url="https://img.example/cat.png", alt_text="照片"),
        ),
    )


@pytest.mark.asyncio
async def test_describe_mode_uses_vision_slot_then_returns_text_prompt() -> None:
    llm = FakeVisionLlm()
    service = VisionService(llm=llm, mode=VisionMode.DESCRIBE)

    prepared = await service.prepare(image_envelope())

    assert "图片理解: 一只橘猫趴在键盘旁边" in prepared.summary
    assert isinstance(prepared.content, str)
    assert "一只橘猫" in prepared.content
    assert any(
        isinstance(part, ImageContentPart)
        for part in llm.calls[0][-1].content
    )


@pytest.mark.asyncio
async def test_direct_mode_preserves_image_part_without_extra_vision_call() -> None:
    llm = FakeVisionLlm()
    service = VisionService(llm=llm, mode=VisionMode.DIRECT)

    prepared = await service.prepare(image_envelope())

    assert not llm.calls
    assert not isinstance(prepared.content, str)
    assert any(isinstance(part, ImageContentPart) for part in prepared.content)


@pytest.mark.asyncio
async def test_vision_failure_degrades_to_chinese_reference_prompt() -> None:
    llm = FakeVisionLlm(error=LlmError("secret upstream body", retryable=True))
    service = VisionService(llm=llm, mode=VisionMode.DESCRIBE)

    prepared = await service.prepare(image_envelope())

    assert "图片暂时无法解析" in prepared.summary
    assert "https://img.example/cat.png" in str(prepared.content)
