"""Opt-in #838 check under Xvfb + D-Bus + a window manager (no pytest live suite).

Run from the repo root with PYTHONPATH=. and the package dependencies installed:
  xvfb-run -a -s '-screen 0 1280x800x24' dbus-run-session -- \
      python scripts/check_gtk_text_range.py
Requires GTK3, AT-SPI2, XTEST and xfwm4. Uses two real GTK TextViews.
"""
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def application():
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk, GLib

    GLib.set_prgname('range838')
    window = Gtk.Window(title='range838')
    window.set_default_size(640, 380)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    source, destination = Gtk.TextView(), Gtk.TextView()
    source.get_buffer().set_text('alpha DRAGWORDK5 omega\nsecond line here')
    for view in (source, destination):
        box.pack_start(view, True, True, 0)
    window.add(box)
    window.connect('destroy', Gtk.main_quit)
    window.show_all()
    window.present()
    Gtk.main()


def check(directory):
    from a11y_computer_use import server, safety
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Scope

    driver = LinuxDriver()
    for _ in range(30):
        try:
            snap = driver.snapshot(Scope.WINDOW, 'range838')
            if sum(el.editable for el in snap.elements) == 2:
                break
        except Exception:
            pass
        time.sleep(.2)
    else:
        raise AssertionError('GTK fixture did not register two text views')
    store = safety.PermissionStore(directory / 'permissions.json')
    with server.Runtime(driver=driver, store=store,
                        audit=safety.AuditLog(directory / 'audit')) as runtime:
        _, app = runtime._resolve_app('range838')
        store.set_tier(app, safety.Tier.FULL)
        print(runtime.find('range838', text='DRAGWORDK5'), flush=True)
        target = next(el for el in runtime._current.elements if el.text_range)
        destination = next(el for el in runtime._current.elements
                           if el.editable and el.ref != target.ref)
        assert target.text_range == (6, 16, 'DRAGWORDK5')
        result = runtime.click(ref=target.ref, count=2)
        assert driver.text_selection(target) == ((6, 16),)
        assert result.outcome == 'confirmed', result
        print('Double-click: selection (6, 16), confirmed', flush=True)
        # Start a separate gesture outside GTK's multi-click interval.
        time.sleep(.5)
        result = runtime.click(ref=target.ref, count=2)
        assert result.outcome == 'suspected_noop', result
        print('Unchanged selection: suspected_noop', flush=True)
        time.sleep(.5)
        assert driver.text_selection(target) == ((6, 16),)
        result = runtime.drag(start_ref=target.ref, end_ref=destination.ref)
        print('Drag receipt:', result.outcome, result.evidence, flush=True)
        fresh = driver.snapshot(Scope.WINDOW, 'range838')
        assert any(el.editable and el.value == 'DRAGWORDK5' for el in fresh.elements), fresh
        print('Destination read-back: DRAGWORDK5; live GTK3 range check passed', flush=True)


if __name__ == '__main__':
    if '--app' in sys.argv:
        application()
    else:
        with tempfile.TemporaryDirectory(prefix='range838-') as temporary:
            processes = []
            try:
                processes.append(subprocess.Popen(['xfwm4']))
                processes.append(subprocess.Popen([sys.executable, __file__, '--app']))
                check(Path(temporary))
            finally:
                for process in reversed(processes):
                    process.terminate()
                    process.wait(timeout=5)
