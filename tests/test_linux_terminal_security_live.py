"""Live terminal execution gate; skips without a terminal and AT-SPI bus."""

from __future__ import annotations

import os
import sys

import pytest

from a11y_computer_use.schema import ComputerUseError, ErrorCode

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")


def _require_bus(driver) -> None:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("no DISPLAY/WAYLAND_DISPLAY")
    try:
        driver.ensure_trusted()
    except (ComputerUseError, ImportError) as exc:
        pytest.skip(f"AT-SPI bus unavailable: {exc}")


def test_terminal_launch_without_exec_is_blocked(tmp_path) -> None:
    """An installed terminal cannot be launched through a FULL grant alone."""
    import json
    import shutil
    from a11y_computer_use import safety, server
    from a11y_computer_use.drivers.linux import LinuxDriver

    binary = next((name for name in ("xfce4-terminal", "gnome-terminal", "xterm")
                   if shutil.which(name)), None)
    if binary is None:
        pytest.skip("no terminal emulator installed")
    driver = LinuxDriver()
    _require_bus(driver)
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier(binary, safety.Tier.FULL)
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    with pytest.raises(ComputerUseError) as caught:
        runtime.app("launch", binary)
    assert caught.value.code is ErrorCode.EXEC_DISABLED
    row, = [json.loads(line) for path in (tmp_path / "audit").glob("*.jsonl")
            for line in path.read_text().splitlines()]
    assert row["result"] == "exec_disabled"
    assert row["metrics"]["security_violation"] == "exec_disabled"
