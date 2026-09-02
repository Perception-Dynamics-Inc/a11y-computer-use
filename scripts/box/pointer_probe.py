"""Coordinate-input probe for a real Linux desktop (run by verify-pointer.sh).

Launches the same GTK3 window the live tests use (a "Save" button that sets the
entry to SAVED), parks the pointer away from the origin, then proves:

1. `LinuxDriver.click(Point)` positions the pointer at the requested absolute
   screen coordinates and the click lands on the button.
2. `Runtime.click(x, y)` with no display_id works off macOS (the display id
   comes from the driver seam) and lands the same way.

Prints one line per check; exits non-zero on the first failure.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from computeruse import safety, server  # noqa: E402
from computeruse.drivers import _linux_input  # noqa: E402
from computeruse.drivers.linux import LinuxDriver  # noqa: E402
from computeruse.schema import Point, Scope  # noqa: E402
from tests.test_linux_live import _APP, _GTK_APP  # noqa: E402


def pointer():
    from Xlib import display as _xd

    q = _xd.Display().screen().root.query_pointer()
    return int(q.root_x), int(q.root_y)


def wait_snapshot(driver, timeout=15.0):
    deadline = time.monotonic() + timeout
    snap = None
    while time.monotonic() < deadline:
        snap = driver.snapshot(Scope.WINDOW, _APP)
        if snap.elements and any(el.editable for el in snap.elements):
            return snap
        time.sleep(0.5)
    return snap


def entry_values(driver):
    return [el.value for el in driver.snapshot(Scope.WINDOW, _APP).elements if el.value]


def check(ok: bool, msg: str) -> None:
    print(("PASS " if ok else "FAIL ") + msg, flush=True)
    if not ok:
        sys.exit(1)


def main() -> None:
    driver = LinuxDriver()
    driver.ensure_trusted()
    tmp = Path(tempfile.mkdtemp())
    script = tmp / "cuatestapp.py"
    script.write_text(_GTK_APP)
    proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        snap = wait_snapshot(driver)
        button = next(el for el in snap.elements if el.clickable and "Save" in el.title)
        entry = next(el for el in snap.elements if el.editable)
        b = button.bounds
        cx, cy = int(b.x + b.width / 2), int(b.y + b.height / 2)
        print(f"button bounds={b} center=({cx}, {cy}); display_id={driver.main_display_id()}")

        # 1. driver-level coordinate click, pointer parked off-origin first
        _linux_input._move(cx + 150, cy + 120)
        _linux_input._flush()
        parked = pointer()
        print(f"parked pointer at {parked}")
        driver.click(Point(display_id=driver.main_display_id(), x=cx, y=cy))
        time.sleep(0.4)
        check(pointer() == (cx, cy), f"driver.click left the pointer at {pointer()} == ({cx}, {cy})")
        check(any("SAVED" in (v or "") for v in entry_values(driver)),
              f"driver.click landed on the button (entry values {entry_values(driver)})")

        # reset the entry through the a11y path, so the next check is fresh
        driver.set_value(driver.resolve_ref(snap, entry.ref), "")
        time.sleep(0.3)
        check(not any("SAVED" in (v or "") for v in entry_values(driver)), "entry cleared via set_value")

        # 2. Runtime.click(x, y) with no display_id (used to NameError off macOS)
        store = safety.PermissionStore(tmp / "permissions.json")
        rt = server.Runtime(store=store, audit=safety.AuditLog(tmp / "audit"), driver=driver)
        front = rt._frontmost()
        print(f"frontmost app id (gating key): {front!r}")
        store.set_tier(front, safety.Tier.FULL)
        _linux_input._move(cx + 150, cy + 120)
        _linux_input._flush()
        msg = rt.click(x=cx, y=cy)
        print(f"Runtime.click -> {msg}")
        time.sleep(0.4)
        check(msg.startswith("clicked (") and "display" in msg,
              f"Runtime.click without display_id returned a click receipt: {msg!r}")
        check(pointer() == (cx, cy), f"Runtime.click left the pointer at {pointer()} == ({cx}, {cy})")
        check(any("SAVED" in (v or "") for v in entry_values(driver)),
              f"Runtime.click landed on the button (entry values {entry_values(driver)})")
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
