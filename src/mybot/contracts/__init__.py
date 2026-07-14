"""Stable public contracts shared by adapters, workers, tools, and plugins."""

from mybot.contracts.conversation import (
    ConversationKey,
    TurnAction,
    TurnDecision,
    TurnTrigger,
)
from mybot.contracts.memory import MemoryItem, MemoryPrivacy, MemoryScope
from mybot.contracts.messages import (
    ChatKind,
    FileSegment,
    ImageSegment,
    MessageEnvelope,
    Platform,
    PlatformCapabilities,
    ReferenceSegment,
    TextSegment,
)
from mybot.contracts.plugins import PluginManifest
from mybot.contracts.replies import Citation, ReplyPlan, TypingProfile
from mybot.contracts.tools import ToolContext, ToolError, ToolResult, ToolRisk, ToolSpec

__all__ = [
    "ChatKind",
    "Citation",
    "ConversationKey",
    "FileSegment",
    "ImageSegment",
    "MemoryItem",
    "MemoryPrivacy",
    "MemoryScope",
    "MessageEnvelope",
    "Platform",
    "PlatformCapabilities",
    "PluginManifest",
    "ReferenceSegment",
    "ReplyPlan",
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
]
