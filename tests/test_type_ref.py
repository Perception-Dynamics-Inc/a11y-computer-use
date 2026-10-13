"""type(ref=...) focuses that element and types only after focus is confirmed.

Hermetic. A fake driver records focus and type. The Linux driver test stubs
AT-SPI so an unconfirmed focus never inserts and never sends a key.
"""

from __future__ import annotations

import pytest

from a11y_computer_use import safety, server
from a11y_computer_use.agent.actions import Action, tool_schemas
from a11y_computer_use.agent.core import keyboard_action, to_runtime_call
from a11y_computer_use.schema import (
    Bounds,
    ComputerUseError,
    Display,
    Element,
    ErrorCode,
    Scope,
    Snapshot,
)

APP = "demo"


def _element(ref: str, role: str, title: str, **kwargs) -> Element:
    bounds = kwargs.pop("bounds", Bounds(0, 10, 20, 80, 24))
    return Element(
        ref=ref,
        role=role,
        title=title,
        value=kwargs.pop("value", None),
        bounds=bounds,
        snapshot_id="snap",
        **kwargs,
    )


def _fields() -> list[Element]:
    return [
        _element("e1", "AXWindow", "Demo", path=("AXWindow",)),
        _element(
            "e2", "AXTextField", "FieldA", parent="e1",
            path=("AXWindow", "AXTextField", "FieldA"),
            value="AAA", editable=True, focused=True,
        ),
        _element(
            "e3", "AXTextField", "FieldB", parent="e1",
            path=("AXWindow", "AXTextField", "FieldB"),
            value="", editable=True, focused=False,
        ),
    ]


class _Driver:
    name = "fake"
    resolves_apps = True

    def __init__(self) -> None:
        self.elements = _fields()
        self.calls: list[tuple] = []
        self.focus_ok = True
        self.resolve_error: ComputerUseError | None = None

    def ensure_trusted(self) -> None:
        return None

    def frontmost_app(self):
        return APP, 4242

    def snapshot(self, scope, app) -> Snapshot:
        return Snapshot(
            snapshot_id="snap",
            scope=scope,
            app=app,
            pid=4242,
            created_at=0.0,
            displays=(Display(0, 800, 600, 1.0, True),),
            elements=tuple(self.elements),
        )

    def resolve_ref(self, snap: Snapshot, ref: str, *, live: Snapshot | None = None) -> Element:
        del live
        if self.resolve_error is not None:
            raise self.resolve_error
        return snap.element(ref)

    def focus_for_type(self, element: Element) -> bool:
        self.calls.append(("focus", element.ref))
        return self.focus_ok

    def type_text(self, text: str, **kwargs) -> int:
        del kwargs
        self.calls.append(("type", text))
        target = next(item for item in self.elements if item.ref == "e3")
        self.elements = [
            _replace(item, value=f"{item.value or ''}{text}") if item.ref == "e3" else item
            for item in self.elements
        ]
        del target
        return len(text)

    def app_at_point(self, point):
        del point
        return APP


def _replace(element: Element, **changes) -> Element:
    import dataclasses

    return dataclasses.replace(element, **changes)


def _runtime(tmp_path, driver: _Driver) -> server.Runtime:
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier(APP, safety.Tier.FULL)
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    runtime.desktop_snapshot(APP)
    return runtime


def test_type_without_ref_still_types_into_the_focused_element(tmp_path) -> None:
    driver = _Driver()
    runtime = _runtime(tmp_path, driver)
    result = runtime.type_text("hi")
    assert result == "typed 2 characters"
    assert ("focus", "e3") not in driver.calls
    assert ("type", "hi") in driver.calls


def test_type_with_ref_focuses_that_element_and_reads_it_back(tmp_path) -> None:
    driver = _Driver()
    runtime = _runtime(tmp_path, driver)
    result = runtime.type_text("BBB", ref="e3")
    assert ("focus", "e3") in driver.calls
    assert driver.calls.index(("focus", "e3")) < driver.calls.index(("type", "BBB"))
    assert result.outcome == "confirmed"
    assert "BBB" in result.evidence
    assert "AAA" not in result.evidence
    values = {item.title: item.value for item in driver.elements}
    assert values["FieldA"] == "AAA"
    assert values["FieldB"] == "BBB"


def test_type_with_ref_types_nothing_when_focus_is_not_confirmed(tmp_path) -> None:
    driver = _Driver()
    driver.focus_ok = False
    runtime = _runtime(tmp_path, driver)
    with pytest.raises(ComputerUseError) as exc:
        runtime.type_text("BBB", ref="e3")
    assert exc.value.code is ErrorCode.FOCUS_LOST
    assert exc.value.detail["reason"] == "focus_lost"
    assert exc.value.detail["outcome"] == "refused"
    assert "keyboard" not in exc.value.detail["next"]
    assert [call for call in driver.calls if call[0] == "type"] == []
    values = {item.title: item.value for item in driver.elements}
    assert values["FieldA"] == "AAA"
    assert values["FieldB"] == ""


def test_type_with_ref_types_nothing_when_the_driver_cannot_confirm_focus(tmp_path) -> None:
    driver = _Driver()
    driver.focus_for_type = None  # type: ignore[method-assign]
    runtime = _runtime(tmp_path, driver)
    with pytest.raises(ComputerUseError) as exc:
        runtime.type_text("BBB", ref="e3")
    assert exc.value.code is ErrorCode.FOCUS_LOST
    assert driver.calls == []


def test_type_with_ref_does_not_type_when_the_instance_was_replaced(tmp_path) -> None:
    driver = _Driver()
    driver.resolve_error = ComputerUseError(
        ErrorCode.STALE_REF,
        "e3 was issued by an application instance that is no longer running",
        detail={"ref": "e3", "reason": "app_restarted", "candidates": []},
    )
    runtime = _runtime(tmp_path, driver)
    with pytest.raises(ComputerUseError) as exc:
        runtime.type_text("BBB", ref="e3")
    assert exc.value.code is ErrorCode.STALE_REF
    assert exc.value.detail["reason"] == "app_restarted"
    assert driver.calls == []
    assert next(item.value for item in driver.elements if item.ref == "e2") == "AAA"


def test_type_with_ref_does_not_type_on_a_plain_stale_ref(tmp_path) -> None:
    driver = _Driver()
    driver.resolve_error = ComputerUseError(
        ErrorCode.STALE_REF,
        "e3 no longer resolves",
        detail={"ref": "e3", "reason": "not_found"},
    )
    runtime = _runtime(tmp_path, driver)
    with pytest.raises(ComputerUseError) as exc:
        runtime.type_text("BBB", ref="e3")
    assert exc.value.code is ErrorCode.STALE_REF
    assert driver.calls == []


def test_blank_ref_is_invalid_and_types_nothing(tmp_path) -> None:
    driver = _Driver()
    runtime = _runtime(tmp_path, driver)
    with pytest.raises(ValueError, match="ref"):
        runtime.type_text("BBB", ref="  ")
    assert driver.calls == []


def test_ref_from_another_app_is_invalid_and_types_nothing(tmp_path) -> None:
    driver = _Driver()
    runtime = _runtime(tmp_path, driver)
    with pytest.raises(ValueError, match="belongs to"):
        runtime.type_text("BBB", app="other", ref="e3")
    assert driver.calls == []


def test_act_type_step_accepts_ref_and_still_rejects_unknown_fields() -> None:
    runtime = server.Runtime.__new__(server.Runtime)
    assert runtime._act_step_argument_error(0, {"do": "type", "text": "hi", "ref": "e3"}) is None
    detail = runtime._act_step_argument_error(0, {"do": "type", "text": "hi", "ref": ""})
    assert detail is not None and "ref" in detail
    unknown = runtime._act_step_argument_error(1, {"do": "type", "text": "hi", "nope": 1})
    assert unknown is not None and "unknown field" in unknown and "ref" in unknown


def test_agent_type_schema_and_mapping_pass_ref() -> None:
    spec = next(item for item in tool_schemas() if item["name"] == "type")
    assert "ref" in spec["parameters"]["properties"]
    assert spec["parameters"]["additionalProperties"] is False
    assert "ref" not in spec["parameters"]["required"]
    assert to_runtime_call(Action("type", {"text": "hi", "ref": "e3"}), APP) == (
        "type", {"text": "hi", "ref": "e3"},
    )
    fallback, label = keyboard_action(Action("set_value", {"ref": "e3", "value": "hi"}))
    assert label == "keyboard:type"
    assert fallback.name == "type"
    assert fallback.args == {"text": "hi", "ref": "e3"}


def test_mcp_type_schema_declares_optional_ref(tmp_path) -> None:
    runtime = _runtime(tmp_path, _Driver())
    spec = next(item for item in server.tool_specs(runtime) if item["name"] == "type")
    properties = spec["input_schema"]["properties"]
    assert "ref" in properties
    assert "text" in properties
    assert spec["input_schema"].get("additionalProperties") is False
    assert "ref" not in spec["input_schema"].get("required", [])


def test_linux_type_into_sends_nothing_when_focus_is_not_confirmed(monkeypatch) -> None:
    from a11y_computer_use.drivers import _atspi
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    driver._run = lambda fn: fn()
    element = _element("e3", "AXTextField", "FieldB", editable=True, value="")
    sent: list[str] = []
    monkeypatch.setattr(
        "a11y_computer_use.observe.ax_handle_for", lambda *_args, **_kwargs: element,
    )
    monkeypatch.setattr(_atspi, "accessible_gone", lambda _acc: False)
    monkeypatch.setattr(_atspi, "hidden_web_target", lambda _acc: False)
    monkeypatch.setattr(_atspi, "_blocks_text_replace", lambda _acc: False)
    monkeypatch.setattr(_atspi, "grab_focus", lambda _acc: True)
    monkeypatch.setattr(_atspi, "_confirm_focus_on_target", lambda _acc: False)
    monkeypatch.setattr(_atspi, "insert_text", lambda *_args, **_kwargs: sent.append("insert"))
    monkeypatch.setattr(_atspi, "_type_string", lambda text: sent.append(text))
    with pytest.raises(ComputerUseError) as exc:
        driver.type_into(element, "leak")
    assert exc.value.code is ErrorCode.FOCUS_LOST
    assert exc.value.detail["outcome"] == "refused"
    assert sent == []


def test_linux_type_into_inserts_only_after_focus_is_confirmed(monkeypatch) -> None:
    from a11y_computer_use.drivers import _atspi
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    driver._run = lambda fn: fn()
    element = _element("e3", "AXTextField", "FieldB", editable=True, value="")
    order: list[str] = []
    monkeypatch.setattr(
        "a11y_computer_use.observe.ax_handle_for", lambda *_args, **_kwargs: element,
    )
    monkeypatch.setattr(_atspi, "accessible_gone", lambda _acc: False)
    monkeypatch.setattr(_atspi, "hidden_web_target", lambda _acc: False)
    monkeypatch.setattr(_atspi, "_blocks_text_replace", lambda _acc: False)
    monkeypatch.setattr(_atspi, "grab_focus", lambda _acc: order.append("focus") or True)
    monkeypatch.setattr(
        _atspi, "_confirm_focus_on_target", lambda _acc: order.append("confirm") or True,
    )
    monkeypatch.setattr(_atspi, "peer_field_texts", lambda _acc: {})
    monkeypatch.setattr(
        _atspi, "insert_text", lambda _acc, text: order.append(f"insert:{text}") or len(text),
    )
    monkeypatch.setattr(_atspi, "_type_string", lambda text: order.append(f"key:{text}"))
    assert driver.type_into(element, "BBB") == 3
    assert order == ["focus", "confirm", "insert:BBB"]
