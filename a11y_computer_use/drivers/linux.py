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

from a11y_computer_use.schema import (
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


def _on_wayland() -> bool:
    """True on a native Wayland session (WAYLAND_DISPLAY set, no X): XTEST
    synthetic input can't reach Wayland-native apps."""
    import os

    return bool(os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"))


def _wayland_input_error(op: str) -> ComputerUseError:
    return ComputerUseError(
        ErrorCode.UNSUPPORTED,
        f"{op}: raw coordinate/key injection (XTEST) is unavailable on native Wayland",
        detail={"hint": "Use ref-based actions — click(ref) / press and set_value work on "
                "Wayland via AT-SPI with no coordinates. Use set_value for text when the "
                "frontmost app cannot be verified. Raw "
                "coordinate/key input on Wayland needs libei/RemoteDesktop portal (planned), "
                "or run under X/XWayland with $DISPLAY set."},
    )


def _secure_focus_error(api: str) -> ComputerUseError:
    return ComputerUseError(
        ErrorCode.SECURE_FIELD,
        "the focused element is a password field; secrets are typed by the human",
        detail={"api": api},
    )


class LinuxDriver:
    """The `Driver` protocol, backed by AT-SPI2 / XTEST / X11."""

    name = "linux"

    def __init__(self) -> None:
        # The last editable element focused via press_element — type_text enters
        # text into it through AT-SPI EditableText (deterministic; see type_text).
        self._focused_editable = None

    def _run(self, fn):
        """Run an AT-SPI (libatspi) op inline, or — when the event cache is opted
        in (A11Y_COMPUTER_USE_ATSPI_EVENTS=1) — on the shared a11y thread so libatspi's
        read cache is trusted (~1.8x faster snapshots) and stays single-threaded.
        Only libatspi ops go through here; XTEST input uses a separate X
        connection and is unaffected."""
        from a11y_computer_use.drivers import _atspi_events

        return _atspi_events.submit(fn) if _atspi_events.enabled() else fn()

    # -- permissions --------------------------------------------------------
    def ensure_trusted(self) -> None:
        """Linux has no per-app TCC grant; the requirement is that the AT-SPI2
        registry is reachable (accessibility bus running). Raise a structured,
        actionable error when it is not — the Grok-desktop default (a11y OFF)."""
        try:
            from a11y_computer_use.drivers import _atspi

            # Force the whole desktop's Chromium/Electron apps to expose their
            # a11y tree (org.a11y.Status flip) before we probe — turns a
            # Grok-style a11y-OFF desktop into an a11y-first one, no relaunch.
            _atspi.enable_a11y_status()  # Gio/session bus — not libatspi, runs inline
            desktop = self._run(lambda: _atspi._safe(lambda: _atspi._atspi().get_desktop(0)))
        except ImportError as exc:
            raise ComputerUseError(
                ErrorCode.PERMISSION_DENIED_ACCESSIBILITY,
                "AT-SPI2 Python bindings are missing",
                detail={"hint": "pip install a11y_computer_use[linux]; apt install "
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
        from a11y_computer_use import observe
        from a11y_computer_use.drivers import _atspi

        def _do() -> Snapshot:
            root = _atspi.find_root(app, scope)  # None -> empty snapshot
            pid = _atspi.pid_of(root) if root is not None else None
            return observe.build_snapshot(
                root, _atspi.ATSPIAccessor(), scope=scope, app=app, pid=pid,
                geometry=_atspi.primary_geometry(),
            )

        return self._run(_do)

    def resolve_ref(self, snap: Snapshot, ref: str, *, live: Snapshot | None = None) -> Element:
        """Re-resolve a snapshot-scoped ref against a fresh live tree via the
        SHARED anchor matcher (`observe._match_anchor`) — the same re-resolution
        semantics as macOS, without macOS's pyobjc `observe.snapshot`."""
        from a11y_computer_use import observe

        if live is None:
            live = self.snapshot(snap.scope, snap.app)
        return observe.rematch_ref(snap, ref, live)

    def press_element(self, element: Element) -> bool:
        from a11y_computer_use import observe
        from a11y_computer_use.drivers import _atspi

        self._focused_editable = None
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
            return self._run(lambda: _atspi.grab_focus(handle) or _atspi.do_press(handle) or True)
        return self._run(lambda: _atspi.do_press(handle))

    def scroll_into_view(self, element: Element) -> bool:
        from a11y_computer_use import observe
        from a11y_computer_use.drivers import _atspi

        handle = observe.ax_handle_for(element.snapshot_id, element.ref)
        if handle is None:
            return False
        return self._run(lambda: _atspi.scroll_to(handle))

    def set_value(self, element: Element, value: str) -> bool:
        from a11y_computer_use import observe
        from a11y_computer_use.drivers import _atspi

        self._focused_editable = None
        if element.secure:
            return False
        handle = observe.ax_handle_for(element.snapshot_id, element.ref)
        if handle is None:
            return False
        # AT-SPI EditableText.set_text_contents (marshaled onto the a11y thread)
        success = self._run(lambda: _atspi.set_text(handle, value))
        if success:
            self._focused_editable = handle
        return success

    # -- act (AT-SPI XTEST event generation) --------------------------------
    def click(self, target: Target, *, button: MouseButton = MouseButton.LEFT, count: int = 1,
              modifiers: tuple[str, ...] = (), pre_check: Callable | None = None,
              dry_run: bool = False) -> object:
        if dry_run:
            return None
        if _on_wayland():
            raise _wayland_input_error("click")
        from a11y_computer_use.drivers import _linux_input

        self._focused_editable = None
        x, y = _point_of(target)
        with _linux_input.held(modifiers):
            _linux_input.click(x, y, button=_BUTTON_NAME.get(button, "left"), count=count)
        return None

    def drag(self, start: Target, end: Target, *, button: MouseButton = MouseButton.LEFT,
             pre_check: Callable | None = None, dry_run: bool = False) -> object:
        if dry_run:
            return None
        if _on_wayland():
            raise _wayland_input_error("drag")
        from a11y_computer_use.drivers import _linux_input

        self._focused_editable = None
        x1, y1 = _point_of(start)
        x2, y2 = _point_of(end)
        _linux_input.drag(x1, y1, x2, y2, button=_BUTTON_NAME.get(button, "left"))
        return None

    def scroll(self, target: Target, *, dx: int = 0, dy: int = 0,
               unit: ScrollUnit = ScrollUnit.LINES, pre_check: Callable | None = None,
               dry_run: bool = False) -> object:
        if dry_run:
            return None
        if _on_wayland():
            raise _wayland_input_error("scroll")
        from a11y_computer_use.drivers import _linux_input

        self._focused_editable = None
        x, y = _point_of(target)
        _linux_input.scroll(x, y, dx=dx, dy=dy)
        return None

    def type_text(self, text: str, *, pre_check: Callable | None = None,
                  dry_run: bool = False) -> object:
        """Enter ``text`` into the focused editable.

        Primary path: AT-SPI EditableText on the element last focused via
        press_element, provided its owner is the current frontmost app. This
        needs no widget focus, but does require a detectable application owner.
        Falls back to synthetic XTEST keystrokes
        when no editable was focused through the driver (e.g. the vision path)."""
        if dry_run or not text:
            return None
        from a11y_computer_use.drivers import _atspi

        handle = self._focused_editable
        if handle is not None and self._run(lambda: _atspi.is_secure(handle)):
            raise _secure_focus_error("AT-SPI role 'password text' on the focused editable")
        if handle is not None:
            from a11y_computer_use.drivers import _linux_system

            app_id, _pid = self.frontmost_app()
            owner_pid = self._run(lambda: _atspi.pid_of(handle))
            owner = _linux_system._comm_for_pid(owner_pid) if owner_pid is not None else None
            if not owner or not app_id or owner != app_id:
                self._focused_editable = None
                raise ComputerUseError(
                    ErrorCode.FOCUS_CHANGED,
                    "the remembered editable does not belong to a verified frontmost app; "
                    "focus the intended field again or use set_value with its ref",
                    detail={"editable_app": owner, "frontmost_app": app_id},
                )
        if handle is not None and self._run(lambda: _atspi.insert_text(handle, text)):
            return None
        if _on_wayland():  # a11y path unavailable and XTEST can't reach Wayland apps
            raise _wayland_input_error("type_text (no focused editable for the a11y path)")
        # The XTEST path types into whatever holds keyboard focus, so probe the
        # focused node of the frontmost app first (the Linux analog of macOS's
        # AXFocusedUIElement check): a password field there refuses the typing.
        app_id, _pid = self.frontmost_app()
        verdict = self._run(lambda: _atspi.focused_secure(app_id)) if app_id else False
        if verdict is True:
            raise _secure_focus_error("AT-SPI STATE_FOCUSED on a 'password text' node")
        if verdict is None:
            # The probe ran out of its node bound without meeting the focused
            # node: typing blind here could land the text in a password field.
            raise ComputerUseError(
                ErrorCode.SECURE_FIELD,
                "cannot verify the focused element (accessibility tree larger than the "
                "focus probe bound); focus a field through a ref (click/set_value) before typing",
                detail={"api": "AT-SPI focus walk exhausted", "app": app_id},
            )
        from a11y_computer_use.drivers import _linux_input

        _linux_input.type_string(text)  # XTEST fallback — separate X connection, not marshaled
        return None

    def key_chord(self, chord: str, *, pre_check: Callable | None = None,
                  dry_run: bool = False) -> object:
        from a11y_computer_use.drivers import _linux_input

        # Validate the chord even on dry_run so a bad chord fails fast.
        if dry_run:
            _linux_input.validate_chord(chord)
            return None
        if _on_wayland():
            raise _wayland_input_error("key_chord")
        self._focused_editable = None
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
        from a11y_computer_use.drivers import _atspi_events

        events_on = _atspi_events.enabled()
        deadline = time.monotonic() + timeout_s
        while True:
            result = checker(target, condition)
            if result is not None:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ComputerUseError(
                    ErrorCode.TIMEOUT,
                    f"{target.ref} did not reach {condition.value} within {timeout_s}s",
                    detail={"ref": target.ref, "condition": condition.value, "timeout_s": timeout_s},
                )
            if events_on:
                # wake the instant the UI changes (any standing a11y event), with
                # a short safety tick — far lower latency than fixed polling.
                _atspi_events.wait_for_event(_atspi_events.current_seq(), timeout=min(0.4, remaining))
            else:
                time.sleep(min(0.1, remaining))

    # -- capture (grim on Wayland, PIL X11 grab otherwise) ------------------
    def screenshot(self, display_id: int | None = None) -> object:
        import io

        from PIL import Image

        from a11y_computer_use import capture
        from a11y_computer_use.drivers import _atspi
        from a11y_computer_use.schema import Display

        png = _grab_png()
        # Derive the real dimensions from the frame itself — on Wayland the
        # Xlib-based primary_geometry() is unavailable, and even on X the frame
        # is the source of truth. Keep the display's scale/id from geometry.
        base = _atspi.primary_geometry()[0].display
        w, h = Image.open(io.BytesIO(png)).size
        display = base if (base.width, base.height) == (w, h) else Display(
            display_id=base.display_id, width=w, height=h, scale=base.scale, is_main=True)
        return capture.Screenshot(png=png, display=display)

    def main_display_id(self) -> int:
        # One display, id 0: the single X screen `_atspi.primary_geometry()` reports.
        return 0

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
        from a11y_computer_use.drivers import _linux_system

        app_id = _linux_system.frontmost_app_id() or None
        return app_id, None

    def app_at_point(self, point: Point) -> str | None:
        from a11y_computer_use.drivers import _linux_system

        return _linux_system.app_at_point_id(point.x, point.y)

    def running_apps(self) -> list[dict]:
        from a11y_computer_use.drivers import _linux_system

        return _linux_system.running_apps()

    def launch_app(self, identifier: str) -> None:
        from a11y_computer_use.drivers import _linux_system

        self._focused_editable = None
        _linux_system.launch_app(identifier)

    def activate_app(self, identifier: str) -> str:
        from a11y_computer_use.drivers import _linux_system

        self._focused_editable = None
        return _linux_system.activate_app(identifier)

    def windows(self) -> list[dict]:
        from a11y_computer_use.drivers import _linux_system

        return _linux_system.windows()

    def window_owner(self, window_id: int) -> str:
        """comm name of the process owning X window ``window_id`` (the grant key)."""
        if _on_wayland():
            raise _wayland_window_error("window_owner")
        from a11y_computer_use.drivers import _linux_system

        owner = _linux_system.window_owner(window_id)
        if owner is None:
            raise _no_such_window(window_id)
        return owner

    def raise_window(self, window_id: int) -> None:
        """EWMH ``_NET_ACTIVE_WINDOW`` client message for that window."""
        if _on_wayland():
            raise _wayland_window_error("raise_window")
        from a11y_computer_use.drivers import _linux_system

        self._focused_editable = None
        if not _linux_system.raise_window(window_id):
            raise _no_such_window(window_id)

    def read_clipboard(self) -> str | None:
        from a11y_computer_use.drivers import _linux_system

        return _linux_system.read_clipboard()

    def write_clipboard(self, text: str) -> None:
        from a11y_computer_use.drivers import _linux_system

        _linux_system.write_clipboard(text)


def _wayland_window_error(op: str) -> ComputerUseError:
    return ComputerUseError(
        ErrorCode.UNSUPPORTED,
        f"{op}: X window ids and EWMH are unavailable on native Wayland",
        detail={"hint": "use `app focus <name>` (AT-SPI/portal based) or run under XWayland."},
    )


def _no_such_window(window_id: int) -> ComputerUseError:
    return ComputerUseError(
        ErrorCode.APP_NOT_FOUND,
        f"no managed X window with id {window_id}",
        detail={"window_id": window_id},
    )


def _grab_wayland() -> bytes | None:
    """Full-screen PNG on Wayland via grim (wlroots ext-image-copy-capture);
    PIL's X11 grab cannot see a Wayland compositor. Returns None when grim is
    absent or fails (caller falls through to the X path). Exercised manually
    under headless sway (2026-08-29); no automated test."""
    import shutil
    import subprocess

    if not shutil.which("grim"):
        return None
    try:
        r = subprocess.run(["grim", "-"], capture_output=True, timeout=10)  # PNG to stdout
    except Exception:
        return None
    return r.stdout if r.returncode == 0 and r.stdout else None


def _grab_png() -> bytes:
    """A full-screen PNG. On Wayland uses grim; on X11/XWayland uses PIL's grab.
    Raises a structured screen-permission error when no capture path works."""
    import io
    import os

    # Native Wayland (WAYLAND_DISPLAY set, no X): PIL grab can't see the
    # compositor — go straight to grim.
    if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        png = _grab_wayland()
        if png is not None:
            return png

    try:
        from PIL import ImageGrab

        img = ImageGrab.grab(xdisplay=os.environ.get("DISPLAY"))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        wl = _grab_wayland()  # last resort: XWayland session where grim also works
        if wl is not None:
            return wl
        raise ComputerUseError(
            ErrorCode.PERMISSION_DENIED_SCREEN,
            "screen capture failed on Linux",
            detail={"hint": "X11: PIL grab needs a reachable $DISPLAY (run under Xvfb on "
                    "headless hosts). Wayland: install grim (wlroots) or a ScreenCast portal. "
                    "The a11y-first path (snapshot/press/type) needs no capture.",
                    "error": str(exc)},
        ) from exc
