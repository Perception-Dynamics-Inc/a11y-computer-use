"""Executable-path binding for Linux app grants.

A Linux process picks its own comm (``prctl(PR_SET_NAME)``, writing
``/proc/self/comm``), and whoever launches it picks the exe basename (a
symlink, a copied interpreter, a shebang script, a compiled launcher). A
grant keyed only by that name is spoofable. This module binds a grant to
the realpath of ``/proc/<pid>/exe``, which the kernel sets at exec time:

* At grant time the app's executable realpath is recorded: a running
  process's exe, or the installed binary resolved through PATH or a
  ``.desktop`` ``Exec=`` line.
* At check time every process that owns the target (an X11 window whose
  program is the name, or an AT-SPI application registered by a process
  of that name) must have that exe. A same-named process that owns no
  window or a11y app cannot be acted on, so it neither blocks nor grants
  (#1322); one owned by another user is refused.
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
    # Cheap test first; the .desktop scan runs only when it fails.
    if os.path.basename(exe) == name and _system_owned(exe):
        return True
    return exe in installed_exes(name)


def grant_exe(name: str) -> str | None:
    """The executable realpath to record for a new grant on ``name``.

    A running non-interpreter process's exe wins (when every such process
    agrees and it is the installed binary or system-owned); else the
    binary PATH or a .desktop file resolves. None when nothing is found or
    ``name`` is an interpreter (interpreter grants are keyed by name).
    """
    if interpreter_key(name):
        return None
    me = os.getuid()
    exes = {exe_of(pid) for pid in target_pids(name) if _uid(pid) == me}
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


def _status(pid: int) -> tuple[int | None, tuple[int, ...]]:
    """(PPid, NSpid) from ``/proc/<pid>/status`` (readable for non-dumpable processes).

    Both are kernel-maintained: a process cannot rewrite its parent or the
    pid namespaces it was created in.
    """
    ppid: int | None = None
    nspid: tuple[int, ...] = ()
    try:
        with open(f"/proc/{int(pid)}/status", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("PPid:"):
                    ppid = int(line.split()[1])
                elif line.startswith("NSpid:"):
                    nspid = tuple(int(x) for x in line.split()[1:])
    except (OSError, ValueError, IndexError):
        return None, ()
    return ppid, nspid


def _gone(pid: int) -> bool:
    """True for a zombie or dead process (State Z/X), or one that has exited.

    Its ``/proc/PID/exe`` is unreadable because the kernel already dropped
    its memory map, not because of anything it chose; it runs no code, owns
    no window and no AT-SPI connection. Chrome's zygote reaps exited
    renderers lazily, so they linger as zombies after every navigation.
    """
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    state = text[text.rfind(")") + 2: text.rfind(")") + 3]
    return state in ("Z", "X", "x")


def _ppid(pid: int) -> int | None:
    """Parent pid from ``/proc/<pid>/status``."""
    return _status(pid)[0]


def _verified_ancestor_exe(pid: int, exes: dict[int, str | None]) -> str | None:
    """Exe of the verified Chrome browser behind an unreadable sandbox helper.

    Chrome's sandboxed zygote/renderers can be non-dumpable, so their
    ``/proc/PID/exe`` may be unreadable. A name and a non-dumpable flag are
    both self-chosen, so being a non-dumpable descendant of Chrome proves
    nothing (a launch-flag, native-messaging or protocol-handler child of
    the browser can ``prctl(PR_SET_DUMPABLE, 0)`` and rename itself).

    What the child cannot choose is where the kernel created it: Chrome's
    namespace sandbox clones the zygote straight into a new pid namespace
    (``clone(CLONE_NEWPID)``) from the browser process itself, so the
    zygote is pid 1 of a namespace one level below the browser and its
    parent is the browser. Any process the browser fork+execs (every helper
    route) lands in the browser's own namespace; it can make a namespace
    of its own only for its *children*, whose namespace init then has that
    helper, not the browser, as parent. So an unreadable process passes
    only when:

    * every step up its PPid chain stays inside the named processes,
      either in the same pid namespace (same NSpid depth) or by leaving a
      namespace whose init it is (innermost NSpid 1, depth one less);
    * the first readable ancestor is reached by such a namespace exit,
      i.e. that ancestor itself created the sandbox namespace.

    The readable ancestor's exe is returned and then checked like any other
    (recorded path or system-owned). Anything else returns None (refused).
    """
    seen = {pid}
    cur = pid
    cur_ppid, cur_ns = _status(cur)
    if not cur_ns:
        return None
    while cur_ppid and cur_ppid > 1 and cur_ppid not in seen:
        parent = cur_ppid
        seen.add(parent)
        if parent not in exes:
            return None  # the chain leaves the named processes
        parent_ppid, parent_ns = _status(parent)
        if not parent_ns:
            return None
        if len(parent_ns) == len(cur_ns):
            crossed = False
        elif len(parent_ns) == len(cur_ns) - 1 and cur_ns[-1] == 1:
            crossed = True
        else:
            return None
        if exes[parent] is not None:
            return exes[parent] if crossed else None
        cur, cur_ppid, cur_ns = parent, parent_ppid, parent_ns
    return None


def _uid(pid: int) -> int | None:
    try:
        return os.stat(f"/proc/{int(pid)}").st_uid
    except OSError:
        return None


def _x11_owner_pids(name: str) -> set[int]:
    try:
        from a11y_computer_use.drivers import _linux_system

        return set(_linux_system.pids_matching(name))
    except Exception:  # noqa: BLE001 - no X11: the a11y bus still names owners
        return set()


def _atspi_owner_pids() -> set[int]:
    """Pids registered on the AT-SPI bus (from D-Bus credentials, not self-reported)."""
    try:
        from a11y_computer_use.drivers import _atspi

        return set(_atspi._registered_pids() or ())
    except Exception:  # noqa: BLE001 - no bus: X11 still names owners
        return set()


def target_pids(name: str, named: set[int] | None = None) -> set[int]:
    """Pids that own what an action on ``name`` reaches (#1322).

    The X11 windows whose program is ``name`` and the AT-SPI applications
    registered by a process named ``name``. A process with neither is not a
    target, whatever it is called.
    """
    named = pids_named(name) if named is None else named
    owners = _x11_owner_pids(name)
    if named - owners:
        owners |= _atspi_owner_pids() & named
    return owners


def check(name: str, recorded: str | None) -> tuple[str, str | None]:
    """(key, refusal hint) for the live processes behind ``name``.

    ``key`` is ``name`` when every live process's exe is the bound one,
    the interpreter when they all run an interpreter, and ``name`` with a
    hint when the exe does not match (the caller refuses). With no live
    process the name is returned unchanged; a later check sees the process.
    """
    if interpreter_key(name):
        return name, None
    named = pids_named(name)
    if not named:
        return name, None
    targets = target_pids(name, named)
    if not targets:
        return name, None  # nothing named this owns a window or a11y app (#1322)
    me = os.getuid()
    foreign = sorted(pid for pid in targets if _uid(pid) not in (me, None))
    if foreign:
        pid = foreign[0]
        return name, (f"a window or a11y app named {name!r} belongs to pid {pid} of another "
                      f"user (uid {_uid(pid)}); refused.")
    # Ancestors for the Chrome sandbox rule come from this user's named processes.
    context = {pid: exe_of(pid) for pid in named | targets if _uid(pid) in (me, None)}
    raw = {pid: context.get(pid) for pid in targets}
    # A zombie's exe is unreadable because it has exited; it is not a live app.
    live = {pid: exe for pid, exe in raw.items() if exe is not None or not _gone(pid)}
    if not live:
        return name, None
    exes = {pid: (exe if exe is not None else _verified_ancestor_exe(pid, context))
            for pid, exe in live.items()}
    hosts = {interpreter_key(e) for e in exes.values() if interpreter_key(e)}
    natives = {e for e in exes.values() if e and not interpreter_key(e)}
    if hosts and not natives and len(hosts) == 1:
        return next(iter(hosts)), None
    bad: list[str] = []
    why: dict[str, str] = {}
    # One verdict per distinct exe: Chrome runs dozens of processes.
    for exe in set(exes.values()):
        if exe is None or interpreter_key(exe):
            bad.append(exe or "(unreadable)")
            why[exe or "(unreadable)"] = "unreadable exe, not a sandbox helper" if exe is None else "interpreter"
        elif recorded is not None:
            if exe != recorded:
                bad.append(exe)
                why[exe] = "not the recorded exe"
        elif not legacy_exe_ok(name, exe):
            bad.append(exe)
            why[exe] = "legacy grant: not installed or system-owned"
    if not bad:
        return name, None
    if recorded is not None:
        hint = (f"a process named {name!r} runs {bad[0]}, not the granted executable {recorded}; "
                f"refused. Re-grant {name} only if you trust that executable.")
    else:
        hint = (f"a process named {name!r} runs {bad[0]}, which is not the installed {name}; "
                f"refused. This grant predates executable binding: re-run "
                f"`a11y-computer-use grant {name} <tier>` while the real app runs.")
    return name, hint + " [" + _diagnose(bad[0], why.get(bad[0], ""), exes, raw) + "]"


def _diagnose(bad: str, why: str, exes: dict[int, str | None], raw: dict[int, str | None]) -> str:
    """Failure-only detail: which pid failed which check (pids, states, chains)."""
    parts = [f"check: {why}"]
    shown = 0
    for pid in sorted(p for p, e in exes.items() if (e or "(unreadable)") == bad):
        if shown >= 3:
            break
        shown += 1
        chain = []
        cur: int | None = pid
        for _ in range(6):
            if not cur or cur <= 1:
                break
            ppid, nspid = _status(cur)
            try:
                with open(f"/proc/{cur}/stat", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                state = text[text.rfind(")") + 2: text.rfind(")") + 3]
            except OSError as exc:
                state = f"?{exc.errno}"
            try:
                uid = os.stat(f"/proc/{cur}").st_uid
            except OSError:
                uid = -1
            try:
                link = os.readlink(f"/proc/{cur}/exe")
            except OSError as exc:
                link = f"errno {exc.errno}"
            chain.append(f"{cur}({_comm(cur)!r} {state} uid={uid} ns={list(nspid)} "
                         f"exe={link!r} named={cur in raw})")
            cur = ppid
        parts.append("pid " + " <- ".join(chain))
    return "; ".join(parts)
