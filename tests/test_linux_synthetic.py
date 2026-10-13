"""Linux backend — synthetic coverage that runs on ANY OS (no AT-SPI bus).

The live AT-SPI walk needs a real accessibility bus (see test_linux_live.py, CI
only). Here we pin the platform-free parts of the Linux backend that don't need
gi/Atspi: the AT-SPI->AX role vocabulary, that vocabulary flowing through the
SHARED pruning engine exactly as macOS/Windows do, and the chord parser/keysym
map. These catch mapping regressions on every developer's machine.
"""

from __future__ import annotations

from types import SimpleNamespace as _NS

import math

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi, _linux_input
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.observe import DisplayGeometry, RawNode, build_snapshot
from a11y_computer_use.schema import (
    Bounds,
    ComputerUseError,
    Display,
    Element,
    ErrorCode,
    Point,
    Scope,
    ScrollUnit,
    Snapshot,
)


def test_role_map_covers_common_atspi_roles() -> None:
    r = _atspi._ROLE
    assert r["push button"] == "AXButton"
    assert r["entry"] == "AXTextField"
    assert r["password text"] == "AXSecureTextField"
    assert r["link"] == "AXLink"
    assert r["check box"] == "AXCheckBox"
    assert r["table cell"] == "AXCell"
    assert r["table column header"] == "AXColumn"
    assert r["table row header"] == "AXRow"
    assert r["table row"] == "AXRow"
    assert r["radio button"] == "AXRadioButton"
    assert r["frame"] == "AXWindow"
    assert r["menu item"] == "AXMenuItem"
    assert r["separator"] == "AXSplitter"  # decorative -> dropped by the engine
    assert r["terminal"] == "AXTextArea"  # VTE: the screen text is its Text iface, else the tab is empty
    assert r["internal frame"] == "AXGroup"
    assert _atspi.atspi_web_kind("document web") == "page"
    assert _atspi.atspi_web_kind("document frame") == "docframe"
    assert _atspi.atspi_web_kind("internal frame") == "iframe"
    assert _atspi.atspi_web_kind("section") == ""


class _FakeAccessor:
    """A TreeAccessor over (RawNode, [children]) tuples — stands in for the live
    ATSPIAccessor so build_snapshot can be exercised without an AT-SPI bus. The
    RawNodes carry the Linux role vocabulary that _atspi.read would produce."""

    def read(self, node):
        return node[0]

    def children(self, node):
        return node[1]


def _geometry():
    return (DisplayGeometry(display=Display(0, 1280, 800, 1.0, True), origin=(0.0, 0.0)),)


def _node(role, title="", *, value=None, actions=(), pos=(0.0, 0.0), size=(1200.0, 700.0),
          children=(), atspi_web="", focusable=False, stable_id=None):
    return (
        RawNode(
            role=role, title=title, value=value, actions=actions, position=pos, size=size,
            atspi_web=atspi_web, focusable=focusable, stable_id=stable_id,
        ),
        list(children),
    )


def _atspi_actions(*names: str) -> tuple[str, ...]:
    """What `_action_names` stores for these AT-SPI action names."""

    class _Action:
        def get_n_actions(self):
            return len(names)

        def get_action_name(self, index):
            return names[index]

    class _Acc:
        def get_action_iface(self):
            return _Action()

    return _atspi._action_names(_Acc())


def test_meld_gutter_is_opaque_and_header_merge_controls_remain_addressable() -> None:
    """Model the reported GTK tree, not the arrows painted inside its gutter.

    The supported alternative documented in linux-port.md depends on retaining
    the real named buttons and pane text without inventing gutter children.
    """
    commands = (
        "Go to the next change",
        "Go to the previous change",
        "Push current change to the left",
        "Push current change to the right",
        "Delete change",
    )
    tree = _node(_atspi._ROLE["frame"], "left.txt : right.txt", children=(
        *(_node(_atspi._ROLE["push button"], name,
                actions=_atspi_actions("click"), pos=(float(i * 100), 0.0),
                size=(90.0, 30.0)) for i, name in enumerate(commands)),
        _node("AXTextArea", value="left text", pos=(0.0, 50.0), size=(500.0, 600.0)),
        _node(_atspi._ROLE["drawing area"], pos=(500.0, 50.0), size=(50.0, 600.0)),
        _node("AXTextArea", value="right text", pos=(550.0, 50.0), size=(500.0, 600.0)),
    ))
    snap = build_snapshot(tree, _FakeAccessor(), scope=Scope.WINDOW,
                          app="meld", pid=510, geometry=_geometry())

    regions = observe.find_elements(snap, role="opaque_region")
    assert len(regions) == 1
    gutter = regions[0]
    assert gutter.title == ""
    assert gutter.bounds == Bounds(0, 500, 50, 50, 600)
    assert not any(el.parent == gutter.ref for el in snap.elements)
    buttons = observe.find_elements(snap, role="button")
    assert {el.title for el in buttons} == set(commands)
    assert all(el.clickable and el.ref for el in buttons)
    assert {el.value for el in snap.elements if el.role == "AXTextArea"} == {
        "left text", "right text",
    }
    rendered = observe.render_text(snap, mode="interactive")
    assert f"{gutter.ref} opaque_region" in rendered
    assert all(name in rendered for name in commands)


def test_canvas_drawing_area_and_canvas_tag_read_as_a_canvas(monkeypatch) -> None:
    """AT-SPI canvas, GTK drawing area, and a ``<canvas>`` section are AXCanvas.

    WebGL is the graphics-document role on that same element. An image stays
    an image; the pruner, not this reader, decides ``opaque_region``.
    """
    monkeypatch.setattr(_atspi, "_extents", lambda acc, keep_zero=False: ((8.0, 9.0), (120.0, 80.0)))
    monkeypatch.setattr(
        _atspi, "_state_flags", lambda acc: (True, False, None, False, None, False, False),
    )
    monkeypatch.setattr(_atspi, "_action_names", lambda acc: ())
    monkeypatch.setattr(_atspi, "_call_first", lambda acc, names, *args, default=None: "")

    def read(role: str, attrs: dict | None = None):
        node = type("Node", (), {"role": role, "attrs": attrs or {}})()
        monkeypatch.setattr(_atspi, "_role_name", lambda acc: acc.role)
        monkeypatch.setattr(_atspi, "_get_attributes", lambda acc: dict(acc.attrs))
        return _atspi.ATSPIAccessor().read(node)

    assert read("canvas").role == "AXCanvas"
    assert read("drawing area").role == "AXCanvas"
    assert read("section", {"tag": "canvas"}).role == "AXCanvas"
    assert read("panel", {"xml-roles": "graphics-document"}).role == "AXCanvas"
    assert read("image").role == "AXImage"
    assert read("push button", {"tag": "button"}).role == "AXButton"
    assert read("panel").browser_chrome is False


def test_browser_frame_view_is_chrome_ui_and_not_the_page() -> None:
    """Synthetic Chrome tree. No live browser.

    A childless panel beside the document is browser UI. A canvas inside
    the document web is page content, even when the frame above it was
    already marked as browser UI. The omnibox popup document is browser UI.
    """

    class _Node:
        def __init__(self, role, parent=None, url=None):
            self.role = role
            self.parent = parent
            self.url = url

        def get_role_name(self):
            return self.role

        def get_parent(self):
            return self.parent

        def get_name(self):
            return ""

        def get_description(self):
            return ""

        def get_hyperlink(self):
            return self if self.url else None

        def get_uri(self, _index):
            return self.url

    app = _Node("application")
    frame = _Node("frame", app)
    toolbar = _Node("panel", frame)
    doc = _Node("document web", frame, url="http://example.test/f117")
    section = _Node("section", doc)
    canvas = _Node("canvas", section)
    cache: dict[int, bool] = {}
    assert _atspi.browser_frame_view(canvas, cache, role_str="canvas") is False
    assert _atspi.browser_frame_view(section, cache) is False
    assert _atspi.browser_frame_view(doc, cache) is False
    assert _atspi.browser_frame_view(toolbar, cache, role_str="panel") is True
    assert _atspi.browser_frame_view(frame, cache) is True
    popup = _Node("document web", frame, url="chrome://omnibox-popup.top-chrome/omnibox")
    assert _atspi.browser_frame_view(popup, {}, role_str="document web") is True


def test_browser_frame_view_reuses_the_object_path(monkeypatch) -> None:
    """A second proxy with the same path does not walk parents again.

    Synthetic nodes. ``get_parent`` on a live accessible returns a new
    wrapper, so the cache is the object path, not ``id()``.
    """

    class _Node:
        def __init__(self, role, parent=None, path=None):
            self.role = role
            self.parent = parent
            self.path = path
            self.parent_reads = 0

        def get_role_name(self):
            return self.role

        def get_parent(self):
            self.parent_reads += 1
            return self.parent

        def get_name(self):
            return ""

    def path_of(acc):
        return getattr(acc, "path", None)

    monkeypatch.setattr(_atspi, "_atspi_object_path", path_of)
    app = _Node("application", path="/org/a11y/atspi/app")
    frame = _Node("frame", app, path="/org/a11y/atspi/frame")
    first = _Node("panel", frame, path="/org/a11y/atspi/panel")
    second = _Node("panel", frame, path="/org/a11y/atspi/panel")
    cache: dict[object, bool] = {}
    assert _atspi.browser_frame_view(first, cache, role_str="panel") is True
    assert frame.parent_reads == 1
    assert _atspi.browser_frame_view(second, cache, role_str="panel") is True
    assert second.parent_reads == 0
    assert frame.parent_reads == 1


def test_atspi_vocabulary_flows_through_shared_engine() -> None:
    """A Linux-shaped tree (window > button + entry) prunes/indexes through the
    identical engine macOS uses: AXWindow root, a clickable button, an editable
    field, pre-order refs e1..eN."""
    tree = _node(
        "AXWindow", "Text Editor",
        pos=(0.0, 0.0), size=(1280.0, 800.0),
        children=[
            _node("AXButton", "Save", actions=("AXPress",), pos=(10.0, 10.0), size=(80.0, 30.0)),
            _node("AXTextField", "Body", value="hello", pos=(10.0, 50.0), size=(1200.0, 700.0)),
        ],
    )
    snap = build_snapshot(tree, _FakeAccessor(), scope=Scope.WINDOW, app="gedit", pid=42,
                          geometry=_geometry())
    roles = {el.role for el in snap.elements}
    assert "AXWindow" in roles
    assert snap.elements[0].ref == "e1"
    assert any(el.clickable and el.title == "Save" for el in snap.elements)
    assert any(el.editable and el.value == "hello" for el in snap.elements)
    assert snap.app == "gedit" and snap.pid == 42


def _lo_node(role, name, screen, window, parent=None, children=()):
    kids = list(children)

    class _Node:
        def get_role_name(self):
            return role

        def get_name(self):
            return name

        def get_parent(self):
            return parent

        def get_child_count(self):
            return len(kids)

        def get_child_at_index(self, index):
            return kids[int(index)]

        def get_component_iface(self):
            return self

        def get_extents(self, coord):
            raw = screen if coord == 0 else window
            return _NS(x=raw[0], y=raw[1], width=raw[2], height=raw[3])

    return _Node()


def test_extents_pass_gtks_negative_sentinel_through_and_drop_zero_size(monkeypatch) -> None:
    class _Rect:
        def __init__(self, x, y, w, h): self.x, self.y, self.width, self.height = x, y, w, h

    class _Comp:
        def __init__(self, rect): self._rect = rect
        def get_extents(self, coord): return self._rect

    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0)))
    monkeypatch.setattr(_atspi, "_component", lambda acc: acc)
    assert _atspi._extents(_Comp(_Rect(-1, -1, -1, -1))) == ((-1.0, -1.0), (-1.0, -1.0))
    assert _atspi._extents(_Comp(_Rect(5, 5, 0, 30))) == (None, None)
    assert _atspi._extents(_Comp(_Rect(5, 5, 0, 30)), keep_zero=True) == ((5.0, 5.0), (0.0, 30.0))
    assert _atspi._extents(_Comp(_Rect(5, 6, 70, 30))) == ((5.0, 6.0), (70.0, 30.0))


def test_offscreen_extents_distinguish_a_scrolled_box_from_zero_and_on_screen(monkeypatch) -> None:
    """A box that misses the screen is off-screen. An overlapping box is not.

    DEFUNCT is gone. A positive box that still overlaps the screen is on
    screen, including a partial clip. A box fully above the screen is the
    scrolled-off case. Zero size is off-screen only when the node is not
    SHOWING, so a failed extents read of a showing control stays absent.
    """

    class _Rect:
        def __init__(self, x, y, w, h):
            self.x, self.y, self.width, self.height = x, y, w, h

    class _Node:
        def __init__(self, role, states, rect):
            self.role = role
            self.states = set(states)
            self.rect = rect

        def get_role_name(self):
            return self.role

        def get_name(self):
            return ""

        def get_toolkit_name(self):
            return ""

        def get_application(self):
            return None

        def get_state_set(self):
            return _NS(names=set(self.states), contains=lambda member: str(member) in self.states)

        def get_component_iface(self):
            return self

        def get_extents(self, _coord):
            return self.rect

    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0)))
    monkeypatch.setattr(_atspi, "_screen_size", lambda: (100, 80))
    assert _atspi.offscreen_extents(_Node("push button", {"DEFUNCT"}, _Rect(-40, 10, 20, 10))) is None
    assert _atspi.offscreen_extents(_Node("push button", {"SHOWING"}, _Rect(10, 12, 20, 16))) is None
    assert _atspi.offscreen_extents(_Node("push button", set(), _Rect(-10, 5, 30, 10))) is None
    assert _atspi.offscreen_extents(_Node("push button", set(), _Rect(8, -80, 200, 60))) == (
        (8.0, -80.0),
        (200.0, 60.0),
    )
    assert _atspi.offscreen_extents(_Node("push button", {"SHOWING"}, _Rect(5, 5, 0, 30))) is None
    assert _atspi.offscreen_extents(_Node("push button", set(), _Rect(5, 5, 0, 30))) == (
        (0.0, 0.0),
        (0.0, 0.0),
    )

    element = Element(
        "e3", "AXButton", "Red swatch", None, Bounds(0, 8, 40, 200, 60), "snap-off",
    )
    issued = Snapshot(
        "snap-off", Scope.WINDOW, "chrome", 1, 0.0, (), (element,),
    )
    handle = object()
    observe._register_epoch(issued.snapshot_id, {}, {"e3": handle})
    monkeypatch.setattr(
        _atspi, "offscreen_extents",
        lambda acc: ((8.0, -80.0), (200.0, 60.0)) if acc is handle else None,
    )
    driver = LinuxDriver()
    assert driver.alive_offscreen(issued, "e3") == Bounds(0, 8, -80, 200, 60)
    monkeypatch.setattr(_atspi, "offscreen_extents", lambda acc: None)
    assert driver.alive_offscreen(issued, "e3") is None
    assert driver.alive_offscreen(issued, "e9") is None


def test_libreoffice_paragraph_bounds_follow_the_frame_insets(monkeypatch) -> None:
    """LibreOffice content uses the coord frame that matches the client area.

    When the root pane's SCREEN top is still the outer frame, the paragraph's
    SCREEN and WINDOW points omit the title bar and the published point is
    the X client origin plus that box. When the root pane sits on the X
    client origin, the paragraph's SCREEN point is already on the pixels and
    the client origin is not added again. A node whose SCREEN point is
    already the client origin plus its WINDOW point stays there. The frame's
    own screen rectangle stays the outer window. A frame with no matching X
    window is not shifted.
    """
    from a11y_computer_use.drivers import _linux_system

    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0, WINDOW=1)))

    def origin(x, y, width, height, title=""):
        del width, height, title
        if (x, y) == (0, 0):
            return (0, 28)
        if (x, y) == (100, 80):
            return (100, 104)
        if (x, y) == (150, 90):
            return (155, 114)
        return None

    monkeypatch.setattr(_linux_system, "client_origin_for_outer_frame", origin)
    accessor = _atspi.ATSPIAccessor()
    accessor.libreoffice = True

    # Client-relative: the root pane's SCREEN top is the outer frame, not the client.
    pane = _lo_node("root pane", "client", (0, 0, 1280, 745), (0, 0, 1280, 745))
    frame = _lo_node(
        "frame", "notes.odt — LibreOffice Writer", (0, 0, 1280, 773), (0, -28, 1280, 801),
        children=[pane],
    )
    paragraph = _lo_node(
        "paragraph", "Beta", (194, 286, 816, 37), (194, 286, 816, 37), parent=frame,
    )
    raw = accessor.read(paragraph)
    assert raw.position == (194.0, 314.0)
    assert raw.size == (816.0, 37.0)
    frame_box = accessor.read(frame)
    assert frame_box.position == (0.0, 0.0)

    # Already on the screen: the root pane sits on the X client origin, so 268 stays 268.
    on_screen = _lo_node("root pane", "client", (0, 28, 1280, 752), (0, 28, 1280, 752))
    absolute = _lo_node(
        "frame", "notes.odt — LibreOffice Writer", (0, 0, 1280, 800), (0, -28, 1280, 800),
        children=[on_screen],
    )
    low = _lo_node(
        "paragraph", "Beta", (207, 268, 793, 18), (207, 268, 793, 18), parent=absolute,
    )
    assert accessor.read(low).position == (207.0, 268.0)

    # This node already stored the client origin in SCREEN. Do not add it twice.
    mixed = _lo_node(
        "paragraph", "Beta", (194, 314, 816, 37), (194, 286, 816, 37), parent=frame,
    )
    assert accessor.read(mixed).position == (194.0, 314.0)

    # Moved window, still client-relative. Outer (100, 80), client (100, 104).
    moved_pane = _lo_node("root pane", "client", (100, 80, 800, 560), (0, 0, 800, 560))
    moved = _lo_node(
        "frame", "moved", (100, 80, 800, 600), (0, -24, 800, 624), children=[moved_pane],
    )
    inside = _lo_node(
        "paragraph", "Beta", (30, 40, 200, 18), (30, 40, 200, 18), parent=moved,
    )
    assert accessor.read(inside).position == (130.0, 144.0)

    # Moved window whose client area is already on the screen.
    # Client is (155, 114): adding it would publish (385, 394).
    screen_pane = _lo_node("root pane", "client", (155, 114, 690, 470), (155, 114, 690, 470))
    screen_frame = _lo_node(
        "frame", "moved", (150, 90, 700, 500), (150, 90, 700, 500), children=[screen_pane],
    )
    screen_para = _lo_node(
        "paragraph", "Beta", (230, 280, 200, 18), (230, 280, 200, 18), parent=screen_frame,
    )
    assert accessor.read(screen_para).position == (230.0, 280.0)

    agreed = _lo_node("frame", "plain", (10, 20, 400, 300), (10, 20, 400, 300))
    child = _lo_node("paragraph", "Beta", (30, 40, 80, 18), (30, 40, 80, 18), parent=agreed)
    same = accessor.read(child)
    assert same.position == (30.0, 40.0)

    accessor.libreoffice = False
    untouched = accessor.read(paragraph)
    assert untouched.position == (194.0, 286.0)


def test_libreoffice_pre_motion_is_screen_a_title_bar_above_window() -> None:
    """The 25.2 gap is SCREEN above WINDOW. Equal, and SCREEN below, are not."""
    assert _atspi.libreoffice_pre_motion(260, 287) is True
    assert _atspi.libreoffice_pre_motion(208, 208) is False
    assert _atspi.libreoffice_pre_motion(235, 208) is False
    assert _atspi.libreoffice_status_point(0, 21, 1280, 779) == (640, 792)
    assert _atspi.libreoffice_status_point(0, 0, 20, 779) is None


def test_settle_libreoffice_geometry_moves_only_when_screen_is_high(monkeypatch) -> None:
    """A pre-motion window gets two status-bar motions. A matched window does not.

    Synthetic nodes, not a live LibreOffice. The placement cache is not
    involved: this only reads raw extents and moves the pointer.
    """
    _atspi._lo_settle_attempts.clear()
    moved: list[tuple[int, int]] = []
    gap = {"on": True}

    class _Node:
        def __init__(self, role, name, children=()):
            self.role = role
            self.name = name
            self.children = list(children)

        def get_role_name(self):
            return self.role

        def get_name(self):
            return self.name

        def get_process_id(self):
            return 4242

        def get_child_count(self):
            return len(self.children)

        def get_child_at_index(self, index):
            return self.children[int(index)]

        def get_component_iface(self):
            return self

        def get_extents(self, coord):
            if self.role == "frame":
                raw = (0, 0, 1280, 800)
            elif gap["on"]:
                raw = (0, 0, 1280, 752) if coord == 0 else (0, 27, 1280, 752)
            else:
                raw = (0, 27, 1280, 752)
            return _NS(x=raw[0], y=raw[1], width=raw[2], height=raw[3])

    pane = _Node("root pane", "client")
    frame = _Node("frame", "Untitled 1 — LibreOffice Calc", [pane])

    def hover(x, y):
        moved.append((int(x), int(y)))
        gap["on"] = False

    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0, WINDOW=1)))
    monkeypatch.setattr(_atspi, "_client_box_for_frame", lambda node: (0, 21, 1280, 779))
    monkeypatch.setattr("a11y_computer_use.drivers._linux_input.hover", hover)
    try:
        _atspi.settle_libreoffice_geometry(frame)
        assert moved == [(640, 792), (643, 792)]
        _atspi.settle_libreoffice_geometry(frame)
        assert moved == [(640, 792), (643, 792)]
    finally:
        _atspi._lo_settle_attempts.clear()


def test_sheet_click_verdict_requires_the_selected_address(monkeypatch) -> None:
    """A Calc click is confirmed only when the selected cell is the target."""
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    chosen = {"address": "B2"}
    focused = {"address": "B4"}
    monkeypatch.setattr(_atspi, "spreadsheet_open", lambda app: app == "soffice")
    monkeypatch.setattr(_atspi, "selected_sheet_address", lambda app: chosen["address"])
    monkeypatch.setattr(_atspi, "_focused_sheet_address", lambda app: focused["address"])
    assert _atspi.sheet_click_verdict("soffice", "B4") == (
        "partial", "the selected cell is B2, not B4",
    )
    chosen["address"] = "B4"
    assert _atspi.sheet_click_verdict("soffice", "B4") == (
        "confirmed", "the selected cell is B4",
    )
    # LibreOffice 24 leaves Selection empty. The focused visible cell is
    # the Name Box, and it can confirm. It cannot override a different
    # published address: that case already returned partial above.
    chosen["address"] = None
    assert _atspi.sheet_click_verdict("soffice", "B4") == (
        "confirmed", "the selected cell is B4",
    )
    focused["address"] = "B2"
    assert _atspi.sheet_click_verdict("soffice", "B4") == (
        "partial", "the selected cell is B2, not B4",
    )
    assert _atspi.sheet_click_verdict("gedit", "B4") is None
    monkeypatch.setattr(_atspi, "spreadsheet_open", lambda app: False)
    assert _atspi.sheet_click_verdict("soffice", "B4") is None


def test_coordinate_click_on_a_calc_cell_uses_the_selected_cell(monkeypatch) -> None:
    """A point click reports partial when the selected cell is not the target.

    Synthetic snapshot, not a live Calc. The driver click is the pointer.
    The outcome comes from the selected address, not from a tree change.
    """
    from a11y_computer_use import server

    cell = Element(
        "e1", "AXCell", "B4", None, Bounds(0, 100, 200, 82, 17), "s",
    )
    snap = Snapshot("s", Scope.WINDOW, "soffice", 0, 0.0, (), (cell,))
    rt = server.Runtime.__new__(server.Runtime)
    rt._current = snap
    rt._view = "compact"
    clicked_at: list[tuple] = []

    class _Driver:
        name = "linux"

        def main_display_id(self):
            return 0

        def click(self, target, **kwargs):
            clicked_at.append((target, kwargs.get("count")))

        def _run(self, fn):
            return fn()

    rt.driver = _Driver()
    rt._reject_coordinate = lambda *args, **kwargs: None
    rt._run_gated = lambda action, app, execute, **kwargs: execute()
    rt._capture = lambda app=None: {
        "snap": snap, "pid": 0, "pid_alive": False, "scope": Scope.WINDOW,
        "app": "soffice", "bounds": "x",
    }
    rt._frontmost = lambda: "soffice"
    rt._refuse_disabled = lambda *args, **kwargs: None
    rt._refuse_secure = lambda *args, **kwargs: None
    rt._dismiss_open_menu = lambda *args, **kwargs: ""
    rt._recheck_target = lambda *args, **kwargs: None
    rt._guard_user = lambda *args, **kwargs: None
    rt._resolves_apps = lambda: False
    monkeypatch.setattr(
        _atspi, "sheet_click_verdict",
        lambda app, address: ("partial", f"the selected cell is B2, not {address}"),
    )
    result = rt.click(x=141, y=208)
    assert clicked_at, "the pointer click was not sent"
    assert result.outcome == "partial"
    assert result.evidence == "the selected cell is B2, not B4"


def test_libreoffice_pointer_shift_is_the_title_bar_only_on_24() -> None:
    """The published box stays on the pixels. The pointer moves on 24.2 only.

    LibreOffice 24.2 hit-tests one title bar above a SCREEN point that
    already includes that bar. The event is shifted by the inset. LibreOffice
    25 hit-tests the point, and a client-relative frame already published the
    inset in the box, so the event is not shifted again.
    """
    delta = _atspi._libreoffice_pointer_delta
    assert delta(True, 24, (0, 21), (0, 0, 1280, 800)) == (0.0, 21.0)
    assert delta(True, 25, (0, 24), (0, 0, 1280, 800)) == (0.0, 0.0)
    assert delta(True, None, (0, 21), (0, 0, 1280, 800)) == (0.0, 0.0)
    assert delta(False, 24, (0, 28), (0, 0, 1280, 773)) == (0.0, 0.0)
    assert delta(True, 24, (100, 104), (100, 80, 800, 600)) == (0.0, 24.0)


def test_libreoffice_pointer_shift_applies_once_per_process(monkeypatch) -> None:
    """The title-bar inset is for the first pointer event in that soffice.

    LibreOffice 24.2 moves its published extents into the hit-test after
    that event. A second click in the same process uses the new box. A
    later process on the same screen rectangle still gets the first inset.
    """
    from a11y_computer_use.drivers import _linux_system

    _atspi._lo_pointer_aligned.clear()
    frame = object()

    def at(x, y, current=frame):
        del x, y
        return current

    keys = {"pid": 4242}

    def align_key(node):
        del node
        return (keys["pid"], 0, 0, 1280, 800)

    monkeypatch.setattr(_atspi, "_libreoffice_frame_at", at)
    monkeypatch.setattr(_atspi, "_libreoffice_frame_key", lambda node: (0, 0, 1280, 800))
    monkeypatch.setattr(_atspi, "_libreoffice_align_key", align_key)
    monkeypatch.setattr(_atspi, "_raw_rect", lambda node, coord: (0, 0, 1280, 800))
    monkeypatch.setattr(_atspi, "libreoffice_major", lambda: 24)
    monkeypatch.setattr(_atspi, "_client_area_is_on_screen", lambda *args: True)
    monkeypatch.setattr(
        _linux_system, "client_origin_for_outer_frame", lambda *args, **kwargs: (0, 21),
    )
    try:
        assert _atspi.libreoffice_pointer_shift(168, 216) == (0.0, 21.0)
        _atspi.note_libreoffice_pointer(168, 216)
        assert _atspi.libreoffice_pointer_shift(180, 200) == (0.0, 0.0)
        keys["pid"] = 9999
        assert _atspi.libreoffice_pointer_shift(168, 216) == (0.0, 21.0)
    finally:
        _atspi._lo_pointer_aligned.clear()


def test_linux_drag_keeps_one_libreoffice_delta(monkeypatch) -> None:
    """A drag shifts every point by the start inset, then notes once."""
    from a11y_computer_use.drivers import _linux_input
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Point

    driver = LinuxDriver()
    calls: list[tuple[int, int]] = []
    noted: list[tuple[int, int]] = []
    captured: dict[str, object] = {}

    def pointer(self, x, y):
        del self
        calls.append((x, y))
        return x, y + 21

    def note(self, x, y):
        del self
        noted.append((x, y))

    def drag(x1, y1, x2, y2, *, button="left", path=()):
        captured["drag"] = (x1, y1, x2, y2, button, list(path))

    monkeypatch.setattr(LinuxDriver, "_pointer", pointer)
    monkeypatch.setattr(LinuxDriver, "_note_pointer", note)
    monkeypatch.setattr("a11y_computer_use.drivers.linux._on_wayland", lambda: False)
    monkeypatch.setattr(_linux_input, "drag", drag)
    driver.drag(Point(1, 10, 20), Point(1, 30, 40), path=(Point(1, 15, 25),))
    assert calls == [(10, 20)]
    assert captured["drag"] == (10, 41, 30, 61, "left", [(15, 46)])
    assert noted == [(10, 20)]


def test_place_paragraph_caret_uses_the_end_when_the_point_misses(monkeypatch) -> None:
    class _Acc:
        def __init__(self, role="paragraph", parent=None, name="soffice"):
            self.role = role
            self.parent = parent
            self.name = name
            self.caret = -1
            self.n = 31
            self.states: set[str] = set()
            self.children: list = []

        def get_role_name(self):
            return self.role

        def get_application(self):
            return self

        def get_name(self):
            return self.name

        def get_parent(self):
            return self.parent

        def get_child_count(self):
            return len(self.children)

        def get_child_at_index(self, index):
            return self.children[index]

    document = _Acc("document text")
    acc = _Acc(parent=document)

    class _Text:
        @staticmethod
        def get_character_count(node):
            return node.n

        @staticmethod
        def get_offset_at_point(node, x, y, coord):
            del node, x, y, coord
            return -1

        @staticmethod
        def set_caret_offset(node, offset):
            node.caret = offset
            return True

        @staticmethod
        def get_caret_offset(node):
            return node.caret

    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0), Text=_Text))
    monkeypatch.setattr(_atspi, "grab_focus", lambda node: True)
    assert _atspi.place_paragraph_caret(acc, 10, 10) is True
    assert acc.caret == 31
    assert _atspi.paragraph_click_verdict(acc) == (
        "confirmed", "the caret is in the target paragraph",
    )
    acc.caret = -1
    assert _atspi.paragraph_click_verdict(acc) == (
        "partial", "the caret is not in the target paragraph",
    )
    acc.role = "push button"
    assert _atspi.place_paragraph_caret(acc, 10, 10) is False
    assert _atspi.paragraph_click_verdict(acc) is None
    slide = _Acc("document presentation")
    shape = _Acc("shape", parent=slide)
    title = _Acc(parent=shape)
    title.children = []
    shape.children = [title]
    assert _atspi.is_libreoffice_text_paragraph(title) is False
    assert _atspi.is_libreoffice_shape_text(title) is True
    assert _atspi.place_paragraph_caret(title, 10, 10) is False
    assert _atspi.paragraph_click_verdict(title) == (
        "partial", "the text frame is not in text edit",
    )
    title.states = {"FOCUSED", "EDITABLE"}
    monkeypatch.setattr(_atspi, "_state_has", lambda node, name: name in getattr(node, "states", ()))
    assert _atspi.paragraph_click_verdict(title) == (
        "confirmed", "a focused editable paragraph is inside the text frame",
    )
    title.states = {"FOCUSED"}
    title.caret = 4
    assert _atspi.paragraph_click_verdict(title) == (
        "confirmed", "the caret is inside the text frame",
    )
    title.states = set()
    title.caret = 4
    assert _atspi.paragraph_click_verdict(title) == (
        "partial", "the text frame is not in text edit",
    )
    drawing = _Acc("document drawing")
    frame = _Acc("text frame", parent=drawing)
    assert _atspi.is_libreoffice_shape_text(frame) is True
    named = _Acc("panel", parent=slide, name="Text Frame ")
    named.get_application = lambda: slide
    assert _atspi.is_libreoffice_shape_text(named) is True
    page = _Acc("panel", parent=slide, name="PageShape: Slide 1")
    page.get_application = lambda: slide
    assert _atspi.is_libreoffice_shape_text(page) is False
    writer = _Acc(parent=document)
    assert _atspi.is_libreoffice_text_paragraph(writer) is True


def test_impress_title_click_uses_the_pointer_and_not_the_writer_caret(monkeypatch) -> None:
    """An Impress paragraph click is a center pointer click, then a double-click.

    The Writer caret shortcut must not run. A single click that does not
    enter text edit is followed by a double-click at the same center. When
    the first click already enters edit, the double-click is not sent.
    """
    from a11y_computer_use import observe
    from a11y_computer_use.drivers import _linux_input
    from a11y_computer_use.drivers.linux import LinuxDriver

    handle = object()
    clicks: list[tuple] = []
    noted: list[tuple[int, int]] = []
    editing = {"ready": False}

    def pointer_click(x, y, *, button="left", count=1):
        clicks.append((x, y, button, count))
        if count >= 2:
            editing["ready"] = True

    monkeypatch.setattr("a11y_computer_use.drivers.linux._on_wayland", lambda: False)
    monkeypatch.setattr(_linux_input, "click", pointer_click)
    monkeypatch.setattr(_atspi, "is_libreoffice_shape_text", lambda acc: acc is handle)
    monkeypatch.setattr(
        _atspi, "place_paragraph_caret",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("writer caret")),
    )
    monkeypatch.setattr(_atspi, "shape_text_editing", lambda acc: editing["ready"] and acc is handle)
    monkeypatch.setattr(_atspi, "is_libreoffice_text_paragraph", lambda acc: False)
    driver = LinuxDriver()
    monkeypatch.setattr(LinuxDriver, "_pointer", lambda self, x, y: (int(x), int(y)))
    monkeypatch.setattr(LinuxDriver, "_note_pointer", lambda self, x, y: noted.append((int(x), int(y))))
    element = Element(
        "e4", "AXStaticText", "Paragraph: 0 Alpha Slide Title", "Alpha Slide Title",
        Bounds(0, 100, 200, 80, 40), "snap-imp", clickable=True,
    )
    observe._register_epoch(element.snapshot_id, {}, {"e4": handle})
    assert driver.press_element(element) is False
    driver.click(element)
    assert clicks == [(140, 220, "left", 1), (140, 220, "left", 2)]
    assert noted == [(140, 220)]
    assert driver._focused_editable is handle

    clicks.clear()
    noted.clear()
    editing["ready"] = True
    driver._focused_editable = None
    driver.click(element)
    assert clicks == [(140, 220, "left", 1)]
    assert driver._focused_editable is handle


def test_type_text_impress_reads_the_title_when_the_document_text_is_empty(
    fake_atspi, monkeypatch,
) -> None:
    """The presentation stays focused and its text stays empty.

    The title paragraph is where the keys land. A read of the document
    would be ``""`` and report text_mismatch after the title already holds
    the characters. The type confirms from that paragraph. A paragraph
    that never changes is a mismatch showing the title, not the empty
    document.
    """
    app = _Acc("application", name="soffice.bin")
    slide = _Acc(
        "document presentation",
        name="deck - LibreOffice Presentation",
        states=("FOCUSED", "EDITABLE"),
    )
    slide.text = ""
    slide.get_application = lambda: app
    page = _Acc("panel", name="PageShape: Slide 1")
    frame = _Acc("panel", name="Text Frame ")
    title = _Acc(
        "paragraph", name="Paragraph: 0",
        states=("FOCUSED", "EDITABLE", "SHOWING"),
    )
    title.text = "Alpha Slide Title"
    title.caret = 17
    _adopt(frame, title)
    _adopt(slide, page, frame)
    reads = {"n": 0}
    real_get_text = fake_atspi.Text.get_text

    def get_text(acc, start, end):
        if acc is title:
            reads["n"] += 1
            acc.text = "Alpha Slide Title" if reads["n"] < 3 else "Alpha Slide Title K1"
        return real_get_text(acc, start, end)

    monkeypatch.setattr(fake_atspi.Text, "get_text", staticmethod(get_text))
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(_atspi, "_TYPE_SETTLE_PAUSE_S", 0)
    monkeypatch.setattr(_atspi, "_TYPE_SETTLE_POLLS", 4)
    monkeypatch.setattr(_atspi, "focused_editable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_atspi, "focused_secure", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(_atspi, "focused_text", lambda _app: "")
    monkeypatch.setattr(_atspi, "spreadsheet_open", lambda _app: False)
    monkeypatch.setattr(_atspi, "sheet_editor_text", lambda _app: None)
    monkeypatch.setattr(_atspi, "_focused_node", lambda _app, **_kwargs: (slide, False))
    monkeypatch.setattr(_linux_input, "type_string", lambda _text: None)
    driver = LinuxDriver()
    driver._focused_editable = None
    driver.frontmost_app = lambda: ("soffice.bin", 1)
    assert driver.type_text(" K1") == 3
    assert reads["n"] >= 3
    assert title.text == "Alpha Slide Title K1"

    reads["n"] = 0

    def stuck(acc, start, end):
        if acc is title:
            acc.text = "Alpha Slide Title"
        return real_get_text(acc, start, end)

    monkeypatch.setattr(fake_atspi.Text, "get_text", staticmethod(stuck))
    with pytest.raises(ComputerUseError) as exc:
        driver.type_text(" K1")
    assert exc.value.detail["reason"] == "text_mismatch"
    assert exc.value.detail["actual"] == "Alpha Slide Title"


def test_hollow_page_tab_keeps_the_document_below_it() -> None:
    """GTK reports (-1, -1, -1, -1) for a notebook page tab whose label is hidden
    (gedit with one document, gnome-terminal with one tab); the page content has
    real bounds. The engine used to drop the whole subtree as zero-size, leaving
    an empty tabgroup and no document text. The tab now survives as a container
    sized to its visible descendants."""
    tree = _node(
        "AXWindow", "doc - gedit", pos=(0.0, 0.0), size=(1280.0, 800.0),
        children=[_node(
            "AXTabGroup", "", pos=(0.0, 60.0), size=(1280.0, 700.0),
            children=[_node(
                "AXTab", "", pos=(-1.0, -1.0), size=(-1.0, -1.0),
                children=[_node("AXTextArea", "", value="a11y box trial",
                                pos=(10.0, 70.0), size=(1200.0, 600.0))],
            )],
        )],
    )
    snap = build_snapshot(tree, _FakeAccessor(), scope=Scope.WINDOW, app="gedit", pid=1,
                          geometry=_geometry())
    text = [el for el in snap.elements if el.role == "AXTextArea"]
    assert text and text[0].value == "a11y box trial"
    tab = [el for el in snap.elements if el.role == "AXTab"]
    assert tab and tab[0].bounds is not None and tab[0].bounds.width == 1200  # union of its content
    # A hollow node with nothing visible below it still disappears.
    tree = _node("AXWindow", "w", pos=(0.0, 0.0), size=(400.0, 300.0),
                 children=[_node("AXTab", "", pos=(-1.0, -1.0), size=(-1.0, -1.0),
                                 children=[_node("AXStaticText", "", pos=(-1.0, -1.0), size=(-1.0, -1.0))])])
    snap = build_snapshot(tree, _FakeAccessor(), scope=Scope.WINDOW, app="x", pid=1, geometry=_geometry())
    assert not [el for el in snap.elements if el.role == "AXTab"]
    # A real-sized node parked off every display keeps dropping its subtree (scrolled-away rows).
    tree = _node("AXWindow", "w", pos=(0.0, 0.0), size=(400.0, 300.0),
                 children=[_node("AXRow", "hidden", pos=(-5000.0, -5000.0), size=(100.0, 20.0),
                                 children=[_node("AXButton", "Inside", actions=("AXPress",),
                                                 pos=(10.0, 10.0), size=(50.0, 20.0))])])
    snap = build_snapshot(tree, _FakeAccessor(), scope=Scope.WINDOW, app="x", pid=1, geometry=_geometry())
    assert not [el for el in snap.elements if el.title in ("hidden", "Inside")]


class _PageNode:
    """A notebook accessible with an explicit box. No gi."""

    def __init__(self, role, name="", box=None, children=()):
        self.role = role
        self.name = name
        self.box = box
        self.children = list(children)
        self.parent = None
        for child in self.children:
            child.parent = self

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_description(self):
        return ""

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def get_parent(self):
        return self.parent


def test_hidden_page_tab_is_named_from_the_frame_and_is_not_the_page(monkeypatch) -> None:
    """A notebook page with no allocation is a named hidden tab, not the page.

    GTK reports (-1, -1, -1, -1) and an empty name for the lone page tab.
    The frame name is the page label. The page is hoisted to the tab list.
    The tab has no box and is not clickable. A page tab that already has a
    box and a name keeps that box.
    """
    def extents(acc, *, keep_zero=False):
        box = getattr(acc, "box", None)
        if box is not None:
            return box
        return (0.0, 0.0), (10.0, 10.0)

    monkeypatch.setattr(_atspi, "_extents", extents)
    monkeypatch.setattr(
        _atspi, "_state_flags",
        lambda _acc: (True, False, None, True, None, False, False),
    )
    monkeypatch.setattr(_atspi, "_value_text", lambda *_args, **_kwargs: None)

    frame = _PageNode("frame", "Probe Title", box=((100.0, 100.0), (800.0, 500.0)))
    hidden = _PageNode("page tab", "", box=((-1.0, -1.0), (-1.0, -1.0)))
    page = _PageNode("panel", "", box=((110.0, 160.0), (780.0, 420.0)))
    terminal = _PageNode("terminal", "Terminal", box=((110.0, 160.0), (760.0, 420.0)))
    page.children = [terminal]
    terminal.parent = page
    tabs = _PageNode("page tab list", "", box=((110.0, 160.0), (780.0, 420.0)), children=[hidden])
    hidden.children = [page]
    page.parent = hidden
    frame.children = [tabs]
    tabs.parent = frame

    accessor = _atspi.ATSPIAccessor()
    raw = accessor.read(hidden)
    assert raw.role == "AXButton"
    assert raw.title == "Probe Title"
    assert raw.selected is True
    assert raw.unallocated is True
    assert raw.position is None and raw.size is None
    assert raw.actions == ()
    assert accessor.children(hidden) == []
    assert accessor.children(tabs) == [hidden, page]

    labelled = _PageNode("page tab", "Kept Label", box=((-1.0, -1.0), (-1.0, -1.0)))
    labelled.parent = tabs
    labelled_raw = accessor.read(labelled)
    assert labelled_raw.title == "Kept Label"
    assert labelled_raw.unallocated is True
    assert labelled_raw.position is None and labelled_raw.size is None

    visible = _PageNode("page tab", "Alpha Title", box=((565.0, 402.0), (74.0, 19.0)))
    visible_page = _PageNode("panel", "", box=((551.0, 429.0), (818.0, 420.0)))
    visible.children = [visible_page]
    visible_page.parent = visible
    visible_tabs = _PageNode("page tab list", "", children=[visible])
    visible.parent = visible_tabs
    assert accessor.read(visible).title == "Alpha Title"
    assert accessor.read(visible).size == (74.0, 19.0)
    assert accessor.children(visible) == [visible_page]
    assert accessor.children(visible_tabs) == [visible]

    snap = build_snapshot(
        frame, accessor, scope=Scope.WINDOW, app="xfce4-terminal", pid=1, geometry=_geometry(),
    )
    buttons = [el for el in snap.elements if el.role == "AXButton" and el.title == "Probe Title"]
    assert len(buttons) == 1
    tab = buttons[0]
    assert tab.bounds is None
    assert tab.clickable is False
    assert tab.actionable is False
    rendered = observe.render_text(snap, include_bounds=True)
    line = next(
        item for item in rendered.splitlines()
        if "button" in item and "Probe Title" in item
    )
    assert "hidden" in line
    assert "@" not in line
    text = [el for el in snap.elements if el.role == "AXTextArea" and el.title == "Terminal"]
    assert len(text) == 1
    area = text[0]
    assert area.bounds is not None
    by_ref = {el.ref: el for el in snap.elements}
    parent = area.parent
    while parent is not None:
        assert by_ref[parent].ref != tab.ref
        parent = by_ref[parent].parent


def test_hidden_page_tab_ref_click_is_not_a_pointer_click() -> None:
    """A ref click on a hidden tab is a typed no-op. The pointer is not called."""
    from a11y_computer_use import server

    tab = Element(
        "e2", "AXButton", "Probe Title", None, None, "s",
        clickable=False, selected=True,
    )
    clicks: list[object] = []

    class _Driver:
        name = "linux"

        def click(self, target, **kwargs):
            clicks.append(target)

        def press_element(self, element):
            clicks.append(("press", element.ref))
            return False

    rt = server.Runtime.__new__(server.Runtime)
    rt.driver = _Driver()
    rt._current = Snapshot("s", Scope.WINDOW, "xfce4-terminal", 1, 0.0, (), (tab,))
    rt._reject_coordinate = lambda *args, **kwargs: None
    rt._target = lambda *args, **kwargs: (tab, "xfce4-terminal")
    rt._capture = lambda app=None: {}
    rt._run_gated = lambda action, app, execute, **kwargs: execute()
    rt._refuse_disabled = lambda *args, **kwargs: None
    rt._refuse_secure = lambda *args, **kwargs: None
    result = rt.click(ref="e2")
    assert clicks == []
    assert result.outcome == "suspected_noop"
    assert result.evidence == "not_actionable"
    assert "no click was sent" in result


def test_accessor_read_marks_internal_frame_and_keeps_zero_size(monkeypatch) -> None:
    """The snapshot read keeps a zero box and marks an internal frame.
    Hit-testing still asks for the default extents, which drop that box."""
    seen: dict[str, bool] = {}

    def extents(acc, *, keep_zero=False):
        seen["keep_zero"] = keep_zero
        return (8.0, 9.0), (0.0, 40.0)

    monkeypatch.setattr(_atspi, "_state_flags", lambda acc: (True, False, None, False, None, False, False))
    monkeypatch.setattr(_atspi, "_extents", extents)

    class Acc:
        def get_role_name(self):
            return "internal frame"

        def get_name(self):
            return "reCAPTCHA"

        def get_description(self):
            return ""

    raw = _atspi.ATSPIAccessor().read(Acc())
    assert seen["keep_zero"] is True
    assert raw.role == "AXGroup"
    assert raw.title == "reCAPTCHA"
    assert raw.atspi_web == "iframe"
    assert raw.size == (0.0, 40.0)


def test_single_line_gtk_text_is_a_text_field_not_a_textarea(monkeypatch) -> None:
    """Gtk.Entry and Gtk.TextView share the AT-SPI role ``text``. The entry is single-line."""
    monkeypatch.setattr(_atspi, "_state_flags", lambda acc: (True, False, None, False, None, False, False))
    monkeypatch.setattr(_atspi, "_extents", lambda acc, keep_zero=False: ((0.0, 0.0), (80.0, 24.0)))
    monkeypatch.setattr(_atspi, "_value_text", lambda acc, role, role_name=None: "")
    monkeypatch.setattr(_atspi, "_action_names", lambda acc: ())
    monkeypatch.setattr(_atspi, "_get_attributes", lambda acc: {})
    monkeypatch.setattr(_atspi, "_stable_id", lambda acc, attrs=None: None)

    class Acc:
        def __init__(self, states):
            self.states = set(states)

        def get_role_name(self):
            return "text"

        def get_name(self):
            return "field"

        def get_description(self):
            return ""

        def get_state_set(self):
            return _States(self.states)

    entry = _atspi.ATSPIAccessor().read(Acc({"SINGLE_LINE", "EDITABLE"}))
    area = _atspi.ATSPIAccessor().read(Acc({"MULTI_LINE", "EDITABLE"}))
    plain = _atspi.ATSPIAccessor().read(Acc(set()))
    assert entry.role == "AXTextField"
    assert area.role == "AXTextArea"
    assert plain.role == "AXTextArea"


def test_chromium_textarea_entry_is_multi_line_text(monkeypatch) -> None:
    """Chromium exposes ``<textarea>`` as AT-SPI ``entry`` plus MULTI_LINE.

    A single-line ``<input>`` stays a text field. The ``multiline`` attribute
    is the same fact when the state is absent. A contenteditable
    ``role=textbox`` with ``aria-multiline`` stays a text field. The snapshot
    word for the textarea is ``textarea``. Not a live Chrome or Electron.
    """
    monkeypatch.setattr(_atspi, "_state_flags", lambda acc: (True, False, None, False, None, False, True))
    monkeypatch.setattr(_atspi, "_extents", lambda acc, keep_zero=False: ((8.0, 40.0), (240.0, 80.0)))
    monkeypatch.setattr(_atspi, "_value_text", lambda acc, role, role_name=None: "line1 line2")
    monkeypatch.setattr(_atspi, "_action_names", lambda acc: ("AXPress",))
    monkeypatch.setattr(_atspi, "_stable_id", lambda acc, attrs=None: None)

    class Acc:
        def __init__(self, states, attrs):
            self.states = set(states)
            self.attrs = dict(attrs)

        def get_role_name(self):
            return "entry"

        def get_name(self):
            return "Note body" if "MULTI_LINE" in self.states or self.attrs.get("tag") == "textarea" else "Note title"

        def get_description(self):
            return ""

        def get_state_set(self):
            return _States(self.states)

        def get_attributes(self):
            return dict(self.attrs)

    def read(states, attrs):
        return _atspi.ATSPIAccessor().read(Acc(states, attrs))

    area = read({"MULTI_LINE", "EDITABLE", "SINGLE_LINE"}, {"tag": "textarea"})
    single = read({"SINGLE_LINE", "EDITABLE"}, {"tag": "input"})
    by_attr = read({"EDITABLE"}, {"multiline": "true"})
    by_bool = read({"EDITABLE"}, {"multiline": True})
    rich = read(
        {"MULTI_LINE", "EDITABLE"},
        {"tag": "div", "xml-roles": "textbox", "aria-multiline": "true"},
    )
    aria_only = read({"EDITABLE"}, {"aria-multiline": "true"})
    off = read({"EDITABLE"}, {"multiline": "false", "aria-multiline": "false"})
    assert area.role == "AXTextArea"
    assert single.role == "AXTextField"
    assert by_attr.role == "AXTextArea"
    assert by_bool.role == "AXTextArea"
    assert rich.role == "AXTextField"
    assert aria_only.role == "AXTextField"
    assert off.role == "AXTextField"

    snap = build_snapshot(
        (area, []), _FakeAccessor(), scope=Scope.WINDOW, app="electron", pid=1, geometry=_geometry(),
    )
    text = observe.render_text(snap)
    assert 'textarea "Note body"' in text
    single_snap = build_snapshot(
        (single, []), _FakeAccessor(), scope=Scope.WINDOW, app="electron", pid=1, geometry=_geometry(),
    )
    assert 'textfield "Note title"' in observe.render_text(single_snap)


class _Section:
    """A Chrome ``<div contenteditable>`` with no textbox role: AT-SPI section."""

    def __init__(self, name: str, text: str, states: set[str]):
        self.name = name
        self.text = text
        self.states = set(states)
        self.attrs = {"tag": "div"}

    def get_role_name(self):
        return "section"

    def get_name(self):
        return self.name

    def get_description(self):
        return ""

    def get_state_set(self):
        return _States(self.states)

    def get_attributes(self):
        return dict(self.attrs)


def _toolkit_app(toolkit: str, name: str):
    class _App:
        def get_toolkit_name(self):
            return toolkit

        def get_name(self):
            return name

    return _App()


def test_editable_section_is_shown_as_an_editable_group(fake_atspi, monkeypatch) -> None:
    """A Chromium contenteditable section is a group the snapshot marks editable.

    The role stays a group. The edit flag comes from STATE_EDITABLE on a
    div section, not from a textbox role and not from EditableText alone.
    A section with neither the state nor a browser toolkit stays non-editable.
    """
    monkeypatch.setattr(_atspi, "_extents", lambda acc, keep_zero=False: ((10.0, 20.0), (240.0, 48.0)))
    monkeypatch.setattr(_atspi, "_action_names", lambda acc: ("AXPress",))
    notes = _Section("Notes box", "old note", {"EDITABLE", "ENABLED", "FOCUSABLE"})
    notes.get_application = lambda: _toolkit_app("Chromium", "Google Chrome")
    raw = _atspi.ATSPIAccessor().read(notes)
    assert raw.role == "AXGroup"
    assert raw.editable is True
    assert raw.value == "old note"
    snap = build_snapshot(
        (raw, []), _FakeAccessor(), scope=Scope.WINDOW, app="chrome", pid=1, geometry=_geometry(),
    )
    rendered = observe.render_text(snap)
    assert 'group "Notes box" ="old note" (click,edit)' in rendered
    assert snap.elements[0].editable is True

    quiet = _atspi.ATSPIAccessor().read(_Section("Quiet", "static words", {"ENABLED"}))
    assert quiet.role == "AXGroup"
    assert quiet.editable is False

    via = _Section("Iface", "typed", {"ENABLED"})
    via.get_editable_text_iface = lambda: object()
    assert _atspi.ATSPIAccessor().read(via).editable is False

    gecko = _Section("Notes box", "old note", {"EDITABLE", "ENABLED", "FOCUSABLE"})
    gecko.get_application = lambda: _toolkit_app("Gecko", "Firefox")
    gecko.get_editable_text_iface = lambda: object()
    assert _atspi.ATSPIAccessor().read(gecko).editable is True


def test_gecko_contenteditable_section_is_a_text_role(fake_atspi, monkeypatch) -> None:
    """A Firefox contenteditable section with a text interface is a text role.

    MULTI_LINE, or a multiline attribute, is a textarea. A single-line editor
    is a text field. A section without EDITABLE, or without a text interface,
    stays a group. Chromium's section stays a group. Not a live Firefox.
    """
    monkeypatch.setattr(_atspi, "_extents", lambda acc, keep_zero=False: ((10.0, 20.0), (240.0, 48.0)))
    monkeypatch.setattr(_atspi, "_action_names", lambda acc: ("AXPress",))
    monkeypatch.setattr(_atspi, "_value_text", lambda acc, role, role_name=None: acc.text)

    def read(node: _Section):
        return _atspi.ATSPIAccessor().read(node)

    def gecko(name: str, text: str, states: set[str], *, iface: bool = True, tag: str = "div"):
        node = _Section(name, text, states)
        node.attrs["tag"] = tag
        node.get_application = lambda: _toolkit_app("Gecko", "Firefox")
        if iface:
            node.get_editable_text_iface = lambda: object()
        return node

    multi = gecko("Notes box", "editable block", {"EDITABLE", "MULTI_LINE", "ENABLED", "FOCUSABLE"})
    raw = read(multi)
    assert raw.role == "AXTextArea"
    assert raw.editable is True
    assert raw.value == "editable block"
    snap = build_snapshot(
        (raw, []), _FakeAccessor(), scope=Scope.WINDOW, app="firefox", pid=1, geometry=_geometry(),
    )
    assert 'textarea "Notes box" ="editable block"' in observe.render_text(snap)

    single = gecko("Plain", "one line", {"EDITABLE", "ENABLED", "FOCUSABLE"})
    assert read(single).role == "AXTextField"
    single_snap = build_snapshot(
        (read(single), []), _FakeAccessor(), scope=Scope.WINDOW, app="firefox", pid=1, geometry=_geometry(),
    )
    assert 'textfield "Plain"' in observe.render_text(single_snap)

    by_attr = gecko("Rich", "rich old", {"EDITABLE", "ENABLED"})
    by_attr.attrs["aria-multiline"] = "true"
    assert read(by_attr).role == "AXTextArea"
    by_bool = gecko("Rich", "rich old", {"EDITABLE", "ENABLED"})
    by_bool.attrs["multiline"] = True
    assert read(by_bool).role == "AXTextArea"
    off = gecko("Plain", "one line", {"EDITABLE", "ENABLED"})
    off.attrs["multiline"] = "false"
    off.attrs["aria-multiline"] = False
    assert read(off).role == "AXTextField"

    quiet = gecko("Quiet", "static words", {"ENABLED"})
    assert read(quiet).role == "AXGroup"
    assert read(quiet).editable is False

    no_iface = _Section("Bare", "words", {"EDITABLE", "MULTI_LINE", "ENABLED"})
    no_iface.get_application = lambda: _toolkit_app("Gecko", "Firefox")
    del no_iface.text
    assert read(no_iface).role == "AXGroup"

    chrome = _Section("Notes box", "old note", {"EDITABLE", "MULTI_LINE", "ENABLED", "FOCUSABLE"})
    chrome.get_application = lambda: _toolkit_app("Chromium", "Google Chrome")
    chrome.get_editable_text_iface = lambda: object()
    assert read(chrome).role == "AXGroup"
    assert read(chrome).editable is True

    monkeypatch.setattr(_atspi, "_value_text", lambda acc, role, role_name=None: 0.0)
    numbered = read(multi)
    assert numbered.role == "AXTextArea"
    assert numbered.value == "editable block"


def test_roleless_contenteditable_set_text_clears_then_types(fake_atspi, monkeypatch) -> None:
    """Fake Chrome section. No EditableText. The #165 clear runs, then the keys.

    Not a browser. BackSpace leaves a newline, which is empty, and Delete is
    still sent. The read-back is the new string. An empty value uses that
    same clear and does not restore the old words.
    """
    field = _KeyClearedWebField("old note")
    _mark_toolkit(field, "Chromium", "Google Chrome")
    field.attrs = {"tag": "div"}
    field.get_attributes = lambda: dict(field.attrs)
    field.get_role_name = lambda: "section"
    field.get_editable_text_iface = None
    sent: list[str] = []

    def press_chord(chord: str) -> None:
        sent.append(chord)
        if chord == "ctrl+a":
            field.selected_all = True
        elif chord == "backspace" and getattr(field, "selected_all", False):
            field.text = "\n"
            field.echo = None
            field.selected_all = False

    def type_string(text: str) -> None:
        field.text = text

    monkeypatch.setattr(_linux_input, "press_chord", press_chord)
    monkeypatch.setattr(_linux_input, "type_string", type_string)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    assert _atspi.set_text(field, "new note") is True
    assert field.text == "new note"
    assert sent[:3] == ["ctrl+a", "backspace", "delete"]

    field.text = "new note"
    sent.clear()
    assert _atspi.set_text(field, "") is True
    assert field.text == "\n"
    assert "new note" not in field.text
    assert sent[:3] == ["ctrl+a", "backspace", "delete"]


def test_set_value_writes_an_editable_group_and_refuses_a_plain_one(
    fake_atspi, monkeypatch
) -> None:
    """Synthetic group. The editable flag is what lets set_value through.

    Not a browser. A plain group raises not_editable and sends no chords.
    """
    field = _KeyClearedWebField("old note")
    _mark_toolkit(field, "Chromium", "Google Chrome")
    field.attrs = {"tag": "div"}
    field.get_attributes = lambda: dict(field.attrs)
    field.get_role_name = lambda: "section"
    field.get_editable_text_iface = None

    def state_set():
        # grab_focus records field.focused and does not touch a static set.
        # Without FOCUSED the gate treats focus as reported and absent.
        names = {"EDITABLE", "ENABLED", "FOCUSABLE"}
        if getattr(field, "focused", False):
            names.add("FOCUSED")
        return _States(names)

    field.get_state_set = state_set
    sent: list[str] = []

    def press_chord(chord: str) -> None:
        sent.append(chord)
        if chord == "ctrl+a":
            field.selected_all = True
        elif chord == "backspace" and getattr(field, "selected_all", False):
            field.text = "\n"
            field.echo = None
            field.selected_all = False

    def type_string(text: str) -> None:
        # One character is one batch once focus is reported. A blank read,
        # including the newline BackSpace leaves, is replaced; later
        # characters append.
        if field.text in ("", "\n"):
            field.text = text
        else:
            field.text += text

    monkeypatch.setattr(_linux_input, "press_chord", press_chord)
    monkeypatch.setattr(_linux_input, "type_string", type_string)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: field)
    box = Bounds(0, 8, 8, 200, 40)
    editable = Element(
        "e4", "AXGroup", "Notes box", "old note", box, "snap-1", editable=True, clickable=True,
    )
    assert LinuxDriver().set_value(editable, "new note") is True
    assert field.text == "new note"
    assert sent[:3] == ["ctrl+a", "backspace", "delete"]
    sent.clear()
    plain = Element("e5", "AXGroup", "Quiet", "static words", box, "snap-1", clickable=True)
    with pytest.raises(ComputerUseError) as exc:
        LinuxDriver().set_value(plain, "nope")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "not_editable"
    assert sent == []
    assert field.text == "new note"


def test_set_value_accepts_focus_on_a_child_text_node(fake_atspi, monkeypatch) -> None:
    """Roleless contenteditable. FOCUSED is on a child text node, not the section.

    Fake transport. The section's state set never includes FOCUSED, and
    grab_focus does not add it. An indexed text child carries FOCUSED, then
    a hypertext embed with no indexed children does. Both writes land. A
    section with no focused descendant sends no chords.
    """
    field = _KeyClearedWebField("old note")
    _mark_toolkit(field, "Chromium", "Google Chrome")
    field.attrs = {"tag": "div"}
    field.get_attributes = lambda: dict(field.attrs)
    field.get_role_name = lambda: "section"
    field.get_editable_text_iface = None
    field.grab_focus = lambda: True
    field.get_state_set = lambda: _States({"EDITABLE", "ENABLED", "FOCUSABLE"})
    sent: list[str] = []

    def press_chord(chord: str) -> None:
        sent.append(chord)
        if chord == "ctrl+a":
            field.selected_all = True
        elif chord == "backspace" and getattr(field, "selected_all", False):
            field.text = "\n"
            field.echo = None
            field.selected_all = False

    def type_string(text: str) -> None:
        if field.text in ("", "\n"):
            field.text = text
        else:
            field.text += text

    monkeypatch.setattr(_linux_input, "press_chord", press_chord)
    monkeypatch.setattr(_linux_input, "type_string", type_string)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: field)
    box = Bounds(0, 8, 8, 200, 40)
    editable = Element(
        "e4", "AXGroup", "Notes box", "old note", box, "snap-1", editable=True, clickable=True,
    )

    class _Text:
        def __init__(self):
            self.states = {"ENABLED", "FOCUSED"}

        def get_role_name(self):
            return "text"

        def get_state_set(self):
            return _States(self.states)

        def get_child_count(self):
            return 0

    text = _Text()
    field.children = [text]
    field.get_child_count = lambda: len(field.children)
    field.get_child_at_index = lambda index: field.children[index]
    field.links = ()
    assert LinuxDriver().set_value(editable, "new note") is True
    assert field.text == "new note"
    assert sent[:3] == ["ctrl+a", "backspace", "delete"]

    field.text = "old note"
    field.selected_all = False
    sent.clear()
    field.children = []
    field.links = (text,)
    assert LinuxDriver().set_value(editable, "from embed") is True
    assert field.text == "from embed"
    assert sent[:3] == ["ctrl+a", "backspace", "delete"]

    field.text = "old note"
    field.selected_all = False
    sent.clear()
    field.links = ()
    with pytest.raises(ComputerUseError) as exc:
        LinuxDriver().set_value(editable, "nope")
    assert exc.value.code is ErrorCode.FOCUS_LOST
    assert exc.value.detail["reason"] == "focus_lost"
    assert sent == []
    assert field.text == "old note"


class _Tagged:
    """One AT-SPI node with a role, a tag, and optional EditableText."""

    def __init__(self, role: str, name: str, text: str, states: set[str], tag: str,
                 toolkit: str = "", app_name: str = ""):
        self.role = role
        self.name = name
        self.text = text
        self.states = set(states)
        self.attrs = {"tag": tag}
        self.toolkit = toolkit
        self.app_name = app_name
        self.iface = None

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_description(self):
        return ""

    def get_state_set(self):
        return _States(self.states)

    def get_attributes(self):
        return dict(self.attrs)

    def get_application(self):
        if not self.toolkit and not self.app_name:
            return None
        return _toolkit_app(self.toolkit, self.app_name)

    def get_editable_text_iface(self):
        return self.iface


def test_firefox_paragraph_and_select_are_not_editable_entries(fake_atspi, monkeypatch) -> None:
    """A paragraph or select is not an edit target. An entry and a contenteditable are.

    EditableText or STATE_EDITABLE on a paragraph, panel, or document does not
    set the flag. set_value on a paragraph raises not_editable and sends no
    select-all, even when the snapshot flag was wrong. A read-only entry is
    not an entry. A combo is not edit; its role no longer implies the flag.
    """
    monkeypatch.setattr(_atspi, "_extents", lambda acc, keep_zero=False: ((4.0, 8.0), (180.0, 24.0)))
    monkeypatch.setattr(_atspi, "_action_names", lambda acc: ())
    monkeypatch.setattr(_atspi, "_value_text", lambda acc, role, role_name=None: acc.text)

    def read(node: _Tagged):
        return _atspi.ATSPIAccessor().read(node)

    paragraph = _Tagged(
        "paragraph", "", "idle", {"ENABLED", "SENSITIVE", "SHOWING"}, "p", "Gecko", "Firefox",
    )
    paragraph.iface = object()
    assert read(paragraph).editable is False
    paragraph.states.add("EDITABLE")
    raw_p = read(paragraph)
    assert raw_p.editable is False
    assert raw_p.role == "AXStaticText"
    snap = build_snapshot(
        (raw_p, []), _FakeAccessor(), scope=Scope.WINDOW, app="firefox", pid=1, geometry=_geometry(),
    )
    assert snap.elements[0].editable is False
    assert "edit" not in observe.render_text(snap)

    panel = _Tagged("panel", "", "bar", {"EDITABLE", "ENABLED"}, "div", "Gecko", "Firefox")
    panel.iface = object()
    assert read(panel).editable is False
    document = _Tagged("document web", "ffedit", "", {"EDITABLE", "ENABLED"}, "", "Gecko", "Firefox")
    document.iface = object()
    assert read(document).editable is False

    select = _Tagged("combo box", "Color", "Red", {"ENABLED", "SHOWING"}, "select", "Gecko", "Firefox")
    raw_s = read(select)
    assert raw_s.editable is False
    assert raw_s.role == "AXComboBox"
    combo_snap = build_snapshot(
        (raw_s, []), _FakeAccessor(), scope=Scope.WINDOW, app="firefox", pid=1, geometry=_geometry(),
    )
    assert combo_snap.elements[0].editable is False
    assert "edit" not in observe.render_text(combo_snap)

    entry = _Tagged(
        "entry", "Name", "", {"EDITABLE", "ENABLED", "SINGLE_LINE"}, "input", "Gecko", "Firefox",
    )
    entry.iface = object()
    raw_e = read(entry)
    assert raw_e.editable is True
    assert raw_e.role == "AXTextField"
    entry_snap = build_snapshot(
        (raw_e, []), _FakeAccessor(), scope=Scope.WINDOW, app="firefox", pid=1, geometry=_geometry(),
    )
    assert entry_snap.elements[0].editable is True

    frozen = _Tagged(
        "entry", "Locked", "no", {"EDITABLE", "READ_ONLY", "ENABLED"}, "input", "Gecko", "Firefox",
    )
    frozen.iface = object()
    assert read(frozen).editable is False

    sent: list[str] = []
    monkeypatch.setattr(_linux_input, "press_chord", lambda chord: sent.append(chord))
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: paragraph)
    box = Bounds(0, 8, 40, 200, 20)
    wrongly = Element(
        "e9", "AXStaticText", "", "idle", box, "snap-1", editable=True,
    )
    with pytest.raises(ComputerUseError) as exc:
        LinuxDriver().set_value(wrongly, "nope")
    assert exc.value.detail["reason"] == "not_editable"
    assert sent == []
    assert paragraph.text == "idle"
    assert _atspi.set_text(paragraph, "nope") is False
    assert sent == []
    assert paragraph.text == "idle"


def test_gecko_textarea_entry_is_multi_line_text(monkeypatch) -> None:
    """Gecko exposes ``<textarea>`` as AT-SPI ``entry`` plus MULTI_LINE.

    A single-line ``<input>`` stays a text field. ``multiline`` and
    ``aria-multiline`` are the same fact when the state is absent. A false
    attribute is not a textarea. Chromium is not this mapping. The snapshot
    word is ``textarea``. Not a live Firefox.
    """
    monkeypatch.setattr(_atspi, "_state_flags", lambda acc: (True, False, None, False, None, False, True))
    monkeypatch.setattr(_atspi, "_extents", lambda acc, keep_zero=False: ((8.0, 40.0), (240.0, 80.0)))
    monkeypatch.setattr(_atspi, "_value_text", lambda acc, role, role_name=None: acc.text)
    monkeypatch.setattr(_atspi, "_action_names", lambda acc: ("AXPress",))
    monkeypatch.setattr(_atspi, "_stable_id", lambda acc, attrs=None: None)

    def read(node: _Tagged):
        return _atspi.ATSPIAccessor().read(node)

    area = _Tagged(
        "entry", "Notes", "line1 line2",
        {"MULTI_LINE", "EDITABLE", "SINGLE_LINE"}, "textarea", "Gecko", "Firefox",
    )
    single = _Tagged(
        "entry", "Name", "initial-name",
        {"SINGLE_LINE", "EDITABLE"}, "input", "Gecko", "Firefox",
    )
    by_attr = _Tagged(
        "entry", "Notes", "line1 line2", {"EDITABLE"}, "textarea", "Gecko", "Firefox",
    )
    by_attr.attrs["multiline"] = "true"
    by_aria = _Tagged(
        "entry", "Notes", "line1 line2", {"EDITABLE"}, "textarea", "Gecko", "Firefox",
    )
    by_aria.attrs["aria-multiline"] = "true"
    by_bool = _Tagged(
        "entry", "Notes", "line1 line2", {"EDITABLE"}, "textarea", "Gecko", "Firefox",
    )
    by_bool.attrs["multiline"] = True
    off = _Tagged(
        "entry", "Name", "initial-name", {"EDITABLE"}, "input", "Gecko", "Firefox",
    )
    off.attrs["multiline"] = "false"
    off.attrs["aria-multiline"] = False
    one_true = _Tagged(
        "entry", "Notes", "line1 line2", {"EDITABLE"}, "textarea", "Gecko", "Firefox",
    )
    one_true.attrs["multiline"] = False
    one_true.attrs["aria-multiline"] = "yes"
    chrome = _Tagged(
        "entry", "Notes", "line1 line2",
        {"MULTI_LINE", "EDITABLE"}, "textarea", "Chromium", "Google Chrome",
    )
    assert _atspi._gecko_multiline_entry(chrome, chrome.get_attributes()) is False

    assert read(area).role == "AXTextArea"
    assert read(single).role == "AXTextField"
    assert read(by_attr).role == "AXTextArea"
    assert read(by_aria).role == "AXTextArea"
    assert read(by_bool).role == "AXTextArea"
    assert read(off).role == "AXTextField"
    assert read(one_true).role == "AXTextArea"

    snap = build_snapshot(
        (read(area), []), _FakeAccessor(), scope=Scope.WINDOW, app="firefox", pid=1, geometry=_geometry(),
    )
    assert 'textarea "Notes"' in observe.render_text(snap)
    single_snap = build_snapshot(
        (read(single), []), _FakeAccessor(), scope=Scope.WINDOW, app="firefox", pid=1, geometry=_geometry(),
    )
    assert 'textfield "Name"' in observe.render_text(single_snap)


def test_section_click_actions_map_like_the_figma_wrapper() -> None:
    """Live ``section#react-page`` exposes exactly click and showContextMenu.

    ``click`` is a press. ``clickAncestor`` and ``showContextMenu`` are not,
    so a container that only has those two is not clickable.
    """
    assert _atspi_actions("click", "showContextMenu") == ("AXPress",)
    assert _atspi_actions("clickAncestor", "showContextMenu") == ()
    assert _atspi._ROLE["section"] == "AXGroup"
    assert _atspi._ROLE.get("div", "AXGroup") == "AXGroup"


def _login_page(*, kind: str, wrapper_role: str = "AXGroup", wrapper_actions=(),
                focusable: bool = False, wrapper_title: str = ""):
    """document web 'Login | Figma' -> section#react-page 1271x0 -> the form.

    The zero-height section carries the actions the caller mapped from AT-SPI.
    """
    email = _node("AXTextField", "EMAIL", pos=(400.0, 300.0), size=(400.0, 40.0))
    password = _node(
        "AXSecureTextField", "PASSWORD", value="s3cret",
        pos=(400.0, 360.0), size=(400.0, 40.0),
    )
    google = _node(
        "AXButton", "Continue with Google", actions=("AXPress",),
        pos=(400.0, 420.0), size=(400.0, 36.0),
    )
    login = _node(
        "AXButton", "Log in", actions=("AXPress",),
        pos=(400.0, 470.0), size=(400.0, 36.0),
    )
    signup = _node(
        "AXLink", "Sign up", actions=("AXPress",),
        pos=(400.0, 520.0), size=(80.0, 16.0),
    )
    form = _node(
        "AXGroup", "", pos=(380.0, 280.0), size=(423.0, 480.0),
        children=[email, password, google, login, signup],
    )
    inner = _node("AXGroup", "", pos=(0.0, 0.0), size=(1271.0, 708.0), children=[form])
    mid = _node(
        "AXGroup", "", pos=(0.0, 0.0), size=(1271.0, 708.0),
        actions=_atspi_actions("clickAncestor", "showContextMenu"),
        children=[inner],
    )
    hollow = _node(
        wrapper_role, wrapper_title, pos=(0.0, 0.0), size=(1271.0, 0.0),
        actions=wrapper_actions, children=[mid], focusable=focusable,
        stable_id="react-page",
    )
    away = _node(
        "AXButton", "Away", actions=("AXPress",),
        pos=(-5000.0, -5000.0), size=(40.0, 20.0),
    )
    ghost = _node(
        "AXButton", "GhostBtn", actions=("AXPress",),
        pos=(10.0, 10.0), size=(0.0, 20.0),
    )
    doc = _node(
        "AXGroup", "Login | Figma", pos=(0.0, 0.0), size=(1271.0, 708.0),
        children=[hollow, away, ghost], atspi_web=kind,
    )
    return _node(
        "AXWindow", "Google Chrome", pos=(0.0, 0.0), size=(1280.0, 800.0),
        children=[doc],
    )


def _assert_login_form(snap) -> None:
    titles = {el.title for el in snap.elements}
    assert {"EMAIL", "Continue with Google", "Log in", "Sign up"} <= titles
    assert "Away" not in titles and "GhostBtn" not in titles
    secure = [el for el in snap.elements if el.role == "AXSecureTextField"]
    assert len(secure) == 1
    assert secure[0].title == "PASSWORD" and secure[0].secure and secure[0].value is None
    assert "s3cret" not in observe.render_text(snap)
    assert observe.find_elements(snap, text="EMAIL")
    assert observe.find_elements(snap, text="Continue with Google")
    assert observe.find_elements(snap, text="Log in")
    # The zero-height wrapper is kept and is not a clickable target.
    assert any(el.role == "AXGroup" and not el.title and not el.clickable for el in snap.elements)
    assert not any(
        el.role == "AXGroup" and not el.title and el.clickable for el in snap.elements
    )


def test_chromium_zero_height_section_keeps_the_login_form(monkeypatch) -> None:
    """section#react-page is 1271 by 0 and exposes click and showContextMenu.

    Those actions used to make the wrapper look interactive, so the pruner
    dropped it and the form with it. The entry, the secure field, and the
    buttons stay. A click on the wrapper does not call do_action.
    """
    actions = _atspi_actions("click", "showContextMenu")
    assert actions == ("AXPress",)
    snap = build_snapshot(
        _login_page(kind="page", wrapper_actions=actions),
        _FakeAccessor(), scope=Scope.WINDOW, app="chrome", pid=1, geometry=_geometry(),
    )
    _assert_login_form(snap)

    framed = build_snapshot(
        _login_page(kind="docframe", wrapper_actions=actions),
        _FakeAccessor(), scope=Scope.WINDOW, app="chrome", pid=1, geometry=_geometry(),
    )
    assert any(el.title == "EMAIL" for el in framed.elements)

    bare = build_snapshot(
        _login_page(kind="", wrapper_actions=actions),
        _FakeAccessor(), scope=Scope.WINDOW, app="chrome", pid=1, geometry=_geometry(),
    )
    assert "EMAIL" not in {el.title for el in bare.elements}
    assert not any(el.role == "AXSecureTextField" for el in bare.elements)

    pressed: list[object] = []
    monkeypatch.setattr(_atspi, "do_press", lambda handle: pressed.append(handle) or True)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    for el in snap.elements:
        if el.role == "AXGroup" and not el.title:
            assert driver.press_element(el) is False
    assert pressed == []
    login = next(el for el in snap.elements if el.title == "Log in")
    assert driver.press_element(login) is True
    assert pressed == [observe.ax_handle_for(login.snapshot_id, login.ref)]


@pytest.mark.parametrize("role_name", ["section", "div"])
def test_zero_section_or_div_with_click_and_show_context_menu_keeps_the_form(role_name: str) -> None:
    """AT-SPI role ``section`` and an unmapped ``div`` are both AXGroup."""
    role = _atspi._ROLE.get(role_name, "AXGroup")
    assert role == "AXGroup"
    snap = build_snapshot(
        _login_page(
            kind="page", wrapper_role=role,
            wrapper_actions=_atspi_actions("click", "showContextMenu"),
        ),
        _FakeAccessor(), scope=Scope.WINDOW, app="chrome", pid=1, geometry=_geometry(),
    )
    _assert_login_form(snap)


def test_find_bar_snapshot_is_remembered_and_the_address_bar_is_not() -> None:
    """The find field latches the Chrome pid. The address bar does not."""
    find = _NS(title="Find", role="AXTextField", value="")
    close = _NS(title="Close find bar", role="AXButton", value=None)
    address = _NS(title="Address and search bar", role="AXTextField", value="https://example.com/")
    button = _NS(title="Find", role="AXButton", value=None)
    assert _atspi.snapshot_shows_browser_bar([find])
    assert _atspi.snapshot_shows_browser_bar([close])
    assert not _atspi.snapshot_shows_browser_bar([address, button])
    _atspi._BROWSER_BAR_PIDS.clear()
    _atspi._BROWSER_BAR_OPEN.clear()


def test_find_field_focus_latches_and_the_address_bar_does_not(monkeypatch) -> None:
    """scroll_to_find types into Find without snapshotting the open bar."""
    _atspi._BROWSER_BAR_PIDS.clear()
    _atspi._BROWSER_BAR_CLEARED.clear()
    _atspi._BROWSER_BAR_OPEN.clear()

    class Find:
        def get_name(self):
            return "Find"

    monkeypatch.setattr(_atspi, "_focused_node", lambda app, **_k: (Find(), False))
    monkeypatch.setattr(_atspi, "_chromium_app", lambda _acc: True)
    monkeypatch.setattr(_atspi, "_get_attributes", lambda _acc: {})
    monkeypatch.setattr(_atspi, "pid_of", lambda _acc: 4242)
    _atspi.note_focused_find_bar("chrome")
    assert 4242 in _atspi._BROWSER_BAR_PIDS

    class Address(Find):
        def get_name(self):
            return "Address and search bar"

    _atspi._BROWSER_BAR_PIDS.clear()
    _atspi._BROWSER_BAR_CLEARED.clear()
    _atspi._BROWSER_BAR_OPEN.clear()
    monkeypatch.setattr(_atspi, "_focused_node", lambda app, **_k: (Address(), False))
    _atspi.note_focused_find_bar("chrome")
    assert _atspi._BROWSER_BAR_PIDS == set()


def test_latch_chromium_find_records_the_pid_before_the_bar_is_seen(monkeypatch) -> None:
    """scroll_to_find closes Find before a snapshot can latch on focus."""
    _atspi._BROWSER_BAR_PIDS.clear()
    _atspi._BROWSER_BAR_CLEARED.clear()
    _atspi._BROWSER_BAR_OPEN.clear()
    _atspi._BROWSER_BAR_CLEARED.add(4242)
    monkeypatch.setattr(_atspi, "find_root", lambda app, scope: object())
    monkeypatch.setattr(_atspi, "_chromium_app", lambda _acc: True)
    monkeypatch.setattr(_atspi, "pid_of", lambda _acc: 4242)
    assert _atspi.latch_chromium_find("chrome") is True
    assert 4242 in _atspi._BROWSER_BAR_PIDS
    assert 4242 not in _atspi._BROWSER_BAR_CLEARED

    _atspi._BROWSER_BAR_PIDS.clear()
    monkeypatch.setattr(_atspi, "_chromium_app", lambda _acc: False)
    assert _atspi.latch_chromium_find("gedit") is False
    assert _atspi._BROWSER_BAR_PIDS == set()

    monkeypatch.setattr(_atspi, "find_root", lambda app, scope: None)
    assert _atspi.latch_chromium_find("chrome") is False
    assert _atspi._BROWSER_BAR_PIDS == set()

    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(_atspi, "find_root", lambda app, scope: object())
    monkeypatch.setattr(_atspi, "_chromium_app", lambda _acc: True)
    assert driver.latch_chromium_find("chrome") is True
    assert 4242 in _atspi._BROWSER_BAR_PIDS
    assert 4242 in _atspi._BROWSER_BAR_OPEN
    _atspi._BROWSER_BAR_CLEARED.add(4242)
    lingering = _NS(title="Close find bar", role="AXButton", value=None)
    _atspi.note_browser_bar(object(), [lingering])
    assert 4242 in _atspi._BROWSER_BAR_CLEARED
    _atspi.note_browser_bar(object(), [])
    assert 4242 not in _atspi._BROWSER_BAR_OPEN
    _atspi.note_browser_bar(object(), [lingering])
    assert 4242 not in _atspi._BROWSER_BAR_CLEARED
    _atspi._BROWSER_BAR_PIDS.clear()
    _atspi._BROWSER_BAR_CLEARED.clear()
    _atspi._BROWSER_BAR_OPEN.clear()


def test_stale_find_frame_beside_the_page_clears_the_cache(monkeypatch) -> None:
    """A cached find-in-page sibling is dropped even before the pid is latched."""
    cleared: list[object] = []
    monkeypatch.setattr(_atspi, "_chromium_app", lambda _root: True)
    monkeypatch.setattr(_atspi, "_browser_bar_latched", lambda _root: False)
    monkeypatch.setattr(
        _atspi, "_call_first", lambda _obj, _names, *_a, **_k: "app",
    )
    monkeypatch.setattr(_atspi, "_application_bar_frame", lambda _app: "bar")
    monkeypatch.setattr(_atspi, "_is_browser_bar_frame", lambda node: node == "bar")
    monkeypatch.setattr(_atspi, "_clear_browser_bar_cache", lambda node: cleared.append(node))
    assert _atspi.chromium_snapshot_root("page") == "page"
    assert cleared == ["app"]
    cleared.clear()
    assert _atspi.chromium_snapshot_root("bar") == "bar"
    assert cleared == []


def test_empty_untitled_chrome_frame_yields_the_named_page() -> None:
    """An empty untitled Chrome frame is not the snapshot when a titled window exists.

    Chrome keeps those frames beside the document. When one is active, the
    walk is an untitled group with no rows, and scroll_to_find stops before
    the wheel. The titled window is the root. A frame that already holds a
    document stays, and the open find bar stays until it is latched.
    """

    class _States:
        def __init__(self, names):
            self.names = frozenset(names)

    class _Node:
        def __init__(self, name, *, role="frame", children=(), states=(), pid=5151):
            self._name = name
            self._role = role
            self._children = list(children)
            self._states = frozenset(states)
            self._pid = pid
            self._app = None

        def get_name(self):
            return self._name

        def get_role_name(self):
            return self._role

        def get_child_count(self):
            return len(self._children)

        def get_child_at_index(self, index):
            return self._children[index]

        def get_state_set(self):
            return _States(self._states)

        def get_process_id(self):
            return self._pid

        def get_application(self):
            return self._app

        def get_toolkit_name(self):
            return "Chrome"

    def application(*frames):
        app = _Node("Google Chrome", role="application", children=list(frames))
        for frame in frames:
            frame._app = app
        return app

    def empty_frame(**states):
        filler = _Node("", role="panel")
        panel = _Node("", role="panel", children=[filler])
        return _Node("", children=[panel], states=states.get("states", ("ACTIVE", "SHOWING")))

    _atspi._BROWSER_BAR_PIDS.clear()
    _atspi._BROWSER_BAR_CLEARED.clear()
    _atspi._BROWSER_BAR_OPEN.clear()
    try:
        empty = empty_frame()
        page = _Node(
            "cualong - Google Chrome",
            states=("SHOWING",),
            children=[_Node("cualong", role="document web")],
        )
        application(empty, page)
        assert _atspi.chromium_snapshot_root(empty) is page
        assert _atspi.chromium_snapshot_root(page) is page

        document = _Node("", children=[_Node("body", role="document web")])
        other = _Node("Other - Google Chrome", states=("SHOWING",))
        application(document, other)
        assert _atspi.chromium_snapshot_root(document) is document

        only = empty_frame()
        application(only)
        assert _atspi.chromium_snapshot_root(only) is only

        bar = _Node("Find in page", states=("ACTIVE", "SHOWING"))
        page = _Node("cualong - Google Chrome", states=("SHOWING",))
        empty = empty_frame(states=("SHOWING",))
        application(empty, bar, page)
        assert _atspi.chromium_snapshot_root(bar) is bar

        _atspi._BROWSER_BAR_PIDS.add(5151)
        assert _atspi.chromium_snapshot_root(bar) is page
        assert _atspi.chromium_snapshot_root(empty) is page
    finally:
        _atspi._BROWSER_BAR_PIDS.clear()
        _atspi._BROWSER_BAR_CLEARED.clear()
        _atspi._BROWSER_BAR_OPEN.clear()


def test_scrolled_off_chromium_root_keeps_the_onscreen_page() -> None:
    """Find-in-page parks the content root above the viewport.

    The section is viewport-sized at a negative Y, so its own box misses
    the screen. The heading now on screen stays. A control that scrolled
    away, and one below the fold, stay out. Without the web mark the
    whole subtree drops.
    """
    heading = _node(
        "AXStaticText", "ElevationMarkerUnique",
        pos=(80.0, 220.0), size=(240.0, 24.0),
    )
    above = _node(
        "AXStaticText", "TopMarkerAlmaty",
        pos=(80.0, -1800.0), size=(200.0, 20.0),
    )
    below = _node(
        "AXStaticText", "BelowFold",
        pos=(80.0, 2400.0), size=(120.0, 20.0),
    )
    article = _node(
        "AXGroup", "Almaty", pos=(54.0, -1400.0), size=(1000.0, 4200.0),
        children=[above, heading, below],
    )
    root = _node(
        "AXGroup", "", pos=(54.0, -2143.0), size=(1077.0, 709.0),
        children=[article],
    )
    doc = _node(
        "AXGroup", "Find Scroll", pos=(54.0, 91.0), size=(1092.0, 709.0),
        children=[root], atspi_web="page",
    )
    window = _node(
        "AXWindow", "Google Chrome", pos=(0.0, 0.0), size=(1280.0, 800.0),
        children=[doc],
    )
    snap = build_snapshot(
        window, _FakeAccessor(), scope=Scope.WINDOW, app="chrome", pid=1,
        geometry=_geometry(),
    )
    titles = {el.title for el in snap.elements}
    assert "ElevationMarkerUnique" in titles
    assert "TopMarkerAlmaty" not in titles
    assert "BelowFold" not in titles
    assert "Find Scroll" in titles

    unmarked = _node(
        "AXGroup", "Find Scroll", pos=(54.0, 91.0), size=(1092.0, 709.0),
        children=[root],
    )
    bare = build_snapshot(
        _node(
            "AXWindow", "Google Chrome", pos=(0.0, 0.0), size=(1280.0, 800.0),
            children=[unmarked],
        ),
        _FakeAccessor(), scope=Scope.WINDOW, app="chrome", pid=1, geometry=_geometry(),
    )
    assert "ElevationMarkerUnique" not in {el.title for el in bare.elements}


def test_focusable_or_named_zero_wrapper_still_drops_its_subtree() -> None:
    """A zero-size node that can take focus, or that has a name, stays dropped.

    A push button with a zero box stays dropped too.
    """
    actions = _atspi_actions("click", "showContextMenu")
    for tree in (
        _login_page(kind="page", wrapper_actions=actions, focusable=True),
        _login_page(kind="page", wrapper_actions=actions, wrapper_title="Banner"),
    ):
        snap = build_snapshot(
            tree, _FakeAccessor(), scope=Scope.WINDOW, app="chrome", pid=1,
            geometry=_geometry(),
        )
        assert "EMAIL" not in {el.title for el in snap.elements}
        assert "GhostBtn" not in {el.title for el in snap.elements}


def _captcha_window(*, marked: bool):
    """internal frame 'reCAPTCHA' -> document web -> the section nesting from
    the 2captcha page -> checkbox. Buried under enough sibling groups that the
    iframe's parent is already at MAX_DEPTH."""
    checkbox = _node(
        "AXCheckBox", "I'm not a robot", actions=("AXPress",),
        pos=(367.0, 401.0), size=(29.0, 29.0),
    )

    def section(children, *, extra: int = 0):
        kids = list(children)
        for i in range(extra):
            kids.append(_node(
                "AXStaticText", f"pad {i}", pos=(310.0, 360.0), size=(20.0, 10.0),
            ))
        return _node("AXGroup", "", pos=(300.0, 350.0), size=(400.0, 80.0), children=kids)

    leaf = section([checkbox])
    leaf = section([leaf])
    leaf = section([leaf])
    leaf = section([leaf], extra=2)  # cc=3
    leaf = section([leaf], extra=1)  # cc=2
    inner = _node(
        "AXGroup", "reCAPTCHA", pos=(300.0, 350.0), size=(304.0, 78.0),
        children=[
            leaf,
            _node("AXStaticText", "privacy", pos=(310.0, 400.0), size=(40.0, 12.0)),
        ],
        atspi_web="page" if marked else "",
    )
    frame = _node(
        "AXGroup", "reCAPTCHA", pos=(300.0, 350.0), size=(304.0, 78.0),
        children=[inner],
        atspi_web="iframe" if marked else "",
    )
    buried = frame
    for i in range(observe.MAX_DEPTH - 1):
        buried = _node(
            "AXGroup", "", pos=(0.0, 0.0), size=(1279.0, 812.0),
            children=[
                buried,
                _node("AXStaticText", f"crumb {i}", pos=(2.0, 2.0), size=(8.0, 8.0)),
            ],
        )
    page = _node(
        "AXGroup", "Demo", pos=(0.0, 0.0), size=(1279.0, 812.0),
        children=[buried], atspi_web="page" if marked else "",
    )
    window = _node(
        "AXWindow", "Chrome", pos=(0.0, 0.0), size=(1280.0, 800.0), children=[page],
    )
    return window, checkbox


class _CountingFake(_FakeAccessor):
    def __init__(self) -> None:
        self.reads = 0

    def read(self, node):
        self.reads += 1
        return super().read(node)


def test_nested_iframe_checkbox_survives_depth_and_presses_via_do_action(monkeypatch) -> None:
    window, checkbox = _captcha_window(marked=True)
    counter = _CountingFake()
    snap = build_snapshot(
        window, counter, scope=Scope.WINDOW, app="chrome", pid=1, geometry=_geometry(),
    )
    assert counter.reads < 500, "the iframe restart must stay bounded"
    boxes = [el for el in snap.elements if el.title == "I'm not a robot"]
    assert len(boxes) == 1
    box = boxes[0]
    assert box.role == "AXCheckBox" and box.clickable
    assert [el.ref for el in observe.find_elements(snap, text="I'm not a robot")] == [box.ref]
    assert box.ref in {el.ref for el in observe.find_elements(snap, role="checkbox")}

    pressed: list[object] = []
    monkeypatch.setattr(_atspi, "do_press", lambda acc: pressed.append(acc) or True)
    monkeypatch.delenv("A11Y_COMPUTER_USE_ATSPI_EVENTS", raising=False)
    assert LinuxDriver().press_element(box) is True
    assert pressed == [checkbox]

    bare, _same = _captcha_window(marked=False)
    hidden = build_snapshot(
        bare, _FakeAccessor(), scope=Scope.WINDOW, app="chrome", pid=1, geometry=_geometry(),
    )
    assert not any(el.title == "I'm not a robot" for el in hidden.elements)


def test_password_role_is_secure_and_never_leaks_value() -> None:
    tree = _node(
        "AXWindow", "Login", pos=(0.0, 0.0), size=(400.0, 300.0),
        children=[_node("AXSecureTextField", "Password", value="hunter2",
                        pos=(10.0, 10.0), size=(300.0, 30.0))],
    )
    snap = build_snapshot(tree, _FakeAccessor(), scope=Scope.WINDOW, app="app", pid=1,
                          geometry=_geometry())
    secure = [el for el in snap.elements if el.secure]
    assert secure, "password text should be marked secure"
    assert all(el.value is None for el in secure), "secure fields must never carry a value"


def test_chord_parser_and_keysyms() -> None:
    # valid chords parse without touching gi/Atspi
    _linux_input.validate_chord("ctrl+a")
    _linux_input.validate_chord("ctrl+shift+t")
    _linux_input.validate_chord("escape")
    mods, key = _linux_input._parse_chord("ctrl+shift+t")
    assert mods == [0xFFE3, 0xFFE1] and key == ord("t")
    with pytest.raises(ValueError):
        _linux_input.validate_chord("ctrl+")  # no non-modifier key
    with pytest.raises(ValueError):
        _linux_input.validate_chord("meta+nope")  # unknown key


def test_enable_a11y_status_opt_out(monkeypatch) -> None:
    from a11y_computer_use.drivers import _atspi
    monkeypatch.setattr(_atspi, "_a11y_status_forced", False)
    monkeypatch.setenv("A11Y_COMPUTER_USE_NO_WEB_A11Y", "1")
    assert _atspi.enable_a11y_status() is False  # opt-out short-circuits before any D-Bus


def test_atspi_events_gate(monkeypatch) -> None:
    import warnings

    from a11y_computer_use.drivers import _atspi_events

    _atspi_events._warned_values.clear()
    monkeypatch.delenv("A11Y_COMPUTER_USE_ATSPI_EVENTS", raising=False)
    monkeypatch.setattr(_atspi_events.sys, "platform", "linux")
    monkeypatch.setattr(_atspi_events, "typelib_imports", lambda: True)
    assert _atspi_events.enabled() is True
    monkeypatch.setattr(_atspi_events, "typelib_imports", lambda: False)
    assert _atspi_events.enabled() is False
    monkeypatch.setattr(_atspi_events.sys, "platform", "darwin")
    assert _atspi_events.enabled() is False
    for off in ("0", "false", "no", "off", "FALSE", " Off "):
        monkeypatch.setenv("A11Y_COMPUTER_USE_ATSPI_EVENTS", off)
        assert _atspi_events.enabled() is False
    for on in ("1", "true", "yes", "on", "YES", " On "):
        monkeypatch.setenv("A11Y_COMPUTER_USE_ATSPI_EVENTS", on)
        assert _atspi_events.enabled() is True

    monkeypatch.setattr(_atspi_events.sys, "platform", "linux")
    monkeypatch.setattr(_atspi_events, "typelib_imports", lambda: True)
    monkeypatch.setenv("A11Y_COMPUTER_USE_ATSPI_EVENTS", "maybe")
    with pytest.warns(RuntimeWarning, match="maybe"):
        assert _atspi_events.enabled() is True
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert _atspi_events.enabled() is True
    assert caught == []

    monkeypatch.setattr(_atspi_events, "typelib_imports", lambda: False)
    _atspi_events._warned_values.clear()
    monkeypatch.setenv("A11Y_COMPUTER_USE_ATSPI_EVENTS", "2")
    with pytest.warns(RuntimeWarning, match="using the default \\(off\\)"):
        assert _atspi_events.enabled() is False


def test_linux_driver_run_inline_when_events_disabled(monkeypatch) -> None:
    from a11y_computer_use.drivers import _atspi_events  # noqa: F401
    from a11y_computer_use.drivers.linux import LinuxDriver
    monkeypatch.setenv("A11Y_COMPUTER_USE_ATSPI_EVENTS", "0")
    d = LinuxDriver()
    assert d._run(lambda: 42) == 42  # opt-out runs inline, no event thread


# --- XTEST pointer positioning (the coordinate/vision fallback) ---------------
#
# python-xlib is not installed on macOS/Windows dev machines, so these tests
# install a fake `Xlib` (X constants + xtest.fake_input recorder) and a fake
# cached display. What they pin: coordinate input positions the pointer with an
# ABSOLUTE XTEST MotionNotify, never `Display.warp_pointer`, which X treats as a
# move relative to the current pointer. Under Xvfb the pointer starts at (0, 0)
# so the two coincide and CI could not see the difference; on a real desktop
# (docs/box-testbed.md) the relative warp put clicks at pointer + (x, y).

_X_MOTION, _X_BPRESS, _X_BRELEASE = 6, 4, 5  # X11 protocol event codes


class _FakeXDisplay:
    def __init__(self):
        self.warps: list[tuple[int, int]] = []
        self.syncs = 0

    def warp_pointer(self, x, y):  # the trap: relative motion
        self.warps.append((x, y))

    def sync(self):
        self.syncs += 1

    def keysym_to_keycode(self, keysym):
        return 0  # no modifiers mapped -> `held()` presses nothing


@pytest.fixture
def xtest_recorder(monkeypatch):
    """Install a fake Xlib whose xtest.fake_input records (event, detail, x, y)
    and route _linux_input at a fake cached display. Yields (events, display)."""
    import sys
    import types

    events: list[tuple[int, int, int, int]] = []
    X = types.ModuleType("Xlib.X")
    X.MotionNotify, X.ButtonPress, X.ButtonRelease = _X_MOTION, _X_BPRESS, _X_BRELEASE
    X.KeyPress, X.KeyRelease, X.CurrentTime, X.NONE = 2, 3, 0, 0
    xtest = types.ModuleType("Xlib.ext.xtest")

    def fake_input(display, event_type, detail=0, time=0, root=0, x=0, y=0):
        events.append((event_type, detail, x, y))

    xtest.fake_input = fake_input
    ext = types.ModuleType("Xlib.ext")
    ext.xtest = xtest
    xlib = types.ModuleType("Xlib")
    xlib.X, xlib.ext = X, ext
    for name, mod in {"Xlib": xlib, "Xlib.X": X, "Xlib.ext": ext, "Xlib.ext.xtest": xtest}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    display = _FakeXDisplay()
    monkeypatch.setattr(_linux_input, "_display", display)
    return events, display


def test_hover_moves_the_pointer_and_sends_no_button(xtest_recorder) -> None:
    events, display = xtest_recorder
    _linux_input.hover(320, 180)
    assert events == [(_X_MOTION, 0, 320, 180)]
    assert display.warps == []
    assert display.syncs == 1


def test_driver_hover_is_motion_only(xtest_recorder) -> None:
    events, display = xtest_recorder
    assert LinuxDriver().hover(Point(0, 240, 90)) is None
    assert events == [(_X_MOTION, 0, 240, 90)]
    assert display.syncs == 1
    assert display.warps == []


def test_driver_hover_on_wayland_sends_nothing(xtest_recorder, monkeypatch) -> None:
    events, display = xtest_recorder
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    with pytest.raises(ComputerUseError) as error:
        LinuxDriver().hover(Point(0, 12, 12))
    assert error.value.code is ErrorCode.UNSUPPORTED
    assert events == []
    assert display.syncs == 0


def test_click_positions_pointer_absolutely_then_presses(xtest_recorder) -> None:
    events, display = xtest_recorder
    _linux_input.click(500, 400)
    assert events == [(_X_MOTION, 0, 500, 400), (_X_BPRESS, 1, 0, 0), (_X_BRELEASE, 1, 0, 0)]
    assert display.warps == [], "relative warp_pointer must never be used for coordinates"
    assert display.syncs == 1  # one flush per logical operation


def test_click_button_and_count(xtest_recorder) -> None:
    events, _ = xtest_recorder
    _linux_input.click(10, 20, button="right", count=2)
    assert events[0] == (_X_MOTION, 0, 10, 20)
    assert events[1:] == [(_X_BPRESS, 3, 0, 0), (_X_BRELEASE, 3, 0, 0)] * 2


def test_drag_moves_absolutely_at_both_endpoints(xtest_recorder, monkeypatch) -> None:
    monkeypatch.setattr(_linux_input, "DRAG_PACE_S", 0)
    monkeypatch.setattr(_linux_input, "DRAG_THRESHOLD_PAUSE_S", 0)
    monkeypatch.setattr(_linux_input, "DRAG_HOVER_S", 0)
    events, display = xtest_recorder
    _linux_input.drag(10, 20, 300, 400)
    assert events[:2] == [(_X_MOTION, 0, 10, 20), (_X_BPRESS, 1, 0, 0)]
    assert events[-2:] == [(_X_MOTION, 0, 300, 400), (_X_BRELEASE, 1, 0, 0)]
    motions = [(x, y) for kind, _d, x, y in events[2:-1]]
    assert all(kind == _X_MOTION for kind, *_ in events[2:-1])  # button held throughout
    hops = [math.hypot(bx - ax, by - ay) for (ax, ay), (bx, by) in zip([(10, 20), *motions], motions)]
    assert max(hops) <= _linux_input.DRAG_STEP_PX + 1  # walked, not teleported (+1: rounding)
    assert display.warps == []


def test_drag_walks_through_every_waypoint_in_order(xtest_recorder, monkeypatch) -> None:
    """A freehand-brush stroke: the waypoints must all be visited, in order,
    with the button held, so a canvas draws the curve rather than a chord."""
    monkeypatch.setattr(_linux_input, "DRAG_PACE_S", 0)
    monkeypatch.setattr(_linux_input, "DRAG_THRESHOLD_PAUSE_S", 0)
    monkeypatch.setattr(_linux_input, "DRAG_HOVER_S", 0)
    events, _display = xtest_recorder
    path = [(60, 20), (60, 80), (10, 80)]
    _linux_input.drag(10, 20, 10, 20, path=path)
    assert events[1] == (_X_BPRESS, 1, 0, 0) and events[-1] == (_X_BRELEASE, 1, 0, 0)
    motions = [(x, y) for kind, _d, x, y in events[2:-1] if kind == _X_MOTION]
    seen = [motions.index(p) for p in [*path, (10, 20)]]  # every waypoint is a motion event
    assert seen == sorted(seen)
    assert len(motions) > len(path) + 1  # intermediate hops between waypoints


def test_hops_end_exactly_on_the_target() -> None:
    assert _linux_input._hops(0, 0, 0, 0) == [(0, 0)]
    hops = _linux_input._hops(0, 0, 100, 0)
    assert hops[-1] == (100, 0) and len(hops) == 13


def test_drag_pauses_past_the_gtk_threshold_then_hovers(xtest_recorder, monkeypatch) -> None:
    """A short stroke still walks in small hops. GTK needs a pause once the
    pointer is past the threshold, and a rest on the target before release."""
    import time

    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))
    events, _display = xtest_recorder
    _linux_input.drag(0, 0, 0, 28)
    assert events[0] == (_X_MOTION, 0, 0, 0)
    assert events[1] == (_X_BPRESS, 1, 0, 0)
    assert events[-2:] == [(_X_MOTION, 0, 0, 28), (_X_BRELEASE, 1, 0, 0)]
    motions = [(x, y) for kind, _d, x, y in events[2:-1]]
    hops = [abs(by - ay) for (_ax, ay), (_bx, by) in zip([(0, 0), *motions], motions)]
    assert hops and max(hops) <= _linux_input.DRAG_STEP_PX + 1
    # 28px is four hops: 7 (still under the threshold), 14 (past it), 21, 28.
    assert sleeps == [
        _linux_input.DRAG_PACE_S,
        _linux_input.DRAG_THRESHOLD_PAUSE_S,
        _linux_input.DRAG_PACE_S,
        _linux_input.DRAG_HOVER_S,
    ]
    sleeps.clear()
    _linux_input.drag(0, 0, 0, 9)
    assert sleeps == [
        _linux_input.DRAG_PACE_S,
        _linux_input.DRAG_THRESHOLD_PAUSE_S,
        _linux_input.DRAG_HOVER_S,
    ]


def test_drag_below_the_gtk_threshold_does_not_pause(xtest_recorder, monkeypatch) -> None:
    """A slider nudge that never clears the threshold keeps the old short pace."""
    import time

    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))
    events, _display = xtest_recorder
    _linux_input.drag(0, 0, 0, 8)
    assert events[-2:] == [(_X_MOTION, 0, 0, 8), (_X_BRELEASE, 1, 0, 0)]
    assert sleeps == [_linux_input.DRAG_PACE_S]
    assert _linux_input.DRAG_THRESHOLD_PAUSE_S not in sleeps
    assert _linux_input.DRAG_HOVER_S not in sleeps


def test_scroll_positions_before_wheel_notches(xtest_recorder) -> None:
    events, display = xtest_recorder
    _linux_input.scroll(100, 200, dy=2, dx=-1)
    assert events[0] == (_X_MOTION, 0, 100, 200)
    assert events[1:5] == [(_X_BPRESS, 5, 0, 0), (_X_BRELEASE, 5, 0, 0)] * 2  # wheel down x2
    assert events[5:] == [(_X_BPRESS, 6, 0, 0), (_X_BRELEASE, 6, 0, 0)]  # horizontal
    assert display.warps == []


class _GermanLayoutDisplay(_FakeXDisplay):
    """The keymap python-xlib reports for a de layout: '@' lives on AltGr+q
    (``keycode 24 = q Q q Q at Greek_OMEGA``), so keysym_to_keycode(0x40) finds
    keycode 24 although neither its base nor its Shift level produces '@'. Records
    keymap changes so a test can see the spare-keycode path being taken."""

    def __init__(self) -> None:
        super().__init__()
        self.keymap = {
            24: [0x71, 0x51, 0x71, 0x51, 0x40, 0x7D9],  # q Q q Q at Greek_OMEGA
            37: [0xFFE3, 0, 0, 0, 0, 0],  # Control_L
            50: [0xFFE1, 0, 0, 0, 0, 0],  # Shift_L
        }
        self.remapped: list[tuple[int, int]] = []  # (keycode, new base keysym)
        self.display = _NS(info=_NS(min_keycode=8, max_keycode=255))

    def keysym_to_keycode(self, keysym):
        return next((kc for kc, syms in self.keymap.items() if keysym in syms), 0)

    def keycode_to_keysym(self, keycode, index):
        syms = self.keymap.get(keycode, [])
        return syms[index] if index < len(syms) else 0

    def get_keyboard_mapping(self, first, count):
        return [self.keymap.get(first + i, [0] * 6) for i in range(count)]

    def change_keyboard_mapping(self, first, keysyms):
        self.remapped.append((first, keysyms[0][0]))
        self.keymap[first] = list(keysyms[0])


def test_altgr_only_keysym_is_typed_through_a_spare_keycode(xtest_recorder, monkeypatch) -> None:
    events, _ = xtest_recorder
    display = _GermanLayoutDisplay()
    monkeypatch.setattr(_linux_input, "_display", display)
    key_press, key_release = 2, 3  # the fake Xlib.X constants installed by the fixture

    # base and Shift levels still map directly; the AltGr-only '@' does not
    assert _linux_input._keycode_and_shift(0x71) == (24, False)
    assert _linux_input._keycode_and_shift(0x51) == (24, True)
    assert _linux_input._keycode_and_shift(0x40) == (None, False)

    _linux_input.type_string("@")
    keys = [(t, d) for t, d, _x, _y in events]
    assert (key_press, 24) not in keys, "typing '@' must not press the q key"
    (spare, bound), (restored, orig) = display.remapped  # bound for the string, then restored
    assert bound == 0x40 and restored == spare and orig == 0 and spare not in (24, 37, 50)
    assert keys == [(key_press, spare), (key_release, spare)]

    events.clear()
    display.remapped.clear()
    _linux_input.press_chord("ctrl+@")
    keys = [(t, d) for t, d, _x, _y in events]
    assert (key_press, 24) not in keys, "ctrl+@ must not become ctrl+q"
    spare = display.remapped[0][0]
    assert keys == [(key_press, 37), (key_press, spare), (key_release, spare), (key_release, 37)]
    assert display.remapped[-1] == (spare, 0)  # the keymap is restored after the chord


def test_text_binds_complete_keymap_before_first_keystroke(xtest_recorder, monkeypatch) -> None:
    """A mixed string must not change key translation while text is in flight."""
    import time

    events, _ = xtest_recorder
    display = _GermanLayoutDisplay()
    display.keymap[65] = [ord(" ")] * 6
    monkeypatch.setattr(_linux_input, "_display", display)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    original_mapping = {kc: list(syms) for kc, syms in display.keymap.items()}
    change_mapping = display.change_keyboard_mapping
    mappings_when_bound: list[int] = []

    def checked_mapping(first: int, keysyms: list[list[int]]) -> None:
        if keysyms[0][0]:
            mappings_when_bound.append(len(events))
        change_mapping(first, keysyms)

    monkeypatch.setattr(display, "change_keyboard_mapping", checked_mapping)
    _linux_input.type_string("q @ü q@")

    assert mappings_when_bound == [0, 0], "prepare every missing keysym before any input"
    presses = [detail for event, detail, _x, _y in events if event == 2]
    bound = {sym: kc for kc, sym in display.remapped if sym}
    assert presses == [24, 65, bound[ord("@")], bound[ord("ü")], 65, 24, bound[ord("@")]]
    assert {kc: syms for kc, syms in display.keymap.items() if any(syms)} == original_mapping


def test_text_capacity_failure_does_not_type_or_change_keymap(xtest_recorder, monkeypatch) -> None:
    """Exhausted keycodes must not silently truncate a partly typed string."""
    import time

    events, _ = xtest_recorder
    display = _GermanLayoutDisplay()
    monkeypatch.setattr(_linux_input, "_display", display)
    monkeypatch.setattr(_linux_input, "_spare_keycodes", lambda _display: [255])
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with pytest.raises(ValueError, match="2 unmapped characters.*1 spare keycodes"):
        _linux_input.type_string("q@ü")

    assert events == []
    assert display.remapped == []


def test_repeated_unmapped_character_only_needs_one_spare(xtest_recorder, monkeypatch) -> None:
    import time

    events, _ = xtest_recorder
    display = _GermanLayoutDisplay()
    monkeypatch.setattr(_linux_input, "_display", display)
    monkeypatch.setattr(_linux_input, "_spare_keycodes", lambda _display: [255])
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    _linux_input.type_string("üüüü")

    assert [(event, detail) for event, detail, _x, _y in events] == [(2, 255), (3, 255)] * 4
    assert display.remapped == [(255, ord("ü")), (255, 0)]


def test_mapped_text_does_not_flood_input_method(xtest_recorder, monkeypatch) -> None:
    """Even ASCII must give asynchronous input methods time to dispatch keys."""
    import time

    events, _ = xtest_recorder
    display = _GermanLayoutDisplay()
    monkeypatch.setattr(_linux_input, "_display", display)
    dispatched: list[list[tuple[int, int]]] = []

    def dispatch(_seconds: float) -> None:
        assert _seconds > 0
        dispatched.append([(event, detail) for event, detail, _x, _y in events])
        events.clear()

    monkeypatch.setattr(time, "sleep", dispatch)
    _linux_input.type_string("qQq")

    assert dispatched == [
        [(2, 24), (3, 24)],
        [(2, 50), (2, 24), (3, 24), (3, 50)],
        [(2, 24), (3, 24)],
    ]
    assert not events
    assert display.remapped == []


class _LockDisplay(_GermanLayoutDisplay):
    """A keymap that can report Caps Lock and Num Lock, plus keypad keysyms.

    Keycode 90 is KP_Insert on level 0 and KP_0 on level 1, the usual
    Num Lock pair. Num_Lock itself sits on modifier 4 (mask 16).
    """

    def __init__(self, *, caps: bool = False, num: bool = False) -> None:
        super().__init__()
        self.keymap[10] = [0x31, 0x21, 0x31, 0x21, 0, 0]
        self.keymap[38] = [ord("b"), ord("B"), ord("b"), ord("B"), 0, 0]
        self.keymap[54] = [ord("c"), ord("C"), ord("c"), ord("C"), 0, 0]
        self.keymap[77] = [0xFF7F, 0, 0, 0, 0, 0]
        self.keymap[90] = [0xFF9E, 0xFFB0, 0, 0, 0, 0]
        self._mask = (2 if caps else 0) | (16 if num else 0)

    def screen(self):
        return _NS(root=_NS(query_pointer=lambda: _NS(mask=self._mask)))

    def get_modifier_mapping(self):
        mapping = [[] for _ in range(8)]
        mapping[4] = [77]
        return mapping


def _keys(events) -> list[tuple[int, int]]:
    return [(event, detail) for event, detail, _x, _y in events]


def test_caps_lock_does_not_invert_typed_letters(xtest_recorder, monkeypatch) -> None:
    """Letters are sent so the requested case lands while Caps Lock stays down.

    Caps Lock XOR Shift is what the server applies. A requested lowercase is
    Shift plus the key; a requested uppercase is the key alone. Digits are
    not letters and are not inverted. No Caps_Lock key event is delivered.
    """
    import time

    events, _ = xtest_recorder
    display = _LockDisplay(caps=True)
    monkeypatch.setattr(_linux_input, "_display", display)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    _linux_input.type_string("qQ1")
    assert _keys(events) == [
        (2, 50), (2, 24), (3, 24), (3, 50),  # q, Shift because Caps is down
        (2, 24), (3, 24),                    # Q, no Shift
        (2, 10), (3, 10),                    # 1, not a letter
    ]


def test_caps_lock_letter_chords_keep_the_requested_case(xtest_recorder, monkeypatch) -> None:
    events, _ = xtest_recorder
    display = _LockDisplay(caps=True)
    monkeypatch.setattr(_linux_input, "_display", display)
    _linux_input.press_chord("b")
    assert _keys(events) == [(2, 50), (2, 38), (3, 38), (3, 50)]
    events.clear()
    _linux_input.press_chord("shift+c")
    assert _keys(events) == [(2, 54), (3, 54)]


def test_keypad_digit_follows_num_lock_and_not_shift(xtest_recorder, monkeypatch) -> None:
    events, _ = xtest_recorder
    display = _LockDisplay(num=False)
    monkeypatch.setattr(_linux_input, "_display", display)
    _linux_input.press_chord("kp_0")
    assert _keys(events) == [(2, 77), (3, 77), (2, 90), (3, 90), (2, 77), (3, 77)]
    assert (2, 50) not in _keys(events)
    events.clear()
    display._mask |= 16
    _linux_input.press_chord("kp_0")
    assert _keys(events) == [(2, 90), (3, 90)]
    events.clear()
    _linux_input.press_chord("kp_insert")
    assert _keys(events) == [(2, 77), (3, 77), (2, 90), (3, 90), (2, 77), (3, 77)]


def test_binding_failure_restores_keymap_without_partial_text(xtest_recorder, monkeypatch) -> None:
    """A server error midway through preparation must not leave a remapped key."""
    import time

    events, _ = xtest_recorder
    display = _GermanLayoutDisplay()
    monkeypatch.setattr(_linux_input, "_display", display)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    change_mapping = display.change_keyboard_mapping

    def fail_second_binding(first: int, keysyms: list[list[int]]) -> None:
        if keysyms[0][0] == ord("ü"):
            raise RuntimeError("test server mapping failure")
        change_mapping(first, keysyms)

    monkeypatch.setattr(display, "change_keyboard_mapping", fail_second_binding)
    with pytest.raises(RuntimeError, match="test server mapping failure"):
        _linux_input.type_string("q@ü")

    assert events == []
    assert display.remapped[0][1] == ord("@")
    assert not any(display.keymap[display.remapped[0][0]])


# --- scroll units: lines are wheel notches, pixels are scroll-bar values ------


class _States:
    def __init__(self, names):
        self.names = set(names)

    def contains(self, member):
        return member in self.names


class _Rect:
    def __init__(self, width, height, x=0.0, y=0.0):
        self.x = float(x)
        self.y = float(y)
        self.width = width
        self.height = height


class _Geom:
    def __init__(self, width, height, x=0.0, y=0.0):
        self._rect = _Rect(width, height, x, y)

    def get_extents(self, _coord):
        return self._rect


class _Hit:
    def __init__(self, child):
        self.child = child

    def get_accessible_at_point(self, _x, _y, _coord):
        return self.child


class _Acc:
    """A stand-in accessible: role, children, and an AT-SPI Value."""

    def __init__(self, role, *, states=(), value=0.0, minimum=0.0, maximum=1000.0,
                 visual_max=None, width=0.0, height=0.0, jump=None, name=""):
        self.role = role
        self.name = name
        self.states = set(states)
        self.value = float(value)
        self.minimum = float(minimum)
        self.maximum = float(maximum)
        self.visual_max = float(self.maximum if visual_max is None else visual_max)
        self.children: list[_Acc] = []
        self.parent: _Acc | None = None
        self.component = _Geom(width, height) if (width or height) else None
        self.jump = jump

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_parent(self):
        return self.parent

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def get_state_set(self):
        return _States(self.states)

    def get_component_iface(self):
        return self.component


def _adopt(parent: _Acc, *children: _Acc) -> _Acc:
    parent.children = list(children)
    for child in children:
        child.parent = parent
    return parent


class _ValueApi:
    @staticmethod
    def get_current_value(acc):
        return acc.value

    @staticmethod
    def get_minimum_value(acc):
        return acc.minimum

    @staticmethod
    def get_maximum_value(acc):
        return acc.maximum

    @staticmethod
    def set_current_value(acc, new):
        hook = getattr(acc, "on_value", None)
        if hook is not None:
            return hook(acc, new)
        if acc.jump is not None:
            acc.value = acc.value + acc.jump
            acc.jump = None
            return True
        upper = min(acc.maximum, acc.visual_max)
        acc.value = min(max(float(new), acc.minimum), upper)
        return True


class _FakeAtspi:
    desktop = None

    class StateType:
        VERTICAL = "VERTICAL"
        HORIZONTAL = "HORIZONTAL"

    class CoordType:
        SCREEN = 1

    class ScrollType:
        ANYWHERE = "ANYWHERE"
        TOP_EDGE = "TOP_EDGE"

    class Hypertext:
        @staticmethod
        def get_n_links(acc):
            return len(getattr(acc, "links", ()) or ())

        @staticmethod
        def get_link(acc, index):
            return (getattr(acc, "links")[index],)

    class Hyperlink:
        @staticmethod
        def get_object(link, index):
            return link[0] if index == 0 else None

    class Text:
        @staticmethod
        def get_caret_offset(acc):
            caret = getattr(acc, "caret", None)
            if caret is None:
                return len(getattr(acc, "text", "") or "")
            return int(caret)

        @staticmethod
        def get_n_selections(acc):
            sel = getattr(acc, "selection", None)
            if not isinstance(sel, tuple) or len(sel) != 2:
                return 0
            return 1 if int(sel[1]) > int(sel[0]) else 0

        @staticmethod
        def get_selection(acc, _selection_num):
            sel = getattr(acc, "selection", None)
            return sel if isinstance(sel, tuple) else None

        @staticmethod
        def get_character_count(acc):
            if getattr(acc, "text_error", False):
                raise RuntimeError("no text")
            echo = getattr(acc, "echo", None)
            if echo is not None:
                return len(echo)
            return len(acc.text)

        @staticmethod
        def get_text(acc, start, end):
            if getattr(acc, "text_error", False):
                raise RuntimeError("no text")
            # Snapshots use end=-1 and must see the DOM, not a bounded echo.
            if end is not None and int(end) < 0:
                return acc.text
            echo = getattr(acc, "echo", None)
            if echo is not None and int(end) == len(echo):
                return echo
            end_i = len(acc.text) if end is None or int(end) > len(acc.text) else int(end)
            return acc.text[int(start):end_i]

        @staticmethod
        def set_selection(acc, _selection_num, start_offset, end_offset):
            acc.selection = (int(start_offset), int(end_offset))
            return True

        @staticmethod
        def set_caret_offset(acc, offset):
            acc.caret = int(offset)
            return True

        @staticmethod
        def add_selection(acc, start_offset, end_offset):
            acc.selection = (int(start_offset), int(end_offset))
            return True

    Value = _ValueApi

    @staticmethod
    def get_desktop(_index):
        return _FakeAtspi.desktop


@pytest.fixture
def fake_atspi(monkeypatch):
    _FakeAtspi.desktop = None
    _atspi.reset_shown_rows()
    monkeypatch.setattr(_atspi, "_atspi", lambda: _FakeAtspi)
    yield _FakeAtspi
    _FakeAtspi.desktop = None
    _atspi.reset_shown_rows()


@pytest.fixture
def gtk_font_combo(fake_atspi, monkeypatch):
    from a11y_computer_use.drivers import _cdp_forms

    def make(labels):
        app = _Acc("application", name="AbiWord")
        app.get_toolkit_name = lambda: "gtk"
        combo = _Acc("combo box")
        combo.get_application = lambda: app
        menu = _adopt(_Acc("menu"), *[
            _Acc("menu item", name=label) for label in labels
        ])
        _adopt(combo, menu)
        selected = []
        combo.get_selection_iface = lambda: combo
        combo.get_selected_child = lambda index: menu.children[selected[-1] if selected else 0]

        def select_child(index):
            selected.append(index)
            return True

        combo.select_child = select_child
        driver = LinuxDriver()
        monkeypatch.setattr(driver, "_run", lambda fn: fn())
        monkeypatch.setattr(driver, "_raise_handle_window", lambda handle: None)
        monkeypatch.setattr(observe, "ax_handle_for", lambda *args: combo)
        monkeypatch.setattr(_cdp_forms, "write_element", lambda *args, **kwargs: None)
        monkeypatch.setattr(_atspi, "peer_field_texts", lambda handle: {})
        monkeypatch.setattr(_atspi, "is_sheet_cell", lambda handle: False)
        element = Element("e1", "AXComboBox", "Font", labels[0],
                          Bounds(0, 0, 0, 100, 20), "font-combo")
        return driver, element, menu, selected

    return make


@pytest.mark.parametrize("option_number", [65, 300, 600])
def test_gtk_font_combo_set_value_searches_all_options(gtk_font_combo, option_number):
    labels = [f"Font {index:03}" for index in range(1, 601)]
    driver, element, menu, selected = gtk_font_combo(labels)
    assert driver.set_value(element, labels[option_number - 1]) is True
    assert selected == [option_number - 1]
    assert menu.children[selected[-1]].name == labels[option_number - 1]


def test_gtk_font_combo_missing_option_has_bounded_honest_message(gtk_font_combo):
    labels = [f"Font {index:03}" for index in range(1, 601)]
    driver, element, menu, selected = gtk_font_combo(labels)
    with pytest.raises(ValueError) as exc:
        driver.set_value(element, "Missing Font")
    message = str(exc.value)
    assert "showing first 64 of 600 options (list truncated)" in message
    assert "is not one of" not in message
    assert len(message) < 2200
    assert selected == []


def test_gtk_small_combo_selection_and_missing_message_unchanged(gtk_font_combo):
    driver, element, menu, selected = gtk_font_combo(["Red", "Green", "Blue"])
    assert driver.set_value(element, "Blue") is True
    assert selected == [2]
    with pytest.raises(ValueError, match="^value 'Missing' is not one of: Red, Green, Blue$"):
        driver.set_value(element, "Missing")
    assert selected == [2]


def test_gtk_combo_missing_message_bounds_long_labels(gtk_font_combo):
    driver, element, menu, selected = gtk_font_combo(["F" * 3000, "Blue"])
    with pytest.raises(ValueError) as exc:
        driver.set_value(element, "Missing")
    assert "showing first 0 of 2 options (list truncated)" in str(exc.value)
    assert len(str(exc.value)) < 2200
    assert selected == []


def _toggle_click_target(monkeypatch, *, toolkit="Qt", role="check box",
                         actions=("Toggle", "SetFocus"), bounds=Bounds(0, 40, 60, 100, 20)):
    """Synthetic Qt Toggle changes state; only mouse input runs the handler."""
    app = _Acc("application", name="toggleprobe")
    app.get_toolkit_name = lambda: toolkit
    control = _Acc(role, name="Option", states=("SHOWING", "VISIBLE", "ENABLED"))
    control.get_application = lambda: app
    control.get_action_iface = lambda: control
    control.get_n_actions = lambda: len(actions)
    control.get_action_name = lambda index: actions[index]
    action_log = []
    events = []

    def do_action(index):
        action_log.append(actions[index])
        control.states.symmetric_difference_update({"CHECKED"})
        return True

    control.do_action = do_action
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: control)
    monkeypatch.setattr(driver, "_raise_handle_window", lambda handle: events.append(("raise", handle)))
    monkeypatch.setattr("a11y_computer_use.drivers.linux._on_wayland", lambda: False)
    monkeypatch.setattr("a11y_computer_use.drivers.linux._screen_span", lambda: (1280, 800))
    monkeypatch.setattr(driver, "_pointer", lambda x, y: (x, y))
    monkeypatch.setattr(driver, "_note_pointer", lambda x, y: None)

    def click(x, y, *, button="left", count=1):
        events.append(("click", x, y, button, count))
        control.handler_ran = True

    monkeypatch.setattr(_linux_input, "click", click)
    element = Element(
        "e493", _atspi._ROLE[role], "Option", None, bounds, "snap-toggle", clickable=True,
    )
    return driver, control, element, action_log, events


@pytest.mark.parametrize("role", ["check box", "toggle button", "radio button", "list item"])
@pytest.mark.parametrize("actions", [("Toggle",), ("SetFocus", "Toggle")])
def test_qt_toggle_only_ref_click_runs_the_pointer_handler(fake_atspi, monkeypatch, role, actions):
    driver, control, element, action_log, events = _toggle_click_target(
        monkeypatch, role=role, actions=actions,
    )
    # Item-view selection can also silently bypass itemClicked. Neither
    # Toggle nor Selection.select_child may run before the mouse handler.
    if role == "list item":
        parent = _adopt(_Acc("list"), control)
        parent.get_selection_iface = lambda: parent
        parent.select_child = lambda _index: pytest.fail("selection bypassed the click handler")

    assert driver.press_element(element) is True
    assert action_log == []
    assert control.handler_ran is True
    assert events == [("raise", control), ("click", 90, 70, "left", 1)]


@pytest.mark.parametrize("toolkit", ["gtk", "Chromium", "Gecko", "VCL", "JAVA", ""])
def test_other_toolkit_toggle_checkbox_keeps_atspi(fake_atspi, monkeypatch, toolkit):
    driver, control, element, action_log, events = _toggle_click_target(monkeypatch, toolkit=toolkit)
    assert driver.press_element(element) is True
    assert action_log == ["Toggle"]
    assert "CHECKED" in control.states
    assert not getattr(control, "handler_ran", False)
    assert events == [("raise", control)]


@pytest.mark.parametrize("bounds", [
    None, Bounds(0, 40, 60, 0, 20), Bounds(0, 40, 60, 100, 0),
    Bounds(0, 40, 60, -1, -1), Bounds(0, -200, 60, 100, 20),
    Bounds(0, 1280, 60, 100, 20), Bounds(0, 40, 800, 100, 20),
])
@pytest.mark.parametrize("role", ["check box", "list item"])
def test_qt_toggle_without_usable_screen_bounds_falls_back(fake_atspi, monkeypatch, bounds, role):
    driver, control, element, action_log, events = _toggle_click_target(
        monkeypatch, bounds=bounds, role=role,
    )
    assert driver.press_element(element) is True
    assert action_log == ["Toggle"]
    assert events == [("raise", control)]


def test_qt_toggle_click_uses_the_visible_part_of_the_bounds(fake_atspi, monkeypatch):
    driver, control, element, action_log, events = _toggle_click_target(
        monkeypatch, bounds=Bounds(0, -90, 60, 100, 20),
    )
    assert driver.press_element(element) is True
    assert action_log == []
    assert events == [("raise", control), ("click", 5, 70, "left", 1)]


@pytest.mark.parametrize("action", ["Press", "Click", "activate", "select"])
@pytest.mark.parametrize("toggle_first", [False, True])
def test_qt_control_with_another_activating_action_keeps_atspi(
    fake_atspi, monkeypatch, action, toggle_first,
):
    actions = ("Toggle", "SetFocus", action) if toggle_first else (action, "Toggle", "SetFocus")
    driver, control, element, action_log, events = _toggle_click_target(
        monkeypatch, actions=actions,
    )
    assert driver.press_element(element) is True
    assert action_log == [actions[0]]
    assert events == [("raise", control)]


def test_qt_toggle_on_wayland_keeps_atspi(fake_atspi, monkeypatch):
    driver, control, element, action_log, events = _toggle_click_target(monkeypatch)
    monkeypatch.setattr("a11y_computer_use.drivers.linux._on_wayland", lambda: True)
    assert driver.press_element(element) is True
    assert action_log == ["Toggle"]
    assert events == [("raise", control)]


def test_qt_toggle_pointer_failure_does_not_run_toggle(fake_atspi, monkeypatch):
    driver, control, element, action_log, events = _toggle_click_target(monkeypatch)

    def failed_click(*_args, **_kwargs):
        raise ComputerUseError(ErrorCode.UNSUPPORTED, "pointer input failed")

    monkeypatch.setattr(_linux_input, "click", failed_click)
    with pytest.raises(ComputerUseError, match="pointer input failed"):
        driver.press_element(element)
    assert action_log == []
    assert events == [("raise", control)]


@pytest.mark.parametrize("checked", [False, True])
@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("changes", [False, True])
def test_gtk_toggle_cell_click_verifies_checked_not_selection(
    fake_atspi, monkeypatch, checked, selected, changes,
):
    driver, cell, element, action_log, events = _toggle_click_target(
        monkeypatch, toolkit="gtk", role="table cell",
        actions=("toggle", "expand or contract", "edit", "activate"),
    )
    if checked:
        cell.states.add("CHECKED")
    if selected:
        cell.states.add("SELECTED")
    monkeypatch.setattr(_atspi.time, "sleep", lambda _: None)

    def click(x, y, *, button="left", count=1):
        events.append(("click", x, y, button, count))
        cell.states.add("SELECTED")
        if changes:
            cell.states.symmetric_difference_update({"CHECKED"})

    monkeypatch.setattr(_linux_input, "click", click)
    if changes:
        assert driver.press_element(element) is True
        assert ("CHECKED" in cell.states) != checked
    else:
        with pytest.raises(ComputerUseError, match="checked state") as exc:
            driver.press_element(element)
        assert exc.value.detail["reason"] == "checked_unchanged"
    assert action_log == []
    assert [event for event in events if event[0] == "click"] == [
        ("click", 90, 70, "left", 1),
    ]


@pytest.mark.parametrize("bounds", [
    None, Bounds(0, 40, 60, 0, 20), Bounds(0, -200, 60, 100, 20),
])
def test_gtk_toggle_cell_without_bounds_refuses_without_toggle(fake_atspi, monkeypatch, bounds):
    driver, cell, element, action_log, events = _toggle_click_target(
        monkeypatch, toolkit="gtk", role="table cell", actions=("toggle",), bounds=bounds,
    )
    with pytest.raises(ComputerUseError):
        driver.press_element(element)
    assert action_log == []
    assert not any(event[0] == "click" for event in events)


def test_gtk_toggle_cell_wayland_refuses_without_toggle(fake_atspi, monkeypatch):
    driver, cell, element, action_log, events = _toggle_click_target(
        monkeypatch, toolkit="gtk", role="table cell", actions=("toggle",),
    )
    monkeypatch.setattr("a11y_computer_use.drivers.linux._on_wayland", lambda: True)
    with pytest.raises(ComputerUseError):
        driver.press_element(element)
    assert action_log == []
    assert not any(event[0] == "click" for event in events)


def _scrolled_text():
    """text inside viewport inside a scroll pane that owns both bars."""
    text = _Acc("text")
    viewport = _Acc("viewport")
    vertical = _Acc(
        "scroll bar", states=("VERTICAL",), value=100, minimum=0, maximum=2000,
        width=14, height=400,
    )
    horizontal = _Acc(
        "scroll bar", states=("HORIZONTAL",), value=40, minimum=0, maximum=2000,
        width=400, height=14,
    )
    _adopt(viewport, text)
    _adopt(_Acc("scroll pane"), viewport, vertical, horizontal)
    return text, vertical, horizontal


def _body(ref="e4"):
    return Element(ref, "AXTextArea", "Body", "hello", Bounds(0, 10, 20, 200, 120), "snap-1")


def test_pixel_scroll_sets_each_bar_by_the_requested_delta(fake_atspi) -> None:
    text, vertical, horizontal = _scrolled_text()
    assert _atspi.scroll_by_pixels(text, dx=5, dy=3) is True
    assert vertical.value == 103  # positive dy: content up, bar value increases
    assert horizontal.value == 45


def test_pixel_scroll_rejects_a_notch_sized_jump_and_restores(fake_atspi) -> None:
    text, vertical, _horizontal = _scrolled_text()
    vertical.jump = 175  # the 0.4.5 wheel-notch step, not +3
    assert _atspi.scroll_by_pixels(text, dy=3) is False
    assert vertical.value == 100


def test_pixel_scroll_keeps_a_clamp_at_the_toolkit_limit(fake_atspi) -> None:
    text, vertical, _horizontal = _scrolled_text()
    vertical.value = 100
    vertical.visual_max = 110  # GTK reports maximum=upper, then clamps to upper-page
    assert _atspi.scroll_by_pixels(text, dy=50) is True
    assert vertical.value == 110


def test_pixel_scroll_skips_a_zero_range_bar(fake_atspi) -> None:
    text = _Acc("text")
    dead = _Acc(
        "scroll bar", states=("VERTICAL",), value=0, minimum=0, maximum=0,
        width=10, height=80,
    )
    real = _Acc(
        "scroll bar", states=("VERTICAL",), value=20, minimum=0, maximum=500,
        width=14, height=300,
    )
    _adopt(_Acc("scroll pane"), text, dead, real)
    assert _atspi.scroll_by_pixels(text, dy=4) is True
    assert dead.value == 0 and real.value == 24


def test_pixel_scroll_uses_extents_when_the_bar_has_no_orientation_state(fake_atspi) -> None:
    text = _Acc("text")
    bar = _Acc("scroll bar", value=5, minimum=0, maximum=100, width=12, height=200)
    _adopt(_Acc("scroll pane"), text, bar)
    assert _atspi.scroll_by_pixels(text, dy=-2) is True
    assert bar.value == 3


def test_pixel_scroll_does_not_treat_a_slider_as_a_scroll_bar(fake_atspi) -> None:
    text = _Acc("text")
    slider = _Acc("slider", states=("VERTICAL",), value=1, minimum=0, maximum=10, width=20, height=100)
    _adopt(_Acc("pane"), text, slider)
    assert _atspi.scroll_by_pixels(text, dy=1) is False
    assert slider.value == 1


def test_pixel_scroll_rolls_back_the_axis_that_already_landed(fake_atspi) -> None:
    text, vertical, horizontal = _scrolled_text()
    horizontal.jump = 80
    assert _atspi.scroll_by_pixels(text, dx=5, dy=3) is False
    assert vertical.value == 100 and horizontal.value == 40


def test_scroll_at_point_hit_tests_then_sets_the_bar(fake_atspi) -> None:
    text, vertical, _horizontal = _scrolled_text()
    desktop = _Acc("desktop frame")
    desktop.component = _Hit(text)
    fake_atspi.desktop = desktop
    assert _atspi.scroll_at_point(15, 25, dy=3) is True
    assert vertical.value == 103


def test_driver_coordinate_pixel_scroll_hit_tests_without_a_ref(fake_atspi, xtest_recorder) -> None:
    events, _display = xtest_recorder
    text, vertical, _horizontal = _scrolled_text()
    desktop = _Acc("desktop frame")
    desktop.component = _Hit(text)
    fake_atspi.desktop = desktop
    LinuxDriver().scroll(Point(0, 15, 25), dy=3, unit=ScrollUnit.PIXELS)
    assert events == []
    assert vertical.value == 103


def test_cdp_scroll_uses_the_topmost_window_not_the_desktop(monkeypatch) -> None:
    """A fullscreen desktop contains every point. The port is read from the top window."""
    from a11y_computer_use.drivers.linux import _cdp_scroll_pixels

    rows = [
        {"pid": 1, "title": "Desktop", "bounds": {"x": 0, "y": 0, "width": 1920, "height": 1200}},
        {"pid": 42, "title": "cualong - Google Chrome", "bounds": {"x": 49, "y": 49, "width": 1000, "height": 700}},
    ]
    monkeypatch.setattr("a11y_computer_use.drivers._linux_system.windows", lambda: rows)
    seen: dict[str, int] = {}

    def port(pid: int):
        seen["pid"] = pid
        return None

    monkeypatch.setattr("a11y_computer_use.drivers.linux._debug_port_for_pid", port)
    assert _cdp_scroll_pixels(100, 150, dx=0, dy=40) is False
    assert seen["pid"] == 42


def test_cdp_scroll_picks_the_tab_the_window_is_showing(monkeypatch) -> None:
    """A shared profile lists other tabs first. The window title names the page."""
    from a11y_computer_use.drivers.linux import _cdp_scroll_pixels

    monkeypatch.setattr(
        "a11y_computer_use.drivers._linux_system.windows",
        lambda: [{
            "pid": 7,
            "title": "cualong - Google Chrome",
            "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
        }],
    )
    monkeypatch.setattr("a11y_computer_use.drivers.linux._debug_port_for_pid", lambda _pid: 9222)
    pages = [
        {"title": "Other", "webSocketDebuggerUrl": "ws://other"},
        {"title": "cualong", "webSocketDebuggerUrl": "ws://long"},
    ]
    monkeypatch.setattr("a11y_computer_use.drivers._cdp.page_targets", lambda _endpoint: pages)
    opened: list[str] = []

    class _Transport:
        def close(self) -> None:
            return None

    class _Session:
        def __init__(self, _transport, default_timeout: float = 3.0) -> None:
            del default_timeout

        def call(self, _method, _params):
            return {"result": {"value": {"before": 0, "after": 80}}}

    monkeypatch.setattr(
        "a11y_computer_use.drivers._cdp.connect",
        lambda ws, timeout=3.0: opened.append(ws) or _Transport(),
    )
    monkeypatch.setattr("a11y_computer_use.drivers._cdp.CDPSession", _Session)
    assert _cdp_scroll_pixels(10, 20, dx=0, dy=80) is True
    assert opened == ["ws://long"]


def test_lines_scroll_uses_cdp_when_the_document_is_not_a_list(xtest_recorder, monkeypatch) -> None:
    """A Chrome document ignores wheel notches. A DevTools scroll is the step."""
    events, _display = xtest_recorder
    seen: dict[str, tuple] = {}

    def cdp(x, y, *, dx, dy):
        seen["at"] = (x, y, dx, dy)
        return True

    monkeypatch.setattr("a11y_computer_use.drivers.linux._cdp_scroll_pixels", cdp)
    LinuxDriver().scroll(Point(0, 30, 40), dy=5, unit=ScrollUnit.LINES)
    assert events == []
    assert seen["at"] == (30, 40, 0, 200)


def test_chromium_document_scroll_waits_until_shown_names_change(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """The wheel returns before Chrome's tree does. The next snapshot must see new rows."""
    events, _display = xtest_recorder
    monkeypatch.setattr(
        "a11y_computer_use.drivers.linux._cdp_scroll_pixels", lambda *_args, **_kwargs: False,
    )
    class _Handle:
        __gpointer__ = 1

    monkeypatch.setattr(observe, "ax_handle_for", lambda _snapshot_id, _ref: _Handle())
    monkeypatch.setattr(_atspi, "list_container", lambda _handle: None)
    monkeypatch.setattr(_atspi, "list_with_overflow_ancestor", lambda _handle: None)
    monkeypatch.setattr(_atspi, "_chromium_app", lambda _handle: True)
    seen = {"n": 0}

    def names(_handle):
        seen["n"] += 1
        if seen["n"] == 1:
            return ("cualong", "TICKET-0000")
        return ("cualong", "TICKET-0016")

    monkeypatch.setattr("a11y_computer_use.drivers.linux._showing_names", names)
    LinuxDriver().scroll(_body(), dy=5, unit=ScrollUnit.LINES)
    assert seen["n"] >= 2
    assert any(event[0] == _X_BPRESS and event[1] == 5 for event in events)


def test_chromium_document_scroll_tops_up_a_short_head_move(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """A five-line burst that only moves the head by one name sends more notches.

    Synthetic names, not a live Chrome. The requested notches go out
    together. When that burst settles on a one-row move, more notches
    follow until the head has moved by about the request. A burst that
    clears the sample is the viewport step and is not topped up.
    """
    events, _display = xtest_recorder
    monkeypatch.setattr(
        "a11y_computer_use.drivers.linux._cdp_scroll_pixels", lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr("a11y_computer_use.drivers.linux.time.sleep", lambda _seconds: None)

    class _Handle:
        __gpointer__ = 1

    monkeypatch.setattr(observe, "ax_handle_for", lambda _snapshot_id, _ref: _Handle())
    monkeypatch.setattr(_atspi, "list_container", lambda _handle: None)
    monkeypatch.setattr(_atspi, "list_with_overflow_ancestor", lambda _handle: None)
    monkeypatch.setattr(_atspi, "_chromium_app", lambda _handle: True)
    script = [tuple(f"ROW-{i:04d}" for i in range(1, 9))]
    # The burst settles on a one-row move. The wait holds that reading
    # across the polls that cover the burst's landing time, then tops up.
    script.extend((tuple(f"ROW-{i:04d}" for i in range(2, 10)),) * 8)
    for head in range(3, 7):
        script.append(tuple(f"ROW-{i:04d}" for i in range(head, head + 8)))
    index = {"i": 0}

    def names(_handle, limit=6):
        item = script[min(index["i"], len(script) - 1)]
        index["i"] += 1
        return item

    monkeypatch.setattr("a11y_computer_use.drivers.linux._showing_names", names)
    LinuxDriver().scroll(_body(), dy=5, unit=ScrollUnit.LINES)
    presses = [event for event in events if event[0] == _X_BPRESS and event[1] == 5]
    # Four in the burst (the fifth notch is the gap past the viewport),
    # then one notch per missing row until the head has moved by that many.
    assert len(presses) == 7, len(presses)


def test_chromium_document_scroll_keeps_a_viewport_burst(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """A viewport burst that clears the on-screen names is pulled back, not extended.

    Synthetic names, not a live Chrome. The burst is four notches down.
    The fifth forward notch is not sent. When that burst leaves the old
    sample, reverse notches follow until one old row is back, or the
    burst count is spent. This mock stays parted, so all four reverse
    notches are sent. Stopping at the first forward notch, and waiting
    for the tree after it, is the four-row two-second regression.
    """
    events, _display = xtest_recorder
    sleeps = {"n": 0}
    monkeypatch.setattr(
        "a11y_computer_use.drivers.linux._cdp_scroll_pixels", lambda *_args, **_kwargs: False,
    )

    def sleep(_seconds: float) -> None:
        sleeps["n"] += 1

    monkeypatch.setattr("a11y_computer_use.drivers.linux.time.sleep", sleep)

    class _Handle:
        __gpointer__ = 1

    monkeypatch.setattr(observe, "ax_handle_for", lambda _snapshot_id, _ref: _Handle())
    monkeypatch.setattr(_atspi, "list_container", lambda _handle: None)
    monkeypatch.setattr(_atspi, "list_with_overflow_ancestor", lambda _handle: None)
    monkeypatch.setattr(_atspi, "_chromium_app", lambda _handle: True)
    reads = {"n": 0}

    def names(_handle, limit=6):
        reads["n"] += 1
        if reads["n"] == 1:
            return tuple(f"ITEM-{i:03d}" for i in range(1, 7))
        return tuple(f"ITEM-{i:03d}" for i in range(21, 27))

    monkeypatch.setattr("a11y_computer_use.drivers.linux._showing_names", names)
    LinuxDriver().scroll(_body(), dy=5, unit=ScrollUnit.LINES)
    down = [event for event in events if event[0] == _X_BPRESS and event[1] == 5]
    up = [event for event in events if event[0] == _X_BPRESS and event[1] == 4]
    assert len(down) == 4, len(down)
    assert len(up) == 4, len(up)
    # One read of the old sample, one of the parted sample, then one per reverse notch.
    assert reads["n"] == 6
    assert sleeps["n"] == 0


def test_document_scroll_marker_is_the_rows_not_the_window_buttons(fake_atspi) -> None:
    """A titled list under the document is not the scroll marker.

    The list's name and the frame's Minimize/Maximize/Close names do not
    move when the page scrolls. Waiting on them runs out the 0.9s clock
    on every step. The marker is the on-screen row text. A row parked
    above the viewport is not part of it. The climb stops at the frame.
    """
    from a11y_computer_use.drivers.linux import _name_source, _showing_names

    frame = _Acc("frame", name="scroll340", width=1000, height=700)
    buttons = [
        _Acc("push button", name=label, width=20, height=20)
        for label in ("Minimize", "Maximize", "Close")
    ]
    parked = _Acc("list item", name="ITEM-000 scroll-row", width=400, height=20)
    parked.component._rect.y = -40
    rows = [
        _Acc("list item", name=f"ITEM-{i:03d} scroll-row", width=400, height=28)
        for i in range(1, 9)
    ]
    listing = _Acc("list", name="Rows", width=400, height=800)
    _adopt(listing, parked, *rows)
    infobar = _Acc("push button", name="Close this view", width=80, height=20)
    document = _Acc("document web", name="scroll340", width=900, height=600)
    _adopt(document, infobar, listing)
    _adopt(frame, *buttons, document)
    cell = rows[0]

    assert _showing_names(document) == tuple(f"ITEM-{i:03d} scroll-row" for i in range(1, 7))
    source, names = _name_source(lambda fn: fn(), cell)
    assert source is listing
    assert names[0] == "ITEM-001 scroll-row"
    assert "Minimize" not in names
    assert "Rows" not in names
    assert "Close this view" not in _showing_names(document)
    frame_source, frame_names = _name_source(lambda fn: fn(), document)
    assert frame_source is document
    assert "Minimize" not in frame_names


def test_document_scroll_marker_is_one_name_per_table_row(fake_atspi) -> None:
    """A table row contributes its first cell, not every cell.

    Six names were two rows (the id, the value, and the button). A two-row
    twitch then parted the whole sample, the burst returned, and the next
    burst jumped over the row ``scroll_to_find`` was hunting.
    """
    from a11y_computer_use.drivers.linux import _showing_names

    table = _Acc("table", name="", width=400, height=800)
    built = []
    for i in range(1, 9):
        row = _Acc("table row", name="", width=400, height=22)
        cells = [
            _Acc("table cell", name=label, width=80, height=22)
            for label in (f"ROW-{i:04d}", f"val {i}", f"edit {i}")
        ]
        _adopt(row, *cells)
        built.append(row)
    _adopt(table, *built)
    assert _showing_names(table) == tuple(f"ROW-{i:04d}" for i in range(1, 7))


def test_lines_scroll_is_one_wheel_notch_per_unit(xtest_recorder, monkeypatch) -> None:
    events, _display = xtest_recorder

    def grab(_box):
        raise AssertionError("a coordinate line scroll has no list to capture")

    monkeypatch.setattr("a11y_computer_use.drivers.linux._grab_region", grab)
    monkeypatch.setattr(
        "a11y_computer_use.drivers.linux._cdp_scroll_pixels", lambda *_args, **_kwargs: False,
    )
    LinuxDriver().scroll(Point(0, 30, 40), dx=-1, dy=2, unit=ScrollUnit.LINES)
    assert events[0] == (_X_MOTION, 0, 30, 40)
    assert events[1:5] == [(_X_BPRESS, 5, 0, 0), (_X_BRELEASE, 5, 0, 0)] * 2
    assert events[5:] == [(_X_BPRESS, 6, 0, 0), (_X_BRELEASE, 6, 0, 0)]


def test_driver_pixel_scroll_does_not_send_wheel_notches(fake_atspi, xtest_recorder, monkeypatch) -> None:
    events, _display = xtest_recorder
    text, vertical, horizontal = _scrolled_text()
    monkeypatch.setattr(observe, "ax_handle_for", lambda _snapshot_id, _ref: text)
    LinuxDriver().scroll(_body(), dx=5, dy=3, unit="pixels")
    assert events == []
    assert vertical.value == 103 and horizontal.value == 45


def test_driver_pixel_scroll_without_a_bar_is_unsupported(fake_atspi, xtest_recorder, monkeypatch) -> None:
    events, _display = xtest_recorder
    monkeypatch.setattr(observe, "ax_handle_for", lambda _snapshot_id, _ref: _Acc("text"))
    monkeypatch.setattr(
        "a11y_computer_use.drivers.linux._cdp_scroll_pixels", lambda *_args, **_kwargs: False,
    )

    def no_grab(_box):
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED, "no grab", detail={"reason": "page_unseen"},
        )

    monkeypatch.setattr("a11y_computer_use.drivers.linux._grab_region", no_grab)
    with pytest.raises(ComputerUseError) as error:
        LinuxDriver().scroll(_body(), dy=3, unit=ScrollUnit.PIXELS)
    assert error.value.code is ErrorCode.UNSUPPORTED
    assert "wheel notches were not sent" in error.value.message
    assert error.value.detail["unit"] == "pixels"
    assert events == []


def test_driver_pixel_scroll_without_a_bar_wheels_when_pixels_move(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """No AT-SPI bar. A wheel at the element box is kept when the grab changes."""
    events, _display = xtest_recorder
    monkeypatch.setattr(observe, "ax_handle_for", lambda _snapshot_id, _ref: _Acc("text"))
    monkeypatch.setattr(
        "a11y_computer_use.drivers.linux._cdp_scroll_pixels", lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr("a11y_computer_use.drivers.linux._grab_region", lambda _box: object())
    monkeypatch.setattr(
        "a11y_computer_use.drivers.linux._list_pixels_moved", lambda _before, _box: (12.0, 1),
    )
    LinuxDriver().scroll(_body(), dy=160, unit=ScrollUnit.PIXELS)
    presses = [event for event in events if event[0] == _X_BPRESS and event[1] == 5]
    assert len(presses) == 2  # 160px -> two notches at 80px each


def test_negative_child_count_does_not_revive_a_fake() -> None:
    """-1 is a D-Bus failure. A test double has no GObject to reconnect."""

    class _Acc:
        def get_child_count(self):
            return -1

    assert _atspi._revive_application(_Acc()) is False
    assert _atspi._child_count(_Acc()) == -1


def test_table_body_cells_are_added_when_children_are_only_headers() -> None:
    """A file chooser's table exposes column headers. Body cells come from the Table iface."""

    class _Node:
        def __init__(self, role, n_rows=0, cells=None):
            self.role = role
            self.n_rows = n_rows
            self.cells = cells or {}

        def get_role_name(self):
            return self.role

        def get_n_rows(self):
            return self.n_rows

        def get_accessible_at(self, row, col):
            return self.cells.get((row, col))

    header = _Node("column header")
    cell = _Node("table cell")
    table = _Node("table", n_rows=1, cells={(0, 0): cell})
    assert _atspi._with_table_body(table, [header]) == [header, cell]
    assert _atspi._with_table_body(table, [cell]) == [cell]
    assert _atspi._with_table_body(table, []) == [cell]
    empty = _Node("table", n_rows=0)
    assert _atspi._with_table_body(empty, [header]) == [header]


def test_pixel_scroll_on_wayland_uses_the_bar_and_lines_stay_unsupported(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    events, _display = xtest_recorder
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    with pytest.raises(ComputerUseError) as error:
        LinuxDriver().scroll(Point(0, 8, 9), dy=1, unit=ScrollUnit.LINES)
    assert error.value.code is ErrorCode.UNSUPPORTED
    assert events == []

    text, vertical, _horizontal = _scrolled_text()
    monkeypatch.setattr(observe, "ax_handle_for", lambda _snapshot_id, _ref: text)
    LinuxDriver().scroll(_body(), dy=3, unit=ScrollUnit.PIXELS)
    assert vertical.value == 103
    assert events == []


class _ReplacingField:
    """GTK EditableText: set_text_contents replaces the whole buffer."""

    def __init__(self, text=""):
        self.text = text
        self.deletes = 0

    def get_editable_text_iface(self):
        return self

    def set_text_contents(self, text):
        self.text = text
        return True

    def delete_text(self, start, end):
        self.deletes += 1
        self.text = self.text[:start] + self.text[end:]
        return True

    def insert_text(self, pos, text, length):
        self.text = self.text[:pos] + text[:length] + self.text[pos:]
        return True


class _AppendingField(_ReplacingField):
    """Chromium web field on a fake transport.

    ``set_text_contents`` appends to the DOM and still returns true.
    ``Text.get_text(0, character_count)`` echoes that request (the read 0.4.9
    treated as success). ``Text.get_text(0, -1)`` is the DOM, which is what a
    snapshot reads. ``delete_text`` returns true and does nothing unless the
    whole DOM range was selected first — the adaptor's true is not a clear.
    """

    def __init__(self, text=""):
        super().__init__(text)
        self.echo: str | None = None
        self.delete_ranges: list[tuple[int, int]] = []

    def set_text_contents(self, text):
        self.text += text
        self.echo = text
        return True

    def delete_text(self, start, end):
        self.deletes += 1
        self.delete_ranges.append((int(start), int(end)))
        selected = getattr(self, "selection", None)
        if selected == (0, len(self.text)) and int(start) == 0 and int(end) >= len(self.text):
            self.text = ""
            self.echo = None
            self.selection = None
        return True


class _KeyClearedWebField(_AppendingField):
    """``delete_text`` never changes the DOM. ctrl+a and BackSpace do.

    The test applies those chords to the DOM. That effect is the fake, not a
    live Chrome key delivery.
    """

    def delete_text(self, start, end):
        self.deletes += 1
        self.delete_ranges.append((int(start), int(end)))
        return True

    def get_component_iface(self):
        return self

    def grab_focus(self):
        self.focused = True
        return True


def test_set_text_replaces_a_web_field_that_appends(fake_atspi) -> None:
    field = _AppendingField()
    assert _atspi.set_text(field, "ALPHA") is True
    assert field.text == "ALPHA"
    assert _atspi.set_text(field, "BETA") is True
    assert field.text == "BETA"
    assert field.deletes == 1


def test_set_text_does_not_clear_when_contents_already_replace(fake_atspi) -> None:
    field = _ReplacingField("old")
    assert _atspi.set_text(field, "ALPHA") is True
    assert _atspi.set_text(field, "BETA") is True
    assert field.text == "BETA"
    assert field.deletes == 0


def test_set_text_trusts_a_replace_it_cannot_read_back(fake_atspi) -> None:
    field = _ReplacingField()
    field.text_error = True
    assert _atspi.set_text(field, "BETA") is True
    assert field.text == "BETA"
    assert field.deletes == 0


def test_set_text_does_not_trust_a_bounded_read_that_echoes_the_request(fake_atspi, monkeypatch) -> None:
    """Fake transport only. The bounded read equals the request; the snapshot read does not."""
    field = _AppendingField("bench-value-0")
    field.set_text_contents("ALPHA")
    count = fake_atspi.Text.get_character_count(field)
    assert fake_atspi.Text.get_text(field, 0, count) == "ALPHA"
    assert fake_atspi.Text.get_text(field, 0, -1).startswith("bench-value-0")
    field.text = "bench-value-0"
    field.echo = None
    field.deletes = 0

    def keys_are_not_required(_chord: str) -> None:
        raise AssertionError("selecting the snapshot range already clears this fake field")

    monkeypatch.setattr(_linux_input, "press_chord", keys_are_not_required)
    assert _atspi.set_text(field, "ALPHA") is True
    assert field.text == "ALPHA"
    assert _atspi.set_text(field, "BETA") is True
    assert field.text == "BETA"
    assert field.delete_ranges[0] == (0, len("bench-value-0ALPHA"))


def test_set_text_uses_x11_keys_when_delete_leaves_the_snapshot_text(fake_atspi, monkeypatch) -> None:
    """Fake transport only: the chords clear the DOM because the test applies them."""
    field = _KeyClearedWebField("bench-value-0")
    sent: list[str] = []

    def press_chord(chord: str) -> None:
        sent.append(chord)
        if chord == "ctrl+a":
            field.selected_all = True
        elif chord == "backspace" and getattr(field, "selected_all", False):
            field.text = ""
            field.echo = None
            field.selected_all = False

    monkeypatch.setattr(_linux_input, "press_chord", press_chord)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    assert _atspi.set_text(field, "BETA") is True
    assert field.text == "BETA"
    assert field.focused is True
    assert sent == ["ctrl+a", "backspace"]


def test_set_text_without_editable_text_clears_then_types(fake_atspi, monkeypatch) -> None:
    """Fake transport only. No EditableText: the test applies the chords and the typing."""
    field = _KeyClearedWebField("bench-value-0")
    field.get_editable_text_iface = None
    sent: list[str] = []
    typed: list[str] = []

    def press_chord(chord: str) -> None:
        sent.append(chord)
        if chord == "ctrl+a":
            field.selected_all = True
        elif chord == "backspace" and getattr(field, "selected_all", False):
            field.text = ""
            field.echo = None
            field.selected_all = False

    def type_string(text: str) -> None:
        typed.append(text)
        field.text += text
        field.echo = None

    monkeypatch.setattr(_linux_input, "press_chord", press_chord)
    monkeypatch.setattr(_linux_input, "type_string", type_string)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    assert _atspi.set_text(field, "BETA") is True
    assert field.text == "BETA"
    assert sent == ["ctrl+a", "backspace"]
    assert typed == ["BETA"]


def test_set_text_without_editable_text_on_wayland_does_not_type(fake_atspi, monkeypatch) -> None:
    field = _KeyClearedWebField("bench-value-0")
    field.get_editable_text_iface = None
    typed: list[str] = []
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(_linux_input, "type_string", typed.append)
    monkeypatch.setattr(_linux_input, "press_chord", lambda chord: typed.append(chord))
    assert _atspi.set_text(field, "BETA") is False
    assert field.text == "bench-value-0"
    assert typed == []


class _NoopWebField(_KeyClearedWebField):
    """EditableText returns true and does not change the DOM. Key events do.

    This is the Firefox web entry on a fake transport, not a live browser.
    """

    def __init__(self, text=""):
        super().__init__(text)
        self.writes: list[str] = []

    def set_text_contents(self, text):
        self.writes.append(text)
        return True

    def insert_text(self, pos, text, length):
        return True

    def delete_text(self, start, end):
        self.deletes += 1
        return True


def _mark_toolkit(field, toolkit: str, name: str) -> None:
    app = _GeckoNode("application", name)
    app.toolkit = toolkit
    field.get_application = lambda: app
    field.application = app


def test_set_text_focuses_and_types_when_editable_text_is_a_noop(fake_atspi, monkeypatch) -> None:
    """Fake Firefox field. The DOM changes only because the test applies the keys."""
    field = _NoopWebField("")
    _mark_toolkit(field, "Gecko", "Firefox")
    typed: list[str] = []

    def type_string(text: str) -> None:
        typed.append(text)
        field.text = text

    monkeypatch.setattr(_linux_input, "type_string", type_string)
    monkeypatch.setattr(_linux_input, "press_chord", lambda chord: None)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    assert _atspi.set_text(field, "Ann Lee") is True
    assert field.text == "Ann Lee"
    assert field.focused is True
    assert typed == ["Ann Lee"]
    assert field.writes  # EditableText was tried before the keys


def test_chromium_clear_does_not_focus_when_editable_text_puts_zero_back(fake_atspi, monkeypatch) -> None:
    """Fake Chromium number. Focusing it would make the empty read-back 0.

    EditableText reports success and then the snapshot read is 0, which is
    not a clear. The Firefox focus fallback must not run. This is not a browser.
    """
    field = _NoopWebField("3")
    _mark_toolkit(field, "Chromium", "Google Chrome")

    def set_text_contents(text):
        field.writes.append(text)
        if field.text == "":
            field.text = "0"
        return True

    def delete_text(start, end):
        field.deletes += 1
        field.text = ""
        return True

    field.set_text_contents = set_text_contents
    field.delete_text = delete_text

    def grab_focus():
        field.focused = True
        field.text = "0"
        return True

    field.grab_focus = grab_focus
    monkeypatch.setattr(_linux_input, "type_string", lambda text: None)
    monkeypatch.setattr(_linux_input, "press_chord", lambda chord: None)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    assert _atspi.set_text(field, "") is False
    assert getattr(field, "focused", False) is not True


class _GeckoNode:
    """A Firefox-shaped accessible. States are a name set, so no gi import."""

    def __init__(self, role, name="", states=(), children=()):
        self.role = role
        self.name = name
        self.states = set(states)
        self.children = list(children)
        self.parent = None
        self.application = None
        self.actions: list[str] = []
        self.pressed = False
        for child in self.children:
            child.parent = self

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_description(self):
        return getattr(self, "description", "")

    def get_attributes(self):
        return getattr(self, "attributes", {})

    def get_toolkit_name(self):
        return getattr(self, "toolkit", "")

    def get_state_set(self):
        return _NS(names=set(self.states), contains=lambda member: str(member) in self.states)

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def get_parent(self):
        return self.parent

    def get_application(self):
        return self.application

    def get_action_iface(self):
        return self if self.actions else None

    def get_n_actions(self):
        return len(self.actions)

    def get_action_name(self, index):
        return self.actions[index]

    def do_action(self, _index):
        self.pressed = True
        return True


def _firefox_documents():
    """Active form, a background tab, and a preloaded New Tab, with on-screen bounds.

    The background documents are VISIBLE and not SHOWING. The selected tab is
    Form Probe. This is the shape from a live Firefox ESR tree, as fakes.
    """
    app = _GeckoNode("application", "Firefox")
    app.toolkit = "Gecko"
    tab = _GeckoNode("page tab", "Form Probe", ("SHOWING", "VISIBLE", "SELECTED"))
    other_tab = _GeckoNode("page tab", "Firefox Privacy Notice", ("SHOWING", "VISIBLE"))
    tabs = _GeckoNode("page tab list", "", ("SHOWING", "VISIBLE"), [tab, other_tab])
    name = _GeckoNode("entry", "Name", ("SHOWING", "VISIBLE", "FOCUSABLE"))
    form = _GeckoNode("document web", "Form Probe", ("SHOWING", "VISIBLE", "FOCUSABLE"), [name])
    form_frame = _GeckoNode("internal frame", "", ("SHOWING", "VISIBLE"), [form])
    form_pane = _GeckoNode("scroll pane", "", ("SHOWING", "VISIBLE"), [form_frame])
    products = _GeckoNode("link", "Products", ("VISIBLE", "FOCUSABLE"), )
    products.actions = ["jump"]
    background = _GeckoNode(
        "document web", "Firefox Privacy Notice", ("VISIBLE", "FOCUSABLE"), [products],
    )
    background_frame = _GeckoNode("internal frame", "", ("VISIBLE",), [background])
    background_pane = _GeckoNode("scroll pane", "", ("VISIBLE",), [background_frame])
    wikipedia = _GeckoNode("link", "Wikipedia", ("VISIBLE", "FOCUSABLE"))
    wikipedia.actions = ["jump"]
    new_tab = _GeckoNode("document web", "New Tab", ("VISIBLE", "FOCUSABLE"), [wikipedia])
    new_frame = _GeckoNode("internal frame", "", ("VISIBLE",), [new_tab])
    new_pane = _GeckoNode("scroll pane", "", ("VISIBLE",), [new_frame])
    panel = _GeckoNode("panel", "", ("SHOWING", "VISIBLE"), [form_pane, background_pane, new_pane])
    frame = _GeckoNode("frame", "Form Probe — Mozilla Firefox", ("SHOWING", "VISIBLE"), [tabs, panel])
    for node in (
        tab, other_tab, tabs, name, form, form_frame, form_pane, products, background,
        background_frame, background_pane, wikipedia, new_tab, new_frame, new_pane, panel, frame,
    ):
        node.application = app
    return {
        "panel": panel,
        "form": form,
        "name": name,
        "products": products,
        "wikipedia": wikipedia,
        "new_tab": new_tab,
        "new_frame": new_frame,
        "new_pane": new_pane,
        "background": background,
        "frame": frame,
    }


def test_calc_document_url_does_not_search_the_sheet(monkeypatch) -> None:
    """LibreOffice has no page URL. Collection on the sheet wedges the bus."""
    from a11y_computer_use.drivers import _atspi
    from a11y_computer_use.drivers.linux import LinuxDriver

    sheet = _GeckoNode("application", "soffice")

    def boom(_root):
        raise AssertionError("collection searched a Calc tree")

    monkeypatch.setattr(_atspi, "_collected_content_document", boom)
    assert _atspi.document_url_of(sheet) is None
    assert LinuxDriver().document_url("soffice.bin") is None
    assert LinuxDriver().document_url("LibreOffice Calc") is None


def test_document_url_follows_the_showing_tab_and_skips_browser_chrome(monkeypatch) -> None:
    """Synthetic trees. The first document is not the page, and an omnibox URL is not either."""
    from a11y_computer_use.drivers import _atspi

    tree = _firefox_documents()
    background = tree["products"].parent
    form = tree["form"]
    background.description = "http://localhost/para.html"
    form.description = "http://127.0.0.1/bg.html"
    panel = tree["panel"]
    # The hidden tab's scroll pane is listed before the showing one.
    panel.children = [panel.children[1], panel.children[0], panel.children[2]]
    for child in panel.children:
        child.parent = panel
    assert _atspi.document_url_of(tree["frame"]) == "http://127.0.0.1/bg.html"
    # Collection is what a live Firefox window answers. The child list can omit
    # the document that Collection still returns.
    empty = _GeckoNode("frame", "Bg Page — Mozilla Firefox", ("SHOWING", "VISIBLE"))
    monkeypatch.setattr(
        _atspi, "_collected_content_document",
        lambda root: "http://127.0.0.1/bg.html" if root is empty else None,
    )
    assert _atspi.document_url_of(empty) == "http://127.0.0.1/bg.html"
    monkeypatch.setattr(_atspi, "_collected_content_document", lambda _root: None)

    app = _GeckoNode("application", "Google Chrome")
    app.toolkit = "Chromium"
    popup_doc = _GeckoNode("document web", "omnibox", ("SHOWING", "VISIBLE"))
    popup_doc.description = "chrome://omnibox-popup.top-chrome/"
    popup_doc.application = app
    page = _GeckoNode("document web", "Bg Page", ("SHOWING", "VISIBLE"))
    page.description = "http://127.0.0.1/bg.html"
    page.application = app
    button = _GeckoNode("push button", "Same frame button", ("SHOWING", "VISIBLE"))
    inner = _GeckoNode("document web", "Frame Page", ("SHOWING", "VISIBLE"), [button])
    inner.description = "http://localhost/frame.html"
    inner.application = app
    iframe = _GeckoNode("internal frame", "", ("SHOWING", "VISIBLE"), [inner])
    iframe.application = app
    top = _GeckoNode("document web", "Ifr Page", ("SHOWING", "VISIBLE"), [iframe])
    top.description = "http://127.0.0.1/ifr.html"
    top.application = app
    entry = _GeckoNode("entry", "Address and search bar", ("SHOWING", "VISIBLE", "FOCUSED"))
    entry.text = "http://localhost/para.html"
    entry.application = app
    bar = _GeckoNode("tool bar", "", ("SHOWING", "VISIBLE"), [entry])
    popup = _GeckoNode("frame", "popup", ("SHOWING",), [popup_doc])
    main = _GeckoNode("frame", "Chrome", ("SHOWING",), [bar, top])
    for node in (popup, main, bar, entry, popup_doc):
        node.application = app
    root = _GeckoNode("application", "Google Chrome", (), [popup, main])
    root.toolkit = "Chromium"
    root.application = root
    assert _atspi.document_url_of(popup) is None
    assert _atspi.document_url_of(main) == "http://127.0.0.1/ifr.html"
    assert _atspi.document_url_for(button) == "http://localhost/frame.html"
    assert _atspi.document_url_for(entry) is None
    assert _atspi.in_browser_chrome(entry) is True
    assert _atspi.in_browser_chrome(button) is False
    assert _atspi.is_location_entry(entry) is True
    assert _atspi.address_bar_text_under(root) == "http://localhost/para.html"
    monkeypatch.setattr(_atspi, "_focused_node", lambda _app: (entry, False))
    monkeypatch.setattr(_atspi, "find_root", lambda *_args, **_kwargs: None)
    assert _atspi.address_bar_text("chrome") == "http://localhost/para.html"
    entry.text = "localhost:9/para.html"
    assert _atspi.address_bar_text("chrome") == "localhost:9/para.html"
    page_entry = _GeckoNode("entry", "Bravo", ("SHOWING", "VISIBLE"), )
    page_entry.text = "http://localhost/nope.html"
    page_entry.application = app
    page_entry.parent = top
    top.children.append(page_entry)
    assert _atspi.is_location_entry(page_entry) is False
    page = _GeckoNode("document web", "Bg Page", ("SHOWING", "VISIBLE", "FOCUSED"))
    monkeypatch.setattr(_atspi, "find_root", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(_atspi, "_focused_accessibles", lambda _root: [page, entry])
    assert _atspi.focused_location_entry("chrome") is entry
    assert _atspi.focus_in_browser_chrome("chrome") is True


def test_firefox_hidden_documents_are_pruned_and_not_clickable(monkeypatch) -> None:
    """Synthetic tree. Not a live Firefox. Hidden documents have on-screen bounds
    in the real tree; here the filter is what drops them."""
    tree = _firefox_documents()
    accessor = _atspi.ATSPIAccessor()
    shown = accessor.children(tree["panel"])
    assert shown == [tree["panel"].children[0]]
    assert accessor.children(tree["panel"].children[0].children[0]) == [tree["form"]]
    assert _atspi.hidden_web_target(tree["products"]) is True
    assert _atspi.hidden_web_target(tree["wikipedia"]) is True
    assert _atspi.hidden_web_target(tree["name"]) is False
    # A showing document that is not the selected tab is not the page on screen.
    tree["new_tab"].states.add("SHOWING")
    tree["new_frame"].states.add("SHOWING")
    tree["new_pane"].states.add("SHOWING")
    assert accessor.children(tree["new_frame"]) == []

    button = _GeckoNode("push button", "Same frame button", ("SHOWING", "VISIBLE"))
    inner = _GeckoNode("document web", "Frame Page", ("VISIBLE",), [button])
    inner.description = "http://localhost/frame.html"
    iframe = _GeckoNode("internal frame", "Same frame", ("VISIBLE",), [inner])
    top = _GeckoNode("document web", "Ifr Page", ("SHOWING", "VISIBLE"), [iframe])
    top.description = "http://127.0.0.1/ifr.html"
    for node in (button, inner, iframe, top):
        node.application = tree["form"].application
    assert _atspi.hidden_web_target(inner) is False
    assert _atspi.hidden_web_target(button) is False
    assert _atspi._hidden_gecko_browser(iframe) is False
    assert accessor.children(top) == [iframe]
    assert accessor.children(inner) == [button]

    from a11y_computer_use.schema import Bounds, Element

    element = Element(
        "e49", "AXLink", "Products", None, Bounds(0, 8, 234, 67, 23), "snap-hidden",
        clickable=True, enabled=True,
    )
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: tree["products"])
    with pytest.raises(ComputerUseError) as exc:
        driver.press_element(element)
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "not_showing"
    assert tree["products"].pressed is False
    with pytest.raises(ComputerUseError) as exc:
        driver.click(element)
    assert exc.value.detail["reason"] == "not_showing"
    assert tree["products"].pressed is False


def _attach_gecko(owner, *nodes):
    for node in nodes:
        node.application = owner.application


def test_titled_iframe_document_stays_when_its_tab_is_selected() -> None:
    """Synthetic tree. Not a live Firefox.

    A child frame whose page has its own title used to be dropped: the
    active-tab title match ran on that document and it was not the tab.
    An untitled child frame already stayed. A titled frame inside a
    background tab, including one that reports SHOWING, still goes.
    """
    tree = _firefox_documents()
    agree = _GeckoNode("check box", "Agree", ("SHOWING", "VISIBLE", "FOCUSABLE", "CHECKABLE"))
    titled = _GeckoNode("document web", "Titled Child", ("SHOWING", "VISIBLE"), [agree])
    titled_frame = _GeckoNode("internal frame", "Titled Child", ("SHOWING", "VISIBLE"), [titled])
    inner = _GeckoNode("push button", "InnerGo", ("SHOWING", "VISIBLE"))
    untitled = _GeckoNode("document web", "", ("SHOWING", "VISIBLE"), [inner])
    untitled_frame = _GeckoNode("internal frame", "", ("SHOWING", "VISIBLE"), [untitled])
    hidden_link = _GeckoNode("link", "HiddenInner", ("SHOWING", "VISIBLE", "FOCUSABLE"))
    hidden_doc = _GeckoNode("document web", "Secret Frame", ("SHOWING", "VISIBLE"), [hidden_link])
    hidden_frame = _GeckoNode("internal frame", "Secret Frame", ("SHOWING", "VISIBLE"), [hidden_doc])
    _attach_gecko(
        tree["form"], agree, titled, titled_frame, inner, untitled, untitled_frame,
        hidden_link, hidden_doc, hidden_frame,
    )
    tree["form"].children.extend([titled_frame, untitled_frame])
    titled_frame.parent = tree["form"]
    untitled_frame.parent = tree["form"]
    tree["background"].children.append(hidden_frame)
    hidden_frame.parent = tree["background"]

    accessor = _atspi.ATSPIAccessor()
    assert accessor.children(titled_frame) == [titled]
    assert accessor.children(titled) == [agree]
    assert accessor.children(untitled_frame) == [untitled]
    assert _atspi.hidden_web_target(agree) is False
    assert _atspi.hidden_web_target(inner) is False
    assert accessor.children(hidden_frame) == []
    assert _atspi.hidden_web_target(hidden_link) is True

    # The background document itself is SHOWING, and so is its titled iframe.
    # The tab is still not selected, so the iframe document is not the page.
    tree["background"].states.add("SHOWING")
    tree["new_tab"].states.add("SHOWING")
    nested_btn = _GeckoNode("push button", "NewTabInner", ("SHOWING", "VISIBLE"))
    nested_doc = _GeckoNode("document web", "New Tab Frame", ("SHOWING", "VISIBLE"), [nested_btn])
    nested_frame = _GeckoNode("internal frame", "New Tab Frame", ("SHOWING", "VISIBLE"), [nested_doc])
    _attach_gecko(tree["new_tab"], nested_btn, nested_doc, nested_frame)
    tree["new_tab"].children.append(nested_frame)
    nested_frame.parent = tree["new_tab"]
    assert accessor.children(nested_frame) == []
    assert _atspi.hidden_web_target(nested_btn) is True
    assert accessor.children(titled_frame) == [titled]


def test_hidden_alive_ref_is_not_showing_and_a_gone_ref_stays_stale(monkeypatch) -> None:
    """Synthetic tree. A ref whose node is still in a hidden document is
    not_showing. A DEFUNCT node is stale_ref. Not a live Firefox."""
    from a11y_computer_use.schema import Bounds, Element, Scope, Snapshot

    tree = _firefox_documents()
    products = Element(
        "e49", "AXLink", "Products", None, Bounds(0, 8, 234, 67, 23), "snap-old",
        clickable=True,
    )
    old = Snapshot("snap-old", Scope.WINDOW, "firefox", 1, 0.0, (), (products,))
    live = Snapshot("snap-live", Scope.WINDOW, "firefox", 1, 0.0, (), ())
    observe._register_epoch("snap-old", {}, {"e49": tree["products"]})
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(driver, "snapshot", lambda *_args, **_kwargs: live)

    with pytest.raises(ComputerUseError) as exc:
        driver.resolve_ref(old, "e49")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "not_showing"
    assert exc.value.detail["outcome"] == "refused"
    assert exc.value.detail["next"][0] == "foreground"
    assert "re-observe" not in exc.value.message

    tree["products"].states.add("DEFUNCT")
    monkeypatch.setattr(_atspi, "find_root", lambda *_args, **_kwargs: tree["frame"])
    with pytest.raises(ComputerUseError) as exc:
        driver.resolve_ref(old, "e49")
    assert exc.value.code is ErrorCode.STALE_REF

    tree["products"].states.discard("DEFUNCT")
    observe._register_epoch("snap-old", {}, {})
    with pytest.raises(ComputerUseError) as exc:
        driver.resolve_ref(old, "e49")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "not_showing"
    assert exc.value.detail["outcome"] == "refused"


def _chrome_documents():
    """Active form and a background tab. The background document is not SHOWING."""
    app = _GeckoNode("application", "Google Chrome")
    app.toolkit = "Chromium"
    name = _GeckoNode("entry", "Name", ("SHOWING", "VISIBLE", "FOCUSABLE", "EDITABLE"))
    form = _GeckoNode("document web", "Form Probe", ("SHOWING", "VISIBLE"), [name])
    hidden = _GeckoNode("entry", "Hidden", ("VISIBLE", "FOCUSABLE", "EDITABLE"))
    background = _GeckoNode("document web", "Other Probe", ("VISIBLE",), [hidden])
    frame = _GeckoNode(
        "frame", "Form Probe - Google Chrome", ("SHOWING", "VISIBLE"), [form, background],
    )
    for node in (name, form, hidden, background, frame):
        node.application = app
    return {"name": name, "hidden": hidden, "form": form, "background": background, "frame": frame}


def test_chrome_hidden_alive_ref_is_not_showing_and_a_gone_ref_stays_stale(monkeypatch) -> None:
    """Synthetic tree. A live node in a background Chrome tab is not_showing.

    A DEFUNCT node stays stale_ref. Not a live Chrome.
    """
    from a11y_computer_use.schema import Bounds, Element, Scope, Snapshot

    tree = _chrome_documents()
    assert _atspi.hidden_web_target(tree["hidden"]) is True
    assert _atspi.hidden_web_target(tree["name"]) is False
    hidden = Element(
        "e3", "AXTextField", "Hidden", None, Bounds(0, 8, 40, 120, 24), "snap-old",
        editable=True,
    )
    showing = Element(
        "e1", "AXTextField", "Name", None, Bounds(0, 8, 10, 120, 24), "snap-old",
        editable=True,
    )
    old = Snapshot("snap-old", Scope.WINDOW, "chrome", 1, 0.0, (), (showing, hidden))
    live = Snapshot("snap-live", Scope.WINDOW, "chrome", 1, 0.0, (), ())
    observe._register_epoch("snap-old", {}, {"e3": tree["hidden"], "e1": tree["name"]})
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(driver, "snapshot", lambda *_args, **_kwargs: live)

    with pytest.raises(ComputerUseError) as exc:
        driver.resolve_ref(old, "e3")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "not_showing"
    assert exc.value.detail["outcome"] == "refused"
    assert exc.value.detail["next"][0] == "foreground"
    assert "re-observe" not in exc.value.message

    with pytest.raises(ComputerUseError) as exc:
        driver.resolve_ref(old, "e1")
    assert exc.value.code is ErrorCode.STALE_REF

    tree["hidden"].states.add("DEFUNCT")
    monkeypatch.setattr(_atspi, "find_root", lambda *_args, **_kwargs: tree["frame"])
    with pytest.raises(ComputerUseError) as exc:
        driver.resolve_ref(old, "e3")
    assert exc.value.code is ErrorCode.STALE_REF

    # Live Chrome leaves SHOWING set and detaches the background document
    # from the frame. A document that is still SHOWING but is not the
    # selected tab is the same refusal.
    app = tree["frame"].application
    orphan = _GeckoNode("entry", "Name", ("SHOWING", "VISIBLE", "EDITABLE"))
    orphan_doc = _GeckoNode("document web", "Outcome Probe", ("SHOWING", "VISIBLE"), [orphan])
    orphan.application = app
    orphan_doc.application = app
    assert _atspi.hidden_web_target(orphan) is True
    tab = _GeckoNode("page tab", "Other Probe", ("SHOWING", "SELECTED"))
    attached = _GeckoNode("entry", "Name", ("SHOWING", "VISIBLE", "EDITABLE"))
    attached_doc = _GeckoNode(
        "document web", "Outcome Probe", ("SHOWING", "VISIBLE"), [attached],
    )
    other = _GeckoNode(
        "frame", "Other Probe - Google Chrome", ("SHOWING", "ACTIVE"), [attached_doc, tab],
    )
    for node in (attached, attached_doc, tab, other):
        node.application = app
    assert _atspi.hidden_web_target(attached) is True


def test_reused_parent_ids_do_not_hide_a_showing_electron_document(monkeypatch) -> None:
    """Electron page content sits several panels under a showing frame.

    ``get_parent`` returns a new wrapper. After the walk drops one, CPython
    allocates a later panel at that same ``id()``. A seen-set of those ids
    stops before the frame and reports the document hidden. The walk keys on
    the AT-SPI bus name and object path, and keeps each wrapper alive when
    that key is missing. A second chain reuses two wrapper objects for
    different ancestors, so ``id()`` collides while both objects are alive.
    Not a live Electron.
    """
    asked: list[tuple[str, str]] = []

    def key(node):
        token = getattr(node, "atspi_key", None)
        if token is not None:
            asked.append(token)
        return token

    monkeypatch.setattr(_atspi, "_atspi_wrapper_key", key)

    class App:
        def get_toolkit_name(self):
            return "Chromium"

        def get_name(self):
            return "electron"

        def get_application(self):
            return self

    app = App()
    frame_at = 9

    def states_of(names: set[str]):
        copied = set(names)
        return _NS(names=copied, contains=lambda member: str(member) in copied)

    class Fresh:
        __slots__ = ("role", "name", "states", "app", "depth", "atspi_key")

        def __init__(self, role: str, depth: int):
            self.role = role
            self.name = "Notes" if role == "document web" else "Save note" if role == "push button" else ""
            self.states = {"SHOWING", "VISIBLE"}
            self.app = app
            self.depth = depth
            if role == "push button":
                path = "/button"
            elif role == "document web":
                path = "/doc"
            elif role == "frame":
                path = "/frame"
            else:
                path = f"/panel/{depth}"
            self.atspi_key = (":1.44", path)

        def get_role_name(self):
            return self.role

        def get_name(self):
            return self.name

        def get_application(self):
            return self.app

        def get_state_set(self):
            return states_of(self.states)

        def get_parent(self):
            if self.depth >= frame_at:
                return None
            nxt = self.depth + 1
            if nxt == 1:
                role = "document web"
            elif nxt == frame_at:
                role = "frame"
            else:
                role = "panel"
            # Same-sized garbage. A freed wrapper's id is what the next
            # ancestor is allocated at when the walk does not keep it.
            dead = [Fresh("panel", -1) for _ in range(8)]
            del dead
            return Fresh(role, nxt)

    for _ in range(30):
        asked.clear()
        assert _atspi.hidden_web_target(Fresh("push button", 0)) is False
        assert (":1.44", "/frame") in asked

    specs = [("document web", "/doc")]
    specs.extend(("panel", f"/panel/{depth}") for depth in range(2, frame_at))
    specs.append(("frame", "/frame"))

    class Recycled(Fresh):
        def get_parent(self):
            if self.depth >= frame_at:
                return None
            return materialize(self.depth + 1)

    shells = [Recycled("panel", 0), Recycled("panel", 0)]

    def materialize(index: int) -> Recycled:
        shell = shells[(index - 1) % 2]
        role, path = specs[index - 1]
        shell.role = role
        shell.name = "Notes" if role == "document web" else ""
        shell.states = {"SHOWING", "VISIBLE"}
        shell.depth = index
        shell.atspi_key = (":1.44", path)
        return shell

    class Button(Recycled):
        def __init__(self):
            super().__init__("push button", 0)
            self.atspi_key = (":1.44", "/button")

        def get_parent(self):
            return materialize(1)

    asked.clear()
    assert _atspi.hidden_web_target(Button()) is False
    assert (":1.44", "/doc") in asked
    assert (":1.44", "/frame") in asked


def test_set_text_on_wayland_does_not_claim_success_when_delete_is_a_noop(fake_atspi, monkeypatch) -> None:
    field = _KeyClearedWebField("bench-value-0")
    sent: list[str] = []
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(_linux_input, "press_chord", lambda chord: sent.append(chord))
    assert _atspi.set_text(field, "BETA") is False
    assert field.text != "BETA"
    assert sent == []


def _chrome_contenteditable(field):
    """A Chrome contenteditable shape: div, role=textbox, no input tag."""
    _mark_toolkit(field, "Chromium", "Google Chrome")
    field.attrs = {"tag": "div", "xml-roles": "textbox"}
    field.get_attributes = lambda: dict(field.attrs)
    field.get_role_name = lambda: "entry"
    return field


def test_typed_visible_ignores_nbsp_and_one_trailing_newline() -> None:
    """Chrome stores edge spaces as NBSP and appends the contenteditable <br>."""
    assert _atspi._typed_visible(
        "Hello", "Hello\u00a0ZZ\u00a0\n", " ZZ ",
    )
    assert _atspi._typed_visible("Hello", "Hello\n", "ZZ") is False
    assert _atspi._typed_visible("a", "a\n", "\n") is True
    # Chrome drops a trailing space. An interior space still has to be there.
    assert _atspi._typed_visible(
        "ZZFirst paraSecond bold para",
        "ZZFirst paraSecond bold para m0",
        " m0 ",
        chrome=True,
    )
    assert _atspi._typed_visible("Hello", "Hello", " m0 ", chrome=True) is False
    # The find bar reopens with the query selected. Typing it again leaves
    # the same string, and that string is the text that landed.
    assert _atspi._typed_visible("4711", "4711", "4711") is True
    assert _atspi._typed_visible("hello world", "hello world", "4711") is False


def test_typed_visible_replaces_a_selection_instead_of_appending() -> None:
    """A selected word is replaced. The check is the final text, not an append.

    Without the selection, ``WORD`` sitting in the middle of a shorter field
    is not success: the old model required the typed text at the end or the
    field to grow. With the selection, the result has to be that range
    replaced and nothing else.
    """
    before = "Hello world startSecond line"
    after = "Hello WORD startSecond line"
    assert _atspi._typed_visible(before, after, "WORD") is False
    assert _atspi._typed_visible(before, after, "WORD", selection=(6, 11)) is True
    assert _atspi._typed_visible(
        before, after, "WORD", selection=(6, 11), chrome=True,
    ) is True
    assert _atspi._typed_visible(before, after, "WORD", selection=(0, 5)) is False
    assert _atspi._typed_visible(before, before + "WORD", "WORD") is True


def test_selection_span_covers_an_embedded_word(fake_atspi) -> None:
    """A contenteditable parent stores the bold word as one U+FFFC.

    The selection of that object is the word in the expanded read-back.
    """
    word = _Acc("text")
    word.text = "world"
    parent = _Acc("section")
    parent.text = "Hello \ufffc start"
    parent.links = [word]
    expanded, starts = _atspi._expansion_starts(parent, parent.text)
    assert expanded == "Hello world start"
    assert starts[6] == 6 and starts[7] == 11
    parent.selection = (6, 7)
    assert _atspi._selection_span(parent, expanded) == (6, 11)
    assert _atspi._typed_visible(
        expanded, "Hello WORD start", "WORD", selection=(6, 11), chrome=True,
    ) is True


def test_selection_span_uses_the_word_inside_the_paragraph(fake_atspi) -> None:
    """Chrome selects the paragraph object and the word inside it.

    The contenteditable's own text is one U+FFFC per paragraph, so its
    selection is the whole first paragraph. The paragraph's selection is the
    word. Typing replaces the word, so the span is the word.
    """
    word = _Acc("static", name="world")
    word.text = "world"
    word.selection = (0, 5)
    para = _Acc("paragraph")
    para.text = "Hello world start"
    para.selection = (6, 11)
    _adopt(para, word)
    second = _Acc("paragraph")
    second.text = "Second line"
    parent = _Acc("entry", name="Story editor")
    parent.text = "\ufffc\ufffc"
    parent.links = [para, second]
    parent.selection = (0, 1)
    readable = _atspi._readable_text(parent)
    assert readable == "Hello world start\nSecond line"
    assert _atspi._selection_span(parent, readable) == (6, 11)
    para.selection = None
    assert _atspi._selection_span(parent, readable) == (6, 11)
    para.selection = (0, len(para.text))
    word.selection = (0, 5)
    assert _atspi._selection_span(parent, readable) == (0, len(para.text))


def test_typed_breaks_match_equates_boundaries_and_keeps_other_text() -> None:
    """Newline, CRLF, U+2028, U+2029, and a block boundary are one break.

    A missing break, an extra break, and any other extra or missing
    character are not a match. The same helper is what Chrome type, Writer
    paragraph lines, and a Calc cell use.
    """
    from a11y_computer_use.outcome import typed_breaks_match

    assert typed_breaks_match("a\nb", "a\nb") is True
    assert typed_breaks_match("a\r\nb", "a\nb") is True
    assert typed_breaks_match("a\u2028b", "a\nb") is True
    assert typed_breaks_match("a\u2029b", "a\nb") is True
    assert typed_breaks_match("a\u00a0\nb", "a \nb") is True
    assert typed_breaks_match("ab", "a\nb") is False
    assert typed_breaks_match("a b", "a\nb") is False
    assert typed_breaks_match("a\n\nb", "a\nb") is False
    assert typed_breaks_match("a\nbX", "a\nb") is False
    assert typed_breaks_match("a\n", "a\nb") is False
    assert typed_breaks_match(None, "a\nb") is False
    assert _atspi._typed_visible(
        "Alpha beta", "Alpha beta gamma\u2028line2", " gamma\nline2", chrome=True,
    ) is True
    assert _atspi._typed_visible(
        "Alpha beta", "Alpha beta gammaline2", " gamma\nline2", chrome=True,
    ) is False
    assert _atspi._typed_visible(
        "Alpha beta", "Alpha beta gamma\nline3", " gamma\nline2", chrome=True,
    ) is False


def test_chrome_contenteditable_type_confirms_a_newline_between_paragraphs(
    fake_atspi, monkeypatch,
) -> None:
    """Fake Chrome contenteditable. A newline becomes the next paragraph.

    Not a browser. The parent text stays one object character per paragraph.
    The read-back keeps the boundary, so the typed newline confirms. The
    same call is a mismatch when the paragraphs are different text, and when
    the boundary was dropped.
    """
    field = _chrome_contenteditable(_KeyClearedWebField("Alpha beta"))
    field.get_editable_text_iface = None

    def type_string(text: str) -> None:
        assert text == " gamma\nline2"
        first = _Acc("paragraph")
        first.text = "Alpha beta gamma"
        second = _Acc("paragraph")
        second.text = "line2"
        field.text = "\ufffc\ufffc"
        field.links = [first, second]

    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    monkeypatch.setattr(_linux_input, "type_string", type_string)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    assert _atspi.chromium_contenteditable_type(field, " gamma\nline2") is True
    assert _atspi._readable_text(field) == "Alpha beta gamma\nline2"

    flat = _chrome_contenteditable(_KeyClearedWebField("Alpha beta gammaline2"))
    flat.get_editable_text_iface = None
    monkeypatch.setattr(_linux_input, "type_string", lambda _text: None)
    assert _atspi.chromium_contenteditable_type(flat, " gamma\nline2") is False

    wrong = _chrome_contenteditable(_KeyClearedWebField("Alpha beta"))
    wrong.get_editable_text_iface = None

    def type_wrong(text: str) -> None:
        first = _Acc("paragraph")
        first.text = "Alpha beta gamma"
        second = _Acc("paragraph")
        second.text = "LINE2"
        wrong.text = "\ufffc\ufffc"
        wrong.links = [first, second]

    monkeypatch.setattr(_linux_input, "type_string", type_wrong)
    assert _atspi.chromium_contenteditable_type(wrong, " gamma\nline2") is False


def test_omnibox_poll_accepts_the_url_after_a_truncated_read() -> None:
    """The address bar can publish a prefix first. The later full URL matches."""
    url = "https://2captcha.com/demo/recaptcha-v2"
    reads = iter([url[:28], url[:28], url])
    seen = _atspi._poll_typed_text(lambda: next(reads), "file:///tmp/omni.html", url)
    assert seen == url
    assert _atspi._typed_visible("file:///tmp/omni.html", seen, url)
    assert _atspi._typed_visible("Hello", "Helloa b", "a  b", chrome=True) is False
    assert _atspi._typed_visible(
        "ZZFirst paraSecond bold para",
        "ZZFirst paraSecond bold para m0",
        " m0 ",
    ) is False


def test_chrome_contenteditable_type_waits_for_the_settled_text(fake_atspi, monkeypatch) -> None:
    """Fake Chrome contenteditable. The first reads are still the old text.

    Not a browser. A later read with NBSP and a trailing newline is success.
    The keys are sent once.
    """
    field = _chrome_contenteditable(_KeyClearedWebField("Hello"))
    field.get_editable_text_iface = None
    reads = {"n": 0}
    typed: list[str] = []

    def readable(_acc):
        reads["n"] += 1
        if reads["n"] < 3:
            return "Hello"
        return "Hello\u00a0ZZ\u00a0\n"

    monkeypatch.setattr(_atspi, "_readable_text", readable)
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    monkeypatch.setattr(_linux_input, "type_string", typed.append)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    assert _atspi.chromium_contenteditable_type(field, " ZZ ") is True
    assert typed == [" ZZ "]
    assert reads["n"] >= 3


def test_chrome_contenteditable_type_replaces_the_selected_word(
    fake_atspi, monkeypatch
) -> None:
    """Fake Chrome contenteditable. Typing replaces the selected word.

    Not a browser. The read-back is the field with that range replaced.
    Appending ``WORD`` onto ``world`` is not success.
    """
    field = _chrome_contenteditable(_KeyClearedWebField("Hello world start"))
    field.get_editable_text_iface = None
    field.selection = (6, 11)
    typed: list[str] = []

    def type_string(text: str) -> None:
        typed.append(text)
        start, end = field.selection
        field.text = field.text[:start] + text + field.text[end:]
        field.selection = None

    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    monkeypatch.setattr(_linux_input, "type_string", type_string)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    assert _atspi.chromium_contenteditable_type(field, "WORD") is True
    assert typed == ["WORD"]
    assert field.text == "Hello WORD start"
    assert _atspi.chromium_contenteditable_type.last_expected == "Hello WORD start"


def test_chrome_contenteditable_type_that_settles_wrong_is_not_success(
    fake_atspi, monkeypatch
) -> None:
    """Fake Chrome contenteditable. A stable wrong read is a mismatch.

    Not a browser. Waiting does not turn the other text into the request.
    """
    field = _chrome_contenteditable(_KeyClearedWebField("Hello"))
    field.get_editable_text_iface = None
    reads = {"n": 0}

    def readable(_acc):
        reads["n"] += 1
        if reads["n"] == 1:
            return "Hello"
        return "HelloNO"

    monkeypatch.setattr(_atspi, "_readable_text", readable)
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    monkeypatch.setattr(_linux_input, "type_string", lambda _text: None)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    assert _atspi.chromium_contenteditable_type(field, "ZZ") is False
    # The wrong text is stable, so the wait stops. It does not run the
    # whole window, and it does not report success.
    assert 1 < reads["n"] < _atspi._TYPE_SETTLE_POLLS
    plain = _AppendingField("Hi")
    _mark_toolkit(plain, "GTK", "gedit")
    assert _atspi.chromium_contenteditable_type(plain, "Z") is None


def test_chrome_contenteditable_clear_accepts_a_newline(fake_atspi, monkeypatch) -> None:
    """Fake Chrome contenteditable. BackSpace leaves a newline, which is empty.

    Not a browser. ``set_value ""`` used to restore "Hello world" because the
    read-back was ``\\n`` rather than ``""``.
    """
    field = _chrome_contenteditable(_KeyClearedWebField("Hello world"))
    field.get_editable_text_iface = None
    sent: list[str] = []

    def press_chord(chord: str) -> None:
        sent.append(chord)
        if chord == "ctrl+a":
            field.selected_all = True
        elif chord == "backspace" and getattr(field, "selected_all", False):
            field.text = "\n"
            field.echo = None
            field.selected_all = False

    monkeypatch.setattr(_linux_input, "press_chord", press_chord)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    assert _atspi.set_text(field, "") is True
    assert field.text == "\n"
    assert "Hello world" not in field.text
    assert sent[:3] == ["ctrl+a", "backspace", "delete"]


def test_chrome_contenteditable_clear_that_does_not_stick_is_not_success(
    fake_atspi, monkeypatch
) -> None:
    """Fake Chrome contenteditable. Keys that do nothing leave the text, and fail.

    Not a browser. The result is not a successful clear.
    """
    field = _chrome_contenteditable(_KeyClearedWebField("Hello world"))
    field.get_editable_text_iface = None
    monkeypatch.setattr(_linux_input, "press_chord", lambda _chord: None)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    assert _atspi.set_text(field, "") is False
    assert field.text == "Hello world"


def test_gtk_set_text_empty_still_replaces(fake_atspi, monkeypatch) -> None:
    """Fake GTK entry. ``set_text_contents`` clears it. No key chords."""
    field = _ReplacingField("kept")
    sent: list[str] = []
    monkeypatch.setattr(_linux_input, "press_chord", sent.append)
    assert _atspi.set_text(field, "") is True
    assert field.text == ""
    assert sent == []


def test_contenteditable_clear_that_leaves_a_newline_still_types(fake_atspi, monkeypatch) -> None:
    """Fake transport. Chrome's empty contenteditable reads back as a newline.

    That newline used to look like leftover text, so the replacement stopped
    after the clear and the editor stayed empty.
    """
    field = _KeyClearedWebField("Hello world")
    field.get_editable_text_iface = None
    sent: list[str] = []

    def press_chord(chord: str) -> None:
        sent.append(chord)
        if chord == "ctrl+a":
            field.selected_all = True
        elif chord == "backspace" and getattr(field, "selected_all", False):
            field.text = "\n"
            field.echo = None
            field.selected_all = False

    def type_string(text: str) -> None:
        field.text = text

    monkeypatch.setattr(_linux_input, "press_chord", press_chord)
    monkeypatch.setattr(_linux_input, "type_string", type_string)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    assert _atspi.set_text(field, "Set 0") is True
    assert field.text == "Set 0"
    assert sent == ["ctrl+a", "backspace"]


def test_contenteditable_restores_the_original_when_the_write_does_not_land(
    fake_atspi, monkeypatch
) -> None:
    """Fake transport. A failed read-back types the original text back."""
    field = _KeyClearedWebField("Hello world")
    field.get_editable_text_iface = None

    def press_chord(chord: str) -> None:
        if chord == "ctrl+a":
            field.selected_all = True
        elif chord == "backspace" and getattr(field, "selected_all", False):
            field.text = "\n"
            field.echo = None
            field.selected_all = False

    def type_string(text: str) -> None:
        field.text = text if text == "Hello world" else "WRONG"

    monkeypatch.setattr(_linux_input, "press_chord", press_chord)
    monkeypatch.setattr(_linux_input, "type_string", type_string)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    assert _atspi.set_text(field, "Set 0") is False
    assert field.text == "Hello world"


def test_contenteditable_nbsp_read_back_matches(fake_atspi, monkeypatch) -> None:
    """Fake transport. A NBSP in the read-back is the space that was requested."""
    field = _KeyClearedWebField("Hello world")
    field.get_editable_text_iface = None

    def press_chord(chord: str) -> None:
        if chord == "ctrl+a":
            field.selected_all = True
        elif chord == "backspace" and getattr(field, "selected_all", False):
            field.text = "\n"
            field.selected_all = False

    def type_string(text: str) -> None:
        field.text = text.replace(" ", "\u00a0")

    monkeypatch.setattr(_linux_input, "press_chord", press_chord)
    monkeypatch.setattr(_linux_input, "type_string", type_string)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    assert _atspi.set_text(field, "Set 0") is True
    assert field.text == "Set\u00a00"


def test_insert_text_accepts_an_expanded_object_replacement(fake_atspi, monkeypatch) -> None:
    """Fake transport. The parent text stays U+FFFC; the child gained the characters."""
    parent = _KeyClearedWebField("\ufffc\ufffc")
    first = _KeyClearedWebField("First para")
    second = _KeyClearedWebField("Second bold para")
    parent.links = [first, second]

    def insert_text(pos, text, length):
        first.text = text[:length] + first.text
        return True

    parent.insert_text = insert_text
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    assert _atspi.insert_text(parent, "ZZ") == 2
    assert first.text.startswith("ZZ")


def test_insert_text_unchanged_compares_the_expanded_text(fake_atspi) -> None:
    """Fake transport. A parent that stays U+FFFC is unchanged only when the
    child text did not change. The error shows that child text."""
    parent = _KeyClearedWebField("\ufffc\ufffc")
    first = _KeyClearedWebField("First para")
    second = _KeyClearedWebField("Second bold para")
    parent.links = [first, second]
    parent.insert_text = lambda *_args: True
    with pytest.raises(ComputerUseError) as exc:
        _atspi.insert_text(parent, "ZZ")
    assert exc.value.detail["unchanged"] is True
    assert exc.value.detail["actual"] == "First paraSecond bold para"
    assert "\ufffc" not in exc.value.detail["actual"]


def test_gecko_type_accepts_nbsp_and_an_expanded_child(fake_atspi, monkeypatch) -> None:
    """Fake Firefox contenteditable. The key fallback lands, and the read-back
    is not a mismatch. Not a live browser."""
    from a11y_computer_use.drivers import _linux_system
    from a11y_computer_use.drivers.linux import LinuxDriver

    editor = _NoopWebField("Hello world")
    _mark_toolkit(editor, "Gecko", "Firefox")
    editor.get_component_iface = lambda: editor

    def grab_focus():
        editor.focused = True
        return True

    editor.grab_focus = grab_focus

    def type_string(text: str) -> None:
        editor.text += text.replace(" ", "\u00a0")

    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr("a11y_computer_use.drivers.linux._on_wayland", lambda: False)
    monkeypatch.setattr(driver, "frontmost_app", lambda: ("firefox", 1))
    monkeypatch.setattr(_atspi, "is_secure", lambda acc: False)
    monkeypatch.setattr(_atspi, "pid_of", lambda acc: 7)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "firefox")
    monkeypatch.setattr(_linux_input, "type_string", type_string)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    driver._focused_editable = editor
    assert driver.type_text("  two spaces end ") == len("  two spaces end ")
    assert "two spaces end" in editor.text.replace("\u00a0", " ")

    parent = _NoopWebField("\ufffc\ufffc")
    _mark_toolkit(parent, "Gecko", "Firefox")
    child = _KeyClearedWebField("First para")
    parent.links = [child]
    parent.get_component_iface = lambda: parent
    parent.grab_focus = grab_focus

    def type_child(text: str) -> None:
        child.text = text + child.text

    monkeypatch.setattr(_linux_input, "type_string", type_child)
    driver._focused_editable = parent
    assert driver.type_text("ZZ") == 2
    assert child.text == "ZZFirst para"


def test_gecko_contenteditable_set_value_restores_when_the_keys_do_not_land(
    fake_atspi, monkeypatch
) -> None:
    """Fake Firefox contenteditable. A failed replace types the original back.

    Not a live browser. The clear is what erases Hello world.
    """
    field = _NoopWebField("Hello world")
    _mark_toolkit(field, "Gecko", "Firefox")
    field.get_component_iface = lambda: field

    def grab_focus():
        field.focused = True
        return True

    field.grab_focus = grab_focus

    def press_chord(chord: str) -> None:
        if chord == "ctrl+a":
            field.selected_all = True
        elif chord == "backspace" and getattr(field, "selected_all", False):
            field.text = "\n"
            field.selected_all = False

    def type_string(text: str) -> None:
        field.text = text if text == "Hello world" else "\n"

    monkeypatch.setattr(_linux_input, "press_chord", press_chord)
    monkeypatch.setattr(_linux_input, "type_string", type_string)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    assert _atspi.set_text(field, "Set 0") is False
    assert field.text == "Hello world"


def test_gecko_contenteditable_set_value_accepts_a_nbsp_read_back(fake_atspi, monkeypatch) -> None:
    """Fake Firefox contenteditable. NBSP in the read-back matches the request."""
    field = _NoopWebField("Hello world")
    _mark_toolkit(field, "Gecko", "Firefox")
    field.get_component_iface = lambda: field
    field.grab_focus = lambda: True

    def press_chord(chord: str) -> None:
        if chord == "ctrl+a":
            field.selected_all = True
        elif chord == "backspace" and getattr(field, "selected_all", False):
            field.text = "\n"
            field.selected_all = False

    def type_string(text: str) -> None:
        field.text = text.replace(" ", "\u00a0")

    monkeypatch.setattr(_linux_input, "press_chord", press_chord)
    monkeypatch.setattr(_linux_input, "type_string", type_string)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    assert _atspi.set_text(field, "Set 0") is True
    assert field.text.replace("\u00a0", " ") == "Set 0"


class _GeckoEditableSection:
    """A Firefox ``<div contenteditable>``: AT-SPI section, not an entry.

    EditableText returns true and does not change the DOM unless a test
    says it does. Not a live browser.
    """

    def __init__(self, text: str = "editable block"):
        self.text = text
        self.states = {"EDITABLE", "ENABLED", "FOCUSABLE", "MULTI_LINE"}
        self.attrs = {"tag": "div"}
        self.focused = False
        self.writes: list[str] = []
        self.deletes = 0
        self.selected_all = False
        self._land = False

    def get_role_name(self):
        return "section"

    def get_name(self):
        return ""

    def get_description(self):
        return ""

    def get_state_set(self):
        names = set(self.states)
        if self.focused:
            names.add("FOCUSED")
        return _States(names)

    def get_attributes(self):
        return dict(self.attrs)

    def get_application(self):
        return _toolkit_app("Gecko", "Firefox")

    def get_editable_text_iface(self):
        return self

    def set_text_contents(self, text):
        self.writes.append(text)
        if self._land:
            self.text = text
        return True

    def delete_text(self, _start, _end):
        self.deletes += 1
        return True

    def get_component_iface(self):
        return self

    def grab_focus(self):
        self.focused = True
        return True

    def get_child_count(self):
        return 0


def test_gecko_contenteditable_set_value_types_only_after_focus(fake_atspi, monkeypatch) -> None:
    """Fake Firefox contenteditable. Keys run only after focus is confirmed.

    EditableText does not change the DOM. The read-back accepts one trailing
    newline. Text selection is not used. Not a live browser.
    """
    field = _GeckoEditableSection("editable block")
    sent: list[str] = []
    selected: list = []

    def press_chord(chord: str) -> None:
        assert field.focused
        sent.append(chord)
        if chord == "ctrl+a":
            field.selected_all = True
        elif chord == "backspace" and field.selected_all:
            field.text = "\n"
            field.selected_all = False

    def type_string(text: str) -> None:
        assert field.focused
        # One character is one batch once focus can be read. A trailing
        # newline is the empty paragraph, added once the request is complete.
        if field.text in ("", "\n"):
            field.text = text
        else:
            field.text = field.text.rstrip("\n") + text
        if field.text == "CE_VAL_1":
            field.text += "\n"

    monkeypatch.setattr(
        fake_atspi.Text, "set_selection", lambda *args: selected.append(args) or True,
    )
    monkeypatch.setattr(_linux_input, "press_chord", press_chord)
    monkeypatch.setattr(_linux_input, "type_string", type_string)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    assert _atspi.set_text(field, "CE_VAL_1") is True
    assert field.text.replace("\n", "") == "CE_VAL_1"
    assert sent[:3] == ["ctrl+a", "backspace", "delete"]
    assert field.writes == ["CE_VAL_1"]
    assert selected == []
    assert field.deletes == 0

    field._land = True
    field.text = "editable block"
    field.focused = False
    sent.clear()
    assert _atspi.set_text(field, "CE_VAL_2") is True
    assert field.text == "CE_VAL_2"
    assert sent == []


def test_gecko_contenteditable_set_value_types_nothing_without_focus(fake_atspi, monkeypatch) -> None:
    """Fake Firefox contenteditable. Unconfirmed focus sends no keys.

    Not a live browser. The DOM stays on the original text.
    """
    field = _GeckoEditableSection("editable block")
    field.grab_focus = lambda: True
    sent: list[str] = []
    monkeypatch.setattr(_linux_input, "press_chord", lambda chord: sent.append(chord))
    monkeypatch.setattr(_linux_input, "type_string", lambda text: sent.append(text))
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: field)
    with pytest.raises(ComputerUseError) as exc:
        _atspi.set_text(field, "CE_VAL")
    assert exc.value.code is ErrorCode.FOCUS_LOST
    assert exc.value.detail["reason"] == "focus_lost"
    assert sent == []
    assert field.text == "editable block"

    box = Bounds(0, 8, 8, 200, 40)
    element = Element(
        "e40", "AXTextArea", "", "editable block", box, "snap-1", editable=True,
    )
    with pytest.raises(ComputerUseError) as driver_exc:
        LinuxDriver().set_value(element, "CE_VAL")
    assert driver_exc.value.code is ErrorCode.FOCUS_LOST
    assert sent == []
    assert field.text == "editable block"


def test_gecko_contenteditable_set_value_restores_when_focused_keys_miss(
    fake_atspi, monkeypatch,
) -> None:
    """Fake Firefox contenteditable. A missed read-back types the original back.

    Focus is confirmed, so the keys are the fallback. Not a live browser.
    """
    field = _GeckoEditableSection("editable block")
    sent: list[str] = []
    field.chunks: list[str] = []

    def press_chord(chord: str) -> None:
        sent.append(chord)
        if chord == "ctrl+a":
            field.selected_all = True
        elif chord == "backspace" and field.selected_all:
            field.text = "\n"
            field.chunks.clear()
            field.selected_all = False

    def type_string(text: str) -> None:
        # One character is one batch. The replacement reads back wrong.
        # The original is typed back one character at a time after the clear.
        field.chunks.append(text)
        joined = "".join(field.chunks)
        field.text = "WRONG" if joined.startswith("C") else joined

    monkeypatch.setattr(_linux_input, "press_chord", press_chord)
    monkeypatch.setattr(_linux_input, "type_string", type_string)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    assert _atspi.set_text(field, "CE_VAL") is False
    assert field.text == "editable block"
    assert sent[:3] == ["ctrl+a", "backspace", "delete"]


def test_driver_set_value_replaces_on_a_web_field_and_on_a_text_area(fake_atspi, monkeypatch) -> None:
    web = _AppendingField("earlier")
    pad = _ReplacingField("earlier")
    seen = {"handle": web}
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: seen["handle"])
    element = _body()
    driver = LinuxDriver()
    assert driver.set_value(element, "ALPHA") is True
    assert web.text == "ALPHA"
    assert driver.set_value(element, "BETA") is True
    assert web.text == "BETA"
    seen["handle"] = pad
    assert driver.set_value(element, "BETA") is True
    assert pad.text == "BETA"
    assert pad.deletes == 0


class _ChromeApp:
    def get_toolkit_name(self):
        return "Chromium"

    def get_name(self):
        return "Google Chrome"


class _ListHit:
    """Synthetic Chromium hit test. Not a screen grab and not a live Chrome list.

    The first call at a point is a stale bounds guess (ITEM-001's window).
    A later call is the layout row. ``screen`` is the on-screen head, 1-based.
    While ``clock`` is below ``hold_until``, that later call stays on
    ``held_head``. A point in the top 8px can answer ``screen - 1`` when
    ``clip`` is set. ``invent``, when set, answers that head instead of
    ``screen``. The cached children never move.
    """

    def __init__(self, rows: list[_Acc], stale: list[_Acc]):
        self.rows = rows
        self.stale = stale
        self.screen = 1
        self.clock = 0
        self.hold_until = 0
        self.held_head = 1
        self.clip = False
        self.invent: int | None = None
        self.on_wheel = None
        self.calls = 0
        # Grabs after the wheel that still return ``frozen`` instead of
        # ``screen``. 0.4.13 treated the first of those as a still page.
        self._paint_left = 0
        self._frozen: int | None = None
        self.grab_colors: list[int] = []
        # While clock < blind_until, every hit is ``offscreen``: the pre-scroll
        # row parked above the list. Not a live Chrome bounds read.
        self.blind_until = 0
        self.offscreen: _Acc | None = None
        # When set, every hit is this row. Used when that row's box still
        # covers the sample so the 0.4.15 bounds check would accept it.
        self.force_row: _Acc | None = None
        self.x, self.y, self.width, self.height = 40, 100, 400, 160
        self._at: dict[tuple[int, int], int] = {}

    @property
    def head(self) -> int:
        return self.screen

    @head.setter
    def head(self, value: int) -> None:
        self.screen = int(value)

    def get_extents(self, _coord):
        return self

    def _layout_row(self, y: int, slot: int):
        if self.invent is not None:
            head = self.invent
        elif self.clock < self.hold_until:
            head = self.held_head
        elif self.clip and int(y) < int(self.y) + 8:
            head = max(1, self.screen - 1)
        else:
            head = self.screen
        index = min(len(self.rows) - 1, max(0, int(head) - 1 + slot))
        return self.rows[index]

    def get_accessible_at_point(self, _x, y, _coord):
        self.calls += 1
        if self.force_row is not None:
            return self.force_row
        if self.offscreen is not None and self.clock < self.blind_until:
            return self.offscreen
        key = (int(_x), int(y))
        n = self._at.get(key, 0) + 1
        self._at[key] = n
        slot = min(7, max(0, (int(y) - int(self.y)) // 20))
        if n == 1:
            return self.stale[min(slot, len(self.stale) - 1)]
        return self._layout_row(int(y), slot)

    def advance(self) -> None:
        self.screen += 8
        self._at.clear()
        self.invent = None
        self.clip = False
        self.hold_until = 0


def _add_tab_strip(window: _Acc) -> _Acc:
    """Chrome's tab strip as a sibling of the window's current children.

    A short page tab list at the top of the frame. Synthetic bounds, not a
    live Chrome window. ``_adopt`` replaces the window's children, so the
    previous children are passed back in.
    """
    tabs = _Acc("page tab list", name="tabs", width=1280, height=36)
    tabs.component._rect.x = 0.0
    tabs.component._rect.y = 0.0
    tab = _Acc("page tab", name="Bench", width=140, height=28)
    tab.component._rect.x = 8.0
    tab.component._rect.y = 4.0
    _adopt(tabs, tab)
    previous = list(window.children)
    _adopt(window, tabs, *previous)
    app = window.get_application()
    tabs.get_application = lambda: app
    tab.get_application = lambda: app
    return tabs


def _document_scroll_page():
    """A body-scroll page: the list box is the content height, taller than the window.

    The document group is the viewport under the tab strip. Rows in the
    snapshot are the eight that sit at the top of the list. ITEM-180 is not
    among them until a wheel that lands on the page advances the window.
    Synthetic tree, not a live Chrome window.
    """
    rows = [_Acc("list item", name=f"ITEM-{i:03d}") for i in range(1, 201)]
    hit = _ListHit(rows, [_Acc("list item", name=f"ITEM-{i:03d}") for i in range(1, 9)])
    hit.x, hit.y, hit.width, hit.height = 40, 120, 1100, 6400
    listing = _Acc("list", name="items")
    listing.component = hit
    app = _ChromeApp()
    listing.get_application = lambda: app
    _show_page_rows(listing, rows, 1)
    for row in rows:
        row.parent = listing
    document = _Acc("document web", name="Bench", width=1280, height=680)
    document.component._rect.x = 0.0
    document.component._rect.y = 90.0
    document.get_application = lambda: app
    _adopt(document, listing)
    window = _Acc("frame", name="bench", width=1280, height=800)
    window.get_application = lambda: app
    _adopt(window, document)
    _add_tab_strip(window)
    return window, listing, hit, rows


def _show_page_rows(listing: _Acc, rows: list[_Acc], head: int) -> None:
    """Eight rows starting at ``head`` (1-based), flush with the list top at y=120.

    A row whose top is the list's top is the snapshot head. Synthetic boxes.
    """
    visible = []
    start = max(0, head - 1)
    for index, row in enumerate(rows[start:start + 8]):
        _place_row(row, 40, 120 + index * 20, 1100, 20)
        visible.append(row)
    _adopt(listing, *visible)


def _chrome_list(stuck: bool = False):
    rows = [_Acc("list item", name=f"ITEM-{i:03d}") for i in range(1, 201)]
    # Distinct objects from the cached children, so the second hit test is a
    # different accessible even when it still names ITEM-001.
    stale = [_Acc("list item", name=f"ITEM-{i:03d}") for i in range(1, 9)]
    hit = _ListHit(rows, stale)
    listing = _Acc("list", name="items")
    listing.component = hit
    app = _ChromeApp()
    listing.get_application = lambda: app
    _adopt(listing, *rows[:8])
    for row in rows[8:]:
        row.parent = listing
    window = _Acc("frame", name="bench", width=1280, height=800)
    window.get_application = lambda: app
    _adopt(window, listing)
    return window, listing, hit, stuck


def _wire_chrome_list(monkeypatch, window, hit, stuck: bool):
    _atspi.reset_shown_rows()
    monkeypatch.setattr(_atspi, "find_root", lambda *_args, **_kwargs: window)
    monkeypatch.setattr(_atspi, "primary_geometry", _geometry)
    monkeypatch.setattr(_atspi, "_screen_size", lambda: (1280, 800))

    def sleep(_seconds):
        hit.clock += 1

    monkeypatch.setattr(_atspi.time, "sleep", sleep)

    def grab(_box):
        from PIL import Image

        # Solid stand-in for the list pixels. Same color means the page did
        # not move. A positive ``_paint_left`` keeps the pre-wheel color for
        # that many grabs, which is the frame 0.4.13 compared too early.
        # Not a capture of a live Chrome window.
        color = int(hit.screen) & 255
        if hit._paint_left > 0 and hit._frozen is not None:
            color = int(hit._frozen) & 255
            hit._paint_left -= 1
        hit.grab_colors.append(color)
        return Image.new("RGB", (4, 4), (color, 0, 0))

    monkeypatch.setattr("a11y_computer_use.drivers.linux._grab_region", grab)
    real_scroll = _linux_input.scroll

    def scrolling(x, y, dx=0, dy=0):
        real_scroll(x, y, dx=dx, dy=dy)
        if stuck or not (int(dx) or int(dy)):
            return
        if hit.on_wheel is not None:
            hit.on_wheel()
        else:
            hit.advance()

    monkeypatch.setattr(_linux_input, "scroll", scrolling)


def _record_wheel_dy(monkeypatch, hit):
    """Remember the dy ``scroll`` was called with before the wired wheel runs.

    ``_wire_chrome_list`` calls ``on_wheel`` without the delta. Synthetic.
    """
    wired = _linux_input.scroll

    def record(x, y, dx=0, dy=0):
        hit.last_dy = int(dy)
        return wired(x, y, dx=dx, dy=dy)

    monkeypatch.setattr(_linux_input, "scroll", record)


def _row_titles(snap) -> list[str]:
    return [el.title for el in snap.elements if el.role == "AXRow"]


def _own(app, *nodes: _Acc) -> None:
    for node in nodes:
        node.get_application = lambda: app


def _expanded_chrome_tree(*, sibling: bool = False):
    """Chromium ARIA tree whose expanded row is as tall as the tree.

    Synthetic boxes, not a live Chrome window. The expanded treeitem's box
    is the tree's box, which is what made the visible-row walk record the
    inner static text instead of the row.
    """
    app = _ChromeApp()
    bullet = _Acc("static", name="•", width=8, height=16)
    label = _Acc("static", name="Docs folder", width=100, height=16)
    child = _Acc("tree item", name="Report child", width=360, height=18)
    group = _Acc("panel", name="", width=400, height=20)
    parent = _Acc(
        "tree item", name="Docs folder",
        states=("EXPANDABLE", "EXPANDED", "SHOWING"),
        width=400, height=40,
    )
    _adopt(group, child)
    _adopt(parent, bullet, label, group)
    tree = _Acc("tree", name="Files", width=400, height=40 if not sibling else 100)
    nodes = [tree, parent, bullet, label, group, child]
    if sibling:
        other = _Acc("tree item", name="Other folder", states=("EXPANDABLE", "SHOWING"), width=400, height=18)
        _adopt(tree, parent, other)
        nodes.append(other)
        _place_row(parent, 40, 120, 400, 92)
        _place_row(tree, 40, 120, 400, 100)
        _place_row(other, 40, 200, 400, 18)
        _place_row(group, 40, 140, 400, 40)
        _place_row(child, 60, 140, 360, 18)
    else:
        other = None
        _adopt(tree, parent)
        _place_row(tree, 40, 120, 400, 40)
        _place_row(parent, 40, 120, 400, 40)
        _place_row(group, 40, 136, 400, 20)
        _place_row(child, 60, 136, 360, 18)
    _place_row(bullet, 40, 120, 8, 16)
    _place_row(label, 50, 120, 100, 16)
    document = _Acc("document web", name="cuatree331", width=1200, height=680)
    document.component._rect.x = 0.0
    document.component._rect.y = 90.0
    _adopt(document, tree)
    window = _Acc("frame", name="cuatree331", width=1280, height=800)
    _adopt(window, document)
    _own(app, window, document, *nodes)
    _add_tab_strip(window)
    return window, tree, parent, child, other


def test_expanded_treeitem_stays_in_the_snapshot_with_its_child(fake_atspi, monkeypatch) -> None:
    """Synthetic Chromium tree, not a live Chrome window.

    The expanded treeitem is as tall as the tree. The snapshot keeps that
    row, with expanded set, and the child row under it. The inner static
    text is not the row.
    """
    window, _tree, _parent, _child, _other = _expanded_chrome_tree()
    hit = _ListHit([], [])
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    snap = LinuxDriver().snapshot(Scope.WINDOW, "chrome")
    by_ref = {el.ref: el for el in snap.elements}
    folder = next(el for el in snap.elements if el.role == "AXRow" and el.title == "Docs folder")
    assert folder.expanded is True
    child = next(el for el in snap.elements if el.role == "AXRow" and el.title == "Report child")
    ancestor = child
    while ancestor.parent is not None and ancestor.ref != folder.ref:
        ancestor = by_ref[ancestor.parent]
    assert ancestor.ref == folder.ref
    label = next(el for el in snap.elements if el.role == "AXStaticText" and el.title == "Docs folder")
    assert label.parent == folder.ref
    rendered = observe.render_text(snap, mode="interactive")
    assert 'row "Docs folder"' in rendered
    assert "expanded" in rendered.split('row "Docs folder"')[1].splitlines()[0]


def test_expanded_treeitem_is_not_replaced_when_a_sibling_is_visible(fake_atspi, monkeypatch) -> None:
    """Synthetic Chromium tree with two root rows, not a live Chrome window.

    The expanded row is taller than nine tenths of the tree, so the old
    walk opened it and listed its label and its child as the tree's rows.
    Both root rows stay, and the child stays under the expanded row.
    """
    window, tree, parent, child, other = _expanded_chrome_tree(sibling=True)
    live = _atspi._in_view_named_rows(tree)
    assert [node.get_role_name() for node, _pos, _size in live] == ["tree item", "tree item"]
    assert [node.get_name() for node, _pos, _size in live] == ["Docs folder", "Other folder"]
    hit = _ListHit([], [])
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    snap = LinuxDriver().snapshot(Scope.WINDOW, "chrome")
    by_ref = {el.ref: el for el in snap.elements}
    folder = next(el for el in snap.elements if el.role == "AXRow" and el.title == "Docs folder")
    assert folder.expanded is True
    assert any(el.role == "AXRow" and el.title == "Other folder" for el in snap.elements)
    nested = next(el for el in snap.elements if el.role == "AXRow" and el.title == "Report child")
    ancestor = nested
    while ancestor.parent is not None and ancestor.ref != folder.ref:
        ancestor = by_ref[ancestor.parent]
    assert ancestor.ref == folder.ref
    assert parent.name == "Docs folder" and child.name == "Report child" and other.name == "Other folder"


def test_snapshot_lists_layout_rows_not_the_cached_children(fake_atspi, monkeypatch) -> None:
    """Synthetic Chromium hit test, not a live Chrome list.

    The cached children stay ITEM-001. The second hit test at each point is
    ITEM-009 and the rows after it. The snapshot scroll_to_find searches
    lists those layout rows.
    """
    window, listing, hit, _stuck = _chrome_list()
    hit.head = 9
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    snap = LinuxDriver().snapshot(Scope.WINDOW, "chrome")
    titles = _row_titles(snap)
    assert titles[0] == "ITEM-009"
    assert "ITEM-016" in titles
    assert "ITEM-001" not in titles
    assert listing.get_child_at_index(0).name == "ITEM-001"
    assert hit.calls >= 2
    assert any(el.title == "ITEM-012" for el in observe.find_elements(snap, text="ITEM-012"))


def _runtime_for(driver, monkeypatch):
    from a11y_computer_use import server

    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = driver
    runtime._run_gated = lambda _action, _app, execute, **_kwargs: execute()
    runtime._require_permission = lambda *_args, **_kwargs: None
    runtime._recheck_target = lambda *_args, **_kwargs: None
    monkeypatch.setattr(server, "_running_app", lambda _name: (None, "chrome"))
    return runtime


def _page_wheel(monkeypatch, hit, advance):
    """Record wheel points. ``advance`` runs only when the point is on the page.

    A wheel on the tab strip (y < 90) or below the screen (y >= 800) does not
    move this synthetic page. Not a live Chrome wheel.
    """
    points: list[tuple[int, int]] = []
    wired = _linux_input.scroll

    def record(x, y, dx=0, dy=0):
        points.append((int(x), int(y)))
        return wired(x, y, dx=dx, dy=dy)

    monkeypatch.setattr(_linux_input, "scroll", record)

    def on_wheel():
        _x, y = points[-1]
        if y < 90 or y >= 800:
            return
        advance()

    hit.on_wheel = on_wheel
    return points


def test_scroll_to_find_on_a_document_scroll_page_wheels_the_page(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic body-scroll page, not a live Chrome window.

    The list is taller than the window and the tab strip is in the tree.
    scroll_to_find has no ref. The wheel lands on the page, below the tab
    strip and on the screen, and ITEM-180 comes into the snapshot. A wheel
    on the strip would leave the head at ITEM-001.
    """
    from a11y_computer_use import server

    window, listing, hit, rows = _document_scroll_page()
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)

    def advance():
        hit.screen += 8
        hit._at.clear()
        _show_page_rows(listing, rows, hit.screen)

    points = _page_wheel(monkeypatch, hit, advance)
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    first = driver.snapshot(Scope.WINDOW, "chrome")
    anchor = server._scroll_anchor(first)
    assert anchor.role != "AXTabGroup"
    assert anchor.role in ("AXGroup", "AXList")
    assert _row_titles(first)[0] == "ITEM-001"
    assert "ITEM-180" not in _row_titles(first)

    runtime = _runtime_for(driver, monkeypatch)
    out = runtime.scroll_to_find("chrome", text="ITEM-180", max_scrolls=25)
    assert "ITEM-180" in out
    assert "found after" in out
    assert "found after 0 scroll" not in out
    assert points
    assert all(90 <= y < 800 for _x, y in points)


def test_scroll_to_find_wheels_a_content_height_list_on_the_screen(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic body-scroll page with no document group, not a live Chrome window.

    The list's own center is below the screen. The wheel uses the on-screen
    part of that list, so the page moves and ITEM-180 is found. The grab is
    the visible part of the list, not the content-height box.
    """
    window, listing, hit, rows = _document_scroll_page()
    # Drop the document group: the list and the tab strip are the frame's children.
    _adopt(window, listing)
    _add_tab_strip(window)
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)
    boxes: list[tuple] = []
    import a11y_computer_use.drivers.linux as linux_mod

    previous_grab = linux_mod._grab_region

    def grab(box):
        boxes.append(tuple(box))
        return previous_grab(box)

    monkeypatch.setattr(linux_mod, "_grab_region", grab)

    def advance():
        hit.screen += 8
        hit._at.clear()
        _show_page_rows(listing, rows, hit.screen)

    points = _page_wheel(monkeypatch, hit, advance)
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    runtime = _runtime_for(driver, monkeypatch)
    out = runtime.scroll_to_find("chrome", text="ITEM-180", max_scrolls=25)
    assert "ITEM-180" in out
    assert points
    assert all(90 <= y < 800 for _x, y in points)
    assert boxes
    assert all(y + height <= 800 for _x, y, _w, height in boxes)


def test_overflow_list_still_beats_a_tab_strip(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic overflow list, not a live Chrome window.

    The list is a fixed-height box. A tab strip in the same window does not
    take the wheel. ITEM-180 is found.
    """
    from a11y_computer_use import server

    window, _listing, hit, _stuck = _chrome_list()
    _add_tab_strip(window)
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)
    points = _page_wheel(monkeypatch, hit, hit.advance)
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    first = driver.snapshot(Scope.WINDOW, "chrome")
    assert server._scroll_anchor(first).role == "AXList"
    runtime = _runtime_for(driver, monkeypatch)
    out = runtime.scroll_to_find("chrome", text="ITEM-180", max_scrolls=25)
    assert "ITEM-180" in out
    assert points
    # The overflow list sits at y=100, height 160, so its center is y=180.
    assert all(100 <= y <= 260 for _x, y in points)


def test_stuck_overflow_list_with_a_tab_strip_stays_page_unchanged(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic overflow list, not a live Chrome window.

    The wheel lands on the list and the grab does not change. The result is
    page_unchanged and the snapshot head stays ITEM-001. Wheeling the tab
    strip would report not-found instead, because a tab strip is not a list.
    """
    window, listing, hit, _stuck = _chrome_list(stuck=True)
    _add_tab_strip(window)
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    runtime = _runtime_for(driver, monkeypatch)
    with pytest.raises(ComputerUseError) as error:
        runtime.scroll_to_find("chrome", text="ITEM-180", max_scrolls=6)
    assert error.value.detail["reason"] == "page_unchanged"
    later = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert later[0] == "ITEM-001"
    assert "ITEM-180" not in later
    assert listing.get_child_at_index(0).name == "ITEM-001"


def test_wheel_point_of_a_content_height_list_stays_on_the_screen(monkeypatch) -> None:
    """The geometric center of a content-height list is below the screen.

    The line-scroll point is the center of the on-screen part. An overflow
    list whose center is already on the screen keeps that center. Synthetic
    bounds, not a live wheel.
    """
    from a11y_computer_use.drivers.linux import _wheel_point
    from a11y_computer_use.schema import Bounds, Element

    monkeypatch.setattr(_atspi, "_screen_size", lambda: (1280, 800))
    tall = Element("e1", "AXList", "items", None, Bounds(0, 40, 120, 1100, 6400), "s")
    x, y = _wheel_point(tall)
    assert 0 <= x < 1280
    assert 120 <= y < 800
    overflow = Element("e2", "AXList", "items", None, Bounds(0, 40, 100, 400, 160), "s")
    assert _wheel_point(overflow) == (240, 180)
    # This helper still returns that center. The 0.4.22 overflow list was
    # not moved by a wheel inside its box.
    low = Element("e3", "AXList", "items", None, Bounds(0, 16, 600, 1239, 422), "s")
    assert _wheel_point(low) == (16 + 1239 // 2, 600 + 422 // 2)


def _lay_out_scrolled_body(listing: _Acc, rows: list[_Acc], head: int) -> None:
    """Park the first rows at the content origin and paint ``head`` on screen.

    Synthetic boxes, not a live Chrome bounds read. ``head`` is 1-based.
    While the origin is on screen the parked rows are the visible rows.
    Once the origin is above the screen, eight rows from ``head`` sit at
    y=100, inside the document (the document fixture starts at y=90). Three
    rows sit in the browser chrome above that document, at y=0, 30, and 60.
    Those rows are on the screen and are not painted. The 0.4.21 retest's
    snapshot head was those rows (ITEM-168, ITEM-174) rather than the first
    full row.
    """
    origin = 120 - (head - 1) * 28
    comp = listing.component
    if hasattr(comp, "_rect"):
        rect = comp._rect
        rect.x, rect.y, rect.width, rect.height = 40.0, float(origin), 1100.0, 6400.0
    else:
        comp.x, comp.y, comp.width, comp.height = 40, origin, 1100, 6400
    parked = []
    for index, row in enumerate(rows[:16]):
        _place_row(row, 40, origin + index * 28, 1100, 24)
        parked.append(row)
    if origin >= 0:
        _adopt(listing, *parked)
        return
    visible = []
    for index, row in enumerate(rows[head - 1:head - 1 + 8]):
        _place_row(row, 40, 100 + index * 28, 1100, 24)
        visible.append(row)
    chrome = []
    if head > 20:
        for index, row in enumerate(rows[head - 4:head - 1]):
            _place_row(row, 40, index * 30, 1100, 24)
            chrome.append(row)
    _adopt(listing, *parked, *chrome, *visible)


def test_snapshot_keeps_rows_painted_in_the_window(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic body-scroll list, not a live Chrome window.

    The list origin is about y=-4808, which is the 0.4.20 retest's shape
    (the list was reported near y=-4847). ITEM-177 through ITEM-184 are
    inside the window, including ITEM-180. Sixteen rows at the content
    origin are not. The snapshot lists the painted rows.
    """
    window, listing, hit, rows = _document_scroll_page()
    _lay_out_scrolled_body(listing, rows, 177)
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    snap = LinuxDriver().snapshot(Scope.WINDOW, "chrome")
    titles = _row_titles(snap)
    assert titles[0] == "ITEM-177"
    assert "ITEM-174" not in titles
    assert "ITEM-175" not in titles
    assert "ITEM-176" not in titles
    assert "ITEM-180" in titles
    assert "ITEM-184" in titles
    assert "ITEM-001" not in titles
    listed = next(el for el in snap.elements if el.role == "AXList")
    assert listed.bounds.y < 0


def test_scroll_to_find_finds_a_row_once_it_is_painted(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic body-scroll page, not a live Chrome window.

    scroll_to_find has no ref. The wheel moves the page. When ITEM-180 is
    inside the window the snapshot contains it, so the tool does not return
    not-found while that row is visible.
    """
    window, listing, hit, rows = _document_scroll_page()
    _lay_out_scrolled_body(listing, rows, 1)
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)
    head = {"n": 1}

    def advance():
        head["n"] = min(177, head["n"] + 8)
        _lay_out_scrolled_body(listing, rows, head["n"])

    points = _page_wheel(monkeypatch, hit, advance)
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    first = driver.snapshot(Scope.WINDOW, "chrome")
    assert _row_titles(first)[0] == "ITEM-001"
    assert "ITEM-180" not in _row_titles(first)
    runtime = _runtime_for(driver, monkeypatch)
    out = runtime.scroll_to_find("chrome", text="ITEM-180", max_scrolls=30)
    assert "ITEM-180" in out
    assert "found after" in out
    assert "found after 0 scroll" not in out
    assert "not found" not in out
    assert points
    assert all(90 <= y < 800 for _x, y in points)
    assert head["n"] >= 177
    painted = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert painted[0] == "ITEM-177"
    assert "ITEM-174" not in painted
    assert "ITEM-180" in painted


def _layout_overflow(listing: _Acc, rows: list[_Acc], hit: _ListHit, head: int) -> None:
    """Place every row. ``head`` (1-based) sits at y=144, pitch 28.

    The list box is (20, 139) 1239 by 422, the 0.4.22 overflow retest.
    Rows below that box stay in the tree so a later row can be revealed.
    Synthetic boxes, not a live Chrome bounds read.
    """
    hit.screen = head
    hit._at.clear()
    for index, row in enumerate(rows):
        _place_row(row, 29, 144 + (index - (head - 1)) * 28, 184, 19)
    _adopt(listing, *rows)


def test_overflow_list_reaches_item_180_when_the_wheel_does_not_move(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic overflow list, not a live Chrome window.

    The anchor is the overflow AXList, 1239 by 422 at (20, 139), not the
    tab strip. The wheel does not change that box: the 0.4.22 retest stayed
    on ITEM-001 with mean_abs 0 whichever point inside the box was used.
    Revealing a later row at the top of the list moves it. ``scroll_to_find``
    ITEM-180 must not return ``page_unchanged`` while the painted head is
    still ITEM-001, and it must not need the wheel to do it.
    """
    from a11y_computer_use import server

    window, listing, hit, _stuck = _chrome_list()
    rows = hit.rows
    hit.x, hit.y, hit.width, hit.height = 20, 139, 1239, 422
    app = window.get_application()
    document = _Acc("document web", name="Bench", width=1271, height=709)
    document.component._rect.x = 4.0
    document.component._rect.y = 86.0
    document.get_application = lambda: app
    _adopt(document, listing)
    _adopt(window, document)
    _add_tab_strip(window)
    _layout_overflow(listing, rows, hit, 1)
    wheels = {"n": 0}

    def reveal(row, _scroll_type):
        number = int(row.name.split("-")[1])
        _layout_overflow(listing, rows, hit, number)
        return True

    for row in rows:
        row.component.scroll_to = lambda scroll_type, row=row: reveal(row, scroll_type)
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)

    def on_wheel():
        wheels["n"] += 1

    hit.on_wheel = on_wheel
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    first = driver.snapshot(Scope.WINDOW, "chrome")
    anchor = server._scroll_anchor(first)
    assert anchor.role == "AXList"
    assert (anchor.bounds.x, anchor.bounds.y) == (20, 139)
    assert (anchor.bounds.width, anchor.bounds.height) == (1239, 422)
    assert _row_titles(first)[0] == "ITEM-001"
    assert "ITEM-180" not in _row_titles(first)
    runtime = _runtime_for(driver, monkeypatch)
    out = runtime.scroll_to_find("chrome", text="ITEM-180", max_scrolls=40)
    assert "ITEM-180" in out
    assert "found after" in out
    assert "page_unchanged" not in out
    assert wheels["n"] == 0
    painted = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert "ITEM-180" in painted
    assert painted[0] != "ITEM-001"


def _overflow_page():
    """Fixed-height overflow list, 1239 by 422 at (20, 139). Synthetic."""
    window, listing, hit, _stuck = _chrome_list()
    rows = hit.rows
    hit.x, hit.y, hit.width, hit.height = 20, 139, 1239, 422
    app = window.get_application()
    document = _Acc("document web", name="Bench", width=1271, height=709)
    document.component._rect.x = 4.0
    document.component._rect.y = 86.0
    document.get_application = lambda: app
    for row in rows:
        row.get_application = lambda: app
    _adopt(document, listing)
    _adopt(window, document)
    _add_tab_strip(window)
    _layout_overflow(listing, rows, hit, 1)
    return window, document, listing, hit, rows


def _keep_overflow_bar(listing, rows, hit, bar, head: int) -> None:
    """Place ``head`` and put ``bar`` back when it is a child of the list.

    ``_layout_overflow`` replaces the list's children. A sibling bar stays
    on the document. Synthetic boxes, not a live Chrome tree.
    """
    parent = bar.parent
    _layout_overflow(listing, rows, hit, head)
    if parent is listing:
        listing.children.append(bar)
        bar.parent = listing


def _bind_overflow_bar(bar, listing, rows, hit, *, jump: bool = False) -> list[float]:
    """Map a bar write onto the row layout. Returns the values written."""
    writes: list[float] = []

    def on_value(acc, new):
        writes.append(float(new))
        if jump and float(new) > acc.value + 1e-9:
            acc.value = acc.maximum
        else:
            upper = min(acc.maximum, acc.visual_max)
            acc.value = min(max(float(new), acc.minimum), upper)
        span = acc.maximum - acc.minimum
        if span <= 2:
            frac = 0.0 if span <= 0 else (acc.value - acc.minimum) / span
            head = 1 + int(round(frac * 199))
        else:
            head = 1 + int(round(acc.value / 28))
        _keep_overflow_bar(listing, rows, hit, bar, max(1, min(186, head)))
        return True

    bar.on_value = on_value
    return writes


def _overflow_bar(*, minimum=0.0, maximum=5572.0, value=0.0) -> _Acc:
    bar = _Acc(
        "scroll bar", states=("VERTICAL",), value=value, minimum=minimum, maximum=maximum,
        width=14, height=422,
    )
    bar.component._rect.x = 1240.0
    bar.component._rect.y = 139.0
    return bar


def test_overflow_list_steps_its_scroll_bar_when_scroll_to_and_the_wheel_do_not_move(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic overflow list, not a live Chrome window.

    The 0.4.23 retest: anchor AXList 1239 by 422 at (20, 139), wheel
    mean_abs 0, ``scroll_to`` returning true did not paint ITEM-180, and
    ``scroll_to_find`` ended on ``page_unchanged`` with the head still
    ITEM-001. Here ``scroll_to`` returns true and does not move the rows,
    and the wheel does not either. The vertical bar is the last of the 200
    rows, which the pixel-scroll walk does not read. Writing that bar by
    five content rows per five lines paints a later row. ITEM-180 is found, the
    wheel is not sent, and ``scroll_to`` is not called.
    """
    from a11y_computer_use import server

    window, _document, listing, hit, rows = _overflow_page()
    bar = _overflow_bar()
    listing.children.append(bar)
    bar.parent = listing
    writes = _bind_overflow_bar(bar, listing, rows, hit)
    calls = {"scroll_to": 0}

    def scroll_to(_scroll_type, row=None):
        calls["scroll_to"] += 1
        return True

    for row in rows:
        row.component.scroll_to = lambda scroll_type, row=row: scroll_to(scroll_type, row)
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    wheels = {"n": 0}
    wired_scroll = _linux_input.scroll

    def counting(x, y, dx=0, dy=0):
        wheels["n"] += 1
        return wired_scroll(x, y, dx=dx, dy=dy)

    monkeypatch.setattr(_linux_input, "scroll", counting)

    def _fail_pixel_walk(*_args, **_kwargs):
        raise AssertionError("line scroll must not use the pixel-scroll bar walk")

    monkeypatch.setattr(_atspi, "_collect_scrollbars", _fail_pixel_walk)
    monkeypatch.setattr(_atspi, "_nudge_scrollbar", _fail_pixel_walk)
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    first = driver.snapshot(Scope.WINDOW, "chrome")
    anchor = server._scroll_anchor(first)
    assert anchor.role == "AXList"
    assert (anchor.bounds.x, anchor.bounds.y) == (20, 139)
    assert (anchor.bounds.width, anchor.bounds.height) == (1239, 422)
    assert _row_titles(first)[0] == "ITEM-001"
    assert "ITEM-180" not in _row_titles(first)
    runtime = _runtime_for(driver, monkeypatch)
    out = runtime.scroll_to_find("chrome", text="ITEM-180", max_scrolls=40)
    assert "ITEM-180" in out
    assert "found after" in out
    assert "page_unchanged" not in out
    assert calls["scroll_to"] == 0
    assert wheels["n"] == 0
    painted = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert "ITEM-180" in painted
    assert painted[0] != "ITEM-001"
    assert writes
    assert max(writes) < bar.maximum
    steps = [writes[0]] + [writes[i] - writes[i - 1] for i in range(1, len(writes))]
    assert max(steps) < 500


def test_overflow_track_click_moves_painted_rows_when_the_bar_value_does_not(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic overflow list, not a live Chrome window.

    The 0.4.24 retest stayed on ITEM-001. Writing the vertical bar's value
    changes the value and does not move the painted rows. ``scroll_to``
    returns true and does not move them either. The wheel does not move
    them. A left click on the vertical track does: the painted head leaves
    ITEM-001 and ``scroll_to_find`` ITEM-180 finds that row. The value
    write is undone. No wheel is sent.
    """
    from a11y_computer_use import server

    window, _document, listing, hit, rows = _overflow_page()
    bar = _overflow_bar(maximum=1.0)
    listing.children.append(bar)
    bar.parent = listing
    writes: list[float] = []

    def on_value(acc, new):
        writes.append(float(new))
        upper = min(acc.maximum, acc.visual_max)
        acc.value = min(max(float(new), acc.minimum), upper)
        return True

    bar.on_value = on_value
    calls = {"scroll_to": 0}

    def scroll_to(_scroll_type, row=None):
        calls["scroll_to"] += 1
        return True

    for row in rows:
        row.component.scroll_to = lambda scroll_type, row=row: scroll_to(scroll_type, row)
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    wheels = {"n": 0}
    clicks: list[tuple[int, int]] = []
    wired_scroll = _linux_input.scroll
    wired_click = _linux_input.click

    def counting(x, y, dx=0, dy=0):
        wheels["n"] += 1
        return wired_scroll(x, y, dx=dx, dy=dy)

    def clicking(x, y, button="left", count=1):
        clicks.append((int(x), int(y)))
        wired_click(x, y, button=button, count=count)
        # Only a point on the track moves the painted rows. A click on the
        # text does not. One page keeps an overlap with the rows on screen.
        if int(x) >= 1230:
            head = min(186, int(hit.screen) + 14)
            _keep_overflow_bar(listing, rows, hit, bar, head)

    monkeypatch.setattr(_linux_input, "scroll", counting)
    monkeypatch.setattr(_linux_input, "click", clicking)
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    first = driver.snapshot(Scope.WINDOW, "chrome")
    anchor = server._scroll_anchor(first)
    assert anchor.role == "AXList"
    assert (anchor.bounds.x, anchor.bounds.y) == (20, 139)
    assert (anchor.bounds.width, anchor.bounds.height) == (1239, 422)
    assert _row_titles(first)[0] == "ITEM-001"
    runtime = _runtime_for(driver, monkeypatch)
    out = runtime.scroll_to_find("chrome", text="ITEM-180", max_scrolls=30)
    assert "ITEM-180" in out
    assert "found after" in out
    assert "page_unchanged" not in out
    assert wheels["n"] == 0
    assert calls["scroll_to"] == 0
    assert writes
    assert bar.value == 0
    assert clicks
    assert all(x >= 1230 for x, _y in clicks)
    assert all(139 <= y <= 561 for _x, y in clicks)
    painted = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert painted[0] != "ITEM-001"
    assert "ITEM-180" in painted


def test_overflow_fractional_bar_reaches_item_180_without_jumping(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic overflow list, not a live Chrome window.

    The bar's range is 0 to 1 and it is a sibling of the list, lined up
    with the box. A step is a fraction of the rows' extent, not a clamp to
    1. ``scroll_to`` does not move the rows and the wheel is not sent.
    ITEM-180 is found while the bar is still below the end.
    """
    window, document, listing, hit, rows = _overflow_page()
    bar = _overflow_bar(maximum=1.0)
    document.children.append(bar)
    bar.parent = document
    writes = _bind_overflow_bar(bar, listing, rows, hit)
    for row in rows:
        row.component.scroll_to = lambda _scroll_type: True
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    wheels = {"n": 0}
    wired_scroll = _linux_input.scroll

    def counting(x, y, dx=0, dy=0):
        wheels["n"] += 1
        return wired_scroll(x, y, dx=dx, dy=dy)

    monkeypatch.setattr(_linux_input, "scroll", counting)
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    runtime = _runtime_for(driver, monkeypatch)
    out = runtime.scroll_to_find("chrome", text="ITEM-180", max_scrolls=40)
    assert "ITEM-180" in out
    assert "page_unchanged" not in out
    assert wheels["n"] == 0
    assert writes
    assert max(writes) < 0.95
    assert bar.value < 0.95
    painted = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert painted[0] != "ITEM-001"
    assert "ITEM-180" in painted


def test_overflow_scroll_bar_jump_is_not_a_line_scroll(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic overflow list, not a live Chrome window.

    A 0-to-1 bar that clamps a small write to the end is undone. The list
    stays on ITEM-001. ``scroll_to`` is not used to paper over that jump,
    and the stuck wheel is ``page_unchanged``.
    """
    window, document, listing, hit, rows = _overflow_page()
    bar = _overflow_bar(maximum=1.0)
    document.children.append(bar)
    bar.parent = document
    calls = {"scroll_to": 0}
    _bind_overflow_bar(bar, listing, rows, hit, jump=True)

    def scroll_to(_scroll_type):
        calls["scroll_to"] += 1
        return True

    for row in rows:
        row.component.scroll_to = scroll_to
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    listing_el = next(
        el for el in driver.snapshot(Scope.WINDOW, "chrome").elements if el.role == "AXList"
    )
    with pytest.raises(ComputerUseError) as error:
        driver.scroll(listing_el, dy=5, unit=ScrollUnit.LINES)
    assert error.value.detail["reason"] == "page_unchanged"
    assert error.value.detail["mean_abs"] == 0
    assert error.value.detail["rows"][0] == "ITEM-001"
    assert calls["scroll_to"] == 0
    assert bar.value == 0
    assert _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))[0] == "ITEM-001"


def test_overflow_bar_at_rest_does_not_scroll_up(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic overflow list, not a live Chrome window.

    The bar is already at its minimum and the painted head is ITEM-001.
    An upward line does not write the bar, does not follow ``scroll_to``,
    and stays ``page_unchanged``.
    """
    window, _document, listing, hit, rows = _overflow_page()
    bar = _overflow_bar()
    listing.children.append(bar)
    bar.parent = listing
    calls = {"scroll_to": 0}
    _bind_overflow_bar(bar, listing, rows, hit)

    def scroll_to(_scroll_type):
        calls["scroll_to"] += 1
        _keep_overflow_bar(listing, rows, hit, bar, 50)
        return True

    for row in rows:
        row.component.scroll_to = scroll_to
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    listing_el = next(
        el for el in driver.snapshot(Scope.WINDOW, "chrome").elements if el.role == "AXList"
    )
    with pytest.raises(ComputerUseError) as error:
        driver.scroll(listing_el, dy=-1, unit=ScrollUnit.LINES)
    assert error.value.detail["reason"] == "page_unchanged"
    assert error.value.detail["mean_abs"] == 0
    assert error.value.detail["dy"] == -1
    assert error.value.detail["rows"][0] == "ITEM-001"
    assert calls["scroll_to"] == 0
    assert bar.value == 0
    assert _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))[0] == "ITEM-001"


def test_page_unchanged_requires_an_unmoved_scroll_offset() -> None:
    """The offset comparison. Not a live scroll."""
    from a11y_computer_use.drivers.linux import _offset_did_not_move

    assert _offset_did_not_move(4.0, 4.2) is True
    assert _offset_did_not_move(4.0, 30.0) is False
    assert _offset_did_not_move(None, 4.0) is None
    assert _offset_did_not_move(4.0, None) is None


def test_short_table_past_the_document_wheels_the_page(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic body-scroll table that fits on the screen, not a live window.

    The list is shorter than the screen and still extends past the document,
    which is the short ticket table. The scroll uses the document wheel. It
    does not write a viewport bar.
    """
    import a11y_computer_use.drivers.linux as linux_mod

    window, listing, hit, rows = _document_scroll_page()
    # Document is y=90, height 680, bottom 770. The list stays inside the
    # 800px screen and runs past that document.
    hit.x, hit.y, hit.width, hit.height = 40, 120, 400, 670
    _show_page_rows(listing, rows, 1)
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)
    called = {"wheel": 0, "nudge": 0}

    def wheel(*_args, **_kwargs):
        called["wheel"] += 1

    def nudge(*_args, **_kwargs):
        called["nudge"] += 1
        return "absent", None

    monkeypatch.setattr(linux_mod, "_wheel_chromium_document", wheel)
    monkeypatch.setattr(_atspi, "nudge_viewport_scrollbar", nudge)
    driver = LinuxDriver()
    snap = driver.snapshot(Scope.WINDOW, "chrome")
    listing_el = next(el for el in snap.elements if el.role == "AXList")
    assert driver.scroll(listing_el, dy=5, unit=ScrollUnit.LINES) is None
    assert called["wheel"] == 1
    assert called["nudge"] == 0
    assert _atspi.scroll_container_kind(listing) == "document"
    _overflow_window, overflow, _overflow_hit, _overflow_stuck = _chrome_list()
    assert _atspi.scroll_container_kind(overflow) == "viewport"
    # After the document moves, the list's top is above the page and the
    # box no longer sits on the screen. It is still body content.
    hit.y = -22
    assert driver.scroll(listing_el, dy=5, unit=ScrollUnit.LINES) is None
    assert called["wheel"] == 2
    assert called["nudge"] == 0
    # Live Chrome on an 800px screen publishes this 25-row table at about
    # 857px. That is still the document. A list more than twice the screen
    # stays a viewport.
    hit.y, hit.height = 120, 857
    assert _atspi.scroll_container_kind(listing) == "document"
    assert driver.scroll(listing_el, dy=5, unit=ScrollUnit.LINES) is None
    assert called["wheel"] == 3
    assert called["nudge"] == 0
    hit.height = 1601
    assert _atspi.scroll_container_kind(listing) == "viewport"


def test_content_height_line_scroll_does_not_write_the_scroll_bar(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic content-height list, not a live Chrome window.

    The list is taller than the screen, which is the body-scroll case.
    A line scroll keeps the wheel. It does not write the list's bar.
    The snapshot head is the painted row.
    """
    window, listing, hit, rows = _document_scroll_page()
    bar = _Acc(
        "scroll bar", states=("VERTICAL",), value=10, minimum=0, maximum=8000,
        width=14, height=400,
    )
    bar.component._rect.x = 1140.0
    bar.component._rect.y = 90.0
    listing.children.append(bar)
    bar.parent = listing
    _show_page_rows(listing, rows, 1)
    listing.children.append(bar)
    bar.parent = listing
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)

    def on_wheel():
        hit.screen = 12
        hit.x, hit.y, hit.width, hit.height = 40, -200, 1100, 6400
        for index, row in enumerate(rows[8:11]):
            _place_row(row, 40, index * 30, 1100, 24)
        visible = []
        for index, row in enumerate(rows[11:19]):
            _place_row(row, 40, 90 + index * 28, 1100, 24)
            visible.append(row)
        _adopt(listing, *rows[8:11], *visible)
        listing.children.append(bar)
        bar.parent = listing

    hit.on_wheel = on_wheel
    clicks = {"n": 0}
    wired_click = _linux_input.click

    def counting_click(x, y, button="left", count=1):
        clicks["n"] += 1
        return wired_click(x, y, button=button, count=count)

    monkeypatch.setattr(_linux_input, "click", counting_click)
    driver = LinuxDriver()
    snap = driver.snapshot(Scope.WINDOW, "chrome")
    assert _row_titles(snap)[0] == "ITEM-001"
    listing_el = next(el for el in snap.elements if el.role == "AXList")
    assert driver.scroll(listing_el, dy=3, unit=ScrollUnit.LINES) is None
    assert bar.value == 10
    assert clicks["n"] == 0
    later = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert later[0] == "ITEM-012"
    assert "ITEM-009" not in later


def test_overflow_list_at_the_top_stays_page_unchanged_when_nothing_moves(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic overflow list, not a live Chrome window.

    An upward line from ITEM-001 has no row to reveal, and the wheel does
    not move the box. The result is ``page_unchanged`` and the head stays
    ITEM-001. A downward search that does move is the previous test.
    """
    window, listing, hit, _stuck = _chrome_list()
    rows = hit.rows
    hit.x, hit.y, hit.width, hit.height = 20, 139, 1239, 422
    app = window.get_application()
    for row in rows:
        row.get_application = lambda: app
    _layout_overflow(listing, rows, hit, 1)
    for row in rows:
        row.component.scroll_to = lambda _scroll_type: True
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    listing_el = next(
        el for el in driver.snapshot(Scope.WINDOW, "chrome").elements if el.role == "AXList"
    )
    with pytest.raises(ComputerUseError) as error:
        driver.scroll(listing_el, dy=-1, unit=ScrollUnit.LINES)
    assert error.value.detail["reason"] == "page_unchanged"
    assert error.value.detail["mean_abs"] == 0
    assert error.value.detail["dy"] == -1
    assert error.value.detail["rows"][0] == "ITEM-001"
    assert _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))[0] == "ITEM-001"


def test_explicit_scroll_head_is_the_painted_row_not_the_chrome_band(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic content-height list, not a live Chrome window.

    A 3-line wheel moves the pixels from ITEM-001 to ITEM-012. Nothing is
    clipped above ITEM-012. ITEM-009, ITEM-010, and ITEM-011 sit in the
    browser chrome, above the document and below the screen top. The scroll
    is a success, not ``rows_stale``, and the snapshot head is ITEM-012.
    """
    window, listing, hit, rows = _document_scroll_page()
    _show_page_rows(listing, rows, 1)
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)

    def on_wheel():
        hit.screen = 12
        hit.x, hit.y, hit.width, hit.height = 40, -200, 1100, 6400
        for index, row in enumerate(rows[8:11]):
            _place_row(row, 40, index * 30, 1100, 24)
        visible = []
        for index, row in enumerate(rows[11:19]):
            _place_row(row, 40, 90 + index * 28, 1100, 24)
            visible.append(row)
        _adopt(listing, *rows[8:11], *visible)

    hit.on_wheel = on_wheel
    driver = LinuxDriver()
    snap = driver.snapshot(Scope.WINDOW, "chrome")
    assert _row_titles(snap)[0] == "ITEM-001"
    listing_el = next(el for el in snap.elements if el.role == "AXList")
    assert driver.scroll(listing_el, dy=3, unit=ScrollUnit.LINES) is None
    later = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert later[0] == "ITEM-012"
    assert "ITEM-009" not in later
    assert "ITEM-010" not in later
    assert "ITEM-011" not in later
    assert "ITEM-001" not in later


def test_explicit_list_scroll_head_matches_the_painted_row(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic content-height list, not a live Chrome window.

    A 3-line wheel moves the pixels. ITEM-009 starts above the screen.
    ITEM-012 is flush with the screen and is the painted head. The scroll
    is not ``rows_stale``, and the snapshot head is ITEM-012.
    """
    window, listing, hit, _stuck = _chrome_list()
    for index, row in enumerate(hit.rows[:8]):
        _place_row(row, 40, 100 + index * 20, 400, 20)
    _adopt(listing, *hit.rows[:8])
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)

    def on_wheel():
        hit.screen = 12
        hit.y = -40
        hit.height = 6400
        hit.width = 1100
        _place_row(hit.rows[8], 40, -12, 1100, 20)
        visible = []
        for index, row in enumerate(hit.rows[11:19]):
            _place_row(row, 40, index * 20, 1100, 20)
            visible.append(row)
        _adopt(listing, hit.rows[8], *visible)

    hit.on_wheel = on_wheel
    driver = LinuxDriver()
    snap = driver.snapshot(Scope.WINDOW, "chrome")
    assert _row_titles(snap)[0] == "ITEM-001"
    listing_el = next(el for el in snap.elements if el.role == "AXList")
    assert driver.scroll(listing_el, dy=3, unit=ScrollUnit.LINES) is None
    later = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert later[0] == "ITEM-012"
    assert "ITEM-009" not in later
    assert "ITEM-001" not in later


def test_still_page_reports_the_rows_on_screen(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic content-height list, not a live Chrome window.

    The screen is ITEM-177 through ITEM-184 and the wheel does not move it.
    Rows saved when the list origin was y=120 must not be the error text or
    the snapshot. The result is ``page_unchanged`` and the head stays the
    painted row.
    """
    window, listing, hit, rows = _document_scroll_page()
    _lay_out_scrolled_body(listing, rows, 177)
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)
    hit.on_wheel = lambda: None
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    snap = driver.snapshot(Scope.WINDOW, "chrome")
    assert _row_titles(snap)[0] == "ITEM-177"
    assert "ITEM-180" in _row_titles(snap)
    saved = [_Acc("list item", name=f"ITEM-{i:03d}") for i in range(1, 9)]
    _atspi._SHOWN.clear()
    _atspi._SHOWN[("list", "items", 40, 120, 1100, 6400)] = [
        (acc, (40.0, float(120 + index * 20)), (1100.0, 20.0))
        for index, acc in enumerate(saved)
    ]
    listing_el = next(el for el in snap.elements if el.role == "AXList")
    with pytest.raises(ComputerUseError) as error:
        driver.scroll(listing_el, dy=3, unit=ScrollUnit.LINES)
    assert error.value.detail["reason"] == "page_unchanged"
    assert error.value.detail["mean_abs"] == 0
    assert error.value.detail["rows"][0] == "ITEM-177"
    assert "ITEM-174" not in error.value.detail["rows"]
    assert "ITEM-175" not in error.value.detail["rows"]
    assert "ITEM-176" not in error.value.detail["rows"]
    assert "ITEM-001" not in error.value.detail["rows"]
    later = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert later[0] == "ITEM-177"
    assert "ITEM-174" not in later
    assert "ITEM-180" in later
    assert "ITEM-001" not in later
    assert _atspi.row_head(_atspi.saved_rows(listing)) == "ITEM-177"


def test_scroll_to_find_reaches_item_180_from_layout_rows(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Runtime.scroll_to_find against this driver. Synthetic hit test, not a live list.

    The cached children stay on ITEM-001 for the whole search. Each wheel moves
    the layout window. The snapshot after that wheel is what the find searches,
    and it contains ITEM-180 before max_scrolls runs out.
    """
    from a11y_computer_use import server

    events, _display = xtest_recorder
    window, listing, hit, _stuck = _chrome_list()
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = driver
    runtime._run_gated = lambda _action, _app, execute, **_kwargs: execute()
    runtime._require_permission = lambda *_args, **_kwargs: None
    runtime._recheck_target = lambda *_args, **_kwargs: None
    monkeypatch.setattr(server, "_running_app", lambda _name: (None, "chrome"))

    out = runtime.scroll_to_find("chrome", text="ITEM-180", max_scrolls=25)
    assert "ITEM-180" in out
    assert "found after" in out
    assert "found after 0 scroll" not in out
    assert listing.get_child_at_index(0).name == "ITEM-001"
    assert listing.children[-1].name == "ITEM-008"
    assert events[0][0] == _X_MOTION
    assert (_X_BPRESS, 5, 0, 0) in [(kind, detail, x, y) for kind, detail, x, y in events]


def test_scroll_to_find_comes_back_after_the_page_stops_past_the_target(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic Chromium list, not a live Chrome window.

    A 5-line step jumps the window by 40 rows, from ITEM-001 to ITEM-193,
    and never shows ITEM-180. The next wheel does not change the grab, so
    the driver raises page_unchanged and does not install a new head. That
    still page is repeated once. The search then steps back five lines at
    a time (four rows per step in this fake) until the window contains
    ITEM-180. This does not prove the live Chrome list.
    """
    from a11y_computer_use import server

    window, listing, hit, _stuck = _chrome_list()
    hit.steps = []
    _adopt(listing, *_positioned_window(hit.rows, 1))
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)
    _record_wheel_dy(monkeypatch, hit)

    def on_wheel():
        dy = int(hit.last_dy)
        hit.steps.append(dy)
        if dy > 0 and hit.screen >= 193:
            return
        if dy < 0 and hit.screen <= 1:
            return
        if dy > 0:
            hit.screen = min(193, hit.screen + 40)
        elif dy < 0:
            hit.screen = max(1, hit.screen - 4)
        else:
            return
        hit.invent = None
        hit.force_row = None
        hit.hold_until = 0
        hit._at.clear()
        _adopt(listing, *_positioned_window(hit.rows, hit.screen))

    hit.on_wheel = on_wheel
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = driver
    runtime._run_gated = lambda _action, _app, execute, **_kwargs: execute()
    runtime._require_permission = lambda *_args, **_kwargs: None
    runtime._recheck_target = lambda *_args, **_kwargs: None
    monkeypatch.setattr(server, "_running_app", lambda _name: (None, "chrome"))

    out = runtime.scroll_to_find("chrome", text="ITEM-180", max_scrolls=6)
    assert "ITEM-180" in out
    assert "found after 11 scroll(s)" in out
    assert "found after 0 scroll" not in out
    assert hit.steps == [5, 5, 5, 5, 5, 5, 5, -5, -5, -5, -5]
    assert -1 not in hit.steps
    titles = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert "ITEM-180" in titles
    assert titles[0] == "ITEM-177"


def test_scroll_to_find_stops_when_the_chrome_list_will_not_move(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic Chromium list, not a live Chrome window.

    The window is ITEM-193 through ITEM-200. ITEM-180 is not in it. Neither
    direction changes the grab. Each direction is page_unchanged twice,
    and the search stops on the second return. The snapshot head stays ITEM-193.
    A hit test that names ITEM-192 is not installed. This does not prove
    the live Chrome list.
    """
    from a11y_computer_use import server

    window, listing, hit, _stuck = _chrome_list()
    hit.screen = 193
    hit.steps = []
    _adopt(listing, *_positioned_window(hit.rows, 193))
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)
    _record_wheel_dy(monkeypatch, hit)

    def on_wheel():
        hit.steps.append(int(hit.last_dy))
        hit.invent = 192
        hit.force_row = _overlapping_named("ITEM-192")
        hit._at.clear()

    hit.on_wheel = on_wheel
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = driver
    runtime._run_gated = lambda _action, _app, execute, **_kwargs: execute()
    runtime._require_permission = lambda *_args, **_kwargs: None
    runtime._recheck_target = lambda *_args, **_kwargs: None
    monkeypatch.setattr(server, "_running_app", lambda _name: (None, "chrome"))

    with pytest.raises(ComputerUseError) as error:
        runtime.scroll_to_find("chrome", text="ITEM-180", max_scrolls=6)
    assert error.value.code is ErrorCode.UNSUPPORTED
    assert error.value.detail["reason"] == "page_unchanged"
    assert error.value.detail["mean_abs"] == 0
    assert error.value.detail["dy"] == -5
    assert hit.steps == [5, 5, -5, -5]
    kept = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert kept[0] == "ITEM-193"
    assert "ITEM-200" in kept
    assert "ITEM-180" not in kept
    assert "ITEM-192" not in kept


def test_three_line_scroll_snapshot_starts_at_the_on_screen_head(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic Chromium list, not a live Chrome window.

    A 3-line wheel moves the pixels from ITEM-001 to ITEM-010. The hit test
    keeps answering ITEM-001 across two polls, and a point in the top 8px
    answers ITEM-009 once that hold ends. The snapshot read after the scroll
    returns starts at ITEM-010. A later hit test that names ITEM-192 does not
    replace that head.
    """
    events, _display = xtest_recorder
    window, listing, hit, _stuck = _chrome_list()
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)

    def on_wheel():
        hit.screen = 10
        hit.hold_until = hit.clock + 2
        hit.held_head = 1
        hit.clip = True
        hit.invent = None
        hit._at.clear()

    hit.on_wheel = on_wheel
    driver = LinuxDriver()
    snap = driver.snapshot(Scope.WINDOW, "chrome")
    assert _row_titles(snap)[0] == "ITEM-001"
    row = next(el for el in snap.elements if el.role == "AXRow" and el.title == "ITEM-001")
    assert driver.scroll(row, dy=3, unit=ScrollUnit.LINES) is None
    later = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert later[0] == "ITEM-010"
    assert "ITEM-017" in later
    assert "ITEM-001" not in later
    assert "ITEM-009" not in later
    assert listing.get_child_at_index(0).name == "ITEM-001"
    presses = [event for event in events if event[0] == _X_BPRESS and event[1] == 5]
    assert len(presses) == 3
    hit.hold_until = 0
    hit._at.clear()
    hit.get_accessible_at_point(hit.x + 10, hit.y + 1, 0)
    clipped = hit.get_accessible_at_point(hit.x + 10, hit.y + 1, 0)
    assert clipped.name == "ITEM-009"
    hit.invent = 192
    hit._at.clear()
    again = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert again[0] == "ITEM-010"
    assert "ITEM-192" not in again


def test_late_paint_is_not_reported_as_an_unmoved_page(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic Chromium list, not a live Chrome window.

    The grabs right after the wheel still show the pre-wheel color. A later
    grab of the same box shows the move from ITEM-001 to ITEM-010. That scroll
    is not page_unchanged, and the snapshot read after it starts at ITEM-010.
    A following wheel whose grabs stay on that color is page_unchanged and
    does not install ITEM-192.
    """
    window, listing, hit, _stuck = _chrome_list()
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)

    def on_wheel():
        hit._frozen = hit.screen
        hit._paint_left = 3
        hit.screen = 10
        hit.clip = True
        hit.invent = None
        hit.hold_until = 0
        hit._at.clear()

    hit.on_wheel = on_wheel
    driver = LinuxDriver()
    snap = driver.snapshot(Scope.WINDOW, "chrome")
    assert _row_titles(snap)[0] == "ITEM-001"
    row = next(el for el in snap.elements if el.role == "AXRow" and el.title == "ITEM-001")
    assert driver.scroll(row, dy=3, unit=ScrollUnit.LINES) is None
    # The pre-wheel grab plus the early post-wheel grabs are the old color.
    assert hit.grab_colors[0] == 1
    assert hit.grab_colors[1:4] == [1, 1, 1]
    assert 10 in hit.grab_colors
    later = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert later[0] == "ITEM-010"
    assert "ITEM-017" in later
    assert "ITEM-001" not in later
    assert "ITEM-009" not in later
    assert listing.get_child_at_index(0).name == "ITEM-001"

    def still(_dx_ignored=None):
        hit.invent = 192
        hit._paint_left = 0
        hit._at.clear()

    hit.on_wheel = still
    anchor = next(
        el for el in driver.snapshot(Scope.WINDOW, "chrome").elements
        if el.role == "AXRow" and el.title == "ITEM-010"
    )
    with pytest.raises(ComputerUseError) as error:
        driver.scroll(anchor, dy=5, unit=ScrollUnit.LINES)
    assert error.value.code is ErrorCode.UNSUPPORTED
    assert error.value.detail["reason"] == "page_unchanged"
    assert error.value.detail["mean_abs"] == 0
    assert error.value.detail["samples"] > 1
    kept = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert kept[0] == "ITEM-010"
    assert "ITEM-192" not in kept
    assert "ITEM-001" not in kept


def _offscreen_item_001() -> _Acc:
    """The pre-scroll row parked above the list. Its box does not cover a
    sample inside the list. Synthetic, not a live Chrome bounds read."""
    row = _Acc("list item", name="ITEM-001", width=20, height=4)
    row.component._rect.x = 0
    row.component._rect.y = -2
    return row


def test_moved_pixels_do_not_keep_the_row_parked_above_the_list(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic Chromium list, not a live Chrome window.

    The pixels move from ITEM-001 to ITEM-010. The hit test first answers
    ITEM-001 with bounds at y=-2, which does not cover the list. That row
    is not the snapshot head. When the hit test answers the rows inside the
    list, the snapshot starts at ITEM-010. A later wheel whose pixels stay
    put is page_unchanged and does not install ITEM-192.
    """
    window, listing, hit, _stuck = _chrome_list()
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)
    offscreen = _offscreen_item_001()

    def on_wheel():
        hit.offscreen = offscreen
        hit.blind_until = hit.clock + 1
        hit.screen = 10
        hit.clip = True
        hit.invent = None
        hit.hold_until = 0
        hit._at.clear()

    hit.on_wheel = on_wheel
    driver = LinuxDriver()
    snap = driver.snapshot(Scope.WINDOW, "chrome")
    assert _row_titles(snap)[0] == "ITEM-001"
    row = next(el for el in snap.elements if el.role == "AXRow" and el.title == "ITEM-001")
    assert driver.scroll(row, dy=3, unit=ScrollUnit.LINES) is None
    later = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert later[0] == "ITEM-010"
    assert "ITEM-017" in later
    assert "ITEM-001" not in later
    assert offscreen.component._rect.y == -2
    assert listing.get_child_at_index(0).name == "ITEM-001"

    def still():
        hit.invent = 192
        hit.blind_until = 0
        hit._at.clear()

    hit.on_wheel = still
    anchor = next(
        el for el in driver.snapshot(Scope.WINDOW, "chrome").elements
        if el.role == "AXRow" and el.title == "ITEM-010"
    )
    with pytest.raises(ComputerUseError) as error:
        driver.scroll(anchor, dy=5, unit=ScrollUnit.LINES)
    assert error.value.detail["reason"] == "page_unchanged"
    assert error.value.detail["mean_abs"] == 0
    kept = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert kept[0] == "ITEM-010"
    assert "ITEM-192" not in kept
    assert "ITEM-001" not in kept


def _place_row(row: _Acc, x: float, y: float, width: float, height: float) -> _Acc:
    if row.component is None:
        row.component = _Geom(width, height)
    row.component._rect.x = x
    row.component._rect.y = y
    row.component._rect.width = width
    row.component._rect.height = height
    return row


def _positioned_window(rows: list[_Acc], head: int) -> list[_Acc]:
    """Eight rows starting at ``head`` (1-based), first top 8px below the list.

    The list fixture sits at y=100, so these rows start at y=108. That is
    inside the viewport. Synthetic boxes, not a live Chrome bounds read.
    """
    visible = []
    start = max(0, head - 1)
    for index, row in enumerate(rows[start:start + 8]):
        _place_row(row, 40, 108 + index * 16, 400, 16)
        visible.append(row)
    return visible


def _overlapping_named(name: str) -> _Acc:
    """A row at y=-2 whose height still covers the sample at y=108.

    The 0.4.15 check kept this shape. A height of 4 at the same y does not
    cover the sample and is a different case.
    """
    row = _Acc("list item", name=name, width=400, height=120)
    row.component._rect.x = 40
    row.component._rect.y = -2
    return row


def test_overlapping_row_above_the_list_is_not_the_snapshot_head(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic Chromium list, not a live Chrome window.

    The 0.4.15 driver dropped a hit only when its box missed the sample.
    Here ITEM-001 sits at y=-2 with height 120, so it covers the sample at
    y=108, and the hit test returns that row for every point. The list's
    children also include ITEM-010 and the rows after it, with tops on the
    head line. A 3-line scroll whose pixels move returns None, and the
    snapshot head is ITEM-010 inside the list. A following wheel whose
    pixels stay put is page_unchanged and does not install ITEM-192.
    This does not prove the live Chrome list.
    """
    window, listing, hit, _stuck = _chrome_list()
    _adopt(listing, *_positioned_window(hit.rows, 1))
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)

    def on_wheel():
        parked = hit.rows[0]
        _place_row(parked, 40, -2, 400, 120)
        hit.screen = 10
        hit.force_row = parked
        hit.invent = None
        hit.hold_until = 0
        hit._at.clear()
        _adopt(listing, parked, *_positioned_window(hit.rows, 10))

    hit.on_wheel = on_wheel
    driver = LinuxDriver()
    snap = driver.snapshot(Scope.WINDOW, "chrome")
    assert _row_titles(snap)[0] == "ITEM-001"
    row = next(el for el in snap.elements if el.role == "AXRow" and el.title == "ITEM-001")
    assert row.bounds.y >= 100
    assert driver.scroll(row, dy=3, unit=ScrollUnit.LINES) is None
    parked = hit.rows[0]
    assert parked.component._rect.y == -2
    assert _atspi._point_in_extents(parked, 240, 108) is True
    assert _atspi._top_above_line(parked, 108) is True
    later_snap = driver.snapshot(Scope.WINDOW, "chrome")
    later = _row_titles(later_snap)
    assert later[0] == "ITEM-010"
    assert "ITEM-017" in later
    assert "ITEM-001" not in later
    head = next(el for el in later_snap.elements if el.role == "AXRow" and el.title == "ITEM-010")
    assert head.bounds.y >= 100
    assert head.bounds.y != -2
    assert listing.get_child_at_index(0).name == "ITEM-001"

    def still():
        # The hit test names a row that is not on screen. The child list stays
        # the rows already inside the list. A still grab must not install the
        # hit-test row.
        hit.invent = 192
        hit.force_row = _overlapping_named("ITEM-192")

    hit.on_wheel = still
    anchor = next(
        el for el in driver.snapshot(Scope.WINDOW, "chrome").elements
        if el.role == "AXRow" and el.title == "ITEM-010"
    )
    with pytest.raises(ComputerUseError) as error:
        driver.scroll(anchor, dy=5, unit=ScrollUnit.LINES)
    assert error.value.code is ErrorCode.UNSUPPORTED
    assert error.value.detail["reason"] == "page_unchanged"
    assert error.value.detail["mean_abs"] == 0
    kept = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert kept[0] == "ITEM-010"
    assert "ITEM-192" not in kept
    assert "ITEM-001" not in kept


def test_scroll_to_find_passes_an_overlapping_stale_head(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic Chromium list, not a live Chrome window.

    Each wheel moves the pixels. The hit test keeps returning a row at
    y=-2 whose box still covers the sample, which is the row 0.4.15 kept.
    The list children are that row plus the rows now inside the list.
    scroll_to_find reaches ITEM-180 and does not stop on rows_stale.
    This does not prove the live Chrome list.
    """
    from a11y_computer_use import server

    window, listing, hit, _stuck = _chrome_list()
    _adopt(listing, *_positioned_window(hit.rows, 1))
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)

    def on_wheel():
        previous = hit.screen
        hit.advance()
        overlap = _overlapping_named(hit.rows[previous - 1].name)
        _adopt(listing, overlap, *_positioned_window(hit.rows, hit.screen))
        hit.force_row = overlap

    hit.on_wheel = on_wheel
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = driver
    runtime._run_gated = lambda _action, _app, execute, **_kwargs: execute()
    runtime._require_permission = lambda *_args, **_kwargs: None
    runtime._recheck_target = lambda *_args, **_kwargs: None
    monkeypatch.setattr(server, "_running_app", lambda _name: (None, "chrome"))

    out = runtime.scroll_to_find("chrome", text="ITEM-180", max_scrolls=25)
    assert "ITEM-180" in out
    assert "found after" in out
    assert "found after 0 scroll" not in out
    assert _atspi._point_in_extents(hit.force_row, 240, 108) is True
    assert listing.get_child_at_index(0).name != "ITEM-180"


def _fresh_list_shells(window, listing, hit) -> None:
    """Each child read of the window returns a new list wrapper.

    The rows stay on ``listing``. The wrapper ``refresh_visible`` probed and
    the wrapper the snapshot walk reads are different objects. Synthetic,
    not a live AT-SPI wrapper.
    """

    def child_at(index):
        if index != 0:
            return None
        shell = _Acc("list", name="items", width=400, height=160)
        shell.component._rect.x = hit.x
        shell.component._rect.y = hit.y
        shell.get_application = listing.get_application
        shell.get_child_count = listing.get_child_count
        shell.get_child_at_index = listing.get_child_at_index
        shell.parent = window
        return shell

    window.get_child_at_index = child_at
    window.get_child_count = lambda: 1


def _bury_visible_window(listing, hit, head: int, parked_name: str) -> _Acc:
    """Visible rows nested under a wrapper that starts above the list.

    Forty-five rows above the head line come first, then a row at y=-2 whose
    height still covers the sample, then the on-screen window at ``head``.
    The 0.4.16 scan did not open that wrapper and stopped at forty nodes.
    Synthetic boxes, not a live Chrome list.
    """
    parked = _overlapping_named(parked_name)
    above = [
        _place_row(_Acc("list item", name=f"OLD-{index:03d}"), 40, -800 + index * 8, 400, 8)
        for index in range(45)
    ]
    group = _Acc("panel", name="client", width=400, height=5000)
    _place_row(group, 40, -400, 400, 5000)
    _adopt(group, parked, *above, *_positioned_window(hit.rows, head))
    _adopt(listing, group)
    hit.force_row = parked
    return parked


def test_walked_list_starts_at_the_row_inside_the_viewport(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic Chromium list, not a live Chrome window.

    The list the snapshot walk reads is a different object from the one a
    saved head was stored on. Its child is a wrapper whose top is above the
    list, then forty-five rows above the head line, then ITEM-001 at y=-2
    with height 120 (that box covers the sample), then ITEM-010 inside the
    list. The hit test returns ITEM-001 for every point. A 3-line scroll
    whose pixels move returns None and the snapshot head is ITEM-010 inside
    the list. A following wheel whose pixels stay put is page_unchanged and
    does not install ITEM-192. This does not prove the live Chrome list.
    """
    window, listing, hit, _stuck = _chrome_list()
    _adopt(listing, *_positioned_window(hit.rows, 1))
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)
    _fresh_list_shells(window, listing, hit)
    assert window.get_child_at_index(0) is not window.get_child_at_index(0)

    def on_wheel():
        hit.screen = 10
        hit.invent = None
        hit.hold_until = 0
        hit._at.clear()
        _bury_visible_window(listing, hit, 10, "ITEM-001")

    hit.on_wheel = on_wheel
    driver = LinuxDriver()
    snap = driver.snapshot(Scope.WINDOW, "chrome")
    assert _row_titles(snap)[0] == "ITEM-001"
    row = next(el for el in snap.elements if el.role == "AXRow" and el.title == "ITEM-001")
    assert row.bounds.y >= 100
    assert driver.scroll(row, dy=3, unit=ScrollUnit.LINES) is None
    parked = hit.force_row
    assert parked.component._rect.y == -2
    assert _atspi._point_in_extents(parked, 240, 108) is True
    assert _atspi._top_above_line(parked, 108) is True
    later_snap = driver.snapshot(Scope.WINDOW, "chrome")
    later = _row_titles(later_snap)
    assert later[0] == "ITEM-010"
    assert "ITEM-017" in later
    assert "ITEM-001" not in later
    assert "OLD-000" not in later
    head = next(el for el in later_snap.elements if el.role == "AXRow" and el.title == "ITEM-010")
    assert head.bounds.y >= 100
    assert head.bounds.y != -2

    def still():
        hit.invent = 192
        hit.force_row = _overlapping_named("ITEM-192")

    hit.on_wheel = still
    anchor = next(
        el for el in driver.snapshot(Scope.WINDOW, "chrome").elements
        if el.role == "AXRow" and el.title == "ITEM-010"
    )
    with pytest.raises(ComputerUseError) as error:
        driver.scroll(anchor, dy=5, unit=ScrollUnit.LINES)
    assert error.value.code is ErrorCode.UNSUPPORTED
    assert error.value.detail["reason"] == "page_unchanged"
    assert error.value.detail["mean_abs"] == 0
    kept = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert kept[0] == "ITEM-010"
    assert "ITEM-192" not in kept
    assert "ITEM-001" not in kept


def test_scroll_to_find_passes_rows_nested_under_the_parked_wrapper(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic Chromium list, not a live Chrome window.

    Each wheel moves the pixels. The on-screen rows are nested under a
    wrapper whose top is above the list, behind forty-five rows above the
    head line and a row at y=-2 that still covers the sample. The hit test
    returns that row. scroll_to_find reaches ITEM-180 and does not stop on
    rows_stale. This does not prove the live Chrome list.
    """
    from a11y_computer_use import server

    window, listing, hit, _stuck = _chrome_list()
    _adopt(listing, *_positioned_window(hit.rows, 1))
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)
    _fresh_list_shells(window, listing, hit)

    def on_wheel():
        previous = hit.screen
        hit.advance()
        _bury_visible_window(listing, hit, hit.screen, f"ITEM-{previous:03d}")

    hit.on_wheel = on_wheel
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = driver
    runtime._run_gated = lambda _action, _app, execute, **_kwargs: execute()
    runtime._require_permission = lambda *_args, **_kwargs: None
    runtime._recheck_target = lambda *_args, **_kwargs: None
    monkeypatch.setattr(server, "_running_app", lambda _name: (None, "chrome"))

    out = runtime.scroll_to_find("chrome", text="ITEM-180", max_scrolls=25)
    assert "ITEM-180" in out
    assert "found after" in out
    assert "found after 0 scroll" not in out
    assert _atspi._point_in_extents(hit.force_row, 240, 108) is True


def test_scroll_to_find_passes_the_offscreen_stale_head(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic Chromium list, not a live Chrome window.

    Each wheel moves the pixels, and the first hit answers are the pre-scroll
    row at y=-2. scroll_to_find keeps going and finds ITEM-180. It does not
    stop on rows_stale.
    """
    from a11y_computer_use import server

    window, listing, hit, _stuck = _chrome_list()
    hit.offscreen = _offscreen_item_001()
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)

    def on_wheel():
        hit.blind_until = hit.clock + 1
        hit.advance()

    hit.on_wheel = on_wheel
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = driver
    runtime._run_gated = lambda _action, _app, execute, **_kwargs: execute()
    runtime._require_permission = lambda *_args, **_kwargs: None
    runtime._recheck_target = lambda *_args, **_kwargs: None
    monkeypatch.setattr(server, "_running_app", lambda _name: (None, "chrome"))

    out = runtime.scroll_to_find("chrome", text="ITEM-180", max_scrolls=25)
    assert "ITEM-180" in out
    assert "found after" in out
    assert "found after 0 scroll" not in out
    assert listing.get_child_at_index(0).name == "ITEM-001"


def test_unmoved_page_stays_unsupported_and_keeps_the_on_screen_rows(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic Chromium list, not a live Chrome window.

    The page is ITEM-173 through ITEM-180. A wheel that does not change the
    pixels returns unsupported, even when the hit test would name ITEM-192.
    The next snapshot still starts at ITEM-173 and still contains ITEM-180.
    """
    events, _display = xtest_recorder
    window, listing, hit, _stuck = _chrome_list()
    hit.screen = 173
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)

    def on_wheel():
        hit.invent = 192
        hit._at.clear()

    hit.on_wheel = on_wheel
    driver = LinuxDriver()
    snap = driver.snapshot(Scope.WINDOW, "chrome")
    titles = _row_titles(snap)
    assert titles[0] == "ITEM-173"
    assert "ITEM-180" in titles
    row = next(el for el in snap.elements if el.role == "AXRow" and el.title == "ITEM-180")
    with pytest.raises(ComputerUseError) as error:
        driver.scroll(row, dy=5, unit=ScrollUnit.LINES)
    assert error.value.code is ErrorCode.UNSUPPORTED
    assert error.value.detail["reason"] == "page_unchanged"
    assert error.value.detail["mean_abs"] == 0
    assert error.value.detail["rows"][0] == "ITEM-173"
    assert "ITEM-192" not in error.value.detail["rows"]
    later = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert later[0] == "ITEM-173"
    assert "ITEM-180" in later
    assert "ITEM-192" not in later
    assert listing.get_child_at_index(0).name == "ITEM-001"
    presses = [event for event in events if event[0] == _X_BPRESS and event[1] == 5]
    assert len(presses) == 5


def test_moved_page_with_a_stale_head_is_not_success(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic Chromium list, not a live Chrome window.

    The pixels change, but every hit test stays on ITEM-001. That is not a
    successful scroll, and the saved rows stay ITEM-001.
    """
    window, _listing, hit, _stuck = _chrome_list()
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)

    def on_wheel():
        hit.screen = 10
        hit.hold_until = 10**9
        hit.held_head = 1
        hit._at.clear()

    hit.on_wheel = on_wheel
    driver = LinuxDriver()
    snap = driver.snapshot(Scope.WINDOW, "chrome")
    row = next(el for el in snap.elements if el.role == "AXRow" and el.title == "ITEM-001")
    with pytest.raises(ComputerUseError) as error:
        driver.scroll(row, dy=3, unit=ScrollUnit.LINES)
    assert error.value.code is ErrorCode.UNSUPPORTED
    assert error.value.detail["reason"] == "rows_stale"
    assert error.value.detail["mean_abs"] > 1
    later = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert later[0] == "ITEM-001"
    assert "ITEM-010" not in later


def test_line_scroll_of_an_unmoved_layout_is_not_success(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """A wheel whose pixels and on-screen rows stay put is unsupported.

    Synthetic Chromium list, not a live Chrome window. The notches were sent.
    The next snapshot still starts at ITEM-001.
    """
    events, _display = xtest_recorder
    window, listing, hit, _stuck = _chrome_list(stuck=True)
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    driver = LinuxDriver()
    snap = driver.snapshot(Scope.WINDOW, "chrome")
    anchor = next(el for el in snap.elements if el.role == "AXList")
    with pytest.raises(ComputerUseError) as error:
        driver.scroll(anchor, dy=5, unit=ScrollUnit.LINES)
    assert error.value.code is ErrorCode.UNSUPPORTED
    assert error.value.detail["reason"] == "page_unchanged"
    assert error.value.detail["rows"][0] == "ITEM-001"
    assert listing.get_child_at_index(0).name == "ITEM-001"
    later = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert later[0] == "ITEM-001"
    assert "ITEM-180" not in later
    assert events[0][0] == _X_MOTION
    assert (_X_BPRESS, 5, 0, 0) in [(kind, detail, x, y) for kind, detail, x, y in events]


def _rows_from(rows: list[_Acc], head: int, y: float, height: float = 20.0) -> list[_Acc]:
    """Place eight rows starting at ``head`` (1-based) with the first top at ``y``.

    The list fixture's top is y=100. Synthetic boxes, not a live Chrome read.
    """
    visible = []
    start = max(0, head - 1)
    for index, row in enumerate(rows[start:start + 8]):
        _place_row(row, 40, y + index * height, 400, height)
        visible.append(row)
    return visible


def test_snapshot_includes_the_fully_visible_row_at_the_list_top(
    fake_atspi, monkeypatch
) -> None:
    """Synthetic Chromium list, not a live Chrome window.

    ITEM-001's own top is the list's top (y=100) and the row is 20px tall,
    so it extends below the 8px edge. 0.4.17 and 0.4.18 required the top to
    clear y=107 and the snapshot started at ITEM-002. The snapshot head is
    ITEM-001, and that row is stored. A row at y=-2 is still above the list.
    This does not prove the live Chrome list.
    """
    window, listing, hit, _stuck = _chrome_list()
    placed = _rows_from(hit.rows, 1, 100)
    _adopt(listing, *placed)
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    snap = LinuxDriver().snapshot(Scope.WINDOW, "chrome")
    titles = _row_titles(snap)
    assert titles[0] == "ITEM-001"
    assert "ITEM-002" in titles
    row = next(el for el in snap.elements if el.role == "AXRow" and el.title == "ITEM-001")
    assert row.bounds.y == 100
    assert _atspi._head_line(listing) == 100
    assert _atspi._top_above_line(placed[0], 100) is False
    assert _atspi._known_head_above(listing, [(placed[0], (40.0, 100.0), (400.0, 20.0))]) is False
    _atspi.commit_shown_rows(
        listing,
        [
            (placed[0], (40.0, 100.0), (400.0, 20.0)),
            (placed[1], (40.0, 120.0), (400.0, 20.0)),
        ],
    )
    assert _atspi.row_head(_atspi.saved_rows(listing)) == "ITEM-001"
    parked = _offscreen_item_001()
    assert _atspi._known_head_above(listing, [(parked, (0.0, -2.0), (20.0, 4.0))]) is True


def test_snapshot_includes_a_fully_visible_row_inset_inside_the_old_sliver(
    fake_atspi, monkeypatch
) -> None:
    """Synthetic Chromium list, not a live Chrome window.

    ITEM-001 starts 4px below the list top, inside the 8px edge 0.4.17
    treated as off screen, and the row is 20px tall. The snapshot head is
    ITEM-001. This does not prove the live Chrome list.
    """
    window, listing, hit, _stuck = _chrome_list()
    _adopt(listing, *_rows_from(hit.rows, 1, 104))
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    snap = LinuxDriver().snapshot(Scope.WINDOW, "chrome")
    titles = _row_titles(snap)
    assert titles[0] == "ITEM-001"
    row = next(el for el in snap.elements if el.role == "AXRow" and el.title == "ITEM-001")
    assert row.bounds.y == 104


def test_clipped_edge_and_a_row_above_the_list_are_not_the_snapshot_head(
    fake_atspi, monkeypatch
) -> None:
    """Synthetic Chromium list, not a live Chrome window.

    A 6px box at the list's top does not extend below the 8px edge. A row
    at y=92 starts above the list. The next full row is the snapshot head.
    This does not prove the live Chrome list.
    """
    window, listing, hit, _stuck = _chrome_list()
    parked = _place_row(hit.rows[0], 40, 92, 400, 20)
    sliver = _place_row(hit.rows[8], 40, 100, 400, 6)
    full = _rows_from(hit.rows, 10, 106)
    _adopt(listing, parked, sliver, *full)
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    titles = _row_titles(LinuxDriver().snapshot(Scope.WINDOW, "chrome"))
    assert titles[0] == "ITEM-010"
    assert "ITEM-009" not in titles
    assert "ITEM-001" not in titles
    assert "ITEM-017" in titles


def test_gtk_snapshot_keeps_cached_children(fake_atspi, monkeypatch) -> None:
    """A non-Chromium list is not rewritten from a hit test."""
    cached = []
    for index, name in enumerate(("ITEM-001", "ITEM-002")):
        row = _Acc("list item", name=name, width=400, height=18)
        row.component.x = 40
        row.component.y = 100 + index * 20
        cached.append(row)
    listing = _Acc("list", name="files", width=400, height=160)
    listing.component.x = 40
    listing.component.y = 100
    hit = _ListHit([_Acc("list item", name="ITEM-010")], cached)
    # The list's own extents stay the cached box. The hit-test object is
    # only there so a probe would be visible in ``hit.calls``.
    listing.component = hit
    _adopt(listing, *cached)
    window = _Acc("frame", name="files", width=800, height=600)
    _adopt(window, listing)
    monkeypatch.setattr(_atspi, "find_root", lambda *_args, **_kwargs: window)
    monkeypatch.setattr(_atspi, "primary_geometry", _geometry)
    before = listing.component.calls
    snap = LinuxDriver().snapshot(Scope.WINDOW, "gedit")
    assert _row_titles(snap)[:2] == ["ITEM-001", "ITEM-002"]
    assert listing.component.calls == before


def test_pixel_scroll_does_not_probe_layout_rows(fake_atspi, xtest_recorder, monkeypatch) -> None:
    text, vertical, horizontal = _scrolled_text()
    events, _display = xtest_recorder
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: text)

    def probe(*_args, **_kwargs):
        raise AssertionError("pixel scroll must not read layout rows or grab the list")

    monkeypatch.setattr(_atspi, "list_container", probe)
    monkeypatch.setattr("a11y_computer_use.drivers.linux._grab_region", probe)
    LinuxDriver().scroll(_body(), dx=5, dy=3, unit=ScrollUnit.PIXELS)
    assert vertical.value == 103 and horizontal.value == 45
    assert events == []


def test_uniform_rows_keep_one_bar_step_and_do_not_wheel(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic overflow list, not a live Chrome window.

    dy=5 writes the vertical bar by one step of five content rows, and not
    more than the rows already on screen. The grab changes by one channel
    of 2, so mean_abs is 2/3: under the still-page threshold of 1 and above
    the uniform-row floor. The painted head leaves ITEM-001. That is the
    step. The bar write is not undone, and no track click or wheel follows
    it. A second action would skip rows the first window never showed.
    """
    window, _document, listing, hit, rows = _overflow_page()
    bar = _overflow_bar()
    listing.children.append(bar)
    bar.parent = listing
    writes = _bind_overflow_bar(bar, listing, rows, hit)
    calls = {"scroll_to": 0}

    def scroll_to(_scroll_type, row=None):
        calls["scroll_to"] += 1
        return True

    for row in rows:
        row.component.scroll_to = lambda scroll_type, row=row: scroll_to(scroll_type, row)
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)

    def grab(_box):
        from PIL import Image

        channel = 0 if hit.screen <= 1 else 2
        return Image.new("RGB", (4, 4), (channel, 0, 0))

    monkeypatch.setattr("a11y_computer_use.drivers.linux._grab_region", grab)
    wheels = {"n": 0}
    clicks: list[tuple[int, int]] = []
    wired_scroll = _linux_input.scroll
    wired_click = _linux_input.click

    def counting(x, y, dx=0, dy=0):
        wheels["n"] += 1
        return wired_scroll(x, y, dx=dx, dy=dy)

    def clicking(x, y, button="left", count=1):
        clicks.append((int(x), int(y)))
        return wired_click(x, y, button=button, count=count)

    monkeypatch.setattr(_linux_input, "scroll", counting)
    monkeypatch.setattr(_linux_input, "click", clicking)
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    snap = driver.snapshot(Scope.WINDOW, "chrome")
    assert _row_titles(snap)[0] == "ITEM-001"
    listing_el = next(el for el in snap.elements if el.role == "AXList")
    assert driver.scroll(listing_el, dy=5, unit=ScrollUnit.LINES) is None
    assert wheels["n"] == 0
    assert clicks == []
    assert calls["scroll_to"] == 0
    assert writes == [140.0]
    assert bar.value == 140.0
    painted = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert painted[0] == "ITEM-006"
    assert "ITEM-001" not in painted


def _uniform_text_grab(monkeypatch, hit):
    """Grab for a list whose rows look almost the same. Synthetic pixels.

    While ``hit.uniform`` is set, every grab is one channel of 1, so a
    five-row step measures mean 0, at or under the uniform-row floor.
    Clearing the flag uses the head as the channel, so a page measures
    well above 1. Not a capture from Tester's display.
    """

    def grab(_box):
        from PIL import Image

        channel = 1 if getattr(hit, "uniform", False) else int(hit.screen) * 4
        hit.grab_colors.append(channel)
        return Image.new("RGB", (4, 4), (channel, 0, 0))

    monkeypatch.setattr("a11y_computer_use.drivers.linux._grab_region", grab)


def _item_number(title: str) -> int:
    return int(title.split("-")[1].split()[0])


def test_low_mean_five_line_step_does_not_page_or_skip_a_row(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic fixed-height list, not Tester's display and not a live pass.

    dy=5 reveals the row five places down. The grab stays at mean 0, which
    is the uniform-row floor the 0.4.28 retest sat under. On that retest the
    driver still clicked the track, so the heads were ITEM-001, ITEM-032,
    ITEM-037, ITEM-068: moves of +31, +5, +31. The page landed with a
    clipped row above the tree, and ITEM-030 was in neither window. Here
    the track click pages by 26 rows on top of the five. Each step must
    stay five rows, the windows must overlap, and ITEM-030 must be listed.
    No track click and no wheel.
    """
    window, listing, hit, rows = _fixed_height_page()
    hit.uniform = True

    def reveal(row, _scroll_type):
        hit.uniform = True
        _layout_fixed_rows(listing, rows, hit, _item_number(row.name))
        return True

    for row in rows:
        row.component.scroll_to = lambda scroll_type, row=row: reveal(row, scroll_type)
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    _uniform_text_grab(monkeypatch, hit)
    wheels = {"n": 0}
    clicks: list[tuple[int, int]] = []
    wired_scroll = _linux_input.scroll
    wired_click = _linux_input.click

    def counting(x, y, dx=0, dy=0):
        wheels["n"] += 1
        return wired_scroll(x, y, dx=dx, dy=dy)

    def clicking(x, y, button="left", count=1):
        clicks.append((int(x), int(y)))
        wired_click(x, y, button=button, count=count)
        # The 0.4.28 second gesture: one page on top of the five-row step.
        hit.uniform = False
        _layout_fixed_rows(listing, rows, hit, min(172, int(hit.screen) + 26))

    monkeypatch.setattr(_linux_input, "scroll", counting)
    monkeypatch.setattr(_linux_input, "click", clicking)
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    snap = driver.snapshot(Scope.WINDOW, "chrome")
    assert _row_titles(snap)[0] == "ITEM-001"
    assert "ITEM-030" not in _row_titles(snap)
    listing_el = next(el for el in snap.elements if el.role == "AXList")
    heads = [1]
    seen: set[int] = set(range(1, 30))
    for _ in range(6):
        assert driver.scroll(listing_el, dy=5, unit=ScrollUnit.LINES) is None
        titles = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
        heads.append(_item_number(titles[0]))
        seen.update(_item_number(title) for title in titles)
    moves = [heads[index + 1] - heads[index] for index in range(len(heads) - 1)]
    assert moves == [5, 5, 5, 5, 5, 5]
    assert 31 not in moves
    # The opening window is ITEM-001 through ITEM-029, so ITEM-030 is not
    # in the seed. A later five-row window lists it. A +31 jump from
    # ITEM-001 lands on ITEM-032 and never does.
    assert 30 in seen
    for index in range(len(heads) - 1):
        assert heads[index] < heads[index + 1] <= heads[index] + 28
    assert clicks == []
    assert wheels["n"] == 0


def test_low_mean_wrapper_search_finds_item_040_without_paging_past_it(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic overflow list, not Tester's display and not a live pass.

    About fifteen rows are on screen. dy=5 reveals five rows and the grab
    stays at mean 0. On the 0.4.28 retest that rejection was followed by a
    page, and the heads alternated +18, +5. The windows were ITEM-024
    through ITEM-038 and then ITEM-042, so ITEM-040 was never listed.
    ``scroll_to_find`` then ran on to the bottom, ITEM-186 through
    ITEM-200. The same search must match ITEM-040 while it is on screen.
    A track click pages by 13 rows. A wheel, if one were sent, jumps to
    ITEM-186. Neither one runs.
    """
    from a11y_computer_use import server

    window, _document, listing, hit, rows = _overflow_page()
    hit.uniform = True

    def reveal(row, _scroll_type):
        hit.uniform = True
        _layout_overflow(listing, rows, hit, _item_number(row.name))
        return True

    for row in rows:
        row.component.scroll_to = lambda scroll_type, row=row: reveal(row, scroll_type)

    def on_wheel():
        hit.uniform = False
        _layout_overflow(listing, rows, hit, 186)

    hit.on_wheel = on_wheel
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)
    _uniform_text_grab(monkeypatch, hit)
    wheels = {"n": 0}
    clicks: list[tuple[int, int]] = []
    wired_scroll = _linux_input.scroll
    wired_click = _linux_input.click

    def counting(x, y, dx=0, dy=0):
        wheels["n"] += 1
        return wired_scroll(x, y, dx=dx, dy=dy)

    def clicking(x, y, button="left", count=1):
        clicks.append((int(x), int(y)))
        wired_click(x, y, button=button, count=count)
        hit.uniform = False
        _layout_overflow(listing, rows, hit, min(186, int(hit.screen) + 13))

    monkeypatch.setattr(_linux_input, "scroll", counting)
    monkeypatch.setattr(_linux_input, "click", clicking)
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    first = driver.snapshot(Scope.WINDOW, "chrome")
    anchor = server._scroll_anchor(first)
    assert anchor.role == "AXList"
    assert _row_titles(first)[:15] == [f"ITEM-{i:03d}" for i in range(1, 16)]
    assert "ITEM-040" not in _row_titles(first)
    runtime = _runtime_for(driver, monkeypatch)
    out = runtime.scroll_to_find("chrome", text="ITEM-040", max_scrolls=25)
    assert "ITEM-040" in out
    assert "found after" in out
    assert "not found" not in out
    assert wheels["n"] == 0
    assert clicks == []
    painted = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert "ITEM-040" in painted
    assert "ITEM-186" not in painted
    assert _item_number(painted[0]) <= 40


def _wrapped_overflow_list():
    """A content-height list inside a shorter panel. The panel scrolls.

    The list box starts two rows above the panel, so those rows are hidden.
    The panel's parent is the document, so a search with no ref anchors on
    the document. Synthetic boxes, not a live Chrome window.
    """
    window, listing, hit, _stuck = _chrome_list()
    rows = hit.rows
    app = window.get_application()
    listing.component = _Geom(1200, 5600)
    listing.component._rect.x = 20.0
    listing.component._rect.y = 124.0
    hit.screen = 3
    for index, row in enumerate(rows):
        row.get_application = lambda: app
        _place_row(row, 29, 124 + index * 28, 184, 19)
    _adopt(listing, *rows)
    panel = _Acc("panel", name="scroller", width=1239, height=420)
    panel.component._rect.x = 20.0
    panel.component._rect.y = 180.0
    panel.get_application = lambda: app
    document = _Acc("document web", name="Bench", width=1280, height=680)
    document.component._rect.x = 0.0
    document.component._rect.y = 90.0
    document.get_application = lambda: app
    _adopt(panel, listing)
    _adopt(document, panel)
    _adopt(window, document)
    _add_tab_strip(window)
    return window, document, panel, listing, hit, rows


def test_wrapped_list_finds_item_040_without_wheeling_past_it(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic wrapped list, not a live Chrome window.

    The list does not scroll. The panel around it does. The snapshot head
    is the first row inside the panel (ITEM-003); ITEM-001 and ITEM-002
    sit above that panel. ``scroll_to_find`` ITEM-040 reveals it through
    ``scroll_to``. A wheel, if one were sent, jumps the paint to ITEM-186
    and skips the target.
    """
    from a11y_computer_use import server

    window, document, _panel, listing, hit, rows = _wrapped_overflow_list()
    wheels = {"n": 0}

    def reveal(row, _scroll_type):
        number = int(row.name.split("-")[1])
        hit.screen = number
        for index, item in enumerate(rows):
            _place_row(item, 29, 180 + (index - (number - 1)) * 28, 184, 19)
        return True

    for row in rows:
        row.component.scroll_to = lambda scroll_type, row=row: reveal(row, scroll_type)

    def on_wheel():
        wheels["n"] += 1
        hit.screen = 186
        for index, item in enumerate(rows):
            _place_row(item, 29, 180 + (index - 185) * 28, 184, 19)

    hit.on_wheel = on_wheel
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    first = driver.snapshot(Scope.WINDOW, "chrome")
    anchor = server._scroll_anchor(first)
    assert anchor.role == "AXGroup"
    assert anchor.title == document.name
    assert anchor.bounds.height > 420
    titles = _row_titles(first)
    assert titles[0] == "ITEM-003"
    assert "ITEM-001" not in titles
    assert "ITEM-002" not in titles
    assert "ITEM-040" not in titles
    runtime = _runtime_for(driver, monkeypatch)
    out = runtime.scroll_to_find("chrome", text="ITEM-040", max_scrolls=25)
    assert "ITEM-040" in out
    assert "found after" in out
    assert "not found" not in out
    assert wheels["n"] == 0
    painted = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert "ITEM-040" in painted
    assert painted[0] != "ITEM-186"
    assert "ITEM-186" not in painted
    assert listing.component._rect.y == 124.0
    assert listing.component._rect.height == 5600


def _hang_markers(rows: list[_Acc]) -> None:
    """Give each row a bullet and a label. The list item's own name is empty.

    Chromium's default ``<ul>`` looks like this. The bullet is not the row.
    Synthetic boxes, not a live Chrome tree.
    """
    for row in rows:
        label = _Acc("static", name=row.name, width=160, height=16)
        bullet = _Acc("static", name="•", width=12, height=16)
        app = row.get_application
        bullet.get_application = app
        label.get_application = app
        row.name = ""
        _adopt(row, bullet, label)


def _sync_markers(rows: list[_Acc]) -> None:
    for row in rows:
        if len(row.children) < 2 or row.component is None:
            continue
        x = float(row.component._rect.x)
        y = float(row.component._rect.y)
        _place_row(row.children[0], x, y, 12, 16)
        _place_row(row.children[1], x + 16, y, 160, 16)


def test_wrapped_list_markers_are_not_the_head_and_five_lines_move_five_rows(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic wrapped list, not a live Chrome window.

    Each row is an unnamed list item, a "•" marker, and ``ITEM-NNN`` text.
    The snapshot head is the text inside the panel (ITEM-003). dy=5 reveals
    ITEM-008, five content rows later. ``scroll_to_find`` matches ITEM-040
    and ITEM-100 through ``scroll_to``. A wheel, if one were sent, jumps
    the paint to ITEM-186.
    """
    from a11y_computer_use import server

    window, document, _panel, listing, hit, rows = _wrapped_overflow_list()
    _hang_markers(rows)
    _sync_markers(rows)
    wheels = {"n": 0}

    def place(number: int) -> None:
        hit.screen = number
        for index, item in enumerate(rows):
            _place_row(item, 29, 180 + (index - (number - 1)) * 28, 184, 19)
        _sync_markers(rows)

    def reveal(label, _scroll_type):
        number = int(label.name.split("-")[1].split()[0])
        place(number)
        return True

    for row in rows:
        label = row.children[1]
        label.component.scroll_to = lambda scroll_type, label=label: reveal(label, scroll_type)

    def on_wheel():
        wheels["n"] += 1
        place(186)

    hit.on_wheel = on_wheel
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    first = driver.snapshot(Scope.WINDOW, "chrome")
    anchor = server._scroll_anchor(first)
    assert anchor.role == "AXGroup"
    assert anchor.title == document.name
    titles = [el.title for el in first.elements if el.title.startswith("ITEM-") or el.title == "•"]
    assert titles[0] == "ITEM-003"
    assert "•" not in titles
    assert "ITEM-001" not in titles
    assert "ITEM-040" not in titles
    assert driver.scroll(anchor, dy=5, unit=ScrollUnit.LINES) is None
    assert wheels["n"] == 0
    stepped = [
        el.title for el in driver.snapshot(Scope.WINDOW, "chrome").elements
        if el.title.startswith("ITEM-") or el.title == "•"
    ]
    assert stepped[0] == "ITEM-008"
    assert "ITEM-003" not in stepped
    runtime = _runtime_for(driver, monkeypatch)
    found_040 = runtime.scroll_to_find("chrome", text="ITEM-040", max_scrolls=25)
    assert "ITEM-040" in found_040
    assert "found after" in found_040
    assert "not found" not in found_040
    found_100 = runtime.scroll_to_find("chrome", text="ITEM-100", max_scrolls=25)
    assert "ITEM-100" in found_100
    assert "found after" in found_100
    assert "not found" not in found_100
    assert wheels["n"] == 0
    painted = [
        el.title for el in driver.snapshot(Scope.WINDOW, "chrome").elements
        if el.title.startswith("ITEM-")
    ]
    assert "ITEM-100" in painted
    assert "ITEM-186" not in painted


def _shell_wrapped_overflow_list():
    """The wrapped list, under an empty Chrome panel larger than the document.

    The panel is still under 90% of the window, so the old anchor (the
    largest group) is this panel. Its first 35 children are empty, and the
    document is the next one. A list walk that reads 30 children from the
    panel never reaches the list. The snapshot walk reads 200 and keeps the
    titled document. Synthetic boxes, not a live Chrome window.
    """
    window, document, panel, listing, hit, rows = _wrapped_overflow_list()
    app = window.get_application()
    shell = _Acc("panel", name="", width=1280, height=710)
    shell.component._rect.x = 0.0
    shell.component._rect.y = 80.0
    shell.get_application = lambda: app
    fillers = []
    for _index in range(35):
        filler = _Acc("panel", name="", width=10, height=10)
        filler.component._rect.x = 4.0
        filler.component._rect.y = 84.0
        filler.get_application = lambda: app
        fillers.append(filler)
    _adopt(shell, *fillers, document)
    others = [child for child in window.children if child is not document]
    _adopt(window, *others, shell)
    return window, document, shell, panel, listing, hit, rows


def test_empty_chrome_panel_does_not_take_the_overflow_search(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic wrapped list, not Tester's display and not a live pass.

    On the 0.4.29 retest the content-height list sat under an empty Chrome
    panel. That panel was larger than the titled document and still under
    90% of the window, so ``scroll_to_find`` anchored on it. The list walk
    from the panel stops at 30 children, before the document, and each of
    the 25 steps sent a wheel. The wheel jumped the paint to ITEM-186
    through ITEM-200 and still returned success, so ITEM-040 and ITEM-100
    were never listed. The anchor is the titled document. From there the
    overflow list is found and dy=5 reveals five rows. A wheel, if one
    were sent, jumps the paint to ITEM-186.
    """
    from a11y_computer_use import server

    window, document, shell, _panel, _listing, hit, rows = _shell_wrapped_overflow_list()
    _hang_markers(rows)
    _sync_markers(rows)
    wheels = {"n": 0}

    def place(number: int) -> None:
        hit.screen = number
        for index, item in enumerate(rows):
            _place_row(item, 29, 180 + (index - (number - 1)) * 28, 184, 19)
        _sync_markers(rows)

    def reveal(label, _scroll_type):
        number = int(label.name.split("-")[1].split()[0])
        place(number)
        return True

    for row in rows:
        label = row.children[1]
        label.component.scroll_to = lambda scroll_type, label=label: reveal(label, scroll_type)

    def on_wheel():
        wheels["n"] += 1
        place(186)

    hit.on_wheel = on_wheel
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)
    assert _atspi.list_with_overflow_ancestor(shell) is None
    assert _atspi.list_with_overflow_ancestor(document) is not None
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    first = driver.snapshot(Scope.WINDOW, "chrome")
    anchor = server._scroll_anchor(first)
    assert anchor.role == "AXGroup"
    assert anchor.title == document.name
    by_ref = {el.ref: el for el in first.elements}
    listing_el = next(el for el in first.elements if el.role == "AXList")
    chain = []
    node = listing_el
    while node is not None:
        chain.append(node)
        node = by_ref.get(node.parent) if node.parent else None
    shell_el = next(el for el in chain if el.bounds.height == 710)
    document_el = next(el for el in chain if el.title == document.name)
    window_el = next(el for el in first.elements if el.role == "AXWindow")
    assert shell_el.title == ""
    assert shell_el.bounds.width * shell_el.bounds.height > (
        document_el.bounds.width * document_el.bounds.height
    )
    assert shell_el.bounds.width * shell_el.bounds.height < 0.9 * (
        window_el.bounds.width * window_el.bounds.height
    )
    titles = [el.title for el in first.elements if el.title.startswith("ITEM-") or el.title == "•"]
    assert titles[0] == "ITEM-003"
    assert "ITEM-040" not in titles
    assert "ITEM-100" not in titles
    assert driver.scroll(anchor, dy=5, unit=ScrollUnit.LINES) is None
    assert wheels["n"] == 0
    stepped = [
        el.title for el in driver.snapshot(Scope.WINDOW, "chrome").elements
        if el.title.startswith("ITEM-") or el.title == "•"
    ]
    assert stepped[0] == "ITEM-008"
    runtime = _runtime_for(driver, monkeypatch)
    found_040 = runtime.scroll_to_find("chrome", text="ITEM-040", max_scrolls=25)
    assert "ITEM-040" in found_040
    assert "found after" in found_040
    assert "not found" not in found_040
    found_100 = runtime.scroll_to_find("chrome", text="ITEM-100", max_scrolls=25)
    assert "ITEM-100" in found_100
    assert "found after" in found_100
    assert "not found" not in found_100
    assert wheels["n"] == 0
    painted = [
        el.title for el in driver.snapshot(Scope.WINDOW, "chrome").elements
        if el.title.startswith("ITEM-")
    ]
    assert "ITEM-100" in painted
    assert "ITEM-186" not in painted


def _bare_runtime(driver):
    from a11y_computer_use import server

    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = driver
    runtime._run_gated = lambda _action, _app, execute, **_kwargs: execute()
    runtime._require_permission = lambda *_args, **_kwargs: None
    runtime._recheck_target = lambda *_args, **_kwargs: None
    return runtime


def test_sheet_cell_is_not_a_numeric_control_and_hides_value_zero(fake_atspi) -> None:
    """Synthetic cell. The Value interface is not read for an address cell."""
    cell = _Acc(
        "table cell", name="D1", value=0.0,
        minimum=-1.7976931348623157e+308, maximum=1.7976931348623157e+308,
    )
    cell.text = ""
    probed = {"n": 0}
    original = fake_atspi.Value.get_minimum_value

    def counting(acc):
        probed["n"] += 1
        return original(acc)

    fake_atspi.Value.get_minimum_value = staticmethod(counting)
    try:
        assert _atspi.control_kind(cell) is None
        assert probed["n"] == 0
        assert _atspi._value_text(cell, "AXCell", "table cell") is None
        cell.text = "setv"
        assert _atspi._value_text(cell, "AXCell", "table cell") == "setv"
        cell.text = ""
        cell.get_attributes = lambda: {"formula": "B1*2"}
        assert _atspi._value_text(cell, "AXCell", "table cell") == "=B1*2"
        assert _atspi.sheet_cell_matches(cell, "=B1*2") is True
        cell.text = "0"
        assert _atspi._value_text(cell, "AXCell", "table cell") == "0"
        assert _atspi.sheet_cell_matches(cell, "=B1*2") is True
        meter = _Acc("filler", name="Meter", value=1, minimum=0, maximum=10)
        assert _atspi.control_kind(meter) == "value"
        assert probed["n"] == 1
    finally:
        fake_atspi.Value.get_minimum_value = staticmethod(original)


def test_writer_cell_replace_writes_a_paragraph_set_text_refuses(fake_atspi) -> None:
    """A Firefox paragraph stays blocked. A Writer cell paragraph is replaced.

    set_text refuses the role so selecting a page paragraph does not select
    the page. The cell write forces that same call.
    """
    table = _Acc("table", name="Table1-1")
    cell = _Acc("table cell", name="B2")
    paragraph = _Acc("paragraph", name="")
    paragraph.text = "Cell B2"

    def set_text_contents(text):
        paragraph.text = text
        return True

    paragraph.get_editable_text_iface = lambda: paragraph
    paragraph.set_text_contents = set_text_contents
    _adopt(table, cell)
    _adopt(cell, paragraph)
    assert _atspi.set_text(paragraph, "nope") is False
    assert paragraph.text == "Cell B2"
    _atspi.replace_writer_cell_text(cell, "NEWB2-1")
    assert paragraph.text == "NEWB2-1"


def test_writer_cell_replace_waits_for_a_stale_paragraph_read(fake_atspi, monkeypatch) -> None:
    """The paragraph write sticks when the first reads still show the old line.

    ``set_text_contents`` has already replaced the line. A snapshot read can
    still return ``Cell B2``. That lag is not a miss, and it must not select
    the paragraph: ctrl+a in Writer selects the document.
    """
    table = _Acc("table", name="Table1-1")
    cell = _Acc("table cell", name="B2")
    paragraph = _Acc("paragraph", name="")
    paragraph.text = "Cell B2"
    reads = {"n": 0}

    def set_text_contents(text):
        paragraph.text = text
        return True

    def delete_text(*_args):
        raise AssertionError("delete")

    paragraph.get_editable_text_iface = lambda: paragraph
    paragraph.set_text_contents = set_text_contents
    paragraph.delete_text = delete_text
    _adopt(table, cell)
    _adopt(cell, paragraph)
    real_text = _atspi._full_text

    def lagging(acc):
        shown = real_text(acc)
        if acc is not paragraph:
            return shown
        reads["n"] += 1
        if reads["n"] <= 4:
            return "Cell B2"
        return shown

    monkeypatch.setattr(_atspi, "_full_text", lagging)

    def selected(*_args, **_kwargs):
        raise AssertionError("ctrl+a")

    monkeypatch.setattr(_atspi, "_x11_select_all_and_delete", selected)
    _atspi.replace_writer_cell_text(cell, "NEWB2-1")
    assert paragraph.text == "NEWB2-1"
    assert reads["n"] > 4


def test_sheet_formula_match_folds_case_whitespace_and_separators(fake_atspi) -> None:
    """Synthetic formula text. A range colon stays a colon.

    ``;`` and ``,`` are the same argument separator. A space inside a
    quoted string is still a difference. The computed number is not the formula.
    """
    assert _atspi.formulas_match("=AVERAGE(B2:B5)", "=average( B2:B5 )") is True
    assert _atspi.formulas_match("AVERAGE(B2\\", "=AVERAGE(B2:B5)") is False
    assert _atspi.formulas_match("=AVERAGE(B", "=AVERAGE(B2:B5)") is False
    assert _atspi.formulas_match("SUM(A1:A3)", "=sum( A1:A3 )") is True
    assert _atspi.formulas_match("SUM(A1\\:A3)", "=SUM(A1:A3)") is True
    assert _atspi.formulas_match("SUM(A1;A2)", "=SUM(A1,A2)") is True
    assert _atspi.formulas_match('CONCAT("a b")', '=concat( "a b" )') is True
    assert _atspi.formulas_match('CONCAT("a b")', '=CONCAT("ab")') is False
    assert _atspi.formulas_match("SUM(A1:A3)", "=SUM(A1:B9)") is False
    assert _atspi.formulas_match("SUM(A1\\", "=SUM(A1:A3)") is False
    cell = _Acc("table cell", name="C1")
    cell.text = "6"
    cell.get_attributes = lambda: {"formula": "SUM(A1:A3)"}
    assert _atspi.sheet_cell_matches(cell, "=sum( A1:A3 )") is True
    cell.get_attributes = lambda: {"formula": "SUM(A1\\"}
    assert _atspi.sheet_cell_matches(cell, "=SUM(A1:A3)") is False
    assert _atspi.sheet_cell_matches(cell, "6") is True


def test_sheet_number_match_accepts_calc_display_and_rejects_a_different_number(
    fake_atspi,
) -> None:
    """Synthetic cell text. ``1.50``, ``1e3``, and ``1,200`` match the display."""
    cell = _Acc("table cell", name="D1")
    cell.text = "14.6"
    cell.get_attributes = lambda: {}
    assert _atspi.sheet_cell_matches(cell, "14.60") is True
    cell.text = "1.5"
    assert _atspi.sheet_cell_matches(cell, "1.50") is True
    assert _atspi.sheet_cell_matches(cell, "14.60") is False
    cell.text = "1000"
    assert _atspi.sheet_cell_matches(cell, "1e3") is True
    cell.text = "1200"
    assert _atspi.sheet_cell_matches(cell, "1,200") is True
    assert _atspi.sheet_outcome_text("soffice.bin", "1,200", cell) == "1,200"
    cell.text = "1.5"
    assert _atspi.sheet_cell_matches(cell, "1,5") is True
    cell.text = "1.6"
    assert _atspi.sheet_cell_matches(cell, "1.50") is False
    cell.text = "6"
    cell.get_attributes = lambda: {"formula": "SUM(A1:A3)"}
    assert _atspi.sheet_cell_matches(cell, "=SUM(A1:A3)") is True
    assert _atspi._numeric_texts_match("6", "=SUM(A1:A3)") is False


def test_sheet_grid_names_the_first_differing_cell(fake_atspi, monkeypatch) -> None:
    """A Calc grid is compared cell by cell from the starting address.

    The first cell that disagrees is the mismatch. A displayed number is
    not a formula, and a Formula attribute cut at the colon is not a prefix
    of the request. F2 is not part of this scan.
    """
    assert _atspi.sheet_type_grid("Item\tQ1\nRent\t1200\n") == [
        ["Item", "Q1"], ["Rent", "1200"],
    ]
    assert _atspi.sheet_type_grid("only") is None
    assert _atspi.shift_sheet_address("A1", 2, 4) == "C5"
    assert _atspi.shift_sheet_address("B2", 1, 0) == "C2"

    def cell(text, formula=""):
        node = _Acc("table cell", name="X")
        node.text = text
        captured = formula
        node.get_attributes = lambda captured=captured: {"formula": captured} if captured else {}
        return node

    assert _atspi._classify_sheet_cell(cell("1200"), "1,200")[0] == "match"
    assert _atspi._classify_sheet_cell(cell("42"), "=E2+31") == ("differ", "42")
    assert _atspi._classify_sheet_cell(cell("2850", "SUM(C2\\"), "=SUM(C2:C4)")[0] == "editor"
    assert _atspi.formulas_match("SUM(C2\\", "=SUM(C2:C4)") is False
    assert _atspi.formulas_match("SUM(C2", "=SUM(C2:C4)") is False

    nodes = {
        "A1": cell("Item"),
        "B1": cell("Q1"),
        "A2": cell("999"),
        "B2": cell("1250"),
    }
    monkeypatch.setattr(_atspi, "_cell_for_address", lambda _app, address: nodes.get(address))
    scanned = _atspi._scan_sheet_grid("soffice", "A1", [["Item", "Q1"], ["1200", "1250"]])
    assert scanned[0] == "differ"
    assert scanned[1] == "A2"
    assert scanned[2] == "1200"
    assert scanned[3] == "999"

    nodes["A2"] = cell("1200")
    assert _atspi._scan_sheet_grid("soffice", "A1", [["Item", "Q1"], ["1200", "1250"]]) == (
        "match", None,
    )
    nodes = {
        "B5": cell("1770", "SUM(B2\\"),
        "C5": cell("nope"),
    }
    monkeypatch.setattr(_atspi, "_cell_for_address", lambda _app, address: nodes.get(address))
    scanned = _atspi._scan_sheet_grid(
        "soffice", "B5", [["=SUM(B2:B4)", "1250"]],
    )
    assert scanned[0] == "editor"
    assert scanned[2] == "differ"
    assert scanned[3] == "C5"


def test_sheet_grid_commits_an_open_editor_before_the_cell_read(fake_atspi, monkeypatch) -> None:
    """Sibling cells stay blank while Calc's editor is open.

    One Return commits that editor. The scan after it is what confirms.
    A grid that already shows every cell does not send the key. No live
    LibreOffice.
    """
    from a11y_computer_use.drivers import _linux_input

    state = {"editor": "=SUM(C2:C4)", "item": ""}
    chords: list[str] = []

    def cell_for(_app, address):
        if address == "A1":
            node = _Acc("table cell", name="A1")
            node.text = state["item"]
            node.get_attributes = lambda: {}
            return node
        if address == "B1":
            node = _Acc("table cell", name="B1")
            node.text = "Q1"
            node.get_attributes = lambda: {}
            return node
        return None

    def press(chord: str) -> None:
        chords.append(chord)
        state["editor"] = None
        state["item"] = "Item"

    monkeypatch.setattr(_atspi, "_cell_for_address", cell_for)
    monkeypatch.setattr(_atspi, "sheet_editor_text", lambda _app: state["editor"])
    monkeypatch.setattr(_atspi, "_SHEET_GRID_PAUSE_S", 0)
    monkeypatch.setattr(_atspi, "_SHEET_GRID_COMMIT_PAUSE_S", 0)
    monkeypatch.setattr(_linux_input, "press_chord", press)
    assert _atspi.sheet_grid_check("soffice.bin", "A1", "Item\tQ1") is None
    assert chords == ["return"]

    chords.clear()
    state["editor"] = None
    assert _atspi.sheet_grid_check("soffice.bin", "A1", "Item\tQ1") is None
    assert chords == []


def test_sheet_grid_waits_for_a_cell_the_table_has_not_published(fake_atspi, monkeypatch) -> None:
    """A missing cell is not a mismatch while the edit is still committing.

    The first reads have no accessible for A1. Return commits the editor,
    and the cell that appears after that is the comparison. No live
    LibreOffice. Extra text is still a mismatch, and it is not held for
    the wait.
    """
    from a11y_computer_use.drivers import _linux_input

    state = {"editor": "Q1", "reads": 0}
    chords: list[str] = []

    def cell_for(_app, address):
        if address == "A1":
            state["reads"] += 1
            if state["reads"] < 3:
                return None
            node = _Acc("table cell", name="A1")
            node.text = "Item"
            node.get_attributes = lambda: {}
            return node
        if address == "B1":
            node = _Acc("table cell", name="B1")
            node.text = "Q1"
            node.get_attributes = lambda: {}
            return node
        return None

    def press(chord: str) -> None:
        chords.append(chord)
        state["editor"] = None

    monkeypatch.setattr(_atspi, "_cell_for_address", cell_for)
    monkeypatch.setattr(_atspi, "sheet_editor_text", lambda _app: state["editor"])
    monkeypatch.setattr(_atspi, "_SHEET_GRID_PAUSE_S", 0)
    monkeypatch.setattr(_atspi, "_SHEET_GRID_COMMIT_PAUSE_S", 0)
    monkeypatch.setattr(_linux_input, "press_chord", press)
    assert _atspi.sheet_grid_check("soffice.bin", "A1", "Item\tQ1") is None
    assert state["reads"] >= 3
    assert chords == ["return"]

    wrong = _Acc("table cell", name="A1")
    wrong.text = "ItemX"
    wrong.get_attributes = lambda: {}
    monkeypatch.setattr(_atspi, "_cell_for_address", lambda _app, address: wrong if address == "A1" else None)
    monkeypatch.setattr(_atspi, "sheet_editor_text", lambda _app: None)
    slept: list[float] = []
    monkeypatch.setattr(_atspi.time, "sleep", lambda seconds: slept.append(seconds))
    assert _atspi.sheet_grid_check("soffice.bin", "A1", "Item\tQ1") == ("A1", "Item", "ItemX")
    assert slept == []

    blank = _Acc("table cell", name="A1")
    blank.text = ""
    blank.get_attributes = lambda: {}
    monkeypatch.setattr(_atspi, "_cell_for_address", lambda _app, address: blank if address == "A1" else None)
    monkeypatch.setattr(_atspi, "_SHEET_GRID_WAIT_S", 0)
    assert _atspi.sheet_grid_check("soffice.bin", "A1", "Item\tQ1") == ("A1", "Item", "")


def test_sheet_grid_restores_the_caret_after_the_formula_read(fake_atspi, monkeypatch) -> None:
    """F2 leaves the caret on the formula. It is put back on the next cell.

    Enter from a one-row grid at A1 selects A2. A caret that LibreOffice
    has already moved off the last typed cell is kept, including when that
    move is straight down. No live LibreOffice. The formula text still has
    to match.
    """
    placed: list[str | None] = []
    caret = {"address": "B1"}
    opened = {"text": "=SUM(B2:B4)"}

    def cell(text, formula=""):
        node = _Acc("table cell", name="X")
        node.text = text
        captured = formula
        node.get_attributes = lambda captured=captured: {"formula": captured} if captured else {}
        return node

    nodes = {"A1": cell("Item"), "B1": cell("1", "SUM(B2\\")}
    monkeypatch.setattr(_atspi, "_cell_for_address", lambda _app, address: nodes.get(address))
    monkeypatch.setattr(_atspi, "sheet_editor_text", lambda _app: None)
    monkeypatch.setattr(_atspi, "_sheet_caret_address", lambda _app: caret["address"])
    monkeypatch.setattr(
        _atspi, "committed_sheet_formula", lambda _cell, _app="soffice.bin": opened["text"],
    )
    monkeypatch.setattr(
        _atspi, "_place_sheet_caret", lambda _app, address: placed.append(address) or True,
    )
    monkeypatch.setattr(_atspi, "_SHEET_GRID_PAUSE_S", 0)
    monkeypatch.setattr(_atspi, "_SHEET_GRID_COMMIT_PAUSE_S", 0)
    assert _atspi.sheet_grid_landing("A1", 5) == "A6"
    assert _atspi.sheet_grid_landing("C3", 2) == "C5"
    assert _atspi.sheet_grid_check("soffice.bin", "A1", "Item\t=SUM(B2:B4)") is None
    assert placed == ["A2"]

    placed.clear()
    caret["address"] = "A2"
    assert _atspi.sheet_grid_check("soffice.bin", "A1", "Item\t=SUM(B2:B4)") is None
    assert placed == ["A2"]

    placed.clear()
    caret["address"] = "C6"
    opened["text"] = "=SUM(C2:C4)"
    nodes = {
        "A1": cell("Item"),
        "B1": cell("Q1"),
        "C1": cell("1", "SUM(C2\\"),
    }
    assert _atspi.sheet_grid_check("soffice.bin", "A1", "Item\tQ1\t=SUM(C2:C4)") is None
    assert placed == ["C6"]

    placed.clear()
    opened["text"] = "=SUM(B2:B9)"
    mismatch = _atspi.sheet_grid_check("soffice.bin", "A1", "Item\tQ1\t=SUM(C2:C4)")
    assert mismatch[0] == "C1"
    assert mismatch[1] == "=SUM(C2:C4)"
    assert placed == ["C6"]


def test_cut_range_formula_outcome_reads_the_editor(fake_atspi, monkeypatch) -> None:
    """Synthetic cell. The cut Formula attribute is not the comparison.

    The editor text is. A complete formula does not open the editor, and a
    different range stays a mismatch.
    """
    cell = _Acc("table cell", name="C1")
    cell.text = "6"
    cell.get_attributes = lambda: {"formula": "SUM(A1\\"}
    monkeypatch.setattr(_atspi, "sheet_editor_text", lambda _app: None)
    opened = {"text": "=sum( A1:A3 )", "n": 0}

    def read(_acc, _app="soffice.bin"):
        opened["n"] += 1
        return opened["text"]

    monkeypatch.setattr(_atspi, "committed_sheet_formula", read)
    assert _atspi.sheet_outcome_text("soffice.bin", "=SUM(A1:A3)", cell) == "=SUM(A1:A3)"
    assert opened["n"] == 1
    opened["text"] = "=SUM(A1:B9)"
    assert _atspi.sheet_outcome_text("soffice.bin", "=SUM(A1:A3)", cell) is None
    cell.get_attributes = lambda: {"formula": "E2+31"}
    assert _atspi.sheet_outcome_text("soffice.bin", "=E2+31", cell) == "=E2+31"
    assert opened["n"] == 2
    assert _atspi.sheet_outcome_text("soffice.bin", "=Z9", cell) is None
    assert opened["n"] == 2


def test_writer_table_cell_replaces_the_paragraph_and_restores_on_a_miss(fake_atspi, monkeypatch) -> None:
    """A Writer cell named B2 is not a Calc write. The paragraph is replaced.

    A write that does not read back as the request puts the paragraph back.
    A cell inside a spreadsheet stays on the Calc path.
    """
    table = _Acc("table", name="Table1-1")
    cell = _Acc("table cell", name="B2")
    paragraph = _Acc("paragraph", name="")
    paragraph.text = "Cell B2"
    _adopt(table, cell)
    _adopt(cell, paragraph)
    assert _atspi.writer_text_cell(cell) is True
    assert _atspi.writer_cell_text(cell) == "Cell B2"

    def replace(acc, text, force=False):
        assert force is True
        acc.text = text
        return True

    monkeypatch.setattr(_atspi, "set_text", replace)
    _atspi.replace_writer_cell_text(cell, "NEWB2-1")
    assert paragraph.text == "NEWB2-1"
    assert _atspi.writer_cell_outcome_text("NEWB2-1", cell) == "NEWB2-1"

    paragraph.text = "Cell B2"
    calls = {"n": 0}
    monkeypatch.setattr(_atspi, "_WRITER_CELL_SETTLE_S", 0.15)

    def miss(acc, text, force=False):
        assert force is True
        calls["n"] += 1
        if text == "Cell B2":
            acc.text = text
            return True
        acc.text = "NEWB2-1\nCell B2"
        return False

    monkeypatch.setattr(_atspi, "set_text", miss)
    with pytest.raises(ComputerUseError) as exc:
        _atspi.replace_writer_cell_text(cell, "NEWB2-1")
    assert exc.value.detail["reason"] == "text_mismatch"
    assert exc.value.detail["actual"] == "NEWB2-1\nCell B2"
    assert "NEWB2-1\\nCell B2" in exc.value.message
    assert paragraph.text == "Cell B2"
    assert calls["n"] == 2

    sheet = _Acc("spreadsheet", name="Sheet")
    grid = _Acc("table", name="Sheet1")
    calc = _Acc("table cell", name="B2")
    calc_text = _Acc("paragraph", name="")
    calc_text.text = "42"
    _adopt(sheet, grid)
    _adopt(grid, calc)
    _adopt(calc, calc_text)
    assert _atspi.writer_text_cell(calc) is False


def test_writer_document_set_text_confirms_from_paragraphs(fake_atspi) -> None:
    """Synthetic Writer document. The textarea node stays empty.

    Paragraph breaks compare equal across newline, CR, CRLF, VT, FF, NEL,
    U+2029, U+2028, and U+FFFC. A repeated break, a trailing break, a missing
    break, and a space stay different. A Mousepad document is not this path.
    """
    app = _Acc("application", name="soffice.bin")
    doc = _Acc("document text", name="Untitled 1 - LibreOffice Document")
    doc.text = ""
    doc.get_application = lambda: app
    blank = _Acc("paragraph", name="")
    blank.text = ""
    _adopt(doc, blank)

    def set_text_contents(text):
        children = []
        for line in text.split("\n"):
            child = _Acc("paragraph", name="")
            child.text = line
            children.append(child)
        _adopt(doc, *children)
        return True

    doc.get_editable_text_iface = lambda: doc
    doc.set_text_contents = set_text_contents
    assert _atspi.set_text(doc, "68.3755") is True
    assert _atspi.writer_document_text(doc) == "68.3755"
    value = (
        "Quarterly Update\n"
        "Revenue grew 12% compared with the previous quarter.\n"
        "We will hire two engineers in November."
    )
    assert _atspi.set_text(doc, value) is True
    assert doc.text == ""
    assert _atspi._value_text(doc, "AXTextArea", "document text") == value
    assert _atspi.writer_document_outcome_text(value, doc) == value
    assert _atspi.writer_document_matches(doc, value.replace("\n", "\r\n")) is True
    assert _atspi.writer_document_matches(doc, value.replace("\n", "\u2029")) is True
    assert _atspi.paragraph_breaks_match("a\r\nb", "a\nb") is True
    assert _atspi.paragraph_breaks_match("a\u2029b\u2028c", "a\nb\nc") is True
    assert _atspi.paragraph_breaks_match("a\ufffcb", "a\nb") is True
    assert _atspi.paragraph_breaks_match("a\u000bb", "a\nb") is True
    assert _atspi.paragraph_breaks_match("a\u000cb", "a\nb") is True
    assert _atspi.paragraph_breaks_match("a\u0085b", "a\nb") is True
    assert _atspi.paragraph_breaks_match("a\n\nb", "a\nb") is False
    assert _atspi.paragraph_breaks_match("a\r\n\nb", "a\nb") is False
    assert _atspi.paragraph_breaks_match("a b", "a\nb") is False
    assert _atspi.paragraph_breaks_match("ab", "a\nb") is False
    assert _atspi.paragraph_breaks_match("a\n", "a") is False
    ended = _Acc("paragraph", name="")
    ended.text = "Quarterly Update\u0085"
    follow = _Acc("paragraph", name="")
    follow.text = "Next\u000c"
    _adopt(doc, ended, follow)
    assert _atspi.writer_document_text(doc) == "Quarterly Update\nNext"
    mousepad = _Acc("application", name="mousepad")
    other = _Acc("document text", name="notes")
    other.text = ""
    other.get_application = lambda: mousepad
    note = _Acc("paragraph", name="")
    note.text = "68.3755"
    _adopt(other, note)
    assert _atspi.writer_document_text(other) is None
    assert _atspi.writer_document_outcome_text("68.3755", other) is None


def test_writer_type_newline_is_return_and_line_breaks_do_not_confirm(fake_atspi, monkeypatch) -> None:
    """A Writer newline is Return. U+2028 stays a line break.

    insertText of the whole string is what stored ``\\n`` as a line break
    and still reported success. The read-back of that one paragraph does
    not match the requested paragraphs. A text entry is not this path.
    """
    from a11y_computer_use.drivers import _linux_input

    app = _Acc("application", name="soffice.bin")
    doc = _Acc(
        "document text", name="Untitled 1 - LibreOffice Document", states=("FOCUSED",),
    )
    doc.text = ""
    doc.get_application = lambda: app
    first = _Acc("paragraph", name="", states=("FOCUSED",))
    first.text = ""
    _adopt(doc, first)
    current = {"node": first}
    inserted: list[str] = []
    returns: list[str] = []
    chords: list[str] = []

    def insert_text(_pos, text, _length):
        inserted.append(text)
        current["node"].text += text
        return True

    def type_string(text):
        returns.append(text)
        new = _Acc("paragraph", name="")
        new.text = ""
        current["node"] = new
        doc.children.append(new)
        new.parent = doc

    def press_chord(chord):
        chords.append(chord)
        current["node"].text += "\u2028"

    doc.insert_text = insert_text
    doc.get_editable_text_iface = lambda: doc
    first.insert_text = insert_text
    first.get_editable_text_iface = lambda: first
    monkeypatch.setattr(_atspi, "_type_string", type_string)
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    monkeypatch.setattr(_linux_input, "press_chord", press_chord)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(_atspi, "_WRITER_DOCUMENT_STABLE_S", 0.0)

    value = (
        "Quarterly Update\n"
        "Revenue grew 12% compared with the previous quarter.\n"
        "We will hire two engineers in November."
    )
    assert _atspi.insert_text(first, value) == len(value)
    assert returns == ["\n", "\n"]
    assert chords == []
    assert all("\n" not in piece for piece in inserted)
    assert [child.text for child in doc.children] == value.split("\n")
    assert _atspi.writer_document_outcome_text(value, doc) == value
    assert _atspi.writer_document_outcome_text(value, first) == value

    broken = _Acc("paragraph", name="")
    broken.text = value.replace("\n", "\u2028")
    _adopt(doc, broken)
    assert _atspi.writer_document_text(doc) == broken.text
    assert _atspi.paragraph_breaks_match(broken.text, value) is True
    assert _atspi.writer_type_conflict(doc, value) is True
    readback = _atspi.writer_document_outcome_text(value, doc)
    assert readback != value
    assert readback is not None and "\u2028" in readback

    _adopt(doc, first)
    first.text = ""
    current["node"] = first
    inserted.clear()
    returns.clear()
    chords.clear()
    assert _atspi.insert_text(first, "a\u2028b\nc") == len("a\u2028b\nc")
    assert returns == ["\n"]
    assert chords == ["shift+return"]
    assert [child.text for child in doc.children] == ["a\u2028b", "c"]
    assert _atspi.writer_document_outcome_text("a\u2028b\nc", doc) == "a\u2028b\nc"
    assert _atspi.writer_type_conflict(doc, "a\nb\nc") is True

    def stay_one_paragraph(text):
        assert text == "\n"
        current["node"].text += "\u2028"

    monkeypatch.setattr(_atspi, "_type_string", stay_one_paragraph)
    _adopt(doc, first)
    first.text = ""
    current["node"] = first
    with pytest.raises(ComputerUseError) as exc:
        _atspi.insert_text(first, "a\nb")
    assert exc.value.detail["reason"] == "text_mismatch"
    assert "inserted as line breaks" in exc.value.message
    assert "\n" not in "".join(inserted)

    entry = _Acc("entry", name="note")
    entry.text = ""

    def entry_insert(_pos, text, _length):
        entry.text += text
        return True

    entry.insert_text = entry_insert
    entry.get_editable_text_iface = lambda: entry
    assert _atspi.insert_text(entry, "a\nb") == 3
    assert entry.text == "a\nb"


def test_writer_document_waits_for_later_paragraphs_and_does_not_clear(fake_atspi, monkeypatch) -> None:
    """Later paragraphs are the read-back. A partial line is not a miss.

    The document cache is dropped on each read. The document is not selected
    or deleted while a later paragraph is still unpublished. A stable value
    that already has every break and differs returns before the deadline.
    """
    import time

    app = _Acc("application", name="soffice.bin")
    doc = _Acc("document text", name="Untitled 1 - LibreOffice Document")
    doc.text = ""
    doc.get_application = lambda: app
    deleted = {"n": 0}
    cleared = {"n": 0}

    def delete_text(*_args):
        deleted["n"] += 1
        return True

    def clear_cache():
        cleared["n"] += 1

    lines = ["Quarterly Update", "Revenue grew 12%.", "We will hire."]
    children = []
    for line in lines:
        child = _Acc("paragraph", name="")
        child.text = line
        children.append(child)
    reads = {"n": 0}

    def get_child_count():
        reads["n"] += 1
        visible = children[:1] if reads["n"] < 4 else children
        doc.children = list(visible)
        for child in doc.children:
            child.parent = doc
        return len(doc.children)

    def set_text_contents(_text):
        return True

    doc.get_editable_text_iface = lambda: doc
    doc.set_text_contents = set_text_contents
    doc.delete_text = delete_text
    doc.clear_cache = clear_cache
    doc.get_child_count = get_child_count
    value = "\n".join(lines)
    assert _atspi.set_text(doc, value) is True
    assert deleted["n"] == 0
    assert cleared["n"] > 1
    assert reads["n"] >= 4
    assert _atspi.writer_document_text(doc) == value

    wrong = []
    for line in ("Other heading", "Other body", "Other close"):
        child = _Acc("paragraph", name="")
        child.text = line
        wrong.append(child)

    def show_wrong():
        doc.children = list(wrong)
        for child in doc.children:
            child.parent = doc
        return len(doc.children)

    doc.get_child_count = show_wrong
    monkeypatch.setattr(_atspi, "_WRITER_DOCUMENT_DEADLINE_S", 2.0)
    started = time.monotonic()
    assert _atspi.set_text(doc, value) is False
    elapsed = time.monotonic() - started
    assert elapsed < 1.0
    assert deleted["n"] == 0

    def show_first():
        doc.children = [children[0]]
        children[0].parent = doc
        return 1

    doc.get_child_count = show_first
    monkeypatch.setattr(_atspi, "_WRITER_DOCUMENT_DEADLINE_S", 0.4)
    started = time.monotonic()
    assert _atspi.set_text(doc, value) is False
    elapsed = time.monotonic() - started
    assert elapsed >= 0.4
    assert elapsed < 1.0
    assert deleted["n"] == 0


def test_writer_document_without_editable_text_types_and_waits(fake_atspi, monkeypatch) -> None:
    """No EditableText, and the document text read fails. Keys still insert.

    The generic key replace treats a missing text read as a miss and deletes
    the field after a few hundredths of a second. This document is typed,
    then the paragraphs are polled. Nothing is selected.
    """
    app = _Acc("application", name="soffice.bin")
    doc = _Acc("document text", name="Untitled 1 - LibreOffice Document")
    doc.text = ""
    doc.text_error = True
    doc.get_application = lambda: app
    deleted = {"n": 0}
    typed = {"n": 0}
    value = "Quarterly Update\nRevenue grew 12%."

    def delete_text(*_args):
        deleted["n"] += 1
        return True

    def type_into(_acc, text):
        typed["n"] += 1
        assert text == value
        lines = []
        for line in text.split("\n"):
            child = _Acc("paragraph", name="")
            child.text = line
            lines.append(child)
        _adopt(doc, *lines)

    doc.delete_text = delete_text
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    monkeypatch.setattr(_atspi, "_type_into_target", type_into)
    monkeypatch.setattr(_atspi, "grab_focus", lambda _acc: True)
    assert _atspi.set_text(doc, value) is True
    assert typed["n"] == 1
    assert deleted["n"] == 0
    assert _atspi.writer_document_text(doc) == value

    bare = _Acc("document text", name="Untitled 2 - LibreOffice Document")
    bare.text = ""
    bare.get_application = lambda: app
    bare.delete_text = delete_text
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: False)
    assert _atspi.set_text(bare, value) is False
    assert deleted["n"] == 0
    assert typed["n"] == 1


def _writer_memo() -> str:
    return (
        "Quarterly Update\n"
        "Revenue grew 12% compared with the previous quarter.\n"
        "We will hire two engineers in November."
    )


def _writer_doc():
    app = _Acc("application", name="soffice.bin")
    doc = _Acc(
        "document text",
        name="Untitled 1 - LibreOffice Document",
        states=("FOCUSED", "EDITABLE"),
    )
    doc.text = ""
    doc.get_application = lambda: app
    blank = _Acc("paragraph", name="")
    blank.text = ""
    _adopt(doc, blank)
    return doc


def _install_writer_lines(doc, text: str) -> None:
    lines = []
    for line in text.split("\n"):
        child = _Acc("paragraph", name="")
        child.text = line
        lines.append(child)
    _adopt(doc, *lines)


def test_writer_insert_text_confirms_from_paragraphs_not_the_empty_node(fake_atspi, monkeypatch) -> None:
    """Synthetic Writer document. The textarea node stays empty.

    EditableText inserts each line. Return, injected here, starts the next
    paragraph. No X display is opened. The document text interface stays
    ``""``. That is a confirm, not a text_mismatch. An insert that does not
    change the paragraphs returns None so the caller can send keys, and
    does not report success.
    """
    memo = _writer_memo()
    doc = _writer_doc()
    monkeypatch.setattr(_atspi, "_WRITER_DOCUMENT_STABLE_S", 0)
    monkeypatch.setattr(_atspi, "_WRITER_DOCUMENT_DEADLINE_S", 0.4)
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    cursor = {"node": doc.children[0]}

    def insert_text(_pos, text, _length):
        cursor["node"].text += text
        return True

    def type_string(text):
        assert text == "\n"
        new = _Acc("paragraph", name="")
        new.text = ""
        doc.children.append(new)
        new.parent = doc
        cursor["node"] = new

    doc.get_editable_text_iface = lambda: doc
    doc.insert_text = insert_text
    monkeypatch.setattr(_atspi, "_type_string", type_string)
    assert _atspi.insert_text(doc, memo) == len(memo)
    assert doc.text == ""
    assert _atspi.writer_document_text(doc) == memo
    assert _atspi._writer_typed_visible("", memo.replace("\n", "\u2029"), memo) is True
    assert _atspi._writer_typed_visible("other", "other", memo) is False

    empty = _writer_doc()

    def insert_nothing(_pos, _text, _length):
        return False

    empty.get_editable_text_iface = lambda: empty
    empty.insert_text = insert_nothing
    monkeypatch.setattr(_atspi, "_type_string", lambda _text: None)
    assert _atspi.insert_text(empty, memo) is None
    assert _atspi.writer_document_text(empty) == ""


def test_type_into_writer_multiline_confirms_when_paragraphs_land(fake_atspi, monkeypatch) -> None:
    """Synthetic type with a ref. No live LibreOffice.

    The document has no EditableText. Keys create three paragraphs and the
    document node still reads ``""``. The type confirms. A document whose
    paragraphs are a different line is text_mismatch, and the actual text
    is that line.
    """
    from a11y_computer_use import observe

    memo = _writer_memo()
    doc = _writer_doc()
    monkeypatch.setattr(_atspi, "_TYPE_SETTLE_PAUSE_S", 0)
    monkeypatch.setattr(_atspi, "_TYPE_SETTLE_POLLS", 2)
    monkeypatch.setattr(_atspi, "_WRITER_DOCUMENT_STABLE_S", 0)
    monkeypatch.setattr(_atspi, "_WRITER_DOCUMENT_DEADLINE_S", 0.4)
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    monkeypatch.setattr(_atspi, "grab_focus", lambda _acc: True)
    monkeypatch.setattr(_atspi, "peer_field_texts", lambda _acc: {})
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: doc)

    def type_into(_acc, text):
        _install_writer_lines(doc, text)

    monkeypatch.setattr(_atspi, "_type_into_target", type_into)
    driver = LinuxDriver()
    driver._run = lambda fn: fn()
    element = Element(
        "e20", "AXTextArea", "Untitled 1 - LibreOffice Document", "",
        Bounds(0, 10, 40, 400, 300), "snap-1", editable=True,
    )
    assert driver.type_into(element, memo) == len(memo)
    assert doc.text == ""
    assert _atspi.writer_document_text(doc) == memo

    missed = _writer_doc()

    def type_wrong(_acc, _text):
        _install_writer_lines(missed, "Not the memo")

    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: missed)
    monkeypatch.setattr(_atspi, "_type_into_target", type_wrong)
    with pytest.raises(ComputerUseError) as exc:
        driver.type_into(element, memo)
    assert exc.value.detail["reason"] == "text_mismatch"
    assert exc.value.detail["actual"] == "Not the memo"
    assert repr(memo) in exc.value.message


def test_type_text_writer_multiline_waits_for_later_paragraphs(fake_atspi, monkeypatch) -> None:
    """Synthetic type with no ref. The first paragraph reads are partial.

    Focus text stays empty. Later paragraphs arrive on the third document
    read. The type confirms only once every line is there. A document that
    stays empty is text_mismatch with an empty actual, not a success.
    """
    from a11y_computer_use.drivers import _linux_input

    memo = _writer_memo()
    doc = _writer_doc()
    lines = memo.split("\n")
    children = []
    for line in lines:
        child = _Acc("paragraph", name="")
        child.text = line
        children.append(child)
    reads = {"n": 0}

    def get_child_count():
        reads["n"] += 1
        visible = children[:1] if reads["n"] < 3 else children
        doc.children = list(visible)
        for child in doc.children:
            child.parent = doc
        return len(doc.children)

    doc.get_child_count = get_child_count
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(_atspi, "_TYPE_SETTLE_PAUSE_S", 0)
    monkeypatch.setattr(_atspi, "_TYPE_SETTLE_POLLS", 2)
    monkeypatch.setattr(_atspi, "_WRITER_DOCUMENT_STABLE_S", 0)
    monkeypatch.setattr(_atspi, "_WRITER_DOCUMENT_DEADLINE_S", 1.0)
    monkeypatch.setattr(_atspi, "focused_editable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_atspi, "focused_secure", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(_atspi, "focused_text", lambda _app: "")
    monkeypatch.setattr(_atspi, "spreadsheet_open", lambda _app: False)
    monkeypatch.setattr(_atspi, "sheet_editor_text", lambda _app: None)
    monkeypatch.setattr(_atspi, "_focused_node", lambda _app, **_kwargs: (doc, False))
    monkeypatch.setattr(_linux_input, "type_string", lambda _text: None)
    driver = LinuxDriver()
    driver._focused_editable = None
    driver.frontmost_app = lambda: ("soffice.bin", 1)
    assert driver.type_text(memo) == len(memo)
    assert reads["n"] >= 3
    assert _atspi.writer_document_text(doc) == memo

    empty = _writer_doc()
    monkeypatch.setattr(_atspi, "_focused_node", lambda _app, **_kwargs: (empty, False))
    monkeypatch.setattr(_atspi, "_WRITER_DOCUMENT_DEADLINE_S", 0.2)
    with pytest.raises(ComputerUseError) as exc:
        driver.type_text(memo)
    assert exc.value.detail["reason"] == "text_mismatch"
    assert exc.value.detail["actual"] == ""


def _drawing_deck(role="document presentation", name="Untitled 1 - LibreOffice Presentation"):
    """Impress or Draw focus: the document is focused, the frame's paragraph is not."""
    app = _Acc("application", name="soffice.bin")
    doc = _Acc(role, name=name, states=("FOCUSED", "SHOWING"))
    doc.text = ""
    doc.get_application = lambda: app
    panel = _Acc("panel", name="PresentationSubtitle", states=("FOCUSED", "SHOWING"))
    paragraph = _Acc("paragraph", name="", states=("SHOWING",))
    paragraph.text = "Click to add Text"
    paragraph.get_editable_text_iface = lambda: paragraph
    _adopt(panel, paragraph)
    _adopt(doc, panel)
    _adopt(app, doc)
    return doc, panel, paragraph


def test_focused_editable_descends_to_the_impress_paragraph(fake_atspi, monkeypatch) -> None:
    """The presentation container is focused and has no EditableText.

    The paragraph in the focused text frame is the type target. A Draw page
    uses document frame and does the same. The slide sorter and a browser
    document frame do not.
    """
    doc, _panel, paragraph = _drawing_deck()
    monkeypatch.setattr(_atspi, "_focused_node", lambda *_args, **_kwargs: (doc, False))
    assert _atspi.focused_editable("soffice.bin") is paragraph

    draw, _draw_panel, draw_paragraph = _drawing_deck(
        "document frame", "Untitled 1 - LibreOffice Drawing",
    )
    monkeypatch.setattr(_atspi, "_focused_node", lambda *_args, **_kwargs: (draw, False))
    assert _atspi.focused_editable("soffice.bin") is draw_paragraph

    app = _Acc("application", name="soffice.bin")
    slides = _Acc("document frame", name="Slides View", states=("FOCUSED",))
    slides.text = ""
    slides.get_application = lambda: app
    slides.parent = app
    monkeypatch.setattr(_atspi, "_focused_node", lambda *_args, **_kwargs: (slides, False))
    assert _atspi.focused_editable("soffice.bin") is None

    chrome = _Acc("application", name="chrome")
    frame = _Acc("document frame", name="page", states=("FOCUSED",))
    frame.text = ""
    frame.get_application = lambda: chrome
    frame.parent = chrome
    monkeypatch.setattr(_atspi, "_focused_node", lambda *_args, **_kwargs: (frame, False))
    assert _atspi.focused_editable("chrome") is None


class _Grab:
    """Component.grab_focus sets FOCUSED on the node it was given."""

    def __init__(self, node):
        self.node = node

    def grab_focus(self):
        self.node.states.add("FOCUSED")
        return True


def _shape_paragraph(text, *, states, role="document frame", doc_name="Untitled 1 - LibreOffice Drawing",
                     app_name="soffice.bin", iface=True, read_only=False):
    """A shape panel and its paragraph under an Impress slide or a Draw page."""
    app = _Acc("application", name=app_name)
    doc = _Acc(role, name=doc_name, states=("SHOWING",))
    doc.get_application = lambda: app
    held = set(states)
    if read_only:
        held.add("READ_ONLY")
    panel = _Acc("panel", name="EllipseB", states=("SHOWING",))
    paragraph = _Acc("paragraph", name="", states=held)
    paragraph.text = text
    if iface:
        def set_text_contents(value, _paragraph=paragraph):
            _paragraph.text = value
            return True

        def insert_text(pos, value, length, _paragraph=paragraph):
            body = value[:int(length)]
            _paragraph.text = _paragraph.text[:int(pos)] + body + _paragraph.text[int(pos):]
            return True

        paragraph.set_text_contents = set_text_contents
        paragraph.insert_text = insert_text
        paragraph.get_editable_text_iface = lambda: paragraph
    _adopt(panel, paragraph)
    _adopt(doc, panel)
    _adopt(app, doc)
    return panel, paragraph


def test_draw_shape_paragraph_is_an_edit_target_and_a_browser_paragraph_is_not(
    fake_atspi, monkeypatch,
) -> None:
    """A Draw or Impress paragraph with EDITABLE and EditableText is edit.

    The role stays static text. set_value replaces through set_text_contents
    and does not select. type(ref) inserts at the caret. A paragraph that
    lacks the state or the interface is not edit. A Writer paragraph and a
    Firefox paragraph stay refused, including when the snapshot flag was set.
    The shape panel is not an entry.
    """
    from a11y_computer_use import observe

    monkeypatch.setattr(_atspi, "_extents", lambda acc, keep_zero=False: ((4.0, 8.0), (120.0, 24.0)))
    monkeypatch.setattr(_atspi, "_action_names", lambda acc: ())
    monkeypatch.setattr(_atspi, "_value_text", lambda acc, role, role_name=None: getattr(acc, "text", None))
    panel, paragraph = _shape_paragraph("ELLIPSE_B", states={"EDITABLE", "ENABLED", "SHOWING", "FOCUSABLE"})
    accessor = _atspi.ATSPIAccessor()
    accessor.libreoffice = True
    raw = accessor.read(paragraph)
    assert raw.role == "AXStaticText"
    assert raw.editable is True
    assert _atspi.drawing_paragraph_shown(paragraph) == "ELLIPSE_B"
    assert _atspi.drawing_paragraph_shown(panel) is None
    snap = build_snapshot(
        (raw, []), _FakeAccessor(), scope=Scope.WINDOW, app="soffice.bin", pid=1, geometry=_geometry(),
    )
    assert snap.elements[0].editable is True
    assert "edit" in observe.render_text(snap)
    shape = accessor.read(panel)
    assert shape.editable is False

    _panel, quiet = _shape_paragraph("ELLIPSE_B", states={"ENABLED", "SHOWING"}, iface=True)
    assert accessor.read(quiet).editable is False
    _panel, no_iface = _shape_paragraph(
        "ELLIPSE_B", states={"EDITABLE", "ENABLED"}, iface=False,
    )
    assert accessor.read(no_iface).editable is False
    _panel, frozen = _shape_paragraph(
        "ELLIPSE_B", states={"EDITABLE", "ENABLED"}, read_only=True,
    )
    assert accessor.read(frozen).editable is False

    impress_panel, impress = _shape_paragraph(
        "Alpha Slide Title", states={"EDITABLE", "ENABLED"},
        role="document presentation", doc_name="Untitled 1 - LibreOffice Presentation",
    )
    del impress_panel
    assert accessor.read(impress).editable is True

    writer = _Acc("document text", name="notes", states=("SHOWING",))
    writer_app = _Acc("application", name="soffice.bin")
    writer.get_application = lambda: writer_app
    writer_paragraph = _Acc("paragraph", name="", states={"EDITABLE", "SHOWING"})
    writer_paragraph.text = "Beta"
    writer_paragraph.get_editable_text_iface = lambda: writer_paragraph
    _adopt(writer, writer_paragraph)
    _adopt(writer_app, writer)
    assert accessor.read(writer_paragraph).editable is False
    assert _atspi.drawing_text_editable(writer_paragraph) is False

    slides = _Acc("document frame", name="Slides View", states=("SHOWING",))
    slides.get_application = lambda: writer_app
    sorter = _Acc("paragraph", name="", states={"EDITABLE", "SHOWING"})
    sorter.text = "slide"
    sorter.get_editable_text_iface = lambda: sorter
    _adopt(slides, sorter)
    assert _atspi.drawing_text_editable(sorter) is False

    browser = _Acc("application", name="Firefox")
    page = _Acc("document web", name="page", states=("SHOWING",))
    page.get_application = lambda: browser
    gecko = _Acc("paragraph", name="", states={"EDITABLE", "SHOWING"})
    gecko.text = "idle"
    gecko.get_editable_text_iface = lambda: gecko
    _adopt(page, gecko)
    _adopt(browser, page)
    plain = _atspi.ATSPIAccessor()
    assert plain.read(gecko).editable is False
    assert _atspi._blocks_text_replace(gecko) is True

    deleted = {"n": 0}

    def delete_text(*_args):
        deleted["n"] += 1
        raise AssertionError("select")

    paragraph.delete_text = delete_text
    assert _atspi.set_text(paragraph, "RECT_SET") is True
    assert paragraph.text == "RECT_SET"
    assert deleted["n"] == 0
    paragraph.text = "stuck"

    def set_stuck(_value):
        return True

    paragraph.set_text_contents = set_stuck
    assert _atspi.set_text(paragraph, "NOPE") is False
    assert paragraph.text == "stuck"
    assert deleted["n"] == 0

    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: paragraph)
    box = Bounds(0, 8, 40, 120, 20)
    element = Element(
        "e26", "AXStaticText", "Paragraph: 0 ELLIPSE_B", "ELLIPSE_B", box, "snap-1",
        editable=True,
    )
    paragraph.text = "ELLIPSE_B"
    paragraph.set_text_contents = lambda value: (_set_paragraph(paragraph, value))
    assert LinuxDriver().set_value(element, "SV_e26") is True
    assert paragraph.text == "SV_e26"
    refused = Element(
        "e26", "AXStaticText", "Paragraph: 0 ELLIPSE_B", "ELLIPSE_B", box, "snap-1",
    )
    with pytest.raises(ComputerUseError) as missing_flag:
        LinuxDriver().set_value(refused, "NO")
    assert missing_flag.value.detail["reason"] == "not_editable"
    assert paragraph.text == "SV_e26"

    assert accessor.read(panel).editable is False

    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: gecko)
    wrongly = Element("e9", "AXStaticText", "", "idle", box, "snap-1", editable=True)
    with pytest.raises(ComputerUseError) as gecko_exc:
        LinuxDriver().set_value(wrongly, "nope")
    assert gecko_exc.value.detail["reason"] == "not_editable"
    assert gecko.text == "idle"

    paragraph.component = _Grab(paragraph)
    paragraph.text = "ELLIPSE_B"
    # Draw reports offset 0 until the shape is in text edit. type(ref) appends.
    paragraph.caret = 0
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: paragraph)
    driver = LinuxDriver()
    driver._run = lambda fn: fn()
    assert driver.type_into(element, "_T") == 2
    assert paragraph.text == "ELLIPSE_B_T"
    assert paragraph.caret == len("ELLIPSE_B")

    paragraph.text = "ELLIPSE_B"
    paragraph.caret = 0
    paragraph.states.add("FOCUSED")
    assert driver.type_into(element, "Q") == 1
    assert paragraph.text == "QELLIPSE_B"
    assert paragraph.caret == 0
    paragraph.states.discard("FOCUSED")

    bare = _shape_paragraph("ELLIPSE_B", states={"EDITABLE", "ENABLED", "SHOWING"})[1]
    bare.component = None
    parent = bare.parent
    parent.component = _Grab(parent)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: bare)
    assert driver.type_into(element, "Z") == 1
    assert bare.text.endswith("Z")


def _set_paragraph(paragraph, value):
    paragraph.text = value
    return True


def test_drawing_type_confirms_multiline_paragraphs(fake_atspi, monkeypatch) -> None:
    """Synthetic Impress. No display and no LibreOffice.

    Return splits the frame into paragraphs. The document text stays empty.
    Newline, U+2028, and that paragraph break compare as one break. ``ab``
    is not ``a\\nb``. A frame that was read and is still empty is a
    mismatch. A frame with no paragraph cannot be read, so the empty
    container is unverified instead of text_mismatch.
    """
    from a11y_computer_use.drivers import _linux_input

    doc, panel, _paragraph = _drawing_deck()
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(_atspi, "_DRAWING_TYPE_DEADLINE_S", 0)
    monkeypatch.setattr(_atspi, "_DRAWING_TYPE_PAUSE_S", 0)
    monkeypatch.setattr(_atspi, "_focused_node", lambda *_args, **_kwargs: (doc, False))
    monkeypatch.setattr(_atspi, "focused_secure", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(_linux_input, "type_string", lambda text: _install_frame_lines(panel, text))
    assert _atspi.drawing_typed_visible("", "A\u2028B", "A\nB") is True
    assert _atspi.drawing_typed_visible("", "AB", "A\nB") is False
    assert _atspi.drawing_typed_visible("", "A\n\nB", "A\nB") is False
    assert _atspi.drawing_typed_visible("A b", "A b", "A\nB") is False

    driver = LinuxDriver()
    driver._focused_editable = None
    driver.frontmost_app = lambda: ("soffice.bin", 1)
    assert driver.type_text("North\nSouth") == len("North\nSouth")
    assert _atspi.drawing_focus_text("soffice.bin") == ("text", "North\nSouth")
    assert driver._type_unverified is False

    monkeypatch.setattr(_linux_input, "type_string", lambda _text: _install_frame_lines(panel, "AB"))
    with pytest.raises(ComputerUseError) as exc:
        driver.type_text("A\nB")
    assert exc.value.detail["reason"] == "text_mismatch"
    assert exc.value.detail["actual"] == "AB"

    monkeypatch.setattr(_linux_input, "type_string", lambda _text: _install_frame_lines(panel, ""))
    with pytest.raises(ComputerUseError) as empty:
        driver.type_text("A\nB")
    assert empty.value.detail["reason"] == "text_mismatch"
    assert empty.value.detail["actual"] == ""

    bare = _Acc("document presentation", name="deck", states=("FOCUSED",))
    bare.text = ""
    bare.get_application = doc.get_application
    monkeypatch.setattr(_atspi, "_focused_node", lambda *_args, **_kwargs: (bare, False))
    sent: list[str] = []
    monkeypatch.setattr(_linux_input, "type_string", lambda text: sent.append(text))
    assert driver.type_text("North\nSouth") == len("North\nSouth")
    assert sent == ["North\nSouth"]
    assert driver._type_unverified is True


def _install_frame_lines(panel, text: str) -> None:
    children = []
    for line in (text.split("\n") if text else [""]):
        child = _Acc("paragraph", name="", states=("SHOWING",))
        child.text = line
        child.get_editable_text_iface = lambda: child
        children.append(child)
    if children:
        children[-1].states.add("FOCUSED")
        children[-1].states.add("ACTIVE")
    _adopt(panel, *children)


def test_drawing_unverified_outcome_is_not_a_mismatch(tmp_path, monkeypatch) -> None:
    """The judge says unverified when the frame cannot be read.

    A frame that shows the typed lines is confirmed. Neither result is
    text_mismatch.
    """
    from a11y_computer_use import safety, server

    driver = LinuxDriver()
    runtime = server.Runtime(
        store=safety.PermissionStore(tmp_path / "permissions.json"),
        audit=safety.AuditLog(tmp_path / "audit"),
        driver=driver,
    )
    driver._type_unverified = True
    driver._drawing_type_state = "unreadable"
    driver._drawing_type_before = None
    monkeypatch.setattr(_atspi, "drawing_frame_outcome_text", lambda *_args, **_kwargs: None)
    verdict = runtime._judge_mutation("soffice.bin", None, None, "North\nSouth")
    assert verdict == ("unverifiable", "unverified")
    assert driver._type_unverified is False

    driver._drawing_type_state = "text"
    driver._drawing_type_before = ""
    monkeypatch.setattr(
        _atspi, "drawing_frame_outcome_text", lambda *_args, **_kwargs: "North\nSouth",
    )
    confirmed = runtime._judge_mutation("soffice.bin", None, None, "North\nSouth")
    assert confirmed == ("confirmed", "read back 'North\\nSouth'")


def test_writer_cell_replace_keeps_the_rebuilt_paragraph(fake_atspi, monkeypatch) -> None:
    """The new paragraph in front of the old line is the committed edit.

    Writer can rebuild the cell child the write was aimed at. The first
    walk then shows ``NEWB2-1`` and ``Cell B2``. That is not a miss: the
    new paragraph stays and the old line is cleared.
    """
    table = _Acc("table", name="Table1-1")
    cell = _Acc("table cell", name="B2")
    paragraph = _Acc("paragraph", name="")
    paragraph.text = "Cell B2"
    _adopt(table, cell)
    _adopt(cell, paragraph)
    fresh = {"node": None}

    def write(acc, text, force=False):
        assert force is True
        if text == "":
            acc.text = ""
            return True
        if fresh["node"] is None:
            created = _Acc("paragraph", name="")
            created.text = text
            fresh["node"] = created
            _adopt(cell, created, paragraph)
            return False
        acc.text = text
        return True

    monkeypatch.setattr(_atspi, "set_text", write)
    _atspi.replace_writer_cell_text(cell, "NEWB2-1")
    assert fresh["node"] is not None and fresh["node"].text == "NEWB2-1"
    assert paragraph.text == ""
    assert _atspi.writer_cell_text(cell) == "NEWB2-1"


@pytest.mark.parametrize("value", ["150%", "150 %", "200%", "150"])
def test_named_percent_slider_explains_unknown_scale_without_writing(fake_atspi, monkeypatch, value):
    slider = _Acc("slider", name="185%", value=0.5,
                  minimum=-2.38087, maximum=1.91581)
    def forbid_write(*_args):
        raise AssertionError("unknown percent scale must not be written")

    monkeypatch.setattr(fake_atspi.Value, "set_current_value", forbid_write)
    with pytest.raises(ValueError) as exc:
        _atspi.set_numeric_value(slider, value)
    message = str(exc.value)
    assert "185%" in message
    assert "non-percent scale" in message
    assert "-2.38087..1.91581" in message
    assert "conversion is unknown" in message
    assert slider.value == 0.5


def test_named_percent_slider_keeps_verified_raw_values(fake_atspi):
    slider = _Acc("slider", name="185%", value=0.5,
                  minimum=-2.38087, maximum=1.91581)
    assert _atspi.set_numeric_value(slider, "0.25") is True
    assert slider.value == 0.25
    ordinary = _Acc("slider", name="Volume", value=50, minimum=0, maximum=100)
    assert _atspi.set_numeric_value(ordinary, "75") is True
    assert ordinary.value == 75


def test_missing_value_interface_is_not_numeric_and_a_real_range_still_is(
    fake_atspi, monkeypatch,
) -> None:
    """A node with no Value interface is not checked as a number.

    AT-SPI reports the interface missing. Minimum and maximum are not read:
    that read is an uninitialized double, about 6.93e-310. set_numeric_value
    returns false before parsing, and set_value on the group is not_editable.
    The same double is not a range when an interface object is handed back.
    A panel whose minimum and maximum are a real range stays numeric.
    """
    junk = 6.93985e-310
    panel = _Acc("panel", name="EllipseB", value=junk, minimum=junk, maximum=junk)
    panel.get_value_iface = lambda: None
    probed = {"n": 0}
    original_min = fake_atspi.Value.get_minimum_value
    original_max = fake_atspi.Value.get_maximum_value

    def counting_min(acc):
        probed["n"] += 1
        return original_min(acc)

    def counting_max(acc):
        probed["n"] += 1
        return original_max(acc)

    fake_atspi.Value.get_minimum_value = staticmethod(counting_min)
    fake_atspi.Value.get_maximum_value = staticmethod(counting_max)
    try:
        assert _atspi._value_interface_present(panel) is False
        assert _atspi._value_range(panel) is None
        assert _atspi.control_kind(panel) is None
        assert _atspi.set_numeric_value(panel, "NOPE") is False
        assert probed["n"] == 0
    finally:
        fake_atspi.Value.get_minimum_value = staticmethod(original_min)
        fake_atspi.Value.get_maximum_value = staticmethod(original_max)

    handed = _Acc("panel", name="EllipseB", value=junk, minimum=junk, maximum=junk)
    handed.get_value_iface = lambda: handed
    assert _atspi._value_range(handed) is None
    assert _atspi.control_kind(handed) is None
    assert _atspi.set_numeric_value(handed, "NOPE") is False

    box = Bounds(0, 100, 80, 100, 80)
    element = Element("e25", "AXGroup", "EllipseB", None, box, "snap-1")
    target = {"node": handed}
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: target["node"])
    driver = LinuxDriver()
    driver._run = lambda fn: fn()
    with pytest.raises(ComputerUseError) as group_exc:
        driver.set_value(element, "NOPE")
    assert group_exc.value.detail["reason"] == "not_editable", group_exc.value
    assert "not a number" not in str(group_exc.value)
    assert "e-310" not in str(group_exc.value)
    target["node"] = panel
    with pytest.raises(ComputerUseError) as missing_exc:
        driver.set_value(element, "NOPE")
    assert missing_exc.value.detail["reason"] == "not_editable", missing_exc.value
    assert "e-310" not in str(missing_exc.value)

    meter = _Acc("filler", name="Meter", value=1.0, minimum=0.0, maximum=10.0)
    assert _atspi.control_kind(meter) == "value"
    with pytest.raises(ValueError, match="not a number") as numeric:
        _atspi.set_numeric_value(meter, "NOPE")
    assert "0" in str(numeric.value) and "10" in str(numeric.value)
    assert "e-310" not in str(numeric.value)


def test_dialog_and_checkbox_drop_uninitialized_doubles_and_keep_small_values(fake_atspi) -> None:
    """Synthetic AT-SPI nodes. Not a live LibreOffice.

    A dialog and a checkbox whose CurrentValue is a subnormal double publish
    no value. Zero, 0.001, and 1e-6 stay. A slider at 0 and a spin button at
    0.001 stay. A filler keeps the subnormal, because that role is not a
    dialog or a checkbox.
    """
    junk = 6.9305862814588e-310
    dialog = _Acc("dialog", name="Text Import", value=junk)
    box = _Acc("check box", name="Comma", value=junk)
    assert _atspi._value_text(dialog, "AXDialog", "dialog") is None
    assert _atspi._value_text(box, "AXCheckBox", "check box") is None
    dialog.value = 0.0
    box.value = 0.001
    assert _atspi._value_text(dialog, "AXDialog", "dialog") == 0.0
    assert _atspi._value_text(box, "AXCheckBox", "check box") == 0.001
    tip = _Acc("alert", name="Tip of the Day", value=1e-6)
    assert _atspi._value_text(tip, "AXDialog", "alert") == 1e-6
    broken = _Acc("dialog", name="Broken", value=float("nan"))
    assert _atspi._value_text(broken, "AXDialog", "dialog") is None
    slider = _Acc("slider", name="Volume", value=0.0, minimum=0.0, maximum=1.0)
    assert _atspi._value_text(slider, "AXSlider", "slider") == 0.0
    spin = _Acc("spin button", name="Count", value=0.001, minimum=0.0, maximum=10.0)
    assert _atspi._value_text(spin, "AXSpinButton", "spin button") == 0.001
    filler = _Acc("filler", name="Meter", value=junk)
    assert _atspi._value_text(filler, "AXGroup", "filler") == junk


def test_sheet_outcome_reads_the_editor_or_the_formula_not_the_display(
    fake_atspi, monkeypatch
) -> None:
    """Synthetic cell. The displayed number is not the formula read-back."""
    cell = _Acc("table cell", name="F2")
    cell.text = "42"
    cell.get_attributes = lambda: {"formula": "E2+31"}
    monkeypatch.setattr(_atspi, "sheet_editor_text", lambda _app: None)
    assert _atspi.sheet_outcome_text("soffice.bin", "=E2+31", cell) == "=E2+31"
    assert _atspi.sheet_outcome_text("soffice.bin", "42", cell) is None
    assert _atspi.sheet_outcome_text("soffice.bin", "=Z9", cell) is None
    assert _atspi.sheet_outcome_text("gedit", "=E2+31", cell) is None
    monkeypatch.setattr(_atspi, "sheet_editor_text", lambda _app: "Résumé ✓")
    assert _atspi.sheet_outcome_text("soffice.bin", "Résumé ✓", cell) == "Résumé ✓"
    plain = _Acc("table cell", name="D1")
    plain.text = "setv"
    monkeypatch.setattr(_atspi, "sheet_editor_text", lambda _app: None)
    assert _atspi.sheet_outcome_text("soffice.bin", "setv", plain) is None


def test_outcome_confirms_a_calc_formula_the_cell_displays_as_a_number(
    fake_atspi, monkeypatch
) -> None:
    """The snapshot value is the computed number. The outcome uses the formula."""
    from a11y_computer_use import outcome, server
    from a11y_computer_use.schema import Display, Snapshot

    cell = _Acc("table cell", name="F2")
    cell.text = "42"
    cell.get_attributes = lambda: {"formula": "E2+31"}
    monkeypatch.setattr(_atspi, "sheet_editor_text", lambda _app: None)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: cell)
    element = Element(
        "e2", "AXCell", "F2", "42", Bounds(0, 10, 10, 40, 16), "after",
        path=("AXWindow", "AXCell"),
    )
    before_el = Element(
        "e2", "AXCell", "F2", "", Bounds(0, 10, 10, 40, 16), "before",
        path=("AXWindow", "AXCell"),
    )

    class _Driver:
        name = "linux"

        def snapshot(self, scope, app):
            del scope, app
            return Snapshot(
                snapshot_id="after",
                scope=Scope.WINDOW,
                app="soffice.bin",
                pid=1,
                created_at=0.0,
                displays=(Display(0, 800, 600, 1.0, True),),
                elements=(element,),
            )

    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = _Driver()
    runtime._current = before_el
    before = {
        "snap": Snapshot(
            snapshot_id="before",
            scope=Scope.WINDOW,
            app="soffice.bin",
            pid=1,
            created_at=0.0,
            displays=(Display(0, 800, 600, 1.0, True),),
            elements=(before_el,),
        ),
        "state": "before",
        "bounds": "before",
        "pid": 1,
        "app": "soffice.bin",
        "pid_alive": False,
    }
    judged, evidence = runtime._judge_mutation(
        "soffice.bin", before, before_el, "=E2+31", previous="",
    )
    assert judged == "confirmed", evidence
    assert evidence == "read back '=E2+31'"
    monkeypatch.setattr(_atspi, "sheet_editor_text", lambda _app: "Résumé ✓")
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: None)
    typed, typed_evidence = runtime._judge_mutation(
        "soffice.bin", before, None, "Résumé ✓", previous="",
    )
    assert typed == "confirmed", typed_evidence
    assert "Résumé ✓" in typed_evidence
    monkeypatch.setattr(_atspi, "sheet_editor_text", lambda _app: None)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: cell)
    untouched, untouched_evidence = runtime._judge_mutation(
        "soffice.bin", before, before_el, "=Z9", previous="",
    )
    assert untouched == "partial"
    assert "42" in untouched_evidence
    assert outcome.judge(changed=False, requested="=Z9", readback="42", before_value="")[0] == "partial"


def test_huge_calc_table_uses_accessible_at(fake_atspi) -> None:
    """Synthetic table. Child count is INT_MAX; row-major children are not the cells."""

    class _Table:
        @staticmethod
        def get_n_rows(_acc):
            return 1048576

        @staticmethod
        def get_n_columns(_acc):
            return 16384

        @staticmethod
        def get_accessible_at(acc, row, col):
            return acc.cells.get((row, col))

    fake_atspi.Table = _Table
    try:
        table = _Acc("table", name="Sheet")
        table.get_child_count = lambda: 2147483647
        table.get_child_at_index = lambda _index: _Acc("table cell", name="ROW1")
        table.cells = {}
        for row in range(4):
            for col in range(8):
                cell = _Acc(
                    "table cell", name=f"{chr(ord('A') + col)}{row + 1}",
                    width=20, height=16,
                )
                table.cells[(row, col)] = cell
        names = [kid.get_name() for kid in _atspi.ATSPIAccessor().children(table)]
        assert "A1" in names and "B2" in names and "E1" in names
        assert "ROW1" not in names
    finally:
        del fake_atspi.Table


def _col_letters(index: int) -> str:
    """0 → A, 15 → P, 26 → AA."""
    number = index + 1
    letters = []
    while number:
        number, rem = divmod(number - 1, 26)
        letters.append(chr(ord("A") + rem))
    return "".join(reversed(letters))


class _Click:
    def get_n_actions(self):
        return 1

    def get_action_name(self, _index):
        return "click"


def _calc_grid(
    fake_atspi,
    *,
    origin_row: int = 0,
    visible_rows: int = 29,
    visible_cols: int = 16,
    pitch_x: float = 60.0,
    pitch_y: float = 20.0,
    origin: tuple[float, float] = (80.0, 140.0),
    hits: bool = False,
):
    """A million-row sheet whose painted box holds ``visible_rows`` × ``visible_cols``.

    Cell ``(origin_row, 0)`` sits at the top of the box. Later rows step down
    by ``pitch_y`` and leave the box. ``get_child_at_index`` is not a cell.
    """
    ox, oy = origin
    width = visible_cols * pitch_x
    height = visible_rows * pitch_y
    calls: list[tuple[int, int]] = []
    index_reads = {"n": 0}
    hit_reads = {"n": 0}
    cache: dict[tuple[int, int], _Acc] = {}

    def cell(row: int, col: int) -> _Acc:
        key = (row, col)
        found = cache.get(key)
        if found is not None:
            return found
        made = _Acc(
            "table cell",
            name=f"{_col_letters(col)}{row + 1}",
            states=("ENABLED", "SENSITIVE", "FOCUSABLE"),
            width=pitch_x,
            height=pitch_y,
        )
        made.component = _Geom(
            pitch_x, pitch_y, x=ox + col * pitch_x, y=oy + (row - origin_row) * pitch_y,
        )
        made.get_action_iface = lambda: _Click()
        if row == origin_row + 10 and col == 0:
            made.text = "Row10"
        cache[key] = made
        return made

    table = _Acc("table", name="Sheet", width=width, height=height)
    table.component = _Geom(width, height, x=ox, y=oy)
    table.calls = calls
    table.index_reads = index_reads
    table.hit_reads = hit_reads

    def child_count():
        return 2147483647

    def child_at(_index):
        index_reads["n"] += 1
        return _Acc("table cell", name="ROW1")

    table.get_child_count = child_count
    table.get_child_at_index = child_at

    if hits:
        class _Hits(_Geom):
            def get_accessible_at_point(self, x, y, _coord):
                hit_reads["n"] += 1
                if x < ox or y < oy or x >= ox + width or y >= oy + height:
                    return None
                col = int((x - ox) // pitch_x)
                row = origin_row + int((y - oy) // pitch_y)
                return cell(row, col)

        table.component = _Hits(width, height, x=ox, y=oy)

    class _Table:
        @staticmethod
        def get_n_rows(_acc):
            return 1048576

        @staticmethod
        def get_n_columns(_acc):
            return 16384

        @staticmethod
        def get_accessible_at(_acc, row, col):
            calls.append((int(row), int(col)))
            if row < 0 or col < 0:
                return None
            return cell(row, col)

    fake_atspi.Table = _Table
    return table


def _sheet_snapshot(table):
    geometry = (
        DisplayGeometry(display=Display(0, 1280, 800, 1.0, True), origin=(0.0, 0.0)),
    )
    return build_snapshot(
        table, _atspi.ATSPIAccessor(), scope=Scope.WINDOW, app="soffice", pid=1, geometry=geometry,
    )


def test_huge_calc_sheet_lists_the_visible_rows(fake_atspi, monkeypatch) -> None:
    """Synthetic sheet. The on-screen box is 29×16; row 7 and A29 stay in the snapshot.

    Child count is INT_MAX. The first 96 row-major cells would be A1..P6.
    Extents name the visible rows, so find sees A29 and the text in A11,
    and the ref has a box. Probes stay far below the million-row sheet.
    """
    monkeypatch.setattr(_atspi, "_screen_size", lambda: (1280, 800))
    table = _calc_grid(fake_atspi)
    try:
        cells = _atspi._sheet_window_cells(table)
        names = {_atspi._node_name(cell) for cell in cells}
        assert len(cells) == 29 * 16
        assert {"A1", "B2", "A7", "A11", "P6", "A29", "P29"} <= names
        assert "A30" not in names and "Q1" not in names and "ROW1" not in names
        assert table.index_reads["n"] == 0
        assert table.calls and max(row for row, _col in table.calls) < 200
        assert len(table.calls) < 800

        snap = _sheet_snapshot(table)
        titles = {el.title for el in snap.elements}
        assert "A29" in titles and "A11" in titles and "A7" in titles
        assert "A30" not in titles and "ROW1" not in titles
        row10 = observe.find_elements(snap, text="Row10")
        assert any(el.title == "A11" and el.value == "Row10" for el in row10)
        found = observe.find_elements(snap, text="A29")
        assert len(found) == 1 and found[0].clickable
        assert found[0].bounds is not None and found[0].bounds.width > 0 and found[0].bounds.height > 0
        assert "A29" in observe.render_text(snap, mode="full")
        assert "A29" in observe.render_text(snap, mode="interactive")
        assert "Row10" in observe.render_text(snap, mode="full")
    finally:
        del fake_atspi.Table


def test_huge_calc_sheet_follows_a_scrolled_visible_band(fake_atspi, monkeypatch) -> None:
    """Synthetic sheet scrolled so row 5001 is the first painted row."""
    monkeypatch.setattr(_atspi, "_screen_size", lambda: (1280, 800))
    table = _calc_grid(fake_atspi, origin_row=5000)
    try:
        names = {_atspi._node_name(cell) for cell in _atspi._sheet_window_cells(table)}
        assert "A5001" in names and "A5029" in names and "B5002" in names
        assert "A5000" not in names and "A5030" not in names and "ROW1" not in names
        assert table.index_reads["n"] == 0
        assert max(row for row, _col in table.calls) < 20000
        assert not any(row > 100000 for row, _col in table.calls)
        assert len(table.calls) < 800
    finally:
        del fake_atspi.Table


def test_huge_calc_sheet_hit_test_names_the_span_when_extents_do_not(fake_atspi, monkeypatch) -> None:
    """Synthetic sheet. Extent search is unavailable; hit-testing the box still lists A29."""
    monkeypatch.setattr(_atspi, "_screen_size", lambda: (1280, 800))
    monkeypatch.setattr(_atspi, "_sheet_span_from_extents", lambda *_args, **_kwargs: None)
    table = _calc_grid(fake_atspi, hits=True)
    try:
        names = {_atspi._node_name(cell) for cell in _atspi._sheet_window_cells(table)}
        assert table.hit_reads["n"] > 0
        assert "A1" in names and "A7" in names and "A29" in names and "P29" in names
        assert "A30" not in names and "ROW1" not in names
        snap = _sheet_snapshot(table)
        assert "A29" in {el.title for el in snap.elements}
        assert observe.find_elements(snap, text="A11")
    finally:
        del fake_atspi.Table


def test_huge_calc_sheet_with_a_short_visible_box_does_not_pad_to_96(fake_atspi, monkeypatch) -> None:
    """Synthetic sheet. Three visible rows are not filled out to A1..P6."""
    monkeypatch.setattr(_atspi, "_screen_size", lambda: (1280, 800))
    table = _calc_grid(fake_atspi, visible_rows=3, visible_cols=4)
    try:
        names = [_atspi._node_name(cell) for cell in _atspi._sheet_window_cells(table)]
        assert len(names) == 12
        assert set(names) == {
            "A1", "B1", "C1", "D1",
            "A2", "B2", "C2", "D2",
            "A3", "B3", "C3", "D3",
        }
        assert table.index_reads["n"] == 0
    finally:
        del fake_atspi.Table


def test_small_table_is_walked_by_child_index(fake_atspi) -> None:
    """A short table keeps the children it already exposed. Not a sheet sample."""
    first = _Acc("table cell", name="A1", width=20, height=16)
    later = _Acc("table cell", name="A7", width=20, height=16)
    table = _Acc("table", name="grid")
    _adopt(table, first, later)
    names = [kid.get_name() for kid in _atspi.ATSPIAccessor().children(table)]
    assert names == ["A1", "A7"]


def test_sheet_editor_paragraph_is_the_in_progress_text(fake_atspi, monkeypatch) -> None:
    """Synthetic tree. The table's child count is not walked."""
    paragraph = _Acc("paragraph", name="")
    paragraph.text = "11"
    panel = _Acc("panel", name="Cell F1")
    _adopt(panel, paragraph)
    table = _Acc("table", name="grid")
    walked = {"n": 0}

    def huge():
        walked["n"] += 1
        return 10**9

    table.get_child_count = huge
    doc = _Acc("document spreadsheet", name="Sheet")
    _adopt(doc, table, panel)
    app = _Acc("application", name="soffice")
    _adopt(app, doc)
    monkeypatch.setattr(_atspi, "find_root", lambda *_args, **_kwargs: app)
    assert _atspi.sheet_editor_text("soffice.bin") == "11"
    assert walked["n"] == 0
    assert _atspi.sheet_editor_text("gedit") is None


def test_type_into_calc_reads_the_editor_and_a_terminal_still_mismatches(
    fake_atspi, monkeypatch
) -> None:
    """Synthetic focus. No live LibreOffice and no live terminal."""
    from a11y_computer_use.drivers import _linux_input

    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":99")
    driver = LinuxDriver()
    driver._focused_editable = None
    state = {"editor": None, "app": "soffice.bin"}
    driver.frontmost_app = lambda: (state["app"], 1)
    monkeypatch.setattr(_atspi, "focused_editable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_atspi, "focused_secure", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(_atspi, "focused_text", lambda _app: "")

    def editor(_app):
        return state["editor"]

    def typed(text):
        state["editor"] = text

    monkeypatch.setattr(_atspi, "sheet_editor_text", editor)
    monkeypatch.setattr(_linux_input, "type_string", typed)
    assert driver.type_text("10") == 2
    state["app"] = "gnome-terminal"
    state["editor"] = None
    monkeypatch.setattr(_atspi, "focused_text", lambda _app: "01")
    with pytest.raises(ComputerUseError) as exc:
        driver.type_text("10")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "text_mismatch"
    assert exc.value.detail["actual"] == "01"


def test_calc_type_waits_for_the_cell_editor_or_the_cell_value(fake_atspi, monkeypatch) -> None:
    """Synthetic Calc. The editor can publish after the keys, then the cell.

    A prefix does not end the poll. A sheet that never shows the text is a
    mismatch. No live LibreOffice.
    """
    from a11y_computer_use.drivers import _linux_input

    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(_atspi, "_TYPE_SETTLE_PAUSE_S", 0)
    monkeypatch.setattr(_atspi, "_TYPE_SETTLE_POLLS", 6)
    driver = LinuxDriver()
    driver._focused_editable = None
    driver.frontmost_app = lambda: ("soffice.bin", 1)
    monkeypatch.setattr(_atspi, "focused_editable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_atspi, "focused_secure", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(_atspi, "focused_text", lambda _app: "")
    monkeypatch.setattr(_atspi, "spreadsheet_open", lambda _app: True)
    monkeypatch.setattr(_linux_input, "type_string", lambda _text: None)
    editor_reads = iter(["4", "4", "42"])

    def editor(_app):
        try:
            return next(editor_reads)
        except StopIteration:
            return "42"

    monkeypatch.setattr(_atspi, "sheet_editor_text", editor)
    monkeypatch.setattr(_atspi, "selected_sheet_text", lambda _app: None)
    assert driver.type_text("42") == 2

    monkeypatch.setattr(_atspi, "sheet_editor_text", lambda _app: None)
    monkeypatch.setattr(_atspi, "selected_sheet_text", lambda _app: "42")
    assert driver.type_text("42") == 2

    monkeypatch.setattr(_atspi, "selected_sheet_text", lambda _app: None)
    with pytest.raises(ComputerUseError) as exc:
        driver.type_text("42")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "text_mismatch"
    assert exc.value.detail["actual"] in {None, ""}


def test_chrome_file_chooser_type_reads_the_location_entry(fake_atspi, monkeypatch) -> None:
    """Synthetic. Chrome's Open File dialog has no AT-SPI entry.

    The page focus stays empty. The location field is read by select-all and
    copy. Right then collapses that selection so the Open button is not left
    under the popup, and the clipboard from before the read is restored. A
    window that is not that dialog still mismatches on the empty page focus.
    """
    from a11y_computer_use.drivers import _linux_input, _linux_system

    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":99")
    driver = LinuxDriver()
    driver._focused_editable = None
    driver.frontmost_app = lambda: ("chrome", 1)
    monkeypatch.setattr(_atspi, "focused_editable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_atspi, "focused_secure", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(_atspi, "focused_text", lambda _app: "")
    monkeypatch.setattr(_atspi, "focused_location_entry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_atspi, "focus_in_browser_chrome", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(_atspi, "libreoffice_app", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(_atspi, "_focused_contenteditable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_atspi, "focused_chrome_rewrite", lambda *_args, **_kwargs: None)
    window = {"title": "Open File", "app": "chrome"}
    monkeypatch.setattr(_linux_system, "active_window", lambda: window)
    board = {"value": "keep-me"}
    chords: list[str] = []

    def write_clipboard(text: str) -> None:
        board["value"] = text

    def read_clipboard() -> str:
        return board["value"]

    def press_chord(chord: str) -> None:
        chords.append(chord)
        if chord == "ctrl+c":
            board["value"] = "/tmp/notes/draft.txt"

    monkeypatch.setattr(_linux_system, "write_clipboard", write_clipboard)
    monkeypatch.setattr(_linux_system, "read_clipboard", read_clipboard)
    monkeypatch.setattr(_linux_input, "press_chord", press_chord)
    monkeypatch.setattr(_linux_input, "type_string", lambda _text: None)
    assert driver.type_text("/tmp/notes/draft.txt") == len("/tmp/notes/draft.txt")
    assert driver._chooser_readback == "/tmp/notes/draft.txt"
    assert chords == ["ctrl+a", "ctrl+c", "right"]
    assert board["value"] == "keep-me"

    window["title"] = "cuaprobe - Google Chrome"
    board["value"] = "keep-me"
    chords.clear()
    with pytest.raises(ComputerUseError) as exc:
        driver.type_text("/tmp/notes/draft.txt")
    assert exc.value.detail["reason"] == "text_mismatch"
    assert exc.value.detail["actual"] == ""
    assert chords == []
    assert board["value"] == "keep-me"


def test_set_value_on_a_sheet_cell_types_and_commits(fake_atspi, monkeypatch) -> None:
    """Synthetic cell. No keystroke reaches an X server."""
    from a11y_computer_use.drivers import _linux_input

    cell = _Acc("table cell", name="D1", value=0.0, minimum=-1e308, maximum=1e308, width=40, height=16)
    cell.text = ""
    cell.component.grab_focus = lambda: True
    sent: list[str] = []
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: cell)
    monkeypatch.setattr(_linux_input, "type_string", sent.append)
    monkeypatch.setattr(_linux_input, "press_chord", sent.append)
    monkeypatch.setattr(
        _atspi, "sheet_cell_matches",
        lambda acc, value: value == "setv" and sent == ["setv", "return"],
    )
    element = Element("e3", "AXCell", "D1", None, Bounds(0, 10, 10, 40, 16), "snap")
    assert LinuxDriver().set_value(element, "setv") is True
    assert sent == ["setv", "return"]


def test_range_formula_set_value_confirms_from_the_editor(fake_atspi, monkeypatch) -> None:
    """Synthetic cell. No keystroke reaches an X server.

    The Formula attribute is the cut string. The editor read is the formula,
    so set_value returns. A different editor formula is still a mismatch.
    """
    from a11y_computer_use.drivers import _linux_input
    import a11y_computer_use.drivers.linux as linux_mod

    cell = _Acc("table cell", name="C1", value=0.0, minimum=-1e308, maximum=1e308, width=40, height=16)
    cell.text = "6"
    cell.get_attributes = lambda: {"formula": "SUM(A1\\"}
    cell.component.grab_focus = lambda: True
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(linux_mod.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: cell)
    monkeypatch.setattr(_linux_input, "type_string", lambda _text: None)
    monkeypatch.setattr(_linux_input, "press_chord", lambda _chord: None)
    reads = {"text": "=SUM(A1:A3)", "n": 0}

    def read(_acc, _app="soffice.bin"):
        reads["n"] += 1
        return reads["text"]

    monkeypatch.setattr(_atspi, "committed_sheet_formula", read)
    element = Element("e9", "AXCell", "C1", None, Bounds(0, 10, 10, 40, 16), "snap")
    assert LinuxDriver().set_value(element, "=SUM(A1:A3)") is True
    assert reads["n"] == 1
    reads["text"] = "=SUM(A1:B9)"
    with pytest.raises(ComputerUseError) as exc:
        LinuxDriver().set_value(element, "=SUM(A1:A3)")
    assert exc.value.detail["reason"] == "text_mismatch"
    assert exc.value.detail["formula"] == "=SUM(A1:B9)"
    assert reads["n"] == 2


def test_soffice_without_a_bridge_is_unsupported_and_other_apps_stay_missing(
    fake_atspi, monkeypatch
) -> None:
    """Synthetic. The process check is stubbed; nothing is launched.

    The registration wait is zero here. A live soffice process with no
    AT-SPI root is the missing bridge, and this case does not sleep.
    """
    monkeypatch.setattr(_atspi, "find_root", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_atspi, "libreoffice_process_running", lambda: True)
    monkeypatch.setattr(_atspi, "ATSPI_REGISTER_WAIT_S", 0.0)
    driver = LinuxDriver()
    with pytest.raises(ComputerUseError) as missing_bridge:
        driver.snapshot(Scope.WINDOW, "soffice")
    assert missing_bridge.value.code is ErrorCode.UNSUPPORTED
    assert missing_bridge.value.detail["reason"] == "no_accessibility_bridge"
    assert "libreoffice-gtk3" in missing_bridge.value.message
    assert "SAL_USE_VCLPLUGIN=gtk3" in missing_bridge.value.message
    with pytest.raises(ComputerUseError) as other:
        driver.snapshot(Scope.WINDOW, "gedit")
    assert other.value.code is ErrorCode.APP_NOT_FOUND
    monkeypatch.setattr(_atspi, "libreoffice_process_running", lambda: False)
    with pytest.raises(ComputerUseError) as stopped:
        driver.snapshot(Scope.WINDOW, "libreoffice")
    assert stopped.value.code is ErrorCode.APP_NOT_FOUND


def test_app_listed_matches_the_app_list_and_the_window_list(monkeypatch) -> None:
    """Synthetic lists. ``LibreOffice`` is the same row as ``soffice.bin``."""
    from a11y_computer_use.drivers import _linux_system

    monkeypatch.setattr(
        _linux_system, "running_apps",
        lambda: [{"bundle_id": "soffice.bin", "name": "soffice.bin", "pid": 9, "frontmost": True}],
    )
    monkeypatch.setattr(
        _linux_system, "windows",
        lambda: [{
            "app": "soffice.bin",
            "title": "LibreOffice",
            "wm_class": "libreoffice",
            "wm_class_class": "libreoffice-startcenter",
        }],
    )
    assert _atspi.app_listed("soffice.bin")
    assert _atspi.app_listed("LibreOffice")
    assert _atspi.app_listed("libreoffice")
    assert not _atspi.app_listed("gedit")


def test_snapshot_resolves_like_the_app_list_and_waits_for_atspi(fake_atspi, monkeypatch) -> None:
    """Synthetic. The clock is fake, so the wait does not take 15 seconds.

    The caller's name is tried first and misses. The comm the app list
    would show is tried next and misses once. After one poll that comm
    is on the bus.
    """
    from a11y_computer_use.drivers import _linux_system

    class _Office:
        def get_toolkit_name(self):
            return "gtk"

        def get_name(self):
            return "soffice.bin"

    window = _Acc("frame", name="LibreOffice", width=640, height=480)
    window.get_application = lambda: _Office()
    seen: list[str] = []

    def find_root(app, _scope):
        seen.append(app)
        if app == "soffice.bin" and seen.count("soffice.bin") >= 2:
            return window
        return None

    clock = {"t": 50.0}

    def monotonic():
        return clock["t"]

    def sleep(seconds):
        clock["t"] += float(seconds)

    monkeypatch.setattr(_atspi, "find_root", find_root)
    monkeypatch.setattr(_atspi, "primary_geometry", _geometry)
    monkeypatch.setattr(_atspi, "_screen_size", lambda: (1280, 800))
    monkeypatch.setattr(
        _linux_system, "resolve_app",
        lambda identifier: "soffice.bin" if "libre" in identifier.lower() else identifier,
    )
    monkeypatch.setattr(_atspi, "should_wait_for_atspi", lambda _name: True)
    monkeypatch.setattr("a11y_computer_use.drivers.linux.time.monotonic", monotonic)
    monkeypatch.setattr("a11y_computer_use.drivers.linux.time.sleep", sleep)
    snap = LinuxDriver().snapshot(Scope.WINDOW, "LibreOffice")
    assert snap.app == "soffice.bin"
    assert seen == ["LibreOffice", "soffice.bin", "LibreOffice", "soffice.bin"]
    assert clock["t"] == pytest.approx(50.25)


def test_snapshot_keeps_an_atspi_name_that_already_matches(fake_atspi, monkeypatch) -> None:
    """Synthetic. A name find_root already has is not replaced by the comm.

    Two Python windows share the comm python3. resolve_app would return
    that comm for cuakeyother. The snapshot stays on cuakeyother.
    """
    from a11y_computer_use.drivers import _linux_system

    class _App:
        def get_toolkit_name(self):
            return "gtk"

        def get_name(self):
            return "cuakeyother"

    window = _Acc("frame", name="cuakeyother", width=420, height=140)
    window.get_application = lambda: _App()
    seen: list[str] = []

    def find_root(app, _scope):
        seen.append(app)
        if app == "cuakeyother":
            return window
        return None

    monkeypatch.setattr(_atspi, "find_root", find_root)
    monkeypatch.setattr(_atspi, "primary_geometry", _geometry)
    monkeypatch.setattr(_atspi, "_screen_size", lambda: (1280, 800))
    monkeypatch.setattr(_linux_system, "resolve_app", lambda _identifier: "python3")
    snap = LinuxDriver().snapshot(Scope.WINDOW, "cuakeyother")
    assert snap.app == "cuakeyother"
    assert seen == ["cuakeyother"]


def test_snapshot_stops_when_the_listed_app_goes_away(fake_atspi, monkeypatch) -> None:
    """Synthetic. One poll, then the list no longer shows the app.

    The deadline is not used up. The result is app_not_found.
    """
    from a11y_computer_use.drivers import _linux_system

    clock = {"t": 10.0}
    listed = {"on": True}

    def monotonic():
        return clock["t"]

    def sleep(seconds):
        clock["t"] += float(seconds)
        listed["on"] = False

    monkeypatch.setattr(_atspi, "find_root", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_atspi, "libreoffice_process_running", lambda: False)
    monkeypatch.setattr(_atspi, "app_listed", lambda _name: listed["on"])
    monkeypatch.setattr(_linux_system, "resolve_app", lambda identifier: identifier)
    monkeypatch.setattr("a11y_computer_use.drivers.linux.time.monotonic", monotonic)
    monkeypatch.setattr("a11y_computer_use.drivers.linux.time.sleep", sleep)
    with pytest.raises(ComputerUseError) as exc:
        LinuxDriver().snapshot(Scope.WINDOW, "mousepad")
    assert exc.value.code is ErrorCode.APP_NOT_FOUND
    assert exc.value.detail["app"] == "mousepad"
    assert clock["t"] == pytest.approx(10.25)


def test_snapshot_reports_the_bridge_after_the_register_deadline(fake_atspi, monkeypatch) -> None:
    """Synthetic clock. The process stays up and never joins the bus.

    The wait runs out at 15 s, then the error names the gtk3 bridge.
    """
    from a11y_computer_use.drivers import _linux_system

    clock = {"t": 0.0}

    def monotonic():
        return clock["t"]

    def sleep(seconds):
        clock["t"] += float(seconds)

    monkeypatch.setattr(_atspi, "find_root", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_atspi, "libreoffice_process_running", lambda: True)
    monkeypatch.setattr(_atspi, "app_listed", lambda _name: False)
    monkeypatch.setattr(_linux_system, "resolve_app", lambda identifier: identifier)
    monkeypatch.setattr("a11y_computer_use.drivers.linux.time.monotonic", monotonic)
    monkeypatch.setattr("a11y_computer_use.drivers.linux.time.sleep", sleep)
    with pytest.raises(ComputerUseError) as exc:
        LinuxDriver().snapshot(Scope.WINDOW, "soffice.bin")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "no_accessibility_bridge"
    assert clock["t"] == pytest.approx(15.0)


def test_unlisted_app_does_not_wait_for_atspi(fake_atspi, monkeypatch) -> None:
    """Synthetic. gedit is not listed and is not LibreOffice. No sleep."""
    from a11y_computer_use.drivers import _linux_system

    slept = {"n": 0}

    def sleep(seconds):
        slept["n"] += 1

    monkeypatch.setattr(_atspi, "find_root", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_atspi, "app_listed", lambda _name: False)
    monkeypatch.setattr(_atspi, "libreoffice_process_running", lambda: False)
    monkeypatch.setattr(_linux_system, "resolve_app", lambda identifier: identifier)
    monkeypatch.setattr("a11y_computer_use.drivers.linux.time.sleep", sleep)
    with pytest.raises(ComputerUseError) as exc:
        LinuxDriver().snapshot(Scope.WINDOW, "gedit")
    assert exc.value.code is ErrorCode.APP_NOT_FOUND
    assert slept["n"] == 0


def _write_proc(root, pid: str, comm: str, state: str, environ: dict[str, str] | None) -> None:
    folder = root / pid
    folder.mkdir()
    (folder / "comm").write_text(comm + "\n", encoding="utf-8")
    (folder / "stat").write_text(f"{pid} ({comm}) {state} 1\n", encoding="utf-8")
    if environ is None:
        return
    raw = b"\0".join(f"{key}={value}".encode() for key, value in environ.items()) + b"\0"
    (folder / "environ").write_bytes(raw)


def test_libreoffice_process_running_is_this_session_only(tmp_path, monkeypatch) -> None:
    """Synthetic /proc. Another display's soffice is not this session.

    The same display number matches with or without the screen suffix.
    A zombie on this display does not count. An unreadable environ does
    not count. With no DISPLAY here, a process that has one is not local.
    A matching AT-SPI bus counts when the displays do not disagree.
    """
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(_atspi, "_proc_root", lambda: str(proc))
    monkeypatch.setenv("DISPLAY", ":1.0")
    monkeypatch.delenv("AT_SPI_BUS_ADDRESS", raising=False)
    monkeypatch.delenv("AT_SPI_BUS", raising=False)

    _write_proc(proc, "10", "soffice.bin", "S", {"DISPLAY": ":2"})
    assert _atspi.libreoffice_process_running() is False

    _write_proc(proc, "11", "soffice.bin", "S", {"DISPLAY": ":1"})
    assert _atspi.libreoffice_process_running() is True

    for child in proc.iterdir():
        for path in child.iterdir():
            path.unlink()
        child.rmdir()
    _write_proc(proc, "12", "soffice.bin", "Z", {"DISPLAY": ":1"})
    _write_proc(proc, "13", "gedit", "S", {"DISPLAY": ":1"})
    _write_proc(proc, "14", "soffice", "S", None)
    assert _atspi.libreoffice_process_running() is False

    monkeypatch.delenv("DISPLAY", raising=False)
    _write_proc(proc, "15", "oosplash", "S", {"DISPLAY": ":2"})
    assert _atspi.libreoffice_process_running() is False

    monkeypatch.setenv("AT_SPI_BUS_ADDRESS", "unix:path=/tmp/bus-a")
    _write_proc(proc, "16", "soffice.bin", "S", {"AT_SPI_BUS_ADDRESS": "unix:path=/tmp/bus-a"})
    assert _atspi.libreoffice_process_running() is True


def test_snapshot_does_not_wait_for_soffice_on_another_display(fake_atspi, monkeypatch) -> None:
    """Synthetic. A foreign soffice process is app_not_found with no sleep."""
    from a11y_computer_use.drivers import _linux_system

    slept = {"n": 0}

    def sleep(seconds):
        slept["n"] += 1

    monkeypatch.setenv("DISPLAY", ":1")
    monkeypatch.delenv("AT_SPI_BUS_ADDRESS", raising=False)
    monkeypatch.delenv("AT_SPI_BUS", raising=False)
    monkeypatch.setattr(
        _atspi, "_libreoffice_processes",
        lambda: [("soffice.bin", "S", {"DISPLAY": ":2"})],
    )
    monkeypatch.setattr(_atspi, "find_root", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_atspi, "app_listed", lambda _name: False)
    monkeypatch.setattr(_linux_system, "resolve_app", lambda identifier: identifier)
    monkeypatch.setattr("a11y_computer_use.drivers.linux.time.sleep", sleep)
    with pytest.raises(ComputerUseError) as exc:
        LinuxDriver().snapshot(Scope.WINDOW, "LibreOffice")
    assert exc.value.code is ErrorCode.APP_NOT_FOUND
    assert "accessibility bridge" not in exc.value.message
    assert slept["n"] == 0


def test_tools_for_an_app_that_is_not_running_return_app_not_found(
    fake_atspi, monkeypatch
) -> None:
    """Synthetic missing app, not a live desktop.

    Resolving the name still returns it, so a grant can precede launch.
    Every app-targeted tool then says the app is not running. Focus does
    not say it activated a terminal that was never launched.
    """
    from a11y_computer_use.drivers import _linux_system

    monkeypatch.setattr(_atspi, "find_root", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_linux_system, "_display", lambda: object())
    monkeypatch.setattr(_linux_system, "_managed_windows", lambda _display: [])
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    runtime = _bare_runtime(driver)
    calls = (
        lambda: runtime.desktop_snapshot("xfce4-terminal"),
        lambda: runtime.find("xfce4-terminal", text="ITEM-001"),
        lambda: runtime.scroll_to_find("xfce4-terminal", text="ITEM-001"),
        lambda: runtime.menu("xfce4-terminal", action="state"),
        lambda: runtime.menu("xfce4-terminal", action="close"),
        lambda: runtime.app("focus", "xfce4-terminal"),
    )
    for call in calls:
        with pytest.raises(ComputerUseError) as exc:
            call()
        assert exc.value.code is ErrorCode.APP_NOT_FOUND
        assert exc.value.detail["app"] == "xfce4-terminal"
        assert "activated" not in exc.value.message
        assert "no elements match" not in exc.value.message
        assert "not found after" not in exc.value.message
        assert "no menu was open" not in exc.value.message


def test_running_app_with_an_empty_tree_is_not_app_not_found(fake_atspi, monkeypatch) -> None:
    """A frame with no children is an open app. Synthetic tree, not a live desktop."""

    class _Term:
        def get_toolkit_name(self):
            return "gtk"

        def get_name(self):
            return "xfce4-terminal"

    window = _Acc("frame", name="Terminal", width=640, height=480)
    window.get_application = lambda: _Term()
    monkeypatch.setattr(_atspi, "find_root", lambda *_args, **_kwargs: window)
    monkeypatch.setattr(_atspi, "primary_geometry", _geometry)
    monkeypatch.setattr(_atspi, "_screen_size", lambda: (1280, 800))
    snap = LinuxDriver().snapshot(Scope.WINDOW, "xfce4-terminal")
    assert snap.app == "xfce4-terminal"
    assert snap.elements
    assert snap.elements[0].role == "AXWindow"


def test_pixel_scroll_dry_run_does_not_write_or_move(fake_atspi, xtest_recorder) -> None:
    events, _display = xtest_recorder
    text, vertical, _horizontal = _scrolled_text()
    driver = LinuxDriver()
    driver._focused_editable = text
    assert driver.scroll(_body(), dy=3, unit=ScrollUnit.PIXELS, dry_run=True) is None
    assert driver._focused_editable is text
    assert vertical.value == 100
    assert events == []


def _layout_fixed_rows(listing: _Acc, rows: list[_Acc], hit: _ListHit, head: int) -> None:
    """Place every row. ``head`` (1-based) is flush with the list top.

    The box is (20, 139) 400 by 520. Pitch and row height are 18px, so
    ITEM-001 through ITEM-029 sit inside the box when ``head`` is 1, and
    ITEM-172 through ITEM-200 when ``head`` is 172. Synthetic boxes, not a
    live Chrome bounds read.
    """
    origin_y = 139
    pitch = 18
    hit.screen = head
    hit.x, hit.y, hit.width, hit.height = 20, origin_y, 400, 520
    hit._at.clear()
    start = head - 1
    for index, row in enumerate(rows):
        _place_row(row, 29, origin_y + (index - start) * pitch, 360, pitch)
    _adopt(listing, *rows)


def _fixed_height_page():
    """200-row overflow list, 400 by 520, 18px rows. Not a live Chrome window."""
    window, listing, hit, _stuck = _chrome_list()
    rows = hit.rows
    app = window.get_application()
    document = _Acc("document web", name="Bench", width=1271, height=709)
    document.component._rect.x = 4.0
    document.component._rect.y = 86.0
    document.get_application = lambda: app
    for row in rows:
        row.get_application = lambda: app
    _adopt(document, listing)
    _adopt(window, document)
    _add_tab_strip(window)
    _layout_fixed_rows(listing, rows, hit, 1)
    return window, listing, hit, rows


def _assert_no_elision(snap) -> None:
    """The painted rows were not dropped with a ``… N more`` marker."""
    for line in observe.render_text(snap).splitlines():
        assert not (line.strip().startswith("…") and line.strip().endswith(" more"))


def test_fixed_height_list_snapshot_lists_every_painted_row(fake_atspi, monkeypatch) -> None:
    """Synthetic overflow list, not a live Chrome window.

    A 520px box of 18px rows paints 29 rows. The 0.4.22 through 0.4.26
    snapshot stopped at 16 and showed no elision marker, so find missed
    ITEM-020. At the bottom the painted run is ITEM-172 through ITEM-200,
    and find must match ITEM-190 and ITEM-200. The hit-test sample count
    stays 16.
    """
    window, listing, hit, rows = _fixed_height_page()
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    assert _atspi._MAX_ROW_SAMPLES == 16
    driver = LinuxDriver()
    snap = driver.snapshot(Scope.WINDOW, "chrome")
    titles = _row_titles(snap)
    assert titles[0] == "ITEM-001"
    assert titles[-1] == "ITEM-029"
    assert len(titles) == 29
    assert "ITEM-016" in titles
    assert "ITEM-017" in titles
    assert "ITEM-020" in titles
    assert "ITEM-030" not in titles
    assert observe.find_elements(snap, text="ITEM-020")
    assert observe.find_elements(snap, text="ITEM-029")
    _assert_no_elision(snap)
    names = _atspi.row_names(_atspi._in_view_named_rows(listing))
    assert names[0] == "ITEM-001"
    assert names[-1] == "ITEM-029"
    assert len(names) == 29

    _layout_fixed_rows(listing, rows, hit, 172)
    _atspi.reset_shown_rows()
    bottom = driver.snapshot(Scope.WINDOW, "chrome")
    later = _row_titles(bottom)
    assert later[0] == "ITEM-172"
    assert later[-1] == "ITEM-200"
    assert len(later) == 29
    assert "ITEM-187" in later
    assert "ITEM-188" in later
    assert "ITEM-190" in later
    assert "ITEM-200" in later
    assert "ITEM-171" not in later
    assert "ITEM-001" not in later
    assert observe.find_elements(bottom, text="ITEM-190")
    assert observe.find_elements(bottom, text="ITEM-200")
    _assert_no_elision(bottom)


@pytest.mark.parametrize("target", ["ITEM-180", "ITEM-190", "ITEM-199", "ITEM-200"])
def test_scroll_to_find_matches_a_row_past_the_sixteenth_painted_row(
    fake_atspi, xtest_recorder, monkeypatch, target: str
) -> None:
    """Synthetic overflow list, not a live Chrome window.

    ``scroll_to`` reveals a later row at the top of the 520px list. The
    target is not in the opening 29 rows. Once it is painted it is in the
    snapshot, including when it sits past the old 16-row window. The search
    does not finish on ``page_unchanged`` and does not leave the list on
    ITEM-001. ITEM-180 still has to be found.
    """
    window, listing, hit, rows = _fixed_height_page()

    def reveal(row, _scroll_type):
        number = int(row.name.split("-")[1])
        _layout_fixed_rows(listing, rows, hit, number)
        return True

    for row in rows:
        row.component.scroll_to = lambda scroll_type, row=row: reveal(row, scroll_type)
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)
    wheels = {"n": 0}

    def on_wheel():
        wheels["n"] += 1

    hit.on_wheel = on_wheel
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    first = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert first[0] == "ITEM-001"
    assert target not in first
    runtime = _runtime_for(driver, monkeypatch)
    out = runtime.scroll_to_find("chrome", text=target, max_scrolls=40)
    assert target in out
    assert "found after" in out
    assert "found after 0 scroll" not in out
    assert "not found" not in out
    assert "page_unchanged" not in out
    assert wheels["n"] == 0
    painted = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert target in painted
    assert painted[0] != "ITEM-001"


def _layout_body_rows(listing: _Acc, rows: list[_Acc], hit: _ListHit, head: int) -> None:
    """Place all 200 rows at a 28px pitch. ``head`` (1-based) sits at y=120.

    The list is the content height. The document fixture clips what is
    painted. Synthetic boxes, not a live Chrome bounds read.
    """
    pitch = 28
    origin = 120 - (head - 1) * pitch
    hit.screen = head
    hit.x, hit.y, hit.width, hit.height = 40, origin, 1100, pitch * len(rows)
    hit._at.clear()
    for index, row in enumerate(rows):
        _place_row(row, 40, origin + index * pitch, 1100, pitch)
    _adopt(listing, *rows)


def test_body_snapshot_lists_painted_rows_past_the_sixteenth(fake_atspi, monkeypatch) -> None:
    """Synthetic body-scroll page, not a live Chrome window.

    28px rows, and the document paints more than 16 of them. The snapshot
    includes ITEM-020 and the last painted row, and it does not include the
    next row, which is below the page.
    """
    window, listing, hit, rows = _document_scroll_page()
    _layout_body_rows(listing, rows, hit, 1)
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    snap = LinuxDriver().snapshot(Scope.WINDOW, "chrome")
    titles = _row_titles(snap)
    assert titles[0] == "ITEM-001"
    assert "ITEM-016" in titles
    assert "ITEM-017" in titles
    assert "ITEM-020" in titles
    assert "ITEM-024" in titles
    assert "ITEM-025" not in titles
    assert len(titles) > 16
    assert observe.find_elements(snap, text="ITEM-020")
    assert observe.find_elements(snap, text="ITEM-024")
    _assert_no_elision(snap)


def test_body_scroll_to_find_matches_the_last_row_when_it_is_painted(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic body-scroll page, not a live Chrome window.

    The wheel moves the page. When ITEM-200 is inside the document the
    snapshot contains it, so scroll_to_find does not return not-found while
    that row is painted.
    """
    window, listing, hit, rows = _document_scroll_page()
    _layout_body_rows(listing, rows, hit, 1)
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)
    head = {"n": 1}

    def advance():
        head["n"] = min(177, head["n"] + 8)
        _layout_body_rows(listing, rows, hit, head["n"])

    points = _page_wheel(monkeypatch, hit, advance)
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    opening = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert opening[0] == "ITEM-001"
    assert "ITEM-200" not in opening
    assert "ITEM-017" in opening
    runtime = _runtime_for(driver, monkeypatch)
    out = runtime.scroll_to_find("chrome", text="ITEM-200", max_scrolls=40)
    assert "ITEM-200" in out
    assert "found after" in out
    assert "found after 0 scroll" not in out
    assert "not found" not in out
    assert "page_unchanged" not in out
    assert points
    assert all(90 <= y < 800 for _x, y in points)
    painted = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert "ITEM-200" in painted
    assert painted[0] != "ITEM-001"


def _long_list_page(count: int = 2000, wrapped: bool = False):
    """Synthetic Chromium list of ``count`` unnamed items, not a live window.

    Each item's name is on a ``static`` child, ITEM-0001 onward. ``wrapped``
    puts those items under one group so the list's own child count is 1.
    """
    app = _ChromeApp()
    items: list[_Acc] = []
    labels: list[_Acc] = []
    for number in range(1, count + 1):
        label = _Acc("static", name=f"ITEM-{number:04d}")
        item = _Acc("list item", name="")
        label.get_application = lambda: app
        item.get_application = lambda: app
        _adopt(item, label)
        items.append(item)
        labels.append(label)
    hit = _ListHit(items, [])
    hit.x, hit.y, hit.width, hit.height = 20, 139, 400, 520
    listing = _Acc("list", name="")
    listing.component = hit
    listing.get_application = lambda: app
    group = None
    if wrapped:
        group = _Acc("panel", name="", width=400, height=520)
        group.component._rect.x = 20.0
        group.component._rect.y = 139.0
        group.get_application = lambda: app
        _adopt(group, *items)
        _adopt(listing, group)
    document = _Acc("document web", name="long-list", width=1271, height=709)
    document.component._rect.x = 4.0
    document.component._rect.y = 86.0
    document.get_application = lambda: app
    _adopt(document, listing)
    window = _Acc("frame", name="long-list", width=1280, height=800)
    window.get_application = lambda: app
    _adopt(window, document)
    _add_tab_strip(window)
    _layout_long_rows(listing, items, labels, hit, 1, group=group)
    return window, listing, hit, items, labels, group


def _layout_long_rows(listing, items, labels, hit, head: int, *, group=None) -> None:
    """Place every row. ``head`` (1-based) is flush with the list top.

    The box is (20, 139) 400 by 520. Pitch and row height are 18px, so
    29 rows are on screen. Synthetic boxes, not a live Chrome bounds read.
    """
    origin_y = 139
    pitch = 18
    hit.screen = head
    hit.x, hit.y, hit.width, hit.height = 20, origin_y, 400, 520
    hit._at.clear()
    start = head - 1
    for index, (item, label) in enumerate(zip(items, labels)):
        y = origin_y + (index - start) * pitch
        _place_row(item, 20, y, 360, pitch)
        _place_row(label, 28, y, 80, pitch)
    if group is None:
        _adopt(listing, *items)
    else:
        _place_row(group, 20, origin_y, 400, 520)
        _adopt(group, *items)
        _adopt(listing, group)


def _long_titles(listing) -> list[str]:
    return list(_atspi.row_names(_atspi._in_view_named_rows(listing)))


def test_long_list_snapshot_lists_every_painted_row_past_child_250(
    fake_atspi, monkeypatch
) -> None:
    """Synthetic 2000-row list, not a live Chrome window.

    A 520px box of 18px rows paints 29 rows. On 0.4.31 the walk started
    at child 0 and stopped after 250 nodes, so ITEM-0201 through
    ITEM-0229 came back as ITEM-0201 through ITEM-0224, ITEM-0226 through
    ITEM-0254 came back as ITEM-0226 through ITEM-0237, and a later
    window came back empty. The hit-test sample count stays 16.
    """
    window, listing, hit, items, labels, _group = _long_list_page()
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    assert _atspi._MAX_ROW_SAMPLES == 16
    assert _atspi._VISIBLE_WALK_CAP == 250
    driver = LinuxDriver()

    def painted(head: int) -> list[str]:
        _layout_long_rows(listing, items, labels, hit, head)
        _atspi.reset_shown_rows()
        names = _long_titles(listing)
        snap = driver.snapshot(Scope.WINDOW, "chrome")
        titles = [el.title for el in snap.elements if el.title.startswith("ITEM-")]
        assert titles == names
        _assert_no_elision(snap)
        return names

    assert painted(1) == [f"ITEM-{number:04d}" for number in range(1, 30)]
    assert painted(201) == [f"ITEM-{number:04d}" for number in range(201, 230)]
    assert "ITEM-0224" in painted(201)
    assert painted(226) == [f"ITEM-{number:04d}" for number in range(226, 255)]
    assert "ITEM-0237" in painted(226)
    assert "ITEM-0254" in painted(226)
    assert painted(456) == [f"ITEM-{number:04d}" for number in range(456, 485)]
    assert observe.find_elements(driver.snapshot(Scope.WINDOW, "chrome"), text="ITEM-0456")
    assert observe.find_elements(driver.snapshot(Scope.WINDOW, "chrome"), text="ITEM-0484")


def test_wrapped_long_list_lists_the_painted_rows_past_child_250(fake_atspi, monkeypatch) -> None:
    """Synthetic group of 2000 rows, not a live Chrome window.

    The list's only child is a panel. The on-screen rows are still that
    panel's children past index 250.
    """
    window, listing, hit, items, labels, group = _long_list_page(wrapped=True)
    _wire_chrome_list(monkeypatch, window, hit, stuck=True)
    _layout_long_rows(listing, items, labels, hit, 456, group=group)
    _atspi.reset_shown_rows()
    names = _long_titles(listing)
    assert names == [f"ITEM-{number:04d}" for number in range(456, 485)]
    assert names[0] == "ITEM-0456"
    assert names[-1] == "ITEM-0484"


def _bind_long_scroll(labels, listing, items, hit) -> None:
    def reveal(label, _scroll_type):
        number = int(label.name.split("-")[1])
        _layout_long_rows(listing, items, labels, hit, number)
        return True

    for label in labels:
        label.component.scroll_to = lambda scroll_type, label=label: reveal(label, scroll_type)


@pytest.mark.parametrize("target", ["ITEM-0240", "ITEM-0270", "ITEM-0400"])
def test_scroll_to_find_reaches_a_row_past_child_250(
    fake_atspi, xtest_recorder, monkeypatch, target: str
) -> None:
    """Synthetic 2000-row list, not a live Chrome window.

    ``scroll_to`` reveals a later row at the top. The search lists the
    target once it is on screen and does not stop on ``rows_stale``.
    ITEM-0400 is past what 60 steps of five rows can reach from ITEM-0001.
    """
    window, listing, hit, items, labels, _group = _long_list_page()
    _layout_long_rows(listing, items, labels, hit, 1)
    _bind_long_scroll(labels, listing, items, hit)
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)
    wheels = {"n": 0}

    def on_wheel():
        wheels["n"] += 1

    hit.on_wheel = on_wheel
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    opening = [el.title for el in driver.snapshot(Scope.WINDOW, "chrome").elements if el.title.startswith("ITEM-")]
    assert opening[0] == "ITEM-0001"
    assert target not in opening
    runtime = _runtime_for(driver, monkeypatch)
    limit = 60 if target != "ITEM-0400" else 80
    out = runtime.scroll_to_find("chrome", text=target, max_scrolls=limit)
    # Five rows per scroll from ITEM-0001. The target is inside the 29-row
    # window on that step: 43, 49, and 75. ITEM-0400 needs more than 60.
    expected = {"ITEM-0240": 43, "ITEM-0270": 49, "ITEM-0400": 75}[target]
    assert out.startswith(f"found after {expected} scroll")
    assert target in out
    assert "found after" in out
    assert "found after 0 scroll" not in out
    assert "not found" not in out
    assert "rows_stale" not in out
    assert "page_unchanged" not in out
    assert wheels["n"] == 0
    painted = [el.title for el in driver.snapshot(Scope.WINDOW, "chrome").elements if el.title.startswith("ITEM-")]
    assert target in painted
    assert painted[0] != "ITEM-0001"


def test_sixty_scrolls_toward_item_0400_are_not_rows_stale(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic 2000-row list, not a live Chrome window.

    Sixty steps of five rows from ITEM-0001 end around ITEM-0301. That
    is not far enough for ITEM-0400. The call returns not found. It does
    not raise ``rows_stale``.
    """
    window, listing, hit, items, labels, _group = _long_list_page()
    _layout_long_rows(listing, items, labels, hit, 1)
    _bind_long_scroll(labels, listing, items, hit)
    _wire_chrome_list(monkeypatch, window, hit, stuck=False)
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    runtime = _runtime_for(driver, monkeypatch)
    out = runtime.scroll_to_find("chrome", text="ITEM-0400", max_scrolls=60)
    assert out.startswith("not found after 60 scroll")
    assert "rows_stale" not in out
    painted = [el.title for el in driver.snapshot(Scope.WINDOW, "chrome").elements if el.title.startswith("ITEM-")]
    assert painted[0] == "ITEM-0301"
    assert "ITEM-0329" in painted
    assert "ITEM-0400" not in painted

def _bind_gtk_app(node: _Acc) -> _Acc:
    """Toolkit ``gtk`` so the table is not Qt, Chromium, or Firefox."""
    app = getattr(node, "_gtk_app", None)
    if app is None:
        app = _Acc("application", name="treeprobe")
        app.get_toolkit_name = lambda: "gtk"
        node._gtk_app = app
    node.get_application = lambda: app
    return node


def _gtk_tree_page():
    """Synthetic 300×2 GTK tree. Not a live window.

    Twenty rows fit in the 484px body. Cells outside that run sit at the
    GTK off-screen sentinel. Child 0 is a column header, so a walk that
    stops at child 250 never reaches row 281.
    """
    state = {"head": 0}
    headers = [
        _Acc("table column header", name=name, width=180, height=24)
        for name in ("Name", "Value")
    ]
    cells = [
        [_Acc("table cell", name=f"ROW-{row:05d}" if col == 0 else f"val{row}", width=180, height=23)
         for col in range(2)]
        for row in range(300)
    ]
    action = _NS(get_n_actions=lambda: 1, get_action_name=lambda _index: "activate")
    table = _Acc("table", name="Rows", width=600, height=484)
    _bind_gtk_app(table)
    ordered: list[_Acc] = list(headers)
    for row in range(300):
        for col in range(2):
            cell = cells[row][col]
            cell.get_action_iface = lambda action=action: action
            cell.get_index_in_parent = lambda row=row, col=col: 2 + row * 2 + col
            ordered.append(cell)
    for index, header in enumerate(headers):
        header.get_index_in_parent = lambda index=index: index
    _adopt(table, *ordered)

    def place(head: int) -> None:
        state["head"] = head
        for col, header in enumerate(headers):
            _place_row(header, 110 + col * 200, 100, 180, 24)
        for row in range(300):
            on_screen = head <= row < head + 20
            for col in range(2):
                y = 124 + (row - head) * 23 if on_screen else -2147483648
                x = 110 + col * 200 if on_screen else -2147483648
                _place_row(cells[row][col], x, y, 180, 23)

    def scroll_to(_scroll_type, row: int) -> bool:
        place(row)
        return True

    for row in range(300):
        for col in range(2):
            cells[row][col].component.scroll_to = lambda _scroll_type, row=row: scroll_to(_scroll_type, row)
    place(0)
    _place_row(table, 100, 100, 600, 484)

    def at_point(_x, y, _coord):
        y = int(y)
        if y < 124:
            return headers[0]
        slot = (y - 124) // 23
        if slot < 0 or slot >= 20:
            return None
        row = state["head"] + int(slot)
        if row < 0 or row >= 300:
            return None
        return cells[row][0]

    table.component.get_accessible_at_point = at_point
    table.get_n_rows = lambda: 300
    table.get_n_columns = lambda: 2
    table.get_accessible_at = lambda row, col: cells[row][col]
    table.get_column_header = lambda col: headers[col]

    def row_at_index(index: int) -> int:
        if index < 2:
            return -1
        return (index - 2) // 2

    table.get_row_at_index = row_at_index
    window = _Acc("frame", name="TreeProbe", width=640, height=560)
    _place_row(window, 80, 40, 640, 560)
    scroll = _Acc("scroll pane", width=600, height=484)
    _place_row(scroll, 100, 100, 600, 484)
    _bind_gtk_app(window)
    _bind_gtk_app(scroll)
    # Share the application the table already bound.
    app = table.get_application()
    window.get_application = lambda: app
    scroll.get_application = lambda: app
    _adopt(scroll, table)
    _adopt(window, scroll)
    return window, table, state, place


def _wire_gtk_tree(monkeypatch, window) -> LinuxDriver:
    monkeypatch.setattr(_atspi, "find_root", lambda *_args, **_kwargs: window)
    monkeypatch.setattr(_atspi, "primary_geometry", _geometry)
    monkeypatch.setattr(_atspi, "_screen_size", lambda: (1280, 800))
    driver = LinuxDriver()
    driver.ensure_trusted = lambda: None
    driver.menu_state = lambda _app: {"open": False, "path": []}
    return driver


def _gtk_row_titles(snap) -> list[str]:
    return [el.title for el in snap.elements if el.title.startswith("ROW-")]


def _large_gtk_tree(rows: int = 10_000, cols: int = 3):
    """Synthetic GTK ListStore tree. Cells are created when the table is asked.

    Twenty body rows fit in the 484px box. The rest sit at the GTK off-screen
    sentinel. ``scroll_to`` returns false. The vertical bar's fraction of
    ``row / (rows - 1)`` is what moves the painted run. Not a live window.
    """
    state = {"head": 0}
    headers = [
        _Acc("table column header", name=name, width=180, height=24)
        for name in ("Name", "Value", "Note")[:cols]
    ]
    cache: dict[tuple[int, int], _Acc] = {}
    table = _Acc("table", name="Rows", width=600, height=484)
    _bind_gtk_app(table)

    def position(cell: _Acc, row: int, col: int, head: int) -> None:
        on_screen = head <= row < head + 20
        y = 124 + (row - head) * 23 if on_screen else -2147483648
        x = 110 + col * 200 if on_screen else -2147483648
        _place_row(cell, x, y, 180, 23)

    def cell_at(row: int, col: int) -> _Acc | None:
        if row < 0 or row >= rows or col < 0 or col >= cols:
            return None
        key = (row, col)
        cell = cache.get(key)
        if cell is None:
            if col == 0:
                name = f"ROW-{row:05d}"
            elif col == 1:
                name = f"val{row}"
            else:
                name = f"note{row}"
            cell = _Acc("table cell", name=name, width=180, height=23)
            cell.parent = table
            cell.get_index_in_parent = lambda row=row, col=col: cols + row * cols + col
            if cell.component is not None:
                cell.component.scroll_to = lambda *_args, **_kwargs: False
            cache[key] = cell
        position(cell, row, col, state["head"])
        return cell

    def place(head: int) -> None:
        state["head"] = max(0, min(int(head), max(0, rows - 20)))
        for col, header in enumerate(headers):
            _place_row(header, 110 + col * 200, 100, 180, 24)
        for (row, col), cell in list(cache.items()):
            position(cell, row, col, state["head"])

    place(0)
    _place_row(table, 100, 100, 600, 484)

    def at_point(_x, y, _coord):
        yy = int(y)
        if yy < 124:
            return headers[0]
        slot = (yy - 124) // 23
        if slot < 0 or slot >= 20:
            return None
        row = state["head"] + int(slot)
        if row < 0 or row >= rows:
            return None
        return cell_at(row, 0)

    table.component.get_accessible_at_point = at_point
    table.get_n_rows = lambda: rows
    table.get_n_columns = lambda: cols
    table.get_accessible_at = cell_at
    table.get_column_header = lambda col: headers[col] if 0 <= col < len(headers) else None

    def row_at_index(index: int) -> int:
        if index < cols:
            return -1
        return (index - cols) // cols

    table.get_row_at_index = row_at_index
    window = _Acc("frame", name="TreeProbe", width=640, height=560)
    _place_row(window, 80, 40, 640, 560)
    scroll = _Acc("scroll pane", width=600, height=484)
    _place_row(scroll, 100, 100, 600, 484)
    bar = _Acc(
        "scroll bar", width=14, height=484, value=0, minimum=0, maximum=6000,
        states=("VERTICAL",),
    )
    _place_row(bar, 700, 100, 14, 484)

    def on_value(acc, new):
        acc.value = float(new)
        head = int(round((float(new) / 6000.0) * (rows - 1)))
        place(head)
        return True

    bar.on_value = on_value
    app = table.get_application()
    window.get_application = lambda: app
    scroll.get_application = lambda: app
    bar.get_application = lambda: app
    _adopt(scroll, table, bar)
    _adopt(window, scroll)
    return window, table, state, place, bar


def _found_scrolls(text: str) -> int:
    return int(text.split(" scroll", 1)[0].removeprefix("found after "))


def test_scrolled_gtk_tree_lists_the_painted_rows(fake_atspi, monkeypatch) -> None:
    """Synthetic 300-row GTK tree, not a live window.

    At the top the painted run is ROW-00000 through ROW-00019. After the
    view moves to row 281, those cells are past child 250 and the rows
    that were on screen are at the GTK sentinel. The snapshot lists the
    painted run, including both columns, and does not drop the tail under
    the dense child cap.
    """
    window, _table, _state, place = _gtk_tree_page()
    driver = _wire_gtk_tree(monkeypatch, window)
    place(0)
    top = driver.snapshot(Scope.WINDOW, "treeprobe")
    titles = _gtk_row_titles(top)
    assert titles[0] == "ROW-00000"
    assert "ROW-00019" in titles
    assert "ROW-00020" not in titles
    assert "val0" in [el.title for el in top.elements]
    assert "Name" in [el.title for el in top.elements]
    _assert_no_elision(top)
    assert observe.interactive_count(top) > 0
    place(281)
    painted = driver.snapshot(Scope.WINDOW, "treeprobe")
    late = _gtk_row_titles(painted)
    assert "ROW-00281" in late
    assert "ROW-00293" in late
    assert "ROW-00299" in late
    assert "ROW-00000" not in late
    assert "val293" in [el.title for el in painted.elements]
    _assert_no_elision(painted)
    assert observe.interactive_count(painted) > 0
    from a11y_computer_use import server

    monkeypatch.setattr(server, "_running_app", lambda name: (None, name))
    runtime = _bare_runtime(driver)
    text = runtime.desktop_snapshot("treeprobe")
    assert "ROW-00293" in text
    assert "custom-drawn" not in text


def test_find_reaches_a_gtk_tree_row_that_is_off_screen(fake_atspi, monkeypatch) -> None:
    """Synthetic 300-row GTK tree, not a live window.

    ROW-00293 is off screen at the top. ``find`` reads it through the Table
    interface, scrolls it into view, and returns the on-screen cell.
    ``scroll_to_find`` matches that cell once it is already painted, and a
    name the table does not have does not move the view.
    """
    from a11y_computer_use import server

    window, _table, state, place = _gtk_tree_page()
    driver = _wire_gtk_tree(monkeypatch, window)
    place(0)
    monkeypatch.setattr(server, "_running_app", lambda name: (None, name))
    runtime = _bare_runtime(driver)
    opening = _gtk_row_titles(driver.snapshot(Scope.WINDOW, "treeprobe"))
    assert "ROW-00000" in opening
    assert "ROW-00293" not in opening
    missed = runtime.find("treeprobe", text="ROW-00999")
    assert "no elements match" in missed
    assert state["head"] == 0
    found = runtime.find("treeprobe", text="row-00293")
    assert "ROW-00293" in found
    assert "no elements match" not in found
    assert state["head"] == 293
    assert "ROW-00293" in _gtk_row_titles(driver.snapshot(Scope.WINDOW, "treeprobe"))
    place(281)
    landed = runtime.scroll_to_find("treeprobe", text="ROW-00293", max_scrolls=0)
    assert landed.startswith("found after 0 scroll")
    assert "ROW-00293" in landed


def test_find_scrolls_a_gtk_tree_with_the_scrollbar_when_scroll_to_is_unsupported(
    fake_atspi, monkeypatch
) -> None:
    """Synthetic tree. GTK ``scroll_to`` returns false, including on screen.

    The vertical bar is a pixel Value. ``find`` sets it from the row index
    and the painted run then contains the name. Not a live window.
    """
    from a11y_computer_use import server

    window, table, state, place = _gtk_tree_page()
    for child in table.children:
        if child.component is not None:
            child.component.scroll_to = lambda *_args, **_kwargs: False
    bar = _Acc(
        "scroll bar", width=14, height=484, value=0, minimum=0, maximum=6000,
        states=("VERTICAL",),
    )
    _place_row(bar, 700, 100, 14, 484)

    def on_value(acc, new):
        acc.value = float(new)
        head = int(round((float(new) / 6000.0) * 299))
        place(max(0, min(head, 299)))
        return True

    bar.on_value = on_value
    scroll = window.children[0]
    _adopt(scroll, table, bar)
    driver = _wire_gtk_tree(monkeypatch, window)
    place(0)
    monkeypatch.setattr(server, "_running_app", lambda name: (None, name))
    runtime = _bare_runtime(driver)
    found = runtime.find("treeprobe", text="ROW-00293")
    assert "ROW-00293" in found
    assert "no elements match" not in found
    assert state["head"] == 293
    assert bar.value > 5000


def _large_gtk_tree(*, rows: int = 10000, cols: int = 3, expose_table: bool = True):
    """Synthetic GTK tree of ``rows``. Not a live window.

    Cells are built when the Table interface or the child list asks for
    them. Off-screen cells sit at the GTK sentinel. ``expose_table`` false
    makes ``get_accessible_at`` return None so the child list is the only
    way to name a deep row.
    """
    state = {"head": 0, "shown": set()}
    sentinel = -2147483648
    titles = ("Name", "Payload", "Index")
    headers = [
        _Acc("table column header", name=titles[col], width=180, height=24)
        for col in range(cols)
    ]
    action = _NS(get_n_actions=lambda: 1, get_action_name=lambda _index: "activate")
    table = _Acc("table", name="Rows", width=600, height=484)
    _bind_gtk_app(table)
    cells: dict[tuple[int, int], _Acc] = {}

    def cell_name(row: int, col: int) -> str:
        if col == 0:
            return f"ROW-{row:05d}"
        if col == 1:
            return f"payload-{row}"
        return str(row)

    def cell(row: int, col: int) -> _Acc:
        key = (row, col)
        found = cells.get(key)
        if found is not None:
            return found
        acc = _Acc("table cell", name=cell_name(row, col), width=180, height=23)
        acc.get_action_iface = lambda action=action: action
        acc.get_index_in_parent = lambda row=row, col=col: cols + row * cols + col
        acc.parent = table
        acc.component.scroll_to = lambda *_args, **_kwargs: False
        _place_row(acc, sentinel, sentinel, 180, 23)
        cells[key] = acc
        return acc

    def place(head: int) -> None:
        previous = set(state["shown"])
        state["head"] = head
        now: set[tuple[int, int]] = set()
        for col, header in enumerate(headers):
            _place_row(header, 110 + col * 200, 100, 180, 24)
        for row in range(head, min(rows, head + 20)):
            for col in range(cols):
                now.add((row, col))
                _place_row(
                    cell(row, col), 110 + col * 200, 124 + (row - head) * 23, 180, 23,
                )
        for key in previous - now:
            acc = cells.get(key)
            if acc is not None:
                _place_row(acc, sentinel, sentinel, 180, 23)
        state["shown"] = now

    def at_point(_x, y, _coord):
        y = int(y)
        if y < 124:
            return headers[0]
        slot = (y - 124) // 23
        if slot < 0 or slot >= 20:
            return None
        row = state["head"] + int(slot)
        if row < 0 or row >= rows:
            return None
        return cell(row, 0)

    table.component.get_accessible_at_point = at_point
    table.get_n_rows = lambda: rows
    table.get_n_columns = lambda: cols

    def accessible_at(row, col):
        if not expose_table or row < 0 or row >= rows or col < 0 or col >= cols:
            return None
        return cell(row, col)

    table.get_accessible_at = accessible_at
    table.get_column_header = lambda col: headers[col] if 0 <= col < cols else None

    def row_at_index(index: int) -> int:
        if index < cols:
            return -1
        return (index - cols) // cols

    table.get_row_at_index = row_at_index
    ordered = list(headers)
    if not expose_table:
        for row in range(20):
            for col in range(cols):
                ordered.append(cell(row, col))
        deep = min(rows - 1, 5000)
        for col in range(cols):
            ordered.append(cell(deep, col))
        end = rows - 1
        if end != deep:
            for col in range(cols):
                ordered.append(cell(end, col))
    for index, header in enumerate(headers):
        header.get_index_in_parent = lambda index=index: index
    _adopt(table, *ordered)
    place(0)
    _place_row(table, 100, 100, 600, 484)
    window = _Acc("frame", name="TreeProbe", width=640, height=560)
    _place_row(window, 80, 40, 640, 560)
    scroll = _Acc("scroll pane", width=600, height=484)
    _place_row(scroll, 100, 100, 600, 484)
    _bind_gtk_app(window)
    _bind_gtk_app(scroll)
    app = table.get_application()
    window.get_application = lambda: app
    scroll.get_application = lambda: app
    _adopt(scroll, table)
    _adopt(window, scroll)
    return window, table, state, place


def test_find_reports_an_offscreen_cell_past_the_old_seek_window(
    fake_atspi, monkeypatch
) -> None:
    """Synthetic 10k-row GTK tree, not a live window.

    ``scroll_to`` is unsupported and there is no bar, so the cell stays at
    the sentinel. ``find`` still names it, with no bounds and an off-screen
    note. The view does not move, and a missing name does not invent a cell.
    """
    import time

    from a11y_computer_use import server

    window, _table, state, _place = _large_gtk_tree()
    driver = _wire_gtk_tree(monkeypatch, window)
    monkeypatch.setattr(server, "_running_app", lambda name: (None, name))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    runtime = _bare_runtime(driver)
    opening = _gtk_row_titles(driver.snapshot(Scope.WINDOW, "treeprobe"))
    assert "ROW-00000" in opening
    assert "ROW-05000" not in opening
    assert "ROW-09999" not in opening
    missed = runtime.find("treeprobe", text="ROW-99999")
    assert "no elements match" in missed
    assert state["head"] == 0
    found = runtime.find("treeprobe", text="ROW-05000")
    assert "no elements match" not in found
    assert "ROW-05000" in found
    assert "off-screen" in found
    assert state["head"] == 0
    shot = runtime._current
    cell = next(el for el in shot.elements if el.title == "ROW-05000")
    assert cell.role == "AXCell"
    assert cell.bounds is None
    end = runtime.find("treeprobe", text="ROW-09999")
    assert "off-screen" in end
    assert "ROW-09999" in end
    assert state["head"] == 0
    end_cell = next(el for el in runtime._current.elements if el.title == "ROW-09999")
    assert end_cell.bounds is None


def test_find_scrolls_a_10k_gtk_row_when_the_bar_can_reach_it(
    fake_atspi, monkeypatch
) -> None:
    """Synthetic 10k-row tree. The bar fraction, not a fabricated box, reveals the cell.

    Row 5000 is past the old 4096-row seek window. Not a live window.
    """
    from a11y_computer_use import server

    window, table, state, place = _large_gtk_tree()
    bar = _Acc(
        "scroll bar", width=14, height=484, value=0, minimum=0, maximum=6000,
        states=("VERTICAL",),
    )
    _place_row(bar, 700, 100, 14, 484)

    def on_value(acc, new):
        acc.value = float(new)
        head = int(round((float(new) / 6000.0) * 9999))
        place(max(0, min(head, 9999)))
        return True

    bar.on_value = on_value
    scroll = window.children[0]
    _adopt(scroll, table, bar)
    driver = _wire_gtk_tree(monkeypatch, window)
    place(0)
    monkeypatch.setattr(server, "_running_app", lambda name: (None, name))
    runtime = _bare_runtime(driver)
    found = runtime.find("treeprobe", text="ROW-05000")
    assert "ROW-05000" in found
    assert "no elements match" not in found
    assert "off-screen" not in found
    assert state["head"] == 5000
    cell = next(el for el in runtime._current.elements if el.title == "ROW-05000")
    assert cell.bounds is not None
    assert cell.bounds.x >= 0 and cell.bounds.y >= 0
    assert cell.bounds.width > 1 and cell.bounds.height > 1


def test_find_names_an_offscreen_child_when_the_table_interface_has_no_cell(
    fake_atspi, monkeypatch
) -> None:
    """Synthetic tree. ``get_accessible_at`` is empty. The child list still has the row.

    The deep cell is not showing. ``find`` reports it off screen and does
    not give it a box. Not a live window.
    """
    import time

    from a11y_computer_use import server

    window, _table, state, _place = _large_gtk_tree(expose_table=False)
    driver = _wire_gtk_tree(monkeypatch, window)
    monkeypatch.setattr(server, "_running_app", lambda name: (None, name))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    runtime = _bare_runtime(driver)
    found = runtime.find("treeprobe", text="ROW-05000")
    assert "no elements match" not in found
    assert "ROW-05000" in found
    assert "off-screen" in found
    assert state["head"] == 0
    cell = next(el for el in runtime._current.elements if el.title == "ROW-05000")
    assert cell.bounds is None
    end = runtime.find("treeprobe", text="ROW-09999")
    assert "ROW-09999" in end
    assert "off-screen" in end


def _found_scrolls(text: str) -> int:
    return int(text.split(" scroll", 1)[0].removeprefix("found after "))


def _attach_large_tree_bar(window, table, place, *, rows: int = 10000):
    """The vertical bar #440 uses. The default tree has none, so find stays off screen."""
    bar = _Acc(
        "scroll bar", width=14, height=484, value=0, minimum=0, maximum=6000,
        states=("VERTICAL",),
    )
    _place_row(bar, 700, 100, 14, 484)

    def on_value(acc, new):
        acc.value = float(new)
        head = int(round((float(new) / 6000.0) * (rows - 1)))
        place(max(0, min(head, rows - 1)))
        return True

    bar.on_value = on_value
    app = table.get_application()
    bar.get_application = lambda: app
    scroll = window.children[0]
    _adopt(scroll, table, bar)
    place(0)
    return bar


def test_scroll_to_find_reaches_a_10k_gtk_row_inside_the_scroll_budget(
    fake_atspi, monkeypatch
) -> None:
    """Synthetic 10000×3 GTK tree, not a live window.

    Five wheel lines move about a dozen rows, so 50 of them stay near row
    650. The table row index sets the vertical bar once. ROW-05000 and
    ROW-09999 are on screen inside that budget, and a line wheel is not sent.
    """
    from a11y_computer_use import server

    window, table, state, place = _large_gtk_tree()
    _attach_large_tree_bar(window, table, place)
    driver = _wire_gtk_tree(monkeypatch, window)
    wheels = {"n": 0}
    keys = {"n": 0}
    original = driver.scroll

    def counting_scroll(*args, **kwargs):
        wheels["n"] += 1
        return original(*args, **kwargs)

    driver.scroll = counting_scroll
    driver.key_chord = lambda *_args, **_kwargs: keys.__setitem__("n", keys["n"] + 1)
    driver.type_text = lambda *_args, **_kwargs: keys.__setitem__("n", keys["n"] + 1)
    monkeypatch.setattr(server, "_running_app", lambda name: (None, name))
    runtime = _bare_runtime(driver)
    opening = runtime.scroll_to_find("treeprobe", text="ROW-00000", max_scrolls=50)
    assert opening.startswith("found after 0 scroll"), opening
    assert state["head"] == 0
    found = runtime.scroll_to_find("treeprobe", text="ROW-05000", max_scrolls=50)
    assert found.startswith("found after "), found
    assert "not found" not in found
    assert "ROW-05000" in found
    assert _found_scrolls(found) <= 6
    assert state["head"] == 5000
    shot = driver.snapshot(Scope.WINDOW, "treeprobe")
    cell = next(
        el for el in shot.elements
        if el.title == "ROW-05000" and el.bounds is not None and el.bounds.y >= 0
    )
    assert cell.bounds.width > 1 and cell.bounds.height > 1
    assert cell.bounds.x >= 0
    far = runtime.scroll_to_find("treeprobe", text="ROW-09999", max_scrolls=50)
    assert far.startswith("found after "), far
    assert "not found" not in far
    assert "ROW-09999" in far
    assert _found_scrolls(far) <= 6
    assert state["head"] >= 9980
    assert "ROW-09999" in _gtk_row_titles(driver.snapshot(Scope.WINDOW, "treeprobe"))
    assert wheels["n"] == 0
    assert keys["n"] == 0


def test_gtk_viewport_page_moves_a_large_tree_by_painted_rows(fake_atspi, monkeypatch) -> None:
    """Synthetic tree. One viewport step is the painted run, not five lines.

    The scroll anchor is the scroll pane. A Chromium table is left alone.
    Not a live window.
    """
    window, table, state, place = _large_gtk_tree()
    bar = _attach_large_tree_bar(window, table, place)
    _wire_gtk_tree(monkeypatch, window)
    scroll = window.children[0]
    assert scroll.get_role_name() == "scroll pane"
    assert _atspi.scroll_gtk_viewport_page(scroll, 1) is True
    assert 15 <= state["head"] <= 25
    assert bar.value > 0
    chrome = _Acc("table", name="chrome")
    app = _Acc("application", name="chrome")
    app.get_toolkit_name = lambda: ""
    chrome.get_application = lambda: app
    assert _atspi.scroll_gtk_viewport_page(chrome, 1) is False
    assert _atspi.reveal_gtk_named_cell(chrome, "ROW-05000") is None


def test_short_gtk_tree_keeps_the_index_walk(fake_atspi, monkeypatch) -> None:
    """Synthetic 5-row tree, not a live window. The visible-span walk is not used."""
    table = _Acc("table", name="Rows", width=200, height=140)
    _bind_gtk_app(table)
    table.get_n_rows = lambda: 5
    table.get_n_columns = lambda: 1
    rows = []
    for index, name in enumerate(("Row A", "Row B", "Row C", "Row D", "Row E")):
        row = _Acc("table cell", name=name, width=180, height=20)
        _place_row(row, 10, 20 + index * 20, 180, 20)
        rows.append(row)
    _adopt(table, *rows)
    _place_row(table, 10, 10, 200, 140)

    def boom(*_args):
        raise AssertionError("a short tree was hit-tested")

    table.component.get_accessible_at_point = boom
    window = _Acc("frame", name="short", width=400, height=300)
    _place_row(window, 0, 0, 400, 300)
    _adopt(window, table)
    driver = _wire_gtk_tree(monkeypatch, window)
    titles = [el.title for el in driver.snapshot(Scope.WINDOW, "short").elements]
    for name in ("Row A", "Row B", "Row C", "Row D", "Row E"):
        assert name in titles


def test_header_only_table_still_adds_its_body_cells(fake_atspi) -> None:
    """Synthetic file list. No on-screen span, so the body still comes from the Table."""
    header = _Acc("table column header", name="Name")
    cells = [_Acc("table cell", name=f"file-{index}") for index in range(40)]
    table = _Acc("table", name="Files")
    _bind_gtk_app(table)
    table.get_n_rows = lambda: 40
    table.get_n_columns = lambda: 1
    table.get_accessible_at = lambda row, _col: cells[row]
    _adopt(table, header)
    names = [kid.get_name() for kid in _atspi.ATSPIAccessor().children(table)]
    assert names[0] == "Name"
    assert "file-0" in names
    assert "file-39" in names


def test_shared_java_column_headers_keep_each_columns_name(fake_atspi, monkeypatch) -> None:
    """Synthetic headers that share one Java ATK name. Not a live table.

    ``get_column_header`` rewrites the name on every object already
    returned. The snapshot still shows ID, Item, and Qty at their own x,
    and ``find`` hits Item. Reading the raw objects afterwards yields Qty.
    """
    from a11y_computer_use import server

    shared = {"name": ""}
    columns = ("ID", "Item", "Qty")
    xs = (46, 263, 481)
    headers = []
    for x in xs:
        header = _Acc("table column header", name="unset", width=200, height=24)
        header.get_name = lambda shared=shared: shared["name"]
        headers.append(header)
    app = _Acc("application", name="swingprobe")
    app.get_toolkit_name = lambda: "JAVA"
    table = _Acc("table", name="Rows", width=640, height=400)
    table.get_application = lambda: app
    cells = []
    for col in range(3):
        cell = _Acc("table cell", name=f"cell-{col}", width=200, height=22)
        cell.get_index_in_parent = lambda index=3 + col: index
        cell.get_application = lambda: app
        cells.append(cell)
    _adopt(table, *headers, *cells)
    for col, header in enumerate(headers):
        _place_row(header, xs[col], 80, 200, 24)
        header.get_application = lambda: app
    for col, cell in enumerate(cells):
        _place_row(cell, xs[col], 110, 200, 22)
    _place_row(table, 40, 70, 640, 400)

    def column_header(col):
        shared["name"] = columns[col]
        return headers[col]

    table.get_column_header = column_header
    table.get_n_rows = lambda: 30
    table.get_n_columns = lambda: 3
    table.get_accessible_at = lambda row, col: cells[col] if row == 0 else None
    table.get_row_at_index = lambda index: -1 if index < 3 else 0
    table.component.get_accessible_at_point = lambda _x, _y, _coord: cells[0]
    window = _Acc("frame", name="SwingHeaders", width=720, height=500)
    _place_row(window, 20, 40, 720, 500)
    window.get_application = lambda: app
    _adopt(window, table)
    driver = _wire_gtk_tree(monkeypatch, window)
    snap = driver.snapshot(Scope.WINDOW, "swingprobe")
    titles = [el.title for el in snap.elements]
    assert titles.count("ID") == 1
    assert titles.count("Item") == 1
    assert titles.count("Qty") == 1
    by_title = {el.title: el for el in snap.elements if el.title in columns}
    assert by_title["ID"].bounds.x == 46
    assert by_title["Item"].bounds.x == 263
    assert by_title["Qty"].bounds.x == 481
    assert all(header.get_name() == "Qty" for header in headers)
    monkeypatch.setattr(server, "_running_app", lambda name: (None, name))
    found = _bare_runtime(driver).find("swingprobe", text="Item")
    assert "no elements match" not in found
    assert "Item" in found
def _bind_java_app(node: _Acc) -> _Acc:
    """Toolkit ``JAVA`` so the table takes the large-table walk and not Qt."""
    app = getattr(node, "_java_app", None)
    if app is None:
        app = _Acc("application", name="swingprobe")
        app.get_toolkit_name = lambda: "JAVA"
        node._java_app = app
    node.get_application = lambda app=app: app
    return app


def _java_table_page():
    """Synthetic 200×3 Swing table. Not a live window.

    The table box runs to the bottom of a 1280×800 screen. The scroll pane
    ends at y=616 and the window at y=635. Rows below the pane keep screen
    coordinates and omit SHOWING. The item text lives on a child label.
    """
    state = {"head": 0}
    headers = [
        _Acc("table column header", name=name, width=170, height=24, states={"SHOWING", "VISIBLE"})
        for name in ("ID", "Item", "Qty")
    ]
    cells: list[list[tuple[_Acc, _Acc]]] = []
    for row in range(200):
        pair = []
        for col, text in enumerate((str(row), f"ROW-{row:03d}", str(row))):
            label = _Acc("label", name=text, width=170, height=22)
            cell = _Acc("table cell", name="", width=170, height=22)
            cell.get_index_in_parent = lambda index=3 + row * 3 + col: index
            _adopt(cell, label)
            pair.append((cell, label))
        cells.append(pair)
    table = _Acc("table", name="Rows", width=540, height=720)
    app = _bind_java_app(table)
    ordered: list[_Acc] = list(headers)
    for row in cells:
        for cell, label in row:
            ordered.append(cell)
            _bind_java_app(cell)
            _bind_java_app(label)
            cell._java_app = table._java_app
            label._java_app = table._java_app
            cell.get_application = lambda app=app: app
            label.get_application = lambda app=app: app
    for header in headers:
        _bind_java_app(header)
        header._java_app = table._java_app
        header.get_application = lambda app=app: app
    _adopt(table, *ordered)

    def place(head: int) -> None:
        state["head"] = head
        for col, header in enumerate(headers):
            _place_row(header, 46 + col * 180, 80, 170, 24)
        for row in range(200):
            y = 104 + (row - head) * 22
            showing = 80 <= y < 616 and y + 22 > 80
            states = {"ENABLED", "VISIBLE", "SENSITIVE"}
            if showing:
                states.add("SHOWING")
            for col, (cell, label) in enumerate(cells[row]):
                _place_row(cell, 46 + col * 180, y, 170, 22)
                _place_row(label, 46 + col * 180, y, 170, 22)
                cell.states = set(states)
                label.states = set(states)

    def scroll_to(_scroll_type, row: int) -> bool:
        place(row)
        return True

    for row in range(200):
        for cell, _label in cells[row]:
            cell.component.scroll_to = lambda _scroll_type, row=row: scroll_to(_scroll_type, row)
    place(0)
    _place_row(table, 46, 80, 540, 720)

    def at_point(_x, y, _coord):
        y = int(y)
        if y < 104:
            return headers[0]
        row = state["head"] + (y - 104) // 22
        if row < 0 or row >= 200:
            return None
        return cells[row][0][0]

    table.component.get_accessible_at_point = at_point
    table.get_n_rows = lambda: 200
    table.get_n_columns = lambda: 3
    table.get_accessible_at = lambda row, col: cells[row][col][0]
    table.get_column_header = lambda col: headers[col]
    table.get_row_at_index = lambda index: -1 if index < 3 else (index - 3) // 3
    window = _Acc("frame", name="SwingTable", width=640, height=595)
    _place_row(window, 20, 40, 640, 595)
    scroll = _Acc("scroll pane", width=560, height=536)
    _place_row(scroll, 40, 80, 560, 536)
    for node in (window, scroll):
        node._java_app = table._java_app
        node.get_application = lambda app=app: app
    _adopt(scroll, table)
    _adopt(window, scroll)
    return window, table, state, place, cells


def _swing_row_titles(snap) -> list[str]:
    return [el.title for el in snap.elements if el.title.startswith("ROW-")]


def test_swing_table_lists_only_rows_inside_the_viewport(fake_atspi, monkeypatch) -> None:
    """Synthetic Swing table, not a live window.

    Rows below the scroll pane still overlap the screen. They are not
    listed, and their coordinates are not published.
    """
    window, _table, _state, _place, _cells = _java_table_page()
    driver = _wire_gtk_tree(monkeypatch, window)
    snap = driver.snapshot(Scope.WINDOW, "swingprobe")
    titles = _swing_row_titles(snap)
    assert "ROW-000" in titles
    assert "ROW-023" in titles
    assert "ROW-024" not in titles
    assert "ROW-030" not in titles
    assert "ROW-150" not in titles
    names = [el.title for el in snap.elements]
    assert names.count("ID") == 1
    assert "Item" in names
    for el in snap.elements:
        if not el.title.startswith("ROW-") or el.bounds is None:
            continue
        assert el.bounds.y < 616, (el.title, el.bounds)
        assert el.bounds.y + el.bounds.height <= 800


def test_find_scrolls_a_swing_row_that_is_below_the_viewport(fake_atspi, monkeypatch) -> None:
    """Synthetic Swing table. The item name is on the cell's child.

    ``find`` reaches ROW-030, which overlaps the screen below the pane,
    and ROW-150, which is off the screen. Both come back inside the
    viewport. A missing name does not move the view.
    """
    from a11y_computer_use import server

    window, _table, state, _place, _cells = _java_table_page()
    driver = _wire_gtk_tree(monkeypatch, window)
    monkeypatch.setattr(server, "_running_app", lambda name: (None, name))
    runtime = _bare_runtime(driver)
    missed = runtime.find("swingprobe", text="ROW-999")
    assert "no elements match" in missed
    assert state["head"] == 0
    found = runtime.find("swingprobe", text="ROW-030")
    assert "no elements match" not in found
    assert "ROW-030" in found
    assert state["head"] == 30
    shown = next(el for el in runtime._current.elements if el.title == "ROW-030")
    assert shown.bounds is not None and shown.bounds.y < 616
    far = runtime.find("swingprobe", text="ROW-150")
    assert "no elements match" not in far
    assert "ROW-150" in far
    assert state["head"] == 150
    landed = next(el for el in runtime._current.elements if el.title == "ROW-150")
    assert landed.bounds is not None and landed.bounds.y < 616


def test_find_does_not_publish_a_swing_row_it_cannot_scroll(fake_atspi, monkeypatch) -> None:
    """Synthetic Swing table. A failed scroll does not invent a box."""
    from a11y_computer_use import server

    window, _table, state, _place, cells = _java_table_page()
    for row in cells:
        for cell, _label in row:
            cell.component.scroll_to = lambda *_args, **_kwargs: False
    driver = _wire_gtk_tree(monkeypatch, window)
    monkeypatch.setattr(server, "_running_app", lambda name: (None, name))
    runtime = _bare_runtime(driver)
    found = runtime.find("swingprobe", text="ROW-150")
    assert "no elements match" in found
    assert state["head"] == 0
    assert not any(el.title == "ROW-150" for el in runtime._current.elements)


def _record_clicks(monkeypatch) -> list:
    clicks: list[tuple] = []
    monkeypatch.setattr("a11y_computer_use.drivers.linux._on_wayland", lambda: False)
    monkeypatch.setattr(
        _linux_input, "click",
        lambda x, y, *, button="left", count=1: clicks.append((int(x), int(y), count)),
    )
    monkeypatch.setattr(LinuxDriver, "_pointer", lambda self, x, y: (int(x), int(y)))
    monkeypatch.setattr(LinuxDriver, "_note_pointer", lambda self, x, y: None)
    return clicks


def test_click_scrolls_an_unpainted_swing_cell_and_uses_the_new_center(
    fake_atspi, monkeypatch,
) -> None:
    """Synthetic Swing cell. The published box is below the window.

    The click scrolls that same cell and lands on its new center. A cell
    that is already painted keeps the published point. A point target is
    those pixels.
    """
    window, _table, state, _place, cells = _java_table_page()
    driver = _wire_gtk_tree(monkeypatch, window)
    clicks = _record_clicks(monkeypatch)
    painted = cells[0][1][1]
    published = Element(
        "e0", "AXStaticText", "ROW-000", "ROW-000",
        Bounds(0, 10, 20, 30, 40), "snap-painted", clickable=True,
    )
    observe._register_epoch(published.snapshot_id, {}, {"e0": painted})
    driver.click(published)
    assert clicks[-1] == (25, 40, 1)
    assert state["head"] == 0

    driver.click(Point(0, 400, 700))
    assert clicks[-1] == (400, 700, 1)
    assert state["head"] == 0

    label = cells[30][1][1]
    element = Element(
        "e30", "AXStaticText", "ROW-030", "ROW-030",
        Bounds(0, 226, 764, 170, 22), "snap-swing", clickable=True,
    )
    observe._register_epoch(element.snapshot_id, {}, {"e30": label})
    driver.click(element)
    assert state["head"] == 30
    x, y, _count = clicks[-1]
    assert y < 616
    assert abs(x - (226 + 85)) <= 1
    assert abs(y - (104 + 11)) <= 1


def test_click_refuses_an_unpainted_swing_cell_that_cannot_be_scrolled(
    fake_atspi, monkeypatch,
) -> None:
    """Synthetic Swing cell. No pointer event is sent, and no center is invented."""
    _window, _table, state, _place, cells = _java_table_page()
    for row in cells:
        for cell, _label in row:
            cell.component.scroll_to = lambda *_args, **_kwargs: False
    driver = _wire_gtk_tree(monkeypatch, _window)
    clicks = _record_clicks(monkeypatch)
    label = cells[30][1][1]
    element = Element(
        "e30", "AXStaticText", "ROW-030", "ROW-030",
        Bounds(0, 226, 764, 170, 22), "snap-hidden", clickable=True,
    )
    observe._register_epoch(element.snapshot_id, {}, {"e30": label})
    with pytest.raises(ComputerUseError) as exc:
        driver.click(element)
    assert exc.value.code is ErrorCode.NOT_VISIBLE
    assert exc.value.detail["reason"] == "not_visible"
    assert exc.value.detail["outcome"] == "refused"
    assert "clicked" not in exc.value.message
    assert "not sent" in exc.value.message
    assert clicks == []
    assert state["head"] == 0


def test_qt_table_does_not_use_the_gtk_visible_span(fake_atspi, monkeypatch) -> None:
    """Synthetic Qt grid, not a live Qt window. The GTK span walk is not used."""
    table = _Acc("table", name="grid", width=200, height=400)
    app = _Acc("application", name="qtprobe")
    app.get_toolkit_name = lambda: "Qt"
    table.get_application = lambda: app
    table.get_n_rows = lambda: 40
    table.get_n_columns = lambda: 1
    rows = []
    for index in range(40):
        row = _Acc("table cell", name=f"R{index}", width=180, height=16)
        _place_row(row, 8, 8 + index * 16, 180, 16)
        rows.append(row)
    _adopt(table, *rows)
    _place_row(table, 8, 8, 200, 400)

    def boom(*_args):
        raise AssertionError("a Qt table was hit-tested as a GTK tree")

    table.component.get_accessible_at_point = boom
    window = _Acc("frame", name="qtprobe", width=400, height=700)
    _place_row(window, 0, 0, 400, 700)
    window.get_application = lambda: app
    _adopt(window, table)
    driver = _wire_gtk_tree(monkeypatch, window)
    titles = [el.title for el in driver.snapshot(Scope.WINDOW, "qtprobe").elements]
    assert "R0" in titles
    assert "R11" in titles


class _WindowNode:
    """AT-SPI stand-in for showing_toplevels. No bus."""

    def __init__(self, role, name="", states=(), children=()):
        self.role = role
        self.name = name
        self.states = set(states)
        self.children = list(children)
        self.parent = None
        for child in self.children:
            child.parent = self

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def get_parent(self):
        return self.parent

    def get_application(self):
        node = self
        while node.parent is not None:
            node = node.parent
        return node

    def get_state_set(self):
        return _NS(names=set(self.states))


def test_showing_toplevels_lists_showing_windows_active_first() -> None:
    dialog = _WindowNode("dialog", "Preferences", states={"SHOWING"})
    editor = _WindowNode("frame", "Editor", states={"SHOWING", "ACTIVE"})
    hidden = _WindowNode("frame", "Minimized")
    menu = _WindowNode("menu", "File", states={"SHOWING"})
    _WindowNode("application", "mousepad", children=[dialog, editor, hidden, menu])
    assert [node.name for node in _atspi.showing_toplevels(dialog)] == ["Editor", "Preferences"]
    only = _WindowNode(
        "application", "solo",
        children=[_WindowNode("frame", "Only", states={"SHOWING", "ACTIVE"})],
    )
    assert [node.name for node in _atspi.showing_toplevels(only)] == ["Only"]


def test_showing_toplevels_skips_a_gtk_combo_popup(monkeypatch) -> None:
    dialog = _WindowNode("dialog", "Form", states={"SHOWING"})
    popup = _WindowNode("window", "", states={"SHOWING", "ACTIVE"})
    _WindowNode("application", "app", children=[popup, dialog])

    def owns(node):
        return node if node is popup else None

    monkeypatch.setattr(_atspi, "_combo_owning_popup", owns)
    assert [node.name for node in _atspi.showing_toplevels(dialog)] == ["Form"]


def test_snapshot_window_list_skips_chrome_cache_frames() -> None:
    """A find bar and an empty cached frame are not sibling windows.

    Chrome keeps both beside the page, and both are SHOWING. Listing them
    made the snapshot an untitled group with no rows, so scroll_to_find
    stopped before the wheel. The named page is the only window. A GTK
    dialog beside its editor stays.
    """
    filler = _WindowNode("panel", "")
    panel = _WindowNode("panel", "", children=[filler])
    empty = _WindowNode("frame", "", states={"SHOWING", "ACTIVE"}, children=[panel])
    page = _WindowNode("frame", "rowtable - Google Chrome", states={"SHOWING"})
    bar = _WindowNode("frame", "Find in page", states={"SHOWING"})
    _WindowNode("application", "Google Chrome", children=[empty, bar, page])
    assert [node.name for node in _atspi.snapshot_window_list(empty)] == [
        "rowtable - Google Chrome",
    ]
    dialog = _WindowNode("dialog", "Preferences", states={"SHOWING"})
    editor = _WindowNode("frame", "Editor", states={"SHOWING", "ACTIVE"})
    _WindowNode("application", "mousepad", children=[dialog, editor])
    assert [node.name for node in _atspi.snapshot_window_list(dialog)] == [
        "Editor", "Preferences",
    ]


def test_raise_skips_a_window_that_is_already_the_ewmh_active_one(monkeypatch) -> None:
    driver = LinuxDriver()
    raised: list[int] = []
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(
        _atspi, "toplevel_raise_target",
        lambda node: (False, "cuatypeform", 9, (10.0, 20.0, 800.0, 600.0)),
    )
    monkeypatch.setattr(
        "a11y_computer_use.drivers._linux_system.window_id_for_toplevel",
        lambda **_kwargs: 42,
    )
    monkeypatch.setattr(
        "a11y_computer_use.drivers._linux_system.active_window",
        lambda: {"window_id": 42, "app": "chrome", "pid": 9, "title": "cuatypeform"},
    )
    monkeypatch.setattr(driver, "raise_window", raised.append)
    driver._raise_handle_window(object())
    assert raised == []
    monkeypatch.setattr(
        "a11y_computer_use.drivers._linux_system.active_window",
        lambda: {"window_id": 7, "app": "mousepad", "pid": 3, "title": "Editor"},
    )
    driver._raise_handle_window(object())
    assert raised == [42]


def test_window_id_for_toplevel_prefers_the_title(monkeypatch) -> None:
    from a11y_computer_use.drivers import _linux_system

    rows = [
        {"window_id": 1, "title": "Editor", "pid": 9, "on_screen": True,
         "bounds": {"x": 0, "y": 0, "width": 400, "height": 300}},
        {"window_id": 2, "title": "Preferences", "pid": 9, "on_screen": True,
         "bounds": {"x": 420, "y": 40, "width": 280, "height": 200}},
        {"window_id": 3, "title": "Editor", "pid": 4, "on_screen": True,
         "bounds": {"x": 0, "y": 0, "width": 400, "height": 300}},
        {"window_id": 4, "title": "Hidden", "pid": 9, "on_screen": False, "bounds": None},
    ]
    monkeypatch.setattr(_linux_system, "windows", lambda: rows)
    assert _linux_system.window_id_for_toplevel(
        title="Preferences", pid=9, extents=(430, 50, 100, 80),
    ) == 2
    assert _linux_system.window_id_for_toplevel(
        title="", pid=9, extents=(10, 10, 100, 80),
    ) == 1
    assert _linux_system.window_id_for_toplevel(
        title="Missing", pid=9, extents=None,
    ) is None


def _outline_cell(name: str, actions: list[str]) -> _Acc:
    cell = _Acc(
        "table cell", name=name, width=180, height=21,
        states=("SHOWING", "VISIBLE", "SELECTABLE", "ENABLED", "SENSITIVE"),
    )
    _place_row(cell, 48, 100, 180, 21)
    cell.actions = list(actions)
    if actions:
        cell.get_action_iface = lambda cell=cell: cell
        cell.get_n_actions = lambda cell=cell: len(cell.actions)
        cell.get_action_name = lambda index, cell=cell: cell.actions[index]
    return cell


def _outline_window(*cells: _Acc, role: str = "tree table") -> _Acc:
    """A GTK outline. Not a live Geany window."""
    tree = _Acc(role, name="Symbols", width=220, height=320)
    _bind_gtk_app(tree)
    _place_row(tree, 40, 80, 220, 320)

    def select_child(index: int) -> bool:
        child = tree.children[index]
        child.states.add("SELECTED")
        return True

    tree.select_child = select_child
    tree.get_selection_iface = lambda: tree
    _adopt(tree, *cells)
    window = _Acc("frame", name="outline", width=640, height=480)
    _place_row(window, 20, 20, 640, 480)
    app = tree.get_application()
    window.get_application = lambda: app
    for cell in cells:
        cell.get_application = lambda app=app: app
    _adopt(window, tree)
    return window


def test_gtk_outline_row_says_it_activates_on_double_click(fake_atspi) -> None:
    """Synthetic GTK tree table. Not a live Geany window.

    A cell whose actions are expand, edit, then activate only selects on a
    single click. The snapshot says the row activates on double-click. A
    cell whose own action is click, and a cell in a plain table, do not.
    The table check does not walk that table's children.
    """
    symbol = _outline_cell("target_fn_row150 [301]", ["expand or contract", "edit", "activate"])
    bare = _outline_cell("plain_row", [])
    clicking = _outline_cell("opens_on_click", ["click"])
    _outline_window(symbol, bare, clicking)
    assert _atspi.outline_double_click_note(symbol) == "activates on double-click"
    assert _atspi.outline_double_click_note(bare) == "activates on double-click"
    assert _atspi.outline_double_click_note(clicking) == ""
    table = _Acc("table", name="grid")
    _bind_gtk_app(table)
    table.get_selection_iface = lambda: table
    scanned: list[int] = []

    def child_at(index, table=table):
        scanned.append(index)
        return table.children[index]

    table.get_child_at_index = child_at
    grid = _outline_cell("A1", [])
    _adopt(table, grid)
    grid.get_application = table.get_application
    assert _atspi.outline_double_click_note(grid) == ""
    assert scanned == []


def test_click_on_an_outline_row_stays_a_single_click(
    fake_atspi, monkeypatch, xtest_recorder, tmp_path
) -> None:
    """Synthetic GTK tree table. Not a live Geany window.

    count=1 selects the row and says it activates on double-click. It does
    not send a double-click and it does not run the activate action.
    count=2 is the double-click the caller asked for.
    """
    from a11y_computer_use import observe, safety, server

    events, _display = xtest_recorder
    symbol = _outline_cell("target_fn_row150 [301]", ["expand or contract", "edit", "activate"])
    window = _outline_window(symbol)
    driver = _wire_gtk_tree(monkeypatch, window)
    monkeypatch.setattr(server, "_running_app", lambda name: (None, name))
    monkeypatch.setattr(safety, "seconds_since_user_input", lambda: None)
    named: list[str] = []
    real_named = _atspi.do_named_action

    def record(acc, name: str) -> bool:
        named.append(name)
        return real_named(acc, name)

    monkeypatch.setattr(_atspi, "do_named_action", record)
    shot = driver.snapshot(Scope.WINDOW, "outline")
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier(shot.app or "outline", safety.Tier.FULL)
    runtime = server.Runtime(
        store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver,
    )
    runtime._current = shot
    # count=2 is a real pointer click. The recheck hit-tests the desktop
    # under this synthetic box, which is the runner's own terminal.
    runtime._recheck_target = lambda *_args, **_kwargs: None
    cell = next(el for el in shot.elements if "target_fn_row150" in el.title)
    assert cell.note == "activates on double-click"
    rendered = observe.render_matches(shot, (cell,))
    assert "(activates on double-click)" in rendered
    events.clear()
    single = runtime.click(cell.ref)
    assert "clicked" in single
    assert "(activates on double-click)" in single
    assert single.outcome == "partial"
    assert single.evidence == "the row was selected and activates on double-click"
    presses = [event for event in events if event[0] == _X_BPRESS]
    assert len(presses) <= 1, events
    assert named == []
    events.clear()
    named.clear()
    shot = driver.snapshot(Scope.WINDOW, "outline")
    runtime._current = shot
    cell = next(el for el in shot.elements if "target_fn_row150" in el.title)
    double = runtime.click(cell.ref, count=2)
    assert "clicked" in double
    assert "(activates on double-click)" not in double
    assert double.outcome != "partial"
    assert double.evidence != "the row was selected and activates on double-click"
    presses = [event for event in events if event[0] == _X_BPRESS]
    assert len(presses) == 2, events
    assert named == []


def test_java_button_without_showing_has_vanished_and_a_showing_one_has_not(fake_atspi) -> None:
    """Synthetic Java button. Not a live dialog.

    DEFUNCT is gone on every toolkit. A Java state set that was reported
    and does not include SHOWING is the disposed dialog button. A showing
    Java button, and a GTK button that simply omits SHOWING, are not.
    """
    app = _Acc("application", name="java")
    app.get_toolkit_name = lambda: "JAVA"
    hidden = _Acc("push button", name="Yes", states={"ENABLED", "SENSITIVE", "VISIBLE"})
    hidden.get_application = lambda: app
    showing = _Acc("push button", name="Yes", states={"ENABLED", "SHOWING", "VISIBLE"})
    showing.get_application = lambda: app
    empty = _Acc("push button", name="Yes", states=())
    empty.get_application = lambda: app
    assert _atspi.target_vanished(hidden) is True
    assert _atspi.target_vanished(showing) is False
    assert _atspi.target_vanished(empty) is False
    dead = _Acc("push button", name="Save", states={"DEFUNCT"})
    assert _atspi.target_vanished(dead) is True
    gtk = _Acc("push button", name="Save", states={"ENABLED"})
    gtk_app = _Acc("application", name="gtk")
    gtk_app.get_toolkit_name = lambda: "gtk"
    gtk.get_application = lambda: gtk_app
    assert _atspi.target_vanished(gtk) is False
    cell = _Acc("table cell", name="ROW-030", states={"ENABLED", "VISIBLE"})
    cell.get_application = lambda: app
    assert _atspi.target_vanished(cell) is False


@pytest.mark.parametrize("effect", ["exclusive", "unchanged", "additive", "no_bounds"])
@pytest.mark.parametrize("named", [True, False])
def test_gtk_icon_ref_click_selects_without_activating(fake_atspi, monkeypatch, effect, named):
    driver, icon, element, actions, events = _toggle_click_target(
        monkeypatch, toolkit="gtk", role="image", actions=("activate",),
    )
    icon.name = "Cherry" if named else ""
    previous = _Acc("image", states=("SELECTED",))
    _adopt(_Acc("layered pane"), previous, icon)
    _place_row(icon, 40, 60, 100, 20)
    if effect == "no_bounds":
        icon.component = None
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)

    def click(x, y, *, button="left", count=1):
        events.append(("click", x, y, button, count))
        if effect in {"exclusive", "additive"}:
            icon.states.add("SELECTED")
        if effect == "exclusive":
            previous.states.discard("SELECTED")

    monkeypatch.setattr(_linux_input, "click", click)
    if effect == "exclusive":
        assert driver.press_element(element) is True
    else:
        with pytest.raises(ComputerUseError) as exc:
            driver.press_element(element)
        assert exc.value.detail["reason"] == "selection_unchanged"
    assert actions == []
    assert events == [("raise", icon)] + (
        [] if effect == "no_bounds" else [("click", 90, 70, "left", 1)]
    )


def test_icon_view_detection_excludes_other_images(fake_atspi, monkeypatch):
    monkeypatch.setattr(_atspi, "_toolkit_name", lambda acc: acc.toolkit)
    icon = _Acc("image")
    icon.toolkit = "gtk"
    _adopt(_Acc("panel"), icon)
    assert not _atspi.gtk_icon_view_item(icon)
    _adopt(_Acc("layered pane"), icon)
    assert _atspi.gtk_icon_view_item(icon)
    icon.toolkit = "Chromium"
    assert not _atspi.gtk_icon_view_item(icon)



@pytest.mark.parametrize("name", [
    "xfce4-terminal", "gnome-terminal", "gnome-terminal-", "xterm", "konsole",
    "/usr/bin/xfce4-terminal", "org.gnome.Terminal.desktop", "x-terminal-emulator",
])
def test_terminal_launch_exec_disabled_is_audited(monkeypatch, tmp_path, name) -> None:
    """A FULL app grant cannot authorize shell execution through a terminal."""
    import json
    from a11y_computer_use import safety, server

    driver = LinuxDriver()
    monkeypatch.setattr(driver, "launch_app", lambda *_args, **_kwargs: pytest.fail("launched"))
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier(name, safety.Tier.FULL)
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    with pytest.raises(ComputerUseError) as caught:
        runtime.app("launch", name)
    assert caught.value.code is ErrorCode.EXEC_DISABLED
    row, = [json.loads(line) for path in (tmp_path / "audit").glob("*.jsonl")
            for line in path.read_text().splitlines()]
    assert row["action"] == "appop" and row["result"] == "exec_disabled"
    assert row["metrics"]["security_violation"] == "exec_disabled"


@pytest.mark.parametrize("operation", ["type", "set_value", "key", "cached_type"])
def test_embedded_terminal_input_is_refused_and_redacted(
    fake_atspi, monkeypatch, tmp_path, operation,
) -> None:
    """Native terminal ancestry survives AXTextArea mapping in an ordinary app."""
    import json
    from a11y_computer_use import safety, server
    from a11y_computer_use.schema import KeyChord, TypeText

    terminal = _Acc("terminal")
    text = _Acc("text")
    _adopt(_Acc("frame"), _adopt(terminal, text))
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    focused = _Acc("text") if operation in {"set_value", "cached_type"} else text
    if operation == "cached_type":
        driver._focused_editable = text
        monkeypatch.setattr(driver, "_verify_remembered_editable", lambda handle: "editor")
    monkeypatch.setattr(_atspi, "_focused_node", lambda app: (focused, False))
    monkeypatch.setattr("a11y_computer_use.drivers.linux._resolved_app", lambda app: app)
    element = Element(
        ref="e2", role="AXTextArea", title="", value=None, bounds=None,
        snapshot_id="terminal-shot",
    )
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: text)
    actions = {
        "type": TypeText(text="touch /tmp/forbidden\n"),
        "cached_type": TypeText(text="touch /tmp/forbidden\n"),
        "set_value": TypeText(text="touch /tmp/forbidden\n"),
        "key": KeyChord(chord="Return"),
    }
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier("editor", safety.Tier.FULL)
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    action = actions[operation]
    with pytest.raises(ComputerUseError) as caught:
        runtime._run_gated(
            action, "editor", lambda: pytest.fail("injected input"),
            input_target=element if operation == "set_value" else None,
        )
    assert caught.value.code is ErrorCode.EXEC_DISABLED
    row, = [json.loads(line) for path in (tmp_path / "audit").glob("*.jsonl")
            for line in path.read_text().splitlines()]
    assert row["result"] == "exec_disabled"
    assert "forbidden" not in json.dumps(row)
    # Explicit opt-in permits this path; a normal text area never needs it.
    runtime.allow_exec = True
    assert runtime._run_gated(action, "editor", lambda: "allowed") == "allowed"
    runtime.allow_exec = False
    terminal.role = "text"
    assert runtime._run_gated(action, "editor", lambda: "editor input") == "editor input"


def test_terminal_focus_change_before_input_is_audited(fake_atspi, monkeypatch, tmp_path) -> None:
    """A terminal appearing during the recheck cannot receive keyboard input."""
    import json
    from a11y_computer_use import safety, server
    from a11y_computer_use.schema import TypeText

    field = _Acc("text")
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(_atspi, "_focused_node", lambda app: (field, False))
    monkeypatch.setattr("a11y_computer_use.drivers.linux._resolved_app", lambda app: app)
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier("editor", safety.Tier.FULL)
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    with pytest.raises(ComputerUseError) as caught:
        runtime._run_gated(
            TypeText(text="command\n"), "editor", lambda: pytest.fail("injected input"),
            recheck=lambda app: setattr(field, "role", "terminal"),
        )
    assert caught.value.code is ErrorCode.EXEC_DISABLED
    row, = [json.loads(line) for path in (tmp_path / "audit").glob("*.jsonl")
            for line in path.read_text().splitlines()]
    assert row["metrics"]["security_violation"] == "exec_disabled"


def test_terminal_launch_alias_is_checked_after_resolution(monkeypatch, tmp_path) -> None:
    """A launcher label is checked against the executable it would start."""
    import json
    from a11y_computer_use import safety, server

    runtime = server.Runtime(
        store=safety.PermissionStore(tmp_path / "permissions.json"),
        audit=safety.AuditLog(tmp_path / "audit"), driver=LinuxDriver(),
    )
    monkeypatch.setattr(runtime, "_prepare_launch", lambda name: ("xterm", "Terminal", None))
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(ComputerUseError) as caught:
        runtime.app("launch", "Terminal")
    assert caught.value.code is ErrorCode.EXEC_DISABLED
    row, = [json.loads(line) for path in (tmp_path / "audit").glob("*.jsonl")
            for line in path.read_text().splitlines()]
    assert row["app"] == "xterm" and row["result"] == "exec_disabled"


def test_terminal_observation_remains_available(monkeypatch, tmp_path) -> None:
    """Execution permission does not gate read-only terminal observations."""
    from a11y_computer_use import safety, server
    from a11y_computer_use.schema import AppOp, AppVerb

    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier("xterm", safety.Tier.READ)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "terminal_target", lambda *_args, **_kwargs: pytest.fail("read gate"))
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    assert runtime._run_gated(AppOp(verb=AppVerb.LIST), "xterm", lambda: "observed") == "observed"


@pytest.mark.parametrize("chord", [None, "Return", "ctrl+v", "a"])
def test_granted_terminal_window_requires_explicit_exec(monkeypatch, tmp_path, chord):
    import json
    from a11y_computer_use import safety, server
    from a11y_computer_use.schema import KeyChord, TypeText

    driver = LinuxDriver()
    monkeypatch.setattr("a11y_computer_use.drivers.linux._resolved_app", lambda app: "xfce4-terminal")
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier("Terminal", safety.Tier.FULL)
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    action = TypeText(text="command\n") if chord is None else KeyChord(chord=chord)
    with pytest.raises(ComputerUseError) as caught:
        runtime._run_gated(action, "Terminal", lambda: pytest.fail("input reached terminal"))
    assert caught.value.code is ErrorCode.EXEC_DISABLED
    rows = [json.loads(line) for path in (tmp_path / "audit").glob("*.jsonl")
            for line in path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["metrics"]["security_violation"] == "exec_disabled"
    runtime.allow_exec = True
    assert runtime._run_gated(action, "Terminal", lambda: "injected") == "injected"


def test_terminal_executable_symlink_is_detected(monkeypatch, tmp_path):
    terminal = tmp_path / "xfce4-terminal"
    terminal.touch()
    alias = tmp_path / "console-alias"
    alias.symlink_to(terminal)
    monkeypatch.setattr("shutil.which", lambda app: str(alias))
    assert LinuxDriver().terminal_target("console-alias", launch=True)


@pytest.mark.parametrize("method", ["press_element", "click"])
def test_gnumeric_go_jump_refused_before_activation(fake_atspi, monkeypatch, method):
    app = _Acc("application", name="Gnumeric")
    image = _Acc("image", name="go-jump")
    image.get_application = lambda: app
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: image)
    monkeypatch.setattr("a11y_computer_use.drivers.linux._on_wayland", lambda: False)
    monkeypatch.setattr(_atspi, "do_press", lambda *_args: pytest.fail("dangerous action sent"))
    monkeypatch.setattr(_linux_input, "click", lambda *_args, **_kwargs: pytest.fail("pointer click sent"))
    monkeypatch.setattr(driver, "_raise_handle_window", lambda *_args: pytest.fail("window raised"))
    element = Element("e492", "AXImage", "go-jump", None,
                      Bounds(0, 20, 30, 16, 16), "snap492", clickable=True)

    with pytest.raises(ComputerUseError) as exc:
        getattr(driver, method)(element)

    assert exc.value.code == ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "gnumeric_go_jump_crash"
    assert exc.value.detail["outcome"] == "refused"
    assert "crash" in str(exc.value)


@pytest.mark.parametrize("app_name,role,title", [
    ("Firefox", "AXImage", "go-jump"),
    ("Gnumeric", "AXButton", "go-jump"),
    ("Gnumeric", "AXImage", "other-image"),
])
def test_gnumeric_crash_guard_is_scoped_to_named_image(fake_atspi, monkeypatch, app_name, role, title):
    app = _Acc("application", name=app_name)
    image = _Acc("image", name=title)
    image.get_application = lambda: app
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    element = Element("e492", role, title, None, None, "snap492", clickable=True)
    driver._refuse_gnumeric_go_jump(element, image)
