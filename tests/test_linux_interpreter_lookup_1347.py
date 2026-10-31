"""#1347: one AT-SPI interpreter_host lookup per name per action, never reused across actions."""

from __future__ import annotations

from a11y_computer_use.drivers import _linux_exe
from tests.test_linux_proc_scan_1347 import ME, REAL, _runtime, _world


def test_screenshot_asks_atspi_once_per_action(tmp_path, monkeypatch) -> None:
    _world(monkeypatch, {52: ("mousepad", REAL, ME)}, 52)
    rt = _runtime(tmp_path, {"mousepad": "full"})
    asked: list[str] = []
    rt.driver.interpreter_host = lambda app: asked.append(app)
    rt.screenshot()
    assert asked == ["mousepad"]  # was one D-Bus lookup per gate pass
    rt.screenshot()
    assert asked == ["mousepad", "mousepad"]  # every action asks again


def test_a_host_that_appears_between_actions_is_seen(tmp_path, monkeypatch) -> None:
    _world(monkeypatch, {52: ("mousepad", REAL, ME)}, 52)
    rt = _runtime(tmp_path, {"mousepad": "full"})
    host: list = [None]
    rt.driver.interpreter_host = lambda app: host[0]
    with _linux_exe.proc_scope():
        assert rt._linux_interpreter_key("mousepad", "mousepad") == "mousepad"
    host[0] = "python3"
    with _linux_exe.proc_scope():
        assert rt._linux_interpreter_key("mousepad", "mousepad") == "python3"
    assert rt._linux_interpreter_key("mousepad", "mousepad") == "python3"


def test_scope_memo_is_dropped_at_every_entry(monkeypatch) -> None:
    calls: list[int] = []

    def compute() -> int:
        calls.append(1)
        return len(calls)

    with _linux_exe.proc_scope():
        assert _linux_exe.scope_memo("k", compute) == 1
        assert _linux_exe.scope_memo("k", compute) == 1
        with _linux_exe.proc_scope():  # a nested action looks again
            assert _linux_exe.scope_memo("k", compute) == 2
    assert _linux_exe.scope_memo("k", compute) == 3  # outside an action: fresh
    assert _linux_exe.scope_memo("k", compute) == 4


def test_exe_check_and_registry_reads_once_per_action_never_across(monkeypatch) -> None:
    """#1347: a screenshot gates the frontmost app twice; the exe verdict and the
    AT-SPI registry reads behind it are taken once per action, fresh in the next."""
    from a11y_computer_use.drivers import _atspi, _atspi_events, _linux_exe

    calls = {"verdict": 0, "named": 0, "owners": 0, "run": 0}

    def verdict(name, recorded):
        calls["verdict"] += 1
        return name, None

    def run(fn):
        calls["run"] += 1
        return fn()

    def named(name):
        calls["named"] += 1
        return {7}

    def owners():
        calls["owners"] += 1
        return {7}

    monkeypatch.setattr(_linux_exe, "_check_verdict", verdict)
    monkeypatch.setattr(_atspi_events, "run", run)
    monkeypatch.setattr(_atspi, "_registered_pids_named", named)
    monkeypatch.setattr(_atspi, "_registered_pids", owners)
    for action in (1, 2):
        with _linux_exe.proc_scope():
            for _ in range(2):
                assert _linux_exe.check("mousepad", None) == ("mousepad", None)
                assert _linux_exe._atspi_named_pids("mousepad") == {7}
                assert _linux_exe._atspi_owner_pids() == {7}
        assert calls == {"verdict": action, "named": action, "owners": action, "run": 2 * action}
    _linux_exe.check("mousepad", None)  # outside an action: never memoized
    _linux_exe.check("mousepad", None)
    assert calls["verdict"] == 4
