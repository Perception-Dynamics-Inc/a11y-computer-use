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

    seen = _keys_seen(private_x, lambda: _linux_input.press_chord("ctrl+z"))

    assert [(kind, code) for kind, code, _ctrl in seen] == [("P", 37), ("P", 52), ("R", 52), ("R", 37)], seen
    assert ("P", 52, True) in seen, "Control must be held when the Z key goes down"
    assert not any(code > 200 for _kind, code, _ctrl in seen), "no spare keycode may be pressed"
