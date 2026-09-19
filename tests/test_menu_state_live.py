"""Live macOS: an open menu is detected, closed, and no longer swallows chords.

Needs the Accessibility grant and a window-server session; skips when
TextEdit cannot be brought to the front (someone else is using the desktop).
Kept short and TextEdit-only.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import os

import pytest

from tests.conftest import HAS_AX, HAS_DISPLAYS

pytestmark = [pytest.mark.skipif(
    bool(os.environ.get("GITHUB_ACTIONS")),
    reason="menu validation and key chords need an interactive desktop session; CI Macs have none",
), pytest.mark.skipif(
    sys.platform != "darwin" or not HAS_AX or not HAS_DISPLAYS,
    reason="needs macOS, the Accessibility grant, and an unlocked window-server session",
)]

from a11y_computer_use import menus, safety, server  # noqa: E402
from a11y_computer_use.schema import ComputerUseError  # noqa: E402

TEXTEDIT = "com.apple.TextEdit"


def _runtime():
    tmp = Path(tempfile.mkdtemp())
    store = safety.PermissionStore(tmp / "perm.json")
    store.set_tier(TEXTEDIT, safety.Tier.FULL)
    return server.Runtime(store=store, audit=safety.AuditLog(tmp / "audit"))


def _window_count() -> int:
    """Count TextEdit's windows through accessibility, not AppleScript.

    ``osascript ... count windows`` blocks for minutes while a menu is tracking
    and then answers 0, which produced a false baseline; AXWindows answers at
    once in either state.
    """
    from a11y_computer_use import observe

    app_el, _accessor, _bundle = menus._macos_app_element("TextEdit")
    ax = observe._appservices()
    err, windows = ax.AXUIElementCopyAttributeValue(app_el, "AXWindows", None)
    return len(windows or ()) if err == 0 else 0


def test_open_menu_is_reported_closed_and_stops_swallowing_chords() -> None:
    rt = _runtime()
    subprocess.run(["/usr/bin/open", "-a", "TextEdit"], check=False)
    try:
        rt.app("focus", "TextEdit")
    except ComputerUseError as exc:
        pytest.skip(f"TextEdit did not come to the front: {exc}")
    time.sleep(0.8)
    from a11y_computer_use import safety
    if safety.frontmost_app()[0] != "com.apple.TextEdit":
        pytest.skip("TextEdit is not frontmost (owner at the keyboard, or a CI Mac with no foreground session)")
    before = _window_count()  # taken while no menu is open: AppleScript stalls during menu tracking
    # Open the File menu through AX (press the bar item, do not pick an entry).
    app_el, accessor, _bundle = menus._macos_app_element("TextEdit")
    bar = menus.menu_bar(accessor, app_el)
    file_item = next(n for n in menus._entries(accessor, bar) if menus._title(accessor, n) == "File")
    assert accessor.press(file_item)
    time.sleep(0.6)
    try:
        state = rt.call_tool("menu", {"app": "TextEdit", "action": "state"})
        if '"open": true' not in state:
            pytest.skip("this runner could not open TextEdit's File menu (no foreground session)")
        assert '"File"' in state, state
        # A chord while the menu is open would be swallowed; the Runtime closes the menu first.
        out = rt.key("cmd+n")
        assert "closed open menu File first" in out, out
        time.sleep(1.5)
        assert _window_count() == before + 1, "cmd+n did not open a new window"
        assert '"open": false' in rt.call_tool("menu", {"app": "TextEdit", "action": "state"})
    finally:
        try:
            menus.macos_menu_close("TextEdit")
        except ComputerUseError:
            pass
        subprocess.run(["/usr/bin/osascript", "-e",
                        'tell application "TextEdit" to close (every window whose modified is false) saving no'],
                       check=False)
