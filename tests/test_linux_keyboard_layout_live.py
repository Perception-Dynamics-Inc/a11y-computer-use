"""#1211: shortcuts on a single non-Latin layout, against a real X server.

A private ``Xvfb -noreset`` with its own keymap, so no other test or display is
touched. ``-noreset`` matters: without it the server resets to us when the
last client disconnects, undoing ``setxkbmap``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import pytest

from a11y_computer_use.drivers import _linux_input

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="X11 only"),
    pytest.mark.skipif(
        not (shutil.which("Xvfb") and shutil.which("setxkbmap")), reason="needs Xvfb and setxkbmap"
    ),
]


@pytest.fixture
def private_x(monkeypatch):
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "640x480x24", "-noreset"],
        pass_fds=[write_fd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    os.close(write_fd)
    try:
        with os.fdopen(read_fd) as number:
            name = ":" + number.readline().strip()
        if name == ":":
            pytest.skip("Xvfb did not start")
        monkeypatch.setenv("DISPLAY", name)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setattr(_linux_input, "_display", None)
        monkeypatch.setattr(_linux_input, "_keymap_seen", None)
        yield name
    finally:
        shown = _linux_input._display
        if shown is not None:
            try:
                shown.close()
            except Exception:  # noqa: BLE001
                pass
        proc.terminate()  # this test's own server: a recorded Popen, never a name match
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def _keys_seen(name: str, send) -> list[tuple[str, int, bool]]:
    """(press/release, keycode, Control held) for the keys ``send`` produces."""
    from Xlib import X, display

    listener = display.Display(name)
    try:
        root = listener.screen().root
        window = root.create_window(
            0, 0, 50, 50, 0, listener.screen().root_depth,
            event_mask=X.KeyPressMask | X.KeyReleaseMask,
        )
        window.map()
        listener.sync()
        listener.set_input_focus(window, X.RevertToParent, X.CurrentTime)
        listener.sync()
        send()
        time.sleep(0.3)
        seen = []
        while listener.pending_events():
            event = listener.next_event()
            if event.type in (X.KeyPress, X.KeyRelease):
                seen.append(("P" if event.type == X.KeyPress else "R", event.detail, bool(event.state & X.ControlMask)))
        return seen
    finally:
        listener.close()


def _layout(name: str, layout: str) -> None:
    subprocess.run(["setxkbmap", "-display", name, "-layout", layout], check=True, timeout=20)


def test_ctrl_z_on_a_russian_layout_presses_the_physical_z_key(private_x) -> None:
    _layout(private_x, "ru")
    from Xlib import display

    probe = display.Display(private_x)
    try:
        row = [int(s) for s in probe.get_keyboard_mapping(52, 1)[0]]
        everything = probe.get_keyboard_mapping(8, 248)
    finally:
        probe.close()
    if row[0] != 0x6D1:  # Cyrillic_ya: the server did not take the layout
        pytest.skip("this X server did not apply setxkbmap ru")
    assert not any(0x20 <= s < 0x7F and chr(s).isalpha() for r in everything for s in r), (
        "ru must have no Latin letter anywhere, or this test proves nothing"
    )

    def chromium_chord() -> None:
        with _linux_input.physical_position_target(lambda: True):
            _linux_input.press_chord("ctrl+z")

    seen = _keys_seen(private_x, chromium_chord)

    assert [(kind, code) for kind, code, _ctrl in seen] == [("P", 37), ("P", 52), ("R", 52), ("R", 37)], seen
    assert ("P", 52, True) in seen, "Control must be held when the Z key goes down"
    assert not any(code > 200 for _kind, code, _ctrl in seen), "no spare keycode may be pressed"


def test_ctrl_z_for_a_non_chromium_target_uses_the_spare_keycode(private_x) -> None:
    """The Tester's #1218 finding: GTK/Qt ignore the physical keycode on a lone ru layout."""
    _layout(private_x, "ru")

    def other_chord() -> None:
        with _linux_input.physical_position_target(lambda: False):
            _linux_input.press_chord("ctrl+z")

    seen = _keys_seen(private_x, other_chord)

    pressed = [code for kind, code, _ctrl in seen if kind == "P"]
    assert pressed[0] == 37 and len(pressed) == 2, seen
    assert pressed[1] not in (52, 38, 54) and pressed[1] > 8, seen


_GTK_PROBE = """
import sys, gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
out = sys.argv[1]
window = Gtk.Window(title="chord-probe")
window.move(0, 0)
window.resize(300, 80)
entry = Gtk.Entry()
entry.set_text("hello")
window.add(entry)
group = Gtk.AccelGroup()
window.add_accel_group(group)
def note(tag):
    with open(out, "a") as handle:
        handle.write(tag + "\\n")
for key in "zcs":
    keyval, mods = Gtk.accelerator_parse("<Control>" + key)
    group.connect(keyval, mods, 0, lambda *a, tag="ctrl+" + key: (note(tag), True)[1])
window.connect("destroy", Gtk.main_quit)
window.show_all()
note("ready")
Gtk.main()
"""


def _focus_window_named(name: str, title: str) -> bool:
    from Xlib import X, display

    probe = display.Display(name)
    try:
        def walk(window):
            for child in window.query_tree().children:
                try:
                    if (child.get_wm_name() or "") == title:
                        return child
                except Exception:  # noqa: BLE001
                    continue
                found = walk(child)
                if found is not None:
                    return found
            return None

        target = walk(probe.screen().root)
        if target is None:
            return False
        probe.set_input_focus(target, X.RevertToParent, X.CurrentTime)
        probe.sync()
        return True
    finally:
        probe.close()


def test_a_real_gtk3_window_gets_its_shortcuts_on_a_russian_layout(private_x, tmp_path, monkeypatch) -> None:
    """#1218 regression (Tester, #1211): the physical keycode is for Chromium only. A
    real GTK3 window under a lone ru layout ignores it and honours the spare keycode."""
    pytest.importorskip("gi")
    _layout(private_x, "ru")
    out = tmp_path / "gtk-chords.txt"
    env = {**os.environ, "DISPLAY": private_x, "GDK_BACKEND": "x11"}
    env.pop("WAYLAND_DISPLAY", None)
    app = subprocess.Popen(
        [sys.executable, "-c", _GTK_PROBE, str(out)], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline and "ready" not in (out.read_text() if out.exists() else ""):
            time.sleep(0.1)
        if "ready" not in (out.read_text() if out.exists() else ""):
            pytest.skip("GTK3 window did not start")
        time.sleep(0.5)
        if not _focus_window_named(private_x, "chord-probe"):
            pytest.skip("could not focus the GTK window without a window manager")
        time.sleep(0.5)
        with _linux_input.physical_position_target(lambda: False):  # what key_chord decides for GTK
            for chord in ("ctrl+z", "ctrl+c", "ctrl+s"):
                _linux_input.press_chord(chord)
                time.sleep(0.4)
        time.sleep(0.5)
        assert out.read_text().split() == ["ready", "ctrl+z", "ctrl+c", "ctrl+s"]
    finally:
        app.terminate()  # this test's own Popen, never a name match
        try:
            app.wait(timeout=10)
        except subprocess.TimeoutExpired:
            app.kill()
            app.wait(timeout=10)
