"""Hermetic coverage for the benchmark failures: empty desktop, ordinary
submit, pre-action step targets, and a missing Linux binding.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from a11y_computer_use.agent.actions import Action, risk_reason
from a11y_computer_use.agent.models.base import ToolCall
from a11y_computer_use.agent.models.scripted import ScriptedModel
from a11y_computer_use.schema import ComputerUseError, ErrorCode
from tests.test_agent_core import FakeRuntime, done, el, run, turn, window


def _messages_text(messages) -> str:
    parts = []
    for message in messages:
        content = getattr(message, "content", "")
        parts.append(content if isinstance(content, str) else str(content))
    return "\n".join(parts)


def test_risk_reason_lets_ordinary_submits_through() -> None:
    submit = Action("click", {"ref": "e2"})
    update = Action("click", {"ref": "e3"})
    pay = Action("click", {"ref": "e4"})
    send = Action("click", {"ref": "e5"})
    delete = Action("click", {"ref": "e6"})
    assert risk_reason(submit, "Submit") is None
    assert risk_reason(update, "Update cart") is None
    assert risk_reason(pay, "Pay now") is not None
    assert risk_reason(send, "Send") is not None
    assert risk_reason(delete, "Delete") is not None
    assert risk_reason(Action("app", {"action": "quit", "name": "demo"}), None) == "app quit"


def test_plain_submit_is_not_approval_and_not_needs_human(tmp_path) -> None:
    elements = window(el("e2", "AXButton", "Submit", parent="e1", clickable=True))
    result, _events, runtime, agent = run(
        ScriptedModel([
            turn(ToolCall("click", {"ref": "e2"})),
            turn(done("filed", [{"element": {"role": "AXButton", "name": "Submit"}}])),
        ]),
        elements,
        trace_dir=tmp_path,
    )
    assert runtime.calls == [("click", {"ref": "e2"})]
    assert result.status == "success"
    assert result.status != "needs_human"
    assert result.step_log[0].error is None
    prompt = agent._messages[0].content
    assert "submitting or sending need approval" not in prompt
    assert "Update cart" in prompt
    assert "Call ask_human only" in prompt


def test_pay_send_and_delete_stay_denied_and_allow_safe_runs_quit(tmp_path) -> None:
    elements = window(
        el("e2", "AXButton", "Pay now", parent="e1", clickable=True),
        el("e3", "AXButton", "Send", parent="e1", clickable=True),
        el("e4", "AXButton", "Delete", parent="e1", clickable=True),
    )
    result, _events, runtime, _agent = run(
        ScriptedModel([
            turn(ToolCall("click", {"ref": "e2"})),
            turn(ToolCall("click", {"ref": "e3"})),
            turn(ToolCall("click", {"ref": "e4"})),
            turn(done("held", [{"element": {"role": "AXButton", "name": "Pay now"}}])),
        ]),
        elements,
        trace_dir=tmp_path,
    )
    assert runtime.calls == []
    assert result.status == "success"
    assert all(str(step.error).startswith("approval_denied") for step in result.step_log[:3])

    quit_elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    quit_result, _events, quit_runtime, _agent = run(
        ScriptedModel([
            turn(ToolCall("app", {"action": "quit", "name": "demo"})),
            turn(done("closed", [{"element": {"role": "AXButton", "name": "Save"}}])),
        ]),
        quit_elements,
        approve_policy="allow-safe",
        auto_deny=True,
        trace_dir=tmp_path / "safe",
    )
    assert quit_runtime.calls and quit_runtime.calls[0][0] == "app"
    assert quit_result.step_log[0].error is None


def test_empty_desktop_observation_is_not_a_permission_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("A11Y_COMPUTER_USE_ALLOW_ANY_PATH", "1")
    seen: list[str] = []

    class _Desktop(FakeRuntime):
        def _frontmost(self) -> str:
            return "unknown"

        def desktop_snapshot(self, app, **kwargs) -> str:
            raise AssertionError(f"snapshotted {app}")

        def call_tool(self, name, params, confirm=None):
            self.calls.append((name, dict(params)))
            if name == "app":
                return "needs_permission: unknown has no permission grant; ask the user"
            if name == "window":
                return [{"title": "notes"}]
            return "ok"

    marker = tmp_path / "ready.txt"
    marker.write_text("ready", encoding="utf-8")

    def script(messages):
        seen.append(_messages_text(messages))
        return turn(done("desk", [{"file_exists": str(marker), "contains": "ready"}]))

    result, events, runtime, _agent = run(
        ScriptedModel(script),
        [],
        runtime=_Desktop([]),
        trace_dir=tmp_path / "trace",
    )
    assert result.status == "success"
    observation = next(event.data["text"] for event in events if event.type == "observation")
    assert "No application is focused" in observation
    assert "action=launch" in observation
    assert "notes" in observation
    assert "needs_permission" not in observation
    assert "ask the user" not in observation
    assert "unknown has no permission grant" not in observation
    assert any(name == "app" for name, _params in runtime.calls)
    assert seen and "No application is focused" in seen[0]


def test_missing_app_after_quit_falls_back_to_the_desktop(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("A11Y_COMPUTER_USE_ALLOW_ANY_PATH", "1")
    class _Gone(FakeRuntime):
        def _frontmost(self) -> str:
            return "mousepad"

        def desktop_snapshot(self, app, **kwargs) -> str:
            raise ComputerUseError(
                ErrorCode.APP_NOT_FOUND,
                f"no running application matches {app!r}",
                detail={"app": app},
            )

        def call_tool(self, name, params, confirm=None):
            self.calls.append((name, dict(params)))
            return "[]"

    marker = tmp_path / "ready.txt"
    marker.write_text("ready", encoding="utf-8")

    def script(messages):
        text = _messages_text(messages)
        assert "no running application" not in text
        assert "ask the user" not in text
        return turn(done("desk", [{"file_exists": str(marker), "contains": "ready"}]))

    result, events, _runtime, _agent = run(
        ScriptedModel(script),
        [],
        runtime=_Gone([]),
        trace_dir=tmp_path / "trace",
    )
    assert result.status == "success"
    observation = next(event.data["text"] for event in events if event.type == "observation")
    assert "No application is focused" in observation
    assert "needs_permission" not in observation


def test_step_log_uses_the_pre_action_element(tmp_path) -> None:
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))

    def on_call(name, params, runtime):
        del name, params
        runtime.elements = window(
            el("e2", "AXGroup", "Item", parent="e1"),
            el("e9", "AXButton", "Save", parent="e1", clickable=True),
        )

    runtime = FakeRuntime(elements)
    runtime.on_call = on_call
    result, _events, _runtime, _agent = run(
        ScriptedModel([
            turn(ToolCall("click", {"ref": "e2"})),
            turn(done("saved", [{"element": {"role": "AXButton", "name": "Save"}}])),
        ]),
        elements,
        runtime=runtime,
        trace_dir=tmp_path,
    )
    assert result.status == "success"
    assert result.step_log[0].target == {"ref": "e2", "role": "AXButton", "name": "Save"}


def test_redaction_uses_the_pre_action_field_not_the_shifted_ref(tmp_path) -> None:
    secret = "hunter2-note"
    elements = window(el("e3", "AXTextField", "Note", parent="e1", editable=True, value=""))

    def on_call(name, params, runtime):
        del name, params
        runtime.elements = window(
            el("e3", "AXTextField", "Password", parent="e1", secure=True, editable=True, value=""),
        )

    runtime = FakeRuntime(elements)
    runtime.on_call = on_call
    result, _events, _runtime, _agent = run(
        ScriptedModel([
            turn(ToolCall("set_value", {"ref": "e3", "value": secret})),
            turn(done("kept", [{"element": {"role": "AXTextField", "name": "Password"}}])),
        ]),
        elements,
        runtime=runtime,
        trace_dir=tmp_path,
    )
    assert result.step_log[0].target["name"] == "Note"
    assert result.step_log[0].args["value"] == secret
    password = "s3cret-value"
    secure = window(el("e2", "AXTextField", "Password", parent="e1", secure=True, editable=True))

    def shift(name, params, runtime):
        del name, params
        runtime.elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))

    secure_runtime = FakeRuntime(secure)
    secure_runtime.on_call = shift
    secured, _events, shifted, _agent = run(
        ScriptedModel([turn(ToolCall("set_value", {"ref": "e2", "value": password}))]),
        secure,
        runtime=secure_runtime,
        trace_dir=tmp_path / "secret",
    )
    assert secured.status == "needs_human"
    assert shifted.calls == []
    blob = json.dumps(secured.to_dict())
    assert password not in blob


def test_missing_linux_bindings_fail_before_the_model(tmp_path, monkeypatch) -> None:
    from a11y_computer_use.agent import cli
    from a11y_computer_use.agent import core

    called = {"n": 0}

    class _Linux(FakeRuntime):
        def __init__(self, elements):
            super().__init__(elements)
            self.driver = SimpleNamespace(name="linux")

    def script(_messages):
        called["n"] += 1
        return turn(done("nope", [{"element": {"role": "AXButton", "name": "Save"}}]))

    monkeypatch.setattr(
        core,
        "linux_binding_message",
        lambda: (
            "Linux accessibility bindings are missing: gi (PyGObject). "
            "Install with pip install 'a11y-computer-use[agent,linux]' "
            "and apt install gir1.2-atspi-2.0 at-spi2-core python3-gi."
        ),
    )
    result, _events, _runtime, _agent = run(
        ScriptedModel(script),
        window(el("e2", "AXButton", "Save", parent="e1", clickable=True)),
        runtime=_Linux([]),
        trace_dir=tmp_path,
    )
    assert called["n"] == 0
    assert result.status == "failed"
    assert result.reason.startswith("error:")
    assert "a11y-computer-use[agent,linux]" in result.reason
    assert "python3-gi" in result.reason
    assert cli.exit_code(result) == 3


def test_missing_atspi_import_is_not_reported_as_an_unreachable_bus(monkeypatch) -> None:
    from a11y_computer_use.drivers import _atspi
    from a11y_computer_use.drivers.linux import LinuxDriver

    monkeypatch.setattr(_atspi, "enable_a11y_status", lambda: False)

    def boom():
        raise ImportError("PyGObject is not installed; the AT-SPI2 binding is unavailable")

    monkeypatch.setattr(_atspi, "_atspi", boom)
    with pytest.raises(ComputerUseError) as exc:
        LinuxDriver().ensure_trusted()
    assert exc.value.code is ErrorCode.PERMISSION_DENIED_ACCESSIBILITY
    assert "bindings are missing" in exc.value.message
    assert "bus is not reachable" not in exc.value.message
    assert "agent,linux" in exc.value.detail["hint"]
