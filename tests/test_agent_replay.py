"""Skill record and replay. No live model and no live desktop."""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

from a11y_computer_use.agent import Agent
from a11y_computer_use.agent.models.base import ModelTurn, ToolCall
from a11y_computer_use.agent.models.scripted import ScriptedModel
from a11y_computer_use.agent.replay import load_skill, replay_skill, run_with_skill, skill_from_run
from a11y_computer_use.agent.result import RunResult, StepRecord
from a11y_computer_use.observe import render_text
from a11y_computer_use.schema import Bounds, Element, Scope, Snapshot


def el(ref, role, title, **kwargs) -> Element:
    bounds = kwargs.pop("bounds", Bounds(0, 20, 30, 80, 24))
    parent = kwargs.pop("parent", None)
    return Element(
        ref=ref, role=role, title=title, value=kwargs.pop("value", None),
        bounds=bounds, snapshot_id="snap", parent=parent, **kwargs,
    )


def window(*children: Element, title: str = "Demo") -> list[Element]:
    return [el("e1", "AXWindow", title), *children]


class FakeRuntime:
    def __init__(self, elements: list[Element]):
        self.elements = list(elements)
        self.calls: list[tuple[str, dict]] = []
        self._current: Snapshot | None = None
        self.driver = SimpleNamespace(name="fake")

    def _frontmost(self) -> str:
        return "demo"

    def desktop_snapshot(self, app, **kwargs) -> str:
        del app, kwargs
        self._current = Snapshot("snap", Scope.WINDOW, "demo", 1, 0.0, (), tuple(self.elements))
        return render_text(self._current)

    def call_tool(self, name, params, confirm=None):
        del confirm
        self.calls.append((name, dict(params)))
        if self.on_call is not None:
            self.on_call(name, params, self)
        return f"{name} ok"

    on_call = None


def replace_ref(runtime: FakeRuntime, ref: str, **changes) -> None:
    runtime.elements = [
        dataclasses.replace(item, **changes) if item.ref == ref else item
        for item in runtime.elements
    ]


def focus_on_click(name, params, runtime: FakeRuntime) -> None:
    if name == "click":
        replace_ref(runtime, params["ref"], focused=True)
    elif name == "set_value":
        replace_ref(runtime, params["ref"], value=params["value"])


class CountingModel:
    """Fails the test if replay calls the model."""

    def __init__(self) -> None:
        self.calls = 0

    name = "counting"
    supports_images = False

    def complete(self, messages, tools, *, timeout=None):
        del messages, tools, timeout
        self.calls += 1
        raise AssertionError("model called")


def _done(answer: str, conditions: list[dict]) -> ToolCall:
    return ToolCall("done", {"answer": answer, "conditions": conditions})


def test_clean_replay_uses_the_live_ref_and_does_not_call_the_model(tmp_path) -> None:
    """A second run of the same goal clicks the control, not the stored point."""
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    runtime = FakeRuntime(elements)
    runtime.on_call = focus_on_click
    goal = "Press the Save button"
    agent = Agent(
        ScriptedModel([
            ModelTurn(calls=[
                ToolCall("click", {"ref": "e2"}),
                _done("pressed", [{"element": {"role": "AXButton", "name": "Save"}}]),
            ]),
        ]),
        runtime=runtime,
        trace_dir=tmp_path / "learn",
    )
    path = tmp_path / "skill.json"
    learned = run_with_skill(agent, goal, path)
    assert learned.status == "success"
    skill = load_skill(path)
    click = skill["steps"][0]
    assert click["locator"]["role"] == "AXButton"
    assert click["locator"]["name"] == "Save"
    assert "x" not in click["args"]
    assert "y" not in click["args"]
    click["args"] = {"x": 532, "y": 205}
    click["point_dropped"] = True
    path.write_text(json.dumps(skill), encoding="utf-8")

    runtime.elements = window(el("e9", "AXButton", "Save", parent="e1", clickable=True))
    runtime.calls.clear()
    model = CountingModel()
    replayed = run_with_skill(
        Agent(model, runtime=runtime, trace_dir=tmp_path / "replay"),
        goal,
        path,
    )
    assert replayed.status == "success"
    assert replayed.reason == "replay"
    assert model.calls == 0
    assert runtime.calls == [("click", {"ref": "e9"})]
    assert "x" not in runtime.calls[0][1]
    assert "y" not in runtime.calls[0][1]


def test_stored_point_without_a_locator_is_not_clicked(tmp_path) -> None:
    """A coordinate-only step is a divergence. The stored point is not sent."""
    runtime = FakeRuntime(window(el("e2", "AXButton", "Save", parent="e1", clickable=True)))
    skill = {
        "version": 1,
        "goal": "Press the Save button",
        "steps": [
            {"action": "click", "args": {"x": 532, "y": 205}, "locator": None, "point_dropped": True},
            {"action": "done", "args": {"answer": "no", "conditions": [
                {"element": {"role": "AXButton", "name": "Save"}},
            ]}},
        ],
    }
    path = tmp_path / "skill.json"
    path.write_text(json.dumps(skill), encoding="utf-8")
    model = ScriptedModel([
        ModelTurn(calls=[_done("handed", [{"element": {"role": "AXButton", "name": "Save"}}])]),
    ])
    result = run_with_skill(Agent(model, runtime=runtime, trace_dir=tmp_path / "run"), skill["goal"], path)
    assert result.status == "success"
    assert result.reason == "done"
    assert runtime.calls == []
    updated = load_skill(path)
    assert updated["steps"][0]["action"] == "done"
    assert all("x" not in step.get("args", {}) for step in updated["steps"])


def test_reused_ref_is_not_clicked_when_the_name_moved() -> None:
    """e2 now names Discard. Replay clicks the Save button's new ref."""
    runtime = FakeRuntime(window(
        el("e2", "AXButton", "Discard", parent="e1", clickable=True),
        el("e9", "AXButton", "Save", parent="e1", clickable=True),
    ))
    runtime.on_call = focus_on_click
    skill = {
        "version": 1,
        "goal": "Press the Save button",
        "steps": [
            {
                "action": "click",
                "args": {"x": 12, "y": 12},
                "point_dropped": True,
                "locator": {"role": "AXButton", "name": "Save", "ref": "e2"},
            },
            {"action": "done", "args": {"answer": "pressed", "conditions": [
                {"element": {"role": "AXButton", "name": "Save"}},
            ]}},
        ],
    }
    model = CountingModel()
    outcome = replay_skill(Agent(model, runtime=runtime), skill, goal=skill["goal"])
    assert outcome.divergence is None
    assert outcome.result is not None
    assert outcome.result.reason == "replay"
    assert model.calls == 0
    assert runtime.calls == [("click", {"ref": "e9"})]


def test_ambiguous_locator_does_not_click() -> None:
    runtime = FakeRuntime(window(
        el("e2", "AXButton", "Save", parent="e1", clickable=True),
        el("e3", "AXButton", "Save", parent="e1", clickable=True),
    ))
    skill = {
        "version": 1,
        "goal": "Press the Save button",
        "steps": [{
            "action": "click",
            "args": {},
            "locator": {"role": "AXButton", "name": "Save"},
        }],
    }
    model = CountingModel()
    outcome = replay_skill(Agent(model, runtime=runtime), skill, goal=skill["goal"])
    assert outcome.result is None
    assert outcome.divergence is not None
    assert "more than one" in outcome.divergence.reason
    assert runtime.calls == []
    assert model.calls == 0


def test_stable_id_picks_one_of_two_same_names() -> None:
    runtime = FakeRuntime(window(
        el("e2", "AXButton", "Save", parent="e1", clickable=True, stable_id="other"),
        el("e4", "AXButton", "Save", parent="e1", clickable=True, stable_id="save-main"),
    ))
    runtime.on_call = focus_on_click
    skill = {
        "version": 1,
        "goal": "Press the Save button",
        "steps": [
            {
                "action": "click",
                "args": {},
                "locator": {"role": "AXButton", "name": "Save", "stable_id": "save-main", "ref": "e2"},
            },
            {"action": "done", "args": {"answer": "pressed", "conditions": [
                {"element": {"role": "AXButton", "name": "Save"}},
            ]}},
        ],
    }
    outcome = replay_skill(Agent(CountingModel(), runtime=runtime), skill, goal=skill["goal"])
    assert outcome.divergence is None
    assert runtime.calls == [("click", {"ref": "e4"})]


def test_stable_id_whose_name_changed_is_not_clicked() -> None:
    runtime = FakeRuntime(window(
        el("e4", "AXButton", "Discard", parent="e1", clickable=True, stable_id="save-main"),
    ))
    skill = {
        "version": 1,
        "goal": "Press the Save button",
        "steps": [{
            "action": "click",
            "args": {"x": 4, "y": 4},
            "point_dropped": True,
            "locator": {"role": "AXButton", "name": "Save", "stable_id": "save-main"},
        }],
    }
    outcome = replay_skill(Agent(CountingModel(), runtime=runtime), skill, goal=skill["goal"])
    assert outcome.divergence is not None
    assert "name changed" in outcome.divergence.reason
    assert runtime.calls == []


def test_disabled_control_and_canvas_are_not_clicked() -> None:
    disabled = FakeRuntime(window(
        el("e2", "AXButton", "Save", parent="e1", clickable=True, enabled=False),
    ))
    skill = {
        "version": 1,
        "goal": "Press the Save button",
        "steps": [{
            "action": "click",
            "args": {},
            "locator": {"role": "AXButton", "name": "Save"},
        }],
    }
    outcome = replay_skill(Agent(CountingModel(), runtime=disabled), skill, goal=skill["goal"])
    assert outcome.divergence is not None
    assert disabled.calls == []

    canvas = FakeRuntime(window(el("e5", "AXCanvas", "Drawing", parent="e1")))
    pointed = {
        "version": 1,
        "goal": "Press the Save button",
        "steps": [{
            "action": "click",
            "args": {"x": 532, "y": 205},
            "point_dropped": True,
            "locator": {"role": "AXCanvas", "name": "Drawing", "ref": "e5"},
        }],
    }
    outcome = replay_skill(Agent(CountingModel(), runtime=canvas), pointed, goal=pointed["goal"])
    assert outcome.divergence is not None
    assert "coordinates" in outcome.divergence.reason
    assert canvas.calls == []


def test_unchanged_snapshot_is_a_divergence() -> None:
    runtime = FakeRuntime(window(el("e2", "AXButton", "Save", parent="e1", clickable=True)))
    skill = {
        "version": 1,
        "goal": "Press the Save button",
        "steps": [{
            "action": "click",
            "args": {},
            "locator": {"role": "AXButton", "name": "Save"},
            "check": {"snapshot_changed": True},
        }],
    }
    outcome = replay_skill(Agent(CountingModel(), runtime=runtime), skill, goal=skill["goal"])
    assert outcome.divergence is not None
    assert "did not change" in outcome.divergence.reason
    assert runtime.calls == [("click", {"ref": "e2"})]


def test_redacted_text_is_not_typed() -> None:
    runtime = FakeRuntime(window(
        el("e3", "AXTextField", "Note", parent="e1", editable=True, value=""),
    ))
    skill = {
        "version": 1,
        "goal": "Fill the note",
        "steps": [{
            "action": "type",
            "args": {"text": "[REDACTED]"},
            "locator": {"role": "AXTextField", "name": "Note"},
        }],
    }
    outcome = replay_skill(Agent(CountingModel(), runtime=runtime), skill, goal=skill["goal"])
    assert outcome.divergence is not None
    assert "redacted" in outcome.divergence.reason
    assert runtime.calls == []


def test_password_screen_is_not_replayed() -> None:
    runtime = FakeRuntime(window(
        el("e2", "AXButton", "Save", parent="e1", clickable=True),
        el("e3", "AXTextField", "Password", parent="e1", editable=True, secure=True),
    ))
    skill = {
        "version": 1,
        "goal": "Press the Save button",
        "steps": [{
            "action": "click",
            "args": {},
            "locator": {"role": "AXButton", "name": "Save"},
        }],
    }
    outcome = replay_skill(Agent(CountingModel(), runtime=runtime), skill, goal=skill["goal"])
    assert outcome.divergence is not None
    assert outcome.divergence.reason == "needs_human: login"
    assert runtime.calls == []


def test_a_different_goal_does_not_overwrite_the_skill(tmp_path) -> None:
    path = tmp_path / "skill.json"
    original = {
        "version": 1,
        "goal": "Press the Save button",
        "steps": [{"action": "done", "args": {"answer": "kept", "conditions": []}}],
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    runtime = FakeRuntime(window(el("e2", "AXButton", "Save", parent="e1", clickable=True)))
    result = run_with_skill(
        Agent(
            ScriptedModel([ModelTurn(calls=[_done("other", [
                {"element": {"role": "AXButton", "name": "Save"}},
            ])])]),
            runtime=runtime,
            trace_dir=tmp_path / "other",
        ),
        "Count the buttons",
        path,
    )
    assert result.status == "success"
    assert json.loads(path.read_text(encoding="utf-8"))["goal"] == "Press the Save button"


def test_skill_from_run_copies_stable_id_from_the_snapshot() -> None:
    button = el(
        "e2", "AXButton", "Save", parent="e1", stable_id="save-main",
        path=("AXWindow", "AXButton"),
    )
    snap = Snapshot("snap", Scope.WINDOW, "demo", 1, 0.0, (), (el("e1", "AXWindow", "Demo"), button))
    result = RunResult(
        status="success",
        answer="pressed",
        steps=1,
        elapsed_s=0.1,
        reason="done",
        step_log=[
            StepRecord(
                index=1,
                action="click",
                target={"ref": "e2", "role": "AXButton", "name": "Save"},
                args={"ref": "e2", "x": 10, "y": 12},
                result="click ok",
                error=None,
                verified=True,
                duration_s=0.1,
            ),
        ],
    )
    skill = skill_from_run("Press the Save button", result, snapshots=[snap])
    locator = skill["steps"][0]["locator"]
    assert locator["stable_id"] == "save-main"
    assert locator["path"] == ["AXWindow", "AXButton"]
    assert "x" not in skill["steps"][0]["args"]
