"""Hermetic checks for click resolution and OCR word boxes.

The agent does not choose a click from the goal. OCR and the grounding
model are stubs. No weights are downloaded.
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
from tests.test_agent_core import FakeRuntime, _look_rows, _proposed_pixel, _solid_png, el, turn, window

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
        ScriptedModel(calls if callable(calls) or isinstance(calls, list) else [calls]),
        runtime=runtime,
        on_event=events.append,
        trace_dir=kwargs.pop("trace_dir", None),
        **kwargs,
    )
    result = agent.run(goal)
    return result, runtime


def test_goal_text_does_not_click_archive(monkeypatch) -> None:
    """'Do not click Archive' is not a target. A missing ref stays missing."""
    def fail_ocr(*_args, **_kwargs):
        raise AssertionError("goal text must not start OCR")

    monkeypatch.setattr(ocr, "ocr", fail_ocr)
    elements = window(
        el("e2", "AXButton", "Archive", parent="e1", clickable=True),
        el("e3", "AXButton", "Save draft", parent="e1", clickable=True),
        el("e4", "AXButton", "Chrome", parent="e1", clickable=True),
        el("e5", "AXStaticText", "Draft", parent="e1"),
    )
    result, runtime = _run(
        "Click the Save draft button in Chrome. Do not click Archive.",
        elements,
        turn(ToolCall("click", {"ref": "e990"}), text="click the Archive button"),
        max_steps=1,
    )
    assert runtime.calls == [("click", {"ref": "e990"})]
    assert result.step_log[0].args.get("ref") == "e990"
    assert result.step_log[0].target.get("name") != "Archive"


def test_explicit_canvas_point_is_not_rewritten_to_chrome(monkeypatch) -> None:
    def fail_ocr(*_args, **_kwargs):
        raise AssertionError("an x/y click must not start OCR")

    class _Ground:
        def ground(self, png, instruction):
            del png, instruction
            raise AssertionError("an x/y click must not call Holo")

    monkeypatch.setattr(ocr, "ocr", fail_ocr)
    elements = window(
        el("e18", "AXButton", "Chrome", parent="e1", clickable=True),
        el("e2", "AXButton", "SUBMIT", parent="e1", clickable=True),
        el("e3", "canvas", "pad", parent="e1", bounds=Bounds(0, 400, 80, 400, 300)),
    )
    point = {"x": 532, "y": 205, "display_id": 0}
    _result, runtime = _run(
        "In the Chrome window, click the green SUBMIT button drawn on the canvas.",
        elements,
        turn(ToolCall("click", point)),
        grounding=_Ground(),
        max_steps=1,
        look_again=False,
    )
    assert runtime.calls == [("click", point)]


def test_ref_plus_point_is_invalid_arguments() -> None:
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    for name in ("click", "right_click", "double_click"):
        result, runtime = _run(
            "Click Save. Do not click Upload.",
            elements,
            turn(ToolCall(name, {"ref": "e2", "x": 10, "y": 20})),
            max_steps=1,
        )
        assert runtime.calls == [], name
        error = result.step_log[0].error or ""
        assert error.startswith(f"invalid_arguments: {name}:"), error
        assert "ref and x/y" in error


def test_missing_ref_resolves_only_from_its_last_known_name(tmp_path) -> None:
    """A rebuilt button keeps its ref. The goal word is not a target."""
    save = el("e2", "AXButton", "Save draft", parent="e1", clickable=True)
    ping = el("e4", "AXButton", "Ping", parent="e1", clickable=True)
    archive = el("e3", "AXButton", "Archive", parent="e1", clickable=True)
    runtime = FakeRuntime(window(save, ping, archive))

    def on_call(name, params, rt):
        if name == "click" and params.get("ref") == "e4":
            rt.elements = [
                item for item in rt.elements if item.ref != "e2"
            ]
            rt.elements.append(el("e5", "AXButton", "Save draft", parent="e1", clickable=True))

    runtime.on_call = on_call
    result, runtime = _run(
        "Click the Save draft button in Chrome. Do not click Archive.",
        runtime.elements,
        [
            turn(ToolCall("click", {"ref": "e4"})),
            turn(ToolCall("click", {"ref": "e2"})),
        ],
        runtime=runtime,
        trace_dir=tmp_path,
        max_steps=2,
    )
    # The raw walk named the rebuilt button e5. Alignment keeps e2, so the
    # click stays on Save draft and does not fall through to Archive.
    assert runtime.calls == [
        ("click", {"ref": "e4"}),
        ("click", {"ref": "e2"}),
    ]
    assert all(call[1].get("ref") != "e3" for call in runtime.calls)
    assert result.step_log[1].target.get("name") == "Save draft"
    assert "resolved e2" not in (result.step_log[1].result or "")
    rows = [
        __import__("json").loads(line)
        for line in (tmp_path / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    click = next(row for row in rows if row.get("action") == "click" and row["args"].get("ref") == "e2")
    assert click["requested"] == {"name": "click", "args": {"ref": "e2"}}
    assert "resolved_from" not in click


def test_a_ref_alignment_cannot_keep_resolves_from_its_last_known_name(tmp_path) -> None:
    """Two same-named buttons are not one control. The surviving name is the target."""
    first = el("e2", "AXButton", "Save draft", parent="e1", clickable=True, path=("AXWindow", "AXButton", "0"))
    other = el("e6", "AXButton", "Save draft", parent="e1", clickable=True, path=("AXWindow", "AXButton", "1"))
    ping = el("e4", "AXButton", "Ping", parent="e1", clickable=True, path=("AXWindow", "AXButton", "2"))
    archive = el("e3", "AXButton", "Archive", parent="e1", clickable=True, path=("AXWindow", "AXButton", "3"))
    runtime = FakeRuntime(window(first, other, ping, archive))

    def on_call(name, params, rt):
        if name == "click" and params.get("ref") == "e4":
            rt.elements = [item for item in rt.elements if item.ref != "e2"]

    runtime.on_call = on_call
    result, runtime = _run(
        "Click the Save draft button in Chrome. Do not click Archive.",
        runtime.elements,
        [
            turn(ToolCall("click", {"ref": "e4"})),
            turn(ToolCall("click", {"ref": "e2"})),
        ],
        runtime=runtime,
        trace_dir=tmp_path,
        max_steps=2,
    )
    assert runtime.calls == [
        ("click", {"ref": "e4"}),
        ("click", {"ref": "e6"}),
    ]
    assert all(call[1].get("ref") != "e3" for call in runtime.calls)
    note = "resolved e2 from last-known AXButton 'Save draft' to e6"
    assert note in (result.step_log[1].result or "")
    rows = [
        __import__("json").loads(line)
        for line in (tmp_path / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    click = next(row for row in rows if row.get("action") == "click" and row["args"].get("ref") == "e6")
    assert click["requested"] == {"name": "click", "args": {"ref": "e2"}}
    assert click["resolved_from"] == note


def test_b5_and_d3_coordinate_clicks_stay_on_their_points(monkeypatch) -> None:
    """Benchmark-style x/y clicks are not rewritten onto a goal word."""
    def fail_ocr(*_args, **_kwargs):
        raise AssertionError("an x/y click must not start OCR")

    monkeypatch.setattr(ocr, "ocr", fail_ocr)
    elements = window(
        el("e2", "AXButton", "B5", parent="e1", clickable=True),
        el("e3", "AXButton", "Chrome", parent="e1", clickable=True),
        el("e4", "AXButton", "Archive", parent="e1", clickable=True),
        el("e5", "AXStaticText", "D3", parent="e1"),
    )
    b5 = {"x": 640, "y": 360, "display_id": 0}
    _result, runtime = _run(
        "Open the B5 total in Chrome.",
        elements,
        turn(ToolCall("click", b5)),
        max_steps=1,
        look_again=False,
    )
    assert runtime.calls == [("click", b5)]

    d3 = {"x": 120, "y": 88, "display_id": 0}
    _result, runtime = _run(
        "Set D3. Do not click Archive.",
        elements,
        turn(ToolCall("click", d3)),
        max_steps=1,
        look_again=False,
    )
    assert runtime.calls == [("click", d3)]


def test_double_click_point_is_not_rewritten_to_upload(monkeypatch) -> None:
    """B5's x/y double-click stays on that point. Upload in the goal is not the target.

    The same point rule covers right_click and the other pointer gestures,
    and a scroll at x/y. A name the loop does not know is an error, not a
    click on Upload.
    """
    def fail_ocr(*_args, **_kwargs):
        raise AssertionError("an x/y double-click must not start OCR")

    monkeypatch.setattr(ocr, "ocr", fail_ocr)
    elements = window(
        el("e2", "AXButton", "Upload", parent="e1", clickable=True),
        el("e3", "AXButton", "B5", parent="e1", clickable=True),
        el("e4", "AXButton", "Chrome", parent="e1", clickable=True),
    )
    point = {"x": 640, "y": 360, "display_id": 0}
    goal = "Open the B5 total in Chrome. Do not click Upload."
    gestures = {
        "double_click": {"button": "left", "count": 2},
        "right_click": {"button": "right", "count": 1},
        "middle_click": {"button": "middle", "count": 1},
        "triple_click": {"button": "left", "count": 3},
        "left_click": {"button": "left", "count": 1},
    }
    for name, gesture in gestures.items():
        _result, runtime = _run(
            goal,
            elements,
            turn(ToolCall(name, {**point, "button": "right", "count": 1})),
            max_steps=1,
            look_again=False,
        )
        assert runtime.calls == [("click", {**point, **gesture})], name
        assert all(call[1].get("ref") != "e2" for call in runtime.calls)

    _result, runtime = _run(
        goal,
        elements,
        turn(ToolCall("scroll", {**point, "dy": -3})),
        max_steps=1,
        look_again=False,
    )
    assert runtime.calls == [("scroll", {**point, "dx": 0, "dy": -3})]
    assert all(call[0] != "click" for call in runtime.calls)

    for name in ("drag", "hover", "mouse_move"):
        result, runtime = _run(
            goal,
            elements,
            turn(ToolCall(name, point)),
            max_steps=1,
            look_again=False,
        )
        assert runtime.calls == [], name
        assert result.step_log[0].error == f"unknown action {name!r}"

    rejected, runtime = _run(
        goal,
        elements,
        turn(ToolCall("double_click", {"ref": "e2", "x": 640, "y": 360})),
        max_steps=1,
    )
    assert runtime.calls == []
    error = rejected.step_log[0].error or ""
    assert error.startswith("invalid_arguments: double_click:")
    assert "ref and x/y" in error


def test_explicit_point_survives_look_again_snap_and_retry(tmp_path, monkeypatch) -> None:
    """Look-again, a snap, and a retry leave a model x/y click on that point.

    Upload is focused and named by the goal. The window contains the point,
    so escalation would otherwise move it to the window center or press
    Return. The verifier asks for a different pixel. The click that is sent
    stays, for a single click, a double-click, and a right-click.
    """
    from a11y_computer_use.agent.grounding import GroundingHit

    def upload_word(_image, **_kwargs):
        return [{
            "text": "Upload",
            "bounds": {"display_id": 0, "x": 10, "y": 10, "width": 40, "height": 20},
            "confidence": 0.9,
        }]

    monkeypatch.setattr(ocr, "ocr", upload_word)
    elements = [
        el("e1", "AXWindow", "Chrome", bounds=Bounds(0, 0, 0, 1280, 800)),
        el(
            "e2", "AXButton", "Upload", parent="e1", clickable=True, focused=True,
            bounds=Bounds(0, 10, 10, 40, 20),
        ),
    ]
    point = {"x": 640, "y": 360, "display_id": 0}
    goal = "Open the B5 total in Chrome. Do not click Upload."
    gestures = {
        "click": {},
        "double_click": {"button": "left", "count": 2},
        "right_click": {"button": "right", "count": 1},
    }

    class _Shift:
        def __init__(self) -> None:
            self.seen = 0

        def ground(self, png, instruction):
            del png
            self.seen += 1
            mx, my = _proposed_pixel(instruction)
            return GroundingHit(mx + 80, my + 80)

    for name, gesture in gestures.items():
        runtime = FakeRuntime(elements)
        runtime.png = _solid_png(1280, 800)
        ground = _Shift()
        args = {**point, "button": "right", "count": 1} if gesture else dict(point)
        _result, runtime = _run(
            goal,
            elements,
            turn(ToolCall(name, args)),
            runtime=runtime,
            grounding=ground,
            trace_dir=tmp_path / name,
            max_retries=2,
            max_steps=1,
        )
        expected = {**point, **gesture} if gesture else dict(point)
        assert runtime.calls == [("click", expected)], (name, runtime.calls)
        assert all(call[0] != "key" for call in runtime.calls)
        assert all(call[1].get("ref") != "e2" for call in runtime.calls)
        assert ground.seen >= 1, name
        notes = _look_rows(tmp_path / name)
        assert notes, name
        assert notes[0]["proposed"] == point
        assert all(call[1].get("x") == 640 and call[1].get("y") == 360 for call in runtime.calls)


def test_pointer_gestures_still_stop_for_payment_and_send() -> None:
    """A double-click on Pay now and a right-click on Send use the click gates."""
    pay = window(el("e2", "AXButton", "Pay now", parent="e1", clickable=True))
    stopped, runtime = _run(
        "finish the form",
        pay,
        turn(ToolCall("double_click", {"ref": "e2"})),
        max_steps=1,
    )
    assert stopped.status == "needs_human", stopped
    assert stopped.needs_human["kind"] == "payment"
    assert runtime.calls == []

    send = window(el("e2", "AXButton", "Send", parent="e1", clickable=True))
    refused, runtime = _run(
        "finish the form",
        send,
        turn(ToolCall("right_click", {"ref": "e2"})),
        max_steps=2,
    )
    assert runtime.calls == []
    assert any("approval_denied" in (step.error or "") for step in refused.step_log)


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


def test_a_goal_word_does_not_call_holo(monkeypatch) -> None:
    def fake_ocr(_image, **_kwargs):
        raise AssertionError("a missing ref with no last-known name does not OCR")

    class _Ground:
        def ground(self, png, instruction):
            del png, instruction
            raise AssertionError("a missing ref is not grounded from the goal")

    monkeypatch.setattr(ocr, "ocr", fake_ocr)
    elements = window(el("e2", "AXButton", "Messages", parent="e1", clickable=True))
    _result, runtime = _run(
        'open "Messages"',
        elements,
        turn(ToolCall("click", {"ref": "e999"})),
        grounding=_Ground(),
        max_steps=1,
    )
    assert runtime.calls == [("click", {"ref": "e999"})]


def test_ambiguous_and_inexact_names_are_errors() -> None:
    elements = window(
        el("e2", "AXButton", "Save", parent="e1", clickable=True),
        el("e3", "AXButton", "Save", parent="e1", clickable=True),
        el("e4", "AXButton", "Archive now", parent="e1", clickable=True),
    )
    ambiguous, runtime = _run(
        "Do not click Archive.",
        elements,
        turn(ToolCall("click", {"label": "Save"})),
        max_steps=1,
    )
    assert runtime.calls == []
    assert "2 elements have the exact name 'Save'" in (ambiguous.step_log[0].error or "")

    missing, runtime = _run(
        "Do not click Archive.",
        elements,
        turn(ToolCall("click", {"label": "Archive"})),
        max_steps=1,
    )
    assert runtime.calls == []
    assert "no element has the exact name 'Archive'" in (missing.step_log[0].error or "")


def test_resolved_payment_and_send_still_stop(tmp_path) -> None:
    pay = el("e2", "AXButton", "Pay now", parent="e1", clickable=True)
    other = el("e4", "AXButton", "Ping", parent="e1", clickable=True)
    runtime = FakeRuntime(window(pay, other))

    def swap_pay(name, params, rt):
        if name == "click" and params.get("ref") == "e4":
            rt.elements = [item for item in rt.elements if item.ref != "e2"]
            rt.elements.append(el("e9", "AXButton", "Pay now", parent="e1", clickable=True))

    runtime.on_call = swap_pay
    stopped, runtime = _run(
        "finish the form",
        runtime.elements,
        [
            turn(ToolCall("click", {"ref": "e4"})),
            turn(ToolCall("click", {"ref": "e2"})),
        ],
        runtime=runtime,
        max_steps=2,
    )
    assert stopped.status == "needs_human", stopped
    assert stopped.needs_human["kind"] == "payment"
    assert "payment" in stopped.reason
    assert "Pay now" in stopped.reason
    # The rebuilt Pay now keeps e2, and that click is stopped before it runs.
    assert [call for call in runtime.calls if call[1].get("ref") in {"e2", "e9"}] == []

    send = el("e2", "AXButton", "Send", parent="e1", clickable=True)
    ping = el("e4", "AXButton", "Ping", parent="e1", clickable=True)
    runtime = FakeRuntime(window(send, ping))

    def swap_send(name, params, rt):
        if name == "click" and params.get("ref") == "e4":
            rt.elements = [item for item in rt.elements if item.ref != "e2"]
            rt.elements.append(el("e9", "AXButton", "Send", parent="e1", clickable=True))

    runtime.on_call = swap_send
    refused, runtime = _run(
        "finish the form",
        runtime.elements,
        [
            turn(ToolCall("click", {"ref": "e4"})),
            turn(ToolCall("click", {"ref": "e2"})),
            turn(ToolCall("done", {"answer": "held", "conditions": [{"window_title_contains": "Demo"}]})),
        ],
        runtime=runtime,
        trace_dir=tmp_path,
        max_steps=3,
    )
    assert [call for call in runtime.calls if call[1].get("ref") in {"e2", "e9"}] == []
    assert any(step.target.get("name") == "Send" for step in refused.step_log)
    assert any("approval_denied" in (step.error or "") for step in refused.step_log)
