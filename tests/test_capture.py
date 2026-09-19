"""Capture module tests.

Live screen capture needs the Screen Recording TCC grant, so the suite runs
on synthetic Pillow images with the capture internals monkeypatched; the
grant-dependent tests are guarded via HAS_SCREEN (see conftest). The
structured permission-error path is asserted both monkeypatched and — on an
ungranted machine — against the real preflight. `request_permission` is
deliberately untested: calling it pops the system TCC dialog.
"""

from __future__ import annotations

import io
import math
import sys

import pytest
from PIL import Image

from a11y_computer_use import capture
from a11y_computer_use.schema import Bounds, ComputerUseError, Display, ErrorCode
from tests.conftest import HAS_DISPLAYS, HAS_SCREEN


def _solid_png(width: int, height: int) -> bytes:
    """A flat single-color PNG — enough for dimension/mapping tests."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (30, 90, 150)).save(buffer, format="PNG")
    return buffer.getvalue()


def _coordinate_png(width: int, height: int) -> bytes:
    """A PNG whose pixel (x, y) encodes its own position: (x%256, y%256, 0).

    Lets crop tests assert the crop grabbed exactly the requested pixels.
    """
    image = Image.new("RGB", (width, height))
    image.putdata([(x % 256, y % 256, 0) for y in range(height) for x in range(width)])
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def fake_screen(monkeypatch) -> Display:
    """Route capture at a synthetic 320x240 'display' with the grant present."""
    display = Display(display_id=1, width=320, height=240, scale=2.0, is_main=True)
    png = _coordinate_png(display.width, display.height)
    monkeypatch.setattr(capture, "_preflight_screen", lambda: True)
    monkeypatch.setattr(capture, "displays", lambda: (display,))
    monkeypatch.setattr(capture, "_capture_display_png", lambda d: png)
    return display


# ---------------------------------------------------------------------------
# downscale: dimension math
# ---------------------------------------------------------------------------


def test_downscale_caps_long_edge_landscape() -> None:
    scaled = capture.downscale(_solid_png(3000, 2000), max_long_edge=1280)
    assert (scaled.width, scaled.height) == (1280, 853)
    assert (scaled.source_width, scaled.source_height) == (3000, 2000)
    assert scaled.scale == pytest.approx(1280 / 3000)


def test_downscale_caps_long_edge_portrait() -> None:
    scaled = capture.downscale(_solid_png(500, 4000), max_long_edge=1280)
    assert (scaled.width, scaled.height) == (160, 1280)


def test_downscale_png_dimensions_match_metadata() -> None:
    scaled = capture.downscale(_solid_png(2560, 1600), max_long_edge=1280)
    image = Image.open(io.BytesIO(scaled.png))
    assert image.size == (scaled.width, scaled.height)


def test_downscale_passthrough_when_already_small() -> None:
    source = _solid_png(1000, 500)
    scaled = capture.downscale(source, max_long_edge=1280)
    assert scaled.png == source, "an in-budget image must pass through unrecoded"
    assert (scaled.width, scaled.height) == (1000, 500)
    assert scaled.scale == 1.0


def test_downscale_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError):
        capture.downscale(_solid_png(10, 10), max_long_edge=0)
    with pytest.raises(ValueError):
        capture.downscale(b"definitely not an image")


# ---------------------------------------------------------------------------
# downscale: coordinate mapping round-trip
# ---------------------------------------------------------------------------


def test_coordinate_mapping_round_trip() -> None:
    scaled = capture.downscale(_solid_png(3000, 2000), max_long_edge=1280)
    # One integer-rounding hop each way: error is bounded by ceil(0.5/scale).
    tolerance = math.ceil(0.5 / scaled.scale)
    for source_x, source_y in [(0, 0), (2999, 1999), (1500, 1000), (37, 1234)]:
        x, y = scaled.from_source(source_x, source_y)
        assert 0 <= x < scaled.width and 0 <= y < scaled.height
        back_x, back_y = scaled.to_source(x, y)
        assert abs(back_x - source_x) <= tolerance
        assert abs(back_y - source_y) <= tolerance


def test_mapping_is_identity_without_scaling() -> None:
    scaled = capture.downscale(_solid_png(640, 480), max_long_edge=1280)
    assert scaled.to_source(123, 45) == (123, 45)
    assert scaled.from_source(123, 45) == (123, 45)


def test_to_source_clamps_out_of_range_model_coordinates() -> None:
    scaled = capture.downscale(_solid_png(3000, 2000), max_long_edge=1280)
    assert scaled.to_source(10 * scaled.width, -5) == (2999, 0)
    assert scaled.from_source(10_000, -1) == (1279, 0)


# ---------------------------------------------------------------------------
# zoom_region: crop bounds clamping
# ---------------------------------------------------------------------------


def test_zoom_region_inside_display(fake_screen: Display) -> None:
    png = capture.zoom_region(Bounds(1, 10, 20, 100, 50))
    image = Image.open(io.BytesIO(png))
    assert image.size == (100, 50)
    assert image.getpixel((0, 0)) == (10, 20, 0)
    assert image.getpixel((99, 49)) == (109, 69, 0)


def test_zoom_region_clamps_negative_origin(fake_screen: Display) -> None:
    png = capture.zoom_region(Bounds(1, -30, -40, 100, 100))
    image = Image.open(io.BytesIO(png))
    assert image.size == (70, 60)
    assert image.getpixel((0, 0)) == (0, 0, 0), "clamped crop starts at the display origin"


def test_zoom_region_clamps_overhanging_edge(fake_screen: Display) -> None:
    png = capture.zoom_region(Bounds(1, 300, 200, 100, 100))
    image = Image.open(io.BytesIO(png))
    assert image.size == (20, 40)
    assert image.getpixel((0, 0)) == (300 % 256, 200, 0)


def test_zoom_region_rejects_fully_offscreen(fake_screen: Display) -> None:
    with pytest.raises(ValueError):
        capture.zoom_region(Bounds(1, 320, 0, 10, 10))
    with pytest.raises(ValueError):
        capture.zoom_region(Bounds(1, -50, 0, 30, 10))


def test_zoom_region_rejects_unknown_display(fake_screen: Display) -> None:
    with pytest.raises(ValueError):
        capture.zoom_region(Bounds(99, 0, 0, 10, 10))


# ---------------------------------------------------------------------------
# screenshot
# ---------------------------------------------------------------------------


def test_screenshot_returns_png_and_display_metadata(fake_screen: Display) -> None:
    shot = capture.screenshot()
    assert shot.display == fake_screen
    image = Image.open(io.BytesIO(shot.png))
    assert image.size == (fake_screen.width, fake_screen.height)


def test_screenshot_rejects_unknown_display(fake_screen: Display) -> None:
    with pytest.raises(ValueError):
        capture.screenshot(display_id=99)


# ---------------------------------------------------------------------------
# permission-denied path
# ---------------------------------------------------------------------------


def test_screenshot_permission_denied_is_structured(monkeypatch) -> None:
    monkeypatch.setattr(capture, "_preflight_screen", lambda: False)
    with pytest.raises(ComputerUseError) as exc_info:
        capture.screenshot()
    error = exc_info.value
    assert error.code is ErrorCode.PERMISSION_DENIED_SCREEN
    assert "a11y_computer_use doctor" in str(error.detail["doctor_hint"])
    assert error.to_dict()["error"] == "permission_denied_screen"


def test_zoom_region_permission_denied_is_structured(monkeypatch) -> None:
    monkeypatch.setattr(capture, "_preflight_screen", lambda: False)
    with pytest.raises(ComputerUseError) as exc_info:
        capture.zoom_region(Bounds(1, 0, 0, 10, 10))
    assert exc_info.value.code is ErrorCode.PERMISSION_DENIED_SCREEN


# ---------------------------------------------------------------------------
# live environment (grant-gated both ways)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not HAS_DISPLAYS,
    reason="needs an unlocked window-server session reporting a display",
)
def test_displays_live_metadata() -> None:
    # Display metadata needs no TCC grant — safe to exercise for real.
    active = capture.displays()
    assert len(active) >= 1
    assert sum(d.is_main for d in active) == 1
    for display in active:
        assert display.width > 0 and display.height > 0
        assert display.scale >= 1.0


@pytest.mark.skipif(
    sys.platform != "darwin" or HAS_SCREEN,
    reason="needs a machine WITHOUT the Screen Recording grant",
)
def test_screenshot_real_permission_error_path() -> None:
    # No monkeypatching: the real preflight must surface the structured error.
    with pytest.raises(ComputerUseError) as exc_info:
        capture.screenshot()
    assert exc_info.value.code is ErrorCode.PERMISSION_DENIED_SCREEN


@pytest.mark.skipif(not HAS_DISPLAYS, reason="needs an unlocked window-server session reporting a display")
@pytest.mark.skipif(not HAS_SCREEN, reason="needs the Screen Recording TCC grant")
def test_screenshot_live_dimensions_match_display() -> None:
    shot = capture.screenshot()
    image = Image.open(io.BytesIO(shot.png))
    assert image.size == (shot.display.width, shot.display.height)


@pytest.mark.skipif(sys.platform != "darwin", reason="patches the Quartz display list; macOS only")
def test_displays_reports_a_locked_screen_as_a_structured_error(monkeypatch) -> None:
    from a11y_computer_use.schema import ComputerUseError, ErrorCode
    monkeypatch.setattr(capture.Quartz, "CGGetActiveDisplayList", lambda n, a, b: (0, [], 0))
    with pytest.raises(ComputerUseError) as info:
        capture.displays()
    assert info.value.code is ErrorCode.UNSUPPORTED and "locked or asleep" in str(info.value)
