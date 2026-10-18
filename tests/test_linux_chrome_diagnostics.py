"""Failure evidence for the live Chromium table regression, without a desktop."""
import json
from types import SimpleNamespace

from tests.test_linux_live import _chrome_table_failure
from a11y_computer_use.drivers import _atspi


def test_table_failure_keeps_phase_identity_and_both_native_views(monkeypatch):
    root = SimpleNamespace(role="frame", children=[])
    table = SimpleNamespace(role="table", children=[object()] * 5000)
    in_thread = False
    fresh = False

    def run(fn):
        nonlocal in_thread
        in_thread = True
        try:
            return fn()
        finally:
            in_thread = False

    def find_root(*args):
        assert in_thread
        return root

    def bypass(node, fn, **kwargs):
        nonlocal fresh
        assert in_thread and node is root and kwargs == {"preserve_properties": True}
        fresh = True
        return fn()

    def child(node, index):
        assert in_thread
        assert node is root, "diagnostics must not enumerate 5000 table rows"
        return table

    monkeypatch.setattr(_atspi, "find_root", find_root)
    monkeypatch.setattr(_atspi, "_without_child_cache", bypass)
    monkeypatch.setattr(_atspi, "_role_name", lambda node: node.role)
    monkeypatch.setattr(_atspi, "_node_name", lambda node: "rowtable")
    monkeypatch.setattr(_atspi, "_child_count", lambda node: 1 if node is root and fresh else len(node.children))
    monkeypatch.setattr(_atspi, "_child_at", child)
    monkeypatch.setattr(_atspi, "_atspi_wrapper_key", lambda node: (":1.7", "/frame"))
    monkeypatch.setattr(_atspi, "_extents", lambda node: ((0, 0), (1000, 700)))
    monkeypatch.setattr(_atspi, "_state_names", lambda node: {"showing"})
    monkeypatch.setattr("subprocess.check_output", lambda *args, **kwargs: "Chrome test\n")
    driver = SimpleNamespace(_run=run, windows=lambda: [{"pid": 7, "title": "rowtable"}])
    proc = SimpleNamespace(pid=7, args=["chrome"], poll=lambda: None)
    shot = SimpleNamespace(pid=7, elements=[])
    result = json.loads(_chrome_table_failure(
        driver, proc, shot, phase="reload 2", samples=[{"elapsed": 30.1, "rows": []}],
    ))
    assert result["phase"] == "reload 2"
    assert result["samples"][0]["elapsed"] == 30.1
    assert result["snapshot"]["pid"] == result["launched_pid"] == 7
    assert len(result["native"]["cached"]["nodes"]) == 1
    assert result["native"]["fresh_children"]["nodes"][1][-1] == 5000
    assert result["version"] == "Chrome test"


def test_table_failure_preserves_evidence_when_native_diagnostics_fail(monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("bus gone")

    monkeypatch.setattr("subprocess.check_output", unavailable)
    driver = SimpleNamespace(_run=unavailable, windows=unavailable)
    proc = SimpleNamespace(pid=7, args=["chrome"], poll=lambda: 1)
    result = json.loads(_chrome_table_failure(
        driver, proc, None, phase="initial launch", samples=[],
    ))
    assert result["phase"] == "initial launch"
    assert result["exit_status"] == 1
    assert result["native"] == {"error": "RuntimeError: bus gone"}
    assert result["windows"] == {"error": "RuntimeError: bus gone"}
