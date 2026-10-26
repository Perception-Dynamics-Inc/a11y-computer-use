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
    monkeypatch.setattr(_page_health, "chromium_process", lambda pid: True)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *args: pytest.fail("no AT-SPI read in snapshot"))
    monkeypatch.setattr(_atspi, "_chromium_app", lambda handle: pytest.fail("no AT-SPI read in snapshot"))
    result = driver._snapshot(Scope.WINDOW, "Chrome")
    assert _page_health.STALE_TREE in result.elements[0].content_note
    assert driver.observation_warning(result) == _page_health.STALE_TREE
    assert result.elements[1:] == snapshot.elements[1:]


@pytest.mark.parametrize("pid", [None, False, 0, -1, "123"])
def test_unknown_process_is_not_a_renderer_diagnosis(monkeypatch, pid):
    monkeypatch.setattr(_cdp_forms, "port_for_pid", lambda pid: pytest.fail("unknown owner"))
    assert _page_health.probe(pid, "Busy Lab") is None


def test_chromium_process_reads_proc_only(tmp_path, monkeypatch):
    import os
    assert _page_health.chromium_process(None) is False
    assert _page_health.chromium_process(os.getpid()) is False  # python


def test_renderer_busy_needs_most_of_the_interval_on_cpu(monkeypatch):
    import os
    monkeypatch.setattr(os, "sysconf", lambda name: 100, raising=False)
    busy = _page_health.renderer_busy((0.0, {5: 10, 6: 3}), (1.0, {5: 105, 6: 4}))
    idle = _page_health.renderer_busy((0.0, {5: 10, 6: 3}), (1.0, {5: 14, 6: 8}))
    assert busy is True and idle is False


@pytest.mark.parametrize("before,after", [
    (None, (1.0, {5: 100})),
    ((0.0, {5: 0}), None),
    ((0.0, {5: 0}), (0.1, {5: 10})),     # too short to separate from idle
    ((0.0, {5: 0}), (1.0, {6: 100})),    # no renderer in both samples
])
def test_renderer_busy_unknown_inputs_are_not_a_hang(monkeypatch, before, after):
    import os
    monkeypatch.setattr(os, "sysconf", lambda name: 100, raising=False)
    assert _page_health.renderer_busy(before, after) is None


def test_renderer_ticks_reads_only_renderer_descendants(tmp_path, monkeypatch):
    import builtins
    tree = {1: [2, 3], 2: [4], 3: [], 4: []}
    cmd = {2: b"chrome\0--type=zygote\0", 3: b"chrome\0--type=gpu-process\0",
           4: b"chrome\0--type=renderer\0"}
    real = builtins.open
    def fake(path, *args, **kw):
        import io
        parts = str(path).split("/")
        if parts[1] != "proc":
            return real(path, *args, **kw)
        pid = int(parts[2])
        if parts[-1] == "children":
            return io.StringIO(" ".join(map(str, tree[pid])))
        if parts[-1] == "cmdline":
            return io.BytesIO(cmd[pid])
        if parts[-1] == "stat":
            return io.StringIO(f"{pid} (chrome (x)) R 2 " + " ".join(["0"] * 9) + " 70 30 0 0")
        raise FileNotFoundError(path)
    monkeypatch.setattr(builtins, "open", fake)
    assert _page_health.renderer_ticks(1) == {4: 100}
    assert _page_health.renderer_ticks(0) is None


def test_renderer_busy_without_sysconf_is_unknown(monkeypatch):
    import os

    monkeypatch.delattr(os, "sysconf", raising=False)
    assert _page_health.renderer_busy((0.0, {5: 0}), (1.0, {5: 100})) is None
