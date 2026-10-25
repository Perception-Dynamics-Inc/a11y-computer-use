"""Measure Linux ref resolution CPU cost without native IPC or input.

Run from the repository root with PYTHONPATH=. Use --baseline REV to compare
that revision's resolve_ref implementation against the current working tree.
This is a resolver microbenchmark, not a live click latency measurement.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import replace
import statistics
import subprocess
import timeit
from unittest.mock import patch

from a11y_computer_use import observe
from a11y_computer_use.drivers import linux
from a11y_computer_use.schema import Element, Scope, Snapshot


def main() -> None:
    """Compare matching within an unchanged document/window context."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline")
    args = parser.parse_args()
    window = Element("e1", "AXWindow", "Chrome", None, None, "old",
                     context_identity=("frame", ":1.42", "/window"))
    document = Element("e2", "AXGroup", "Page", None, None, "old", parent="e1",
                       context_identity=("document web", ":1.42", "/doc"))
    button = Element("e3", "AXButton", "Alpha action", None, None, "old", parent="e2")
    old = Snapshot("old", Scope.WINDOW, "chrome", 1, 0., (), (window, document, button))
    live = replace(old, snapshot_id="live", elements=tuple(
        replace(el, snapshot_id="live") for el in old.elements))
    driver = linux.LinuxDriver()
    methods = [("current", linux.LinuxDriver.resolve_ref)]
    if args.baseline:
        source = subprocess.check_output([
            "git", "show", f"{args.baseline}:a11y_computer_use/drivers/linux.py",
        ], text=True)
        cls = next(node for node in ast.parse(source).body
                   if isinstance(node, ast.ClassDef) and node.name == "LinuxDriver")
        method = next(node for node in cls.body
                      if isinstance(node, ast.FunctionDef) and node.name == "resolve_ref")
        namespace = dict(vars(linux))
        exec(compile(ast.Module(body=[method], type_ignores=[]), "<baseline>", "exec"), namespace)
        methods.insert(0, (args.baseline, namespace["resolve_ref"]))
    with patch.object(observe, "ax_handle_for", return_value=None):
        for name, resolve in methods:
            samples = timeit.repeat(lambda: resolve(driver, old, "e3", live=live),
                                    number=10000, repeat=7)
            print(f"{name}: {statistics.median(samples) / 10000 * 1e6:.2f} us/resolve "
                  "(median, 7 x 10000; prebuilt snapshots, no native IPC)")


if __name__ == "__main__":
    main()
