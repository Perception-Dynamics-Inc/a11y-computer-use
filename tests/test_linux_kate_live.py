"""Live Kate document coverage; skips without Kate or an AT-SPI display."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tests.test_linux_live import _require_bus, _runtime_for, _stop, _wait_named_tree, _which

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")


def test_linux_kate_find_does_not_match_an_offscreen_document_line(tmp_path) -> None:
    """Kate's whole-buffer Text must not report the bottom line at the top."""
    from a11y_computer_use.drivers.linux import LinuxDriver

    binary = _which("kate")
    if not binary:
        pytest.skip("kate is not installed")
    driver = LinuxDriver()
    _require_bus(driver)
    document = tmp_path / "kate-probe.txt"
    document.write_text("A11Y_KATE_TOP\n" + "padding\n" * 518 + "BOTTOM_MARKER_520\n")
    env = os.environ.copy()
    env["QT_ACCESSIBILITY"] = "1"
    proc = subprocess.Popen(
        [binary, "--new", str(document)], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        _wait_named_tree(
            driver, "kate",
            lambda snap: any("A11Y_KATE_TOP" in str(el.value or "") for el in snap.elements),
            timeout_s=20,
        )
        runtime = _runtime_for(tmp_path, driver, "kate")
        runtime.find("kate", text="BOTTOM_MARKER_520")
        from a11y_computer_use import observe
        assert not observe.find_elements(runtime._current, text="BOTTOM_MARKER_520")
    finally:
        _stop(proc)
