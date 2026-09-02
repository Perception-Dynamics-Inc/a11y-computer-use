"""Linux EWMH probes (`drivers/_linux_system.py`) with fake X objects, on any OS.

Reproduces the trap found on a real desktop (docs/box-testbed.md): a client
window at (158, 50) and a full-screen desktop window at (0, 0).
``win.translate_coords(root, 0, 0)`` yields root's origin in WINDOW coordinates,
(-158, -50), so the app window never contains any screen point and the desktop
window wins every act-time hit-test. ``root.translate_coords(win, 0, 0)`` yields
the window origin in root coordinates, which is what a screen-point hit-test and
the window listing need. Under Xvfb with no window manager every window sits at
(0, 0), where both forms agree, which is why CI never saw it.
"""

from __future__ import annotations

from types import SimpleNamespace as _NS

import pytest

from computeruse.drivers import _linux_system


class _FakeXWin:
    def __init__(self, wid: int, x: int, y: int, w: int, h: int, pid: int):
        self.id, self.x, self.y, self.w, self.h, self.pid = wid, x, y, w, h, pid

    def get_geometry(self):
        return _NS(x=0, y=0, width=self.w, height=self.h)  # relative to the WM frame

    def translate_coords(self, src, sx, sy):
        # root's origin expressed in this window's coordinates: the negated position
        return _NS(x=sx - self.x, y=sy - self.y)

    def get_full_property(self, atom, kind):
        return _NS(value=[self.pid]) if atom == "_NET_WM_PID" else None


class _FakeXRoot:
    def __init__(self, stacking):  # bottom -> top, per EWMH _NET_CLIENT_LIST_STACKING
        self.stacking = stacking

    def translate_coords(self, src, sx, sy):
        # a window's origin expressed in root (screen) coordinates
        return _NS(x=src.x + sx, y=src.y + sy)

    def get_full_property(self, atom, kind):
        if atom == "_NET_CLIENT_LIST_STACKING":
            return _NS(value=[w.id for w in self.stacking])
        return None


@pytest.fixture
def fake_ewmh(monkeypatch):
    desktop = _FakeXWin(0x10, 0, 0, 1920, 1080, pid=100)
    app = _FakeXWin(0x20, 158, 50, 400, 200, pid=200)
    root = _FakeXRoot([desktop, app])
    by_id = {w.id: w for w in (desktop, app)}
    display = _NS(
        screen=lambda: _NS(root=root),
        intern_atom=lambda name: name,
        create_resource_object=lambda kind, wid: by_id[int(wid)],
    )
    monkeypatch.setattr(_linux_system, "_display", lambda: display)
    monkeypatch.setattr(
        _linux_system, "_comm_for_pid", lambda pid: {100: "nemo-desktop", 200: "python3"}.get(pid)
    )
    return display


def test_window_geometry_is_reported_in_root_coordinates(fake_ewmh) -> None:
    app = fake_ewmh.create_resource_object("window", 0x20)
    assert _linux_system._geometry_on_root(app, fake_ewmh) == (158, 50, 400, 200)


def test_app_at_point_prefers_the_window_that_contains_the_point(fake_ewmh) -> None:
    assert _linux_system.app_at_point_id(358, 84) == "python3"  # inside the app window
    assert _linux_system.app_at_point_id(5, 5) == "nemo-desktop"  # only the desktop is there
    assert _linux_system.app_at_point_id(3000, 3000) is None


def test_topmost_window_wins_when_windows_overlap(monkeypatch) -> None:
    """Stacking order is bottom -> top; the hit-test must walk it top -> bottom."""
    below = _FakeXWin(0x30, 100, 100, 600, 400, pid=300)
    above = _FakeXWin(0x40, 300, 200, 200, 100, pid=400)
    root = _FakeXRoot([below, above])
    by_id = {w.id: w for w in (below, above)}
    display = _NS(
        screen=lambda: _NS(root=root),
        intern_atom=lambda name: name,
        create_resource_object=lambda kind, wid: by_id[int(wid)],
    )
    monkeypatch.setattr(_linux_system, "_display", lambda: display)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: {300: "gedit", 400: "dialog"}.get(pid))
    assert _linux_system.app_at_point_id(350, 250) == "dialog"  # inside both: topmost wins
    assert _linux_system.app_at_point_id(150, 150) == "gedit"  # only the lower window
