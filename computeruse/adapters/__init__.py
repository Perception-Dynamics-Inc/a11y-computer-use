"""Provider executor adapters: run Anthropic or OpenAI computer-use actions
through the gated computerUse Runtime, with coordinate clicks snapped to
accessibility refs when one is under the point. See ``docs/provider-adapters.md``.
"""

from computeruse.adapters.anthropic_computer import AnthropicComputerAdapter
from computeruse.adapters.base import (
    ComputerAdapter,
    Result,
    openai_keys_to_chords,
    xdotool_to_chord,
)
from computeruse.adapters.openai_computer import OpenAIComputerAdapter

__all__ = [
    "AnthropicComputerAdapter",
    "ComputerAdapter",
    "OpenAIComputerAdapter",
    "Result",
    "openai_keys_to_chords",
    "xdotool_to_chord",
]
