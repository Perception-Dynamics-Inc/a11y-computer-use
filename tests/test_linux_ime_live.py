"""#1219 live: Chrome with an IBus engine that transforms ASCII.

Skipped unless Chrome, ``ibus`` and a running IBus daemon that can load
``m17n:ru:translit`` are present. The user's engine is put back afterwards.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")

from a11y_computer_use.schema import ComputerUseError, ErrorCode, Scope  # noqa: E402
from tests.test_linux_live import _chrome_binary, _runtime_for, _stop_group  # noqa: E402

_PAGE = (
    "<!doctype html><meta charset=utf-8><title>cuaime</title>"
    "<input id=f aria-label='Name field'>"
)
_ENGINE = "m17n:ru:translit"
_TEXT = "hello world 42"


def _ibus(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ibus", *args], capture_output=True, text=True, timeout=5, check=False)


@pytest.fixture
def transforming_engine(monkeypatch):
    # tests/conftest.py disables the bypass so no test touches a real engine.
    # This one exists to exercise it.
    monkeypatch.setenv("A11Y_IME_BYPASS", "1")
    if shutil.which("ibus") is None:
        pytest.skip("ibus is not installed")
    before = _ibus("engine")
    if before.returncode != 0 or not before.stdout.strip() or " " in before.stdout.strip():
        pytest.skip("no running IBus daemon with an engine set")
    original = before.stdout.strip()
    _ibus("engine", _ENGINE)
    if _ibus("engine").stdout.strip() != _ENGINE:
        _ibus("engine", original)
        pytest.skip(f"IBus cannot load {_ENGINE}")
    yield original
    _ibus("engine", original)
    for _ in range(3):
        if _ibus("engine").stdout.strip() == original:
            break
        _ibus("engine", original)


@pytest.fixture
def chrome_field(tmp_path, transforming_engine):
    from a11y_computer_use.drivers.linux import LinuxDriver

    binary = _chrome_binary()
    if binary is None:
        pytest.skip("no Chrome")
    page = tmp_path / "ime.html"
    page.write_text(_PAGE)
    profile = tmp_path / "profile"
    profile.mkdir()
    env = dict(os.environ, XMODIFIERS="@im=ibus", GTK_IM_MODULE="ibus", QT_IM_MODULE="ibus")
    proc = subprocess.Popen(
        [
            binary, "--force-renderer-accessibility", "--no-sandbox", "--disable-gpu",
            "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
            f"--user-data-dir={profile}", "--window-size=1000,500", f"file://{page}",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=env,
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
        yield driver, runtime, field, transforming_engine
    finally:
        _stop_group(proc)


def _value(driver) -> str | None:
    shot = driver.snapshot(Scope.WINDOW, "chrome")
    return next(e.value for e in shot.elements if e.title == "Name field")


def test_type_ref_lands_ascii_exactly_and_restores_the_engine(chrome_field) -> None:
    driver, runtime, field, original = chrome_field
    runtime.type_text(_TEXT, ref=field.ref)
    assert _value(driver) == _TEXT
    assert _ibus("engine").stdout.strip() == _ENGINE


def test_set_value_lands_ascii_exactly_and_keeps_focus(chrome_field) -> None:
    driver, runtime, field, original = chrome_field
    runtime.set_value(field.ref, _TEXT)
    shot = driver.snapshot(Scope.WINDOW, "chrome")
    now = next(e for e in shot.elements if e.title == "Name field")
    assert now.value == _TEXT
    assert now.focused is True
    assert _ibus("engine").stdout.strip() == _ENGINE
