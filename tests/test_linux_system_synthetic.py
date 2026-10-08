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

from a11y_computer_use.drivers import _linux_system
from a11y_computer_use.schema import ComputerUseError, ErrorCode


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


def test_plain_windows_stay_on_screen_when_atoms_are_names(fake_ewmh) -> None:
    """intern_atom returns the atom name in these fakes. That must not throw
    out of the window list or mark a normal window minimized."""
    rows = {row["window_id"]: row for row in _linux_system.windows()}
    assert rows[0x20]["app"] == "python3"
    assert rows[0x20]["on_screen"] is True
    assert rows[0x20]["bounds"] == {"display_id": 0, "x": 158, "y": 50, "width": 400, "height": 200}


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


def test_pids_matching_matches_the_owner_comm_but_never_a_window_title(monkeypatch) -> None:
    """find_root ranks a PID match like a name match and takes the ACTIVE frame, so a
    Chrome tab titled "gedit - Google Search" must not put Chrome's PID in gedit's
    set, or Runtime.snapshot("gedit") would return Chrome's tree under gedit's grant.
    The title -> comm mapping lives one layer up, in resolve_app."""

    class _TitledWin(_FakeXWin):
        def __init__(self, wid: int, pid: int, title: str):
            super().__init__(wid, 0, 0, 800, 600, pid)
            self.title = title

        def get_full_property(self, atom, kind):
            if atom == "_NET_WM_NAME":
                return _NS(value=self.title.encode())
            return super().get_full_property(atom, kind)

    editor = _TitledWin(0x50, pid=1, title="doc - gedit")
    chrome = _TitledWin(0x60, pid=2, title="gedit - Google Search - Google Chrome")
    root = _FakeXRoot([editor, chrome])
    by_id = {w.id: w for w in (editor, chrome)}
    display = _NS(
        screen=lambda: _NS(root=root),
        intern_atom=lambda name: name,
        create_resource_object=lambda kind, wid: by_id[int(wid)],
    )
    monkeypatch.setattr(_linux_system, "_display", lambda: display)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: {1: "gedit", 2: "chrome"}.get(pid))
    assert _linux_system.pids_matching("gedit") == {1}
    assert _linux_system.pids_matching("chrome") == {2}
    assert _linux_system.pids_matching("") == set()
    assert _linux_system.resolve_app("Google Search") == "chrome"  # titles resolve here, by design


def test_resolve_app_prefers_the_owning_comm_over_a_window_that_names_it(monkeypatch) -> None:
    """Krita's Help menu opened a Chromium tab "Donations | Krita" stacked above
    Krita's own window; resolve_app("krita") walked the stack top-down and took
    the first title hit, so `window list app=krita` returned Chromium's window
    and the planner lost Krita. A comm match wins wherever it sits in the stack."""

    class _TitledWin(_FakeXWin):
        def __init__(self, wid: int, pid: int, title: str):
            super().__init__(wid, 0, 0, 800, 600, pid)
            self.title = title

        def get_full_property(self, atom, kind):
            if atom == "_NET_WM_NAME":
                return _NS(value=self.title.encode())
            return super().get_full_property(atom, kind)

    donate = _TitledWin(0x70, pid=2, title="Donations | Krita - Chromium")
    krita = _TitledWin(0x80, pid=1, title="Krita")
    root = _FakeXRoot([donate, krita])  # Chromium first in stacking order
    by_id = {w.id: w for w in (donate, krita)}
    display = _NS(
        screen=lambda: _NS(root=root),
        intern_atom=lambda name: name,
        create_resource_object=lambda kind, wid: by_id[int(wid)],
    )
    monkeypatch.setattr(_linux_system, "_display", lambda: display)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: {1: "krita", 2: "chrome"}.get(pid))
    assert _linux_system.resolve_app("krita") == "krita"
    assert _linux_system.resolve_app("Donations") == "chrome"  # a pure title still resolves
    assert _linux_system.resolve_app("nothing-here") == "nothing-here"


def test_google_chrome_resolves_to_the_running_chrome_process(monkeypatch) -> None:
    """Chrome's launcher is google-chrome and its comm is chrome. The window
    title is "Page - Google Chrome". All three must name the same app, and a
    title that merely mentions another app must not steal that app's PIDs."""

    class _TitledWin(_FakeXWin):
        def __init__(self, wid: int, pid: int, title: str):
            super().__init__(wid, 0, 0, 800, 600, pid)
            self.title = title

        def get_full_property(self, atom, kind):
            if atom == "_NET_WM_NAME":
                return _NS(value=self.title.encode())
            return super().get_full_property(atom, kind)

    chrome = _TitledWin(0x90, pid=7, title="Example - Google Chrome")
    terminal = _TitledWin(0x91, pid=8, title="gnome-terminal-server")
    root = _FakeXRoot([chrome, terminal])
    by_id = {w.id: w for w in (chrome, terminal)}
    display = _NS(
        screen=lambda: _NS(root=root),
        intern_atom=lambda name: name,
        create_resource_object=lambda kind, wid: by_id[int(wid)],
    )
    monkeypatch.setattr(_linux_system, "_display", lambda: display)
    monkeypatch.setattr(
        _linux_system, "_comm_for_pid",
        lambda pid: {7: "chrome", 8: "gnome-terminal-"}.get(pid),
    )
    assert _linux_system.resolve_app("chrome") == "chrome"
    assert _linux_system.resolve_app("google-chrome") == "chrome"
    assert _linux_system.resolve_app("Google Chrome") == "chrome"
    assert _linux_system.resolve_app("gnome-terminal-server") == "gnome-terminal-"
    assert _linux_system.resolve_app("nothing-here") == "nothing-here"
    assert _linux_system.pids_matching("google-chrome") == {7}
    assert _linux_system.pids_matching("chrome") == {7}
    assert _linux_system.pids_matching("Google Chrome") == set()
    assert _linux_system.pids_matching("gnome-terminal-server") == {8}


def _isolate_desktop_dirs(tmp_path, monkeypatch) -> None:
    home = tmp_path / "xdg-home"
    system = tmp_path / "xdg-dirs"
    home.mkdir()
    system.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(home))
    monkeypatch.setenv("XDG_DATA_DIRS", str(system))


def _record_spawns(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def popen(argv, **kwargs):
        calls.append(list(argv))
        return _NS()

    monkeypatch.setattr(_linux_system.subprocess, "Popen", popen)
    return calls


def test_missing_program_is_not_launched(tmp_path, monkeypatch) -> None:
    _isolate_desktop_dirs(tmp_path, monkeypatch)
    calls = _record_spawns(monkeypatch)
    with pytest.raises(ComputerUseError) as exc:
        _linux_system.launch_app("no-such-binary-a11y")
    assert exc.value.code is ErrorCode.APP_NOT_FOUND
    assert "not on PATH" in exc.value.message
    assert calls == []


def test_path_executable_is_spawned_directly(monkeypatch) -> None:
    def which(name):
        return "/usr/bin/mousepad" if name == "mousepad" else None

    monkeypatch.setattr(_linux_system.shutil, "which", which)
    calls = _record_spawns(monkeypatch)
    _linux_system.launch_app("mousepad")
    assert calls == [["/usr/bin/mousepad"]]


def test_desktop_id_uses_gtk_launch(tmp_path, monkeypatch) -> None:
    _isolate_desktop_dirs(tmp_path, monkeypatch)
    apps = tmp_path / "xdg-home" / "applications"
    apps.mkdir()
    (apps / "org.example.App.desktop").write_text(
        "[Desktop Entry]\nType=Application\nExec=real-app %F\n", encoding="utf-8",
    )

    def which(name):
        return "/usr/bin/gtk-launch" if name == "gtk-launch" else None

    monkeypatch.setattr(_linux_system.shutil, "which", which)
    calls = _record_spawns(monkeypatch)
    _linux_system.launch_app("org.example.App")
    assert calls == [["/usr/bin/gtk-launch", "org.example.App.desktop"]]


def test_nested_desktop_id_uses_gtk_launch(tmp_path, monkeypatch) -> None:
    _isolate_desktop_dirs(tmp_path, monkeypatch)
    nested = tmp_path / "xdg-home" / "applications" / "foo"
    nested.mkdir(parents=True)
    (nested / "bar.desktop").write_text(
        "[Desktop Entry]\nType=Application\nExec=real-app\n", encoding="utf-8",
    )

    def which(name):
        return "/usr/bin/gtk-launch" if name == "gtk-launch" else None

    monkeypatch.setattr(_linux_system.shutil, "which", which)
    calls = _record_spawns(monkeypatch)
    _linux_system.launch_app("foo-bar")
    assert calls == [["/usr/bin/gtk-launch", "foo-bar.desktop"]]


def test_desktop_exec_runs_when_gtk_launch_is_absent(tmp_path, monkeypatch) -> None:
    _isolate_desktop_dirs(tmp_path, monkeypatch)
    apps = tmp_path / "xdg-home" / "applications"
    apps.mkdir()
    (apps / "org.example.App.desktop").write_text(
        '[Desktop Entry]\nType=Application\nExec="/usr/bin/real-app" --flag %F\n',
        encoding="utf-8",
    )

    def which(name):
        if name == "/usr/bin/real-app":
            return "/usr/bin/real-app"
        return None

    monkeypatch.setattr(_linux_system.shutil, "which", which)
    calls = _record_spawns(monkeypatch)
    _linux_system.launch_app("org.example.App")
    assert calls == [["/usr/bin/real-app", "--flag"]]


def test_desktop_file_without_a_launcher_or_exec_fails_immediately(tmp_path, monkeypatch) -> None:
    _isolate_desktop_dirs(tmp_path, monkeypatch)
    apps = tmp_path / "xdg-home" / "applications"
    apps.mkdir()
    (apps / "org.example.App.desktop").write_text(
        "[Desktop Entry]\nType=Application\nExec=missing-real-app %F\n", encoding="utf-8",
    )
    monkeypatch.setattr(_linux_system.shutil, "which", lambda name: None)
    calls = _record_spawns(monkeypatch)
    with pytest.raises(ComputerUseError) as exc:
        _linux_system.launch_app("org.example.App")
    assert exc.value.code is ErrorCode.APP_NOT_FOUND
    assert calls == []


def test_app_launch_of_a_missing_program_does_not_wait_for_a_window(tmp_path, monkeypatch) -> None:
    import time

    from a11y_computer_use import safety, server

    _isolate_desktop_dirs(tmp_path, monkeypatch)
    calls = _record_spawns(monkeypatch)
    waited: list[str] = []

    class _D:
        resolves_apps = False
        name = "linux"

        def frontmost_app(self):
            return ("shell", 1)

        def main_display_id(self):
            return 0

        def launch_app(self, ident):
            _linux_system.launch_app(ident)

        def windows(self):
            waited.append("windows")
            return []

    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("no-such-binary-a11y", safety.Tier.CLICK)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_D())
    rt.APP_LAUNCH_WAIT_S = 30
    monkeypatch.setattr(
        rt, "_resolve_app",
        lambda ident: (_ for _ in ()).throw(ComputerUseError(ErrorCode.APP_NOT_FOUND, "not running")),
    )
    started = time.monotonic()
    with pytest.raises(ComputerUseError) as exc:
        rt.app("launch", "no-such-binary-a11y")
    assert time.monotonic() - started < 2
    assert exc.value.code is ErrorCode.APP_NOT_FOUND
    assert "not on PATH" in exc.value.message
    # One list, taken before the spawn, so a second window can be told from
    # one that was already open. The missing program is not spawned and the
    # call does not poll until the launch timeout.
    assert calls == [] and waited == ["windows"]


def test_activate_app_with_no_window_is_app_not_found(monkeypatch) -> None:
    """A granted name with no window is not activated. Synthetic window list."""
    monkeypatch.setattr(_linux_system, "_display", lambda: object())
    monkeypatch.setattr(_linux_system, "_managed_windows", lambda _display: [])
    with pytest.raises(ComputerUseError) as exc:
        _linux_system.activate_app("xfce4-terminal")
    assert exc.value.code is ErrorCode.APP_NOT_FOUND
    assert exc.value.detail["app"] == "xfce4-terminal"
    assert "xfce4-terminal" in exc.value.message
    assert "activated" not in exc.value.message


def test_pidless_windows_use_wm_class_and_minimized_windows_are_off_screen(monkeypatch) -> None:
    """No _NET_WM_PID: the app id is the WM_CLASS instance. Iconic or hidden
    windows report on_screen false and no bounds."""

    class _PropWin(_FakeXWin):
        def __init__(self, wid, pid, props, x=10, y=20, w=100, h=80):
            super().__init__(wid, x, y, w, h, pid)
            self.props = props

        def get_full_property(self, atom, kind):
            if atom == "_NET_WM_PID":
                return _NS(value=[self.pid]) if self.pid else None
            if atom in self.props:
                return _NS(value=self.props[atom])
            return None

    mousepad = _PropWin(1, pid=11, props={})
    xmessage = _PropWin(2, pid=0, props={"WM_CLASS": b"xmessage\x00Xmessage\x00"})
    nameless = _PropWin(3, pid=0, props={})
    iconic = _PropWin(4, pid=11, props={"WM_STATE": [3, 0]})
    hidden = _PropWin(5, pid=11, props={"_NET_WM_STATE": ["_NET_WM_STATE_HIDDEN"]})
    root = _FakeXRoot([mousepad, xmessage, nameless, iconic, hidden])
    by_id = {w.id: w for w in (mousepad, xmessage, nameless, iconic, hidden)}
    display = _NS(
        screen=lambda: _NS(root=root),
        intern_atom=lambda name: name,
        create_resource_object=lambda kind, wid: by_id[int(wid)],
    )
    monkeypatch.setattr(_linux_system, "_display", lambda: display)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "mousepad" if pid == 11 else None)
    rows = {row["window_id"]: row for row in _linux_system.windows()}
    assert rows[1]["app"] == "mousepad" and rows[1]["on_screen"] is True
    assert rows[2]["app"] == "xmessage" and rows[2]["pid"] == 0 and rows[2]["on_screen"] is True
    assert rows[3]["app"] == "" and rows[3]["on_screen"] is True
    assert rows[4]["on_screen"] is False and rows[4]["bounds"] is None
    assert rows[5]["on_screen"] is False and rows[5]["bounds"] is None
    assert _linux_system.window_owner(2) == "xmessage"
    assert _linux_system.window_owner(3) == ""
    assert _linux_system.resolve_app("xmessage") == "xmessage"


class _Proc:
    def __init__(self, code):
        self.code = code
        self.polls = 0

    def poll(self):
        self.polls += 1
        return self.code


def _launch_runtime(tmp_path, monkeypatch, name, handle, windows):
    from a11y_computer_use import safety, server

    listed = {"n": 0}

    class _D:
        resolves_apps = False
        name = "linux"

        def ensure_trusted(self):
            return None

        def frontmost_app(self):
            return ("shell", 1)

        def main_display_id(self):
            return 0

        def launch_app(self, ident):
            assert ident == name
            return handle

        def windows(self):
            listed["n"] += 1
            if callable(windows):
                return windows(listed["n"])
            return windows

    monkeypatch.setattr(
        server, "_running_app",
        lambda ident: (_ for _ in ()).throw(ComputerUseError(ErrorCode.APP_NOT_FOUND, "not running")),
    )
    monkeypatch.setattr(server, "_installed_bundle_id", lambda ident: None)
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier(name, safety.Tier.CLICK)
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_D())
    return runtime


@pytest.mark.parametrize("code", [0, 1])
def test_launch_of_an_exited_process_fails_immediately(tmp_path, monkeypatch, code) -> None:
    import time

    proc = _Proc(code)
    handle = {"pid": 50, "proc": proc, "identifier": "true", "names": ["true"], "is_launcher": False}
    runtime = _launch_runtime(tmp_path, monkeypatch, "true", handle, [])
    runtime.APP_LAUNCH_WAIT_S = 30
    started = time.monotonic()
    with pytest.raises(ComputerUseError) as exc:
        runtime.app("launch", "true")
    assert time.monotonic() - started < 2
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "process_exited"
    assert exc.value.detail["exit_code"] == code
    assert f"status {code}" in exc.value.message
    assert proc.polls >= 1


def test_absolute_path_launch_reports_the_window_the_process_opened(tmp_path, monkeypatch) -> None:
    handle = {
        "pid": 4242, "proc": _Proc(None), "identifier": "/usr/bin/mousepad",
        "names": ["/usr/bin/mousepad", "mousepad"], "is_launcher": False,
    }
    row = {
        "window_id": 7, "app": "mousepad", "title": "*Untitled 1 - Mousepad",
        "pid": 4242, "wm_class": "mousepad",
    }
    runtime = _launch_runtime(tmp_path, monkeypatch, "/usr/bin/mousepad", handle, [row])
    assert runtime.app("launch", "/usr/bin/mousepad") == (
        "launched /usr/bin/mousepad; first window: '*Untitled 1 - Mousepad'"
    )


def test_launch_reports_the_new_window_not_one_already_open(tmp_path, monkeypatch) -> None:
    handle = {
        "pid": 222, "proc": _Proc(None), "identifier": "mousepad",
        "names": ["mousepad"], "is_launcher": False,
    }
    old = {"window_id": 1, "app": "mousepad", "title": "*Untitled 1", "pid": 111}
    new = {"window_id": 2, "app": "mousepad", "title": "Untitled 2", "pid": 222}

    def rows(n):
        return [old] if n == 1 else [old, new]

    runtime = _launch_runtime(tmp_path, monkeypatch, "mousepad", handle, rows)
    assert runtime.app("launch", "mousepad") == "launched mousepad; first window: 'Untitled 2'"


def test_gtk_launch_exit_zero_waits_for_the_desktop_apps_window(tmp_path, monkeypatch) -> None:
    import time

    handle = {
        "pid": 50, "proc": _Proc(0), "identifier": "org.example.App",
        "names": ["org.example.App", "real-app"], "is_launcher": True,
    }

    def rows(n):
        if n < 3:
            return []
        return [{
            "window_id": 9, "app": "real-app", "title": "Real", "pid": 77, "wm_class": "real-app",
        }]

    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(time, "sleep", lambda seconds: clock.__setitem__("t", clock["t"] + seconds))
    runtime = _launch_runtime(tmp_path, monkeypatch, "org.example.App", handle, rows)
    runtime.APP_LAUNCH_WAIT_S = 5
    assert runtime.app("launch", "org.example.App") == "launched org.example.App; first window: 'Real'"


def test_gtk_launch_nonzero_exit_fails_immediately(tmp_path, monkeypatch) -> None:
    import time

    proc = _Proc(1)
    handle = {
        "pid": 50, "proc": proc, "identifier": "org.example.App",
        "names": ["org.example.App", "real-app"], "is_launcher": True,
    }
    runtime = _launch_runtime(tmp_path, monkeypatch, "org.example.App", handle, [])
    runtime.APP_LAUNCH_WAIT_S = 30
    started = time.monotonic()
    with pytest.raises(ComputerUseError) as exc:
        runtime.app("launch", "org.example.App")
    assert time.monotonic() - started < 2
    assert exc.value.detail["reason"] == "process_exited"
    assert exc.value.detail["exit_code"] == 1
    assert proc.polls >= 1


def test_a_running_launch_with_no_window_is_a_timeout(tmp_path, monkeypatch) -> None:
    import time

    handle = {
        "pid": 9, "proc": _Proc(None), "identifier": "mousepad",
        "names": ["mousepad"], "is_launcher": False,
    }
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(time, "sleep", lambda seconds: clock.__setitem__("t", clock["t"] + seconds))
    runtime = _launch_runtime(tmp_path, monkeypatch, "mousepad", handle, [])
    runtime.APP_LAUNCH_WAIT_S = 1
    with pytest.raises(ComputerUseError) as exc:
        runtime.app("launch", "mousepad")
    assert exc.value.code is ErrorCode.TIMEOUT
    assert exc.value.detail["reason"] == "no_window"
    assert "no window appeared" in exc.value.message


def test_gtk_launch_handle_names_the_desktop_app(tmp_path, monkeypatch) -> None:
    _isolate_desktop_dirs(tmp_path, monkeypatch)
    apps = tmp_path / "xdg-home" / "applications"
    apps.mkdir()
    (apps / "org.example.App.desktop").write_text(
        "[Desktop Entry]\nType=Application\nExec=real-app %F\nStartupWMClass=RealApp\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        _linux_system.shutil, "which",
        lambda name: "/usr/bin/gtk-launch" if name == "gtk-launch" else None,
    )
    _record_spawns(monkeypatch)
    handle = _linux_system.launch_app("org.example.App")
    assert handle["is_launcher"] is True
    assert "real-app" in handle["names"]
    assert "RealApp" in handle["names"]
    assert "gtk-launch" not in handle["names"]
