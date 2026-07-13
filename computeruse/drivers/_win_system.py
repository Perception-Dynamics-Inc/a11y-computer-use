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


def resolve_app(identifier: str) -> str:
    """Resolve a window title / class / exe substring to the owning process image
    name (the permission-keying id), or the identifier itself if unmatched."""
    import uiautomation as auto

    needle = (identifier or "").lower()
    for w in auto.GetRootControl().GetChildren():
        name = (getattr(w, "Name", "") or "").lower()
        cls = (getattr(w, "ClassName", "") or "").lower()
        exe = (_exe_for_pid(getattr(w, "ProcessId", 0)) or "").lower()
        if needle and (needle in name or needle in cls or needle in exe):
            return exe or identifier
    return identifier
