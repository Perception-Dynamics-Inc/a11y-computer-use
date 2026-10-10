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

The pieces:

- `OcrEngine`: turns PNG bytes into `TextBox` rows in image pixels. `VisionOcr`
  is the macOS engine (Apple's Vision framework through pyobjc, accurate
  recognition level, language correction on). `RapidOcr` is PP-OCRv5 mobile
  ONNX on CPU, from the optional ``[ocr]`` extra (the package downloads its
  own models; this repo does not vendor weights). `TesseractOcr` shells out
  to the system ``tesseract`` binary when RapidOCR is not installed.
  `FakeOcr` serves tests. Nothing here imports an engine at module load, so
  the module imports on every OS with the base install.
- `ocr`: the call a vision fallback uses. It accepts a window screenshot, a
  ``crop`` result, or ``(image, bounds)`` for an element crop or an opaque
  region, and returns ``{text, bounds, confidence}`` in screen pixels. ``text``
  has been through `fence_untrusted`. A missing engine raises
  `ComputerUseError` ``unsupported`` with ``detail["reason"] ==
  "missing_dependency"``.
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
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from a11y_computer_use.schema import Bounds, ComputerUseError, Display, ErrorCode, Point
from a11y_computer_use.untrusted import fence_untrusted

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
    """The engine `screen_text` uses when the caller does not pass one.

    macOS: Vision, when the pyobjc framework imports. Other platforms: None,
    so `screen_text` stays ``unsupported`` until a caller passes an engine.
    The vision-fallback helper `ocr` chooses separately: RapidOCR, then the
    ``tesseract`` binary, then Vision.
    """
    if sys.platform != "darwin":
        return None
    try:
        import Vision  # noqa: F401
    except ImportError:
        return None
    return VisionOcr()


def _rapidocr_importable() -> bool:
    try:
        import rapidocr  # noqa: F401
    except ImportError:
        return False
    return True


def _tesseract_binary() -> str | None:
    return shutil.which("tesseract")


def _vision_importable() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import Vision  # noqa: F401
    except ImportError:
        return False
    return True


def _missing_engine(name: str, hint: str) -> ComputerUseError:
    """Typed failure when an OCR engine cannot be loaded. Not a crash."""
    return ComputerUseError(
        ErrorCode.UNSUPPORTED,
        f"OCR engine {name!r} is not available",
        detail={"reason": "missing_dependency", "engine": name, "hint": hint},
    )


_RAPID_HINT = "pip install 'a11y-computer-use[ocr]'"
_TESS_HINT = "install the tesseract binary (Debian/Ubuntu: apt install tesseract-ocr)"
_VISION_HINT = "pip install pyobjc-framework-Vision"


def choose_engine_name(name: str | None = None) -> str:
    """Resolve ``name`` to ``rapidocr``, ``tesseract``, or ``vision``.

    None prefers RapidOCR, then the ``tesseract`` binary, then Vision on
    macOS. An explicit name does not fall through to another engine. A name
    that cannot be loaded raises `ComputerUseError` with
    ``detail["reason"] == "missing_dependency"`` (or ``unsupported_platform``
    for Vision off macOS).
    """
    if name is None:
        if _rapidocr_importable():
            return "rapidocr"
        if _tesseract_binary():
            return "tesseract"
        if _vision_importable():
            return "vision"
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            "no OCR engine is available",
            detail={
                "reason": "missing_dependency",
                "engine": "auto",
                "tried": ["rapidocr", "tesseract", "vision"],
                "hint": f"{_RAPID_HINT}; or {_TESS_HINT}",
            },
        )
    if not isinstance(name, str):
        raise ValueError(f"engine must be rapidocr, tesseract, vision, or None, got {name!r}")
    chosen = name.strip().lower()
    if chosen == "rapidocr":
        if not _rapidocr_importable():
            raise _missing_engine("rapidocr", _RAPID_HINT)
        return chosen
    if chosen == "tesseract":
        if not _tesseract_binary():
            raise _missing_engine("tesseract", _TESS_HINT)
        return chosen
    if chosen == "vision":
        if sys.platform != "darwin":
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                "Vision OCR is macOS-only",
                detail={"reason": "unsupported_platform", "engine": "vision"},
            )
        if not _vision_importable():
            raise _missing_engine("vision", _VISION_HINT)
        return chosen
    raise ValueError(f"engine must be rapidocr, tesseract, or vision, got {name!r}")


class RapidOcr:
    """PP-OCRv5 mobile recognition through RapidOCR's ONNX Runtime backend.

    The ``[ocr]`` extra installs ``rapidocr`` and CPU ``onnxruntime``. RapidOCR
    downloads the mobile detection and recognition models into its own package
    directory on first use. This class does not select a GPU execution
    provider. The instance loads those models on the first `recognize`.
    """

    name = "rapidocr"

    def __init__(self) -> None:
        self._engine = None

    def recognize(self, png: bytes) -> list[TextBox]:
        engine = self._load()
        import numpy as np
        from PIL import Image

        try:
            image = Image.open(io.BytesIO(png)).convert("RGB")
        except Exception as exc:  # noqa: BLE001 - PIL's decoder errors are not one class
            raise ValueError("not a decodable image") from exc
        try:
            result = engine(np.asarray(image))
        except ImportError as exc:
            raise _missing_engine("rapidocr", _RAPID_HINT) from exc
        return _boxes_from_rapid(result)

    def _load(self):
        if self._engine is not None:
            return self._engine
        try:
            from rapidocr import EngineType, LangDet, LangRec, ModelType, OCRVersion, RapidOCR
        except ImportError as exc:
            raise _missing_engine("rapidocr", _RAPID_HINT) from exc
        # Chinese PP-OCRv5 mobile also reads Latin. Detection and recognition
        # stay on ONNX Runtime's default CPU provider; no CUDA provider is set.
        self._engine = RapidOCR(
            params={
                "Det.engine_type": EngineType.ONNXRUNTIME,
                "Det.lang_type": LangDet.CH,
                "Det.model_type": ModelType.MOBILE,
                "Det.ocr_version": OCRVersion.PPOCRV5,
                "Rec.engine_type": EngineType.ONNXRUNTIME,
                "Rec.lang_type": LangRec.CH,
                "Rec.model_type": ModelType.MOBILE,
                "Rec.ocr_version": OCRVersion.PPOCRV5,
                "Cls.engine_type": EngineType.ONNXRUNTIME,
                "Cls.model_type": ModelType.MOBILE,
            }
        )
        return self._engine


def _boxes_from_rapid(result: object) -> list[TextBox]:
    boxes = getattr(result, "boxes", None)
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if boxes is None or texts is None:
        return []
    score_list = list(scores) if scores is not None else []
    found: list[TextBox] = []
    for index, (quad, text) in enumerate(zip(boxes, texts)):
        raw = str(text).strip()
        if not raw:
            continue
        xs = [float(point[0]) for point in quad]
        ys = [float(point[1]) for point in quad]
        left, top = min(xs), min(ys)
        score = float(score_list[index]) if index < len(score_list) else 1.0
        if score > 1.0:
            score /= 100.0
        found.append(
            TextBox(
                text=raw,
                x=int(round(left)),
                y=int(round(top)),
                w=max(1, int(round(max(xs) - left))),
                h=max(1, int(round(max(ys) - top))),
                confidence=score,
            )
        )
    return found


class TesseractOcr:
    """Word boxes from the system ``tesseract`` binary (TSV on stdout).

    No Python OCR package. A missing binary is `missing_dependency`, raised
    by `choose_engine_name` before this runs and again if the binary
    disappears. The process is not a shell.
    """

    name = "tesseract"

    def __init__(self, *, timeout_s: float = 30.0) -> None:
        self._timeout_s = timeout_s

    def recognize(self, png: bytes) -> list[TextBox]:
        binary = _tesseract_binary()
        if not binary:
            raise _missing_engine("tesseract", _TESS_HINT)
        with tempfile.TemporaryDirectory(prefix="a11y-ocr-") as directory:
            image = Path(directory) / "image.png"
            image.write_bytes(png)
            try:
                completed = subprocess.run(
                    [binary, str(image), "stdout", "-l", "eng", "--psm", "6", "tsv"],
                    check=False,
                    capture_output=True,
                    timeout=self._timeout_s,
                )
            except FileNotFoundError as exc:
                raise _missing_engine("tesseract", _TESS_HINT) from exc
            except subprocess.TimeoutExpired as exc:
                raise ComputerUseError(
                    ErrorCode.TIMEOUT,
                    f"tesseract did not finish within {self._timeout_s:g}s",
                    detail={"reason": "ocr_failed", "engine": "tesseract"},
                ) from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                "tesseract failed",
                detail={"reason": "ocr_failed", "engine": "tesseract", "error": detail[:500]},
            )
        return _boxes_from_tesseract_tsv(completed.stdout.decode("utf-8", errors="replace"))


def _boxes_from_tesseract_tsv(text: str) -> list[TextBox]:
    """Word rows (TSV level 5). Line rows from Tesseract 5 often have no text."""
    rows = text.splitlines()
    if not rows:
        return []
    header = rows[0].split("\t")
    try:
        level_i = header.index("level")
        left_i = header.index("left")
        top_i = header.index("top")
        width_i = header.index("width")
        height_i = header.index("height")
        conf_i = header.index("conf")
        text_i = header.index("text")
    except ValueError:
        return []
    found: list[TextBox] = []
    for line in rows[1:]:
        cols = line.split("\t")
        if len(cols) <= text_i or cols[level_i] != "5":
            continue
        raw = cols[text_i].strip()
        if not raw:
            continue
        try:
            confidence = float(cols[conf_i])
        except ValueError:
            continue
        if confidence < 0:
            continue
        if confidence > 1.0:
            confidence /= 100.0
        found.append(
            TextBox(
                text=raw,
                x=int(cols[left_i]),
                y=int(cols[top_i]),
                w=max(1, int(cols[width_i])),
                h=max(1, int(cols[height_i])),
                confidence=confidence,
            )
        )
    return found


_ENGINES: dict[str, OcrEngine] = {}


def select_engine(name: str | None = None) -> OcrEngine:
    """The engine `choose_engine_name` selected, created once per process."""
    chosen = choose_engine_name(name)
    cached = _ENGINES.get(chosen)
    if cached is not None:
        return cached
    if chosen == "rapidocr":
        engine: OcrEngine = RapidOcr()
    elif chosen == "tesseract":
        engine = TesseractOcr()
    else:
        engine = VisionOcr()
    _ENGINES[chosen] = engine
    return engine


#: OCR lines are fenced in full up to this many characters. The helper trims
#: past it and still escapes fence markers.
_OCR_TEXT_LIMIT = 240

_CROP_BOUNDS = re.compile(
    r"on display (?P<display_id>-?\d+) at \((?P<x>-?\d+), (?P<y>-?\d+)\) "
    r"(?P<width>\d+)x(?P<height>\d+)"
)


def ocr(
    image_or_region: object,
    *,
    engine: str | OcrEngine | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> list[dict[str, object]]:
    """Read text from a screenshot, a crop, or an ``(image, bounds)`` pair.

    ``image_or_region`` is PNG bytes, a filesystem path, a PIL image, a
    `capture.Screenshot` (the whole display, origin at the display's
    top-left), a `capture.ScaledImage` (source pixels from ``(0, 0)`` on
    display 0), a ``crop`` result ``(text, ScaledImage)`` whose text is
    `format_crop`, or ``(image, bounds)`` / ``{"png": ..., "bounds": ...}``
    for an element crop or an opaque region. ``bounds`` is that image's
    rectangle in screen pixels. Boxes are scaled from image pixels onto
    that rectangle.

    Each item is ``{"text", "bounds", "confidence"}``. ``text`` is
    `fence_untrusted` output. ``bounds`` is
    ``{display_id, x, y, width, height}`` in screen pixels. Words on one
    line are one item. Items under ``min_confidence`` are omitted.

    ``engine`` is ``"rapidocr"``, ``"tesseract"``, ``"vision"``, None, or
    an object with ``recognize(png) -> list[TextBox]`` (tests). None uses
    `choose_engine_name`. A missing engine raises `ComputerUseError` and
    does not fall through when the name was explicit.
    """
    if isinstance(min_confidence, bool) or not isinstance(min_confidence, (int, float)):
        raise ValueError("min_confidence must be between 0 and 1")
    if not 0.0 <= float(min_confidence) <= 1.0:
        raise ValueError("min_confidence must be between 0 and 1")
    png, bounds = _image_and_bounds(image_or_region)
    if engine is None or isinstance(engine, str):
        reader = select_engine(engine)
    elif hasattr(engine, "recognize"):
        reader = engine
    else:
        raise ValueError(f"engine must be a name or an OCR engine, got {engine!r}")
    try:
        from PIL import Image
    except ImportError as exc:  # pillow is a core dependency
        raise _missing_engine("pillow", "pip install pillow") from exc
    try:
        with Image.open(io.BytesIO(png)) as image:
            image_w, image_h = image.size
    except Exception as exc:  # noqa: BLE001 - PIL's decoder errors are not one class
        raise ValueError("not a decodable image") from exc
    boxes = reader.recognize(png)
    return _spans(boxes, bounds, image_w, image_h, float(min_confidence))


def _spans(
    boxes: Sequence[TextBox],
    bounds: Bounds,
    image_w: int,
    image_h: int,
    min_confidence: float,
) -> list[dict[str, object]]:
    sx = bounds.width / max(1, image_w)
    sy = bounds.height / max(1, image_h)
    kept = [box for box in boxes if box.confidence >= min_confidence and box.text.strip()]
    spans: list[dict[str, object]] = []
    for group in group_lines(kept):
        left = min(box.x for box in group)
        top = min(box.y for box in group)
        right = max(box.x + box.w for box in group)
        bottom = max(box.y + box.h for box in group)
        shown = fence_untrusted(" ".join(box.text.strip() for box in group), limit=_OCR_TEXT_LIMIT)
        if not shown:
            continue
        spans.append(
            {
                "text": shown,
                "bounds": {
                    "display_id": bounds.display_id,
                    "x": int(round(left * sx)) + bounds.x,
                    "y": int(round(top * sy)) + bounds.y,
                    "width": max(1, int(round((right - left) * sx))),
                    "height": max(1, int(round((bottom - top) * sy))),
                },
                "confidence": min(box.confidence for box in group),
            }
        )
    return spans


def _image_and_bounds(image_or_region: object) -> tuple[bytes, Bounds]:
    if isinstance(image_or_region, (tuple, list)) and len(image_or_region) == 2:
        image, region = image_or_region
        if isinstance(image, str) and _is_scaled_image(region):
            parsed = _bounds_from_crop_text(image)
            return _png_of(region), parsed
        return _png_of(image), _bounds_of(region, _png_of(image))
    if isinstance(image_or_region, Mapping):
        image = image_or_region.get("image", image_or_region.get("png", image_or_region.get("path")))
        if image is None:
            raise ValueError("image_or_region mapping needs image, png, or path")
        region = image_or_region.get("bounds", image_or_region.get("region"))
        png = _png_of(image)
        if region is None:
            return png, _bounds_of(None, png)
        return png, _bounds_of(region, png)
    png = _png_of(image_or_region)
    display = getattr(image_or_region, "display", None)
    if display is not None and hasattr(display, "width"):
        return png, Bounds(int(display.display_id), 0, 0, int(display.width), int(display.height))
    if _is_scaled_image(image_or_region):
        return png, Bounds(
            0,
            0,
            0,
            int(image_or_region.source_width),  # type: ignore[attr-defined]
            int(image_or_region.source_height),  # type: ignore[attr-defined]
        )
    return png, _bounds_of(None, png)


def _is_scaled_image(value: object) -> bool:
    return isinstance(getattr(value, "png", None), (bytes, bytearray)) and hasattr(value, "source_width")


def _bounds_from_crop_text(text: str) -> Bounds:
    match = _CROP_BOUNDS.search(text)
    if match is None:
        raise ValueError("crop text has no display rectangle; pass (image, bounds)")
    return Bounds(
        int(match.group("display_id")),
        int(match.group("x")),
        int(match.group("y")),
        int(match.group("width")),
        int(match.group("height")),
    )


def _png_of(image: object) -> bytes:
    if isinstance(image, (bytes, bytearray)):
        return bytes(image)
    if _is_scaled_image(image) or isinstance(getattr(image, "png", None), (bytes, bytearray)):
        return bytes(image.png)  # type: ignore[attr-defined]
    if isinstance(image, (str, Path)):
        path = Path(image)
        if not path.is_file():
            raise ValueError(f"image path not found: {image}")
        return path.read_bytes()
    save = getattr(image, "save", None)
    if callable(save):
        buffer = io.BytesIO()
        save(buffer, format="PNG")
        return buffer.getvalue()
    raise ValueError(f"image_or_region must be an image, a crop, or (image, bounds), got {type(image).__name__}")


def _bounds_of(region: object, png: bytes) -> Bounds:
    if isinstance(region, Bounds):
        return region
    if isinstance(region, Mapping):
        try:
            return Bounds(
                int(region.get("display_id", 0)),
                int(region["x"]),
                int(region["y"]),
                int(region["width"]),
                int(region["height"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("bounds must be {x, y, width, height}") from exc
    if region is None:
        from PIL import Image

        with Image.open(io.BytesIO(png)) as image:
            width, height = image.size
        return Bounds(0, 0, 0, width, height)
    raise ValueError(f"bounds must be a Bounds or a mapping, got {type(region).__name__}")


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
    target_size: tuple[int, int] | None = None,
    engine: str = "",
    created_at: float | None = None,
) -> ScreenText:
    """Turn engine boxes into a `ScreenText` epoch in display-space pixels.

    ``image_width``/``image_height`` are the recognised image's dimensions;
    boxes are scaled onto ``display.width``/``display.height`` so a capture at
    the backing scale and a capture at nominal resolution both yield the same
    display-qualified rects (the contract every `Point`/`Bounds` follows).
    When the image is a crop, ``target_size`` is the crop's size in display
    pixels (the scaling target instead of the whole display) and ``offset``
    is its top-left corner in display pixels. Boxes under ``min_confidence``
    are dropped before grouping.
    """
    target_w, target_h = target_size if target_size is not None else (display.width, display.height)
    sx = target_w / max(1, image_width)
    sy = target_h / max(1, image_height)
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


def crop_png(
    png: bytes, region: Bounds, display: Display
) -> tuple[bytes, int, int, tuple[int, int], tuple[int, int]]:
    """Crop ``png`` (a capture of ``display``) to ``region`` (display pixels).

    Returns the cropped PNG, its pixel size, the display-space offset of its
    top-left corner, and the clamped region's display-space size, for
    `build_screen_text`'s ``offset`` and ``target_size``. The capture may not
    be at display resolution, so the crop box is rescaled onto the image.
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
    return buffer.getvalue(), crop.width, crop.height, (left, top), (right - left, bottom - top)


__all__ = [
    "DEFAULT_MIN_CONFIDENCE",
    "REMATCH_RADIUS_PX",
    "FakeOcr",
    "OcrEngine",
    "RapidOcr",
    "ScreenText",
    "TesseractOcr",
    "TextBox",
    "TextLine",
    "VisionOcr",
    "build_screen_text",
    "choose_engine_name",
    "crop_png",
    "default_engine",
    "find_lines",
    "group_lines",
    "is_ocr_ref",
    "ocr",
    "rematch_line",
    "render_screen_text",
    "select_engine",
]
