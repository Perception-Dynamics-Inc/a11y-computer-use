"""Live Linux-backend test — runs on the ubuntu CI runner under Xvfb + a real
AT-SPI2 bus.

Proves the Linux `Driver` walks a real AT-SPI2 tree through the SHARED pruning
engine and drives it accessibility-first: it launches a tiny GTK window (a
labelled button + a text entry), snapshots it, **presses the button through the
AT-SPI action API** (no pointer movement — the a11y-first payoff) and observes
the effect, and **enters text through AT-SPI EditableText** (deterministic; needs
no widget focus, which headless X cannot grant). macOS/Windows skip this file; it
also self-skips when the AT-SPI bus is unreachable, so it never breaks a
non-configured environment.

Note: synthetic key chords (`key_chord`) go through XTEST, which requires real
widget focus and so is only meaningful in a full desktop session — its parsing
is covered by tests/test_linux_synthetic.py; its live effect is not asserted here.
"""

from __future__ import annotations

import subprocess
import os
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")

from computeruse.schema import ComputerUseError, ErrorCode, Scope  # noqa: E402

_APP = "cuatestapp"

_GTK_APP = textwrap.dedent(
    """
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib
    GLib.set_prgname("cuatestapp")
    win = Gtk.Window(title="cuatestapp")
    win.set_name("cuatestapp")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    entry = Gtk.Entry()
    btn = Gtk.Button(label="Save")
    # Pressing the button (via AT-SPI do_action) has an observable effect,
    # so the test can prove the a11y activation actually fired.
    btn.connect("clicked", lambda _b: entry.set_text("SAVED"))
    box.pack_start(btn, False, False, 0)
    box.pack_start(entry, False, False, 0)
    win.add(box)
    win.set_default_size(400, 200)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    win.present()
    Gtk.main()
    """
)


def _require_bus(driver) -> None:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("no DISPLAY/WAYLAND_DISPLAY: the GTK test app cannot open a window")
    try:
        driver.ensure_trusted()
    except ComputerUseError as exc:
        pytest.skip(f"AT-SPI bus not reachable: {exc.message}")
    except ImportError as exc:  # pragma: no cover - env guard
        pytest.skip(f"PyGObject/Atspi missing: {exc}")


def _launch_app(tmp_path) -> subprocess.Popen:
    script = tmp_path / "cuatestapp.py"
    script.write_text(_GTK_APP)
    return subprocess.Popen([sys.executable, str(script)])


def _wait_for_snapshot(driver, timeout_s: float = 15.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = driver.snapshot(Scope.WINDOW, _APP)
        if last.elements and any(el.editable for el in last.elements):
            return last
        time.sleep(0.5)
    return last


def test_linux_driver_reports_its_name() -> None:
    from computeruse.drivers import get_driver

    assert get_driver().name == "linux"


def test_linux_atspi_snapshot_of_gtk_app(tmp_path) -> None:
    from computeruse.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    proc = _launch_app(tmp_path)
    try:
        snap = _wait_for_snapshot(driver)
        assert snap and snap.elements, "AT-SPI walk produced no elements"
        roles = {el.role for el in snap.elements}
        assert "AXWindow" in roles, f"no window in snapshot; roles={roles}"
        assert snap.elements[0].ref == "e1"  # shared engine indexed it
        assert any(el.clickable and "Save" in el.title for el in snap.elements), \
            f"no Save button; {[(e.role, e.title) for e in snap.elements]}"
        assert any(el.editable for el in snap.elements), f"no editable entry; roles={roles}"
    finally:
        proc.terminate()


def test_linux_a11y_press_button(tmp_path) -> None:
    """The a11y-first activation path: press the Save button through the AT-SPI
    action API (no pointer movement); its handler sets the entry to 'SAVED',
    confirmed by a re-snapshot."""
    from computeruse.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    proc = _launch_app(tmp_path)
    try:
        snap = _wait_for_snapshot(driver)
        button = next((el for el in snap.elements if el.clickable and "Save" in el.title), None)
        assert button is not None, f"no Save button; {[(e.role, e.title) for e in snap.elements]}"

        assert driver.press_element(driver.resolve_ref(snap, button.ref)), "AT-SPI press failed"
        time.sleep(0.4)

        after = driver.snapshot(Scope.WINDOW, _APP)
        values = [el.value for el in after.elements if el.value]
        assert any("SAVED" in (v or "") for v in values), f"button press had no effect; {values}"
    finally:
        proc.terminate()


def test_linux_a11y_type_text(tmp_path) -> None:
    """The a11y-first text path: focus the entry, then enter text via AT-SPI
    EditableText (deterministic, no widget-focus needed), confirmed by a
    re-snapshot."""
    from computeruse.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    proc = _launch_app(tmp_path)
    try:
        snap = _wait_for_snapshot(driver)
        entry = next((el for el in snap.elements if el.editable), None)
        assert entry is not None, f"no editable entry; roles={[e.role for e in snap.elements]}"

        driver.press_element(driver.resolve_ref(snap, entry.ref))  # records + focuses it
        time.sleep(0.3)
        driver.type_text("hello atspi")
        time.sleep(0.3)
        driver.type_text(" more")  # a second call appends — insert-at-caret semantics
        time.sleep(0.3)

        after = driver.snapshot(Scope.WINDOW, _APP)
        values = [el.value for el in after.elements if el.value]
        assert any("hello atspi more" in (v or "") for v in values), f"typed text missing; {values}"
    finally:
        proc.terminate()


def test_linux_forces_a11y_status() -> None:
    """The org.a11y.Status flip: after the driver initializes, the desktop's
    accessibility flags are on so Chromium/Electron apps expose their tree with
    no relaunch (the Linux counter to Grok's a11y-OFF desktop)."""
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import GLib, Gio

    from computeruse.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)  # ensure_trusted() -> enable_a11y_status()

    def getp(name: str):
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        r = bus.call_sync(
            "org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", ("org.a11y.Status", name)), GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE, -1, None,
        )
        return r.unpack()[0]

    assert getp("IsEnabled") is True
    assert getp("ScreenReaderEnabled") is True


def _pointer_xy():
    """Current pointer position from the X server (None if Xlib is unavailable)."""
    try:
        from Xlib import display as _xd

        root = _xd.Display().screen().root
        q = root.query_pointer()
        return int(q.root_x), int(q.root_y)
    except Exception:  # pragma: no cover - environment guard
        return None


def test_linux_coordinate_click_lands_with_pointer_away_from_origin(tmp_path) -> None:
    """The coordinate/vision-fallback click: position the pointer ABSOLUTELY and
    press. The pointer is first parked away from (0, 0) on purpose: under Xvfb it
    starts at the origin, where a relative warp and an absolute move coincide,
    which is how a relative `warp_pointer` shipped unnoticed until a real desktop
    (Budgie/Xorg, docs/box-testbed.md) put every click at pointer + (x, y)."""
    from computeruse.drivers import _linux_input
    from computeruse.drivers.linux import LinuxDriver
    from computeruse.schema import Point

    driver = LinuxDriver()
    _require_bus(driver)
    if _pointer_xy() is None:
        pytest.skip("no X pointer (python-xlib or DISPLAY unavailable)")
    proc = _launch_app(tmp_path)
    try:
        snap = _wait_for_snapshot(driver)
        button = next((el for el in snap.elements if el.clickable and "Save" in el.title), None)
        assert button is not None, f"no Save button; {[(e.role, e.title) for e in snap.elements]}"
        b = button.bounds
        cx, cy = int(b.x + b.width / 2), int(b.y + b.height / 2)

        _linux_input._move(cx + 150, cy + 120)  # park the pointer off-origin, off-target
        _linux_input._flush()
        parked = _pointer_xy()
        assert parked != (cx, cy), "test setup: pointer must start away from the target"

        try:
            driver.click(Point(display_id=driver.main_display_id(), x=cx, y=cy))
        except ComputerUseError as exc:
            pytest.skip(f"coordinate input unsupported here: {exc.message}")
        time.sleep(0.4)

        assert _pointer_xy() == (cx, cy), f"pointer ended at {_pointer_xy()}, wanted {(cx, cy)}"
        after = driver.snapshot(Scope.WINDOW, _APP)
        values = [el.value for el in after.elements if el.value]
        assert any("SAVED" in (v or "") for v in values), f"coordinate click missed; {values}"
    finally:
        proc.terminate()


def test_linux_runtime_click_without_display_id(tmp_path) -> None:
    """`Runtime.click(x, y)` with no display_id must resolve the default display
    through the driver (it used to call Quartz unconditionally and raise
    NameError off macOS). Needs a window manager so the GTK app is the active
    window the gate keys on; self-skips when there is none (Xvfb without a WM)."""
    from computeruse import safety, server
    from computeruse.drivers import _linux_input
    from computeruse.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    if _pointer_xy() is None:
        pytest.skip("no X pointer (python-xlib or DISPLAY unavailable)")
    proc = _launch_app(tmp_path)
    try:
        snap = _wait_for_snapshot(driver)
        button = next((el for el in snap.elements if el.clickable and "Save" in el.title), None)
        assert button is not None
        b = button.bounds
        cx, cy = int(b.x + b.width / 2), int(b.y + b.height / 2)

        store = safety.PermissionStore(tmp_path / "permissions.json")
        rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
        front = rt._frontmost()
        if not front:
            pytest.skip("no active window (no window manager): the gate has no app to key on")
        store.set_tier(front, safety.Tier.FULL)

        _linux_input._move(cx + 150, cy + 120)
        _linux_input._flush()
        try:
            msg = rt.click(x=cx, y=cy)  # no display_id: the driver seam supplies it
        except ComputerUseError as exc:
            if exc.code is ErrorCode.UNSUPPORTED:
                pytest.skip(f"coordinate input unsupported here: {exc.message}")
            raise
        assert msg.startswith("clicked (") and "on display" in msg, msg
        assert _pointer_xy() == (cx, cy)
    finally:
        proc.terminate()
