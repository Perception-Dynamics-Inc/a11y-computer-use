"""Launch the GTK fixture and the local HTML pages, and read a snapshot.

The live tests share this harness. It talks to the existing Linux and browser
drivers only. It does not import ``a11y_computer_use.agent``.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from a11y_computer_use.schema import ComputerUseError, ErrorCode, Scope

FIXTURES = Path(__file__).resolve().parent
PAGES = FIXTURES / "pages"
GTK_APP = FIXTURES / "gtk_app.py"
APP_NAME = "cuagentfix"

NOTE = "M1 live note\nsecond line\n"
FORM_RESULT = "RESULT name=Ada color=blue subscribe=yes"

# Rendered snapshot lines from observe.render_text:
#   e7 textarea "Notes" ="M1 live note second line " (edit,focus)
_LINE = re.compile(
    r"""(?m)^[ \t]*(e\d+)\s+(\S+)\s+"([^"]*)"(?:\s+="([^"]*)")?(?:\s+\(([^)]*)\))?"""
)


@dataclass(frozen=True)
class Seen:
    """One element line parsed out of a snapshot the model was shown."""

    ref: str
    role: str
    name: str
    value: str | None
    flags: str


def message_text(messages: object) -> str:
    """Flatten model messages to the text a script can scan for refs."""
    if messages is None:
        return ""
    if not isinstance(messages, (list, tuple)):
        messages = [messages]
    parts: list[str] = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", message)
        parts.append(_content_text(content))
    return "\n".join(part for part in parts if part)


def _content_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return "\n".join(_content_text(block) for block in content)
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "")
        return " ".join(str(value) for value in content.values() if isinstance(value, (str, int)))
    return str(content)


def parse_snapshot(text: str) -> list[Seen]:
    """Element lines in ``text``. Later duplicates replace earlier ones."""
    found: dict[str, Seen] = {}
    for match in _LINE.finditer(text or ""):
        ref, role, name, value, flags = match.groups()
        found[ref] = Seen(ref, role, name, value, flags or "")
    return list(found.values())


def pick(elements: list[Seen], *, role: str, name: str) -> Seen | None:
    matches = [el for el in elements if el.role == role and el.name == name]
    return matches[-1] if matches else None


def launch_gtk() -> subprocess.Popen:
    """Start the fixture app. The caller terminates the process."""
    env = os.environ.copy()
    env["GTK_MODULES"] = "gail:atk-bridge"
    env["NO_AT_BRIDGE"] = "0"
    return subprocess.Popen([sys.executable, str(GTK_APP)], env=env)


def wait_gtk_snapshot(driver, timeout_s: float = 15.0):
    """Poll until ``cuagentfix`` exposes its Save button."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            last = driver.snapshot(Scope.WINDOW, APP_NAME)
        except ComputerUseError as exc:
            if exc.code is not ErrorCode.APP_NOT_FOUND:
                raise
            last = None
        else:
            if any(el.role == "AXButton" and el.title == "Save" for el in last.elements):
                return last
        time.sleep(0.3)
    return last


def stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


class PageSite:
    """Two local origins: the fixture pages, and the cross-origin captcha widget."""

    def __init__(self) -> None:
        self._widget: ThreadingHTTPServer | None = None
        self._pages: ThreadingHTTPServer | None = None
        self.base = ""
        self.widget_url = ""

    def __enter__(self) -> "PageSite":
        widget = _serve(PAGES, {})
        self.widget_url = f"http://127.0.0.1:{widget.server_port}/captcha_widget.html"
        pages = _serve(PAGES, {"CAPTCHA_WIDGET_URL": self.widget_url})
        self.base = f"http://127.0.0.1:{pages.server_port}"
        self._widget = widget
        self._pages = pages
        return self

    def __exit__(self, *_args: object) -> None:
        for httpd in (self._pages, self._widget):
            if httpd is not None:
                httpd.shutdown()
                httpd.server_close()

    def url(self, name: str) -> str:
        return f"{self.base}/{name}"


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory: Path, replacements: dict[str, str], **kwargs):
        self._replacements = replacements
        super().__init__(*args, directory=str(directory), **kwargs)

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/captcha.html", "/captcha"}:
            text = (PAGES / "captcha.html").read_text(encoding="utf-8")
            for key, value in self._replacements.items():
                text = text.replace(key, value)
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def _serve(directory: Path, replacements: dict[str, str]) -> ThreadingHTTPServer:
    handler = partial(_Handler, directory=directory, replacements=replacements)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def chrome_binary() -> str | None:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def launch_chrome(user_data_dir: Path, port: int | None = None) -> tuple[subprocess.Popen, str]:
    """Headless Chrome with a DevTools port. The caller terminates it."""
    binary = chrome_binary()
    if binary is None:
        raise RuntimeError("google-chrome or chromium is not on PATH")
    port = port or free_port()
    user_data_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            binary,
            "--headless=new",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={user_data_dir}",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1280,800",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, f"http://127.0.0.1:{port}"


def wait_cdp(endpoint: str, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(endpoint + "/json/version", timeout=0.5) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001 - Chrome is still starting
            last = str(exc)
        time.sleep(0.2)
    raise RuntimeError(f"Chrome did not open {endpoint}: {last}")


def element(snap, *, role: str, title: str):
    return next((el for el in snap.elements if el.role == role and el.title == title), None)
