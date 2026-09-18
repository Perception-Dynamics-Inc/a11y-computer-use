"""Set-of-Mark overlay — composite element refs onto a screenshot.

The bridge between the a11y and vision paths: we already know every interactive
element's ref and on-screen bounds, so we draw the ref number over each one on
the screenshot. A vision model then names a ref ("click e7") instead of guessing
pixel coordinates — pixel-accurate grounding for free, and the same e-indices as
the text snapshot, so the two observations are interchangeable.

Pure Pillow — no platform dependencies — so every backend (and the tests) can use
it without pyobjc/gi.
"""

from __future__ import annotations

import io
from collections.abc import Sequence

_OUTLINE = (255, 40, 40)
_LABEL_BG = (255, 40, 40)
_LABEL_FG = (255, 255, 255)


def draw_marks(png: bytes, marks: Sequence[tuple[str, int, int, int, int]]) -> bytes:
    """Return a new PNG with each mark drawn on ``png``.

    Each mark is ``(label, x, y, w, h)`` in the image's own pixel space: a
    rectangle around the element and its label (the element ref) in a small chip
    at the top-left corner. Marks whose rectangle is fully outside the image are
    skipped. Empty ``marks`` returns the original bytes unchanged.
    """
    if not marks:
        return png
    from PIL import Image, ImageDraw

    img = Image.open(io.BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(img)
    iw, ih = img.size
    for label, x, y, w, h in marks:
        if x + w < 0 or y + h < 0 or x > iw or y > ih:
            continue  # fully offscreen
        draw.rectangle([x, y, x + w, y + h], outline=_OUTLINE, width=2)
        chip_w = 6 * len(label) + 4
        cy = max(0, y)
        draw.rectangle([x, cy, x + chip_w, cy + 12], fill=_LABEL_BG)
        draw.text((x + 2, cy + 1), label, fill=_LABEL_FG)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def marks_for(snap, scaled, display_id: int) -> list[tuple[str, int, int, int, int]]:
    """Map a snapshot's interactive elements on one display to marks in a
    downscaled screenshot's pixel space (label = ref). Only clickable/editable
    elements on ``display_id`` are marked — the ones the model can act on."""
    marks: list[tuple[str, int, int, int, int]] = []
    for el in snap.elements:
        if not (el.clickable or el.editable):
            continue
        b = el.bounds
        if b.display_id != display_id:
            continue
        x, y = scaled.from_source(b.x, b.y)
        marks.append((el.ref, x, y, max(1, round(b.width * scaled.scale)),
                      max(1, round(b.height * scaled.scale))))
    return marks
