"""One isolated native-app/MCP sample. Invoked by run.py, never by pytest."""
from __future__ import annotations

import argparse
import asyncio
import gzip
import functools
import http.server
import shutil
import threading
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

FIXTURES = Path(__file__).with_name("fixtures")


def ref(text, title):
    for line in text.splitlines():
        match = re.match(r"\s*(e\d+)\s", line)
        if match and title in line:
            return match[1]
    raise AssertionError(f"Missing {title!r}: {text}")


async def settled_snapshot(snapshot, markers, timeout=30.0, pause=0.25):
    """Snapshot until every marker is present, or fail with the last tree.

    Chrome registers on AT-SPI before its renderer exposes the page, so the
    first snapshot can hold only the browser UI. That is fixture setup, not a
    measured step: wait for the page controls before the timer starts.
    """
    deadline = time.monotonic() + timeout
    while True:
        text = await snapshot()
        if all(marker in text for marker in markers):
            return text
        if time.monotonic() >= deadline:
            raise AssertionError(f"Page did not expose {list(markers)} in {timeout}s: {text}")
        await asyncio.sleep(pause)


def run_once_click(python, point, env):
    """Run the measured CLI once, preserving crash diagnostics on failure."""
    command = [python, str(Path(__file__).with_name("run_once.py")), "run-once",
               json.dumps(dict(tool="click", x=int(point.x), y=int(point.y)))]
    result = subprocess.run(command, env=dict(env, PYTHONFAULTHANDLER="1"),
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"run-once exited {result.returncode}: "
                           f"stdout={result.stdout!r} stderr={result.stderr!r}")
    return result


async def sample(row, head):
    from a11y_computer_use import safety
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Scope

    driver = LinuxDriver()
    driver.ensure_trusted()
    with tempfile.TemporaryDirectory(prefix="a11y-latency-") as temp:
        home = Path(temp)
        env = dict(os.environ, HOME=temp, XDG_CACHE_HOME=str(home / "cache"),
                   A11Y_VISIBLE_ACTIONS="0", NO_AT_BRIDGE="0", GTK_MODULES="gail:atk-bridge")
        app = ("soffice.bin" if row.startswith("R12-") else "bigtree" if row == "R5"
               else "mousepad" if row == "R7" else "firefox" if row == "R2" else "chrome")
        store = safety.PermissionStore(home / ".a11y-computer-use" / "permissions.json")
        for alias in (app, "firefox-bin" if app == "firefox" else app,
                      "python3" if app == "bigtree" else app,
                      "python3.13" if app == "bigtree" else app,
                      "python" if app == "bigtree" else app):
            store.set_tier(alias, safety.Tier.FULL)
        if row.startswith("R12-"):
            args = ["libreoffice", "-env:UserInstallation=" + (home / "lo").as_uri(),
                    "--writer", "--norestore"]
        elif row == "R5":
            args = [sys.executable, str(FIXTURES / "bigtree.py"), "20000"]
        elif row == "R7":
            args = ["mousepad", "--disable-server"]
        elif row == "R2":
            page = home / "seven-summits.html"
            page.write_bytes(gzip.decompress((FIXTURES / "seven-summits.html.gz").read_bytes())
                             + b"<style>" + gzip.decompress((FIXTURES / "seven-summits.css.gz").read_bytes()) + b"</style>"
                             + b"<script>" + (FIXTURES / "offline-sort.js").read_bytes() + b"</script>")
            (home / "user.js").write_text('user_pref("browser.aboutwelcome.enabled",false);user_pref("browser.shell.checkDefaultBrowser",false);user_pref("browser.startup.homepage_override.mstone","ignore");user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification",true);')
            args = [os.environ.get("FIREFOX", "firefox"), "--no-remote", "--width", "1280", "--height", "800", "--profile", temp, page.as_uri()]
        else:
            args = [os.environ.get("CHROME", "google-chrome"), "--no-sandbox", "--force-renderer-accessibility",
                    "--no-first-run", "--no-default-browser-check", "--disable-dev-shm-usage", "--disable-gpu",
                    "--password-store=basic", "--window-size=1280,800", f"--user-data-dir={home / 'chrome'}",
                    (FIXTURES / "controls.html").as_uri()]
        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def end_headers(self):
                self.send_header("Content-Security-Policy",
                    "default-src 'none'; script-src 'unsafe-inline'; "
                    "style-src 'unsafe-inline'; img-src data:; frame-src 'self'")
                super().end_headers()
        for fixture in FIXTURES.glob("*.html"):
            shutil.copyfile(fixture, home / fixture.name)
        http_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
            functools.partial(QuietHandler, directory=temp))
        threading.Thread(target=http_server.serve_forever, daemon=True).start()
        if app in {"chrome", "firefox"}:
            page_name = {"R2": "seven-summits.html", "R1": "top.html", "R1-iframe": "ifr.html",
                         **{f"R13-{op}": "latbig.html" for op in ("click", "type", "key")}}.get(row, "controls.html")
            args[-1] = f"http://127.0.0.1:{http_server.server_port}/{page_name}"
        with open(home / "app.log", "w") as log:
            process = subprocess.Popen(args, env=env, stdout=log, stderr=log, start_new_session=True)
            try:
                # Wait for registration without warming the R5 snapshot/tree.
                from a11y_computer_use.drivers import _linux_system
                deadline = time.monotonic() + 30
                while not any(app in str(w.get("app", "")).lower()
                              or (row == "R5" and w.get("title") == "Big Tree")
                              for w in _linux_system.windows()):
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise AssertionError(("app registration", log.name, process.poll()))
                    await asyncio.sleep(.1)
                from a11y_computer_use.drivers import _atspi
                while driver._run(lambda: _atspi.find_root(app, Scope.WINDOW)) is None:
                    assert time.monotonic() < deadline, "AT-SPI registration did not settle"
                    await asyncio.sleep(.1)
                params = StdioServerParameters(command=sys.executable, args=["-m", "a11y_computer_use", "mcp"], env=env)
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        outcomes = []
                        async def call(tool, **kwargs):
                            reply = await session.call_tool(tool, kwargs)
                            text = "\n".join(getattr(c, "text", "") for c in reply.content)
                            assert not reply.isError, (tool, kwargs, text)
                            if tool in {"click", "type", "key"}:
                                outcomes.append(getattr(reply, "structuredContent", None))
                            return text
                        async def snapshot():
                            return await call("desktop_snapshot", app=app, scope="window", mode="full")
                        if row == "R5":
                            start = time.perf_counter()
                            text = await call("desktop_snapshot", app=app, scope="window", mode="interactive")
                            elapsed = time.perf_counter() - start
                            assert "Row 00001" in text and "opaque_region" not in text
                            # RSS belongs to the server, not the client or GTK app.
                            # Discover the direct MCP child by parent PID and command.
                            children = []
                            for status in Path("/proc").glob("[0-9]*/status"):
                                try:
                                    data = status.read_text()
                                    if int(status.parent.name) != process.pid and re.search(rf"^PPid:\s+{os.getpid()}$", data, re.M):
                                        children.append(int(re.search(r"^VmRSS:\s+(\d+)", data, re.M)[1]))
                                except (OSError, TypeError):
                                    pass
                            assert len(children) == 1, children
                            return dict(seconds=elapsed, rss_kib=children[0])
                        text = await snapshot()
                        if row.startswith("R12-"):
                            # Fixture setup only: use the real Tools > Options menu.
                            await call("app", name=app, action="focus")
                            subprocess.run(["xdotool", "key", "--clearmodifiers", "alt+t"], check=True)
                            await asyncio.sleep(.5)
                            subprocess.run(["xdotool", "key", "o"], check=True)
                            ready = time.monotonic() + 15
                            while True:
                                text = await snapshot()
                                if 'dialog "Options' in text:
                                    break
                                assert time.monotonic() < ready, text
                                await asyncio.sleep(.1)
                            if row == "R12-find":
                                start = time.perf_counter()
                                receipt = await call("find", app=app, text="Fonts", role="cell")
                                elapsed = time.perf_counter() - start
                                assert 'cell "Fonts"' in receipt, receipt
                            else:
                                found = await call("find", app=app, text="Fonts", role="cell")
                                start = time.perf_counter()
                                receipt = await call("click", ref=ref(found, 'cell "Fonts"'))
                                elapsed = time.perf_counter() - start
                                after = await snapshot()
                                assert re.search(r'cell "Fonts"[^\n]*selected', after), (receipt, after)
                                assert outcomes[-1] and outcomes[-1]["outcome"] == "partial", (receipt, outcomes[-1])
                        elif row.startswith("R13-"):
                            text = await settled_snapshot(snapshot, ('button "Go"', 'textfield "Name"'))
                            if row == "R13-key":
                                await call("type", ref=ref(text, 'textfield "Name"'), text="hello")
                            start = time.perf_counter()
                            if row == "R13-click":
                                receipt = await call("click", ref=ref(text, 'button "Go"'))
                                expected = "c=1 s=0"
                            elif row == "R13-type":
                                receipt = await call("type", ref=ref(text, 'textfield "Name"'), text="world")
                                expected = "name=world"
                            else:
                                receipt = await call("key", app=app, chord="Return")
                                expected = "c=0 s=1"
                            elapsed = time.perf_counter() - start
                            assert outcomes[-1] and outcomes[-1]["outcome"] == "confirmed", (receipt, outcomes[-1])
                            assert expected in await snapshot(), receipt
                        elif row == "R7":
                            await call("click", ref=ref(text, '"Help"'))
                            text = await snapshot()
                            target = ref(text, '"About')
                            start = time.perf_counter()
                            receipt = await call("click", ref=target)
                            elapsed = time.perf_counter() - start
                            assert "About Mousepad" in await snapshot(), receipt
                        elif row == "R2":
                            # Find is outside the CLI-click timer, as in #1067.
                            await call("scroll_to_find", app=app, text="First ascent")
                            await call("find", app=app, text="First ascent")
                            driver = LinuxDriver()
                            snap = driver.snapshot(Scope.WINDOW, app)
                            header = next(el for el in snap.elements if el.title == "First ascent" and el.bounds)
                            point = header.bounds.center
                            start = time.perf_counter()
                            reply = run_once_click(sys.executable, point, env)
                            elapsed = time.perf_counter() - start
                            assert "refused" not in reply.stdout
                            # The fixture changes this native window title only
                            # after the real table row order actually changes.
                            settled = time.monotonic() + 2
                            while not any("Offline sort confirmed" in w.get("title", "")
                                          for w in _linux_system.windows()):
                                assert time.monotonic() < settled, reply.stdout
                                await asyncio.sleep(.05)
                        elif row == "R8":
                            driver = LinuxDriver()
                            snap = driver.snapshot(Scope.WINDOW, app)
                            box = next(el.bounds for el in snap.elements if el.title == "Pointer box" and el.bounds)
                            x, y = int(box.center.x), int(box.center.y)
                            path = [[x + i * 15, y] for i in range(18)]
                            start = time.perf_counter()
                            receipt = await call("drag", start_x=x, start_y=y, end_x=x+17*15, end_y=y, path=path)
                            elapsed = time.perf_counter() - start
                            assert "Drag completed" in await snapshot(), receipt
                        elif row in {"R1", "R1-iframe"}:
                            found = await call("find", app=app, text="Agree box")
                            target = ref(found, '"Agree box"')
                            start = time.perf_counter()
                            receipt = await call("click", ref=target)
                            elapsed = time.perf_counter() - start
                            after = await snapshot()
                            assert re.search(r"\bchecked\b", after), receipt
                        elif row == "R9":
                            await call("click", ref=ref(text, '"Change title"'))
                            fresh = await snapshot()
                            receipt = await call("click", ref=ref(fresh, '"Row action"'))
                            assert "Row activated" in await snapshot(), receipt
                            return dict(correct=True)
                        elif row == "R10":
                            await call("click", ref=ref(text, '"Open modal"'))
                            text = await snapshot()
                            for _ in range(3):
                                reply = await session.call_tool("click", dict(ref=ref(text, '"Background Action"')))
                                error = " ".join(getattr(c, "text", "") for c in reply.content)
                                assert reply.isError and "outside_active_modal" in error, error
                            await call("key", chord="ctrl+l", app=app)
                            return dict(correct=True, refusals=3, shortcuts_not_refused=["ctrl+l"])
                        else:
                            raise ValueError(row)
                        return dict(seconds=elapsed, outcome=outcomes[-1] if outcomes else None)
            finally:
                import signal
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                process.wait(timeout=15)
                http_server.shutdown()
                http_server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("row")
    parser.add_argument("--head", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(sample(args.row, args.head))))
