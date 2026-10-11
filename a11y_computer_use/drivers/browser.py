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
import json
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
    printable_chord,
)

if TYPE_CHECKING:
    from a11y_computer_use.drivers._cdp import CDPSession

_DEFAULT_ENDPOINT = "http://127.0.0.1:9222"
_FEED_LIMIT = 1000
_FEED_TEXT_LIMIT = 4096
_WEBMCP_TOOL_LIMIT = 200
_WEBMCP_RESULT_LIMIT = 16384
_WEBMCP_CALL_TIMEOUT_S = 30.0

# The registration recorder. WebMCP (navigator.modelContext) offers a page no
# way to enumerate what it registered, so the driver records registerTool /
# unregisterTool calls into a hidden, non-enumerable window.__a11y_webmcp
# registry: it wraps the native methods when the browser has the API and
# polyfills the object when it does not (so a page guarded with
# `if ('modelContext' in navigator)` still registers). Idempotent per document.
_WEBMCP_SHIM_JS = r"""
(() => {
  const g = globalThis;
  if (g.__a11y_webmcp) return;
  const nav = g.navigator;
  if (!nav) return;
  const registry = new Map();
  let native = null;
  try { native = nav.modelContext || null; } catch (e) { native = null; }
  const store = { registry, native: !!native };
  try { Object.defineProperty(g, '__a11y_webmcp', { value: store, enumerable: false, configurable: false, writable: false }); }
  catch (e) { return; }
  const record = (tool) => { if (tool && typeof tool.name === 'string' && tool.name) registry.set(tool.name, tool); };
  const forget = (name) => { registry.delete(String(name)); };
  if (native) {
    const wrap = (method, hook) => {
      const orig = typeof native[method] === 'function' ? native[method].bind(native) : null;
      try {
        Object.defineProperty(native, method, { configurable: true, writable: true, value: function (...a) { hook(...a); return orig ? orig(...a) : undefined; } });
      } catch (e) {}
    };
    wrap('registerTool', (tool) => record(tool));
    wrap('unregisterTool', (name) => forget(name));
    wrap('provideContext', (ctx) => { registry.clear(); for (const t of (ctx && ctx.tools) || []) record(t); });
    wrap('clearContext', () => registry.clear());
  } else {
    const shim = {
      registerTool(tool) { record(tool); },
      unregisterTool(name) { forget(name); },
      provideContext(ctx) { registry.clear(); for (const t of (ctx && ctx.tools) || []) record(t); },
      clearContext() { registry.clear(); },
    };
    try { Object.defineProperty(nav, 'modelContext', { value: shim, configurable: true, enumerable: true, writable: false }); }
    catch (e) {}
  }
})();
"""

_WEBMCP_LIST_JS = r"""
(async () => {
  const nav = globalThis.navigator;
  const store = globalThis.__a11y_webmcp;
  let hasApi = false;
  try { hasApi = !!(nav && ('modelContext' in nav) && nav.modelContext); } catch (e) {}
  const api = store ? (store.native ? 'native' : 'shim') : (hasApi ? 'native' : 'absent');
  const plain = (v) => { try { return JSON.parse(JSON.stringify(v === undefined ? null : v)); } catch (e) { return null; } };
  const tools = [];
  const seen = new Set();
  const push = (name, description, schema, kind) => {
    if (typeof name !== 'string' || !name || seen.has(name)) return;
    seen.add(name);
    tools.push({ name, description: String(description || ''), inputSchema: plain(schema), kind });
  };
  if (store) for (const [name, t] of store.registry) push(name, t.description, t.inputSchema, 'script');
  if (hasApi && typeof nav.modelContext.listTools === 'function') {
    try {
      const listed = await nav.modelContext.listTools();
      for (const t of listed || []) if (t) push(t.name, t.description, t.inputSchema, 'script');
    } catch (e) {}
  }
  for (const f of document.querySelectorAll('form[toolname]')) {
    const name = f.getAttribute('toolname');
    if (!name || seen.has(name)) continue;
    const props = {}; const required = [];
    for (const el of f.elements) {
      if (!el.name || el.disabled || props[el.name]) continue;
      if (['submit', 'button', 'hidden', 'reset', 'image'].includes(el.type)) continue;
      const p = { type: (el.type === 'number' || el.type === 'range') ? 'number' : (el.type === 'checkbox' ? 'boolean' : 'string') };
      const d = el.getAttribute('toolparamdescription'); if (d) p.description = d;
      const t = el.getAttribute('toolparamtitle'); if (t) p.title = t;
      if (el.tagName === 'SELECT') p.enum = Array.from(el.options).map((o) => o.value);
      props[el.name] = p;
      if (el.required) required.push(el.name);
    }
    push(name, f.getAttribute('tooldescription'), { type: 'object', properties: props, required }, 'form');
  }
  return { api, tools };
})()
"""

# %s slots: JSON-encoded tool name, JSON-encoded arguments object.
_WEBMCP_CALL_JS = r"""
(async () => {
  const name = %s;
  const args = %s;
  const store = globalThis.__a11y_webmcp;
  const text = (r) => { try { return JSON.stringify(r === undefined ? null : r); } catch (e) { return JSON.stringify(String(r)); } };
  const tool = store && store.registry.get(name);
  if (tool) {
    if (typeof tool.execute !== 'function') return { ok: false, error: 'tool has no execute function', kind: 'script' };
    try { const r = await tool.execute(args, {}); return { ok: true, result: text(r), kind: 'script' }; }
    catch (e) { return { ok: false, error: String(e && e.message ? e.message : e), kind: 'script' }; }
  }
  const form = Array.from(document.querySelectorAll('form[toolname]')).find((f) => f.getAttribute('toolname') === name);
  if (form) {
    for (const [k, v] of Object.entries(args || {})) {
      const el = form.elements.namedItem(k);
      if (!el) continue;
      if (el.type === 'checkbox') el.checked = !!v; else el.value = String(v);
      try { el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); } catch (e) {}
    }
    try { form.requestSubmit(); } catch (e) { return { ok: false, error: String(e && e.message ? e.message : e), kind: 'form' }; }
    return { ok: true, result: text({ content: [{ type: 'text', text: 'submitted form tool ' + name }] }), kind: 'form' };
  }
  return { ok: false, error: 'not_found' };
})()
"""


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

    def __init__(self, endpoint: str | None = None, *, target_id: str | None = None,
                 webmcp_shim: bool | None = None) -> None:
        self._endpoint = endpoint or os.environ.get("A11Y_COMPUTER_USE_CDP_ENDPOINT", _DEFAULT_ENDPOINT)
        self._target_id = target_id
        #: Whether `webmcp_tools` may install the registration recorder into
        #: the page (see `_install_webmcp_shim`). Default on; the env var
        #: A11Y_COMPUTER_USE_WEBMCP_SHIM=0 turns it off for a page that must
        #: not be touched, leaving only declarative form tools observable.
        if webmcp_shim is None:
            webmcp_shim = os.environ.get("A11Y_COMPUTER_USE_WEBMCP_SHIM", "1") not in ("0", "false", "no")
        self._webmcp_shim = bool(webmcp_shim)
        self._webmcp_shim_installed = False
        self._session: CDPSession | None = None  # connected lazily
        # targetId -> {session_id, parent_id, url} for OOPIFs on this connection.
        # A later setAutoAttach does not re-emit targets that are already attached.
        self._oopif_targets: dict[str, dict] = {}
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
        self._oopif_targets = {}
        self._webmcp_shim_installed = False  # the recorder lives in the session
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
        raw_geom, secure_ids, empty_numbers = _cdp_ax.parse_dom_snapshot(dom)
        offsets = _cdp_ax.build_frame_offsets(raw_geom, frames)
        from a11y_computer_use.drivers import _cdp_frames

        # OOPIF boxes live in the child session's snapshot, not the parent's.
        # Their backend ids collide with the parent document, so they are stored
        # under (session_id, backend) after this shift.
        local = _cdp_frames.place_oopif_offsets(frames, raw_geom, offsets)
        # Single-frame pages (the common case) offset to (0,0) everywhere, so the
        # second parse would be identity — reuse the first instead of re-walking
        # the whole DOMSnapshot (and re-scanning every node for password fields).
        if any(off != (0.0, 0.0) for off in offsets.values()):
            geometry, _, _ = _cdp_ax.parse_dom_snapshot(dom, offsets)
        else:
            geometry = raw_geom
        geometry = dict(geometry)
        secure: set = set(secure_ids)
        empty: set = set(empty_numbers)
        _cdp_frames.merge_oopif_geometry(frames, local, offsets, geometry, secure, empty)
        stitched = _cdp_ax.stitch_frames([
            {
                "nodes": f["nodes"],
                "owner_backend": f["owner_backend"],
                "session_id": f.get("session_id") or "",
                "parent_session_id": f.get("parent_session_id") or "",
            }
            for f in frames
        ])
        accessor = _cdp_ax.CDPAccessor(stitched, geometry, frozenset(secure), frozenset(empty))
        return observe.build_snapshot(
            accessor.root(), accessor, scope=scope, app=self._target_id, pid=None,
            geometry=self._page_geometry(dom),
        )

    #: Cap on frames stitched into one snapshot — a backstop against pathological
    #: ad-heavy pages, not a real-page limit.
    _MAX_FRAMES = 24

    def _collect_frames(self, sess: CDPSession) -> list[dict]:
        """The main AX tree plus each reachable child frame's, in tree order.

        Same-process frames (same-origin, about:blank, srcdoc) come from
        ``Page.getFrameTree``. A cross-origin iframe is a separate target:
        ``getFullAXTree(frameId)`` on the page session fails, and the frame is
        absent from the frame tree. ``Target.setAutoAttach`` (flatten) attaches
        that target; its tree is read on the child session and grafted under
        the owner iframe. A frame that still cannot be read is skipped, so one
        detached iframe does not fail the snapshot. ``crop`` uses the composed
        box.
        """
        from a11y_computer_use.drivers import _cdp_frames

        main_nodes = sess.call("Accessibility.getFullAXTree").get("nodes", [])
        tree = sess.call("Page.getFrameTree").get("frameTree", {})
        main_id = tree.get("frame", {}).get("id")
        frames = [{"id": main_id, "parent_id": None, "owner_backend": None, "nodes": main_nodes}]
        queue = deque((c, main_id) for c in islice(tree.get("childFrames", []), self._MAX_FRAMES - 1))
        attempts = 1
        while queue and attempts < self._MAX_FRAMES:
            node, parent_id = queue.popleft()
            attempts += 1  # failed lookups consume the budget too
            fid = node.get("frame", {}).get("id")
            if not fid:
                continue
            owner = None
            try:
                owner = sess.call("DOM.getFrameOwner", {"frameId": fid}).get("backendNodeId")
                sub = sess.call("Accessibility.getFullAXTree", {"frameId": fid}).get("nodes", [])
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.UNSUPPORTED:
                    raise
                continue  # detached frame; transport failures still surface
            frames.append({"id": fid, "parent_id": parent_id,
                           "owner_backend": owner, "nodes": sub})
            available = max(0, self._MAX_FRAMES - attempts - len(queue))
            queue.extend((c, fid) for c in islice(node.get("childFrames", []), available))
        _cdp_frames.attach_oopif_frames(
            sess, frames, limit=self._MAX_FRAMES, remembered=self._oopif_targets,
        )
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
    def _ax_handle(self, element: Element) -> dict | None:
        from a11y_computer_use import observe

        handle = observe.ax_handle_for(element.snapshot_id, element.ref)
        return handle if isinstance(handle, dict) else None

    def _backend_id(self, element: Element) -> int | None:
        handle = self._ax_handle(element)
        if handle is None:
            return None
        backend = handle.get("backendDOMNodeId")
        return backend if isinstance(backend, int) else None

    def _session_id(self, element: Element) -> str | None:
        """Child-session id for an OOPIF node; None addresses the page session."""
        handle = self._ax_handle(element)
        if handle is None:
            return None
        session = handle.get("_sessionId")
        return session if isinstance(session, str) and session else None

    def _object_id(self, backend_id: int, session_id: str | None = None) -> str | None:
        sess = self._connect()
        try:
            obj = sess.call(
                "DOM.resolveNode", {"backendNodeId": backend_id}, session_id=session_id,
            ).get("object", {})
        except ComputerUseError:
            return None
        return obj.get("objectId")

    def _call_on(self, backend_id: int, fn: str, args: list | None = None, *,
                 return_value: bool = False, session_id: str | None = None):
        object_id = self._object_id(backend_id, session_id)
        if object_id is None:
            return False
        sess = self._connect()
        try:
            result = sess.call("Runtime.callFunctionOn", {
                "objectId": object_id,
                "functionDeclaration": fn,
                "arguments": [{"value": a} for a in (args or [])],
                "returnByValue": True,
            }, session_id=session_id)
            if result.get("exceptionDetails"):
                raise ComputerUseError(
                    ErrorCode.UNSUPPORTED, "the page could not perform the DOM action",
                    detail={"backend_id": backend_id,
                            "error": result["exceptionDetails"].get("text", "JavaScript exception")},
                )
            if return_value:
                return result.get("result", {}).get("value")
            return True
        finally:
            # CDP keeps every resolved node alive until explicitly released.
            # Navigating in the action may already have destroyed its context.
            try:
                sess.call("Runtime.releaseObject", {"objectId": object_id},
                          timeout=1.0, session_id=session_id)
            except ComputerUseError:
                pass

    def focus_for_type(self, element: Element) -> bool:
        """Focus ``element`` and confirm ``document.activeElement``. Does not type."""
        if element.secure:
            return False
        backend = self._backend_id(element)
        if backend is None:
            return False
        result = self._call_on(
            backend,
            "function(){this.focus(); return document.activeElement === this;}",
            return_value=True,
            session_id=self._session_id(element),
        )
        return result is True

    def press_element(self, element: Element) -> bool:
        if element.secure:
            return False
        backend = self._backend_id(element)
        if backend is None:
            return False
        session_id = self._session_id(element)
        if element.editable:
            # Focus so a following type_text (Input.insertText) lands here — the
            # coordinate-free analog of the macOS/Linux focus-then-type path.
            return self._call_on(backend, "function(){this.focus()}", session_id=session_id)
        return self._call_on(backend, "function(){this.click()}", session_id=session_id)

    def scroll_into_view(self, element: Element) -> bool:
        backend = self._backend_id(element)
        if backend is None:
            return False
        return self._call_on(
            backend, "function(){this.scrollIntoView({block:'center',inline:'center'})}",
            session_id=self._session_id(element),
        )

    def set_value(self, element: Element, value: str) -> bool:
        if element.secure:
            return False
        backend = self._backend_id(element)
        if backend is None:
            return False
        # Native value setter + input/change events, so React/Vue controlled
        # inputs see the change (a plain ``this.value=`` would not fire their
        # listeners). One deterministic op, no keystrokes. A select whose
        # option does not exist, or a number/range outside its type, raises
        # ValueError before the setter runs. The JS result is the read-back.
        # An OOPIF node is resolved in its own session; backend ids collide.
        result = self._call_on(
            backend, _SET_VALUE_FN, [value], return_value=True,
            session_id=self._session_id(element),
        )
        if result is False:
            return False
        _raise_for_set_result(result, value)
        return True

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
        if printable_chord(chord) and self._focused_is_password():
            raise ComputerUseError(
                ErrorCode.SECURE_FIELD,
                "the focused element is a password field; secrets are typed by the human",
                detail={"api": "document.activeElement"},
            )
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

    def displays(self):
        """The bound document as display 0, in CSS pixels. Not the viewport."""
        return (self._metrics_geometry()[0].display,)

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

    # -- WebMCP (navigator.modelContext) ------------------------------------
    def _install_webmcp_shim(self, sess: CDPSession) -> None:
        """Install the registration recorder once per session.

        WebMCP has no page-visible enumeration, so the driver records what a
        page registers: `Page.addScriptToEvaluateOnNewDocument` covers every
        document loaded from now on, and one evaluate covers the current one
        (which misses tools that page already registered before this call, a
        limit stated in docs/webmcp.md). Only when the driver was asked to
        (``webmcp_shim=True``, the default; env A11Y_COMPUTER_USE_WEBMCP_SHIM=0
        turns it off), and never before the first WebMCP request.
        """
        if not self._webmcp_shim or self._webmcp_shim_installed:
            return
        try:
            sess.call("Page.addScriptToEvaluateOnNewDocument", {"source": _WEBMCP_SHIM_JS})
        except ComputerUseError as exc:
            if exc.code is not ErrorCode.UNSUPPORTED:
                raise
        sess.call("Runtime.evaluate", {"expression": _WEBMCP_SHIM_JS, "returnByValue": True})
        self._webmcp_shim_installed = True

    def webmcp_tools(self, *, app: str | None = None) -> dict:
        """The WebMCP tools the bound tab (or tab ``app``) exposes, as
        ``{"api": native|shim|absent, "tools": [{name, description, inputSchema, kind}]}``.

        ``api`` says how the page saw ``navigator.modelContext``: ``native`` is
        the browser's own implementation (Chrome behind its WebMCP flag),
        ``shim`` is the driver's polyfill (the page called it as if native), and
        ``absent`` means no API and no shim, so the list holds only declarative
        ``<form toolname>`` tools. ``kind`` is ``script`` for
        ``registerTool`` calls and ``form`` for declarative forms. Only the top
        document is read: tools registered inside cross-origin frames are not
        visible to a main-frame evaluate and are skipped.
        """
        sess = self._bind(app)
        self._install_webmcp_shim(sess)
        reply = sess.call("Runtime.evaluate", {
            "expression": _WEBMCP_LIST_JS, "awaitPromise": True, "returnByValue": True,
        })
        _raise_page_exception(reply, "listing WebMCP tools")
        raw = reply.get("result", {}).get("value")
        if not isinstance(raw, dict):
            return {"api": "absent", "tools": []}
        tools: list[dict] = []
        for t in raw.get("tools") or []:
            if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not t["name"]:
                continue
            schema = t.get("inputSchema")
            tools.append({
                "name": t["name"][:_FEED_TEXT_LIMIT],
                "description": str(t.get("description") or "")[:_FEED_TEXT_LIMIT],
                "inputSchema": schema if isinstance(schema, dict) else None,
                "kind": "form" if t.get("kind") == "form" else "script",
            })
        api = raw.get("api")
        return {"api": api if api in ("native", "shim", "absent") else "absent",
                "tools": tools[:_WEBMCP_TOOL_LIMIT]}

    def webmcp_call(self, name: str, arguments: dict | None = None, *, app: str | None = None) -> dict:
        """Run the page's WebMCP tool ``name`` with ``arguments`` and return
        ``{"name", "kind", "result", "truncated"?}``.

        A script tool's ``execute`` runs in the page and its awaited return
        value comes back JSON-serialised (the spec shape is
        ``{content: [{type: "text", text}]}``; anything JSON-able is passed
        through, capped at ``_WEBMCP_RESULT_LIMIT`` characters with
        ``truncated: true``). A declarative form tool is filled from
        ``arguments`` and submitted with ``requestSubmit()``. A tool that is
        not registered raises ``stale_ref`` (list again); a tool whose execute
        throws raises ``unsupported`` carrying the page's error text.
        """
        if not isinstance(name, str) or not name:
            raise ValueError("name must be a non-empty string")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object (dict) or None")
        try:
            args_json = json.dumps(arguments)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"arguments must be JSON-serialisable: {exc}") from None
        sess = self._bind(app)
        self._install_webmcp_shim(sess)
        expression = _WEBMCP_CALL_JS % (json.dumps(name), args_json)
        reply = sess.call("Runtime.evaluate", {
            "expression": expression, "awaitPromise": True, "returnByValue": True,
        }, timeout=_WEBMCP_CALL_TIMEOUT_S)
        _raise_page_exception(reply, f"calling WebMCP tool {name}")
        raw = reply.get("result", {}).get("value")
        if not isinstance(raw, dict):
            raise ComputerUseError(ErrorCode.UNSUPPORTED, f"WebMCP tool {name} returned no result",
                                   detail={"tool": name})
        if not raw.get("ok"):
            error = str(raw.get("error") or "failed")[:_FEED_TEXT_LIMIT]
            if error == "not_found":
                raise ComputerUseError(
                    ErrorCode.STALE_REF, f"WebMCP tool {name} is not registered on this page",
                    detail={"tool": name, "reason": "tool_not_registered",
                            "hint": "list the tools again; the page may have unregistered it"},
                )
            raise ComputerUseError(ErrorCode.UNSUPPORTED, f"WebMCP tool {name} failed: {error}",
                                   detail={"tool": name, "page_error": error})
        text = raw.get("result")
        out: dict = {"name": name, "kind": raw.get("kind", "script")}
        if isinstance(text, str) and len(text) > _WEBMCP_RESULT_LIMIT:
            out["result"] = text[:_WEBMCP_RESULT_LIMIT]
            out["truncated"] = True
            return out
        try:
            out["result"] = json.loads(text) if isinstance(text, str) else text
        except ValueError:
            out["result"] = text
        return out

    # -- system / windowing (tabs as apps/windows) --------------------------
    def frontmost_app(self) -> tuple[str | None, int | None]:
        if self._target_id is None:
            self._connect()  # binds a tab and sets _target_id
        return self._target_id, None

    def app_at_point(self, point: Point) -> str | None:
        return self._target_id

    def occlusion(self, element: Element, app: str | None) -> str | None:
        """``off_screen`` or ``covered`` when the element's center is not visible.

        The page's visual viewport is the screen. ``elementFromPoint`` names
        what is painted there. An iframe whose box contains the point is the
        cross-origin document, not a cover. A probe that cannot run returns
        None so a crop is not refused for lack of a hit test.
        """
        del app
        try:
            metrics = self._connect().call("Page.getLayoutMetrics")
        except ComputerUseError:
            return None
        viewport = metrics.get("cssVisualViewport") or {}
        try:
            page_x = float(viewport.get("pageX") or 0)
            page_y = float(viewport.get("pageY") or 0)
            view_w = float(viewport.get("clientWidth") or 0)
            view_h = float(viewport.get("clientHeight") or 0)
        except (TypeError, ValueError):
            return None
        if view_w <= 0 or view_h <= 0:
            return None
        bounds = element.bounds
        left = max(float(bounds.x), page_x)
        top = max(float(bounds.y), page_y)
        right = min(float(bounds.x + bounds.width), page_x + view_w)
        bottom = min(float(bounds.y + bounds.height), page_y + view_h)
        if right <= left or bottom <= top:
            return "off_screen"
        vx = (left + right) / 2 - page_x
        vy = (top + bottom) / 2 - page_y
        target = (bounds.x - page_x, bounds.y - page_y, bounds.width, bounds.height)
        expression = (
            "(()=>{const x=%s,y=%s;const hit=document.elementFromPoint(x,y);"
            "if(!hit)return{kind:'none'};const r=hit.getBoundingClientRect();"
            "return{kind:'hit',tag:hit.tagName,x:r.x,y:r.y,w:r.width,h:r.height};})()"
        ) % (vx, vy)
        try:
            reply = self._connect().call("Runtime.evaluate", {
                "expression": expression, "returnByValue": True,
            })
        except ComputerUseError:
            return None
        value = (reply.get("result") or {}).get("value")
        if not isinstance(value, dict):
            return None
        return _interpret_hit(value, target)

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

    def document_url(self) -> str | None:
        """Current page URL from ``Page.getFrameTree``, or None if CDP has none."""
        try:
            sess = self._connect()
            tree = sess.call("Page.getFrameTree", timeout=2.0)
        except Exception:  # noqa: BLE001 - a missing URL fails open for the policy
            return None
        frame = tree.get("frameTree", {}).get("frame", {}) if isinstance(tree, dict) else {}
        url = frame.get("url") if isinstance(frame, dict) else None
        if isinstance(url, str) and url.strip():
            return url.strip()
        return None

    def element_url(self, element: Element) -> str | None:
        """Absolute href of ``element`` or its nearest link, via the DOM."""
        backend = self._backend_id(element)
        if backend is None:
            return None
        try:
            value = self._call_on(
                backend,
                "function(){try{var el=(this.closest&&this.closest('a'))||this;"
                "return (el && (el.href||(el.getAttribute&&el.getAttribute('href'))))||'';}"
                "catch(e){return '';}}",
                return_value=True,
            )
        except ComputerUseError:
            return None
        if isinstance(value, str) and _looks_like_url(value.strip()):
            return value.strip()
        return None

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


def _interpret_hit(hit: dict, target: tuple[float, float, float, float]) -> str | None:
    """Whether ``elementFromPoint`` shows ``target`` or something covering it.

    ``target`` is ``(x, y, width, height)`` in viewport pixels. An iframe that
    contains the target's center is the cross-origin document. A hit whose box
    sits inside the target (the element or its child) is the element itself.
    """
    if hit.get("kind") == "none":
        return "off_screen"
    if hit.get("kind") != "hit":
        return None
    try:
        hx, hy = float(hit["x"]), float(hit["y"])
        hw, hh = float(hit["w"]), float(hit["h"])
    except (KeyError, TypeError, ValueError):
        return None
    tx, ty, tw, th = target
    cx, cy = tx + tw / 2, ty + th / 2
    tag = str(hit.get("tag") or "").upper()
    if tag in {"IFRAME", "FRAME"} and hx <= cx <= hx + hw and hy <= cy <= hy + hh:
        return None
    slop = 8.0
    inside = (
        hx >= tx - slop and hy >= ty - slop
        and hx + hw <= tx + tw + slop and hy + hh <= ty + th + slop
    )
    if inside:
        return None
    return "covered"


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


def _raise_page_exception(reply: dict, what: str) -> None:
    """Turn a `Runtime.evaluate` exception into a structured error."""
    det = reply.get("exceptionDetails") if isinstance(reply, dict) else None
    if not det:
        return
    exc = det.get("exception") or {}
    text = str(exc.get("description") or det.get("text") or "JavaScript exception")[:_FEED_TEXT_LIMIT]
    raise ComputerUseError(ErrorCode.UNSUPPORTED, f"{what} raised in the page: {text}",
                           detail={"page_error": text})


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


def validate_chord(chord: str) -> None:
    """Raise ValueError if ``chord`` cannot be dispatched. Sends nothing."""
    _key_events(chord)


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
    "const el=this;"
    "const tag=String(el.tagName||'').toUpperCase();"
    "const type=String(el.type||'').toLowerCase();"
    "const fire=()=>{"
    "el.dispatchEvent(new Event('input',{bubbles:true}));"
    "el.dispatchEvent(new Event('change',{bubbles:true}));};"
    "const setProp=(node,next)=>{"
    "const proto=node.constructor&&node.constructor.prototype;"
    "const p=proto&&Object.getOwnPropertyDescriptor(proto,'value');"
    "if(p&&p.set){p.set.call(node,next);}else{node.value=next;}};"
    "if(tag==='SELECT'){"
    "const opts=Array.from(el.options||[]);"
    "const labels=opts.map((o)=>String(o.label||o.text||o.value));"
    "const hit=opts.find((o)=>o.value===v||String(o.label||o.text)===v);"
    "if(!hit){return {ok:false,code:'invalid_option',options:labels};}"
    "setProp(el,hit.value);fire();"
    "const chosen=el.selectedIndex>=0?el.options[el.selectedIndex]:null;"
    "const shown=chosen?String(chosen.label||chosen.text||chosen.value):'';"
    "if(el.value!==hit.value){return {ok:false,code:'mismatch',actual:shown||String(el.value)};}"
    "return {ok:true,actual:shown||String(el.value)};}"
    "if(tag==='INPUT'&&(type==='number'||type==='range')){"
    "const min=(el.min===''||el.min==null)?null:Number(el.min);"
    "const max=(el.max===''||el.max==null)?null:Number(el.max);"
    "const text=String(v);"
    "if(type==='number'&&text.trim()===''){"
    "setProp(el,'');fire();"
    "if(String(el.value)!==''){return {ok:false,code:'mismatch',actual:String(el.value)};}"
    "return {ok:true,actual:''};}"
    "const n=Number(text);"
    "if(text.trim()===''||!Number.isFinite(n)){"
    "return {ok:false,code:'invalid_number',min:min,max:max};}"
    "if((min!==null&&Number.isFinite(min)&&n<min)||(max!==null&&Number.isFinite(max)&&n>max)){"
    "return {ok:false,code:'out_of_range',min:min,max:max};}"
    "setProp(el,text);fire();"
    "const got=String(el.value);"
    "if(got===''||!Number.isFinite(Number(got))||Number(got)!==n){"
    "return {ok:false,code:'mismatch',actual:got};}"
    "return {ok:true,actual:got};}"
    "if(el.isContentEditable){"
    "const html=el.innerHTML;"
    "const norm=s=>String(s==null?'':s).replace(/\\u00a0/g,' ').replace(/\\r\\n/g,'\\n').replace(/\\n$/,'');"
    "el.textContent=String(v);"
    "fire();"
    "const shown=(el.innerText!=null)?el.innerText:el.textContent;"
    "if(norm(shown)!==norm(v)){"
    "el.innerHTML=html;"
    "return {ok:false,code:'mismatch',actual:String(shown)};}"
    "return {ok:true,actual:String(shown)};}"
    "setProp(el,v);fire();"
    "if('value' in el&&String(el.value)!==String(v)){"
    "return {ok:false,code:'mismatch',actual:String(el.value)};}"
    "return {ok:true,actual:('value' in el)?String(el.value):String(v)};}"
)


def _raise_for_set_result(result: object, value: str) -> None:
    """Turn a set_value JS result into ValueError or a read-back error.

    A missing result is the scripted transport, which does not run the
    function. A real page returns ``{ok: true}`` or a refusal.
    """
    if not isinstance(result, dict):
        return
    if result.get("ok") is True:
        return
    code = result.get("code")
    if code == "invalid_option":
        options = result.get("options") or []
        listed = ", ".join(str(item) for item in options)
        raise ValueError(f"value {value!r} is not one of: {listed}")
    if code in {"invalid_number", "out_of_range"}:
        low, high = result.get("min"), result.get("max")
        if code == "out_of_range":
            raise ValueError(f"value {value!r} is outside {low}..{high}")
        raise ValueError(f"value {value!r} is not a number; valid range is {low}..{high}")
    actual = result.get("actual")
    raise ComputerUseError(
        ErrorCode.UNSUPPORTED,
        f"the value read back {actual!r} does not match {value!r}",
        detail={"reason": "text_mismatch", "expected": value, "actual": actual},
    )

__all__ = ["BrowserDriver"]
