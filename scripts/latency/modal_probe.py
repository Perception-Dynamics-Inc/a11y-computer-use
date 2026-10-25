"""Native Runtime diagnostic for modal dismissal, inside clicks and Return."""
from __future__ import annotations

import argparse
import cProfile
import json
from pathlib import Path
import subprocess
import tempfile
import time

from a11y_computer_use import safety, server
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import ComputerUseError, Scope

PAGE = '''<title>Latency open</title>
<style>#bd{position:fixed;inset:0;background:#8888}#dl{position:fixed;top:200px;left:340px;width:420px;background:white;padding:20px}</style>
<label>Search field<input onkeydown="if(event.key==='Enter')document.title='Return verified'"></label>
<button style="position:fixed;left:30px;top:100px" onclick="document.title='BACKGROUND ACTIVATED'">Background Action</button>
<div id="bd" onclick="bd.remove();dl.remove();document.title='Latency closed'"></div>
<div id="dl" role="dialog" aria-modal="true" aria-label="Photo viewer">
<button onclick="this.textContent='Inside '+(++window.n)">Inside Action</button></div>
<script>window.n=0</script>'''


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--unsafe-baseline", action="store_true", help="Do not require 0.4.76 to refuse background input")
    parser.add_argument("--profile-return", type=Path)
    args = parser.parse_args()
    results = {}
    with tempfile.TemporaryDirectory(prefix="a11y-modal-probe-") as temp:
        home = Path(temp)
        page = home / "modal.html"
        page.write_text(PAGE)
        driver = LinuxDriver()
        driver.ensure_trusted()
        process = subprocess.Popen([
            "/opt/google/chrome/chrome", "--force-renderer-accessibility", "--no-sandbox",
            "--disable-gpu", "--disable-dev-shm-usage", "--no-first-run",
            "--no-default-browser-check", "--password-store=basic", "--window-size=1280,800",
            f"--user-data-dir={home / 'chrome'}", page.as_uri(),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            store = safety.PermissionStore(home / "permissions.json")
            store.set_tier("chrome", safety.Tier.FULL)
            with server.Runtime(driver=driver, store=store, audit=safety.AuditLog(home / "audit")) as runtime:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        snap = driver.snapshot(Scope.WINDOW, "chrome")
                        if any(el.title == "Photo viewer" for el in snap.elements):
                            break
                    except ComputerUseError:
                        pass
                    assert time.monotonic() < deadline, "Modal did not register"
                    time.sleep(.2)
                for trial in range(5):
                    runtime.desktop_snapshot("chrome", "window", "interactive")
                    target = next(el for el in runtime._current.elements if el.title.startswith("Inside "))
                    start = time.perf_counter()
                    receipt = runtime.click(ref=target.ref)
                    results.setdefault("inside_modal", []).append(time.perf_counter() - start)
                    assert f"Inside {trial + 1}" in runtime.desktop_snapshot("chrome", "window", "interactive"), receipt
                if not args.unsafe_baseline:
                    background = next(el for el in runtime._current.elements if el.title == "Background Action")
                    for _ in range(3):
                        try:
                            runtime.click(ref=background.ref)
                        except ComputerUseError as exc:
                            assert "outside_active_modal" in str(exc)
                        else:
                            raise AssertionError("Input behind modal was allowed")
                    results["background_refusals"] = 3
                for trial in range(5):
                    runtime.desktop_snapshot("chrome", "window", "interactive")
                    start = time.perf_counter()
                    receipt = runtime.click(x=1000, y=650)
                    results.setdefault("backdrop_dismiss", []).append(time.perf_counter() - start)
                    assert "Latency closed" in runtime.desktop_snapshot("chrome", "window", "interactive"), receipt
                    if trial < 4:
                        subprocess.run(["xdotool", "key", "ctrl+r"], check=True)
                        time.sleep(.5)
                if args.profile_return:
                    field = next(el for el in runtime._current.elements if el.editable and el.title == "Search field")
                    runtime.click(ref=field.ref)
                    profile = cProfile.Profile()
                    profile.enable()
                    start = time.perf_counter()
                    receipt = runtime.key("Return", app="chrome")
                    results["return_seconds"] = time.perf_counter() - start
                    profile.disable()
                    profile.dump_stats(str(args.profile_return))
                    assert "Return verified" in runtime.desktop_snapshot("chrome", "window", "interactive"), receipt
        finally:
            import os
            import signal
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            process.wait(timeout=15)
    print(json.dumps(results))


if __name__ == "__main__":
    main()
