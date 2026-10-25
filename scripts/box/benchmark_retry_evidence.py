"""Measure scoped retry-read overhead against real GTK, in a private desktop.

Run with a unique writable XDG_CACHE_HOME and XDG_CONFIG_HOME:
  dbus-run-session -- xvfb-run -a python scripts/box/benchmark_retry_evidence.py

Requires GTK3, AT-SPI2, python-xlib, xfwm4 and xdotool. This measures the
added native read pair on an inert-label click, not whole Runtime latency.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time


def fixture() -> None:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    window = Gtk.Window(title=f"Retry evidence probe {os.getpid()}")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.get_accessible().set_name("Probe controls")
    label, entry = Gtk.Label(label="Inert label"), Gtk.Entry()
    box.pack_start(label, False, False, 0)
    box.pack_start(entry, False, False, 0)
    window.add(box)
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    entry.grab_focus()
    Gtk.main()


def measure() -> None:
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    from a11y_computer_use import outcome
    from a11y_computer_use.drivers import _atspi, _linux_input
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Click, Element

    wm = subprocess.Popen(["xfwm4", "--compositor=off"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--fixture"])
    try:
        deadline = time.monotonic() + 10
        application = None
        while time.monotonic() < deadline:
            desktop = Atspi.get_desktop(0)
            apps = [desktop.get_child_at_index(i) for i in range(desktop.get_child_count())]
            application = next((app for app in apps if app.get_process_id() == process.pid), None)
            if application is not None and application.get_child_count():
                break
            time.sleep(0.05)
        assert application is not None, "fixture did not register"
        pending, label, focus = [application], None, None
        while pending:
            node = pending.pop()
            if node.get_role_name() == "label":
                label = node
            if node.get_role_name() == "text":
                focus = node
            pending.extend(node.get_child_at_index(i) for i in range(node.get_child_count()))
        assert label is not None and focus is not None
        time.sleep(0.3)  # Setup only: let the private WM map the fixture.
        subprocess.run(["xdotool", "search", "--onlyvisible", "--pid", str(process.pid), "--name",
                        "Retry evidence probe", "windowfocus", "--sync"], check=True)
        time.sleep(0.1)
        rectangle = Atspi.Component.get_extents(label, Atspi.CoordType.SCREEN)
        x, y = rectangle.x + rectangle.width // 2, rectangle.y + rectangle.height // 2
        action = Click(Element("e1", "AXStaticText", "Inert label", None, None, "s"))
        driver = LinuxDriver()
        driver._run(lambda: None)  # The real pre-action snapshot initializes this worker.

        def read():
            return driver._run(lambda: _atspi.retry_evidence(action, label, focus))

        baseline, measured, extra = [], [], []
        for _ in range(50):
            started = time.perf_counter()
            _linux_input.click(x, y)
            baseline.append(time.perf_counter() - started)
            started = time.perf_counter()
            before = read()
            dispatched = time.perf_counter()
            _linux_input.click(x, y)
            completed = time.perf_counter()
            after = read()
            ended = time.perf_counter()
            assert outcome.retry_verdict(("suspected_noop", "unchanged"), before, after)[0] == "suspected_noop"
            measured.append(ended - started)
            extra.append(dispatched - started + ended - completed)
        print(json.dumps({
            "before_action_p50_s": statistics.median(baseline),
            "after_action_p50_s": statistics.median(measured),
            "extra_pair_p50_s": statistics.median(extra),
            "extra_pair_max_s": max(extra), "readable_pairs": len(extra),
        }))
        assert max(extra) <= 0.1, "extra reads exceeded the per-action budget"
    finally:
        process.terminate()
        process.wait()
        wm.terminate()
        wm.wait()


if __name__ == "__main__":
    fixture() if "--fixture" in sys.argv else measure()
