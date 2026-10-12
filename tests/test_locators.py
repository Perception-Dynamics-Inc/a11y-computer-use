"""Durable locators. No live desktop."""

from __future__ import annotations

import pytest

from a11y_computer_use.locators import (
    apply_selector,
    locator_for,
    render_with_locators,
    resolve_locator,
)
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, ErrorCode, Scope, Snapshot


def el(ref, role, title, **kwargs) -> Element:
    parent = kwargs.pop("parent", None)
    path = kwargs.pop("path", ())
    return Element(
        ref=ref,
        role=role,
        title=title,
        value=None,
        bounds=Bounds(0, 10, 10, 40, 20),
        snapshot_id="snap",
        parent=parent,
        path=path,
        **kwargs,
    )


def tree(*elements: Element, snapshot_id: str = "snap") -> Snapshot:
    return Snapshot(snapshot_id, Scope.WINDOW, "demo", 1, 0.0, (), tuple(elements))


def save_dialog(*, button_ref: str = "e3", other_ref: str = "e4") -> tuple[Element, ...]:
    window = el("e1", "AXWindow", "Editor", path=("AXWindow",))
    dialog = el("e2", "AXDialog", "Save As", parent="e1", path=("AXWindow", "AXDialog"))
    button = el(
        button_ref, "AXButton", "Save", parent="e2",
        path=("AXWindow", "AXDialog", "AXButton"), stable_id="save-main", clickable=True,
    )
    other = el(
        other_ref, "AXButton", "Save", parent="e1",
        path=("AXWindow", "AXButton"), clickable=True,
    )
    return window, dialog, button, other


def test_locator_matches_the_replay_shape_and_is_emitted_beside_the_ref() -> None:
    elements = save_dialog()
    snap = tree(*elements)
    button = elements[2]
    locator = locator_for(button, snap)
    assert locator["role"] == "AXButton"
    assert locator["name"] == "Save"
    assert locator["ref"] == "e3"
    assert locator["stable_id"] == "save-main"
    assert locator["path"] == ["AXWindow", "AXDialog", "AXButton"]
    assert locator["selector"] == (
        'AXWindow:"Editor" >> AXDialog:"Save As" >> AXButton:"Save"@save-main'
    )
    rendered = render_with_locators(snap)
    line = next(item for item in rendered.splitlines() if item.strip().startswith("e3 "))
    assert 'e3 button "Save" (click)  locator=' in line
    assert line.endswith('AXWindow:"Editor" >> AXDialog:"Save As" >> AXButton:"Save"@save-main')


def test_selector_survives_a_resnapshot_and_ignores_a_reused_ref() -> None:
    original = tree(*save_dialog())
    locator = locator_for(original.element("e3"), original)
    # New epoch. e3 is now the other Save. The dialog button is e9.
    window = el("e7", "AXWindow", "Editor", path=("AXWindow",))
    dialog = el("e8", "AXDialog", "Save As", parent="e7", path=("AXWindow", "AXDialog"))
    moved = el(
        "e9", "AXButton", "Save", parent="e8",
        path=("AXWindow", "AXDialog", "AXButton"), stable_id="save-main", clickable=True,
    )
    reused = el(
        "e3", "AXButton", "Save", parent="e7",
        path=("AXWindow", "AXButton"), clickable=True,
    )
    live = tree(window, dialog, moved, reused, snapshot_id="snap-2")
    found = resolve_locator(live, locator)
    assert found.ref == "e9"
    assert found.stable_id == "save-main"
    bound = apply_selector({"selector": locator["selector"], "x": 532, "y": 205, "display_id": 0}, live)
    assert bound == {"ref": "e9"}
    assert "x" not in bound and "y" not in bound


def test_replay_dict_without_a_selector_resolves_by_role_name_and_path() -> None:
    elements = save_dialog()
    original = tree(*elements)
    emitted = locator_for(original.element("e3"), original)
    replay_shape = {
        key: emitted[key]
        for key in ("role", "name", "ref", "stable_id", "path")
    }
    window = el("e1", "AXWindow", "Editor", path=("AXWindow",))
    dialog = el("e2", "AXDialog", "Save As", parent="e1", path=("AXWindow", "AXDialog"))
    button = el(
        "e20", "AXButton", "Save", parent="e2",
        path=("AXWindow", "AXDialog", "AXButton"), stable_id="save-main", clickable=True,
    )
    found = resolve_locator(tree(window, dialog, button), replay_shape)
    assert found.ref == "e20"


def test_ambiguous_name_is_not_resolved() -> None:
    window = el("e1", "AXWindow", "Editor", path=("AXWindow",))
    first = el("e2", "AXButton", "Save", parent="e1", path=("AXWindow", "AXButton"), clickable=True)
    second = el("e3", "AXButton", "Save", parent="e1", path=("AXWindow", "AXButton"), clickable=True)
    snap = tree(window, first, second)
    locator = locator_for(first, snap)
    with pytest.raises(ComputerUseError) as raised:
        resolve_locator(snap, locator["selector"])
    assert raised.value.code is ErrorCode.STALE_REF
    assert "more than one" in raised.value.message


def test_push_button_alias_and_quoted_name() -> None:
    window = el("e1", "AXWindow", 'Say "hi"', path=("AXWindow",))
    button = el("e2", "AXButton", "Save", parent="e1", path=("AXWindow", "AXButton"), clickable=True)
    snap = tree(window, button)
    selector = locator_for(button, snap)["selector"]
    assert selector == 'AXWindow:"Say \\"hi\\"" >> AXButton:"Save"'
    assert resolve_locator(snap, 'AXWindow:"Say \\"hi\\"" >> push button:"Save"').ref == "e2"


def test_canvas_selector_is_not_a_coordinate_click() -> None:
    window = el("e1", "AXWindow", "Editor", path=("AXWindow",))
    canvas = el("e2", "AXCanvas", "Drawing", parent="e1", path=("AXWindow", "AXCanvas"))
    snap = tree(window, canvas)
    selector = locator_for(canvas, snap)["selector"]
    with pytest.raises(ComputerUseError) as raised:
        apply_selector({"selector": selector, "x": 10, "y": 12}, snap)
    assert raised.value.code is ErrorCode.STALE_REF
    assert "coordinates" in raised.value.message


def test_stable_id_name_change_is_not_another_control() -> None:
    window = el("e1", "AXWindow", "Editor", path=("AXWindow",))
    button = el(
        "e2", "AXButton", "Discard", parent="e1",
        path=("AXWindow", "AXButton"), stable_id="save-main", clickable=True,
    )
    snap = tree(window, button)
    locator = {
        "role": "AXButton",
        "name": "Save",
        "stable_id": "save-main",
        "path": ["AXWindow", "AXButton"],
        "ref": "e2",
    }
    with pytest.raises(ComputerUseError) as raised:
        resolve_locator(snap, locator)
    assert "name changed" in raised.value.message
