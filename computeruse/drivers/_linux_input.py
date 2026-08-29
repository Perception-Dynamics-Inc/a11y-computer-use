"""Linux synthesized input via XTEST (python-xlib). Linux-only.

XTEST is the X11 field standard apps accept as genuine input (the research's
"read via a11y, write via XTEST"). We use it — not AT-SPI's
`generate_keyboard_event`/`generate_mouse_event`, which are deprecated and
unreliable (no-op/hang) in modern at-spi2 (2.5x). python-xlib is pure Python
(no build deps) and the XTEST extension is served by every X server including
Xvfb.

`type_string` types text by mapping each character to an X keysym → keycode
(with automatic Shift for the upper level), so it is layout-aware via the live
keymap. `press_chord` maps a chord onto keysyms the same way. The a11y-first
element activation (press/focus without moving the pointer) lives in the driver
via AT-SPI actions — this module is only the coordinate/synthetic fallback plus
the focused-element typing path.
"""

from __future__ import annotations

from contextlib import contextmanager

_display = None


def _disp():
    """A cached Xlib display connection (reopened if it went away)."""
    global _display
    if _display is None:
        from Xlib import display as _xd

        _display = _xd.Display()
    return _display


def _fake(event_type, detail):
    """Queue one XTEST event WITHOUT flushing — callers flush once at the end of
    a logical operation (a whole string, chord, or click). X processes queued
    requests in order, so batching a keystroke's press+release (or a click's
    warp+press+release) into one round-trip is both correct and ~2-4x fewer
    blocking syncs than flushing per event."""
    from Xlib.ext import xtest

    xtest.fake_input(_disp(), event_type, detail)


def _flush():
    """Send all queued events and wait for the server to process them (one
    round-trip) — the single sync point at the end of an input operation."""
    _disp().sync()


# --- keysym helpers --------------------------------------------------------

# X11 keysym codes (keysymdef.h) for named non-printable keys and modifiers.
_KEYSYM_MODS = {
    "ctrl": 0xFFE3, "control": 0xFFE3,
    "shift": 0xFFE1,
    "alt": 0xFFE9, "option": 0xFFE9,
    "super": 0xFFEB, "win": 0xFFEB, "cmd": 0xFFEB, "meta": 0xFFEB,
}
_KEYSYM_KEYS: dict[str, int] = {c: ord(c) for c in "abcdefghijklmnopqrstuvwxyz"}
_KEYSYM_KEYS.update({d: ord(d) for d in "0123456789"})
_KEYSYM_KEYS.update({
    "enter": 0xFF0D, "return": 0xFF0D, "tab": 0xFF09, "space": 0x0020,
    "backspace": 0xFF08, "delete": 0xFFFF, "escape": 0xFF1B, "esc": 0xFF1B,
    "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
    "home": 0xFF50, "end": 0xFF57, "pageup": 0xFF55, "pagedown": 0xFF56,
    "insert": 0xFF63,
})
_KEYSYM_KEYS.update({f"f{i}": 0xFFBD + i for i in range(1, 13)})  # F1=0xFFBE .. F12=0xFFC9


def _char_keysym(ch: str) -> int:
    """X keysym for a character: the codepoint for Latin-1, else the X Unicode
    keysym convention (0x01000000 | codepoint)."""
    cp = ord(ch)
    return cp if cp < 0x100 else (0x01000000 | cp)


def _keycode_and_shift(keysym: int):
    """(keycode, needs_shift) for ``keysym`` from the live keymap, or (None, False)
    when the keysym is not mapped to any key."""
    from Xlib import X

    d = _disp()
    keycode = d.keysym_to_keycode(keysym)
    if not keycode:
        return None, False
    lvl0 = d.keycode_to_keysym(keycode, 0)
    lvl1 = d.keycode_to_keysym(keycode, 1)
    needs_shift = keysym == lvl1 and keysym != lvl0
    return keycode, needs_shift


def _tap_keysym(keysym: int) -> bool:
    """Press+release the key for ``keysym`` (bracketing with Shift if the keysym
    is on the upper level). Returns False if the keysym is unmapped."""
    from Xlib import X

    keycode, needs_shift = _keycode_and_shift(keysym)
    if keycode is None:
        return False
    shift_kc = _disp().keysym_to_keycode(_KEYSYM_MODS["shift"]) if needs_shift else 0
    if shift_kc:
        _fake(X.KeyPress, shift_kc)
    _fake(X.KeyPress, keycode)
    _fake(X.KeyRelease, keycode)
    if shift_kc:
        _fake(X.KeyRelease, shift_kc)
    return True


# --- text ------------------------------------------------------------------


def type_string(text: str) -> None:
    """Type ``text`` into the focused element via XTEST, one keysym per char.
    All events are queued and flushed once (one round-trip for the whole string,
    not two per character)."""
    if not text:
        return
    for ch in text:
        _tap_keysym(_char_keysym(ch))
    _flush()


# --- key chords ------------------------------------------------------------


def _parse_chord(chord: str) -> tuple[list[int], int]:
    """(modifier keysyms, key keysym) for a chord like 'ctrl+shift+t'. Raises
    ValueError on an empty/unknown chord (validated before any event is sent)."""
    parts = [p.strip().lower() for p in chord.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty chord {chord!r}")
    *mods, key = parts
    msyms = []
    for m in mods:
        if m not in _KEYSYM_MODS:
            raise ValueError(f"unknown modifier {m!r} in {chord!r}")
        msyms.append(_KEYSYM_MODS[m])
    if key in _KEYSYM_MODS:
        raise ValueError(f"chord {chord!r} has no non-modifier key")
    if key not in _KEYSYM_KEYS:
        raise ValueError(f"unknown key {key!r} in {chord!r}")
    return msyms, _KEYSYM_KEYS[key]


def validate_chord(chord: str) -> None:
    """Raise ValueError if ``chord`` is malformed (no events sent) — the dry-run
    / fail-fast path, mirroring macOS `act.parse_chord`."""
    _parse_chord(chord)


def press_chord(chord: str) -> None:
    """Press a chord like 'ctrl+a' / 'ctrl+shift+t' / 'escape' via XTEST keysyms."""
    from Xlib import X

    msyms, ksym = _parse_chord(chord)
    mod_kcs = [_disp().keysym_to_keycode(s) for s in msyms]
    key_kc, _ = _keycode_and_shift(ksym)
    if key_kc is None:
        raise ValueError(f"key in {chord!r} is not on the current keymap")
    for kc in mod_kcs:
        if kc:
            _fake(X.KeyPress, kc)
    _fake(X.KeyPress, key_kc)
    _fake(X.KeyRelease, key_kc)
    for kc in reversed(mod_kcs):
        if kc:
            _fake(X.KeyRelease, kc)
    _flush()


@contextmanager
def held(modifiers):
    """Hold ``modifiers`` (e.g. ('shift',)) pressed for the block — modifier
    clicks. Unknown modifiers are ignored."""
    from Xlib import X

    kcs = [_disp().keysym_to_keycode(_KEYSYM_MODS[m]) for m in (modifiers or ())
           if m in _KEYSYM_MODS]
    kcs = [kc for kc in kcs if kc]
    for kc in kcs:
        _fake(X.KeyPress, kc)
    _flush()  # modifiers down before the wrapped action runs
    try:
        yield
    finally:
        for kc in reversed(kcs):
            _fake(X.KeyRelease, kc)
        _flush()


# --- mouse (coordinate fallback for the vision path) -----------------------

_BUTTON_NUM = {"left": 1, "middle": 2, "right": 3}


def click(x: int, y: int, *, button: str = "left", count: int = 1) -> None:
    """Synthesize a mouse click at screen (x, y). Only used when the a11y press
    path is unavailable (coordinate/vision fallback)."""
    from Xlib import X

    d = _disp()
    d.warp_pointer(int(x), int(y))  # queued; X processes warp before the buttons
    num = _BUTTON_NUM.get(button, 1)
    for _ in range(max(1, count)):
        _fake(X.ButtonPress, num)
        _fake(X.ButtonRelease, num)
    _flush()


def drag(x1: int, y1: int, x2: int, y2: int, *, button: str = "left") -> None:
    from Xlib import X

    d = _disp()
    num = _BUTTON_NUM.get(button, 1)
    d.warp_pointer(int(x1), int(y1))
    _fake(X.ButtonPress, num)
    d.warp_pointer(int(x2), int(y2))  # motion while the button is held = the drag
    _fake(X.ButtonRelease, num)
    _flush()


def scroll(x: int, y: int, *, dx: int = 0, dy: int = 0) -> None:
    """Wheel scroll at (x, y): X buttons 4/5 = vertical, 6/7 = horizontal.
    One button tap per notch; positive dy scrolls content up (wheel down)."""
    from Xlib import X

    d = _disp()
    d.warp_pointer(int(x), int(y))
    for _ in range(abs(int(dy))):
        _fake(X.ButtonPress, 5 if dy > 0 else 4)
        _fake(X.ButtonRelease, 5 if dy > 0 else 4)
    for _ in range(abs(int(dx))):
        _fake(X.ButtonPress, 7 if dx > 0 else 6)
        _fake(X.ButtonRelease, 7 if dx > 0 else 6)
    _flush()
