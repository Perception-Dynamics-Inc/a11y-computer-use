"""Hermetic checks for tree, then an OCR word, then Holo.

No weights are downloaded. OCR and the grounding model are stubs.
"""

from __future__ import annotations

import io
import os
import sys

from a11y_computer_use import ocr
from a11y_computer_use.agent import Agent
from a11y_computer_use.agent.models.base import ToolCall
from a11y_computer_use.agent.models.scripted import ScriptedModel
from a11y_computer_use.agent.vision_order import (
    OcrWord,
    TreeBox,
    cap_worker_threads,
    labels_from_instruction,
    match_ocr_word,
    snap_point,
    tree_for_labels,
    words_from_spans,
)
from a11y_computer_use.schema import Bounds
from tests.test_agent_core import FakeRuntime, el, turn, window

PNG = b"\x89PNG\r\n\x1a\nfake"


def _png(width: int, height: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (20, 20, 20)).save(buf, format="PNG")
    return buf.getvalue()


def _word(text: str, x: int, y: int, w: int, h: int, display: int = 0) -> dict:
    return {
        "text": text,
        "bounds": {"display_id": display, "x": x, "y": y, "width": w, "height": h},
    }


def test_labels_keep_quoted_phrases_and_drop_generic_words() -> None:
    labels = labels_from_instruction('Click the green "Pay now" button')
    assert labels[0] == "Pay now"
    assert "click" not in {item.casefold() for item in labels}
    assert "green" not in {item.casefold() for item in labels}
    assert "button" not in {item.casefold() for item in labels}


def test_ocr_span_keeps_each_input_box_and_the_line_is_not_a_word() -> None:
    spans = ocr.ocr(
        _png(200, 40),
        engine=ocr.FakeOcr([
            ocr.TextBox("Saved", 10, 8, 40, 16, 0.8),
            ocr.TextBox("Messages", 54, 8, 70, 16, 0.7),
        ]),
    )
    assert len(spans) == 1
    words = spans[0]["words"]
    assert [item["text"] for item in words] == ["Saved", "Messages"]
    assert words[0]["bounds"]["width"] == 40
    assert spans[0]["bounds"]["width"] == 114
    parsed = words_from_spans(spans)
    assert [word.text for word in parsed] == ["Saved", "Messages"]
    line_only = words_from_spans([{
        "text": "Saved Messages",
        "bounds": {"display_id": 0, "x": 10, "y": 8, "width": 114, "height": 16},
    }])
    assert line_only == []


def test_match_clicks_the_word_on_the_matching_line_not_the_union() -> None:
    words = words_from_spans([{
        "text": "Saved Messages",
        "bounds": {"display_id": 0, "x": 10, "y": 8, "width": 114, "height": 16},
        "words": [
            _word("Saved", 10, 8, 40, 16),
            _word("Messages", 54, 8, 70, 16),
        ],
    }])
    hit = match_ocr_word(words, ["Messages"])
    assert hit is not None
    assert hit.text == "Messages"
    assert hit.center() == (54 + 70 // 2, 8 + 16 // 2)
    assert hit.center() != (10 + 114 // 2, 8 + 16 // 2)


def test_tree_match_is_the_smallest_exact_title() -> None:
    boxes = [
        TreeBox("e9", "Save", 0, 0, 200, 40),
        TreeBox("e2", "Save", 0, 0, 40, 16),
        TreeBox("e3", "Cancel", 0, 0, 10, 10),
    ]
    hit = tree_for_labels(boxes, ["save"])
    assert hit is not None and hit.ref == "e2"


def test_snap_rejects_a_box_wider_than_200_or_taller_than_40() -> None:
    wide = TreeBox("e1", "Row", 0, 0, 400, 20)
    tall = OcrWord("Icon", 0, 0, 20, 80)
    small = OcrWord("Save", 10, 10, 30, 16)
    assert snap_point(20, 10, [wide], [tall]) is None
    snapped = snap_point(12, 12, [wide], [small])
    assert snapped is not None
    assert snapped[0] == "word"
    assert (snapped[1], snapped[2]) == small.center()


def test_thread_cap_does_not_import_torch_and_does_not_raise_a_lower_pool(monkeypatch) -> None:
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        monkeypatch.delenv(name, raising=False)
    sentinel = object()
    previous = sys.modules.get("torch", sentinel)
    sys.modules.pop("torch", None)
    try:
        assert cap_worker_threads(32) == 4
        assert os.environ["OMP_NUM_THREADS"] == "4"
        assert "torch" not in sys.modules
    finally:
        if previous is sentinel:
            sys.modules.pop("torch", None)
        else:
            sys.modules["torch"] = previous

    monkeypatch.setenv("OMP_NUM_THREADS", "2")
    monkeypatch.setenv("MKL_NUM_THREADS", "8")
    seen: list[int] = []

    class _Torch:
        @staticmethod
        def set_num_threads(count: int) -> None:
            seen.append(count)

    monkeypatch.setitem(sys.modules, "torch", _Torch)
    assert cap_worker_threads() == 2
    assert os.environ["OMP_NUM_THREADS"] == "2"
    assert os.environ["MKL_NUM_THREADS"] == "2"
    assert os.environ["OPENBLAS_NUM_THREADS"] == "2"
    assert seen == [2]


def _run(goal: str, elements, calls, **kwargs):
    runtime = kwargs.pop("runtime", None) or FakeRuntime(elements)
    events: list = []
    agent = Agent(
        ScriptedModel(calls if callable(calls) else [calls]),
        runtime=runtime,
        on_event=events.append,
        trace_dir=kwargs.pop("trace_dir", None),
        **kwargs,
    )
    result = agent.run(goal)
    return result, runtime


def test_tree_match_clicks_the_ref_and_does_not_call_ocr(monkeypatch) -> None:
    def fail_ocr(*_args, **_kwargs):
        raise AssertionError("OCR runs only after the tree misses")

    monkeypatch.setattr(ocr, "ocr", fail_ocr)
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    result, runtime = _run(
        'press "Save"',
        elements,
        turn(ToolCall("click", {"ref": "e999"})),
        max_steps=1,
    )
    assert runtime.calls == [("click", {"ref": "e2"})]
    assert result.steps == 1


def test_existing_ref_is_not_rewritten(monkeypatch) -> None:
    def fail_ocr(*_args, **_kwargs):
        raise AssertionError("a ref in the tree is the click")

    monkeypatch.setattr(ocr, "ocr", fail_ocr)
    elements = window(
        el("e2", "AXButton", "Save", parent="e1", clickable=True),
        el("e3", "AXButton", "Other", parent="e1", clickable=True),
    )
    _result, runtime = _run(
        'press "Other"',
        elements,
        turn(ToolCall("click", {"ref": "e2"})),
        max_steps=1,
    )
    assert runtime.calls[0] == ("click", {"ref": "e2"})


def test_ocr_word_is_clicked_and_holo_is_not_called(monkeypatch) -> None:
    called: list[str] = []

    def fake_ocr(_image, **_kwargs):
        return [{
            "text": "Saved Messages",
            "bounds": {"display_id": 0, "x": 10, "y": 8, "width": 114, "height": 16},
            "words": [
                _word("Saved", 10, 8, 40, 16),
                _word("Messages", 54, 8, 70, 16),
            ],
        }]

    class _Ground:
        def ground(self, png, instruction):
            del png, instruction
            called.append("holo")
            raise AssertionError("Holo runs only when OCR finds no word")

    monkeypatch.setattr(ocr, "ocr", fake_ocr)
    elements = window(el("e2", "AXButton", "Other", parent="e1", clickable=True))
    _result, runtime = _run(
        'open "Messages"',
        elements,
        turn(ToolCall("click", {"ref": "e999"})),
        grounding=_Ground(),
        max_steps=1,
    )
    assert called == []
    assert runtime.calls == [("click", {"x": 54 + 35, "y": 8 + 8, "display_id": 0})]


def test_a_line_box_is_not_the_click_so_holo_runs(monkeypatch) -> None:
    seen: list[tuple] = []

    def fake_ocr(_image, **_kwargs):
        return [{
            "text": "Saved Messages",
            "bounds": {"display_id": 0, "x": 10, "y": 8, "width": 400, "height": 16},
        }]

    class _Ground:
        def ground(self, png, instruction):
            seen.append((png, instruction))
            return type("Hit", (), {"x": 15, "y": 12})()

    monkeypatch.setattr(ocr, "ocr", fake_ocr)
    elements = window(el("e2", "opaque_region", "", parent="e1", bounds=Bounds(0, 0, 0, 400, 300)))
    _result, runtime = _run(
        'open "Messages"',
        elements,
        turn(ToolCall("click", {"ref": "e999"})),
        grounding=_Ground(),
        max_steps=1,
    )
    assert seen and seen[0][1] == 'open "Messages"'
    assert runtime.calls == [("click", {"x": 15, "y": 12, "display_id": 0})]


def test_model_point_is_kept_when_ocr_misses_and_holo_is_not_called(monkeypatch) -> None:
    def fake_ocr(_image, **_kwargs):
        return [{
            "text": "Ready",
            "words": [_word("Ready", 1, 1, 20, 10)],
        }]

    class _Ground:
        def ground(self, png, instruction):
            del png, instruction
            raise AssertionError("a planner point is not replaced by Holo")

    monkeypatch.setattr(ocr, "ocr", fake_ocr)
    elements = window(el("e2", "AXButton", "Ready", parent="e1", clickable=True))
    _result, runtime = _run(
        "Click the red square drawn inside the canvas.",
        elements,
        turn(ToolCall("click", {"x": 220, "y": 120, "display_id": 0})),
        grounding=_Ground(),
        max_steps=1,
    )
    assert runtime.calls == [("click", {"x": 220, "y": 120, "display_id": 0})]


def test_holo_snaps_only_into_a_small_tree_or_word_box(monkeypatch) -> None:
    def no_text(_image, **_kwargs):
        return []

    class _Ground:
        def ground(self, png, instruction):
            del png, instruction
            return {"x": 15, "y": 15}

    monkeypatch.setattr(ocr, "ocr", no_text)
    small = el(
        "e2", "AXButton", "Mark", parent="e1", clickable=True,
        bounds=Bounds(0, 10, 10, 40, 20),
    )
    _result, runtime = _run(
        "choose the icon",
        window(small),
        turn(ToolCall("click", {"ref": "e999"})),
        grounding=_Ground(),
        max_steps=1,
    )
    assert runtime.calls == [("click", {"x": 30, "y": 20, "display_id": 0})]

    wide = el(
        "e2", "AXButton", "Banner", parent="e1", clickable=True,
        bounds=Bounds(0, 0, 0, 400, 30),
    )
    _result, runtime = _run(
        "choose the icon",
        window(wide),
        turn(ToolCall("click", {"ref": "e999"})),
        grounding=_Ground(),
        max_steps=1,
    )
    assert runtime.calls == [("click", {"x": 15, "y": 15, "display_id": 0})]


def test_holo_point_is_mapped_with_the_screenshot_scale(monkeypatch) -> None:
    def no_text(_image, **_kwargs):
        return []

    class _Image:
        png = PNG
        width = 100
        height = 50
        source_width = 200
        source_height = 100

        def to_source(self, x: int, y: int) -> tuple[int, int]:
            return x * 2, y * 2

    class _Ground:
        def ground(self, png, instruction):
            assert png == PNG
            assert instruction == "choose the icon"
            return {"x": 10, "y": 5}

    monkeypatch.setattr(ocr, "ocr", no_text)
    runtime = FakeRuntime(window(el("e2", "AXButton", "Other", parent="e1", clickable=True)))
    runtime.screenshot = lambda **_kwargs: ("display 3: 100x50 px image", _Image())  # type: ignore[method-assign]
    _result, runtime = _run(
        "choose the icon",
        runtime.elements,
        turn(ToolCall("click", {"ref": "e999"})),
        runtime=runtime,
        grounding=_Ground(),
        max_steps=1,
    )
    assert runtime.calls == [("click", {"x": 20, "y": 10, "display_id": 3})]


def test_tree_payment_label_stops_before_the_click_is_sent() -> None:
    pay = el(
        "e2", "AXButton", "Pay now", parent="e1", clickable=True,
        bounds=Bounds(0, 100, 200, 80, 40),
    )
    result, runtime = _run(
        'press "Pay now"',
        window(pay),
        turn(ToolCall("click", {"ref": "e999"})),
        max_steps=1,
    )
    assert result.status == "needs_human", result
    assert result.needs_human["kind"] == "payment"
    assert runtime.calls == []
