"""Live macOS menu tests against TextEdit (need the Accessibility grant).

Menu bars are process-level accessibility objects, so these run even in a
shell that has no window-server session (where window and display calls
fail). They verify presses by their effect on menu titles, which macOS
validates when a menu opens: after ``Show Fonts`` succeeds, the same menu
offers ``Hide Fonts``, and a second ``Hide Fonts`` is refused because the
item now reads ``Show Fonts`` again.
"""
from __future__ import annotations

import subprocess
import sys
import time

import os

import pytest

from tests.conftest import HAS_AX

pytestmark = [pytest.mark.skipif(
    bool(os.environ.get("GITHUB_ACTIONS")),
    reason="menu validation and key chords need an interactive desktop session; CI Macs have none",
), pytest.mark.skipif(
    sys.platform != "darwin" or not HAS_AX,
    reason="live menu tests need macOS and the Accessibility TCC grant",
)]

from a11y_computer_use import menus  # noqa: E402
from a11y_computer_use.schema import ComputerUseError, ErrorCode  # noqa: E402

SETTLE_S = 1.0


@pytest.fixture(autouse=True, scope="module")
def _textedit_in_front():
    """Validated menus (Show Fonts / Hide Fonts) only update while the app is
    active. Bring TextEdit to the front once; skip on a runner that cannot
    (a CI Mac has no interactive user session)."""
    from a11y_computer_use import safety, server

    subprocess.run(["open", "-a", "TextEdit"], check=False, timeout=15)
    for _ in range(40):
        try:
            running, _bundle = server._running_app("com.apple.TextEdit")
            server._activate(running)
        except Exception:  # noqa: BLE001 - still launching, or cannot activate here
            pass
        if safety.frontmost_app()[0] == "com.apple.TextEdit":
            break
        time.sleep(0.5)
    else:
        pytest.skip("this runner cannot bring TextEdit to the foreground; menus need a user session")


@pytest.fixture(scope="module", autouse=True)
def textedit_running():
    subprocess.run(["/usr/bin/open", "-a", "TextEdit"], check=False)
    subprocess.run(["/usr/bin/osascript", "-e", 'tell application "TextEdit" to activate'], check=False)
    time.sleep(1.5)
    yield
    # Leave TextEdit as we found it as far as menus go: make sure the Fonts panel is closed.
    try:
        menus.macos_menu_press("TextEdit", "Format > Font > Hide Fonts")
    except ComputerUseError:
        pass


def _press(path: str) -> bool:
    try:
        menus.macos_menu_press("TextEdit", path)
        return True
    except ComputerUseError as exc:
        assert exc.code is ErrorCode.APP_NOT_FOUND, exc
        return False


def _press_once_offered(path: str, attempts: int = 6) -> bool:
    """Press ``path`` as soon as the menu offers it. The panel toggles a moment
    after the press and the title is validated only when the menu opens, so
    the first attempt can race the app."""
    for _ in range(attempts):
        if _press(path):
            return True
        time.sleep(SETTLE_S)
    return False


def test_menu_list_of_file_names_real_items_with_shortcuts() -> None:
    top = [i["title"] for i in menus.macos_menu_items("TextEdit", None)]
    assert "File" in top and "Format" in top
    rows = {i["title"]: i for i in menus.macos_menu_items("TextEdit", "File")}
    assert rows["New"]["shortcut"] == "cmd+n"
    assert rows["Open…"]["shortcut"] == "cmd+o"
    assert rows["Open Recent"]["submenu"] is True


def test_show_then_hide_fonts_each_take_effect() -> None:
    _press("Format > Font > Hide Fonts")  # normalise: panel closed, whatever the start state
    time.sleep(SETTLE_S)
    assert _press_once_offered("Format > Font > Show Fonts"), "Show Fonts should be offered while the panel is closed"
    assert _press_once_offered("Format > Font > Hide Fonts"), "Hide Fonts should be offered once the panel is open"
    time.sleep(SETTLE_S)
    # Once hidden, the validated menu offers Show Fonts again, so Hide Fonts is refused.
    available: list[str] = []
    for _ in range(6):
        try:
            menus.macos_menu_press("TextEdit", "Format > Font > Hide Fonts")
        except ComputerUseError as exc:
            available = exc.detail["available"]
            break
        time.sleep(SETTLE_S)  # the panel was still open; that press closed it, poll again
    assert "Show Fonts" in available, available
