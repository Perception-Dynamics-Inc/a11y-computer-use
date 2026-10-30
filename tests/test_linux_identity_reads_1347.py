"""#1347: one registry/X identity read per gate, never staler than one poll.

The desktop application list, ``pids_matching`` and ``active_window`` are
shared by ``find_root`` and the exe check while an action is being gated.
Every poll iteration drops them, and the action body (after the gate) reads
them live. Hermetic: /proc, X and the AT-SPI bus are modelled.
"""
from __future__ import annotations

import types

from a11y_computer_use.drivers import _atspi, _atspi_events, _linux_exe, _linux_system
from a11y_computer_use.schema import Scope
from tests.test_linux_proc_scan_1347 import IMPOSTOR, ME, REAL, _runtime, _world


class _Acc:
    def __init__(self, name: str = "", pid: int = 0, children: list | None = None) -> None:
        self.name, self.pid, self.children = name, pid, children if children is not None else []

    def get_name(self) -> str:
        return self.name

    def get_process_id(self) -> int:
        return self.pid

    def get_child_count(self) -> int:
        return len(self.children)

    def get_child_at_index(self, index: int):
        return self.children[index]


def _bus(monkeypatch) -> dict:
    """A modelled AT-SPI registry and X probes, with read counters."""
    desktop = _Acc("main")
    reads = {"apps": 0, "pids_matching": 0, "active_window": 0}
    fresh = _atspi._fresh_application_children

    def counted(acc):
        if acc is desktop:
            reads["apps"] += 1
        return fresh(acc)

    def pids_matching(identifier):
        reads["pids_matching"] += 1
        return {a.pid for a in desktop.children if a.name == identifier}

    def active_window():
        reads["active_window"] += 1
        return None

    monkeypatch.setattr(_atspi, "_atspi", lambda: types.SimpleNamespace(get_desktop=lambda i: desktop))
    monkeypatch.setattr(_atspi, "_without_child_cache", lambda acc, read: read())
    monkeypatch.setattr(_atspi, "_fresh_application_children", counted)
    monkeypatch.setattr(_atspi_events, "run", lambda fn: fn())
    monkeypatch.setattr(_linux_system, "_pids_matching", pids_matching)
    monkeypatch.setattr(_linux_system, "_active_window", active_window)
    return {"desktop": desktop, "reads": reads}


def test_one_app_list_read_per_screenshot(tmp_path, monkeypatch) -> None:
    _world(monkeypatch, {52: ("mousepad", REAL, ME)}, 52)
    # The exe check reads the live registry (``_world`` stubs it out).
    monkeypatch.setattr(_linux_exe, "_atspi_named_pids", _ATSPI_NAMED)
    monkeypatch.setattr(_linux_exe, "_atspi_owner_pids", _ATSPI_OWNERS)
    bus = _bus(monkeypatch)
    bus["desktop"].children.append(_Acc("gedit", 77))
    rt = _runtime(tmp_path, {"mousepad": "full"})
    roots: list = []
    rt.driver.interpreter_host = lambda app: roots.append(_atspi.find_root(app, Scope.APP))
    rt.screenshot()
    assert roots == [None]
    # find_root and both registry reads of the exe check: one list, was three.
    assert bus["reads"] == {"apps": 1, "pids_matching": 1, "active_window": 1}
    rt.screenshot()
    assert bus["reads"] == {"apps": 2, "pids_matching": 2, "active_window": 2}  # every action reads again


def test_launch_wait_sees_an_app_that_appears_mid_action(tmp_path, monkeypatch) -> None:
    _world(monkeypatch, {}, None)
    bus = _bus(monkeypatch)
    rt = _runtime(tmp_path, {"mousepad": "full"})
    polls = {"n": 0}

    def windows():
        polls["n"] += 1
        if polls["n"] == 2:  # the app registers between two polls
            bus["desktop"].children.append(_Acc("mousepad", 52))
        return [{"app": "mousepad", "pid": pid, "title": "Untitled", "window_id": pid}
                for pid in _linux_system.pids_matching("mousepad")]

    rt.driver.windows = windows
    monkeypatch.setattr(rt, "_resolves_apps", lambda: False)
    with _linux_exe.proc_scope():  # still gating: the shared reads are on
        assert _linux_system.pids_matching("mousepad") == set()
        assert rt._wait_first_window("mousepad", 5.0) == "Untitled"
    assert polls["n"] == 2  # seen on the first poll after it appeared


def test_an_impostor_between_poll_iterations_is_caught(tmp_path, monkeypatch) -> None:
    procs = _world(monkeypatch, {52: ("mousepad", REAL, ME)}, 52)
    rt = _runtime(tmp_path, {"mousepad": "full"})
    with _linux_exe.proc_scope():  # still gating: the shared reads are on
        assert rt._linux_exe_key("mousepad") == "mousepad"
        procs[51] = ("mousepad", IMPOSTOR, ME)  # starts between two polls
        _linux_exe.drop_scope_memo()  # the next poll iteration
        assert rt._linux_exe_key("mousepad") == "mousepad (unverified executable)"
    procs.pop(51)
    with _linux_exe.proc_scope():
        assert rt._linux_exe_key("mousepad") == "mousepad"
        _linux_exe.seal_scope_memo()  # the gate has decided: the body reads live
        procs[51] = ("mousepad", IMPOSTOR, ME)
        assert rt._linux_exe_key("mousepad") == "mousepad (unverified executable)"


def test_the_action_body_and_recheck_read_live(tmp_path, monkeypatch) -> None:
    bus = _bus(monkeypatch)
    with _linux_exe.proc_scope():
        assert _linux_system.pids_matching("mousepad") == set()
        _linux_exe.seal_scope_memo()
        bus["desktop"].children.append(_Acc("mousepad", 52))
        assert _linux_system.pids_matching("mousepad") == {52}
        assert _atspi._registered_pids_named("mousepad") == {52}
        assert _atspi._registered_pids_named("mousepad") == {52}
        assert bus["reads"]["apps"] == 2  # sealed: no sharing, every call reads
        with _linux_exe.proc_scope():  # a batch step gates afresh
            _atspi._registered_pids()
            _atspi._registered_pids()
            assert bus["reads"]["apps"] == 3
        _atspi._registered_pids()  # back in the sealed outer body
        assert bus["reads"]["apps"] == 4


_ATSPI_NAMED = _linux_exe._atspi_named_pids
_ATSPI_OWNERS = _linux_exe._atspi_owner_pids


def test_gate_x_probes_once_per_gate_live_after_seal(monkeypatch) -> None:
    """#1347: the gate's X probes (interpreter_hosting, verified_pids_matching,
    resolve_app) are read once per gate, fresh in every action, every poll and
    the sealed body."""
    reads = {"interpreter_hosting": 0, "verified_pids_matching": 0, "resolve_app": 0}
    world = {"host": None, "pids": set(), "key": "mousepad"}

    def hosting(identifier):
        reads["interpreter_hosting"] += 1
        return world["host"]

    def verified(identifier):
        reads["verified_pids_matching"] += 1
        return set(world["pids"])

    def resolve(identifier):
        reads["resolve_app"] += 1
        return world["key"]

    monkeypatch.setattr(_linux_system, "_interpreter_hosting", hosting)
    monkeypatch.setattr(_linux_system, "_verified_pids_matching", verified)
    monkeypatch.setattr(_linux_system, "_resolve_app", resolve)

    def probe():
        return (_linux_system.interpreter_hosting("mousepad"),
                _linux_system.verified_pids_matching("mousepad"),
                _linux_system.resolve_app("mousepad"))

    with _linux_exe.proc_scope():
        assert probe() == probe() == (None, set(), "mousepad")
        assert reads == {"interpreter_hosting": 1, "verified_pids_matching": 1, "resolve_app": 1}
        world.update(host="python3", pids={51}, key="python3")  # an impostor maps a window
        _linux_exe.drop_scope_memo()  # next poll iteration
        assert probe() == ("python3", {51}, "python3")
        world.update(host=None, pids={52}, key="mousepad")
        _linux_exe.seal_scope_memo()  # the gate has decided: the body reads live
        assert probe() == (None, {52}, "mousepad")
        world["pids"] = {51, 52}
        assert probe()[1] == {51, 52}
    with _linux_exe.proc_scope():  # the next action reads again
        probe()
        probe()
    assert reads == {"interpreter_hosting": 5, "verified_pids_matching": 5, "resolve_app": 5}
    probe()  # outside an action: never shared
    probe()
    assert reads["resolve_app"] == 7


class _CountedAcc(_Acc):
    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.asked = {"get_name": 0, "get_process_id": 0}

    def get_name(self) -> str:
        self.asked["get_name"] += 1
        return self.name

    def get_process_id(self) -> int:
        self.asked["get_process_id"] += 1
        return self.pid


def test_registry_name_and_pid_read_once_per_gate(monkeypatch) -> None:
    """#1347: find_root and the exe check's registry reads ask each application
    for its name and pid once per gate; every poll and the sealed body ask again,
    and a failed or empty read is never shared."""
    bus = _bus(monkeypatch)
    monkeypatch.setattr(_linux_exe, "binds", lambda app, pid: True)
    gedit, mp = _CountedAcc("gedit", 77), _CountedAcc("mousepad", 52)
    bus["desktop"].children += [gedit, mp]

    def gate():
        _atspi.find_root("mousepad", Scope.APP)
        assert _atspi._registered_pids_named("mousepad") == {mp.pid} - {0}
        _atspi._registered_pids()

    with _linux_exe.proc_scope():
        gate()
        assert gedit.asked == {"get_name": 1, "get_process_id": 1}
        mousepad_reads = dict(mp.asked)
        gate()
        # The matched app's names are shared; only find_root's own pid reads
        # under its reply watch stay live.
        assert mp.asked["get_name"] == mousepad_reads["get_name"]
        assert gedit.asked == {"get_name": 1, "get_process_id": 1}
        mp.pid = 53  # the app restarts between two polls
        _linux_exe.drop_scope_memo()
        gate()
        assert _atspi._registered_pids() == {77, 53}
        assert gedit.asked["get_name"] == 2
        _linux_exe.seal_scope_memo()  # the body reads live
        mp.pid = 54
        assert _atspi._registered_pids() == {77, 54}
        before = gedit.asked["get_name"]
        _atspi._registered_pids_named("mousepad")
        _atspi._registered_pids_named("mousepad")
        assert gedit.asked["get_name"] == before + 2
    gedit.name, gedit.pid = "", 0  # an unanswered registrant
    with _linux_exe.proc_scope():
        before = dict(gedit.asked)
        _atspi._registered_pids()
        _atspi._registered_pids()
        _atspi._registered_pids_named("mousepad")
        _atspi._registered_pids_named("mousepad")
        assert gedit.asked["get_process_id"] == before["get_process_id"] + 2
        assert gedit.asked["get_name"] == before["get_name"] + 2
