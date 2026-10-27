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

    def get_attributes(self):
        return _NS(map_state=2)  # ordinary mapped client

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


def test_app_at_point_uses_wm_class_when_the_window_has_no_pid(monkeypatch) -> None:
    """A Tk or Xt window has no comm. The hit-test names its WM_CLASS."""
    win = _ClassWin(0x21, pid=0, instance="xmessage", klass="Xmessage", title="note")
    _active_display(monkeypatch, [win], 0x21)
    assert _linux_system.app_at_point_id(20, 20) == "xmessage"
    assert _linux_system.pid_at_point(20, 20) is None
    blank = _ClassWin(0x22, pid=0, instance="", klass="", title="blank")
    _active_display(monkeypatch, [blank], 0x22)
    assert _linux_system.app_at_point_id(20, 20) is None


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
    monkeypatch.setattr(_linux_system, "_win_title", lambda win, d: "Preferences" if win.id == 0x40 else "Editor")
    assert _linux_system.app_at_point_id(350, 250) == "dialog"  # inside both: topmost wins
    assert _linux_system.app_at_point_id(150, 150) == "gedit"  # only the lower window
    assert _linux_system.window_at_point(350, 250) == {"window_id": 0x40, "title": "Preferences"}
    assert _linux_system.window_at_point(150, 150) == {"window_id": 0x30, "title": "Editor"}
    assert _linux_system.window_at_point(3000, 3000) is None


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


class _ClassWin(_FakeXWin):
    def __init__(self, wid: int, pid: int, instance: str = "", klass: str = "", title: str = ""):
        super().__init__(wid, 10, 10, 200, 100, pid)
        self.instance = instance
        self.klass = klass
        self.title = title

    def get_full_property(self, atom, kind):
        if atom == "_NET_WM_PID":
            return _NS(value=[self.pid]) if self.pid else None
        if atom == "WM_CLASS" and (self.instance or self.klass):
            return _NS(value=f"{self.instance}\x00{self.klass}\x00".encode())
        if atom == "_NET_WM_NAME" and self.title:
            return _NS(value=self.title.encode())
        return None


def _active_display(monkeypatch, wins: list, active_id: int) -> None:
    class _Root(_FakeXRoot):
        def get_full_property(self, atom, kind):
            if atom == "_NET_ACTIVE_WINDOW":
                return _NS(value=[active_id])
            return super().get_full_property(atom, kind)

    root = _Root(wins)
    by_id = {win.id: win for win in wins}
    display = _NS(
        screen=lambda: _NS(root=root),
        intern_atom=lambda name: name,
        create_resource_object=lambda kind, wid: by_id[int(wid)],
    )
    monkeypatch.setattr(_linux_system, "_display", lambda: display)
    monkeypatch.setattr(
        _linux_system, "_comm_for_pid",
        lambda pid: {200: "python3"}.get(int(pid or 0)) or None,
    )


def test_active_window_names_the_ewmh_window_and_its_comm(monkeypatch) -> None:
    win = _ClassWin(0x20, pid=200, instance="cuakeytarget", klass="Cuakeytarget", title="cuakeytarget")
    _active_display(monkeypatch, [win], 0x20)
    assert _linux_system.active_window() == {
        "window_id": 0x20, "app": "python3", "pid": 200, "title": "cuakeytarget",
    }
    assert _linux_system.frontmost_app_id() == "python3"


def test_active_window_uses_wm_class_when_the_window_has_no_pid(monkeypatch) -> None:
    """``frontmost_app_id`` stays empty: permission identity is the comm only."""
    win = _ClassWin(0x21, pid=0, instance="xmessage", klass="Xmessage", title="note")
    _active_display(monkeypatch, [win], 0x21)
    assert _linux_system.active_window() == {
        "window_id": 0x21, "app": "xmessage", "pid": 0, "title": "note",
    }
    assert _linux_system.frontmost_app_id() == ""


def test_active_window_id_zero_is_none(monkeypatch) -> None:
    win = _ClassWin(0x22, pid=200, title="idle")
    _active_display(monkeypatch, [win], 0)
    assert _linux_system.active_window() is None


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


def test_display_name_finds_the_lower_case_program(monkeypatch) -> None:
    """``Mousepad`` is the display name. PATH has ``mousepad`` only."""
    def which(name):
        return "/usr/bin/mousepad" if name == "mousepad" else None

    monkeypatch.setattr(_linux_system.shutil, "which", which)
    calls = _record_spawns(monkeypatch)
    _linux_system.launch_app("Mousepad")
    assert calls == [["/usr/bin/mousepad"]]


def _mousepad_desktop(path) -> None:
    path.write_text(
        "[Desktop Entry]\nType=Application\nName=Mousepad\n"
        "GenericName=Text Editor\nExec=mousepad %F\nStartupWMClass=mousepad\n",
        encoding="utf-8",
    )


def test_display_name_and_generic_name_launch_the_desktop_file(tmp_path, monkeypatch) -> None:
    """With no PATH hit, ``Mousepad`` and ``Text Editor`` start that desktop file."""
    _isolate_desktop_dirs(tmp_path, monkeypatch)
    apps = tmp_path / "xdg-home" / "applications"
    apps.mkdir(parents=True)
    _mousepad_desktop(apps / "org.xfce.mousepad.desktop")

    def which(name):
        return "/usr/bin/gtk-launch" if name == "gtk-launch" else None

    monkeypatch.setattr(_linux_system.shutil, "which", which)
    calls = _record_spawns(monkeypatch)
    _linux_system.launch_app("Mousepad")
    _linux_system.launch_app("Text Editor")
    assert calls == [
        ["/usr/bin/gtk-launch", "org.xfce.mousepad.desktop"],
        ["/usr/bin/gtk-launch", "org.xfce.mousepad.desktop"],
    ]
    calls.clear()
    with pytest.raises(ComputerUseError) as exc:
        _linux_system.launch_app("Not a Desktop App")
    assert exc.value.code is ErrorCode.APP_NOT_FOUND
    assert "not on PATH" in exc.value.message
    assert calls == []


def test_generic_name_exec_runs_when_gtk_launch_is_absent(tmp_path, monkeypatch) -> None:
    _isolate_desktop_dirs(tmp_path, monkeypatch)
    apps = tmp_path / "xdg-home" / "applications"
    apps.mkdir(parents=True)
    _mousepad_desktop(apps / "org.xfce.mousepad.desktop")

    def which(name):
        return "/usr/bin/mousepad" if name == "mousepad" else None

    monkeypatch.setattr(_linux_system.shutil, "which", which)
    calls = _record_spawns(monkeypatch)
    _linux_system.launch_app("Text Editor")
    assert calls == [["/usr/bin/mousepad"]]


def test_name_beats_an_earlier_generic_name_and_skips_hidden(tmp_path, monkeypatch) -> None:
    """A hidden file and another app's GenericName do not steal ``Mousepad``."""
    _isolate_desktop_dirs(tmp_path, monkeypatch)
    home = tmp_path / "xdg-home" / "applications"
    system = tmp_path / "xdg-dirs" / "applications"
    home.mkdir(parents=True)
    system.mkdir(parents=True)
    (home / "hidden-mousepad.desktop").write_text(
        "[Desktop Entry]\nType=Application\nHidden=true\nName=Mousepad\nExec=hidden-app\n",
        encoding="utf-8",
    )
    (home / "org.example.Notes.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Notes\nGenericName=Mousepad\nExec=notes\n",
        encoding="utf-8",
    )
    _mousepad_desktop(system / "org.xfce.mousepad.desktop")

    def which(name):
        return "/usr/bin/gtk-launch" if name == "gtk-launch" else None

    monkeypatch.setattr(_linux_system.shutil, "which", which)
    calls = _record_spawns(monkeypatch)
    _linux_system.launch_app("Mousepad")
    assert calls == [["/usr/bin/gtk-launch", "org.xfce.mousepad.desktop"]]


def test_module_argv_spawns_the_program_and_its_flag(monkeypatch) -> None:
    def which(name):
        return "/usr/bin/soffice" if name in {"soffice", "/usr/bin/soffice"} else None

    monkeypatch.setattr(_linux_system.shutil, "which", which)
    calls = _record_spawns(monkeypatch)
    handle = _linux_system.launch_app("soffice", argv=("soffice", "--calc"))
    assert calls == [["/usr/bin/soffice", "--calc"]]
    assert "soffice.bin" in handle["names"]
    assert handle["is_launcher"] is False


def test_module_argv_missing_program_is_not_spawned(monkeypatch) -> None:
    monkeypatch.setattr(_linux_system.shutil, "which", lambda _name: None)
    calls = _record_spawns(monkeypatch)
    with pytest.raises(ComputerUseError) as exc:
        _linux_system.launch_app("soffice", argv=("soffice", "--writer"))
    assert exc.value.code is ErrorCode.APP_NOT_FOUND
    assert calls == []


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


def test_granted_display_name_launches_and_an_ungranted_app_does_not(tmp_path, monkeypatch) -> None:
    """``app launch "Mousepad"`` starts mousepad when that grant is held.

    The same call with only ``gedit`` granted does not spawn. ``Text Editor``
    is the desktop GenericName and follows the same gate.
    """
    from a11y_computer_use import safety, server

    _isolate_desktop_dirs(tmp_path, monkeypatch)
    apps = tmp_path / "xdg-home" / "applications"
    apps.mkdir(parents=True)
    _mousepad_desktop(apps / "org.xfce.mousepad.desktop")

    def which(name):
        if name == "mousepad":
            return "/usr/bin/mousepad"
        if name == "gtk-launch":
            return "/usr/bin/gtk-launch"
        return None

    monkeypatch.setattr(_linux_system.shutil, "which", which)
    calls = _record_spawns(monkeypatch)
    launched = {"n": 0}

    class _D:
        resolves_apps = False
        name = "linux"

        def ensure_trusted(self):
            return None

        def frontmost_app(self):
            return ("shell", 1)

        def main_display_id(self):
            return 0

        def launch_app(self, ident, *, argv=None):
            launched["n"] += 1
            if argv:
                return _linux_system.launch_app(ident, argv=argv)
            return _linux_system.launch_app(ident)

        def windows(self):
            if not launched["n"]:
                return []
            return [{
                "window_id": 1, "app": "mousepad",
                "title": "Untitled 1 - Mousepad", "pid": 4,
            }]

    monkeypatch.setattr(
        server, "_running_app",
        lambda ident: (_ for _ in ()).throw(ComputerUseError(ErrorCode.APP_NOT_FOUND, "not running")),
    )

    def runtime(name: str):
        store = safety.PermissionStore(tmp_path / f"{name}.json")
        rt = server.Runtime(
            store=store, audit=safety.AuditLog(tmp_path / f"{name}-audit"), driver=_D(),
        )
        rt.APP_LAUNCH_WAIT_S = 1
        monkeypatch.setattr(
            rt, "_resolve_app",
            lambda ident: (_ for _ in ()).throw(ComputerUseError(ErrorCode.APP_NOT_FOUND, "not running")),
        )
        return rt, store

    both, both_store = runtime("both")
    both_store.set_tier("mousepad", safety.Tier.CLICK)
    both_store.set_tier("Mousepad", safety.Tier.CLICK)
    text = both.app("launch", "Mousepad")
    assert calls == [["/usr/bin/mousepad"]]
    assert "Untitled 1 - Mousepad" in text

    calls.clear()
    launched["n"] = 0
    display, display_store = runtime("display")
    display_store.set_tier("Mousepad", safety.Tier.CLICK)
    text = display.app("launch", "Mousepad")
    assert calls == [["/usr/bin/mousepad"]]
    assert "Untitled 1 - Mousepad" in text

    calls.clear()
    launched["n"] = 0
    editor, editor_store = runtime("editor")
    editor_store.set_tier("mousepad", safety.Tier.CLICK)
    text = editor.app("launch", "Text Editor")
    assert calls == [["/usr/bin/mousepad"]]
    assert "Untitled 1 - Mousepad" in text

    calls.clear()
    launched["n"] = 0
    other, other_store = runtime("other")
    other_store.set_tier("gedit", safety.Tier.CLICK)
    with pytest.raises(server.ActionRefused) as refused:
        other.app("launch", "Mousepad")
    assert refused.value.decision.verdict is safety.Verdict.NEEDS_PERMISSION
    assert refused.value.decision.app != "gedit"
    assert calls == []

    with pytest.raises(server.ActionRefused) as refused_editor:
        other.app("launch", "Text Editor")
    assert refused_editor.value.decision.verdict is safety.Verdict.NEEDS_PERMISSION
    assert refused_editor.value.decision.app != "gedit"
    assert calls == []


def test_absolute_path_resolves_and_activates_the_basename(monkeypatch) -> None:
    """``/usr/bin/mousepad`` is the running ``mousepad`` window, not a missing app."""
    window = _FakeXWin(7, 10, 20, 200, 100, pid=11)
    root = _FakeXRoot([window])
    display = _NS(
        screen=lambda: _NS(root=root),
        intern_atom=lambda name: name,
        create_resource_object=lambda kind, wid: window,
    )
    activated: list[int] = []
    monkeypatch.setattr(_linux_system, "_display", lambda: display)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "mousepad" if pid == 11 else None)
    monkeypatch.setattr(_linux_system, "_send_active_window", lambda _d, win: activated.append(int(win.id)))
    assert _linux_system.resolve_app("/usr/bin/mousepad") == "mousepad"
    assert _linux_system.activate_app("/usr/bin/mousepad") == "mousepad"
    assert activated == [7]
    assert 11 in _linux_system.pids_matching("/usr/bin/mousepad")


def test_absolute_path_matches_wm_class_when_the_comm_is_the_interpreter(monkeypatch) -> None:
    """A script path matches WM_CLASS, then focus returns the interpreter comm."""

    class _ClassWin(_FakeXWin):
        def get_full_property(self, atom, kind):
            if atom == "_NET_WM_PID":
                return _NS(value=[self.pid])
            if atom == "WM_CLASS":
                return _NS(value=b"cuahandoff\x00Cuahandoff\x00")
            return None

    window = _ClassWin(9, 10, 20, 200, 100, pid=31)
    root = _FakeXRoot([window])
    display = _NS(
        screen=lambda: _NS(root=root),
        intern_atom=lambda name: name,
        create_resource_object=lambda kind, wid: window,
    )
    activated: list[int] = []
    monkeypatch.setattr(_linux_system, "_display", lambda: display)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "python3" if pid == 31 else None)
    monkeypatch.setattr(_linux_system, "_send_active_window", lambda _d, win: activated.append(int(win.id)))
    path = "/tmp/cuahandoff"
    assert _linux_system.resolve_app(path) == "python3"
    assert _linux_system.activate_app(path) == "python3"
    assert activated == [9]
    assert _linux_system.pids_matching(path) == set()


def test_focus_retries_until_the_active_window_stays(monkeypatch) -> None:
    """Synthetic X. Not a live window manager.

    The first ``_NET_ACTIVE_WINDOW`` is ignored, the way a second client's
    present() can leave the previous window active. The next request is
    recorded as the active window, and focus returns once two reads agree.
    A root with no such property still sends one message and does not poll.
    """
    other = _FakeXWin(4, 0, 0, 10, 10, pid=1)
    target = _FakeXWin(7, 0, 0, 10, 10, pid=2)
    state = {"active": other.id, "sends": 0, "sleeps": 0}

    class _Root(_FakeXRoot):
        def get_full_property(self, atom, kind):
            if atom == "_NET_ACTIVE_WINDOW":
                return _NS(value=[state["active"]])
            return super().get_full_property(atom, kind)

        def send_event(self, event, event_mask):
            return None

    root = _Root([other, target])
    by_id = {win.id: win for win in (other, target)}
    display = _NS(
        screen=lambda: _NS(root=root),
        intern_atom=lambda name: name,
        create_resource_object=lambda kind, wid: by_id[int(wid)],
        flush=lambda: None,
    )
    monkeypatch.setattr(_linux_system, "_display", lambda: display)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "gedit" if pid == 2 else "other")
    monkeypatch.setattr(_linux_system.time, "sleep", lambda _seconds: state.__setitem__("sleeps", state["sleeps"] + 1))

    def send(_d, win):
        state["sends"] += 1
        if state["sends"] >= 2:
            state["active"] = int(win.id)

    monkeypatch.setattr(_linux_system, "_send_active_window", send)
    assert _linux_system.focus_window(7) is True
    assert state["sends"] == 2
    assert state["active"] == 7
    assert state["sleeps"] == 2

    state["sends"] = 0
    state["sleeps"] = 0
    state["active"] = 7

    def send_once(_d, win):
        state["sends"] += 1
        state["active"] = int(win.id)

    monkeypatch.setattr(_linux_system, "_send_active_window", send_once)
    assert _linux_system.raise_window(7) is True
    assert state["sends"] == 1
    assert state["sleeps"] == 1

    bare = _FakeXRoot([target])
    bare_display = _NS(
        screen=lambda: _NS(root=bare),
        intern_atom=lambda name: name,
        create_resource_object=lambda kind, wid: target,
        flush=lambda: None,
    )
    monkeypatch.setattr(_linux_system, "_display", lambda: bare_display)
    state["sends"] = 0
    state["sleeps"] = 0
    assert _linux_system.focus_window(7) is True
    assert state["sends"] == 1
    assert state["sleeps"] == 0


def test_window_list_waits_for_a_late_pid_and_a_missing_one_waits_once(monkeypatch) -> None:
    """Synthetic X. Not a live window manager.

    ``_NET_WM_PID`` shows up after the window is already listed. The list
    waits and reports that pid. A window that never sets it is pid 0, and
    the next list does not wait again.
    """
    _linux_system._pid_absent.clear()
    clock = {"t": 0.0}
    state = {"sleeps": 0, "pid": 0}

    def sleep(seconds):
        state["sleeps"] += 1
        clock["t"] += seconds
        if state["sleeps"] >= 2:
            state["pid"] = 42

    class _Root(_FakeXRoot):
        def get_full_property(self, atom, kind):
            if atom == "_NET_ACTIVE_WINDOW":
                return _NS(value=[1])
            return super().get_full_property(atom, kind)

    class _PidWin(_FakeXWin):
        def get_full_property(self, atom, kind):
            if atom == "_NET_WM_PID":
                return _NS(value=[state["pid"]]) if state["pid"] else None
            if atom == "WM_CLASS":
                return _NS(value=b"a11yprobe\x00A11yProbe\x00")
            return None

    window = _PidWin(7, 10, 20, 180, 90, pid=0)
    root = _Root([window])
    display = _NS(
        screen=lambda: _NS(root=root),
        intern_atom=lambda name: name,
        create_resource_object=lambda kind, wid: window,
    )
    monkeypatch.setattr(_linux_system, "_display", lambda: display)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "a11yprobe" if pid == 42 else None)
    monkeypatch.setattr(_linux_system.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(_linux_system.time, "sleep", sleep)
    rows = _linux_system.windows()
    assert rows[0]["pid"] == 42
    assert rows[0]["app"] == "a11yprobe"
    assert state["sleeps"] >= 2

    state["pid"] = 0
    state["sleeps"] = 0
    clock["t"] = 0.0
    bare = _PidWin(8, 10, 20, 180, 90, pid=0)
    bare_root = _Root([bare])
    monkeypatch.setattr(
        _linux_system, "_display",
        lambda: _NS(
            screen=lambda: _NS(root=bare_root),
            intern_atom=lambda name: name,
            create_resource_object=lambda kind, wid: bare,
        ),
    )

    def never(seconds):
        state["sleeps"] += 1
        clock["t"] += seconds

    monkeypatch.setattr(_linux_system.time, "sleep", never)
    assert _linux_system.windows()[0]["pid"] == 0
    waited = state["sleeps"]
    assert waited > 0
    assert _linux_system.windows()[0]["pid"] == 0
    assert state["sleeps"] == waited
    _linux_system._pid_absent.clear()


def test_minimize_and_move_retry_until_the_window_matches(monkeypatch) -> None:
    """Synthetic X. The first client message is ignored. The next one lands."""
    window = _FakeXWin(7, 10, 20, 180, 90, pid=1)
    sends: list[str] = []
    hidden = {"value": False}

    class _Root(_FakeXRoot):
        def get_full_property(self, atom, kind):
            if atom == "_NET_ACTIVE_WINDOW":
                return _NS(value=[1])
            return super().get_full_property(atom, kind)

    root = _Root([window])
    display = _NS(
        screen=lambda: _NS(root=root),
        intern_atom=lambda name: name,
        create_resource_object=lambda kind, wid: window,
        flush=lambda: None,
    )
    monkeypatch.setattr(_linux_system, "_display", lambda: display)
    monkeypatch.setattr(_linux_system, "_is_hidden", lambda win, d: hidden["value"])
    clock = {"t": 0.0}
    monkeypatch.setattr(_linux_system.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(
        _linux_system.time, "sleep", lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
    )

    def client(_d, _win, atom, _data):
        sends.append(atom)
        if atom == "WM_CHANGE_STATE" and sends.count("WM_CHANGE_STATE") >= 2:
            hidden["value"] = True
        if atom == "_NET_MOVERESIZE_WINDOW" and sends.count("_NET_MOVERESIZE_WINDOW") >= 2:
            window.x, window.y = 300, 180

    monkeypatch.setattr(_linux_system, "_client_message", client)
    assert _linux_system.minimize_window(7) is True
    assert sends.count("WM_CHANGE_STATE") == 2
    assert hidden["value"] is True

    sends.clear()
    assert _linux_system.move_window(7, 300, 180) is True
    assert sends.count("_NET_MOVERESIZE_WINDOW") == 2
    assert (window.x, window.y) == (300, 180)

    class _BareRoot(_FakeXRoot):
        def send_event(self, event, event_mask):
            return None

    bare = _BareRoot([window])
    monkeypatch.setattr(
        _linux_system, "_display",
        lambda: _NS(
            screen=lambda: _NS(root=bare),
            intern_atom=lambda name: name,
            create_resource_object=lambda kind, wid: window,
            flush=lambda: None,
        ),
    )
    once: list[str] = []

    def client_once(_d, _win, atom, _data):
        once.append(atom)

    monkeypatch.setattr(_linux_system, "_client_message", client_once)
    assert _linux_system.minimize_window(7) is True
    assert once == ["WM_CHANGE_STATE", "_NET_WM_STATE"]


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


def test_second_launch_waits_for_the_running_apps_new_title(tmp_path, monkeypatch) -> None:
    """A hand-off that exits 0 still opened Untitled 2 in the existing window."""
    proc = _Proc(0)
    handle = {
        "pid": 222, "proc": proc, "identifier": "mousepad",
        "names": ["mousepad"], "is_launcher": False,
    }
    old = {
        "window_id": 1, "app": "mousepad", "title": "Untitled 1 - Mousepad",
        "pid": 111, "wm_class": "mousepad",
    }

    def rows(n):
        if n < 3:
            return [dict(old)]
        return [{**old, "title": "Untitled 2 - Mousepad"}]

    runtime = _launch_runtime(tmp_path, monkeypatch, "mousepad", handle, rows)
    runtime.APP_LAUNCH_WAIT_S = 5
    assert runtime.app("launch", "mousepad") == (
        "launched mousepad; first window: 'Untitled 2 - Mousepad'"
    )
    assert proc.polls >= 1


def test_exit_zero_with_no_new_window_is_still_process_exited(tmp_path, monkeypatch) -> None:
    """An existing window whose title never changes is not a successful launch."""
    import time

    proc = _Proc(0)
    handle = {
        "pid": 222, "proc": proc, "identifier": "mousepad",
        "names": ["mousepad"], "is_launcher": False,
    }
    old = {
        "window_id": 1, "app": "mousepad", "title": "Untitled 1 - Mousepad",
        "pid": 111, "wm_class": "mousepad",
    }
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(time, "sleep", lambda seconds: clock.__setitem__("t", clock["t"] + seconds))
    runtime = _launch_runtime(tmp_path, monkeypatch, "mousepad", handle, [old])
    runtime.APP_LAUNCH_WAIT_S = 1
    with pytest.raises(ComputerUseError) as exc:
        runtime.app("launch", "mousepad")
    assert exc.value.detail["reason"] == "process_exited"
    assert exc.value.detail["exit_code"] == 0
    assert "Untitled 1" not in exc.value.message


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


def _hide_xlib(monkeypatch) -> None:
    """Make ``import Xlib`` raise, including after the module was already loaded."""
    import builtins
    import sys

    real = builtins.__import__

    def blocked(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "Xlib" or name.startswith("Xlib."):
            raise ModuleNotFoundError("No module named 'Xlib'")
        return real(name, globals, locals, fromlist, level)

    for key in list(sys.modules):
        if key == "Xlib" or key.startswith("Xlib."):
            monkeypatch.delitem(sys.modules, key, raising=False)
    monkeypatch.setattr(builtins, "__import__", blocked)


def _assert_missing_xlib(exc: ComputerUseError) -> None:
    assert exc.code is ErrorCode.UNSUPPORTED
    assert exc.detail["reason"] == "missing_dependency"
    assert exc.detail["module"] == "Xlib"
    assert exc.detail["hint"] == "pip install python-xlib"
    assert "python-xlib is not installed" in exc.message
    assert exc.detail["reason"] != "no_accessibility_bridge"
    assert exc.detail["reason"] != "no_window"


def test_missing_xlib_is_not_an_empty_confirmed_list(tmp_path, monkeypatch) -> None:
    """A failed Xlib import is one typed error. It is not ``[]`` confirmed,
    and LibreOffice is not blamed for a missing gtk3 bridge after 15s.

    Synthetic. No display and no LibreOffice process. The registration
    budget stays at 15s so a wait would show up in the elapsed time.
    """
    import time

    from a11y_computer_use import safety, server
    from a11y_computer_use.doctor import _check_window_manager
    from a11y_computer_use.drivers import _atspi
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Scope

    _hide_xlib(monkeypatch)
    # The autouse fixture turns the check off when Xlib cannot be imported,
    # so the other synthetic cases keep app_not_found and the gtk3-bridge
    # error. This case is the real import path: force the check back on.
    monkeypatch.setattr(_linux_system, "xlib_required", lambda: True)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")

    with pytest.raises(ComputerUseError) as apps:
        _linux_system.running_apps()
    _assert_missing_xlib(apps.value)
    with pytest.raises(ComputerUseError) as wins:
        _linux_system.windows()
    _assert_missing_xlib(wins.value)

    doctor = _check_window_manager()
    assert doctor["ok"] is False
    assert doctor["detail"].startswith("python-xlib is not installed")
    assert "no EWMH window manager" not in doctor["detail"]
    assert doctor["fix"] == "pip install python-xlib"

    driver = LinuxDriver()
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("shell", safety.Tier.READ)
    store.set_tier("LibreOffice", safety.Tier.READ)
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "shell")

    with pytest.raises(ComputerUseError) as listed_apps:
        runtime.app("list")
    _assert_missing_xlib(listed_apps.value)
    with pytest.raises(ComputerUseError) as listed_windows:
        runtime.window("list")
    _assert_missing_xlib(listed_windows.value)

    monkeypatch.setattr(server, "_running_app", lambda ident: (None, ident))
    started = time.monotonic()
    with pytest.raises(ComputerUseError) as keyed:
        runtime.key("pagedown", app="soffice")
    assert time.monotonic() - started < 2
    _assert_missing_xlib(keyed.value)

    monkeypatch.setattr(_atspi, "find_root", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_atspi, "libreoffice_process_running", lambda: True)
    monkeypatch.setattr(_atspi, "ATSPI_REGISTER_WAIT_S", 15.0)
    monkeypatch.setattr(driver, "ensure_trusted", lambda: None)
    started = time.monotonic()
    with pytest.raises(ComputerUseError) as found:
        runtime.find("LibreOffice", text="SECOND-SLIDE")
    assert time.monotonic() - started < 2
    _assert_missing_xlib(found.value)

    started = time.monotonic()
    with pytest.raises(ComputerUseError) as snap:
        driver.snapshot(Scope.WINDOW, "LibreOffice")
    assert time.monotonic() - started < 2
    _assert_missing_xlib(snap.value)


def test_client_origin_is_the_window_inside_the_matching_frame(monkeypatch) -> None:
    """The outer frame is the client origin minus the title bar and borders."""
    from contextlib import nullcontext

    win = object()
    monkeypatch.setattr(_linux_system, "_open_display", lambda: nullcontext(object()))
    monkeypatch.setattr(_linux_system, "_managed_windows", lambda _display: [win])
    monkeypatch.setattr(_linux_system, "_geometry_on_root", lambda _win, _display: (0, 28, 1280, 772))
    monkeypatch.setattr(_linux_system, "_frame_extent_box", lambda _win, _display: (0, 0, 28, 0))
    monkeypatch.setattr(_linux_system, "_win_title", lambda _win, _display: "notes.odt — LibreOffice Writer")
    assert _linux_system.client_origin_for_outer_frame(
        0, 0, 1280, 800, title="notes.odt — LibreOffice Writer",
    ) == (0, 28)
    # Writer's frame screen height can be short of the outer frame by the title bar.
    assert _linux_system.client_origin_for_outer_frame(
        0, 0, 1280, 773, title="notes.odt — LibreOffice Writer",
    ) == (0, 28)
    assert _linux_system.client_origin_for_outer_frame(40, 40, 200, 100, title="other") is None


def _program_names(mapping: dict[int, str]):
    """cmdline and exe basenames for fake pids. Both agree."""
    def read(pid: int) -> str | None:
        return mapping.get(int(pid))

    return read


def test_full_program_name_matches_a_grant_and_does_not_widen(monkeypatch, tmp_path) -> None:
    """A 15-byte comm is not the grant key when cmdline names the program.

    ``gnome-text-editor`` and ``gnome-text-editor-nightly`` both cut to
    ``gnome-text-edit``. A grant for the editor covers that process only.
    The cut name resolves to the editor when it is the only such process,
    and resolves to neither program when both are running. ``text`` is not
    the editor. chrome's short comm is unchanged.
    """
    from a11y_computer_use.safety import PermissionStore, Tier, check_action
    from a11y_computer_use.schema import ObserveOp, ObserveVerb
    from a11y_computer_use.server import _permission_app_id, refusal_text

    class _Named(_FakeXWin):
        def __init__(self, wid: int, pid: int, title: str):
            super().__init__(wid, 0, 0, 800, 600, pid)
            self.title = title

        def get_full_property(self, atom, kind):
            if atom == "_NET_WM_NAME":
                return _NS(value=self.title.encode())
            return super().get_full_property(atom, kind)

    editor = _Named(0xA1, pid=41, title="Untitled 1")
    nightly = _Named(0xA2, pid=42, title="Nightly")
    chrome = _Named(0xA3, pid=7, title="Example - Google Chrome")

    def show(wins, active):
        _active_display(monkeypatch, wins, active)
        monkeypatch.setattr(
            _linux_system, "_comm_for_pid",
            lambda pid: {41: "gnome-text-edit", 42: "gnome-text-edit", 7: "chrome"}.get(int(pid)),
        )

    names = {41: "gnome-text-editor", 42: "gnome-text-editor-nightly", 7: "chrome"}
    read = _program_names(names)
    monkeypatch.setattr(_linux_system, "_cmdline_basename", read)
    monkeypatch.setattr(_linux_system, "_exe_basename", read)

    show([editor], 0xA1)
    assert _linux_system._identity_for_pid(41, "gnome-text-edit") == "gnome-text-editor"
    assert _linux_system.resolve_app("gnome-text-editor") == "gnome-text-editor"
    assert _linux_system.resolve_app("gnome-text-edit") == "gnome-text-editor"
    assert _linux_system.resolve_app("text") == "text"
    assert _linux_system.pids_matching("text") == set()
    assert _linux_system.pids_matching("gnome-text-editor") == {41}
    assert _linux_system.pids_matching("gnome-text-editor-nightly") == set()
    assert _linux_system.frontmost_app_id() == "gnome-text-editor"
    assert _linux_system.running_apps()[0]["bundle_id"] == "gnome-text-editor"
    assert _linux_system.windows()[0]["app"] == "gnome-text-editor"
    monkeypatch.setattr("a11y_computer_use.server.sys.platform", "linux")
    assert _permission_app_id("gnome-text-editor") == "gnome-text-editor"

    store = PermissionStore(tmp_path / "permissions.json")
    store.set_tier("gnome-text-editor", Tier.FULL)
    decision = check_action(
        ObserveOp(verb=ObserveVerb.SNAPSHOT, app="gnome-text-editor"),
        _linux_system.resolve_app("gnome-text-editor"),
        store=store,
    )
    assert decision.verdict.value == "allow"
    assert decision.app == "gnome-text-editor"
    refused = check_action(
        ObserveOp(verb=ObserveVerb.SNAPSHOT, app="gnome-text-editor-nightly"),
        "gnome-text-editor-nightly",
        store=store,
    )
    assert refused.verdict.value == "needs_permission"
    assert refused.app == "gnome-text-editor-nightly"
    assert refused.reason.startswith("gnome-text-editor-nightly ")
    hint = refusal_text(refused)
    assert "grant_app(app='gnome-text-editor-nightly'" in hint
    assert "grant_app(app='gnome-text-edit'" not in hint

    show([editor, nightly], 0xA1)
    assert _linux_system.resolve_app("gnome-text-editor") == "gnome-text-editor"
    assert _linux_system.resolve_app("gnome-text-editor-nightly") == "gnome-text-editor-nightly"
    assert _linux_system.resolve_app("gnome-text-edit") == "gnome-text-edit"
    assert _linux_system.pids_matching("gnome-text-editor") == {41}
    assert _linux_system.pids_matching("gnome-text-editor-nightly") == {42}
    assert _linux_system.pids_matching("gnome-text-edit") == {41, 42}

    show([chrome], 0xA3)
    assert _linux_system.resolve_app("google-chrome") == "chrome"
    assert _linux_system.resolve_app("chrome") == "chrome"


def test_disagreeing_cmdline_and_exe_do_not_pick_a_program(monkeypatch) -> None:
    """Two longer names for one comm stay the comm. Neither grant is implied."""
    class _Named(_FakeXWin):
        def __init__(self, wid: int, pid: int, title: str):
            super().__init__(wid, 0, 0, 400, 300, pid)
            self.title = title

        def get_full_property(self, atom, kind):
            if atom == "_NET_WM_NAME":
                return _NS(value=self.title.encode())
            return super().get_full_property(atom, kind)

    win = _Named(0xB1, pid=55, title="Editor")
    _active_display(monkeypatch, [win], 0xB1)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "gnome-text-edit")
    monkeypatch.setattr(_linux_system, "_cmdline_basename", lambda pid: "gnome-text-editor")
    monkeypatch.setattr(_linux_system, "_exe_basename", lambda pid: "gnome-text-editor-nightly")
    assert _linux_system._identity_for_pid(55, "gnome-text-edit") == "gnome-text-edit"
    assert _linux_system.resolve_app("gnome-text-editor") == "gnome-text-edit"
    assert _linux_system.frontmost_app_id() == "gnome-text-edit"


@pytest.mark.parametrize("parent_app", ["chrome", "ungranted"])
def test_portal_dialog_inherits_only_explicit_parent_grant(monkeypatch, tmp_path, parent_app):
    from a11y_computer_use.safety import PermissionStore, Tier, check_action
    from a11y_computer_use.schema import WindowOp, WindowVerb

    class Portal(_ClassWin):
        def get_full_property(self, atom, kind):
            if atom == "WM_TRANSIENT_FOR":
                return _NS(value=[0x20])
            return super().get_full_property(atom, kind)

    parent = _ClassWin(0x20, pid=200, title="Browser")
    portal = Portal(0x30, pid=300, title="Open File")
    _active_display(monkeypatch, [parent, portal], 0x30)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: {200: parent_app, 300: "xdg-desktop-por"}.get(pid))
    monkeypatch.setattr(_linux_system, "_cmdline_basename", lambda pid: "xdg-desktop-portal-gtk" if pid == 300 else None)
    monkeypatch.setattr(_linux_system, "_exe_basename", lambda pid: "xdg-desktop-portal-gtk" if pid == 300 else None)
    monkeypatch.setenv("HOME", str(tmp_path))
    store = PermissionStore()
    store.set_tier("chrome", Tier.FULL)
    assert _linux_system.frontmost_app_id() == parent_app
    assert _linux_system.window_owner(0x30) == parent_app
    assert _linux_system.active_window()["app"] == parent_app
    assert _linux_system.app_at_point_id(20, 20) == parent_app
    decision = check_action(WindowOp(verb=WindowVerb.LIST), _linux_system.frontmost_app_id(), store=store)
    assert decision.allowed is (parent_app == "chrome")
    # Sharing a backend PID must never authorize all of its dialogs.
    assert 300 not in _linux_system.pids_matching(parent_app)
    # A different program sharing the truncated name must not inherit grants.
    monkeypatch.setattr(_linux_system, "_cmdline_basename", lambda pid: "xdg-desktop-portal-impostor")
    monkeypatch.setattr(_linux_system, "_exe_basename", lambda pid: "xdg-desktop-portal-impostor")
    assert _linux_system.window_owner(0x30) == "xdg-desktop-portal-impostor"


def test_portal_without_managed_parent_keeps_own_identity(monkeypatch):
    portal = _ClassWin(0x30, pid=300, title="Open File")
    _active_display(monkeypatch, [portal], 0x30)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "xdg-desktop-por")
    monkeypatch.setattr(_linux_system, "_cmdline_basename", lambda pid: "xdg-desktop-portal-gtk")
    monkeypatch.setattr(_linux_system, "_exe_basename", lambda pid: "xdg-desktop-portal-gtk")
    assert _linux_system.frontmost_app_id() == "xdg-desktop-portal-gtk"


def test_cover_rows_read_stacking_once_without_process_identity(fake_ewmh, monkeypatch):
    """#718: snapshot cover evidence needs geometry/PID, not full app discovery."""
    original = _linux_system._managed_windows
    calls = []

    def managed(display):
        calls.append(display)
        return original(display)

    def unexpected(*args, **kwargs):
        pytest.fail("cover scans must not resolve apps or wait for PID settlement")

    monkeypatch.setattr(_linux_system, "_managed_windows", managed)
    monkeypatch.setattr(_linux_system, "_app_id", unexpected)
    monkeypatch.setattr(_linux_system, "_settled_pid", unexpected)
    rows = _linux_system.window_cover_rows()
    assert calls == [fake_ewmh]
    assert [row["window_id"] for row in rows] == [0x10, 0x20]
    assert rows[1]["pid"] == 200
    assert rows[1]["bounds"] == {"x": 158, "y": 50, "width": 400, "height": 200}
    assert _linux_system.window_id_for_toplevel(
        title="", pid=200, extents=(158, 50, 400, 200), rows=rows,
    ) == 0x20
    assert calls == [fake_ewmh]


@pytest.mark.parametrize("missing", ["hidden", "geometry", "display"])
def test_cover_rows_omit_unavailable_window_evidence(fake_ewmh, monkeypatch, missing):
    if missing == "hidden":
        monkeypatch.setattr(_linux_system, "_is_hidden", lambda win, d: win.id == 0x20)
    elif missing == "geometry":
        original = _linux_system._geometry_on_root
        monkeypatch.setattr(_linux_system, "_geometry_on_root", lambda win, d: (
            None if win.id == 0x20 else original(win, d)
        ))
    else:
        def unavailable():
            raise RuntimeError("no X11")
        monkeypatch.setattr(_linux_system, "_display", unavailable)
    rows = _linux_system.window_cover_rows()
    assert [row["window_id"] for row in rows] == ([] if missing == "display" else [0x10])


@pytest.mark.parametrize("verb", ["focus_window", "raise_window", "activate_app"])
@pytest.mark.parametrize("wm_restores", [True, False])
def test_shaded_activation_requires_mapped_unshaded_client(monkeypatch, verb, wm_restores):
    """An active Xfce title bar is not evidence that the client accepts input."""
    target = _FakeXWin(7, 0, 0, 100, 100, pid=2)
    _active_display(monkeypatch, [target], 7)
    state = {"shaded": True, "mapped": False, "time": 0.0}
    original = target.get_full_property
    target.get_full_property = lambda atom, kind: (
        _NS(value=["_NET_WM_STATE_SHADED"] if state["shaded"] else [])
        if atom == "_NET_WM_STATE" else original(atom, kind)
    )
    target.get_attributes = lambda: _NS(map_state=2 if state["mapped"] else 0)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "editor")
    monkeypatch.setattr(_linux_system, "_send_active_window", lambda *args: None)
    sent = []

    def message(d, win, atom, data):
        sent.append((atom, data))
        if wm_restores and data == [0, "_NET_WM_STATE_SHADED", 0, 1, 0]:
            state["shaded"] = False

    def sleep(seconds):
        state["time"] += seconds
        # Mapping can lag behind removal of the shade property.
        if wm_restores and not state["shaded"]:
            state["mapped"] = True

    monkeypatch.setattr(_linux_system, "_client_message", message)
    monkeypatch.setattr(_linux_system.time, "monotonic", lambda: state["time"])
    monkeypatch.setattr(_linux_system.time, "sleep", sleep)
    call = lambda: getattr(_linux_system, verb)("editor" if verb == "activate_app" else 7)
    if wm_restores:
        assert call()
        assert state["mapped"] and not state["shaded"]
        assert state["time"] >= 2 * _linux_system._ACTIVATE_POLL_S
    else:
        with pytest.raises(ComputerUseError) as exc:
            call()
        assert exc.value.detail["reason"] == "window_shaded"
    assert ("_NET_WM_STATE", [0, "_NET_WM_STATE_SHADED", 0, 1, 0]) in sent


@pytest.mark.parametrize("state,reason", [("shaded", "window_shaded"), ("unmapped", "window_unmapped")])
@pytest.mark.parametrize("method", ["type_text", "key_chord"])
def test_input_to_active_hidden_client_is_refused(monkeypatch, state, reason, method):
    from a11y_computer_use.drivers import linux, _linux_input, _atspi

    target = _FakeXWin(7, 0, 0, 100, 100, pid=2)
    _active_display(monkeypatch, [target], 7)
    original = target.get_full_property
    target.get_full_property = lambda atom, kind: (
        _NS(value=["_NET_WM_STATE_SHADED"] if state == "shaded" else [])
        if atom == "_NET_WM_STATE" else original(atom, kind)
    )
    target.get_attributes = lambda: _NS(map_state=0)
    monkeypatch.setattr(linux, "_on_wayland", lambda: False)
    def unexpected(*args, **kwargs):
        pytest.fail("hidden client must not receive text or keys")
    monkeypatch.setattr(_linux_input, "type_string", unexpected)
    monkeypatch.setattr(_linux_input, "press_chord", unexpected)
    monkeypatch.setattr(_atspi, "insert_text", unexpected)
    with pytest.raises(ComputerUseError) as exc:
        getattr(linux.LinuxDriver(), method)("x")
    assert exc.value.detail["reason"] == reason
    assert exc.value.detail["window_id"] == 7


def test_active_atspi_ref_does_not_bypass_shaded_guard(monkeypatch):
    from a11y_computer_use.drivers import linux, _atspi

    driver = linux.LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(linux, "_on_wayland", lambda: False)
    monkeypatch.setattr(_atspi, "toplevel_raise_target", lambda handle: (True, "editor", 2, None))
    monkeypatch.setattr(_linux_system, "window_id_for_toplevel", lambda **kwargs: 7)
    def refuse(window_id):
        assert window_id == 7
        raise _linux_system._activation_error(_NS(id=7), "window_shaded")
    monkeypatch.setattr(_linux_system, "refuse_unviewable_input", refuse)
    with pytest.raises(ComputerUseError, match="window_shaded"):
        driver._raise_handle_window(object())


@pytest.mark.parametrize("visibility", ["unmapped", "unreadable"])
def test_focus_cannot_confirm_when_shade_clears_without_visible_client(monkeypatch, visibility):
    target = _FakeXWin(7, 0, 0, 100, 100, pid=2)
    _active_display(monkeypatch, [target], 7)
    def attributes():
        if visibility == "unreadable":
            raise RuntimeError("client disappeared")
        return _NS(map_state=0)
    target.get_attributes = attributes
    clock = [0.0]
    monkeypatch.setattr(_linux_system, "_send_active_window", lambda *args: None)
    monkeypatch.setattr(_linux_system.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(_linux_system.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    with pytest.raises(ComputerUseError) as exc:
        _linux_system.focus_window(7)
    assert exc.value.detail["reason"] == (
        "window_unmapped" if visibility == "unmapped" else "window_visibility_unverified"
    )


@pytest.mark.parametrize("app", ["xfce4-panel", "gnome-panel", "gnome-shell", "plasmashell"])
@pytest.mark.parametrize("identity", ["class", "role", "dialog_title", "title_only", "ordinary"])
def test_command_launcher_window_identity(monkeypatch, app, identity):
    class RunWindow(_ClassWin):
        def get_full_property(self, atom, kind):
            if atom == "WM_WINDOW_ROLE" and identity == "role":
                return _NS(value=b"run-dialog\x00")
            if atom == "_NET_WM_WINDOW_TYPE" and identity == "dialog_title":
                return _NS(value=["_NET_WM_WINDOW_TYPE_DIALOG"])
            return super().get_full_property(atom, kind)

    win = RunWindow(20, 200, instance=app,
                    klass="KRunner" if identity == "class" else app,
                    title="Run Program" if identity in {"title_only", "dialog_title"} else "Localized title")
    _active_display(monkeypatch, [win], 20)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: app)
    assert _linux_system.command_launcher_open(app) is (identity in {"class", "role", "dialog_title"})
    assert not _linux_system.command_launcher_open("mousepad")


@pytest.mark.parametrize("window_type", ["NOTIFICATION", "NORMAL", "DIALOG", None, "unreadable"])
def test_ref_notification_preparation_does_not_wait_for_activation(monkeypatch, window_type):
    """#935: a native notification type bypasses only implicit activation."""
    from a11y_computer_use.drivers import linux, _atspi

    target = _FakeXWin(7, 10, 20, 200, 100, pid=2)
    _active_display(monkeypatch, [target], 4)
    original = target.get_full_property

    def property_value(atom, kind):
        if atom == "_NET_WM_WINDOW_TYPE":
            if window_type == "unreadable":
                raise RuntimeError("property unavailable")
            return None if window_type is None else _NS(value=[f"_NET_WM_WINDOW_TYPE_{window_type}"])
        return original(atom, kind)

    target.get_full_property = property_value
    driver = linux.LinuxDriver()
    monkeypatch.setattr(linux, "_on_wayland", lambda: False)
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(_atspi, "toplevel_raise_target", lambda handle: (
        False, "Bubble", 2, (10, 20, 200, 100),
    ))
    monkeypatch.setattr(_linux_system, "window_id_for_toplevel", lambda **kwargs: 7)
    monkeypatch.setattr(_linux_system, "active_window", lambda: {"window_id": 4})
    activated = []
    monkeypatch.setattr(_linux_system, "_activate_and_settle", lambda d, win, **kwargs: activated.append(win.id))
    driver._raise_handle_window(object())
    assert activated == ([] if window_type == "NOTIFICATION" else [7])

    # Explicit focus still requires activation; the shortcut claims no focus.
    activated.clear()
    assert _linux_system.focus_window(7)
    assert activated == [7]


@pytest.mark.parametrize("state, reason", [
    ("shaded", "window_shaded"), ("unmapped", "window_unmapped"),
])
def test_notification_preparation_preserves_visibility_refusal(monkeypatch, state, reason):
    from a11y_computer_use.drivers import linux, _atspi

    target = _FakeXWin(7, 10, 20, 200, 100, pid=2)
    _active_display(monkeypatch, [target], 4)
    target.get_full_property = lambda atom, kind: _NS(value={
        "_NET_WM_WINDOW_TYPE": ["_NET_WM_WINDOW_TYPE_NOTIFICATION"],
        "_NET_WM_STATE": ["_NET_WM_STATE_SHADED"] if state == "shaded" else [],
    }.get(atom, []))
    target.get_attributes = lambda: _NS(map_state=0 if state == "unmapped" else 2)
    driver = linux.LinuxDriver()
    monkeypatch.setattr(linux, "_on_wayland", lambda: False)
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(_atspi, "toplevel_raise_target", lambda handle: (
        False, "Bubble", 2, (10, 20, 200, 100),
    ))
    monkeypatch.setattr(_linux_system, "window_id_for_toplevel", lambda **kwargs: 7)
    with pytest.raises(ComputerUseError) as exc:
        driver._raise_handle_window(object())
    assert exc.value.detail["reason"] == reason


@pytest.mark.parametrize("blocker", [None, "window_unmapped", "window_shaded", "missing"])
def test_input_activation_requests_once_without_restoring_or_waiting(monkeypatch, blocker):
    from contextlib import contextmanager

    display, window = object(), object()
    calls = []

    @contextmanager
    def with_window(window_id):
        assert window_id == 7
        yield display, None if blocker == "missing" else window

    monkeypatch.setattr(_linux_system, "_with_window", with_window)
    monkeypatch.setattr(_linux_system, "_activation_blocker", lambda d, w: blocker)
    monkeypatch.setattr(_linux_system, "_send_active_window",
                        lambda d, w: calls.append((d, w)))
    monkeypatch.setattr(_linux_system, "_restore_if_hidden",
                        lambda *args: pytest.fail("must not restore hidden windows"))
    monkeypatch.setattr(_linux_system.time, "sleep",
                        lambda *args: pytest.fail("must not extend the focus wait"))
    assert _linux_system.request_input_activation(7) is (blocker is None)
    assert calls == ([(display, window)] if blocker is None else [])


@pytest.mark.parametrize('cover', ['overlay', 'managed', 'hidden', 'input_only', 'unknown'])
def test_drag_overlay_uses_root_stacking_not_managed_clients(monkeypatch, cover):
    """Observed Xfwm sequence: Mousepad below two screenshooter OR windows."""
    import sys
    monkeypatch.setitem(sys.modules, 'Xlib', _NS(X=_NS(IsViewable=2, InputOutput=1)))
    background = _FakeXWin(1, 0, 0, 1280, 800, 10)
    overlay = _FakeXWin(2, 0, 0, 1280, 800, 20)
    utility = _FakeXWin(3, 0, 0, 100, 50, 20)
    background.get_attributes = lambda: _NS(map_state=2, win_class=1, override_redirect=False)
    overlay.get_attributes = lambda: _NS(
        map_state=0 if cover == 'hidden' else 2,
        win_class=2 if cover == 'input_only' else 1,
        override_redirect=cover != 'managed')
    utility.get_attributes = lambda: _NS(map_state=2, win_class=1, override_redirect=True)
    children = [background, overlay, utility]
    root = _FakeXRoot([background])  # EWMH omits both overlay windows.
    root.get_geometry = lambda: _NS(width=1280, height=800)
    root.query_tree = lambda: _NS(children=children)
    display = _NS(screen=lambda: _NS(root=root))
    monkeypatch.setattr(_linux_system, '_display', lambda: display)
    monkeypatch.setattr(_linux_system, '_app_id', lambda win, d:
                        '' if cover == 'unknown' else 'screenshooter')
    expected = (2, 'unknown' if cover == 'unknown' else 'screenshooter')
    assert _linux_system.drag_overlay_at_point(200, 150) == (
        expected if cover in {'overlay', 'unknown'} else None)
    assert _linux_system.drag_overlay_at_point(20, 20) == (
        expected if cover in {'overlay', 'unknown'} else None)  # inside the utility
    # After release the overlay is unmapped; do not infer another owner.
    children[:] = [background]
    assert _linux_system.drag_overlay_at_point(200, 150) is None


def test_drag_overlay_unreadable_window_does_not_use_background(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'Xlib', _NS(X=_NS(IsViewable=2, InputOutput=1)))
    def gone():
        raise RuntimeError('window disappeared')
    root = _NS(get_geometry=lambda: _NS(width=1280, height=800),
               query_tree=lambda: _NS(children=[_NS(get_attributes=gone)]))
    monkeypatch.setattr(_linux_system, '_display', lambda: _NS(screen=lambda: _NS(root=root)))
    with pytest.raises(ComputerUseError) as exc:
        _linux_system.drag_overlay_at_point(200, 150)
    assert exc.value.code is ErrorCode.FOCUS_CHANGED
    assert exc.value.detail['reason'] == 'drag_overlay_unreadable'


@pytest.mark.parametrize("exit_kind", ["normal", "SIGTERM", "SIGKILL", "SIGSEGV"])
def test_launched_child_is_reaped_without_another_launch(exit_kind) -> None:
    """Real child processes need no desktop or native accessibility service."""
    import os
    import signal
    import sys
    import time

    if not sys.platform.startswith("linux"):
        pytest.skip("Linux child reaping regression")

    script = (
        "import resource, time; "
        "resource.setrlimit(resource.RLIMIT_CORE, (0, 0)); "
        + ("time.sleep(0.1); raise SystemExit(7)" if exit_kind == "normal"
           else "time.sleep(60)")
    )
    handle = _linux_system.launch_app(sys.executable, argv=(sys.executable, "-c", script))
    proc = handle["proc"]
    try:
        if exit_kind != "normal":
            os.kill(proc.pid, getattr(signal, exit_kind))
        deadline = time.monotonic() + 5
        # Do not call poll/wait: either would hide the original zombie bug.
        while proc.returncode is None and time.monotonic() < deadline:
            time.sleep(0.01)
        expected = 7 if exit_kind == "normal" else -getattr(signal, exit_kind)
        assert proc.returncode == expected
        with pytest.raises(ProcessLookupError):
            os.kill(proc.pid, 0)
        assert proc.poll() == expected
    finally:
        if proc.returncode is None:
            proc.kill()
        proc.wait()


@pytest.mark.parametrize("change", [None, "normal", "no-parent", "inactive", "foreign", "duplicate", "fixed-parent"])
def test_active_transient_dialog_requires_exact_native_relationship(monkeypatch, change):
    parent = _ClassWin(0x21, 200, title="Parent")
    child = _ClassWin(0x22, 200, title="Settings Modal")
    parent_original = parent.get_full_property
    parent.get_full_property = lambda atom, kind: (
        _NS(value=[] if change == "fixed-parent" else ["_NET_WM_ACTION_RESIZE"])
        if atom == "_NET_WM_ALLOWED_ACTIONS" else parent_original(atom, kind))
    original = child.get_full_property
    def prop(atom, kind):
        if atom == "_NET_WM_WINDOW_TYPE":
            return _NS(value=["normal" if change == "normal" else "_NET_WM_WINDOW_TYPE_DIALOG"])
        if atom == "WM_TRANSIENT_FOR":
            return None if change == "no-parent" else _NS(value=[parent.id])
        if atom == "_NET_WM_PID" and change == "foreign":
            return _NS(value=[999])
        return original(atom, kind)
    child.get_full_property = prop
    wins = [parent, child]
    if change == "duplicate":
        wins.append(_ClassWin(0x23, 200, title="Parent"))
    _active_display(monkeypatch, wins, parent.id if change == "inactive" else child.id)
    assert _linux_system.active_transient_dialog(
        pid=200, title="Settings Modal", parent_title="Parent") is (change is None)


@pytest.mark.parametrize("change", [None, "no-parent", "foreign", "duplicate-child", "duplicate-parent", "self"])
def test_transient_parent_resolves_inactive_dialog_without_geometry(monkeypatch, change):
    parent = _ClassWin(0x21, 200, title="Document B")
    child = _ClassWin(0x22, 200, title="Go To")
    other = _ClassWin(0x23, 200, title="Document A")
    original = child.get_full_property
    child.get_full_property = lambda atom, kind: (
        None if change == "no-parent" else _NS(value=[child.id if change == "self" else parent.id])
    ) if atom == "WM_TRANSIENT_FOR" else original(atom, kind)
    if change == "foreign":
        parent = _ClassWin(0x21, 999, title="Document B")
    wins = [parent, child, other]
    if change in {"duplicate-child", "duplicate-parent"}:
        wins.append(_ClassWin(0x24, 200, title="Go To" if change == "duplicate-child" else "Document B"))
    _active_display(monkeypatch, wins, other.id)
    assert _linux_system.transient_parent_title(pid=200, title="Go To") == (
        "Document B" if change is None else None)


@pytest.mark.parametrize("change", [None, "not-modal", "no-parent", "other-parent", "foreign",
                                    "duplicate-parent", "unmapped", "hidden"])
def test_transient_modal_windows_require_modal_state_and_exact_parent(monkeypatch, change):
    """#1146: an X11-only modal names one same-process parent and says MODAL."""
    parent = _ClassWin(0x31, 200, title="Msgbox Parent")
    other = _ClassWin(0x32, 200, title="Other Window")
    child = _ClassWin(0x33, 300 if change == "foreign" else 200, title="Confirm Change")
    original = child.get_full_property

    def prop(atom, kind):
        if atom == "_NET_WM_STATE":
            states = ["_NET_WM_STATE_SKIP_TASKBAR"]
            if change != "not-modal":
                states.append("_NET_WM_STATE_MODAL")
            if change == "hidden":
                states.append("_NET_WM_STATE_HIDDEN")
            return _NS(value=states)
        if atom == "_NET_WM_WINDOW_TYPE":
            return _NS(value=["_NET_WM_WINDOW_TYPE_DIALOG"])
        if atom == "WM_TRANSIENT_FOR":
            if change == "no-parent":
                return None
            return _NS(value=[other.id if change == "other-parent" else parent.id])
        return original(atom, kind)

    child.get_full_property = prop
    if change == "unmapped":
        child.get_attributes = lambda: _NS(map_state=0)
    wins = [parent, other, child]
    if change == "duplicate-parent":
        wins.append(_ClassWin(0x34, 200, title="Msgbox Parent"))
    _active_display(monkeypatch, wins, child.id)
    found = _linux_system.transient_modal_windows(pid=200, parent_title="Msgbox Parent")
    if change is None:
        assert [(w["window_id"], w["title"]) for w in found] == [(child.id, "Confirm Change")]
        assert found[0]["bounds"]["width"] == 200
    else:
        assert found == []
    # Only the exact transient parent is blocked (#1139/#1140 scoping).
    sibling = _linux_system.transient_modal_windows(pid=200, parent_title="Other Window")
    assert [w["title"] for w in sibling] == (["Confirm Change"] if change == "other-parent" else [])


def test_transient_modal_windows_fail_open_without_display(monkeypatch):
    def broken():
        raise OSError("no display")
    monkeypatch.setattr(_linux_system, "_display", broken)
    assert _linux_system.transient_modal_windows(pid=200, parent_title="Parent") == []
    assert _linux_system.transient_modal_windows(pid=0, parent_title="Parent") == []


@pytest.mark.parametrize("needle,name,expected", [
    ("python", "python", True),
    ("python", "python3", False),
    ("firefox", "firefox-esr", False),
    ("code", "code-insiders", False),
    ("gimp", "gimp-2.10", False),
    ("soffice", "soffice.bin", False),
    ("chrome", "google chrome", True),
    ("terminal", "xfce4-terminal", True),
    ("python3", "python", False),
])
def test_names_program_refuses_longer_program_with_same_prefix(needle, name, expected) -> None:
    """#1191: ``python`` must never name the different program ``python3``."""
    from a11y_computer_use.drivers import _linux_system

    assert _linux_system._names_program(needle, name) is expected


@pytest.mark.parametrize("other", ["python3", "python3.12"])
def test_exited_or_frozen_app_does_not_match_prefix_neighbour(other) -> None:
    """#1191: with ``python`` gone, its id must not resolve to ``python3``'s process."""
    from a11y_computer_use.drivers import _linux_system

    assert not _linux_system._identifier_matches_process("python", other)
    assert not _linux_system._comm_matches_identifier("python", other)
    assert _linux_system._identifier_matches_process("python", "python")
    assert _linux_system._comm_matches_identifier("python", "python")
    # LibreOffice keeps resolving through its alias group, not the prefix.
    assert _linux_system._comm_matches_identifier("soffice", "soffice.bin")
