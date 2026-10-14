"""Live regression for a GTK 4 masked Gtk.Entry without security metadata."""

import os
import subprocess
import sys
import time

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Scope
from tests.test_linux_gtk4_empty_value_live import _require_bus, _stop_group, _window


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")
def test_gtk4_masked_entry_never_returns_plaintext(tmp_path):
    driver = LinuxDriver()
    _require_bus(driver)
    check = subprocess.run(
        [sys.executable, "-c",
         "import gi; gi.require_version('Gtk', '4.0'); from gi.repository import Gtk"],
        capture_output=True, text=True,
    )
    if check.returncode:
        pytest.skip("GTK 4 Python bindings unavailable")
    script = tmp_path / "masked_entry.py"
    script.write_text("""
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk
def activate(app):
    window = Gtk.ApplicationWindow(application=app, title="MaskedEntry578")
    entry = Gtk.Entry()
    entry.set_visibility(False)
    entry.set_text("hunter2-578-live")
    entry.update_property([Gtk.AccessibleProperty.LABEL], ["PIN"])
    window.set_child(entry)
    window.present()
app = Gtk.Application(application_id="org.example.MaskedEntry578")
app.connect("activate", activate)
app.run([])
""")
    env = dict(os.environ, GTK_A11Y="atspi", NO_AT_BRIDGE="0")
    proc = subprocess.Popen([sys.executable, str(script)], env=env, start_new_session=True)
    try:
        row = _window(driver, "MaskedEntry578", proc.pid)
        assert row is not None, driver.windows()
        driver.focus_window(int(row["window_id"]))
        deadline = time.monotonic() + 15
        while True:
            snap = driver.snapshot(Scope.WINDOW, str(row["app"]))
            fields = [el for el in snap.elements if el.title == "PIN"]
            if fields or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        assert fields, "masked entry must be observed for this test to prove redaction"
        assert all(el.value is None for el in fields)
        assert not observe.find_elements(snap, text="hunter2-578-live")
        assert "hunter2-578-live" not in repr(snap)
    finally:
        _stop_group(proc)
