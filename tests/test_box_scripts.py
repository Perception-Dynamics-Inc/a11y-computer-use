"""The remote release gate must reject skipped or absent live coverage."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/box/check_results.py'
if not _SCRIPT.exists():
    pytest.skip('Box scripts are excluded from the source distribution', allow_module_level=True)
_spec = importlib.util.spec_from_file_location('box_check_results', _SCRIPT)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)


@pytest.mark.parametrize('xml', [
    '<testsuites><testsuite/></testsuites>',
    '<testsuites><testsuite><testcase name="bus"><skipped/></testcase></testsuite></testsuites>',
    '<testsuite><testcase name="click"><failure/></testcase></testsuite>',
    '<testsuite><testcase name="setup"><error/></testcase></testsuite>',
    '<testsuite errors="1"><testcase name="unrelated_pass"/></testsuite>',
    '<testsuites skipped="1"><testsuite><testcase name="unrelated_pass"/></testsuite></testsuites>',
])
def test_live_gate_rejects_missing_or_failed_coverage(tmp_path: Path, xml: str) -> None:
    report = tmp_path / 'run.xml'
    report.write_text(xml)
    with pytest.raises(ValueError):
        _module.verify_report(report)


def test_live_gate_accepts_completed_tests(tmp_path: Path) -> None:
    report = tmp_path / 'run.xml'
    report.write_text('<testsuites><testsuite><testcase name="click"/>'
                      '<testcase name="type"/></testsuite></testsuites>')
    assert _module.verify_report(report) == 2


_load_spec = importlib.util.spec_from_file_location('box_load_browser', _SCRIPT.with_name('load_browser.py'))
assert _load_spec is not None and _load_spec.loader is not None
_load = importlib.util.module_from_spec(_load_spec)
sys.modules[_load_spec.name] = _load
_load_spec.loader.exec_module(_load)


def _fake_worker(monkeypatch, *, apply: bool, close_error: bool = False) -> list[str]:
    """Fake only the browser/runtime boundary; exercise real load-worker logic."""
    from computeruse import safety, server
    from computeruse.drivers import browser

    requests: list[str] = []

    def open_request(request, timeout):
        url = request if isinstance(request, str) else request.full_url
        requests.append(url)
        return io.BytesIO(b'{"id":"TAB"}')

    class Driver:
        def __init__(self, endpoint, target_id):
            pass

        def navigate(self, url):
            assert "applied%3A" in url

        def close(self):
            if close_error:
                raise OSError("close failed")

    class Runtime:
        def __init__(self, **kwargs):
            self.marker = None
            self.applied = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def desktop_snapshot(self, target_id):
            elements = [
                SimpleNamespace(editable=True, title="Work item", clickable=False, ref="entry"),
                SimpleNamespace(editable=False, title="Apply", value=None, clickable=True, ref="button"),
            ]
            if self.marker:
                # A noneditable child of the input mirrors its current value.
                text = f"applied:{self.marker}" if self.applied else self.marker
                elements.append(SimpleNamespace(editable=False, title=text, value=None, clickable=False))
            self._current = SimpleNamespace(elements=elements)

        def set_value(self, ref, marker):
            self.marker = marker

        def click(self, *, ref):
            self.applied = apply

    monkeypatch.setattr(_load.urllib.request, "urlopen", open_request)
    monkeypatch.setattr(browser, "BrowserDriver", Driver)
    monkeypatch.setattr(server, "Runtime", Runtime)
    monkeypatch.setattr(safety, "PermissionStore", lambda path: SimpleNamespace(set_tier=lambda *args: None))
    monkeypatch.setattr(safety, "AuditLog", lambda path: None)
    return requests


def test_load_rejects_input_value_without_button_effect(monkeypatch, tmp_path):
    requests = _fake_worker(monkeypatch, apply=False)
    result = _load.run_worker("http://fake", 0, 1, str(tmp_path))
    assert result.completed == 0
    assert any("page did not confirm" in error for error in result.errors)
    assert requests[-1] == "http://fake/json/close/TAB"


def test_load_counts_only_confirmed_iterations(monkeypatch, tmp_path):
    requests = _fake_worker(monkeypatch, apply=True)
    result = _load.run_worker("http://fake", 0, 3, str(tmp_path))
    assert result.completed == 3 and not result.errors
    assert len(result.durations_ms) == 3
    assert requests[-1] == "http://fake/json/close/TAB"


def test_load_still_closes_tab_when_driver_cleanup_fails(monkeypatch, tmp_path):
    requests = _fake_worker(monkeypatch, apply=True, close_error=True)
    result = _load.run_worker("http://fake", 0, 1, str(tmp_path))
    assert result.completed == 1
    assert any("driver cleanup failed" in error for error in result.errors)
    assert requests[-1] == "http://fake/json/close/TAB"


@pytest.mark.parametrize("crash_at", ["result", "submit"])
def test_load_process_crash_writes_a_failed_report(monkeypatch, tmp_path, crash_at):
    class Future:
        def result(self):
            raise RuntimeError("worker exited abruptly")

    class Pool:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def submit(self, *args):
            if crash_at == "submit":
                raise RuntimeError("process pool failed to start")
            return Future()

    output = tmp_path / "result.json"
    monkeypatch.setattr(_load, "ProcessPoolExecutor", Pool)
    monkeypatch.setattr(sys, "argv", ["load_browser.py", "--workers", "2", "--iterations", "1",
                                     "--output", str(output)])
    assert _load.main() == 1
    report = json.loads(output.read_text())
    assert report["completed"] == 0 and report["expected"] == 2 and report["errors"] == 2
    assert "failed" in report["worker_results"][0]["errors"][0]
