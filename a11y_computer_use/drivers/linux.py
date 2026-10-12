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

import re
import time
from collections.abc import Callable, Sequence

from a11y_computer_use.schema import (
    Bounds,
    ComputerUseError,
    Display,
    Element,
    ErrorCode,
    printable_chord,
    MouseButton,
    Point,
    clip_region_to_display,
    unknown_display_message,
    Scope,
    ScrollUnit,
    Snapshot,
    Target,
    WaitCondition,
)


def _hint_pid(app: str) -> int | None:
    """The only X11 pid for ``app``, or None when the name is not unique.

    Used when AT-SPI's own pid read does not answer. A missing display is
    None, the same as no match.
    """
    from a11y_computer_use.drivers import _linux_system

    try:
        pids = _linux_system.pids_matching(app)
    except ComputerUseError:
        raise
    except Exception:  # noqa: BLE001 - X11 may be unavailable (Wayland/headless)
        return None
    if len(pids) == 1:
        return next(iter(pids))
    return None


def _resolved_app(identifier: str) -> str:
    """The app id app list would show for ``identifier``.

    A window title, a process comm, and a WM_CLASS all resolve to the comm,
    the same way ``resolve_app`` does before an app-list row is built. An
    unmatched name is returned unchanged.
    """
    from a11y_computer_use.drivers import _linux_system

    found = _linux_system.resolve_app(identifier)
    return found or identifier


def _point_of(target: Target) -> tuple[int, int]:
    """Screen (x, y) for a coordinate action: a Point directly, else an
    Element's center. AT-SPI SCREEN coords == our physical-pixel space (scale 1).

    An element with no box is not a pointer target. Callers that already
    returned a typed no-op do not reach this.
    """
    if isinstance(target, Point):
        return int(target.x), int(target.y)
    if target.bounds is None:
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            f"{target.ref} ({target.role}) has no bounds; no pointer click was sent",
            detail={"ref": target.ref, "role": target.role, "reason": "not_actionable"},
        )
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


def _anchor_scroll_point(target: Target, handle, x: int, y: int) -> tuple[int, int] | None:
    """A point inside ``target`` whose window pid is ``handle``'s, or None.

    The center of a scroll area can sit on another window. The wheel moves
    to another point inside the same box when that point belongs to this
    process. None leaves ``(x, y)`` unchanged, including when no point
    matches: that is not reported as a scroll. A Chromium list does not
    call this; its row point is already chosen.
    """
    if handle is None or not isinstance(target, Element):
        return None
    from a11y_computer_use.drivers import _atspi, _linux_system
    from a11y_computer_use.drivers._linux_scroll_anchor import point_for_pid

    pid = _atspi.pid_of(handle)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    width, height = _screen_span()
    screen = (width, height) if width > 0 and height > 0 else None
    try:
        found = point_for_pid(
            target.bounds, pid, _linux_system.pid_at_point, screen=screen,
        )
    except Exception:
        return None
    if found is None or found == (int(x), int(y)):
        return None
    return found


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


_PIXELS_PER_NOTCH = 80
_MAX_PIXEL_NOTCHES = 24
# One line on a document that is not a Chromium list. The browser driver
# uses the same 40px step. XTEST notches do not move a Chrome document.
_PIXELS_PER_LINE = 40
_DEBUG_PORT = re.compile(rb"--remote-debugging-port=(\d+)")


def _pixel_notches(pixels: int) -> int:
    """Wheel notches for a pixel delta. One notch is about 80 pixels, capped."""
    pixels = int(pixels)
    if pixels == 0:
        return 0
    count = max(1, min(_MAX_PIXEL_NOTCHES, (abs(pixels) + _PIXELS_PER_NOTCH - 1) // _PIXELS_PER_NOTCH))
    return count if pixels > 0 else -count


def _pixel_scroll_box(target: Target) -> tuple[int, int, int, int] | None:
    """On-screen box whose pixels prove a scrollbar-less pixel scroll moved."""
    if isinstance(target, Element):
        bounds = target.bounds
        box = (int(bounds.x), int(bounds.y), int(bounds.width), int(bounds.height))
    elif isinstance(target, Point):
        box = (int(target.x) - 40, int(target.y) - 40, 80, 80)
    else:
        return None
    if box[2] <= 1 or box[3] <= 1:
        return None
    return _clip_box_to_screen(box)


def _pixel_scroll_unsupported(dx: int, dy: int) -> ComputerUseError:
    return ComputerUseError(
        ErrorCode.UNSUPPORTED,
        "pixel scroll needs an accessible scroll bar; wheel notches were not sent",
        detail={
            "unit": "pixels",
            "dx": dx,
            "dy": dy,
            "api": "AT-SPI Value.set_current_value on a scroll bar",
            "hint": "The delta is applied to the scroll bar's accessible value, "
                    "which GTK scrolled windows expose in pixels, and the write "
                    "must read back as that delta. With no bar, a DOM scroll is "
                    "used when Chrome was started with --remote-debugging-port, "
                    "otherwise wheel notches are sent at the element and kept "
                    "only when the pixels change.",
        },
    )


def _debug_port_for_pid(pid: int) -> int | None:
    try:
        raw = open(f"/proc/{int(pid)}/cmdline", "rb").read()
    except OSError:
        return None
    match = _DEBUG_PORT.search(raw)
    if match is None:
        return None
    port = int(match.group(1))
    return port if port > 0 else None


def _window_covers(row: dict, x: int, y: int) -> bool:
    bounds = row.get("bounds")
    if not isinstance(bounds, dict):
        return False
    left, top = int(bounds.get("x") or 0), int(bounds.get("y") or 0)
    width, height = int(bounds.get("width") or 0), int(bounds.get("height") or 0)
    return width > 0 and height > 0 and left <= x < left + width and top <= y < top + height


def _topmost_window_at(x: int, y: int) -> dict | None:
    """The highest managed window whose bounds contain ``(x, y)``.

    ``windows()`` is bottom-to-top, the same order as the stacking client
    list. A fullscreen desktop is first and contains every point.
    """
    from a11y_computer_use.drivers import _linux_system

    found = None
    for row in _linux_system.windows():
        if isinstance(row, dict) and _window_covers(row, x, y):
            found = row
    return found


def _cdp_socket_for_window(pages: list, window_title: str) -> str | None:
    """The DevTools socket of the tab this window is showing.

    A shared profile has other tabs. Scrolling the first one moves a page
    the user is not looking at. The window title is the page title plus the
    browser suffix, so the page title has to be that prefix. One tab is used
    as itself.
    """
    sockets = [str(page["webSocketDebuggerUrl"]) for page in pages if page.get("webSocketDebuggerUrl")]
    if not sockets:
        return None
    if len(sockets) == 1:
        return sockets[0]
    best: tuple[int, str] | None = None
    for page in pages:
        ws = page.get("webSocketDebuggerUrl")
        title = str(page.get("title") or "")
        if not ws or not title or not window_title:
            continue
        if window_title != title and not (
            window_title.startswith(title) and window_title[len(title):len(title) + 1] in " -—–"
        ):
            continue
        choice = (len(title), str(ws))
        if best is None or choice[0] >= best[0]:
            best = choice
    return None if best is None else best[1]


def _cdp_scroll_pixels(x: int, y: int, *, dx: int, dy: int) -> bool:
    """DOM-scroll a Chrome window that advertises a DevTools port.

    True only when ``scrollTop`` changes. False when no such window is under
    the point, so the caller can send a wheel and check the pixels.

    The pid is the topmost window at the point. A fullscreen desktop is
    also in the client list and contains every point, and it is not the
    window that paints the page. The socket is the tab whose title the
    window is showing, not whichever tab the browser listed first.
    """
    try:
        row = _topmost_window_at(x, y)
        if row is None:
            return False
        pid = row.get("pid")
        if not isinstance(pid, int):
            return False
        port = _debug_port_for_pid(pid)
        if port is None:
            return False
        from a11y_computer_use.drivers._cdp import CDPSession, connect, page_targets

        pages = page_targets(f"http://127.0.0.1:{port}")
        ws = _cdp_socket_for_window(pages, str(row.get("title") or ""))
        if not ws:
            return False
        transport = connect(str(ws), timeout=3.0)
        session = CDPSession(transport, default_timeout=3.0)
        try:
            expression = (
                "(() => { const el = document.scrollingElement || document.documentElement;"
                f" const before = el.scrollTop; el.scrollBy({int(dx)}, {int(dy)});"
                " return {before: before, after: el.scrollTop}; })()"
            )
            reply = session.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        finally:
            transport.close()
        value = ((reply.get("result") or {}).get("value") or {})
        before = float(value.get("before"))
        after = float(value.get("after"))
    except Exception:
        return False
    return abs(after - before) >= 1.0


def _merge_click_box(atspi_box, dom: dict | None) -> tuple[float, float, float, float] | None:
    """Screen box for a canvas or image click.

    A positive AT-SPI width and height is the whole box. A zero AT-SPI size
    keeps that origin when it is away from the screen corner and takes the
    DOM width and height. Otherwise the DOM rectangle is the screen box.
    """
    if atspi_box is not None and float(atspi_box[2]) > 0 and float(atspi_box[3]) > 0:
        return (
            float(atspi_box[0]), float(atspi_box[1]),
            float(atspi_box[2]), float(atspi_box[3]),
        )
    if not isinstance(dom, dict):
        return None
    try:
        width = float(dom["width"])
        height = float(dom["height"])
        screen_x = float(dom["screenX"])
        screen_y = float(dom["screenY"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    if atspi_box is not None and (abs(float(atspi_box[0])) > 1 or abs(float(atspi_box[1])) > 1):
        return (float(atspi_box[0]), float(atspi_box[1]), width, height)
    return (screen_x, screen_y, width, height)


def _dom_click_box(info: dict) -> dict | None:
    """DOM box of a Chrome node, in physical pixels, or None.

    Used when AT-SPI reports a zero size. The page is a DevTools target of
    the process that owns the node. The node is the DOM id, then the
    aria-label, then the only element of that tag.
    """
    pid = info.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    port = _debug_port_for_pid(pid)
    if port is None:
        return None
    import json

    spec = {
        "id": str(info.get("element_id") or ""),
        "label": str(info.get("label") or ""),
        "tag": str(info.get("tag") or ""),
    }
    expression = (
        "(() => {"
        f" const spec = {json.dumps(spec)};"
        " const find = () => {"
        "  if (spec.id) { const byId = document.getElementById(spec.id); if (byId) return byId; }"
        "  if (spec.label) {"
        "   const nodes = document.querySelectorAll(spec.tag || '*');"
        "   for (const node of nodes) {"
        "    if ((node.getAttribute('aria-label') || '') === spec.label) return node;"
        "   }"
        "  }"
        "  if (spec.tag) {"
        "   const all = document.getElementsByTagName(spec.tag);"
        "   if (all.length === 1) return all[0];"
        "  }"
        "  return null;"
        " };"
        " const el = find();"
        " if (!el) return null;"
        " const r = el.getBoundingClientRect();"
        " const dpr = window.devicePixelRatio || 1;"
        " const topInset = Math.max(0, window.outerHeight - window.innerHeight);"
        " return {"
        "  width: r.width * dpr,"
        "  height: r.height * dpr,"
        "  screenX: window.screenX * dpr + r.left * dpr,"
        "  screenY: window.screenY * dpr + topInset * dpr + r.top * dpr"
        " };"
        "})()"
    )
    try:
        from a11y_computer_use.drivers._cdp import CDPSession, connect, page_targets

        pages = page_targets(f"http://127.0.0.1:{port}")
    except Exception:
        return None
    for page in pages:
        ws = page.get("webSocketDebuggerUrl")
        if not ws:
            continue
        try:
            transport = connect(str(ws), timeout=3.0)
            session = CDPSession(transport, default_timeout=3.0)
            try:
                reply = session.call(
                    "Runtime.evaluate",
                    {"expression": expression, "returnByValue": True},
                )
            finally:
                transport.close()
        except Exception:
            continue
        value = (reply.get("result") or {}).get("value")
        if not isinstance(value, dict):
            continue
        try:
            if float(value.get("width") or 0) > 0 and float(value.get("height") or 0) > 0:
                return value
        except (TypeError, ValueError):
            continue
    return None


def _dom_activate_canvas_fallback(info: dict) -> bool:
    """Click the DOM node for canvas fallback content. False when it cannot.

    The node has to be inside a ``<canvas>`` and must not be the canvas
    itself. ``element.click()`` runs the control's handler. A coordinate
    click is not sent: the canvas is what is painted at that box.
    """
    pid = info.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    port = _debug_port_for_pid(pid)
    if port is None:
        return False
    import json

    spec = {
        "id": str(info.get("element_id") or ""),
        "label": str(info.get("label") or ""),
        "tag": str(info.get("tag") or ""),
    }
    expression = (
        "(() => {"
        f" const spec = {json.dumps(spec)};"
        " const insideCanvas = (node) => {"
        "  if (!node || String(node.tagName || '').toUpperCase() === 'CANVAS') return false;"
        "  let parent = node.parentElement;"
        "  while (parent) {"
        "   if (String(parent.tagName || '').toUpperCase() === 'CANVAS') return true;"
        "   parent = parent.parentElement;"
        "  }"
        "  return false;"
        " };"
        " const find = () => {"
        "  if (spec.id) {"
        "   const byId = document.getElementById(spec.id);"
        "   if (insideCanvas(byId)) return byId;"
        "  }"
        "  if (spec.label) {"
        "   const nodes = document.querySelectorAll(spec.tag || '*');"
        "   for (const node of nodes) {"
        "    if (!insideCanvas(node)) continue;"
        "    const label = node.getAttribute('aria-label') || '';"
        "    const text = (node.textContent || '').trim();"
        "    if (label === spec.label || text === spec.label) return node;"
        "   }"
        "  }"
        "  if (spec.tag) {"
        "   const all = [];"
        "   for (const node of document.getElementsByTagName(spec.tag)) {"
        "    if (insideCanvas(node)) all.push(node);"
        "   }"
        "   if (all.length === 1) return all[0];"
        "  }"
        "  return null;"
        " };"
        " const el = find();"
        " if (!el || typeof el.click !== 'function') return false;"
        " el.click();"
        " return true;"
        "})()"
    )
    try:
        from a11y_computer_use.drivers._cdp import CDPSession, connect, page_targets

        pages = page_targets(f"http://127.0.0.1:{port}")
    except Exception:
        return False
    for page in pages:
        ws = page.get("webSocketDebuggerUrl")
        if not ws:
            continue
        try:
            transport = connect(str(ws), timeout=3.0)
            session = CDPSession(transport, default_timeout=3.0)
            try:
                reply = session.call(
                    "Runtime.evaluate",
                    {"expression": expression, "returnByValue": True},
                )
            finally:
                transport.close()
        except Exception:
            continue
        if (reply.get("result") or {}).get("value") is True:
            return True
    return False


def _coordinate_click_box(info: dict, bounds: Bounds) -> tuple[float, float, float, float] | None:
    """Full screen box. AT-SPI size wins; a zero size uses the DOM box."""
    atspi_box = info.get("box")
    if atspi_box is not None and float(atspi_box[2]) > 0 and float(atspi_box[3]) > 0:
        return (
            float(atspi_box[0]), float(atspi_box[1]),
            float(atspi_box[2]), float(atspi_box[3]),
        )
    merged = _merge_click_box(atspi_box, _dom_click_box(info))
    if merged is not None:
        return merged
    if bounds.width > 1 and bounds.height > 1:
        return (float(bounds.x), float(bounds.y), float(bounds.width), float(bounds.height))
    return None


def _on_screen_name(node) -> str | None:
    """The node's name when its box meets the top of the screen, else None.

    A Chrome document lists every row, including ones parked above the
    viewport. Those are not the marker a scroll is judged by.
    """
    from a11y_computer_use.drivers import _atspi

    name = _atspi._node_name(node)
    pos, size = _atspi._extents(node)
    if not name or pos is None:
        return None
    top = int(pos[1])
    height = int(size[1]) if size is not None else 0
    if top + max(height, 0) < 0:
        return None
    return name


# A document's own name, and a list's accessible name, do not move when the
# page scrolls. The rows inside do. The browser frame's buttons do not.
_SCROLL_DESCEND_ROLES = frozenset({
    "document web", "document frame", "internal frame", "scroll pane",
    "list", "table", "tree", "tree table", "panel", "grouping", "section",
    "landmark", "filler", "form", "scroll view", "viewport", "layered pane",
})
_SCROLL_FRAME_ROLES = frozenset({
    "frame", "window", "application", "desktop frame", "menu bar",
})
_ROW_LIST_ROLES = frozenset({"list", "table", "tree", "tree table"})
# One name per row. A table cell's siblings (the value, the button) are not
# extra rows: six of those filled the sample after two rows, so a two-row
# twitch looked like the viewport and the next burst jumped over the target.
_ROW_ROLES = frozenset({
    "table row", "row", "list item", "tree item", "list box item",
})


def _above_viewport(node) -> bool:
    """True when ``node``'s box sits entirely above the screen."""
    from a11y_computer_use.drivers import _atspi

    _name, pos, size = None, *_atspi._extents(node)
    if pos is None:
        return False
    top = int(pos[1])
    height = int(size[1]) if size is not None else 0
    return top + max(height, 0) < 0


def _first_visible_row(node, count: int) -> int:
    """Index of the first child that is not parked above the viewport.

    List children stay in order, including the rows already scrolled off
    the top. A binary search finds that boundary. Reading only the first
    80 dropped the head once the page had moved past them.
    """
    from a11y_computer_use.drivers import _atspi

    lo, hi = 0, max(0, count - 1)
    first = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        child = _atspi._child_at(node, mid)
        if child is not None and _above_viewport(child):
            first = mid + 1
            lo = mid + 1
        else:
            hi = mid - 1
    return min(first, max(0, count - 1))


def _row_label(node) -> str | None:
    """The row's own name, or the first cell's, not every cell in the row."""
    from a11y_computer_use.drivers import _atspi

    def accept(name: str | None) -> str | None:
        if name and not _atspi._is_list_marker(name):
            return name
        return None

    own = accept(_on_screen_name(node))
    if own:
        return own
    count = _atspi._raw_child_count(node)
    count = count if isinstance(count, int) and count > 0 else 0
    for index in range(min(count, 4)):
        child = _atspi._child_at(node, index)
        if child is None:
            continue
        label = accept(_on_screen_name(child))
        if label:
            return label
        nested = _atspi._raw_child_count(child)
        nested = nested if isinstance(nested, int) and nested > 0 else 0
        for inner in range(min(nested, 2)):
            text = _atspi._child_at(child, inner)
            if text is None:
                continue
            label = accept(_on_screen_name(text))
            if label:
                return label
    return None


def _scan_row_children(node, role: str, name: str, count: int, depth: int, walk, found: list[str], limit: int) -> None:
    """On-screen rows of a list, starting at the first visible child."""
    from a11y_computer_use.drivers import _atspi

    del role, name
    index = _first_visible_row(node, count) if count > limit else 0
    examined = 0
    while index < count and examined < limit * 4 and len(found) < limit:
        child = _atspi._child_at(node, index)
        examined += 1
        if child is not None:
            walk(child, depth + 1)
        index += 1


def _showing_names(handle, limit: int = 6) -> tuple[str, ...]:
    """On-screen row names under ``handle``.

    Chrome's document keeps the old static texts for a fraction of a second
    after the wheel. The marker is the row names at or below the top of the
    screen, not the rows the scroll has already parked above it, and not the
    list's own name. A titled list (``aria-label``) used to be the whole
    sample, so the wait never saw the rows move and ran out the clock.
    Window buttons are not rows. A bullet marker is not a row. A table
    contributes one name per row, the first cell, so the sample is six
    rows and not two.
    """
    from a11y_computer_use.drivers import _atspi

    found: list[str] = []

    def walk(node, depth: int) -> None:
        if node is None or len(found) >= limit or depth > 8:
            return
        role = _atspi._role_name(node)
        if role in _ROW_ROLES:
            label = _row_label(node)
            if label:
                found.append(label)
            return
        name = _on_screen_name(node)
        descend = (
            depth == 0
            or role in _SCROLL_DESCEND_ROLES
            or not name
        )
        if not descend:
            if name and not _atspi._is_list_marker(name):
                found.append(name)
            return
        count = _atspi._raw_child_count(node)
        count = count if isinstance(count, int) and count > 0 else 0
        if role in _ROW_LIST_ROLES and count > 0:
            _scan_row_children(node, role, name or "", count, depth, walk, found, limit)
            return
        # A document's list is the marker. The file infobar is a sibling
        # and its names do not move. Walk the list first.
        span = min(count, 24)
        children = []
        for index in range(span):
            child = _atspi._child_at(node, index)
            if child is not None:
                children.append(child)
        lists = [child for child in children if _atspi._role_name(child) in _ROW_LIST_ROLES]
        others = [child for child in children if child not in lists]
        before = len(found)
        for child in lists:
            if len(found) >= limit:
                return
            walk(child, depth + 1)
        if lists and len(found) > before:
            return
        for child in others:
            if len(found) >= limit:
                return
            walk(child, depth + 1)
        if (
            depth and len(found) == before and count == 0
            and name and not _atspi._is_list_marker(name)
        ):
            found.append(name)

    walk(handle, 0)
    return tuple(found)


def _refresh_shown_names(run, handle) -> tuple[str, ...]:
    """On-screen row names after dropping libatspi's child cache.

    The cache answers ``get_child_at_index`` with the pre-wheel rows. A
    poll that does not clear it waits out the deadline on names that
    cannot change.
    """
    from a11y_computer_use.drivers import _atspi

    run(lambda node=handle: _atspi._invalidate_tree_cache(node))
    return run(lambda node=handle: _showing_names(node))


def _settle_shown_names(run, handle) -> None:
    """Wait until a live Chromium document's on-screen rows leave the pre-scroll set.

    The wheel returns before AT-SPI does. A search that snapshots immediately
    still sees the old rows and scrolls again, so the target goes by between
    reads. A test double has no GObject pointer and is not waited on. The
    read is the rows, and it returns when those names change. Give up at
    0.9s; a tree that never changes is not spun on.
    """
    from a11y_computer_use.drivers import _atspi

    try:
        if not run(lambda: hasattr(handle, "__gpointer__")):
            return
        if not run(lambda: _atspi._chromium_app(handle)):
            return
        before = run(lambda: _showing_names(handle))
    except Exception:
        return
    if len(before) < 2:
        return
    deadline = time.monotonic() + 0.9
    while time.monotonic() < deadline:
        time.sleep(0.05)
        try:
            after = _refresh_shown_names(run, handle)
        except Exception:
            return
        if after and after != before:
            return


def _strip_shared_names(
    before: tuple[str, ...], after: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Drop a shared prefix, such as the document title, from both samples."""
    shared = 0
    while shared < len(before) and shared < len(after) and before[shared] == after[shared]:
        shared += 1
    return before[shared:], after[shared:]


def _shown_name_advance(before: tuple[str, ...], after: tuple[str, ...], sign: int) -> int:
    """How far the on-screen head moved, in names. Positive is later names.

    A shared prefix, such as the document title, is ignored. The new head
    at index ``k`` of what remains of ``before`` is a move of ``k`` toward
    the end. The old head at index ``k`` of ``after`` is a move of ``k``
    toward the start. No shared name is a move of at least the whole
    remaining sample, in the direction ``sign`` asked for.
    """
    before, after = _strip_shared_names(before, after)
    # A document title stays at the front of both samples. The rows after
    # it are the move. Nothing left on one side means the head did not move.
    if not before or not after:
        return 0
    if after[0] in before:
        return before.index(after[0])
    if before[0] in after:
        return -after.index(before[0])
    return sign * len(before)


def _row_marker(names: tuple[str, ...]) -> bool:
    """True when ``names`` includes a content row, not only window chrome.

    Row labels on the pages this wait is for carry a digit (``ITEM-001``,
    ``TICKET-0040``, ``ROW-0300``). The list's accessible name and the file
    infobar do not. A sample of only those does not show whether the page
    moved.
    """
    return any(any(ch.isdigit() for ch in name) for name in names)


def _names_parted(before: tuple[str, ...], after: tuple[str, ...]) -> bool:
    """True when the new head has left the old sample.

    A five-notch burst that moves about a viewport does this: the rows
    that were on screen are gone, and the advance is at least the whole
    sample. A burst that collapsed to one row still names that row inside
    the old sample, so this is false and the step can be topped up.
    """
    before, after = _strip_shared_names(before, after)
    if not before or not after:
        return False
    return after[0] not in before and before[0] not in after


def _name_source(run, handle):
    """A node whose on-screen row names still describe the document after a wheel.

    The anchor is often one cell. That cell leaves the tree as soon as the
    page moves, so its names cannot say whether the step was one row or a
    whole viewport. An ancestor that lists several rows stays. The browser
    frame does not: its Minimize, Maximize, and Close names never move, and
    waiting on them runs out the clock on every step.
    """
    from a11y_computer_use.drivers import _atspi

    def blocked(node) -> bool:
        try:
            role = run(lambda acc=node: _atspi._role_name(acc))
        except Exception:
            return False
        return role in _SCROLL_FRAME_ROLES

    source = handle
    try:
        before = run(lambda: _showing_names(handle))
    except Exception:
        return handle, ()
    if len(before) >= 4 or blocked(handle):
        return handle, before
    current = handle
    for _ in range(8):
        try:
            parent = run(lambda node=current: _atspi._parent_of(node))
        except Exception:
            break
        if parent is None or blocked(parent):
            break
        current = parent
        try:
            names = run(lambda node=current: _showing_names(node))
        except Exception:
            break
        if len(names) > len(before):
            source, before = current, names
        if len(before) >= 4:
            break
    return source, before


def _offset_did_not_move(before: float | None, after: float | None) -> bool | None:
    """Whether a container's scroll offset stayed put.

    True only when both reads exist and match. False when they differ.
    None when either read is missing, which is not proof the page was
    unchanged.
    """
    if before is None or after is None:
        return None
    return abs(float(after) - float(before)) < 0.5


def _document_did_not_move(dx: int, dy: int, offset: float | None = None) -> ComputerUseError:
    """The document wheel ran and the on-screen rows stayed put.

    ``surface=document`` tells ``scroll_to_find`` this is the end of the
    page. A list that did not move does not set it, and still turns around.
    """
    return ComputerUseError(
        ErrorCode.UNSUPPORTED,
        "the document did not move after the wheel scroll",
        detail={
            "reason": "page_unchanged",
            "surface": "document",
            "unit": "lines",
            "dx": int(dx),
            "dy": int(dy),
            "scroll_position": offset,
        },
    )


def _wheel_chromium_document(run, handle, x: int, y: int, dx: int, dy: int) -> None:
    """Wheel a document by the requested notches, then wait until the tree says so.

    Chrome's document is a group, so this is the notch path, not the list
    bar. One notch moves several rows. Sending them one at a time, and
    stopping once the head had moved by about ``|dy|`` names, made each
    ``scroll_to_find`` step about four rows and about two seconds. The
    step sends the requested notches together, which is about a viewport,
    and returns as soon as the on-screen head shows that move. The last
    notch of a five-notch request is not sent: on short rows it lands
    past the rows that were on screen, and the next snapshot skips them.

    A burst can collapse into one row on a busy runner. When the head
    then settles still inside the old sample and short of ``|dy|`` names,
    more notches go out one at a time until the head has moved by about
    that many names, or a notch leaves the names unchanged. A head that
    has already left the sample took the viewport step; more notches are
    not added on top of it. A burst whose rows never change raises
    ``page_unchanged`` with ``surface=document``: the page is at its end.
    """
    from a11y_computer_use.drivers import _atspi, _linux_input

    wanted = abs(int(dy))
    sign = 1 if int(dy) > 0 else -1

    def plain() -> None:
        _linux_input.scroll(x, y, dx=dx, dy=dy)
        if handle is not None:
            _settle_shown_names(run, handle)

    if handle is None or wanted == 0:
        _linux_input.scroll(x, y, dx=dx, dy=dy)
        return
    try:
        if not run(lambda: hasattr(handle, "__gpointer__")):
            plain()
            return
        if not run(lambda: _atspi._chromium_app(handle)):
            plain()
            return
    except Exception:
        plain()
        return
    source, before = _name_source(run, handle)
    if len(before) < 2:
        plain()
        return
    try:
        before_offset = run(lambda node=source: _atspi.scroll_offset(node))
    except Exception:
        before_offset = None

    def measure() -> tuple[tuple[str, ...], int]:
        after = _refresh_shown_names(run, source)
        return after, _shown_name_advance(before, after, sign)

    # Five notches clear a viewport of tall rows and then a couple more
    # of short ones, so the next snapshot starts below the previous tail
    # and the row in that gap is never shown. Four notches are the burst.
    # On short rows those four can still clear the sample. The search
    # then wheels back until one of the old rows is on screen again.
    burst = wanted - 1 if wanted >= 5 else wanted
    # The whole request goes out together. A tree that publishes that
    # viewport inside this window — the same 0.9s ``_settle_shown_names``
    # uses — is not given extra notches.
    _linux_input.scroll(x, y, dx=dx, dy=sign * burst)
    moved = 0
    changed = False
    stable = 0
    polls = 0
    last: int | None = None
    after = before
    deadline = time.monotonic() + 0.9
    while True:
        try:
            after, nxt = measure()
        except Exception:
            return
        if abs(nxt) >= burst or _names_parted(before, after):
            # A read that lost the rows (the list label, or the file
            # infobar) is not the viewport landing. Keep polling.
            if _row_marker(before) and not _row_marker(after):
                pass
            else:
                if (
                    _names_parted(before, after)
                    and _row_marker(before)
                    and _row_marker(after)
                ):
                    # The burst cleared the sample. One notch back brings
                    # the previous tail on screen, so the next snapshot
                    # still contains the row this step would have skipped.
                    # A sample that stays apart stops after ``burst``
                    # reverse notches.
                    for _back in range(burst):
                        _linux_input.scroll(x, y, dy=-sign)
                        try:
                            after = _refresh_shown_names(run, source)
                        except Exception:
                            return
                        if not _names_parted(before, after):
                            return
                return
        moved = nxt
        if after != before:
            changed = True
        if moved == last:
            stable += 1
        else:
            stable = 0
            last = moved
        # A one-row twitch can sit still for two polls while the rest of
        # the burst is still arriving. Topping up then stacks another
        # notch on a viewport and the next snapshot skips a band of rows.
        # Eight polls is about 0.4s, long enough for the burst to land. A
        # burst that is still short then is the collapsed step.
        polls += 1
        if stable >= 2 and changed and polls >= 8:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    # No measurable move, or the head already left the sample. The burst
    # is the step. A sample that never changed is the end of the document
    # only when the container's scroll offset also stayed put. A missing
    # offset is not that proof. A short move still inside the old sample
    # is the collapsed burst: top it up.
    if not changed:
        try:
            after_offset = run(lambda node=source: _atspi.scroll_offset(node))
        except Exception:
            after_offset = None
        if _offset_did_not_move(before_offset, after_offset) is True:
            raise _document_did_not_move(dx, dy, before_offset)
        return
    if not (0 < abs(moved) < burst) or _names_parted(before, after):
        return
    for _extra in range(wanted * 2):
        _linux_input.scroll(x, y, dy=sign)
        saw = False
        for _ in range(8):
            try:
                after, nxt = measure()
            except Exception:
                return
            if abs(nxt) >= burst or _names_parted(before, after):
                return
            if nxt != moved:
                moved = nxt
                saw = True
                break
            time.sleep(0.05)
        if not saw:
            return


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
    """True when the snapshot says ``set_value`` may write ``element``.

    The snapshot sets ``editable`` from a text-field role, or from an AT-SPI
    editable entry. A browser paragraph, document, or combo is not an entry.
    An Impress or Draw paragraph with ``STATE_EDITABLE`` and EditableText is.
    A synthetic element can name an editable role without that flag; the role
    set is the one the pruner uses. A menu, static text, button, or combo is
    not in it unless the snapshot flag is set. A combo is written earlier,
    through its own items. A live handle that is not an editable entry is
    refused even when this flag is set, before any select-all.
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


def _named_browser(app_id: str | None) -> bool:
    """True when ``app_id`` is Chrome or Firefox, which own an address bar."""
    name = (app_id or "").lower()
    return "chrome" in name or "chromium" in name or "firefox" in name


def _browser_family(app_id: str | None) -> str:
    """``chrome`` or ``firefox`` for those apps, otherwise the name itself."""
    name = (app_id or "").casefold()
    if "chrom" in name:
        return "chrome"
    if "firefox" in name:
        return "firefox"
    return name


def _missing_atspi_bindings(exc: BaseException) -> ComputerUseError:
    """Missing PyGObject or the Atspi typelib. Not a macOS Accessibility grant.

    ``doctor`` prints the same hint. A model that follows the macOS pane text
    asks for ``request_permission``, which cannot install ``gi``.
    """
    from a11y_computer_use.doctor import ATSPI_BINDINGS_FIX

    return ComputerUseError(
        ErrorCode.UNSUPPORTED,
        "AT-SPI2 Python bindings are missing",
        detail={
            "reason": "missing_dependency",
            "module": "gi",
            "hint": ATSPI_BINDINGS_FIX,
            "error": str(exc),
        },
    )


class LinuxDriver:
    """The `Driver` protocol, backed by AT-SPI2 / XTEST / X11."""

    name = "linux"
    #: What `app quit` sends: the desktop convention (cmd+q means Super+q on X, a nop).
    quit_chord = "ctrl+q"

    def __init__(self) -> None:
        # Set when a file-chooser type was confirmed from the location entry
        # rather than the page. The outcome judge reads it once.
        self._chooser_readback: str | None = None
        # Final field text when type replaced an active selection. The
        # outcome check confirms that string instead of an append.
        self._type_expected: str | None = None
        # Impress or Draw frame text from before a type, and whether that
        # frame could be read. The outcome judge consumes these once.
        self._drawing_type_before: str | None = None
        self._drawing_type_state: str | None = None
        self._type_unverified: bool = False
        # The last editable element focused via press_element — type_text enters
        # text into it through AT-SPI EditableText (deterministic; see type_text).
        self._focused_editable = None
        # ``find`` sets this around one snapshot so a GTK or Chromium table
        # row that is still off screen can be scrolled into the tree. A
        # plain snapshot leaves it empty and does not move the view.
        self._table_seek: str | None = None
        # ``(atspi key, start, limit)`` for one snapshot. expand sets it so
        # that node lists its next page of children instead of the band the
        # last snapshot already showed. Cleared after the snapshot.
        self._expand_page: tuple | None = None
        # How far expand has already paged each node, keyed by AT-SPI path.
        self._expand_cursors: dict[object, int] = {}

    def _run(self, fn):
        """Run an AT-SPI (libatspi) op inline, or — when the event cache is opted
        in (A11Y_COMPUTER_USE_ATSPI_EVENTS=1) — on the shared a11y thread so libatspi's
        read cache is trusted (~1.8x faster snapshots) and stays single-threaded.
        Only libatspi ops go through here; XTEST input uses a separate X
        connection and is unaffected.

        A dropped accessibility bus reconnects once. The outermost call retries,
        so a snapshot walks the tree again instead of reusing a disposed node.
        """
        from a11y_computer_use.drivers import _atspi_events, _dbus_guard

        def once():
            return _atspi_events.submit(fn) if _atspi_events.enabled() else fn()

        return _dbus_guard.call_with_reconnect(once)

    # -- permissions --------------------------------------------------------
    def ensure_trusted(self) -> None:
        """Linux has no per-app TCC grant; the requirement is that the AT-SPI2
        registry is reachable (accessibility bus running). A missing ``gi``
        import is ``unsupported`` / ``missing_dependency`` with the doctor
        hint. An unreachable bus is the accessibility-off error."""
        try:
            from a11y_computer_use.drivers import _atspi

            # Force the whole desktop's Chromium/Electron apps to expose their
            # a11y tree (org.a11y.Status flip) before we probe — turns a
            # Grok-style a11y-OFF desktop into an a11y-first one, no relaunch.
            _atspi.enable_a11y_status()  # Gio/session bus — not libatspi, runs inline
            # ImportError from a missing gi or typelib must not be swallowed
            # by _safe: that used to look like an unreachable accessibility bus.
            Atspi = _atspi._atspi()
            desktop = self._run(lambda: _atspi._safe(lambda: Atspi.get_desktop(0)))
        except ImportError as exc:
            raise _missing_atspi_bindings(exc) from exc
        if desktop is None:
            raise ComputerUseError(
                ErrorCode.PERMISSION_DENIED_ACCESSIBILITY,
                "the AT-SPI2 accessibility bus is not reachable",
                detail={"hint": "enable accessibility: apt install at-spi2-core; run under a "
                        "session bus; `gsettings set org.gnome.desktop.interface "
                        "toolkit-accessibility true`; for Chromium add "
                        "--force-renderer-accessibility. See docs/linux-port.md."},
            )

    def _root_for_snapshot(self, app: str, scope: Scope):
        """(name, accessible) for a snapshot.

        The given name wins when it already selects an application. The
        comm from app list is the fallback, which is how ``LibreOffice``
        becomes ``soffice.bin``.
        """
        from a11y_computer_use.drivers import _atspi

        root = self._run(lambda name=app: _atspi.find_root(name, scope))
        if root is not None:
            return app, root
        resolved = _resolved_app(app)
        if resolved != app:
            root = self._run(lambda name=resolved: _atspi.find_root(name, scope))
            return resolved, root
        return app, None

    # -- observe (AT-SPI2) --------------------------------------------------
    def snapshot(self, scope: Scope, app: str) -> Snapshot:
        """Pruned snapshot of ``app``.

        If the accessibility bus drops while LibreOffice is registering, the
        walk is retried once on a new connection. A second failure is a
        retryable ``timeout`` with reason ``bus_disconnected``.

        ``Scope.DISPLAY`` walks every application on the AT-SPI desktop. An
        application that does not answer is omitted and the others are
        returned. A stopped target of ``Scope.WINDOW`` or ``Scope.APP``
        raises ``app_not_responding`` instead of an empty tree.
        """
        from a11y_computer_use.drivers import _dbus_guard

        if scope is Scope.DISPLAY:
            return _dbus_guard.call_with_reconnect(self._snapshot_desktop)
        return _dbus_guard.call_with_reconnect(lambda: self._snapshot(scope, app))

    def _not_responding(self, app: str, exc: ComputerUseError) -> ComputerUseError:
        """The caller's app name, with the pid the bus actually timed out on."""
        pid = exc.detail.get("pid")
        pid_text = f" (pid {pid})" if isinstance(pid, int) and pid > 0 else ""
        detail = dict(exc.detail)
        detail["app"] = app
        detail["pid"] = pid if isinstance(pid, int) and pid > 0 else None
        return ComputerUseError(
            ErrorCode.APP_NOT_RESPONDING,
            f"{app} did not answer accessibility queries{pid_text}",
            detail=detail,
        )

    def _snapshot_desktop(self) -> Snapshot:
        """One snapshot of every application that answers AT-SPI.

        A frozen application is left out. When at least one other application
        answers, its tree is in the result. When none answer, the error names
        the failure instead of an empty success.
        """
        from a11y_computer_use import observe
        from a11y_computer_use.drivers import _atspi

        found = self._run(_atspi.desktop_application_roots)
        if not found.ready:
            if found.silent:
                raise ComputerUseError(
                    ErrorCode.APP_NOT_RESPONDING,
                    "an application did not answer accessibility queries",
                    detail={
                        "app": None,
                        "pid": None,
                        "silent": found.silent,
                        "hint": _atspi._APP_NOT_RESPONDING_HINT,
                    },
                )
            return self._empty_display()
        items = [
            (name, None, lambda name=name, root=root: self._snapshot_root(name, root, Scope.APP))
            for name, root in found.ready
        ]
        snaps = _atspi.collect_app_snapshots(items)
        return observe.merge_snapshots(snaps)

    def _empty_display(self) -> Snapshot:
        from a11y_computer_use import observe
        from a11y_computer_use.drivers import _atspi

        return observe.build_snapshot(
            None,
            _atspi.ATSPIAccessor(),
            scope=Scope.DISPLAY,
            app=None,
            pid=None,
            geometry=_atspi.primary_geometry(),
        )

    def _snapshot(self, scope: Scope, app: str) -> Snapshot:
        try:
            return self._snapshot_resolved(scope, app)
        except ComputerUseError as exc:
            if exc.code is ErrorCode.APP_NOT_RESPONDING and exc.detail.get("app") != app:
                raise self._not_responding(app, exc) from exc
            raise

    def _snapshot_resolved(self, scope: Scope, app: str) -> Snapshot:
        from a11y_computer_use.drivers import _atspi

        # A stopped process is already visible in /proc. Raising here skips
        # the desktop scan, which otherwise spends a D-Bus timeout on every
        # sibling before this app's own read fails.
        _atspi.raise_if_app_stopped(app)
        # A name find_root already answers is the AT-SPI application. Two
        # Python windows share the comm python3; resolving that name to the
        # comm first would snapshot the other window. LibreOffice is the
        # other case: the caller says LibreOffice and the bus says
        # soffice.bin, so the comm is used only after the given name misses.
        resolved, root = self._root_for_snapshot(app, scope)
        # The X window is in app list and window list before the application
        # accessible exists. LibreOffice's gap was 3–13 s. Wait only while
        # the list still shows the app, or a LibreOffice process in this
        # session is up, and stop at ATSPI_REGISTER_WAIT_S. A name that is
        # not listed does not wait, including a soffice process on another
        # display. The missing gtk3 bridge is reported after that deadline,
        # not during the registration gap.
        if root is None and (
            _atspi.should_wait_for_atspi(app) or _atspi.should_wait_for_atspi(resolved)
        ):
            deadline = time.monotonic() + _atspi.ATSPI_REGISTER_WAIT_S
            while root is None and time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(_atspi.ATSPI_REGISTER_POLL_S, remaining))
                resolved, root = self._root_for_snapshot(app, scope)
                if root is not None:
                    break
                if not (
                    _atspi.should_wait_for_atspi(app) or _atspi.should_wait_for_atspi(resolved)
                ):
                    break
        # An empty tree is a running app with nothing to show. No AT-SPI
        # application at all is the same answer menu list already gives:
        # the app is not running. An empty snapshot there told the agent
        # the app was open and custom-drawn.
        if root is None:
            if _atspi.libreoffice_without_bridge(resolved):
                raise ComputerUseError(
                    ErrorCode.UNSUPPORTED,
                    "LibreOffice is running without an accessibility bridge. "
                    "Install libreoffice-gtk3 and start it with SAL_USE_VCLPLUGIN=gtk3.",
                    detail={
                        "app": resolved,
                        "reason": "no_accessibility_bridge",
                        "hint": "apt install libreoffice-gtk3 && SAL_USE_VCLPLUGIN=gtk3 soffice --calc",
                    },
                )
            raise ComputerUseError(
                ErrorCode.APP_NOT_FOUND,
                f"no running application matches {resolved!r}",
                detail={"app": resolved},
            )

        return self._snapshot_root(resolved, root, scope, caller=app)

    def _snapshot_root(
        self, app: str, root: object, scope: Scope, caller: str | None = None,
    ) -> Snapshot:
        """Pruned snapshot of one already-resolved AT-SPI root.

        Unanswered reads of this application raise ``app_not_responding``
        with its name and pid. The budget starts here, after the desktop
        scan, so a frozen sibling is not charged to ``app``.
        """
        from a11y_computer_use import observe
        from a11y_computer_use.drivers import _atspi

        def _do() -> Snapshot:
            hinted = _hint_pid(app)
            with _atspi.app_reply_watch(app, hinted, root) as watch:
                # A find bar or omnibox popup fills libatspi's child cache.
                # Closing the bar and Reload leave that cache in place, so
                # the next walk would keep the bar or an empty document.
                # Drop it and use the frame that holds the selected tab.
                # Until that bar has been snapshotted, this is ``root``.
                walked = _atspi.chromium_snapshot_root(root)
                pid = _atspi.pid_of(walked) or _atspi.pid_of(root) or hinted
                if isinstance(pid, int) and pid > 0:
                    watch.pid = pid
                accessor = _atspi.ATSPIAccessor()
                # ``app`` is the resolved comm when the caller said LibreOffice
                # and the bus said soffice.bin. Either name marks the suite.
                accessor.libreoffice = _atspi.libreoffice_app(app or "") or _atspi.libreoffice_app(
                    caller or ""
                )
                # LibreOffice 25.2 reports content a title bar high until the
                # first pointer event. A status-bar motion, then this walk,
                # republishes those boxes. LibreOffice 24 already matches and
                # is not moved, so its pointer inset and the placement cache
                # stay as they were.
                if accessor.libreoffice:
                    _atspi.settle_libreoffice_geometry(root)
                # A table that starts below the viewport is dropped before
                # children() can seek it. Scroll the matching cell first, and
                # only for Chromium: GTK tables seek inside their own walk.
                # After the find bar, the page frame is ``walked``.
                seek = self._table_seek.strip() if isinstance(self._table_seek, str) else ""
                if seek and _atspi._chromium_app(root):
                    _atspi.reveal_text_in_document(walked, seek)
                # Chromium lists: the rows are read from the list node this walk
                # holds. A saved head on another wrapper is not the snapshot.
                accessor.refresh_visible(walked)
                accessor._table_seek = self._table_seek
                accessor._expand_page = self._expand_page
                snap = observe.build_snapshot(
                    walked, accessor, scope=scope, app=app, pid=watch.pid,
                    geometry=_atspi.primary_geometry(),
                )
                _atspi.note_browser_bar(walked, snap.elements)
                return snap

        return self._run(_do)

    def document_url(self, app: str | None = None) -> str | None:
        """AT-SPI DocURL for ``app``'s document, or None when the tree has none."""
        from a11y_computer_use.drivers import _atspi
        from a11y_computer_use.schema import Scope

        if not app:
            return None
        # Calc has no document URL. Searching the sheet for one wedges the
        # accessibility connection; the action that follows then takes the
        # per-call timeout on every read.
        if _atspi.libreoffice_app(app):
            return None

        def _do() -> str | None:
            window = _atspi.find_root(app, Scope.WINDOW)
            url = _atspi.document_url_of(window) if window is not None else None
            if url:
                return url
            app_root = _atspi.find_root(app, Scope.APP)
            if app_root is None:
                return None
            # The showing document may be invisible to a walk of the active
            # frame. Collection on the application still lists it.
            if app_root is not window:
                url = _atspi._collected_content_document(app_root)
                if url:
                    return url
                return _atspi.other_frame_document_url(app_root, window)
            return None

        try:
            return self._run(_do)
        except Exception:  # noqa: BLE001 - a missing URL must not block native apps
            return None

    def element_url(self, element: Element) -> str | None:
        """Hyperlink URI for ``element``, walking a few ancestors. None if unset."""
        from a11y_computer_use import observe
        from a11y_computer_use.drivers import _atspi

        handle = observe.ax_handle_for(element.snapshot_id, element.ref)
        if handle is None:
            return None
        try:
            return self._run(lambda: _atspi.hyperlink_uri(handle))
        except Exception:  # noqa: BLE001 - no URI is "not a link", not a failure
            return None

    def element_document_url(self, element: Element) -> str | None:
        """URL of the document that owns ``element``, including an iframe."""
        from a11y_computer_use import observe
        from a11y_computer_use.drivers import _atspi

        handle = observe.ax_handle_for(element.snapshot_id, element.ref)
        if handle is None:
            return None
        try:
            return self._run(lambda: _atspi.document_url_for(handle))
        except Exception:  # noqa: BLE001 - a missing document is not a failed action
            return None

    def element_in_browser_chrome(self, element: Element) -> bool:
        """True when ``element`` is browser UI (the tab strip, the omnibox)."""
        from a11y_computer_use import observe
        from a11y_computer_use.drivers import _atspi

        handle = observe.ax_handle_for(element.snapshot_id, element.ref)
        if handle is None:
            return False
        try:
            return bool(self._run(lambda: _atspi.in_browser_chrome(handle)))
        except Exception:  # noqa: BLE001 - unknown chrome is treated as page content
            return False

    def focus_in_browser_chrome(self, app: str) -> bool:
        """True when keyboard focus is in browser UI rather than the page."""
        from a11y_computer_use.drivers import _atspi

        if not app:
            return False
        try:
            return bool(self._run(lambda: _atspi.focus_in_browser_chrome(app)))
        except Exception:  # noqa: BLE001 - unknown focus falls through to the page URL
            return False

    def address_bar_text(self, app: str) -> str | None:
        """Text of the address bar, read from the application tree."""
        from a11y_computer_use.drivers import _atspi

        if not app:
            return None
        try:
            return self._run(lambda: _atspi.address_bar_text(app))
        except Exception:  # noqa: BLE001 - an unreadable bar is not a destination
            return None

    def resolve_ref(self, snap: Snapshot, ref: str, *, live: Snapshot | None = None) -> Element:
        """Re-resolve a snapshot-scoped ref against a fresh live tree via the
        SHARED anchor matcher (`observe._match_anchor`) — the same re-resolution
        semantics as macOS, without macOS's pyobjc `observe.snapshot`.

        A node the pruned tree dropped because its document is not showing is
        ``not_showing``, not ``stale_ref``. ``stale_ref`` stays the answer when
        the node is gone. A node that is still alive and has scrolled off the
        display stays ``stale_ref`` here; crop asks `alive_offscreen` and
        reports ``not_visible`` instead. Click keeps this ``stale_ref``.
        """
        from a11y_computer_use import observe

        if live is None:
            live = self.snapshot(snap.scope, snap.app)
        try:
            return observe.rematch_ref(snap, ref, live)
        except ComputerUseError as exc:
            # A quit-and-relaunched app, or a different document, must stay
            # stale_ref. Searching the new tree for a hidden namesake would
            # retarget the error onto the replacement.
            if exc.code is ErrorCode.STALE_REF and exc.detail.get("reason") not in {
                "app_restarted",
                "document_changed",
            }:
                self._raise_if_hidden_alive(snap, ref)
            raise

    def _not_showing_error(self, ref: str, role: str, title: str) -> ComputerUseError:
        shown = f" {title!r}" if title else ""
        return ComputerUseError(
            ErrorCode.UNSUPPORTED,
            f"{ref} ({role}{shown}) is not showing",
            detail={
                "ref": ref,
                "role": role,
                "reason": "not_showing",
                "outcome": "refused",
                "next": ["foreground", "ref"],
                "evidence": f"{ref} is still in the tree but its document is not showing",
            },
        )

    def _raise_if_hidden_alive(self, snap: Snapshot, ref: str) -> None:
        """Raise ``not_showing`` when ``ref`` still exists in a hidden document.

        The handle from the snapshot that issued the ref is checked first. A
        DEFUNCT handle is not that case. When the handle itself is gone, the
        raw tree (including documents the snapshot prunes) is searched for the
        same role and name. A showing match, or no match, leaves the original
        ``stale_ref`` in place.
        """
        from a11y_computer_use import observe
        from a11y_computer_use.drivers import _atspi

        try:
            element = snap.element(ref)
        except KeyError:
            return
        handle = observe.ax_handle_for(snap.snapshot_id, ref)
        if handle is not None:
            try:
                gone = self._run(lambda: _atspi.accessible_gone(handle))
                hidden = False if gone else self._run(lambda: _atspi.hidden_web_target(handle))
            except Exception:
                return
            if not gone and hidden:
                raise self._not_showing_error(element.ref, element.role, element.title)
            if not gone:
                return
        try:
            root = self._run(lambda: _atspi.find_root(snap.app or "", snap.scope))
            if root is None:
                return
            found = self._run(lambda: _atspi.hidden_named_target(root, element.role, element.title))
        except Exception:
            return
        if found:
            raise self._not_showing_error(element.ref, element.role, element.title)

    def alive_offscreen(self, snap: Snapshot, ref: str) -> Bounds | None:
        """Bounds when ``ref`` is still alive and misses the display.

        None when the handle is missing, DEFUNCT, in a hidden document, or
        still intersects the screen. Crop uses this so a scrolled-off control
        is ``not_visible`` (the ref is still valid). Click does not call it,
        so a reordered list stays ``stale_ref``.
        """
        from a11y_computer_use import observe
        from a11y_computer_use.drivers import _atspi

        try:
            element = snap.element(ref)
        except KeyError:
            return None
        handle = observe.ax_handle_for(snap.snapshot_id, ref)
        if handle is None:
            return None
        try:
            found = self._run(lambda: _atspi.offscreen_extents(handle))
        except Exception:
            return None
        if found is None or element.bounds is None:
            return None
        pos, size = found
        return Bounds(
            element.bounds.display_id,
            int(pos[0]),
            int(pos[1]),
            max(0, int(size[0])),
            max(0, int(size[1])),
        )

    def _refuse_hidden(self, element: Element, handle) -> None:
        """Raise when ``handle`` is in a Firefox document that is not showing.

        A link in a background tab has on-screen bounds and its action
        reports success. The click did not happen on screen. GTK and
        Chromium handles are not this case. A destroyed accessible is not
        this case either: that stays a stale ref from resolution.
        """
        from a11y_computer_use.drivers import _atspi

        if handle is None:
            return
        try:
            if self._run(lambda: _atspi.accessible_gone(handle)):
                return
            hidden = self._run(lambda: _atspi.hidden_web_target(handle))
        except Exception:
            return
        if not hidden:
            return
        raise self._not_showing_error(element.ref, element.role, element.title)

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
        self._refuse_hidden(element, handle)
        # A Writer paragraph's action does not place the caret, and an
        # Impress or Draw shape's action does not enter text edit. The click
        # path does, and the outcome checks that result afterwards.
        if self._run(lambda: _atspi.is_libreoffice_text_paragraph(handle)):
            return False
        if self._run(lambda: _atspi.is_libreoffice_shape_text(handle)):
            return False
        if element.editable:
            # Remember it so type_text can enter text via EditableText, and focus
            # it (best-effort — grab_focus is cursor-free but headless X may not
            # grant real widget focus; EditableText does not need it).
            self._focused_editable = handle
            return self._run(lambda: _atspi.grab_focus(handle) or _atspi.do_press(handle) or True)
        # Fallback content inside a <canvas> is in the tree and is not painted.
        # A pointer click at its box lands on the canvas, so the control's
        # handler never runs. Activate the accessible, then the DOM node.
        # Neither path moves the pointer. Failure is an error, not a click.
        if self._run(lambda: _atspi.canvas_fallback_content(handle)):
            if self._run(lambda: _atspi.do_press(handle)):
                return True
            spec = self._run(lambda: _atspi.canvas_fallback_dom_spec(handle))
            if spec and _dom_activate_canvas_fallback(spec):
                return True
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                f"{element.ref} ({element.role} {element.title!r}) is inside a canvas "
                "and was not activated; no pointer click was sent",
                detail={
                    "ref": element.ref,
                    "role": element.role,
                    "reason": "canvas_fallback",
                    "hint": (
                        "this control is fallback content inside a canvas and is not painted; "
                        "a coordinate click would land on the canvas"
                    ),
                },
            )
        # Chrome's click action on a canvas or image has no position, so the
        # page sees offset 0,0. A pointer click uses the full AT-SPI box, or
        # the DOM box when that size is 0, and lands on the center.
        info = self._run(lambda: _atspi.coordinate_click_info(handle))
        if info is not None:
            box = _coordinate_click_box(info, element.bounds)
            if box is None:
                raise ComputerUseError(
                    ErrorCode.UNSUPPORTED,
                    f"{element.ref} ({element.role}) has no on-screen size, so the click was not sent",
                    detail={
                        "ref": element.ref,
                        "role": element.role,
                        "reason": "click_without_coordinates",
                        "hint": "AT-SPI reported no size and the DOM box was not available",
                    },
                )
            if _on_wayland():
                raise _wayland_input_error("click")
            from a11y_computer_use.drivers import _linux_input

            _linux_input.click(
                int(round(box[0] + box[2] / 2)),
                int(round(box[1] + box[3] / 2)),
            )
            return True
        # A Qt grid cell's Toggle action adds the cell and leaves currentItem
        # where it was. Selection.select_child does the same on Qt 6.11. A
        # pointer click at the cell center selects only that cell and makes
        # it current. Success is that observation. A tree item is not a grid
        # cell and stays on the row path below.
        if self._run(lambda: _atspi.qt_table_cell(handle)):
            if self._run(lambda: _atspi.activate_qt_table_cell(handle)):
                return True
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                f"{element.ref} ({element.role}) click did not select only that cell "
                "and make it current",
                detail={"ref": element.ref, "role": element.role, "reason": "selection_unchanged"},
            )
        # A GTK tree cell's activate/expand action reports success and leaves
        # the selection where it was. Select through the parent's Selection
        # interface, or click the row's on-screen center, and require the
        # selection to actually move onto this row.
        # A Chrome listbox option's ``select`` action toggles, and calling
        # Selection.select_child while that action is in flight cancels it
        # and can clear the selection that was already there. A click at the
        # row's center is what selects the row. Nothing is changed on the
        # accessibility tree before that click. If the click does not select
        # the row, the previous selection is put back.
        # An ARIA combobox option's handler sets the input and closes the
        # list. The row is gone, so it never becomes SELECTED. The combobox
        # value, its name, aria-selected, or the collapsed text is the
        # read-back. That reading is taken before the click.
        if self._run(lambda: _atspi.chromium_list_row(handle)):
            before = self._run(lambda: _atspi.selected_option_names(handle))
            choice = self._run(lambda: _atspi.combobox_option_context(handle))
            bounds = element.bounds
            if bounds is not None and bounds.width > 0 and bounds.height > 0:
                try:
                    self.click(element)
                except ComputerUseError:
                    pass
                if self._run(lambda: _atspi.row_becomes_selected(handle)):
                    return True
                if self._run(lambda: _atspi.combobox_option_committed(choice, handle)):
                    return True
            self._run(lambda: _atspi.restore_selection(handle, before))
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                f"{element.ref} ({element.role}) click did not change the selection",
                detail={"ref": element.ref, "role": element.role, "reason": "selection_unchanged"},
            )
        # A side pane, or a tree or list row that opens a folder, is not
        # selected into place. Selection only highlights the row. The row's
        # own activation is a pointer click, or activate, or Return when
        # that is the action that opens the folder. The outcome checks the
        # location or path bar afterwards.
        gesture = self._run(lambda: _atspi.navigation_gesture(handle))
        if gesture:
            self._open_folder_row(element, handle, gesture)
            return True
        # Read the toggle before the click action. Chrome publishes EXPANDED
        # a beat after doAction("click") returns, and the selection does not
        # move on a treeitem that has no aria-selected. A center click would
        # run that handler again and collapse the node. The combobox reading
        # is taken here too, before that action closes the list.
        before_toggle = self._run(lambda: _atspi.row_toggle_state(handle))
        choice = self._run(lambda: _atspi.combobox_option_context(handle))
        selected = self._run(lambda: _atspi.select_contained_row(handle))
        if selected is None:
            pressed = self._run(lambda: _atspi.do_press(handle))
            # A Chrome <select> option's click action changes the value and
            # leaves the native popup open. Close it the way set_value does,
            # and only when this item belongs to that select.
            if pressed:
                self._run(lambda: _atspi.finish_chrome_select_option_click(handle))
                self._run(lambda: _atspi.finish_gtk_combo_option_click(handle))
            return pressed
        if selected:
            return True
        if self._run(lambda: _atspi.combobox_option_committed(choice, handle)):
            return True
        # Only a row whose click or toggle action already ran. A GTK expand
        # action is not that, and still falls through to the center click.
        if self._run(lambda: _atspi.row_toggle_blocks_pointer(handle)):
            if self._run(lambda: _atspi.row_toggle_committed(before_toggle, handle)):
                return True
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                f"{element.ref} ({element.role}) click did not change the selection",
                detail={"ref": element.ref, "role": element.role, "reason": "selection_unchanged"},
            )
        bounds = element.bounds
        if bounds is not None and bounds.width > 0 and bounds.height > 0:
            try:
                self.click(element)
            except ComputerUseError:
                if self._run(lambda: _atspi.row_is_selected(handle)):
                    return True
                if self._run(lambda: _atspi.combobox_option_committed(choice, handle)):
                    return True
                raise
            if self._run(lambda: _atspi.row_is_selected(handle)):
                return True
            if self._run(lambda: _atspi.combobox_option_committed(choice, handle)):
                return True
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            f"{element.ref} ({element.role}) click did not change the selection",
            detail={"ref": element.ref, "role": element.role, "reason": "selection_unchanged"},
        )

    def _open_folder_row(self, element: Element, handle, gesture: str) -> None:
        """Open a side-pane or navigating row by that row's own activation.

        A pointer click is the single click that opens a shortcut cell.
        ``activate`` runs only when that action is the row's activation, not
        the expand or edit action ahead of it. Return is sent only when the
        row has no box and no activate action. The caller checks that the
        location or path bar changed.
        """
        from a11y_computer_use.drivers import _atspi

        if gesture == "activate":
            self._run(lambda: _atspi.do_named_action(handle, "activate"))
            return
        if gesture == "return":
            self._run(lambda: _atspi.select_contained_row(handle))
            self._run(lambda: _atspi.grab_focus(handle))
            if _on_wayland():
                raise _wayland_input_error("click")
            from a11y_computer_use.drivers import _linux_input

            _linux_input.press_chord("Return")
            return
        bounds = element.bounds
        if bounds is None or bounds.width <= 0 or bounds.height <= 0:
            return
        self.click(element)

    def scroll_into_view(self, element: Element) -> bool:
        from a11y_computer_use import observe
        from a11y_computer_use.drivers import _atspi

        handle = observe.ax_handle_for(element.snapshot_id, element.ref)
        if handle is None:
            return False
        return self._run(lambda: _atspi.scroll_to(handle))

    def _refuse_changed_peers(self, handle, peers: dict, element: Element, exc: ComputerUseError) -> None:
        """After a failed set_value, another field's text must be unchanged.

        A ``text_mismatch`` that also changed a different field is reported as
        ``focus_lost``. A ``focus_lost`` that already stopped the keys records
        the fields that did change.
        """
        if handle is None or not peers:
            return
        from a11y_computer_use.drivers import _atspi

        try:
            changed = self._run(lambda: _atspi.changed_peer_fields(handle, peers))
        except Exception:
            return
        if not changed:
            return
        if exc.detail.get("reason") == "focus_lost":
            exc.detail["changed_fields"] = changed[:8]
            return
        raise ComputerUseError(
            ErrorCode.FOCUS_LOST,
            "set_value stopped because another field changed",
            detail={
                "ref": element.ref,
                "role": element.role,
                "reason": "focus_lost",
                "outcome": "refused",
                "next": ["ref", "cdp"],
                "changed_fields": changed[:8],
                "evidence": "another field changed during set_value",
            },
        ) from exc

    def set_value(self, element: Element, value: str) -> bool:
        from a11y_computer_use import observe
        from a11y_computer_use.drivers import _atspi

        if element.secure:
            self._focused_editable = None
            return False
        self._focused_editable = None
        handle = observe.ax_handle_for(element.snapshot_id, element.ref)
        peers: dict = {}
        if handle is not None:
            try:
                peers = self._run(lambda: _atspi.peer_field_texts(handle)) or {}
            except Exception:
                peers = {}
        try:
            return self._apply_set_value(element, value, handle)
        except ComputerUseError as exc:
            self._refuse_changed_peers(handle, peers, element, exc)
            raise

    def _apply_set_value(self, element: Element, value: str, handle) -> bool:
        from a11y_computer_use.drivers import _atspi

        if handle is not None and self._run(lambda: _atspi.is_sheet_cell(handle)):
            # A Writer table cell is named like a Calc address, but typing
            # into it inserts a paragraph and the cell node reads back empty.
            # Replace the child paragraph, and put the old text back on a miss.
            if self._run(lambda: _atspi.writer_text_cell(handle)):
                self._run(lambda: _atspi.replace_writer_cell_text(handle, value))
                return True
            # Not the Value interface: that range is a double and rejects text.
            # Focus, type (a selected cell replaces), commit with Return, then
            # the cell text or the formula attribute has to match.
            self._write_sheet_cell(handle, value)
            return True
        if handle is not None:
            kind = self._run(lambda: _atspi.control_kind(handle))
            if kind == "combo":
                # Selection or this combo's own entry. A miss raises before
                # any keystroke, so the Runtime cannot type into a different
                # focused field or leave the popup open.
                self._run(lambda: _atspi.set_combo_value(handle, value))
                return True
            if kind == "value":
                wrote = self._run(lambda: _atspi.set_numeric_value(handle, value))
                if wrote:
                    # A snapped spin button returns the text of the value held
                    # ("5" for a request of "4.6") so the result is not the
                    # number the adjustment rejected.
                    return wrote if isinstance(wrote, str) else True
                if not _accepts_text(element):
                    raise ComputerUseError(
                        ErrorCode.UNSUPPORTED,
                        f"{element.ref} ({element.role}) has no Value interface",
                        detail={"ref": element.ref, "role": element.role, "reason": "text_mismatch"},
                    )
        # A menu, heading, label, button, browser paragraph, or select is not
        # a text target. Raising here is what stops the Runtime from focusing
        # it and typing the value into whatever is frontmost. No key, click,
        # or focus is sent. The snapshot flag is not enough: Firefox reports
        # STATE_EDITABLE and EditableText on static nodes, and selecting one
        # selects the page. The live node has to be an editable entry. An
        # Impress or Draw paragraph with that state and EditableText is one.
        if not _accepts_text(element):
            raise _not_editable(element)
        if handle is None:
            return False
        # A synthetic field has no AT-SPI role and stays on set_text. A
        # paragraph, panel, document, combo, or read-only node is refused
        # here, before set_text can select it. A contenteditable section and
        # a Chrome date segment are not refused.
        if self._run(lambda: _atspi._blocks_text_replace(handle)):
            raise _not_editable(element)
        # EditableText replace, or X11 clear-and-type when that interface is
        # missing. Success is the snapshot text read (Text.get_text 0, -1),
        # not a bounded read that can echo the request. A write that does not
        # stick raises: returning False made the Runtime type into whatever
        # was focused and still report success. Marshaled onto the a11y thread.
        success = self._run(lambda: _atspi.set_text(handle, value))
        if not success:
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                f"the value read back does not match {value!r}",
                detail={"ref": element.ref, "role": element.role, "reason": "text_mismatch"},
            )
        self._focused_editable = handle
        return True

    # -- act (AT-SPI XTEST event generation) --------------------------------
    def _pointer(self, x: int, y: int) -> tuple[int, int]:
        """Screen point for an XTEST event.

        Published LibreOffice bounds stay on the pixels. On 24.2 the hit
        test sits one title bar above those pixels, so the event moves by
        that inset. A point outside LibreOffice is unchanged.
        """
        from a11y_computer_use.drivers import _atspi

        try:
            dx, dy = self._run(lambda: _atspi.libreoffice_pointer_shift(int(x), int(y)))
        except Exception:
            return int(x), int(y)
        return int(x) + int(dx), int(y) + int(dy)

    def _note_pointer(self, x: int, y: int) -> None:
        """The pointer event at the published point has been sent.

        LibreOffice 24.2 then republishes extents in hit-test space. Noting
        the original point, not the shifted one, keeps the next event from
        adding the title bar a second time.
        """
        from a11y_computer_use.drivers import _atspi

        try:
            self._run(lambda: _atspi.note_libreoffice_pointer(int(x), int(y)))
        except Exception:
            return

    def click(self, target: Target, *, button: MouseButton = MouseButton.LEFT, count: int = 1,
              modifiers: tuple[str, ...] = (), pre_check: Callable | None = None,
              dry_run: bool = False) -> object:
        if dry_run:
            return None
        if _on_wayland():
            raise _wayland_input_error("click")
        from a11y_computer_use.drivers import _atspi, _linux_input

        self._focused_editable = None
        handle = None
        if isinstance(target, Element):
            from a11y_computer_use import observe

            handle = observe.ax_handle_for(target.snapshot_id, target.ref)
            self._refuse_hidden(target, handle)
        x, y = _point_of(target)
        # A plain left click on an Impress or Draw shape or text frame is a
        # pointer click at the element's center. A single click that does
        # not enter text edit is followed by a double-click. Writer text
        # stays on the caret shortcut below. A crop uses the published
        # point as the pixel rectangle.
        if (
            handle is not None
            and button is MouseButton.LEFT
            and not modifiers
            and self._run(lambda: _atspi.is_libreoffice_shape_text(handle))
        ):
            self._click_shape_text(handle, x, y, count)
            return None
        # A plain left single-click on a Writer paragraph places the caret
        # in that paragraph. A double-click uses the published point, shifted
        # onto the hit-test on LibreOffice 24.2.
        if (
            handle is not None
            and button is MouseButton.LEFT
            and count == 1
            and not modifiers
            and self._run(lambda: _atspi.place_paragraph_caret(handle, x, y))
        ):
            self._focused_editable = handle
            return None
        px, py = self._pointer(x, y)
        with _linux_input.held(modifiers):
            _linux_input.click(px, py, button=_BUTTON_NAME.get(button, "left"), count=count)
        self._note_pointer(x, y)
        # A double-click does not place the caret through the text
        # interface. When the pointer did land in this paragraph, the
        # following type inserts there. A miss leaves the caret elsewhere
        # and does not retarget the type.
        if handle is not None and self._run(
            lambda: _atspi.paragraph_click_verdict(handle)
        ) == ("confirmed", "the caret is in the target paragraph"):
            self._focused_editable = handle
        return None

    def _click_shape_text(self, handle, x: int, y: int, count: int) -> None:
        """Click the center of an Impress or Draw text frame, then enter edit.

        The published center is shifted the same way a coordinate click is.
        One click is enough when the frame starts editing. Otherwise a
        double-click follows. The following type is aimed at this frame
        only after that edit is visible.
        """
        from a11y_computer_use.drivers import _linux_input

        px, py = self._pointer(x, y)
        clicks = max(1, int(count))
        _linux_input.click(px, py, count=clicks)
        if clicks == 1 and not self._shape_text_ready(handle):
            _linux_input.click(px, py, count=2)
        self._note_pointer(x, y)
        if self._shape_text_ready(handle):
            self._focused_editable = handle

    def _shape_text_ready(self, handle) -> bool:
        """True once the frame shows a focused paragraph or a caret."""
        from a11y_computer_use.drivers import _atspi

        deadline = time.monotonic() + 0.6
        while True:
            try:
                ready = bool(self._run(lambda: _atspi.shape_text_editing(handle)))
            except Exception:
                ready = False
            if ready or time.monotonic() >= deadline:
                return ready
            time.sleep(0.05)

    def hover(self, target: Target, *, dry_run: bool = False) -> object:
        """Move the pointer onto ``target`` and deliver a hover. No button."""
        if dry_run:
            return None
        if _on_wayland():
            raise _wayland_input_error("hover")
        from a11y_computer_use.drivers import _linux_input

        self._focused_editable = None
        x, y = _point_of(target)
        px, py = self._pointer(x, y)
        _linux_input.hover(px, py)
        self._note_pointer(x, y)
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
        # One delta for the whole gesture. Noting after the first point
        # would drop the inset from the end and the path.
        x1, y1 = _point_of(start)
        sx, sy = self._pointer(x1, y1)
        dx, dy = sx - int(x1), sy - int(y1)
        x2, y2 = _point_of(end)
        moved = [(_point_of(p)[0] + dx, _point_of(p)[1] + dy) for p in path]
        _linux_input.drag(
            sx, sy, int(x2) + dx, int(y2) + dy,
            button=_BUTTON_NAME.get(button, "left"), path=moved,
        )
        self._note_pointer(x1, y1)
        return None

    def scroll_offset(self, target: Element) -> float | None:
        """Vertical offset of the scroll container that holds ``target``.

        None when that container has no readable bar.
        """
        from a11y_computer_use import observe
        from a11y_computer_use.drivers import _atspi

        try:
            handle = observe.ax_handle_for(target.snapshot_id, target.ref)
        except Exception:
            return None
        if handle is None:
            return None
        try:
            value = self._run(lambda: _atspi.scroll_offset(handle))
        except Exception:
            return None
        return float(value) if isinstance(value, (int, float)) else None

    def latch_chromium_find(self, app: str) -> bool:
        """Remember this Chrome process before the post-find snapshot.

        Ctrl+F closes the bar before that snapshot can see the Find field.
        Without the pid, the next walk keeps the find frame or an empty
        document, and a wheel on that frame does not move the page.
        True when a Chrome pid was recorded.
        """
        from a11y_computer_use.drivers import _atspi

        return bool(self._run(lambda: _atspi.latch_chromium_find(app)))

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
        keeps its own center. XTEST notches at that center do not move a
        Chrome document. When the window's process was started with
        ``--remote-debugging-port``, a line step is a DOM ``scrollBy`` of
        about 40 pixels per line, kept only when ``scrollTop`` changes.
        Otherwise the requested notches are sent together. That burst is
        about a viewport, and the call returns as soon as the on-screen
        head shows it. One notch at a time, stopped at about ``|dy|``
        names, made each step about four rows and about two seconds. On a
        busy runner the same burst can collapse into one row; notches are
        then added one at a time until the head has moved by about the
        requested number of names. A head that has already left the
        sampled names is not given those extra notches.
        ``unit=pixels`` writes the AT-SPI scroll-bar value
        by that delta and reads it back when a bar is exposed. GTK scrolled
        windows expose the value in pixels. A missing bar tries a CDP DOM
        scroll of the document when the window's process was started with
        ``--remote-debugging-port``, and checks that ``scrollTop`` changed.
        Otherwise it sends wheel notches at the element's on-screen box
        (about one notch per 80 pixels, at most 24) and checks that the
        pixels in that box changed. A step that cannot be verified, or that
        does not move, raises `unsupported` and is not reported as a scroll.
        Wayland still has the bar path; the wheel fallback needs XTEST.
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
            anchored = _anchor_scroll_point(target, handle, x, y)
            if anchored is not None:
                x, y = anchored
            # Chrome's document is a group, not a list, and a DevTools port
            # scrolls it. A GTK window has no port and keeps the notch.
            # Either way the Chromium tree trails the paint, so the search
            # waits for the on-screen names before the next snapshot.
            if not _cdp_scroll_pixels(
                x, y,
                dx=int(dx) * _PIXELS_PER_LINE,
                dy=int(dy) * _PIXELS_PER_LINE,
            ):
                _wheel_chromium_document(self._run, handle, x, y, dx=int(dx), dy=int(dy))
            elif handle is not None:
                _settle_shown_names(self._run, handle)
            return None
        box = self._run(lambda: _atspi.list_screen_box(container))
        raw_box = box
        ancestor_box = self._run(lambda: _atspi.overflow_ancestor_box(container))
        # A short body-scroll table is content, not a scrollport. It can
        # fit on a tall screen and still run past the document, and after
        # the page moves its top is above the document. The bar, the track,
        # and a list-judged wheel do not move it, and that stall is reported
        # as unchanged while the document can still scroll. Hover first so
        # the wheel lands on the focused page.
        if (
            ancestor_box is None
            and raw_box is not None
            and int(dy)
            and self._run(lambda: _atspi.scroll_container_kind(container)) == "document"
        ):
            display_id = target.display_id if isinstance(target, Point) else target.bounds.display_id
            self.hover(Point(display_id, x, y))
            if not _cdp_scroll_pixels(
                x, y,
                dx=int(dx) * _PIXELS_PER_LINE,
                dy=int(dy) * _PIXELS_PER_LINE,
            ):
                _wheel_chromium_document(self._run, handle, x, y, dx=int(dx), dy=int(dy))
            elif handle is not None:
                _settle_shown_names(self._run, handle)
            return None
        if ancestor_box is not None:
            painted = _clip_box_to_screen(ancestor_box)
            box = painted if painted is not None else ancestor_box
        elif box is not None:
            box = _clip_box_to_screen(box)
        if box is None:
            _linux_input.scroll(x, y, dx=dx, dy=dy)
            return None
        display_id = target.display_id if isinstance(target, Point) else target.bounds.display_id
        try:
            offset_before = self._run(lambda: _atspi.scroll_offset(container))
        except Exception:
            offset_before = None
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
        try:
            offset_after = self._run(lambda: _atspi.scroll_offset(container))
        except Exception:
            offset_after = None
        # A bar that moved is a scroll, even when the painted rows have not
        # caught up. page_unchanged is only a bar that was read twice and
        # stayed put. A list with no readable bar still uses the painted
        # rows: that list is the scrollport, and there is no other offset.
        if _offset_did_not_move(offset_before, offset_after) is False:
            return None
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
                "scroll_position": offset_after if offset_after is not None else offset_before,
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
        if _cdp_scroll_pixels(x, y, dx=dx, dy=dy):
            return
        if _on_wayland():
            raise _pixel_scroll_unsupported(dx, dy)
        box = _pixel_scroll_box(target)
        if box is None:
            raise _pixel_scroll_unsupported(dx, dy)
        try:
            before = _grab_region(box)
        except ComputerUseError:
            raise _pixel_scroll_unsupported(dx, dy) from None
        from a11y_computer_use.drivers import _linux_input

        _linux_input.scroll(
            box[0] + box[2] // 2, box[1] + box[3] // 2,
            dx=_pixel_notches(dx), dy=_pixel_notches(dy),
        )
        mean, _samples = _list_pixels_moved(before, box)
        if mean is not None and mean > _atspi._PAGE_MOVE_MEAN:
            return
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            "pixel scroll did not move the page",
            detail={
                "reason": "page_unchanged",
                "unit": "pixels",
                "dx": dx,
                "dy": dy,
                "mean_abs": mean,
                "api": "XTEST wheel at the element box",
            },
        )

    def type_into(self, element: Element, text: str) -> int:
        """Type ``text`` into ``element`` only after focus is confirmed on it.

        ``grab_focus`` runs first. A later read has to show keyboard focus on
        that accessible, or on a descendant inside it. Anything else raises
        ``focus_lost`` and does not insert and does not send keystrokes, so
        the characters cannot land in a different field. A confirmed field
        is written through EditableText on that accessible. When that
        interface is missing or leaves the field unchanged, keystrokes are
        sent only while focus stays on it, and the read-back is that same
        accessible.
        """
        from a11y_computer_use import observe
        from a11y_computer_use.drivers import _atspi

        self._focused_editable = None
        if element.secure:
            raise _secure_focus_error("the requested ref is a password field")
        handle = observe.ax_handle_for(element.snapshot_id, element.ref)
        if handle is None:
            raise self._focus_lost_on(element)
        self._refuse_hidden(element, handle)
        if not _accepts_text(element) or self._run(lambda: _atspi._blocks_text_replace(handle)):
            raise _not_editable(element)
        if self._run(lambda: _atspi.drawing_text_editable(handle)):
            # The paragraph often has no Component. Focus lands on it or on
            # the shape panel, and EditableText inserts on the paragraph.
            # A paragraph that was not already editing reports caret 0. Move
            # it to the end, including after the grab, so the insert appends.
            # A paragraph that already has focus keeps the caret the user set.
            editing = self._run(lambda: _atspi._state_has(handle, "FOCUSED"))
            if not editing:
                self._run(lambda: _atspi.place_drawing_caret_at_end(handle))
            if not self._run(lambda: _atspi.focus_drawing_text(handle)):
                raise self._focus_lost_on(element)
            if not editing:
                self._run(
                    lambda: _atspi.place_drawing_caret_at_end(handle, ignore_focus=True)
                )
        else:
            self._run(lambda: _atspi.grab_focus(handle))
            if not self._run(lambda: _atspi._confirm_focus_on_target(handle)):
                raise self._focus_lost_on(element)
        self._focused_editable = handle
        peers: dict = {}
        try:
            peers = self._run(lambda: _atspi.peer_field_texts(handle)) or {}
        except Exception:
            peers = {}
        try:
            count = self._type_confirmed_handle(handle, text)
        except ComputerUseError as exc:
            self._refuse_changed_peers(handle, peers, element, exc)
            raise
        self._raise_if_peers_changed(handle, peers, element)
        return count

    def _focus_lost_on(self, element: Element) -> ComputerUseError:
        return ComputerUseError(
            ErrorCode.FOCUS_LOST,
            f"focus is not confirmed on {element.ref}; nothing was typed",
            detail={
                "ref": element.ref,
                "role": element.role,
                "reason": "focus_lost",
                "outcome": "refused",
                "next": ["ref", "cdp"],
                "evidence": "focus did not land on the target, so no keystrokes were sent",
            },
        )

    def _raise_if_peers_changed(self, handle, peers: dict, element: Element) -> None:
        """After a type that reported success, another field must be unchanged."""
        if handle is None or not peers:
            return
        from a11y_computer_use.drivers import _atspi

        try:
            changed = self._run(lambda: _atspi.changed_peer_fields(handle, peers))
        except Exception:
            return
        if not changed:
            return
        raise ComputerUseError(
            ErrorCode.FOCUS_LOST,
            "type stopped because another field changed",
            detail={
                "ref": element.ref,
                "role": element.role,
                "reason": "focus_lost",
                "outcome": "refused",
                "next": ["ref", "cdp"],
                "changed_fields": changed[:8],
                "evidence": "another field changed during type",
            },
        )

    def _type_confirmed_handle(self, handle, text: str) -> int:
        """Insert ``text`` into a handle whose focus was already confirmed."""
        from a11y_computer_use.drivers import _atspi

        text = text.replace("\r\n", "\n")
        if not text:
            return 0
        # Writer keeps the lines in paragraph children. The document node's
        # own text stays empty, so the read-back is those paragraphs,
        # captured before the insert.
        writer_doc = self._run(lambda: _atspi.writer_document_for(handle))
        writer_before = ""
        if writer_doc is not None:
            writer_before = self._run(lambda: _atspi.writer_document_text(writer_doc)) or ""
        before = self._run(lambda: _atspi._readable_text(handle))
        try:
            inserted = self._run(lambda: _atspi.insert_text(handle, text))
        except ComputerUseError as exc:
            detail = exc.detail or {}
            if detail.get("reason") == "text_mismatch" and detail.get("unchanged"):
                inserted = None
            elif detail.get("reason") == "selection_not_replaced" and self._run(
                lambda: _atspi.is_location_entry(handle)
            ):
                app_id, _pid = self.frontmost_app()
                return self._replace_location_selection(handle, text, app_id)
            else:
                raise
        if isinstance(inserted, int) and not isinstance(inserted, bool):
            return inserted
        if inserted:
            return len(text)
        if not self._run(lambda: _atspi._x11_keys_available()):
            raise _wayland_input_error("type")
        self._run(lambda: _atspi._type_into_target(handle, text))
        chrome = bool(self._run(lambda: _atspi._chromium_app(handle)))
        after = _atspi._poll_typed_text(
            lambda: self._run(lambda: _atspi._readable_text(handle)),
            before,
            text,
            chrome=chrome,
        )
        if _atspi._typed_visible(before, after, text, chrome=chrome):
            if chrome:
                # The outcome compares this string. A paragraph boundary is
                # a newline here, so the judge does not require the same
                # separator character the field stored.
                self._type_expected = _atspi._field_text_for_type(after, text)
            return len(text)
        if writer_doc is not None:
            shown = self._run(
                lambda: _atspi.poll_writer_document(writer_doc, writer_before, text)
            )
            if _atspi._writer_typed_visible(writer_before, shown, text):
                return len(text)
            after = shown if shown is not None else after
        raise _atspi._text_mismatch(
            "text_mismatch",
            f"the text read back does not contain {text!r}",
            expected=text,
            actual=after,
        )

    def type_text(self, text: str, *, pre_check: Callable | None = None,
                  dry_run: bool = False) -> object:
        """Enter ``text`` into the focused editable.

        Primary path: AT-SPI EditableText. A ref click remembers the element.
        A coordinate click does not, so this looks up the focused accessible
        of the frontmost app and uses the same `insert_text` helper: UTF-8
        byte length for GTK, character length for Qt, insert at the caret
        (a character offset), replace a selection, and return the character
        count read back. A Qt field also has to report that same character
        count, because a NUL in the widget truncates the D-Bus string. A
        mismatch raises instead of reporting success. When EditableText
        returns success and the field text does not change (Firefox web
        entries), the field is focused and the same text is sent as key
        events, and that field is read back. Falls back to synthetic XTEST
        keystrokes when that lookup finds no EditableText (Chrome's ATK
        objects, or a click that focused nothing editable). A CRLF is one
        newline on both paths.
        """
        if dry_run or not text:
            return None
        self._chooser_readback = None
        self._type_expected = None
        self._sheet_type_origin = None
        self._drawing_type_before = None
        self._drawing_type_state = None
        self._type_unverified = False
        text = text.replace("\r\n", "\n")
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
        else:
            app_id, _pid = self.frontmost_app()
            if app_id:
                handle = self._run(lambda: _atspi.focused_editable(app_id))
                if handle is not None and self._run(lambda: _atspi.is_secure(handle)):
                    raise _secure_focus_error(
                        "AT-SPI STATE_FOCUSED on a 'password text' node"
                    )
        if app_id:
            # The find field latches this process even when the open bar is
            # not in a snapshot. scroll_to_find types and closes it first.
            self._run(lambda: _atspi.note_focused_find_bar(app_id))
        if (
            handle is not None
            and ("\t" in text or "\n" in text)
            and self._run(
                lambda: _atspi.is_sheet_cell(handle) and not _atspi.writer_text_cell(handle)
            )
        ):
            # Tab and Return leave the cell. insertText would keep the grid
            # in this cell, and the read-back would only see that cell.
            handle = None
        if (
            handle is not None
            and "\n" in text
            and self._run(lambda: _atspi.is_drawing_text_paragraph(handle))
        ):
            # Return starts the next paragraph in an Impress or Draw frame.
            # insertText of the newline stays in this paragraph and drops the
            # rest. The keys already do that split. The frame's paragraphs
            # are the read-back.
            handle = None
        if app_id and _atspi.libreoffice_app(app_id):
            status, shown = self._run(lambda: _atspi.drawing_focus_text(app_id))
            if status != "skip":
                self._drawing_type_state = status
                self._drawing_type_before = "" if shown is None else shown
        if handle is not None:
            try:
                inserted = self._run(lambda: _atspi.insert_text(handle, text))
            except ComputerUseError as exc:
                detail = exc.detail or {}
                # Firefox selects the whole urlbar on ctrl+l and delete_text
                # does not clear that selection. Keystrokes replace it. A page
                # field, including a contenteditable, still raises.
                if detail.get("reason") == "selection_not_replaced" and self._run(
                    lambda: _atspi.is_location_entry(handle)
                ):
                    return self._replace_location_selection(handle, text, app_id)
                # EditableText claimed the insert and the field did not change.
                # Focus it and send the keys, then read this field back.
                if detail.get("reason") == "text_mismatch" and detail.get("unchanged"):
                    if self._run(lambda: _atspi.focus_and_type_into(handle, text)):
                        return len(text)
                    # The pre-key error still says the field was unchanged.
                    # Read it again. NBSP and U+FFFC are not the comparison.
                    after = self._run(lambda: _atspi._readable_text(handle))
                    before = detail.get("before")
                    same = _atspi._norm_nbsp(after) == _atspi._norm_nbsp(
                        before if isinstance(before, str) else None
                    )
                    raise _atspi._text_mismatch(
                        "text_mismatch",
                        "the field text after type does not match what was inserted",
                        expected=text,
                        actual=after,
                        inserted_chars=len(text),
                        unchanged=bool(same),
                    )
                raise
            if isinstance(inserted, int) and not isinstance(inserted, bool):
                return inserted
            if inserted:
                return len(text)
            # Chrome's contenteditable has no EditableText. insert_text
            # returns None without sending keys. Type, then poll: the first
            # AT-SPI read is often still the old text. A settled read that
            # lacks the characters is text_mismatch. Inputs and GTK do not
            # take this branch.
            landed = self._run(lambda: _atspi.chromium_contenteditable_type(handle, text))
            if landed is True:
                expected = getattr(_atspi.chromium_contenteditable_type, "last_expected", None)
                if isinstance(expected, str):
                    self._type_expected = expected
                return len(text)
            if landed is False:
                after = self._run(lambda: _atspi._readable_text(handle))
                raise _atspi._text_mismatch(
                    "text_mismatch",
                    f"the text read back does not contain {text!r}",
                    expected=text,
                    actual=after,
                )
        if _on_wayland():  # a11y path unavailable and XTEST can't reach Wayland apps
            raise _wayland_input_error("type_text (no focused editable for the a11y path)")
        # The XTEST path types into whatever holds keyboard focus, so probe the
        # focused node of the frontmost app first (the Linux analog of macOS's
        # AXFocusedUIElement check): a password field there refuses the typing.
        self._refuse_xtest_password_focus()
        from a11y_computer_use.drivers import _linux_input

        app_id, _pid = self.frontmost_app()
        # Only a browser has an address bar. Asking LibreOffice for one walks
        # Collection and drops the cell handles a later set_value still holds.
        browser = _named_browser(app_id)
        location = self._location_entry(app_id) if app_id and browser else None
        # ctrl+l can leave a11y focus on the omnibox popup. The keys still
        # land in the address bar. The popup's text is not that URL.
        bar_before = None
        if location is not None:
            before = self._run(lambda: _atspi._readable_text(location))
        elif browser and self._run(lambda: _atspi.focus_in_browser_chrome(app_id)):
            bar_before = self._run(lambda: _atspi.address_bar_text(app_id))
            before = bar_before
        else:
            # LibreOffice verifies against the open cell editor. Every other
            # app uses the focused node's text.
            before = self._typed_readback(app_id)
        # A Writer document's own text is empty. The paragraphs are the
        # read-back, taken before the keys so a later line is not missed.
        writer_before = None
        # Impress and Draw keep the document focused. Its text stays empty
        # while the paragraph inside the text frame holds the characters.
        # That paragraph is the read-back, captured before the keys.
        shape_before = None
        if app_id and location is None and bar_before is None and _atspi.libreoffice_app(app_id):
            writer_before = self._run(lambda: _atspi.writer_type_before(app_id))
            if writer_before is None:
                shape_before = self._run(lambda: _atspi.focused_shape_edit_text(app_id))
        # A contenteditable selection has to be read before the keys replace
        # it. The expected result is that range replaced, not the old text
        # with the typed string appended.
        selection = None
        editor_before = None
        editor_selection = None
        if location is None and bar_before is None and app_id and not _atspi.libreoffice_app(app_id):
            def _spans():
                # No AT-SPI binding (macOS and Windows hermetic runs) is not a
                # selection. The key path still types. A missing bus is the same.
                try:
                    acc, truncated = _atspi._focused_node(app_id)
                    focused_span = None
                    if not truncated and acc is not None:
                        focused_span = _atspi._selection_span(acc, before)
                    editor = _atspi._focused_contenteditable(app_id)
                    editor_text = _atspi._readable_text(editor) if editor is not None else None
                    editor_span = (
                        _atspi._selection_span(editor, editor_text) if editor is not None else None
                    )
                    return focused_span, editor_text, editor_span
                except Exception:
                    return None, None, None

            selection, editor_before, editor_selection = self._run(_spans)
        # Chrome's omnibox drops the tail of a URL at the default key pace.
        # A contenteditable and a Calc cell keep that pace.
        if (
            app_id
            and location is None
            and bar_before is None
            and ("\t" in text or "\n" in text)
            and _atspi.libreoffice_app(app_id)
            and self._run(lambda: _atspi.spreadsheet_open(app_id))
        ):
            origin = self._run(
                lambda: _atspi.selected_sheet_address(app_id) or _atspi._focused_sheet_address(app_id)
            )
            if isinstance(origin, str) and origin:
                self._sheet_type_origin = origin
        if location is not None or bar_before is not None:
            _linux_input.type_string(text, delay=0.05)
        else:
            _linux_input.type_string(text)
        if location is not None:
            return self._location_read_back(location, before, text, app_id)
        if self._sheet_type_origin:
            origin = self._sheet_type_origin
            # Wait for the committed cells. Then leave the caret on the
            # cell Enter moves to, not on the last cell the read opened.
            mismatch = self._run(lambda: _atspi.sheet_grid_check(app_id, origin, text))
            if mismatch is None:
                return len(text)
            self._sheet_type_origin = None
            address, wanted, actual = mismatch
            raise _atspi._text_mismatch(
                "text_mismatch",
                f"the text read back from {address} does not match {wanted!r}",
                expected=wanted,
                actual=actual,
                cell=address,
            )
        # A Writer newline is Return, which this key path already sends. The
        # paragraphs are the read-back. One paragraph that holds the same
        # characters as line breaks is not a match. A sheet is not this.
        if app_id and "\n" in text and _atspi.libreoffice_app(app_id):
            writer = self._run(lambda: _atspi.focused_writer_document(app_id))
            if writer is not None and not self._run(lambda: _atspi.spreadsheet_open(app_id)):
                if self._run(lambda: _atspi.writer_type_landed(writer, text)):
                    return len(text)
                if self._run(lambda: _atspi.writer_type_conflict(writer, text)):
                    raise _atspi._text_mismatch(
                        "text_mismatch",
                        "inserted as line breaks",
                        expected=text,
                        actual=self._run(lambda: _atspi.writer_type_readback(writer)),
                    )
        if self._drawing_type_state not in (None, "skip"):
            status, shown = self._run(
                lambda: _atspi.poll_drawing_type(app_id, self._drawing_type_before, text)
            )
            self._drawing_type_state = status
            if status == "text" and _atspi.drawing_typed_visible(
                self._drawing_type_before, shown, text,
            ):
                return len(text)
            if status == "unreadable":
                # The container's empty text is not the frame. Reporting it
                # as a mismatch makes the caller type the same string again.
                self._type_unverified = True
                return len(text)
            if status == "text":
                raise _atspi._text_mismatch(
                    "text_mismatch",
                    f"the text read back does not contain {text!r}",
                    expected=text,
                    actual=shown,
                )
        after = self._typed_readback(app_id)
        if selection and _atspi._typed_visible(before, after, text, selection=selection):
            self._type_expected = _atspi._matched_replacement(before, after, text, selection)
        # A Chrome contenteditable can publish the keys after that first
        # read. Poll until the text settles. The address bar does too: the
        # first read can be a truncated URL. Calc is the same lag: the cell
        # editor and the cell value publish after the keys, and one empty
        # read is not a failed type. A sheet that still does not show the
        # text is a mismatch. Any other focused control keeps the single
        # read. No readable text is still not a mismatch.
        if (
            app_id
            and _atspi.libreoffice_app(app_id)
            and not _atspi._typed_visible(before, after, text)
        ):
            sheet = bool(self._run(lambda: _atspi.spreadsheet_open(app_id)))
            after = _atspi.poll_sheet_type(
                lambda: self._run(lambda: _atspi.sheet_editor_text(app_id)),
                lambda: self._run(lambda: _atspi.selected_sheet_text(app_id)),
                lambda: self._run(lambda: _atspi.focused_text(app_id)),
                before,
                text,
                sheet=sheet,
            )
            if _atspi._typed_visible(before, after, text):
                return len(text)
            if sheet:
                raise _atspi._text_mismatch(
                    "text_mismatch",
                    f"the text read back does not contain {text!r}",
                    expected=text,
                    actual=after,
                )
            # Not a sheet. Writer publishes one paragraph at a time, and the
            # focused node's text stays empty. The same settle set_value uses
            # confirms the lines. A document that is not Writer leaves
            # writer_before unset.
            if writer_before is not None:
                shown = self._run(
                    lambda: _atspi.poll_focused_writer_type(app_id, writer_before, text)
                )
                if _atspi._writer_typed_visible(writer_before, shown, text):
                    return len(text)
                if shown is not None:
                    after = shown
            elif shape_before is not None:
                shown = self._run(
                    lambda: _atspi.poll_focused_shape_type(app_id, shape_before, text)
                )
                if _atspi._typed_visible(shape_before, shown, text):
                    return len(text)
                if shown is not None:
                    after = shown
        if (
            app_id
            and after is not None
            and not _atspi.libreoffice_app(app_id)
            and not _atspi._typed_visible(before, after, text, selection=selection)
        ):
            focused = self._run(lambda: _atspi._focused_contenteditable(app_id))
            if focused is not None:
                # Sleep between reads on this thread. Each read is its own
                # AT-SPI call, so the a11y thread is not held for the wait.
                # The selection was taken before the keys. When focus sits on
                # a child text node, the editor read-back is the parent, and
                # that parent's span is the one that was captured.
                poll_before = before
                poll_selection = selection
                if editor_selection is not None and editor_before not in (None, before):
                    poll_before = editor_before
                    poll_selection = editor_selection
                after = _atspi._poll_typed_text(
                    lambda: self._run(lambda: _atspi._readable_text(focused)),
                    poll_before,
                    text,
                    chrome=True,
                    selection=poll_selection,
                )
                if _atspi._typed_visible(
                    poll_before, after, text, chrome=True, selection=poll_selection,
                ):
                    self._type_expected = _atspi._matched_replacement(
                        poll_before, after, text, poll_selection, chrome=True,
                    ) or _atspi._field_text_for_type(after, text)
                    return len(text)
            elif self._run(lambda: _atspi.focused_chrome_rewrite(app_id)) is not None:
                # The address bar publishes a short URL while suggestions
                # settle, then the whole string. Poll that field. A settled
                # value that changed and still is not the request is Chrome's
                # own formatting: not a hard mismatch. The outcome judge
                # reads whatever the field shows. The find bar that already
                # equals the typed text is a match above and does not poll.
                polled = _atspi._poll_typed_text(
                    lambda: self._run(lambda: _atspi.focused_text(app_id)),
                    before,
                    text,
                )
                if _atspi._typed_visible(before, polled, text):
                    return len(text)
                if polled is not None:
                    after = polled
                settled = _atspi._field_text_for_type(after, text)
                earlier = _atspi._field_text_for_type(before, text)
                if settled is not None and settled != earlier:
                    return len(text)
        # No readable text means the read-back is not possible. A terminal
        # screen that shows the inverted string is a mismatch, not a success.
        if after is not None and not _atspi._typed_visible(
            before, after, text, selection=selection,
        ):
            if app_id and bar_before is not None:
                bar_after = self._run(lambda: _atspi.address_bar_text(app_id))
                if bar_after == bar_before:
                    fresh = self._run(lambda: _atspi._collected_address_bar(app_id))
                    if fresh:
                        bar_after = fresh
                if _atspi.location_shows_typed(bar_before, bar_after, text):
                    return len(text)
            # Chrome's GTK Open File dialog is an X window. Its location entry
            # is not an AT-SPI node, so the page focus stays empty after the
            # keys land. The entry's own text is the read-back.
            copied = self._chooser_location_text(app_id, text)
            if copied is not None and _atspi._typed_visible(None, copied, text):
                self._chooser_readback = copied
                return len(text)
            raise _atspi._text_mismatch(
                "text_mismatch",
                f"the text read back does not contain {text!r}",
                expected=text,
                actual=after,
            )
        return len(text)

    def _chooser_location_text(self, app_id: str | None, text: str) -> str | None:
        """Text in the active file chooser's location entry, or None.

        The dialog has keyboard focus and no accessibility entry. Select-all
        and copy read the field the keystrokes went to. Selecting that entry
        also pops a list over the Open and Cancel buttons, so Right collapses
        the selection before returning and those buttons stay clickable. A
        sentinel is written first so a clipboard that already held ``text``
        cannot count, and the previous clipboard is put back.
        """
        if not text or not _named_browser(app_id):
            return None
        from a11y_computer_use.drivers import _atspi, _linux_system

        active = _linux_system.active_window()
        if not active:
            return None
        owner = str(active.get("app") or "")
        if app_id and owner and _browser_family(owner) != _browser_family(app_id):
            return None
        copied = self._copy_focused_chooser_text()
        if isinstance(copied, str) and _atspi._typed_visible(None, copied, text):
            return copied
        return None

    def _copy_focused_chooser_text(self) -> str | None:
        """Text in the focused Open/Save location entry, or None.

        The dialog has keyboard focus and no accessibility entry. Select-all
        and copy read that field. Selecting it also pops a list over Open and
        Cancel, so Right collapses the selection before returning. A sentinel
        is written first so a clipboard that already held the path cannot
        count, and the previous clipboard is put back.
        """
        from a11y_computer_use.drivers import _linux_input, _linux_system

        active = _linux_system.active_window()
        if not active:
            return None
        title = " ".join(str(active.get("title") or "").casefold().split())
        if title not in {"open file", "save file", "save as"}:
            return None
        saved = None
        restore = False
        try:
            saved = _linux_system.read_clipboard()
            restore = True
        except ComputerUseError:
            saved = None
        token = f"cu-chooser-{time.monotonic_ns()}"
        try:
            _linux_system.write_clipboard(token)
        except ComputerUseError:
            return None
        try:
            _linux_input.press_chord("ctrl+a")
            _linux_input.press_chord("ctrl+c")
            deadline = time.monotonic() + 0.5
            got = None
            while True:
                try:
                    got = _linux_system.read_clipboard()
                except ComputerUseError:
                    got = None
                if isinstance(got, str) and got != token:
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            if isinstance(got, str) and got.strip():
                text = got.replace("\u00a0", " ").strip("\n")
                if "\n" not in text and "no file chosen" not in text.casefold():
                    return text.strip()
            return None
        finally:
            # Select-all leaves a popup over the action buttons. Collapse the
            # selection so a later click can hit Open.
            try:
                _linux_input.press_chord("right")
            except Exception:
                pass
            if restore:
                try:
                    _linux_system.write_clipboard(saved or "")
                except ComputerUseError:
                    pass

    def _typed_readback(self, app_id: str | None) -> str | None:
        """Text used to verify a keystroke type.

        For LibreOffice, an open cell editor's paragraph is the in-progress
        string. The focused cell's own text stays empty until Return, which
        is why 0.4.45 reported ``actual: ""`` after the digits had landed.
        ``type_text`` polls that editor, then the selected cell, when this
        first read does not show the request. Any other app uses the focused
        node's text, so a terminal that shows the inverted string is still
        a mismatch.
        """
        from a11y_computer_use.drivers import _atspi

        if not app_id:
            return None
        text = self._run(lambda: _atspi.focused_text(app_id))
        if not _atspi.libreoffice_app(app_id):
            return text
        editor = self._run(lambda: _atspi.sheet_editor_text(app_id))
        if editor:
            return editor
        return text

    def _write_sheet_cell(self, handle, value: str) -> None:
        """Type ``value`` into a Calc cell and commit it with Return."""
        from a11y_computer_use.drivers import _atspi

        self._run(lambda: _atspi.grab_focus(handle))
        if _on_wayland():
            raise _wayland_input_error("set_value on a spreadsheet cell")
        from a11y_computer_use.drivers import _linux_input

        _linux_input.type_string(value)
        _linux_input.press_chord("return")
        for attempt in range(15):
            if self._run(lambda: _atspi.sheet_cell_matches(handle, value)):
                return
            if attempt + 1 < 15:
                time.sleep(0.1)
        actual = self._run(lambda: _atspi._full_text(handle))
        formula = self._run(lambda: _atspi._sheet_formula(handle))
        # A range colon is escaped in the Formula attribute and the value is
        # split there, so SUM(A1:A3) arrives as SUM(A1\. The editor has the
        # formula that committed.
        if _atspi._formula_needs_editor(formula, value):
            opened = self._run(lambda: _atspi.committed_sheet_formula(handle))
            if opened and _atspi.formulas_match(opened, value):
                return
            if opened:
                formula = opened
        raise _atspi._text_mismatch(
            "text_mismatch",
            f"the value read back does not match {value!r}",
            expected=value,
            actual=actual,
            formula=formula,
        )

    def _location_entry(self, app_id: str):
        """The focused node when it is the address bar, else None."""
        from a11y_computer_use.drivers import _atspi

        try:
            return self._run(lambda: _atspi.focused_location_entry(app_id))
        except Exception:  # noqa: BLE001 - unknown focus is not the address bar
            return None

    def _replace_location_selection(self, handle, text: str, app_id: str | None) -> int:
        """Replace a urlbar selection that EditableText would not delete."""
        from a11y_computer_use.drivers import _atspi, _linux_input

        before = self._run(lambda: _atspi._readable_text(handle))
        _linux_input.type_string(text, delay=0.05)
        return self._location_read_back(handle, before, text, app_id)

    def _location_read_back(self, handle, before: str | None, text: str, app_id: str | None) -> int:
        """Confirm ``text`` in the address bar, including a scheme-less omnibox."""
        from a11y_computer_use.drivers import _atspi

        after = self._run(lambda: _atspi._readable_text(handle))
        if _atspi.location_shows_typed(before, after, text):
            return len(text)
        bar = None
        if app_id:
            bar = self._run(lambda: _atspi.address_bar_text(app_id))
            if bar == after:
                # The focused wrapper is stale. Collection may hold a fresh one.
                root_text = self._run(lambda: _atspi._collected_address_bar(app_id))
                if root_text:
                    bar = root_text
        if _atspi.location_shows_typed(before, bar, text):
            return len(text)
        raise _atspi._text_mismatch(
            "text_mismatch",
            f"the text read back does not contain {text!r}",
            expected=text,
            actual=after if after is not None else bar,
        )

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
        # Return in a file chooser's location entry closes the dialog and
        # leaves the file unchosen. Activate Open or Save instead, and do
        # not report the key as pressed unless the opener shows the file.
        if self._confirm_file_chooser(chord):
            return None
        if printable_chord(chord):
            self._refuse_xtest_password_focus()
        self._focused_editable = None
        _linux_input.press_chord(chord)
        return None

    def _confirm_file_chooser(self, chord: str) -> bool:
        """Activate the chooser's Open or Save button. True when this chord was that confirm.

        A bare Return, Enter, or kp_enter is the confirm. The key is not
        sent. When the chooser has an accessibility button, that button is
        scrolled or the window is resized when its box is missing or outside
        the screen, then its AT-SPI action runs. The chooser window has to
        close and the opener has to show the file. A browser chooser with no
        accessibility button, including Chrome's, is fitted on screen and
        Open is clicked. The page's file input has to show the file. Anything
        else raises. A window that is not a chooser, and a non-browser
        chooser with no accessibility button, returns False so the key is sent.
        """
        if not _bare_chooser_confirm(chord):
            return False
        from a11y_computer_use.drivers import _atspi, _linux_system

        active = _linux_system.active_window()
        if not active or not _atspi.file_chooser_window_title(str(active.get("title") or "")):
            return False
        pid = int(active.get("pid") or 0) or None
        title = str(active.get("title") or "")
        try:
            found = self._run(lambda: _atspi.file_chooser_controls(pid, title))
        except ImportError:
            return False
        if found is None:
            if _named_browser(str(active.get("app") or "")):
                self._confirm_browser_chooser(active)
                return True
            return False
        button, path, app_root = found
        base = _atspi.file_basename(path)
        if not base:
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                "the file chooser has no typed path or selected file to open",
                detail={
                    "reason": "file_not_chosen",
                    "outcome": "refused",
                    "window_id": int(active.get("window_id") or 0),
                },
            )
        window_id = int(active.get("window_id") or 0)
        self._reveal_file_chooser_button(window_id, button)
        if not self._run(lambda: _atspi.do_press(button)):
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                "the file chooser's Open or Save button did not run its accessibility action",
                detail={
                    "reason": "file_chooser_action",
                    "outcome": "refused",
                    "window_id": window_id,
                    "expected": base,
                },
            )
        self._verify_file_chooser(active, path, app_root)
        return True

    def _reveal_file_chooser_button(self, window_id: int, button) -> None:
        """Scroll, then resize or move, when the accept button is not on screen."""
        from a11y_computer_use.drivers import _atspi

        def box():
            return _atspi.atspi_screen_box(button)

        current = self._run(box)
        if not _chooser_button_clipped(current):
            return
        self._run(lambda: _atspi.scroll_to(button))
        current = self._run(box)
        if not _chooser_button_clipped(current):
            return
        if window_id <= 0:
            return
        self._place_chooser_on_screen(window_id)

    def _place_chooser_on_screen(self, window_id: int) -> None:
        """Resize and move ``window_id`` so the dialog sits inside the display."""
        from a11y_computer_use.drivers import _atspi, _linux_system

        if window_id <= 0:
            return
        geom = _atspi.primary_geometry()[0]
        margin = 32
        dw = int(geom.display.width)
        dh = int(geom.display.height)
        fit_w = max(360, dw - 2 * margin)
        fit_h = max(280, dh - 2 * margin)
        rows = _linux_system.windows()
        row = next((item for item in rows if int(item.get("window_id") or 0) == window_id), None)
        bounds = (row or {}).get("bounds") or {}
        width = int(bounds.get("width") or 0)
        height = int(bounds.get("height") or 0)
        x = int(bounds.get("x") or margin)
        y = int(bounds.get("y") or margin)
        if width > fit_w or height > fit_h or width <= 1 or height <= 1:
            new_w = min(width, fit_w) if width > 1 else min(900, fit_w)
            new_h = min(height, fit_h) if height > 1 else fit_h
            _linux_system.resize_window(window_id, int(new_w), int(new_h))
            rows = _linux_system.windows()
            row = next((item for item in rows if int(item.get("window_id") or 0) == window_id), None)
            bounds = (row or {}).get("bounds") or {}
            width = int(bounds.get("width") or new_w)
            height = int(bounds.get("height") or new_h)
            x = int(bounds.get("x") or x)
            y = int(bounds.get("y") or y)
        nx, ny = x, y
        if nx < margin:
            nx = margin
        if ny < margin:
            ny = margin
        if nx + max(width, 1) > dw - margin:
            nx = max(margin, dw - margin - max(width, 1))
        if ny + max(height, 1) > dh - margin:
            ny = max(margin, dh - margin - max(height, 1))
        if (nx, ny) != (x, y):
            _linux_system.move_window(window_id, int(nx), int(ny))

    def _confirm_browser_chooser(self, active: dict) -> None:
        """Click Open on a browser chooser that has no accessibility button.

        Chrome's GTK dialog is an X window, so Return closes it and the page
        stays on "No file chosen". The location entry is read back, the
        window is fitted on screen, and Open is clicked at the bottom-right.
        The key is not sent. Success is the dialog gone and the page input
        showing the file. A filename in the address bar or the window title
        does not count while the input still says no file was chosen.
        """
        from a11y_computer_use.drivers import _atspi, _linux_input, _linux_system

        window_id = int(active.get("window_id") or 0)
        path = self._copy_focused_chooser_text() or str(self._chooser_readback or "")
        path = str(path or "").strip()
        base = _atspi.file_basename(path)
        if not base or "\n" in path or "no file chosen" in path.casefold():
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                "the file chooser has no typed path or selected file to open",
                detail={
                    "reason": "file_not_chosen",
                    "outcome": "refused",
                    "window_id": window_id,
                },
            )
        self._place_chooser_on_screen(window_id)
        bounds = _chooser_window_bounds(window_id)
        width = int((bounds or {}).get("width") or 0)
        height = int((bounds or {}).get("height") or 0)
        if not bounds or width <= 1 or height <= 1:
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                "the file chooser has no on-screen bounds for its Open button",
                detail={
                    "reason": "file_chooser_unchanged",
                    "outcome": "refused",
                    "window_id": window_id,
                    "expected": base,
                },
            )
        if window_id > 0:
            try:
                _linux_system.focus_window(window_id)
            except ComputerUseError:
                pass
        _linux_input.click(
            int(bounds["x"]) + max(1, width - _CHOOSER_OPEN_FROM_RIGHT),
            int(bounds["y"]) + max(1, height - _CHOOSER_OPEN_FROM_BOTTOM),
        )
        self._verify_browser_chooser(active, path)

    def _verify_browser_chooser(self, active: dict, path: str) -> None:
        """The chooser is gone and the page's file input shows ``path``."""
        from a11y_computer_use.drivers import _atspi

        window_id = int(active.get("window_id") or 0)
        title = str(active.get("title") or "")
        app = str(active.get("app") or "")
        base = _atspi.file_basename(path)
        deadline = time.monotonic() + 6.0
        closed = False
        while time.monotonic() < deadline:
            if not _chooser_window_open(window_id, title, app):
                closed = True
                if self._page_shows_chosen_file(app, base):
                    return
            time.sleep(0.1)
        if not closed:
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                "the file chooser stayed open after the Open or Save action",
                detail={
                    "reason": "file_chooser_unchanged",
                    "outcome": "refused",
                    "window_id": window_id,
                    "expected": base,
                },
            )
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            f"the file chooser closed without the page showing {base!r}",
            detail={
                "reason": "file_not_chosen",
                "outcome": "refused",
                "window_id": window_id,
                "expected": base,
                "path": path,
            },
        )

    def _page_shows_chosen_file(self, app: str, base: str) -> bool:
        """True when a page snapshot shows ``base`` and not an empty file input.

        A window title or an address bar that contains the filename is not
        enough: an element that still says no file was chosen fails the check.
        """
        if not base:
            return False
        names = [app] if app else []
        if _browser_family(app) == "chrome" and "chrome" not in names:
            names.append("chrome")
        saw_file = False
        for scope in (Scope.WINDOW, Scope.APP):
            for name in names:
                try:
                    shot = self.snapshot(scope, name)
                except ComputerUseError:
                    continue
                texts = _snapshot_texts(shot)
                folded = "\n".join(texts).casefold()
                if any(phrase in folded for phrase in _FILE_INPUT_EMPTY):
                    return False
                if any(base in text for text in texts):
                    saw_file = True
        return saw_file

    def _verify_file_chooser(self, active: dict, path: str, app_root) -> None:
        """The chooser window is gone and the opener shows the file."""
        from a11y_computer_use.drivers import _atspi

        window_id = int(active.get("window_id") or 0)
        title = str(active.get("title") or "")
        app = str(active.get("app") or "")
        base = _atspi.file_basename(path)
        deadline = time.monotonic() + 4.0
        closed = False
        while time.monotonic() < deadline:
            if not _chooser_window_open(window_id, title, app):
                closed = True
                if self._opener_shows_file(app_root, path, title):
                    return
            time.sleep(0.1)
        if not closed:
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                "the file chooser stayed open after the Open or Save action",
                detail={
                    "reason": "file_chooser_unchanged",
                    "outcome": "refused",
                    "window_id": window_id,
                    "expected": base,
                },
            )
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            f"the file chooser closed without showing {base!r}",
            detail={
                "reason": "file_not_chosen",
                "outcome": "refused",
                "window_id": window_id,
                "expected": base,
                "path": path,
            },
        )

    def _opener_shows_file(self, app_root, path: str, chooser_title: str) -> bool:
        from a11y_computer_use.drivers import _atspi, _linux_system

        try:
            if app_root is not None and self._run(
                lambda: _atspi.tree_shows_file(app_root, path, skip_titles=(chooser_title,))
            ):
                return True
        except Exception:
            pass
        base = _atspi.file_basename(path)
        full = str(path or "").strip()
        try:
            rows = _linux_system.windows()
        except ComputerUseError:
            return False
        for row in rows:
            title = str(row.get("title") or "")
            if _atspi.file_chooser_window_title(title):
                continue
            if base and base in title:
                return True
            if full and full in title:
                return True
        return False

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

    def displays(self):
        """The X screen as one display, id 0. Coordinates are root-window pixels."""
        from a11y_computer_use.drivers import _atspi

        return tuple(geom.display for geom in _atspi.primary_geometry())

    def _require_display(self, display_id: int | None) -> None:
        if display_id is None:
            return
        found = self.displays()
        if any(item.display_id == display_id for item in found):
            return
        raise ValueError(unknown_display_message(display_id, found))

    # -- capture (grim on Wayland, PIL X11 grab otherwise) ------------------
    def screenshot(self, display_id: int | None = None) -> object:
        import io

        from PIL import Image

        from a11y_computer_use import capture
        from a11y_computer_use.drivers import _atspi
        from a11y_computer_use.schema import Display

        self._require_display(display_id)
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

        self._require_display(region.display_id)
        full = Image.open(io.BytesIO(_grab_png()))
        # Clip to the frame. A region past the image used to come back black
        # and look like a successful capture of a dark UI.
        frame = Display(region.display_id, full.width, full.height, 1.0, True)
        clipped = clip_region_to_display(region.x, region.y, region.width, region.height, frame)
        crop = full.crop((
            clipped.x, clipped.y, clipped.x + clipped.width, clipped.y + clipped.height,
        ))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        return buf.getvalue()

    # -- system / windowing -------------------------------------------------
    def frontmost_app(self) -> tuple[str | None, int | None]:
        from a11y_computer_use.drivers import _linux_system

        app_id = _linux_system.frontmost_app_id() or None
        return app_id, None

    def active_window(self) -> dict | None:
        """The EWMH active window (``window_id``, ``app``, ``pid``, ``title``).

        None on Wayland, where there is no EWMH window id, and when no window
        is active.
        """
        if _on_wayland():
            return None
        from a11y_computer_use.drivers import _linux_system

        return _linux_system.active_window()

    def app_at_point(self, point: Point) -> str | None:
        from a11y_computer_use.drivers import _linux_system

        return _linux_system.app_at_point_id(point.x, point.y)

    def occlusion(self, element: Element, app: str | None) -> str | None:
        """None when the element's center belongs to ``app``.

        A GTK process launched from Python has comm ``python3`` and an AT-SPI
        name from ``GLib.set_prgname``. Comparing those strings would call the
        app's own window a cover. The same pid is the same app. A different
        pid is that window's comm (``covered by <comm>``). An unknown pid is
        not a cover: a missing hit-test must not refuse the crop.
        """
        from a11y_computer_use.drivers import _atspi, _linux_system

        bounds = element.bounds
        if bounds is None:
            return "off_screen"
        cx = float(bounds.x) + float(bounds.width) / 2.0
        cy = float(bounds.y) + float(bounds.height) / 2.0
        try:
            owner_pid = _linux_system.pid_at_point(cx, cy)
        except Exception:  # noqa: BLE001 - no X hit-test
            return None
        if not owner_pid:
            return None
        app_pid = None
        if app:
            try:
                root = self._run(lambda: _atspi.find_root(app, Scope.APP))
                app_pid = _atspi.pid_of(root) if root is not None else None
            except Exception:  # noqa: BLE001 - the bus may be down in a unit test
                app_pid = None
        if app_pid is not None and int(owner_pid) == int(app_pid):
            return None
        try:
            owner = _linux_system._comm_for_pid(int(owner_pid))
        except Exception:  # noqa: BLE001
            owner = None
        if app and owner and (
            owner.casefold() == app.casefold()
            or _linux_system._comm_matches_identifier(app, owner)
            or _linux_system._comm_matches_identifier(owner, app)
        ):
            return None
        if app_pid is None:
            return None
        return owner or "covered"

    def running_apps(self) -> list[dict]:
        from a11y_computer_use.drivers import _linux_system

        return _linux_system.running_apps()

    def launch_app(self, identifier: str, *, argv: tuple[str, ...] | None = None) -> dict:
        from a11y_computer_use.drivers import _linux_system

        self._focused_editable = None
        if argv:
            return _linux_system.launch_app(identifier, argv=argv)
        return _linux_system.launch_app(identifier)

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
        """EWMH ``_NET_ACTIVE_WINDOW``. A minimized window is uniconified first.

        The request is retried until ``_NET_ACTIVE_WINDOW`` stays on this window.
        """
        self._ewmh(window_id, "raise_window", lambda sys: sys.raise_window(window_id))

    def focus_window(self, window_id: int) -> None:
        """EWMH ``_NET_ACTIVE_WINDOW``. A minimized window is uniconified first.

        The request is retried until ``_NET_ACTIVE_WINDOW`` stays on this window.
        """
        self._ewmh(window_id, "focus_window", lambda sys: sys.focus_window(window_id))

    def minimize_window(self, window_id: int) -> None:
        """ICCCM iconic state plus ``_NET_WM_STATE_HIDDEN``.

        The request is retried until the window is hidden.
        """
        self._ewmh(window_id, "minimize_window", lambda sys: sys.minimize_window(window_id))

    def maximize_window(self, window_id: int) -> None:
        """``_NET_WM_STATE`` maximized vertically and horizontally."""
        self._ewmh(window_id, "maximize_window", lambda sys: sys.maximize_window(window_id))

    def move_window(self, window_id: int, x: int, y: int) -> None:
        """Place the client window's top-left at (x, y).

        The same origin ``windows`` reports. The outer frame is shifted by
        ``_NET_FRAME_EXTENTS`` so a decorated window does not list a few
        pixels down and to the right of the point that was asked for. A
        frame origin above or left of the screen is sent as a signed
        coordinate. A float, a bool, a negative, or a point outside the
        screen raises ValueError and the window is not moved.
        """
        self._ewmh(window_id, "move_window", lambda sys: sys.move_window(window_id, x, y))

    def resize_window(self, window_id: int, width: int, height: int) -> None:
        """``_NET_MOVERESIZE_WINDOW`` with the width and height flags."""
        self._ewmh(window_id, "resize_window", lambda sys: sys.resize_window(window_id, width, height))

    def close_window(self, window_id: int) -> None:
        """``_NET_CLOSE_WINDOW`` client message, retried until the window is gone."""
        self._ewmh(window_id, "close_window", lambda sys: sys.close_window(window_id))

    def _ewmh(self, window_id: int, op: str, call) -> None:
        """Run an X11 window verb. Native Wayland has no EWMH window ids."""
        if _on_wayland():
            raise _wayland_window_error(op)
        from a11y_computer_use.drivers import _linux_system

        self._focused_editable = None
        if not call(_linux_system):
            raise _no_such_window(window_id)

    def _menu_root(self, app: str) -> object:
        """The AT-SPI application accessible for ``app``, or a structured error.

        Missing bindings are the same missing-dependency error as `ensure_trusted`.
        An unknown name is `app_not_found` and does not wait on a bus.
        """
        from a11y_computer_use.drivers import _atspi

        try:
            root = self._run(lambda: _atspi.find_root(app, Scope.APP))
        except ImportError as exc:
            raise _missing_atspi_bindings(exc) from exc
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
        from a11y_computer_use.drivers import _atspi, _linux_menus

        root = self._menu_root(app)
        # The leaf goes out on the app's private socket. Dialog.run() from
        # that handler must not hold the connection the next read uses.
        return self._run(lambda: _linux_menus.menu_press(
            root, path,
            dispatch=_atspi.dispatch_menu_leaf,
            await_dialog=_atspi.await_menu_dialog,
        ))

    def menu_mnemonic(self, app: str, letter: str) -> str | None:
        """Top-level menu whose Alt mnemonic is ``letter``, or None."""
        from a11y_computer_use.drivers import _linux_menus

        try:
            root = self._menu_root(app)
        except ComputerUseError as exc:
            if exc.code is ErrorCode.APP_NOT_FOUND:
                raise
            return None
        try:
            return self._run(lambda: _linux_menus.menu_mnemonic(root, letter))
        except ComputerUseError:
            return None

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


_CHOOSER_CONFIRM_KEYS = frozenset({"return", "enter", "kp_enter"})
# Chrome's Open button sits inset from the dialog's bottom-right. A click
# here lands on Open after the window has been fitted on screen.
_CHOOSER_OPEN_FROM_RIGHT = 53
_CHOOSER_OPEN_FROM_BOTTOM = 27
_FILE_INPUT_EMPTY = ("no file chosen", "no file selected")


def _bare_chooser_confirm(chord: str) -> bool:
    """True for Return/Enter with no modifier. Shift+Return stays a key."""
    parts = [part.strip().lower() for part in str(chord).split("+") if part.strip()]
    return len(parts) == 1 and parts[0] in _CHOOSER_CONFIRM_KEYS


def _chooser_button_clipped(box) -> bool:
    """True when ``box`` is missing, zero-sized, or outside the primary screen."""
    if box is None:
        return True
    x, y, width, height = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
    if width <= 1 or height <= 1:
        return True
    from a11y_computer_use.drivers import _atspi

    geom = _atspi.primary_geometry()[0]
    ox, oy = geom.origin
    dw = float(geom.display.width)
    dh = float(geom.display.height)
    return x < ox or y < oy or x + width > ox + dw or y + height > oy + dh


def _chooser_window_bounds(window_id: int) -> dict | None:
    """Bounds of ``window_id``, or None when that window is not listed."""
    from a11y_computer_use.drivers import _linux_system

    if window_id <= 0:
        return None
    for row in _linux_system.windows():
        if int(row.get("window_id") or 0) == int(window_id) and row.get("bounds"):
            return row["bounds"]
    return None


def _snapshot_texts(shot) -> list[str]:
    """Titles and values from a snapshot. Window titles are not included."""
    texts: list[str] = []
    for element in getattr(shot, "elements", ()) or ():
        title = str(getattr(element, "title", "") or "")
        if title:
            texts.append(title)
        value = getattr(element, "value", None)
        if value is not None and str(value):
            texts.append(str(value))
    return texts


def _chooser_window_open(window_id: int, title: str, app: str) -> bool:
    """True while the chooser window id, or that title on the same app, is mapped."""
    from a11y_computer_use.drivers import _linux_system

    folded = " ".join(str(title or "").casefold().split())
    for row in _linux_system.windows():
        if window_id and int(row.get("window_id") or 0) == int(window_id):
            return True
        row_title = " ".join(str(row.get("title") or "").casefold().split())
        if folded and row_title == folded and (not app or str(row.get("app") or "") == app):
            return True
    return False


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
                "Return activates Open or Save through that button's accessibility action "
                "and checks that the dialog closed and the app shows the file. "
                "A browser chooser with no accessibility button, including Chrome's, "
                "is fitted on screen and Open is clicked. The page's file input has to "
                "show the file; a page that still says no file was chosen is a failure. "
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
