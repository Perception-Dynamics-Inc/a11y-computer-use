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
