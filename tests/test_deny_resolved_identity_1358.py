"""#1358: a deny on an interpreter-hosted app's own name applies to the target.

The app (prgname / AT-SPI name ``cuprobe1296``, WM_CLASS ``cuprobe1296``,
comm ``python3``, exe ``/usr/bin/python3.13``) is reachable as ``python3``
(the #1296 grant key), as a ``python3``-labelled snapshot, and as the
frontmost app of an untargeted ``type``. Every route must be refused when
``cuprobe1296`` is denied, and an unreadable identity fails closed.

Hermetic: no display, no AT-SPI; the identity sources are stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from a11y_computer_use import safety, server
from a11y_computer_use.drivers import _atspi, _linux_exe, _linux_system
from a11y_computer_use.schema import ObserveOp, ObserveVerb, TypeText
from a11y_computer_use.server import ActionRefused

PID = 4242


class _Audit:
    def __init__(self) -> None:
        self.rows: list = []

    def record_action(self, *args, **kwargs) -> None:
        self.rows.append((args, kwargs))


@pytest.fixture
def rt(tmp_path, monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "linux")
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier("python3", safety.Tier.FULL)
    store.set_tier("cuprobe1296", safety.Tier.FULL)
    store.add_deny("cuprobe1296")
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.store = store
    runtime._current = None
    runtime._resolves_apps = lambda: False
    runtime.driver = SimpleNamespace(name="linux")
    runtime.audit = _Audit()
    _identity(monkeypatch)
    return runtime


def _identity(monkeypatch, *, atspi=True, x11=True, atspi_name="cuprobe1296", wm="cuprobe1296"):
    monkeypatch.setattr(_linux_exe, "target_pids", lambda name, named=None: {PID} if name == "python3" else set())
    monkeypatch.setattr(_linux_exe, "exe_of", lambda pid: "/usr/bin/python3.13" if pid == PID else None)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "python3" if pid == PID else None)
    # raising=False: the helpers are new in the fix; on main these tests fail, not error.
    monkeypatch.setattr(_atspi, "registered_names_by_pid",
                        lambda: ({PID: {atspi_name}} if atspi_name else {PID: set()}) if atspi else None,
                        raising=False)
    monkeypatch.setattr(_linux_system, "window_names_by_pid",
                        lambda: ({PID: {wm}} if wm else {}) if x11 else None, raising=False)
    monkeypatch.setattr(_linux_system, "active_window_owner", lambda: PID)


def _refused(runtime, app: str) -> safety.Decision:
    with pytest.raises(ActionRefused) as info:
        runtime._require_permission(TypeText(text="PY1312"), app)
    return info.value.decision


def test_type_app_python3_is_refused_by_the_atspi_name(rt) -> None:
    decision = _refused(rt, "python3")
    assert decision.verdict is safety.Verdict.DENY
    assert "cuprobe1296 is on the deny list" in decision.reason
    assert rt.audit.rows, "the refusal is audited before any input"


def test_python3_labelled_snapshot_is_refused(rt) -> None:
    with pytest.raises(ActionRefused) as info:
        rt._require_permission(ObserveOp(verb=ObserveVerb.SNAPSHOT, app="python3"), "python3")
    assert "cuprobe1296 is on the deny list" in info.value.decision.reason


def test_ref_action_from_a_python3_snapshot_is_refused(rt) -> None:
    rt._current = SimpleNamespace(app="python3", snapshot_id="snap-3")
    rt._requested_names = {"snap-3": "python3"}
    key = rt._gate_app("python3")
    assert key == "python3"
    assert _refused(rt, key).verdict is safety.Verdict.DENY


def test_wm_class_alone_also_denies(rt, monkeypatch) -> None:
    _identity(monkeypatch, atspi_name="", wm="cuprobe1296")
    assert "cuprobe1296" in _refused(rt, "python3").reason


def test_untargeted_type_gates_on_the_active_window_identity(rt, monkeypatch) -> None:
    monkeypatch.setattr(rt, "_resolve_app", lambda ident: (None, "python3"))
    key = rt._linux_frontmost_key("python3")
    assert key == "cuprobe1296"
    decision = safety.check_action(TypeText(text="FRONT1312"), key, store=rt.store)
    assert decision.verdict is safety.Verdict.DENY


def test_unreadable_atspi_identity_fails_closed(rt, monkeypatch) -> None:
    _identity(monkeypatch, atspi=False)
    _identity_wm_none(monkeypatch)
    decision = _refused(rt, "python3")
    assert decision.verdict is safety.Verdict.DENY and "fail closed" in decision.reason


def _identity_wm_none(monkeypatch) -> None:
    monkeypatch.setattr(_linux_system, "window_names_by_pid", lambda: {PID: {"other"}}, raising=False)


def test_unreadable_x11_identity_fails_closed(rt, monkeypatch) -> None:
    _identity(monkeypatch, x11=False, atspi_name="other")
    assert "fail closed" in _refused(rt, "python3").reason


def test_unreadable_frontmost_identity_fails_closed(rt, monkeypatch) -> None:
    _identity(monkeypatch, atspi=False, wm="other")
    monkeypatch.setattr(rt, "_resolve_app", lambda ident: (None, "python3"))
    key = rt._linux_frontmost_key("python3")
    assert key != "python3"
    assert not safety.check_action(TypeText(text="x"), key, store=rt.store).allowed


def test_other_python_app_still_allowed(rt, monkeypatch) -> None:
    """Control: a python3 app whose names are not denied keeps its grant."""
    _identity(monkeypatch, atspi_name="otherapp", wm="otherapp")
    assert rt._require_permission(TypeText(text="ok"), "python3").allowed


def test_no_deny_list_skips_the_identity_read(tmp_path, rt, monkeypatch) -> None:
    rt.store = safety.PermissionStore(tmp_path / "nodeny.json")
    rt.store.set_tier("python3", safety.Tier.FULL)
    monkeypatch.setattr(_atspi, "registered_names_by_pid", lambda: pytest.fail("read without a deny list"),
                        raising=False)
    assert rt._require_permission(TypeText(text="ok"), "python3").allowed


def test_other_platforms_are_unchanged(rt, monkeypatch) -> None:
    monkeypatch.setattr(server.sys, "platform", "darwin")
    assert rt._require_permission(TypeText(text="ok"), "python3").allowed
