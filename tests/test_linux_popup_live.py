"""#1227 live: the IBus candidate window is a popup the on-top check can see.

Skipped unless Chrome, xdotool, ``ibus`` with ``libpinyin`` and the ``ibus-ui-gtk3``
panel (whose candidate window is an override-redirect window) are present.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")

from a11y_computer_use.schema import ComputerUseError, ErrorCode, Scope  # noqa: E402
from tests.test_linux_live import _chrome_binary, _runtime_for, _stop_group  # noqa: E402

_PAGE = (
    "<!doctype html><meta charset=utf-8><title>cuapopup</title>"
    "<input id=f aria-label='Name field'>"
)
_PANEL = "ibus-ui-gtk3"


def _ibus(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ibus", *args], capture_output=True, text=True, timeout=5, check=False)


def _wait_for(check, seconds: float = 8.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.1)
    return check()


@pytest.fixture
def composing(tmp_path):
    from a11y_computer_use.drivers.linux import LinuxDriver

    for tool in ("ibus", "xdotool"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not installed")
    binary = _chrome_binary()
    if binary is None:
        pytest.skip("no Chrome")
    before = _ibus("engine")
    original = before.stdout.strip()
    if before.returncode != 0 or not original or " " in original:
        pytest.skip("no running IBus daemon with an engine set")
    _ibus("engine", "libpinyin")
    if _ibus("engine").stdout.strip() != "libpinyin":
        _ibus("engine", original)
        pytest.skip("IBus cannot load libpinyin")
    page = tmp_path / "popup.html"
    page.write_text(_PAGE)
    profile = tmp_path / "profile"
    profile.mkdir()
    env = dict(os.environ, XMODIFIERS="@im=ibus", GTK_IM_MODULE="ibus", QT_IM_MODULE="ibus")
    proc = subprocess.Popen(
        [
            binary, "--force-renderer-accessibility", "--no-sandbox", "--disable-gpu",
            "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
            f"--user-data-dir={profile}", "--window-size=1000,500", f"file://{page}",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=env,
    )
    driver = LinuxDriver()
    try:
        field = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and field is None:
            try:
                shot = driver.snapshot(Scope.WINDOW, "chrome")
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
            else:
                field = next((e for e in shot.elements if e.title == "Name field"), None)
            time.sleep(0.5)
        assert field is not None, "Chrome never exposed the field"
        runtime = _runtime_for(tmp_path, driver, "chrome", "google-chrome", _PANEL)
        runtime._current = shot
        runtime.click(field.ref)
        subprocess.run(["xdotool", "type", "--delay", "80", "nihao"], check=True, timeout=10)
        yield driver, runtime
    finally:
        subprocess.run(["xdotool", "key", "Escape"], check=False, timeout=5)
        _stop_group(proc)
        _ibus("engine", original)


def _candidate(driver, text: str):
    try:
        snap = driver.snapshot(Scope.APP, _PANEL)
    except ComputerUseError:
        return None
    found = next((e for e in snap.elements if e.title == text), None)
    return (snap, found) if found is not None else None


def test_the_candidate_popup_is_listed_and_clickable_by_ref(composing) -> None:
    driver, runtime = composing
    found = _wait_for(lambda: _candidate(driver, "你好"))
    if not found:
        pytest.skip("the IBus panel shows no candidate window (is ibus-ui-gtk3 the panel?)")
    snap, candidate = found
    assert _PANEL in [app["name"] for app in driver.running_apps()]
    runtime._current = snap
    runtime.click(candidate.ref)  # was refused: "covered by chrome"
    value = _wait_for(lambda: next(
        (e.value for e in driver.snapshot(Scope.WINDOW, "chrome").elements
         if e.title == "Name field" and e.value == "你好"), None))
    assert value == "你好"  # the click picked the candidate


_GTK_ENTRY = """
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
w = Gtk.Window(title="cuapopup entry")
w.set_default_size({w}, {h})
w.move(0, 0)
w.add(Gtk.Entry())
w.connect("destroy", Gtk.main_quit)
w.show_all()
Gtk.main()
"""
_GTK_UNDER = _GTK_ENTRY.replace("cuapopup entry", "cuapopup under").replace("Gtk.Entry()", "Gtk.Label(label='under')")


def test_the_candidate_is_clickable_when_another_managed_window_sits_under_it(tmp_path) -> None:
    """The popup's own window is unmanaged; two managed windows overlap it (#1227).

    The overlap guess for "the managed window that owns this toplevel" picks
    the window underneath, which is not the topmost managed window at the
    point, so the cover check called the popup covered, and the raise buried
    it and dropped the composition.
    """
    from a11y_computer_use.drivers import _linux_system
    from a11y_computer_use.drivers.linux import LinuxDriver

    for tool in ("ibus", "xdotool"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not installed")
    python = shutil.which("python3")
    try:
        subprocess.run([python, "-c", "import gi; gi.require_version('Gtk','3.0')"],
                       check=True, capture_output=True, timeout=20)
    except Exception:
        pytest.skip("no PyGObject GTK3")
    before = _ibus("engine")
    original = before.stdout.strip()
    if before.returncode != 0 or not original or " " in original:
        pytest.skip("no running IBus daemon with an engine set")
    _ibus("engine", "libpinyin")
    if _ibus("engine").stdout.strip() != "libpinyin":
        _ibus("engine", original)
        pytest.skip("IBus cannot load libpinyin")
    under_app = tmp_path / "cuaunder"
    under_app.symlink_to(python)  # a second application name for the window below
    (tmp_path / "entry.py").write_text(_GTK_ENTRY.format(w=1000, h=790))
    (tmp_path / "under.py").write_text(_GTK_UNDER.format(w=1270, h=795))
    env = dict(os.environ, XMODIFIERS="@im=ibus", GTK_IM_MODULE="ibus", QT_IM_MODULE="ibus")
    procs = []
    driver = LinuxDriver()
    try:
        for cmd in ([str(under_app), str(tmp_path / "under.py")], [python, str(tmp_path / "entry.py")]):
            procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          start_new_session=True, env=env))
            time.sleep(1.5)
        app = os.path.basename(python)
        field = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and field is None:
            try:
                shot = driver.snapshot(Scope.WINDOW, app)
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
            else:
                field = next((e for e in shot.elements if e.editable), None)
            time.sleep(0.5)
        assert field is not None, "the GTK3 entry never appeared"
        runtime = _runtime_for(tmp_path, driver, app, "cuaunder", _PANEL)
        runtime._current = shot
        runtime.click(field.ref)
        subprocess.run(["xdotool", "type", "--delay", "80", "nihao"], check=True, timeout=10)
        found = _wait_for(lambda: _candidate(driver, "你好"))
        if not found:
            pytest.skip("the IBus panel shows no candidate window")
        snap, candidate = found
        b = candidate.bounds
        point = (b.x + b.width / 2, b.y + b.height / 2)
        front = _linux_system.window_at_point(*point)
        if front is None or front["title"] != "cuapopup entry":
            pytest.skip("the candidate window did not land over the entry window")
        runtime._current = snap
        runtime.click(candidate.ref)  # was refused "covered by cuapopup entry", then buried
        value = _wait_for(lambda: next(
            (e.value for e in driver.snapshot(Scope.WINDOW, app).elements
             if e.editable and e.value == "你好"), None))
        assert value == "你好"
    finally:
        subprocess.run(["xdotool", "key", "Escape"], check=False, timeout=5)
        for proc in procs:
            _stop_group(proc)
        _ibus("engine", original)
