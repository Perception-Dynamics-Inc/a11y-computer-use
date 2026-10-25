"""Standalone #1097 safety check; run with session.sh, never pytest live files."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

from a11y_computer_use import safety, server
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import ComputerUseError, Scope


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--electron", required=True)
    args = parser.parse_args()
    driver = LinuxDriver()
    driver.ensure_trusted()
    with tempfile.TemporaryDirectory(prefix="electron-modal-check-") as temp:
        home = Path(temp)
        process = subprocess.Popen([args.electron, "--no-sandbox", "--disable-gpu",
            "--force-renderer-accessibility", "--password-store=basic", f"--user-data-dir={home / 'profile'}",
            str(Path(__file__).with_name("fixtures") / "electron-modal.js")],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            store = safety.PermissionStore(home / "permissions")
            store.set_tier("electron", safety.Tier.FULL)
            with server.Runtime(driver=driver, store=store, audit=safety.AuditLog(home / "audit")) as runtime:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        snap = driver.snapshot(Scope.WINDOW, "electron")
                        text = str([el.title for el in snap.elements])
                    except ComputerUseError:
                        assert time.monotonic() < deadline, "Electron did not register"
                        time.sleep(.2)
                        continue
                    if any(el.title == "Count up" for el in snap.elements):
                        break
                    assert time.monotonic() < deadline, text
                    time.sleep(.2)
                runtime.app("focus", "electron")
                subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+shift+y"], check=True)
                while True:
                    text = runtime.desktop_snapshot("electron", "window", "interactive")
                    if '"Settings Modal" (modal,active)' in text:
                        break
                    assert time.monotonic() < deadline, text
                    time.sleep(.1)
                targets = {el.title: el.ref for el in runtime._current.elements}
                results = {}
                for name, call in {
                    "click": lambda: runtime.click(ref=targets["Count up"]),
                    "type": lambda: runtime.type_text("zz", ref=targets["Main field"]),
                    "set_value": lambda: runtime.set_value(targets["Fruit"], "banana"),
                }.items():
                    for _ in range(3):
                        try:
                            call()
                        except ComputerUseError as exc:
                            assert exc.detail["reason"] == "outside_active_modal", exc
                            assert not exc.detail.get("next"), exc
                        else:
                            raise AssertionError(f"Blocked parent {name} was allowed")
                    results[name] = 3
                after = runtime.desktop_snapshot("electron", "window", "full")
                assert "BACKGROUND ACTIVATED" not in after, after
                assert any(el.title == "Fruit" and el.value == "apple" for el in runtime._current.elements), after
                field = next(el for el in runtime._current.elements if el.title == "Main field")
                assert not field.value, field
                inside = next(el for el in runtime._current.elements if el.title == "Name")
                receipt = runtime.type_text("inside", ref=inside.ref)
                assert receipt.outcome == "confirmed", receipt
                print(json.dumps(dict(refusals=results, parent_unchanged=True, inside_outcome=receipt.outcome)))
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            process.wait(timeout=15)


if __name__ == "__main__":
    main()
