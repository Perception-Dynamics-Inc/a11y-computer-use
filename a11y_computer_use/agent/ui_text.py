"""Fence text that came from the screen before the agent server returns it.

``a11y_computer_use.untrusted.fence`` is the only fence implementation.
UI and page text is never treated as a fence this process already issued.
Closers are escaped with ``escape_untrusted`` first, then ``fence`` wraps
the string, so a page-supplied ``<untrusted nonce=...>`` block cannot be
returned unchanged or end the fence early.
"""

from __future__ import annotations

from a11y_computer_use.untrusted import escape_untrusted, fence


def fence_ui(text: str) -> str:
    """Wrap UI or page text. A forged fence is escaped and wrapped again."""
    return fence(escape_untrusted(str(text))).text


__all__ = ["fence_ui"]
