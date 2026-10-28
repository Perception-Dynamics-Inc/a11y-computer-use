"""#1259: a pointer click at a center outside the ref's own window is refused."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from a11y_computer_use.schema import Bounds, ComputerUseError, Element
from a11y_computer_use.server import Runtime


def _el(ref, role, bounds, parent=None):
    return Element(ref=ref, role=role, title=ref, value=None, bounds=bounds,
                   snapshot_id="s0", parent=parent, path=(role,))


WINDOW = _el("e0", "AXWindow", Bounds(0, 100, 58, 800, 600))
PANE = _el("e1", "AXGroup", Bounds(0, 100, 100, 800, 558), parent="e0")


def _fake(driver="linux"):
    btn = _el("e2", "AXButton", Bounds(0, 200, 300, 80, 30), parent="e1")
    return SimpleNamespace(driver=SimpleNamespace(name=driver),
                           _current=SimpleNamespace(elements=(WINDOW, PANE, btn))), btn


def test_a_ref_inside_its_window_passes():
    rt, btn = _fake()
    Runtime._refuse_center_outside_window(rt, btn)


def test_stale_bounds_above_the_window_are_refused():
    rt, btn = _fake()
    stale = replace(btn, bounds=Bounds(0, 200, -6, 80, 28))  # Firefox after zoom out
    with pytest.raises(ComputerUseError) as err:
        Runtime._refuse_center_outside_window(rt, stale)
    assert err.value.detail["reason"] == "outside_window"
    # The center is outside the window; whether the bounds are stale is not known.
    message = str(err.value)
    assert "no click was sent" in message
    assert "app reported stale bounds" not in message
    assert err.value.detail["ref"] == "e2"


def test_other_drivers_and_unparented_refs_are_left_alone():
    rt, btn = _fake("macos")
    Runtime._refuse_center_outside_window(rt, replace(btn, bounds=Bounds(0, 200, 0, 80, 10)))
    rt, btn = _fake()
    Runtime._refuse_center_outside_window(rt, replace(btn, parent=None, bounds=Bounds(0, 200, 0, 80, 10)))
