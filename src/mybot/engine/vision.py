"""Prepare inbound image segments for direct or descriptor-based vision prompts."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import structlog

from mybot.contracts import ImageSegment, MessageEnvelope
from mybot.engine.prompt import EnvelopePrompt, envelope_prompt
from mybot.infrastructure.llm import ChatMessage, LlmError, LlmReply

logger = structlog.get_logger("mybot.vision")

_VISION_SYSTEM_PROMPT = (
    "请准确描述用户图片中与问题相关的可见内容。不要猜测身份、隐私属性或图片外信息。"
    "只输出简洁中文描述, 不要输出链接。"
)


class VisionMode(StrEnum):
    OFF = "off"
    DESCRIBE = "describe"
    DIRECT = "direct"


class VisionLlm(Protocol):
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, object]] | None = None,
    ) -> LlmReply: ...


@dataclass(slots=True)
class VisionService:
    llm: VisionLlm | None
    mode: VisionMode = VisionMode.DESCRIBE
    max_description_chars: int = 2_000

    async def prepare(self, envelope: MessageEnvelope) -> EnvelopePrompt:
        has_images = any(isinstance(segment, ImageSegment) for segment in envelope.segments)
        if not has_images or self.mode is VisionMode.OFF:
            return envelope_prompt(envelope, include_images=False)

        multimodal = envelope_prompt(envelope, include_images=True)
        if self.mode is VisionMode.DIRECT:
            return multimodal
        if self.llm is None:
            return self._fallback(envelope)
        try:
            reply = await self.llm.complete(
                [
                    ChatMessage(role="system", content=_VISION_SYSTEM_PROMPT),
                    ChatMessage(role="user", content=multimodal.content),
                ]
            )
        except asyncio.CancelledError:
            raise
        except LlmError as error:
            logger.warning(
                "vision_prepare_failed",
                retryable=error.retryable,
                error_code=type(error).__name__,
            )
            return self._fallback(envelope)
        description = reply.text.strip()[: self.max_description_chars]
        if not description:
            return self._fallback(envelope)
        summary = f"{multimodal.summary}\n图片理解: {description}"
        return EnvelopePrompt(summary=summary, content=summary)

    @staticmethod
    def _fallback(envelope: MessageEnvelope) -> EnvelopePrompt:
        prepared = envelope_prompt(envelope, include_images=False)
        summary = f"{prepared.summary}\n[图片暂时无法解析, 请结合图片引用谨慎回答]"
        content = f"{prepared.content}\n[图片暂时无法解析, 请结合图片引用谨慎回答]"
        return EnvelopePrompt(summary=summary, content=content)
