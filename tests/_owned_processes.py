"""Process cleanup for live tests on a shared box: only what the test started.

Never stop a process by name, command line or user (no pkill/killall, no
/proc comm scan followed by a kill). A test records the pid it launched with
``start_new_session=True`` and stops that session's members. A same-named
process that was already running belongs to someone else: the test fails
with its pids rather than killing it.
"""
from __future__ import annotations

import os
import signal
import time

import pytest


def _stat(pid: int) -> tuple[str, str, int | None] | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            stat = fh.read()
    except OSError:
        return None
    comm = stat[stat.find("(") + 1:stat.rfind(")")]
    fields = stat[stat.rfind(")") + 2:].split()
    if not fields:
        return None
    sid = int(fields[3]) if len(fields) > 3 and fields[3].lstrip("-").isdigit() else None
    return comm, fields[0], sid


def _pids() -> list[int]:
    try:
        return [int(e) for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return []


def running(comms) -> list[int]:
    """Live (non-zombie) pids whose comm is in ``comms``. Read only."""
    comms = set(comms)
    found = []
    for pid in _pids():
        info = _stat(pid)
        if info and info[0] in comms and info[1] != "Z":
            found.append(pid)
    return found


def refuse_preexisting(comms, label: str) -> None:
    """Fail when ``label`` is already running: it is not this test's to stop."""
    pids = running(comms)
    if pids:
        pytest.fail(
            f"{label} is already running (pids {sorted(pids)}) and was not started by "
            "this test; it is left alone. Close it and rerun.")


def await_exit(comms, label: str, timeout: float = 15.0) -> None:
    """Wait for ``label`` processes to exit on their own; fail, never kill."""
    deadline = time.monotonic() + timeout
    while running(comms) and time.monotonic() < deadline:
        time.sleep(0.2)
    refuse_preexisting(comms, label)


def session_members(leader: int) -> list[int]:
    """Live pids in the session ``leader`` started (its recorded pid)."""
    out = []
    for pid in _pids():
        info = _stat(pid)
        if info and info[2] == leader and info[1] != "Z":
            out.append(pid)
    return out


def stop_session(proc_or_pid, timeout: float = 3.0) -> None:
    """Stop the recorded launch session: SIGTERM, then SIGKILL its members."""
    leader = getattr(proc_or_pid, "pid", proc_or_pid)
    if not isinstance(leader, int) or leader <= 1:
        return
    for sig, wait in ((signal.SIGTERM, timeout), (signal.SIGKILL, timeout)):
        members = session_members(leader)
        if not members:
            return
        for pid in members:
            try:
                os.kill(pid, sig)  # owned pid: member of our recorded session
            except OSError:
                pass
        deadline = time.monotonic() + wait
        while session_members(leader) and time.monotonic() < deadline:
            time.sleep(0.1)


def _children(pid: int) -> list[int]:
    out = []
    for other in _pids():
        info = _stat(other)
        if info and info[1] != "Z" and _ppid(other) == pid:
            out.append(other)
    return out


def _ppid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            stat = fh.read()
    except OSError:
        return None
    fields = stat[stat.rfind(")") + 2:].split()
    return int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else None


def record_spawns(monkeypatch) -> list:
    """Record every Popen the runtime's app launch creates (``_spawn``).

    The returned list holds those Popen handles: the only processes a test
    that launches through ``runtime.app`` may stop.
    """
    from a11y_computer_use.drivers import _linux_system

    spawned: list = []
    real = _linux_system._spawn

    def spawn(argv, identifier):
        proc = real(argv, identifier)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(_linux_system, "_spawn", spawn)
    return spawned


def stop_spawned(procs, timeout: float = 3.0) -> None:
    """Stop the recorded Popen processes and the children they forked.

    A child is ours only while its parent pid is a recorded launch (or one of
    its recorded children); nothing is selected by name, title or user.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        tree: list[int] = []
        for proc in procs:
            pid = getattr(proc, "pid", None)
            if not isinstance(pid, int) or pid <= 1:
                continue
            if getattr(proc, "poll", lambda: None)() is None:
                tree.append(pid)
            frontier = [pid]
            while frontier:
                kids = _children(frontier.pop())
                tree.extend(kids)
                frontier.extend(kids)
        if not tree:
            return
        for pid in tree:
            try:
                os.kill(pid, sig)  # owned pid: a recorded Popen or its forked child
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and any(
            _stat(p) and _stat(p)[1] != "Z" for p in tree
        ):
            for proc in procs:
                getattr(proc, "poll", lambda: None)()
            time.sleep(0.1)


def fail_on_leftovers(comms, before, label: str, timeout: float = 5.0) -> None:
    """Fail, never kill, when same-named processes this test did not record remain.

    ``before`` is ``running(comms)`` taken before the launch. Pids that are
    new but not ours (a daemonised or re-parented app) are reported for the
    operator to inspect.
    """
    deadline = time.monotonic() + timeout
    left = [p for p in running(comms) if p not in set(before)]
    while left and time.monotonic() < deadline:
        time.sleep(0.1)
        left = [p for p in running(comms) if p not in set(before)]
    if left:
        pytest.fail(f"{label} left processes it did not launch itself: {sorted(left)}; "
                    "they were not killed")
