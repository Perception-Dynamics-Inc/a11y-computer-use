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
import os
import time
from collections.abc import Callable

from computeruse.schema import (
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

_DEFAULT_ENDPOINT = "http://127.0.0.1:9222"

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
_MODIFIERS = frozenset({"alt", "ctrl", "control", "meta", "cmd", "command", "shift"})


def _point_of(target: Target) -> tuple[float, float]:
    if isinstance(target, Point):
        return float(target.x), float(target.y)
    return float(target.bounds.center.x), float(target.bounds.center.y)


class BrowserDriver:
    """The `Driver` protocol, backed by the Chrome DevTools Protocol."""

    name = "browser"

    def __init__(self, endpoint: str | None = None, *, target_id: str | None = None) -> None:
        self._endpoint = endpoint or os.environ.get("COMPUTERUSE_CDP_ENDPOINT", _DEFAULT_ENDPOINT)
        self._target_id = target_id
        self._session = None  # lazily connected _cdp.CDPSession
        self._enabled = False

    # -- connection ---------------------------------------------------------
    def _connect(self):
        """Bind to a page target and return a ready CDPSession (domains enabled)."""
        if self._session is not None:
            return self._session
        from computeruse.drivers import _cdp

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
        target = target or targets[0]
        self._target_id = target.get("id")
        self._session = _cdp.CDPSession(_cdp.connect(target["webSocketDebuggerUrl"]))
        for domain in ("DOM", "Page", "Runtime"):
            try:
                self._session.call(f"{domain}.enable")
            except ComputerUseError:
                pass  # some builds gate a domain; observe/act degrade, not crash
        self._enabled = True
        return self._session

    def _reset(self) -> None:
        if self._session is not None:
            self._session.close()
        self._session = None

    # -- permissions --------------------------------------------------------
    def ensure_trusted(self) -> None:
        """No OS grant; the requirement is a reachable CDP endpoint with a page."""
        self._connect()

    # -- observe ------------------------------------------------------------
    def snapshot(self, scope: Scope, app: str) -> Snapshot:
        from computeruse import observe
        from computeruse.drivers import _cdp_ax

        sess = self._bind(app)
        frames = self._collect_frames(sess)  # main first, then reachable child frames
        dom = sess.call("DOMSnapshot.captureSnapshot", {"computedStyles": []})
        # Two-pass geometry: frame-local first (to read each iframe's owner box),
        # then offset each frame's boxes into the top document's space.
        raw_geom, secure_ids = _cdp_ax.parse_dom_snapshot(dom)
        offsets = _cdp_ax.build_frame_offsets(raw_geom, frames)
        geometry, _ = _cdp_ax.parse_dom_snapshot(dom, offsets)
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

    def _collect_frames(self, sess) -> list[dict]:
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
        queue = [(c, main_id) for c in tree.get("childFrames", [])]
        while queue and len(frames) < self._MAX_FRAMES:
            node, parent_id = queue.pop(0)
            fid = node.get("frame", {}).get("id")
            if not fid:
                continue
            try:
                owner = sess.call("DOM.getFrameOwner", {"frameId": fid}).get("backendNodeId")
                sub = sess.call("Accessibility.getFullAXTree", {"frameId": fid}).get("nodes", [])
            except ComputerUseError:
                continue  # OOPIF / detached frame — skip, never fail the snapshot
            frames.append({"id": fid, "parent_id": parent_id,
                           "owner_backend": owner, "nodes": sub})
            queue.extend((c, fid) for c in node.get("childFrames", []))
        return frames

    def _bind(self, app: str | None):
        """Return the session for ``app`` (a page target id), switching if needed."""
        if app and app != self._target_id:
            self._reset()
            self._target_id = app
        return self._connect()

    def _page_geometry(self, dom: dict):
        """One `DisplayGeometry` sized to the document (CSS px, scale 1.0) so no
        below-the-fold node is projected offscreen. Document coordinates and the
        AX/DOM box space coincide, so the engine's point->pixel map is identity."""
        from computeruse.observe import DisplayGeometry

        doc = (dom.get("documents") or [{}])[0]
        width = int(doc.get("contentWidth") or 0) or 1280
        height = int(doc.get("contentHeight") or 0) or 800
        display = Display(display_id=0, width=width, height=height, scale=1.0, is_main=True)
        return (DisplayGeometry(display=display, origin=(0.0, 0.0)),)

    def resolve_ref(self, snap: Snapshot, ref: str, *, live: Snapshot | None = None) -> Element:
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
                        "live_snapshot_id": live.snapshot_id, "reason": reason,
                        "candidates": observe.stale_ref_candidates(anchor, live)},
            )
        return match

    # -- a11y-first act (coordinate-free) -----------------------------------
    def _backend_id(self, element: Element) -> int | None:
        from computeruse import observe

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
        self._connect().call("Runtime.callFunctionOn", {
            "objectId": object_id,
            "functionDeclaration": fn,
            "arguments": [{"value": a} for a in (args or [])],
        })
        return True

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

    def type_text(self, text: str, *, pre_check: Callable | None = None,
                  dry_run: bool = False) -> object:
        if dry_run or not text:
            return None
        if pre_check is not None:
            pre_check()
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
        btn = {MouseButton.LEFT: "left", MouseButton.RIGHT: "right",
               MouseButton.MIDDLE: "middle"}.get(button, "left")
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
             pre_check: Callable | None = None, dry_run: bool = False) -> object:
        if dry_run:
            return None
        if pre_check is not None:
            pre_check()
        x1, y1 = self._viewport_point(*_point_of(start))
        x2, y2 = self._viewport_point(*_point_of(end))
        sess = self._connect()
        sess.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x1, "y": y1,
                                               "button": "left", "clickCount": 1})
        sess.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x2, "y": y2,
                                               "button": "left"})
        sess.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x2, "y": y2,
                                               "button": "left", "clickCount": 1})
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
        self._connect().call("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": x, "y": y,
            "deltaX": -dx * step, "deltaY": -dy * step})
        return None

    def _viewport_point(self, doc_x: float, doc_y: float) -> tuple[float, float]:
        """Document coords -> viewport CSS coords (subtract the scroll offset).

        Input events are viewport-relative; our geometry is document-relative.
        """
        try:
            vv = self._connect().call("Page.getLayoutMetrics").get("cssVisualViewport", {})
            return doc_x - float(vv.get("pageX", 0)), doc_y - float(vv.get("pageY", 0))
        except ComputerUseError:
            return doc_x, doc_y

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
    def screenshot(self, display_id: int | None = None) -> object:
        from computeruse.capture import Screenshot

        sess = self._connect()
        data = sess.call("Page.captureScreenshot", {"format": "png",
                                                    "captureBeyondViewport": True})["data"]
        dom = sess.call("DOMSnapshot.captureSnapshot", {"computedStyles": []})
        display = self._page_geometry(dom)[0].display
        return Screenshot(png=base64.b64decode(data), display=display)

    def zoom_region(self, region: Bounds) -> bytes:
        data = self._connect().call("Page.captureScreenshot", {
            "format": "png", "captureBeyondViewport": True,
            "clip": {"x": float(region.x), "y": float(region.y),
                     "width": float(region.width), "height": float(region.height), "scale": 1.0},
        })["data"]
        return base64.b64decode(data)

    # -- system / windowing (tabs as apps/windows) --------------------------
    def frontmost_app(self) -> tuple[str | None, int | None]:
        return (self._target_id or (self._connect() and self._target_id)), None

    def app_at_point(self, point: Point) -> str | None:
        return self._target_id

    def running_apps(self) -> list[dict]:
        from computeruse.drivers import _cdp

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
        sess = self._connect()
        result = sess.call("Page.navigate", {"url": url})
        if isinstance(result, dict) and result.get("errorText"):
            raise ComputerUseError(
                ErrorCode.APP_NOT_FOUND, f"navigation to {url} failed: {result['errorText']}",
                detail={"url": url},
            )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                state = sess.call("Runtime.evaluate", {
                    "expression": "document.readyState", "returnByValue": True,
                }).get("result", {}).get("value")
            except ComputerUseError:
                state = None
            if state == "complete":
                return
            time.sleep(0.05)

    def activate_app(self, identifier: str) -> str:
        self._bind(identifier)
        return identifier

    def windows(self) -> list[dict]:
        return [{"app": a["id"], "title": a["name"], "url": a["url"]} for a in self.running_apps()]

    def read_clipboard(self) -> str | None:
        return None  # navigator.clipboard needs a user gesture/permission; not exposed via CDP

    def write_clipboard(self, text: str) -> None:
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            "clipboard write is not available through the browser backend",
        )


def _looks_like_url(s: str) -> bool:
    return "://" in s or s.startswith(("about:", "data:", "file:", "chrome:"))


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
