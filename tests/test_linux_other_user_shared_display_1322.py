"""#1322 o_beside: another user's same-named app on the same X display.

Tester's live shape (#1324 retest): the agent's own ``/usr/bin/mousepad``
and another uid's real ``/usr/bin/mousepad`` both own a window on the same
display. The grant gate refused the agent's own app (fails closed, but a
regression against 0.4.80). Now this user's own targets decide the grant,
and every per-action route keeps the other user's window out of reach:
the pointer recheck names it as another app, and ``activate_app`` never
raises it. A target owned only by another user is still refused.
"""
from __future__ import annotations

import os
from types import SimpleNamespace as _NS

import pytest

from a11y_computer_use.drivers import _linux_exe, _linux_system
from a11y_computer_use.schema import ComputerUseError, ErrorCode
from tests.test_linux_system_synthetic import _FakeXRoot, _FakeXWin

MOUSEPAD = "/usr/bin/mousepad"
ME = os.getuid()
OTHER = ME + 1


@pytest.fixture(autouse=True)
def _honest_x_server(monkeypatch):
    """XRes names the window's own pid, as on Tester's local Xvfb."""
    monkeypatch.setattr(_linux_system, "_xres_pid", lambda win, d: _linux_system._pid_of(win, d) or None)


def _procs(monkeypatch, exes: dict[int, str | None], uids: dict[int, int]):
    m = _linux_exe
    monkeypatch.setattr(m, "pids_named", lambda n: set(exes))
    monkeypatch.setattr(m, "_x11_owner_pids", lambda n: set(exes))  # both own a window
    monkeypatch.setattr(m, "_atspi_owner_pids", lambda: set())
    monkeypatch.setattr(m, "_atspi_named_pids", lambda n: set())
    monkeypatch.setattr(m, "_uid", lambda pid: uids.get(pid, ME))
    monkeypatch.setattr(m, "exe_of", lambda pid: exes.get(pid))
    monkeypatch.setattr(m, "_status", lambda pid: (1, (pid,)) if pid in exes else (None, ()))
    monkeypatch.setattr(m, "_gone", lambda pid: False)
    return m


def test_own_mousepad_beside_another_users_on_the_same_display_is_granted(monkeypatch) -> None:
    # The other user's exe is unreadable to us (EACCES), as Tester saw.
    m = _procs(monkeypatch, {10: MOUSEPAD, 30: None}, {30: OTHER})
    assert m.check("mousepad", MOUSEPAD) == ("mousepad", None)


def test_own_mousepad_beside_another_users_readable_one_is_granted(monkeypatch) -> None:
    m = _procs(monkeypatch, {10: MOUSEPAD, 30: MOUSEPAD}, {30: OTHER})
    assert m.check("mousepad", MOUSEPAD) == ("mousepad", None)


def test_only_another_users_mousepad_is_still_refused(monkeypatch) -> None:
    m = _procs(monkeypatch, {30: MOUSEPAD}, {30: OTHER})
    key, hint = m.check("mousepad", MOUSEPAD)
    assert key == "mousepad" and hint and "another user" in hint and f"uid {OTHER}" in hint


def test_a_same_session_spoof_beside_another_users_app_is_still_refused(monkeypatch) -> None:
    """The #1310 spoof is unchanged: this user's own targets must all verify."""
    m = _procs(monkeypatch, {10: MOUSEPAD, 20: "/home/u/.local/bin/mousepad", 30: None},
               {30: OTHER})
    key, hint = m.check("mousepad", MOUSEPAD)
    assert hint and "/home/u/.local/bin/mousepad" in hint


def test_binds_never_takes_another_users_pid(monkeypatch) -> None:
    m = _procs(monkeypatch, {10: MOUSEPAD, 30: MOUSEPAD}, {30: OTHER})
    assert m.check("mousepad", MOUSEPAD)[1] is None
    assert m.binds("mousepad", 10) is True
    assert m.binds("mousepad", 30) is False


# --- per action: the other user's window is never the gated app -----------


def _shared_display(monkeypatch, mine_on_top: bool):
    mine = _FakeXWin(0x20, 0, 0, 400, 300, pid=10)
    theirs = _FakeXWin(0x30, 0, 0, 400, 300, pid=30)
    stack = [theirs, mine] if mine_on_top else [mine, theirs]
    root = _FakeXRoot(stack)
    by_id = {w.id: w for w in stack}
    display = _NS(screen=lambda: _NS(root=root), intern_atom=lambda name: name,
                  create_resource_object=lambda kind, wid: by_id[int(wid)])
    monkeypatch.setattr(_linux_system, "_display", lambda: display)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "mousepad")
    real_stat = os.stat

    def fake_stat(path, *a, **k):
        if str(path) == "/proc/30":
            return _NS(st_uid=OTHER)
        if str(path) == "/proc/10":
            return _NS(st_uid=ME)
        return real_stat(path, *a, **k)

    monkeypatch.setattr(_linux_system.os, "stat", fake_stat)
    return mine, theirs


def test_point_over_another_users_mousepad_is_not_mousepad(monkeypatch) -> None:
    _shared_display(monkeypatch, mine_on_top=False)
    owner = _linux_system.app_at_point_id(100, 100)
    assert owner != "mousepad" and f"uid {OTHER}" in owner


def test_point_over_my_mousepad_is_still_mousepad(monkeypatch) -> None:
    _shared_display(monkeypatch, mine_on_top=True)
    assert _linux_system.app_at_point_id(100, 100) == "mousepad"


def test_pointer_recheck_refuses_another_users_window(monkeypatch) -> None:
    from a11y_computer_use import server
    from a11y_computer_use.schema import Point

    _shared_display(monkeypatch, mine_on_top=False)
    monkeypatch.setattr(server, "_system_ops", lambda: _linux_system)
    with pytest.raises(ComputerUseError) as exc:
        server._recheck_target_app("mousepad", Point(x=100, y=100, display_id=0))
    assert exc.value.code is ErrorCode.FOCUS_CHANGED


def _activation(monkeypatch):
    raised: list[int] = []
    monkeypatch.setattr(_linux_system, "_managed_windows", lambda d: list(d.screen().root.stacking))
    monkeypatch.setattr(_linux_system, "_wm_class_strings", lambda win, d: ("mousepad", "Mousepad"))
    monkeypatch.setattr(_linux_system, "_win_title", lambda win, d: "note.txt - Mousepad")
    monkeypatch.setattr(_linux_system, "_is_hidden", lambda win, d: False)
    monkeypatch.setattr(_linux_system, "_geometry_on_screen", lambda g, s: True)
    monkeypatch.setattr(_linux_system, "_activate_and_settle",
                        lambda d, win, restore: raised.append(int(win.id)))
    return raised


def test_activate_app_raises_my_window_not_the_other_users_on_top(monkeypatch) -> None:
    mine, _theirs = _shared_display(monkeypatch, mine_on_top=False)
    raised = _activation(monkeypatch)
    _linux_system.activate_app("mousepad")
    assert raised == [mine.id]


def test_activate_app_with_only_another_users_window_raises_nothing(monkeypatch) -> None:
    _mine, theirs = _shared_display(monkeypatch, mine_on_top=False)
    theirs_only = _FakeXRoot([theirs])
    display = _NS(screen=lambda: _NS(root=theirs_only), intern_atom=lambda name: name,
                  create_resource_object=lambda kind, wid: theirs)
    monkeypatch.setattr(_linux_system, "_display", lambda: display)
    raised = _activation(monkeypatch)
    with pytest.raises(ComputerUseError) as exc:
        _linux_system.activate_app("mousepad")
    assert exc.value.code is ErrorCode.APP_NOT_FOUND and raised == []
