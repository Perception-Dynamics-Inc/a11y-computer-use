"""Auto-OCR escalation and `screen_text(app=...)` crop to the app's windows.

The live trials showed the whole-display fallback reading the menu bar,
Telegram, and other apps' pixels into the refs. Hermetic through the fixtures
in `tests.test_ocr`: a 1600x1000 display captured at backing scale 2.
"""
from __future__ import annotations

import io

import pytest

from a11y_computer_use.schema import ComputerUseError, ErrorCode
from PIL import Image

from a11y_computer_use import ocr, safety, server
from a11y_computer_use.schema import Bounds, Display, Element, Scope, Snapshot
from tests.test_ocr import APP, DISPLAY, FakeDriver, _box

WINDOW = Bounds(1, 200, 100, 800, 600)  # display pixels
SECOND = Bounds(1, 900, 400, 300, 500)


class RecordingOcr(ocr.FakeOcr):
    """FakeOcr that remembers the size of every image it was handed."""

    def __init__(self, boxes):
        super().__init__(boxes)
        self.sizes: list[tuple[int, int]] = []

    def recognize(self, png: bytes):
        with Image.open(io.BytesIO(png)) as im:
            self.sizes.append(im.size)
        return super().recognize(png)


class WindowedDriver(FakeDriver):
    def __init__(self, windows: tuple[Bounds, ...] = (WINDOW,), *, rows: list[dict] | None = None):
        super().__init__()
        self._windows = windows
        self.rows = rows if rows is not None else []

    def snapshot(self, scope, app):
        self.snapshots += 1
        els = tuple(
            Element(f"e{i + 1}", "AXWindow", f"Window {i + 1}", None, b, f"snap-{self.snapshots}",
                    path=("AXWindow",))
            for i, b in enumerate(self._windows)
        )
        return Snapshot(f"snap-{self.snapshots}", scope, app, 77, 0.0, (DISPLAY,), els)

    def windows(self):
        return list(self.rows)


def make_runtime(tmp_path, driver, engine):
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier(APP, safety.Tier.CLICK)
    return server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"),
                          driver=driver, ocr_engine=engine)


def test_escalation_crops_to_the_window_and_offsets_the_refs(tmp_path) -> None:
    # A word at the top-left of the CROPPED image sits at the window's origin on screen.
    engine = RecordingOcr((_box("Saved Messages", 0, 0, w=300, h=40),))
    rt = make_runtime(tmp_path, WindowedDriver(), engine)
    text = rt.desktop_snapshot(APP)
    assert engine.sizes[-1] == (WINDOW.width * 2, WINDOW.height * 2)  # 800x600 display px at scale 2
    assert "(OCR cropped to this app's window)" in text
    line = rt._screen_text.line("o1")
    assert line.text == "Saved Messages"
    assert (line.bounds.x, line.bounds.y) == (WINDOW.x, WINDOW.y)


def test_escalation_unions_several_windows(tmp_path) -> None:
    engine = RecordingOcr(())
    rt = make_runtime(tmp_path, WindowedDriver((WINDOW, SECOND)), engine)
    text = rt.desktop_snapshot(APP)
    union_w, union_h = (SECOND.x + SECOND.width) - WINDOW.x, (SECOND.y + SECOND.height) - WINDOW.y
    assert engine.sizes[-1] == (union_w * 2, union_h * 2)
    assert "(OCR cropped to this app's windows)" in text


def test_escalation_without_a_window_rect_skips_ocr_instead_of_reading_the_display(tmp_path) -> None:
    """Issue #10: a whole-display OCR under one app's grant returned another app's text."""
    engine = RecordingOcr(())
    rt = make_runtime(tmp_path, WindowedDriver(()), engine)  # no window elements, no window rows
    text = rt.desktop_snapshot(APP)
    assert engine.sizes == []  # nothing captured
    assert "OCR skipped rather than read the whole display" in text


def test_escalation_uses_the_driver_window_list_when_the_tree_has_no_windows(tmp_path) -> None:
    rows = [{"app": APP, "bounds": {"display_id": 1, "x": 50, "y": 60, "width": 400, "height": 300}},
            {"app": "com.other", "bounds": {"display_id": 1, "x": 0, "y": 0, "width": 1600, "height": 1000}}]
    engine = RecordingOcr(())
    rt = make_runtime(tmp_path, WindowedDriver((), rows=rows), engine)
    rt.desktop_snapshot(APP)
    assert engine.sizes[-1] == (800, 600)  # only this app's row


def test_opt_out_still_disables_the_escalation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTO_OCR", False)
    engine = RecordingOcr(())
    rt = make_runtime(tmp_path, WindowedDriver(), engine)
    assert "[ocr-" not in rt.desktop_snapshot(APP) and engine.sizes == []


def test_screen_text_app_crops_to_that_apps_windows(tmp_path) -> None:
    engine = RecordingOcr((_box("Chats", 0, 0, w=200, h=40),))
    rt = make_runtime(tmp_path, WindowedDriver(), engine)
    rt.desktop_snapshot(APP)  # the window rect comes from the current snapshot
    text = rt.screen_text(app=APP)
    assert f"(OCR cropped to {APP}'s windows)" in text
    assert engine.sizes[-1] == (WINDOW.width * 2, WINDOW.height * 2)
    assert (rt._screen_text.line("o1").bounds.x, rt._screen_text.line("o1").bounds.y) == (WINDOW.x, WINDOW.y)
    with pytest.raises(ValueError, match="either app or region"):
        rt.screen_text(app=APP, region={"x": 0, "y": 0, "width": 10, "height": 10})


def test_screen_text_app_without_a_known_window_is_a_structured_refusal(tmp_path) -> None:
    """Issue #10: never read the whole display under one app's grant."""
    engine = RecordingOcr(())
    rt = make_runtime(tmp_path, WindowedDriver(()), engine)
    with pytest.raises(ComputerUseError) as info:
        rt.screen_text(app=APP)
    assert info.value.code is ErrorCode.UNSUPPORTED and info.value.detail["reason"] == "app_not_on_screen"
    assert engine.sizes == []


def test_screen_text_app_captures_the_window_itself_when_the_driver_can(tmp_path) -> None:
    """Issue #12: one window's own pixels, whatever covers it, instead of a display crop."""
    import io

    from PIL import Image

    rows = [{"app": APP, "window_id": 77, "bounds": {"display_id": 1, "x": 50, "y": 60, "width": 400, "height": 300}}]
    engine = RecordingOcr(())
    driver = WindowedDriver((), rows=rows)
    captured = []

    def window_png(window_id):
        captured.append(window_id)
        buf = io.BytesIO(); Image.new("RGB", (800, 600), (0, 0, 0)).save(buf, format="PNG")
        return buf.getvalue()

    driver.window_png = window_png
    rt = make_runtime(tmp_path, driver, engine)
    text = rt.screen_text(app=APP)
    assert captured == [77] and engine.sizes[-1] == (800, 600)  # the window image, not the display
    assert f"(OCR of {APP}'s window 77: its own pixels, whatever covers it)" in text


def test_window_capture_failure_is_a_refusal_not_a_display_crop(tmp_path) -> None:
    """Issue #13: the crop at a window's rect showed another app on the user's Space."""
    rows = [{"app": APP, "window_id": 77, "bounds": {"display_id": 1, "x": 50, "y": 60, "width": 400, "height": 300}}]
    engine = RecordingOcr(())
    driver = WindowedDriver((), rows=rows)
    driver.window_png = lambda window_id: (_ for _ in ()).throw(RuntimeError("gone"))
    rt = make_runtime(tmp_path, driver, engine)
    with pytest.raises(ComputerUseError) as info:
        rt.screen_text(app=APP)
    assert info.value.detail["reason"] == "window_not_capturable" and engine.sizes == []

