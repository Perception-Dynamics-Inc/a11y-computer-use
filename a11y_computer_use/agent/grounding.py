"""Optional local grounding for the vision fallback.

``GroundingModel.ground(png, instruction)`` returns a point or a box in
image pixels. ``Agent`` does not construct one. Pass an instance only when
the caller opts in.

``HoloLocalGrounding`` loads ``Hcompany/Holo-3.1-0.8B`` on CPU. The model
card at https://huggingface.co/Hcompany/Holo-3.1-0.8B says
``license: apache-2.0`` and "License: Apache 2.0 License". Apache 2.0
permits commercial use. This package does not vendor the weights, does not
download them at import, and does not call a hosted API. Float32 weights
for an 0.8B model are about 3.2 GB, plus the vision tower and runtime,
which fits a 15 GB machine with no GPU. The adapter never moves the model
to a CUDA device.
"""

from __future__ import annotations

import io
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

HOLO_MODEL_ID = "Hcompany/Holo-3.1-0.8B"

_POINT = re.compile(
    r'"point"\s*:\s*\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]'
)
_BOX = re.compile(
    r'"box"\s*:\s*\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*'
    r'(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]'
)
_SPACE = re.compile(r'"space"\s*:\s*"(image|norm1000)"')


class GroundingUnavailable(RuntimeError):
    """The local grounding model cannot run in this process."""


@dataclass(frozen=True, slots=True)
class GroundingHit:
    """A point, and optionally a box, in the image's pixel space.

    ``x`` and ``y`` are the click point. ``box`` is
    ``(x, y, width, height)`` when the model returned one. The origin is
    the top-left of the PNG that was passed in.
    """

    x: int
    y: int
    box: tuple[int, int, int, int] | None = None


@runtime_checkable
class GroundingModel(Protocol):
    """Image plus an instruction, then a point or a box. No network."""

    def ground(self, png: bytes, instruction: str) -> GroundingHit:
        """Locate ``instruction`` in ``png``."""


class ScriptedGrounding:
    """Return a fixed hit, or the result of ``(png, instruction) -> hit``."""

    def __init__(self, hit: GroundingHit | Callable[[bytes, str], GroundingHit]) -> None:
        self._hit = hit
        self.seen: list[tuple[bytes, str]] = []

    def ground(self, png: bytes, instruction: str) -> GroundingHit:
        self.seen.append((png, instruction))
        if callable(self._hit) and not isinstance(self._hit, GroundingHit):
            return self._hit(png, instruction)
        return self._hit


def parse_grounding_text(text: str, width: int, height: int) -> GroundingHit | None:
    """Read a point or a box from a model reply.

    ``space`` ``image`` means pixels. ``space`` ``norm1000`` means the
    0..1000 grid, scaled onto ``width`` and ``height``. When ``space`` is
    omitted, values that all sit in 0..1000 on an image larger than 1000
    on that axis are treated as the normalized grid. Anything else is
    image pixels.
    """
    if not text:
        return None
    space = _SPACE.search(text)
    named = space.group(1) if space else None
    box_match = _BOX.search(text)
    point_match = _POINT.search(text)
    if box_match is None and point_match is None:
        return None
    if box_match is not None:
        raw = tuple(float(box_match.group(index)) for index in range(1, 5))
        left, top, wide, tall = _scale_box(raw, named, width, height)
        box = (left, top, max(1, wide), max(1, tall))
        return GroundingHit(x=left + box[2] // 2, y=top + box[3] // 2, box=box)
    assert point_match is not None
    px, py = _scale_point(
        (float(point_match.group(1)), float(point_match.group(2))),
        named,
        width,
        height,
    )
    return GroundingHit(x=px, y=py)


def _scale_point(
    point: tuple[float, float], space: str | None, width: int, height: int,
) -> tuple[int, int]:
    x, y = point
    if _use_norm1000(space, (x, y), width, height):
        x = x / 1000.0 * max(width, 1)
        y = y / 1000.0 * max(height, 1)
    return int(round(x)), int(round(y))


def _scale_box(
    raw: tuple[float, float, float, float],
    space: str | None,
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    left, top, wide, tall = raw
    if _use_norm1000(space, (left, top, wide, tall), width, height):
        left = left / 1000.0 * max(width, 1)
        top = top / 1000.0 * max(height, 1)
        wide = wide / 1000.0 * max(width, 1)
        tall = tall / 1000.0 * max(height, 1)
    return int(round(left)), int(round(top)), int(round(wide)), int(round(tall))


def _use_norm1000(
    space: str | None, values: tuple[float, ...], width: int, height: int,
) -> bool:
    if space == "norm1000":
        return True
    if space == "image":
        return False
    if width <= 1000 and height <= 1000:
        return False
    return all(0.0 <= value <= 1000.0 for value in values)


class HoloLocalGrounding:
    """CPU-only loader for ``Hcompany/Holo-3.1-0.8B``.

    Constructing this class does not import torch and does not download
    weights. ``ground`` does both, and raises `GroundingUnavailable` when
    the local stack or the weights are missing. No other model id is
    accepted.
    """

    def __init__(self, model_id: str = HOLO_MODEL_ID) -> None:
        if model_id != HOLO_MODEL_ID:
            raise ValueError(
                "the local grounding adapter only loads Hcompany/Holo-3.1-0.8B"
            )
        self.model_id = model_id
        self._bundle: tuple[object, object] | None = None

    def ground(self, png: bytes, instruction: str) -> GroundingHit:
        text = self._generate(png, instruction)
        width, height = _png_size(png)
        hit = parse_grounding_text(text, width, height)
        if hit is None:
            raise GroundingUnavailable("the local model did not return a point or a box")
        return hit

    def _generate(self, png: bytes, instruction: str) -> str:
        torch, model_cls, processor_cls = _torch_stack()
        if self._bundle is None:
            try:
                model = model_cls.from_pretrained(self.model_id, torch_dtype=torch.float32)
                processor = processor_cls.from_pretrained(self.model_id)
                model.to("cpu")
                model.eval()
            except Exception as exc:  # noqa: BLE001 - a missing weight download stays local
                raise GroundingUnavailable(
                    f"could not load {self.model_id} on CPU: {type(exc).__name__}: {exc}"
                ) from exc
            self._bundle = (model, processor)
        model, processor = self._bundle
        try:
            from PIL import Image
        except ImportError as exc:
            raise GroundingUnavailable("pillow is required to read the screenshot") from exc
        try:
            image = Image.open(io.BytesIO(png)).convert("RGB")
        except Exception as exc:  # noqa: BLE001 - PIL's decoder errors are not one class
            raise GroundingUnavailable("not a decodable image") from exc
        prompt = (
            "Locate the target in the image. Reply with JSON only: "
            '{"point": [x, y], "space": "image"} with x and y in pixels '
            "from the top-left. Instruction: "
            + instruction
        )
        try:
            inputs = processor(text=prompt, images=image, return_tensors="pt")
            if hasattr(inputs, "to"):
                inputs = inputs.to("cpu")
            else:
                inputs = {
                    key: value.to("cpu") if hasattr(value, "to") else value
                    for key, value in inputs.items()
                }
            with torch.no_grad():
                output = model.generate(**inputs, max_new_tokens=128)
            decoded = processor.batch_decode(output, skip_special_tokens=True)
        except Exception as exc:  # noqa: BLE001 - a local generate failure is not fatal upstream
            raise GroundingUnavailable(
                f"local grounding failed: {type(exc).__name__}: {exc}"
            ) from exc
        if not decoded:
            raise GroundingUnavailable("the local model returned no text")
        return str(decoded[0])


def _torch_stack():
    try:
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor
    except ImportError as exc:
        raise GroundingUnavailable(
            "local grounding needs torch and transformers, which are not installed"
        ) from exc
    return torch, AutoModelForImageTextToText, AutoProcessor


def _png_size(png: bytes) -> tuple[int, int]:
    try:
        from PIL import Image
    except ImportError:
        return 0, 0
    try:
        with Image.open(io.BytesIO(png)) as image:
            return int(image.size[0]), int(image.size[1])
    except Exception:  # noqa: BLE001 - a stub PNG has no size; parsing still runs
        return 0, 0


__all__ = [
    "HOLO_MODEL_ID",
    "GroundingHit",
    "GroundingModel",
    "GroundingUnavailable",
    "HoloLocalGrounding",
    "ScriptedGrounding",
    "parse_grounding_text",
]
