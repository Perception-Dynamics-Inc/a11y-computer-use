"""The selected GIMP brush tip takes the dock label that names it.

Hermetic. No display, no bus, no GIMP process. Brush icons publish no name,
description, or text. The selected brush is a label on the filler that also
holds the icon grid. That label becomes the selected icon's name, so the
snapshot keeps an image instead of an unlabeled opaque region. Unselected
icons stay unlabeled: the tree has no name for them. ``Hardness 075`` is not
invented.
"""

from __future__ import annotations

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.observe import DisplayGeometry, RawNode, build_snapshot
from a11y_computer_use.schema import Display, Scope


_LABEL = "2. Hardness 050 (51 × 51)"


def _geometry():
    return (DisplayGeometry(display=Display(0, 1280, 800, 1.0, True), origin=(0.0, 0.0)),)


class _Action:
    def get_n_actions(self):
        return 1

    def get_action_name(self, _index):
        return "activate"


class _Node:
    def __init__(
        self,
        role,
        name="",
        *,
        children=(),
        box=((0.0, 0.0), (40.0, 40.0)),
        selected=False,
    ):
        self.role = role
        self.name = name
        self.children = list(children)
        self.box = box
        self.selected = selected
        self.parent = None
        for child in self.children:
            child.parent = self

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_description(self):
        return ""

    def get_parent(self):
        return self.parent

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def get_action_iface(self):
        return _Action() if self.role == "icon" else None


class _Tree:
    def read(self, node):
        return _atspi.ATSPIAccessor().read(node)

    def children(self, node):
        return list(node.children)


def _patch(monkeypatch) -> None:
    def flags(acc):
        return (True, False, None, bool(getattr(acc, "selected", False)), None, True, False)

    monkeypatch.setattr(_atspi, "_state_flags", flags)
    monkeypatch.setattr(_atspi, "_extents", lambda acc, keep_zero=False: acc.box)
    # A label's value read imports PyGObject. macOS and Windows CI have no gi.
    monkeypatch.setattr(_atspi, "_value_text", lambda *args, **kwargs: None)


def _grid(labels, *, selected_name=""):
    selected = _Node(
        "icon",
        selected_name,
        box=((101.0, 160.0), (32.0, 32.0)),
        selected=True,
    )
    other = _Node("icon", box=((140.0, 160.0), (32.0, 32.0)))
    third = _Node("icon", box=((180.0, 160.0), (32.0, 32.0)))
    pane = _Node(
        "layered pane",
        children=[selected, other, third],
        box=((100.0, 141.0), (239.0, 331.0)),
    )
    scroll = _Node(
        "scroll pane",
        children=[pane],
        box=((100.0, 140.0), (241.0, 333.0)),
    )
    label_nodes = [
        _Node("label", text, box=((100.0, 120.0), (241.0, 19.0)))
        for text in labels
    ]
    filler = _Node(
        "filler",
        children=[*label_nodes, scroll],
        box=((100.0, 100.0), (241.0, 456.0)),
    )
    frame = _Node(
        "frame",
        "GIMP",
        children=[filler],
        box=((0.0, 0.0), (1280.0, 800.0)),
    )
    return frame, selected


def _snap(monkeypatch, frame):
    _patch(monkeypatch)
    return build_snapshot(
        frame, _Tree(), scope=Scope.WINDOW, app="gimp", pid=1, geometry=_geometry(),
    )


def test_selected_tip_uses_the_dock_label_and_others_stay_blank(monkeypatch) -> None:
    frame, selected = _grid([_LABEL])
    _patch(monkeypatch)
    raw = _atspi.ATSPIAccessor().read(selected)
    assert raw.role == "AXImage"
    assert raw.title == _LABEL
    assert raw.actions == ("AXPress",)
    assert raw.position == (101.0, 160.0)
    assert raw.size == (32.0, 32.0)

    snap = _snap(monkeypatch, frame)
    images = [el for el in snap.elements if el.role == "AXImage"]
    assert len(images) == 1
    assert images[0].title == _LABEL
    assert images[0].clickable
    assert images[0].bounds.x == 101
    assert images[0].bounds.y == 160
    assert images[0].bounds.width == 32
    assert images[0].bounds.height == 32
    opaque = [el for el in snap.elements if el.role == "opaque_region"]
    assert len(opaque) == 2
    assert all(el.title == "" for el in opaque)
    hits = observe.find_elements(snap, text="Hardness 050")
    assert images[0] in hits
    assert observe.find_elements(snap, text="Hardness 075") == ()
    rendered = observe.render_text(snap, mode="full")
    assert f'image "{_LABEL}"' in rendered
    assert "Hardness 075" not in rendered


def test_two_labels_or_none_leave_the_selected_icon_unnamed(monkeypatch) -> None:
    _patch(monkeypatch)
    _frame, ambiguous = _grid([_LABEL, "Brushes"])
    assert _atspi.ATSPIAccessor().read(ambiguous).title == ""
    _frame, bare = _grid([])
    assert _atspi.ATSPIAccessor().read(bare).title == ""


def test_a_generic_label_and_an_existing_icon_name_are_not_replaced(monkeypatch) -> None:
    _patch(monkeypatch)
    _frame, generic = _grid(["icon"])
    assert _atspi.ATSPIAccessor().read(generic).title == ""
    _frame, named = _grid([_LABEL], selected_name="go-down")
    raw = _atspi.ATSPIAccessor().read(named)
    assert raw.title == "go-down"


def test_a_label_on_the_page_tab_is_not_borrowed(monkeypatch) -> None:
    """The brush name has to sit on the icon's own dock filler."""
    icon = _Node("icon", box=((10.0, 10.0), (32.0, 32.0)), selected=True)
    pane = _Node("layered pane", children=[icon], box=((0.0, 0.0), (80.0, 80.0)))
    filler = _Node("filler", children=[pane], box=((0.0, 0.0), (100.0, 100.0)))
    label = _Node("label", _LABEL, box=((0.0, 0.0), (80.0, 16.0)))
    tab = _Node("page tab", children=[label, filler], box=((0.0, 0.0), (200.0, 200.0)))
    _patch(monkeypatch)
    assert _atspi.ATSPIAccessor().read(icon).title == ""
