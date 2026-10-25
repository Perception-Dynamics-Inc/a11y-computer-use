"""Verify #1101 on a private X11 desktop (not a pytest live-suite run).

Run with PYTHONPATH pointing at the checkout, a unique writable XDG_CACHE_HOME,
and GSETTINGS_BACKEND=memory, under dbus-run-session -- xvfb-run -a python.
Requires Mousepad, GTK3, Google Chrome, xfwm4 and xmessage.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import time


def entry() -> None:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    window = Gtk.Window(title="Entry1101")
    field = Gtk.Entry()
    field.set_text("start middle end")
    window.add(field)
    window.show_all()
    field.grab_focus()
    Gtk.main()


def verify() -> None:
    from a11y_computer_use import safety, server
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Scope

    processes = []

    def launch(args):
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(process)

    try:
        with tempfile.TemporaryDirectory(prefix="gtk-keyboard-") as directory:
            root = Path(directory)
            document = root / "document.txt"
            document.write_text("start middle end\nsecond line\n")
            page = root / "input.html"
            page.write_text('<title>Chrome1101</title><input aria-label="Probe input" autofocus value="start middle end">')
            launch(["xfwm4", "--compositor=off"])
            launch(["mousepad", "--disable-server", str(document)])
            launch([sys.executable, str(Path(__file__).resolve()), "--entry"])
            launch(["xmessage", "-title", "Other1101", "Other application"])
            launch(["google-chrome", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                    "--no-first-run", "--password-store=basic", "--force-renderer-accessibility",
                    f"--user-data-dir={root / 'chrome'}", page.as_uri()])
            time.sleep(3)  # Fixture startup only; no production wait is changed.
            driver = LinuxDriver()
            driver.ensure_trusted()
            rows = driver.windows()
            other = next(row["window_id"] for row in rows if row["title"] == "Other1101")
            gtk = next(row for row in rows if row["title"] == "Entry1101")
            store = safety.PermissionStore(root / "permissions.json")
            for app in ("mousepad", gtk["app"], "chrome"):
                store.set_tier(app, safety.Tier.FULL)
            runtime = server.Runtime(store=store, audit=safety.AuditLog(root / "audit"), driver=driver)
            for app in ("mousepad", gtk["app"], "chrome"):
                for _ in range(3):
                    runtime.key("ctrl+End", app=app)
                    if app == "chrome":
                        snap = driver.snapshot(Scope.WINDOW, app)
                        field = next(element for element in snap.elements if element.editable and element.title == "Probe input")
                        assert driver.press_element(field)
                        runtime.key("End", app=app)
                    before = runtime._key_focus_probe(app)
                    driver.focus_window(other)
                    result = runtime.type_text("Sw1t", app=app)
                    after = runtime._key_focus_probe(app)
                    assert after[0].count("Sw1t") == before[0].count("Sw1t") + 1
                    assert result.outcome == "confirmed", result.evidence
                    assert not result.next
                    print(app, "type", result.outcome, result.evidence, flush=True)
                    runtime.key("ctrl+Home", app=app)
                    caret_before = runtime._key_focus_probe(app)
                    driver.focus_window(other)
                    result = runtime.key("ctrl+End" if app == "mousepad" else "End", app=app)
                    caret_after = runtime._key_focus_probe(app)
                    assert caret_after[1] > caret_before[1]
                    assert result.outcome == "confirmed", result.evidence
                    assert not result.next
                    print(app, "key", result.outcome, result.evidence, flush=True)
    finally:
        for process in reversed(processes):
            process.terminate()
        for process in processes:
            process.wait(timeout=10)


if __name__ == "__main__":
    entry() if "--entry" in sys.argv else verify()
