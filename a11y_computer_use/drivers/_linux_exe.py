"""Executable-path binding for Linux app grants.

A Linux process picks its own comm (``prctl(PR_SET_NAME)``, writing
``/proc/self/comm``), and whoever launches it picks the exe basename (a
symlink, a copied interpreter, a shebang script, a compiled launcher). A
grant keyed only by that name is spoofable. This module binds a grant to
the realpath of ``/proc/<pid>/exe``, which the kernel sets at exec time:

* At grant time the app's executable realpath is recorded: a running
  process's exe, or the installed binary resolved through PATH or a
  ``.desktop`` ``Exec=`` line.
* At check time every live process behind the key must have that exe.
* A process whose exe is an interpreter (python3, perl, gjs, sh...) is
  keyed as that interpreter, so a shebang script named ``mousepad`` needs
  a python3 grant.
* A grant recorded before this change has no exe. It keeps working only
  when the live exe is the one PATH or a ``.desktop`` file resolves for
  the name, or the name's own binary in a root-owned system directory the
  user cannot write (how ``soffice.bin`` under /usr/lib/libreoffice is
  kept); otherwise it is refused with a re-grant hint.

Nothing here is cached: every answer reads ``/proc`` fresh.
"""

from __future__ import annotations

import glob
import os
import re
import shlex
import shutil
import stat

#: Interpreter executables. A process running one of these hosts code whose
#: every self-reported name is its own choice, so it is keyed as the
#: interpreter itself.
_INTERPRETER_RE = re.compile(
    r"^(python|pypy|perl|ruby|node|nodejs|gjs|lua|luajit|tclsh|wish|php|sh|bash|dash|zsh|ksh|fish)"
    r"([0-9][0-9.]*)?(-dbg)?$"
)

_DESKTOP_DIRS = (
    "~/.local/share/applications",
    "/usr/local/share/applications",
    "/usr/share/applications",
    "/var/lib/flatpak/exports/share/applications",
    "/var/lib/snapd/desktop/applications",
)


def exe_of(pid: int) -> str | None:
    """Realpath of ``/proc/<pid>/exe``; None when unreadable or gone."""
    try:
        target = os.readlink(f"/proc/{int(pid)}/exe")
    except (OSError, ValueError):
        return None
    if target.endswith(" (deleted)"):
        return None
    return os.path.realpath(target)


def interpreter_key(exe: str | None) -> str | None:
    """``python3`` for ``/usr/bin/python3.13``; None for a non-interpreter exe."""
    if not exe:
        return None
    match = _INTERPRETER_RE.match(os.path.basename(exe))
    if not match:
        return None
    family, version = match.group(1), match.group(2) or ""
    major = version.split(".", 1)[0]
    return family + major


def _comm(pid: int) -> str:
    try:
        with open(f"/proc/{int(pid)}/comm", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def pids_named(name: str) -> set[int]:
    """Live pids whose comm or exe basename is ``name`` (or its 15-byte cut).

    Includes processes with no window yet, so a pre-window spoof is seen.
    """
    wanted = (name or "").strip()
    if not wanted:
        return set()
    cut = wanted[:15]
    found: set[int] = set()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if _comm(pid) == cut:
            found.add(pid)
            continue
        exe = exe_of(pid)
        if exe and os.path.basename(exe) == wanted:
            found.add(pid)
    return found


def _desktop_exec_paths(name: str) -> list[str]:
    """Realpaths of the ``Exec=`` binaries of .desktop files that name ``name``."""
    wanted = name.casefold()
    paths: list[str] = []
    for directory in _DESKTOP_DIRS:
        for path in glob.glob(os.path.join(os.path.expanduser(directory), "*.desktop")):
            stem = os.path.basename(path)[: -len(".desktop")].casefold()
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            line = next((ln[5:] for ln in text.splitlines() if ln.startswith("Exec=")), None)
            if not line:
                continue
            try:
                argv = shlex.split(line)
            except ValueError:
                continue
            if not argv:
                continue
            first = argv[0]
            if os.path.basename(first).casefold() != wanted and stem.rsplit(".", 1)[-1] != wanted:
                continue
            resolved = first if os.path.isabs(first) else shutil.which(first)
            if resolved:
                paths.append(os.path.realpath(resolved))
    return paths


def installed_exes(name: str) -> set[str]:
    """Realpaths PATH or a .desktop ``Exec=`` resolve for ``name``."""
    out: set[str] = set()
    found = shutil.which(name)
    if found:
        out.add(os.path.realpath(found))
    out.update(_desktop_exec_paths(name))
    return out


def _system_owned(path: str) -> bool:
    """True when ``path`` and every parent are root-owned and not writable by others.

    The user cannot plant or replace such a file, so it is an installed
    binary rather than a copy or launcher the user made.
    """
    current = path
    while True:
        try:
            st = os.lstat(current)
        except OSError:
            return False
        if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return False
        parent = os.path.dirname(current)
        if parent == current:
            return True
        current = parent


def legacy_exe_ok(name: str, exe: str) -> bool:
    """Whether a name-only (pre-binding) grant for ``name`` covers ``exe``."""
    if exe in installed_exes(name):
        return True
    return os.path.basename(exe) == name and _system_owned(exe)


def grant_exe(name: str) -> str | None:
    """The executable realpath to record for a new grant on ``name``.

    A running non-interpreter process's exe wins (when every such process
    agrees and it is the installed binary or system-owned); else the
    binary PATH or a .desktop file resolves. None when nothing is found or
    ``name`` is an interpreter (interpreter grants are keyed by name).
    """
    if interpreter_key(name):
        return None
    exes = {exe_of(pid) for pid in pids_named(name)}
    exes.discard(None)
    exes = {e for e in exes if not interpreter_key(e)}
    if len(exes) == 1:
        (only,) = exes
        if legacy_exe_ok(name, only):
            return only
    installed = installed_exes(name)
    if len(installed) == 1:
        return next(iter(installed))
    return None


def check(name: str, recorded: str | None) -> tuple[str, str | None]:
    """(key, refusal hint) for the live processes behind ``name``.

    ``key`` is ``name`` when every live process's exe is the bound one,
    the interpreter when they all run an interpreter, and ``name`` with a
    hint when the exe does not match (the caller refuses). With no live
    process the name is returned unchanged; a later check sees the process.
    """
    pids = pids_named(name)
    if not pids or interpreter_key(name):
        return name, None
    exes = {pid: exe_of(pid) for pid in pids}
    hosts = {interpreter_key(e) for e in exes.values() if interpreter_key(e)}
    natives = {e for e in exes.values() if e and not interpreter_key(e)}
    if hosts and not natives and len(hosts) == 1:
        return next(iter(hosts)), None
    bad: list[str] = []
    for exe in exes.values():
        if exe is None or interpreter_key(exe):
            bad.append(exe or "(unreadable)")
        elif recorded is not None:
            if exe != recorded:
                bad.append(exe)
        elif not legacy_exe_ok(name, exe):
            bad.append(exe)
    if not bad:
        return name, None
    if recorded is not None:
        hint = (f"a process named {name!r} runs {bad[0]}, not the granted executable {recorded}; "
                f"refused. Re-grant {name} only if you trust that executable.")
    else:
        hint = (f"a process named {name!r} runs {bad[0]}, which is not the installed {name}; "
                f"refused. This grant predates executable binding: re-run "
                f"`a11y-computer-use grant {name} <tier>` while the real app runs.")
    return name, hint
