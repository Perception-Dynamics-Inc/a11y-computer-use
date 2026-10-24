"""Manually verify #1014 against real Chrome through Linux AT-SPI.

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

from a11y_computer_use import safety
from a11y_computer_use.server import Runtime
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import ComputerUseError, ErrorCode, Scope


def main() -> None:
    driver = LinuxDriver()
    driver.ensure_trusted()
    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/chromium_iframe_modal.html"
    with tempfile.TemporaryDirectory(prefix="chrome-iframe-modal-") as profile:
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
                        page = next(p for p in pages if "iframe_modal" in p["url"])
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

                store = safety.PermissionStore(Path(profile) / "permissions.json")
                store.set_tier("chrome", safety.Tier.FULL)
                for mode in ("interactive", "full"):
                    evaluate("document.querySelector('dialog').close()")
                    time.sleep(.5)
                    with Runtime(driver=driver, store=store,
                                 audit=safety.AuditLog(Path(profile) / "audit")) as runtime:
                        deadline = time.monotonic() + 30
                        while True:
                            root = driver._run(lambda: _atspi.find_root("chrome", Scope.APP))
                            if root is not None:
                                driver._run(root.clear_cache)
                            result = runtime.find("chrome", text="Open dialog from iframe")
                            targets = [el for el in runtime._last_issued.elements
                                       if el.title == "Open dialog from iframe" and el.role == "AXButton"]
                            if targets:
                                break
                            assert time.monotonic() < deadline, result
                            time.sleep(.25)
                        target = targets[0]
                        assert target.ref in result
                        result = runtime.click(ref=target.ref, verify=True)
                        assert evaluate("document.querySelector('dialog').open"), result
                        runtime.desktop_snapshot("chrome", mode=mode)
                        # Count every driver input call as well as independent DOM effects.
                        calls = []
                        original_click = driver.click
                        original_press = driver.press_element
                        def tracked_click(*args, **kwargs):
                            calls.append(args)
                            return original_click(*args, **kwargs)
                        def tracked_press(*args, **kwargs):
                            calls.append(args)
                            return original_press(*args, **kwargs)
                        driver.click = tracked_click
                        driver.press_element = tracked_press
                        try:
                            runtime.click(ref=target.ref, verify=True)
                        except ComputerUseError as exc:
                            assert exc.code is ErrorCode.STALE_REF, exc
                            assert exc.detail["reason"] == "target_changed", exc.detail
                            assert exc.detail["outcome"] == "refused"
                        else:
                            raise AssertionError("Snapshot rebound iframe button ref")
                        finally:
                            driver.click = original_click
                            driver.press_element = original_press
                        assert calls == []
                        assert evaluate("window.clicks") == 0
                        assert evaluate("document.querySelector('dialog').open")
                        print(f"PASS {mode}: {target.ref} stale_ref/target_changed; zero input; modal stays open")
                        evaluate("document.querySelector('dialog').close()")
                        time.sleep(.5)
                        runtime.desktop_snapshot("chrome", mode=mode)
                        assert runtime._anchor(target.ref)[0] is runtime._current
                        runtime.click(ref=target.ref, verify=True)
                        assert evaluate("document.querySelector('dialog').open")
                        print(f"PASS {mode}: same iframe control rebinds and opens modal again")
            finally:
                if ws is not None:
                    ws.close()
                proc.terminate()
                proc.wait(timeout=15)


if __name__ == "__main__":
    main()
