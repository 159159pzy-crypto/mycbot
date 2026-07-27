"""Stable public contracts shared by adapters, workers, tools, and plugins."""

from mybot.contracts.conversation import (
    ConversationKey,
    TurnAction,
    TurnDecision,
    TurnTrigger,
)
from mybot.contracts.memory import (
    CoreBlock,
    CoreBlockLabel,
    MemoryItem,
    MemoryMergeDecision,
    MemoryOperation,
    MemoryPrivacy,
    MemoryScope,
)
from mybot.contracts.messages import (
    AtSegment,
    ChatKind,
    FileSegment,
    ImageSegment,
    MessageEnvelope,
    Platform,
    PlatformCapabilities,
    ReferenceSegment,
    StickerSegment,
    TextSegment,
    VoiceSegment,
)
from mybot.contracts.plugins import PluginManifest
from mybot.contracts.replies import Citation, ReplyPlan, TypingProfile
from mybot.contracts.tools import ToolContext, ToolError, ToolResult, ToolRisk, ToolSpec

__all__ = [
    "AtSegment",
    "ChatKind",
    "Citation",
    "ConversationKey",
    "CoreBlock",
    "CoreBlockLabel",
    "FileSegment",
    "ImageSegment",
    "MemoryItem",
    "MemoryMergeDecision",
    "MemoryOperation",
    "MemoryPrivacy",
    "MemoryScope",
    "MessageEnvelope",
    "Platform",
    "PlatformCapabilities",
    "PluginManifest",
    "ReferenceSegment",
    "ReplyPlan",
    "StickerSegment",
    "TextSegment",
    "ToolContext",
    "ToolError",
    "ToolResult",
    "ToolRisk",
    "ToolSpec",
    "TurnAction",
    "TurnDecision",
    "TurnTrigger",
    "TypingProfile",
    "VoiceSegment",
]
