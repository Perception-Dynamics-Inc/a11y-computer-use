"""#1208: Chrome's own UI is in DIP at a scale above 1, its page in pixels."""
from __future__ import annotations

from dataclasses import replace

import pytest

from a11y_computer_use import outcome, safety, server
from a11y_computer_use.drivers import _chromium_scale

from a11y_computer_use.schema import Bounds, ComputerUseError, Display, Element, Scope, Snapshot

APP = "google-chrome"


def _el(ref, role, title, bounds, *, parent=None, selected=False, identity=()):
    return Element(
        ref, role, title, None, bounds, "s", parent=parent, path=(role,),
        selected=selected, context_identity=identity,
    )


def _chrome(scale: float, *, selected: int = 0, tabs: int = 4) -> Snapshot:
    """A frame, tab strip and toolbar in DIP, and a document in pixels."""
    elements = [
        _el("e1", "AXWindow", f"Tab {selected + 1} - Chrome", Bounds(0, 0, 0, 1000, 700)),
        _el("e2", "AXTabList", "tabs", Bounds(0, 0, 0, 1000, 41), parent="e1"),
    ]
    for index in range(tabs):
        elements.append(_el(
            f"t{index}", "AXTab", f"Tab {index + 1}",
            Bounds(0, index * 200, 0, 200, 41), parent="e2", selected=index == selected,
        ))
        elements.append(_el(
            f"c{index}", "AXButton", "Close",
            Bounds(0, index * 200 + 170, 8, 24, 24), parent=f"t{index}",
        ))
    elements += [
        _el("e3", "AXToolbar", "toolbar", Bounds(0, 0, 41, 1000, 46), parent="e1"),
        _el("r", "AXButton", "Reload", Bounds(0, 78, 46, 34, 34), parent="e3"),
        _el("web", "AXGroup", "page", Bounds(0, 0, 87, 1000, 613), parent="e1"),
        _el("doc", "AXWebArea", "page", Bounds(
            0, 0, round(87 * scale), round(1000 * scale), round(613 * scale),
        ), parent="web", identity=("document web", 1)),
        _el("link", "AXLink", "inside", Bounds(0, 10, 200, 50, 20), parent="doc"),
    ]
    return Snapshot(
        "s", Scope.WINDOW, APP, 1, 0.0, (Display(0, 1920, 1200, 1.0, True),), tuple(elements),
    )


def _by(snap: Snapshot) -> dict:
    return {el.ref: el for el in snap.elements}


@pytest.mark.parametrize("scale", [1.5, 2.0])
def test_browser_ui_bounds_are_scaled_into_device_pixels(scale: float) -> None:
    snap = _chrome(scale)
    assert _chromium_scale.device_scale(snap) == scale
    fixed = _by(_chromium_scale.scale_browser_ui(snap))
    # Reload is 78,46 34x34 DIP: its center is where the pixel is.
    assert fixed["r"].bounds == Bounds(0, round(78 * scale), round(46 * scale), round(34 * scale), round(34 * scale))
    assert fixed["t3"].bounds.x == round(600 * scale)
    assert fixed["e1"].bounds.width == round(1000 * scale)
    # The page is already in pixels, and so is everything inside it.
    assert fixed["doc"].bounds == _by(snap)["doc"].bounds
    assert fixed["link"].bounds == _by(snap)["link"].bounds


def test_scale_one_and_unknown_layouts_are_left_alone() -> None:
    snap = _chrome(1.0)
    assert _chromium_scale.scale_browser_ui(snap) is snap
    no_doc = Snapshot(
        "s", Scope.WINDOW, "gedit", 1, 0.0, (),
        tuple(el for el in _chrome(1.5).elements if el.ref not in {"doc", "link"}),
    )
    assert _chromium_scale.scale_browser_ui(no_doc) is no_doc
    # Two documents that imply different factors: do not guess.
    mixed = _chrome(1.5)
    other = _el("doc2", "AXWebArea", "x", Bounds(0, 0, 87, 2000, 100), parent="web", identity=("document web", 2))
    both = Snapshot("s", Scope.WINDOW, APP, 1, 0.0, (), mixed.elements + (other,))
    assert _chromium_scale.scale_browser_ui(both) is both


def test_a_scrollbar_trimmed_document_still_snaps_to_the_factor() -> None:
    snap = _chrome(1.5)
    trimmed = tuple(
        el if el.ref != "doc" else replace(el, bounds=Bounds(0, 0, 130, 1500 - 22, 920))
        for el in snap.elements
    )
    assert _chromium_scale.device_scale(Snapshot(
        "s", Scope.WINDOW, APP, 1, 0.0, (), trimmed,
    )) == 1.5


def test_a_tab_click_that_activated_the_neighbour_is_not_confirmed() -> None:
    before = _chrome(1.5, selected=0)
    tab4 = _by(before)["t3"]
    activated = _chrome(1.5, selected=2)
    msg = outcome.wrong_tab_effect(before, activated, tab4)
    assert msg == "clicked tab 'Tab 4' is not selected; tab 'Tab 3' was activated instead"
    # Nothing selected it at all (a click that landed on the edge).
    unchanged = _chrome(1.5, selected=0)
    assert "'Tab 4' is not selected after the click" in outcome.wrong_tab_effect(before, unchanged, tab4)
    # The right tab, and a tab that was already the selected one.
    assert outcome.wrong_tab_effect(before, _chrome(1.5, selected=3), tab4) is None
    assert outcome.wrong_tab_effect(before, before, _by(before)["t0"]) is None


def test_a_close_click_that_selected_the_tab_instead_is_not_confirmed() -> None:
    before = _chrome(1.5, selected=0)
    close = _by(before)["c1"]
    selected = _chrome(1.5, selected=1)
    assert outcome.wrong_tab_effect(before, selected, close) == (
        "the click activated tab 'Tab 2' instead of closing it; the Close button was not hit"
    )
    # Closing a background tab leaves one fewer tab and no layout to compare.
    assert outcome.wrong_tab_effect(before, _chrome(1.5, selected=0, tabs=3), close) is None
    # A Close button outside a tab is not this check's business.
    reload = _by(before)["r"]
    assert outcome.wrong_tab_effect(before, selected, reload) is None


def _runtime(tmp_path, before: Snapshot, after: Snapshot):
    class _D:
        resolves_apps = True
        name = "fake"
        i = 0

        def ensure_trusted(self):
            return None

        def frontmost_app(self):
            return (APP, 1)

        def main_display_id(self):
            return 0

        def snapshot(self, scope, app):
            del scope, app
            return after if _D.i else before

        def resolve_ref(self, snap, ref, *, live=None):
            del live
            return snap.element(ref)

        def press_element(self, element):
            return False

        def click(self, target, **kwargs):
            del target, kwargs
            _D.i += 1

    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier(APP, safety.Tier.CLICK)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_D())
    rt._current = before
    return rt


def _renumber(snap: Snapshot, snapshot_id: str) -> Snapshot:
    return replace(snap, snapshot_id=snapshot_id, elements=tuple(
        replace(el, snapshot_id=snapshot_id) for el in snap.elements
    ))


def test_click_on_a_tab_reports_the_tab_that_was_activated(tmp_path) -> None:
    before = _renumber(_chrome(1.5, selected=0), "s0")
    after = _renumber(_chrome(1.5, selected=2), "s1")
    out = _runtime(tmp_path, before, after).click(ref="t3")
    assert out.outcome != "confirmed"
    assert "Tab 3" in out.evidence and "Tab 4" in out.evidence

    landed = _renumber(_chrome(1.5, selected=3), "s1")
    out = _runtime(tmp_path, before, landed).click(ref="t3")
    assert out.outcome == "confirmed"


def test_a_ref_whose_center_is_beyond_the_display_is_refused(tmp_path) -> None:
    snap = _renumber(_chrome(1.5), "s0")
    rt = _runtime(tmp_path, snap, snap)
    rt.driver.name = "linux"
    rt._known_displays = lambda: [Display(0, 1280, 800, 1.0, True)]
    inside = _by(snap)["t0"]
    rt._refuse_center_off_display(inside)
    far = replace(_by(snap)["t3"], bounds=Bounds(0, 1500, 0, 300, 61))
    with pytest.raises(ComputerUseError) as err:
        rt._refuse_center_off_display(far)
    assert err.value.detail["reason"] == "off_screen"
