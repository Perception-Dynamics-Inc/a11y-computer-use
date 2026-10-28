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


def test_qt6_unexposed_editor_is_detected_by_the_view_losing_focus(rig, monkeypatch):
    # Qt 6 never exposes the delegate editor; the view drops FOCUSED instead.
    drv, cell, keys = rig
    monkeypatch.setattr(_atspi, "qt_cell_editor", lambda acc: None)
    monkeypatch.setattr(_atspi, "qt_view_focused", lambda acc: not cell.editing)
    drv._edit_qt_cell(_element(), cell, "Bob", replace=True)
    assert cell.name == "Bob"
    assert keys == ["F2", "ctrl+a", ("type", "Bob"), "Return"]


def test_qt6_read_only_cell_keeps_the_view_focused_and_nothing_is_typed(rig, monkeypatch):
    drv, cell, keys = rig
    cell.editor_opens = False
    monkeypatch.setattr(_atspi, "qt_cell_editor", lambda acc: None)
    monkeypatch.setattr(_atspi, "qt_view_focused", lambda acc: not cell.editing)
    with pytest.raises(ComputerUseError) as err:
        drv._edit_qt_cell(_element(), cell, "Bob", replace=True)
    assert err.value.detail["reason"] == "not_editable"
    assert keys == ["F2"]
    assert cell.name == "Alice"


def test_target_cell_is_made_current_by_a_click_when_grab_focus_fails(rig, monkeypatch):
    # Qt 6 refuses grab_focus on a cell; F2 would then edit the current index.
    drv, cell, keys = rig
    clicks = []
    monkeypatch.setattr(_atspi, "grab_focus", lambda acc: False)
    monkeypatch.setattr(_atspi, "_extents", lambda acc: ((100, 40), (80, 20)))
    monkeypatch.setattr(_atspi, "_state_has", lambda acc, name: bool(clicks))
    monkeypatch.setattr(_linux_input, "click", lambda x, y, **_: clicks.append((x, y)))
    drv._edit_qt_cell(_element(), cell, "Bob", replace=True)
    assert clicks == [(140, 50)]
    assert keys[0] == "F2" and cell.name == "Bob"


def test_no_keys_are_sent_when_the_click_does_not_make_the_cell_current(rig, monkeypatch):
    drv, cell, keys = rig
    monkeypatch.setattr(_atspi, "grab_focus", lambda acc: False)
    monkeypatch.setattr(_atspi, "_extents", lambda acc: ((100, 40), (80, 20)))
    monkeypatch.setattr(_atspi, "_state_has", lambda acc, name: False)
    monkeypatch.setattr(_linux_input, "click", lambda x, y, **_: None)
    monkeypatch.setattr(drv, "_focus_lost_on",
                        lambda el: ComputerUseError(linux.ErrorCode.UNSUPPORTED, "lost",
                                                    detail={"reason": "focus_lost"}), raising=False)
    with pytest.raises(ComputerUseError):
        drv._edit_qt_cell(_element(), cell, "Bob", replace=True)
    assert keys == [] and cell.name == "Alice"


def test_type_moves_to_the_end_before_appending(rig):
    # Qt's F2 editor opens with all text selected; typing there would replace it.
    drv, cell, keys = rig
    drv._edit_qt_cell(_element(), cell, "son", replace=False)
    assert keys == ["F2", "End", ("type", "son"), "Return"]


def _judge_cell_edit(monkeypatch, drv, requested, previous, after_title):
    """Run the outcome layer after a cell edit whose new text is the title."""
    import dataclasses

    drv.name = "linux"
    el = _element()
    before_snap = SimpleNamespace(elements=[el])
    after_snap = SimpleNamespace(elements=[dataclasses.replace(el, ref="e9", title=after_title,
                                                               snapshot_id="s1")])
    rt = Runtime.__new__(Runtime)
    rt.driver = drv
    rereads = []

    def reread(*_a):
        rereads.append(1)
        return after_snap

    monkeypatch.setattr(rt, "_reread", reread)
    monkeypatch.setattr(rt, "_process_died", lambda _b: False)
    monkeypatch.setattr(rt, "_remember_baseline", lambda _s: None, raising=False)
    verdict = rt._judge_mutation("qtprobe", {"snap": before_snap, "scope": None}, el,
                                 requested, previous=previous)
    return verdict, rereads


def test_set_value_on_a_qt_cell_is_confirmed_from_the_driver_readback(rig, monkeypatch):
    # #1278 Tester: the edit landed, but the renamed cell no longer matched by
    # title, so the outcome was unverifiable after ~1.7 s of rereads.
    drv, cell, _keys = rig
    drv._edit_qt_cell(_element(), cell, "Bob", replace=True)
    (verdict, evidence), rereads = _judge_cell_edit(monkeypatch, drv, "Bob", "Alice", "Bob")
    assert verdict == "confirmed" and "Bob" in evidence
    assert len(rereads) == 1
    assert drv._cell_readback is None  # single use


def test_type_on_a_qt_cell_is_confirmed_from_the_driver_readback(rig, monkeypatch):
    drv, cell, _keys = rig
    drv._type_expected = None
    drv._edit_qt_cell(_element(), cell, "son", replace=False)
    (verdict, _ev), rereads = _judge_cell_edit(monkeypatch, drv, "son", None, "Aliceson")
    assert verdict == "confirmed"
    assert len(rereads) == 1


def test_a_cell_readback_for_another_ref_is_not_used(rig, monkeypatch):
    drv, _cell, _keys = rig
    drv._cell_readback = ("s0", "e99", "Bob")
    drv._type_expected = None
    (verdict, _ev), _r = _judge_cell_edit(monkeypatch, drv, "Bob", "Alice", "Bob")
    assert verdict != "confirmed"


def test_a_qt_list_item_also_opens_the_cell_editor(rig):
    # #1288: QListWidget items snapshot as AXRow, not AXCell.
    import dataclasses

    drv, _cell, _keys = rig
    row = dataclasses.replace(_element(), role="AXRow", path=("AXRow",))
    assert drv.opens_cell_editor(row) is True
    rt = SimpleNamespace(driver=drv)
    assert Runtime._type_target_error(rt, row) is None
    button = dataclasses.replace(_element(), role="AXButton", path=("AXButton",))
    assert drv.opens_cell_editor(button) is False


class _Node:
    def __init__(self, role, parent=None, table_cell=True, editable=False):
        self.role, self.parent = role, parent
        self.table_cell, self.editable = table_cell, editable


@pytest.mark.parametrize("role,parent_role,table_cell,expect", [
    ("list item", "list", True, True),      # QListWidget item (#1288)
    ("table cell", "table", True, True),    # QTableWidget cell (#1278)
    ("list item", "list", False, False),    # no TableCell: another toolkit's row
    ("list item", "panel", True, False),    # not inside an item view
    ("push button", "list", True, False),
])
def test_qt_item_view_cell_accepts_qt_list_items(monkeypatch, role, parent_role, table_cell, expect):
    view = _Node(parent_role)
    node = _Node(role, parent=view, table_cell=table_cell)
    monkeypatch.setattr(_atspi, "_qt_app", lambda acc: True)
    monkeypatch.setattr(_atspi, "_role_name", lambda acc: acc.role)
    monkeypatch.setattr(_atspi, "_parent_of", lambda acc: acc.parent)
    monkeypatch.setattr(_atspi, "_table_cell_iface", lambda acc: object() if acc.table_cell else None)
    monkeypatch.setattr(_atspi, "_editable_iface", lambda acc: object() if acc.editable else None)
    assert _atspi.qt_item_view_cell(node) is expect
