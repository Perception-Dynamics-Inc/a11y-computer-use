"""Live Electron ``<webview>`` checks on a real AT-SPI bus (#1266).

They skip unless ``A11Y_ELECTRON_BIN`` (or ``electron`` on PATH) names an
Electron binary, and when there is no display or AT-SPI bus.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")

from a11y_computer_use.schema import ComputerUseError, ErrorCode  # noqa: E402

_MAIN = """\
const {app, BrowserWindow} = require('electron');
app.commandLine.appendSwitch('force-renderer-accessibility');
app.whenReady().then(() => {
  new BrowserWindow({width: 900, height: 600, frame: false, title: 'ewv1266',
    webPreferences: {webviewTag: true}}).loadFile(__dirname + '/host.html');
});
"""
_HOST = ('<!doctype html><title>ewv1266</title><input aria-label="Host field">'
         '<webview src="guest.html" style="width:860px;height:420px"></webview>')
_GUEST = ('<!doctype html><title>Guest Page</title>'
          '<label>Guest name <input value="keep me"></label><button>Guest submit</button>')


def _electron() -> str:
    found = os.environ.get("A11Y_ELECTRON_BIN") or shutil.which("electron")
    if not found or not os.access(found, os.X_OK):
        pytest.skip("no Electron binary (set A11Y_ELECTRON_BIN)")
    return found


def _runtime(tmp_path, driver):
    from a11y_computer_use import safety, server

    store = safety.PermissionStore(tmp_path / "permissions.json")
    for app in ("electron", "ewv1266"):
        store.set_tier(app, safety.Tier.FULL)
    front = driver.frontmost_app()[0]
    if front:
        store.set_tier(front, safety.Tier.FULL)
    return server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)


def _field(runtime, title):
    runtime.find("electron", text=title)
    return next(el for el in runtime._current.elements if el.title == title and el.editable)


def test_linux_electron_webview_set_value_from_host_focus_is_confirmed(tmp_path) -> None:
    """#1266: set_value on a guest input while focus is in the host page.

    AT-SPI focus does not cross into the guest, so the call was refused
    ``focus_lost`` after about 1.9 s. It has to land and be confirmed.
    """
    from a11y_computer_use.drivers.linux import LinuxDriver

    binary = _electron()
    driver = LinuxDriver()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("no display")
    try:
        driver.ensure_trusted()
    except ComputerUseError as exc:
        pytest.skip(f"AT-SPI bus not reachable: {exc.message}")
    for name, body in (("main.js", _MAIN), ("host.html", _HOST), ("guest.html", _GUEST)):
        (tmp_path / name).write_text(body)
    (tmp_path / "package.json").write_text('{"name":"ewv1266","main":"main.js"}')
    log = open(tmp_path / "electron.log", "wb")
    proc = subprocess.Popen([binary, "--no-sandbox", str(tmp_path)], stdout=log,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    try:
        runtime = _runtime(tmp_path, driver)
        deadline = time.monotonic() + 45
        while True:
            if proc.poll() is not None:
                raise AssertionError(f"Electron exited {proc.returncode}")
            try:
                _field(runtime, "Host field")
                _field(runtime, "Guest name")
                break
            except (ComputerUseError, StopIteration):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        host = _field(runtime, "Host field")
        runtime.set_value(ref=host.ref, value="hostval")
        guest = _field(runtime, "Guest name")
        assert guest.value == "keep me"
        try:
            result = runtime.set_value(ref=guest.ref, value="Astana")
        except ComputerUseError as exc:
            raise AssertionError(exc.detail) from exc
        assert getattr(result, "outcome", "confirmed") == "confirmed", result
        assert _field(runtime, "Guest name").value == "Astana"
        assert _field(runtime, "Host field").value == "hostval"
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
