"""Hermetic HTML popover metadata and dismissed-ref regressions."""
import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.schema import ComputerUseError, ErrorCode
from tests.test_observe import snap_of, by_title, save_window
from tests.fixtures.trees import ax, button
from tests.test_linux_status_bar_role import _FakeAccessor, _geometry
from a11y_computer_use.observe import RawNode
from a11y_computer_use.schema import Scope


class Panel:
    def get_role_name(self):
        return "panel"

    def get_name(self):
        return ""


@pytest.mark.parametrize("showing", [True, False])
@pytest.mark.parametrize("kind", ["auto", "manual", "hint", None, "false"])
def test_popover_metadata(monkeypatch, kind, showing):
    monkeypatch.setattr(_atspi, "_get_attributes", lambda node: {"ispopup": kind})
    monkeypatch.setattr(_atspi, "_state_flags", lambda node: (True, False, None, False, None, False, False))
    monkeypatch.setattr(_atspi, "_state_has", lambda node, state: showing and state == "SHOWING")
    monkeypatch.setattr(_atspi, "_extents", lambda node, **kw: ((100, 100), (300, 200)))
    monkeypatch.setattr(_atspi, "_relation_targets", lambda node, relation: ["opener"] if relation == "details_for" else [])
    monkeypatch.setattr(_atspi, "_node_name", lambda node: "Open Menu")
    raw = _atspi.ATSPIAccessor().read(Panel())
    if showing and kind in {"auto", "manual", "hint"}:
        assert raw.role == "AXPopover"
        assert raw.note == "open; opened by 'Open Menu'"
        assert raw.value is None
        root = (RawNode(role="AXWindow", title="Chrome", position=(0, 0), size=(1280, 800)),
                [(raw, [])])
        snap = observe.build_snapshot(root, _FakeAccessor(), scope=Scope.WINDOW,
                                      app="chrome", pid=1, geometry=_geometry())
        rendered = observe.render_text(snap, mode="full")
        assert "popover" in rendered
        assert "open; opened by 'Open Menu'" in rendered
    else:
        assert raw.role == "AXGroup"


def popover_tree():
    return save_window(extra=(ax("AXPopover", at=(200, 200), size=(300, 200),
                                stable_id="menu", children=[button("Alpha action", (220, 220))]),))


def test_dismissed_popover_ref_has_no_unrelated_candidates():
    old = snap_of(popover_tree())
    live = snap_of(save_window(extra=(button("New Tab", (220, 220)),)))
    with pytest.raises(ComputerUseError) as caught:
        observe.rematch_ref(old, by_title(old, "Alpha action").ref, live)
    assert caught.value.code is ErrorCode.STALE_REF
    assert caught.value.detail["reason"] == "popover_closed"
    assert caught.value.detail["candidates"] == []
    assert "Reopen" in str(caught.value)
    assert "reordered" not in str(caught.value)
    assert "New Tab" not in str(caught.value)


def test_open_popover_ref_still_resolves():
    old, live = snap_of(popover_tree()), snap_of(popover_tree())
    assert observe.rematch_ref(old, by_title(old, "Alpha action").ref, live).title == "Alpha action"


def test_another_popover_at_old_position_does_not_retain_ref():
    old = snap_of(popover_tree())
    tree = popover_tree()
    tree["children"][-1]["stable_id"] = "other-menu"
    live = snap_of(tree)
    with pytest.raises(ComputerUseError) as caught:
        observe.rematch_ref(old, by_title(old, "Alpha action").ref, live)
    assert caught.value.detail["reason"] == "popover_closed"


def test_partial_snapshot_with_omitted_parent_still_resolves():
    from dataclasses import replace

    old = snap_of(save_window())
    target = next(element for element in old.elements if element.parent)
    partial = replace(old, elements=(target,))
    assert observe.rematch_ref(partial, target.ref, old) == target
