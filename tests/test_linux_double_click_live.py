"""#1224 live: a double-click on a word of Chrome page text is confirmed by its selection.

Skipped without Chrome or an X display.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")

from a11y_computer_use.schema import ComputerUseError, ErrorCode, Scope  # noqa: E402
from tests.test_linux_live import _chrome_binary, _runtime_for, _stop_group  # noqa: E402

_PAGE = (
    "<!doctype html><meta charset=utf-8><title>cuadbl</title>"
    "<p>Paragraph text gamma delta for selecting</p>"
    "<p>Second paragraph words here</p>"
)


@pytest.fixture
def chrome_page(tmp_path):
    from a11y_computer_use.drivers.linux import LinuxDriver

    if not os.environ.get("DISPLAY"):
        pytest.skip("needs an X display")
    binary = _chrome_binary()
    if binary is None:
        pytest.skip("no Chrome")
    page = tmp_path / "dbl.html"
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
        shot = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                shot = driver.snapshot(Scope.WINDOW, "chrome")
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
            else:
                if any(e.title and e.title.startswith("Paragraph text") for e in shot.elements):
                    break
            time.sleep(0.5)
        else:
            pytest.fail("Chrome never exposed the page text")
        runtime = _runtime_for(tmp_path, driver, "chrome", "google-chrome")
        runtime._current = shot
        yield driver, runtime, shot
    finally:
        _stop_group(proc)


def _paragraph(shot, prefix: str):
    return next(e for e in shot.elements if e.role == "AXStaticText" and e.title.startswith(prefix))


def test_double_click_on_page_text_is_confirmed_by_the_selection(chrome_page) -> None:
    driver, runtime, shot = chrome_page
    target = _paragraph(shot, "Paragraph text")
    assert driver.selection_details(target) == ()  # nothing is selected yet
    result = runtime.click(target.ref, count=2)
    assert result.outcome == "confirmed", (result.outcome, result.evidence)
    assert "selection changed" in result.evidence
    rows = driver.selection_details(target)
    assert rows and len(rows) == 1 and rows[0][2].strip() in "Paragraph text gamma delta for selecting"
    assert " " not in rows[0][2].strip()  # one word


def test_double_click_on_another_paragraph_is_confirmed_too(chrome_page) -> None:
    """A selection left in an earlier paragraph does not make the next one a no-op."""
    driver, runtime, shot = chrome_page
    first = runtime.click(_paragraph(shot, "Paragraph text").ref, count=2)
    assert first.outcome == "confirmed", (first.outcome, first.evidence)
    second = runtime.click(_paragraph(shot, "Second paragraph").ref, count=2)
    assert second.outcome == "confirmed", (second.outcome, second.evidence)
