"""Live #800 probe; run inside a private D-Bus/Xvfb session with Chrome installed.

A11Y_VISIBLE_ACTIONS=0 dbus-run-session -- xvfb-run -a -s '-screen 0 1280x800x24' \
    python scripts/box/repro_chromium_transform.py

Uses the issue's unchanged minimal page. --expect-misclick verifies the old
failure on main. This is an explicit live probe, not part of pytest collection.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import tempfile
import time

from a11y_computer_use import safety, server
from a11y_computer_use.drivers.linux import LinuxDriver

PAGE = """<!doctype html><title>STALE</title>
<style>.vp{width:600px;height:80px;overflow:hidden}.track{display:flex;width:2400px}
.track button{width:280px;height:40px;margin:20px 310px 20px 10px;flex:none}</style>
<div class="vp"><div class="track" id="t" style="transform:translateX(0px)">
  <button id="a">Item A</button><button id="b">Item B</button></div></div>
<script>
let hit='-',sh=0; const T=()=>document.title=`STALE shifted=${sh} hit=${hit}`;
document.querySelectorAll('button').forEach(b=>b.onclick=()=>{hit=b.id;T()});
setTimeout(()=>{sh=1;document.getElementById('t').style.transform='translateX(-600px)';T()},6000); T();
</script>
"""


def main() -> None:
    """Assert the painted carousel result and the Runtime delivery receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-misclick", action="store_true")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="chromium-transform-"))
    page = root / "page.html"
    page.write_text(PAGE)
    print(f"source={server.__file__} artifacts={root}", flush=True)
    wm = subprocess.Popen(["xfwm4", "--compositor=off"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
    chrome = None
    try:
        time.sleep(0.5)
        chrome = subprocess.Popen([
            "google-chrome", "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run",
            "--no-default-browser-check", "--password-store=basic",
            "--force-renderer-accessibility", "--start-maximized",
            f"--user-data-dir={root / 'profile'}", page.as_uri(),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        driver = LinuxDriver(visible_actions=False)
        store = safety.PermissionStore(root / "permissions.json")
        store.set_tier("chrome", safety.Tier.FULL)
        runtime = server.Runtime(store=store, audit=safety.AuditLog(root / "audit"), driver=driver)
        target = None
        for _ in range(30):
            try:
                text = runtime.desktop_snapshot(app="chrome", mode="interactive", include_bounds=True)
                target = next(e for e in runtime._current.elements
                              if e.title == "Item A" and e.role == "AXButton")
                break
            except Exception:
                time.sleep(0.1)
        assert target is not None, "Chrome did not publish Item A before the timer"
        print(text, flush=True)

        def title() -> str:
            return subprocess.check_output(
                ["xdotool", "getactivewindow", "getwindowname"], text=True,
            ).strip()

        assert "shifted=0" in title(), title()
        deadline = time.monotonic() + 10
        while "shifted=1" not in title() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "shifted=1 hit=-" in title(), title()
        time.sleep(0.5)
        result = runtime.click(ref=target.ref)
        time.sleep(0.2)
        after = title()
        print(f"result={result}\noutcome={result.outcome}\ntitle={after}", flush=True)
        assert result.outcome == "confirmed", result
        if args.expect_misclick:
            assert "hit=b" in after, after
            assert "via AT-SPI doAction" not in result, result
        else:
            assert "hit=a" in after, after
            assert "via AT-SPI doAction(press)" in result or "via AT-SPI doAction(click)" in result
        print("PASS", flush=True)
    finally:
        if chrome is not None:
            chrome.terminate()
            chrome.wait(timeout=5)
        wm.terminate()
        wm.wait(timeout=5)


if __name__ == "__main__":
    main()
