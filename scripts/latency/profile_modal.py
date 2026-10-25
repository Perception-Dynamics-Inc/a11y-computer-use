"""Profile one Options find/ref click on the native worker (not its wait).

Run with session.sh using the 0.4.76 interpreter, pre-fix PYTHONPATH, or head.
Counts are adapter invocations, including cached getters, not D-Bus packets.
"""
from __future__ import annotations

import argparse
import cProfile
import json
import os
from pathlib import Path
import pstats
import signal
import subprocess
import tempfile
import time
from types import CodeType


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from a11y_computer_use import safety, server
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.drivers import _atspi
    from a11y_computer_use.schema import ComputerUseError, Scope

    with tempfile.TemporaryDirectory(prefix="modal-profile-") as temp:
        home = Path(temp)
        os.environ.update(HOME=temp, XDG_CACHE_HOME=str(home / "cache"), A11Y_VISIBLE_ACTIONS="0")
        driver = LinuxDriver()
        driver.ensure_trusted()
        proc = subprocess.Popen(["libreoffice", "-env:UserInstallation=" + (home / "lo").as_uri(),
                                 "--writer", "--norestore"], start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            store = safety.PermissionStore(home / "permissions")
            store.set_tier("soffice.bin", safety.Tier.FULL)
            with server.Runtime(driver=driver, store=store, audit=safety.AuditLog(home / "audit")) as runtime:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        snap = driver.snapshot(Scope.WINDOW, "soffice.bin")
                    except ComputerUseError:
                        assert time.monotonic() < deadline, "Writer did not register"
                        time.sleep(.2)
                        continue
                    if len(snap.elements) > 20:
                        break
                    assert time.monotonic() < deadline, "Writer did not register"
                    time.sleep(.2)
                runtime.app("focus", "soffice.bin")
                subprocess.run(["xdotool", "key", "--clearmodifiers", "alt+t"], check=True)
                time.sleep(.5)
                subprocess.run(["xdotool", "key", "o"], check=True)
                while 'dialog "Options' not in runtime.desktop_snapshot("soffice.bin", "window", "interactive"):
                    assert time.monotonic() < deadline, "Options did not open"
                    time.sleep(.1)
                original = driver._run
                profile = cProfile.Profile()
                def run(fn):
                    def task():
                        profile.enable()
                        try:
                            return fn()
                        finally:
                            profile.disable()
                    return original(task)
                driver._run = run
                report = {}
                for action in ("find", "click"):
                    start = time.perf_counter()
                    if action == "find":
                        receipt = runtime.find("soffice.bin", text="Fonts", role="cell")
                        assert 'cell "Fonts"' in receipt, receipt
                    else:
                        target = next(el for el in runtime._current.elements if el.title == "Fonts")
                        receipt = runtime.click(ref=target.ref)
                    elapsed = time.perf_counter() - start
                    path = args.output.with_suffix("." + action + ".prof")
                    profile.dump_stats(str(path))
                    stats = pstats.Stats(profile)
                    counts = {}
                    for (_, _, name), values in stats.stats.items():
                        if name in {"_call_first_inline", "find_root", "build_snapshot", "active_modals",
                                    "outside_modals", "snapshot_window_list", "_fresh_application_children"}:
                            counts[name] = dict(calls=values[1], cumulative_seconds=values[3])
                    # Count the actual getter/mutator callbacks after cache
                    # lookup, separately from all adapter requests.
                    callbacks = {code.co_firstlineno for code in _atspi._call_first_inline.__code__.co_consts
                                 if isinstance(code, CodeType) and code.co_name == "<lambda>"}
                    dispatched = sum(values[1] for (filename, line, name), values in stats.stats.items()
                                     if filename == _atspi.__file__ and line in callbacks and name == "<lambda>")
                    report[action] = dict(seconds=elapsed, receipt=str(receipt),
                                          dispatched_adapter_methods=dispatched, native_worker=counts)
                    profile = cProfile.Profile()
                driver._run = original
                runtime.desktop_snapshot("soffice.bin", "window", "full")
                assert any(el.title == "Fonts" and el.selected
                           for el in runtime._current.elements), "Fonts was not selected"
                args.output.write_text(json.dumps(report, indent=2) + "\n")
        finally:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            proc.wait(timeout=15)


if __name__ == "__main__":
    main()
