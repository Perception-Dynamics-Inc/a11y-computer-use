"""Optional local grounding for the vision fallback.

``GroundingModel.ground(png, instruction)`` returns a point or a box in
image pixels. ``Agent`` does not construct one. Pass an instance only when
the caller opts in.

``HoloLocalGrounding`` loads ``Hcompany/Holo-3.1-0.8B`` on CPU. The model
card at https://huggingface.co/Hcompany/Holo-3.1-0.8B says
``license: apache-2.0`` and "License: Apache 2.0 License". Apache 2.0
permits commercial use. This package does not vendor the weights, does not
download them at import, and does not call a hosted API. Install the
optional ``[holo]`` extra (``torch``, ``torchvision``, and ``transformers``).
``AutoProcessor`` builds ``Qwen3VLVideoProcessor``, and that class imports
torchvision even though ``ground`` only sends an image. A CPU torch and the
default PyPI torchvision wheel are not the same build: importing torchvision
raises ``RuntimeError: operator torchvision::nms does not exist``. Install
torch and torchvision from the same index (the CPU index for a ``+cpu``
torch). That failure is ``GroundingUnavailable``. The adapter stays on
CPU: it loads bfloat16 when that dtype runs, otherwise float16, otherwise
float32, and it caps torch at
4 threads. The reply is ``{"x", "y"}`` on a 0..1000 grid, scaled onto the
image. The adapter never moves the model to a CUDA device.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

HOLO_MODEL_ID = "Hcompany/Holo-3.1-0.8B"

#: Torch is capped here. More threads oversubscribed the eval machine.
HOLO_MAX_THREADS = 4

#: The reply is about ten tokens. Fourteen leaves room for the JSON and stops.
HOLO_MAX_NEW_TOKENS = 14

#: H Company's element-localization schema. Coordinates are integers in 0..1000.
_HOLO_SCHEMA = {
    "properties": {
        "x": {
            "description": "X coordinate as integer in [0, 1000]",
            "maximum": 1000,
            "minimum": 0,
            "type": "integer",
        },
        "y": {
            "description": "Y coordinate as integer in [0, 1000]",
            "maximum": 1000,
            "minimum": 0,
            "type": "integer",
        },
    },
    "required": ["x", "y"],
    "title": "VisualLocalizerOutput",
    "type": "object",
}

_POINT = re.compile(
    r'"point"\s*:\s*\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]'
)
_BOX = re.compile(
    r'"box"\s*:\s*\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*'
    r'(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]'
)
_SPACE = re.compile(r'"space"\s*:\s*"(image|norm1000)"')
_XY = re.compile(
    r'"x"\s*:\s*(-?\d+(?:\.\d+)?)\s*,\s*"y"\s*:\s*(-?\d+(?:\.\d+)?)'
)
_YX = re.compile(
    r'"y"\s*:\s*(-?\d+(?:\.\d+)?)\s*,\s*"x"\s*:\s*(-?\d+(?:\.\d+)?)'
)


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

    A Holo reply is ``{"x": ..., "y": ...}`` with no ``point`` key. Those
    two numbers are always the 0..1000 grid, including when the image is
    1000 pixels or smaller on both axes, and they are scaled onto
    ``width`` and ``height``.
    """
    if not text:
        return None
    space = _SPACE.search(text)
    named = space.group(1) if space else None
    box_match = _BOX.search(text)
    point_match = _POINT.search(text)
    if box_match is None and point_match is None:
        return _parse_holo_xy(text, width, height)
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


def _parse_holo_xy(text: str, width: int, height: int) -> GroundingHit | None:
    """Scale Holo's ``x``/``y`` pair from the 0..1000 grid into pixels."""
    match = _XY.search(text)
    if match is not None:
        raw = (float(match.group(1)), float(match.group(2)))
    else:
        swapped = _YX.search(text)
        if swapped is None:
            return None
        raw = (float(swapped.group(2)), float(swapped.group(1)))
    px, py = _scale_point(raw, "norm1000", width, height)
    return GroundingHit(x=px, y=py)


def _holo_prompt(target: str, extra: str = "") -> str:
    """H Company's element-localization prompt. ``extra`` is unused by ``ground``."""
    return (
        "Localize an element on the GUI image according to the provided target "
        "and output a click position.\n"
        " * You must output a valid JSON following the format: "
        f"{json.dumps(_HOLO_SCHEMA)}\n"
        f"{extra} Your target is:\n{target}"
    )


def _cap_torch_threads(threads: int) -> int:
    """A torch thread count from 1 through ``HOLO_MAX_THREADS``."""
    try:
        count = int(threads)
    except (TypeError, ValueError):
        return HOLO_MAX_THREADS
    if count < 1:
        return 1
    if count > HOLO_MAX_THREADS:
        return HOLO_MAX_THREADS
    return count


def _dtype_ops_work(torch, dtype: object) -> bool:
    zeros = getattr(torch, "zeros", None)
    if not callable(zeros):
        return False
    try:
        probe = zeros(1, dtype=dtype)
        probe + probe
    except Exception:  # noqa: BLE001 - an unsupported dtype is a fallback, not a failure
        return False
    return True


def _select_weight_dtype(torch, preference: str = "auto"):
    """bfloat16, else float16, else float32. An explicit name is tried first.

    ``float32`` is the last resort. It is not probed: every torch build
    this adapter targets can allocate it.
    """
    order = {
        "auto": ("bfloat16", "float16", "float32"),
        "bfloat16": ("bfloat16", "float16", "float32"),
        "bf16": ("bfloat16", "float16", "float32"),
        "float16": ("float16", "bfloat16", "float32"),
        "fp16": ("float16", "bfloat16", "float32"),
        "float32": ("float32",),
        "fp32": ("float32",),
    }.get(str(preference).lower(), ("bfloat16", "float16", "float32"))
    chosen = None
    for name in order:
        dtype = getattr(torch, name, None)
        if dtype is None:
            continue
        if name == "float32" or _dtype_ops_work(torch, dtype):
            chosen = dtype
            break
    if chosen is None:
        chosen = getattr(torch, "float32", None)
    if chosen is None:
        raise GroundingUnavailable("torch has no usable floating dtype")
    return chosen


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
    the local stack or the weights are missing. A failed import names the
    missing distribution (``torch``, ``torchvision``, or ``transformers``).
    A ``RuntimeError`` or ``OSError`` from those imports, including
    ``operator torchvision::nms does not exist`` when a CPU torch is paired
    with the default PyPI torchvision wheel, is the same error and names
    the CPU install command.
    No other model id is accepted. ``threads`` is capped at 4. ``dtype``
    ``auto`` (the default) uses bfloat16 when that dtype runs, otherwise
    float16, otherwise float32.
    """

    def __init__(
        self,
        model_id: str = HOLO_MODEL_ID,
        *,
        threads: int = HOLO_MAX_THREADS,
        dtype: str = "auto",
    ) -> None:
        if model_id != HOLO_MODEL_ID:
            raise ValueError(
                "the local grounding adapter only loads Hcompany/Holo-3.1-0.8B"
            )
        self.model_id = model_id
        self.threads = _cap_torch_threads(threads)
        self.dtype = dtype
        self._bundle: tuple[object, object] | None = None

    def ground(self, png: bytes, instruction: str) -> GroundingHit:
        text, width, height = self._generate(png, instruction)
        hit = parse_grounding_text(text, width, height)
        if hit is None:
            raise GroundingUnavailable("the local model did not return a point or a box")
        return hit

    def _generate(self, png: bytes, instruction: str) -> tuple[str, int, int]:
        torch, model_cls, processor_cls = _torch_stack()
        self._cap_runtime_threads(torch)
        if self._bundle is None:
            self._bundle = self._load(torch, model_cls, processor_cls)
        model, processor = self._bundle
        image = _open_rgb(png)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image"},
                    {"type": "text", "text": _holo_prompt(instruction)},
                ],
            }
        ]
        try:
            templated = processor.apply_chat_template(
                messages,
                add_generation_prompt=True,
                tokenize=False,
                enable_thinking=False,
            )
            inputs = processor(text=[templated], images=[image], return_tensors="pt")
            inputs = _inputs_on_cpu(inputs)
            tokenizer = getattr(processor, "tokenizer", None)
            generate_kwargs: dict[str, object] = {
                "max_new_tokens": HOLO_MAX_NEW_TOKENS,
                "do_sample": False,
                "stop_strings": ["}"],
            }
            if tokenizer is not None:
                generate_kwargs["tokenizer"] = tokenizer
                eos = getattr(tokenizer, "eos_token_id", None)
                if eos is not None:
                    generate_kwargs["eos_token_id"] = eos
            with _inference_mode(torch):
                output = model.generate(**inputs, **generate_kwargs)
            decoded = processor.batch_decode(
                _new_token_ids(inputs, output),
                skip_special_tokens=True,
            )
        except GroundingUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - a local generate failure is not fatal upstream
            mismatch = _stack_mismatch_error(exc, where="load")
            if mismatch is not None:
                raise mismatch from exc
            raise GroundingUnavailable(
                f"local grounding failed: {type(exc).__name__}: {exc}"
            ) from exc
        if not decoded:
            raise GroundingUnavailable("the local model returned no text")
        return str(decoded[0]), int(image.size[0]), int(image.size[1])

    def _cap_runtime_threads(self, torch) -> None:
        setter = getattr(torch, "set_num_threads", None)
        if not callable(setter):
            raise GroundingUnavailable(
                "torch.set_num_threads is missing; cannot cap torch threads at 4"
            )
        setter(_cap_torch_threads(self.threads))

    def _load(self, torch, model_cls, processor_cls) -> tuple[object, object]:
        try:
            dtype = _select_weight_dtype(torch, self.dtype)
            model = model_cls.from_pretrained(self.model_id, dtype=dtype)
            processor = processor_cls.from_pretrained(self.model_id)
            if hasattr(model, "to"):
                model = model.to("cpu")
            if hasattr(model, "eval"):
                model.eval()
        except GroundingUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - a missing weight download stays local
            mismatch = _stack_mismatch_error(exc, where="load")
            if mismatch is not None:
                raise mismatch from exc
            raise _load_unavailable(self.model_id, exc) from exc
        return model, processor


def _inference_mode(torch):
    method = getattr(torch, "inference_mode", None)
    if callable(method):
        return method()
    return torch.no_grad()


def _inputs_on_cpu(inputs):
    if hasattr(inputs, "to"):
        return inputs.to("cpu")
    return {
        key: value.to("cpu") if hasattr(value, "to") else value
        for key, value in inputs.items()
    }


def _new_token_ids(inputs, output):
    """Token ids the model generated, excluding the prompt."""
    input_ids = inputs["input_ids"]
    prompt_len = int(input_ids.shape[1])
    return output[:, prompt_len:]


def _open_rgb(png: bytes):
    try:
        from PIL import Image
    except ImportError as exc:
        raise GroundingUnavailable("pillow is required to read the screenshot") from exc
    try:
        return Image.open(io.BytesIO(png)).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - PIL's decoder errors are not one class
        raise GroundingUnavailable("not a decodable image") from exc


#: Imported before any weight download. The processor imports torchvision
#: while it builds the video processor, including for an image-only call.
_HOLO_IMPORTS = ("torch", "torchvision", "transformers")

#: A ``+cpu`` torch and the default PyPI torchvision wheel are different
#: builds. ``pip`` cannot put this index inside the ``[holo]`` extra.
_CPU_TORCH_INSTALL = (
    "pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision"
)

#: Longer names first so "torchvision" is not reported as "torch".
_HOLO_PACKAGES = ("torchvision", "transformers", "torch")


def _try_import(name: str) -> object:
    return __import__(name)


def _package_in_text(text: str) -> str | None:
    lowered = text.lower()
    for package in _HOLO_PACKAGES:
        if package in lowered:
            return package
    return None


def _package_named_by(exc: BaseException) -> str | None:
    """The holo distribution an import failure is about, when it names one."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ImportError):
            named = _package_in_text(getattr(current, "name", None) or "")
            if named:
                return named
            named = _package_in_text(str(current))
            if named:
                return named
        cause = current.__cause__
        if cause is not None:
            current = cause
            continue
        if current.__suppress_context__:
            break
        current = current.__context__
    return None


def _format_package_list(packages: list[str]) -> str:
    if len(packages) == 1:
        return packages[0]
    if len(packages) == 2:
        return f"{packages[0]} and {packages[1]}"
    return ", ".join(packages[:-1]) + f", and {packages[-1]}"


def _exception_text(exc: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current))
        cause = current.__cause__
        if cause is not None:
            current = cause
            continue
        if current.__suppress_context__:
            break
        current = current.__context__
    return " ".join(parts)


def _is_stack_mismatch(exc: BaseException) -> bool:
    """True when torch and torchvision are installed but are not one build.

    The default PyPI torchvision wheel raises ``RuntimeError: operator
    torchvision::nms does not exist`` inside a ``+cpu`` torch. A missing
    module is an ``ImportError`` and is not this case. An ``OSError`` that
    names torchvision or libtorch is a mismatched binary.
    """
    text = _exception_text(exc).lower()
    if isinstance(exc, ImportError) and "torchvision::" not in text:
        return False
    if "torchvision::nms" in text or "operator torchvision::" in text:
        return True
    return isinstance(exc, OSError) and ("torchvision" in text or "libtorch" in text)


def _mismatch_package(exc: BaseException) -> str:
    named = _package_named_by(exc) or _package_in_text(_exception_text(exc))
    return named or "torchvision"


def _stack_mismatch_error(
    exc: BaseException, *, where: str,
) -> GroundingUnavailable | None:
    """The CPU/PyPI torchvision mismatch, or None when ``exc`` is another failure."""
    if not _is_stack_mismatch(exc):
        return None
    package = _mismatch_package(exc)
    detail = f"{type(exc).__name__}: {exc}"
    return GroundingUnavailable(
        f"local grounding could not {where} {package}: {detail}. "
        "torch and torchvision must be the same build. "
        "The default PyPI torchvision wheel does not load into a CPU torch "
        "(operator torchvision::nms does not exist). "
        "Install torchvision from the same index as torch, for example the CPU index: "
        f"{_CPU_TORCH_INSTALL}"
    )


def _load_unavailable(model_id: str, exc: BaseException) -> GroundingUnavailable:
    """A weight or processor load failure. An import error names its package."""
    missing = _package_named_by(exc)
    detail = f"{type(exc).__name__}: {exc}"
    if missing:
        return GroundingUnavailable(
            f"could not load {model_id} on CPU: missing package {missing} "
            f"(pip install 'a11y-computer-use[holo]'): {detail}"
        )
    return GroundingUnavailable(f"could not load {model_id} on CPU: {detail}")


def _import_failure(name: str, exc: BaseException) -> GroundingUnavailable:
    """A torch-stack import that is not a missing distribution."""
    return _stack_mismatch_error(exc, where="import") or GroundingUnavailable(
        f"local grounding could not import {name}: {type(exc).__name__}: {exc}. "
        "torch and torchvision must be the same build. "
        "Install torchvision from the same index as torch, for example the CPU index: "
        f"{_CPU_TORCH_INSTALL}"
    )


def _torch_stack():
    missing: list[str] = []
    failure: ImportError | None = None
    for name in _HOLO_IMPORTS:
        try:
            _try_import(name)
        except ImportError as exc:
            mismatch = _stack_mismatch_error(exc, where="import")
            if mismatch is not None:
                raise mismatch from exc
            if failure is None:
                failure = exc
            package = _package_named_by(exc) or name
            if package not in missing:
                missing.append(package)
        except Exception as exc:
            raise _import_failure(name, exc) from exc
    if missing:
        listed = _format_package_list(missing)
        verb = "is" if len(missing) == 1 else "are"
        raise GroundingUnavailable(
            f"local grounding needs {listed}, which {verb} not installed "
            "(pip install 'a11y-computer-use[holo]')"
        ) from failure
    try:
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor
    except Exception as exc:
        raise _import_failure("transformers", exc) from exc

    return torch, AutoModelForImageTextToText, AutoProcessor


__all__ = [
    "HOLO_MAX_NEW_TOKENS",
    "HOLO_MAX_THREADS",
    "HOLO_MODEL_ID",
    "GroundingHit",
    "GroundingModel",
    "GroundingUnavailable",
    "HoloLocalGrounding",
    "ScriptedGrounding",
    "parse_grounding_text",
]
