"""The Inkscape SVG canvas keeps an honest name, and a miss says why.

Hermetic. No display, no bus, no Inkscape process. The canvas is a large
unnamed drawing area, so the pruner makes it an opaque region. The name is
the document label when exactly one label prefixes the frame title,
otherwise the frame title. It is never a made-up word such as Canvas.
When the drawing area has no text and no children, the snapshot and a
find that misses say that canvas content isn't accessible. A shape is not
given a box. Vision and OCR stay off.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import time

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.observe import DisplayGeometry, build_snapshot
from a11y_computer_use.schema import Display, Scope


_FRAME = "probe.svg - Inkscape"
_DOC = "probe.svg"
_PROBE = "A11Y_INKSCAPE_PROBE"
_CANVAS_BOX = ((40.0, 80.0), (798.0, 545.0))
_NOTE = "canvas content isn't accessible"


def _geometry():
    return (
        DisplayGeometry(display=Display(0, 1280, 800, 1.0, True), origin=(0.0, 0.0)),
    )


class _App:
    def __init__(self, name: str) -> None:
        self.name = name

    def get_name(self):
        return self.name

    def get_role_name(self):
        return "application"


class _Node:
    def __init__(
        self,
        role,
        name="",
        *,
        children=(),
        box=((0.0, 0.0), (40.0, 40.0)),
        focusable=False,
        description="",
        text="",
        app_name="inkscape",
    ):
        self.role = role
        self.name = name
        self.description = description
        self.children = list(children)
        self.box = box
        self.focusable = focusable
        self.text = text
        self.app = _App(app_name)
        self.parent = None
        for child in self.children:
            child.parent = self
            child.app = self.app

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_description(self):
        return self.description

    def get_parent(self):
        return self.parent

    def get_application(self):
        return self.app

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]


class _Tree:
    def read(self, node):
        return _atspi.ATSPIAccessor().read(node)

    def children(self, node):
        return list(node.children)


def _patch(monkeypatch) -> None:
    def flags(acc):
        focusable = bool(getattr(acc, "focusable", False))
        return (True, False, None, False, None, focusable, False)

    def extents(acc, keep_zero=False):
        return acc.box

    def page_text(acc):
        text = getattr(acc, "text", "") or ""
        return text or None

    monkeypatch.setattr(_atspi, "_state_flags", flags)
    monkeypatch.setattr(_atspi, "_extents", extents)
    monkeypatch.setattr(_atspi, "_value_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(_atspi, "_page_text", page_text)


def _frame(*children, title=_FRAME, app_name="inkscape"):
    frame = _Node(
        "frame",
        title,
        children=list(children),
        box=((0.0, 0.0), (1200.0, 800.0)),
        app_name=app_name,
    )
    return frame


def _snap(monkeypatch, frame):
    _patch(monkeypatch)
    return build_snapshot(
        frame, _Tree(), scope=Scope.WINDOW, app="inkscape", pid=1, geometry=_geometry(),
    )


def test_empty_inkscape_dock_keeps_window_label_and_native_box(monkeypatch) -> None:
    dock = _Node("filler", box=((890.0, 265.0), (379.0, 348.0)))
    frame = _frame(dock)
    snap = _snap(monkeypatch, frame)
    region = next(el for el in snap.elements if el.role == "opaque_region")
    assert region.title == _FRAME
    assert region.content_note == ""
    assert region.bounds.x == 890
    assert region.bounds.y == 265
    assert region.bounds.width == 379
    assert region.bounds.height == 348
    dock.app = _App("other editor")
    assert _atspi.ATSPIAccessor().read(dock).surface_name == ""
    unnamed = next(el for el in _snap(monkeypatch, frame).elements if el.role == "opaque_region")
    assert region.clickable == unnamed.clickable


def test_document_title_names_the_canvas_and_a_miss_says_why(monkeypatch) -> None:
    label = _Node("label", _DOC, box=((-8.0, -8.0), (1.0, 1.0)))
    canvas = _Node("drawing area", box=_CANVAS_BOX, focusable=True)
    ruler = _Node("drawing area", box=((40.0, 40.0), (798.0, 24.0)))
    swatch = _Node("drawing area", "Black", box=((20.0, 700.0), (16.0, 16.0)))
    frame = _frame(label, canvas, ruler, swatch)
    _patch(monkeypatch)
    raw = _atspi.ATSPIAccessor().read(canvas)
    assert raw.role == "AXCanvas"
    assert raw.title == ""
    assert raw.surface_name == _DOC
    assert raw.content_note == _NOTE
    assert raw.value in (None, "")
    assert raw.position == (40.0, 80.0)
    assert raw.size == (798.0, 545.0)

    snap = _snap(monkeypatch, frame)
    opaque = [el for el in snap.elements if el.role == "opaque_region"]
    named = [el for el in opaque if el.title == _DOC]
    assert len(named) == 1
    canvas_el = named[0]
    assert canvas_el.clickable
    assert canvas_el.bounds.width == 798
    assert canvas_el.bounds.height == 545
    assert canvas_el.bounds.x == 40
    assert canvas_el.bounds.y == 80
    assert canvas_el.content_note == _NOTE
    found = observe.find_elements(snap, text=_DOC)
    assert canvas_el in found
    assert observe.find_elements(snap, text="Canvas") == ()
    assert observe.find_elements(snap, text="drawing area") == ()
    assert observe.find_elements(snap, text=_PROBE) == ()
    rendered = observe.render_text(snap, mode="full")
    assert rendered.splitlines()[1] == _NOTE
    assert f'opaque_region "{_DOC}"' in rendered
    assert f"({_NOTE})" in rendered
    assert "Canvas" not in rendered
    assert _PROBE not in rendered
    interactive = observe.render_text(snap, mode="interactive")
    assert _NOTE in interactive
    assert f'opaque_region "{_DOC}"' in interactive
    missed = observe.render_matches(snap, observe.find_elements(snap, text=_PROBE), text=_PROBE)
    assert "no elements match" in missed
    assert _NOTE in missed
    ruler_raw = _atspi.ATSPIAccessor().read(ruler)
    assert ruler_raw.surface_name == ""
    assert ruler_raw.content_note == ""
    swatch_el = next(el for el in opaque if el.title == "Black")
    assert swatch_el.content_note == ""
    assert swatch_el.bounds.width == 16


def test_frame_title_is_used_when_no_document_label_matches(monkeypatch) -> None:
    canvas = _Node("drawing area", box=_CANVAS_BOX)
    frame = _frame(canvas)
    _patch(monkeypatch)
    raw = _atspi.ATSPIAccessor().read(canvas)
    assert raw.surface_name == _FRAME
    snap = _snap(monkeypatch, frame)
    opaque = next(el for el in snap.elements if el.role == "opaque_region" and el.bounds.width == 798)
    assert opaque.title == _FRAME
    assert opaque.title not in {"Canvas", "Image display", "drawing", "drawing area"}
    assert opaque.content_note == _NOTE


def test_two_prefix_labels_fall_back_to_the_frame_title(monkeypatch) -> None:
    canvas = _Node("drawing area", box=_CANVAS_BOX)
    one = _Node("label", _DOC, box=((-8.0, -8.0), (1.0, 1.0)))
    two = _Node("label", _DOC, box=((-8.0, -8.0), (1.0, 1.0)))
    frame = _frame(canvas, one, two)
    _patch(monkeypatch)
    assert _atspi.ATSPIAccessor().read(canvas).surface_name == _FRAME


def test_exposed_text_is_findable_and_does_not_claim_the_canvas_is_closed(monkeypatch) -> None:
    canvas = _Node("drawing area", box=_CANVAS_BOX, text=_PROBE)
    frame = _frame(canvas)
    snap = _snap(monkeypatch, frame)
    found = observe.find_elements(snap, text=_PROBE)
    assert len(found) == 1
    assert found[0].role == "opaque_region"
    assert found[0].value == _PROBE
    assert found[0].content_note == ""
    assert found[0].bounds.width == 798
    assert found[0].bounds.height == 545
    rendered = observe.render_text(snap)
    assert _PROBE in rendered
    assert _NOTE not in rendered
    matched = observe.render_matches(snap, found, text=_PROBE)
    assert "1 match" in matched
    assert _NOTE not in matched


def test_a_named_child_means_the_canvas_content_is_exposed(monkeypatch) -> None:
    shape = _Node("label", _PROBE, box=((80.0, 120.0), (40.0, 16.0)))
    canvas = _Node("drawing area", children=[shape], box=_CANVAS_BOX)
    frame = _frame(canvas)
    snap = _snap(monkeypatch, frame)
    assert observe.find_elements(snap, text=_PROBE)
    assert all(el.content_note == "" for el in snap.elements)
    assert _NOTE not in observe.render_text(snap)
    canvas_el = next(
        el for el in snap.elements if el.role == "opaque_region" and el.bounds.width == 798
    )
    assert canvas_el.title == _FRAME
    probe = observe.find_elements(snap, text=_PROBE)[0]
    assert probe.bounds.width == 40
    assert probe.bounds.height == 16


def test_a_drawing_area_outside_inkscape_stays_unnamed(monkeypatch) -> None:
    canvas = _Node(
        "drawing area", box=_CANVAS_BOX, app_name="cuadrawapp", text="PIXELWORD",
    )
    frame = _frame(canvas, title="cuadrawapp", app_name="cuadrawapp")
    snap = _snap(monkeypatch, frame)
    opaque = [el for el in snap.elements if el.role == "opaque_region"]
    assert len(opaque) == 1
    assert opaque[0].title == ""
    assert opaque[0].content_note == ""
    assert opaque[0].value in (None, "")
    rendered = observe.render_text(snap)
    assert _NOTE not in rendered
    assert "PIXELWORD" not in rendered
    missed = observe.render_matches(snap, ())
    assert missed.endswith("no elements match")
    assert _NOTE not in missed


def test_the_frame_title_suffix_names_the_canvas_without_an_app_name(monkeypatch) -> None:
    canvas = _Node("drawing area", box=_CANVAS_BOX, app_name="")
    frame = _frame(canvas, app_name="")
    _patch(monkeypatch)
    raw = _atspi.ATSPIAccessor().read(canvas)
    assert raw.surface_name == _FRAME
    assert raw.content_note == _NOTE


def test_live_inkscape_canvas_is_named_and_hidden_svg_is_not_silent(tmp_path) -> None:
    """Live Inkscape. The document canvas is named from the window title.

    ``A11Y_INKSCAPE_PROBE`` is painted in the SVG. When AT-SPI does not
    expose it, find and the snapshot say canvas content isn't accessible.
    When AT-SPI does expose it, find matches that text. No OCR, and no
    box is invented for the word.
    """
    if not sys.platform.startswith("linux"):
        import pytest

        pytest.skip("Linux backend")
    if shutil.which("inkscape") is None:
        import pytest

        pytest.skip("inkscape is not installed")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        import pytest

        pytest.skip("no DISPLAY/WAYLAND_DISPLAY")
    import pytest

    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    driver = LinuxDriver()
    try:
        driver.ensure_trusted()
    except ComputerUseError as exc:
        pytest.skip(f"AT-SPI bus not reachable: {exc.message}")
    except ImportError as exc:
        pytest.skip(f"PyGObject/Atspi missing: {exc}")

    home = tmp_path / "home"
    config = home / ".config" / "inkscape"
    config.mkdir(parents=True)
    (config / "preferences.xml").write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"no\"?>\n"
        "<inkscape version=\"1\">\n"
        "  <group id=\"options\">\n"
        "    <group id=\"boot\" enabled=\"0\"/>\n"
        "  </group>\n"
        "</inkscape>\n",
        encoding="utf-8",
    )
    svg = tmp_path / "a11y-inkscape-probe.svg"
    svg.write_text(
        textwrap.dedent(
            """\
            <?xml version="1.0" encoding="UTF-8"?>
            <svg xmlns="http://www.w3.org/2000/svg" width="400" height="200">
              <rect id="probe-rect" x="20" y="40" width="80" height="40"/>
              <text x="20" y="120">A11Y_INKSCAPE_PROBE</text>
            </svg>
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.setdefault("GTK_MODULES", "gail:atk-bridge")
    env.setdefault("NO_AT_BRIDGE", "0")
    proc = subprocess.Popen(
        ["inkscape", str(svg)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 40
        snap = None
        last = ""
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(f"inkscape exited with status {proc.returncode}")
            try:
                shot = driver.snapshot(Scope.WINDOW, "inkscape")
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
                shot = None
            else:
                last = observe.render_text(shot)
                windows = [el for el in shot.elements if el.role == "AXWindow" and el.title]
                large = [
                    el for el in shot.elements
                    if el.role == "opaque_region"
                    and el.bounds is not None
                    and el.bounds.width >= 64
                    and el.bounds.height >= 64
                    and el.title
                ]
                if windows and large:
                    snap = shot
                    break
            time.sleep(0.4)
        assert snap is not None, last[:1600]
        window = next(el for el in snap.elements if el.role == "AXWindow" and el.title)
        canvases = [
            el for el in snap.elements
            if el.role == "opaque_region"
            and el.bounds is not None
            and el.bounds.width >= 64
            and el.bounds.height >= 64
        ]
        assert canvases, last[:1600]
        for el in canvases:
            assert el.title
            assert el.title.casefold() not in {"canvas", "drawing area", "image", "unnamed"}
            assert el.title == window.title or window.title.startswith(el.title)
            assert el.bounds.width > 0 and el.bounds.height > 0
        rendered = observe.render_text(snap)
        exposed = observe.find_elements(snap, text=_PROBE)
        if exposed:
            assert all(el.content_note == "" for el in exposed)
            assert all(el.bounds is not None and el.bounds.width > 0 for el in exposed)
        else:
            assert _NOTE in rendered
            missed = observe.render_matches(snap, exposed, text=_PROBE)
            assert "no elements match" in missed
            assert _NOTE in missed
            assert all(_PROBE not in (el.title or "") and _PROBE not in (el.value or "") for el in snap.elements)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
