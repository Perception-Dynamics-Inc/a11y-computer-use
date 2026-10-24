"""Manually verify #1039 against Chrome or Electron through Linux AT-SPI.

Run from the repo root under a private Xvfb/D-Bus session. Requires Chrome,
GI/AT-SPI, Xlib and websocket-client. This is not part of the hermetic suite.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request

import websocket

from a11y_computer_use import safety
from a11y_computer_use.server import Runtime
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Scope


@contextmanager
def serve_fixture(fixture: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            content = fixture.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as http:
        thread = Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{http.server_port}/{fixture.name}"
        finally:
            http.shutdown()
            thread.join()


def main(electron: str | None = None) -> None:
    app = "electron" if electron else "chrome"
    driver = LinuxDriver()
    driver.ensure_trusted()
    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/chromium_title_change.html"
    with serve_fixture(fixture) as url, tempfile.TemporaryDirectory(prefix="chromium-title-change-") as profile:
        with open(Path(profile) / "chrome.log", "w") as log:
            flags = ["--force-renderer-accessibility", "--no-sandbox",
                     "--disable-gpu", "--disable-dev-shm-usage", "--no-first-run",
                     "--no-default-browser-check", "--password-store=basic",
                     "--window-size=1000,800", "--remote-allow-origins=*",
                     "--remote-debugging-port=0", f"--user-data-dir={profile}"]
            if electron:
                main_js = Path(profile) / "main.js"
                main_js.write_text("const {app,BrowserWindow}=require('electron');"
                                   "app.whenReady().then(()=>{const w=new BrowserWindow({width:1000,height:800});"
                                   f"w.loadURL({json.dumps(url)});" + "});")
                command = [electron, *flags, str(main_js)]
            else:
                command = ["google-chrome", *flags, url]
            proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            ws = None
            try:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        port = (Path(profile) / "DevToolsActivePort").read_text().splitlines()[0]
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json") as reply:
                            pages = json.load(reply)
                        page = next(p for p in pages if "title_change" in p["url"])
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

                # Wait for Chrome's startup chrome to settle before issuing refs.
                # These driver-only observations do not authorize any actions.
                previous, stable = None, 0
                deadline = time.monotonic() + 30
                while stable < 3:
                    shot = driver.snapshot(Scope.WINDOW, app)
                    signature = tuple((el.ref, el.role, el.title, el.parent) for el in shot.elements)
                    stable = stable + 1 if signature == previous else 0
                    previous = signature
                    assert time.monotonic() < deadline, "Startup tree did not settle"
                    time.sleep(.25)

                store = safety.PermissionStore(Path(profile) / "permissions.json")
                store.set_tier(app, safety.Tier.FULL)
                with Runtime(driver=driver, store=store,
                             audit=safety.AuditLog(Path(profile) / "audit")) as runtime:
                    for name, text, expected in (
                        ("Title", "Milk", "Notes Lab"),
                        ("Save", None, "Notes Lab - saved:Milk"),
                        ("Meeting", None, "Notes Lab - Meeting"),
                        ("Tags", None, "Notes Lab - Tags"),
                        ("Pinned", None, "Notes Lab - Pinned"),
                        ("Body", "hello world", "Notes Lab - body:hello world"),
                    ):
                        deadline = time.monotonic() + 30
                        while True:
                            runtime.desktop_snapshot(app, scope="window", mode="interactive")
                            targets = [el for el in runtime._current.elements if el.title == name and el.actionable]
                            if targets:
                                break
                            assert time.monotonic() < deadline, runtime._current
                            time.sleep(.25)
                        target = targets[0]
                        result = (runtime.type_text(text, ref=target.ref) if text is not None
                                  else runtime.click(ref=target.ref, verify=True))
                        assert result.outcome == "confirmed", (name, result.as_dict(), result)
                        assert evaluate("document.title") == expected, (name, result, evaluate("document.title"))
                        print(f"PASS {app}: {name}: {expected}", flush=True)

                    # Rename outside Runtime, so no verified click can authorize
                    # the next name. Only the new public observation binds it.
                    for n in range(3):
                        evaluate(f"document.querySelector('#counter').textContent='Count {n}'")
                        runtime.desktop_snapshot(app, scope="window", mode="interactive")
                        target = next(el for el in runtime._current.elements
                                      if el.title == f"Count {n}" and el.actionable)
                        assert runtime._anchor(target.ref)[1].title == f"Count {n}"
                        result = runtime.click(ref=target.ref, verify=True)
                        assert result.outcome == "confirmed", result.as_dict()
                        assert evaluate("Number(document.querySelector('#counter').dataset.clicks)") == n + 1
                        print(f"PASS {app}: Count {n}: fresh renamed ref clicked", flush=True)
            finally:
                if ws is not None:
                    ws.close()
                proc.terminate()
                proc.wait(timeout=15)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--electron", help="Path to Electron executable (otherwise Chrome)")
    args = parser.parse_args()
    main(args.electron)
