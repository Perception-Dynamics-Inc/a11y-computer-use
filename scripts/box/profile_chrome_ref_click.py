#!/usr/bin/env python3
"""Profile #1019 revisions against one Chrome session, outside pytest.

Run under a private Xvfb/window-manager/D-Bus session with the Linux backend
installed. Example (repeat checkouts to interleave batches)::

    python scripts/box/profile_chrome_ref_click.py /tmp/v0476 /tmp/main /tmp/fix \
        /tmp/v0476 /tmp/main /tmp/fix --runs 6

Reports actual _safe invocations (including non-getter native calls), snapshot
and identity/discovery step times, and end-to-end medians. Step times overlap.
Every click must confirm and an independent native read must see the counter.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import functools
import http.server
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading
import time

PAGE = '''<!doctype html><title>Ref click profile</title>
<button onclick="document.getElementById('count').textContent='Value '+(++window.n)">Outside</button>
<p id="count">Value 0</p><script>window.n=0</script>''' + ''.join(
    f'<label>Field {i}<input></label><button>Button {i}</button>' for i in range(6)
) + '''
<button popovertarget="auto">Auto popover</button><div id="auto" popover>Automatic</div>
<button popovertarget="manual">Manual popover</button>
<div id="manual" popover="manual">Manual</div>
<details><summary>Details</summary>Details content</details>
<select><option>First</option><option>Second</option></select>
<input list="choices"><datalist id="choices"><option value="Choice"></datalist>
<input type="date"><input type="color">
'''


def worker(checkout: Path, runs: int) -> None:
    sys.path.insert(0, str(checkout.resolve()))
    from a11y_computer_use import safety, server
    from a11y_computer_use.drivers import _atspi
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Scope

    steps = defaultdict(lambda: [0, 0.0])
    for name in ("_safe", "chromium_row_identity", "_list_containers", "_in_view_named_rows"):
        original = getattr(_atspi, name, None)
        if original is None:  # Older revisions have no row-identity guard.
            continue
        def measured(*args, _name=name, _fn=original, **kwargs):
            label = _name
            if _name == "_safe":
                defaults = getattr(args[0], "__defaults__", None)
                if defaults:
                    label = "native:" + getattr(defaults[0], "__name__", "other")
            start = time.perf_counter()
            try:
                return _fn(*args, **kwargs)
            finally:
                steps[label][0] += 1
                steps[label][1] += time.perf_counter() - start
        setattr(_atspi, name, measured)

    with tempfile.TemporaryDirectory(prefix="ref-click-profile-") as tmp:
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
        rows = []
        for _ in range(runs):
            runtime.desktop_snapshot("chrome", scope="window", mode="interactive")
            target = next(el for el in runtime._current.elements if el.title == "Outside")
            full = original_snapshot(Scope.WINDOW, "chrome")
            previous = next(int(el.title.split()[1]) for el in full.elements
                            if el.title.startswith("Value "))
            reads.clear()
            steps.clear()
            start = time.perf_counter()
            result = runtime.click(ref=target.ref)
            row = dict(checkout=str(checkout), seconds=time.perf_counter() - start,
                       reads=list(reads), steps={k: list(v) for k, v in steps.items()},
                       outcome=result.outcome, evidence=result.evidence)
            after = original_snapshot(Scope.WINDOW, "chrome")
            assert any(el.title == f"Value {previous + 1}" for el in after.elements), row
            assert result.outcome == "confirmed", row
            rows.append(row)
            print(json.dumps(row), flush=True)
        print(json.dumps(dict(checkout=str(checkout), median=statistics.median(
            row["seconds"] for row in rows))), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkouts", nargs="+", type=Path)
    parser.add_argument("--runs", type=int, default=6)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("runs must be positive")
    if args.worker:
        worker(args.checkouts[0], args.runs)
        return
    with tempfile.TemporaryDirectory(prefix="chrome-ref-profile-") as tmp:
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
            for checkout in args.checkouts:
                subprocess.run([sys.executable, __file__, str(checkout), "--worker",
                                "--runs", str(args.runs)], check=True)
        finally:
            chrome.terminate()
            chrome.wait(timeout=15)
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    main()
