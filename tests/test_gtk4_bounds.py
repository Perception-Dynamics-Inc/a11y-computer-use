"""GTK 4 reports every SCREEN origin as (0, 0).

The screen point is the X client origin plus the WINDOW box. A missing
origin is not replaced with a guess, and a pointer click is not sent to
that collapsed point. Negative WINDOW boxes outside the reported frame are
also unreliable. Synthetic nodes and fake X windows; no live GTK.
"""

from __future__ import annotations

import builtins
from types import SimpleNamespace as _NS

import pytest

from a11y_computer_use.drivers import _atspi, _linux_system
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, ErrorCode, MouseButton


@pytest.fixture(autouse=True)
def _honest_x_server(monkeypatch):
    """The fake X windows model an honest local server: X-Resource names the
    same owner the client claims in ``_NET_WM_PID`` (#1327)."""
    monkeypatch.setattr(_linux_system, "_xres_pid", lambda win, d: _linux_system._pid_of(win, d) or None)


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


@pytest.mark.parametrize("window", [(227, -5, 34, 34), (-6, 10, 34, 34)])
@pytest.mark.parametrize("action", ["click", "press"])
def test_gtk4_header_bar_click_activates_without_pointer(monkeypatch, window, action):
    clicks = _stub_click(monkeypatch)
    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0, WINDOW=1)))
    _frame, button = _button(_App("GTK", "4.18.6"), window=window)
    button.actions = ("expand", action)
    monkeypatch.setattr("a11y_computer_use.observe.ax_handle_for", lambda *_args: button)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    # Even a positive recovered screen box must not bypass the WINDOW guard.
    assert driver.click(_element(Bounds(0, 524, 275, 34, 34))) is None
    assert button.pressed == [1]
    assert clicks == []


@pytest.mark.parametrize("failure", ["missing", "unrelated", "failed", "right", "double", "modified"])
def test_gtk4_header_bar_unreliable_click_is_refused(monkeypatch, failure):
    clicks = _stub_click(monkeypatch)
    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0, WINDOW=1)))
    _frame, button = _button(_App("GTK", "4.18.6"), window=(227, -5, 34, 34))
    if failure == "missing":
        button.actions = ()
    elif failure == "unrelated":
        button.actions = ("expand",)
    elif failure == "failed":
        monkeypatch.setattr(button, "do_action", lambda _index: False)
    monkeypatch.setattr("a11y_computer_use.observe.ax_handle_for", lambda *_args: button)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    kwargs = {
        "right": {"button": MouseButton.RIGHT},
        "double": {"count": 2},
        "modified": {"modifiers": ("shift",)},
    }.get(failure, {})
    with pytest.raises(ComputerUseError) as exc:
        driver.click(_element(Bounds(0, 1054, -5, 34, 34)), **kwargs)
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["outcome"] == "refused"
    assert button.pressed == []
    assert clicks == []


@pytest.mark.parametrize("toolkit,version", [("GTK", "3.24"), ("Qt", "6.8"), ("GTK", "")])
def test_non_gtk4_negative_window_origin_keeps_pointer_click(monkeypatch, toolkit, version):
    clicks = _stub_click(monkeypatch)
    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0, WINDOW=1)))
    _frame, button = _button(_App(toolkit, version), window=(227, -5, 34, 34))
    monkeypatch.setattr("a11y_computer_use.observe.ax_handle_for", lambda *_args: button)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    driver.click(_element(Bounds(0, 524, 275, 34, 34)))
    assert clicks == [(541, 292)]
    assert button.pressed == []


@pytest.mark.parametrize("role", ["AXTextField", "AXTextArea", "AXSearchField"])
@pytest.mark.parametrize("focused", [True, False, None])
def test_gtk4_text_click_requires_observed_focus(monkeypatch, role, focused) -> None:
    """An entry's no-op activate cannot replace a pointer click or its evidence."""
    clicks = _stub_click(monkeypatch)
    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0, WINDOW=1)))
    _frame, field = _button(_App("GTK", "4.18.6"))
    field.role = "text"
    field.actions = ("activate",)
    monkeypatch.setattr("a11y_computer_use.observe.ax_handle_for", lambda *_args: field)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(driver, "_raise_handle_window", lambda _handle: None)
    monkeypatch.setattr("a11y_computer_use.drivers.linux.time.sleep", lambda _seconds: None)
    def unexpected_focus(_node):
        pytest.fail("an explicit click must not be replaced with grab_focus")

    monkeypatch.setattr(_atspi, "grab_focus", unexpected_focus)
    observations = []

    def focus(node):
        assert node is field
        assert clicks == [(881, 435)]
        observations.append(node)
        # Focus may arrive after the input call returns.
        return focused if len(observations) > 1 else False

    monkeypatch.setattr(_atspi, "_focus_on_target", focus)
    element = _element(Bounds(0, 846, 413, 71, 44), role)
    if focused is True:
        assert driver.click(element) is None
        assert len(observations) == 2
    else:
        with pytest.raises(ComputerUseError) as exc:
            driver.click(element)
        assert exc.value.code is ErrorCode.FOCUS_LOST
        assert exc.value.detail["outcome"] == "unconfirmed"
    assert clicks == [(881, 435)]
    assert field.pressed == []
    assert driver._focused_editable is None


@pytest.mark.parametrize("role", ["AXTextField", "AXTextArea", "AXSearchField"])
def test_gtk4_text_click_refuses_untrusted_geometry_even_with_activate(monkeypatch, role) -> None:
    clicks = _stub_click(monkeypatch)
    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0, WINDOW=1)))
    _frame, field = _button(_App("GTK", "4.18.6"))
    field.actions = ("activate",)
    monkeypatch.setattr("a11y_computer_use.observe.ax_handle_for", lambda *_args: field)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(driver, "_raise_handle_window", lambda _handle: None)
    monkeypatch.setattr(driver, "_gtk4_point_untrusted", lambda *_args: True)
    with pytest.raises(ComputerUseError) as exc:
        driver.click(_element(Bounds(0, 0, 0, 71, 44), role))
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["outcome"] == "refused"
    assert field.pressed == []
    assert clicks == []


@pytest.mark.parametrize("role", ["alert", "dialog"])
@pytest.mark.parametrize("kwargs", [{}, {"button": MouseButton.RIGHT}, {"count": 2}, {"modifiers": ("shift",)}])
def test_gtk4_in_window_dialog_positive_box_refuses_click(monkeypatch, role, kwargs):
    """#520: a recovered positive box can omit the AdwAlertDialog offset."""
    clicks = _stub_click(monkeypatch)
    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0, WINDOW=1)))
    frame, button = _button(_App("GTK", "4.18.6"), window=(377, 372, 156, 44))
    dialog = _Node(role, "End sleep?", (0, 0, 400, 200), (200, 250, 400, 200),
                   parent=frame, app=button.app)
    panel = _Node("panel", "", (0, 0, 400, 100), (200, 350, 400, 100),
                  parent=dialog, app=button.app)
    button.parent = panel
    monkeypatch.setattr("a11y_computer_use.observe.ax_handle_for", lambda *_args: button)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    with pytest.raises(ComputerUseError) as exc:
        driver.click(_element(Bounds(0, 852, 447, 156, 44)), **kwargs)
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "not_actionable"
    assert exc.value.detail["outcome"] == "refused"
    assert clicks == []
    assert button.pressed == []


@pytest.mark.parametrize("toolkit,version,nested", [
    ("GTK", "3.24", True), ("Qt", "6.8", True), ("GTK", "", True),
    ("GTK", "4.18.6", False),
])
def test_other_dialogs_keep_pointer_click(monkeypatch, toolkit, version, nested):
    clicks = _stub_click(monkeypatch)
    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0, WINDOW=1)))
    frame, button = _button(_App(toolkit, version))
    dialog = _Node("dialog", "Confirm", (0, 0, 400, 200), (0, 0, 400, 200),
                   parent=frame if nested else None, app=button.app)
    button.parent = dialog
    monkeypatch.setattr("a11y_computer_use.observe.ax_handle_for", lambda *_args: button)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    driver.click(_element(Bounds(0, 852, 447, 156, 44)))
    assert clicks == [(930, 469)]
    assert button.pressed == []


@pytest.mark.parametrize("role", ["table row", "table cell"])
@pytest.mark.parametrize("origin", [(320, 270), (5, 0)])
def test_baobab_left_edge_row_keeps_recovered_pointer_click(monkeypatch, role, origin):
    """#617: a flush GTK4 body row shares the frame's negative WINDOW x."""
    clicks = _stub_click(monkeypatch)
    monkeypatch.setattr(_linux_system, "gtk4_content_origin", lambda *_args: origin)
    frame, row = _button(_App("GTK", "4.18.6"))
    frame.window = (-5, -5, 960, 616)
    row.role = role
    row.name = ".config"
    row.screen = (0, 0, 474, 35)
    row.window = (-5, 106, 474, 35)
    row.actions = ("listitem.scroll-to",)
    raw = _read(monkeypatch, row)
    assert raw.position == (origin[0] - 5, origin[1] + 106)
    assert raw.size == (474, 35)
    monkeypatch.setattr("a11y_computer_use.observe.ax_handle_for", lambda *_args: row)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(driver, "_raise_handle_window", lambda _handle: None)
    bounds = Bounds(0, *raw.position, *raw.size)
    driver.click(_element(bounds, raw.role))
    assert clicks == [(int(bounds.center.x), int(bounds.center.y))]
    assert row.pressed == []


@pytest.mark.parametrize("frame_box", [
    None, (-5, -5, 0, 616), (-5, -5, 960, 616), (-5, -5, float("inf"), 616),
])
@pytest.mark.parametrize("window", [
    (-6, 106, 474, 35), (227, -6, 34, 34),
    (-5, 106, 961, 35), (-5, 600, 474, 35),
])
def test_negative_gtk4_box_without_frame_containment_stays_untrusted(
    monkeypatch, frame_box, window,
):
    """No tolerance or inferred frame size may bypass the #502 guard."""
    frame, row = _button(_App("GTK", "4.18.6"), window=window)
    frame.window = frame_box or (0, 0, 0, 0)
    if frame_box is None:
        row.parent = None
    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0, WINDOW=1)))
    assert _atspi.gtk4_window_origin_untrusted(row) is True


@pytest.mark.parametrize("role", ["AXTextField", "AXTextArea", "AXSearchField"])
@pytest.mark.parametrize("case", ["focused", "missed", "untrusted"])
def test_gtk4_editable_ref_click_bypasses_noop_focus(monkeypatch, role, case):
    """The default AX-preferred route must reach the verified pointer path."""
    import dataclasses

    clicks = _stub_click(monkeypatch)
    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS(CoordType=_NS(SCREEN=0, WINDOW=1)))
    _frame, field = _button(_App("GTK", "4.18.6"))
    field.role = "text"
    field.actions = ("activate",)
    monkeypatch.setattr("a11y_computer_use.observe.ax_handle_for", lambda *_args: field)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(driver, "_raise_handle_window", lambda _handle: None)
    monkeypatch.setattr("a11y_computer_use.drivers.linux.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(driver, "_gtk4_point_untrusted", lambda *_args: case == "untrusted")

    def unexpected_focus(_node):
        pytest.fail("a ref click must not substitute grab_focus for pointer input")

    monkeypatch.setattr(_atspi, "grab_focus", unexpected_focus)
    monkeypatch.setattr(
        _atspi, "_focus_on_target",
        lambda node: node is field and bool(clicks) and case == "focused",
    )
    element = dataclasses.replace(
        _element(Bounds(0, 846, 413, 71, 44), role), editable=True, clickable=False,
    )
    # Runtime's plain ref-click sequence, including the no-dispatch receipt.
    assert driver.press_element(element) is False
    assert driver._focused_editable is None
    assert field.pressed == []
    assert clicks == []
    target = driver.verify_click_fallback(element, "python3")
    assert target is element
    assert driver._click_without_press is None
    if case == "focused":
        assert driver.click(target) is None
    else:
        with pytest.raises(ComputerUseError) as exc:
            driver.click(target)
        assert exc.value.code is (
            ErrorCode.FOCUS_LOST if case == "missed" else ErrorCode.UNSUPPORTED
        )
        assert exc.value.detail["outcome"] == (
            "unconfirmed" if case == "missed" else "refused"
        )
    assert clicks == ([] if case == "untrusted" else [(881, 435)])
    assert field.pressed == []


# --- #1237: GTK 4 entries need pointer focus -------------------------------

from types import SimpleNamespace as _NS  # noqa: E402


def _focus_env(monkeypatch, *, gtk4=True, role="text", states=("EDITABLE", "SHOWING")):
    from a11y_computer_use.drivers import _atspi
    monkeypatch.setattr(_atspi, "_gtk4_app", lambda acc: gtk4)
    monkeypatch.setattr(_atspi, "_role_name", lambda acc: role)
    monkeypatch.setattr(_atspi, "_state_has", lambda acc, name: name in states)
    return _atspi


@pytest.mark.parametrize("role", ["text", "entry", "password text"])
def test_gtk4_editable_entry_needs_pointer_focus(monkeypatch, role) -> None:
    assert _focus_env(monkeypatch, role=role).gtk4_entry_needs_pointer_focus(object())


@pytest.mark.parametrize("kw", [
    {"gtk4": False},                       # GTK 3 can grab focus over AT-SPI
    {"role": "push button"},
    {"states": ("SHOWING",)},              # not editable
    {"states": ("EDITABLE",)},             # not showing
])
def test_other_nodes_do_not_take_pointer_focus(monkeypatch, kw) -> None:
    assert not _focus_env(monkeypatch, **kw).gtk4_entry_needs_pointer_focus(object())
    assert not _focus_env(monkeypatch).gtk4_entry_needs_pointer_focus(None)


def test_place_caret_at_end_is_true_only_when_read_back(monkeypatch) -> None:
    from a11y_computer_use.drivers import _atspi
    caret = {"at": 3}
    monkeypatch.setattr(_atspi, "_full_text", lambda acc: "keep me")
    monkeypatch.setattr(_atspi, "_atspi", lambda: _NS())
    monkeypatch.setattr(_atspi, "_move_caret", lambda mod, acc, off: caret.update(at=off))
    monkeypatch.setattr(_atspi, "_caret_offset", lambda acc: caret["at"])
    assert _atspi.place_caret_at_end(object()) and caret["at"] == 7
    monkeypatch.setattr(_atspi, "_move_caret", lambda mod, acc, off: None)  # ignored
    caret["at"] = 3
    assert not _atspi.place_caret_at_end(object())
    monkeypatch.setattr(_atspi, "_full_text", lambda acc: None)
    assert not _atspi.place_caret_at_end(object())
