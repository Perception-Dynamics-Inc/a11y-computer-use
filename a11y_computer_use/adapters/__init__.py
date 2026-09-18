"""Provider executor adapters: run Anthropic or OpenAI computer-use actions
through the gated a11y-computer-use Runtime, with coordinate clicks snapped to
accessibility refs when one is under the point. See ``docs/provider-adapters.md``.
"""

from a11y_computer_use.adapters.anthropic_computer import AnthropicComputerAdapter
from a11y_computer_use.adapters.base import (
    ComputerAdapter,
    Result,
    openai_keys_to_chords,
    xdotool_to_chord,
)
from a11y_computer_use.adapters.openai_computer import OpenAIComputerAdapter

__all__ = [
    "AnthropicComputerAdapter",
    "ComputerAdapter",
    "OpenAIComputerAdapter",
    "Result",
    "openai_keys_to_chords",
    "xdotool_to_chord",
]
