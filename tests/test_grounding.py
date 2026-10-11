"""Hermetic checks for the optional local grounding adapter. No weights are loaded."""

from __future__ import annotations

import io
import tomllib
from pathlib import Path

import pytest
from PIL import Image

from a11y_computer_use.agent.grounding import (
    HOLO_MAX_NEW_TOKENS,
    HOLO_MAX_THREADS,
    HOLO_MODEL_ID,
    GroundingUnavailable,
    HoloLocalGrounding,
    _package_named_by,
    _torch_stack,
    parse_grounding_text,
)


def test_parse_image_pixels_and_the_normalized_grid() -> None:
    pixels = parse_grounding_text('{"point": [10, 20], "space": "image"}', 2000, 1000)
    assert pixels is not None
    assert (pixels.x, pixels.y) == (10, 20)
    assert pixels.box is None

    scaled = parse_grounding_text('{"point": [500, 250], "space": "norm1000"}', 2000, 1000)
    assert scaled is not None
    assert (scaled.x, scaled.y) == (1000, 250)

    implied = parse_grounding_text('{"point": [500, 500]}', 2000, 2000)
    assert implied is not None
    assert (implied.x, implied.y) == (1000, 1000)

    small = parse_grounding_text('{"point": [40, 12]}', 200, 80)
    assert small is not None
    assert (small.x, small.y) == (40, 12)

    box = parse_grounding_text('{"box": [0, 0, 100, 40], "space": "image"}', 200, 80)
    assert box is not None
    assert box.box == (0, 0, 100, 40)
    assert (box.x, box.y) == (50, 20)


def test_holo_adapter_accepts_only_its_model_id() -> None:
    model = HoloLocalGrounding()
    assert model.model_id == HOLO_MODEL_ID
    with pytest.raises(ValueError):
        HoloLocalGrounding("some/other-model")


def test_import_error_names_torchvision_rather_than_torch() -> None:
    """The processor's message says Torchvision; the package is torchvision."""
    exc = ImportError(
        "Qwen3VLVideoProcessor requires the Torchvision library but it was "
        "not found in your environment."
    )
    assert _package_named_by(exc) == "torchvision"
    chained = ImportError("could not build the video processor")
    chained.__cause__ = ModuleNotFoundError("No module named 'torchvision'", name="torchvision")
    assert _package_named_by(chained) == "torchvision"
    assert _package_named_by(ImportError("cannot import name 'missing_symbol'")) is None
    assert _package_named_by(ModuleNotFoundError("No module named 'torch'", name="torch")) == "torch"


def test_torch_stack_names_only_the_missing_package(monkeypatch) -> None:
    def fake_import(name: str) -> object:
        if name == "torchvision":
            raise ModuleNotFoundError("No module named 'torchvision'", name="torchvision")
        return object()

    monkeypatch.setattr("a11y_computer_use.agent.grounding._try_import", fake_import)
    with pytest.raises(GroundingUnavailable, match="torchvision") as raised:
        _torch_stack()
    message = str(raised.value)
    assert "local grounding needs torchvision, which is not installed" in message
    assert "transformers" not in message
    assert "a11y-computer-use[holo]" in message


def test_processor_load_names_the_missing_package(monkeypatch) -> None:
    class _ProcessorCls:
        def from_pretrained(self, model_id: str) -> None:
            assert model_id == HOLO_MODEL_ID
            raise ImportError(
                "Qwen3VLVideoProcessor requires the Torchvision library but it was "
                "not found in your environment."
            )

    _install_stack(monkeypatch, _Torch(), _ModelCls(), _ProcessorCls())
    with pytest.raises(GroundingUnavailable, match="torchvision") as raised:
        HoloLocalGrounding().ground(_png(8, 8), "the green SUBMIT button")
    message = str(raised.value)
    assert "missing package torchvision" in message
    assert "missing package torch " not in message
    assert "a11y-computer-use[holo]" in message


def test_processor_load_keeps_an_unrelated_error(monkeypatch) -> None:
    class _ProcessorCls:
        def from_pretrained(self, model_id: str) -> None:
            del model_id
            raise RuntimeError("weights file is truncated")

    _install_stack(monkeypatch, _Torch(), _ModelCls(), _ProcessorCls())
    with pytest.raises(GroundingUnavailable, match="truncated") as raised:
        HoloLocalGrounding().ground(_png(8, 8), "icon")
    assert "missing package" not in str(raised.value)


def test_holo_ground_reports_a_missing_local_stack(monkeypatch) -> None:
    def missing():
        raise GroundingUnavailable(
            "local grounding needs torch and transformers, which are not installed"
        )

    monkeypatch.setattr(
        "a11y_computer_use.agent.grounding._torch_stack",
        missing,
    )
    with pytest.raises(GroundingUnavailable, match="torch"):
        HoloLocalGrounding().ground(b"not a png", "click the red square")


def test_holo_xy_is_always_scaled_from_the_0_1000_grid() -> None:
    """Holo's ``{"x","y"}`` reply is norm-1000, including on a small image."""
    wide = parse_grounding_text('{"x":621,"y":581}', 1280, 800)
    assert wide is not None
    assert (wide.x, wide.y) == (795, 465)
    assert wide.box is None

    small = parse_grounding_text('{"x": 500, "y": 250}', 200, 80)
    assert small is not None
    assert (small.x, small.y) == (100, 20)

    swapped = parse_grounding_text('{"y":250,"x":500}', 200, 80)
    assert swapped is not None
    assert (swapped.x, swapped.y) == (100, 20)

    assert parse_grounding_text("no coordinates here", 1280, 800) is None


def test_constructing_holo_does_not_touch_the_stack_and_caps_threads(monkeypatch) -> None:
    def boom():
        raise AssertionError("constructing the adapter must not import torch")

    monkeypatch.setattr("a11y_computer_use.agent.grounding._torch_stack", boom)
    model = HoloLocalGrounding(threads=32)
    assert model.threads == HOLO_MAX_THREADS
    assert HoloLocalGrounding(threads=0).threads == 1
    assert HoloLocalGrounding(threads=4).threads == 4


class _Probe:
    def __init__(self, dtype: object) -> None:
        self.dtype = dtype

    def __add__(self, other: object) -> "_Probe":
        del other
        return self


class _Tensor:
    def __init__(self, rows: int, cols: int) -> None:
        self.shape = (rows, cols)
        self.device = None

    def to(self, device: str) -> "_Tensor":
        self.device = device
        return self


class _Output:
    def __init__(self, prompt_len: int) -> None:
        self.prompt_len = prompt_len
        self.sliced_at: int | None = None

    def __getitem__(self, item: object) -> str:
        if not isinstance(item, tuple) or len(item) != 2:
            raise AssertionError(item)
        start = item[1].start  # type: ignore[attr-defined]
        self.sliced_at = start
        if start != self.prompt_len:
            raise AssertionError(start)
        return "new-tokens"


class _Tokenizer:
    eos_token_id = 151645


class _Processor:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.tokenizer = _Tokenizer()
        self.template_calls: list[tuple] = []
        self.process_calls: list[dict] = []
        self.decoded = None

    def apply_chat_template(self, messages, **kwargs):
        self.template_calls.append((messages, kwargs))
        return "TEMPLATED-WITH-IMAGE"

    def __call__(self, text=None, images=None, **kwargs):
        self.process_calls.append({"text": text, "images": images, "kwargs": kwargs})
        return {"input_ids": _Tensor(1, 11)}

    def batch_decode(self, tokens, skip_special_tokens=False):
        self.decoded = (tokens, skip_special_tokens)
        return [self.reply]


class _ProcessorCls:
    def __init__(self, processor: _Processor) -> None:
        self.processor = processor

    def from_pretrained(self, model_id: str) -> _Processor:
        assert model_id == HOLO_MODEL_ID
        return self.processor


class _Model:
    def __init__(self) -> None:
        self.device = None
        self.eval_called = False
        self.generate_kwargs: dict | None = None
        self.output = _Output(11)

    def to(self, device: str) -> "_Model":
        self.device = device
        return self

    def eval(self) -> "_Model":
        self.eval_called = True
        return self

    def generate(self, **kwargs):
        self.generate_kwargs = kwargs
        return self.output


class _ModelCls:
    def __init__(self) -> None:
        self.kwargs: dict | None = None
        self.model = _Model()

    def from_pretrained(self, model_id: str, **kwargs):
        assert model_id == HOLO_MODEL_ID
        self.kwargs = kwargs
        return self.model


class _Torch:
    bfloat16 = "bf16"
    float16 = "fp16"
    float32 = "fp32"
    threads: int | None = None

    def __init__(self, *, break_bf16: bool = False, break_fp16: bool = False) -> None:
        self.break_bf16 = break_bf16
        self.break_fp16 = break_fp16

    def set_num_threads(self, count: int) -> None:
        self.threads = count

    def zeros(self, n: int, dtype: object = None):
        del n
        if self.break_bf16 and dtype == self.bfloat16:
            raise RuntimeError("bfloat16 is not supported")
        if self.break_fp16 and dtype == self.float16:
            raise RuntimeError("float16 is not supported")
        return _Probe(dtype)

    def inference_mode(self):
        return _Null()


class _Null:
    def __enter__(self):
        return None

    def __exit__(self, *args):
        return False


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def _install_stack(monkeypatch, torch, model_cls, processor_cls) -> None:
    monkeypatch.setattr(
        "a11y_computer_use.agent.grounding._torch_stack",
        lambda: (torch, model_cls, processor_cls),
    )


def test_ground_sends_image_tokens_stops_and_caps_threads(monkeypatch) -> None:
    torch = _Torch()
    model_cls = _ModelCls()
    processor = _Processor('{"x": 500, "y": 250}')
    _install_stack(monkeypatch, torch, model_cls, _ProcessorCls(processor))
    hit = HoloLocalGrounding(threads=16).ground(_png(20, 10), "the Save button")
    assert (hit.x, hit.y) == (10, 2)
    assert torch.threads == HOLO_MAX_THREADS

    messages, template_kwargs = processor.template_calls[0]
    assert template_kwargs["add_generation_prompt"] is True
    assert template_kwargs["tokenize"] is False
    assert template_kwargs["enable_thinking"] is False
    content = messages[0]["content"]
    assert content[0] == {"type": "image"}
    assert content[1]["type"] == "text"
    assert "the Save button" in content[1]["text"]
    assert "VisualLocalizerOutput" in content[1]["text"]
    assert "Your target is:" in content[1]["text"]

    processed = processor.process_calls[0]
    assert processed["text"] == ["TEMPLATED-WITH-IMAGE"]
    assert len(processed["images"]) == 1
    assert processed["images"][0].size == (20, 10)
    assert processed["kwargs"]["return_tensors"] == "pt"

    generated = model_cls.model.generate_kwargs
    assert generated is not None
    assert generated["max_new_tokens"] == HOLO_MAX_NEW_TOKENS
    assert generated["do_sample"] is False
    assert generated["stop_strings"] == ["}"]
    assert generated["eos_token_id"] == processor.tokenizer.eos_token_id
    assert generated["tokenizer"] is processor.tokenizer
    assert "input_ids" in generated
    assert generated["input_ids"].device == "cpu"
    assert processor.decoded == ("new-tokens", True)
    assert model_cls.model.output.sliced_at == 11
    assert model_cls.kwargs == {"dtype": torch.bfloat16}
    assert model_cls.model.device == "cpu"
    assert model_cls.model.eval_called is True


def test_weight_dtype_falls_back_when_bf16_does_not_run(monkeypatch) -> None:
    torch = _Torch(break_bf16=True)
    model_cls = _ModelCls()
    processor = _Processor('{"x": 0, "y": 0}')
    _install_stack(monkeypatch, torch, model_cls, _ProcessorCls(processor))
    hit = HoloLocalGrounding().ground(_png(4, 4), "icon")
    assert (hit.x, hit.y) == (0, 0)
    assert model_cls.kwargs == {"dtype": torch.float16}
    assert torch.threads == HOLO_MAX_THREADS


def test_weight_dtype_uses_float32_only_when_narrower_dtypes_fail(monkeypatch) -> None:
    torch = _Torch(break_bf16=True, break_fp16=True)
    model_cls = _ModelCls()
    processor = _Processor('{"x": 1000, "y": 1000}')
    _install_stack(monkeypatch, torch, model_cls, _ProcessorCls(processor))
    hit = HoloLocalGrounding().ground(_png(10, 8), "corner")
    assert (hit.x, hit.y) == (10, 8)
    assert model_cls.kwargs == {"dtype": torch.float32}


def test_holo_extra_pins_transformers_and_vendors_no_weights() -> None:
    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    base = project["project"]["dependencies"]
    extra = project["project"]["optional-dependencies"]["holo"]
    assert not any("torch" in dep.lower() or "transformers" in dep.lower() for dep in base)
    assert "holo" not in project["project"]["dependencies"]
    assert "torch" in extra
    assert "torchvision" in extra
    assert "transformers>=5.6,<5.20" in extra
    # CPU torch (for example 2.14.1+cpu) satisfies torchvision's torch
    # requirement. The extra does not point at a CUDA wheel index.
    assert all("cuda" not in dep.lower() and "://" not in dep for dep in extra)
    root = Path(__file__).resolve().parents[1]
    weights = [
        path
        for path in list(root.rglob("*.safetensors")) + list(root.rglob("*.onnx"))
        if ".venv" not in path.parts and "site-packages" not in path.parts
    ]
    assert weights == []
