"""Firefox PDF.js page text is in the snapshot, and find can match it.

PDF.js paints the page in ``div.canvasWrapper`` and exposes the same words
on unnamed ``span`` sections under ``div.textLayer``. Hermetic cases use a
fake accessible modeled on Firefox ESR 153.6.0. The live case launches
Firefox when the binary and a display are both present, and skips otherwise.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.observe import build_snapshot
from a11y_computer_use.schema import Scope
from tests.test_linux_synthetic import _Acc, _FakeAtspi, _adopt, _geometry


def _app(name: str, toolkit: str) -> _Acc:
    app = _Acc("application", name=name)
    app.get_toolkit_name = lambda toolkit=toolkit: toolkit
    return app


def _node(
    app: _Acc,
    role: str,
    *,
    name: str = "",
    text: str = "",
    attrs: dict | None = None,
    width: float = 0.0,
    height: float = 0.0,
    x: float = 0.0,
    y: float = 0.0,
    states: tuple[str, ...] = ("SHOWING", "VISIBLE", "SENSITIVE", "ENABLED"),
) -> _Acc:
    node = _Acc(role, name=name, width=width, height=height, states=states)
    node.get_application = lambda: app
    node.get_attributes = lambda attrs=dict(attrs or {}): dict(attrs)
    node.text = text
    if node.component is not None:
        node.component._rect.x = x
        node.component._rect.y = y
    return node


def _pdf_frame(app: _Acc, *, layer_class: str = "textLayer"):
    """The ESR 153 page shape: landmark, canvas wrapper, text-layer spans."""
    frame = _node(app, "frame", name="probe.pdf — Mozilla Firefox", width=1280, height=800)
    document = _node(
        app, "document web", name="probe.pdf", width=1200, height=760, x=0, y=20,
    )
    page = _node(
        app, "landmark", name="Page 1", width=800, height=700, x=40, y=40,
        attrs={"tag": "div", "class": "page", "xml-roles": "region"},
    )
    canvas = _node(
        app, "section", width=760, height=660, x=50, y=50,
        attrs={"tag": "div", "class": "canvasWrapper"},
    )
    layer = _node(
        app, "section", width=760, height=660, x=50, y=50,
        text="\ufffc\ufffc\ufffc\ufffc",
        attrs={"tag": "div", "class": layer_class},
        states=("SHOWING", "VISIBLE", "SENSITIVE", "ENABLED", "FOCUSABLE"),
    )
    visible = _node(
        app, "section", text="ALPHA_MARKER token", width=220, height=18, x=80, y=90,
        attrs={"tag": "span"},
    )
    prose = _node(
        app, "section", text="The quick brown fox jumps.", width=240, height=18, x=80, y=120,
        attrs={"tag": "span"},
    )
    hidden = _node(
        app, "section", text="BETA_MARKER token", width=220, height=18, x=80, y=3000,
        attrs={"tag": "span"},
    )
    wrapped = _node(
        app, "section", text="WRAPPED_MARKER", width=180, height=18, x=80, y=150,
        attrs={"tag": "span"},
    )
    button = _node(app, "push button", name="Run", width=40, height=18, x=80, y=150)
    outside = _node(
        app, "section", width=200, height=24, x=10, y=10,
        attrs={"tag": "div", "class": "toolbar"}, text="\ufffc",
    )
    plain = _node(
        app, "section", text="NOT_PDF_TEXT", width=120, height=18, x=10, y=10,
        attrs={"tag": "span"},
    )
    _adopt(wrapped, button)
    _adopt(layer, visible, prose, hidden, wrapped)
    _adopt(page, canvas, layer)
    _adopt(document, page)
    _adopt(outside, plain)
    _adopt(frame, document, outside)
    _adopt(app, frame)
    return frame


@pytest.fixture
def fake_atspi(monkeypatch):
    _FakeAtspi.desktop = None
    _atspi.reset_shown_rows()
    monkeypatch.setattr(_atspi, "_atspi", lambda: _FakeAtspi)
    yield _FakeAtspi
    _FakeAtspi.desktop = None
    _atspi.reset_shown_rows()


def _snap(frame: _Acc):
    return build_snapshot(
        frame, _atspi.ATSPIAccessor(), scope=Scope.WINDOW, app="firefox", pid=1,
        geometry=_geometry(),
    )


def test_pdfjs_text_layer_spans_are_in_the_snapshot_and_find(fake_atspi) -> None:
    """On-screen PDF.js lines are static text. The canvas wrapper stays opaque.

    An off-screen line is clipped. A span outside the text layer, and a span
    that wraps a control, do not publish their Text as page text.
    """
    snap = _snap(_pdf_frame(_app("Firefox", "Gecko")))
    rendered = observe.render_text(snap)
    assert "ALPHA_MARKER token" in rendered
    assert "The quick brown fox jumps." in rendered
    assert "BETA_MARKER token" not in rendered
    assert "NOT_PDF_TEXT" not in rendered
    assert "WRAPPED_MARKER" not in rendered
    matches = observe.find_elements(snap, text="ALPHA_MARKER")
    assert len(matches) == 1
    assert matches[0].role == "AXStaticText"
    assert matches[0].editable is False
    assert "ALPHA_MARKER token" in (matches[0].value or "")
    assert observe.find_elements(snap, text="quick brown fox")
    assert observe.find_elements(snap, text="BETA_MARKER") == ()
    assert observe.find_elements(snap, text="NOT_PDF_TEXT") == ()
    assert observe.find_elements(snap, text="WRAPPED_MARKER") == ()
    opaque = [el for el in snap.elements if el.role == "opaque_region"]
    assert opaque
    assert all("ALPHA_MARKER" not in f"{el.title} {el.value or ''}" for el in opaque)
    assert any(el.title == "Page 1" for el in snap.elements)


def test_pdfjs_text_layer_requires_the_class_token_and_gecko(fake_atspi) -> None:
    """``textLayerX`` is not the PDF.js layer. Chromium is not this path."""
    almost = _snap(_pdf_frame(_app("Firefox", "Gecko"), layer_class="textLayerX"))
    assert observe.find_elements(almost, text="ALPHA_MARKER") == ()
    chrome = _snap(_pdf_frame(_app("Google Chrome", "Chromium")))
    assert observe.find_elements(chrome, text="ALPHA_MARKER") == ()
    assert "ALPHA_MARKER" not in observe.render_text(chrome)


def test_pdfjs_blank_span_stays_a_group(fake_atspi) -> None:
    """No readable line text leaves the span unconverted."""
    app = _app("Firefox", "Gecko")
    frame = _node(app, "frame", name="blank.pdf", width=800, height=600)
    layer = _node(
        app, "section", width=400, height=300, x=20, y=20,
        attrs={"tag": "div", "class": "textLayer"}, text="\ufffc",
    )
    blank = _node(
        app, "section", text="\ufffc", width=80, height=16, x=30, y=40,
        attrs={"tag": "span"},
    )
    _adopt(layer, blank)
    _adopt(frame, layer)
    _adopt(app, frame)
    snap = _snap(frame)
    assert all(el.role != "AXStaticText" or not el.value for el in snap.elements)


def _pdf(pages: list[list[str]]) -> bytes:
    """A text PDF. Each page is Helvetica lines. No external library."""
    streams = []
    for lines in pages:
        body = "BT /F1 12 Tf\n" + "\n".join(
            f"1 0 0 1 72 {720 - index * 18} Tm ({line}) Tj" for index, line in enumerate(lines)
        ) + "\nET"
        streams.append(body.encode("ascii"))
    count = len(streams)
    font = 3 + count * 2
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            "<< /Type /Pages /Kids ["
            + " ".join(f"{3 + index * 2} 0 R" for index in range(count))
            + f"] /Count {count} >>"
        ).encode("ascii"),
        font: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for index, stream in enumerate(streams):
        page_id = 3 + index * 2
        contents_id = page_id + 1
        objects[contents_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
        )
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {contents_id} 0 R "
            f"/Resources << /Font << /F1 {font} 0 R >> >> >>"
        ).encode("ascii")
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index in range(1, font + 1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii") + objects[index] + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {font + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for index in range(1, font + 1):
        out += f"{offsets[index]:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer << /Size {font + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


def _firefox_binary() -> str | None:
    for name in ("firefox", "firefox-esr"):
        found = shutil.which(name)
        if found:
            return found
    candidate = "/opt/firefox/firefox"
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


_PROFILE_JS = """\
user_pref("accessibility.force_disabled", -1);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.tabs.warnOnClose", false);
user_pref("browser.startup.firstrunSkipsHomepage", true);
user_pref("browser.newtab.preload", false);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("trailhead.firstrun.didSeeAboutWelcome", true);
user_pref("pdfjs.disabled", false);
user_pref("termsofuse.acceptedVersion", 4);
user_pref("termsofuse.acceptedDate", "1");
user_pref("browser.termsofuse.prefMigrationCheck", true);
"""


def _press_continue(snap) -> bool:
    """Press a blocking Terms/welcome Continue button, if the snapshot has one."""
    button = next(
        (el for el in snap.elements if el.role == "AXButton" and el.title == "Continue"),
        None,
    )
    if button is None:
        return False
    handle = observe.ax_handle_for(snap.snapshot_id, button.ref)
    if handle is None:
        return False
    action = getattr(handle, "get_action_iface", lambda: None)()
    if action is None or not action.get_n_actions():
        return False
    return bool(action.do_action(0))


def test_live_firefox_pdfjs_page_text_is_in_the_snapshot_and_find(tmp_path) -> None:
    """Live Firefox PDF.js. The on-screen page line is static text find can match.

    The canvas wrapper stays an opaque region and does not carry the line.
    Skips when Firefox or a display is absent. The words come from the text
    layer's Text interface. Nothing is read from pixels.
    """
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux backend")
    binary = _firefox_binary()
    if binary is None:
        pytest.skip("firefox is not installed")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("no DISPLAY/WAYLAND_DISPLAY")
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    driver = LinuxDriver()
    try:
        driver.ensure_trusted()
    except ComputerUseError as exc:
        pytest.skip(f"AT-SPI bus not reachable: {exc.message}")
    except ImportError as exc:
        pytest.skip(f"PyGObject/Atspi missing: {exc}")

    path = tmp_path / "firefox-pdfjs-page-text.pdf"
    path.write_bytes(_pdf([
        ["PAGE-1 heading", "Alpha line on page 1", "ALPHA_MARKER token", "The quick brown fox jumps."],
        ["PAGE-2 heading", "BETA_MARKER token"],
        ["PAGE-3 heading", "GAMMA_MARKER token"],
    ]))
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "user.js").write_text(_PROFILE_JS)
    env = os.environ.copy()
    env["MOZ_ENABLE_ACCESSIBILITY"] = "1"
    env["GTK_MODULES"] = "gail:atk-bridge"
    env["NO_AT_BRIDGE"] = "0"
    proc = subprocess.Popen(
        [binary, "--profile", str(profile), "--no-remote", "--new-instance", str(path)],
        env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        snap = None
        last = ""
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(f"firefox exited with status {proc.returncode}")
            try:
                shot = driver.snapshot(Scope.WINDOW, "firefox")
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
                shot = None
            else:
                last = observe.render_text(shot)
                if observe.find_elements(shot, text="ALPHA_MARKER"):
                    snap = shot
                    break
                _press_continue(shot)
            time.sleep(0.4)
        assert snap is not None, last[:2000]
        matches = observe.find_elements(snap, text="ALPHA_MARKER")
        assert len(matches) == 1
        assert matches[0].role == "AXStaticText"
        assert matches[0].role != "opaque_region"
        assert "ALPHA_MARKER token" in (matches[0].value or "")
        assert "ALPHA_MARKER" in observe.render_text(snap)
        assert observe.find_elements(snap, text="quick brown fox")
        opaque = [el for el in snap.elements if el.role == "opaque_region"]
        assert opaque
        assert all("ALPHA_MARKER" not in f"{el.title} {el.value or ''}" for el in opaque)
        assert any(el.title == "Page 1" for el in snap.elements)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
