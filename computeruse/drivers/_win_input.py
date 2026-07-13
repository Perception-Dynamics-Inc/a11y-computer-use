"""Windows synthesized input via `SendInput` (ctypes). Windows-only.

`type_unicode` is the layout-free Unicode path (the SendInput equivalent of
macOS's `CGEventKeyboardSetUnicodeString`): it injects each UTF-16 code unit
with `KEYEVENTF_UNICODE`, so it works regardless of keyboard layout and handles
astral characters via surrogate pairs.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class _INPUTUNION(ctypes.Union):
    # union sized to the largest member so ctypes.sizeof(_INPUT) == real cbSize
    _fields_ = (("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT))


class _INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


def type_unicode(text: str) -> None:
    """Inject ``text`` as Unicode key events to the focused window."""
    if not text:
        return
    events: list[_INPUT] = []
    for code_unit in memoryview(text.encode("utf-16-le")).cast("H"):
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            ki = _KEYBDINPUT(0, code_unit, flags, 0, None)
            events.append(_INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=ki)))
    n = len(events)
    arr = (_INPUT * n)(*events)
    ctypes.windll.user32.SendInput(n, arr, ctypes.sizeof(_INPUT))
