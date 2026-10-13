"""Fence text that came from the screen before the agent server returns it.

``a11y_computer_use.untrusted.fence`` is the only fence implementation.
A fence this process already issued is returned as that fence, so a tool
result the agent wrapped is not wrapped again. ``fence`` escapes openers
and closers and wraps a page-supplied ``<untrusted nonce=...>`` block
again, so that block cannot be returned unchanged or end the fence early.
"""

from __future__ import annotations

from a11y_computer_use.untrusted import fence


def fence_ui(text: str) -> str:
    """Fence UI or page text once. An issued fence is returned as that fence."""
    return fence(str(text)).text


__all__ = ["fence_ui"]
