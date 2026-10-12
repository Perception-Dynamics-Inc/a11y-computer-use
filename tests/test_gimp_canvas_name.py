"""The GIMP image canvas keeps an honest name from the AT-SPI tree.

Hermetic. No display, no bus, no GIMP process. The canvas is a focusable
childless panel, so the pruner makes it an opaque region. Its own name and
description are empty. The name on that region is the image-title label when
exactly one label prefixes the frame title, otherwise the frame title. It is
never a made-up word such as Canvas.
"""

from __future__ import annotations

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.observe import DisplayGeometry, RawNode, build_snapshot
from a11y_computer_use.schema import Display, Scope


_FRAME = (
    "[Untitled]-1.0 (RGB color 8-bit non-linear integer, GIMP built-in sRGB, "
    "1 layer) 1920x1080 – GIMP"
)
_IMAGE = "[Untitled]-1"
_CANVAS_BOX = ((216.0, 137.0), (1280.0, 738.0))


def _geometry():
    return (
        DisplayGeometry(display=Display(0, 1920, 1200, 1.0, True), origin=(0.0, 0.0)),
    )


class _Node:
    def __init__(self, role, name="", *, children=(), box=((0.0, 0.0), (40.0, 40.0)), focusable=False):
        self.role = role
        self.name = name
        self.children = list(children)
        self.box = box
        self.focusable = focusable
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


class _Tree:
    def read(self, node):
        return _atspi.ATSPIAccessor().read(node)

    def children(self, node):
        return list(node.children)


def _patch(monkeypatch) -> None:
    def flags(acc):
        focusable = bool(getattr(acc, "focusable", False))
        return (True, False, None, False, None, focusable, False)

    def extents(acc, keep_zero=False):
        return acc.box

    monkeypatch.setattr(_atspi, "_state_flags", flags)
    monkeypatch.setattr(_atspi, "_extents", extents)
    # A label's value read imports PyGObject. macOS and Windows CI have no gi.
    monkeypatch.setattr(_atspi, "_value_text", lambda *args, **kwargs: None)


def _canvas_tree(*, image_label=True, extra_prefix=False, page_tab=True):
    canvas = _Node("panel", box=_CANVAS_BOX, focusable=True)
    decoy = _Node("label", "Background (19.3 MB)", box=((-1.0, -1.0), (10.0, 10.0)))
    holder = _Node("panel", children=[canvas, decoy], box=((196.0, 117.0), (1318.0, 804.0)))
    if page_tab:
        # The tab bar is on screen in this fixture so the pruner walks the
        # page. A negative tab extent is hoisted by ATSPIAccessor.children,
        # which this test does not use. The name walk only needs the role.
        tab = _Node("page tab", children=[holder], box=((0.0, 0.0), (1767.0, 893.0)))
        tabs = _Node("page tab list", children=[tab], box=((0.0, 0.0), (1767.0, 893.0)))
        image_branch = [tabs]
    else:
        image_branch = [holder]
    extras = []
    if image_label:
        label = _Node("label", _IMAGE, box=((-8.0, -8.0), (1.0, 1.0)))
        extras.append(_Node("filler", children=[label], box=((1.0, 1.0), (20.0, 20.0))))
    if extra_prefix:
        extras.append(_Node("label", "[Untitled]", box=((-8.0, -8.0), (1.0, 1.0))))
    menu_item = _Node("menu item", "[Untitled]", box=((-8.0, -8.0), (1.0, 1.0)))
    frame = _Node(
        "frame",
        _FRAME,
        children=[*image_branch, *extras, menu_item],
        box=((0.0, 0.0), (1767.0, 893.0)),
    )
    return frame, canvas


def _snap(monkeypatch, frame):
    _patch(monkeypatch)
    return build_snapshot(
        frame, _Tree(), scope=Scope.WINDOW, app="gimp", pid=1, geometry=_geometry(),
    )


def test_image_title_names_the_opaque_canvas(monkeypatch) -> None:
    frame, canvas = _canvas_tree()
    _patch(monkeypatch)
    raw = _atspi.ATSPIAccessor().read(canvas)
    assert raw.role == "AXGroup"
    assert raw.title == ""
    assert raw.description == ""
    assert raw.surface_name == _IMAGE
    assert raw.position == (216.0, 137.0)
    assert raw.size == (1280.0, 738.0)

    snap = _snap(monkeypatch, frame)
    opaque = [el for el in snap.elements if el.role == "opaque_region"]
    assert len(opaque) == 1
    assert opaque[0].title == _IMAGE
    assert opaque[0].clickable
    assert opaque[0].bounds.x == 216
    assert opaque[0].bounds.y == 137
    assert opaque[0].bounds.width == 1280
    assert opaque[0].bounds.height == 738
    found = observe.find_elements(snap, text=_IMAGE)
    assert opaque[0] in found
    assert observe.find_elements(snap, text="Canvas") == ()
    assert observe.find_elements(snap, text="drawing") == ()
    assert observe.find_elements(snap, text="Image display") == ()
    rendered = observe.render_text(snap, mode="full")
    assert f'opaque_region "{_IMAGE}"' in rendered
    assert "Canvas" not in rendered


def test_frame_title_is_used_when_no_image_label_matches(monkeypatch) -> None:
    frame, canvas = _canvas_tree(image_label=False)
    _patch(monkeypatch)
    raw = _atspi.ATSPIAccessor().read(canvas)
    assert raw.surface_name == _FRAME
    snap = _snap(monkeypatch, frame)
    opaque = next(el for el in snap.elements if el.role == "opaque_region")
    assert opaque.title == _FRAME
    assert opaque.title not in {"Canvas", "Image display", "drawing", "drawing area"}


def test_two_prefix_labels_fall_back_to_the_frame_title(monkeypatch) -> None:
    frame, canvas = _canvas_tree(extra_prefix=True)
    _patch(monkeypatch)
    assert _atspi.ATSPIAccessor().read(canvas).surface_name == _FRAME


def test_a_panel_outside_a_page_tab_stays_unnamed(monkeypatch) -> None:
    frame, canvas = _canvas_tree(page_tab=False)
    _patch(monkeypatch)
    raw = _atspi.ATSPIAccessor().read(canvas)
    assert raw.surface_name == ""
    snap = _snap(monkeypatch, frame)
    opaque = [el for el in snap.elements if el.role == "opaque_region"]
    assert len(opaque) == 1
    assert opaque[0].title == ""


def test_a_panel_with_a_child_is_not_given_a_surface_name(monkeypatch) -> None:
    child = _Node("push button", "OK", box=((20.0, 20.0), (40.0, 24.0)))
    panel = _Node(
        "panel",
        children=[child],
        box=((10.0, 10.0), (200.0, 80.0)),
        focusable=True,
    )
    tab = _Node("page tab", children=[panel])
    frame = _Node("frame", _FRAME, children=[tab], box=((0.0, 0.0), (800.0, 600.0)))
    _patch(monkeypatch)
    assert _atspi.ATSPIAccessor().read(panel).surface_name == ""


def test_surface_name_labels_a_dead_region_without_blocking_demotion() -> None:
    """The inherited title is not the node's own content.

    A childless group with only ``surface_name`` still becomes an opaque
    region, and that region shows the inherited title. A group that already
    has a name stays a group. A canvas keeps its own title. A 10 by 10
    spacer is not promoted to a clickable region.
    """
    window = (
        RawNode(role="AXWindow", title="Pixels", position=(0.0, 0.0), size=(800.0, 600.0)),
        [
            (
                RawNode(
                    role="AXGroup",
                    position=(40.0, 40.0),
                    size=(180.0, 80.0),
                    surface_name=_IMAGE,
                ),
                [],
            ),
            (
                RawNode(
                    role="AXGroup",
                    title="Sidebar",
                    position=(240.0, 40.0),
                    size=(180.0, 80.0),
                    surface_name="ignored",
                ),
                [],
            ),
            (
                RawNode(
                    role="AXGroup",
                    position=(40.0, 140.0),
                    size=(10.0, 10.0),
                    surface_name="tiny",
                ),
                [],
            ),
            (
                RawNode(
                    role="AXCanvas",
                    title="Game board",
                    position=(40.0, 240.0),
                    size=(200.0, 100.0),
                    surface_name="other",
                ),
                [],
            ),
            (
                RawNode(
                    role="AXCanvas",
                    position=(40.0, 360.0),
                    size=(120.0, 80.0),
                    surface_name=_IMAGE,
                ),
                [],
            ),
        ],
    )

    class _Accessor:
        def read(self, node):
            return node[0]

        def children(self, node):
            return node[1]

    snap = build_snapshot(
        window,
        _Accessor(),
        scope=Scope.WINDOW,
        app="gimp",
        pid=1,
        geometry=_geometry(),
    )
    opaque = [el for el in snap.elements if el.role == "opaque_region"]
    assert sorted(el.title for el in opaque) == ["Game board", _IMAGE, _IMAGE]
    named = next(el for el in opaque if el.bounds.y == 40)
    assert named.title == _IMAGE
    assert named.clickable
    assert named.bounds.width == 180 and named.bounds.height == 80
    assert any(el.role == "AXGroup" and el.title == "Sidebar" for el in snap.elements)
    spacer = next(el for el in snap.elements if el.bounds.width == 10)
    assert spacer.role == "AXGroup"
    assert spacer.title == ""
    assert not spacer.clickable
