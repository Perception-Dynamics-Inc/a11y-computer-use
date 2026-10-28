"""#1273: a ref click under the active MDI sibling subwindow is refused as covered."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from a11y_computer_use.schema import Bounds, ComputerUseError, Element
from a11y_computer_use.server import Runtime


def _el(ref, role, bounds, parent=None, title=None, focused=False):
    return Element(ref=ref, role=role, title=title or ref, value=None, bounds=bounds,
                   snapshot_id="s0", parent=parent, path=(role,), focused=focused)


def _mdi(active="b"):
    """QMainWindow > QMdiArea > Doc A (10,10) and Doc B (60,75) over A's button."""
    elements = [
        _el("e1", "AXWindow", Bounds(0, 45, 69, 900, 600), title="MDI Lab"),
        _el("e2", "AXGroup", Bounds(0, 50, 100, 880, 560), parent="e1"),
        _el("e3", "AXWindow", Bounds(0, 60, 110, 300, 200), parent="e2", title="Doc A",
            focused=active == "a"),
        _el("e5", "AXTextArea", Bounds(0, 68, 113, 274, 25), parent="e3", title="A field"),
        _el("e6", "AXButton", Bounds(0, 68, 143, 274, 25), parent="e3", title="A button"),
        _el("e8", "AXWindow", Bounds(0, 110, 145, 300, 200), parent="e2", title="Doc B"),
        _el("e10", "AXTextArea", Bounds(0, 118, 175, 274, 25), parent="e8", title="B field",
            focused=active == "b"),
        _el("e11", "AXButton", Bounds(0, 118, 205, 274, 25), parent="e8", title="B button"),
    ]
    rt = SimpleNamespace(driver=SimpleNamespace(name="linux"),
                         _current=SimpleNamespace(snapshot_id="s0", elements=tuple(elements)))
    return rt, {e.ref: e for e in elements}


def test_a_button_under_the_active_subwindow_is_refused_as_covered():
    rt, by = _mdi()
    with pytest.raises(ComputerUseError) as err:
        Runtime._refuse_center_under_active_sibling_window(rt, by["e6"], None, "e6")
    assert err.value.detail["reason"] == "covered"
    assert err.value.detail["ref"] == "e6"
    assert "'Doc B'" in str(err.value) and "no click was sent" in str(err.value)


def test_controls_of_the_active_subwindow_and_uncovered_ones_pass():
    rt, by = _mdi()
    Runtime._refuse_center_under_active_sibling_window(rt, by["e11"], None, "e11")
    Runtime._refuse_center_under_active_sibling_window(rt, by["e5"], None, "e5")  # above Doc B


def test_once_the_target_subwindow_is_active_the_click_passes():
    rt, by = _mdi(active="a")
    Runtime._refuse_center_under_active_sibling_window(rt, by["e6"], None, "e6")


def test_unknown_z_order_and_other_snapshots_fail_open():
    rt, by = _mdi(active=None)
    Runtime._refuse_center_under_active_sibling_window(rt, by["e6"], None, "e6")
    rt, by = _mdi()
    Runtime._refuse_center_under_active_sibling_window(
        rt, replace(by["e6"], snapshot_id="s9"), None, "e6")
    rt.driver.name = "macos"
    Runtime._refuse_center_under_active_sibling_window(rt, by["e6"], None, "e6")


def test_a_separate_top_level_window_is_left_to_the_x11_cover_check():
    rt, by = _mdi(active=None)
    floater = _el("e20", "AXWindow", Bounds(0, 60, 140, 300, 100), title="Floater", focused=True)
    rt._current = SimpleNamespace(snapshot_id="s0", elements=rt._current.elements + (floater,))
    Runtime._refuse_center_under_active_sibling_window(rt, by["e6"], None, "e6")
