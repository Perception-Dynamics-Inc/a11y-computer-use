"""#1330 live: after another app's snapshot or ``app(focus)``, an old ref is
refused before any input instead of acting on the same-numbered control of
the other app. Self-skips without an EWMH window manager and AT-SPI bus."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux desktop only")

from tests.test_linux_desktop_live import requires_desktop  # noqa: E402

_SRC = """
import sys
import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk, GLib
name, x = sys.argv[1], int(sys.argv[2])
GLib.set_prgname(name)
w = Gtk.Window(title=name); w.set_default_size(300, 160); w.move(x, 200)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6); w.add(box)
btn = Gtk.Button(label="Save " + name); box.pack_start(btn, False, False, 0)
lbl = Gtk.Label(label="idle"); box.pack_start(lbl, False, False, 0)
btn.connect("clicked", lambda *_: lbl.set_text("saved"))
w.connect("destroy", Gtk.main_quit); w.show_all(); Gtk.main()
"""
_A, _B = "cuarefa", "cuarefb"


@pytest.fixture
def two_apps(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    script = tmp_path / "app.py"
    script.write_text(_SRC)
    # One interpreter copy per app: the owner id follows the executable, so two
    # plain ``python3`` processes (or symlinks to one) would be the same app.
    exes = {}
    for name in (_A, _B):
        exes[name] = tmp_path / name
        shutil.copy2(os.path.realpath(sys.executable), exes[name])
    procs = [subprocess.Popen([str(exes[name]), str(script), name, x],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             for name, x in ((_A, "60"), (_B, "520"))]
    try:
        from a11y_computer_use import safety, server

        rt = server.Runtime()
        store = safety.PermissionStore()
        for name in (_A, _B):
            store.set_tier(name, safety.Tier.FULL, exe=os.path.realpath(exes[name]))
        deadline = time.time() + 20
        last: Exception | None = None
        while time.time() < deadline:
            try:
                if all("Save " + n in rt.desktop_snapshot(n) for n in (_A, _B)):
                    break
            except Exception as ex:  # noqa: BLE001
                last = ex
            time.sleep(0.3)
        else:
            pytest.fail(f"the test windows never appeared on the a11y bus: {last!r}")
        yield rt
    finally:
        for proc in procs:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def _ref(text: str, label: str) -> str:
    line = next(line for line in text.splitlines() if label in line)
    return next(tok for tok in line.split() if tok.startswith("e") and tok[1:].isdigit())


def _labels(rt) -> dict[str, bool]:
    return {n: "saved" in rt.desktop_snapshot(n) for n in (_A, _B)}


@requires_desktop
def test_live_old_ref_after_focus_or_other_snapshot_never_acts_in_the_other_app(two_apps) -> None:
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    rt = two_apps
    ref_a = _ref(rt.desktop_snapshot(_A), "Save " + _A)
    ref_b = _ref(rt.desktop_snapshot(_B), "Save " + _B)
    assert ref_a == ref_b  # same number in both apps: Tester's precondition

    with pytest.raises(ComputerUseError) as caught:  # step 4: uncovered, other snapshot
        rt.click(ref=ref_a)
    assert caught.value.code is ErrorCode.STALE_REF
    assert caught.value.detail["reason"] == "other_snapshot"

    rt.desktop_snapshot(_B)
    rt.app("focus", _A)
    with pytest.raises(ComputerUseError) as caught:  # step 7: focused A, B's tree current
        rt.click(ref=ref_a)
    assert caught.value.detail["reason"] == "other_app_focused"
    time.sleep(0.5)
    assert _labels(rt) == {_A: False, _B: False}
