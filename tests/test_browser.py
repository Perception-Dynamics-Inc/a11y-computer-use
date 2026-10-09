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

from a11y_computer_use.drivers import _cdp, _cdp_ax, browser
from a11y_computer_use.schema import ComputerUseError, ErrorCode, Scope


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
        # Parallel to ``sent``. None addresses the page session. Kept off
        # ``sent`` so ``(method, params) in t.sent`` stays a pair.
        self.session_ids: list[str | None] = []
        self.current_session: str | None = None
        self._inbox: list[str] = []

    def send(self, payload: str) -> None:
        msg = json.loads(payload)
        self.sent.append((msg["method"], msg.get("params", {})))
        self.current_session = msg.get("sessionId")
        self.session_ids.append(msg.get("sessionId"))
        try:
            result = self.responder(msg["method"], msg.get("params", {}))
        except _CDPError as exc:
            self._inbox.append(json.dumps({"id": msg["id"], "error": {"message": str(exc)}}))
            return
        events: list = []
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], list):
            result, events = result
        for event in events:
            # Queued before the reply so CDPSession buffers it while matching id.
            self._inbox.append(json.dumps(event))
        reply: dict = {"id": msg["id"], "result": result or {}}
        if msg.get("sessionId"):
            reply["sessionId"] = msg["sessionId"]
        self._inbox.append(json.dumps(reply))

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
        return {"cssVisualViewport": {"pageX": 0, "pageY": 0},
                "cssContentSize": {"width": 800, "height": 600}}
    if method == "Runtime.evaluate":
        # The focused-password probe type_text runs; the fixture page has no
        # focused password field. Tests that need "focused" override this.
        return {"result": {"type": "boolean", "value": False}}
    if method in ("Input.insertText", "Input.dispatchKeyEvent", "Input.dispatchMouseEvent",
                  "DOM.enable", "Page.enable", "Runtime.enable", "Runtime.releaseObject",
                  "Page.addScriptToEvaluateOnNewDocument"):  # the WebMCP recorder install
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
    geometry, secure, empty_numbers = _cdp_ax.parse_dom_snapshot(_DOM_SNAPSHOT)
    assert geometry[101] == (8, 8, 80, 30)  # button box, document CSS px
    assert geometry[100] == (0, 0, 800, 600)
    assert secure == frozenset({103})  # only the type=password input
    assert empty_numbers == frozenset()


# --------------------------------------------------------------------------- #
# accessor role/flag mapping
# --------------------------------------------------------------------------- #
def test_cdp_accessor_roles_flags_and_stable_id() -> None:
    geometry, secure, _empty = _cdp_ax.parse_dom_snapshot(_DOM_SNAPSHOT)
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


_OOPIF_MAIN_AX = [
    _ax("1", "RootWebArea", backend=100, children=["2", "3"]),
    _ax("2", "button", "Outer", backend=101, parent="1", props={"focusable": True}),
    _ax("3", "Iframe", backend=110, parent="1"),
]
_OOPIF_CHILD_AX = [
    _ax("1", "RootWebArea", backend=1, children=["2"]),
    # Backend 8 collides with a node in the parent document. The session key
    # is what keeps this button's box at the iframe, not at (1, 1).
    _ax("2", "button", "Inner", backend=8, parent="1", props={"focusable": True}),
]
_OOPIF_PARENT_DOM = {
    "strings": ["MAIN"],
    "documents": [{
        "frameId": 0, "contentWidth": 800, "contentHeight": 600,
        "nodes": {"backendNodeId": [100, 101, 110, 8], "nodeName": [-1, -1, -1, -1],
                  "attributes": [[], [], [], []]},
        "layout": {"nodeIndex": [0, 1, 2, 3],
                   "bounds": [[0, 0, 800, 600], [8, 8, 80, 30], [50, 60, 300, 200],
                              [1, 1, 9, 9]]},
    }],
}
_OOPIF_CHILD_DOM = {
    "strings": ["CHILD"],
    "documents": [{
        "frameId": 0, "contentWidth": 300, "contentHeight": 200,
        "nodes": {"backendNodeId": [1, 8], "nodeName": [-1, -1], "attributes": [[], []]},
        "layout": {"nodeIndex": [0, 1], "bounds": [[0, 0, 300, 200], [8, 8, 80, 30]]},
    }],
}


class _OopifResponder:
    """Page.getFrameTree has no child. The iframe arrives as an attach event.

    The event is injected only on the first setAutoAttach. A second snapshot
    has to reuse the session the driver remembered.
    """

    def __init__(self) -> None:
        self.transport: ScriptedTransport | None = None
        self.attach_calls = 0

    def __call__(self, method: str, params: dict):
        session = self.transport.current_session if self.transport is not None else None
        if method == "Target.setAutoAttach":
            self.attach_calls += 1
            event = {
                "method": "Target.attachedToTarget",
                "params": {
                    "sessionId": "CHILDSESS",
                    "targetInfo": {
                        "targetId": "CHILD",
                        "type": "iframe",
                        "url": "http://localhost:9/child",
                        "title": "child",
                        "parentFrameId": "MAIN",
                    },
                },
            }
            if self.attach_calls == 1:
                return {}, [event]
            return {}
        if method == "Accessibility.getFullAXTree":
            nodes = _OOPIF_CHILD_AX if session == "CHILDSESS" else _OOPIF_MAIN_AX
            return {"nodes": nodes}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "MAIN"}, "childFrames": []}}
        if method == "DOM.getFrameOwner":
            assert params.get("frameId") == "CHILD"
            assert session in (None, "")
            return {"backendNodeId": 110}
        if method == "DOMSnapshot.captureSnapshot":
            return _OOPIF_CHILD_DOM if session == "CHILDSESS" else _OOPIF_PARENT_DOM
        if method in ("DOM.enable", "Page.enable", "Runtime.enable", "Accessibility.enable",
                      "Runtime.releaseObject"):
            return {}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": f"obj-{params.get('backendNodeId')}"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"type": "undefined"}}
        raise AssertionError(f"unexpected CDP method {method} session={session}")


def test_browser_snapshot_includes_cross_origin_iframe_and_presses_in_its_session() -> None:
    responder = _OopifResponder()
    driver, transport = _driver_on(responder)
    responder.transport = transport
    snap = driver.snapshot(Scope.WINDOW, "TAB1")
    titles = {el.title: el for el in snap.elements}
    assert "Outer" in titles
    inner = titles["Inner"]
    assert inner.clickable and inner.role == "AXButton"
    # iframe at (50, 60) plus the child button at (8, 8); not the parent node
    # that happens to reuse backend id 8 at (1, 1).
    assert inner.bounds.x == 58 and inner.bounds.y == 68
    assert inner.stable_id == "CHILDSESS:8"

    again = driver.snapshot(Scope.WINDOW, "TAB1")
    assert any(el.title == "Inner" and el.stable_id == "CHILDSESS:8" for el in again.elements)
    assert responder.attach_calls == 2  # the second attach emitted no new target

    transport.sent.clear()
    transport.session_ids.clear()
    assert driver.press_element(inner) is True
    paired = list(zip(transport.sent, transport.session_ids, strict=True))
    assert any(
        method == "DOM.resolveNode" and params.get("backendNodeId") == 8 and sid == "CHILDSESS"
        for (method, params), sid in paired
    )
    assert any(
        method == "Runtime.callFunctionOn" and sid == "CHILDSESS"
        and "this.click()" in params.get("functionDeclaration", "")
        for (method, params), sid in paired
    )


def test_cdp_call_puts_session_id_on_the_message() -> None:
    class T:
        def __init__(self) -> None:
            self.sent: list[dict] = []
            self.q: list[str] = []

        def send(self, payload: str) -> None:
            msg = json.loads(payload)
            self.sent.append(msg)
            self.q.append(json.dumps({
                "id": msg["id"], "sessionId": msg.get("sessionId"), "result": {"ok": True},
            }))

        def recv(self, timeout=None) -> str:
            return self.q.pop(0)

        def close(self) -> None:
            pass

    transport = T()
    session = _cdp.CDPSession(transport)
    assert session.call("DOM.resolveNode", {"backendNodeId": 3}, session_id="CHILDSESS") == {"ok": True}
    assert transport.sent[0]["sessionId"] == "CHILDSESS"
    assert transport.sent[0]["params"] == {"backendNodeId": 3}
    session.call("Page.getFrameTree")
    assert "sessionId" not in transport.sent[1]


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


def test_browser_set_value_rejects_a_missing_option_and_a_bad_number() -> None:
    """The page function returns a refusal. The driver raises before reporting success."""
    answers = {
        "invalid_option": {
            "ok": False, "code": "invalid_option",
            "options": ["Kazakhstan", "Japan", "Peru"],
        },
        "invalid_number": {"ok": False, "code": "invalid_number", "min": 0, "max": 10},
        "out_of_range": {"ok": False, "code": "out_of_range", "min": 0, "max": 10},
        "mismatch": {"ok": False, "code": "mismatch", "actual": ""},
        "ok": {"ok": True, "actual": "Peru"},
    }

    def responder(method, params):
        if method == "Runtime.callFunctionOn":
            arg = params["arguments"][0]["value"]
            return {"result": {"type": "object", "value": answers[arg]}}
        return _fixture_responder(method, params)

    d, t = _driver_on(responder)
    snap = d.snapshot(Scope.WINDOW, "TAB1")
    field = next(e for e in snap.elements if e.title == "Name")
    with pytest.raises(ValueError) as exc:
        d.set_value(field, "invalid_option")
    assert "Kazakhstan" in str(exc.value) and "Peru" in str(exc.value)
    with pytest.raises(ValueError) as exc:
        d.set_value(field, "invalid_number")
    assert "not a number" in str(exc.value) and "0" in str(exc.value) and "10" in str(exc.value)
    with pytest.raises(ValueError) as exc:
        d.set_value(field, "out_of_range")
    assert "outside" in str(exc.value) and "0" in str(exc.value) and "10" in str(exc.value)
    with pytest.raises(ComputerUseError) as exc:
        d.set_value(field, "mismatch")
    assert exc.value.detail["reason"] == "text_mismatch"
    assert d.set_value(field, "ok") is True
    declaration = next(p["functionDeclaration"] for m, p in t.sent if m == "Runtime.callFunctionOn")
    assert "SELECT" in declaration and "invalid_option" in declaration
    assert "number" in declaration and "range" in declaration


def test_set_value_javascript_rejects_before_writing(monkeypatch) -> None:
    """The real page function, run by node against stand-in elements. Not a browser."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = """
const fn = %s;
function run(el, value) {
  const before = el.value;
  const result = fn.call(el, value);
  return {result, value: el.value, innerHTML: el.innerHTML, before};
}
const select = {
  tagName: 'SELECT', type: '', value: 'Kazakhstan', selectedIndex: 0,
  options: [
    {value: 'Kazakhstan', text: 'Kazakhstan', label: ''},
    {value: 'Japan', text: 'Japan', label: ''},
    {value: 'Peru', text: 'Peru', label: ''},
  ],
  dispatchEvent() {},
};
const number = {
  tagName: 'INPUT', type: 'number', value: '3', min: '0', max: '10',
  dispatchEvent() {},
};
const range = {
  tagName: 'INPUT', type: 'range', value: '40', min: '0', max: '100',
  dispatchEvent() {},
};
const text = {tagName: 'INPUT', type: 'text', value: 'Paris', dispatchEvent() {}};
const out = {
  mars: run(select, 'Mars'),
  peru: run(select, 'Peru'),
  abc: run(number, 'abc'),
  fifteen: run(number, '15'),
  seven: run(number, '7'),
  high: run(range, '150'),
  mid: run(range, '55'),
  lima: run(text, 'Lima'),
  editor: run({
    tagName: 'DIV', isContentEditable: true, innerHTML: 'Hello world',
    _text: 'Hello world',
    get textContent(){ return this._text; },
    set textContent(v){ this._text = String(v); },
    get innerText(){ return this._text + '\\n'; },
    dispatchEvent() {},
  }, 'Set 0'),
  nbsp: run({
    tagName: 'DIV', isContentEditable: true, innerHTML: 'Hello world',
    _text: 'Hello world',
    get textContent(){ return this._text; },
    set textContent(v){ this._text = String(v).replace(/ /g, '\\u00a0'); },
    get innerText(){ return this._text; },
    dispatchEvent() {},
  }, 'Set 0'),
  stuck: run({
    tagName: 'DIV', isContentEditable: true, innerHTML: 'Hello world',
    textContent: 'Hello world',
    get innerText(){ return 'nope'; },
    dispatchEvent() {},
  }, 'Set 0'),
};
process.stdout.write(JSON.stringify(out));
""" % browser._SET_VALUE_FN
    completed = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
    got = json.loads(completed.stdout)
    assert got["mars"]["result"]["code"] == "invalid_option"
    assert "Japan" in got["mars"]["result"]["options"]
    assert got["mars"]["value"] == "Kazakhstan"
    assert got["peru"]["result"]["ok"] is True and got["peru"]["value"] == "Peru"
    assert got["abc"]["result"]["code"] == "invalid_number" and got["abc"]["value"] == "3"
    assert got["fifteen"]["result"]["code"] == "out_of_range" and got["fifteen"]["value"] == "3"
    assert got["seven"]["result"]["ok"] is True and got["seven"]["value"] == "7"
    assert got["high"]["result"]["code"] == "out_of_range" and got["high"]["value"] == "40"
    assert got["mid"]["result"]["ok"] is True and got["mid"]["value"] == "55"
    assert got["lima"]["result"]["ok"] is True and got["lima"]["value"] == "Lima"
    assert got["editor"]["result"]["ok"] is True
    assert got["editor"]["result"]["actual"].startswith("Set 0")
    assert got["nbsp"]["result"]["ok"] is True
    assert got["stuck"]["result"]["code"] == "mismatch"
    assert got["stuck"]["innerHTML"] == "Hello world"


def test_cdp_snapshot_shows_selected_text_pressed_and_blank_number() -> None:
    nodes = [
        _ax("1", "RootWebArea", "page", backend=1, children=["2", "3", "4", "5", "6"]),
        _ax("2", "combobox", "Country", backend=2, parent="1", children=["21", "22"]),
        _ax("21", "option", "Kazakhstan", backend=21, parent="2", props={"selected": True}),
        _ax("22", "option", "Japan", backend=22, parent="2"),
        _ax("3", "listbox", "Fruits", backend=3, parent="1", children=["31"]),
        _ax("31", "option", "Banana", backend=31, parent="3", props={"selected": True}),
        _ax("4", "button", "Italic toggle", backend=4, parent="1", props={"pressed": True}),
        _ax("5", "spinbutton", "Empty", backend=5, parent="1"),
        _ax("6", "spinbutton", "Seats", backend=6, parent="1"),
    ]
    nodes[1]["value"] = {"value": "\ufffc"}
    nodes[4]["value"] = {"value": "\ufffc\ufffc\ufffc\ufffc"}
    nodes[7]["value"] = {"value": 0.0}
    nodes[8]["value"] = {"value": "3"}
    label = _ax("7", "StaticText", "Country \ufffc", backend=7)
    nodes.append(label)
    nodes[0]["childIds"].append("7")
    acc = _cdp_ax.CDPAccessor(nodes, {}, frozenset(), frozenset({5}))
    by_id = {node["nodeId"]: acc.read(node) for node in nodes}
    assert by_id["2"].value == "Kazakhstan"
    assert by_id["3"].value == "Banana"
    assert by_id["4"].checked is True
    assert by_id["5"].value is None
    assert by_id["6"].value == "3"
    assert by_id["7"].title == "Country"
    assert "\ufffc" not in by_id["7"].title


def test_cdp_paragraph_value_includes_inline_children() -> None:
    """Synthetic AX tree. The paragraph's own value is empty; the words are children."""
    nodes = [
        _ax("1", "RootWebArea", "page", backend=1, children=["2", "3", "4"]),
        _ax("2", "paragraph", "", backend=2, parent="1", children=["21", "22", "23", "24", "25"]),
        _ax("21", "StaticText", "The ", backend=21, parent="2"),
        _ax("22", "link", "quick brown", backend=22, parent="2"),
        _ax("23", "StaticText", " fox ", backend=23, parent="2"),
        _ax("24", "emphasis", "", backend=24, parent="2", children=["241"]),
        _ax("241", "StaticText", "jumps", backend=241, parent="24"),
        _ax("25", "StaticText", " over the lazy dog.", backend=25, parent="2"),
        _ax("3", "paragraph", "", backend=3, parent="1", children=["31"]),
        _ax("31", "button", "Para button", backend=31, parent="3"),
        _ax("4", "StaticText", "", backend=4, parent="1", children=["41"]),
        _ax("41", "checkbox", "Verify you are human", backend=41, parent="4"),
    ]
    acc = _cdp_ax.CDPAccessor(nodes, {}, frozenset())
    assert acc.read(nodes[1]).value == "The quick brown fox jumps over the lazy dog."
    assert acc.read(nodes[8]).value in (None, "")
    from a11y_computer_use import observe
    from tests.fixtures.trees import GEOMETRY

    geometry = {
        1: (0.0, 0.0, 800.0, 600.0),
        2: (8.0, 8.0, 500.0, 24.0),
        21: (8.0, 8.0, 40.0, 24.0),
        22: (48.0, 8.0, 90.0, 24.0),
        23: (138.0, 8.0, 40.0, 24.0),
        24: (178.0, 8.0, 40.0, 24.0),
        241: (178.0, 8.0, 40.0, 24.0),
        25: (218.0, 8.0, 140.0, 24.0),
        3: (8.0, 40.0, 120.0, 30.0),
        31: (8.0, 40.0, 100.0, 30.0),
        4: (8.0, 80.0, 220.0, 24.0),
        41: (8.0, 80.0, 200.0, 24.0),
    }
    acc = _cdp_ax.CDPAccessor(nodes, geometry, frozenset())
    snap = observe.build_snapshot(
        acc.root(), acc, scope=Scope.WINDOW, app="tab", pid=1, geometry=GEOMETRY,
    )
    assert observe.find_elements(snap, text="quick brown fox")
    assert any(el.title == "Para button" for el in snap.elements)
    assert any(el.title == "Verify you are human" and el.role == "AXCheckBox" for el in snap.elements)


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


def test_browser_scroll_wheel_sign_follows_the_tool_contract() -> None:
    """Positive dy scrolls content up (reader moves down): CDP wants a POSITIVE
    deltaY for that, so the driver must not negate. cu-arena's long_list task
    caught the inverted sign (every scroll down at the top of a list did nothing)."""
    from a11y_computer_use.schema import ScrollUnit

    d, t = _driver_on()
    snap = d.snapshot(Scope.WINDOW, "TAB1")
    button = next(e for e in snap.elements if e.role == "AXButton")
    d.scroll(button, dy=3, unit=ScrollUnit.LINES)
    wheel = next(p for m, p in t.sent if m == "Input.dispatchMouseEvent" and p.get("type") == "mouseWheel")
    assert wheel["deltaY"] == 120 and wheel["deltaX"] == 0  # 3 lines * 40 px, scrolls down
    d.scroll(button, dx=-2, dy=-1, unit=ScrollUnit.PIXELS)
    wheel = [p for m, p in t.sent if m == "Input.dispatchMouseEvent" and p.get("type") == "mouseWheel"][-1]
    assert wheel["deltaX"] == -2 and wheel["deltaY"] == -1


def test_browser_printable_key_refuses_a_focused_password(monkeypatch) -> None:
    d, t = _driver_on()
    monkeypatch.setattr(d, "_focused_is_password", lambda: True)
    with pytest.raises(ComputerUseError) as exc:
        d.key_chord("a")
    assert exc.value.code is ErrorCode.SECURE_FIELD
    assert exc.value.detail["api"] == "document.activeElement"
    with pytest.raises(ComputerUseError):
        d.key_chord("shift+a")
    assert [m for m, _p in t.sent if m == "Input.dispatchKeyEvent"] == []
    d.key_chord("tab")
    d.key_chord("ctrl+a")
    downs = [p for m, p in t.sent if m == "Input.dispatchKeyEvent" and p["type"] == "keyDown"]
    assert [p["key"] for p in downs] == ["Tab", "a"]
    assert "text" not in downs[0] and downs[1]["modifiers"] == 2
    monkeypatch.setattr(d, "_focused_is_password", lambda: False)
    d.key_chord("a")
    downs = [p for m, p in t.sent if m == "Input.dispatchKeyEvent" and p["type"] == "keyDown"]
    assert downs[-1]["text"] == "a" and downs[-1]["modifiers"] == 0


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
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F", "loaderId": "L"}}}
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
    from a11y_computer_use import safety, server

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


def test_console_entry_parsing() -> None:
    log = browser._console_entry({"method": "Runtime.consoleAPICalled",
                                  "params": {"type": "error", "args": [{"value": "boom"}, {"value": 42}]}})
    assert log == {"level": "error", "text": "boom 42"}
    exc = browser._console_entry({"method": "Runtime.exceptionThrown",
                                  "params": {"exceptionDetails": {"exception": {
                                      "description": "TypeError: x is not a function\n  at f"}}}})
    assert exc["level"] == "exception" and exc["text"] == "TypeError: x is not a function"
    assert browser._console_entry({"method": "Page.frameNavigated", "params": {}}) is None


def test_console_messages_drains_accumulates_and_clears() -> None:
    d, _ = _driver_on(lambda m, p: {})  # Runtime.evaluate (the drain kick) returns {}
    d._session._events = [  # events CDP pushed onto the buffer between actions
        {"method": "Runtime.consoleAPICalled", "params": {"type": "warning", "args": [{"value": "careful"}]}},
        {"method": "Runtime.exceptionThrown",
         "params": {"exceptionDetails": {"exception": {"description": "ReferenceError: nope"}}}},
    ]
    msgs = d.console_messages()
    assert {"level": "warning", "text": "careful"} in msgs
    assert any(m["level"] == "exception" and "ReferenceError" in m["text"] for m in msgs)
    assert d.console_messages() == []  # reading cleared the buffer


def test_runtime_console_gated_on_browser_and_unsupported_elsewhere(tmp_path) -> None:
    from a11y_computer_use import safety, server

    d, _ = _driver_on(lambda m, p: {})
    d._session._events = [{"method": "Runtime.consoleAPICalled",
                           "params": {"type": "error", "args": [{"value": "kaboom"}]}}]
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("TAB1", safety.Tier.READ)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "a"), driver=d)
    assert "kaboom" in rt.console("TAB1")  # gated READ, through the driver

    # a backend without a console feed answers UNSUPPORTED, not a crash
    rt.driver = type("NoConsole", (), {"name": "macos"})()
    with pytest.raises(ComputerUseError) as ei:
        rt.console("TAB1")
    assert ei.value.code is ErrorCode.UNSUPPORTED


def test_network_requests_join_status_and_failures() -> None:
    d, _ = _driver_on(lambda m, p: {})
    d._session._events = [
        {"method": "Network.requestWillBeSent",
         "params": {"requestId": "1", "request": {"method": "POST", "url": "/api/save"}}},
        {"method": "Network.responseReceived",
         "params": {"requestId": "1", "response": {"status": 200, "url": "/api/save"}}},
        {"method": "Network.requestWillBeSent",
         "params": {"requestId": "2", "request": {"method": "GET", "url": "/broken"}}},
        {"method": "Network.loadingFailed",
         "params": {"requestId": "2", "errorText": "net::ERR_FAILED"}},
    ]
    reqs = d.network_requests()
    assert {"method": "POST", "url": "/api/save", "status": 200} in reqs
    assert {"method": "GET", "url": "/broken", "error": "net::ERR_FAILED"} in reqs
    assert d.network_requests() == []  # cleared


def test_runtime_network_gated_and_unsupported_elsewhere(tmp_path) -> None:
    from a11y_computer_use import safety, server

    d, _ = _driver_on(lambda m, p: {})
    d._session._events = [
        {"method": "Network.requestWillBeSent",
         "params": {"requestId": "9", "request": {"method": "GET", "url": "/x"}}},
        {"method": "Network.responseReceived",
         "params": {"requestId": "9", "response": {"status": 404, "url": "/x"}}},
    ]
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("TAB1", safety.Tier.READ)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "a"), driver=d)
    assert "404" in rt.network("TAB1")

    rt.driver = type("NoNet", (), {"name": "linux"})()
    with pytest.raises(ComputerUseError) as ei:
        rt.network("TAB1")
    assert ei.value.code is ErrorCode.UNSUPPORTED


def test_cdp_session_event_buffer_is_bounded() -> None:
    from collections import deque
    sess = _cdp.CDPSession(ScriptedTransport(lambda m, p: {}), event_buffer=3)
    assert isinstance(sess._events, deque) and sess._events.maxlen == 3
    for i in range(10):
        sess._events.append({"i": i})
    assert len(sess._events) == 3  # oldest dropped, never unbounded


def test_mcp_server_exposes_console_and_network_only_on_browser(monkeypatch) -> None:
    """`A11Y_COMPUTER_USE_DRIVER=browser a11y_computer_use mcp` registers the browser-only
    feeds; the OS surfaces don't grow. Proves the conditional registration end
    to end, without a live browser."""
    import asyncio

    from a11y_computer_use import drivers, server

    async def names(srv):
        return {t.name for t in await srv.list_tools()}

    monkeypatch.setattr(drivers, "get_driver", lambda *a, **k: browser.BrowserDriver(endpoint="x"))
    browser_tools = asyncio.run(names(server.build_server()))
    assert {"console", "network"} <= browser_tools

    monkeypatch.setattr(drivers, "get_driver",
                        lambda *a, **k: type("Bare", (), {"name": "macos"})())
    os_tools = asyncio.run(names(server.build_server()))
    assert "console" not in os_tools and "network" not in os_tools
    assert "hover" in browser_tools and "hover" in os_tools


def test_browser_app_window_clipboard_tools_via_runtime(tmp_path, monkeypatch) -> None:
    """The app/window/clipboard TOOLS run on the browser backend (tabs), routed
    through the driver — app focus <tab> no longer crashes, list returns tabs,
    launch navigates, clipboard degrades cleanly."""
    import json as _json

    from a11y_computer_use import safety, server
    from a11y_computer_use.drivers import _cdp

    def responder(m, p):
        if m == "Page.navigate":
            return {"frameId": "F"}
        if m == "Runtime.evaluate":
            return {"result": {"value": "complete"}}
        if m in ("DOM.enable", "Page.enable", "Runtime.enable"):
            return {}
        raise AssertionError(m)

    d, _ = _driver_on(responder)
    monkeypatch.setattr(_cdp, "page_targets", lambda ep: [
        {"id": "TAB1", "title": "Home", "url": "https://home"},
        {"id": "TAB2", "title": "Docs", "url": "https://docs"}])
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("TAB1", safety.Tier.FULL)  # the bound/frontmost tab
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "a"), driver=d)

    assert {r["id"] for r in _json.loads(rt.app("list"))} == {"TAB1", "TAB2"}  # tabs, not OS apps
    assert any(w["app"] == "TAB1" for w in _json.loads(rt.window("list")))
    assert rt.app("focus", "TAB1") == "focused TAB1"  # was _activate(None) AttributeError
    assert "launched" in rt.app("launch", "data:text/html,<h1>x</h1>")  # navigates the tab

    assert rt.clipboard("read") == ""  # browser exposes no clipboard -> empty, not a crash
    with pytest.raises(ComputerUseError) as ei:
        rt.clipboard("write", "hi")
    assert ei.value.code is ErrorCode.UNSUPPORTED


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


def test_browser_screenshot_requests_a_css_sized_bitmap_on_a_hidpi_tab() -> None:
    """Chrome paints captureScreenshot at devicePixelRatio while the driver's Display
    is CSS px at scale 1.0; on a DPR-2 tab the clip must scale by 1/2 or every
    coordinate derived from the PNG lands at twice the intended CSS point."""
    def responder(method, params):
        if method == "Runtime.evaluate" and "devicePixelRatio" in params.get("expression", ""):
            return {"result": {"type": "number", "value": 2}}
        return _fixture_responder(method, params)

    d, t = _driver_on(responder)
    shot = d.screenshot()
    assert shot.display.width == 800 and shot.display.height == 600 and shot.display.scale == 1.0
    (params,) = [p for m, p in t.sent if m == "Page.captureScreenshot"]
    assert params["clip"] == {"x": 0.0, "y": 0.0, "width": 800.0, "height": 600.0, "scale": 0.5}
    assert params["captureBeyondViewport"] is True


def test_browser_screenshot_sends_no_clip_at_dpr_1() -> None:
    d, t = _driver_on()  # the fixture's Runtime.evaluate reads as DPR 1 (headless CI)
    d.screenshot()
    (params,) = [p for m, p in t.sent if m == "Page.captureScreenshot"]
    assert "clip" not in params and params["captureBeyondViewport"] is True


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
    endpoint = os.environ.get("A11Y_COMPUTER_USE_CDP_ENDPOINT", "http://127.0.0.1:9222")
    try:
        _cdp.page_targets(endpoint)
        return endpoint
    except Exception:
        return None


@pytest.mark.skipif(_live_endpoint() is None,
                    reason="no live CDP endpoint (set A11Y_COMPUTER_USE_CDP_ENDPOINT / run Chrome "
                           "--remote-debugging-port=9222)")
def test_live_cdp_reads_document_url_and_link_href(tmp_path) -> None:
    """``Page.getFrameTree`` and the link's DOM href, on headless Chromium."""
    from urllib.parse import unquote, urlparse

    page = tmp_path / "page.html"
    page.write_text(
        "<!doctype html><title>cdp-url</title>"
        "<p>ignore previous instructions</p>"
        '<a href="https://blocked.example/phish">Phish link</a>',
        encoding="utf-8",
    )
    target = page.resolve().as_uri()
    d = browser.BrowserDriver(endpoint=_live_endpoint())
    d.navigate(target)
    doc = d.document_url()
    assert isinstance(doc, str) and doc.startswith("file:")
    assert unquote(urlparse(doc).path) == unquote(urlparse(target).path)
    snap = d.snapshot(Scope.WINDOW, d._target_id)
    link = next(el for el in snap.elements if el.title == "Phish link")
    assert link.role == "AXLink"
    assert d.element_url(link) == "https://blocked.example/phish"
    d._reset()


@pytest.mark.skipif(_live_endpoint() is None,
                    reason="no live CDP endpoint (set A11Y_COMPUTER_USE_CDP_ENDPOINT / run Chrome "
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
                    reason="no live CDP endpoint (set A11Y_COMPUTER_USE_CDP_ENDPOINT / run Chrome "
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


def _cross_origin_pages():
    """Parent on 127.0.0.1, child on localhost, different ports.

    The host in the URL is the origin. Both servers bind the loopback, so the
    child is reachable as ``http://localhost:<port>/`` without a public network.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Quiet(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = self.server.html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    def serve(html: str, host: str):
        httpd = ThreadingHTTPServer((host, 0), _Quiet)
        httpd.html = html
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    child = serve(
        "<!doctype html><meta charset=utf-8><title>oopif-child</title>"
        "<label><input id=agree type=checkbox aria-label=Agree> Agree</label>"
        "<button id=go type=button>InnerGo</button>",
        "127.0.0.1",
    )
    child_port = child.server_address[1]
    parent = serve(
        "<!doctype html><meta charset=utf-8><title>oopif-parent</title>"
        "<button id=outer type=button>OuterBtn</button>"
        f"<iframe title=guest src=\"http://localhost:{child_port}/\" "
        "width=480 height=260></iframe>",
        "127.0.0.1",
    )
    parent_port = parent.server_address[1]
    return parent, child, f"http://127.0.0.1:{parent_port}/"


@pytest.mark.skipif(_live_endpoint() is None,
                    reason="no live CDP endpoint (set A11Y_COMPUTER_USE_CDP_ENDPOINT / run Chrome "
                           "--remote-debugging-port=9222)")
def test_live_cross_origin_iframe_checkbox_reads_checked() -> None:
    """Click the checkbox inside a cross-origin iframe and read checked back.

    Live against the Browser job's Chrome. The page is served locally; no
    third-party captcha host is contacted.
    """
    import time

    parent, child, url = _cross_origin_pages()
    driver = browser.BrowserDriver(endpoint=_live_endpoint())
    try:
        driver.navigate(url)
        deadline = time.monotonic() + 15
        box = None
        last = None
        while time.monotonic() < deadline:
            last = driver.snapshot(Scope.WINDOW, driver._target_id)
            titles = {el.title for el in last.elements}
            found = [
                el for el in last.elements
                if el.title == "Agree" and el.role == "AXCheckBox"
            ]
            if "OuterBtn" in titles and "InnerGo" in titles and found:
                box = found[0]
                break
            time.sleep(0.3)
        assert box is not None, [
            (el.role, el.title, el.checked) for el in (last.elements if last else [])
        ]
        assert box.checked is not True
        assert driver.press_element(box) is True
        deadline = time.monotonic() + 8
        checked = None
        while time.monotonic() < deadline:
            last = driver.snapshot(Scope.WINDOW, driver._target_id)
            again = next(
                (el for el in last.elements if el.title == "Agree" and el.role == "AXCheckBox"),
                None,
            )
            checked = None if again is None else again.checked
            if checked is True:
                break
            time.sleep(0.3)
        assert checked is True, [
            (el.role, el.title, el.checked) for el in last.elements
        ]
    finally:
        driver._reset()
        parent.shutdown()
        child.shutdown()


@pytest.mark.skipif(_live_endpoint() is None,
                    reason="no live CDP endpoint (set A11Y_COMPUTER_USE_CDP_ENDPOINT / run Chrome "
                           "--remote-debugging-port=9222)")
def test_live_console_captures_logs_and_exceptions() -> None:
    import urllib.parse

    d = browser.BrowserDriver(endpoint=_live_endpoint())
    page = ("<script>console.log('hi');console.error('boom');nope.bad()</script>")
    d.navigate("data:text/html," + urllib.parse.quote(page))
    msgs = d.console_messages()
    texts = {m["text"] for m in msgs}
    levels = {m["level"] for m in msgs}
    assert "boom" in texts and "error" in levels
    assert any(m["level"] == "exception" for m in msgs)  # the uncaught ReferenceError
    assert d.console_messages() == []  # cleared after reading
    d._reset()


@pytest.mark.skipif(_live_endpoint() is None,
                    reason="no live CDP endpoint (set A11Y_COMPUTER_USE_CDP_ENDPOINT / run Chrome "
                           "--remote-debugging-port=9222)")
def test_live_network_reports_status_and_failures() -> None:
    import urllib.parse

    d = browser.BrowserDriver(endpoint=_live_endpoint())
    # a same-doc fetch (200) and a fetch to an unsafe port (fails) — no external net
    page = ("<script>fetch('data:text/plain,ok');"
            "fetch('http://127.0.0.1:1/x').catch(()=>{})</script>")
    d.navigate("data:text/html," + urllib.parse.quote(page))
    reqs = d.network_requests()
    assert any(r.get("status") == 200 for r in reqs)  # the main document / data fetch
    assert any("error" in r for r in reqs)  # the unsafe-port fetch failed
    d._reset()


# --- a live reorder under a ref (the incident-gauntlet failure) ------------- #
def _list_page(rows: list[tuple[int, str]], first_y: int = 40):
    """AX + DOMSnapshot payloads for a list of rows: (backendDOMNodeId, title)."""
    nodes = [_ax("1", "RootWebArea", backend=100, children=["L"]),
             _ax("L", "list", "Services", backend=101, parent="1",
                 children=[f"r{i}" for i in range(len(rows))])]
    ids, bounds = [100, 101], [[0, 0, 800, 600], [0, first_y - 8, 400, 500]]
    for i, (backend, title) in enumerate(rows):
        nodes.append(_ax(f"r{i}", "row", title, backend=backend, parent="L",
                         props={"focusable": True}))
        ids.append(backend)
        bounds.append([8, first_y + 24 * i, 380, 22])
    dom = {"strings": ["DIV"], "documents": [{
        "contentWidth": 800, "contentHeight": 600,
        "nodes": {"backendNodeId": ids, "nodeName": [-1] * len(ids),
                  "attributes": [[] for _ in ids]},
        "layout": {"nodeIndex": list(range(len(ids))), "bounds": bounds},
    }]}
    return nodes, dom


def _phased_driver(before, after):
    phase = {"nodes": before[0], "dom": before[1]}

    def responder(method: str, params: dict):
        if method == "Accessibility.getFullAXTree":
            return {"nodes": phase["nodes"]}
        if method == "DOMSnapshot.captureSnapshot":
            return phase["dom"]
        return _fixture_responder(method, params)

    d, t = _driver_on(responder)

    def flip():
        phase["nodes"], phase["dom"] = after

    return d, t, flip


def test_click_after_a_live_reorder_is_stale_not_the_slot_occupant() -> None:
    """Two snapshots of a virtualized list: after a refresh the rows are new DOM
    nodes (new backend ids) in a new order and the target scrolled out of the
    rendered window. The old ref must raise stale_ref naming the row that now
    sits at that position, not resolve onto it."""
    before = _list_page([(201, "auth-gateway production"), (202, "email-router production"),
                         (203, "config-sync production")])
    after = _list_page([(311, "payments-api staging"), (312, "config-sync production"),
                        (313, "auth-gateway production")])
    d, t, flip = _phased_driver(before, after)
    snap = d.snapshot(Scope.WINDOW, "TAB1")
    target = next(el for el in snap.elements if el.title == "email-router production")
    flip()
    with pytest.raises(ComputerUseError) as exc:
        d.resolve_ref(snap, target.ref)
    assert exc.value.code is ErrorCode.STALE_REF
    assert exc.value.detail["reason"] == "title_changed"
    occupant = next(c for c in exc.value.detail["candidates"] if c.get("at_old_position"))
    assert occupant["title"] == "config-sync production"
    # Nothing was clicked: no mouse or DOM action reached the page.
    assert "Input.dispatchMouseEvent" not in t.methods()
    assert "DOM.resolveNode" not in t.methods()


def test_after_a_reorder_the_ref_follows_the_title_when_the_row_is_still_rendered() -> None:
    before = _list_page([(201, "auth-gateway production"), (202, "email-router production"),
                         (203, "config-sync production")])
    after = _list_page([(312, "config-sync production"), (313, "auth-gateway production"),
                        (314, "payments-api staging"), (315, "email-router production")])
    d, _t, flip = _phased_driver(before, after)
    snap = d.snapshot(Scope.WINDOW, "TAB1")
    target = next(el for el in snap.elements if el.title == "email-router production")
    flip()
    live = d.resolve_ref(snap, target.ref)
    assert live.title == "email-router production" and live.stable_id == "315"


_LIVE_PARA_HTML = (
    "<!doctype html><meta charset=utf-8><title>cuapara</title>"
    "<style>body{margin:8px;font:13px sans-serif}"
    ".row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}</style>"
    "<h1>Para variants</h1><div class=row>"
    "<p><label>Bravo <input id=bravo></label></p>"
    "<p>Read <a href='#x'>the docs link</a> now.</p>"
    "<p><button type=button>Para button</button></p>"
    "<p><input aria-label='Bare para input'></p>"
    "<p><input type=checkbox id=k> <label for=k>Para checkbox</label></p>"
    "<div><button type=button>Div button</button></div>"
    "</div>"
    "<p id=fox>The <a href='#a'>quick brown</a> fox <b>jumps</b> over the <em>lazy</em> dog.</p>"
    "<div id=ed contenteditable=true role=textbox aria-label='Editor A'>Hello world</div>"
    "<label><input id=human type=checkbox aria-label='Verify you are human'></label>"
    "<label><input type=checkbox aria-label='Accept terms'></label>"
    "<label><input type=radio name=r aria-label='Option one'></label>"
    "<label><input aria-label='Your answer'></label>"
    "<input type=checkbox aria-label='Bare checkbox'>"
)


@pytest.mark.skipif(_live_endpoint() is None,
                    reason="no live CDP endpoint (set A11Y_COMPUTER_USE_CDP_ENDPOINT / run Chrome "
                           "--remote-debugging-port=9222)")
def test_live_paragraph_controls_sentence_and_empty_label() -> None:
    """Live headless Chrome. Controls in a paragraph and in an empty label are listed."""
    import urllib.parse

    from a11y_computer_use import observe

    d = browser.BrowserDriver(endpoint=_live_endpoint())
    d.navigate("data:text/html," + urllib.parse.quote(_LIVE_PARA_HTML))
    snap = d.snapshot(Scope.WINDOW, d._target_id)
    rendered = observe.render_text(snap)
    for name in (
        "Bravo", "the docs link", "Para button", "Bare para input", "Para checkbox",
        "Div button", "Verify you are human", "Accept terms", "Option one",
        "Your answer", "Bare checkbox",
    ):
        assert name in rendered, rendered
    assert observe.find_elements(snap, text="quick brown fox")
    assert observe.find_elements(snap, text="The quick")
    human = next(el for el in snap.elements if el.title == "Verify you are human" and el.role == "AXCheckBox")
    assert d.press_element(human) is True
    sess = d._connect()
    checked = sess.call(
        "Runtime.evaluate",
        {"expression": "document.getElementById('human').checked", "returnByValue": True},
    )["result"]["value"]
    assert checked is True
    d._reset()


@pytest.mark.skipif(_live_endpoint() is None,
                    reason="no live CDP endpoint (set A11Y_COMPUTER_USE_CDP_ENDPOINT / run Chrome "
                           "--remote-debugging-port=9222)")
def test_live_contenteditable_set_value_and_type() -> None:
    """Live headless Chrome. set_value replaces the editor, and type inserts."""
    import urllib.parse

    d = browser.BrowserDriver(endpoint=_live_endpoint())
    sess = d._connect()
    d.navigate("data:text/html," + urllib.parse.quote(_LIVE_PARA_HTML))
    snap = d.snapshot(Scope.WINDOW, d._target_id)
    editor = next(el for el in snap.elements if el.title == "Editor A" and el.editable)
    before = sess.call(
        "Runtime.evaluate",
        {"expression": "document.getElementById('ed').innerText", "returnByValue": True},
    )["result"]["value"]
    assert "Hello world" in before
    assert d.set_value(editor, "Set 0") is True
    shown = sess.call(
        "Runtime.evaluate",
        {"expression": "document.getElementById('ed').innerText", "returnByValue": True},
    )["result"]["value"]
    assert shown.replace("\u00a0", " ").strip() == "Set 0"
    assert d.press_element(editor) is True
    d.type_text(" more")
    shown = sess.call(
        "Runtime.evaluate",
        {"expression": "document.getElementById('ed').innerText", "returnByValue": True},
    )["result"]["value"]
    assert "more" in shown.replace("\u00a0", " ")
    d._reset()
