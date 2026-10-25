"""Interleave baseline/head native microbench samples in one private desktop."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import platform

from compare import verdict

ROOT = Path(__file__).resolve().parents[2]
ROWS = ("R1", "R1-iframe", "R2", "R5", "R7", "R8")


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--baseline", required=True, help="0.4.76 venv Python")
    parser.add_argument("--head", required=True, help="head venv Python")
    parser.add_argument("--output", type=Path, default=Path("latency-results.json"))
    parser.add_argument("--rows", nargs="+", default=ROWS)
    args = parser.parse_args()
    if hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    report = dict(baseline="0.4.76", commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                  samples=7, warmups=2, rows={}, correctness={},
                  versions={"python": sys.version, "platform": platform.platform(),
                            "native": subprocess.check_output(["dpkg-query", "-W", "-f=${Package}=${Version}\n",
                                "at-spi2-core", "mousepad", "xfwm4", "gir1.2-gtk-3.0"], text=True)})
    for app, command in {"chrome": [os.environ.get("CHROME", "google-chrome"), "--version"],
                         "firefox": [os.environ.get("FIREFOX", "firefox"), "--version"],
                         "mousepad": ["mousepad", "--version"]}.items():
        report["versions"][app] = subprocess.check_output(command, text=True).splitlines()[0]

    def save():
        args.output.write_text(json.dumps(report, indent=2) + "\n")

    def sample(build, row):
        env = dict(os.environ, PYTHONPATH=str(ROOT) if build == "head" else "")
        command = [getattr(args, build), str(ROOT / "scripts/latency/sample.py"), row]
        if build == "head":
            command.append("--head")
        result = subprocess.run(command, env=env, cwd="/tmp", capture_output=True, text=True)
        if result.returncode:
            report["error"] = dict(build=build, row=row, stderr=result.stderr, stdout=result.stdout)
            save()
            raise RuntimeError(report["error"])
        return json.loads(result.stdout.strip().splitlines()[-1])

    try:
        # Correctness never gets a timing waiver, and the unsafe old baseline
        # must not be required to refuse inputs it historically allowed.
        for row in ("R9", "R10"):
            report["correctness"][row] = sample("head", row)
            save()
        for row in args.rows:
            for _ in range(2):
                sample("baseline", row)
                sample("head", row)
            attempts = []
            for attempt in range(2):
                samples = {"baseline": [], "head": []}
                for _ in range(7):
                    for build in ("baseline", "head"):
                        samples[build].append(sample(build, row))
                attempts.append(samples)
                result = verdict([s["seconds"] for s in samples["baseline"]],
                                 [s["seconds"] for s in samples["head"]])
                if attempt:
                    first = attempts[0]
                    result = verdict([s["seconds"] for s in first["baseline"]],
                                     [s["seconds"] for s in first["head"]],
                                     ([s["seconds"] for s in samples["baseline"]],
                                      [s["seconds"] for s in samples["head"]]))
                if row == "R5":
                    memory = verdict([s["rss_kib"] for s in samples["baseline"]],
                                     [s["rss_kib"] for s in samples["head"]])
                    # Memory has no noise waiver.
                    result["memory"] = memory
                    if memory["ratio"] > 1.25:
                        result["status"] = "fail"
                report["rows"][row] = dict(**result, attempts=attempts)
                save()
                if result["status"] != "retry":
                    break
            print(f'{row}: {result["ratio"]:.3f}x {result["status"]}', flush=True)
        return int(any(row["status"] != "pass" for row in report["rows"].values()))
    finally:
        save()


if __name__ == "__main__":
    raise SystemExit(main())
