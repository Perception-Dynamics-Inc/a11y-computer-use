#!/usr/bin/env python3
"""Measure issue #850 outside pytest in a disposable Xvfb/Openbox/D-Bus session.

Pass a checkout to compare revisions using the same harness and interpreter::

    python scripts/box/profile_chrome_click.py /path/to/checkout --runs 10

Chrome needs --force-renderer-accessibility (supplied here). The caller must
start the window manager and set A11Y_VISIBLE_ACTIONS=0. Each measured click
follows a fresh Runtime snapshot; independent, untimed native reads verify the
counter afterwards. No CDP endpoint or action bypass is used.
"""
from __future__ import annotations

import argparse
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

PAGE = """<!doctype html><html><head><title>One button</title></head><body><button id="b" onclick="this.textContent='Count '+(++window.n)">Count 0</button><script>window.n=0</script></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()
    if args.runs < 5:
        parser.error("at least five runs are required")
    sys.path.insert(0, str(args.checkout.resolve()))
    from a11y_computer_use import server, safety
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Scope

    with tempfile.TemporaryDirectory(prefix="click850-") as tmp:
        path = Path(tmp)
        (path / "index.html").write_text(PAGE, encoding="utf-8")
        httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            functools.partial(http.server.SimpleHTTPRequestHandler, directory=tmp),
        )
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        chrome = subprocess.Popen([
            "google-chrome", "--no-sandbox", "--disable-dev-shm-usage",
            "--no-first-run", "--no-default-browser-check", "--password-store=basic",
            "--force-renderer-accessibility", f"--user-data-dir={tmp}/profile",
            f"http://127.0.0.1:{httpd.server_port}/index.html",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            driver = LinuxDriver(visible_actions=False)
            store = safety.PermissionStore(path / "permissions.json")
            store.set_tier("chrome", safety.Tier.FULL)
            runtime = server.Runtime(
                store=store, audit=safety.AuditLog(path / "audit"), driver=driver,
            )
            for _ in range(30):
                time.sleep(1)
                try:
                    runtime.desktop_snapshot("chrome", scope="window", mode="interactive")
                    if any(el.title == "Count 0" for el in runtime._current.elements):
                        break
                except Exception:
                    pass  # Chrome may not have registered its accessibility tree yet.
            else:
                raise RuntimeError("Chrome button unavailable")
            original = driver.snapshot
            reads = []

            def snapshot(*args, **kwargs):
                start = time.perf_counter()
                result = original(*args, **kwargs)
                reads.append(time.perf_counter() - start)
                return result

            driver.snapshot = snapshot
            rows = []
            for n in range(args.runs):
                runtime.desktop_snapshot("chrome", scope="window", mode="interactive")
                target = next(el for el in runtime._current.elements
                              if el.title == f"Count {n}" and el.role == "AXButton")
                reads.clear()
                start = time.perf_counter()
                result = runtime.click(ref=target.ref)
                elapsed = time.perf_counter() - start
                row = dict(seconds=elapsed, reads=list(reads), outcome=result.outcome,
                           evidence=result.evidence)
                after = original(Scope.WINDOW, "chrome")
                assert any(el.title == f"Count {n + 1}" for el in after.elements), row
                assert result.outcome == "confirmed", row
                rows.append(row)
                print(json.dumps(row), flush=True)
            print("MEDIAN", statistics.median(row["seconds"] for row in rows), flush=True)
        finally:
            chrome.terminate()
            chrome.wait(timeout=15)
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    main()
