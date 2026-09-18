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


# --- key chords (virtual-key codes) ----------------------------------------

_VK_MODS = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12,
            "win": 0x5B, "cmd": 0x5B, "meta": 0x5B}
_VK_KEYS: dict[str, int] = {c: 0x41 + i for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")}
_VK_KEYS.update({str(d): 0x30 + d for d in range(10)})
_VK_KEYS.update({
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "space": 0x20, "backspace": 0x08,
    "delete": 0x2E, "escape": 0x1B, "esc": 0x1B, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28, "home": 0x24, "end": 0x23, "pageup": 0x21,
    "pagedown": 0x22,
})
_VK_KEYS.update({f"f{i}": 0x6F + i for i in range(1, 13)})  # F1=0x70 .. F12=0x7B


def _vk_event(vk: int, up: bool) -> "_INPUT":
    ki = _KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, None)
    return _INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=ki))


def press_chord(chord: str) -> None:
    """Press a chord like 'ctrl+a' / 'ctrl+shift+t' via VK codes (SendInput)."""
    parts = [p.strip().lower() for p in chord.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty chord {chord!r}")
    *mods, key = parts
    mvks = []
    for m in mods:
        if m not in _VK_MODS:
            raise ValueError(f"unknown modifier {m!r} in {chord!r}")
        mvks.append(_VK_MODS[m])
    if key in _VK_MODS:
        raise ValueError(f"chord {chord!r} has no non-modifier key")
    if key not in _VK_KEYS:
        raise ValueError(f"unknown key {key!r} in {chord!r}")
    kvk = _VK_KEYS[key]
    events = (
        [_vk_event(vk, False) for vk in mvks]
        + [_vk_event(kvk, False), _vk_event(kvk, True)]
        + [_vk_event(vk, True) for vk in reversed(mvks)]
    )
    n = len(events)
    arr = (_INPUT * n)(*events)
    ctypes.windll.user32.SendInput(n, arr, ctypes.sizeof(_INPUT))
