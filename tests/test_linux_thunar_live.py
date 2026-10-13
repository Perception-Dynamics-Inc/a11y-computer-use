"""Live Thunar file refs, with no coordinate clicks on its opaque view."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from a11y_computer_use.schema import ComputerUseError, Scope
from tests.test_linux_live import _kill_thunar, _require_bus, _runtime_for, _stop_group

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")


def test_linux_thunar_opaque_icon_view_selects_and_renames_by_ref(tmp_path) -> None:
    """Thunar's opaque icon view yields a named file ref usable before F2."""
    import shutil

    from a11y_computer_use import observe
    from a11y_computer_use.drivers.linux import LinuxDriver

    binary = shutil.which("thunar")
    xfconf = shutil.which("xfconf-query")
    if not binary or not xfconf:
        pytest.skip("thunar/xfconf-query is not installed")
    driver = LinuxDriver()
    _require_bus(driver)
    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "report.txt").write_text("report\n")
    subprocess.run(
        [xfconf, "-c", "thunar", "-p", "/last-view", "-n", "-t", "string", "-s", "ThunarIconView"],
        check=False, capture_output=True,
    )
    _kill_thunar()
    env = os.environ.copy()
    env.update(GTK_MODULES="gail:atk-bridge", NO_AT_BRIDGE="0")
    proc = subprocess.Popen(
        [binary, str(folder)], env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 25
        shot = None
        item = None
        while time.monotonic() < deadline:
            try:
                shot = driver.snapshot(Scope.WINDOW, "thunar")
                item = next((el for el in shot.elements if el.title == "report.txt"), None)
                if item is not None:
                    break
            except ComputerUseError:
                pass
            time.sleep(0.2)
        assert shot is not None and item is not None, "Thunar did not expose report.txt"
        assert item.role != "opaque_region"
        runtime = _runtime_for(tmp_path, driver, "thunar")
        runtime.desktop_snapshot("thunar")
        current = runtime._current
        assert current is not None
        item = next(el for el in current.elements if el.title == "report.txt")
        runtime.click(item.ref)
        runtime.key("F2", app="thunar")
        runtime.key("ctrl+a", app="thunar")
        runtime.type_text("renamed.txt", app="thunar")
        runtime.key("return", app="thunar")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (folder / "renamed.txt").exists():
            time.sleep(0.1)
        assert (folder / "renamed.txt").exists()
        assert not (folder / "report.txt").exists()
    finally:
        _stop_group(proc)
        _kill_thunar()


