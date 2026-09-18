"""Browser backend — a11y-first control of Chromium over the DevTools Protocol.

The fourth `Driver`, and the one that is coordinate-free by construction: it
observes a page through ``Accessibility.getFullAXTree`` (joined with
``DOMSnapshot`` geometry) and acts through the DOM — ``resolveNode`` +
``callFunctionOn`` to activate/focus an element, ``Input.insertText`` to type,
``Input.dispatchKeyEvent`` for chords — so a click needs no pixel and no window
focus, and N tabs drive concurrently. Pixel input (mouse/wheel) is available as
the vision fallback via ``Input.dispatchMouseEvent``.

Unlike the OS drivers this one is selected explicitly (``get_driver("browser")``)
and attaches to a *running* Chromium exposing a CDP endpoint
(``--remote-debugging-port``); each page target is modelled as one "app"/"window".
It is 100% container-testable: all wire I/O sits behind `_cdp.Transport`, so unit
tests drive it with a scripted fake transport, and an opt-in live test exercises
headless Chromium end to end. Import-safe on every OS (websocket-client and PIL
are imported lazily).
"""

from __future__ import annotations

import base64
import math
import os
import time
from collections import deque
from collections.abc import Callable, Sequence
from itertools import islice
from typing import TYPE_CHECKING

from a11y_computer_use.schema import (
    Bounds,
    ComputerUseError,
    Display,
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

if TYPE_CHECKING:
    from a11y_computer_use.drivers._cdp import CDPSession

_DEFAULT_ENDPOINT = "http://127.0.0.1:9222"
_FEED_LIMIT = 1000
_FEED_TEXT_LIMIT = 4096

# CDP dispatchKeyEvent modifier bitmask (Alt=1, Ctrl=2, Meta/Cmd=4, Shift=8).
_MOD_BIT = {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "cmd": 4, "command": 4, "shift": 8}

# chord token -> (key, code, windowsVirtualKeyCode) for non-printable keys.
_NAMED_KEY = {
    "enter": ("Enter", "Enter", 13),
    "return": ("Enter", "Enter", 13),
    "tab": ("Tab", "Tab", 9),
    "escape": ("Escape", "Escape", 27),
    "esc": ("Escape", "Escape", 27),
    "backspace": ("Backspace", "Backspace", 8),
    "delete": ("Delete", "Delete", 46),
    "space": (" ", "Space", 32),
    "up": ("ArrowUp", "ArrowUp", 38),
    "down": ("ArrowDown", "ArrowDown", 40),
    "left": ("ArrowLeft", "ArrowLeft", 37),
    "right": ("ArrowRight", "ArrowRight", 39),
    "home": ("Home", "Home", 36),
    "end": ("End", "End", 35),
    "pageup": ("PageUp", "PageUp", 33),
    "pagedown": ("PageDown", "PageDown", 34),
}
_MODIFIERS = frozenset(_MOD_BIT)  # the modifier tokens, straight from the bitmask table


def _point_of(target: Target) -> tuple[float, float]:
    if isinstance(target, Point):
        return float(target.x), float(target.y)
    return float(target.bounds.center.x), float(target.bounds.center.y)


class BrowserDriver:
    """The `Driver` protocol, backed by the Chrome DevTools Protocol."""

    name = "browser"
    #: This backend's "apps" are CDP page targets (tabs), not OS applications, so
    #: the Runtime must resolve app identity + frontmost + recheck through the
    #: driver (frontmost_app/running_apps/activate_app), not the platform
    #: system-ops. See Runtime._resolves_apps.
    resolves_apps = True

    def __init__(self, endpoint: str | None = None, *, target_id: str | None = None) -> None:
        self._endpoint = endpoint or os.environ.get("A11Y_COMPUTER_USE_CDP_ENDPOINT", _DEFAULT_ENDPOINT)
        self._target_id = target_id
        self._session: CDPSession | None = None  # connected lazily
        self._console: deque[dict] = deque(maxlen=_FEED_LIMIT)
        self._net_pending: dict[str, dict] = {}  # requestId -> {method,url} in flight
        self._network: deque[dict] = deque(maxlen=_FEED_LIMIT)

    # -- connection ---------------------------------------------------------
    def _connect(self) -> CDPSession:
        """Bind to a page target and return a ready CDPSession (domains enabled)."""
        if self._session is not None:
            if not self._session.closed:
                return self._session
            self.close()
        from a11y_computer_use.drivers import _cdp

        targets = _cdp.page_targets(self._endpoint)
        if not targets:
            raise ComputerUseError(
                ErrorCode.APP_NOT_FOUND,
                f"no page targets at the CDP endpoint {self._endpoint}",
                detail={"hint": "open a tab in the debugged browser, or check the port."},
            )
        target = None
        if self._target_id is not None:
            target = next((t for t in targets if t.get("id") == self._target_id), None)
            if target is None:
                raise ComputerUseError(
                    ErrorCode.APP_NOT_FOUND, f"CDP page target {self._target_id} no longer exists",
                    detail={"target_id": self._target_id},
                )
        target = target or targets[0]
        if not target.get("id") or not target.get("webSocketDebuggerUrl"):
            raise ComputerUseError(ErrorCode.APP_NOT_FOUND, "CDP page has no target id or WebSocket URL")
        sess = _cdp.CDPSession(_cdp.connect(target["webSocketDebuggerUrl"]))
        # Runtime + Log emit console / exception events; Network emits request/
        # response/failure events — the console + network feeds a vision agent is
        # blind to (buffered, bounded, by the session). Enabling here starts both
        # feeds before the first action so nothing is missed.
        try:
            for domain in ("DOM", "Page", "Runtime", "Log", "Network"):
                try:
                    sess.call(f"{domain}.enable")
                except ComputerUseError as exc:
                    if exc.code is not ErrorCode.UNSUPPORTED:
                        raise
        except Exception:
            sess.close()
            raise
        self._target_id = target["id"]
        self._session = sess
        return sess

    def close(self) -> None:
        """Drop the CDP connection (a fresh one opens on next use)."""
        if self._session is not None:
            self._session.close()
        self._session = None
        self._console.clear()
        self._network.clear()
        self._net_pending.clear()

    _reset = close  # internal alias kept for existing call sites

    # -- permissions --------------------------------------------------------
    def ensure_trusted(self) -> None:
        """No OS grant; the requirement is a reachable CDP endpoint with a page."""
        self._connect()

    # -- observe ------------------------------------------------------------
    def snapshot(self, scope: Scope, app: str) -> Snapshot:
        from a11y_computer_use import observe
        from a11y_computer_use.drivers import _cdp_ax

        sess = self._bind(app)
        frames = self._collect_frames(sess)  # main first, then reachable child frames
        dom = sess.call("DOMSnapshot.captureSnapshot", {"computedStyles": []})
        # Two-pass geometry: frame-local first (to read each iframe's owner box),
        # then offset each frame's boxes into the top document's space.
        raw_geom, secure_ids = _cdp_ax.parse_dom_snapshot(dom)
        offsets = _cdp_ax.build_frame_offsets(raw_geom, frames)
        # Single-frame pages (the common case) offset to (0,0) everywhere, so the
        # second parse would be identity — reuse the first instead of re-walking
        # the whole DOMSnapshot (and re-scanning every node for password fields).
        if any(off != (0.0, 0.0) for off in offsets.values()):
            geometry, _ = _cdp_ax.parse_dom_snapshot(dom, offsets)
        else:
            geometry = raw_geom
        stitched = _cdp_ax.stitch_frames(
            [{"nodes": f["nodes"], "owner_backend": f["owner_backend"]} for f in frames]
        )
        accessor = _cdp_ax.CDPAccessor(stitched, geometry, secure_ids)
        return observe.build_snapshot(
            accessor.root(), accessor, scope=scope, app=self._target_id, pid=None,
            geometry=self._page_geometry(dom),
        )

    #: Cap on frames stitched into one snapshot — a backstop against pathological
    #: ad-heavy pages, not a real-page limit.
    _MAX_FRAMES = 24

    def _collect_frames(self, sess: CDPSession) -> list[dict]:
        """The main AX tree plus each reachable child frame's, in tree order.

        Cross-origin out-of-process iframes live in a separate CDP target; their
        ``getFullAXTree``/``getFrameOwner`` raise here and are skipped (a
        documented follow-up), so same-process (same-origin/about:blank) frames —
        the common embedded-form/widget case — become observable without the
        stitch ever crashing on an OOPIF.
        """
        main_nodes = sess.call("Accessibility.getFullAXTree").get("nodes", [])
        tree = sess.call("Page.getFrameTree").get("frameTree", {})
        main_id = tree.get("frame", {}).get("id")
        frames = [{"id": main_id, "parent_id": None, "owner_backend": None, "nodes": main_nodes}]
        queue = deque((c, main_id) for c in islice(tree.get("childFrames", []), self._MAX_FRAMES - 1))
        attempts = 1
        while queue and attempts < self._MAX_FRAMES:
            node, parent_id = queue.popleft()
            attempts += 1  # failed/OOPIF lookups consume the budget too
            fid = node.get("frame", {}).get("id")
            if not fid:
                continue
            try:
                owner = sess.call("DOM.getFrameOwner", {"frameId": fid}).get("backendNodeId")
                sub = sess.call("Accessibility.getFullAXTree", {"frameId": fid}).get("nodes", [])
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.UNSUPPORTED:
                    raise
                continue  # OOPIF / detached frame; transport failures still surface
            frames.append({"id": fid, "parent_id": parent_id,
                           "owner_backend": owner, "nodes": sub})
            available = max(0, self._MAX_FRAMES - attempts - len(queue))
            queue.extend((c, fid) for c in islice(node.get("childFrames", []), available))
        return frames

    def _bind(self, app: str | None) -> CDPSession:
        """Return the session for ``app`` (a page target id), switching if needed."""
        if app and app != self._target_id:
            self._reset()
            self._target_id = app
        return self._connect()

    def _display(self, width: int, height: int):
        """One `DisplayGeometry` sized to the document (CSS px, scale 1.0) so no
        below-the-fold node is projected offscreen. Document coordinates and the
        AX/DOM box space coincide, so the engine's point->pixel map is identity."""
        from a11y_computer_use.observe import DisplayGeometry

        display = Display(display_id=0, width=width or 1280, height=height or 800,
                         scale=1.0, is_main=True)
        return (DisplayGeometry(display=display, origin=(0.0, 0.0)),)

    def _page_geometry(self, dom: dict):
        """Document geometry from a DOMSnapshot the caller already has."""
        doc = (dom.get("documents") or [{}])[0]
        return self._display(int(doc.get("contentWidth") or 0), int(doc.get("contentHeight") or 0))

    def _metrics_geometry(self):
        """Document geometry from `Page.getLayoutMetrics` — a tiny reply, for
        capture paths that have no DOMSnapshot to piggyback on."""
        cs = self._connect().call("Page.getLayoutMetrics").get("cssContentSize", {})
        return self._display(int(cs.get("width") or 0), int(cs.get("height") or 0))

    def resolve_ref(self, snap: Snapshot, ref: str, *, live: Snapshot | None = None) -> Element:
        from a11y_computer_use import observe

        if live is None:
            live = self.snapshot(snap.scope, snap.app)
        return observe.rematch_ref(snap, ref, live)

    # -- a11y-first act (coordinate-free) -----------------------------------
    def _backend_id(self, element: Element) -> int | None:
        from a11y_computer_use import observe

        handle = observe.ax_handle_for(element.snapshot_id, element.ref)
        if not isinstance(handle, dict):
            return None
        return handle.get("backendDOMNodeId")

    def _object_id(self, backend_id: int) -> str | None:
        sess = self._connect()
        try:
            obj = sess.call("DOM.resolveNode", {"backendNodeId": backend_id}).get("object", {})
        except ComputerUseError:
            return None
        return obj.get("objectId")

    def _call_on(self, backend_id: int, fn: str, args: list | None = None) -> bool:
        object_id = self._object_id(backend_id)
        if object_id is None:
            return False
        sess = self._connect()
        try:
            result = sess.call("Runtime.callFunctionOn", {
                "objectId": object_id,
                "functionDeclaration": fn,
                "arguments": [{"value": a} for a in (args or [])],
                "returnByValue": True,
            })
            if result.get("exceptionDetails"):
                raise ComputerUseError(
                    ErrorCode.UNSUPPORTED, "the page could not perform the DOM action",
                    detail={"backend_id": backend_id,
                            "error": result["exceptionDetails"].get("text", "JavaScript exception")},
                )
            return True
        finally:
            # CDP keeps every resolved node alive until explicitly released.
            # Navigating in the action may already have destroyed its context.
            try:
                sess.call("Runtime.releaseObject", {"objectId": object_id}, timeout=1.0)
            except ComputerUseError:
                pass

    def press_element(self, element: Element) -> bool:
        if element.secure:
            return False
        backend = self._backend_id(element)
        if backend is None:
            return False
        if element.editable:
            # Focus so a following type_text (Input.insertText) lands here — the
            # coordinate-free analog of the macOS/Linux focus-then-type path.
            return self._call_on(backend, "function(){this.focus()}")
        return self._call_on(backend, "function(){this.click()}")

    def scroll_into_view(self, element: Element) -> bool:
        backend = self._backend_id(element)
        if backend is None:
            return False
        return self._call_on(
            backend, "function(){this.scrollIntoView({block:'center',inline:'center'})}"
        )

    def set_value(self, element: Element, value: str) -> bool:
        if element.secure:
            return False
        backend = self._backend_id(element)
        if backend is None:
            return False
        # Native value setter + input/change events, so React/Vue controlled
        # inputs see the change (a plain ``this.value=`` would not fire their
        # listeners). One deterministic op, no keystrokes.
        return self._call_on(backend, _SET_VALUE_FN, [value])

    def _focused_is_password(self) -> bool:
        """Whether the page's focused element is ``<input type=password>``.

        One ``Runtime.evaluate`` that follows ``document.activeElement`` through
        open shadow roots and same-origin iframes. An unreadable focus probe
        refuses typing rather than treating an unknown field as safe."""
        reply = self._connect().call("Runtime.evaluate", {
            "expression": _FOCUSED_PASSWORD_JS, "returnByValue": True})
        value = reply.get("result", {}).get("value")
        if reply.get("exceptionDetails") or not isinstance(value, bool):
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED, "could not verify whether the focused field is secure",
            )
        return value

    def type_text(self, text: str, *, pre_check: Callable | None = None,
                  dry_run: bool = False) -> object:
        """Insert ``text`` at the focused element (``Input.insertText``).

        Refuses with `ErrorCode.SECURE_FIELD` when the focused element is a
        password input: the browser analog of the macOS focused-secure-field
        probe, so a model cannot type a secret into a field it focused by
        coordinates or that the user left focused."""
        if dry_run or not text:
            return None
        if pre_check is not None:
            pre_check()
        if self._focused_is_password():
            raise ComputerUseError(
                ErrorCode.SECURE_FIELD,
                "the focused element is a password field; secrets are typed by the human",
                detail={"api": "document.activeElement"},
            )
        self._connect().call("Input.insertText", {"text": text})
        return None

    def key_chord(self, chord: str, *, pre_check: Callable | None = None,
                  dry_run: bool = False) -> object:
        events = _key_events(chord)  # validates; raises on a bad chord
        if dry_run:
            return None
        if pre_check is not None:
            pre_check()
        sess = self._connect()
        for ev in events:
            sess.call("Input.dispatchKeyEvent", ev)
        return None

    # -- coordinate act (vision fallback) -----------------------------------
    def click(self, target: Target, *, button: MouseButton = MouseButton.LEFT, count: int = 1,
              modifiers: tuple[str, ...] = (), pre_check: Callable | None = None,
              dry_run: bool = False) -> object:
        if dry_run:
            return None
        if pre_check is not None:
            pre_check()
        x, y = self._viewport_point(*_point_of(target))
        mods = _mods_mask(modifiers)
        btn = button.value  # MouseButton values are exactly "left"/"right"/"middle"
        sess = self._connect()
        for _ in range(max(1, count)):
            sess.call("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": x, "y": y, "button": btn,
                "clickCount": 1, "modifiers": mods})
            sess.call("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": x, "y": y, "button": btn,
                "clickCount": 1, "modifiers": mods})
        return None

    def drag(self, start: Target, end: Target, *, button: MouseButton = MouseButton.LEFT,
             path: Sequence[Target] = (), pre_check: Callable | None = None,
             dry_run: bool = False) -> object:
        if dry_run:
            return None
        if pre_check is not None:
            pre_check()
        ox, oy = self._scroll_offset()  # one metrics round-trip for the whole gesture
        (sx, sy), (ex, ey) = _point_of(start), _point_of(end)
        x1, y1, x2, y2 = sx - ox, sy - oy, ex - ox, ey - oy
        btn = button.value
        sess = self._connect()
        sess.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x1, "y": y1,
                                               "button": btn, "clickCount": 1})
        sess.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x2, "y": y2,
                                               "button": btn})
        sess.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x2, "y": y2,
                                               "button": btn, "clickCount": 1})
        return None

    def scroll(self, target: Target, *, dx: int = 0, dy: int = 0,
               unit: ScrollUnit = ScrollUnit.LINES, pre_check: Callable | None = None,
               dry_run: bool = False) -> object:
        if dry_run:
            return None
        if pre_check is not None:
            pre_check()
        x, y = self._viewport_point(*_point_of(target))
        step = 40 if unit is ScrollUnit.LINES else 1  # ~40 px per wheel line
        # Tool contract: positive dy scrolls content UP (the reader moves down the
        # page), positive dx scrolls content LEFT. CDP's wheel deltas use the same
        # sign (positive deltaY scrolls the scroller down), so they pass through
        # unnegated. (cu-arena's long_list task caught the earlier inverted sign:
        # every "scroll down" at the top of a list was a no-op.)
        self._connect().call("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": x, "y": y,
            "deltaX": dx * step, "deltaY": dy * step})
        return None

    def _scroll_offset(self) -> tuple[float, float]:
        """The page scroll offset (cssVisualViewport pageX/pageY), or (0, 0)."""
        try:
            vv = self._connect().call("Page.getLayoutMetrics").get("cssVisualViewport", {})
            return float(vv.get("pageX", 0)), float(vv.get("pageY", 0))
        except ComputerUseError:
            return 0.0, 0.0

    def _viewport_point(self, doc_x: float, doc_y: float) -> tuple[float, float]:
        """Document coords -> viewport CSS coords (input events are viewport-relative,
        our geometry is document-relative)."""
        ox, oy = self._scroll_offset()
        return doc_x - ox, doc_y - oy

    def wait_for(self, target: Element, *, condition: WaitCondition, timeout_s: float,
                 checker: Callable | None = None) -> Element:
        if checker is None:
            raise ValueError("BrowserDriver.wait_for needs a checker; the Runtime supplies one")
        deadline = time.monotonic() + timeout_s
        while True:
            result = checker(target, condition)
            if result is not None:
                return result
            if deadline - time.monotonic() <= 0:
                raise ComputerUseError(
                    ErrorCode.TIMEOUT,
                    f"{target.ref} did not reach {condition.value} within {timeout_s}s",
                    detail={"ref": target.ref, "condition": condition.value,
                            "timeout_s": timeout_s},
                )
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    # -- capture ------------------------------------------------------------
    def _device_pixel_ratio(self) -> float:
        """``window.devicePixelRatio`` of the bound tab, 1.0 when unreadable."""
        try:
            reply = self._connect().call("Runtime.evaluate", {
                "expression": "window.devicePixelRatio", "returnByValue": True})
            dpr = float((reply.get("result") or {}).get("value") or 1.0)
        except (ComputerUseError, TypeError, ValueError, AttributeError):
            return 1.0
        return dpr if dpr > 0 else 1.0

    def screenshot(self, display_id: int | None = None) -> object:
        from a11y_computer_use.capture import Screenshot

        sess = self._connect()
        # Page dimensions come from getLayoutMetrics (a tiny reply), not a full
        # DOMSnapshot — the capture path needs only the size, not the tree.
        display = self._metrics_geometry()[0].display
        params: dict = {"format": "png", "captureBeyondViewport": True}
        dpr = self._device_pixel_ratio()
        if dpr != 1.0:
            # Chrome paints captureScreenshot at devicePixelRatio, while our
            # geometry (Element bounds, Points, marks, snap_to_ref) is CSS px at
            # scale 1.0 and capture.Screenshot requires PNG dims == display dims.
            # clip.scale = 1/dpr brings the PNG back to CSS px (live: DPR 2 gave
            # 1600x1026 without the clip, 800x513 with it; clip.scale 1.0 does not).
            params["clip"] = {"x": 0.0, "y": 0.0, "width": float(display.width),
                              "height": float(display.height), "scale": 1.0 / dpr}
        data = sess.call("Page.captureScreenshot", params)["data"]
        return Screenshot(png=base64.b64decode(data), display=display)

    def main_display_id(self) -> int:
        # One display, id 0: the bound tab's document, see `_display()`.
        return 0

    def zoom_region(self, region: Bounds) -> bytes:
        data = self._connect().call("Page.captureScreenshot", {
            "format": "png", "captureBeyondViewport": True,
            "clip": {"x": float(region.x), "y": float(region.y),
                     "width": float(region.width), "height": float(region.height), "scale": 1.0},
        })["data"]
        return base64.b64decode(data)

    # -- observation the vision path can't see ------------------------------
    def _pump_events(self) -> None:
        """Drain the session's buffered CDP events into BOTH the console and
        network feeds. Events arrive asynchronously, so a cheap evaluate first
        pumps any pending frames off the socket; routing every drained event to
        both feeds means reading one never discards the other's events (the buffer
        is shared and drain-clears)."""
        sess = self._connect()
        try:
            sess.call("Runtime.evaluate", {"expression": "0", "returnByValue": True})
        except ComputerUseError as exc:
            if exc.code is not ErrorCode.UNSUPPORTED:
                raise
        for ev in sess.drain_events():
            entry = _console_entry(ev)
            if entry is not None:
                entry["text"] = str(entry["text"])[:_FEED_TEXT_LIMIT]
                self._console.append(entry)
            self._ingest_network(ev)

    def console_messages(self, *, clear: bool = True) -> list[dict]:
        """Console output + uncaught JS exceptions from the bound tab.

        The web agent's answer to "did that click actually work?" — errors and
        exceptions a screenshot never shows. Each entry is
        ``{"level": log|warning|error|exception, "text": ...}``. ``clear`` empties
        the buffer after reading (the default: an agent wants what's new since it
        last looked)."""
        self._pump_events()
        out = list(self._console)
        if clear:
            self._console.clear()
        return out

    def network_requests(self, *, clear: bool = True) -> list[dict]:
        """Completed network outcomes for the bound tab — status codes and
        failures a screenshot can't show ("did that POST return 200?").

        Each entry is ``{"method", "url", "status"}`` for a response, or
        ``{"method", "url", "error"}`` for a failure. Request/response events are
        joined by CDP ``requestId``; the pending-request map is bounded so a
        long-lived tab can't accumulate ids without limit. ``clear`` empties the
        completed buffer after reading."""
        self._pump_events()
        out = list(self._network)
        if clear:
            self._network.clear()
        return out

    def _ingest_network(self, ev: dict) -> None:
        method = ev.get("method")
        p = ev.get("params", {})
        rid = p.get("requestId")
        if method == "Network.requestWillBeSent":
            req = p.get("request", {})
            self._net_pending[rid] = {"method": req.get("method", "GET"),
                                      "url": str(req.get("url", ""))[:_FEED_TEXT_LIMIT]}
            if len(self._net_pending) > 512:  # bound: drop the oldest in-flight ids
                for k in list(self._net_pending)[:256]:
                    self._net_pending.pop(k, None)
        elif method == "Network.responseReceived":
            base = self._net_pending.pop(rid, {"method": "GET",
                                               "url": str(p.get("response", {}).get("url", ""))[:_FEED_TEXT_LIMIT]})
            self._network.append({**base, "status": p.get("response", {}).get("status")})
        elif method == "Network.loadingFailed":
            base = self._net_pending.pop(rid, {"method": "GET", "url": ""})
            self._network.append({**base, "error": str(p.get("errorText", "failed"))[:_FEED_TEXT_LIMIT]})

    # -- system / windowing (tabs as apps/windows) --------------------------
    def frontmost_app(self) -> tuple[str | None, int | None]:
        if self._target_id is None:
            self._connect()  # binds a tab and sets _target_id
        return self._target_id, None

    def app_at_point(self, point: Point) -> str | None:
        return self._target_id

    def running_apps(self) -> list[dict]:
        from a11y_computer_use.drivers import _cdp

        return [{"id": t.get("id"), "name": t.get("title") or t.get("url", ""),
                 "url": t.get("url", "")} for t in _cdp.page_targets(self._endpoint)]

    def launch_app(self, identifier: str) -> None:
        """Navigate the bound tab to a URL — the browser analog of launching an
        app (so the existing ``app`` tool's launch action drives it, no new MCP
        surface). ``identifier`` must be a URL (``https://``, ``http://``,
        ``about:``, ``data:``, ``file:``); anything else is rejected."""
        if not _looks_like_url(identifier):
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                "the browser backend attaches to a running Chromium; launch takes a URL to open",
                detail={"hint": "pass a URL (https://…) to navigate the tab; start Chrome with "
                        "--remote-debugging-port to attach."},
            )
        self.navigate(identifier)

    def navigate(self, url: str, *, timeout_s: float = 15.0) -> None:
        """Open ``url`` in the bound tab and block until the document finishes
        loading (``document.readyState == "complete"``) or ``timeout_s`` elapses.

        A load-aware wait means the very next snapshot sees the loaded page, not a
        blank frame — no fixed sleep, no racing the navigation.
        """
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive")
        sess = self._connect()
        deadline = time.monotonic() + timeout_s
        result = sess.call("Page.navigate", {"url": url}, timeout=timeout_s)
        if isinstance(result, dict) and result.get("errorText"):
            raise ComputerUseError(
                ErrorCode.APP_NOT_FOUND, f"navigation to {url} failed: {result['errorText']}",
                detail={"url": url},
            )
        loader_id = result.get("loaderId")
        while time.monotonic() < deadline:
            try:
                # A completed OLD document can remain visible until navigation
                # commits. Only accept readiness for the requested loader.
                if loader_id:
                    frame = sess.call("Page.getFrameTree", timeout=max(0.001, deadline - time.monotonic()))
                    current_loader = frame.get("frameTree", {}).get("frame", {}).get("loaderId")
                    if current_loader != loader_id:
                        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
                        continue
                state = sess.call("Runtime.evaluate", {
                    "expression": "document.readyState", "returnByValue": True,
                }, timeout=max(0.001, deadline - time.monotonic())).get("result", {}).get("value")
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.UNSUPPORTED:
                    raise
                # A navigation can briefly destroy the old execution context.
                state = None
            if state == "complete":
                return
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        raise ComputerUseError(
            ErrorCode.TIMEOUT, f"navigation to {url} did not finish within {timeout_s}s",
            detail={"url": url, "timeout_s": timeout_s, "outcome_unknown": True},
        )

    def activate_app(self, identifier: str) -> str:
        self._bind(identifier)
        return identifier

    def windows(self) -> list[dict]:
        return [{"app": a["id"], "title": a["name"], "url": a["url"]} for a in self.running_apps()]

    def window_owner(self, window_id: int) -> str:
        raise _no_window_ids(window_id)

    def raise_window(self, window_id: int) -> None:
        raise _no_window_ids(window_id)

    def menu_items(self, app: str, path: str | None) -> list[dict]:
        raise _no_menus("menu_items")

    def menu_press(self, app: str, path: str) -> str:
        raise _no_menus("menu_press")

    def menu_state(self, app: str) -> dict:
        return {"open": False, "path": []}  # no accessible menu bar on this backend

    def menu_close(self, app: str) -> list[str]:
        return []

    def file_dialog(self, verb: object, path: str, app: str) -> dict:
        raise _no_menus("file_dialog")

    def read_clipboard(self) -> str | None:
        return None  # navigator.clipboard needs a user gesture/permission; not exposed via CDP

    def write_clipboard(self, text: str) -> None:
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            "clipboard write is not available through the browser backend",
        )


def _looks_like_url(s: str) -> bool:
    return "://" in s or s.startswith(("about:", "data:", "file:", "chrome:"))


def _no_menus(op: str) -> ComputerUseError:
    """A page has no native menu bar or file panel; those belong to the browser."""
    return ComputerUseError(
        ErrorCode.UNSUPPORTED,
        f"{op}: a browser tab has no native menu bar or file panel",
        detail={"hint": "drive page controls by ref; use the OS driver for the browser's own menus"},
    )


def _no_window_ids(window_id: int) -> ComputerUseError:
    """The browser's windows are tabs addressed by target id, not integers."""
    return ComputerUseError(
        ErrorCode.UNSUPPORTED,
        "the browser backend has no integer window ids; its windows are tabs",
        detail={"window_id": window_id,
                "hint": "use `app list` for the tab ids and `app focus <id>` to raise one."},
    )


#: Follows document.activeElement through open shadow roots and same-origin
#: iframes (cross-origin frames throw and stop the descent), then answers
#: whether the focused element is a password input.
_FOCUSED_PASSWORD_JS = (
    "(()=>{let e=document.activeElement;"
    "for(let i=0;i<8&&e;i++){"
    "if(e.shadowRoot&&e.shadowRoot.activeElement){e=e.shadowRoot.activeElement;continue;}"
    "try{if(e.contentDocument&&e.contentDocument.activeElement){"
    "e=e.contentDocument.activeElement;continue;}}catch(_){}"
    "break;}"
    "return !!(e&&e.tagName==='INPUT'&&String(e.type).toLowerCase()==='password');})()"
)


def _console_entry(event: dict) -> dict | None:
    """Map one CDP event to a ``{"level", "text"}`` console entry, or None."""
    method = event.get("method")
    params = event.get("params", {})
    if method == "Runtime.consoleAPICalled":
        args = params.get("args", [])
        parts = [str(a.get("value", a.get("description", ""))) for a in args]
        # CDP levels: log|warning|error|debug|info|... — keep the model's vocab.
        return {"level": params.get("type", "log"), "text": " ".join(parts)}
    if method == "Runtime.exceptionThrown":
        det = params.get("exceptionDetails", {})
        text = det.get("exception", {}).get("description") or det.get("text", "uncaught exception")
        return {"level": "exception", "text": text.splitlines()[0] if text else "uncaught exception"}
    if method == "Log.entryAdded":
        entry = params.get("entry", {})
        return {"level": entry.get("level", "info"), "text": entry.get("text", "")}
    return None


def _mods_mask(modifiers: tuple[str, ...]) -> int:
    mask = 0
    for m in modifiers:
        mask |= _MOD_BIT.get(m.lower(), 0)
    return mask


def _key_events(chord: str) -> list[dict]:
    """A validated chord -> the dispatchKeyEvent payloads (down..., up...).

    ``modifiers first, one regular key last`` — e.g. ``cmd+a`` or ``enter``.
    Raises ValueError on an empty or malformed chord (so dry_run fails fast).
    """
    parts = [p.strip().lower() for p in chord.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty key chord: {chord!r}")
    *mods, key = parts
    for m in mods:
        if m not in _MODIFIERS:
            raise ValueError(f"unknown modifier {m!r} in chord {chord!r}")
    mask = _mods_mask(tuple(mods))
    if key in _NAMED_KEY:
        k, code, vk = _NAMED_KEY[key]
    elif len(key) == 1:
        k, code, vk = key, _code_for_char(key), ord(key.upper())
    else:
        raise ValueError(f"unknown key {key!r} in chord {chord!r}")
    down = {"type": "keyDown", "key": k, "code": code, "windowsVirtualKeyCode": vk,
            "modifiers": mask}
    if len(k) == 1 and not mask & ~8:  # a plain (or shift-only) printable char
        down["text"] = k
    up = {"type": "keyUp", "key": k, "code": code, "windowsVirtualKeyCode": vk, "modifiers": mask}
    return [down, up]


def _code_for_char(ch: str) -> str:
    if ch.isalpha():
        return f"Key{ch.upper()}"
    if ch.isdigit():
        return f"Digit{ch}"
    return ""


_SET_VALUE_FN = (
    "function(v){"
    "const p=Object.getOwnPropertyDescriptor(this.constructor.prototype,'value');"
    "if(p&&p.set){p.set.call(this,v);}else{this.value=v;}"
    "this.dispatchEvent(new Event('input',{bubbles:true}));"
    "this.dispatchEvent(new Event('change',{bubbles:true}));}"
)

__all__ = ["BrowserDriver"]
