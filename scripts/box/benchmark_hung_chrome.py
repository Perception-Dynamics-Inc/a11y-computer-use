"""Private Chrome busy-loop regression; no pytest live suite is invoked.

PYTHONPATH=. XDG_CACHE_HOME=$(mktemp -d) dbus-run-session -- xvfb-run -a \
  /workspace/a11y-venv-codex/bin/python scripts/box/benchmark_hung_chrome.py [--cdp]
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from a11y_computer_use import safety, server
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.drivers import _atspi_events
from a11y_computer_use.schema import ComputerUseError

HTML = '''<title>Busy Lab c=0</title><style>button,input {margin:30px;padding:20px}</style>
<button onclick="setTimeout(()=>{const end=performance.now()+6000;while(performance.now()<end){}},100)">Freeze</button>
<button onclick="document.title='Busy Lab c='+(++window.count)">Go</button>
<input aria-label="Name"><script>window.count=0</script>'''


def main():
    with tempfile.TemporaryDirectory(prefix="hung-chrome-") as folder:
        root = Path(folder)
        (root / "page.html").write_text(HTML)
        wm = subprocess.Popen(["xfwm4", "--compositor=off"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        driver = LinuxDriver()
        driver.ensure_trusted()
        flags = ["--remote-debugging-port=0"] if "--cdp" in sys.argv else []
        browser = subprocess.Popen(["google-chrome", "--no-sandbox", "--disable-dev-shm-usage",
            "--force-renderer-accessibility", "--no-first-run", "--no-default-browser-check",
            "--password-store=basic", f"--user-data-dir={root / 'profile'}", *flags,
            (root / "page.html").as_uri()], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            store = safety.PermissionStore(root / "permissions.json")
            store.set_tier("google-chrome", safety.Tier.FULL)
            runtime = server.Runtime(store=store, audit=safety.AuditLog(root / "audit"), driver=driver)
            deadline = time.monotonic() + 15
            while True:
                try:
                    runtime.desktop_snapshot("google-chrome")
                    if any(el.title == "Freeze" for el in runtime._current.elements): break
                except Exception:
                    if time.monotonic() >= deadline: raise
                if time.monotonic() >= deadline: raise RuntimeError("fixture not ready")
                time.sleep(.2)
            clicks = 0
            for tool in ("click", "type", "find") * 3:
                runtime.desktop_snapshot("google-chrome")
                elements = runtime._current.elements
                freeze = next(el for el in elements if el.title == "Freeze")
                target = next(el for el in elements if el.title == ("Name" if tool == "type" else "Go"))
                b = freeze.bounds
                subprocess.run(["xdotool", "mousemove", str(int(b.x+b.width/2)), str(int(b.y+b.height/2)), "click", "1"], check=True)
                time.sleep(.2)
                start = time.monotonic()
                try:
                    if tool == "click": result = runtime.click(ref=target.ref)
                    elif tool == "type": result = runtime.type_text("abc", ref=target.ref)
                    else: result = runtime.find("google-chrome", text="Go")
                    row = {"outcome": getattr(result, "outcome", None), "next": getattr(result, "next", None),
                           "evidence": getattr(result, "evidence", str(result))}
                except ComputerUseError as exc:
                    row = {"error": exc.code.value, **exc.detail}
                elapsed = time.monotonic()-start
                row.update(tool=tool, elapsed_s=round(elapsed, 3), cdp=bool(flags))
                print(json.dumps(row), flush=True)
                if tool in {"click", "type"}: assert not row.get("next"), row
                if tool == "click": assert row.get("outcome") != "suspected_noop", row
                if tool == "find":
                    assert (row.get("reason") == "page_not_responding" or
                            "accessibility tree may be stale" in row.get("evidence", "")), row
                time.sleep(max(0, 6.2 - (time.monotonic()-start)))
                runtime.desktop_snapshot("google-chrome")
                names = [el.title for el in runtime._current.elements]
                titles = [name for name in names if "Busy Lab c=" in name]
                field = next(el for el in runtime._current.elements if el.title == "Name")
                assert not field.value, field
                if tool == "click" and row.get("outcome") == "unverifiable" and not row.get("error"):
                    clicks += 1
                assert any(f"Busy Lab c={clicks}" in name for name in titles), titles
                print(json.dumps({"recovered_titles": titles, "field": field.value}), flush=True)
            field = next(el for el in runtime._current.elements if el.title == "Name")
            result = runtime.type_text("healthy", ref=field.ref)
            assert result.outcome == "confirmed", result
            runtime.desktop_snapshot("google-chrome")
            assert next(el for el in runtime._current.elements if el.title == "Name").value == "healthy"
            print(json.dumps({"recovered_type": result.outcome}), flush=True)
        finally:
            for process in (browser, wm):
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
            thread = _atspi_events._thread
            if thread is not None and thread.is_alive():
                from gi.repository import Atspi
                _atspi_events.submit(Atspi.event_quit)
                thread.join(timeout=2)


if __name__ == "__main__": main()
