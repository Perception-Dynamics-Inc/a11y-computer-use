"""Linux real-desktop live tests: the coordinate/vision input path under a REAL
window manager, plus the system tools (apps, windows, clipboard) and the gated
Runtime, against a live GTK3 window.

These are the checks Xvfb cannot make honestly (no window manager means no real
focus and a pointer parked at (0, 0), which hid the relative-warp bug). They
self-skip unless the process is inside an X11 session that has an EWMH window
manager AND a reachable AT-SPI bus, so they run on a Box/desktop VM
(scripts/box/run-live.sh) and stay skipped on Xvfb CI.
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux desktop only")

from computeruse.schema import Point, Scope  # noqa: E402

_APP = "cuatestapp"
_APP_SRC = """
import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk, GLib
GLib.set_prgname("cuatestapp")
w = Gtk.Window(title="cuatestapp"); w.set_name("cuatestapp"); w.set_default_size(460, 220); w.move(500, 260)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6); w.add(box)
entry = Gtk.Entry(); entry.set_name("entry"); box.pack_start(entry, False, False, 0)
spin = Gtk.SpinButton.new_with_range(0, 100, 1); spin.set_value(50); spin.set_name("spin"); box.pack_start(spin, False, False, 0)
scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1); scale.set_value(0); scale.set_name("scale"); scale.set_draw_value(False); box.pack_start(scale, False, False, 0)
btn = Gtk.Button(label="Save"); box.pack_start(btn, False, False, 0)
lbl = Gtk.Label(label="status: idle"); box.pack_start(lbl, False, False, 0)
btn.connect("clicked", lambda *_: lbl.set_text("status: saved " + entry.get_text()))
w.connect("destroy", Gtk.main_quit); w.show_all(); entry.grab_focus(); Gtk.main()
"""


def _has_wm_and_bus() -> tuple[bool, str]:
    import os

    if not os.environ.get("DISPLAY"):
        return False, "no DISPLAY (not inside an X session)"
    try:
        from Xlib import display as xdisplay

        d = xdisplay.Display()
        root = d.screen().root
        atom = d.intern_atom("_NET_SUPPORTING_WM_CHECK")
        if root.get_full_property(atom, 0) is None:
            return False, "no EWMH window manager on this display (Xvfb without a WM)"
    except Exception as ex:  # noqa: BLE001
        return False, f"cannot open the X display: {ex}"
    try:
        from computeruse.drivers.linux import LinuxDriver

        LinuxDriver().ensure_trusted()
    except Exception as ex:  # noqa: BLE001
        return False, f"AT-SPI bus not reachable: {ex}"
    return True, ""


_OK, _WHY = _has_wm_and_bus()
requires_desktop = pytest.mark.skipif(not _OK, reason=_WHY or "needs a real Linux desktop session")


def _mouse() -> tuple[int, int]:
    out = subprocess.run(["xdotool", "getmouselocation", "--shell"], capture_output=True, text=True, timeout=5).stdout
    kv = dict(line.split("=") for line in out.strip().splitlines())
    return int(kv["X"]), int(kv["Y"])


@pytest.fixture
def app(tmp_path):
    script = tmp_path / "cuatestapp.py"
    script.write_text(_APP_SRC)
    proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        from computeruse.drivers.linux import LinuxDriver

        driver = LinuxDriver()
        driver.ensure_trusted()
        deadline = time.time() + 20
        snap = None
        while time.time() < deadline:
            try:
                snap = driver.snapshot(Scope.WINDOW, _APP)
                if any(e.clickable and "Save" in (e.title or "") for e in snap.elements):
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.25)
        assert snap is not None, "the GTK test window never appeared on the a11y bus"
        time.sleep(0.5)  # let the WM finish mapping/focusing the window
        yield driver, driver.snapshot(Scope.WINDOW, _APP)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _entry(snap):
    return next(e for e in snap.elements if e.editable and e.role in ("AXTextField", "AXTextArea"))


def _spin(snap):
    return next(e for e in snap.elements if e.role in ("AXIncrementor", "AXTextField", "AXTextArea") and e is not _entry(snap) and e.editable)


def _label(snap):
    for e in snap.elements:
        txt = (e.title or "") + (e.value or "")
        if e.role == "AXStaticText" and "status" in txt:
            return e.title or e.value
    return None


def _center(el) -> tuple[int, int]:
    b = el.bounds
    return int(b.x + b.width / 2), int(b.y + b.height / 2)


def _fresh(driver):
    return driver.snapshot(Scope.WINDOW, _APP)


@requires_desktop
def test_coordinate_click_moves_pointer_to_absolute_position(app) -> None:
    """The relative-warp regression: a click at (x, y) must leave the pointer AT
    (x, y), whatever its starting position."""
    driver, snap = app
    x, y = _center(_entry(snap))
    subprocess.run(["xdotool", "mousemove", "40", "40"], timeout=5)  # park it somewhere non-zero
    driver.click(Point(display_id=0, x=x, y=y))
    time.sleep(0.3)
    px, py = _mouse()
    assert abs(px - x) <= 2 and abs(py - y) <= 2, f"pointer at {(px, py)}, expected {(x, y)}"


@requires_desktop
def test_xtest_typing_lands_including_unicode_and_punctuation(app) -> None:
    """XTEST typing into the focused entry, including characters that are NOT on
    the keymap (accented, CJK) which need the temporary-keycode remap."""
    driver, snap = app
    x, y = _center(_entry(snap))
    driver.click(Point(display_id=0, x=x, y=y))
    time.sleep(0.3)
    text = "Hello, World! 42 <a/b> ünïcödé 日本"
    # Repeated replacements exercise keymap restore/rebind and the asynchronous
    # input method. A single short string previously hid intermittent reordering.
    for attempt in range(5):
        driver.key_chord("ctrl+a")
        driver.type_text(text)
        deadline = time.monotonic() + 2
        value = None
        while time.monotonic() < deadline:
            value = _entry(_fresh(driver)).value
            if value == text:
                break
            time.sleep(0.05)
        assert value == text, f"typing round {attempt + 1}: {value!r} != {text!r}"


@requires_desktop
def test_key_chords_select_all_and_replace(app) -> None:
    driver, snap = app
    x, y = _center(_entry(snap))
    driver.click(Point(display_id=0, x=x, y=y))
    time.sleep(0.3)
    driver.type_text("first")
    driver.key_chord("ctrl+a")
    driver.type_text("second")
    time.sleep(0.5)
    assert _entry(_fresh(driver)).value == "second"
    driver.key_chord("end")
    driver.type_text("-x")
    driver.key_chord("backspace")
    time.sleep(0.5)
    assert _entry(_fresh(driver)).value == "second-"


@requires_desktop
def test_chord_with_punctuation_key_is_accepted(app) -> None:
    driver, _snap = app
    from computeruse.drivers import _linux_input

    for chord in ("ctrl+/", "ctrl+minus", "ctrl+plus", "alt+.", "shift+tab", "f13"):
        _linux_input.validate_chord(chord)  # no event sent; must parse


@requires_desktop
def test_scroll_wheel_changes_spinbutton(app) -> None:
    driver, snap = app
    spin = _spin(snap)
    before = float(spin.value or 0)
    x, y = _center(spin)
    driver.click(Point(display_id=0, x=x, y=y))
    driver.scroll(Point(display_id=0, x=x, y=y), dy=-3)  # wheel up = increment
    time.sleep(0.5)
    after = float(_spin(_fresh(driver)).value or 0)
    assert after != before, f"spinbutton value unchanged at {after}"


@requires_desktop
def test_drag_moves_scale(app) -> None:
    driver, snap = app
    scale = next(e for e in snap.elements if e.role == "AXSlider")
    b = scale.bounds
    start = Point(display_id=0, x=int(b.x + 8), y=int(b.y + b.height / 2))
    end = Point(display_id=0, x=int(b.x + b.width * 0.7), y=int(b.y + b.height / 2))
    driver.drag(start, end)
    time.sleep(0.5)
    value = float(next(e for e in _fresh(driver).elements if e.role == "AXSlider").value or 0)
    assert value > 30, f"slider value {value} after drag"


@requires_desktop
def test_coordinate_click_on_button_triggers_it(app) -> None:
    driver, snap = app
    save = next(e for e in snap.elements if e.clickable and "Save" in (e.title or ""))
    x, y = _center(save)
    driver.click(Point(display_id=0, x=x, y=y))
    time.sleep(0.5)
    assert (_label(_fresh(driver)) or "").startswith("status: saved")


@requires_desktop
def test_apps_windows_clipboard(app) -> None:
    driver, _snap = app
    # The Linux app id is the owning process comm ("python3" for a GTK script),
    # the window carries the human title.
    names = [a.get("name") for a in driver.running_apps()]
    assert any(n and (_APP in n or n.startswith("python")) for n in names), names
    wins = driver.windows()
    assert any(_APP in (w.get("title") or "") for w in wins), wins
    resolved = driver.activate_app(_APP)
    assert resolved
    time.sleep(0.4)
    front, _pid = driver.frontmost_app()
    assert front and front.startswith("python"), front  # comm of the GTK process
    payload = "clip-roundtrip ünï 42"
    driver.write_clipboard(payload)
    assert driver.read_clipboard() == payload


@requires_desktop
def test_screenshot_matches_display(app) -> None:
    import io

    from PIL import Image

    driver, snap = app
    shot = driver.screenshot()
    im = Image.open(io.BytesIO(shot.png))
    d = snap.displays[0]
    assert im.size == (d.width, d.height)


@requires_desktop
def test_gated_runtime_coordinate_path(app, tmp_path, monkeypatch) -> None:
    """What `computeruse run-once` and the MCP tools go through: click(x, y)
    with no display_id, type, key, observe — all through the safety gate."""
    driver, snap = app
    monkeypatch.setenv("HOME", str(tmp_path))  # fresh permission store + audit log
    from computeruse import safety, server
    from computeruse.drivers import _linux_system

    rt = server.Runtime()
    app_id = _linux_system.frontmost_app_id()
    safety.PermissionStore().set_tier(app_id, safety.Tier.FULL)
    x, y = _center(_entry(snap))
    assert rt.click(x=x, y=y).startswith("clicked")
    rt.key("ctrl+a")
    rt.type_text("runtime path ok")
    time.sleep(0.5)
    assert _entry(_fresh(driver)).value == "runtime path ok"
    text = rt.desktop_snapshot(_APP)
    assert "Save" in text
