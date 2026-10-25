"""Same-run relative latency gate. The release baseline remains 0.4.76."""
from __future__ import annotations

import statistics


def summary(samples: list[float]) -> dict:
    """Report the full distribution without dropping slow samples."""
    quartiles = statistics.quantiles(samples, n=4, method="inclusive")
    return dict(median=statistics.median(samples), minimum=min(samples),
                maximum=max(samples), iqr=quartiles[2] - quartiles[0], samples=samples)


def verdict(baseline: list[float], head: list[float], retry: tuple | None = None) -> dict:
    """Fail above 1.25x, with one fresh retry only in the 1.25–1.5 noise band."""
    ratio = statistics.median(head) / statistics.median(baseline)
    result = dict(baseline=summary(baseline), head=summary(head), ratio=ratio)
    if 1.25 < ratio <= 1.5 and retry is None:
        result["status"] = "retry"
    elif 1.25 < ratio <= 1.5:
        second = verdict(*retry)
        # A second noise-band result is a failure, not another retry.
        result["retry"] = second
        result["status"] = "fail" if second["ratio"] > 1.25 else "pass"
    else:
        result["status"] = "fail" if ratio > 1.25 else "pass"
    return result
