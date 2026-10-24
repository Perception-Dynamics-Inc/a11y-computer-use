#!/usr/bin/env python3
"""Profile Linux ref/path drag latency on a fresh Chrome page per sample.

Run under private Xvfb/Xfwm4/D-Bus with the Linux backend and Pillow installed:
    python scripts/box/profile_linux_drag.py /tmp/main /tmp/fix --runs 3

Reports overlapping native probe times and full snapshot reads. Ref drags
must confirm movement and independently move the native bounds; path drags
must change canvas pixels (their accessibility outcome may be unverifiable).
This is a standalone benchmark, not a pytest live suite.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import functools
import http.server
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

PAGE = """<!doctype html><title>Drag profile</title>
<div id=box role=button aria-label="Pointer box" style="position:absolute;left:50px;top:100px;width:100px;height:50px;background:red;touch-action:none"></div>
<div role=button aria-label="Pointer zone" style="position:absolute;left:500px;top:100px;width:120px;height:80px;background:gray"></div>
<canvas id=cv width=800 height=450 aria-label="Drawing canvas" style="position:absolute;left:50px;top:220px;border:1px solid"></canvas>
<script>
box.onpointerdown=e=>{box.setPointerCapture(e.pointerId);box.down=true};
box.onpointermove=e=>{if(box.down){box.style.left=(e.clientX-50)+'px';box.style.top=(e.clientY-25)+'px'}};
box.onpointerup=e=>{box.down=false};
const g=cv.getContext('2d');let d=false;
cv.onpointerdown=e=>{d=true;g.beginPath();g.moveTo(e.offsetX,e.offsetY)};
cv.onpointermove=e=>{if(d){g.lineTo(e.offsetX,e.offsetY);g.stroke()}};
cv.onpointerup=()=>d=false;
</script>"""


def worker(checkout: Path, mode: str) -> None:
    sys.path.insert(0, str(checkout.resolve()))
    from a11y_computer_use import safety, server
    from a11y_computer_use.drivers import _atspi
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Scope
    from PIL import ImageGrab, ImageChops

    steps = defaultdict(lambda: [0, 0.0])
    for name in ("chromium_row_identity", "active_modals", "_accessible_at_point"):
        original = getattr(_atspi, name, None)
        if original is None:  # Older revisions have no row-identity guard.
            continue
        def measured(*args, _name=name, _fn=original, **kwargs):
            label = _name
            start = time.perf_counter()
            try:
                return _fn(*args, **kwargs)
            finally:
                steps[label][0] += 1
                steps[label][1] += time.perf_counter() - start
        setattr(_atspi, name, measured)

    with tempfile.TemporaryDirectory(prefix="drag-profile-") as tmp:
        path = Path(tmp)
        driver = LinuxDriver(visible_actions=False)
        store = safety.PermissionStore(path / "permissions.json")
        store.set_tier("chrome", safety.Tier.FULL)
        runtime = server.Runtime(store=store, audit=safety.AuditLog(path / "audit"), driver=driver)
        original_snapshot = driver.snapshot
        reads = []
        def snapshot(*args, **kwargs):
            start = time.perf_counter()
            try:
                return original_snapshot(*args, **kwargs)
            finally:
                reads.append(time.perf_counter() - start)
        driver.snapshot = snapshot
        runtime.desktop_snapshot("chrome", scope="window", mode="interactive")
        source = next(el for el in runtime._current.elements if el.title == "Pointer box")
        target = next(el for el in runtime._current.elements if el.title == "Pointer zone")
        canvas = next(el for el in runtime._current.elements if el.role == "opaque_region")
        bounds = canvas.bounds
        assert bounds is not None and source.bounds is not None
        x, y = int(bounds.x + 40), int(bounds.y + 80)
        region = (int(bounds.x), int(bounds.y),
                  int(bounds.x + bounds.width), int(bounds.y + bounds.height))
        pixels = ImageGrab.grab().crop(region)
        reads.clear()
        steps.clear()
        start = time.perf_counter()
        if mode == "ref":
            result = runtime.drag(start_ref=source.ref, end_ref=target.ref)
        else:
            result = runtime.drag(start_x=x, start_y=y, end_x=x+580, end_y=y,
                                  path=[[x+30*i, y+20] for i in range(1, 19)])
        elapsed = time.perf_counter() - start
        row = dict(checkout=str(checkout), mode=mode, seconds=elapsed,
                   reads=list(reads), steps={k: list(v) for k, v in steps.items()},
                   outcome=result.outcome, evidence=result.evidence)
        if mode == "ref":
            assert result.outcome == "confirmed", row
            after = original_snapshot(Scope.WINDOW, "chrome")
            moved = next(el for el in after.elements if el.title == "Pointer box")
            assert moved.bounds != source.bounds, row
        else:
            assert ImageChops.difference(pixels, ImageGrab.grab().crop(region)).getbbox(), row
        print(json.dumps(row), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkouts", nargs="+", type=Path)
    parser.add_argument("--runs", type=int, default=6)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=["ref", "path"], default="ref")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("runs must be positive")
    if args.worker:
        worker(args.checkouts[0], args.mode)
        return
    for _ in range(args.runs):
        for checkout in args.checkouts:
            for mode in ("ref", "path"):
                sample(checkout, mode)


def sample(checkout: Path, mode: str) -> None:
    """Launch one isolated page so every sample starts before the first drag."""
    with tempfile.TemporaryDirectory(prefix="chrome-drag-profile-") as tmp:
        (Path(tmp) / "index.html").write_text(PAGE)
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=tmp))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        chrome = subprocess.Popen([
            "google-chrome", "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run",
            "--no-default-browser-check", "--password-store=basic", "--force-renderer-accessibility",
            f"--user-data-dir={tmp}/profile", f"http://127.0.0.1:{httpd.server_port}/index.html",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(3)
            subprocess.run([sys.executable, __file__, str(checkout), "--worker",
                            "--mode", mode], check=True)
        finally:
            chrome.terminate()
            chrome.wait(timeout=15)
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    main()
