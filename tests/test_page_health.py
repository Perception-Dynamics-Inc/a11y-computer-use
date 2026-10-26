"""Renderer liveness must not be inferred from Chrome's cached AX tree."""
import json
from types import SimpleNamespace

import pytest

from a11y_computer_use.drivers import _cdp, _cdp_forms, _page_health
from a11y_computer_use.schema import ComputerUseError, ErrorCode


@pytest.fixture
def endpoint(monkeypatch):
    page = {"type": "page", "title": "Busy Lab", "url": "file:///busy.html",
            "webSocketDebuggerUrl": "ws://127.0.0.1:1234/devtools/page/one"}
    pages = [page]
    monkeypatch.setattr(_cdp_forms, "port_for_pid", lambda pid: 1234)
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return json.dumps(pages).encode()
    monkeypatch.setattr(_page_health.urllib.request, "urlopen", lambda *args, **kw: Response())
    return pages


@pytest.mark.parametrize("result", [True, False, None])
def test_renderer_response_timeout_and_protocol_failure_are_distinct(endpoint, monkeypatch, result):
    closed = []
    class Transport:
        def send(self, raw):
            data = json.loads(raw)
            assert data["method"] == "Runtime.evaluate"
            assert data["params"] == {"expression": "1", "returnByValue": True}
        def recv(self, timeout=None):
            assert 0 < timeout <= _page_health.BUDGET
            if result is False: raise TimeoutError
            if result is None: raise OSError("disconnected")
            return json.dumps({"id": 1, "result": {"result": {"value": 1}}})
        def close(self): closed.append(True)
    monkeypatch.setattr(_cdp, "connect", lambda *args, **kw: Transport())
    assert _page_health.probe(42, "Busy Lab - Google Chrome", "file:///busy.html") is result
    assert closed


@pytest.mark.parametrize("case", ["duplicate", "wrong_url", "wrong_title", "no_port", "discovery_failure"])
def test_unknown_binding_never_probes_a_different_page(endpoint, monkeypatch, case):
    monkeypatch.setattr(_cdp, "connect", lambda *args, **kwargs: pytest.fail("wrong target"))
    if case == "duplicate": endpoint.append(dict(endpoint[0]))
    if case == "wrong_url": endpoint[0]["url"] = "file:///other.html"
    if case == "wrong_title": endpoint[0]["title"] = "other"
    if case == "no_port": monkeypatch.setattr(_cdp_forms, "port_for_pid", lambda pid: None)
    if case == "discovery_failure":
        def fail(*args, **kwargs): raise TimeoutError
        monkeypatch.setattr(_page_health.urllib.request, "urlopen", fail)
    assert _page_health.probe(42, "Busy Lab", None if case == "wrong_title" else "file:///busy.html") is None


def test_snapshot_refuses_stale_tree_before_dom_overlay(monkeypatch):
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Scope
    driver = LinuxDriver()
    snapshot = SimpleNamespace(elements=[SimpleNamespace(modal_scope_root=True)], pid=42, url=None)
    monkeypatch.setattr(driver, "_snapshot_resolved", lambda *args: snapshot)
    monkeypatch.setattr(_cdp_forms, "_window_title", lambda snap: "Busy Lab")
    monkeypatch.setattr(_page_health, "probe", lambda *args: False)
    monkeypatch.setattr(_cdp_forms, "overlay_snapshot", lambda *args: pytest.fail("stale tree"))
    with pytest.raises(ComputerUseError) as raised:
        driver._snapshot(Scope.WINDOW, "Chrome")
    assert raised.value.detail["reason"] == "page_not_responding"
    assert raised.value.detail["next"] == []


def test_find_displays_driver_freshness_warning_even_with_matches(tmp_path):
    from tests.test_type_ref import _Driver, _runtime
    driver = _Driver()
    driver.observation_warning = lambda snap: _page_health.STALE_TREE
    runtime = _runtime(tmp_path, driver)
    result = runtime.find("demo", text="FieldB")
    assert "1 match(es)" in result
    assert _page_health.STALE_TREE in result


def test_unknown_chromium_snapshot_marks_tree_stale(monkeypatch):
    from dataclasses import replace
    from tests.conftest import build_synthetic_snapshot
    from a11y_computer_use import observe
    from a11y_computer_use.drivers import _atspi
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Scope
    driver = LinuxDriver()
    driver._run = lambda fn: fn()
    original = build_synthetic_snapshot()
    doc = replace(original.elements[0], modal_scope_root=True)
    snapshot = replace(original, elements=(doc, *original.elements[1:]))
    monkeypatch.setattr(driver, "_snapshot_resolved", lambda *args: snapshot)
    monkeypatch.setattr(driver, "_snapshot_selections", lambda snap: snap)
    monkeypatch.setattr(_page_health, "probe", lambda *args: None)
    monkeypatch.setattr(_cdp_forms, "overlay_snapshot", lambda snap: snap)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *args: object())
    monkeypatch.setattr(_atspi, "_chromium_app", lambda handle: True)
    result = driver._snapshot(Scope.WINDOW, "Chrome")
    assert _page_health.STALE_TREE in result.elements[0].content_note
    assert driver.observation_warning(result) == _page_health.STALE_TREE
    assert result.elements[1:] == snapshot.elements[1:]


@pytest.mark.parametrize("pid", [None, False, 0, -1, "123"])
def test_unknown_process_is_not_a_renderer_diagnosis(monkeypatch, pid):
    monkeypatch.setattr(_cdp_forms, "port_for_pid", lambda pid: pytest.fail("unknown owner"))
    assert _page_health.probe(pid, "Busy Lab") is None
