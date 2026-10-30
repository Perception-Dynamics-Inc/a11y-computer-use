"""Remembered AT-SPI editables must not outlive focus changes or app grants."""

from __future__ import annotations

import json
import sys
import types
from contextlib import nullcontext

import pytest

from a11y_computer_use import drivers, observe, safety, server
from a11y_computer_use.drivers import _atspi, _linux_system, linux
from a11y_computer_use.schema import ComputerUseError, ErrorCode, Point
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
    monkeypatch.setattr(_atspi, "do_press", lambda acc, **kwargs: False)
    monkeypatch.setattr(_atspi, "set_text", lambda acc, text: True)
    monkeypatch.setattr(_atspi, "insert_text", lambda acc, text: inserted.append((acc, text)) or True)
    monkeypatch.setattr(_atspi, "focused_secure", lambda app: False)
    monkeypatch.setattr(_atspi, "focused_editable", lambda app: None)
    # This fixture models an editor with no native terminal focused.
    monkeypatch.setattr(_atspi, "_focused_node", lambda app: (None, False))
    monkeypatch.setattr(_atspi, "focused_text", lambda app: None)
    fake_input = types.ModuleType("a11y_computer_use.drivers._linux_input")
    fake_input.held = lambda modifiers: nullcontext()
    fake_input.click = lambda *args, **kwargs: None
    fake_input.drag = lambda *args, **kwargs: None
    fake_input.scroll = lambda *args, **kwargs: None
    fake_input.press_chord = lambda chord: None
    fake_input.validate_chord = lambda chord: None
    fake_input.physical_position_target = lambda is_chromium: nullcontext()
    fake_input.type_string = typed.append
    monkeypatch.setitem(sys.modules, "a11y_computer_use.drivers._linux_input", fake_input)
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


@pytest.mark.parametrize("app", ["xournalpp", "abiword"])
def test_canvas_without_text_types_once_and_reports_unverifiable(
    focus_driver, monkeypatch, app,
) -> None:
    driver, front, _, _, inserted, typed = focus_driver
    front["app"] = app

    class Canvas:
        def get_text_iface(self):
            return None

        def get_role_name(self):
            return "unknown"

    canvas = Canvas()
    monkeypatch.setattr(_atspi, "focused_text", _ORIGINAL_FOCUSED_TEXT)
    monkeypatch.setattr(_atspi, "_focused_node", lambda *args, **kwargs: (canvas, False))
    # GTK's static Text call returns an empty string even without Text.
    monkeypatch.setattr(_atspi, "_readable_text", lambda acc: "")
    assert _atspi.focused_text(app) is None
    assert driver.type_text("XPROBE1 beta") == 12
    assert typed == ["XPROBE1 beta"]
    assert inserted == []
    runtime = server.Runtime(driver=driver)
    assert runtime._drawing_type_verdict(app, "XPROBE1 beta") == (
        "unverifiable", "unverified",
    )


_ORIGINAL_FOCUSED_TEXT = _atspi.focused_text


def test_readable_empty_text_is_still_a_mismatch(focus_driver, monkeypatch) -> None:
    driver, _, _, _, inserted, typed = focus_driver

    class Field:
        def get_text_iface(self):
            return self

        def get_role_name(self):
            return "entry"

    monkeypatch.setattr(_atspi, "focused_text", _ORIGINAL_FOCUSED_TEXT)
    monkeypatch.setattr(_atspi, "_focused_node", lambda *args, **kwargs: (Field(), False))
    monkeypatch.setattr(_atspi, "_readable_text", lambda acc: "")
    with pytest.raises(ComputerUseError) as error:
        driver.type_text("hello")
    assert error.value.detail["reason"] == "text_mismatch"
    assert typed == ["hello"] and inserted == []


@pytest.mark.usefixtures("front_named_only")
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
    with pytest.raises(ComputerUseError) as error:
        driver.set_value(field, "failed")
    assert error.value.detail["reason"] == "text_mismatch"
    assert driver._focused_editable is None


@pytest.mark.parametrize("chord", ["a", "shift+a", "shift+1", "space"])
def test_printable_key_refuses_a_focused_password_field(focus_driver, monkeypatch, chord: str) -> None:
    driver, _, _, _, _, _ = focus_driver
    pressed: list[str] = []
    sys.modules["a11y_computer_use.drivers._linux_input"].press_chord = pressed.append
    monkeypatch.setattr(_atspi, "focused_secure", lambda app: True)
    with pytest.raises(ComputerUseError) as error:
        driver.key_chord(chord)
    assert error.value.code is ErrorCode.SECURE_FIELD
    assert "password field" in error.value.message
    assert pressed == []


@pytest.mark.parametrize("chord", [
    "tab", "shift+tab", "enter", "escape", "left", "right", "up", "down",
    "backspace", "delete", "home", "end", "ctrl+a", "alt+f4",
])
def test_navigation_and_shortcuts_still_reach_a_focused_password_field(
    focus_driver, monkeypatch, chord: str
) -> None:
    driver, _, _, _, _, _ = focus_driver
    pressed: list[str] = []
    sys.modules["a11y_computer_use.drivers._linux_input"].press_chord = pressed.append
    monkeypatch.setattr(_atspi, "focused_secure", lambda app: True)
    driver.key_chord(chord)
    assert pressed == [chord]


@pytest.mark.usefixtures("front_named_only")
def test_key_and_act_key_refuse_and_redact_a_printable_chord(
    focus_driver, monkeypatch, tmp_path
) -> None:
    driver, front, _, _, _, _ = focus_driver
    pressed: list[str] = []
    sys.modules["a11y_computer_use.drivers._linux_input"].press_chord = pressed.append
    monkeypatch.setattr(_atspi, "focused_secure", lambda app: True)
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier(front["app"], safety.Tier.FULL)
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: front["app"])
    runtime = server.Runtime(
        driver=driver, store=store, audit=safety.AuditLog(tmp_path / "audit"),
    )
    with pytest.raises(ComputerUseError) as error:
        runtime.key("a")
    assert error.value.code is ErrorCode.SECURE_FIELD
    assert pressed == []
    row = json.loads(next((tmp_path / "audit").glob("*.jsonl")).read_text().splitlines()[-1])
    assert row["action"] == "keychord" and row["result"] == "secure_field"
    assert row["params"]["chord"] == "[REDACTED]" and row["params"]["chars"] == 1
    raw = next((tmp_path / "audit").glob("*.jsonl")).read_text()
    assert '"chord": "a"' not in raw

    batch = json.loads(runtime.act_batch([{"do": "key", "chord": "shift+a"}]))
    assert batch[0]["ok"] is False and "secure_field" in batch[0]["error"]
    assert pressed == []
    raw = next((tmp_path / "audit").glob("*.jsonl")).read_text()
    assert "shift+a" not in raw

    assert runtime.key("tab").startswith("pressed tab")
    assert pressed == ["tab"]
    tab = json.loads(next((tmp_path / "audit").glob("*.jsonl")).read_text().splitlines()[-1])
    assert tab["params"]["chord"] == "tab" and tab["result"] == "ok"

    monkeypatch.setattr(_atspi, "focused_secure", lambda app: False)
    assert runtime.key("a").startswith("pressed a")
    assert pressed[-1] == "a"
    ok = json.loads(next((tmp_path / "audit").glob("*.jsonl")).read_text().splitlines()[-1])
    assert ok["result"] == "ok"
    assert ok["params"]["chord"] == "[REDACTED]" and ok["params"]["chars"] == 1
    assert '"chord": "a"' not in next((tmp_path / "audit").glob("*.jsonl")).read_text()


@pytest.mark.usefixtures("front_named_only")
def test_act_key_modifiers_fold_and_keep_the_password_refusal(
    focus_driver, monkeypatch, tmp_path
) -> None:
    """A modifiers list is part of the chord before the password probe.
    shift+a built that way is printable: refused, nothing pressed, chord
    redacted. ctrl+a built that way is a shortcut and is still pressed."""
    driver, front, _, _, _, _ = focus_driver
    pressed: list[str] = []
    sys.modules["a11y_computer_use.drivers._linux_input"].press_chord = pressed.append
    monkeypatch.setattr(_atspi, "focused_secure", lambda app: True)
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier(front["app"], safety.Tier.FULL)
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: front["app"])
    runtime = server.Runtime(
        driver=driver, store=store, audit=safety.AuditLog(tmp_path / "audit"),
    )

    refused = json.loads(runtime.act_batch([
        {"do": "key", "chord": "a", "modifiers": ["shift"]},
    ]))
    assert refused[0]["ok"] is False and "secure_field" in refused[0]["error"]
    assert pressed == []
    raw = next((tmp_path / "audit").glob("*.jsonl")).read_text()
    row = json.loads(raw.splitlines()[-1])
    assert row["result"] == "secure_field"
    assert row["params"]["chord"] == "[REDACTED]" and row["params"]["chars"] == 1
    assert "shift+a" not in raw

    sent = json.loads(runtime.act_batch([
        {"do": "key", "chord": "a", "modifiers": ["ctrl"]},
    ]))
    assert sent[0]["ok"] is True and "ctrl+a" in sent[0]["result"]
    assert pressed == ["ctrl+a"]
    shortcut = json.loads(next((tmp_path / "audit").glob("*.jsonl")).read_text().splitlines()[-1])
    assert shortcut["result"] == "ok" and shortcut["params"]["chord"] == "ctrl+a"


def test_dry_run_keeps_the_existing_focus_target(focus_driver) -> None:
    driver, _, field, handle, _, _ = focus_driver
    driver.press_element(field)
    driver.click(Point(0, 1, 1), dry_run=True)
    driver.key_chord("tab", dry_run=True)
    assert driver._focused_editable is handle


def test_xtest_type_rejects_a_readback_that_does_not_contain_the_text(focus_driver) -> None:
    """A terminal that shows the case-inverted string is not a successful type."""
    import sys

    driver, _, _, _, _, typed = focus_driver
    state = {"text": "echo "}
    fake = sys.modules["a11y_computer_use.drivers._linux_input"]

    def type_string(text: str) -> None:
        typed.append(text)
        state["text"] = "ECHO hELLO"

    fake.type_string = type_string
    monkey_focused = _atspi.focused_text
    _atspi.focused_text = lambda app: state["text"]
    _atspi.focused_editable = lambda app: None
    try:
        with pytest.raises(ComputerUseError) as exc:
            driver.type_text("Hello")
    finally:
        _atspi.focused_text = monkey_focused
        _atspi.focused_editable = lambda app: None
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "text_mismatch"
    assert typed == ["Hello"]


def test_xtest_type_still_types_when_the_selection_lookup_has_no_atspi(focus_driver, monkeypatch) -> None:
    """A host without PyGObject still types. The selection read is not required.

    macOS and Windows hermetic runs have no ``gi``. ``_focused_node`` raises
    ImportError there. That is not a failed type.
    """
    import sys

    driver, _, _, _, _, typed = focus_driver
    state = {"text": ""}
    fake = sys.modules["a11y_computer_use.drivers._linux_input"]

    def type_string(text: str) -> None:
        typed.append(text)
        state["text"] = text

    fake.type_string = type_string
    monkeypatch.setattr(_atspi, "focused_text", lambda app: state["text"])
    monkeypatch.setattr(_atspi, "focused_editable", lambda app: None)

    def missing(_app, **_kwargs):
        raise ImportError("PyGObject is not installed; the AT-SPI2 binding is unavailable")

    monkeypatch.setattr(_atspi, "_focused_node", missing)
    assert driver.type_text("Hello") == 5
    assert typed == ["Hello"]


def test_xtest_type_returns_the_count_when_the_readback_matches(focus_driver) -> None:
    import sys

    driver, _, _, _, _, typed = focus_driver
    state = {"text": ""}
    fake = sys.modules["a11y_computer_use.drivers._linux_input"]

    def type_string(text: str) -> None:
        typed.append(text)
        state["text"] = text

    fake.type_string = type_string
    _atspi.focused_text = lambda app: state["text"]
    _atspi.focused_editable = lambda app: None
    assert driver.type_text("Hello") == 5


@pytest.mark.parametrize("focused", [True, False])
def test_chromium_ref_type_presses_field_before_inserting(focus_driver, monkeypatch, focused):
    driver, _, field, handle, _, _ = focus_driver
    events = []
    for name in ("_check_modal_target", "_refuse_hidden", "_raise_handle_window", "_visible_action"):
        monkeypatch.setattr(driver, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(_atspi, "_blocks_text_replace", lambda h: False)
    monkeypatch.setattr(_atspi, "_chromium_app", lambda h: True)
    monkeypatch.setattr(_atspi, "web_entry_context", lambda h: None)
    monkeypatch.setattr(_atspi, "peer_field_texts", lambda h: {})
    monkeypatch.setattr(_atspi, "_confirm_focus_on_target", lambda h: focused)
    monkeypatch.setattr(_atspi, "grab_focus", lambda h: pytest.fail("must deliver a pointer press"))
    monkeypatch.setattr(driver, "click", lambda target: events.append(("pointer", target)))
    monkeypatch.setattr(driver, "_focus_web_entry", lambda element, h, **kwargs: h)
    monkeypatch.setattr(driver, "_type_confirmed_handle", lambda h, text: events.append(("type", text)) or len(text))
    if focused:
        assert driver.type_into(field, "abc") == 3
        assert events == [("pointer", field), ("type", "abc")]
    else:
        with pytest.raises(ComputerUseError) as error:
            driver.type_into(field, "abc")
        assert error.value.code is ErrorCode.FOCUS_LOST
        assert events == [("pointer", field)]


def test_chromium_field_press_defers_to_checked_pointer_path(focus_driver, monkeypatch):
    from a11y_computer_use.drivers import _cdp_forms

    driver, _, field, _, _, _ = focus_driver
    for name in ("_check_modal_target", "_verify_chromium_row_target", "_refuse_hidden",
                 "_refuse_calendar_click", "_refuse_expander_click", "_refuse_gnumeric_go_jump",
                 "_refuse_unverified_pdf_thumbnail"):
        monkeypatch.setattr(driver, name, lambda *args: None)
    monkeypatch.setattr(driver, "_dom_window_title", lambda h: None)
    monkeypatch.setattr(driver, "_dom_pids", lambda h: [])
    monkeypatch.setattr(_cdp_forms, "press_checkable", lambda *args, **kwargs: None)
    monkeypatch.setattr(_atspi, "_gtk4_app", lambda h: False)
    monkeypatch.setattr(_atspi, "_chromium_app", lambda h: True)
    monkeypatch.setattr(_atspi, "grab_focus", lambda h: pytest.fail("focus is not a click"))
    monkeypatch.setattr(_atspi, "do_press", lambda h: pytest.fail("must use pointer"))
    assert driver.press_element(field) is False
    assert driver.verify_click_fallback(field, "chrome") is field
    assert driver._focused_editable is None


@pytest.mark.parametrize("focused", [True, False])
def test_chromium_type_has_one_focus_wait_and_no_regrab(focus_driver, monkeypatch, focused):
    from a11y_computer_use.drivers import _page_health
    driver, _, field, handle, _, _ = focus_driver
    context = ("entry", "Name", object(), object(), object(), (None, None), (), ())
    events = []
    for name in ("_check_modal_target", "_refuse_hidden", "_raise_handle_window", "_visible_action"):
        monkeypatch.setattr(driver, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(_atspi, "_blocks_text_replace", lambda h: False)
    monkeypatch.setattr(_atspi, "_chromium_app", lambda h: True)
    monkeypatch.setattr(_atspi, "web_entry_context", lambda h: context)
    monkeypatch.setattr(_atspi, "_node_name", lambda h: "Busy Lab")
    monkeypatch.setattr(_atspi, "_doc_url", lambda h: "file:///busy.html")
    monkeypatch.setattr(_page_health, "probe", lambda *args: None)
    monkeypatch.setattr(_atspi, "peer_field_texts", lambda h: {})
    # The fake handle has no AT-SPI Text; keep the #1269 caret read hermetic
    # so the test does not need PyGObject on macOS and Windows runners.
    monkeypatch.setattr(_atspi, "entry_text_and_caret", lambda h: None)
    monkeypatch.setattr(_atspi, "_confirm_focus_on_target", lambda h: pytest.fail("second focus wait"))
    monkeypatch.setattr(_atspi, "grab_focus", lambda h: pytest.fail("pointer must not regrab"))
    monkeypatch.setattr(driver, "click", lambda target: events.append("pointer"))
    def focus(element, h, **kwargs):
        assert kwargs == {"context": context, "request_focus": False}
        events.append("wait")
        if not focused: raise _atspi._focus_lost_error("nothing typed")
        return h
    monkeypatch.setattr(driver, "_focus_web_entry", focus)
    monkeypatch.setattr(driver, "_type_confirmed_handle", lambda h, text: events.append("type") or len(text))
    if focused:
        assert driver.type_into(field, "abc") == 3
        assert events == ["pointer", "wait", "type"]
    else:
        with pytest.raises(ComputerUseError) as error:
            driver.type_into(field, "abc")
        assert error.value.detail["next"] == []
        assert "input may be queued" in error.value.message
        assert events == ["pointer", "wait"]


def test_pointer_focus_wait_accepts_original_and_keeps_existing_budget(monkeypatch):
    handle = object()
    context = ("entry", "Name", object(), object(), None, (None, None), (), ())
    sleeps = []
    monkeypatch.setattr(_atspi, "grab_focus", lambda h: pytest.fail("no focus mutation"))
    monkeypatch.setattr(_atspi, "_focus_on_target", lambda h: False)
    monkeypatch.setattr(_atspi, "_focused_accessibles", lambda doc: [])
    monkeypatch.setattr(_atspi.time, "sleep", sleeps.append)
    with pytest.raises(ComputerUseError):
        _atspi.focus_web_entry(handle, context=context, request_focus=False)
    assert len(sleeps) == _atspi._FOCUS_CONFIRM_POLLS - 1
    assert sum(sleeps) < 1.6
    monkeypatch.setattr(_atspi, "_focus_on_target", lambda h: True)
    monkeypatch.setattr(_atspi, "_state_has", lambda h, state: state == "EDITABLE")
    assert _atspi.focus_web_entry(handle, context=context, request_focus=False) is handle
