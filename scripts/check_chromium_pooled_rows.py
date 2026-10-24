"""Manually verify #977 against real Chrome through Linux AT-SPI.

Run from the repo root under a private Xvfb/D-Bus session. Requires Chrome,
GI/AT-SPI, Xlib and websocket-client. This is not part of the hermetic suite.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request

import websocket

from a11y_computer_use import observe, safety
from a11y_computer_use.server import Runtime
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import ComputerUseError, ErrorCode, Scope


def main() -> None:
    driver = LinuxDriver()
    driver.ensure_trusted()
    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/chromium_pooled_rows.html"
    with tempfile.TemporaryDirectory(prefix="chrome-pooled-rows-") as profile:
        with open(Path(profile) / "chrome.log", "w") as log:
            proc = subprocess.Popen([
                "google-chrome", "--force-renderer-accessibility", "--no-sandbox",
                "--disable-gpu", "--disable-dev-shm-usage", "--no-first-run",
                "--no-default-browser-check", "--password-store=basic",
                "--window-size=1000,800", "--remote-allow-origins=*",
                "--remote-debugging-port=0", f"--user-data-dir={profile}", fixture.as_uri(),
            ], stdout=log, stderr=subprocess.STDOUT)
            ws = None
            try:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        port = (Path(profile) / "DevToolsActivePort").read_text().splitlines()[0]
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json") as reply:
                            pages = json.load(reply)
                        page = next(p for p in pages if "pooled_rows" in p["url"])
                        break
                    except (OSError, StopIteration, IndexError):
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(.2)
                ws = websocket.create_connection(page["webSocketDebuggerUrl"])
                seq = 0

                def evaluate(expression):
                    nonlocal seq
                    seq += 1
                    ws.send(json.dumps({"id": seq, "method": "Runtime.evaluate", "params": {
                        "expression": expression, "returnByValue": True,
                    }}))
                    while True:
                        message = json.loads(ws.recv())
                        if message.get("id") == seq:
                            return message["result"]["result"].get("value")

                deadline = time.monotonic() + 30
                while True:
                    # Chrome can initially cache an empty application root.
                    root = driver._run(lambda: _atspi.find_root("chrome", Scope.APP))
                    if root is not None:
                        driver._run(root.clear_cache)
                    shot = driver.snapshot(Scope.WINDOW, "chrome")
                    target = next((el for el in shot.elements
                                   if el.title == "Row 00010" and el.role == "AXStaticText"), None)
                    if target is not None:
                        break
                    assert time.monotonic() < deadline, "Chrome page tree did not appear"
                    time.sleep(.25)
                assert target.row_identity
                handle = observe.ax_handle_for(shot.snapshot_id, target.ref)
                key = driver._run(lambda: _atspi._atspi_wrapper_key(handle))
                assert key is not None
                evaluate("document.querySelector('#list').scrollTop = 1200")
                time.sleep(.5)
                assert key == driver._run(lambda: _atspi._atspi_wrapper_key(handle))
                for action in (lambda: driver.resolve_ref(shot, target.ref), lambda: driver.click(target)):
                    try:
                        action()
                    except ComputerUseError as exc:
                        assert exc.code is ErrorCode.STALE_REF
                        assert exc.detail["reason"] == "target_changed"
                    else:
                        raise AssertionError("Recycled row accepted")
                current = driver._run(lambda: _atspi.chromium_row_identity(handle, fresh=True))
                assert current[0][1] == "Row 00050", current
                assert evaluate("window.clicks || 0") == 0
                evaluate("document.querySelector('#list').scrollTop = 0")
                time.sleep(.5)
                store = safety.PermissionStore(Path(profile) / "permissions.json")
                store.set_tier("chrome", safety.Tier.FULL)
                with Runtime(driver=driver, store=store,
                             audit=safety.AuditLog(Path(profile) / "audit")) as runtime:
                    result = runtime.find("chrome", text="Row 00010")
                    issued = runtime._last_issued
                    row = next(el for el in issued.elements
                               if el.title == "Row 00010" and el.role == "AXStaticText")
                    assert row.ref in result
                    result = runtime.scroll_to_find("chrome", text="Row 00300", max_scrolls=8)
                    print(result)
                    assert runtime._anchor(row.ref)[0] is issued
                    assert evaluate("document.querySelector('#list').scrollTop") > 0
                    try:
                        runtime.click(ref=row.ref, verify=True)
                    except ComputerUseError as exc:
                        assert exc.code is ErrorCode.STALE_REF
                        assert exc.detail["reason"] == "target_changed"
                        assert exc.detail["outcome"] == "refused"
                    else:
                        raise AssertionError("find -> scroll_to_find -> click accepted recycled row")
                    assert evaluate("window.clicks || 0") == 0
                    assert evaluate("document.querySelector('h1').textContent") == "Nothing selected"
                    print("PASS: Runtime find -> scroll_to_find -> click refuses original ref; "
                          "zero DOM clicks and Nothing selected")
                print("PASS: Row 00010 -> Row 00050 on the same native object; "
                      "resolve/click return stale_ref target_changed; zero DOM clicks")
            finally:
                if ws is not None:
                    ws.close()
                proc.terminate()
                proc.wait(timeout=15)


if __name__ == "__main__":
    main()
