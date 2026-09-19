"""Screen capture: screenshots, model-friendly downscaling, and full-res zooms.

Capture strategy (PLAN.md §6): try ``CGWindowListCreateImage`` first (fast,
in-process), fall back to ``/usr/sbin/screencapture -x`` when Quartz returns
nothing (the API is deprecated on recent macOS and may yield nil). Output is
always PNG bytes at *physical* pixel resolution plus `schema.Display`
metadata, because the Anthropic contract (PLAN.md §3) needs the physical
dimensions and backing scale to map model coordinates back after downscaling.

Permission discipline: every capture entry point preflights the Screen
Recording TCC grant via ``CGPreflightScreenCaptureAccess`` and raises a
structured `ErrorCode.PERMISSION_DENIED_SCREEN` when it is missing. The TCC
prompt is never triggered implicitly — only an explicit `request_permission()`
call invokes ``CGRequestScreenCaptureAccess``.
"""

from __future__ import annotations

import io
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from a11y_computer_use.schema import Bounds, ComputerUseError, Display, ErrorCode

# pyobjc backs only the macOS capture functions (displays/screenshot/zoom_region);
# the Screenshot/ScaledImage dataclasses and downscale() are pure PIL. Gate the
# imports so `import a11y_computer_use.capture` works on Linux/Windows too — the Linux
# driver constructs Screenshot directly and uses downscale/draw_marks, never the
# Quartz paths. (Same discipline as server.py's pyobjc gating.)
try:
    import Quartz
    from AppKit import NSScreen
    from CoreFoundation import CFDataCreateMutable
except ImportError:  # non-macOS
    Quartz = None  # type: ignore[assignment]
    NSScreen = None  # type: ignore[assignment]
    CFDataCreateMutable = None  # type: ignore[assignment]

#: Default long-edge budget for `downscale`. 1280 px keeps a screenshot well
#: under every provider cap (Sonnet 5 / Opus 4.8 reject > 2576 px long edge,
#: older models > 1568 px — PLAN.md §3) while staying legible.
DEFAULT_MAX_LONG_EDGE = 1280

_MAX_DISPLAYS = 16

_DOCTOR_HINT = (
    "Run `a11y_computer_use doctor` to see which host app needs the Screen Recording"
    " grant, then enable it under System Settings > Privacy & Security >"
    " Screen & System Audio Recording."
)


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Screenshot:
    """A captured PNG plus the display metadata needed to interpret it.

    Attributes:
        png: PNG-encoded pixels at native (physical) resolution.
        display: The captured display; ``display.width``/``display.height``
            are the expected image dimensions in physical pixels and
            ``display.scale`` is the backing scale factor (2.0 on Retina).
    """

    png: bytes
    display: Display


@dataclass(frozen=True, slots=True)
class ScaledImage:
    """A downscaled image plus the mapping back to source coordinates.

    A model that received this image emits coordinates in scaled-image space;
    `to_source` maps them back to the source (physical-pixel) space so clicks
    land where the model pointed.

    Attributes:
        png: PNG-encoded scaled pixels.
        width: Scaled image width in pixels.
        height: Scaled image height in pixels.
        source_width: Width of the image `downscale` was given.
        source_height: Height of the image `downscale` was given.
    """

    png: bytes
    width: int
    height: int
    source_width: int
    source_height: int

    @property
    def scale(self) -> float:
        """Scaled pixels per source pixel (1.0 when nothing was scaled)."""
        return self.width / self.source_width

    def to_source(self, x: int, y: int) -> tuple[int, int]:
        """Map a scaled-image point back to source pixels, clamped in-bounds."""
        sx = round(x * self.source_width / self.width)
        sy = round(y * self.source_height / self.height)
        return _clamp(sx, self.source_width), _clamp(sy, self.source_height)

    def from_source(self, x: int, y: int) -> tuple[int, int]:
        """Map a source-pixel point onto the scaled image, clamped in-bounds."""
        sx = round(x * self.width / self.source_width)
        sy = round(y * self.height / self.source_height)
        return _clamp(sx, self.width), _clamp(sy, self.height)


def _clamp(value: int, size: int) -> int:
    return min(max(value, 0), size - 1)


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


def _preflight_screen() -> bool:
    """True when this process holds the Screen Recording TCC grant."""
    return bool(Quartz.CGPreflightScreenCaptureAccess())


def _require_screen_permission() -> None:
    if not _preflight_screen():
        raise ComputerUseError(
            ErrorCode.PERMISSION_DENIED_SCREEN,
            "Screen Recording permission is not granted to this process",
            {"doctor_hint": _DOCTOR_HINT},
        )


def request_permission() -> bool:
    """Explicitly trigger the macOS Screen Recording TCC prompt.

    The only call site allowed to pop the system dialog (the quickstart owns
    the guided "TCC dance", PLAN.md §7); ordinary capture calls preflight and
    raise instead.

    Returns:
        True when the grant is present after the request.
    """
    return bool(Quartz.CGRequestScreenCaptureAccess())


# ---------------------------------------------------------------------------
# Display metadata
# ---------------------------------------------------------------------------


def displays() -> tuple[Display, ...]:
    """Metadata for all active displays, main display first (no TCC needed).

    Physical dimensions are points (``CGDisplayBounds``) times the backing
    scale factor (``NSScreen.backingScaleFactor``), per the `schema.Display`
    contract that every coordinate is display-qualified physical pixels.
    """
    err, ids, count = Quartz.CGGetActiveDisplayList(_MAX_DISPLAYS, None, None)
    if err != 0 or not ids or not count:
        # No active display means the session is locked or the display is
        # asleep: a structured error the agent loop can surface, not a crash
        # that kills a 30-minute mission.
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            "no active display: the screen is locked or asleep (CGGetActiveDisplayList "
            f"returned {count or 0} displays, error {err}); unlock the screen, or keep it "
            "awake with `caffeinate -dimsu` during long runs",
            detail={"active_displays": int(count or 0), "error": int(err)},
        )
    main_id = int(Quartz.CGMainDisplayID())
    found = []
    for display_id in (int(d) for d in list(ids)[:count]):
        bounds = Quartz.CGDisplayBounds(display_id)
        scale = _backing_scale(display_id)
        found.append(
            Display(
                display_id=display_id,
                width=round(bounds.size.width * scale),
                height=round(bounds.size.height * scale),
                scale=scale,
                is_main=display_id == main_id,
            )
        )
    found.sort(key=lambda d: not d.is_main)
    return tuple(found)


def _backing_scale(display_id: int) -> float:
    """Backing scale via NSScreen, display-mode arithmetic as fallback."""
    for screen in NSScreen.screens():
        if int(screen.deviceDescription()["NSScreenNumber"]) == display_id:
            return float(screen.backingScaleFactor())
    mode = Quartz.CGDisplayCopyDisplayMode(display_id)
    if mode is not None:
        points = Quartz.CGDisplayBounds(display_id).size.width
        if points:
            return Quartz.CGDisplayModeGetPixelWidth(mode) / points
    return 1.0


def _display(display_id: int | None) -> Display:
    """Resolve ``display_id`` (None = main) against the active display list.

    Raises:
        ValueError: when ``display_id`` matches no active display — a caller
            bug, not a model-reactable state, hence not a `ComputerUseError`.
    """
    active = displays()
    if display_id is None:
        return next((d for d in active if d.is_main), active[0])
    for display in active:
        if display.display_id == display_id:
            return display
    raise ValueError(
        f"no active display with id {display_id}; have {[d.display_id for d in active]}"
    )


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def screenshot(display_id: int | None = None) -> Screenshot:
    """Capture one display as a native-resolution PNG plus its metadata.

    Args:
        display_id: CGDirectDisplayID to capture; the main display when None.

    Returns:
        `Screenshot` with physical-pixel PNG bytes and the `Display` whose
        scale/dimensions let callers rescale model coordinates (PLAN.md §3).

    Raises:
        ComputerUseError: `ErrorCode.PERMISSION_DENIED_SCREEN` when the
            Screen Recording TCC grant is missing.
        ValueError: when ``display_id`` matches no active display.
    """
    _require_screen_permission()
    display = _display(display_id)
    return Screenshot(png=_capture_display_png(display), display=display)


def zoom_region(region: Bounds) -> bytes:
    """Capture a full-resolution PNG crop of ``region``.

    Solves tiny-text illegibility after model-side downscaling: the model
    asks to zoom a region and receives it at native physical resolution.
    Regions overhanging the display edge are clamped to it.

    Args:
        region: Display-qualified rect in physical pixels.

    Returns:
        PNG bytes of the clamped crop, native resolution.

    Raises:
        ComputerUseError: `ErrorCode.PERMISSION_DENIED_SCREEN` when the
            Screen Recording TCC grant is missing.
        ValueError: when ``region.display_id`` matches no active display or
            the region lies entirely outside it.
    """
    _require_screen_permission()
    display = _display(region.display_id)
    left = max(region.x, 0)
    top = max(region.y, 0)
    right = min(region.x + region.width, display.width)
    bottom = min(region.y + region.height, display.height)
    if right <= left or bottom <= top:
        raise ValueError(
            f"region {region} lies entirely outside display {display.display_id}"
            f" ({display.width}x{display.height} physical px)"
        )
    image = Image.open(io.BytesIO(_capture_display_png(display)))
    # Both capture paths should deliver physical pixels; guard against a
    # nominal-resolution capture by rescaling the crop box onto the actual
    # image dimensions instead of trusting them blindly.
    rx = image.width / display.width
    ry = image.height / display.height
    crop = image.crop((round(left * rx), round(top * ry), round(right * rx), round(bottom * ry)))
    buffer = io.BytesIO()
    crop.save(buffer, format="PNG")
    return buffer.getvalue()


def downscale(png: bytes, max_long_edge: int = DEFAULT_MAX_LONG_EDGE) -> ScaledImage:
    """Downscale a PNG so its long edge is at most ``max_long_edge``.

    Providers reject oversized screenshots and their coordinates refer to the
    image actually sent (PLAN.md §3), so callers send `ScaledImage.png` and
    map returned coordinates back with `ScaledImage.to_source`.

    Args:
        png: Source image bytes (any Pillow-decodable format).
        max_long_edge: Long-edge budget in pixels, >= 1.

    Returns:
        `ScaledImage`; when the source already fits, the original bytes pass
        through untouched with ``scale == 1.0``.

    Raises:
        ValueError: for ``max_long_edge < 1`` or undecodable image bytes.
    """
    if max_long_edge < 1:
        raise ValueError(f"max_long_edge must be >= 1, got {max_long_edge}")
    try:
        image = Image.open(io.BytesIO(png))
        image.load()
    except OSError as exc:
        raise ValueError(f"not a decodable image: {exc}") from exc
    width, height = image.size
    long_edge = max(width, height)
    if long_edge <= max_long_edge:
        return ScaledImage(
            png=png, width=width, height=height, source_width=width, source_height=height
        )
    factor = max_long_edge / long_edge
    new_width = max(1, round(width * factor))
    new_height = max(1, round(height * factor))
    scaled = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    scaled.save(buffer, format="PNG")
    return ScaledImage(
        png=buffer.getvalue(),
        width=new_width,
        height=new_height,
        source_width=width,
        source_height=height,
    )


# ---------------------------------------------------------------------------
# Capture backends
# ---------------------------------------------------------------------------


def _capture_display_png(display: Display) -> bytes:
    """Capture one display as PNG: Quartz first, ``screencapture`` fallback."""
    png = _capture_via_quartz(display)
    if png is None:
        png = _capture_via_screencapture(display)
    return png


def _capture_via_quartz(display: Display) -> bytes | None:
    """In-process capture via ``CGWindowListCreateImage``; None on failure.

    The API is deprecated on recent macOS and may return nil even with the
    grant present, hence the None-not-raise contract: the caller falls back
    to the ``screencapture`` binary.
    """
    image = Quartz.CGWindowListCreateImage(
        Quartz.CGDisplayBounds(display.display_id),
        Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID,
        Quartz.kCGWindowImageBestResolution,
    )
    if image is None:
        return None
    data = CFDataCreateMutable(None, 0)
    destination = Quartz.CGImageDestinationCreateWithData(data, "public.png", 1, None)
    if destination is None:
        return None
    Quartz.CGImageDestinationAddImage(destination, image, None)
    if not Quartz.CGImageDestinationFinalize(destination):
        return None
    return bytes(data)


def _capture_via_screencapture(display: Display) -> bytes:
    """Out-of-process fallback via ``/usr/sbin/screencapture -x`` (silent).

    ``screencapture -D`` takes a 1-based display ordinal, not a
    CGDirectDisplayID; the ordinal follows the active-display-list order.
    """
    err, ids, count = Quartz.CGGetActiveDisplayList(_MAX_DISPLAYS, None, None)
    if err != 0 or not ids:
        raise RuntimeError(f"CGGetActiveDisplayList failed (error {err})")
    ordinal = [int(d) for d in list(ids)[:count]].index(display.display_id) + 1
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "capture.png"
        result = subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-D", str(ordinal), str(path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not path.exists():
            reason = result.stderr.strip() or f"exit code {result.returncode}"
            raise RuntimeError(f"screencapture failed: {reason}")
        return path.read_bytes()
