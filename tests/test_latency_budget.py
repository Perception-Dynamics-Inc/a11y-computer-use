"""The latency gate must never waive slow rows or discard outliers."""
from scripts.latency.compare import summary, verdict


def test_gate_boundary_and_noise_retry():
    baseline = [1.] * 7
    assert verdict(baseline, [1.25] * 7)["status"] == "pass"
    assert verdict(baseline, [1.26] * 7)["status"] == "retry"
    assert verdict(baseline, [1.51] * 7)["status"] == "fail"
    assert verdict(baseline, [1.3] * 7, (baseline, [1.26] * 7))["status"] == "fail"
    assert verdict(baseline, [1.3] * 7, (baseline, [1.2] * 7))["status"] == "pass"


def test_gate_uses_median_and_preserves_every_sample():
    samples = [1., 1., 1., 1., 1., 10., 20.]
    assert summary(samples)["maximum"] == 20.
    assert summary(samples)["samples"] == samples
    assert verdict([1.] * 7, samples)["status"] == "pass"


def test_modal_and_large_page_rows_are_release_gated():
    from scripts.latency.run import ROWS

    required = {"R12-find", "R12-click", "R13-key", "R13-click", "R13-type"}
    assert required <= set(ROWS)
    # The new cases go through the same relative gate, including its retry.
    result = verdict([1.] * 7, [1.26] * 7, ([1.] * 7, [1.26] * 7))
    assert result["status"] == "fail"


def test_cli_crash_keeps_native_diagnostics_without_retry(monkeypatch):
    from types import SimpleNamespace
    import pytest
    from scripts.latency import sample

    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=-11, stdout="", stderr="Fatal Python error: Segmentation fault")

    monkeypatch.setattr(sample.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="Segmentation fault"):
        sample.run_once_click("/opt/baseline/bin/python", SimpleNamespace(x=1237, y=453), {})
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[0] == "/opt/baseline/bin/python"
    assert command[1] == str(sample.Path(sample.__file__).with_name("run_once.py"))
    assert command[2] == "run-once"
    assert sample.json.loads(command[3]) == dict(tool="click", x=1237, y=453)
    assert kwargs["env"]["PYTHONFAULTHANDLER"] == "1"


def test_cli_launcher_stops_native_callbacks_before_exit_and_preserves_failure(monkeypatch):
    import queue
    import sys
    import threading
    from types import SimpleNamespace
    import pytest
    from scripts.latency import run_once

    callbacks = queue.Queue()
    stopped = threading.Event()
    calls = []

    def native_loop():
        callbacks.get()()
        stopped.set()

    worker = threading.Thread(target=native_loop, daemon=True)
    worker.start()

    def quit_loop():
        assert threading.current_thread() is worker
        calls.append("quit")

    def native_exit():
        assert stopped.is_set() and not worker.is_alive()
        calls.append("exit")

    def cli(module, **kwargs):
        assert module == "a11y_computer_use"
        assert kwargs == dict(run_name="__main__", alter_sys=True)
        assert sys.argv[1:] == ["run-once", '{"tool":"click","x":1237,"y":453}']
        calls.append("cli")
        raise SystemExit(7)

    monkeypatch.setattr(sys, "argv", ["launcher", "run-once", '{"tool":"click","x":1237,"y":453}'])
    monkeypatch.setattr(run_once.runpy, "run_module", cli)
    monkeypatch.setitem(sys.modules, "a11y_computer_use.drivers._atspi_events",
                        SimpleNamespace(_thread=worker, listening=lambda: True, submit=callbacks.put))
    monkeypatch.setitem(sys.modules, "gi.repository.Atspi",
                        SimpleNamespace(event_quit=quit_loop, exit=native_exit))
    try:
        with pytest.raises(SystemExit) as exc:
            run_once.main()
        assert exc.value.code == 7
        assert calls == ["cli", "quit", "exit"]
    finally:
        if worker.is_alive():
            callbacks.put(lambda: None)
            worker.join(timeout=15)


def test_cli_launcher_refuses_cleanup_with_live_native_thread(monkeypatch):
    import sys
    from types import SimpleNamespace
    import pytest
    from scripts.latency import run_once

    calls = []
    thread = SimpleNamespace(is_alive=lambda: True, join=lambda **kwargs: calls.append("join"))
    monkeypatch.setitem(sys.modules, "a11y_computer_use.drivers._atspi_events",
                        SimpleNamespace(_thread=thread, listening=lambda: True, submit=lambda fn: fn()))
    monkeypatch.setitem(sys.modules, "gi.repository.Atspi",
                        SimpleNamespace(event_quit=lambda: calls.append("quit"), exit=lambda: calls.append("exit")))
    with pytest.raises(RuntimeError, match="did not stop"):
        run_once.close_atspi()
    assert calls == ["quit", "join"]


def test_cli_launcher_does_not_load_native_bindings_for_cli_errors(monkeypatch):
    import sys
    import pytest
    from scripts.latency import run_once

    monkeypatch.delitem(sys.modules, "gi.repository.Atspi", raising=False)

    def invalid_cli(*args, **kwargs):
        raise SystemExit(2)

    monkeypatch.setattr(run_once.runpy, "run_module", invalid_cli)
    with pytest.raises(SystemExit) as exc:
        run_once.main()
    assert exc.value.code == 2
    assert "gi.repository.Atspi" not in sys.modules
