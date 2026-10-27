"""#1232 live: a ref ``type`` into a filled GTK 3 entry appends to its text.

GTK selects an entry's whole text when it takes focus, and an AT-SPI insert
replaces a selection. A ref type has no replace request, so it used to wipe
'keep me' and still report confirmed. Skipped without an X display and GTK 3.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")

from a11y_computer_use.schema import ComputerUseError  # noqa: E402
from tests.test_linux_live import _runtime_for, _stop_group  # noqa: E402

_FIXTURE = textwrap.dedent(
    """
    import sys
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    focus = sys.argv[2]
    win = Gtk.Window(title="Notes1232")
    win.move(0, 0)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    win.add(box)
    entry = Gtk.Entry()
    entry.set_text("keep me")
    box.pack_start(entry, False, False, 0)
    save = Gtk.Button(label="Save")
    box.pack_start(save, False, False, 0)
    log = open(sys.argv[1], "a")
    entry.connect("changed", lambda w: (log.write(w.get_text() + "\\n"), log.flush()))
    win.show_all()
    (save if focus == "button" else entry).grab_focus()
    Gtk.main()
    """
)


_FIXTURE4 = textwrap.dedent(
    """
    import sys
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk

    def activate(app):
        win = Gtk.ApplicationWindow(application=app, title="Notes1232")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        win.set_child(box)
        entry = Gtk.Entry()
        entry.set_text("keep me")
        box.append(entry)
        box.append(Gtk.Button(label="Save"))
        log = open(sys.argv[1], "a")
        entry.connect("changed", lambda w: (log.write(w.get_text() + "\\n"), log.flush()))
        win.present()
        GLib.idle_add(lambda: (entry.grab_focus(), False)[1])

    app = Gtk.Application(application_id="x.notes1232")
    app.connect("activate", activate)
    app.run()
    """
)


def _gtk3_ready() -> None:
    if not os.environ.get("DISPLAY"):
        pytest.skip("needs an X display")
    probe = subprocess.run(
        ["/usr/bin/python3", "-c",
         "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk"],
        capture_output=True,
    )
    if probe.returncode != 0:
        pytest.skip("no GTK 3 for /usr/bin/python3")


def _gtk4_ready() -> None:
    if not os.environ.get("DISPLAY"):
        pytest.skip("needs an X display")
    probe = subprocess.run(
        ["/usr/bin/python3", "-c",
         "import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk"],
        capture_output=True,
    )
    if probe.returncode != 0:
        pytest.skip("no GTK 4 for /usr/bin/python3")


def _run_case(tmp_path, focus: str, typed: str, gtk: int = 3):
    from a11y_computer_use.drivers.linux import LinuxDriver

    (_gtk3_ready if gtk == 3 else _gtk4_ready)()
    script = tmp_path / "entry1232.py"
    script.write_text(_FIXTURE if gtk == 3 else _FIXTURE4)
    log = tmp_path / "changes.log"
    log.write_text("")
    proc = subprocess.Popen(
        ["/usr/bin/python3", str(script), str(log), focus], start_new_session=True,
    )
    try:
        driver = LinuxDriver()
        runtime = _runtime_for(tmp_path, driver, "python3")
        deadline = time.monotonic() + 30
        entry = None
        while time.monotonic() < deadline and entry is None:
            time.sleep(0.5)
            try:
                runtime.desktop_snapshot("python3")
            except ComputerUseError:
                continue
            fields = [e for e in runtime._current.elements
                      if e.role in ("AXTextField", "AXTextArea")]
            entry = fields[0] if fields else None
        assert entry is not None, "the entry never appeared in the snapshot"
        outcome = None
        error = None
        try:
            outcome = runtime.type_text(typed, ref=entry.ref)
        except ComputerUseError as exc:
            error = exc
        time.sleep(0.3)
        changes = [line for line in log.read_text().split("\n") if line != ""]
        return outcome, error, changes
    finally:
        _stop_group(proc)


@pytest.mark.parametrize("focus", ["button", "entry"])
def test_ref_type_appends_to_a_filled_entry(tmp_path, focus):
    """Entry unfocused (Save has focus) or holding the initial select-all focus."""
    outcome, error, changes = _run_case(tmp_path, focus, " more")
    assert error is None, error
    assert "typed 5 characters" in str(outcome), outcome
    assert changes and changes[-1] == "keep me more", changes
    # No intermediate state may have wiped the text.
    assert "" not in [c for c in changes], changes


def test_ref_type_appends_to_a_gtk4_entry_that_already_has_focus(tmp_path):
    """GTK 4 marks an entry neither single-line nor multi-line, and the
    entry that holds focus already has its whole text selected."""
    outcome, error, changes = _run_case(tmp_path, "entry", " more", gtk=4)
    assert error is None, error
    assert "typed 5 characters" in str(outcome), outcome
    assert changes and changes[-1] == "keep me more", changes
    assert "" not in changes, changes
