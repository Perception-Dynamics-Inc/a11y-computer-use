"""Linux backend — synthetic coverage that runs on ANY OS (no AT-SPI bus).

The live AT-SPI walk needs a real accessibility bus (see test_linux_live.py, CI
only). Here we pin the platform-free parts of the Linux backend that don't need
gi/Atspi: the AT-SPI->AX role vocabulary, that vocabulary flowing through the
SHARED pruning engine exactly as macOS/Windows do, and the chord parser/keysym
map. These catch mapping regressions on every developer's machine.
"""

from __future__ import annotations

from types import SimpleNamespace as _NS

import pytest

from computeruse.drivers import _atspi, _linux_input
from computeruse.observe import DisplayGeometry, RawNode, build_snapshot
from computeruse.schema import Display, Scope


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
    from computeruse.drivers import _atspi
    monkeypatch.setattr(_atspi, "_a11y_status_forced", False)
    monkeypatch.setenv("COMPUTERUSE_NO_WEB_A11Y", "1")
    assert _atspi.enable_a11y_status() is False  # opt-out short-circuits before any D-Bus


def test_atspi_events_gate(monkeypatch) -> None:
    from computeruse.drivers import _atspi_events
    monkeypatch.delenv("COMPUTERUSE_ATSPI_EVENTS", raising=False)
    assert _atspi_events.enabled() is False
    monkeypatch.setenv("COMPUTERUSE_ATSPI_EVENTS", "1")
    assert _atspi_events.enabled() is True


def test_linux_driver_run_inline_when_events_disabled(monkeypatch) -> None:
    from computeruse.drivers import _atspi_events  # noqa: F401
    from computeruse.drivers.linux import LinuxDriver
    monkeypatch.delenv("COMPUTERUSE_ATSPI_EVENTS", raising=False)
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


def test_drag_moves_absolutely_at_both_endpoints(xtest_recorder) -> None:
    events, display = xtest_recorder
    _linux_input.drag(10, 20, 300, 400)
    assert events == [
        (_X_MOTION, 0, 10, 20), (_X_BPRESS, 1, 0, 0),
        (_X_MOTION, 0, 300, 400), (_X_BRELEASE, 1, 0, 0),
    ]
    assert display.warps == []


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
