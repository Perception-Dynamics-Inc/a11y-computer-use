"""GTK 4 reports every SCREEN origin as (0, 0). WINDOW coordinates are real.

The screen point is the X client origin plus the WINDOW box. A missing
origin is not replaced with a guess, and a pointer click is not sent to
that collapsed point. Synthetic nodes and fake X windows; no live GTK.
"""

from __future__ import annotations

import builtins
from types import SimpleNamespace as _NS

import pytest

from a11y_computer_use.drivers import _atspi, _linux_system
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, ErrorCode, MouseButton


class _App:
    def __init__(self, toolkit: str, version: str, pid: int = 77):
        self.toolkit = toolkit
        self.version = version
        self.pid = pid

    def get_toolkit_name(self):
        return self.toolkit

    def get_toolkit_version(self):
        return self.version

    def get_process_id(self):
        return self.pid


class _Node:
    def __init__(self, role, name, screen, window, parent=None, app=None, actions=()):
        self.role = role
        self.name = name
        self.screen = screen
        self.window = window
        self.parent = parent
        self.app = app
        self.actions = tuple(actions)
        self.pressed: list[int] = []

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_description(self):
        return ""

    def get_parent(self):
        return self.parent

    def get_application(self):
        return self.app

    def get_process_id(self):
        return self.app.pid if self.app is not None else 0

    def get_state_set(self):
        return _NS(names=set(), contains=lambda _member: False)

    def get_attributes(self):
        return {}

    def get_component_iface(self):
        return self

    def get_extents(self, coord):
        raw = self.screen if int(coord) == 0 else self.window
        return _NS(x=raw[0], y=raw[1], width=raw[2], height=raw[3])

    def get_action_iface(self):
        return self if self.actions else None

    def get_n_actions(self):
        return len(self.actions)

    def get_action_name(self, index):
        return self.actions[int(index)]

    def do_action(self, index):
        self.pressed.append(int(index))
        return True


def _button(app, screen=(0, 0, 71, 44), window=(19, 413, 71, 44)):
    frame = _Node("frame", "Calculator", (0, 0, 403, 616), (0, 0, 403, 616), app=app)
    button = _Node("push button", "7", screen, window, parent=frame, app=app, actions=("click",))
    return frame, button


def _read(monkeypatch, node):
    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0, WINDOW=1)))
    return _atspi.ATSPIAccessor().read(node)


def test_gtk4_screen_origin_uses_the_client_window_plus_window_extents(monkeypatch) -> None:
    """Button 7 is not at the top-left. 827 + 19, 0 + 413 is the screen point."""
    seen: list[tuple] = []

    def origin(pid, width=None, height=None):
        seen.append((pid, width, height))
        return (827, 0)

    monkeypatch.setattr(_linux_system, "gtk4_content_origin", origin)
    _frame, button = _button(_App("GTK", "4.18.6"))
    raw = _read(monkeypatch, button)
    assert raw.role == "AXButton"
    assert raw.position == (846.0, 413.0)
    assert raw.size == (71.0, 44.0)
    assert seen == [(77, 403, 616)]


def test_gtk4_without_an_x_origin_keeps_the_reported_screen_point(monkeypatch) -> None:
    """A missing window origin is not filled in."""
    monkeypatch.setattr(_linux_system, "gtk4_content_origin", lambda *_args, **_kwargs: None)
    _frame, button = _button(_App("GTK", "4.18.6"))
    raw = _read(monkeypatch, button)
    assert raw.position == (0.0, 0.0)
    assert raw.size == (71.0, 44.0)


def test_gtk3_and_a_missing_version_are_not_shifted(monkeypatch) -> None:
    """Only a reported GTK 4 (or newer) toolkit gets the WINDOW shift."""
    monkeypatch.setattr(_linux_system, "gtk4_content_origin", lambda *_args, **_kwargs: (827, 0))
    for app in (_App("gtk", "3.24.41"), _App("GTK", ""), _App("Qt", "6.4.2")):
        _frame, button = _button(app)
        raw = _read(monkeypatch, button)
        assert raw.position == (0.0, 0.0), app.toolkit


def test_a_window_that_really_sits_at_the_origin_is_not_moved(monkeypatch) -> None:
    """Origin plus a WINDOW box of (0, 0) is the SCREEN point already."""
    monkeypatch.setattr(_linux_system, "gtk4_content_origin", lambda *_args, **_kwargs: (0, 0))
    _frame, button = _button(_App("GTK", "4.18.6"), screen=(0, 0, 71, 44), window=(0, 0, 71, 44))
    raw = _read(monkeypatch, button)
    assert raw.position == (0.0, 0.0)
    assert raw.size == (71.0, 44.0)


def test_two_gtk4_windows_use_the_one_whose_size_matches(monkeypatch) -> None:
    """Same pid, two origins. The frame size picks one. A tie picks neither."""

    class _Win:
        def __init__(self, wid, x, y, w, h, pid, hidden=False):
            self.id, self.x, self.y, self.w, self.h, self.pid = wid, x, y, w, h, pid
            self.hidden = hidden

        def get_geometry(self):
            return _NS(x=0, y=0, width=self.w, height=self.h)

        def get_full_property(self, atom, kind):
            if atom == "_NET_WM_PID":
                return _NS(value=[self.pid])
            if atom == "WM_STATE" and self.hidden:
                return _NS(value=[3, 0])
            return None

    class _Root:
        def __init__(self, stacking):
            self.stacking = stacking

        def translate_coords(self, src, sx, sy):
            return _NS(x=src.x + sx, y=src.y + sy)

        def get_full_property(self, atom, kind):
            if atom == "_NET_CLIENT_LIST_STACKING":
                return _NS(value=[win.id for win in self.stacking])
            return None

    calc = _Win(1, 827, 0, 403, 616, pid=77)
    other = _Win(2, 40, 40, 200, 200, pid=77)
    hidden = _Win(3, 1, 1, 403, 616, pid=77, hidden=True)
    root = _Root([other, hidden, calc])
    by_id = {win.id: win for win in (calc, other, hidden)}
    display = _NS(
        screen=lambda: _NS(root=root),
        intern_atom=lambda name: name,
        create_resource_object=lambda kind, wid: by_id[int(wid)],
    )
    monkeypatch.setattr(_linux_system, "_display", lambda: display)
    assert _linux_system.client_origin_for_pid(77, 403, 616) == (827, 0)
    assert _linux_system.client_origin_for_pid(77, 200, 200) == (40, 40)
    assert _linux_system.client_origin_for_pid(77, 10, 10) is None
    assert _linux_system.client_origin_for_pid(77) is None
    alone = _Root([calc])
    monkeypatch.setattr(
        _linux_system, "_display",
        lambda: _NS(
            screen=lambda: _NS(root=alone),
            intern_atom=lambda name: name,
            create_resource_object=lambda kind, wid: by_id[int(wid)],
        ),
    )
    assert _linux_system.client_origin_for_pid(77, 1, 1) == (827, 0)


def test_gtk4_content_origin_adds_the_input_shape_margin(monkeypatch) -> None:
    """A shadow around the X window is not part of the GTK frame.

    The input shape is the surface that receives clicks. A few pixels of
    that shape outside the AT-SPI frame are the border, split on each side.
    """

    class _Win:
        def __init__(self, wid, x, y, w, h, pid):
            self.id, self.x, self.y, self.w, self.h, self.pid = wid, x, y, w, h, pid

        def get_geometry(self):
            return _NS(x=0, y=0, width=self.w, height=self.h)

        def get_full_property(self, atom, kind):
            if atom == "_NET_WM_PID":
                return _NS(value=[self.pid])
            return None

        def shape_get_rectangles(self, _kind):
            return _NS(rectangles=[_NS(x=49, y=43, width=384, height=515)])

    class _Root:
        def __init__(self, stacking):
            self.stacking = stacking

        def translate_coords(self, src, sx, sy):
            return _NS(x=src.x + sx, y=src.y + sy)

        def get_full_property(self, atom, kind):
            if atom == "_NET_CLIENT_LIST_STACKING":
                return _NS(value=[win.id for win in self.stacking])
            return None

    win = _Win(1, 200, 100, 482, 613, pid=77)
    root = _Root([win])
    monkeypatch.setattr(
        _linux_system, "_display",
        lambda: _NS(
            screen=lambda: _NS(root=root),
            intern_atom=lambda name: name,
            has_extension=lambda name: name == "SHAPE",
            create_resource_object=lambda kind, wid: win,
        ),
    )
    assert _linux_system.client_origin_for_pid(77, 360, 491) == (200, 100)
    assert _linux_system.gtk4_content_origin(77, 360, 491) == (261, 155)
    # A large gap is not split in half. The input offset is still real.
    win.shape_get_rectangles = lambda _kind: _NS(rectangles=[_NS(x=10, y=20, width=800, height=900)])
    assert _linux_system.gtk4_content_origin(77, 360, 491) == (210, 120)


def test_a_minimized_window_is_not_the_window_under_a_point(monkeypatch) -> None:
    """The iconified terminal keeps its old rectangle and does not cover it."""

    class _Win:
        def __init__(self, wid, x, y, w, h, pid, hidden=False):
            self.id, self.x, self.y, self.w, self.h, self.pid = wid, x, y, w, h, pid
            self.hidden = hidden

        def get_geometry(self):
            return _NS(x=0, y=0, width=self.w, height=self.h)

        def get_full_property(self, atom, kind):
            if atom == "_NET_WM_PID":
                return _NS(value=[self.pid])
            if self.hidden and atom == "_NET_WM_STATE":
                return _NS(value=["_NET_WM_STATE_HIDDEN"])
            return None

    class _Root:
        def __init__(self, stacking):
            self.stacking = stacking

        def translate_coords(self, src, sx, sy):
            return _NS(x=src.x + sx, y=src.y + sy)

        def get_full_property(self, atom, kind):
            if atom == "_NET_CLIENT_LIST_STACKING":
                return _NS(value=[win.id for win in self.stacking])
            return None

    editor = _Win(1, 100, 80, 640, 480, pid=41)
    terminal = _Win(2, 100, 80, 640, 480, pid=42, hidden=True)
    root = _Root([editor, terminal])  # terminal is stacked above, and minimized
    by_id = {win.id: win for win in (editor, terminal)}
    monkeypatch.setattr(
        _linux_system, "_display",
        lambda: _NS(
            screen=lambda: _NS(root=root),
            intern_atom=lambda name: name,
            create_resource_object=lambda kind, wid: by_id[int(wid)],
        ),
    )
    assert _linux_system.pid_at_point(200, 200) == 41


def _element(bounds: Bounds, role: str = "AXButton") -> Element:
    return Element("e12", role, "7", None, bounds, "snap-gtk4", clickable=True)


def _stub_click(monkeypatch) -> list[tuple[int, int]]:
    """Record the pointer click. ``held`` stays real and must not open X."""
    clicks: list[tuple[int, int]] = []

    def click(x, y, *_args, **_kwargs):
        clicks.append((int(x), int(y)))

    monkeypatch.setattr("a11y_computer_use.drivers._linux_input.click", click)
    return clicks


def test_plain_hold_does_not_import_xlib(monkeypatch) -> None:
    """No modifiers: held does not import Xlib. Not a live display."""
    from a11y_computer_use.drivers import _linux_input

    real = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "Xlib" or str(name).startswith("Xlib."):
            raise ModuleNotFoundError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with _linux_input.held(()):
        pass
    with _linux_input.held(None):
        pass


def test_unrecovered_gtk4_click_uses_the_action_and_not_the_origin(monkeypatch) -> None:
    """No X origin: do_action runs, and the pointer is not moved to (0, 0)."""
    clicks = _stub_click(monkeypatch)
    monkeypatch.setattr(_linux_system, "gtk4_content_origin", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0, WINDOW=1)))
    _frame, button = _button(_App("GTK", "4.18.6"))
    monkeypatch.setattr(
        "a11y_computer_use.observe.ax_handle_for", lambda *_args, **_kwargs: button,
    )
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    assert driver.click(_element(Bounds(0, 0, 0, 71, 44))) is None
    assert button.pressed == [0]
    assert clicks == []


def test_unrecovered_gtk4_without_an_action_is_not_a_successful_click(monkeypatch) -> None:
    """A collapsed box with no action is not_actionable. Nothing is clicked."""
    clicks = _stub_click(monkeypatch)
    monkeypatch.setattr(_linux_system, "gtk4_content_origin", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0, WINDOW=1)))
    _frame, button = _button(_App("GTK", "4.18.6"))
    button.actions = ()
    monkeypatch.setattr(
        "a11y_computer_use.observe.ax_handle_for", lambda *_args, **_kwargs: button,
    )
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    with pytest.raises(ComputerUseError) as exc:
        driver.click(_element(Bounds(0, 0, 0, 71, 44)))
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "not_actionable"
    assert exc.value.detail["outcome"] == "refused"
    assert clicks == []
    assert button.pressed == []


def test_recovered_gtk4_click_uses_the_real_center(monkeypatch) -> None:
    """A box that was shifted off the origin is a normal pointer click."""
    clicks = _stub_click(monkeypatch)
    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0, WINDOW=1)))
    _frame, button = _button(_App("GTK", "4.18.6"))
    monkeypatch.setattr(
        "a11y_computer_use.observe.ax_handle_for", lambda *_args, **_kwargs: button,
    )
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    assert driver.click(_element(Bounds(0, 846, 413, 71, 44))) is None
    assert clicks == [(881, 435)]
    assert button.pressed == []


def test_a_real_origin_window_still_receives_a_pointer_click(monkeypatch) -> None:
    """GTK 4 at screen (0, 0) because the client window is there is a real point."""
    clicks = _stub_click(monkeypatch)
    monkeypatch.setattr(_linux_system, "gtk4_content_origin", lambda *_args, **_kwargs: (0, 0))
    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0, WINDOW=1)))
    _frame, button = _button(_App("GTK", "4.18.6"), screen=(0, 0, 71, 44), window=(0, 0, 71, 44))
    monkeypatch.setattr(
        "a11y_computer_use.observe.ax_handle_for", lambda *_args, **_kwargs: button,
    )
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    assert driver.click(_element(Bounds(0, 0, 0, 71, 44))) is None
    assert clicks == [(35, 22)]
    assert button.pressed == []


def test_degenerate_gtk4_box_is_not_clicked(monkeypatch) -> None:
    """All-zero and a zero side are unknown. The pointer stays put."""
    clicks = _stub_click(monkeypatch)
    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0, WINDOW=1)))
    _frame, button = _button(_App("GTK", "4.18.6"), screen=(0, 0, 0, 0), window=(0, 0, 0, 0))
    button.actions = ()
    monkeypatch.setattr(
        "a11y_computer_use.observe.ax_handle_for", lambda *_args, **_kwargs: button,
    )
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    with pytest.raises(ComputerUseError) as exc:
        driver.click(_element(Bounds(0, 0, 0, 0, 0)))
    assert exc.value.detail["reason"] == "not_actionable"
    assert clicks == []


def test_collapsed_gtk4_bounds_are_not_reported_covered(monkeypatch) -> None:
    """The top-left corner is not the text field, so it is not a cover.

    A recovered box still hit-tests. A minimized window is not that hit.
    """
    monkeypatch.setattr(_linux_system, "gtk4_content_origin", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0, WINDOW=1)))
    _frame, field = _button(_App("GTK", "4.18.6"))
    field.role = "text"
    field.name = ""
    probed: list[tuple[float, float]] = []

    def at_point(x, y):
        probed.append((x, y))
        return 99

    monkeypatch.setattr(_linux_system, "pid_at_point", at_point)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "xfce4-terminal")
    monkeypatch.setattr(_atspi, "find_root", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(_atspi, "pid_of", lambda root: 77 if root is not field else 77)
    monkeypatch.setattr(
        "a11y_computer_use.observe.ax_handle_for", lambda *_args, **_kwargs: field,
    )
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    assert driver.occlusion(_element(Bounds(0, 0, 0, 400, 300), "AXTextArea"), "gnome-text-editor") is None
    assert probed == []

    monkeypatch.setattr(_linux_system, "gtk4_content_origin", lambda *_args, **_kwargs: (200, 80))
    assert driver.occlusion(_element(Bounds(0, 220, 180, 400, 300), "AXTextArea"), "gnome-text-editor") == "xfce4-terminal"
    assert probed == [(420.0, 330.0)]


def test_right_click_on_a_collapsed_box_is_refused(monkeypatch) -> None:
    """A non-primary click cannot fall through to the origin either."""
    clicks = _stub_click(monkeypatch)
    monkeypatch.setattr(_linux_system, "gtk4_content_origin", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0, WINDOW=1)))
    _frame, button = _button(_App("GTK", "4.18.6"))
    monkeypatch.setattr(
        "a11y_computer_use.observe.ax_handle_for", lambda *_args, **_kwargs: button,
    )
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    with pytest.raises(ComputerUseError) as exc:
        driver.click(_element(Bounds(0, 0, 0, 71, 44)), button=MouseButton.RIGHT)
    assert exc.value.detail["reason"] == "not_actionable"
    assert clicks == []
    assert button.pressed == []
