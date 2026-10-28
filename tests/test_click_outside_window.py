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


def _floating_dock():
    """#1272: Qt 6 floating QDockWidget, a separate top-level owned by the app."""
    main = _el("e1", "AXWindow", Bounds(0, 45, 69, 900, 600))
    agent = SimpleNamespace(snapshot_id="s0", elements=(
        main,
        _el("e2", "AXGroup", Bounds(0, 45, 100, 900, 560), parent="e1"),
        _el("e3", "AXGroup", Bounds(0, 50, 110, 400, 300), parent="e2"),
        _el("e20", "AXWindow", Bounds(0, 960, 80, 260, 160)),
        _el("e21", "AXGroup", Bounds(0, 965, 110, 250, 120), parent="e20"),
        _el("e23", "AXButton", Bounds(0, 970, 182, 240, 25), parent="e21"),
    ))
    # The click target re-resolved into a fresh live tree, numbered anew.
    def live(ref, role, bounds, parent=None):
        return replace(_el(ref, role, bounds, parent), snapshot_id="s1")
    button = live("e4", "AXButton", Bounds(0, 970, 182, 240, 25), parent="e3")
    resolved = SimpleNamespace(snapshot_id="s1", elements=(
        live("e1", "AXWindow", Bounds(0, 45, 69, 900, 600)),
        live("e2", "AXWindow", Bounds(0, 960, 80, 260, 160)),
        live("e3", "AXGroup", Bounds(0, 965, 110, 250, 120), parent="e2"),
        button,
    ))
    rt = SimpleNamespace(driver=SimpleNamespace(name="linux"), _current=agent)
    return rt, resolved, button


def test_a_floating_dock_button_is_judged_against_its_own_top_level():
    rt, resolved, button = _floating_dock()
    Runtime._refuse_center_outside_window(rt, button, resolved, "e23")


def test_parents_are_never_walked_through_another_snapshots_refs():
    rt, _resolved, button = _floating_dock()
    # Without the owning tree there is no trustworthy ancestry: no refusal.
    Runtime._refuse_center_outside_window(rt, button, None, "e23")


def test_the_refusal_names_the_ref_the_caller_sent():
    rt, resolved, button = _floating_dock()
    stale = replace(button, bounds=Bounds(0, 970, 20, 240, 25))  # above "Floater"
    with pytest.raises(ComputerUseError) as err:
        Runtime._refuse_center_outside_window(rt, stale, resolved, "e23")
    assert err.value.detail["ref"] == "e23"
    assert str(err.value).startswith("e23 is outside-window")
    assert "(960, 80, 260x160)" in str(err.value)
    assert "e4" not in str(err.value)
