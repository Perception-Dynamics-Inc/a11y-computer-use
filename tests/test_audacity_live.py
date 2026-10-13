"""Optional live Audacity coverage for the wxGTK toolbar panel fallback."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from a11y_computer_use.schema import ComputerUseError, ErrorCode, Scope

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")


def _require_bus(driver) -> None:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("no DISPLAY/WAYLAND_DISPLAY: Audacity cannot open a window")
    try:
        driver.ensure_trusted()
    except (ComputerUseError, ImportError) as exc:
        pytest.skip(f"AT-SPI unavailable: {exc}")


def test_linux_audacity_transport_panels_are_buttons(tmp_path) -> None:
    """wx toolbar tooltips are button names in a real Audacity snapshot."""
    import shutil
    import wave

    from a11y_computer_use import observe
    from a11y_computer_use.drivers.linux import LinuxDriver

    binary = shutil.which("audacity")
    if binary is None:
        pytest.skip("no Audacity binary")
    driver = LinuxDriver()
    _require_bus(driver)
    tone = tmp_path / "probe_tone.wav"
    with wave.open(str(tone), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(44100)
        audio.writeframes(b"\x00\x00" * 44100)
    proc = subprocess.Popen([binary, str(tone)])
    try:
        deadline = time.monotonic() + 20.0
        snap = None
        while time.monotonic() < deadline:
            try:
                snap = driver.snapshot(Scope.APP, "audacity")
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
            else:
                if observe.find_elements(snap, role="button", text="Play"):
                    break
            time.sleep(0.5)
        assert snap is not None
        for label in ("Play", "Pause", "Stop"):
            matches = observe.find_elements(snap, role="button", text=label)
            assert matches, observe.render_text(snap)
            assert any(el.clickable for el in matches)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
