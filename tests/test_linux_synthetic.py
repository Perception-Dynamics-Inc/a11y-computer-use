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
)


def test_role_map_covers_common_atspi_roles() -> None:
    r = _atspi._ROLE
    assert r["push button"] == "AXButton"
    assert r["entry"] == "AXTextField"
    assert r["password text"] == "AXSecureTextField"
    assert r["link"] == "AXLink"
    assert r["check box"] == "AXCheckBox"
    assert r["radio button"] == "AXRadioButton"
    assert r["frame"] == "AXWindow"
    assert r["menu item"] == "AXMenuItem"
    assert r["separator"] == "AXSplitter"  # decorative -> dropped by the engine
    assert r["terminal"] == "AXTextArea"  # VTE: the screen text is its Text iface, else the tab is empty


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
          children=()):
    return (
        RawNode(role=role, title=title, value=value, actions=actions, position=pos, size=size),
        list(children),
    )


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
    assert _atspi._extents(_Comp(_Rect(5, 6, 70, 30))) == ((5.0, 6.0), (70.0, 30.0))


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
    from a11y_computer_use.drivers import _atspi_events
    monkeypatch.delenv("A11Y_COMPUTER_USE_ATSPI_EVENTS", raising=False)
    assert _atspi_events.enabled() is False
    monkeypatch.setenv("A11Y_COMPUTER_USE_ATSPI_EVENTS", "1")
    assert _atspi_events.enabled() is True


def test_linux_driver_run_inline_when_events_disabled(monkeypatch) -> None:
    from a11y_computer_use.drivers import _atspi_events  # noqa: F401
    from a11y_computer_use.drivers.linux import LinuxDriver
    monkeypatch.delenv("A11Y_COMPUTER_USE_ATSPI_EVENTS", raising=False)
    d = LinuxDriver()
    assert d._run(lambda: 42) == 42  # default path runs inline, no thread/gi needed


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
    def __init__(self, width, height):
        self.x = 0.0
        self.y = 0.0
        self.width = width
        self.height = height


class _Geom:
    def __init__(self, width, height):
        self._rect = _Rect(width, height)

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

    class Text:
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
    monkeypatch.setattr(_atspi, "_atspi", lambda: _FakeAtspi)
    yield _FakeAtspi
    _FakeAtspi.desktop = None


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


def test_lines_scroll_is_one_wheel_notch_per_unit(xtest_recorder) -> None:
    events, _display = xtest_recorder
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
    with pytest.raises(ComputerUseError) as error:
        LinuxDriver().scroll(_body(), dy=3, unit=ScrollUnit.PIXELS)
    assert error.value.code is ErrorCode.UNSUPPORTED
    assert "wheel notches were not sent" in error.value.message
    assert error.value.detail["unit"] == "pixels"
    assert events == []


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


def test_line_scroll_of_an_unchanged_list_is_not_success(fake_atspi, xtest_recorder, monkeypatch) -> None:
    events, _display = xtest_recorder
    rows = [_Acc("list item", name=f"ITEM-{i:03d}") for i in range(1, 9)]
    _adopt(_Acc("list"), *rows)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: rows[0])
    monkeypatch.setattr("a11y_computer_use.drivers.linux.time.sleep", lambda _seconds: None)
    element = Element("e1", "AXRow", "ITEM-001", None, Bounds(0, 10, 20, 100, 18), "snap-1")
    with pytest.raises(ComputerUseError) as error:
        LinuxDriver().scroll(element, dy=1, unit=ScrollUnit.LINES)
    assert error.value.code is ErrorCode.UNSUPPORTED
    assert error.value.detail["reason"] == "tree_unchanged"
    assert "notches were not sent" not in error.value.message
    assert events[0] == (_X_MOTION, 0, 60, 29)
    assert events[1:3] == [(_X_BPRESS, 5, 0, 0), (_X_BRELEASE, 5, 0, 0)]


def test_line_scroll_succeeds_when_the_list_names_change(fake_atspi, xtest_recorder, monkeypatch) -> None:
    events, _display = xtest_recorder
    rows = [_Acc("list item", name=f"ITEM-{i:03d}") for i in range(1, 9)]
    _adopt(_Acc("list"), *rows)
    real = _linux_input.scroll

    def scrolling(x, y, *, dx=0, dy=0):
        real(x, y, dx=dx, dy=dy)
        for index, row in enumerate(rows):
            row.name = f"ITEM-{index + 6:03d}"

    monkeypatch.setattr(_linux_input, "scroll", scrolling)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: rows[0])
    element = Element("e1", "AXRow", "ITEM-001", None, Bounds(0, 10, 20, 100, 18), "snap-1")
    assert LinuxDriver().scroll(element, dy=1, unit=ScrollUnit.LINES) is None
    assert rows[0].name == "ITEM-006"
    assert events[1:3] == [(_X_BPRESS, 5, 0, 0), (_X_BRELEASE, 5, 0, 0)]


def test_line_scroll_of_a_text_area_does_not_require_a_list(fake_atspi, xtest_recorder, monkeypatch) -> None:
    events, _display = xtest_recorder
    text = _Acc("text", name="Body")
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: text)
    assert LinuxDriver().scroll(_body(), dy=1, unit=ScrollUnit.LINES) is None
    assert events[1:3] == [(_X_BPRESS, 5, 0, 0), (_X_BRELEASE, 5, 0, 0)]


class _CachedRows(_Acc):
    """Child names stay put until ``clear_cache`` publishes ``pending``.

    Fake-transport stand-in for an AT-SPI child cache filled by the pre-wheel
    read. ``lag`` is how many ``clear_cache`` calls after ``queue`` still
    return the old names. ``lag`` 1 publishes on the next clear.
    """

    def __init__(self, names: list[str]):
        super().__init__("list")
        self.pending: list[str] | None = None
        self.lag = 0
        self.clears = 0
        self._show(names)

    def _show(self, names: list[str]) -> None:
        _adopt(self, *[_Acc("list item", name=name) for name in names])

    def queue(self, names: list[str], *, lag: int = 1) -> None:
        self.pending = list(names)
        self.lag = lag

    def clear_cache(self) -> None:
        self.clears += 1
        if self.pending is None:
            return
        self.lag -= 1
        if self.lag <= 0:
            self._show(self.pending)
            self.pending = None

    def names(self) -> list[str]:
        return [child.name for child in self.children]


def _row_element() -> Element:
    return Element("e1", "AXRow", "ITEM-001", None, Bounds(0, 10, 20, 100, 18), "snap-1")


def test_line_scroll_accepts_a_move_hidden_by_the_child_cache(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Fake transport only. The wheel updates a pending list; names change after clear_cache."""
    events, _display = xtest_recorder
    listing = _CachedRows([f"ITEM-{i:03d}" for i in range(1, 9)])
    anchor = listing.children[0]
    real = _linux_input.scroll

    def scrolling(x, y, *, dx=0, dy=0):
        real(x, y, dx=dx, dy=dy)
        listing.queue([f"ITEM-{i:03d}" for i in range(12, 20)])

    monkeypatch.setattr(_linux_input, "scroll", scrolling)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: anchor)
    assert LinuxDriver().scroll(_row_element(), dy=5, unit=ScrollUnit.LINES) is None
    assert listing.names()[0] == "ITEM-012"
    assert listing.clears >= 2  # the pre-wheel read and the post-wheel read
    assert events[1:3] == [(_X_BPRESS, 5, 0, 0), (_X_BRELEASE, 5, 0, 0)]


def test_line_scroll_waits_for_a_list_that_publishes_on_the_next_read(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Fake transport only. The first refreshed read is still the old rows."""
    listing = _CachedRows([f"ITEM-{i:03d}" for i in range(3, 11)])
    anchor = listing.children[0]
    real = _linux_input.scroll
    sleeps: list[float] = []

    def scrolling(x, y, *, dx=0, dy=0):
        real(x, y, dx=dx, dy=dy)
        listing.queue([f"ITEM-{i:03d}" for i in range(12, 20)], lag=2)

    monkeypatch.setattr(_linux_input, "scroll", scrolling)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: anchor)
    monkeypatch.setattr("a11y_computer_use.drivers.linux.time.sleep", sleeps.append)
    assert LinuxDriver().scroll(_row_element(), dy=5, unit=ScrollUnit.LINES) is None
    assert listing.names()[0] == "ITEM-012"
    assert sleeps  # the first post-wheel read had not published yet


def test_line_scroll_of_a_cached_list_that_does_not_move_is_unsupported(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    listing = _CachedRows([f"ITEM-{i:03d}" for i in range(3, 11)])
    anchor = listing.children[0]
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: anchor)
    monkeypatch.setattr("a11y_computer_use.drivers.linux.time.sleep", lambda _seconds: None)
    with pytest.raises(ComputerUseError) as error:
        LinuxDriver().scroll(_row_element(), dy=5, unit=ScrollUnit.LINES)
    assert error.value.detail["reason"] == "tree_unchanged"
    assert listing.names()[0] == "ITEM-003"
    assert listing.clears >= 2


def test_line_scroll_sees_nested_row_labels(fake_atspi, xtest_recorder, monkeypatch) -> None:
    """A list whose direct children are unnamed groups. Fake transport only."""
    listing = _Acc("list")
    labels: list[_Acc] = []
    groups = []
    for index in range(1, 9):
        label = _Acc("label", name=f"ITEM-{index:03d}")
        group = _Acc("panel")
        _adopt(group, label)
        labels.append(label)
        groups.append(group)
    _adopt(listing, *groups)
    real = _linux_input.scroll

    def scrolling(x, y, *, dx=0, dy=0):
        real(x, y, dx=dx, dy=dy)
        for offset, label in enumerate(labels):
            label.name = f"ITEM-{offset + 12:03d}"

    monkeypatch.setattr(_linux_input, "scroll", scrolling)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: labels[0])
    assert LinuxDriver().scroll(_row_element(), dy=5, unit=ScrollUnit.LINES) is None
    assert labels[0].name == "ITEM-012"


def test_unchanged_nested_row_labels_are_tree_unchanged(fake_atspi, xtest_recorder, monkeypatch) -> None:
    listing = _Acc("list")
    labels = []
    groups = []
    for index in range(1, 9):
        label = _Acc("label", name=f"ITEM-{index:03d}")
        group = _Acc("panel")
        _adopt(group, label)
        labels.append(label)
        groups.append(group)
    _adopt(listing, *groups)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: labels[0])
    monkeypatch.setattr("a11y_computer_use.drivers.linux.time.sleep", lambda _seconds: None)
    with pytest.raises(ComputerUseError) as error:
        LinuxDriver().scroll(_row_element(), dy=5, unit=ScrollUnit.LINES)
    assert error.value.detail["reason"] == "tree_unchanged"
    assert labels[0].name == "ITEM-001"


def test_line_scrolls_reach_a_later_row_when_each_wheel_moves_the_cached_list(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Fake transport stand-in for scroll_to_find: each accepted wheel advances the window.

    Not the Runtime tool, and not a live Chrome list. The driver must not raise
    tree_unchanged on a wheel whose rows change once the child cache is cleared,
    or the find loop stops on the first notch.
    """
    window = [f"ITEM-{i:03d}" for i in range(1, 11)]
    listing = _CachedRows(window)
    anchor = listing.children[0]
    real = _linux_input.scroll
    start = {"n": 1}

    def scrolling(x, y, *, dx=0, dy=0):
        real(x, y, dx=dx, dy=dy)
        start["n"] += 10
        listing.queue([f"ITEM-{i:03d}" for i in range(start["n"], start["n"] + 10)])

    monkeypatch.setattr(_linux_input, "scroll", scrolling)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: anchor)
    visible: list[str] = []
    for _ in range(25):
        LinuxDriver().scroll(_row_element(), dy=5, unit=ScrollUnit.LINES)
        visible = listing.names()
        if "ITEM-180" in visible:
            break
    assert "ITEM-180" in visible


def test_pixel_scroll_dry_run_does_not_write_or_move(fake_atspi, xtest_recorder) -> None:
    events, _display = xtest_recorder
    text, vertical, _horizontal = _scrolled_text()
    driver = LinuxDriver()
    driver._focused_editable = text
    assert driver.scroll(_body(), dy=3, unit=ScrollUnit.PIXELS, dry_run=True) is None
    assert driver._focused_editable is text
    assert vertical.value == 100
    assert events == []
