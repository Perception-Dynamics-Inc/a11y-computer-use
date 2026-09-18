"""OCR refs: text on screen becomes targetable refs when the accessibility tree
gives the agent nothing to hold on to.

Some apps expose no accessibility tree at all (Telegram on macOS), or expose a
shell around a custom-drawn canvas (After Effects panels, Krita, most games).
The pixel path is the classic fallback: screenshot, let the model guess an
(x, y) pair, click. This module keeps the model out of the coordinate business
on that path too. The screen is captured, on-device OCR reads every piece of
text with its bounding box, and each line becomes a ref ``o1..oN`` with a rect
in display-qualified physical pixels, exactly like the ``e`` refs of a
snapshot. ``click(ref="o7")`` lands on the centre of that text.

Two layers:

- `OcrEngine`: turns PNG bytes into `TextBox` rows in image pixels. `VisionOcr`
  is the macOS engine (Apple's Vision framework through pyobjc, accurate
  recognition level, language correction on). `FakeOcr` serves tests and any
  caller that wants to plug in another engine. Nothing here imports pyobjc at
  module load, so the module is importable on every OS.
- `ScreenText`: one OCR epoch, the analogue of a `Snapshot`. `build_screen_text`
  groups boxes into lines, maps image pixels onto the display's physical pixel
  space (a capture may be at the backing scale or not), filters by confidence,
  and assigns refs in reading order. `rematch_line` re-resolves a ref against a
  fresh epoch by text and proximity, the way `observe.rematch_ref` does for
  accessibility elements, so a stale ``o`` ref reports candidates instead of
  clicking the wrong place.
"""

from __future__ import annotations

import difflib
import io
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from a11y_computer_use.schema import Bounds, Display, Point

#: Default confidence floor for a recognised line (engines report 0..1).
DEFAULT_MIN_CONFIDENCE = 0.5

#: A stale ``o`` ref re-resolves to the same text only within this radius
#: (physical pixels) of its old centre; beyond it the match is reported as a
#: candidate, not taken. Same order of magnitude as the accessibility anchor.
REMATCH_RADIUS_PX = 400

#: Longest text kept per line in the render.
_RENDER_TEXT_CHARS = 80


# ---------------------------------------------------------------------------
# Engine layer: PNG bytes -> text boxes in image pixels
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextBox:
    """One recognised run of text in image pixel space (origin top-left)."""

    text: str
    x: int
    y: int
    w: int
    h: int
    confidence: float = 1.0


@runtime_checkable
class OcrEngine(Protocol):
    """Recognise text in a PNG. Boxes are in the image's own pixel space."""

    name: str

    def recognize(self, png: bytes) -> list[TextBox]: ...


class FakeOcr:
    """A scripted engine for tests: returns the given boxes, or the result of a
    callable that receives the PNG bytes (so a test can vary the answer per
    call, e.g. to simulate a moved or vanished line)."""

    name = "fake"

    def __init__(self, boxes: Sequence[TextBox] | Callable[[bytes], Sequence[TextBox]] = ()) -> None:
        self._boxes = boxes
        self.calls = 0

    def recognize(self, png: bytes) -> list[TextBox]:
        self.calls += 1
        boxes = self._boxes(png) if callable(self._boxes) else self._boxes
        return list(boxes)


class VisionOcr:
    """On-device OCR through Apple's Vision framework (macOS only).

    Uses ``VNRecognizeTextRequest`` at the accurate recognition level with
    language correction on. Vision returns one observation per text line with a
    normalised bounding box (origin bottom-left); the boxes come back here in
    image pixels with a top-left origin. No network, no model download, and no
    TCC grant beyond whatever produced the PNG (Vision reads image bytes, so it
    works on a rendered image even when Screen Recording is not granted).
    """

    name = "vision"

    def __init__(self, *, languages: Sequence[str] | None = None) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("VisionOcr needs macOS (the Vision framework)")
        self._languages = tuple(languages or ())

    def recognize(self, png: bytes) -> list[TextBox]:
        import Quartz  # pyobjc, macOS only; loaded at call time
        import Vision
        from Foundation import NSData

        data = NSData.dataWithBytes_length_(png, len(png))
        source = Quartz.CGImageSourceCreateWithData(data, None)
        if source is None:
            raise ValueError("not a decodable image")
        image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
        if image is None:
            raise ValueError("not a decodable image")
        width = Quartz.CGImageGetWidth(image)
        height = Quartz.CGImageGetHeight(image)

        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(True)
        if self._languages:
            request.setRecognitionLanguages_(list(self._languages))
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, {})
        ok, error = handler.performRequests_error_([request], None)
        if not ok:
            raise RuntimeError(f"Vision text recognition failed: {error}")
        boxes: list[TextBox] = []
        for observation in request.results() or ():
            candidates = observation.topCandidates_(1)
            if not candidates:
                continue
            best = candidates[0]
            text = str(best.string()).strip()
            if not text:
                continue
            bb = observation.boundingBox()  # normalised, origin bottom-left
            x = bb.origin.x * width
            y = (1.0 - bb.origin.y - bb.size.height) * height
            boxes.append(
                TextBox(
                    text=text,
                    x=int(round(x)),
                    y=int(round(y)),
                    w=max(1, int(round(bb.size.width * width))),
                    h=max(1, int(round(bb.size.height * height))),
                    confidence=float(best.confidence()),
                )
            )
        return boxes


def default_engine() -> OcrEngine | None:
    """The platform's OCR engine, or None where none is available (the caller
    reports `unsupported`). macOS: Vision, when the pyobjc framework imports."""
    if sys.platform != "darwin":
        return None
    try:
        import Vision  # noqa: F401
    except ImportError:
        return None
    return VisionOcr()


# ---------------------------------------------------------------------------
# Epoch layer: text lines with refs in display space
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextLine:
    """One line of on-screen text with its ref and display-space rect."""

    ref: str
    text: str
    bounds: Bounds
    confidence: float

    @property
    def center(self) -> Point:
        return self.bounds.center


@dataclass(frozen=True, slots=True)
class ScreenText:
    """One OCR epoch: what was readable on ``display`` at ``created_at``.

    Refs ``o1..oN`` are valid only against this epoch (the same rule as
    snapshot refs); re-resolution against a fresh epoch goes through
    `rematch_line`.
    """

    text_id: str
    display: Display
    created_at: float
    lines: tuple[TextLine, ...]
    engine: str = ""

    def line(self, ref: str) -> TextLine:
        for line in self.lines:
            if line.ref == ref:
                return line
        raise KeyError(f"no text line {ref!r} in {self.text_id!r}")


def is_ocr_ref(ref: str | None) -> bool:
    """True for ``o<digits>`` refs (OCR lines), False for ``e`` refs and None."""
    return bool(ref) and ref[0] == "o" and ref[1:].isdigit()


def group_lines(boxes: Sequence[TextBox], *, gap_ratio: float = 0.8) -> list[list[TextBox]]:
    """Group boxes into reading-order lines.

    Two boxes share a line when their vertical centres lie within half the
    smaller height of each other and the horizontal gap between them is under
    ``gap_ratio`` times that height. Line-level engines (Vision) mostly return
    one box per line already; word-level engines and tests rely on this.
    Lines come back top-to-bottom, boxes within a line left-to-right.
    """
    ordered = sorted(boxes, key=lambda b: (b.y + b.h / 2, b.x))
    lines: list[list[TextBox]] = []
    for box in ordered:
        placed = False
        for line in lines:
            last = line[-1]
            h = max(1, min(last.h, box.h))
            same_row = abs((last.y + last.h / 2) - (box.y + box.h / 2)) <= h / 2
            gap = box.x - (last.x + last.w)
            if same_row and -h / 2 <= gap <= gap_ratio * h:
                line.append(box)
                placed = True
                break
        if not placed:
            lines.append([box])
    for line in lines:
        line.sort(key=lambda b: b.x)
    lines.sort(key=lambda line: (min(b.y for b in line) + max(b.y + b.h for b in line)) / 2)
    return lines


def build_screen_text(
    boxes: Sequence[TextBox],
    *,
    display: Display,
    image_width: int,
    image_height: int,
    text_id: str,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    offset: tuple[int, int] = (0, 0),
    engine: str = "",
    created_at: float | None = None,
) -> ScreenText:
    """Turn engine boxes into a `ScreenText` epoch in display-space pixels.

    ``image_width``/``image_height`` are the recognised image's dimensions;
    boxes are scaled onto ``display.width``/``display.height`` so a capture at
    the backing scale and a capture at nominal resolution both yield the same
    display-qualified rects (the contract every `Point`/`Bounds` follows).
    ``offset`` shifts the result when the image was a crop (``region``), in
    display pixels. Boxes under ``min_confidence`` are dropped before grouping.
    """
    sx = display.width / max(1, image_width)
    sy = display.height / max(1, image_height)
    kept = [b for b in boxes if b.confidence >= min_confidence and b.text.strip()]
    lines: list[TextLine] = []
    for index, group in enumerate(group_lines(kept), start=1):
        left = min(b.x for b in group)
        top = min(b.y for b in group)
        right = max(b.x + b.w for b in group)
        bottom = max(b.y + b.h for b in group)
        text = " ".join(b.text.strip() for b in group)
        confidence = min(b.confidence for b in group)
        lines.append(
            TextLine(
                ref=f"o{index}",
                text=text,
                bounds=Bounds(
                    display_id=display.display_id,
                    x=int(round(left * sx)) + offset[0],
                    y=int(round(top * sy)) + offset[1],
                    width=max(1, int(round((right - left) * sx))),
                    height=max(1, int(round((bottom - top) * sy))),
                ),
                confidence=confidence,
            )
        )
    return ScreenText(
        text_id=text_id,
        display=display,
        created_at=time.time() if created_at is None else created_at,
        lines=tuple(lines),
        engine=engine,
    )


def render_screen_text(screen: ScreenText, lines: Sequence[TextLine] | None = None) -> str:
    """Compact text render, one line per OCR line, mirroring the snapshot style:
    ``o3 "Saved Messages" [220x18 @1:64,112] (0.97)``."""
    rows = screen.lines if lines is None else tuple(lines)
    header = (
        f"[{screen.text_id}] display {screen.display.display_id}: {len(rows)} text line(s) "
        f"via OCR; refs o1..oN target the text centre (click ref=\"o7\")"
    )
    if not rows:
        return header + "\n  (no text recognised)"
    out = [header]
    for line in rows:
        b = line.bounds
        text = line.text if len(line.text) <= _RENDER_TEXT_CHARS else line.text[: _RENDER_TEXT_CHARS - 1] + "…"
        out.append(f'  {line.ref} "{text}" [{b.width}x{b.height} @{b.display_id}:{b.x},{b.y}] ({line.confidence:.2f})')
    return "\n".join(out)


def find_lines(screen: ScreenText, text: str) -> tuple[TextLine, ...]:
    """Lines whose text contains ``text`` (case-insensitive substring)."""
    needle = _norm(text)
    return tuple(line for line in screen.lines if needle in _norm(line.text))


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _distance(a: Point, b: Point) -> float:
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5


def rematch_line(old: TextLine, live: ScreenText) -> tuple[TextLine | None, tuple[TextLine, ...]]:
    """Re-resolve ``old`` against a fresh epoch.

    Match order: the same text (whitespace and case folded) nearest to the old
    centre within `REMATCH_RADIUS_PX`; then a line containing the old text or
    contained by it, same radius. Returns ``(match, candidates)``; when nothing
    matches, ``candidates`` are up to three of the most similar lines by text
    ratio (nearest first on ties) so the caller can report them.
    """
    target = _norm(old.text)
    centre = old.center
    same = [line for line in live.lines if _norm(line.text) == target]
    same.sort(key=lambda line: _distance(line.center, centre))
    if same and _distance(same[0].center, centre) <= REMATCH_RADIUS_PX:
        return same[0], ()
    partial = [
        line for line in live.lines
        if line not in same and (target in _norm(line.text) or _norm(line.text) in target)
        and len(_norm(line.text)) >= 3
    ]
    partial.sort(key=lambda line: _distance(line.center, centre))
    if partial and _distance(partial[0].center, centre) <= REMATCH_RADIUS_PX:
        return partial[0], ()
    scored = sorted(
        live.lines,
        key=lambda line: (
            -difflib.SequenceMatcher(None, target, _norm(line.text)).ratio(),
            _distance(line.center, centre),
        ),
    )
    return None, tuple(scored[:3])


def crop_png(png: bytes, region: Bounds, display: Display) -> tuple[bytes, int, int, tuple[int, int]]:
    """Crop ``png`` (a capture of ``display``) to ``region`` (display pixels).

    Returns the cropped PNG, its pixel size, and the display-space offset of
    its top-left corner, for `build_screen_text`'s ``offset``. The capture may
    not be at display resolution, so the crop box is rescaled onto the image.
    """
    from PIL import Image

    image = Image.open(io.BytesIO(png))
    rx = image.width / max(1, display.width)
    ry = image.height / max(1, display.height)
    left = max(0, region.x)
    top = max(0, region.y)
    right = min(display.width, region.x + region.width)
    bottom = min(display.height, region.y + region.height)
    if right <= left or bottom <= top:
        raise ValueError(f"region {region} lies outside display {display.display_id}")
    crop = image.crop((round(left * rx), round(top * ry), round(right * rx), round(bottom * ry)))
    buffer = io.BytesIO()
    crop.save(buffer, format="PNG")
    return buffer.getvalue(), crop.width, crop.height, (left, top)


__all__ = [
    "DEFAULT_MIN_CONFIDENCE",
    "REMATCH_RADIUS_PX",
    "FakeOcr",
    "OcrEngine",
    "ScreenText",
    "TextBox",
    "TextLine",
    "VisionOcr",
    "build_screen_text",
    "crop_png",
    "default_engine",
    "find_lines",
    "group_lines",
    "is_ocr_ref",
    "rematch_line",
    "render_screen_text",
]
