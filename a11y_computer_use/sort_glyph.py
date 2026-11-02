"""Column-header sort indicators are header state, not part of its name (#1451)."""

from __future__ import annotations

_SORT_GLYPHS = "\u25b2\u25bc\u25b4\u25be\u25b3\u25bd\u25b5\u25bf\u2191\u2193\u21c5\u2195\u02c4\u02c5\u2bc5\u2bc6"


def strip_sort_glyph(name: str) -> str:
    """Drop a trailing sort indicator (\u25b2, \u25bc, \u2191 ...) from a header name (#1451).

    Only trailing glyphs and the whitespace before them go; any other text
    change is still a rename.
    """
    text = name or ""
    stripped = text.rstrip().rstrip(_SORT_GLYPHS + " \u00a0")
    return stripped if stripped else text
