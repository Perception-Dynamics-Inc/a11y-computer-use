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
                os.kill(pid, sig)
            except OSError:
                pass
        deadline = time.monotonic() + wait
        while session_members(leader) and time.monotonic() < deadline:
            time.sleep(0.1)


def stop_pids(pids, timeout: float = 3.0) -> None:
    """Stop exactly the pids a test recorded as its own launch."""
    pids = [p for p in pids if isinstance(p, int) and p > 1]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        live = [p for p in pids if _stat(p) and _stat(p)[1] != "Z"]
        if not live:
            return
        for pid in live:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while any(_stat(p) and _stat(p)[1] != "Z" for p in live) and time.monotonic() < deadline:
            time.sleep(0.1)
