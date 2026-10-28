"""#1287: set_value checks a checkable Qt item through space, verified live."""
from __future__ import annotations

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi, _linux_input, linux
from a11y_computer_use.schema import Bounds, ComputerUseError, Element


class _Item:
    def __init__(self, checked=False, focusable=True, toggles=True):
        self.checked, self.current = checked, False
        self.focusable, self.toggles = focusable, toggles


def _element():
    return Element(ref="e4", role="AXRow", title="Cherry", value=None,
                   bounds=Bounds(0, 10, 10, 80, 20), snapshot_id="s0", path=("AXRow",))


@pytest.fixture
def rig(monkeypatch):
    item = _Item()
    keys, clicks = [], []

    def chord(c):
        keys.append(c)
        if c == "space" and item.current and item.toggles:
            item.checked = not item.checked

    def state(acc, name):
        return {"CHECKED": acc.checked, "FOCUSED": acc.current, "SELECTED": acc.current,
                "CHECKABLE": True}.get(name, False)

    def focus(acc):
        acc.current = acc.focusable
        return acc.focusable

    monkeypatch.setattr(_linux_input, "press_chord", chord)
    monkeypatch.setattr(_linux_input, "click", lambda x, y, **_: clicks.append((x, y)))
    monkeypatch.setattr(_atspi, "_state_has", state)
    monkeypatch.setattr(_atspi, "grab_focus", focus)
    monkeypatch.setattr(_atspi, "_drop_cached_properties", lambda acc: None)
    monkeypatch.setattr(_atspi, "_extents", lambda acc: ((100, 40), (80, 20)))
    monkeypatch.setattr(observe, "ax_handle_for", lambda sid, ref: item)
    monkeypatch.setattr(linux.time, "sleep", lambda s: None)
    drv = object.__new__(linux.LinuxDriver)
    drv._run = lambda fn: fn()
    return drv, item, keys, clicks


def test_checks_through_space_and_reports_the_live_state(rig):
    drv, item, keys, _ = rig
    drv._set_qt_item_checked(_element(), item, True)
    assert item.checked and keys == ["space"]
    assert drv.take_check_readback(_element()) == (True, True)
    assert drv.take_check_readback(_element()) is None  # single use


def test_already_in_state_sends_nothing(rig):
    drv, item, keys, _ = rig
    item.checked = True
    drv._set_qt_item_checked(_element(), item, True)
    assert keys == [] and drv.take_check_readback(_element()) == (True, False)


def test_a_click_on_the_center_makes_the_item_current_when_focus_fails(rig):
    drv, item, keys, clicks = rig
    item.focusable = False

    def click(x, y, **_):
        clicks.append((x, y))
        item.current = True

    _linux_input.click = click
    drv._set_qt_item_checked(_element(), item, True)
    assert clicks == [(140, 50)] and keys == ["space"] and item.checked


def test_no_key_when_the_item_never_becomes_current(rig):
    drv, item, keys, _ = rig
    item.focusable = False
    with pytest.raises(ComputerUseError) as err:
        drv._set_qt_item_checked(_element(), item, True)
    assert err.value.detail["reason"] == "focus_lost" and keys == []


def test_a_space_that_does_not_toggle_raises(rig):
    drv, item, _keys, _ = rig
    item.toggles = False
    with pytest.raises(ComputerUseError) as err:
        drv._set_qt_item_checked(_element(), item, True)
    assert err.value.detail["reason"] == "check_unchanged"
    assert drv.take_check_readback(_element()) is None


def test_a_readback_for_another_ref_is_not_used(rig):
    drv, item, _keys, _ = rig
    drv._set_qt_item_checked(_element(), item, True)
    import dataclasses
    assert drv.take_check_readback(dataclasses.replace(_element(), ref="e9")) is None


@pytest.mark.parametrize("role,checkable,qt,expect", [
    ("list item", True, True, True),
    ("table cell", True, True, True),
    ("list item", False, True, False),
    ("list item", True, False, False),
    ("push button", True, True, False),
])
def test_qt_checkable_item(monkeypatch, role, checkable, qt, expect):
    monkeypatch.setattr(_atspi, "_qt_app", lambda acc: qt)
    monkeypatch.setattr(_atspi, "_role_name", lambda acc: role)
    monkeypatch.setattr(_atspi, "_state_has", lambda acc, n: checkable and n == "CHECKABLE")
    assert _atspi.qt_checkable_item(object()) is expect
