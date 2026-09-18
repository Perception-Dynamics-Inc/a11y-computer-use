"""Set-of-Mark overlay — pure PIL compositing + ref→image mapping (any OS)."""

from __future__ import annotations

import io

from PIL import Image

from a11y_computer_use import marks
from a11y_computer_use.schema import Bounds, Display, Element, Scope, Snapshot


def _png(w: int = 40, h: int = 30) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (0, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


def test_draw_marks_returns_valid_same_size_png_with_ink() -> None:
    out = marks.draw_marks(_png(40, 30), [("e1", 2, 2, 12, 8), ("e2", 20, 15, 12, 8)])
    img = Image.open(io.BytesIO(out)).convert("RGB")
    assert img.size == (40, 30)
    assert any(px == (255, 40, 40) for _n, px in img.getcolors(maxcolors=100000))  # marks drawn


def test_draw_marks_empty_is_noop() -> None:
    p = _png()
    assert marks.draw_marks(p, []) == p


class _Scaled:
    scale = 0.5

    def from_source(self, x: int, y: int) -> tuple[int, int]:
        return round(x * 0.5), round(y * 0.5)


def _el(ref: str, x: int, y: int, w: int, h: int, *, did: int = 0,
        clickable: bool = True, editable: bool = False) -> Element:
    return Element(ref=ref, role="AXButton", title="", value=None,
                   bounds=Bounds(did, x, y, w, h), snapshot_id="s",
                   clickable=clickable, editable=editable)


def test_marks_for_only_interactive_on_the_captured_display() -> None:
    snap = Snapshot(
        snapshot_id="s", scope=Scope.WINDOW, app="a", pid=1, created_at=0.0,
        displays=(Display(0, 100, 100, 1.0, True),),
        elements=(
            _el("e1", 10, 20, 40, 30),                                  # interactive, display 0
            _el("e2", 0, 0, 10, 10, clickable=False, editable=False),   # not interactive
            _el("e3", 10, 10, 20, 20, did=1),                           # other display
            _el("e4", 4, 4, 8, 8, clickable=False, editable=True),      # editable counts
        ),
    )
    m = marks.marks_for(snap, _Scaled(), 0)
    assert ("e1", 5, 10, 20, 15) in m   # mapped by 0.5
    assert ("e4", 2, 2, 4, 4) in m
    assert not any(lbl in {"e2", "e3"} for lbl, *_ in m)
