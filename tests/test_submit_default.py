"""Explicit submit metadata is an observation/ranking hint, never an action."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi, _cdp_ax
from a11y_computer_use.schema import Scope
from tests.fixtures.trees import GEOMETRY


def test_submit_marker_survives_snapshot_and_ranks_without_changing_refs():
    nodes = [
        observe.RawNode(role="AXWindow", title="Form", position=(0, 0), size=(600, 400)),
        observe.RawNode(role="AXButton", title="Cancel", actions=("AXPress",)),
        observe.RawNode(role="AXButton", title="Register", actions=("AXPress",), default=True),
        observe.RawNode(role="AXButton", title="Submit", actions=("AXPress",)),
    ]
    nodes = [replace(node, position=(10, 10), size=(80, 30)) if i else node
             for i, node in enumerate(nodes)]

    class Accessor:
        def read(self, i):
            return nodes[i]
        def children(self, i):
            return [1, 2, 3] if i == 0 else []
    snap = observe.build_snapshot(0, Accessor(), scope=Scope.WINDOW, app="form", pid=1, geometry=GEOMETRY)
    assert [e.title for e in observe.find_elements(snap, role="button")] == ["Register", "Cancel", "Submit"]
    matches = observe.prefer_exact_targets(observe.find_elements(snap, text="submit"), "submit")
    assert [e.title for e in matches] == ["Register", "Submit"]
    assert [e.title for e in observe.find_elements(snap, text="Cancel")] == ["Cancel"]
    assert [e.title for e in observe.find_elements(snap)] == ["Form", "Cancel", "Register", "Submit"]
    assert "default" in observe.render_matches(snap, matches)
    assert "default" not in observe.render_matches(snap, [replace(matches[0], default=False)])
    assert observe._state_changes(matches[0], replace(matches[0], default=False)) == {}
    assert observe._identity(matches[0]) == observe._identity(replace(matches[0], default=False))
    assert matches[0].ref == next(e.ref for e in snap.elements if e.title == "Register")


def test_dom_submit_ownership_and_session_isolation():
    strings = []
    def intern(s):
        if s not in strings:
            strings.append(s)
        return strings.index(s)
    tags = ["DIV", "FORM", "BUTTON", "BUTTON", "INPUT", "BUTTON", "BUTTON", "FORM", "BUTTON", "INPUT"]
    fields = [{}, {"id": "f"}, {}, {"type": "button"}, {"type": "submit"}, {"form": "f"}, {"form": "missing"}, {"id": "g"}, {"type": "reset"}, {"type": "image"}]
    dom = {"strings": strings, "documents": [{"nodes": {
        "nodeName": [intern(t) for t in tags],
        "backendNodeId": list(range(10)),
        "parentIndex": [-1, 0, 1, 1, 1, 0, 1, 0, 7, 7],
        "attributes": [[intern(v) for pair in row.items() for v in pair] for row in fields],
    }}]}
    submits = _cdp_ax.submit_forms(dom, "child")
    assert submits == {("child", 2): "child:1", ("child", 4): "child:1", ("child", 5): "child:1", ("child", 9): "child:7"}
    node = {"nodeId": "a", "backendDOMNodeId": 2, "role": {"value": "button"}, "_sessionId": "child"}
    accessor = _cdp_ax.CDPAccessor([node], {}, frozenset(), submit_forms=submits)
    assert accessor.read(node).default
    assert accessor.read(node).form_id == "child:1"
    assert not accessor.read({**node, "_sessionId": ""}).default
    assert not accessor.read({**node, "role": {"value": "textbox"}}).default


@pytest.mark.parametrize("native", [False, True])
def test_atspi_default_state(monkeypatch, native):
    token = object()
    monkeypatch.setattr(_atspi, "_atspi", lambda: SimpleNamespace(StateType=SimpleNamespace(IS_DEFAULT=token) if native else None))
    for marked in [True, False]:
        states = SimpleNamespace(names={"is-default"} if marked else set(), contains=lambda value: marked and value is token)
        node = SimpleNamespace(get_state_set=lambda: states)
        assert _atspi._has_default_state(node) is marked


def test_browser_driver_publishes_dom_submit_marker():
    from copy import deepcopy
    from tests.test_browser import _driver_on, _DOM_SNAPSHOT, _fixture_responder

    dom = deepcopy(_DOM_SNAPSHOT)
    dom["strings"].append("FORM")
    nodes = dom["documents"][0]["nodes"]
    nodes["nodeName"][0] = len(dom["strings"]) - 1
    nodes["parentIndex"] = [-1, 0, 0, 0, 0, 0]

    def responder(method, params):
        if method == "DOMSnapshot.captureSnapshot":
            return dom
        return _fixture_responder(method, params)

    driver, transport = _driver_on(responder)
    snap = driver.snapshot(Scope.WINDOW, "TAB1")
    save = next(el for el in snap.elements if el.title == "Save")
    assert save.default and save.form_id == ":100"
    assert observe.find_elements(snap, text="submit") == (save,)
    assert not any(method.startswith("Input.") for method, _ in transport.sent)


@pytest.mark.parametrize("names, expected", [(None, False), (set(), False), ({"is-default"}, True)])
def test_atspi_default_state_without_optional_binding(monkeypatch, names, expected):
    def unavailable():
        raise ImportError("PyGObject is not installed")

    monkeypatch.setattr(_atspi, "_atspi", unavailable)
    node = SimpleNamespace(get_state_set=lambda: SimpleNamespace(names=names))
    assert _atspi._has_default_state(node) is expected
    assert not _atspi._has_default_state(SimpleNamespace())


def test_atspi_is_default_without_names_ranks_register_before_minimize(monkeypatch):
    """Real StateSet exposes contains(IS_DEFAULT), with no names attribute."""
    token = object()
    monkeypatch.setattr(
        _atspi, "_atspi",
        lambda: SimpleNamespace(StateType=SimpleNamespace(IS_DEFAULT=token)),
    )

    class StateSet:
        def __init__(self, marked):
            self.marked = marked

        def contains(self, member):
            assert member is token
            return self.marked

    nodes = [
        observe.RawNode(role="AXWindow", title="Chrome", position=(0, 0), size=(600, 400)),
    ]
    for title in ("Minimize", "Cancel", "Register", "Clear", "Preview"):
        states = StateSet(title == "Register")
        native = SimpleNamespace(get_state_set=lambda states=states: states)
        nodes.append(observe.RawNode(
            role="AXButton", title=title, actions=("AXPress",),
            position=(10, 10), size=(80, 30),
            default=_atspi._has_default_state(native),
        ))

    class Accessor:
        def read(self, index):
            return nodes[index]

        def children(self, index):
            return list(range(1, len(nodes))) if index == 0 else []

    snap = observe.build_snapshot(
        0, Accessor(), scope=Scope.WINDOW, app="chrome", pid=1, geometry=GEOMETRY,
    )
    assert [el.title for el in snap.elements if el.default] == ["Register"]
    assert [el.title for el in observe.find_elements(snap, role="button")] == [
        "Register", "Minimize", "Cancel", "Clear", "Preview",
    ]
    matches = observe.find_elements(snap, text="submit")
    assert [el.title for el in matches] == ["Register"]
    assert "default" in observe.render_matches(snap, matches)
    assert [el.title for el in observe.find_elements(snap, text="Minimize")] == ["Minimize"]
