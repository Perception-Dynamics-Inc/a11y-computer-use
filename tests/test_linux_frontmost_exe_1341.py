"""#1341: the frontmost app on a no-app route is exe/uid/interpreter-bound like a named app.

Hermetic: the X display, /proc and the AT-SPI bus are modelled; nothing on
the host is read.
"""
from __future__ import annotations

from types import SimpleNamespace as _NS

import pytest

from a11y_computer_use import safety, server
from a11y_computer_use.drivers import _linux_exe, _linux_system
from a11y_computer_use.schema import TypeText

REAL = "/usr/bin/mousepad"
IMPOSTOR = "/home/u/.local/bin/mousepad"
PY = "/usr/bin/python3.12"
ME = 1000


def _world(monkeypatch, procs: dict, front_pid: int | None, *, front_name: str = "mousepad") -> None:
    """``procs``: pid -> (comm, exe, uid). ``front_pid`` owns the active window (X server's view)."""
    names = {pid: comm for pid, (comm, _exe, _uid) in procs.items()}
    monkeypatch.setattr(server.sys, "platform", "linux")
    monkeypatch.setattr(_linux_system, "frontmost_app_id", lambda: front_name if front_pid else "")
    monkeypatch.setattr(_linux_system, "active_window_owner", lambda: front_pid, raising=False)
    monkeypatch.setattr(_linux_system, "interpreter_hosting", lambda name: None)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: names.get(pid))
    monkeypatch.setattr(_linux_system.shutil, "which", lambda name: None)
    monkeypatch.setattr(_linux_exe, "pids_named", lambda name: {p for p, c in names.items() if c == name})
    monkeypatch.setattr(_linux_exe, "_x11_owner_pids", lambda name: {p for p, c in names.items() if c == name})
    monkeypatch.setattr(_linux_exe, "_atspi_named_pids", lambda name: set())
    monkeypatch.setattr(_linux_exe, "_atspi_owner_pids", lambda: set())
    monkeypatch.setattr(_linux_exe, "exe_of", lambda pid: procs[pid][1] if pid in procs else None)
    monkeypatch.setattr(_linux_exe, "_uid", lambda pid: procs[pid][2] if pid in procs else None)
    monkeypatch.setattr(_linux_exe, "_me", lambda: ME)
    monkeypatch.setattr(_linux_exe, "_gone", lambda pid: pid not in procs)
    monkeypatch.setattr(_linux_exe, "_system_owned", lambda path: path in (REAL, PY))
    monkeypatch.setattr(_linux_exe, "_GATED", {})


def _runtime(tmp_path, tiers: dict):
    store = safety.PermissionStore(tmp_path / "permissions.json")
    for app, tier in tiers.items():
        store.set_tier(app, tier)
    driver = _NS(name="linux", interpreter_host=lambda app: None)
    return server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)


def _type_frontmost(rt) -> list:
    """The no-app, no-ref type route's gate: ``type_text`` gates ``_frontmost()``."""
    typed: list = []
    front = rt._frontmost()
    rt._run_gated(TypeText(text="secret"), front, lambda: typed.append(front) or 6)
    return typed


def test_frontmost_impostor_alone_is_refused(tmp_path, monkeypatch) -> None:
    _world(monkeypatch, {51: ("mousepad", IMPOSTOR, ME)}, 51)
    rt = _runtime(tmp_path, {"mousepad": "full"})
    with pytest.raises(server.ActionRefused):
        _type_frontmost(rt)


def test_frontmost_impostor_next_to_the_real_mousepad_is_refused(tmp_path, monkeypatch) -> None:
    _world(monkeypatch, {51: ("mousepad", IMPOSTOR, ME), 52: ("mousepad", REAL, ME)}, 51)
    rt = _runtime(tmp_path, {"mousepad": "full"})
    with pytest.raises(server.ActionRefused):
        _type_frontmost(rt)


def test_frontmost_of_another_user_is_refused(tmp_path, monkeypatch) -> None:
    _world(monkeypatch, {53: ("mousepad", REAL, 1001)}, 53)
    rt = _runtime(tmp_path, {"mousepad": "full"})
    with pytest.raises(server.ActionRefused):
        _type_frontmost(rt)


def test_frontmost_whose_owner_the_x_server_cannot_name_is_refused(tmp_path, monkeypatch) -> None:
    """The active window's pid comes from XRes; a self-reported one is never trusted."""
    _world(monkeypatch, {52: ("mousepad", REAL, ME)}, 52)
    monkeypatch.setattr(_linux_system, "active_window_owner", lambda: None, raising=False)
    rt = _runtime(tmp_path, {"mousepad": "full"})
    with pytest.raises(server.ActionRefused):
        _type_frontmost(rt)


def test_frontmost_python_copy_under_a_python3_grant_is_refused(tmp_path, monkeypatch) -> None:
    _world(monkeypatch, {54: ("python3", "/tmp/python3", ME)}, 54, front_name="python3")
    rt = _runtime(tmp_path, {"python3": "full"})
    with pytest.raises(server.ActionRefused):
        _type_frontmost(rt)


def test_control_the_real_mousepad_still_types(tmp_path, monkeypatch) -> None:
    _world(monkeypatch, {52: ("mousepad", REAL, ME)}, 52)
    rt = _runtime(tmp_path, {"mousepad": "full"})
    assert _type_frontmost(rt) == ["mousepad"]


def test_control_an_installed_python3_app_still_types(tmp_path, monkeypatch) -> None:
    _world(monkeypatch, {54: ("python3", PY, ME)}, 54, front_name="python3")
    rt = _runtime(tmp_path, {"python3": "full"})
    assert _type_frontmost(rt) == ["python3"]
