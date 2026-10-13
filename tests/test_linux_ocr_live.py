"""Live Linux OCR: RapidOCR and Tesseract both read a GTK label.

The hermetic suite does not install the ``[ocr]`` extra. This file runs in
the Linux CI live step, which installs that extra and the ``tesseract``
binary. Without ``A11Y_OCR_LIVE=1`` a missing engine skips; with it, a
missing engine fails the job.
"""

from __future__ import annotations

import io
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux OCR")

from a11y_computer_use import ocr  # noqa: E402
from a11y_computer_use.schema import Bounds, ComputerUseError, ErrorCode, Scope  # noqa: E402
from a11y_computer_use.untrusted import unwrap  # noqa: E402

_APP = "cuaocr"
_TOKEN = "CUAOCRLABEL"

_GTK = textwrap.dedent(
    """
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk
    GLib.set_prgname("cuaocr")
    win = Gtk.Window(title="cuaocr")
    label = Gtk.Label(label="CUAOCRLABEL")
    label.set_name("ocrlabel")
    label.get_accessible().set_name("CUAOCRLABEL")
    css = Gtk.CssProvider()
    css.load_from_data(b'''
    window, #ocrlabel {
      background-color: #ffffff;
      color: #000000;
      font-family: sans-serif;
      font-size: 48px;
    }
    ''')
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_USER,
    )
    win.add(label)
    win.set_default_size(640, 180)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    win.present()
    Gtk.main()
    """
)


def _require(name: str) -> None:
    if os.environ.get("A11Y_OCR_LIVE") == "1":
        missing = pytest.fail
    else:
        missing = pytest.skip
    if name == "rapidocr":
        try:
            import rapidocr  # noqa: F401
        except ImportError:
            missing("rapidocr is not installed; pip install 'a11y-computer-use[ocr]'")
    elif shutil.which("tesseract") is None:
        missing("tesseract is not on PATH; apt install tesseract-ocr")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def test_linux_ocr_reads_a_gtk_label_with_rapidocr_and_tesseract(tmp_path) -> None:
    """Both engines read CUAOCRLABEL from the label's pixels, in screen coordinates."""
    from PIL import Image

    from a11y_computer_use.drivers.linux import LinuxDriver

    _require("rapidocr")
    _require("tesseract")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("no DISPLAY/WAYLAND_DISPLAY")
    driver = LinuxDriver()
    try:
        driver.ensure_trusted()
    except ComputerUseError as exc:
        pytest.skip(f"AT-SPI bus not reachable: {exc.message}")
    except ImportError as exc:
        pytest.skip(f"PyGObject/Atspi missing: {exc}")

    script = tmp_path / "cuaocr.py"
    script.write_text(_GTK)
    proc = subprocess.Popen([sys.executable, str(script)])
    try:
        deadline = time.monotonic() + 15
        snap = None
        label = None
        while time.monotonic() < deadline:
            try:
                snap = driver.snapshot(Scope.WINDOW, _APP)
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
                snap = None
            else:
                label = next((el for el in snap.elements if _TOKEN in (el.title or "")), None)
                if label is not None and label.bounds.width > 40 and label.bounds.height > 20:
                    break
            time.sleep(0.3)
        assert snap is not None and label is not None, "GTK label was not on the bus"
        shot = driver.screenshot()
        frame = Image.open(io.BytesIO(shot.png))
        bounds = label.bounds
        right = min(frame.width, bounds.x + bounds.width)
        bottom = min(frame.height, bounds.y + bounds.height)
        left = max(0, bounds.x)
        top = max(0, bounds.y)
        assert right > left and bottom > top, bounds
        crop = frame.crop((left, top, right, bottom))
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        region = Bounds(bounds.display_id, left, top, right - left, bottom - top)
        png = buffer.getvalue()
        for name in ("rapidocr", "tesseract"):
            spans = ocr.ocr((png, region), engine=name)
            hit = next(
                (span for span in spans if _TOKEN in (unwrap(str(span["text"])) or "").upper()),
                None,
            )
            assert hit is not None, (name, [(unwrap(str(span["text"])), span["bounds"]) for span in spans])
            assert str(hit["text"]).startswith("<untrusted nonce=")
            box = hit["bounds"]
            assert box["display_id"] == region.display_id
            assert region.x - 12 <= box["x"] <= region.x + region.width
            assert region.y - 12 <= box["y"] <= region.y + region.height
            assert float(hit["confidence"]) > 0.3
    finally:
        _stop(proc)
