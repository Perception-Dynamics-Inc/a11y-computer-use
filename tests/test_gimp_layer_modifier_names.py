"""GIMP layer-dock buttons keep the visible label as their accessible name.

Hermetic. The accessible is a fake: no display, no bus, no GIMP process.
GIMP 3.0.4 leaves the button name empty and stores the tooltip, including
Shift/Ctrl lines, in the description. GTK has already stripped the markup,
so each hint line is ``Shift  ...`` with two spaces. The snapshot name is
the first line. The hint lines stay on the raw description.
"""

from __future__ import annotations

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.observe import DisplayGeometry, RawNode, build_snapshot
from a11y_computer_use.schema import Display, Scope


_NEW_LAYER = (
    "Create a new layer and add it to the image\n"
    "Shift  Create new layers with last used values"
)
_MERGE = (
    "Merge these layers with the first visible layer below each\n"
    "Shift  Merge the layer groups' layers into one normal layer\n"
    "Ctrl  Merge all visible layers into one layer\n"
    "Shift+Ctrl  Merge all visible layers with last used values"
)
_MASK = (
    "Add masks to selected layers that allows non-destructive editing of transparency\n"
    "Shift  Add mask to selected layers with last used values\n"
    "Ctrl  Remove layer masks and their effect\n"
    "Shift+Ctrl  Apply the effect of the layer masks and remove them"
)
_RESET = "Reset to default values\nShift  Reset all tool options"
_PLUGIN = (
    "Align all visible layers of the image\n"
    "This plug-in only works on the following layer types: RGB, Grayscale, Indexed."
)


def _geometry():
    return (DisplayGeometry(display=Display(0, 1280, 800, 1.0, True), origin=(0.0, 0.0)),)


class _Button:
    def __init__(self, description: str, name: str = "") -> None:
        self._name = name
        self._description = description

    def get_role_name(self):
        return "push button"

    def get_name(self):
        return self._name

    def get_description(self):
        return self._description


def _read(monkeypatch, description: str, name: str = "") -> RawNode:
    monkeypatch.setattr(
        _atspi, "_state_flags", lambda acc: (True, False, None, False, None, True, False),
    )
    monkeypatch.setattr(
        _atspi, "_extents", lambda acc, keep_zero=False: ((4.0, 700.0), (28.0, 28.0)),
    )
    return _atspi.ATSPIAccessor().read(_Button(description, name))


def test_empty_name_keeps_the_visible_label_and_stores_the_hint(monkeypatch) -> None:
    raw = _read(monkeypatch, _NEW_LAYER)
    assert raw.role == "AXButton"
    assert raw.title == "Create a new layer and add it to the image"
    assert raw.description == "Shift  Create new layers with last used values"
    assert "\n" not in raw.title


def test_shift_ctrl_and_shift_ctrl_lines_all_leave_the_name(monkeypatch) -> None:
    merged = _read(monkeypatch, _MERGE)
    assert merged.title == "Merge these layers with the first visible layer below each"
    assert merged.description == (
        "Shift  Merge the layer groups' layers into one normal layer\n"
        "Ctrl  Merge all visible layers into one layer\n"
        "Shift+Ctrl  Merge all visible layers with last used values"
    )
    masked = _read(monkeypatch, _MASK)
    assert masked.title.startswith("Add masks to selected layers")
    assert "Shift+Ctrl  Apply the effect" in masked.description
    assert "Ctrl  Remove layer masks" in masked.description
    reset = _read(monkeypatch, _RESET)
    assert reset.title == "Reset to default values"
    assert reset.description == "Shift  Reset all tool options"


def test_snapshot_find_matches_the_visible_label(monkeypatch) -> None:
    raw = _read(monkeypatch, _NEW_LAYER)
    window = (
        RawNode(role="AXWindow", title="GIMP", position=(0.0, 0.0), size=(1280.0, 800.0)),
        [(raw, [])],
    )

    class _Accessor:
        def read(self, node):
            return node[0]

        def children(self, node):
            return node[1]

    snap = build_snapshot(
        window, _Accessor(), scope=Scope.WINDOW, app="gimp", pid=1, geometry=_geometry(),
    )
    rendered = observe.render_text(snap, mode="full")
    assert 'button "Create a new layer and add it to the image"' in rendered
    assert "Shift  Create" not in rendered
    found = observe.find_elements(snap, text="Create a new layer")
    assert len(found) == 1
    assert found[0].role == "AXButton"
    assert found[0].title == "Create a new layer and add it to the image"
    assert observe.find_elements(snap, text="Shift  Create new layers") == ()


def test_single_line_description_is_unchanged(monkeypatch) -> None:
    raw = _read(monkeypatch, "Delete selected layers")
    assert raw.title == ""
    assert raw.description == "Delete selected layers"


def test_plugin_blurb_and_a_single_space_are_not_modifier_hints(monkeypatch) -> None:
    plugin = _read(monkeypatch, _PLUGIN)
    assert plugin.title == ""
    assert plugin.description == _PLUGIN
    sentence = _read(monkeypatch, "Shift the selected layer\nDown one step")
    assert sentence.title == ""
    assert sentence.description == "Shift the selected layer\nDown one step"
    one_space = _read(monkeypatch, "Move the layer\nShift the layer down")
    assert one_space.description == "Move the layer\nShift the layer down"


def test_a_name_that_already_contains_the_hint_lines_is_split(monkeypatch) -> None:
    raw = _read(monkeypatch, "", name=_NEW_LAYER)
    assert raw.title == "Create a new layer and add it to the image"
    assert raw.description == "Shift  Create new layers with last used values"


def test_an_existing_description_is_not_replaced_when_the_name_splits(monkeypatch) -> None:
    raw = _read(monkeypatch, "kept", name=_NEW_LAYER)
    assert raw.title == "Create a new layer and add it to the image"
    assert raw.description == "kept"
