"""Exercise isolated browser workers through the gated Runtime, without an LLM.

Each process owns one tab, grants file and audit directory. Every iteration sets
an entry, presses a button by ref, then verifies the page's resulting value.
This measures driver/runtime capacity on a small local page, not LLM throughput.
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import os
import platform
import tempfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class WorkerResult:
    worker: int
    completed: int = 0
    durations_ms: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    peak_rss_mb: float | None = None


def peak_rss_mb() -> float | None:
    """Return this worker's process high-water RSS, when resource is available."""
    try:
        import resource
    except ImportError:
        return None
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024 if platform.system() == "Darwin" else 1024)


def run_worker(endpoint: str, worker: int, iterations: int, directory: str) -> WorkerResult:
    """Return verified iteration counts and timing samples for one isolated tab."""
    from computeruse.drivers.browser import BrowserDriver
    from computeruse.safety import AuditLog, PermissionStore, Tier
    from computeruse.server import Runtime

    result = WorkerResult(worker=worker)
    target_id: str | None = None
    driver: BrowserDriver | None = None
    try:
        request = urllib.request.Request(f"{endpoint}/json/new?about:blank", method="PUT")
        with urllib.request.urlopen(request, timeout=10) as response:
            target_id = json.load(response)["id"]
        driver = BrowserDriver(endpoint, target_id=target_id)
        html = (
            f'<!doctype html><title>Worker {worker}</title>'
            '<label for="entry">Work item</label><input id="entry">'
            '<button id="apply" onclick="document.getElementById(\'result\').textContent='
            "'applied:'+document.getElementById('entry').value;console.log('applied')\">Apply</button>"
            '<p id="result">waiting</p>'
        )
        driver.navigate("data:text/html," + urllib.parse.quote(html))
        folder = Path(directory) / str(worker)
        store = PermissionStore(folder / "permissions.json")
        store.set_tier(target_id, Tier.FULL)
        with Runtime(store=store, audit=AuditLog(folder / "audit"), driver=driver) as runtime:
            for iteration in range(iterations):
                marker = f"worker-{worker}-iteration-{iteration}"
                # Input controls can expose noneditable AX descendants holding
                # their value. Only the click produces this distinct output.
                confirmation = f"applied:{marker}"
                started = time.perf_counter()
                runtime.desktop_snapshot(target_id)
                snapshot = runtime._current  # benchmark inspects the snapshot it just requested
                if snapshot is None:
                    raise RuntimeError("observation produced no snapshot")
                entry = next(el for el in snapshot.elements if el.editable)
                button = next(el for el in snapshot.elements if el.title == "Apply" and el.clickable)
                runtime.set_value(entry.ref, marker)
                runtime.click(ref=button.ref)
                runtime.desktop_snapshot(target_id)
                snapshot = runtime._current
                if snapshot is None or not any(
                    not el.editable and (el.title == confirmation or el.value == confirmation)
                    for el in snapshot.elements
                ):
                    raise RuntimeError(f"page did not confirm {marker}")
                result.durations_ms.append((time.perf_counter() - started) * 1000)
                result.completed += 1
    except Exception as exc:
        result.errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception as exc:
                result.errors.append(f"driver cleanup failed: {type(exc).__name__}: {exc}")
        if target_id is not None:
            try:
                with urllib.request.urlopen(f"{endpoint}/json/close/{target_id}", timeout=5):
                    pass
            except Exception as exc:
                result.errors.append(f"tab cleanup failed: {exc}")
        result.peak_rss_mb = peak_rss_mb()
    return result


def percentile(samples: list[float], quantile: float) -> float:
    """Return the nearest-rank percentile, or zero when there are no samples."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    return round(ordered[max(0, math.ceil(len(ordered) * quantile) - 1)], 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=os.getenv("COMPUTERUSE_CDP_ENDPOINT", "http://127.0.0.1:9222"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.workers <= 32 or not 1 <= args.iterations <= 10000:
        parser.error("workers must be 1..32; iterations must be 1..10000")
    started = time.perf_counter()
    results: list[WorkerResult] = []
    with tempfile.TemporaryDirectory(prefix="computeruse-load-") as directory:
        try:
            with ProcessPoolExecutor(max_workers=args.workers,
                                     mp_context=multiprocessing.get_context("spawn")) as pool:
                futures = [pool.submit(run_worker, args.endpoint.rstrip("/"), worker,
                                       args.iterations, directory) for worker in range(args.workers)]
                for worker, future in enumerate(futures):
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        # A crashed worker must still leave a failed report.
                        results.append(WorkerResult(worker=worker, errors=[
                            f"worker process failed: {type(exc).__name__}: {exc}"]))
        except Exception as exc:
            # Include pool startup/submission failures in the same evidence.
            recorded = {result.worker for result in results}
            error = f"process pool failed: {type(exc).__name__}: {exc}"
            results.extend(WorkerResult(worker=worker, errors=[error])
                           for worker in range(args.workers) if worker not in recorded)
            if len(recorded) == args.workers:
                results[0].errors.append(error)
    elapsed = time.perf_counter() - started
    samples = [value for result in results for value in result.durations_ms]
    completed = sum(result.completed for result in results)
    errors = sum(len(result.errors) for result in results)
    report = {
        "workload": "local HTML form: observe, set_value, ref click, observe and verify",
        "isolation": "one spawned process, Runtime, tab and permission store per worker",
        "platform": platform.platform(), "python": platform.python_version(),
        "cpu_count": os.cpu_count(), "workers": args.workers,
        "iterations_per_worker": args.iterations, "completed": completed,
        "expected": args.workers * args.iterations, "errors": errors,
        "wall_time_s": round(elapsed, 2),
        "verified_iterations_per_second": round(completed / elapsed, 2),
        "iteration_ms": {"p50": percentile(samples, .5), "p95": percentile(samples, .95),
                         "p99": percentile(samples, .99)},
        "worker_results": [{k: v for k, v in asdict(result).items() if k != "durations_ms"}
                           for result in results],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return int(errors != 0 or completed != args.workers * args.iterations)


if __name__ == "__main__":
    raise SystemExit(main())
