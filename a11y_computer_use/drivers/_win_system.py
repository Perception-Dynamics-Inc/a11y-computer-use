"""Windows system/windowing probes (ctypes): foreground app, app-under-point,
and identifier→process resolution. Windows-only; used by server.py's
platform-dispatching helpers so the Runtime's gating works on Windows.

App identity on Windows is the process image name (e.g. "notepad.exe") — the
analog of a macOS bundle id for permission-keying.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _exe_for_pid(pid: int) -> str | None:
    if not pid:
        return None
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        k32.CloseHandle(handle)
    return None


def _pid_for_hwnd(hwnd) -> int:
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def frontmost_app_id() -> str:
    """Process image name of the foreground window's owner (e.g. "notepad.exe")."""
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return ""
    return _exe_for_pid(_pid_for_hwnd(hwnd)) or ""


def app_at_point_id(x: float, y: float) -> str | None:
    """Process image name of the window at a screen point (the act-time hit-test)."""
    user32 = ctypes.windll.user32
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    if not hwnd:
        return None
    return _exe_for_pid(_pid_for_hwnd(hwnd))


def exe_matches(identifier: str, exe: str) -> bool:
    """Whether ``identifier`` names the process image ``exe``: the basename,
    case-insensitive, with ".exe" optional. Never a substring: "notepad" is
    not "notepad++.exe", and a window title is not a program (#1191)."""
    want = os.path.basename((identifier or "").strip().replace("\\", "/")).lower()
    have = (exe or "").lower()
    if not want or not have:
        return False
    if want.endswith(".exe"):
        want = want[:-4]
    if have.endswith(".exe"):
        have = have[:-4]
    return want == have


def pick_window(identifier: str, rows):
    """From ``(window, pid, exe)`` rows, the one window whose exe is named by
    ``identifier``, or None. More than one process of that program is
    ambiguous: raises APP_NOT_FOUND with reason ``ambiguous_app`` rather than
    acting on whichever window happens to be listed first."""
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    hits = [(w, pid, exe) for w, pid, exe in rows if exe_matches(identifier, exe)]
    if not hits:
        return None
    pids = sorted({int(pid or 0) for _, pid, _ in hits})
    if len(pids) > 1:
        raise ComputerUseError(
            ErrorCode.APP_NOT_FOUND,
            f"{identifier!r} matches {len(pids)} running processes; name one by pid",
            {"reason": "ambiguous_app", "app": identifier,
             "candidates": [{"pid": p, "exe": next(e for _, q, e in hits if int(q or 0) == p)}
                            for p in pids]},
        )
    return hits[0][0]


def _top_level_rows():
    import uiautomation as auto

    rows = []
    for w in auto.GetRootControl().GetChildren():
        try:
            pid = int(getattr(w, "ProcessId", 0) or 0)
        except Exception:
            pid = 0
        rows.append((w, pid, _exe_for_pid(pid) or ""))
    return rows


def resolve_app(identifier: str) -> str:
    """Resolve an exe name (".exe" optional) to the owning process image name
    (the permission-keying id), or the identifier itself if unmatched. Window
    titles and classes no longer resolve: a browser tab titled "notepad tips"
    is not Notepad (#1191)."""
    for _w, _pid, exe in _top_level_rows():
        if exe_matches(identifier, exe):
            return exe
    return identifier
