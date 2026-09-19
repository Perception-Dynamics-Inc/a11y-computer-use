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

import math
from collections.abc import Sequence

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
    """Queue one XTEST event; callers flush a keystroke, chord, or click.

    Server request order does not imply application input-method completion.
    Text therefore paces whole keystrokes instead of flooding the client.
    """
    from Xlib.ext import xtest

    xtest.fake_input(_disp(), event_type, detail)


def _flush():
    """Wait for the X server, without waiting for applications to handle input."""
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
_KEYSYM_KEYS.update({f"f{i}": 0xFFBD + i for i in range(1, 25)})  # F1=0xFFBE .. F24=0xFFD5
# Named punctuation (so "ctrl+plus" / "ctrl+minus" work — "+" is the chord
# separator) and the remaining keyboard keys a user can press.
_KEYSYM_KEYS.update({
    "minus": 0x002D, "equal": 0x003D, "plus": 0x002B, "comma": 0x002C, "period": 0x002E,
    "slash": 0x002F, "backslash": 0x005C, "semicolon": 0x003B, "apostrophe": 0x0027,
    "quote": 0x0027, "grave": 0x0060, "backtick": 0x0060, "bracketleft": 0x005B,
    "bracketright": 0x005D, "less": 0x003C, "greater": 0x003E,
    "capslock": 0xFFE5, "numlock": 0xFF7F, "scrolllock": 0xFF14, "print": 0xFF61,
    "printscreen": 0xFF61, "pause": 0xFF13, "menu": 0xFF67,
})


_CONTROL_CHARS = {"\n": 0xFF0D, "\r": 0xFF0D, "\t": 0xFF09, "\b": 0xFF08, "\x1b": 0xFF1B}


def _char_keysym(ch: str) -> int:
    """X keysym for a character: the codepoint for Latin-1, else the X Unicode
    keysym convention (0x01000000 | codepoint). Newline/tab/backspace/escape map
    to their key keysyms so ``type_text("a\\nb")`` presses Return, not a
    non-existent control keysym."""
    if ch in _CONTROL_CHARS:
        return _CONTROL_CHARS[ch]
    cp = ord(ch)
    return cp if cp < 0x100 else (0x01000000 | cp)


def _keycode_and_shift(keysym: int) -> tuple[int | None, bool]:
    """(keycode, needs_shift) for ``keysym`` when it sits on the base or Shift
    level of some key in the live keymap, else (None, False). python-xlib's
    keysym_to_keycode also matches the AltGr/level-3 and second-group columns
    (de: ``keycode 24 = q Q q Q at Greek_OMEGA ...``); a plain or Shift-bracketed
    tap of such a keycode emits the base character ('q' for '@'), so those
    keysyms are reported as unmapped and callers route them through _TempKeymap."""
    d = _disp()
    keycode = d.keysym_to_keycode(keysym)
    if not keycode:
        return None, False
    if keysym == d.keycode_to_keysym(keycode, 0):
        return keycode, False
    if keysym == d.keycode_to_keysym(keycode, 1):
        return keycode, True
    return None, False  # only reachable via AltGr / another group


def _tap_keysym(keysym: int) -> bool:
    """Press+release the key for ``keysym`` (bracketing with Shift if the keysym
    is on the upper level). Returns False if the keysym is unmapped."""
    keycode, needs_shift = _keycode_and_shift(keysym)
    if keycode is None:
        return False
    _tap_keycode(keycode, needs_shift)
    return True


def _tap_keycode(keycode: int, needs_shift: bool) -> None:
    """Queue a prepared keycode with its required Shift state."""
    from Xlib import X

    shift_kc = _disp().keysym_to_keycode(_KEYSYM_MODS["shift"]) if needs_shift else 0
    if shift_kc:
        _fake(X.KeyPress, shift_kc)
    _fake(X.KeyPress, keycode)
    _fake(X.KeyRelease, keycode)
    if shift_kc:
        _fake(X.KeyRelease, shift_kc)


def _spare_keycodes(d) -> list[int]:
    """Keycodes with no keysyms bound in the current map (layouts leave gaps at
    the top of the range), lowest first; callers pop from the end."""
    info = d.display.info
    lo, hi = info.min_keycode, info.max_keycode
    mapping = d.get_keyboard_mapping(lo, hi - lo + 1)
    return [lo + i for i, syms in enumerate(mapping) if all(int(s) == 0 for s in syms)]


class _TempKeymap:
    """Spare keycodes temporarily bound to keysyms the current layout lacks
    (CJK, emoji, symbols, accented letters on a US layout), so they can be
    typed through XTEST like any other key. The same trick xdotool uses.

    Each distinct keysym gets its own keycode for the whole operation and the
    map is restored once at the end. Remap-press-restore per character does
    NOT work: toolkits translate keycodes lazily from a cached keymap, so a
    keycode rebound before the client processed the key comes out as whichever
    character it held at translation time (verified on a Budgie/Xorg desktop:
    'ü' arrived as the 'ï' typed two characters later)."""

    def __init__(self) -> None:
        self._d = _disp()
        self._bound: dict[int, int] = {}  # keysym -> keycode
        self._saved: dict[int, list[int]] = {}  # keycode -> original keysyms
        self._spare = _spare_keycodes(self._d)

    def bind_all(self, keysyms: list[int]) -> dict[int, int]:
        """Reserve the complete text before typing, or fail without input."""
        missing = list(dict.fromkeys(sym for sym in keysyms if sym not in self._bound))
        if len(missing) > len(self._spare):
            raise ValueError(
                f"text needs {len(missing)} unmapped characters but only "
                f"{len(self._spare)} spare keycodes are available; use set_value "
                "or focus an editable element through its accessibility ref"
            )
        for sym in missing:
            self.bind(sym)
        return {sym: self._bound[sym] for sym in keysyms}

    def bind(self, keysym: int) -> int | None:
        """Keycode currently producing ``keysym`` (binding a spare one on first
        use), or None when the map has no spare keycode left."""
        import time

        kc = self._bound.get(keysym)
        if kc is not None:
            return kc
        if not self._spare:
            return None
        kc = self._spare.pop()
        self._saved[kc] = list(self._d.get_keyboard_mapping(kc, 1)[0])
        _flush()  # anything queued under the old map goes out first
        self._d.change_keyboard_mapping(kc, [[keysym] * len(self._saved[kc])])
        self._d.sync()
        time.sleep(0.03)  # clients refresh their keymap on MappingNotify before the key arrives
        self._bound[keysym] = kc
        return kc

    def restore(self) -> None:
        import time

        if not self._saved:
            return
        _flush()
        time.sleep(0.05)  # let clients translate the last keys under the temporary map
        for kc, syms in self._saved.items():
            self._d.change_keyboard_mapping(kc, [syms])
        self._d.sync()
        self._saved.clear()
        self._bound.clear()


# --- text ------------------------------------------------------------------


def type_string(text: str) -> None:
    """Type with a stable keymap and paced keystrokes into the focused element.

    Bind the complete text before sending input: midstream MappingNotify events
    race client keymap refreshes. Pace mapped characters as well as Unicode;
    asynchronous input methods can otherwise commit Unicode before preceding
    ASCII that they requeue for the application. Prefer AT-SPI for bulk text.
    """
    if not text:
        return
    import time

    keysyms = [_char_keysym(ch) for ch in text]
    keys = {sym: _keycode_and_shift(sym) for sym in dict.fromkeys(keysyms)}
    missing = [sym for sym, (kc, _shift) in keys.items() if kc is None]
    pool = _TempKeymap() if missing else None
    try:
        if pool is not None:
            keys.update({sym: (kc, False) for sym, kc in pool.bind_all(missing).items()})
        for sym in keysyms:
            keycode, needs_shift = keys[sym]
            assert keycode is not None  # all characters were resolved before input
            _tap_keycode(keycode, needs_shift)
            _flush()
            time.sleep(0.012)  # let the client/input method dispatch this character
    finally:
        if pool is not None:
            pool.restore()


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
    if key in _KEYSYM_KEYS:
        return msyms, _KEYSYM_KEYS[key]
    if len(key) == 1 and key.isprintable():
        return msyms, _char_keysym(key)  # any single printable key: "ctrl+/", "ctrl+-", "alt+."
    raise ValueError(f"unknown key {key!r} in {chord!r}")


def validate_chord(chord: str) -> None:
    """Raise ValueError if ``chord`` is malformed (no events sent) — the dry-run
    / fail-fast path, mirroring macOS `act.parse_chord`."""
    _parse_chord(chord)


def press_chord(chord: str) -> None:
    """Press a chord like 'ctrl+a' / 'ctrl+shift+t' / 'escape' via XTEST keysyms."""
    from Xlib import X

    msyms, ksym = _parse_chord(chord)
    mod_kcs = [_disp().keysym_to_keycode(s) for s in msyms]
    key_kc, needs_shift = _keycode_and_shift(ksym)
    pool = None
    if key_kc is None:  # e.g. "ctrl+ü" on a US layout: bind it for this chord
        pool = _TempKeymap()
        key_kc = pool.bind(ksym)
        if key_kc is None:
            raise ValueError(f"key in {chord!r} is not on the current keymap and no spare keycode is free")
    if needs_shift and _KEYSYM_MODS["shift"] not in msyms:
        # The key lives on the shifted level (e.g. "ctrl+_" or "ctrl+:"): hold
        # Shift too, otherwise the base-level character is sent instead.
        mod_kcs.append(_disp().keysym_to_keycode(_KEYSYM_MODS["shift"]))
    for kc in mod_kcs:
        if kc:
            _fake(X.KeyPress, kc)
    _fake(X.KeyPress, key_kc)
    _fake(X.KeyRelease, key_kc)
    for kc in reversed(mod_kcs):
        if kc:
            _fake(X.KeyRelease, kc)
    _flush()
    if pool is not None:
        pool.restore()


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


def _move(x: int, y: int) -> None:
    """Queue an ABSOLUTE pointer move to screen (x, y) via XTEST MotionNotify
    (detail=0 = absolute). Not `Display.warp_pointer`: python-xlib's
    `Display.warp_pointer(x, y)` is a WarpPointer with no destination window,
    which X defines as a move RELATIVE to the current pointer position. That
    only coincides with absolute coordinates when the pointer sits at (0, 0),
    which is exactly the Xvfb state that hid this on CI; on a real desktop the
    click landed at pointer + (x, y). Verified on a Budgie/Xorg desktop
    (docs/box-testbed.md)."""
    from Xlib import X
    from Xlib.ext import xtest

    xtest.fake_input(_disp(), X.MotionNotify, 0, x=int(x), y=int(y))


def click(x: int, y: int, *, button: str = "left", count: int = 1) -> None:
    """Synthesize a mouse click at screen (x, y). Only used when the a11y press
    path is unavailable (coordinate/vision fallback)."""
    from Xlib import X

    _move(x, y)  # queued; X processes the motion before the buttons
    num = _BUTTON_NUM.get(button, 1)
    for _ in range(max(1, count)):
        _fake(X.ButtonPress, num)
        _fake(X.ButtonRelease, num)
    _flush()


DRAG_STEP_PX = 8  #: longest pointer jump inside a held-button stroke
DRAG_PACE_S = 0.004  #: pause between motion events so apps see a stroke, not a teleport


def drag(x1: int, y1: int, x2: int, y2: int, *, button: str = "left",
         path: Sequence[tuple[int, int]] = ()) -> None:
    """Press at (x1, y1), move through ``path`` to (x2, y2), release.

    The pointer is walked in DRAG_STEP_PX hops with a short pace between
    them. One press + one motion + one release is a valid X drag, but a
    freehand brush (Krita, GIMP) or a canvas that samples pointer velocity
    turns a single jump into a dot or a straight line; walking the stroke
    makes the waypoints an actual curve. Each hop is flushed so the pace is
    real time, not a queue the server drains at once."""
    import time

    from Xlib import X

    num = _BUTTON_NUM.get(button, 1)
    _move(x1, y1)
    _fake(X.ButtonPress, num)
    _flush()
    cx, cy = int(x1), int(y1)
    for px, py in [*path, (x2, y2)]:
        for hx, hy in _hops(cx, cy, int(px), int(py)):
            _move(hx, hy)  # motion while the button is held = the drag
            _flush()
            time.sleep(DRAG_PACE_S)
        cx, cy = int(px), int(py)
    _fake(X.ButtonRelease, num)
    _flush()


def _hops(x1: int, y1: int, x2: int, y2: int) -> list[tuple[int, int]]:
    """Points from (x1, y1) exclusive to (x2, y2) inclusive, at most
    DRAG_STEP_PX apart; the endpoint itself is always the last hop."""
    n = max(1, math.ceil(math.hypot(x2 - x1, y2 - y1) / DRAG_STEP_PX))
    return [(round(x1 + (x2 - x1) * i / n), round(y1 + (y2 - y1) * i / n)) for i in range(1, n + 1)]


def scroll(x: int, y: int, *, dx: int = 0, dy: int = 0) -> None:
    """Wheel scroll at (x, y): X buttons 4/5 = vertical, 6/7 = horizontal.
    One button tap per notch; positive dy scrolls content up (wheel down)."""
    from Xlib import X

    _move(x, y)
    for _ in range(abs(int(dy))):
        _fake(X.ButtonPress, 5 if dy > 0 else 4)
        _fake(X.ButtonRelease, 5 if dy > 0 else 4)
    for _ in range(abs(int(dx))):
        _fake(X.ButtonPress, 7 if dx > 0 else 6)
        _fake(X.ButtonRelease, 7 if dx > 0 else 6)
    _flush()
