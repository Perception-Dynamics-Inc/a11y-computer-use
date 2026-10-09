"""Hermetic tests for the goal-running agent. ScriptedModel and a fake runtime only."""

from __future__ import annotations

import dataclasses
import json
import os
from types import SimpleNamespace

from a11y_computer_use.agent import Agent, ReservedPermission, tool_schemas
from a11y_computer_use.agent.core import check_conditions
from a11y_computer_use.agent.models.base import ModelTurn, ToolCall
from a11y_computer_use.agent.models.scripted import ScriptedModel
from a11y_computer_use.observe import render_text
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, ErrorCode, Scope, Snapshot

PNG = b"\x89PNG\r\n\x1a\nfake"


def el(ref, role, title, **kwargs) -> Element:
    parent = kwargs.pop("parent", None)
    bounds = kwargs.pop("bounds", Bounds(0, 20, 30, 80, 24))
    return Element(
        ref=ref,
        role=role,
        title=title,
        value=kwargs.pop("value", None),
        bounds=bounds,
        snapshot_id="snap",
        parent=parent,
        **kwargs,
    )


def window(*children: Element, title: str = "Demo") -> list[Element]:
    root = el("e1", "AXWindow", title)
    return [root, *children]


class FakeRuntime:
    """Records tool calls and serves one mutable accessibility tree."""

    def __init__(self, elements: list[Element]):
        self.elements = list(elements)
        self.calls: list[tuple[str, dict]] = []
        self._current: Snapshot | None = None
        self.driver = SimpleNamespace(name="fake")
        self.fail: dict[str, Exception] = {}
        self.on_call = None
        self.png = PNG

    def _frontmost(self) -> str:
        return "demo"

    def desktop_snapshot(self, app, **kwargs) -> str:
        del app, kwargs
        self._current = Snapshot("snap", Scope.WINDOW, "demo", 1, 0.0, (), tuple(self.elements))
        return render_text(self._current)

    def call_tool(self, name, params, confirm=None):
        del confirm
        self.calls.append((name, dict(params)))
        ref = params.get("ref")
        if ref in self.fail:
            raise self.fail[ref]
        if self.on_call is not None:
            self.on_call(name, params, self)
        return f"{name} ok"

    def screenshot(self, **kwargs):
        del kwargs
        return ("shot", SimpleNamespace(png=self.png))


def replace_ref(runtime: FakeRuntime, ref: str, **changes) -> None:
    runtime.elements = [
        dataclasses.replace(item, **changes) if item.ref == ref else item
        for item in runtime.elements
    ]


def done(answer: str, conditions: list[dict], call_id: str | None = None) -> ToolCall:
    return ToolCall("done", {"answer": answer, "conditions": conditions}, id=call_id)


def turn(*calls: ToolCall, text: str = "") -> ModelTurn:
    return ModelTurn(calls=list(calls), text=text)


def run(model, elements, **kwargs):
    events: list = []
    runtime = kwargs.pop("runtime", None) or FakeRuntime(elements)
    trace = kwargs.pop("trace_dir", None)
    agent = Agent(model, runtime=runtime, trace_dir=trace, on_event=events.append, **kwargs)
    result = agent.run("finish the form")
    return result, events, runtime, agent


def types(events) -> list[str]:
    return [event.type for event in events]


def test_done_only_event_order(tmp_path):
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    result, events, runtime, _agent = run(
        ScriptedModel([turn(done("saved", [{"element": {"role": "AXButton", "name": "Save"}}]))]),
        elements,
        trace_dir=tmp_path,
    )
    assert result.status == "success"
    assert result.reason == "done"
    assert result.answer == "saved"
    assert result.steps == 1
    assert isinstance(result.steps, int)
    assert types(events) == [
        "observation", "plan", "step_started", "observation", "action", "step_finished", "done",
    ]
    assert runtime.calls == []
    assert result.conditions[0]["ok"] is True


def test_click_then_done_event_order(tmp_path):
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))

    def on_call(name, params, runtime):
        if name == "click":
            replace_ref(runtime, "e2", focused=True)

    runtime = FakeRuntime(elements)
    runtime.on_call = on_call
    result, events, runtime, _agent = run(
        ScriptedModel([
            turn(ToolCall("click", {"ref": "e2"})),
            turn(done("saved", [{"element": {"role": "button", "name": "Save"}}])),
        ]),
        elements,
        runtime=runtime,
        trace_dir=tmp_path,
    )
    assert result.status == "success"
    assert result.steps == 2
    assert types(events) == [
        "observation", "plan", "step_started", "action", "observation", "step_finished",
        "observation", "plan", "step_started", "observation", "action", "step_finished", "done",
    ]


def test_one_turn_runs_calls_one_at_a_time(tmp_path):
    elements = window(
        el("e2", "AXButton", "Save", parent="e1", clickable=True),
        el("e3", "AXTextField", "Note", parent="e1", editable=True, value=""),
    )

    def on_call(name, params, runtime):
        if name == "set_value":
            replace_ref(runtime, params["ref"], value=params["value"])

    runtime = FakeRuntime(elements)
    runtime.on_call = on_call
    result, _events, runtime, _agent = run(
        ScriptedModel([turn(
            ToolCall("set_value", {"ref": "e3", "value": "hello"}),
            ToolCall("click", {"ref": "e2"}),
            done("typed", [{"value": {"ref": "e3", "equals": "hello"}}]),
        )]),
        elements,
        runtime=runtime,
        trace_dir=tmp_path,
    )
    assert [name for name, _params in runtime.calls] == ["set_value", "click"]
    assert result.status == "success"
    assert result.steps == 3
    assert result.conditions[0]["ok"] is True
    assert result.step_log[0].action == "set_value"
    assert result.step_log[1].action == "click"
    assert result.step_log[2].action == "done"


def test_max_steps_stops_before_a_third_observation():
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    result, events, runtime, _agent = run(
        ScriptedModel(lambda _messages: turn(ToolCall("click", {"ref": "e2"}))),
        elements,
        max_steps=2,
    )
    assert result.status == "failed"
    assert result.reason == "max_steps"
    assert result.steps == 2
    assert len(runtime.calls) == 2
    # Two plan-time observations and one post-action observation per click.
    # The third turn is refused before it observes.
    assert types(events).count("observation") == 4


def test_max_time_zero_stops_immediately():
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    called = {"n": 0}

    def script(_messages):
        called["n"] += 1
        return turn(ToolCall("click", {"ref": "e2"}))

    result, events, runtime, _agent = run(ScriptedModel(script), elements, max_time_s=0)
    assert result.reason == "max_time"
    assert result.steps == 0
    assert events == []
    assert runtime.calls == []
    assert called["n"] == 0


def test_cancel_before_the_click():
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    holder: dict = {}

    def script(_messages):
        holder["agent"].cancel()
        return turn(ToolCall("click", {"ref": "e2"}))

    runtime = FakeRuntime(elements)
    agent = Agent(ScriptedModel(script), runtime=runtime)
    holder["agent"] = agent
    events = []
    agent.on_event = events.append
    result = agent.run("click save")
    assert result.reason == "cancelled"
    assert result.status == "failed"
    assert runtime.calls == []
    assert "error" in types(events)


def test_approve_deny_then_done(tmp_path):
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    seen: list[str] = []

    def approve(action):
        seen.append(action.name)
        return False

    result, _events, runtime, _agent = run(
        ScriptedModel([
            turn(ToolCall("app", {"action": "quit", "name": "demo"})),
            turn(done("left open", [{"window_title_contains": "Demo"}])),
        ]),
        elements,
        approve=approve,
        trace_dir=tmp_path,
    )
    assert seen == ["app"]
    assert runtime.calls == []
    assert result.status == "success"
    assert result.step_log[0].error.startswith("approval_denied")
    assert result.step_log[0].verified is False


def test_auto_deny_is_the_default(tmp_path):
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    result, _events, runtime, _agent = run(
        ScriptedModel([
            turn(ToolCall("app", {"action": "quit", "name": "demo"})),
            turn(done("left open", [{"element": {"role": "AXButton", "name": "Save"}}])),
        ]),
        elements,
        trace_dir=tmp_path,
    )
    assert runtime.calls == []
    assert result.status == "success"
    assert result.step_log[0].error.startswith("approval_denied")


def test_needs_human_password_otp_card_and_captcha():
    cases = [
        (el("e2", "AXTextField", "Password", parent="e1", secure=True, editable=True), "login"),
        (el("e2", "AXTextField", "One-time code", parent="e1", editable=True), "2fa"),
        (el("e2", "AXTextField", "Card number", parent="e1", editable=True), "payment"),
        (el("e2", "iframe", "reCAPTCHA", parent="e1"), "captcha"),
    ]
    for field, kind in cases:
        called = {"n": 0}

        def script(_messages, called=called):
            called["n"] += 1
            return turn(ToolCall("click", {"ref": "e2"}))

        result, events, runtime, _agent = run(ScriptedModel(script), window(field))
        assert result.status == "needs_human", kind
        assert result.needs_human["kind"] == kind
        assert result.needs_human["ref"] == "e2"
        assert result.needs_human["window"] == "Demo"
        assert "Secrets are not typed" in result.needs_human["message"]
        assert runtime.calls == []
        assert called["n"] == 0
        assert types(events) == ["observation", "needs_human"]


def test_static_labels_do_not_pause(tmp_path):
    elements = window(
        el("e2", "AXStaticText", "Password", parent="e1"),
        el("e3", "AXStaticText", "reCAPTCHA", parent="e1"),
        el("e4", "AXButton", "Save", parent="e1", clickable=True),
    )
    result, _events, runtime, _agent = run(
        ScriptedModel([turn(done("ok", [{"element": {"role": "AXButton", "name": "Save"}}]))]),
        elements,
        trace_dir=tmp_path,
    )
    assert result.status == "success"
    assert runtime.calls == []


def test_ask_human(tmp_path):
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    result, events, _runtime, _agent = run(
        ScriptedModel([turn(ToolCall("ask_human", {"kind": "other", "message": "which account?"}))]),
        elements,
        trace_dir=tmp_path,
    )
    assert result.status == "needs_human"
    assert result.needs_human["kind"] == "other"
    assert result.needs_human["message"] == "which account?"
    assert result.steps == 1
    assert "needs_human" in types(events)


def test_rejected_done_then_accepted(tmp_path):
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    result, _events, _runtime, _agent = run(
        ScriptedModel([
            turn(done("nope", [{"element": {"role": "AXButton", "name": "Missing"}}])),
            turn(done("yes", [{"element": {"role": "AXButton", "name": "Save"}}])),
        ]),
        elements,
        trace_dir=tmp_path,
    )
    assert result.status == "success"
    assert result.answer == "yes"
    assert result.step_log[0].verified is False
    assert result.step_log[0].error == "evidence_failed"
    assert result.step_log[1].verified is True
    assert result.conditions[0]["ok"] is True
    assert "Save" in result.conditions[0]["detail"]


def test_file_exists_contains(tmp_path, monkeypatch):
    monkeypatch.setenv("A11Y_COMPUTER_USE_ALLOW_ANY_PATH", "1")
    path = tmp_path / "note.txt"
    path.write_text("hello world", encoding="utf-8")
    elements = window()
    result, _events, _runtime, _agent = run(
        ScriptedModel([turn(done("wrote", [{"file_exists": str(path), "contains": "hello"}]))]),
        elements,
        trace_dir=tmp_path / "trace",
    )
    assert result.status == "success"
    assert result.conditions[0]["ok"] is True
    assert "hello" in result.conditions[0]["detail"]


def test_file_exists_outside_home_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("A11Y_COMPUTER_USE_ALLOW_ANY_PATH", raising=False)
    path = tmp_path / "outside.txt"
    path.write_text("secret note", encoding="utf-8")
    # The checker refuses this even though the file is there.
    assert not str(path).startswith(str(os.path.expanduser("~")))
    result, _events, _runtime, _agent = run(
        ScriptedModel([turn(done("wrote", [{"file_exists": str(path), "contains": "secret"}]))]),
        window(),
        trace_dir=tmp_path / "trace",
    )
    assert result.status == "failed"
    assert result.reason == "no_action"
    assert result.conditions[0]["ok"] is False
    assert "home directory" in result.conditions[0]["detail"]
    assert result.step_log[0].error == "evidence_failed"


def test_check_conditions_window_title():
    elements = window(title="Notes — scratch")
    runtime = FakeRuntime(elements)
    runtime.desktop_snapshot("demo")
    checked = check_conditions([{"window_title_contains": "Notes"}], runtime._current)
    assert checked[0]["ok"] is True


def test_stuck_alternate_ref_then_coordinate_then_keyboard():
    save = el("e2", "AXButton", "Save", parent="e1", clickable=True, bounds=Bounds(0, 20, 30, 80, 24))
    other = el("e9", "AXButton", "Save", parent="e1", clickable=True, bounds=Bounds(0, 200, 30, 80, 24))
    result, _events, runtime, _agent = run(
        ScriptedModel(lambda _messages: turn(ToolCall("click", {"ref": "e2"}))),
        window(save, other),
        max_steps=2,
    )
    assert result.reason == "max_steps"
    assert runtime.calls[0] == ("click", {"ref": "e2"})
    assert runtime.calls[1][0] == "click"
    assert runtime.calls[1][1]["ref"] == "e9"

    result, _events, runtime, _agent = run(
        ScriptedModel(lambda _messages: turn(ToolCall("click", {"ref": "e2"}))),
        window(save),
        max_steps=3,
    )
    assert runtime.calls[0] == ("click", {"ref": "e2"})
    assert runtime.calls[1] == ("click", {"x": 60, "y": 42, "display_id": 0})
    assert runtime.calls[2] == ("key", {"chord": "Return"})
    assert result.steps == 3


def test_repeated_screen_becomes_stuck():
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    seen: list[str] = []

    def script(messages):
        seen.extend(
            message.content for message in messages
            if isinstance(message.content, str)
        )
        return turn(ToolCall("click", {"ref": "e2"}))

    result, events, runtime, _agent = run(
        ScriptedModel(script),
        elements,
        max_steps=20,
        max_replans=1,
    )
    assert result.reason == "stuck"
    assert result.status == "failed"
    stuck = [event for event in events if event.type == "stuck"]
    assert stuck[0].data["terminal"] is False
    assert stuck[-1].data["terminal"] is True
    assert any("different strategy" in text for text in seen)
    assert len(runtime.calls) == 5


def test_stale_ref_backtracks_then_succeeds(tmp_path):
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))

    def on_call(name, params, runtime):
        if name == "click" and params.get("ref") == "e2":
            replace_ref(runtime, "e2", focused=True)

    runtime = FakeRuntime(elements)
    runtime.fail["e999"] = ComputerUseError(ErrorCode.STALE_REF, "ref e999 is gone")
    runtime.on_call = on_call
    result, _events, runtime, _agent = run(
        ScriptedModel([
            turn(ToolCall("click", {"ref": "e999"})),
            turn(ToolCall("click", {"ref": "e2"})),
            turn(done("saved", [{"element": {"role": "AXButton", "name": "Save"}}])),
        ]),
        elements,
        runtime=runtime,
        trace_dir=tmp_path,
    )
    assert ("key", {"chord": "Escape"}) in runtime.calls
    assert ("click", {"ref": "e2"}) in runtime.calls
    assert result.status == "success"
    assert result.reason == "done"


def test_vision_attaches_a_window_screenshot(tmp_path):
    elements = window(el("e2", "AXImage", "", parent="e1"))
    captured: list = []

    def script(messages):
        captured.append(messages[-1].content)
        return turn(done("saw it", [{"window_title_contains": "Demo"}]))

    result, _events, _runtime, _agent = run(
        ScriptedModel(script), elements, vision=True, trace_dir=tmp_path,
    )
    assert result.status == "success"
    content = captured[0]
    assert isinstance(content, list)
    image = content[-1]
    assert image["type"] == "image"
    assert image["mime"] == "image/png"
    assert os.path.isfile(image["path"])

    captured.clear()
    run(ScriptedModel(script), elements, vision=False, trace_dir=tmp_path / "off")
    assert isinstance(captured[0], str)


def test_display_sets_environment():
    # Agent writes os.environ directly. Restore it here: monkeypatch.delenv
    # does not record an undo when the variable was already absent, so a
    # later assignment would leak into the rest of the process.
    previous = os.environ.get("DISPLAY")
    os.environ.pop("DISPLAY", None)
    try:
        elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
        run(
            ScriptedModel([turn(done("ok", [{"window_title_contains": "Demo"}]))]),
            elements,
            display=":77",
        )
        assert os.environ["DISPLAY"] == ":77"
    finally:
        if previous is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = previous


def test_trajectory_redacts_card_numbers_and_saves_png(tmp_path):
    pan = "4111111111111111"
    elements = window(el("e3", "AXTextField", "Note", parent="e1", editable=True, value=""))

    def on_call(name, params, runtime):
        if name == "set_value":
            replace_ref(runtime, "e3", value=params["value"])

    runtime = FakeRuntime(elements)
    runtime.on_call = on_call
    result, _events, _runtime, _agent = run(
        ScriptedModel([turn(
            ToolCall("set_value", {"ref": "e3", "value": pan}),
            done("stored", [{"element": {"role": "AXTextField", "name": "Note"}}]),
        )]),
        elements,
        runtime=runtime,
        trace_dir=tmp_path,
    )
    assert result.status == "success"
    trajectory = (tmp_path / "trajectory.jsonl").read_text(encoding="utf-8")
    assert pan not in trajectory
    assert "[REDACTED]" in trajectory
    steps = (tmp_path / "steps.jsonl").read_text(encoding="utf-8")
    assert pan not in steps
    pngs = list(tmp_path.glob("step-*.png"))
    assert pngs
    assert pngs[0].read_bytes().startswith(b"\x89PNG")
    assert result.step_log[0].args["value"] == "[REDACTED]"


def test_scripted_spec_and_schemas(tmp_path):
    script = tmp_path / "turns.json"
    script.write_text(json.dumps({
        "turns": [{
            "text": "finishing",
            "calls": [{
                "name": "done",
                "arguments": {
                    "answer": "saved",
                    "conditions": [{"element": {"role": "AXButton", "name": "Save"}}],
                },
            }],
        }],
    }), encoding="utf-8")
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    result, _events, _runtime, _agent = run(f"scripted:{script}", elements, trace_dir=tmp_path / "trace")
    assert result.status == "success"
    assert result.answer == "saved"
    names = {item["name"] for item in tool_schemas()}
    assert "done" in names
    assert "exec" not in names
    done_schema = next(item for item in tool_schemas() if item["name"] == "done")
    assert "conditions" in done_schema["parameters"]["properties"]
    assert ReservedPermission.EXEC == "exec"
