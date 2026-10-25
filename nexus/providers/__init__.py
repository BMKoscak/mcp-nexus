"""LLM provider adapters (direct async HTTP clients, no vendor SDKs)."""

from nexus.providers.anthropic import AnthropicProvider
from nexus.providers.base import (
    Done,
    Message,
    ModelInfo,
    Provider,
    ProviderError,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolDef,
)
from nexus.providers.ollama import OllamaProvider
from nexus.providers.openai import OpenAIProvider

PROVIDER_CLASSES: dict[str, type[Provider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "ollama": OllamaProvider,
}

__all__ = [
    "PROVIDER_CLASSES",
    "AnthropicProvider",
    "Done",
    "Message",
    "ModelInfo",
    "OllamaProvider",
    "OpenAIProvider",
    "Provider",
    "ProviderError",
    "StreamEvent",
    "TextDelta",
    "ToolCall",
    "ToolCallEvent",
    "ToolDef",
]
