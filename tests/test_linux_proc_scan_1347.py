"""#1347: one /proc walk per action, with every exe/uid/interpreter check intact.

Hermetic: /proc, the X display and the AT-SPI bus are modelled; nothing on
the host is read.
"""
from __future__ import annotations

import io
import os
import sys
import threading
from types import SimpleNamespace as _NS

import pytest

from a11y_computer_use import capture, safety, server
from a11y_computer_use.drivers import _linux_exe, _linux_system
from a11y_computer_use.schema import Display

REAL = "/usr/bin/mousepad"
IMPOSTOR = "/home/u/.local/bin/mousepad"
ME = 1000


def _png() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (8, 6)).save(out, format="PNG")
    return out.getvalue()


def _world(monkeypatch, procs: dict, front_pid: int | None) -> dict:
    """``procs``: pid -> (comm, exe, uid), read through a modelled /proc.

    ``pids_named`` is the real one; only the /proc entries under it are fake.
    Returns ``procs`` so a test can start a process between actions.
    """
    real_listdir = _linux_exe.os.listdir

    def listdir(path="."):
        if path == "/proc":
            return [str(pid) for pid in procs] + ["self", "sys"]
        return real_listdir(path)

    def names():
        return {pid: comm for pid, (comm, _exe, _uid) in procs.items()}

    monkeypatch.setattr(_linux_exe.os, "listdir", listdir)
    monkeypatch.setattr(_linux_exe, "_comm", lambda pid: procs[pid][0] if pid in procs else "")
    monkeypatch.setattr(_linux_exe, "exe_of", lambda pid: procs[pid][1] if pid in procs else None)
    monkeypatch.setattr(_linux_exe, "_uid", lambda pid: procs[pid][2] if pid in procs else None)
    monkeypatch.setattr(_linux_exe, "_me", lambda: ME)
    monkeypatch.setattr(_linux_exe, "_gone", lambda pid: pid not in procs)
    monkeypatch.setattr(_linux_exe, "_system_owned", lambda path: path == REAL)
    monkeypatch.setattr(_linux_exe, "_x11_owner_pids",
                        lambda name: {p for p, c in names().items() if c == name})
    monkeypatch.setattr(_linux_exe, "_atspi_named_pids", lambda name: set())
    monkeypatch.setattr(_linux_exe, "_atspi_owner_pids", lambda: set())
    monkeypatch.setattr(_linux_exe, "_GATED", {})
    monkeypatch.setattr(server.sys, "platform", "linux")
    monkeypatch.setattr(_linux_system, "frontmost_app_id", lambda: "mousepad" if front_pid else "")
    monkeypatch.setattr(_linux_system, "active_window_owner", lambda: front_pid, raising=False)
    monkeypatch.setattr(_linux_system, "interpreter_hosting", lambda name: None)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: names().get(pid))
    monkeypatch.setattr(_linux_system.shutil, "which", lambda name: None)
    return procs


def _runtime(tmp_path, tiers: dict):
    store = safety.PermissionStore(tmp_path / "permissions.json")
    for app, tier in tiers.items():
        store.set_tier(app, tier)
    png = _png()
    shot = capture.Screenshot(png=png, display=Display(display_id=0, width=8, height=6, scale=1.0, is_main=True))
    driver = _NS(name="linux", interpreter_host=lambda app: None, screenshot=lambda display_id=None: shot)
    return server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)


def test_screenshot_walks_proc_once(tmp_path, monkeypatch) -> None:
    _world(monkeypatch, {52: ("mousepad", REAL, ME), 60: ("bash", "/usr/bin/bash", ME)}, 52)
    rt = _runtime(tmp_path, {"mousepad": "full"})
    lookups: list[str] = []
    real = _linux_exe.pids_named
    monkeypatch.setattr(_linux_exe, "pids_named", lambda name: lookups.append(name) or real(name))
    before = _linux_exe.PROC_SCANS
    rt.screenshot()
    assert len(lookups) >= 2  # several name lookups, one walk
    assert _linux_exe.PROC_SCANS - before == 1
    rt.screenshot()
    rt.screenshot()
    assert _linux_exe.PROC_SCANS - before == 3  # one per action, never carried over


def test_screenshot_of_a_frontmost_impostor_is_still_refused(tmp_path, monkeypatch) -> None:
    _world(monkeypatch, {51: ("mousepad", IMPOSTOR, ME), 52: ("mousepad", REAL, ME)}, 51)
    rt = _runtime(tmp_path, {"mousepad": "full"})
    before = _linux_exe.PROC_SCANS
    with pytest.raises(server.ActionRefused):
        rt.screenshot()
    assert _linux_exe.PROC_SCANS - before <= 1


def test_an_impostor_started_between_actions_is_seen(tmp_path, monkeypatch) -> None:
    """The walk is per action: a process started after one action is in the next one's walk."""
    procs = _world(monkeypatch, {52: ("mousepad", REAL, ME)}, 52)
    rt = _runtime(tmp_path, {"mousepad": "full"})
    rt.screenshot()
    procs[51] = ("mousepad", IMPOSTOR, ME)
    with pytest.raises(server.ActionRefused):
        rt.screenshot()


def test_outside_an_action_every_lookup_walks_proc(monkeypatch) -> None:
    procs = _world(monkeypatch, {52: ("mousepad", REAL, ME)}, 52)
    before = _linux_exe.PROC_SCANS
    assert _linux_exe.pids_named("mousepad") == {52}
    procs[51] = ("mousepad", IMPOSTOR, ME)
    assert _linux_exe.pids_named("mousepad") == {51, 52}
    assert _linux_exe.PROC_SCANS - before == 2


def test_scope_walk_is_shared_with_the_atspi_thread_and_dropped_on_entry(monkeypatch) -> None:
    """find_root's ``binds`` runs on the AT-SPI thread; it reads the action's walk.
    A nested action (a batch step) walks again rather than reuse the outer walk."""
    procs = _world(monkeypatch, {52: ("mousepad", REAL, ME)}, 52)
    before = _linux_exe.PROC_SCANS
    with _linux_exe.proc_scope():
        assert _linux_exe.pids_named("mousepad") == {52}
        seen: list = []
        worker = threading.Thread(target=lambda: seen.append(_linux_exe.pids_named("mousepad")))
        worker.start()
        worker.join()
        assert seen == [{52}]
        assert _linux_exe.PROC_SCANS - before == 1
        procs[51] = ("mousepad", IMPOSTOR, ME)
        with _linux_exe.proc_scope():
            assert _linux_exe.pids_named("mousepad") == {51, 52}
        assert _linux_exe.pids_named("mousepad") == {51, 52}
    assert _linux_exe.PROC_SCANS - before == 3


def test_name_lookup_matches_comm_cut_and_exe_basename(monkeypatch) -> None:
    _world(monkeypatch, {
        1: ("soffice.bin", "/usr/lib/libreoffice/program/soffice.bin", ME),
        2: ("a-very-long-pro", "/opt/x/a-very-long-program-name", ME),
        3: ("renamed", "/usr/bin/mousepad", ME),
        4: ("mousepad", "/usr/bin/other", ME),
    }, None)
    with _linux_exe.proc_scope():
        assert _linux_exe.pids_named("mousepad") == {3, 4}
        assert _linux_exe.pids_named("a-very-long-program-name") == {2}
        assert _linux_exe.pids_named("soffice.bin") == {1}
        assert _linux_exe.pids_named("") == set()


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not os.path.isdir("/proc"),
    reason="reads the host's real /proc, which only Linux has",
)
def test_direct_walk_matches_the_reader_walk() -> None:
    """#1354: the inlined /proc walk gives exactly the walk through _comm and exe_of."""
    import os as _os

    entries = _os.listdir("/proc")
    direct = _linux_exe._scan_proc_direct(entries, {}, {})
    by_comm: dict = {}
    by_exe: dict = {}
    for entry in entries:
        if entry.isdigit():
            pid = int(entry)
            if comm := _linux_exe._comm(pid):
                by_comm.setdefault(comm, set()).add(pid)
            if exe := _linux_exe.exe_of(pid):
                by_exe.setdefault(_os.path.basename(exe), set()).add(pid)
    me = _os.getpid()
    # Processes may come and go between the walks; this one is in both.
    assert me in direct[0][_linux_exe._comm(me)] and me in by_comm[_linux_exe._comm(me)]
    common = set(direct[0]) & set(by_comm)
    assert len(common) >= 0.9 * max(len(by_comm), 1)
    for name in common:
        assert len(direct[0][name] ^ by_comm[name]) <= 2
    exe = _os.path.basename(_linux_exe.exe_of(me))
    assert me in direct[1][exe] and me in by_exe[exe]
