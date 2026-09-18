"""OCR refs: text on screen becomes o1..oN refs for apps without a tree.

Hermetic on every OS through `ocr.FakeOcr` and a fake driver whose "display"
is a 1600x1000 screen captured at backing scale 2 (a 3200x2000 PNG), so the
image-to-display mapping is exercised on every test. Two macOS-only tests use
the real Vision engine: one on a rendered PNG (needs no TCC grant), one on the
live screen (needs Screen Recording, skips otherwise).
"""

from __future__ import annotations

import io
import json
import sys

import pytest
from PIL import Image

from a11y_computer_use import ocr, safety, server
from a11y_computer_use.schema import (
    Bounds,
    ComputerUseError,
    Display,
    Element,
    ErrorCode,
    Point,
    Scope,
    Snapshot,
)
from tests.conftest import HAS_SCREEN

APP = "ru.keepcoder.Telegram"
DISPLAY = Display(display_id=1, width=1600, height=1000, scale=2.0, is_main=True)
IMG_W, IMG_H = 3200, 2000  # the capture is at backing scale: 2 image px per display px


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (11, 18, 32)).save(buf, format="PNG")
    return buf.getvalue()


def _box(text: str, x: int, y: int, w: int = 300, h: int = 40, conf: float = 0.95) -> ocr.TextBox:
    """A box in IMAGE pixels."""
    return ocr.TextBox(text, x, y, w, h, conf)


# The screen as the fake engine sees it (image pixels). "Saved Messages" sits at
# image (400, 600) -> display (200, 300), centre (275, 310) in display pixels.
BOXES = (
    _box("Telegram", 100, 100),
    _box("Saved Messages", 400, 600),
    _box("Write a message...", 400, 1800, w=600),
    _box("faint", 2000, 1000, conf=0.2),  # below the default confidence floor
)


def _window_only(snapshot_id: str, *, secure_at: Bounds | None = None) -> Snapshot:
    """A custom-drawn app: one window, no actionable element (the handoff case).
    ``secure_at`` adds a password field so the o-ref hit-test can refuse it."""
    els = [
        Element("e1", "AXWindow", "Telegram", None, Bounds(1, 0, 0, 1600, 1000), snapshot_id,
                path=("AXWindow",)),
    ]
    if secure_at is not None:
        els.append(Element("e2", "AXSecureTextField", "Password", None, secure_at, snapshot_id,
                           parent="e1", path=("AXWindow", "AXSecureTextField"), clickable=True,
                           editable=True, secure=True))
    return Snapshot(snapshot_id, Scope.WINDOW, APP, 77, 0.0, (DISPLAY,), tuple(els))


class FakeDriver:
    """Resolves its own apps (like the browser backend) so the whole gated
    Runtime runs without OS system-ops; records pointer calls."""

    name = "fake"
    resolves_apps = True

    def __init__(self, *, secure_at: Bounds | None = None, frontmost: str = APP) -> None:
        self.calls: dict[str, list] = {k: [] for k in ("click", "drag", "scroll", "press")}
        self.snapshots = 0
        self.captures = 0
        self.secure_at = secure_at
        self.frontmost = frontmost

    def ensure_trusted(self) -> None:
        pass

    def snapshot(self, scope, app):
        self.snapshots += 1
        return _window_only(f"snap-{self.snapshots}", secure_at=self.secure_at)

    def resolve_ref(self, snap, ref, *, live=None):
        return snap.element(ref)

    def press_element(self, element) -> bool:
        self.calls["press"].append(element.ref)
        return True

    def scroll_into_view(self, element) -> bool:
        return False

    def set_value(self, element, value) -> bool:
        return False

    def click(self, target, *, button, count, modifiers, pre_check=None, dry_run=False):
        self.calls["click"].append((target, button.value, count, tuple(modifiers)))

    def drag(self, start, end, *, button=None, pre_check=None, dry_run=False):
        self.calls["drag"].append((start, end))

    def scroll(self, target, *, dx=0, dy=0, unit=None, pre_check=None, dry_run=False):
        self.calls["scroll"].append((target, dx, dy, unit.value))

    def type_text(self, text, *, pre_check=None, dry_run=False):
        pass

    def key_chord(self, chord, *, pre_check=None, dry_run=False):
        pass

    def wait_for(self, target, *, condition, timeout_s, checker=None):
        return target

    def screenshot(self, display_id=None):
        from a11y_computer_use import capture

        self.captures += 1
        return capture.Screenshot(png=_png(IMG_W, IMG_H), display=DISPLAY)

    def main_display_id(self) -> int:
        return DISPLAY.display_id

    def zoom_region(self, region):
        return _png(region.width, region.height)

    def frontmost_app(self):
        return self.frontmost, 77

    def app_at_point(self, point):
        return APP


def make_runtime(tmp_path, *, engine=None, tier=safety.Tier.CLICK, driver=None):
    store = safety.PermissionStore(tmp_path / "perm.json")
    if tier is not None:
        store.set_tier(APP, tier)
    return server.Runtime(
        store=store, audit=safety.AuditLog(tmp_path / "audit"),
        driver=driver or FakeDriver(), ocr_engine=engine if engine is not None else ocr.FakeOcr(BOXES),
    )


def audit_rows(tmp_path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted((tmp_path / "audit").glob("*.jsonl")):
        rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return rows


# --- pure helpers -------------------------------------------------------------


def test_group_lines_merges_words_on_a_row_and_keeps_rows_apart() -> None:
    words = [_box("Messages", 260, 100, w=120, h=30), _box("Saved", 100, 100, w=140, h=30),
             _box("Settings", 100, 400, w=200, h=30)]
    lines = ocr.group_lines(words)
    assert [[b.text for b in line] for line in lines] == [["Saved", "Messages"], ["Settings"]]


def test_group_lines_keeps_distant_words_on_the_same_row_apart() -> None:
    # Two columns on one row: the horizontal gap (700 px) dwarfs the box height.
    lines = ocr.group_lines([_box("Left", 0, 0, w=100, h=30), _box("Right", 800, 0, w=100, h=30)])
    assert len(lines) == 2


def test_build_screen_text_scales_image_pixels_onto_the_display_and_filters_confidence() -> None:
    screen = ocr.build_screen_text(BOXES, display=DISPLAY, image_width=IMG_W, image_height=IMG_H,
                                   text_id="ocr-1")
    assert [line.ref for line in screen.lines] == ["o1", "o2", "o3"]  # "faint" dropped
    saved = screen.line("o2")
    assert saved.text == "Saved Messages"
    assert saved.bounds == Bounds(1, 200, 300, 150, 20)  # image / 2
    assert saved.center == Point(1, 275, 310)
    assert screen.line("o1").text == "Telegram"  # reading order: top first


def test_build_screen_text_applies_a_region_offset() -> None:
    screen = ocr.build_screen_text([_box("Send", 0, 0, w=100, h=40)], display=DISPLAY,
                                   image_width=200, image_height=80, text_id="ocr-9",
                                   offset=(500, 700), target_size=(100, 40))
    # A 200x80 image of a 100x40 display-pixel region: half scale, plus the offset.
    line = screen.line("o1")
    assert (line.bounds.x, line.bounds.y) == (500, 700)
    assert (line.bounds.width, line.bounds.height) == (50, 20)


def test_render_lists_refs_text_rect_and_confidence() -> None:
    screen = ocr.build_screen_text(BOXES, display=DISPLAY, image_width=IMG_W, image_height=IMG_H,
                                   text_id="ocr-1")
    text = ocr.render_screen_text(screen)
    assert text.splitlines()[0].startswith("[ocr-1] display 1: 3 text line(s) via OCR")
    assert '  o2 "Saved Messages" [150x20 @1:200,300] (0.95)' in text
    assert "(no text recognised)" in ocr.render_screen_text(
        ocr.build_screen_text([], display=DISPLAY, image_width=IMG_W, image_height=IMG_H, text_id="ocr-2"))


def test_find_lines_is_a_case_insensitive_substring() -> None:
    screen = ocr.build_screen_text(BOXES, display=DISPLAY, image_width=IMG_W, image_height=IMG_H,
                                   text_id="ocr-1")
    assert [l.ref for l in ocr.find_lines(screen, "saved")] == ["o2"]
    assert ocr.find_lines(screen, "nope") == ()


def test_rematch_prefers_same_text_nearby_then_partial_then_reports_candidates() -> None:
    old = ocr.build_screen_text(BOXES, display=DISPLAY, image_width=IMG_W, image_height=IMG_H,
                                text_id="ocr-1").line("o2")
    moved = ocr.build_screen_text([_box("Saved Messages", 500, 700)], display=DISPLAY,
                                  image_width=IMG_W, image_height=IMG_H, text_id="ocr-2")
    match, cands = ocr.rematch_line(old, moved)
    assert match is not None and match.bounds.x == 250 and cands == ()

    partial = ocr.build_screen_text([_box("3 Saved Messages", 400, 620, w=400)], display=DISPLAY,
                                    image_width=IMG_W, image_height=IMG_H, text_id="ocr-3")
    match, _ = ocr.rematch_line(old, partial)
    assert match is not None and match.text == "3 Saved Messages"

    far = ocr.build_screen_text([_box("Saved Messages", 3000, 1900)], display=DISPLAY,
                                image_width=IMG_W, image_height=IMG_H, text_id="ocr-4")
    match, cands = ocr.rematch_line(old, far)  # same text, but 1000+ px away: a candidate only
    assert match is None and [c.text for c in cands] == ["Saved Messages"]

    gone = ocr.build_screen_text([_box("Saved Photos", 400, 600), _box("Chats", 400, 900),
                                  _box("Telegram", 100, 100), _box("Zzz", 0, 0)],
                                 display=DISPLAY, image_width=IMG_W, image_height=IMG_H, text_id="ocr-5")
    match, cands = ocr.rematch_line(old, gone)
    assert match is None and len(cands) == 3 and cands[0].text == "Saved Photos"


def test_is_ocr_ref() -> None:
    assert ocr.is_ocr_ref("o7") and ocr.is_ocr_ref("o12")
    assert not ocr.is_ocr_ref("e7") and not ocr.is_ocr_ref("o") and not ocr.is_ocr_ref(None)


# --- Runtime: screen_text, o-ref actions, escalation --------------------------


def test_screen_text_publishes_o_refs_in_display_pixels_and_is_gated(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    text = rt.screen_text()
    assert text.startswith("[ocr-1] display 1: 3 text line(s)")
    assert '  o2 "Saved Messages" [150x20 @1:200,300]' in text
    row = audit_rows(tmp_path)[-1]
    assert row["action"] == "observeop" and row["result"] == "ok"

    ungranted = make_runtime(tmp_path / "u", tier=None)
    with pytest.raises(server.ActionRefused):
        ungranted.screen_text()


def test_click_on_an_o_ref_re_reads_the_screen_and_clicks_the_live_text_centre(tmp_path) -> None:
    calls = {"n": 0}

    def engine_boxes(png: bytes):
        calls["n"] += 1
        if calls["n"] == 1:
            return BOXES
        return (_box("Saved Messages", 500, 700),)  # the line moved by (+50, +50) display px

    driver = FakeDriver()
    rt = make_runtime(tmp_path, engine=ocr.FakeOcr(engine_boxes), driver=driver)
    rt.screen_text()
    msg = rt.click(ref="o2")
    assert msg == "clicked o2 (text 'Saved Messages')"
    target, button, count, mods = driver.calls["click"][0]
    assert target == Point(1, 325, 360)  # new centre, in display pixels
    assert (button, count, mods) == ("left", 1, ())
    assert driver.calls["press"] == []  # a point, not an AX element
    assert driver.captures == 2  # the observation, then the re-read at act time


def test_click_on_a_vanished_o_ref_is_stale_with_candidates_and_audited(tmp_path) -> None:
    calls = {"n": 0}

    def engine_boxes(png: bytes):
        calls["n"] += 1
        if calls["n"] == 1:
            return BOXES
        return (_box("Saved Photos", 400, 600), _box("Chats", 400, 900))

    driver = FakeDriver()
    rt = make_runtime(tmp_path, engine=ocr.FakeOcr(engine_boxes), driver=driver)
    rt.screen_text()
    with pytest.raises(ComputerUseError) as info:
        rt.click(ref="o2")
    assert info.value.code is ErrorCode.STALE_REF
    assert [c["text"] for c in info.value.detail["candidates"]][0] == "Saved Photos"
    assert driver.calls["click"] == []
    assert audit_rows(tmp_path)[-1]["result"] == "stale_ref"


def test_o_ref_without_an_epoch_or_from_another_epoch_is_stale(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    with pytest.raises(ComputerUseError) as info:
        rt.click(ref="o1")
    assert info.value.code is ErrorCode.STALE_REF and "screen_text" in str(info.value)
    rt.screen_text()
    with pytest.raises(ComputerUseError) as info:
        rt.click(ref="o9")
    assert info.value.code is ErrorCode.STALE_REF


def test_rematch_can_be_disabled_to_trust_the_stored_box(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "OCR_REMATCH", False)
    driver = FakeDriver()
    rt = make_runtime(tmp_path, driver=driver)
    rt.screen_text()
    rt.click(ref="o2")
    assert driver.calls["click"][0][0] == Point(1, 275, 310)
    assert driver.captures == 1


def test_o_ref_over_a_secure_field_is_refused(tmp_path) -> None:
    driver = FakeDriver(secure_at=Bounds(1, 180, 290, 300, 40))  # covers the o2 text
    rt = make_runtime(tmp_path, driver=driver)
    rt.desktop_snapshot(APP)  # the a11y epoch that knows where the secure field is
    rt.screen_text()
    with pytest.raises(ComputerUseError) as info:
        rt.click(ref="o2")
    assert info.value.code is ErrorCode.SECURE_FIELD
    assert driver.calls["click"] == []


def test_drag_and_scroll_accept_o_refs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "OCR_REMATCH", False)
    driver = FakeDriver()
    rt = make_runtime(tmp_path, driver=driver)
    rt.screen_text()
    assert rt.drag(start_ref="o1", end_ref="o3") == "dragged o1 (text 'Telegram') -> o3 (text 'Write a message...')"
    start, end = driver.calls["drag"][0]
    assert isinstance(start, Point) and isinstance(end, Point)
    assert rt.scroll(ref="o2", dy=3).startswith("scrolled o2 (text 'Saved Messages') by")


def test_empty_snapshot_escalates_to_ocr_lines_in_the_same_reply(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    text = rt.desktop_snapshot(APP)
    assert "no interactive elements were found" in text
    assert "[ocr-1] display 1: 3 text line(s)" in text and '  o2 "Saved Messages"' in text
    assert rt.click(ref="o2").startswith("clicked o2")  # the appended refs are live


def test_escalation_is_opt_out_and_never_captures_another_app(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTO_OCR", False)
    assert "[ocr-" not in make_runtime(tmp_path / "a").desktop_snapshot(APP)
    monkeypatch.setattr(server, "AUTO_OCR", True)
    other_front = make_runtime(tmp_path / "b", driver=FakeDriver(frontmost="com.apple.finder"))
    other_front.store.set_tier("com.apple.finder", safety.Tier.READ)
    text = other_front.desktop_snapshot(APP)
    assert "[ocr-" not in text and "Call `screen_text` once this app is frontmost" in text


def test_escalation_reports_a_missing_screen_grant_instead_of_failing(tmp_path) -> None:
    class NoScreen(FakeDriver):
        def screenshot(self, display_id=None):
            raise ComputerUseError(ErrorCode.PERMISSION_DENIED_SCREEN, "no grant")

    text = make_runtime(tmp_path, driver=NoScreen()).desktop_snapshot(APP)
    assert "(auto OCR unavailable: permission_denied_screen)" in text


def test_find_ocr_returns_only_matching_lines_as_live_refs(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    text = rt.find(APP, text="message", ocr=True)
    lines = [l for l in text.splitlines()[1:]]
    assert len(lines) == 2 and all("essage" in l for l in lines)
    assert "o2" in lines[0] and "o3" in lines[1]  # refs from the fresh epoch, not renumbered
    with pytest.raises(ValueError):
        rt.find(APP, ocr=True)
    assert "no text line contains 'zzz'" in rt.find(APP, text="zzz", ocr=True)


def test_wait_for_on_an_o_ref_polls_ocr_until_gone_or_times_out(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "OCR_WAIT_POLL_S", 0.01)
    seen = {"n": 0}

    def engine_boxes(png: bytes):
        seen["n"] += 1
        return BOXES if seen["n"] <= 3 else (_box("Telegram", 100, 100),)

    rt = make_runtime(tmp_path, engine=ocr.FakeOcr(engine_boxes))
    rt.screen_text()
    assert rt.wait_for("o2", "gone", timeout_s=5) == "o2 gone: satisfied"
    assert seen["n"] >= 4
    with pytest.raises(ComputerUseError) as info:
        rt.wait_for("o1", "gone", timeout_s=0.05)  # "Telegram" never leaves
    assert info.value.code is ErrorCode.TIMEOUT


def test_screenshot_marks_draw_o_refs_in_blue(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    plain, plain_img = rt.screenshot(marks=True)
    assert "OCR" not in plain
    rt.screen_text()
    text, marked = rt.screenshot(marks=True)
    assert "3 OCR text lines are marked in blue" in text
    assert marked.png != plain_img.png
    img = Image.open(io.BytesIO(marked.png)).convert("RGB")
    # the o2 chip sits at the scaled position of display (200, 300) -> image (160, 240)
    assert img.getpixel((161, 241)) == (42, 140, 255)


def test_screen_text_region_crops_and_offsets(tmp_path) -> None:
    engine = ocr.FakeOcr(lambda png: (_box("Send", 10, 10, w=100, h=20),))
    rt = make_runtime(tmp_path, engine=engine)
    text = rt.screen_text(region={"x": 1000, "y": 800, "width": 400, "height": 100})
    # crop of display (1000,800)-(1400,900) is 800x200 image px; box at (10,10) -> +5,+5 display px
    assert '  o1 "Send" [50x10 @1:1005,805]' in text
    with pytest.raises(ValueError):
        rt.screen_text(region={"x": 1, "y": 2})


def test_screen_text_without_an_engine_is_unsupported(tmp_path) -> None:
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier(APP, safety.Tier.READ)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=FakeDriver(),
                        ocr_engine=ocr.FakeOcr())
    rt._ocr_engine = None  # what default_engine() yields off macOS
    with pytest.raises(ComputerUseError) as info:
        rt.screen_text()
    assert info.value.code is ErrorCode.UNSUPPORTED
    assert "[ocr-" not in rt.desktop_snapshot(APP)  # escalation stays silent without an engine


def test_call_tool_and_mcp_expose_screen_text(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    assert rt.call_tool("screen_text", {}).startswith("[ocr-1]")
    specs = {s["name"]: s for s in server.tool_specs(rt)}
    assert "screen_text" in specs and "region" in specs["screen_text"]["input_schema"]["properties"]
    assert "ocr" in specs["find"]["input_schema"]["properties"]


# --- the real engine (macOS) -------------------------------------------------


def _rendered_text_png() -> bytes:
    from PIL import ImageDraw, ImageFont

    img = Image.new("RGB", (900, 260), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 56)
    except OSError:
        font = ImageFont.load_default()
    draw.text((40, 40), "Saved Messages", fill="black", font=font)
    draw.text((40, 150), "Write a message", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.skipif(sys.platform != "darwin", reason="Vision framework is macOS-only")
def test_vision_engine_reads_rendered_text_without_any_grant() -> None:
    pytest.importorskip("Vision")
    boxes = ocr.VisionOcr().recognize(_rendered_text_png())
    texts = " | ".join(b.text.lower() for b in boxes)
    assert "saved messages" in texts and "write a message" in texts
    first = min(boxes, key=lambda b: b.y)
    assert 0 <= first.x < 200 and 0 <= first.y < 120 and first.confidence > 0.3


@pytest.mark.skipif(sys.platform != "darwin" or not HAS_SCREEN,
                    reason="live OCR of the screen needs macOS + the Screen Recording grant")
def test_live_screen_has_readable_text() -> None:
    pytest.importorskip("Vision")
    from a11y_computer_use import capture

    shot = capture.screenshot()
    boxes = ocr.VisionOcr().recognize(shot.png)
    assert boxes, "expected at least one line of text on the live screen"
