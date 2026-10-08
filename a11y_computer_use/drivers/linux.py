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
from collections.abc import Callable, Sequence

from a11y_computer_use.schema import (
    Bounds,
    ComputerUseError,
    Element,
    ErrorCode,
    printable_chord,
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


def _screen_span() -> tuple[int, int]:
    """Primary screen size in pixels. 1280x800 when it cannot be read."""
    from a11y_computer_use.drivers import _atspi

    return _atspi._screen_size()


def _wheel_point(target: Target) -> tuple[int, int]:
    """Where a line-scroll wheel lands for an element that is not a Chromium list.

    The element's center when that point is on the screen, and also when
    the element itself is no larger than the screen. A document group is
    that size: the body-scroll find wheels its center. A content-height box
    whose center sits below the screen is wheeled at the center of the part
    on the screen. A Chromium list does not use this point; ``list_wheel_point``
    lands on the first painted row. A point target is unchanged.
    ``unit=pixels`` does not call this.
    """
    if isinstance(target, Point):
        return int(target.x), int(target.y)
    bounds = target.bounds
    cx, cy = int(bounds.center.x), int(bounds.center.y)
    width, height = _screen_span()
    if width <= 0 or height <= 0:
        return cx, cy
    if bounds.height <= height and bounds.width <= width:
        return cx, cy
    if 0 <= cx < width and 0 <= cy < height:
        return cx, cy
    left = max(int(bounds.x), 0)
    top = max(int(bounds.y), 0)
    right = min(int(bounds.x) + int(bounds.width), width)
    bottom = min(int(bounds.y) + int(bounds.height), height)
    if right <= left or bottom <= top:
        return cx, cy
    return (left + right) // 2, (top + bottom) // 2


def _clip_box_to_screen(box: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    """The part of ``box`` that lies on the screen.

    A content-height list extends below the screen. The pixel check photographs
    the visible part. A box already on the screen is returned unchanged. A box
    no larger than the screen is also returned unchanged: cropping a
    fixed-height overflow list to the reported screen photographed a region
    that did not move. None when a content-height box misses the screen.
    """
    x, y, width, height = (int(v) for v in box)
    sw, sh = _screen_span()
    if (
        height <= sh
        and width <= sw
        and x < sw
        and y < sh
        and x + width > 0
        and y + height > 0
    ):
        return (x, y, width, height)
    if x >= 0 and y >= 0 and x + width <= sw and y + height <= sh:
        return (x, y, width, height)
    left = max(x, 0)
    top = max(y, 0)
    right = min(x + width, sw)
    bottom = min(y + height, sh)
    if right - left < 1 or bottom - top < 1:
        return None
    return (left, top, right - left, bottom - top)


def _box_inside_screen(box: tuple[int, int, int, int]) -> bool:
    """True when ``box`` lies entirely on the screen.

    The 0.4.22 overflow list was 1239 by 422 at (20, 139), which fits. A
    content-height list is taller than the screen and does not.
    """
    x, y, width, height = (int(v) for v in box)
    sw, sh = _screen_span()
    if sw <= 0 or sh <= 0 or width < 1 or height < 1:
        return False
    return (
        width <= sw
        and height <= sh
        and x >= 0
        and y >= 0
        and x + width <= sw
        and y + height <= sh
    )


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


def _accepts_text(element: Element) -> bool:
    """True when ``set_value`` may write ``element``.

    The snapshot sets ``editable`` from the role. A synthetic element can name
    an editable role without that flag; the role set is the one the pruner
    uses. A menu, static text, or button is not in it.
    """
    if element.editable:
        return True
    from a11y_computer_use.observe import _EDITABLE_ROLES

    return element.role in _EDITABLE_ROLES


def _not_editable(element: Element) -> ComputerUseError:
    title = f" {element.title!r}" if element.title else ""
    return ComputerUseError(
        ErrorCode.UNSUPPORTED,
        f"{element.ref} ({element.role}{title}) is not editable",
        detail={"ref": element.ref, "role": element.role, "reason": "not_editable"},
    )


class LinuxDriver:
    """The `Driver` protocol, backed by AT-SPI2 / XTEST / X11."""

    name = "linux"
    #: What `app quit` sends: the desktop convention (cmd+q means Super+q on X, a nop).
    quit_chord = "ctrl+q"

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
            root = _atspi.find_root(app, scope)
            # An empty tree is a running app with nothing to show. No AT-SPI
            # application at all is the same answer menu list already gives:
            # the app is not running. An empty snapshot there told the agent
            # the app was open and custom-drawn.
            if root is None:
                raise ComputerUseError(
                    ErrorCode.APP_NOT_FOUND,
                    f"no running application matches {app!r}",
                    detail={"app": app},
                )
            pid = _atspi.pid_of(root) if root is not None else None
            accessor = _atspi.ATSPIAccessor()
            # Chromium lists: the rows are read from the list node this walk
            # holds. A saved head on another wrapper is not the snapshot.
            accessor.refresh_visible(root)
            return observe.build_snapshot(
                root, accessor, scope=scope, app=app, pid=pid,
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
        # A plain zero-size web wrapper can carry Chrome's click action and
        # still not be a target. press_element must not fire that action.
        if not element.clickable and not element.editable:
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

        if element.secure:
            self._focused_editable = None
            return False
        # A menu, heading, label, or button is not a text target. Raising
        # here is what stops the Runtime from focusing it and typing the
        # value into whatever is frontmost. No key, click, or focus is sent.
        if not _accepts_text(element):
            raise _not_editable(element)
        self._focused_editable = None
        handle = observe.ax_handle_for(element.snapshot_id, element.ref)
        if handle is None:
            return False
        # EditableText replace, or X11 clear-and-type when that interface is
        # missing. Success is the snapshot text read (Text.get_text 0, -1),
        # not a bounded read that can echo the request. Marshaled onto the
        # a11y thread.
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

    def hover(self, target: Target, *, dry_run: bool = False) -> object:
        """Move the pointer onto ``target`` and deliver a hover. No button."""
        if dry_run:
            return None
        if _on_wayland():
            raise _wayland_input_error("hover")
        from a11y_computer_use.drivers import _linux_input

        self._focused_editable = None
        x, y = _point_of(target)
        _linux_input.hover(x, y)
        return None

    def drag(self, start: Target, end: Target, *, button: MouseButton = MouseButton.LEFT,
             path: Sequence[Target] = (), pre_check: Callable | None = None,
             dry_run: bool = False) -> object:
        if dry_run:
            return None
        if _on_wayland():
            raise _wayland_input_error("drag")
        from a11y_computer_use.drivers import _linux_input

        self._focused_editable = None
        x1, y1 = _point_of(start)
        x2, y2 = _point_of(end)
        _linux_input.drag(x1, y1, x2, y2, button=_BUTTON_NAME.get(button, "left"),
                          path=[_point_of(p) for p in path])
        return None

    def scroll(self, target: Target, *, dx: int = 0, dy: int = 0,
               unit: ScrollUnit = ScrollUnit.LINES, pre_check: Callable | None = None,
               dry_run: bool = False) -> object:
        """Scroll ``target``.

        ``unit=lines`` sends one XTEST wheel notch per unit (X buttons 4/5
        and 6/7) when the target is not a Chromium list. A Chromium list
        whose own box sits fully on the screen, or a list inside a shorter
        overflow ancestor, steps its vertical AT-SPI scroll bar by one
        content row per line first. One line is one row, and not more than
        the rows already on screen. A missing bar tries AT-SPI ``scroll_to``
        (``TOP_EDGE``). When that value write or ``scroll_to`` does not move
        the painted rows, a left click lands on the vertical track. The
        wheel runs only when those do not move the list. A content-height
        list whose parent is the document, and a document group, keep the
        wheel.
        On a Chromium list a step is a success only when two things are
        both true: the pixels inside the list box change, and the snapshot
        head leaves the pre-step row and stays on the new row for two
        reads. The head is the first row of the list node being read whose
        own top is on or below the on-screen top of the list and which
        extends below the 8px clipped edge. That top is the list's own top
        when the list sits on the screen. A fully visible row flush with
        that top is the head. A row parked above the list, and a row at the
        content origin of a list whose top is above the screen, are not the
        head, even when the box covers a sample or a saved wrapper still
        names it. The scan keeps going through rows above the viewport and
        opens a wrapper that starts above the list when that wrapper still
        covers the list. A list with more children than the child-fetch
        cap is not read from child 0. The on-screen walk starts at the
        first child whose box reaches the viewport, so a row past that
        cap is listed when it is on screen. Rows the tree does not
        expose are not invented.
        The pixel check
        resamples the list's own screen box. The
        frame grabbed in the same turn as the wheel can still be the
        pre-paint image, which is what 0.4.13 reported as ``mean_abs`` 0.0
        while the list on screen had moved. A resample that stays at or
        below the still-page threshold raises `unsupported` with
        ``reason=page_unchanged`` and does not replace the rows. A grab
        that changed while no on-screen head leaves the old row raises
        `unsupported` with ``reason=rows_stale`` and does not replace the
        rows either. A pixel difference alone is not a successful scroll.
        ``snapshot`` then lists the confirmed rows, which is what
        ``scroll_to_find`` searches. A coordinate target and a
        non-Chromium element are not checked.         A Chromium list whose own
        box sits fully on the screen is a fixed-height overflow list. On
        the 0.4.24 retest that list was 1239 by 422 at (20, 139). Stepping
        its vertical AT-SPI scroll bar and AT-SPI ``scroll_to`` did not
        move the painted rows: the list stayed on ITEM-001 and the call
        ended on ``page_unchanged``. When that value write does not move
        the pixels, a left click lands on the vertical track (the lower
        track to go down, the upper track to go up).         The click is one
        page, inside the list. No wheel is sent when the click moves the
        pixels and the on-screen head. The bar write is still tried first
        and still has to move the pixels.         A grab under 1 can still be that
        move: uniform rows leave ``mean_abs`` about 0.5 to 0.7 while the
        on-screen head leaves the old row. That head change is the step.
        The bar write is not undone, and no track click or wheel follows
        it. A grab at or under the uniform-row floor is still that step
        when the head moved by about the requested lines, and not by a
        page. The 0.4.28 retest's dy=5 moved five rows under that floor
        and then clicked the track, so the list jumped 31 or 18 rows and
        skipped the ones in between. A far hit-test name on a still grab
        is not a step. ``page_unchanged`` is only
        the case where the head stays and the grab stays at or under 1.
        A list inside a shorter ancestor, the overflow wrapper, uses that
        wrapper as the painted viewport. Rows the wrapper hides are not
        the head. The same bar, ``scroll_to``, and track checks scroll
        that wrapper, and no wheel is sent when the step moves the head.
        A content-height list whose parent is the document, and a document
        group, keep the wheel. A document group is not a list and
        keeps its own center. ``unit=pixels`` writes the AT-SPI scroll-bar value
        by that delta and reads it back. It does not grab the list, does not
        hit-test it, and does not send notches. GTK scrolled windows expose
        the value in pixels. A missing bar, or a write that jumps or does
        not stick, raises `unsupported`. A shorter write is kept when one
        more pixel will not move (the bar is at its end). Pixel scroll does
        not need XTEST, so it is available on Wayland when a scroll bar is
        exposed.
        """
        if dry_run:
            return None
        resolved = ScrollUnit(unit)
        if resolved is ScrollUnit.PIXELS:
            self._focused_editable = None
            self._scroll_pixels(target, dx=int(dx), dy=int(dy))
            return None
        if _on_wayland():
            raise _wayland_input_error("scroll")
        from a11y_computer_use import observe
        from a11y_computer_use.drivers import _atspi, _linux_input

        self._focused_editable = None
        x, y = _wheel_point(target)
        handle = None
        if isinstance(target, Element):
            handle = observe.ax_handle_for(target.snapshot_id, target.ref)
        container = None
        if handle is not None and (int(dx) or int(dy)):
            container = self._run(lambda: _atspi.list_container(handle))
            # A content-height list inside an overflow wrapper is not the
            # anchor. The document is. The wrapper is what scrolls.
            if container is None:
                container = self._run(lambda: _atspi.list_with_overflow_ancestor(handle))
        if container is not None:
            point = self._run(lambda: _atspi.list_wheel_point(container))
            if point is not None:
                x, y = point
        if container is None:
            _linux_input.scroll(x, y, dx=dx, dy=dy)
            return None
        box = self._run(lambda: _atspi.list_screen_box(container))
        raw_box = box
        ancestor_box = self._run(lambda: _atspi.overflow_ancestor_box(container))
        if ancestor_box is not None:
            painted = _clip_box_to_screen(ancestor_box)
            box = painted if painted is not None else ancestor_box
        elif box is not None:
            box = _clip_box_to_screen(box)
        if box is None:
            _linux_input.scroll(x, y, dx=dx, dy=dy)
            return None
        display_id = target.display_id if isinstance(target, Point) else target.bounds.display_id
        still: dict[str, object] = {}

        def judge(mutate) -> bool:
            """True when ``mutate`` moved the list and the head left the old row.

            False when ``mutate`` did nothing or the pixels stayed, so the
            caller can try the wheel. A grab that changed while the head
            did not is ``rows_stale`` and is not retried.
            """
            before_grab = _grab_region(box)
            saved = self._run(lambda: _atspi.ensure_shown_rows(container))
            before_head = self._run(lambda: _atspi.row_head(saved))
            if not mutate():
                return False
            mean, samples = _list_pixels_moved(before_grab, box)
            shown = self._run(lambda: _atspi.row_names(saved))
            still["mean"] = mean
            still["samples"] = samples
            still["shown"] = shown
            if mean is None:
                raise ComputerUseError(
                    ErrorCode.UNSUPPORTED,
                    "the list could not be compared after the wheel scroll, so it was not "
                    "reported as a success",
                    detail={
                        "reason": "page_unseen",
                        "unit": "lines",
                        "dx": int(dx),
                        "dy": int(dy),
                        "rows": list(shown[:8]),
                    },
                )
            # A grab at or under the uniform-row floor is still a line
            # step when the head moved by about ``dy`` rows. The 0.4.28
            # retest left that five-row scroll_to in place and then
            # clicked the track. A page-sized jump is not the step, and
            # neither is a hit-test name that is not one of those rows.
            if mean <= _atspi._UNIFORM_ROW_MEAN:
                after = self._run(lambda: _atspi.wait_for_shown_rows(container, before_head))
                if after is not None and self._run(
                    lambda: _atspi.shown_line_step(container, before_head, after, int(dy))
                ):
                    self._run(lambda: _atspi.commit_shown_rows(container, after))
                    return True
                return False
            after = self._run(lambda: _atspi.wait_for_shown_rows(container, before_head))
            if after is not None:
                # Uniform rows stay under the still-page threshold of 1 and
                # the head has still left the old row. That is the step.
                # Do not fall through to a track click or a wheel.
                self._run(lambda: _atspi.commit_shown_rows(container, after))
                return True
            if mean <= _atspi._PAGE_MOVE_MEAN:
                return False
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                "the list moved on screen but the snapshot would still show the old rows",
                detail={
                    "reason": "rows_stale",
                    "unit": "lines",
                    "dx": int(dx),
                    "dy": int(dy),
                    "mean_abs": mean,
                    "rows": list(shown[:8]),
                },
            )

        # A viewport-sized Chromium list can ignore the wheel and
        # ``scroll_to``. Step its own vertical bar first. The pixel check
        # still decides success. A bar that is present and does not move
        # the list is not followed by ``scroll_to``.
        if raw_box is not None and (
            _box_inside_screen(raw_box) or ancestor_box is not None
        ) and int(dy):
            outcome: dict[str, object] = {"kind": "absent", "undo": None}

            def nudge() -> bool:
                kind, undo = _atspi.nudge_viewport_scrollbar(container, int(dy))
                outcome["kind"] = kind
                outcome["undo"] = undo
                return kind == "moved"

            if judge(lambda: bool(self._run(nudge))):
                return None
            undo = outcome.get("undo")
            if undo is not None:
                self._run(undo)
            if outcome["kind"] == "absent" and judge(lambda: bool(self._run(
                lambda: _atspi.scroll_viewport_by_lines(container, int(dy))
            ))):
                return None
            # The value write and scroll_to can both report success without
            # moving the painted rows. A track click is a pointer event on
            # the bar itself. The same pixel and head checks decide it.
            point = self._run(lambda: _atspi.viewport_track_point(container, int(dy)))
            if point is not None:
                self.hover(Point(display_id, point[0], point[1]))

                def click_track() -> bool:
                    _linux_input.click(point[0], point[1])
                    return True

                if judge(click_track):
                    return None
        self.hover(Point(display_id, x, y))

        def wheel() -> bool:
            _linux_input.scroll(x, y, dx=dx, dy=dy)
            return True

        if judge(wheel):
            return None
        mean = still.get("mean")
        samples = still.get("samples", 0)
        shown = tuple(still.get("shown") or ())
        if mean is None:
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                "the list could not be compared after the wheel scroll, so it was not "
                "reported as a success",
                detail={
                    "reason": "page_unseen",
                    "unit": "lines",
                    "dx": int(dx),
                    "dy": int(dy),
                    "rows": list(shown[:8]),
                },
            )
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            "the rows on screen did not change after the wheel scroll",
            detail={
                "reason": "page_unchanged",
                "unit": "lines",
                "dx": int(dx),
                "dy": int(dy),
                "mean_abs": mean,
                "samples": samples,
                "box": list(box),
                "rows": list(shown[:8]),
            },
        )

    def _scroll_pixels(self, target: Target, *, dx: int, dy: int) -> None:
        from a11y_computer_use import observe
        from a11y_computer_use.drivers import _atspi

        if dx == 0 and dy == 0:
            return
        handle = None
        if isinstance(target, Element):
            handle = observe.ax_handle_for(target.snapshot_id, target.ref)
        x, y = _point_of(target)

        def do() -> bool:
            if handle is not None and _atspi.scroll_by_pixels(handle, dx=dx, dy=dy):
                return True
            return _atspi.scroll_at_point(x, y, dx=dx, dy=dy)

        if self._run(do):
            return
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            "pixel scroll needs an accessible scroll bar; wheel notches were not sent",
            detail={
                "unit": "pixels",
                "dx": dx,
                "dy": dy,
                "api": "AT-SPI Value.set_current_value on a scroll bar",
                "hint": "The delta is applied to the scroll bar's accessible value, "
                        "which GTK scrolled windows expose in pixels, and the write "
                        "must read back as that delta. Pass a ref inside a scrolled "
                        "view that exposes a scroll bar, or use unit=lines for one "
                        "X11 wheel notch per unit.",
            },
        )

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
        self._refuse_xtest_password_focus()
        from a11y_computer_use.drivers import _linux_input

        _linux_input.type_string(text)  # XTEST fallback — separate X connection, not marshaled
        return None

    def _refuse_xtest_password_focus(self) -> None:
        """The focused-password probe `type_text` uses before XTEST keystrokes.

        A printable `key` chord lands in the same focused control, so it uses
        this probe too. Navigation chords do not.
        """
        from a11y_computer_use.drivers import _atspi

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

    def key_chord(self, chord: str, *, pre_check: Callable | None = None,
                  dry_run: bool = False) -> object:
        from a11y_computer_use.drivers import _linux_input

        # Validate the chord even on dry_run so a bad chord fails fast.
        if dry_run:
            _linux_input.validate_chord(chord)
            return None
        if _on_wayland():
            raise _wayland_input_error("key_chord")
        if printable_chord(chord):
            self._refuse_xtest_password_focus()
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

    def _menu_root(self, app: str) -> object:
        """The AT-SPI application accessible for ``app``, or a structured error.

        Missing bindings are the same permission error as `ensure_trusted`.
        An unknown name is `app_not_found` and does not wait on a bus.
        """
        from a11y_computer_use.drivers import _atspi

        try:
            root = self._run(lambda: _atspi.find_root(app, Scope.APP))
        except ImportError as exc:
            raise ComputerUseError(
                ErrorCode.PERMISSION_DENIED_ACCESSIBILITY,
                "AT-SPI2 Python bindings are missing",
                detail={"hint": "pip install a11y_computer_use[linux]; apt install "
                        "gir1.2-atspi-2.0 at-spi2-core", "error": str(exc)},
            ) from exc
        if root is None:
            raise ComputerUseError(
                ErrorCode.APP_NOT_FOUND,
                f"no running application matches {app!r}",
                detail={"app": app},
            )
        return root

    def menu_items(self, app: str, path: str | None) -> list[dict]:
        from a11y_computer_use.drivers import _linux_menus

        root = self._menu_root(app)
        return self._run(lambda: _linux_menus.menu_items(root, path))

    def menu_press(self, app: str, path: str) -> str:
        from a11y_computer_use.drivers import _linux_menus

        root = self._menu_root(app)
        return self._run(lambda: _linux_menus.menu_press(root, path))

    def menu_state(self, app: str) -> dict:
        """Open menu path, or closed when the bus is unavailable.

        An app that is not running is ``app_not_found``, the same answer as
        menu list. The Runtime asks before keystrokes and catches that error.
        A missing AT-SPI bus is not that answer: the menu is not known, and
        the call stays closed so a keystroke is not blocked on the bus.
        """
        from a11y_computer_use.drivers import _linux_menus

        try:
            root = self._menu_root(app)
        except ComputerUseError as exc:
            if exc.code is ErrorCode.APP_NOT_FOUND:
                raise
            return {"open": False, "path": []}
        try:
            return self._run(lambda: _linux_menus.menu_state(root))
        except ComputerUseError:
            return {"open": False, "path": []}

    def menu_close(self, app: str) -> list[str]:
        """Close the open menu. An app that is not running is ``app_not_found``.

        A missing AT-SPI bus returns no path. A menu that is still open
        after Escape raises. That error is not turned into an empty path:
        an empty path means nothing was open, and the Runtime would report
        success.
        """
        from a11y_computer_use.drivers import _linux_menus

        try:
            root = self._menu_root(app)
        except ComputerUseError as exc:
            if exc.code is ErrorCode.APP_NOT_FOUND:
                raise
            return []

        def read() -> dict:
            return self._run(lambda: _linux_menus.menu_state(root))

        return _linux_menus.menu_close(root, state=read)

    def file_dialog(self, verb: object, path: str, app: str) -> dict:
        raise _unsupported_file_dialog()

    def read_clipboard(self) -> str | None:
        from a11y_computer_use.drivers import _linux_system

        return _linux_system.read_clipboard()

    def write_clipboard(self, text: str) -> None:
        from a11y_computer_use.drivers import _linux_system

        _linux_system.write_clipboard(text)


def _unsupported_file_dialog() -> ComputerUseError:
    """GTK and portal file choosers are not driven. The message is the limit."""
    return ComputerUseError(
        ErrorCode.UNSUPPORTED,
        "file_dialog is not supported on Linux: GTK and portal file choosers are not driven by this tool",
        detail={
            "platform": "linux",
            "reason": "no_file_dialog",
            "hint": (
                "Open the chooser (for example menu 'File > Open'), then use the snapshot. "
                "In a GTK 3 file chooser, Ctrl+L focuses the location bar. "
                "set_value or type fills the location or name field. "
                "An xdg-desktop-portal chooser is a separate dialog; drive the fields it exposes."
            ),
        },
    )


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


def _list_pixels_moved(before, box: tuple[int, int, int, int]) -> tuple[float | None, int]:
    """Mean absolute difference of the list box against the pre-wheel grab.

    The first grab after the wheel can still be the pre-paint frame. On the
    0.4.13 retest that one sample was 0.0 while a later photograph of the
    list had moved. Later samples of the same box are compared to the
    pre-wheel grab until one clears the still-page threshold or the tries
    end. The returned mean is that first clear sample, or the last sample
    when none clears it. None means a grab could not be compared. The count
    is how many grabs were taken after the wheel.
    """
    from a11y_computer_use.drivers import _atspi

    last: float | None = 0.0
    for attempt in range(_atspi._PAINT_POLLS):
        after = _grab_region(box)
        mean = _region_mean_change(before, after)
        if mean is None:
            return None, attempt + 1
        last = mean
        if mean > _atspi._PAGE_MOVE_MEAN:
            return mean, attempt + 1
        if attempt + 1 < _atspi._PAINT_POLLS:
            _atspi.time.sleep(_atspi._PAINT_PAUSE_S)
    return last, _atspi._PAINT_POLLS


def _grab_region(box: tuple[int, int, int, int]):
    """PIL image of the list box. A failure is `unsupported`, not a scroll.

    ``box`` is ``(x, y, width, height)`` in screen pixels. Tests replace
    this with a fake grab. A coordinate line scroll and ``unit=pixels`` do
    not call it.
    """
    import os

    x, y, width, height = box
    try:
        from PIL import ImageGrab

        return ImageGrab.grab(
            bbox=(int(x), int(y), int(x) + int(width), int(y) + int(height)),
            xdisplay=os.environ.get("DISPLAY"),
        )
    except Exception as exc:
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            "the list could not be captured, so the scroll was not judged a success",
            detail={"reason": "page_unseen", "error": str(exc)},
        ) from exc


def _region_mean_change(before, after) -> float | None:
    """Mean absolute difference of two grabs, across the RGB channels.

    None when the two images cannot be compared. A still page is 0. This
    number is a veto, not a success: a difference does not by itself make
    the scroll succeed.
    """
    if before is None or after is None:
        return None
    try:
        from PIL import ImageChops, ImageStat

        left = before.convert("RGB")
        right = after.convert("RGB")
        if left.size != right.size:
            return None
        channels = ImageStat.Stat(ImageChops.difference(left, right)).mean[:3]
        if not channels:
            return None
        return float(sum(channels) / len(channels))
    except Exception:
        return None


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
