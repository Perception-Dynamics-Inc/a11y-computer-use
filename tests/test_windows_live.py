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

from computeruse.schema import Scope  # noqa: E402


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
    from computeruse.drivers.windows import WindowsDriver

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
    from computeruse.drivers import get_driver

    assert get_driver().name == "windows"
