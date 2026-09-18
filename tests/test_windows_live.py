"""Live Windows-backend test — runs on the windows-latest CI runner.

Proves the Windows `Driver.snapshot` actually walks a real UIA tree and feeds
the SHARED pruning engine: it launches Notepad, snapshots it, and asserts the
result is a real indexed tree (a window with refs), exercising the identical
`observe.build_snapshot` path macOS uses. macOS/Linux skip this file.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows backend")

from a11y_computer_use.schema import Scope  # noqa: E402


def _open_notepad() -> subprocess.Popen:
    proc = subprocess.Popen(["notepad.exe"])
    # UIA needs the window to exist and be built out.
    for _ in range(30):
        time.sleep(0.5)
        import uiautomation as auto

        if any(
            "notepad" in ((w.Name or "").lower() + (w.ClassName or "").lower())
            for w in auto.GetRootControl().GetChildren()
        ):
            return proc
    return proc


def test_windows_uia_snapshot_of_notepad() -> None:
    from a11y_computer_use.drivers.windows import WindowsDriver

    driver = WindowsDriver()
    proc = _open_notepad()
    try:
        snap = driver.snapshot(Scope.WINDOW, "notepad")
        assert snap.elements, "UIA walk produced no elements"
        roles = {el.role for el in snap.elements}
        assert "AXWindow" in roles, f"no window in the snapshot; roles={roles}"
        # the shared engine indexed it: pre-order refs e1..eN
        assert snap.elements[0].ref == "e1"
        # Notepad exposes an editable document/edit area
        assert any(el.editable for el in snap.elements) or any(
            el.role in {"AXTextArea", "AXTextField", "AXDocument"} for el in snap.elements
        ), f"no editable text area found; roles={roles}"
    finally:
        proc.terminate()


def test_windows_driver_reports_its_name() -> None:
    from a11y_computer_use.drivers import get_driver

    assert get_driver().name == "windows"


def test_windows_uia_type_into_notepad() -> None:
    """Full Windows act loop: focus the edit via a UIA pattern, type via
    SendInput, and confirm the text via a re-snapshot — all through the driver."""
    from a11y_computer_use.drivers.windows import WindowsDriver

    driver = WindowsDriver()
    proc = _open_notepad()
    try:
        snap = driver.snapshot(Scope.WINDOW, "notepad")
        edit = next((el for el in snap.elements if el.editable), None)
        assert edit is not None, f"no editable element; roles={[e.role for e in snap.elements]}"

        assert driver.press_element(edit)  # focus the edit area (UIA SetFocus)
        time.sleep(0.4)
        driver.type_text("a11y-computer-use on Windows")  # SendInput Unicode
        time.sleep(0.4)

        after = driver.snapshot(Scope.WINDOW, "notepad")
        values = [el.value for el in after.elements if el.value]
        assert any("a11y-computer-use" in (v or "") for v in values), f"typed text missing; values={values}"
    finally:
        proc.terminate()


def test_windows_key_chord_select_all() -> None:
    """Prove key chords work: type 'abc', Ctrl+A to select all, type 'X' to
    replace — the edit should end up 'X', not 'abcX'."""
    from a11y_computer_use.drivers.windows import WindowsDriver

    driver = WindowsDriver()
    proc = _open_notepad()
    try:
        snap = driver.snapshot(Scope.WINDOW, "notepad")
        edit = next((el for el in snap.elements if el.editable), None)
        assert edit is not None
        driver.press_element(edit)
        time.sleep(0.3)
        driver.type_text("abc")
        time.sleep(0.3)
        driver.key_chord("ctrl+a")  # select all
        time.sleep(0.3)
        driver.type_text("X")  # replaces the selection
        time.sleep(0.3)

        after = driver.snapshot(Scope.WINDOW, "notepad")
        values = [el.value for el in after.elements if el.value]
        assert any("X" in (v or "") and "abc" not in (v or "") for v in values), \
            f"Ctrl+A select-all did not replace; values={values}"
    finally:
        proc.terminate()


def test_windows_runtime_end_to_end(tmp_path) -> None:
    """The full server stack on Windows: Runtime + safety gating (backed by the
    Windows system ops) + driver — snapshot Notepad, type through the *gated*
    Runtime, and confirm via a re-snapshot."""
    from a11y_computer_use import safety, server
    from a11y_computer_use.drivers import _win_system

    proc = _open_notepad()
    try:
        app_id = _win_system.frontmost_app_id()  # e.g. "notepad.exe"
        assert app_id.lower().startswith("notepad"), app_id
        store = safety.PermissionStore(tmp_path / "perms.json")
        store.set_tier(app_id, safety.Tier.FULL)
        rt = server.Runtime(store=store)

        tree = rt.desktop_snapshot("notepad", "window")  # gated + UIA snapshot
        assert "window" in tree.lower(), tree[:200]

        rt.type_text("via the gated MCP Runtime on Windows")  # gated + SendInput
        after = rt.desktop_snapshot("notepad", "window")
        assert "via the gated MCP Runtime" in after, after[:300]
    finally:
        proc.terminate()
