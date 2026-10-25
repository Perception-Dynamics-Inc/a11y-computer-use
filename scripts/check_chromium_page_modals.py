"""Check #1084 with real Chrome in a private Xvfb/D-Bus session.

Set DISPLAY to an unused display. Requires Chrome, xfwm4, xdotool, GI/AT-SPI,
Xlib, Pillow and websocket-client. --baseline measures 0.4.76 without asserting
the new implicit-modal-input refusal.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request

import websocket

from a11y_computer_use import safety
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import ComputerUseError, Scope
from a11y_computer_use.server import Runtime


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true")
    baseline = parser.parse_args().baseline
    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/chromium_page_modals.html"
    processes = []

    def start(args):
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(proc)
        return proc

    with tempfile.TemporaryDirectory(prefix="chrome-page-modal-") as directory:
        work = Path(directory)
        ws = None
        runtime = None
        try:
            xserver = start(["Xvfb", os.environ["DISPLAY"], "-screen", "0", "1280x900x24"])
            time.sleep(.5)
            assert xserver.poll() is None, "DISPLAY must be unused; private Xvfb failed"
            start(["xfwm4", "--compositor=off"])
            start([
                "google-chrome", "--no-sandbox", "--disable-dev-shm-usage",
                "--no-first-run", "--no-default-browser-check", "--password-store=basic",
                "--force-renderer-accessibility", "--remote-debugging-port=0",
                "--remote-allow-origins=*", f"--user-data-dir={work / 'profile'}",
                "--window-position=0,0", "--window-size=1280,900", fixture.as_uri() + "#n",
            ])
            deadline = time.monotonic() + 30
            while True:
                try:
                    port = (work / "profile/DevToolsActivePort").read_text().splitlines()[0]
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as reply:
                        page = next(p for p in json.load(reply) if p["type"] == "page")
                    break
                except (OSError, StopIteration, IndexError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.2)
            ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=10)
            sequence = 0

            def evaluate(expression):
                nonlocal sequence
                sequence += 1
                ws.send(json.dumps({"id": sequence, "method": "Runtime.evaluate", "params": {
                    "expression": expression, "returnByValue": True,
                }}))
                while True:
                    message = json.loads(ws.recv())
                    if message.get("id") == sequence:
                        assert "exceptionDetails" not in message["result"], message
                        return message["result"]["result"].get("value")

            driver = LinuxDriver()
            store = safety.PermissionStore(work / "permissions.json")
            store.set_tier("chrome", safety.Tier.FULL)
            runtime = Runtime(driver=driver, store=store, audit=safety.AuditLog(work / "audit"))
            time.sleep(2)
            for kind in ("n", "a"):
                evaluate("location.href=" + json.dumps(fixture.as_uri() + "#" + kind)
                         + "; location.reload()")
                time.sleep(1)
                for trial in range(3):
                    dialog = "dn" if kind == "n" else "da"
                    evaluate("if(!dn.open)dn.showModal()" if kind == "n" else "da.hidden=false")
                    app = driver.frontmost_app()[0]

                    def document_bounds():
                        queue = deque([_atspi.find_root(app, Scope.WINDOW)])
                        while queue:
                            node = queue.popleft()
                            if _atspi._role_name(node) == "document web":
                                return _atspi._raw_rect(node, "SCREEN")
                            queue.extend(node.get_child_at_index(i)
                                         for i in range(node.get_child_count()))
                        raise AssertionError("No native document bounds")

                    box = driver._run(document_bounds)
                    selector = "#dn p" if kind == "n" else "#da p"
                    point = evaluate("(()=>{let r=document.querySelector("
                                     + json.dumps(selector) + ").getBoundingClientRect();"
                                     "return [r.x+r.width/2,r.y+r.height/2]})()")
                    # Fixture setup returns keyboard focus from chrome to the
                    # modal's reported text area, without pressing a control.
                    subprocess.run(["xdotool", "mousemove", str(int(box[0] + point[0])),
                                    str(int(box[1] + point[1])), "click", "1"], check=True)
                    evaluate(dialog + ".querySelector('button').focus()")
                    time.sleep(.15)
                    if not baseline:
                        try:
                            runtime.type_text("https://example.test/\n", app="chrome")
                        except ComputerUseError as exc:
                            assert exc.detail["reason"] == "native_dialog_open_focus_moved", exc
                        else:
                            raise AssertionError("Implicit modal typing was allowed")
                    assert evaluate("ins") == 0
                    snapshot = runtime.desktop_snapshot("chrome", mode="interactive")
                    assert "Address and search bar" in snapshot, snapshot
                    started = time.perf_counter()
                    result = runtime.click(x=500, y=62, verify=True)
                    elapsed = time.perf_counter() - started
                    focus, _ = driver._run(lambda: _atspi._focused_node(app))
                    assert "Address" in driver._run(lambda: _atspi._node_name(focus)), result
                    typed = runtime.type_text("https://example.test/", app="chrome")
                    actual = driver._run(lambda: _atspi._atspi().Text.get_text(focus, 0, -1))
                    assert "https://example.test/" in actual, typed
                    assert evaluate("ins") == 0
                    print(f"PASS {kind} {trial}: address focus/read-back, inside=0; "
                          f"click={elapsed:.3f}s; implicit guard={not baseline}", flush=True)
                    subprocess.run(["xdotool", "key", "Escape"], check=True)
        finally:
            if runtime is not None:
                runtime.close()
            if ws is not None:
                ws.close()
            for proc in reversed(processes):
                if proc.poll() is None:
                    proc.terminate()
            for proc in processes:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


if __name__ == "__main__":
    main()
