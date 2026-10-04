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
    _atspi.reset_shown_rows()
    monkeypatch.setattr(_atspi, "_atspi", lambda: _FakeAtspi)
    yield _FakeAtspi
    _FakeAtspi.desktop = None
    _atspi.reset_shown_rows()


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


def test_lines_scroll_is_one_wheel_notch_per_unit(xtest_recorder, monkeypatch) -> None:
    events, _display = xtest_recorder

    def grab(_box):
        raise AssertionError("a coordinate line scroll has no list to capture")

    monkeypatch.setattr("a11y_computer_use.drivers.linux._grab_region", grab)
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
    the driver raises page_unchanged and does not install a new head. The
    search then steps back one line at a time (four rows per step here)
    until the window contains ITEM-180. This does not prove the live Chrome list.
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
    assert "found after 10 scroll(s)" in out
    assert "found after 0 scroll" not in out
    assert hit.steps == [5, 5, 5, 5, 5, 5, -1, -1, -1, -1]
    assert -5 not in hit.steps
    titles = _row_titles(driver.snapshot(Scope.WINDOW, "chrome"))
    assert "ITEM-180" in titles
    assert titles[0] == "ITEM-177"


def test_scroll_to_find_stops_when_the_chrome_list_will_not_move(
    fake_atspi, xtest_recorder, monkeypatch
) -> None:
    """Synthetic Chromium list, not a live Chrome window.

    The window is ITEM-193 through ITEM-200. ITEM-180 is not in it. Neither
    direction changes the grab, so both wheels are page_unchanged. The
    search stops on the second one and the snapshot head stays ITEM-193.
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
    assert error.value.detail["dy"] == -1
    assert hit.steps == [5, -1]
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


def test_pixel_scroll_dry_run_does_not_write_or_move(fake_atspi, xtest_recorder) -> None:
    events, _display = xtest_recorder
    text, vertical, _horizontal = _scrolled_text()
    driver = LinuxDriver()
    driver._focused_editable = text
    assert driver.scroll(_body(), dy=3, unit=ScrollUnit.PIXELS, dry_run=True) is None
    assert driver._focused_editable is text
    assert vertical.value == 100
    assert events == []
