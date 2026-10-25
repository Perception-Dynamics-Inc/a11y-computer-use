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
