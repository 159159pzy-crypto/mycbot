from datetime import UTC, datetime

import pytest

from mybot.contracts import ChatKind, ImageSegment, MessageEnvelope, Platform, TextSegment
from mybot.engine.vision import VisionMode, VisionService
from mybot.infrastructure.llm import ImageContentPart, ImageUrl, LlmError, LlmReply


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


class TelegramImageResolver:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def resolve(self, segment: ImageSegment) -> ImageSegment:
        self.urls.append(segment.url)
        return segment.model_copy(
            update={"url": "data:image/jpeg;base64,dGVsZWdyYW0taW1hZ2U="}
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
async def test_telegram_internal_image_is_resolved_before_direct_vision_call() -> None:
    llm = FakeVisionLlm()
    resolver = TelegramImageResolver()
    envelope = image_envelope().model_copy(
        update={
            "platform": Platform.TELEGRAM,
            "segments": (
                TextSegment(text="这是什么"),
                ImageSegment(url="tg-file://photo-1"),
            ),
        }
    )
    service = VisionService(
        llm=llm,
        mode=VisionMode.DIRECT,
        image_resolver=resolver,
    )

    prepared = await service.prepare(envelope)

    assert resolver.urls == ["tg-file://photo-1"]
    assert envelope.segments[-1] == ImageSegment(url="tg-file://photo-1")
    assert not isinstance(prepared.content, str)
    image_parts = [part for part in prepared.content if isinstance(part, ImageContentPart)]
    assert image_parts == [
        ImageContentPart(
            image_url=ImageUrl(url="data:image/jpeg;base64,dGVsZWdyYW0taW1hZ2U=")
        )
    ]


@pytest.mark.asyncio
async def test_unresolved_telegram_image_degrades_without_leaking_file_id() -> None:
    envelope = image_envelope().model_copy(
        update={
            "platform": Platform.TELEGRAM,
            "segments": (ImageSegment(url="tg-file://sensitive-file-id"),),
        }
    )

    prepared = await VisionService(llm=None, mode=VisionMode.DIRECT).prepare(envelope)

    assert isinstance(prepared.content, str)
    assert "tg-file://" not in prepared.content
    assert "sensitive-file-id" not in prepared.content


@pytest.mark.asyncio
async def test_vision_failure_degrades_to_chinese_reference_prompt() -> None:
    llm = FakeVisionLlm(error=LlmError("secret upstream body", retryable=True))
    service = VisionService(llm=llm, mode=VisionMode.DESCRIBE)

    prepared = await service.prepare(image_envelope())

    assert "图片暂时无法解析" in prepared.summary
    assert "https://img.example/cat.png" in str(prepared.content)
