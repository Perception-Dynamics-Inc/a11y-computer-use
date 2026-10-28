"""#1286 live: a GTK 3 notebook tab scrolled out of the tab bar is reachable.

A scrollable Gtk.Notebook parks tabs that do not fit at an off-screen
position. ``find`` must list such a tab, a ref click must switch to it
through the tab list's Selection (confirmed, and the page really switches),
and ``scroll_to_find`` must bring another one in. Skipped without an X
display and GTK 3.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")

from a11y_computer_use.schema import ComputerUseError  # noqa: E402
from tests.test_linux_gtk_selection_click_live import _ref_of  # noqa: E402
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
    win = Gtk.Window(title="Tabs1286")
    win.move(0, 0)
    win.set_default_size(560, 200)
    nb = Gtk.Notebook()
    nb.set_scrollable(True)
    for i in range(1, 16):
        nb.append_page(Gtk.Button(label="Go %d" % i), Gtk.Label(label="Tab %d" % i))
    nb.connect("switch-page", lambda n, p, i: note("PAGE %d" % (i + 1)))
    win.add(nb)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    note("READY")
    Gtk.main()
    """
)


def test_overflowed_notebook_tab_is_found_clicked_and_scrolled_to(tmp_path):
    from a11y_computer_use.drivers.linux import LinuxDriver

    _gtk3_ready()
    script = tmp_path / "tabs1286.py"
    script.write_text(_FIXTURE)
    log = tmp_path / "tabs.log"
    log.write_text("")
    proc = subprocess.Popen(["/usr/bin/python3", str(script), str(log)], start_new_session=True)
    try:
        runtime = _runtime_for(tmp_path, LinuxDriver(), "python3")
        deadline = time.monotonic() + 30
        text = ""
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                text = runtime.find(app="python3", text="Tab 12")
            except ComputerUseError:
                continue
            if '"Tab 12"' in text:
                break
        assert '"Tab 12"' in text, text
        assert "scrolled out of the tab bar" in text, text

        mark = len(log.read_text())
        result = runtime.click(ref=_ref_of(text, "Tab 12"))
        time.sleep(0.3)
        assert "PAGE 12" in log.read_text()[mark:], (result, log.read_text())
        assert result.outcome == "confirmed", result
        after = runtime.find(app="python3", text="Go 12")
        assert '"Go 12"' in after and "@" in after, after

        mark = len(log.read_text())
        found = runtime.scroll_to_find(app="python3", text="Tab 15")
        assert found.startswith("found"), found
        assert '"Tab 15"' in found, found
        assert "PAGE 15" in log.read_text()[mark:], log.read_text()
    finally:
        _stop_group(proc)
