"""Hermetic Chrome/Electron DOM form merge. No browser and no AT-SPI bus.

A debugging port is opt-in on that process. DOM values replace the
matching AT-SPI fields. Bounds and refs stay. A failed read-back is not
success, and a control the snapshot does not already have is not added.
"""

from __future__ import annotations

import pytest

from a11y_computer_use.drivers import _atspi, _cdp_forms
from a11y_computer_use.drivers._cdp_forms import FormControl
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, ComputerUseError, Display, Element, ErrorCode, Scope, Snapshot


def _bounds() -> Bounds:
    return Bounds(0, 10, 20, 80, 30)


def _element(**kwargs) -> Element:
    fields = dict(
        ref="e1",
        role="AXTextField",
        title="Name",
        value="old",
        bounds=_bounds(),
        snapshot_id="snap-1",
        stable_id="name",
        enabled=True,
        instance_id="pid:9|start:1",
    )
    fields.update(kwargs)
    return Element(**fields)


def _snapshot(*elements: Element, pid: int = 9) -> Snapshot:
    return Snapshot(
        snapshot_id="snap-1",
        scope=Scope.WINDOW,
        app="chrome",
        pid=pid,
        created_at=0.0,
        displays=(Display(0, 800, 600, 1.0, True),),
        elements=elements,
    )


def _control(kind: str = "text", **kwargs) -> FormControl:
    fields = dict(
        kind=kind, dom_id="name", name="Name", value="Ada", checked=None,
        selected=(), options=(),
    )
    fields.update(kwargs)
    return FormControl(**fields)


def test_debug_port_requires_the_flag_and_rejects_bad_types(tmp_path) -> None:
    with pytest.raises(TypeError):
        _cdp_forms.debug_port_from_cmdline("--remote-debugging-port=9333")
    assert _cdp_forms.debug_port_from_cmdline(b"chrome\x00--user-data-dir=/tmp/x\x00") is None
    assert _cdp_forms.debug_port_from_cmdline(
        b"chrome\x00--remote-debugging-port=9333\x00"
    ) == 9333
    # Chrome binds the last flag. A launcher prefix of 9222 is not the port.
    assert _cdp_forms.debug_port_from_cmdline(
        b"chrome\x00--remote-debugging-port=9222\x00--remote-debugging-port=9333\x00"
    ) == 9333
    assert _cdp_forms.debug_port_from_cmdline(b"--remote-debugging-port=0\x00") is None
    assert _cdp_forms.debug_port_from_cmdline(b"--remote-debugging-port=70000\x00") is None
    folder = tmp_path / "profile"
    folder.mkdir()
    (folder / "DevToolsActivePort").write_text("9334\n/devtools/browser/abc\n", encoding="utf-8")
    raw = f"--remote-debugging-port=0\x00--user-data-dir={folder}\x00".encode()
    assert _cdp_forms.debug_port_from_cmdline(raw) == 9334
    wrapper = tmp_path / "wrapper-profile"
    wrapper.mkdir()
    (wrapper / "DevToolsActivePort").write_text("9222\n", encoding="utf-8")
    repeated = (
        f"--remote-debugging-port=9222\x00--user-data-dir={wrapper}\x00"
        f"--user-data-dir={folder}\x00--remote-debugging-port=0\x00"
    ).encode()
    assert _cdp_forms.debug_port_from_cmdline(repeated) == 9334
    with pytest.raises(TypeError):
        _cdp_forms.port_for_pid(True)
    with pytest.raises(ValueError):
        _cdp_forms.port_for_pid(0)
    assert _cdp_forms.pid_from_instance("pid:15|start:9") == 15
    assert _cdp_forms.pid_from_instance("start:9") is None
    assert _cdp_forms.pid_from_instance(None) is None


def test_choose_socket_does_not_pick_another_tab() -> None:
    only = [{"title": "Other", "webSocketDebuggerUrl": "ws://only"}]
    assert _cdp_forms.choose_socket(only, "cuahybrid119 - Google Chrome") == "ws://only"
    pages = [
        {"title": "Inbox", "webSocketDebuggerUrl": "ws://inbox"},
        {"title": "cuahybrid119", "webSocketDebuggerUrl": "ws://form"},
    ]
    assert _cdp_forms.choose_socket(pages, "cuahybrid119 - Google Chrome") == "ws://form"
    assert _cdp_forms.choose_socket(pages, "Something else") is None
    assert _cdp_forms.choose_socket([], "") is None
    with pytest.raises(TypeError):
        _cdp_forms.choose_socket({"title": "x"}, "")


def test_malformed_dom_rows_and_geometry_are_dropped() -> None:
    assert _cdp_forms.parse_control({"kind": "text", "id": "name", "value": "Ada", "screenX": 1, "screenY": 2}) is not None
    dropped = _cdp_forms.parse_control({"kind": "text", "id": "name", "checked": "true", "value": "Ada"})
    assert dropped is None
    assert _cdp_forms.parse_control({"kind": "password", "id": "secret", "value": "hunter2"}) is None
    assert _cdp_forms.parse_control({"kind": "text", "id": "name", "value": "x" * 201}) is None
    with pytest.raises(TypeError):
        FormControl("text", "name", "Name", "Ada", 1, (), ())  # type: ignore[arg-type]


def test_apply_keeps_bounds_and_refs_and_does_not_invent_nodes() -> None:
    bounds = _bounds()
    field = _element(bounds=bounds)
    menu = _element(ref="e2", role="AXMenu", title="Name", stable_id="menu", value=None)
    city_a = _element(ref="e3", role="AXTextField", title="City", stable_id="", value="kept")
    city_b = _element(ref="e4", role="AXTextField", title="City", stable_id="", value="also")
    window = _element(ref="e0", role="AXWindow", title="cuahybrid119 - Google Chrome", stable_id="", value=None)
    snap = _snapshot(window, field, menu, city_a, city_b)
    controls = (
        _control(value="Ada"),
        _control(dom_id="", name="City", value="from-title"),
        FormControl("text", "missing", "Ghost", "nope", None, (), ()),
    )
    merged = _cdp_forms.apply_form_controls(snap, controls)
    assert len(merged.elements) == len(snap.elements)
    assert merged.snapshot_id == snap.snapshot_id
    assert merged.elements[1].value == "Ada"
    assert merged.elements[1].bounds is bounds
    assert merged.elements[1].ref == "e1"
    assert merged.elements[2].value is None
    assert merged.elements[3].value == "kept"
    assert merged.elements[4].value == "also"
    assert _cdp_forms.apply_form_controls(snap, ()) is snap
    with pytest.raises(TypeError):
        _cdp_forms.apply_form_controls(snap, [_control()])
    with pytest.raises(TypeError):
        _cdp_forms.apply_form_controls(object(), ())  # type: ignore[arg-type]


def test_select_value_and_row_selection_follow_the_dom() -> None:
    combo = _element(
        ref="e2", role="AXComboBox", title="Country", stable_id="country", value="Kazakhstan",
    )
    japan = _element(ref="e3", role="AXMenuItem", title="Japan", stable_id="", value=None, parent="e2", selected=False)
    peru = _element(ref="e4", role="AXMenuItem", title="Peru", stable_id="", value=None, parent="e2", selected=True)
    box = _element(ref="e5", role="AXCheckBox", title="Agree", stable_id="agree", value="on", checked=False)
    snap = _snapshot(combo, japan, peru, box)
    controls = (
        FormControl("select", "country", "Country", None, None, ("Japan",), ("Kazakhstan", "Japan", "Peru")),
        FormControl("checkbox", "agree", "Agree", None, True, (), ()),
    )
    merged = _cdp_forms.apply_form_controls(snap, controls)
    assert merged.elements[0].value == "Japan"
    assert merged.elements[0].bounds == combo.bounds
    assert merged.elements[1].selected is True
    assert merged.elements[2].selected is False
    assert merged.elements[3].checked is True
    assert merged.elements[3].value == "on"


def test_overlay_without_a_port_returns_the_atspi_snapshot(monkeypatch) -> None:
    snap = _snapshot(_element())
    monkeypatch.setattr(_cdp_forms, "port_for_pid", lambda pid: None)
    assert _cdp_forms.overlay_snapshot(snap) is snap
    with pytest.raises(TypeError):
        _cdp_forms.fetch_form_controls(True, "")  # type: ignore[arg-type]
    monkeypatch.setattr(_cdp_forms, "port_for_pid", lambda pid: 9333)

    def explode(port, title):
        raise ComputerUseError(ErrorCode.APP_NOT_FOUND, "down")

    monkeypatch.setattr(_cdp_forms, "fetch_form_controls", explode)
    assert _cdp_forms.overlay_snapshot(snap) is snap


def test_snapshot_merges_dom_text_and_leaves_bounds(monkeypatch) -> None:
    bounds = _bounds()
    element = _element(bounds=bounds)
    snap = _snapshot(element)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_snapshot_resolved", lambda scope, app: snap)
    monkeypatch.setattr(_cdp_forms, "port_for_pid", lambda pid: None)
    assert driver._snapshot(Scope.WINDOW, "chrome") is snap
    monkeypatch.setattr(_cdp_forms, "port_for_pid", lambda pid: 9333)
    monkeypatch.setattr(
        _cdp_forms, "fetch_form_controls",
        lambda port, title: (_control(value="Ada"),),
    )
    merged = driver._snapshot(Scope.WINDOW, "chrome")
    assert merged.elements[0].value == "Ada"
    assert merged.elements[0].bounds is bounds
    assert merged.elements[0].ref == element.ref


def test_dom_write_readback_failure_is_not_success(monkeypatch) -> None:
    element = _element()
    monkeypatch.setattr(_cdp_forms, "port_for_pid", lambda pid: 9333)
    monkeypatch.setattr(
        _cdp_forms, "_evaluate",
        lambda port, title, expression: {"found": True, "wrote": False, "value": "nope", "options": []},
    )
    with pytest.raises(ComputerUseError) as info:
        _cdp_forms.write_element(element, "Ada")
    assert info.value.code is ErrorCode.UNSUPPORTED
    assert info.value.detail["reason"] == "text_mismatch"
    monkeypatch.setattr(
        _cdp_forms, "_evaluate",
        lambda port, title, expression: {
            "found": True, "wrote": False, "reason": "not_an_option",
            "options": ["Kazakhstan", "Japan", "Peru"],
        },
    )
    combo = _element(role="AXComboBox", title="Country", stable_id="country")
    with pytest.raises(ValueError, match="Kazakhstan") as bad:
        _cdp_forms.write_element(combo, "Mars")
    assert "Japan" in str(bad.value) and "Peru" in str(bad.value)
    monkeypatch.setattr(
        _cdp_forms, "_evaluate",
        lambda port, title, expression: {"found": True, "wrote": False, "reason": "not_a_number", "min": "0", "max": "12"},
    )
    seats = _element(role="AXSpinButton", title="Seats", stable_id="seats")
    with pytest.raises(ValueError, match="not a number"):
        _cdp_forms.write_element(seats, "abc")
    with pytest.raises(TypeError):
        _cdp_forms.write_element(element, True)  # type: ignore[arg-type]
    monkeypatch.setattr(_cdp_forms, "port_for_pid", lambda pid: None)
    assert _cdp_forms.write_element(element, "Ada") is None


def test_driver_uses_dom_write_without_calling_atspi(monkeypatch) -> None:
    driver = LinuxDriver()
    element = _element()
    monkeypatch.setattr(_cdp_forms, "write_element", lambda el, value: True)

    def boom(*_args, **_kwargs):
        raise AssertionError("AT-SPI set_value ran")

    monkeypatch.setattr(_atspi, "is_sheet_cell", boom)
    assert driver._apply_set_value(element, "Ada", object()) is True
    box = _element(role="AXCheckBox", title="Agree", checked=True)
    monkeypatch.setattr(_cdp_forms, "press_checkable", lambda el: True)
    assert driver.press_element(box) is True
    monkeypatch.setattr(_cdp_forms, "press_checkable", lambda el: None)
    assert driver.press_element(box) is False
