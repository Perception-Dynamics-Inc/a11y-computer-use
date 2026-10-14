#!/usr/bin/env python3
"""Profile the unchanged Calc five-second regression outside the live pytest suite.

Run in a disposable Xvfb/D-Bus desktop with a working window manager::

    PYTHONPATH=$PWD /workspace/a11y-venv-codex/bin/python \
        scripts/box/profile_calc_actions.py --stress-loops 4 --raw

Omit --raw for native call counts/timings, and add --profile-dir /tmp/calc-prof
for cProfile data from the AT-SPI event thread. Instrumentation adds overhead.
The test's five-second assertions and cell-value verification remain intact.
The extracted setup terminates LibreOffice processes, just as the test does.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import cProfile
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

import pytest

from a11y_computer_use.drivers import _atspi
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import ComputerUseError, ErrorCode, Scope


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stress-loops", type=int, choices=range(5), default=2)
    parser.add_argument("--raw", action="store_true", help="measure without native counters")
    parser.add_argument("--profile-dir", type=Path)
    args = parser.parse_args()
    if args.profile_dir:
        args.profile_dir.mkdir(parents=True, exist_ok=True)

    source = Path(__file__).resolve().parents[2] / "tests/test_linux_live.py"
    test_name = "test_linux_calc_click_type_and_key_finish_within_five_seconds"
    names = {test_name, "_require_bus", "_cell", "_wait_cell_value", "_stop_group", "_kill_libreoffice"}
    # Read only the exact regression and its helpers, without importing or
    # collecting the live pytest module. Retain every assertion unchanged.
    selected = []
    for node in ast.parse(source.read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_LO_REGISTRY" for target in node.targets
        ):
            selected.append(node)
    namespace = dict(globals())
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), namespace)

    calls = Counter()
    elapsed = Counter()
    original_safe = _atspi._safe

    def measured(fn, default=None, **kwargs):
        defaults = getattr(fn, "__defaults__", None)
        method = defaults[0] if defaults else fn
        name = getattr(method, "__name__", "native")
        started = time.perf_counter()
        try:
            return original_safe(fn, default, **kwargs)
        finally:
            calls[name] += 1
            elapsed[name] += time.perf_counter() - started

    if not args.raw:
        _atspi._safe = measured
        import gi

        require_version = gi.require_version

        def measured_require(*params):
            calls["require_version"] += 1
            return require_version(*params)

        gi.require_version = measured_require

    if args.profile_dir:
        original_run = LinuxDriver._run
        operation = 0

        def profiled_run(self, fn):
            nonlocal operation
            operation += 1
            destination = args.profile_dir / f"operation-{operation}.prof"

            def profiled():
                profile = cProfile.Profile()
                try:
                    return profile.runcall(fn)
                finally:
                    profile.dump_stats(str(destination))

            return original_run(self, profiled)

        LinuxDriver._run = profiled_run

    def instrument(name, original):
        def timed(self, *params, **kwargs):
            calls.clear()
            elapsed.clear()
            started = time.perf_counter()
            try:
                return original(self, *params, **kwargs)
            finally:
                print(json.dumps({
                    "step": name, "seconds": time.perf_counter() - started,
                    "calls": calls, "native_seconds": elapsed,
                }), flush=True)
        return timed

    for name in ("document_url", "snapshot", "click", "type_text", "key_chord"):
        setattr(LinuxDriver, name, instrument(name, getattr(LinuxDriver, name)))

    stress = []
    try:
        for _ in range(args.stress_loops):
            stress.append(subprocess.Popen([sys.executable, "-c", "while True: pass"]))
        with tempfile.TemporaryDirectory(prefix="calc-profile-") as temporary:
            namespace[test_name](Path(temporary))
        print("PASS: unchanged five-second bounds and committed A1 value", flush=True)
    finally:
        for proc in stress:
            proc.terminate()
        for proc in stress:
            proc.wait()


if __name__ == "__main__":
    main()
