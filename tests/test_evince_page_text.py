"""Evince PDF page text is in the snapshot, and find can match it.

The page is AT-SPI role ``page``. Its Text interface is the page. Hermetic
cases use a fake accessible. The live case launches Evince when the binary
and a display are both present, and skips otherwise.
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


_PAGE = (
    "PAGE-1 heading\n"
    "Alpha line on page 1\n"
    "MARKER-P1-UNIQUE token\n"
    "The quick brown fox jumps.\n"
    "End of page 1"
)


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
        acc.reads.append((start, end))
        if acc.text is None:
            raise RuntimeError("no text")
        if end < 0:
            end = len(acc.text)
        return acc.text[int(start):int(end)]


class _Atspi:
    Text = _Text


class _Node:
    def __init__(self, role: str, text: str | None, *, name: str = "", box=None) -> None:
        self.role = role
        self.text = text
        self.name = name
        self.box = box or ((40.0, 40.0), (500.0, 640.0))
        self.reads: list[tuple[int, int]] = []

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_description(self):
        return ""

    def get_attributes(self):
        return {}


@pytest.fixture
def fake_atspi(monkeypatch):
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Atspi)
    monkeypatch.setattr(
        _atspi, "_state_flags", lambda acc: (True, True, None, False, None, True, False),
    )

    def extents(acc, keep_zero=False):
        return acc.box

    monkeypatch.setattr(_atspi, "_extents", extents)


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


def test_page_role_is_static_text() -> None:
    assert _atspi._ROLE["page"] == "AXStaticText"
    assert "page" in _atspi._STATIC_REPLACE_ROLES
    assert _atspi._PAGE_TEXT_CHARS <= 4000


def test_page_text_is_not_an_editable_field(fake_atspi, monkeypatch) -> None:
    node = _Node("page", _PAGE)
    assert _atspi.editable_entry(node, role_str="page") is False
    assert _atspi._blocks_text_replace(node) is True
    monkeypatch.setattr(_atspi, "_focused_node", lambda app, max_nodes=400: (node, False))
    assert _atspi.focused_text("evince") is None


def test_on_screen_page_text_is_in_the_snapshot_and_find_matches_it(fake_atspi) -> None:
    """The visible page's text is the node's value. Find matches a token in it.

    An off-screen page stays out of the snapshot. The Text read is bounded:
    the end offset is not -1.
    """
    visible = _Node("page", _PAGE)
    hidden = _Node(
        "page",
        "PAGE-3 heading\nMARKER-P3-UNIQUE token\nEnd of page 3",
        box=((40.0, 3000.0), (500.0, 640.0)),
    )
    shown = _read(visible)
    away = _read(hidden)
    assert shown.role == "AXStaticText"
    assert shown.editable is False
    assert shown.value is not None
    assert "MARKER-P1-UNIQUE" in shown.value
    assert "The quick brown fox jumps." in shown.value
    assert visible.reads
    assert all(end >= 0 for _start, end in visible.reads)
    assert away.value is not None and "MARKER-P3-UNIQUE" in away.value

    snap = _snap(shown, away)
    rendered = observe.render_text(snap)
    assert "PAGE-1 heading" in rendered
    assert "MARKER-P3-UNIQUE" not in rendered
    assert "opaque_region" not in rendered
    matches = observe.find_elements(snap, text="MARKER-P1-UNIQUE")
    assert len(matches) == 1
    assert matches[0].role == "AXStaticText"
    assert "MARKER-P1-UNIQUE" in (matches[0].value or "")
    assert observe.find_elements(snap, text="quick brown fox")
    assert observe.find_elements(snap, text="MARKER-P3-UNIQUE") == ()


@pytest.mark.parametrize("visible_index", [1, 2, 3])
def test_evince_page_visibility_uses_showing_not_drifting_extents(
    fake_atspi, visible_index,
) -> None:
    app = _Node("application", None, name="evince")
    pages = [
        _Node("page", f"PAGE-{index} TOKEN-{index}",
              box=((689.0, 21.0 if index == visible_index - 1 else 2292.0), (562.0, 727.0)))
        for index in range(4)
    ]
    for index, page in enumerate(pages):
        page.get_application = lambda: app
        page.get_state_set = lambda index=index: type(
            "States", (), {"names": {"SHOWING"} if index == visible_index else {"VISIBLE"}},
        )()
    document = _Node("document frame", None, name="Document View")
    document.get_child_count = lambda: len(pages)
    document.get_child_at_index = lambda index: pages[index]
    accessor = _atspi.ATSPIAccessor()
    children = accessor.children(document)
    assert children == [pages[visible_index]]
    raw = accessor.read(children[0])
    assert raw.position is None and raw.size is None
    snap = _snap(raw)
    assert f"TOKEN-{visible_index}" in observe.render_text(snap)
    matches = observe.find_elements(snap, text=f"TOKEN-{visible_index}")
    assert len(matches) == 1
    assert matches[0].bounds is None
    for index in range(4):
        if index != visible_index:
            assert observe.find_elements(snap, text=f"TOKEN-{index}") == ()


def test_other_app_page_keeps_native_bounds(fake_atspi) -> None:
    app = _Node("application", None, name="other-viewer")
    page = _Node("page", _PAGE)
    page.get_application = lambda: app
    raw = _read(page)
    assert raw.position == page.box[0]
    assert raw.size == page.box[1]
    assert raw.unallocated is False
    assert observe.find_elements(_snap(raw), text="MARKER-P1-UNIQUE")[0].bounds is not None


def test_find_matches_page_text_past_the_shown_value(fake_atspi) -> None:
    """Find searches the page past the value the model reads.

    ``scroll_to_find`` uses that same match. The rendered line stays clipped.
    """
    marker = "PAST-THE-CAP-TOKEN"
    body = ("x" * (observe._MAX_VALUE_CHARS + 20)) + marker
    assert len(body) < _atspi._PAGE_TEXT_CHARS
    raw = _read(_Node("page", body))
    snap = _snap(raw)
    matches = observe.find_elements(snap, text=marker)
    assert len(matches) == 1
    assert marker not in (matches[0].value or "")
    rendered = observe.render_text(snap)
    assert marker not in rendered
    assert "xxxx" in rendered


def test_page_text_read_is_bounded(fake_atspi) -> None:
    """A page longer than the cap is cut at the cap. The tail is not searchable."""
    cap = _atspi._PAGE_TEXT_CHARS
    body = "HEADTOKEN " + ("x" * cap) + " TAILTOKEN"
    node = _Node("page", body)
    raw = _read(node)
    assert raw.value is not None
    assert "HEADTOKEN" in raw.value
    assert "TAILTOKEN" not in raw.value
    assert len(raw.value) <= cap
    assert node.reads == [(0, cap)]
    snap = _snap(raw)
    assert observe.find_elements(snap, text="HEADTOKEN")
    assert observe.find_elements(snap, text="TAILTOKEN") == ()


def test_blank_page_stays_a_region_and_a_group_does_not_read_text(fake_atspi) -> None:
    """No Text interface leaves the page a group. A panel does not read Text."""
    blank = _Node("page", None)
    raw = _read(blank)
    assert raw.role == "AXGroup"
    assert raw.value is None
    assert blank.reads == []
    snap = _snap(raw)
    assert any(el.role == "opaque_region" for el in snap.elements)
    assert observe.find_elements(snap, text="MARKER-P1-UNIQUE") == ()

    panel = _Node("panel", "SECRET-PANEL")
    panel_raw = _read(panel)
    assert panel_raw.role == "AXGroup"
    assert panel_raw.value is None
    assert panel.reads == []
    assert "SECRET-PANEL" not in observe.render_text(_snap(panel_raw))


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
        objects[contents_id] = f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
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
    out += f"trailer << /Size {font + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    return bytes(out)


def _page_lines(number: int) -> list[str]:
    return [
        f"PAGE-{number} heading",
        f"Alpha line on page {number}",
        f"MARKER-P{number}-UNIQUE token",
        "The quick brown fox jumps.",
        f"End of page {number}",
    ]


def test_live_evince_page_text_is_in_the_snapshot_and_find(tmp_path) -> None:
    """Live Evince. The on-screen page's text is in the snapshot and find matches it.

    A page that is fully off screen stays out. Skips when Evince or a display
    is absent. The words come from the Text interface. Nothing is read from
    pixels.
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

    path = tmp_path / "evince-page-text.pdf"
    path.write_bytes(_pdf([_page_lines(1), _page_lines(2), _page_lines(3)]))
    proc = subprocess.Popen(
        ["evince", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 25
        snap = None
        last = ""
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(f"evince exited with status {proc.returncode}")
            try:
                shot = driver.snapshot(Scope.WINDOW, "evince")
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
                shot = None
            else:
                last = observe.render_text(shot)
                if observe.find_elements(shot, text="MARKER-P1-UNIQUE"):
                    snap = shot
                    break
            time.sleep(0.4)
        assert snap is not None, last[:1600]
        rendered = observe.render_text(snap)
        assert "PAGE-1 heading" in rendered
        assert "MARKER-P3-UNIQUE" not in rendered
        matches = observe.find_elements(snap, text="MARKER-P1-UNIQUE")
        assert matches
        assert all(el.role != "opaque_region" for el in matches)
        assert "The quick brown fox jumps." in (matches[0].value or "")
        assert observe.find_elements(snap, text="Alpha line on page 1")
        assert observe.find_elements(snap, text="MARKER-P3-UNIQUE") == ()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
