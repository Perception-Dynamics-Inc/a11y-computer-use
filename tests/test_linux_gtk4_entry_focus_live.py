"""#1237 live: type(ref) into an unfocused GTK 4 Gtk.Entry.

GTK 4 does not implement AT-SPI Component.GrabFocus, so the ref path used to
refuse with focus_lost. Skipped unless there is an X display with AT-SPI and
PyGObject with GTK 4.
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
from tests.test_linux_live import _runtime_for, _stop_group  # noqa: E402

_APP = r'''
import gi, sys
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk
log = open(sys.argv[1], "a")
def act(app):
    w = Gtk.ApplicationWindow(application=app, title="Entry Focus Lab")
    b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL); w.set_child(b)
    e = Gtk.Entry(); e.set_text("keep me"); b.append(e)
    e.connect("activate", lambda _e: (log.write("ACTIVATE\n"), log.flush()))
    bt = Gtk.Button(label="Save"); b.append(bt)
    bt.connect("clicked", lambda _b: (log.write("SAVE\n"), log.flush()))
    w.set_default_size(420, 160); w.present(); bt.grab_focus()
a = Gtk.Application(application_id="org.a11y.test1237"); a.connect("activate", act); a.run(None)
'''


@pytest.fixture
def lab(tmp_path):
    from a11y_computer_use.drivers.linux import LinuxDriver

    if not os.environ.get("DISPLAY"):
        pytest.skip("no X display")
    python = shutil.which("python3")
    try:
        subprocess.run([python, "-c", "import gi; gi.require_version('Gtk','4.0')"],
                       check=True, capture_output=True, timeout=20)
    except Exception:
        pytest.skip("no PyGObject GTK 4")
    script, log = tmp_path / "entry.py", tmp_path / "events.log"
    script.write_text(_APP)
    log.write_text("")
    proc = subprocess.Popen([python, str(script), str(log)], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    driver = LinuxDriver()
    app = os.path.basename(python)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                shot = driver.snapshot(Scope.WINDOW, app)
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
            else:
                if any(e.value == "keep me" for e in shot.elements):
                    break
            time.sleep(0.5)
        else:
            pytest.skip("the GTK 4 entry never appeared")
        yield driver, _runtime_for(tmp_path, driver, app), app, log
    finally:
        _stop_group(proc)


def _entry(driver, app):
    shot = driver.snapshot(Scope.WINDOW, app)
    return shot, next(e for e in shot.elements if e.role in {"AXTextField", "AXTextArea"})


def test_ref_type_into_an_unfocused_gtk4_entry_appends(lab) -> None:
    driver, runtime, app, log = lab
    runtime._current, entry = _entry(driver, app)
    assert entry.value == "keep me"
    started = time.monotonic()
    result = runtime.type_text(" more", ref=entry.ref)
    elapsed = time.monotonic() - started
    assert "typed 5 characters" in str(result)
    _shot, after = _entry(driver, app)
    assert after.value == "keep me more"
    assert elapsed < 1.5, elapsed  # the old refusal took ~1.9 s
    # The press focuses the entry; nothing was activated or saved.
    assert "ACTIVATE" not in log.read_text() and "SAVE" not in log.read_text()
