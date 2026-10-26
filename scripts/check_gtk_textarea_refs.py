"""Standalone #1130 GTK3/GTK4 ref and latency probe (requires a private desktop).

Run with the checkout on PYTHONPATH; repeat identically against v0.4.76.
No pytest live files are needed. JSON output includes measured Runtime timings.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time

LONG = "\n".join(f"Row {i:03d} the quick brown fox jumps over the lazy dog" for i in range(150))


def fixture(version, directory):
    import gi
    gi.require_version("Gtk", version + ".0")
    from gi.repository import GLib, Gtk

    loop = GLib.MainLoop()
    window = Gtk.Window(title="Ref1130")
    window.set_default_size(700, 420)
    scroll = Gtk.ScrolledWindow()
    text = Gtk.TextView()
    buffer = text.get_buffer()
    buffer.set_text(LONG)
    if version == "3":
        scroll.add(text)
        window.add(scroll)
        window.show_all()
    else:
        scroll.set_child(text)
        window.set_child(scroll)
        window.present()
    text.grab_focus()

    def poll():
        command = directory / "command.json"
        if command.exists():
            buffer.set_text(json.loads(command.read_text()))
            command.unlink()
        start, end = buffer.get_bounds()
        pending = directory / "state.tmp"
        pending.write_text(json.dumps(buffer.get_text(start, end, True)))
        pending.replace(directory / "state.json")
        return True

    GLib.timeout_add(30, poll)
    loop.run()


def wait_text(directory, expected):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = directory / "state.json"
        if state.exists() and json.loads(state.read_text()) == expected:
            return
        time.sleep(.03)
    raise AssertionError(f"GTK buffer did not become {expected[:20]!r}")


def run(version, repeats):
    from a11y_computer_use import safety, server
    from a11y_computer_use.drivers.linux import LinuxDriver

    results = []
    with tempfile.TemporaryDirectory(prefix="ref1130-") as temp:
        directory = Path(temp)
        driver = LinuxDriver()
        driver.ensure_trusted()
        process = subprocess.Popen(["/usr/bin/python3", __file__, "--fixture", "--gtk", version,
                                    "--directory", temp])
        try:
            wait_text(directory, LONG)
            time.sleep(1)  # Allow native window registration before measurements.
            store = safety.PermissionStore(directory / "permissions.json")
            store.set_tier("python3", safety.Tier.FULL)
            for repeat in range(repeats):
                for external in (False, True):
                    (directory / "command.json").write_text(json.dumps(LONG))
                    wait_text(directory, LONG)
                    time.sleep(.2)
                    with server.Runtime(driver=driver, store=store,
                                        audit=safety.AuditLog(directory / "audit")) as runtime:
                        def timed(name, fn):
                            start = time.perf_counter()
                            value = fn()
                            row[name] = time.perf_counter() - start
                            return value

                        row = {"gtk": version, "repeat": repeat, "external": external}
                        before = timed("snapshot_before", lambda: runtime.desktop_snapshot("python3"))
                        assert re.search(r"\be3 textarea", before), before
                        if external:
                            (directory / "command.json").write_text(json.dumps("y"))
                            wait_text(directory, "y")
                            time.sleep(.2)
                        else:
                            cleared = timed("clear", lambda: runtime.set_value("e3", ""))
                            assert cleared.outcome == "confirmed", cleared
                            wait_text(directory, "")
                        after = timed("snapshot_after", lambda: runtime.desktop_snapshot("python3"))
                        assert re.search(r"\be2 textarea", after), after
                        # Alternate snapshot-only and snapshot -> find issuance.
                        if repeat % 2:
                            found = runtime.find("python3", role="textarea")
                            assert re.search(r"\be2 textarea", found), found
                        typed = timed("type", lambda: runtime.type_text(text="x", ref="e2"))
                        assert typed.outcome == "confirmed", typed
                        wait_text(directory, "yx" if external else "x")
                        replaced = timed("set_value", lambda: runtime.set_value("e2", "short"))
                        assert replaced.outcome == "confirmed", replaced
                        wait_text(directory, "short")
                        # Reverse transition must retain a usable fresh e3 too.
                        (directory / "command.json").write_text(json.dumps(LONG))
                        wait_text(directory, LONG)
                        time.sleep(.2)
                        reverse = runtime.desktop_snapshot("python3")
                        assert re.search(r"\be3 textarea", reverse), reverse
                        reset = runtime.set_value("e3", "done")
                        assert reset.outcome == "confirmed", reset
                        wait_text(directory, "done")
                        results.append(row)
                        print(json.dumps(row), flush=True)
        finally:
            process.terminate()
            process.wait(timeout=5)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gtk", choices=("3", "4"), required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--directory")
    args = parser.parse_args()
    if args.fixture:
        fixture(args.gtk, Path(args.directory))
    else:
        try:
            run(args.gtk, args.repeats)
        finally:
            # Same explicit shutdown for head and baseline; avoid daemon exit races.
            from latency.run_once import close_atspi
            close_atspi()
