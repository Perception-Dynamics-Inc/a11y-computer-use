"""Remembered AT-SPI editables must not outlive focus changes or app grants."""

from __future__ import annotations

import json
import sys
import types
from contextlib import nullcontext

import pytest

from computeruse import drivers, observe, safety, server
from computeruse.drivers import _atspi, _linux_system, linux
from computeruse.schema import ComputerUseError, ErrorCode, Point
from tests.conftest import build_synthetic_snapshot


@pytest.fixture
def focus_driver(monkeypatch):
    driver = linux.LinuxDriver()
    front = {"app": "editor"}
    inserted: list[tuple[object, str]] = []
    typed: list[str] = []
    handle = object()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(driver, "frontmost_app", lambda: (front["app"], None))
    monkeypatch.setattr(linux, "_on_wayland", lambda: False)
    monkeypatch.setattr(observe, "ax_handle_for", lambda snapshot_id, ref: handle)
    monkeypatch.setattr(_atspi, "is_secure", lambda acc: False)
    monkeypatch.setattr(_atspi, "pid_of", lambda acc: 42)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "editor")
    monkeypatch.setattr(_atspi, "grab_focus", lambda acc: False)
    monkeypatch.setattr(_atspi, "do_press", lambda acc: False)
    monkeypatch.setattr(_atspi, "set_text", lambda acc, text: True)
    monkeypatch.setattr(_atspi, "insert_text", lambda acc, text: inserted.append((acc, text)) or True)
    monkeypatch.setattr(_atspi, "focused_secure", lambda app: False)
    fake_input = types.ModuleType("computeruse.drivers._linux_input")
    fake_input.held = lambda modifiers: nullcontext()
    fake_input.click = lambda *args, **kwargs: None
    fake_input.drag = lambda *args, **kwargs: None
    fake_input.scroll = lambda *args, **kwargs: None
    fake_input.press_chord = lambda chord: None
    fake_input.validate_chord = lambda chord: None
    fake_input.type_string = typed.append
    monkeypatch.setitem(sys.modules, "computeruse.drivers._linux_input", fake_input)
    monkeypatch.setattr(drivers, "_linux_input", fake_input, raising=False)
    monkeypatch.setattr(_linux_system, "activate_app", lambda app: app)
    monkeypatch.setattr(_linux_system, "launch_app", lambda app: None)
    monkeypatch.setattr(_linux_system, "raise_window", lambda wid: True)
    field = build_synthetic_snapshot().element("e3")
    assert field.editable and not field.secure
    return driver, front, field, handle, inserted, typed


def test_detectable_owner_preserves_ref_typing_without_widget_focus(focus_driver) -> None:
    driver, _, field, handle, inserted, typed = focus_driver
    assert driver.press_element(field)  # grab_focus=False: headless widget semantics survive
    driver.type_text("hello")
    driver.type_text(" again")
    assert inserted == [(handle, "hello"), (handle, " again")]
    assert typed == []


def test_runtime_cannot_use_another_apps_full_grant_for_a_cached_editable(
    focus_driver, monkeypatch, tmp_path
) -> None:
    driver, front, field, _, inserted, typed = focus_driver
    assert driver.press_element(field)
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier("editor", safety.Tier.CLICK)  # ref click allowed, typing is forbidden
    store.set_tier("terminal", safety.Tier.FULL)
    front["app"] = "terminal"
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: front["app"])
    runtime = server.Runtime(driver=driver, store=store, audit=safety.AuditLog(tmp_path / "audit"))
    with pytest.raises(ComputerUseError) as error:
        runtime.type_text("must not enter editor")
    assert error.value.code is ErrorCode.FOCUS_CHANGED
    assert error.value.detail == {"editable_app": "editor", "frontmost_app": "terminal"}
    assert inserted == [] and typed == []
    assert driver._focused_editable is None
    row = json.loads(next((tmp_path / "audit").glob("*.jsonl")).read_text())
    assert row["app"] == "terminal" and row["result"] == "focus_changed"


@pytest.mark.parametrize("operation", [
    "click", "drag", "scroll", "key_chord", "press_button", "activate_app", "launch_app", "raise_window",
])
def test_focus_changing_actions_clear_the_remembered_editable(focus_driver, operation: str) -> None:
    driver, _, field, _, inserted, typed = focus_driver
    driver.press_element(field)
    point = Point(0, 20, 20)
    if operation == "click":
        driver.click(point)
    elif operation == "drag":
        driver.drag(point, Point(0, 50, 50))
    elif operation == "scroll":
        driver.scroll(point, dy=1)
    elif operation == "key_chord":
        driver.key_chord("tab")
    elif operation == "press_button":
        driver.press_element(build_synthetic_snapshot().element("e2"))
    elif operation in {"activate_app", "launch_app"}:
        getattr(driver, operation)("editor")
    else:
        driver.raise_window(7)
    assert driver._focused_editable is None
    driver.type_text("new focus")
    assert inserted == [] and typed == ["new focus"]


def test_coordinate_click_does_not_hide_the_new_password_focus(focus_driver, monkeypatch) -> None:
    driver, _, field, _, inserted, typed = focus_driver
    driver.press_element(field)
    driver.click(Point(0, 20, 20))
    monkeypatch.setattr(_atspi, "focused_secure", lambda app: True)
    with pytest.raises(ComputerUseError) as error:
        driver.type_text("secret")
    assert error.value.code is ErrorCode.SECURE_FIELD
    assert inserted == [] and typed == []


@pytest.mark.parametrize("missing", ["owner", "frontmost"])
def test_unverifiable_cached_target_fails_closed(focus_driver, monkeypatch, missing: str) -> None:
    driver, front, field, _, inserted, typed = focus_driver
    driver.press_element(field)
    if missing == "owner":
        monkeypatch.setattr(_atspi, "pid_of", lambda acc: None)
    else:
        front["app"] = None
    with pytest.raises(ComputerUseError) as error:
        driver.type_text("unsafe")
    assert error.value.code is ErrorCode.FOCUS_CHANGED
    assert inserted == [] and typed == []


def test_explicit_set_value_remains_available_without_frontmost(focus_driver) -> None:
    driver, front, field, _, _, _ = focus_driver
    front["app"] = None
    assert driver.set_value(field, "explicit target")


def test_failed_set_value_does_not_leave_a_typing_target(focus_driver, monkeypatch) -> None:
    driver, _, field, _, _, _ = focus_driver
    driver.press_element(field)
    monkeypatch.setattr(_atspi, "set_text", lambda acc, text: False)
    assert driver.set_value(field, "failed") is False
    assert driver._focused_editable is None


def test_dry_run_keeps_the_existing_focus_target(focus_driver) -> None:
    driver, _, field, handle, _, _ = focus_driver
    driver.press_element(field)
    driver.click(Point(0, 1, 1), dry_run=True)
    driver.key_chord("tab", dry_run=True)
    assert driver._focused_editable is handle
