"""Evince side-pane thumbnails keep the page number from the icon Text.

The thumbnail is an AT-SPI ``icon`` with an empty name. Its Text interface
is the page label (``1``, ``2``, ``3``). Hermetic cases use a fake
accessible. The live case launches Evince when the binary and a display
are both present, and skips otherwise.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.observe import RawNode, build_snapshot
from a11y_computer_use.schema import Display, Scope


def _geometry():
    return (
        observe.DisplayGeometry(
            display=Display(0, 1280, 800, 1.0, True), origin=(0.0, 0.0),
        ),
    )


class _FakeAccessor:
    def read(self, node):
        return node[0]

    def children(self, node):
        return node[1]


class _Text:
    @staticmethod
    def get_character_count(acc):
        if acc.text is None:
            raise RuntimeError("no text")
        return len(acc.text)

    @staticmethod
    def get_text(acc, start, end):
        acc.reads.append((int(start), int(end)))
        if acc.text is None:
            raise RuntimeError("no text")
        if end < 0:
            end = len(acc.text)
        return acc.text[int(start):int(end)]


class _Atspi:
    Text = _Text
    CoordType = type("CoordType", (), {"SCREEN": 0})


class _Rect:
    def __init__(self, x, y, width, height):
        self.x = x
        self.y = y
        self.width = width
        self.height = height


class _Node:
    def __init__(
        self,
        role: str,
        text: str | None,
        *,
        name: str = "",
        description: str = "",
        box=None,
        actions=("click",),
        selected: bool = False,
    ) -> None:
        self.role = role
        self.text = text
        self.name = name
        self.description = description
        self.box = box or ((672.0, 369.0), (102.0, 154.0))
        self.actions = list(actions)
        self.selected = selected
        self.reads: list[tuple[int, int]] = []

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_description(self):
        return self.description

    def get_attributes(self):
        return {}

    def get_action_iface(self):
        return self if self.actions else None

    def get_n_actions(self):
        return len(self.actions)

    def get_action_name(self, index):
        return self.actions[index]

    def get_component_iface(self):
        return self

    def get_extents(self, _coord):
        x, y = self.box[0]
        w, h = self.box[1]
        return _Rect(x, y, w, h)


@pytest.fixture
def fake_atspi(monkeypatch):
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Atspi)


def _read(node: _Node):
    return _atspi.ATSPIAccessor().read(node)


def _snap(*nodes: RawNode):
    window = (
        RawNode(role="AXWindow", title="evince", position=(0.0, 0.0), size=(1280.0, 800.0)),
        [(node, []) for node in nodes],
    )
    return build_snapshot(
        window, _FakeAccessor(), scope=Scope.WINDOW, app="evince", pid=1, geometry=_geometry(),
    )


def test_image_label_is_bounded() -> None:
    assert _atspi._IMAGE_LABEL_CHARS <= 80


def test_unnamed_icon_text_is_the_snapshot_name(fake_atspi) -> None:
    """The page number is the image name. Find matches it. The icon stays an image.

    The read end is the character count, not -1. A selected thumbnail keeps
    that state. The rendered line is ``image "3"``, not a value and not an
    opaque region.
    """
    icon = _Node("icon", "3", selected=False)
    raw = _read(icon)
    assert raw.role == "AXImage"
    assert raw.title == "3"
    assert raw.value is None
    assert icon.reads == [(0, 1)]
    assert all(end >= 0 for _start, end in icon.reads)

    snap = _snap(raw)
    rendered = observe.render_text(snap)
    assert 'image "3" (click)' in rendered
    assert "opaque_region" not in rendered
    matches = observe.find_elements(snap, text="3", role="image")
    assert len(matches) == 1
    assert matches[0].role == "AXImage"
    assert matches[0].title == "3"
    assert matches[0].clickable is True
    assert matches[0].bounds is not None
    assert matches[0].bounds.width == 102
    assert matches[0].bounds.height == 154


def test_page_labels_and_a_named_icon(fake_atspi) -> None:
    """Labels 1, 2, and 3 are three images. A named toolbar icon is not retitled.

    Non-breaking spaces and an object-replacement character collapse to the
    page number. A useful description is left as the description, and Text
    is not read for that icon.
    """
    first = _Node("icon", "\u00a01\u00a0", box=((672.0, 369.0), (102.0, 154.0)))
    second = _Node("icon", "\ufffc2", box=((672.0, 541.0), (102.0, 154.0)))
    third = _Node("image", "3", box=((672.0, 713.0), (102.0, 154.0)))
    named = _Node("icon", "9", name="pan-down-symbolic", box=((10.0, 10.0), (24.0, 24.0)))
    described = _Node(
        "image", "9", description="Sync status", box=((40.0, 10.0), (24.0, 24.0)),
    )
    nodes = [_read(first), _read(second), _read(third), _read(named), _read(described)]
    assert [node.title for node in nodes[:3]] == ["1", "2", "3"]
    assert named.reads == []
    assert nodes[3].title == "pan-down-symbolic"
    assert described.reads == []
    assert nodes[4].title == ""
    assert nodes[4].description == "Sync status"

    snap = _snap(*nodes)
    for label in ("1", "2", "3"):
        found = observe.find_elements(snap, text=label, role="image")
        assert [el.title for el in found if el.title == label] == [label]
        assert all(el.role == "AXImage" for el in found if el.title == label)
    toolbar = observe.find_elements(snap, text="pan-down-symbolic", role="image")
    assert len(toolbar) == 1
    assert toolbar[0].title == "pan-down-symbolic"
    status = observe.find_elements(snap, text="Sync status", role="image")
    assert len(status) == 1
    assert status[0].role == "AXImage"


def test_blank_or_generic_text_stays_an_opaque_region(fake_atspi) -> None:
    """No caption, or the word ``image``, still folds a large unnamed icon."""
    blank = _Node("icon", None)
    generic = _Node("icon", "image", box=((672.0, 541.0), (102.0, 154.0)))
    blank_raw = _read(blank)
    generic_raw = _read(generic)
    assert blank_raw.role == "AXImage"
    assert blank_raw.title == ""
    assert blank.reads == []
    assert generic_raw.title == ""
    assert generic.reads == [(0, 5)]

    snap = _snap(blank_raw, generic_raw)
    regions = [el for el in snap.elements if el.role == "opaque_region"]
    assert len(regions) == 2
    assert observe.find_elements(snap, role="image") == ()
    assert 'image "image"' not in observe.render_text(snap)


def test_label_read_stops_at_the_cap(fake_atspi) -> None:
    """A caption longer than the cap is cut there. The tail is not the name."""
    cap = _atspi._IMAGE_LABEL_CHARS
    body = "HEAD " + ("x" * cap) + " TAIL"
    node = _Node("icon", body)
    raw = _read(node)
    assert raw.title.startswith("HEAD ")
    assert "TAIL" not in raw.title
    assert len(raw.title) <= cap
    assert node.reads == [(0, cap)]
    snap = _snap(raw)
    assert observe.find_elements(snap, text="HEAD", role="image")
    assert observe.find_elements(snap, text="TAIL", role="image") == ()


def test_text_label_is_a_named_click_and_a_blank_icon_is_not(fake_atspi) -> None:
    """A page number is a label, so the click is not an unnamed-image verdict.

    A canvas stays unverifiable. An icon with no Text stays unverifiable.
    A name that is already set does not read Text.
    """
    labelled = _Node("icon", "3")
    assert _atspi.opaque_click_verdict(labelled) is None
    assert labelled.reads == [(0, 1)]

    blank = _Node("icon", None)
    verdict = _atspi.opaque_click_verdict(blank)
    assert verdict is not None
    assert verdict[0] == "unverifiable"
    assert "crop" in verdict[1] and "screenshot" in verdict[1]
    assert blank.reads == []

    named = _Node("image", "9", name="Logo")
    assert _atspi.opaque_click_verdict(named) is None
    assert named.reads == []

    canvas = _Node("canvas", "3", name="Drawing board")
    canvas_verdict = _atspi.opaque_click_verdict(canvas)
    assert canvas_verdict is not None
    assert canvas_verdict[0] == "unverifiable"
    assert canvas.reads == []


def test_offscreen_thumbnail_is_not_in_the_snapshot(fake_atspi) -> None:
    """A thumbnail with no overlap of the display is dropped. Its box is the tree's."""
    visible = _Node("icon", "1", box=((672.0, 369.0), (102.0, 154.0)))
    hidden = _Node("icon", "9", box=((672.0, 3000.0), (102.0, 154.0)))
    snap = _snap(_read(visible), _read(hidden))
    rendered = observe.render_text(snap)
    assert 'image "1"' in rendered
    assert 'image "9"' not in rendered
    assert observe.find_elements(snap, text="9", role="image") == ()


def _pdf(pages: int) -> bytes:
    """A text PDF with ``pages`` blank-enough pages. No external library."""
    streams = []
    for number in range(1, pages + 1):
        line = f"PAGE-{number}"
        body = f"BT /F1 12 Tf\n1 0 0 1 72 720 Tm ({line}) Tj\nET"
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


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def test_live_evince_thumbnail_page_numbers_are_image_names(tmp_path) -> None:
    """Live Evince. Side-pane thumbnails are images named with their page numbers.

    Skips when Evince or a display is absent. The names come from the Text
    interface. Nothing is read from pixels, and no click coordinate is sent.
    """
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux backend")
    if shutil.which("evince") is None:
        pytest.skip("evince is not installed")
    import os

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

    subprocess.run(["pkill", "-x", "evince"], check=False)
    path = tmp_path / "evince-thumbnails.pdf"
    path.write_bytes(_pdf(3))
    proc = subprocess.Popen(
        ["evince", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 25
        pane = None
        last = ""
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(f"evince exited with status {proc.returncode}")
            try:
                shot = driver.snapshot(Scope.WINDOW, "evince")
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
                time.sleep(0.4)
                continue
            last = observe.render_text(shot)
            buttons = [
                el for el in shot.elements
                if el.role == "AXButton" and el.title.strip().casefold() == "side pane"
            ]
            if buttons:
                pane = buttons[0]
                break
            time.sleep(0.4)
        assert pane is not None, last[:1600]
        if pane.checked is not True:
            assert driver.press_element(pane) is True

        deadline = time.monotonic() + 15
        snap = None
        while time.monotonic() < deadline:
            shot = driver.snapshot(Scope.WINDOW, "evince")
            last = observe.render_text(shot)
            labelled = {
                el.title: el
                for el in observe.find_elements(shot, role="image")
                if el.title in {"1", "2", "3"}
            }
            if {"1", "2", "3"} <= set(labelled):
                snap = shot
                break
            thumbs = [
                el for el in shot.elements
                if el.role == "AXRadioButton" and el.title == "Thumbnails" and el.checked is not True
            ]
            if thumbs:
                driver.press_element(thumbs[0])
            time.sleep(0.4)
        assert snap is not None, last[:2000]
        rendered = observe.render_text(snap)
        for label in ("1", "2", "3"):
            assert f'image "{label}"' in rendered
            matches = observe.find_elements(snap, text=label, role="image")
            named = [el for el in matches if el.title == label]
            assert len(named) == 1
            assert named[0].role == "AXImage"
            assert named[0].role != "opaque_region"
            assert named[0].clickable is True
            box = named[0].bounds
            assert box is not None and box.width > 0 and box.height > 0
        page_one = observe.find_elements(snap, text="1", role="image")
        current = [el for el in page_one if el.title == "1"]
        assert current and current[0].selected is True
    finally:
        _stop(proc)
