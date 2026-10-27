"""#1225 live: ``set_value`` into a filled Chrome field on a lone Russian layout.

Skipped without Chrome, ``setxkbmap`` or an X display. The layout the display
had is put back afterwards.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")

from a11y_computer_use.drivers import _linux_input  # noqa: E402
from a11y_computer_use.schema import ComputerUseError, ErrorCode, Scope  # noqa: E402
from tests.test_linux_live import _chrome_binary, _runtime_for, _stop_group  # noqa: E402

_PAGE = (
    "<!doctype html><meta charset=utf-8><title>cuaru</title>"
    "<label>Name <input id=f aria-label='Name field' value='keep me'></label>"
)
_TEXT = "hello world 42"


def _query() -> dict[str, str]:
    out = subprocess.run(["setxkbmap", "-query"], capture_output=True, text=True, timeout=5, check=False)
    return {
        key.strip(): value.strip()
        for key, _, value in (line.partition(":") for line in out.stdout.splitlines())
    }


@pytest.fixture
def russian_chrome(tmp_path, monkeypatch):
    from a11y_computer_use.drivers.linux import LinuxDriver

    if not os.environ.get("DISPLAY") or shutil.which("setxkbmap") is None:
        pytest.skip("needs an X display and setxkbmap")
    binary = _chrome_binary()
    if binary is None:
        pytest.skip("no Chrome")
    before = _query()
    if not before.get("layout"):
        pytest.skip("cannot read the current keyboard layout")
    page = tmp_path / "ru.html"
    page.write_text(_PAGE)
    profile = tmp_path / "profile"
    profile.mkdir()
    proc = subprocess.Popen(
        [
            binary, "--force-renderer-accessibility", "--no-sandbox", "--disable-gpu",
            "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
            f"--user-data-dir={profile}", "--window-size=1000,500", f"file://{page}",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    driver = LinuxDriver()
    try:
        field = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and field is None:
            try:
                shot = driver.snapshot(Scope.WINDOW, "chrome")
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
            else:
                field = next((e for e in shot.elements if e.title == "Name field"), None)
            time.sleep(0.5)
        assert field is not None, "Chrome never exposed the field"
        runtime = _runtime_for(tmp_path, driver, "chrome", "google-chrome")
        runtime._current = shot
        subprocess.run(["setxkbmap", "-layout", "ru"], check=True, timeout=10)
        monkeypatch.setattr(_linux_input, "_display", None)
        time.sleep(0.5)
        yield driver, runtime, field
    finally:
        restore = ["setxkbmap", "-layout", before["layout"]]
        if before.get("variant"):
            restore += ["-variant", before["variant"]]
        subprocess.run(restore, check=False, timeout=10)
        _stop_group(proc)


def _field(driver):
    shot = driver.snapshot(Scope.WINDOW, "chrome")
    return next(e for e in shot.elements if e.title == "Name field")


def test_set_value_replaces_a_filled_field_on_a_russian_layout(russian_chrome) -> None:
    driver, runtime, field = russian_chrome
    assert _field(driver).value == "keep me"
    runtime.click(field.ref)
    runtime.set_value(field.ref, _TEXT)
    assert _field(driver).value == _TEXT


def test_a_dropped_select_all_leaves_the_value_untouched(russian_chrome, monkeypatch) -> None:
    """Even if Ctrl+A is lost, no BackSpace is sent: the old text is not eaten."""
    driver, runtime, field = russian_chrome
    monkeypatch.setattr(_linux_input, "_physical_position_target", lambda: False)  # the #1225 bug
    runtime.click(field.ref)
    with pytest.raises(ComputerUseError) as exc:
        runtime.set_value(field.ref, _TEXT)
    assert _field(driver).value == "keep me"
