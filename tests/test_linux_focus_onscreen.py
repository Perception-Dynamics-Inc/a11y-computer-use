"""Focus and capture skip a Linux window that misses the display.

Synthetic X and a fake Runtime. A mapped window at x=2000 is still listed
(``on_screen`` is only "not minimized"), and the stacking list is bottom to
top. Focus must take the topmost window that meets the screen, and
``screen_text`` must not OCR a window that sits past a known display.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from a11y_computer_use import ocr, safety, server
from a11y_computer_use.drivers import _linux_system
from a11y_computer_use.schema import ComputerUseError, Display, ErrorCode
from tests.test_linux_system_synthetic import _FakeXRoot, _FakeXWin

SCREEN = Display(display_id=0, width=1920, height=1080, scale=1.0, is_main=True)


def _display(stacking, *, width: int | None = None, height: int | None = None):
    root = _FakeXRoot(stacking)
    screen = _NS_screen(root, width, height)
    by_id = {win.id: win for win in stacking}
    return _Simple(
        screen=lambda: screen,
        intern_atom=lambda name: name,
        create_resource_object=lambda kind, wid: by_id[int(wid)],
    )


class _Simple:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _NS_screen(root, width, height):
    screen = _Simple(root=root)
    if width is not None and height is not None:
        screen.width_in_pixels = width
        screen.height_in_pixels = height
    return screen


def _chrome_stack(monkeypatch, windows, *, width=1920, height=1080):
    display = _display(windows, width=width, height=height)
    activated: list[int] = []
    monkeypatch.setattr(_linux_system, "_display", lambda: display)
    monkeypatch.setattr(
        _linux_system, "_comm_for_pid", lambda pid: "chrome" if pid == 7 else None,
    )
    monkeypatch.setattr(
        _linux_system, "_send_active_window", lambda _d, win: activated.append(int(win.id)),
    )
    return activated


def test_activate_prefers_the_topmost_on_screen_window(monkeypatch) -> None:
    """The back window is off the display. The front one is on it.

    Stacking is bottom to top, so the old walk stopped on the off-screen
    window and reported focus. The request must name the on-screen id.
    """
    back = _FakeXWin(1, 2000, 40, 800, 600, pid=7)
    front = _FakeXWin(2, 40, 40, 800, 600, pid=7)
    activated = _chrome_stack(monkeypatch, [back, front])
    assert _linux_system.activate_app("chrome") == "chrome"
    assert activated == [2]


def test_activate_skips_an_off_screen_window_stacked_above(monkeypatch) -> None:
    """A later window past the right edge does not replace the visible one."""
    visible = _FakeXWin(1, 40, 40, 800, 600, pid=7)
    above = _FakeXWin(2, 2000, 40, 800, 600, pid=7)
    activated = _chrome_stack(monkeypatch, [visible, above])
    assert _linux_system.activate_app("chrome") == "chrome"
    assert activated == [1]


def test_activate_of_only_off_screen_windows_sends_nothing(monkeypatch) -> None:
    off = _FakeXWin(3, 2000, 40, 800, 600, pid=7)
    activated = _chrome_stack(monkeypatch, [off])
    with pytest.raises(ComputerUseError) as exc:
        _linux_system.activate_app("chrome")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "window_off_screen"
    assert exc.value.detail["app"] == "chrome"
    assert activated == []


def test_activate_of_a_minimized_window_sends_nothing(monkeypatch) -> None:
    window = _FakeXWin(4, 40, 40, 800, 600, pid=7)
    activated = _chrome_stack(monkeypatch, [window])
    monkeypatch.setattr(_linux_system, "_is_hidden", lambda _win, _d: True)
    with pytest.raises(ComputerUseError) as exc:
        _linux_system.activate_app("chrome")
    assert exc.value.detail["reason"] == "window_off_screen"
    assert activated == []


def test_activate_without_a_screen_size_still_focuses_a_positive_box(monkeypatch) -> None:
    """A fake display with no ``width_in_pixels`` is not a zero-size screen."""
    window = _FakeXWin(7, 10, 20, 200, 100, pid=7)
    display = _display([window])
    activated: list[int] = []
    monkeypatch.setattr(_linux_system, "_display", lambda: display)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "chrome")
    monkeypatch.setattr(
        _linux_system, "_send_active_window", lambda _d, win: activated.append(int(win.id)),
    )
    assert _linux_system.activate_app("chrome") == "chrome"
    assert activated == [7]


def test_activate_without_a_screen_size_takes_the_top_window(monkeypatch) -> None:
    bottom = _FakeXWin(1, 10, 20, 200, 100, pid=7)
    top = _FakeXWin(2, 30, 40, 200, 100, pid=7)
    display = _display([bottom, top])
    activated: list[int] = []
    monkeypatch.setattr(_linux_system, "_display", lambda: display)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "chrome")
    monkeypatch.setattr(
        _linux_system, "_send_active_window", lambda _d, win: activated.append(int(win.id)),
    )
    assert _linux_system.activate_app("chrome") == "chrome"
    assert activated == [2]


class _LinuxCapture:
    """Linux window list, bottom to top, with a known display and no window capture."""

    name = "linux"
    resolves_apps = True

    def __init__(self, rows):
        self.rows = rows
        self.shots = 0

    def frontmost_app(self):
        return ("other", 1)

    def windows(self):
        return list(self.rows)

    def displays(self):
        return (SCREEN,)

    def screenshot(self, display_id=None):
        self.shots += 1
        buf = io.BytesIO()
        Image.new("RGB", (SCREEN.width, SCREEN.height), (8, 8, 8)).save(buf, format="PNG")
        from a11y_computer_use import capture

        return capture.Screenshot(png=buf.getvalue(), display=SCREEN)


def _runtime(tmp_path, driver, engine):
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier("chrome", safety.Tier.READ)
    return server.Runtime(
        store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver, ocr_engine=engine,
    )


def test_screen_text_refuses_an_app_whose_windows_miss_the_display(tmp_path) -> None:
    rows = [{
        "app": "chrome", "window_id": 9, "on_screen": True,
        "bounds": {"display_id": 0, "x": 2000, "y": 40, "width": 800, "height": 600},
    }]
    driver = _LinuxCapture(rows)
    engine = ocr.FakeOcr(())
    rt = _runtime(tmp_path, driver, engine)
    with pytest.raises(ComputerUseError) as exc:
        rt.screen_text(app="chrome")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "app_not_on_screen"
    assert engine.calls == 0
    assert driver.shots == 0
    assert not hasattr(driver, "window_png")


def test_screen_text_crops_the_front_on_screen_window(tmp_path) -> None:
    """Bottom to top: an off-screen window, then the visible one. Index 0 is the visible id."""
    rows = [
        {
            "app": "chrome", "window_id": 1, "on_screen": True,
            "bounds": {"display_id": 0, "x": 2000, "y": 40, "width": 800, "height": 600},
        },
        {
            "app": "chrome", "window_id": 2, "on_screen": True,
            "bounds": {"display_id": 0, "x": 40, "y": 50, "width": 400, "height": 300},
        },
    ]
    driver = _LinuxCapture(rows)
    engine = ocr.FakeOcr((ocr.TextBox("City", 0, 0, 40, 16, 0.9),))
    rt = _runtime(tmp_path, driver, engine)
    text = rt.screen_text(app="chrome")
    assert "window 2" in text
    assert "window 1" not in text
    assert engine.calls == 1
    assert driver.shots == 1
    line = rt._screen_text.line("o1")
    assert line.bounds.x == 40 and line.bounds.y == 50


def test_app_windows_drops_the_off_screen_row_and_puts_the_front_window_first(tmp_path) -> None:
    rows = [
        {
            "app": "chrome", "window_id": 1, "on_screen": True,
            "bounds": {"display_id": 0, "x": 10, "y": 20, "width": 100, "height": 80},
        },
        {
            "app": "chrome", "window_id": 2, "on_screen": True,
            "bounds": {"display_id": 0, "x": 2000, "y": 20, "width": 100, "height": 80},
        },
        {
            "app": "chrome", "window_id": 3, "on_screen": True,
            "bounds": {"display_id": 0, "x": 30, "y": 40, "width": 100, "height": 80},
        },
    ]
    rt = _runtime(tmp_path, _LinuxCapture(rows), ocr.FakeOcr(()))
    assert [wid for wid, _rect in rt._app_windows("chrome")] == [3, 1]


class _RaiseDriver:
    name = "linux"
    resolves_apps = True

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple] = []

    def frontmost_app(self):
        return ("chrome", 1)

    def windows(self):
        return list(self.rows)

    def displays(self):
        return (SCREEN,)

    def window_owner(self, window_id):
        return "chrome"

    def raise_window(self, window_id):
        self.calls.append(("raise", window_id))

    def focus_window(self, window_id):
        self.calls.append(("focus", window_id))


def test_raise_of_a_window_outside_the_display_is_not_confirmed(tmp_path) -> None:
    rows = [
        {
            "window_id": 4, "app": "chrome", "on_screen": True,
            "bounds": {"display_id": 0, "x": 2000, "y": 40, "width": 800, "height": 600},
        },
        {
            "window_id": 5, "app": "chrome", "on_screen": True,
            "bounds": {"display_id": 0, "x": 40, "y": 40, "width": 800, "height": 600},
        },
    ]
    driver = _RaiseDriver(rows)
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier("chrome", safety.Tier.CLICK)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    off = rt.window("raise", window_id=4)
    assert off == "raised window 4 (chrome)"
    assert off.outcome == "suspected_noop"
    assert "outside the display" in off.evidence
    on = rt.window("focus", window_id=5)
    assert on == "focused window 5 (chrome)"
    assert on.outcome == "confirmed"
    assert driver.calls == [("raise", 4), ("focus", 5)]


def test_non_linux_cover_still_reads_the_first_window(tmp_path, monkeypatch) -> None:
    """A driver that is not Linux keeps the first listed window, including a missing box."""

    class _Other:
        name = "fake-bg"
        resolves_apps = False

        def frontmost_app(self):
            return ("other", 1)

        def windows(self):
            return [{
                "app": "chrome",
                "bounds": {"display_id": 1, "x": 100, "y": 100, "width": 800, "height": 600},
            }]

    store = safety.PermissionStore(tmp_path / "perm.json")
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_Other())
    monkeypatch.setattr(
        server, "_app_at_point",
        lambda point: "codex" if (point.x, point.y) == (500, 400) else "chrome",
    )
    assert rt._window_cover("chrome", "chrome") == "codex"

    class _Missing(_Other):
        def windows(self):
            return [{"app": "chrome", "bounds": None}, {
                "app": "chrome",
                "bounds": {"display_id": 1, "x": 100, "y": 100, "width": 800, "height": 600},
            }]

    bare = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit2"), driver=_Missing())
    assert bare._window_cover("chrome", "chrome") is None


def test_linux_cover_skips_an_off_screen_window(tmp_path, monkeypatch) -> None:
    """Bottom to top: a minimized row, the visible window, then one past the display.

    The visible centre is the only point asked. An off-screen-only list asks none.
    """
    rows = [
        {"app": "chrome", "on_screen": False, "bounds": None},
        {
            "app": "chrome", "on_screen": True,
            "bounds": {"display_id": 0, "x": 100, "y": 80, "width": 200, "height": 100},
        },
        {
            "app": "chrome", "on_screen": True,
            "bounds": {"display_id": 0, "x": 2000, "y": 40, "width": 800, "height": 600},
        },
    ]

    class _Cover(_LinuxCapture):
        resolves_apps = False

    store = safety.PermissionStore(tmp_path / "perm.json")
    rt = server.Runtime(
        store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_Cover(rows),
    )
    seen: list[tuple[int, int]] = []

    def at_point(point):
        seen.append((point.x, point.y))
        return "plank"

    monkeypatch.setattr(server, "_app_at_point", at_point)
    assert rt._window_cover("chrome", "chrome") == "plank"
    assert seen == [(200, 130)]

    only_off = [{
        "app": "chrome", "on_screen": True,
        "bounds": {"display_id": 0, "x": 2000, "y": 40, "width": 800, "height": 600},
    }]
    missed = server.Runtime(
        store=store, audit=safety.AuditLog(tmp_path / "audit-off"), driver=_Cover(only_off),
    )
    assert missed._window_cover("chrome", "chrome") is None
    assert seen == [(200, 130)]


def test_raise_of_a_minimized_window_is_not_confirmed(tmp_path) -> None:
    rows = [{"window_id": 6, "app": "chrome", "on_screen": False, "bounds": None}]
    driver = _RaiseDriver(rows)
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier("chrome", safety.Tier.CLICK)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    result = rt.window("raise", window_id=6)
    assert result == "raised window 6 (chrome)"
    assert result.outcome == "suspected_noop"
    assert "not on screen" in result.evidence
