#!/usr/bin/env python3
"""Measure Scribus New Document set_value in a disposable X11/D-Bus session.

Run with the repository on PYTHONPATH, a window manager, and
QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1. Launches Scribus with a fresh HOME;
relaunch the script if Scribus fails to register on AT-SPI. No live pytest
module is imported. Prints inclusive helper times and native adapter call
counts (nested helper counts overlap), plus the verified snapshot value.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi, _linux_system
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Scope


def main():
    with tempfile.TemporaryDirectory(prefix="scribus-spin-profile-") as config:
        with open(Path(config) / "scribus.log", "w") as log:
            process = subprocess.Popen(
                ["scribus"], env={**os.environ, "HOME": config},
                stdout=log, stderr=subprocess.STDOUT,
            )
            try:
                measure(LinuxDriver())
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def measure(driver):
    for attempt in range(35):
        time.sleep(1)
        try:
            snapshot = driver.snapshot(Scope.APP, "scribus")
            element = next(item for item in snapshot.elements
                           if item.role == "AXSpinButton" and "Number of Pages" in item.title)
            break
        except Exception as exc:
            print("Waiting for New Document:", repr(exc), flush=True)
            if attempt == 34:
                raise
    handle = observe.ax_handle_for(element.snapshot_id, element.ref)
    print("Target:", element, flush=True)
    print("Native raise target:", driver._run(lambda: _atspi.toplevel_raise_target(handle)), flush=True)
    print("Windows:", _linux_system.windows(), flush=True)
    print("Active:", _linux_system.active_window(), flush=True)
    stats = defaultdict(lambda: [0, 0.0, Counter()])
    stack = []
    originals = []

    def instrument(owner, name):
        original = getattr(owner, name)
        originals.append((owner, name, original))

        def measured(*args, **kwargs):
            started = time.perf_counter()
            stack.append(name)
            try:
                return original(*args, **kwargs)
            finally:
                stack.pop()
                stats[name][0] += 1
                stats[name][1] += time.perf_counter() - started
        setattr(owner, name, measured)

    for name in (
        "set_numeric_value", "set_text", "_set_text_body", "_capture_text_points",
        "_verification_matches", "_qt_text_count_matches", "_confirm_text",
        "_wait_until_gone", "_text_is_gone", "peer_field_texts", "_qt_viewport_text",
    ):
        instrument(_atspi, name)
    for name in ("_raise_handle_window", "raise_window", "_visible_action", "_apply_set_value"):
        instrument(driver, name)
    original_safe = _atspi._safe

    def measured_safe(fn, *args, **kwargs):
        defaults = getattr(fn, "__defaults__", None)
        method = defaults[0] if defaults and callable(defaults[0]) else fn
        name = getattr(method, "__name__", "native")
        if name == "<lambda>":
            name = ",".join(fn.__code__.co_names)
        for helper in stack:
            stats[helper][2][name] += 1
        return original_safe(fn, *args, **kwargs)

    _atspi._safe = measured_safe
    try:
        started = time.perf_counter()
        result = driver.set_value(element, "3")
        print("Result:", result, "seconds:", time.perf_counter() - started, flush=True)
        print(json.dumps(stats, indent=2), flush=True)
        assert result is True
    finally:
        _atspi._safe = original_safe
        for owner, name, original in reversed(originals):
            setattr(owner, name, original)
    snapshot = driver.snapshot(Scope.APP, "scribus")
    values = [item.value for item in snapshot.elements
              if item.role == "AXSpinButton" and "Number of Pages" in item.title]
    print("Read-back:", values, flush=True)
    assert values == ["3"]


if __name__ == "__main__":
    main()
