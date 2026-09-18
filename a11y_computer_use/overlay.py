"""Visual presence overlays — make a11y-computer-use *visible* without hijacking the
user's pointer.

Because ref actions activate elements through the AX API (see `observe.press_element`),
the user's real cursor never moves. That's great for staying out of the way,
but it also means the agent is invisible. This module adds two opt-in, purely
cosmetic overlays so a human can see, at a glance, that a11y-computer-use is working
and where:

* **screen-edge glow** — a click-through, translucent border around the active
  display while a11y-computer-use holds a session/action, like a screen-recording rim.
* **agent cursor** — a11y-computer-use's own arrow marker (distinct shape + color)
  that appears at the point it is acting on, so it reads as a second cursor
  rather than a stolen one.

Both are borderless, `ignoresMouseEvents` (click-through) `NSWindow`s at the
screen-saver level, so they float above every app and never intercept input.
They require a main-thread run loop to render; the server integration and a
standalone `demo()` (used to eyeball them) both pump one.

Everything here is best-effort cosmetic: any failure to build a window is
swallowed so the overlay can never break an actual action.
"""

from __future__ import annotations

import os

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSCompositingOperationSourceOver,
    NSMakePoint,
    NSMakeRect,
    NSScreen,
    NSShadow,
    NSView,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
)
from Foundation import NSDate, NSInsetRect, NSRunLoop

#: a11y-computer-use's signature colour (electric blue, ~#2A8CFF) as RGBA 0..1.
#: Override at runtime via A11Y_COMPUTER_USE_OVERLAY_RGBA="r,g,b" (0..1 or 0..255).
BRAND_RGBA: tuple[float, float, float, float] = (0.16, 0.55, 1.0, 1.0)

#: Borderless style mask (NSWindowStyleMaskBorderless == 0).
_BORDERLESS = 0
#: Window level above normal app windows and the menu bar. The screen-saver
#: level constant isn't reliably bridged, so use its known numeric value.
_OVERLAY_LEVEL = 1000

_BORDER_INSET = 3.0  #: gap from the physical screen edge, points
_BORDER_WIDTH = 6.0  #: stroke thickness, points
_BORDER_RADIUS = 14.0  #: corner radius, points
_CURSOR_BOX = 24.0  #: agent-cursor window size, points (smaller = sharper)


def _resolve_rgba() -> tuple[float, float, float, float]:
    """`BRAND_RGBA`, unless A11Y_COMPUTER_USE_OVERLAY_RGBA overrides it as "r,g,b"
    (or "r,g,b,a"), accepting either 0..1 floats or 0..255 ints."""
    raw = os.environ.get("A11Y_COMPUTER_USE_OVERLAY_RGBA", "").strip()
    if not raw:
        return BRAND_RGBA
    try:
        parts = [float(p) for p in raw.split(",")]
    except ValueError:
        return BRAND_RGBA
    if len(parts) not in (3, 4):
        return BRAND_RGBA
    if max(parts[:3]) > 1.0:  # 0..255 form
        parts = [parts[0] / 255, parts[1] / 255, parts[2] / 255, *parts[3:]]
    r, g, b = parts[0], parts[1], parts[2]
    a = parts[3] if len(parts) == 4 else 1.0
    return (r, g, b, a)


def _nscolor(rgba: tuple[float, float, float, float]) -> NSColor:
    r, g, b, a = rgba
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)


class _BorderView(NSView):
    """Draws a glowing rounded-rect stroke just inside the window bounds."""

    def initWithFrame_color_(self, frame, color):
        self = objc.super(_BorderView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._color = color
        return self

    def isOpaque(self) -> bool:  # noqa: N802 (Cocoa selector)
        return False

    def drawRect_(self, _dirty):  # noqa: N802
        inset = _BORDER_INSET + _BORDER_WIDTH / 2.0
        rect = NSInsetRect(self.bounds(), inset, inset)
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rect, _BORDER_RADIUS, _BORDER_RADIUS
        )
        path.setLineWidth_(_BORDER_WIDTH)
        glow = NSShadow.alloc().init()
        glow.setShadowColor_(self._color.colorWithAlphaComponent_(0.9))
        glow.setShadowBlurRadius_(22.0)
        glow.setShadowOffset_(NSMakePoint(0.0, 0.0))
        glow.set()
        self._color.setStroke()
        path.stroke()  # stroked twice so the glow reads on light desktops
        path.stroke()


class _CursorView(NSView):
    """Draws a11y-computer-use's arrow pointer (distinct from the OS cursor)."""

    def initWithFrame_color_(self, frame, color):
        self = objc.super(_CursorView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._color = color
        return self

    def isFlipped(self) -> bool:  # noqa: N802 (top-left origin, like a cursor)
        return True

    def isOpaque(self) -> bool:  # noqa: N802
        return False

    def drawRect_(self, _dirty):  # noqa: N802
        # A classic arrow pointer with the tip at the very top-left of the box,
        # scaled up ~1.5x for presence. Rendered as a crisp WHITE arrow behind a
        # coral glow halo (matching the reference look), with a thin coloured
        # edge so it stays legible on white app backgrounds.
        base = [(2, 1), (2, 22), (7, 17), (11, 25), (14, 24), (10, 16), (17, 16)]
        s = 1.05  # small + sharp
        arrow = NSBezierPath.bezierPath()
        arrow.moveToPoint_(NSMakePoint(base[0][0] * s, base[0][1] * s))
        for x, y in base[1:]:
            arrow.lineToPoint_(NSMakePoint(x * s, y * s))
        arrow.closePath()
        # 1) tight coloured glow halo (small blur = crisp, not fuzzy)
        glow = NSShadow.alloc().init()
        glow.setShadowColor_(self._color.colorWithAlphaComponent_(0.95))
        glow.setShadowBlurRadius_(6.0)
        glow.setShadowOffset_(NSMakePoint(0.0, 0.0))
        glow.set()
        # 2) white body (the shadow is cast from this fill)
        NSColor.whiteColor().setFill()
        arrow.fill()
        # 3) crisp coloured edge for definition on light UIs
        self._color.setStroke()
        arrow.setLineWidth_(1.0)
        arrow.stroke()


def _make_window(frame, view) -> NSWindow:
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        frame, _BORDERLESS, NSBackingStoreBuffered, False
    )
    win.setOpaque_(False)
    win.setBackgroundColor_(NSColor.clearColor())
    win.setLevel_(_OVERLAY_LEVEL)
    win.setIgnoresMouseEvents_(True)  # click-through: never intercept input
    win.setHasShadow_(False)
    win.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorStationary
        | NSWindowCollectionBehaviorFullScreenAuxiliary
    )
    win.setContentView_(view)
    win.orderFrontRegardless()
    return win


class Overlay:
    """Owns the border + agent-cursor windows for one display.

    Cosmetic only: construction never raises into a caller — a failure just
    means no overlay. Call `close()` when done. A process that shows these must
    keep a main-thread run loop spinning (the server does; `demo()` pumps one).
    """

    def __init__(self, rgba: tuple[float, float, float, float] | None = None) -> None:
        self._color = _nscolor(rgba or _resolve_rgba())
        self._border: NSWindow | None = None
        self._cursor: NSWindow | None = None
        self._cursor_pos: tuple[float, float] | None = None
        # Accessory policy: show floating windows with no Dock icon and without
        # stealing focus from the app a11y-computer-use is driving.
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    def show_border(self, screen: object | None = None) -> None:
        if self._border is not None:
            return
        try:
            scr = screen or NSScreen.mainScreen()
            frame = scr.frame()
            view = _BorderView.alloc().initWithFrame_color_(
                NSMakeRect(0, 0, frame.size.width, frame.size.height), self._color
            )
            self._border = _make_window(frame, view)
        except Exception:  # cosmetic only
            self._border = None

    def move_cursor(self, screen_x: float, screen_y: float) -> None:
        """Instantly place the agent cursor at a point in Cocoa screen coords
        (bottom-left origin, points); the arrow tip lands on the point.

        Only orders the window front on first creation — re-ordering every
        frame is what made gliding stutter."""
        try:
            if self._cursor is None:
                view = _CursorView.alloc().initWithFrame_color_(
                    NSMakeRect(0, 0, _CURSOR_BOX, _CURSOR_BOX), self._color
                )
                self._cursor = _make_window(NSMakeRect(0, 0, _CURSOR_BOX, _CURSOR_BOX), view)
            self._cursor.setFrameOrigin_(NSMakePoint(screen_x, screen_y - _CURSOR_BOX))
            self._cursor_pos = (screen_x, screen_y)
        except Exception:
            self._cursor = None

    def glide_to(self, screen_x: float, screen_y: float, duration: float = 0.5,
                 fps: int = 60) -> None:
        """Smoothly animate the cursor from its current spot to the target with
        ease-in-out, ~``fps`` updates/sec. Requires a pumped run loop (demo /
        the hero scripts have one). Teleports if there is no current position."""
        if self._cursor is None or self._cursor_pos is None:
            self.move_cursor(screen_x, screen_y)
            return
        x0, y0 = self._cursor_pos
        frames = max(1, int(duration * fps))
        for i in range(1, frames + 1):
            t = i / frames
            te = t * t * (3.0 - 2.0 * t)  # smoothstep ease-in-out
            self.move_cursor(x0 + (screen_x - x0) * te, y0 + (screen_y - y0) * te)
            _pump(1.0 / fps)

    def close(self) -> None:
        for win in (self._border, self._cursor):
            if win is not None:
                try:
                    win.orderOut_(None)
                except Exception:
                    pass
        self._border = self._cursor = None


def _pump(seconds: float) -> None:
    NSRunLoop.currentRunLoop().runUntilDate_(
        NSDate.dateWithTimeIntervalSinceNow_(seconds)
    )


def demo(seconds: float = 8.0) -> None:
    """Show the border glow and sweep the agent cursor, so a human can eyeball
    it. Blocks (pumping the run loop) for ``seconds`` then tears down."""
    scr = NSScreen.mainScreen().frame()
    ov = Overlay()
    ov.show_border()
    print(f"Overlay shown on {int(scr.size.width)}x{int(scr.size.height)} pt screen "
          f"for {seconds:.0f}s — watch for the edge glow + arrow cursor "
          f"(a11y-computer-use's signature colour), gliding smoothly between points.")
    waypoints = [(0.20, 0.78), (0.80, 0.58), (0.32, 0.30), (0.72, 0.82), (0.50, 0.50)]
    ov.move_cursor(scr.size.width * waypoints[0][0], scr.size.height * waypoints[0][1])
    _pump(0.4)
    per = max(0.5, (seconds - 0.4) / max(1, len(waypoints) - 1))
    for fx, fy in waypoints[1:]:
        ov.glide_to(scr.size.width * fx, scr.size.height * fy, duration=min(0.7, per * 0.65))
        _pump(max(0.15, per * 0.35))  # brief settle at each target, like a real action
    ov.close()
    _pump(0.3)
    print("Overlay torn down.")


if __name__ == "__main__":
    demo()
