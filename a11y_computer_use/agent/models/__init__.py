"""Computer-use agent models.

``make_model("openai:gpt-4.1")`` and the `Model` protocol live here. HTTP
providers (OpenAI-compatible, Anthropic, Gemini) need the ``agent`` extra
(``httpx``). `ScriptedModel` and `CommandModel` use only the standard library.
"""

from __future__ import annotations

from a11y_computer_use.agent.models.base import (
    Message,
    Model,
    ModelError,
    ModelTurn,
    ToolCall,
    assistant_message,
    make_model,
    message_to_dict,
    model_turn_from_dict,
)
from a11y_computer_use.agent.models.command import CommandModel
from a11y_computer_use.agent.models.scripted import ScriptedModel

__all__ = [
    "AnthropicModel",
    "CommandModel",
    "GeminiModel",
    "Message",
    "Model",
    "ModelError",
    "ModelTurn",
    "OpenAICompatibleModel",
    "ScriptedModel",
    "ToolCall",
    "assistant_message",
    "make_model",
    "message_to_dict",
    "model_turn_from_dict",
]


def __getattr__(name: str):
    if name == "OpenAICompatibleModel":
        from a11y_computer_use.agent.models.openai_compat import OpenAICompatibleModel

        return OpenAICompatibleModel
    if name == "AnthropicModel":
        from a11y_computer_use.agent.models.anthropic import AnthropicModel

        return AnthropicModel
    if name == "GeminiModel":
        from a11y_computer_use.agent.models.gemini import GeminiModel

        return GeminiModel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
