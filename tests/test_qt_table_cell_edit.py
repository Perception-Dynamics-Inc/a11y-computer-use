"""#1278: Qt 6 table cells edit through F2, not a not_editable refusal."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi, _linux_input, linux
from a11y_computer_use.schema import Bounds, ComputerUseError, Element
from a11y_computer_use.server import Runtime


class _Cell:
    def __init__(self, name, editor_opens=True):
        self.name = name
        self.editor_opens = editor_opens
        self.editing = False
        self.buffer = ""


def _element():
    return Element(ref="e7", role="AXCell", title="Alice", value=None,
                   bounds=Bounds(0, 10, 10, 80, 20), snapshot_id="s0", path=("AXCell",))


@pytest.fixture
def rig(monkeypatch):
    cell = _Cell("Alice")
    keys = []

    def chord(c):
        keys.append(c)
        if c == "F2" and cell.editor_opens:
            cell.editing, cell.buffer = True, cell.name
        elif c == "ctrl+a" and cell.editing:
            cell.buffer = ""
        elif c == "Return" and cell.editing:
            cell.editing, cell.name = False, cell.buffer

    def type_string(text, **_):
        keys.append(("type", text))
        if cell.editing:
            cell.buffer += text

    monkeypatch.setattr(linux, "_on_wayland", lambda: False)
    monkeypatch.setattr(_linux_input, "press_chord", chord)
    monkeypatch.setattr(_linux_input, "type_string", type_string)
    monkeypatch.setattr(observe, "ax_handle_for", lambda sid, ref: cell)
    monkeypatch.setattr(_atspi, "qt_item_view_cell", lambda acc: acc is cell)
    monkeypatch.setattr(_atspi, "qt_cell_editor", lambda acc: object() if cell.editing else None)
    monkeypatch.setattr(_atspi, "_node_name", lambda acc: acc.name)
    monkeypatch.setattr(_atspi, "grab_focus", lambda acc: True)
    monkeypatch.setattr(_atspi, "_drop_cached_properties", lambda acc: None)
    monkeypatch.setattr(linux.time, "sleep", lambda s: None)
    drv = object.__new__(linux.LinuxDriver)
    drv._run = lambda fn: fn()
    return drv, cell, keys


def test_set_value_replaces_a_qt_cell_through_its_editor(rig):
    drv, cell, keys = rig
    drv._edit_qt_cell(_element(), cell, "Bob", replace=True)
    assert cell.name == "Bob"
    assert keys == ["F2", "ctrl+a", ("type", "Bob"), "Return"]


def test_type_appends_to_a_qt_cell(rig):
    drv, cell, _keys = rig
    drv._edit_qt_cell(_element(), cell, "son", replace=False)
    assert cell.name == "Aliceson"


def test_a_read_only_cell_is_refused_with_nothing_typed(rig):
    drv, cell, keys = rig
    cell.editor_opens = False
    with pytest.raises(ComputerUseError) as err:
        drv._edit_qt_cell(_element(), cell, "Bob", replace=True)
    assert err.value.detail["reason"] == "not_editable"
    assert keys == ["F2"]
    assert cell.name == "Alice"


def test_runtime_lets_type_reach_a_qt_cell(rig):
    drv, _cell, _keys = rig
    assert drv.opens_cell_editor(_element()) is True
    rt = SimpleNamespace(driver=drv)
    assert Runtime._type_target_error(rt, _element()) is None
    rt = SimpleNamespace(driver=SimpleNamespace())
    assert Runtime._type_target_error(rt, _element()).detail["reason"] == "not_editable"
