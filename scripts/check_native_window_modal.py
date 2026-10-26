"""Check #1139 in a private X11/AT-SPI session, without running pytest live files.

Head:
  PYTHONPATH=. GSETTINGS_BACKEND=memory bash scripts/latency/session.sh \
    /workspace/a11y-venv-codex/bin/python scripts/check_native_window_modal.py --head

Run the same script with a 0.4.76 interpreter (without --head and without the
checkout on PYTHONPATH) for baseline timings. Compare per-operation medians.
Only the Mousepad process launched here is terminated; session.sh owns its WM.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time

from a11y_computer_use import observe, safety
from a11y_computer_use.drivers import _atspi, _linux_system
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import ComputerUseError
from a11y_computer_use.server import Runtime


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--head", action="store_true", help="require same-window refusal")
    args = parser.parse_args()
    driver = LinuxDriver()
    driver.ensure_trusted()
    results = []
    with tempfile.TemporaryDirectory(prefix="native-window-modal-") as temp:
        home = Path(temp)
        store = safety.PermissionStore(home / "permissions.json")
        store.set_tier("mousepad", safety.Tier.FULL)
        runtime = Runtime(driver=driver, store=store, audit=safety.AuditLog(home / "audit"),
                          visible_actions=False)
        for name in ("a", "b"):
            (home / f"{name}.txt").write_text("First line\nSecond line\n")
        process = subprocess.Popen(
            ["mousepad", "--disable-server", "--opening-mode=window",
             str(home / "a.txt"), str(home / "b.txt")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                windows = [w for w in _linux_system.windows() if w["pid"] == process.pid]
                if len(windows) == 2:
                    break
                assert process.poll() is None and time.monotonic() < deadline, windows
                time.sleep(.1)
            window_a = next(w for w in windows if "a.txt" in w["title"])
            window_b = next(w for w in windows if "b.txt" in w["title"])

            def activate(window):
                subprocess.run(["wmctrl", "-ia", hex(window["window_id"])], check=True)
                time.sleep(.2)

            def snapshot():
                runtime.desktop_snapshot("mousepad")
                return runtime._current

            def editor():
                return next(e for e in runtime._current.elements if e.role == "AXTextArea")

            activate(window_b)
            subprocess.run(["xdotool", "key", "ctrl+l"], check=True)
            time.sleep(.4)
            activate(window_a)
            snapshot()
            assert "a.txt" in next(e.title for e in runtime._current.elements if e.role == "AXWindow")
            assert not getattr(editor(), "blocked_by_modal", False)
            assert any(e.title == "Go To" for e in runtime._current.elements)

            for attempt in range(3):
                for operation in ("type_ref", "set_value", "type_app", "key_app", "click_ref", "click_point"):
                    activate(window_a)
                    subprocess.run(["xdotool", "key", "Escape", "ctrl+Home"], check=True)
                    snapshot()
                    target = editor()
                    handle = observe.ax_handle_for(target.snapshot_id, target.ref)
                    token = f"Probe{attempt}{operation}"
                    start = time.perf_counter()
                    if operation == "type_ref":
                        receipt = runtime.type_text(token, ref=target.ref)
                    elif operation == "set_value":
                        receipt = runtime.set_value(target.ref, token + "\nSecond line\n")
                    elif operation == "type_app":
                        receipt = runtime.type_text(token, app="mousepad")
                    elif operation == "key_app":
                        receipt = runtime.key("ctrl+End", app="mousepad")
                    elif operation == "click_ref":
                        ref = next(e.ref for e in runtime._current.elements if e.title == "File")
                        receipt = runtime.click(ref=ref)
                    else:
                        box = target.bounds
                        assert box is not None
                        receipt = runtime.click(x=int(box.x + box.width / 2), y=int(box.y + box.height / 2))
                    elapsed = time.perf_counter() - start
                    assert "outside_active_modal" not in receipt
                    snapshot()
                    if operation.startswith("type") or operation == "set_value":
                        assert token in (editor().value or ""), receipt
                    elif operation == "key_app":
                        assert driver._run(lambda: _atspi._caret_offset(handle)) == len(target.value)
                    elif operation == "click_ref":
                        assert any(e.role == "AXMenuItem" and e.title.strip() == "New" for e in runtime._current.elements), [(e.role, e.title) for e in runtime._current.elements]
                    else:
                        assert driver._run(lambda: _atspi._state_has(handle, "FOCUSED"))
                    assert any(e.title == "Go To" for e in runtime._current.elements), "dialog closed"
                    results.append(dict(operation=operation, seconds=elapsed))
                    print(operation, elapsed, receipt, flush=True)

            if args.head:
                snapshot()
                blocked = next(e for e in runtime._current.elements
                               if e.role == "AXTextArea" and e.blocked_by_modal)
                original = blocked.value
                for attempt in range(3):
                    try:
                        runtime.type_text("MUST_NOT_LAND", ref=blocked.ref)
                    except ComputerUseError as exc:
                        assert exc.detail["reason"] == "outside_active_modal", exc
                    else:
                        raise AssertionError("same-window input authorized")
                    snapshot()
                    blocked = next(e for e in runtime._current.elements
                                   if e.role == "AXTextArea" and e.blocked_by_modal)
                    assert blocked.value == original
                    print("same-window refused", attempt, flush=True)
            print(json.dumps(results), flush=True)
        finally:
            process.terminate()
            process.wait()
            runtime.close()


if __name__ == "__main__":
    main()
