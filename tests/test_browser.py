"""BrowserDriver (CDP) — hermetic unit tests over a scripted fake transport, plus
an opt-in live test against a real headless Chromium.

The transport seam (`_cdp.Transport`) is what makes the driver testable with no
browser: `ScriptedTransport` answers each CDP command from a fixture, so we can
assert both the observe mapping (AX tree + DOMSnapshot -> pruned Snapshot) and the
exact act payloads (resolveNode -> callFunctionOn click, insertText, key events)
deterministically. The live test (RUN only when a CDP endpoint is reachable)
proves the same paths end to end.
"""

from __future__ import annotations

import json
import os

import pytest

from computeruse.drivers import _cdp, _cdp_ax, browser
from computeruse.schema import ComputerUseError, ErrorCode, Scope


# --------------------------------------------------------------------------- #
# fake transport / session
# --------------------------------------------------------------------------- #
class _CDPError(Exception):
    pass


class ScriptedTransport:
    """A `_cdp.Transport` that answers each command via ``responder`` and records
    every (method, params) it was sent."""

    def __init__(self, responder) -> None:
        self.responder = responder
        self.sent: list[tuple[str, dict]] = []
        self._inbox: list[str] = []

    def send(self, payload: str) -> None:
        msg = json.loads(payload)
        self.sent.append((msg["method"], msg.get("params", {})))
        try:
            result = self.responder(msg["method"], msg.get("params", {}))
            self._inbox.append(json.dumps({"id": msg["id"], "result": result or {}}))
        except _CDPError as exc:
            self._inbox.append(json.dumps({"id": msg["id"], "error": {"message": str(exc)}}))

    def recv(self, timeout=None) -> str:
        assert self._inbox, "recv() with nothing queued — a call had no scripted reply"
        return self._inbox.pop(0)

    def close(self) -> None:
        pass

    def methods(self) -> list[str]:
        return [m for m, _ in self.sent]


def _fixture_responder(method: str, params: dict):
    """A small, realistic page: button, text field, password field, checkbox, link."""
    if method == "Accessibility.getFullAXTree":
        return {"nodes": _AX_NODES}
    if method == "Page.getFrameTree":
        return {"frameTree": {"frame": {"id": "MAIN"}, "childFrames": []}}
    if method == "DOMSnapshot.captureSnapshot":
        return _DOM_SNAPSHOT
    if method == "DOM.resolveNode":
        return {"object": {"objectId": f"obj-{params.get('backendNodeId')}"}}
    if method == "Runtime.callFunctionOn":
        return {"result": {"type": "undefined"}}
    if method == "Page.getLayoutMetrics":
        return {"cssVisualViewport": {"pageX": 0, "pageY": 0}}
    if method in ("Input.insertText", "Input.dispatchKeyEvent", "Input.dispatchMouseEvent",
                  "DOM.enable", "Page.enable", "Runtime.enable"):
        return {}
    if method == "Page.captureScreenshot":
        import base64
        return {"data": base64.b64encode(b"\x89PNG_fake").decode()}
    raise AssertionError(f"unexpected CDP method {method}")


def _ax(node_id, role, name="", backend=None, children=(), props=None, parent=None):
    n = {"nodeId": node_id, "role": {"value": role}, "name": {"value": name},
         "childIds": list(children)}
    if backend is not None:
        n["backendDOMNodeId"] = backend
    if parent is not None:
        n["parentId"] = parent
    if props:
        n["properties"] = [{"name": k, "value": {"value": v}} for k, v in props.items()]
    return n


_AX_NODES = [
    _ax("1", "RootWebArea", backend=100, children=["2", "3", "4", "5", "6"]),
    _ax("2", "button", "Save", backend=101, parent="1", props={"focusable": True}),
    _ax("3", "textbox", "Name", backend=102, parent="1",
        props={"editable": "plaintext", "multiline": False, "settable": True}),
    _ax("4", "textbox", "Password", backend=103, parent="1", props={"editable": "plaintext"}),
    _ax("5", "checkbox", "Agree", backend=104, parent="1", props={"checked": "true"}),
    _ax("6", "link", "Home", backend=105, parent="1", props={"url": "https://x/"}),
]

_DOM_SNAPSHOT = {
    "strings": ["INPUT", "type", "password", "text", "BUTTON", "A"],
    "documents": [{
        "contentWidth": 800, "contentHeight": 600,
        "nodes": {
            "backendNodeId": [100, 101, 102, 103, 104, 105],
            "nodeName": [-1, 4, 0, 0, 0, 5],
            "attributes": [[], [], [1, 3], [1, 2], [1, 3], []],
        },
        "layout": {
            "nodeIndex": [0, 1, 2, 3, 4, 5],
            "bounds": [[0, 0, 800, 600], [8, 8, 80, 30], [8, 40, 200, 30],
                       [8, 80, 200, 30], [8, 120, 20, 20], [8, 150, 60, 20]],
        },
    }],
}


# --- iframe fixtures: a main page with one same-process child frame ---------- #
_IFRAME_MAIN = [
    _ax("1", "RootWebArea", backend=100, children=["2", "3"]),
    _ax("2", "button", "Outer", backend=101, parent="1", props={"focusable": True}),
    _ax("3", "Iframe", backend=110, parent="1"),  # empty childIds — the dead end
]
_IFRAME_CHILD = [
    _ax("1", "RootWebArea", backend=200, children=["2"]),
    _ax("2", "button", "Inner", backend=201, parent="1", props={"focusable": True}),
]
_IFRAME_DOM = {
    "strings": ["MAIN", "CHILD", "about:outer", "about:inner"],
    "documents": [
        {"frameId": 0, "documentURL": 2, "contentWidth": 800, "contentHeight": 600,
         "nodes": {"backendNodeId": [100, 101, 110], "nodeName": [-1, -1, -1],
                   "attributes": [[], [], []]},
         "layout": {"nodeIndex": [0, 1, 2],
                    "bounds": [[0, 0, 800, 600], [8, 8, 80, 30], [50, 60, 300, 200]]}},
        {"frameId": 1, "documentURL": 3, "contentWidth": 300, "contentHeight": 200,
         "nodes": {"backendNodeId": [200, 201], "nodeName": [-1, -1], "attributes": [[], []]},
         "layout": {"nodeIndex": [0, 1], "bounds": [[0, 0, 300, 200], [8, 8, 80, 30]]}},
    ],
}


def _iframe_responder(method: str, params: dict):
    if method == "Accessibility.getFullAXTree":
        return {"nodes": _IFRAME_CHILD if params.get("frameId") == "CHILD" else _IFRAME_MAIN}
    if method == "Page.getFrameTree":
        return {"frameTree": {"frame": {"id": "MAIN"},
                              "childFrames": [{"frame": {"id": "CHILD"}}]}}
    if method == "DOM.getFrameOwner":
        return {"backendNodeId": 110}  # the <iframe> element
    if method == "DOMSnapshot.captureSnapshot":
        return _IFRAME_DOM
    if method in ("DOM.enable", "Page.enable", "Runtime.enable"):
        return {}
    raise AssertionError(f"unexpected CDP method {method}")


def _driver_on(responder=_fixture_responder) -> tuple[browser.BrowserDriver, ScriptedTransport]:
    """A BrowserDriver whose session is a scripted transport (no real browser)."""
    d = browser.BrowserDriver(endpoint="http://scripted")
    transport = ScriptedTransport(responder)
    d._session = _cdp.CDPSession(transport)
    d._target_id = "TAB1"
    d._enabled = True
    return d, transport


# --------------------------------------------------------------------------- #
# CDP session
# --------------------------------------------------------------------------- #
def test_cdp_session_skips_events_and_raises_on_error() -> None:
    class T:
        def __init__(self):
            self.q = []

        def send(self, payload):
            mid = json.loads(payload)["id"]
            # an unrelated event, then the real reply — call() must skip the event
            self.q = [json.dumps({"method": "Page.frameNavigated", "params": {}}),
                      json.dumps({"id": mid, "result": {"ok": 1}})]

        def recv(self, timeout=None):
            return self.q.pop(0)

        def close(self):
            pass

    sess = _cdp.CDPSession(T())
    assert sess.call("Foo.bar") == {"ok": 1}
    assert sess.drain_events()[0]["method"] == "Page.frameNavigated"

    sess_err = _cdp.CDPSession(ScriptedTransport(
        lambda m, p: (_ for _ in ()).throw(_CDPError("nope"))))
    with pytest.raises(ComputerUseError) as ei:
        sess_err.call("Bad.method")
    assert ei.value.code is ErrorCode.UNSUPPORTED and "nope" in str(ei.value)


# --------------------------------------------------------------------------- #
# DOMSnapshot join
# --------------------------------------------------------------------------- #
def test_parse_dom_snapshot_geometry_and_secure() -> None:
    geometry, secure = _cdp_ax.parse_dom_snapshot(_DOM_SNAPSHOT)
    assert geometry[101] == (8, 8, 80, 30)  # button box, document CSS px
    assert geometry[100] == (0, 0, 800, 600)
    assert secure == frozenset({103})  # only the type=password input


# --------------------------------------------------------------------------- #
# accessor role/flag mapping
# --------------------------------------------------------------------------- #
def test_cdp_accessor_roles_flags_and_stable_id() -> None:
    geometry, secure = _cdp_ax.parse_dom_snapshot(_DOM_SNAPSHOT)
    acc = _cdp_ax.CDPAccessor(_AX_NODES, geometry, secure)
    by_backend = {acc.read(n).stable_id: acc.read(n) for n in _AX_NODES}

    assert by_backend["101"].role == "AXButton" and by_backend["101"].actions == ("AXPress",)
    assert by_backend["102"].role == "AXTextField"  # editable, plaintext, single-line
    assert by_backend["103"].role == "AXSecureTextField"  # password -> secure
    assert by_backend["104"].role == "AXCheckBox" and by_backend["104"].checked is True
    assert by_backend["105"].role == "AXLink" and by_backend["105"].actions == ("AXPress",)
    assert acc.root()["nodeId"] == "1"


def test_cdp_accessor_multiline_textbox_is_textarea() -> None:
    node = _ax("9", "textbox", "Bio", backend=9, props={"editable": "plaintext", "multiline": True})
    acc = _cdp_ax.CDPAccessor([node], {9: (0, 0, 100, 60)}, frozenset())
    assert acc.read(node).role == "AXTextArea"


# --------------------------------------------------------------------------- #
# iframe stitching
# --------------------------------------------------------------------------- #
def test_stitch_frames_grafts_child_under_owner_iframe() -> None:
    pooled = _cdp_ax.stitch_frames([
        {"nodes": _IFRAME_MAIN, "owner_backend": None},
        {"nodes": _IFRAME_CHILD, "owner_backend": 110},  # owned by the Iframe node (backend 110)
    ])
    by_id = {n["nodeId"]: n for n in pooled}
    # node ids are namespaced per frame so they never collide across frames
    assert "0:1" in by_id and "1:1" in by_id
    iframe_node = next(n for n in pooled if n.get("backendDOMNodeId") == 110)
    assert "1:1" in iframe_node["childIds"]  # child frame root grafted under the <iframe>
    assert by_id["1:1"]["parentId"] == iframe_node["nodeId"]
    # exactly one root survives (the main frame)
    assert [n["nodeId"] for n in pooled if not n.get("parentId")] == ["0:1"]


def test_build_frame_offsets_composes_nested() -> None:
    raw = {110: (50, 60, 300, 200), 210: (5, 5, 100, 100)}
    frames = [
        {"id": "MAIN", "parent_id": None, "owner_backend": None},
        {"id": "CHILD", "parent_id": "MAIN", "owner_backend": 110},
        {"id": "GRAND", "parent_id": "CHILD", "owner_backend": 210},
    ]
    offsets = _cdp_ax.build_frame_offsets(raw, frames)
    assert offsets["MAIN"] == (0.0, 0.0)
    assert offsets["CHILD"] == (50, 60)
    assert offsets["GRAND"] == (55, 65)  # parent offset + owner's frame-local position


def test_browser_snapshot_includes_iframe_content_offset() -> None:
    d, _ = _driver_on(_iframe_responder)
    snap = d.snapshot(Scope.WINDOW, "TAB1")
    titles = {e.title: e for e in snap.elements}
    assert "Outer" in titles and titles["Outer"].clickable  # main-frame button
    inner = titles["Inner"]  # button INSIDE the iframe — invisible without stitching
    assert inner.clickable and inner.role == "AXButton"
    # its box was shifted into the top document's space (iframe at 50,60 + 8,8)
    assert inner.bounds.x == 58 and inner.bounds.y == 68


def test_browser_snapshot_survives_cross_origin_frame() -> None:
    def oopif(method, params):
        if method == "Accessibility.getFullAXTree" and params.get("frameId") == "CHILD":
            raise _CDPError("Frame with the given id is not found (OOPIF)")
        return _iframe_responder(method, params)

    d, _ = _driver_on(oopif)
    snap = d.snapshot(Scope.WINDOW, "TAB1")  # must not raise
    titles = {e.title for e in snap.elements}
    assert "Outer" in titles and "Inner" not in titles  # OOPIF skipped, main intact


# --------------------------------------------------------------------------- #
# observe: full snapshot through the shared engine
# --------------------------------------------------------------------------- #
def test_browser_snapshot_builds_pruned_indexed_elements() -> None:
    d, _ = _driver_on()
    snap = d.snapshot(Scope.WINDOW, "TAB1")
    roles = {e.title: e for e in snap.elements}
    assert roles["Save"].clickable and roles["Save"].role == "AXButton"
    assert roles["Name"].editable and not roles["Name"].secure
    assert roles["Password"].secure and roles["Password"].editable
    assert roles["Agree"].checked is True
    assert roles["Home"].clickable and roles["Home"].role == "AXLink"
    # geometry survived the DOMSnapshot join (document coords, scale 1.0)
    assert roles["Save"].bounds.width == 80 and roles["Save"].bounds.height == 30


# --------------------------------------------------------------------------- #
# act: a11y-first, coordinate-free
# --------------------------------------------------------------------------- #
def test_browser_press_click_and_focus_editable() -> None:
    d, t = _driver_on()
    snap = d.snapshot(Scope.WINDOW, "TAB1")
    save = next(e for e in snap.elements if e.title == "Save")
    name = next(e for e in snap.elements if e.title == "Name")

    assert d.press_element(save) is True
    # a plain element is clicked via callFunctionOn this.click()
    call = next(p for m, p in t.sent if m == "Runtime.callFunctionOn")
    assert "this.click()" in call["functionDeclaration"]
    assert ("DOM.resolveNode", {"backendNodeId": 101}) in t.sent

    t.sent.clear()
    assert d.press_element(name) is True  # editable -> focus, not click
    call = next(p for m, p in t.sent if m == "Runtime.callFunctionOn")
    assert "this.focus()" in call["functionDeclaration"]


def test_browser_press_refuses_secure_field() -> None:
    d, t = _driver_on()
    snap = d.snapshot(Scope.WINDOW, "TAB1")
    pw = next(e for e in snap.elements if e.title == "Password")
    assert d.press_element(pw) is False  # never focus/activate a password field
    assert d.set_value(pw, "hunter2") is False
    assert not any(m == "Input.insertText" for m in t.methods())


def test_browser_type_and_set_value_emit_expected_cdp() -> None:
    d, t = _driver_on()
    snap = d.snapshot(Scope.WINDOW, "TAB1")
    name = next(e for e in snap.elements if e.title == "Name")

    d.type_text("hello")
    assert ("Input.insertText", {"text": "hello"}) in t.sent

    t.sent.clear()
    assert d.set_value(name, "Alice") is True
    call = next(p for m, p in t.sent if m == "Runtime.callFunctionOn")
    assert call["arguments"] == [{"value": "Alice"}]
    assert "dispatchEvent" in call["functionDeclaration"]  # fires input/change


def test_browser_type_dry_run_and_empty_are_noops() -> None:
    d, t = _driver_on()
    d.type_text("", dry_run=False)
    d.type_text("x", dry_run=True)
    assert not any(m == "Input.insertText" for m in t.methods())


# --------------------------------------------------------------------------- #
# key chords
# --------------------------------------------------------------------------- #
def test_key_events_named_printable_and_chord() -> None:
    enter = browser._key_events("enter")
    assert [e["type"] for e in enter] == ["keyDown", "keyUp"]
    assert enter[0]["key"] == "Enter" and enter[0]["windowsVirtualKeyCode"] == 13

    a = browser._key_events("a")
    assert a[0]["text"] == "a"  # a bare printable inserts text

    chord = browser._key_events("cmd+a")
    assert chord[0]["modifiers"] == 4 and chord[0]["key"] == "a"
    assert "text" not in chord[0]  # cmd+a is a shortcut, not text insertion

    for bad in ("", "cmd+", "ctrl+nope", "frobnicate"):
        with pytest.raises(ValueError):
            browser._key_events(bad)


def test_browser_key_chord_dispatches_key_events() -> None:
    d, t = _driver_on()
    d.key_chord("cmd+a")
    key_events = [p for m, p in t.sent if m == "Input.dispatchKeyEvent"]
    assert len(key_events) == 2 and key_events[0]["modifiers"] == 4
    d.key_chord("enter", dry_run=True)  # validates only, no new dispatch
    assert len([m for m in t.methods() if m == "Input.dispatchKeyEvent"]) == 2


# --------------------------------------------------------------------------- #
# capture + windowing
# --------------------------------------------------------------------------- #
def test_browser_navigate_waits_for_ready_and_launch_maps_to_url() -> None:
    states = iter(["loading", "loading", "complete"])  # readyState settles after 2 polls

    def responder(method, params):
        if method == "Page.navigate":
            assert params["url"] == "https://example.com"
            return {"frameId": "F", "loaderId": "L"}
        if method == "Runtime.evaluate":
            return {"result": {"value": next(states, "complete")}}
        if method in ("DOM.enable", "Page.enable", "Runtime.enable"):
            return {}
        raise AssertionError(method)

    d, t = _driver_on(responder)
    d.navigate("https://example.com", timeout_s=5)
    assert ("Page.navigate", {"url": "https://example.com"}) in t.sent
    assert sum(1 for m in t.methods() if m == "Runtime.evaluate") == 3  # polled until complete

    # launch_app is the browser analog of "open": a URL navigates...
    t.sent.clear()
    states2 = iter(["complete"])
    d._session = _cdp.CDPSession(ScriptedTransport(
        lambda m, p: ({"result": {"value": next(states2, "complete")}} if m == "Runtime.evaluate"
                      else {})))
    d.launch_app("https://example.com/next")
    assert any(m == "Page.navigate" for m in d._session._t.methods())


def test_gated_runtime_runs_on_the_browser_driver(tmp_path) -> None:
    """The WHOLE gated Runtime — grants, ref resolution, recheck, verify, audit —
    runs on the browser backend, with app identity resolved through the driver
    (tabs), not the OS system-ops. Mirrors the Windows/Linux full-Runtime proof."""
    from computeruse import safety, server

    d, t = _driver_on()
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier("TAB1", safety.Tier.FULL)  # grant the tab (the browser's "app")
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=d)

    # observe through the gate (READ), resolving "TAB1" via the driver
    out = rt.desktop_snapshot("TAB1", scope="window")
    assert "Save" in out and rt._current is not None
    save = next(e for e in rt._current.elements if e.title == "Save")

    # act through the gate (CLICK) with an Effect Receipt — a11y-first, no OS app
    t.sent.clear()
    result = rt.click(ref=save.ref, verify=True)
    assert "clicked" in result and "effect:" in result  # verify diff appended
    assert any("this.click()" in p.get("functionDeclaration", "")
               for m, p in t.sent if m == "Runtime.callFunctionOn")


def test_browser_launch_app_rejects_non_url() -> None:
    d, _ = _driver_on()
    with pytest.raises(ComputerUseError) as ei:
        d.launch_app("com.apple.Safari")  # not a URL
    assert ei.value.code is ErrorCode.UNSUPPORTED


def test_browser_screenshot_returns_png_and_display() -> None:
    d, _ = _driver_on()
    shot = d.screenshot()
    assert shot.png.startswith(b"\x89PNG")
    assert shot.display.width == 800 and shot.display.scale == 1.0


def test_browser_missing_websocket_client_is_structured(monkeypatch) -> None:
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "websocket":
            raise ImportError("no websocket")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ComputerUseError) as ei:
        _cdp.connect("ws://x")
    assert ei.value.code is ErrorCode.UNSUPPORTED and "browser" in str(ei.value.detail)


# --------------------------------------------------------------------------- #
# live: real headless Chromium (opt-in — only when an endpoint is reachable)
# --------------------------------------------------------------------------- #
def _live_endpoint() -> str | None:
    endpoint = os.environ.get("COMPUTERUSE_CDP_ENDPOINT", "http://127.0.0.1:9222")
    try:
        _cdp.page_targets(endpoint)
        return endpoint
    except Exception:
        return None


@pytest.mark.skipif(_live_endpoint() is None,
                    reason="no live CDP endpoint (set COMPUTERUSE_CDP_ENDPOINT / run Chrome "
                           "--remote-debugging-port=9222)")
def test_live_observe_act_verify() -> None:
    import urllib.parse

    endpoint = _live_endpoint()
    d = browser.BrowserDriver(endpoint=endpoint)
    sess = d._connect()
    html = ("<h1>hi</h1><button id=b onclick=\"document.title='CLICKED'\">Go</button>"
            "<input id=t placeholder=Name>")
    d.navigate("data:text/html," + urllib.parse.quote(html))  # load-aware; no fixed sleep

    snap = d.snapshot(Scope.WINDOW, d._target_id)
    go = next(e for e in snap.elements if e.title == "Go")
    field = next(e for e in snap.elements if e.editable)
    assert go.clickable

    assert d.press_element(go) is True
    title = sess.call("Runtime.evaluate", {"expression": "document.title"})["result"]["value"]
    assert title == "CLICKED"  # a11y-first click, no coordinates

    assert d.press_element(field) is True  # focus
    d.type_text("Alice")
    val = sess.call("Runtime.evaluate",
                    {"expression": "document.getElementById('t').value"})["result"]["value"]
    assert val == "Alice"
    d._reset()


@pytest.mark.skipif(_live_endpoint() is None,
                    reason="no live CDP endpoint (set COMPUTERUSE_CDP_ENDPOINT / run Chrome "
                           "--remote-debugging-port=9222)")
def test_live_iframe_content_is_observable_and_actionable() -> None:
    import urllib.parse

    d = browser.BrowserDriver(endpoint=_live_endpoint())
    # srcdoc keeps the child same-origin (same process), so getFullAXTree(frameId)
    # reaches it — the common embedded-form/widget case.
    outer = ("<button>OuterBtn</button>"
             "<iframe width=300 height=200 srcdoc=\"<button id=i>InnerBtn</button>\"></iframe>")
    d.navigate("data:text/html," + urllib.parse.quote(outer))  # load-aware; no fixed sleep

    snap = d.snapshot(Scope.WINDOW, d._target_id)
    titles = {e.title for e in snap.elements}
    assert "OuterBtn" in titles
    inner = next(e for e in snap.elements if e.title == "InnerBtn")  # stitched from the child frame
    assert inner.clickable and inner.role == "AXButton"
    assert d.press_element(inner) is True  # coordinate-free click INTO the iframe
    d._reset()
