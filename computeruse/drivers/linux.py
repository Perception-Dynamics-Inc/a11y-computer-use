"""Linux backend — AT-SPI2 (observe) / AT-SPI XTEST event generation (act) /
PIL X11 grab (capture).

The third `Driver` behind the shared, platform-free core. Same contract as
macOS and Windows: `_atspi` maps AT-SPI role names onto the SAME AX role
vocabulary the pruning engine keys off, so a Linux tree prunes/indexes through
the identical `observe.build_snapshot` path. The a11y-first payoff — activating
an element through the API without moving the pointer — is `AtspiAction.do_action`
(the analog of macOS `AXPress` / Windows UIA `Invoke`); the coordinate/vision
fallback synthesizes input via AT-SPI's XTEST-backed event generation.

This is the driver that turns Grok's accessibility-OFF Linux desktop into an
accessibility-FIRST one (see docs/linux-port.md). Import-safe on every OS: the
gi/Atspi/Xlib imports are lazy, inside the methods.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from computeruse.schema import (
    Bounds,
    ComputerUseError,
    Element,
    ErrorCode,
    MouseButton,
    Point,
    Scope,
    ScrollUnit,
    Snapshot,
    Target,
    WaitCondition,
)


def _point_of(target: Target) -> tuple[int, int]:
    """Screen (x, y) for a coordinate action: a Point directly, else an
    Element's center. AT-SPI SCREEN coords == our physical-pixel space (scale 1)."""
    if isinstance(target, Point):
        return int(target.x), int(target.y)
    return int(target.bounds.center.x), int(target.bounds.center.y)


_BUTTON_NAME = {MouseButton.LEFT: "left", MouseButton.RIGHT: "right", MouseButton.MIDDLE: "middle"}


class LinuxDriver:
    """The `Driver` protocol, backed by AT-SPI2 / XTEST / X11."""

    name = "linux"

    def __init__(self) -> None:
        # The last editable element focused via press_element — type_text enters
        # text into it through AT-SPI EditableText (deterministic; see type_text).
        self._focused_editable = None

    # -- permissions --------------------------------------------------------
    def ensure_trusted(self) -> None:
        """Linux has no per-app TCC grant; the requirement is that the AT-SPI2
        registry is reachable (accessibility bus running). Raise a structured,
        actionable error when it is not — the Grok-desktop default (a11y OFF)."""
        try:
            from computeruse.drivers import _atspi

            desktop = _atspi._safe(lambda: _atspi._atspi().get_desktop(0))
        except ImportError as exc:
            raise ComputerUseError(
                ErrorCode.PERMISSION_DENIED_ACCESSIBILITY,
                "AT-SPI2 Python bindings are missing",
                detail={"hint": "pip install computeruse[linux]; apt install "
                        "gir1.2-atspi-2.0 at-spi2-core", "error": str(exc)},
            ) from exc
        if desktop is None:
            raise ComputerUseError(
                ErrorCode.PERMISSION_DENIED_ACCESSIBILITY,
                "the AT-SPI2 accessibility bus is not reachable",
                detail={"hint": "enable accessibility: apt install at-spi2-core; run under a "
                        "session bus; `gsettings set org.gnome.desktop.interface "
                        "toolkit-accessibility true`; for Chromium add "
                        "--force-renderer-accessibility. See docs/linux-port.md."},
            )

    # -- observe (AT-SPI2) --------------------------------------------------
    def snapshot(self, scope: Scope, app: str) -> Snapshot:
        from computeruse import observe
        from computeruse.drivers import _atspi

        root = _atspi.find_root(app, scope)  # None -> empty snapshot
        pid = _atspi.pid_of(root) if root is not None else None
        return observe.build_snapshot(
            root, _atspi.ATSPIAccessor(), scope=scope, app=app, pid=pid,
            geometry=_atspi.primary_geometry(),
        )

    def resolve_ref(self, snap: Snapshot, ref: str, *, live: Snapshot | None = None) -> Element:
        """Re-resolve a snapshot-scoped ref against a fresh live tree via the
        SHARED anchor matcher (`observe._match_anchor`) — the same re-resolution
        semantics as macOS, without macOS's pyobjc `observe.snapshot`."""
        from computeruse import observe

        anchor = snap.element(ref)
        if live is None:
            live = self.snapshot(snap.scope, snap.app)
        match, reason = observe._match_anchor(anchor, live)
        if match is None:
            raise ComputerUseError(
                ErrorCode.STALE_REF,
                f"{ref} ({anchor.role} {anchor.title!r}) no longer resolves; re-observe",
                detail={"ref": ref, "snapshot_id": snap.snapshot_id,
                        "live_snapshot_id": live.snapshot_id, "reason": reason},
            )
        return match

    def press_element(self, element: Element) -> bool:
        from computeruse import observe
        from computeruse.drivers import _atspi

        if element.secure:
            return False
        handle = observe.ax_handle_for(element.snapshot_id, element.ref)
        if handle is None:
            return False
        if element.editable:
            # Remember it so type_text can enter text via EditableText, and focus
            # it (best-effort — grab_focus is cursor-free but headless X may not
            # grant real widget focus; EditableText does not need it).
            self._focused_editable = handle
            focused = _atspi.grab_focus(handle)
            return focused or _atspi.do_press(handle) or True
        return _atspi.do_press(handle)

    def scroll_into_view(self, element: Element) -> bool:
        from computeruse import observe
        from computeruse.drivers import _atspi

        handle = observe.ax_handle_for(element.snapshot_id, element.ref)
        if handle is None:
            return False
        return _atspi.scroll_to(handle)

    # -- act (AT-SPI XTEST event generation) --------------------------------
    def click(self, target: Target, *, button: MouseButton = MouseButton.LEFT, count: int = 1,
              modifiers: tuple[str, ...] = (), pre_check: Callable | None = None,
              dry_run: bool = False) -> object:
        if dry_run:
            return None
        from computeruse.drivers import _linux_input

        x, y = _point_of(target)
        with _linux_input.held(modifiers):
            _linux_input.click(x, y, button=_BUTTON_NAME.get(button, "left"), count=count)
        return None

    def drag(self, start: Target, end: Target, *, button: MouseButton = MouseButton.LEFT,
             pre_check: Callable | None = None, dry_run: bool = False) -> object:
        if dry_run:
            return None
        from computeruse.drivers import _linux_input

        x1, y1 = _point_of(start)
        x2, y2 = _point_of(end)
        _linux_input.drag(x1, y1, x2, y2, button=_BUTTON_NAME.get(button, "left"))
        return None

    def scroll(self, target: Target, *, dx: int = 0, dy: int = 0,
               unit: ScrollUnit = ScrollUnit.LINES, pre_check: Callable | None = None,
               dry_run: bool = False) -> object:
        if dry_run:
            return None
        from computeruse.drivers import _linux_input

        x, y = _point_of(target)
        _linux_input.scroll(x, y, dx=dx, dy=dy)
        return None

    def type_text(self, text: str, *, pre_check: Callable | None = None,
                  dry_run: bool = False) -> object:
        """Enter ``text`` into the focused editable.

        Primary path: AT-SPI EditableText on the element last focused via
        press_element — deterministic and needs no X/widget focus (which headless
        AT-SPI cannot reliably grant). Falls back to synthetic XTEST keystrokes
        when no editable was focused through the driver (e.g. the vision path)."""
        if dry_run or not text:
            return None
        from computeruse.drivers import _atspi

        if self._focused_editable is not None and _atspi.insert_text(self._focused_editable, text):
            return None
        from computeruse.drivers import _linux_input

        _linux_input.type_string(text)
        return None

    def key_chord(self, chord: str, *, pre_check: Callable | None = None,
                  dry_run: bool = False) -> object:
        from computeruse.drivers import _linux_input

        # Validate the chord even on dry_run so a bad chord fails fast.
        if dry_run:
            _linux_input.validate_chord(chord)
            return None
        _linux_input.press_chord(chord)
        return None

    def wait_for(self, target: Element, *, condition: WaitCondition, timeout_s: float,
                 checker: Callable | None = None) -> Element:
        """Poll ``checker`` until ``condition`` holds or ``timeout_s`` elapses.

        The Runtime supplies ``checker`` (built from the originating snapshot and
        re-resolving through this driver), so the poll loop is identical across
        platforms — the platform-specific re-resolution lives in `resolve_ref`.
        """
        if checker is None:
            raise ValueError("LinuxDriver.wait_for needs a checker; the Runtime supplies one")
        deadline = time.monotonic() + timeout_s
        while True:
            result = checker(target, condition)
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                raise ComputerUseError(
                    ErrorCode.TIMEOUT,
                    f"{target.ref} did not reach {condition.value} within {timeout_s}s",
                    detail={"ref": target.ref, "condition": condition.value, "timeout_s": timeout_s},
                )
            time.sleep(0.1)

    # -- capture (PIL X11 grab) ---------------------------------------------
    def screenshot(self, display_id: int | None = None) -> object:
        from computeruse import capture
        from computeruse.drivers import _atspi

        png = _grab_png()
        display = _atspi.primary_geometry()[0].display
        return capture.Screenshot(png=png, display=display)

    def zoom_region(self, region: Bounds) -> bytes:
        import io

        from PIL import Image

        full = Image.open(io.BytesIO(_grab_png()))
        crop = full.crop((region.x, region.y, region.x + region.width, region.y + region.height))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        return buf.getvalue()

    # -- system / windowing -------------------------------------------------
    def frontmost_app(self) -> tuple[str | None, int | None]:
        from computeruse.drivers import _linux_system

        app_id = _linux_system.frontmost_app_id() or None
        return app_id, None

    def app_at_point(self, point: Point) -> str | None:
        from computeruse.drivers import _linux_system

        return _linux_system.app_at_point_id(point.x, point.y)

    def running_apps(self) -> list[dict]:
        from computeruse.drivers import _linux_system

        return _linux_system.running_apps()

    def launch_app(self, identifier: str) -> None:
        from computeruse.drivers import _linux_system

        _linux_system.launch_app(identifier)

    def activate_app(self, identifier: str) -> str:
        from computeruse.drivers import _linux_system

        return _linux_system.activate_app(identifier)

    def windows(self) -> list[dict]:
        from computeruse.drivers import _linux_system

        return _linux_system.windows()

    def read_clipboard(self) -> str | None:
        from computeruse.drivers import _linux_system

        return _linux_system.read_clipboard()

    def write_clipboard(self, text: str) -> None:
        from computeruse.drivers import _linux_system

        _linux_system.write_clipboard(text)


def _grab_png() -> bytes:
    """A full-screen PNG via PIL's X11 grab. Raises a structured screen-permission
    error when no X capture path is available (headless without an X server)."""
    import io
    import os

    try:
        from PIL import ImageGrab

        img = ImageGrab.grab(xdisplay=os.environ.get("DISPLAY"))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        raise ComputerUseError(
            ErrorCode.PERMISSION_DENIED_SCREEN,
            "screen capture failed on Linux",
            detail={"hint": "PIL X11 grab needs a reachable $DISPLAY; on headless hosts "
                    "run under Xvfb. The a11y-first path (snapshot/press) needs no capture.",
                    "error": str(exc)},
        ) from exc
