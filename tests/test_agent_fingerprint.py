"""Screen-change faults use snapshots only; no desktop or model service."""

import dataclasses
import json

import pytest

from a11y_computer_use.agent.actions import Action
from a11y_computer_use.agent.core import snapshot_digest
from a11y_computer_use.agent.fingerprint import ScreenFingerprint, diff, expected_changes
from a11y_computer_use.agent.models.base import ToolCall
from a11y_computer_use.agent.models.scripted import ScriptedModel
from tests.test_agent_core import FakeRuntime, el, replace_ref, run, turn, window


class CountingRuntime(FakeRuntime):
    def __init__(self):
        super().__init__(window(
            el("e2", "AXTextField", "Name", parent="e1", value="", focused=True),
            el("e3", "AXTextField", "Email", parent="e1", value=""),
        ))
        self.observations = 0
        self.url = "https://example.test/form"
        self.app = "demo"
        self.pid = 1

    def desktop_snapshot(self, app, **kwargs):
        self.observations += 1
        text = super().desktop_snapshot(app, **kwargs)
        self._current = dataclasses.replace(self._current, url=self.url, app=self.app, pid=self.pid)
        return text


@pytest.mark.parametrize("fault,component", [
    ("dialog", "window"), ("window", "window"), ("app", "app"),
    ("pid", "app"), ("url", "url"), ("focus", "focus"),
])
def test_verified_mid_turn_screen_fault_skips_queued_key(tmp_path, fault, component):
    runtime = CountingRuntime()

    def on_call(name, params, rt):
        replace_ref(rt, "e2", value="Alice")
        if fault == "dialog":
            rt.elements.append(el("e4", "AXDialog", "Save As", parent="e1"))
        elif fault == "window":
            replace_ref(rt, "e1", title="Other document")
        elif fault == "focus":
            replace_ref(rt, "e2", focused=False)
            replace_ref(rt, "e3", focused=True)
        else:
            setattr(rt, fault, {"app": "other", "pid": 2, "url": "https://example.test/other"}[fault])

    runtime.on_call = on_call
    result, events, rt, agent = run(
        ScriptedModel([turn(
            ToolCall("set_value", {"ref": "e2", "value": "Alice"}),
            ToolCall("key", {"chord": "Return"}),
        )]), [], runtime=runtime, trace_dir=tmp_path, max_steps=1,
    )
    assert [name for name, _ in rt.calls] == ["set_value"]
    assert rt.observations == 2  # initial + existing post-action observe
    step = result.step_log[0]
    assert step.verified and step.turn_stop == "screen_change"
    assert [call["name"] for call in step.skipped] == ["key"]
    assert any(e.data.get("turn_stop") == "screen_change" for e in events)
    feedback = next(m.content for m in agent._messages if m.role == "tool")
    assert component + " changed" in feedback or component + "," in feedback
    assert "Did not run: key" in feedback
    row = json.loads((tmp_path / "steps.jsonl").read_text())
    assert set(row["fingerprint"]) == {"app", "window", "url", "focus", "tree"}
    assert all(len(value) == 64 and set(value) <= set("0123456789abcdef") for value in row["fingerprint"].values())


@pytest.mark.parametrize("single,verified", [(True, True), (False, False)])
def test_single_call_unchanged_and_failure_keeps_precedence(tmp_path, single, verified):
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    rt = CountingRuntime()
    def mutate(name, params, runtime):
        replace_ref(runtime, "e1", title="Other window")
        if not verified:
            raise ComputerUseError(ErrorCode.UNSUPPORTED, "refused")
    rt.on_call = mutate
    calls = [ToolCall("set_value", {"ref": "e2", "value": "Alice"})]
    if not single:
        calls.append(ToolCall("key", {"chord": "Return"}))
    result, _, _, _ = run(ScriptedModel([turn(*calls)]), [], runtime=rt,
                           trace_dir=tmp_path, max_steps=1, max_retries=0)
    assert result.step_log[0].turn_stop == (None if single else "failure")
    assert result.step_log[0].verified is verified
    assert rt.observations == 2


def test_form_fill_then_declared_screen_changing_last_call(tmp_path):
    rt = CountingRuntime()
    def mutate(name, params, runtime):
        if name == "set_value":
            for item in list(runtime.elements):
                replace_ref(runtime, item.ref, focused=item.ref == params["ref"])
            replace_ref(runtime, params["ref"], value=params["value"])
        else:
            replace_ref(runtime, "e1", title="Submitted")
    rt.on_call = mutate
    result, _, _, _ = run(ScriptedModel([turn(
        ToolCall("set_value", {"ref": "e2", "value": "Alice"}),
        ToolCall("set_value", {"ref": "e3", "value": "alice@example.test"}),
        ToolCall("key", {"chord": "Return"}),
    )]), [], runtime=rt, trace_dir=tmp_path, max_steps=3)
    assert len(rt.calls) == 3
    assert rt.observations == 4
    assert all(s.verified and s.turn_stop is None and not s.skipped for s in result.step_log)


@pytest.mark.parametrize("name,args,role,screen", [
    ("type", {}, "AXTextField", False), ("set_value", {}, "AXSlider", False),
    ("select", {}, "AXComboBox", False), ("click", {}, "AXCheckBox", False),
    ("click", {}, "AXButton", True), ("click", {}, "AXLink", True),
    ("click", {}, "AXTab", True), ("click", {}, "AXMenuItem", True),
    ("click", {"x": 2, "y": 3}, "AXTextField", True),
    ("menu", {"action": "press"}, None, True),
    ("app", {}, None, True), ("window", {}, None, True),
    ("scroll", {}, None, True), ("choose_file", {}, None, True),
    *[("key", {"chord": chord}, None, True) for chord in
      ["Return", "Enter", "Escape", "alt+Tab", "alt+x", "super", "ctrl+l", "ctrl+o", "ctrl+s", "ctrl+w", "ctrl+t", "ctrl+n", "ctrl+q", "ctrl+r", "F5"]],
    *[("key", {"chord": chord}, None, False) for chord in ["ctrl+a", "ctrl+f", "Left"]],
])
def test_expected_changes_are_pure(name, args, role, screen):
    action = Action(name, args)
    target = el("e2", role, "Target") if role else None
    assert ("window" in expected_changes(action, target)) is screen
    assert action.args == args


def test_digest_ignores_snapshot_epoch_and_geometry():
    rt = CountingRuntime()
    rt.desktop_snapshot("demo")
    before = rt._current
    after = dataclasses.replace(before, snapshot_id="next", created_at=99,
                                elements=tuple(dataclasses.replace(e, bounds=None) for e in before.elements))
    first = ScreenFingerprint.from_snapshot(before, snapshot_digest(before, ""))
    second = ScreenFingerprint.from_snapshot(after, snapshot_digest(after, ""))
    assert diff(first, second) == set()


def test_focus_inside_target_subtree_is_expected():
    from a11y_computer_use.agent.fingerprint import unexpected_changes

    rt = CountingRuntime()
    rt.elements.append(el("e4", "AXTextField", "Editor", parent="e2"))
    rt.desktop_snapshot("demo")
    before = rt._current
    replace_ref(rt, "e2", focused=False)
    replace_ref(rt, "e4", focused=True)
    rt.desktop_snapshot("demo")
    after = rt._current
    assert unexpected_changes(
        ScreenFingerprint.from_snapshot(before, snapshot_digest(before, "")),
        ScreenFingerprint.from_snapshot(after, snapshot_digest(after, "")),
        Action("set_value", {"ref": "e2", "value": "Alice"}), before.element("e2"), after,
    ) == set()


def test_implicit_type_stops_when_focus_leaves_the_original_field(tmp_path):
    rt = CountingRuntime()
    def mutate(name, params, runtime):
        replace_ref(runtime, "e2", focused=False, value="Alice")
        replace_ref(runtime, "e3", focused=True)
    rt.on_call = mutate
    result, _, _, _ = run(ScriptedModel([turn(
        ToolCall("type", {"text": "Alice"}), ToolCall("key", {"chord": "Return"}),
    )]), [], runtime=rt, trace_dir=tmp_path, max_steps=1)
    assert result.step_log[0].verified
    assert result.step_log[0].turn_stop == "screen_change"
    assert len(rt.calls) == 1 and rt.observations == 2
