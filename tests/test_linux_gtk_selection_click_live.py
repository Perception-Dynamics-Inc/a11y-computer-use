"""#1238/#1240 live: GTK 3 ListBox row and FlowBox tile clicks are confirmed.

The fixture logs every selection change. Each ref click must select its row or
tile and come back ``confirmed``. Skipped without an X display and GTK 3.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")

from a11y_computer_use.schema import ComputerUseError  # noqa: E402
from tests.test_linux_live import _runtime_for, _stop_group  # noqa: E402
from tests.test_linux_stack_refs_live import _gtk3_ready  # noqa: E402

_FIXTURE = textwrap.dedent(
    """
    import sys
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    log = open(sys.argv[1], "a")
    def note(text):
        log.write(text + "\\n"); log.flush()
    win = Gtk.Window(title="Sel1238")
    win.move(0, 0)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    lb = Gtk.ListBox()
    for name in ("Row one", "Row two", "Row three"):
        row = Gtk.ListBoxRow(); row.add(Gtk.Label(label=name)); row.nm = name; lb.add(row)
    lb.connect("row-selected", lambda l, r: r and note("SELECT " + r.nm))
    fb = Gtk.FlowBox()
    fb.set_max_children_per_line(3)
    fb.set_selection_mode(Gtk.SelectionMode.SINGLE)
    for i in range(1, 7):
        fb.add(Gtk.Label(label="Tile %d" % i))
    fb.connect("selected-children-changed", lambda f: [
        note("FLOWSEL " + c.get_child().get_label()) for c in f.get_selected_children()])
    box.pack_start(lb, False, False, 0)
    box.pack_start(fb, False, False, 0)
    win.add(box)
    win.set_default_size(520, 360)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    lb.unselect_all()  # GTK selects the first row on map; start with none
    note("READY")
    Gtk.main()
    """
)


def _ref_of(text: str, label: str) -> str:
    for line in text.splitlines():
        if f'"{label}"' in line:
            found = re.search(r"\be\d+\b", line)
            if found:
                return found.group(0)
    raise AssertionError(f"{label} is not in the snapshot:\n{text}")


def test_listbox_rows_and_flowbox_tiles_are_confirmed(tmp_path):
    from a11y_computer_use.drivers.linux import LinuxDriver

    _gtk3_ready()
    script = tmp_path / "sel1238.py"
    script.write_text(_FIXTURE)
    log = tmp_path / "sel.log"
    log.write_text("")
    proc = subprocess.Popen(["/usr/bin/python3", str(script), str(log)], start_new_session=True)
    try:
        runtime = _runtime_for(tmp_path, LinuxDriver(), "python3")
        deadline = time.monotonic() + 30
        text = ""
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                text = runtime.desktop_snapshot("python3", mode="full")
            except ComputerUseError:
                continue
            if "Row three" in text and "Tile 5" in text:
                break
        outcomes = []
        for label, expect in (("Row one", "SELECT Row one"), ("Row two", "SELECT Row two"),
                              ("Row three", "SELECT Row three"), ("Tile 5", "FLOWSEL Tile 5"),
                              ("Tile 2", "FLOWSEL Tile 2")):
            text = runtime.desktop_snapshot("python3", mode="full")
            mark = len(log.read_text())
            result = runtime.click(ref=_ref_of(text, label))
            time.sleep(0.3)
            assert expect in log.read_text()[mark:], (label, log.read_text())
            outcomes.append((label, result.outcome, result.evidence))
        assert all(o == "confirmed" for _, o, _ in outcomes), outcomes
    finally:
        _stop_group(proc)
