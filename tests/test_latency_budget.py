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
    assert command[:4] == ["/opt/baseline/bin/python", "-m", "a11y_computer_use", "run-once"]
    assert sample.json.loads(command[4]) == dict(tool="click", x=1237, y=453)
    assert kwargs["env"]["PYTHONFAULTHANDLER"] == "1"
