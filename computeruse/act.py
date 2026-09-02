"""Actions: synthesized input (CGEvent) against elements or raw points.

Every public action:

* takes an optional ``pre_check`` callback — the safety layer's hook. It
  receives the fully-built `schema.Action` *before* anything else happens and
  aborts the action by raising.
* refuses to post events when the Accessibility TCC grant is missing
  (``AXIsProcessTrusted()`` false), raising
  `ErrorCode.PERMISSION_DENIED_ACCESSIBILITY` instead.
* supports ``dry_run=True``: every CGEvent is built exactly as it would be
  posted, but nothing is posted and no delay is slept. Unit tests inspect the
  returned `BuiltEvent` list with the Quartz getters.

`Element` targets are re-resolved through a module-level resolver slot
(`set_resolver`) that ``observe.resolve_ref`` fills at integration; until
then the default resolver trusts the snapshot's recorded bounds. Likewise
`wait_for` polls a checker slot (`set_wait_checker`) that observe supplies.
Acting on a secure/password element raises `ErrorCode.SECURE_FIELD`.

Coordinates: schema points are display-qualified *physical* pixels;
CGEvents live in the global desktop space measured in *points*. The
conversion (`_point_to_global`) divides by the display's backing scale and
offsets by its global origin.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import math
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeAlias

if sys.platform == "darwin":
    import Quartz
    from AppKit import NSPasteboard, NSPasteboardItem, NSPasteboardTypeString
else:  # pragma: no cover - the CGEvent executor is macOS-only; the pure helpers
    # (chord parsing, event building types) must still import on Linux/Windows
    # so the shared test modules and `server.py`'s lazy imports collect there.
    Quartz = None  # type: ignore[assignment]
    NSPasteboard = NSPasteboardItem = NSPasteboardTypeString = None  # type: ignore[assignment]

from computeruse.schema import (
    MODIFIER_KEYS,
    Action,
    Click,
    ComputerUseError,
    Drag,
    Element,
    ErrorCode,
    KeyChord,
    MouseButton,
    Point,
    Scroll,
    ScrollUnit,
    Target,
    TypeText,
    WaitCondition,
    WaitFor,
)

__all__ = [
    "BuiltEvent",
    "Pasteboard",
    "click",
    "drag",
    "key_chord",
    "parse_chord",
    "scroll",
    "set_resolver",
    "set_wait_checker",
    "type_text",
    "wait_for",
]

# --- Tunables (module-level so tests can shrink delays/intervals) ----------

#: Text longer than this many characters goes through the clipboard-paste
#: fast path instead of per-chunk Unicode events.
CLIPBOARD_PATH_THRESHOLD = 50
#: Max UTF-16 code units per CGEventKeyboardSetUnicodeString chunk.
UNICODE_CHUNK_UTF16_UNITS = 20
#: Sleep between posted events (live mode only).
EVENT_DELAY_S = 0.01
#: Delay between posting cmd+v and restoring the saved pasteboard, giving the
#: frontmost app time to service the paste before the text disappears.
PASTE_RESTORE_DELAY_S = 0.2
#: Approximate distance (global points) between interpolated drag moves.
DRAG_STEP_PT = 40.0
#: `wait_for` polling interval.
WAIT_POLL_INTERVAL_S = 0.1

# --- Callback / injection types --------------------------------------------

#: Safety-layer hook: receives the built Action, raises to veto it.
PreCheck: TypeAlias = Callable[[Action], None]
#: Re-resolves a snapshot Element against the live tree (raises STALE_REF).
Resolver: TypeAlias = Callable[[Element], Element]
#: Returns the re-resolved Element once ``condition`` holds, else None.
WaitChecker: TypeAlias = Callable[[Element, WaitCondition], Element | None]


@dataclass(frozen=True, slots=True)
class BuiltEvent:
    """One synthesized CGEvent plus what it is for.

    Attributes:
        kind: One of ``mouse_move``, ``mouse_down``, ``mouse_up``,
            ``mouse_drag``, ``scroll``, ``key_down``, ``key_up``,
            ``unicode_down``, ``unicode_up``.
        event: The underlying ``CGEventRef``; inspect it with the Quartz
            getters (``CGEventGetLocation``, ``CGEventGetFlags``, ...).
    """

    kind: str
    event: object


class Pasteboard(Protocol):
    """Minimal clipboard interface so tests can inject a fake.

    ``save`` returns an opaque snapshot that ``restore`` accepts back;
    ``change_count`` mirrors ``NSPasteboard.changeCount`` (any write by
    anyone increments it) so callers can detect concurrent writers.
    """

    def save(self) -> object: ...

    def write_text(self, text: str) -> None: ...

    def restore(self, saved: object) -> None: ...

    def change_count(self) -> int: ...


#: Pasteboard flavor from the nspasteboard.org convention: well-behaved
#: clipboard managers skip items carrying it, so the transient paste text is
#: not recorded into clipboard history.
_CONCEALED_TYPE = "org.nspasteboard.ConcealedType"


class _SystemPasteboard:
    """The real ``NSPasteboard.generalPasteboard()``.

    ``save``/``restore`` snapshot every item with every flavor (RTF, images,
    file URLs — not just plain text), so the paste fast path never destroys
    a non-string clipboard.
    """

    def save(self) -> list[dict[str, bytes]]:
        items: list[dict[str, bytes]] = []
        for item in NSPasteboard.generalPasteboard().pasteboardItems() or ():
            flavors: dict[str, bytes] = {}
            for uti in item.types() or ():
                data = item.dataForType_(uti)
                if data is not None:
                    flavors[str(uti)] = bytes(data)
            items.append(flavors)
        return items

    def write_text(self, text: str) -> None:
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)
        pb.setString_forType_("", _CONCEALED_TYPE)

    def restore(self, saved: list[dict[str, bytes]]) -> None:
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        items = []
        for flavors in saved:
            item = NSPasteboardItem.alloc().init()
            for uti, data in flavors.items():
                item.setData_forType_(data, uti)  # pyobjc bridges bytes->NSData
            items.append(item)
        if items:
            pb.writeObjects_(items)

    def change_count(self) -> int:
        return int(NSPasteboard.generalPasteboard().changeCount())


class _MemoryPasteboard:
    """In-memory stand-in used by ``dry_run`` so it never touches the system
    clipboard while still exercising the save/restore code path."""

    def __init__(self) -> None:
        self._content: str | None = None
        self._changes = 0

    def save(self) -> str | None:
        return self._content

    def write_text(self, text: str) -> None:
        self._content = text
        self._changes += 1

    def restore(self, saved: str | None) -> None:
        self._content = saved
        self._changes += 1

    def change_count(self) -> int:
        return self._changes


# --- Permission & secure-input probes (ctypes: no pyobjc side effects) -----


def _ax_trusted() -> bool:
    """Whether this process holds the Accessibility TCC grant."""
    try:
        path = ctypes.util.find_library("ApplicationServices")
        if not path:
            return False
        lib = ctypes.cdll.LoadLibrary(path)
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except (OSError, AttributeError):
        return False


def _require_ax() -> None:
    """Gate every live event post on the Accessibility grant."""
    if not _ax_trusted():
        raise ComputerUseError(
            ErrorCode.PERMISSION_DENIED_ACCESSIBILITY,
            "Accessibility permission missing: this process is not trusted to "
            "synthesize input. Grant it under System Settings > Privacy & "
            "Security > Accessibility (see `computeruse doctor`).",
            detail={"api": "AXIsProcessTrusted"},
        )


def _secure_input_enabled() -> bool:
    """Whether some app (a password field) holds secure event input."""
    try:
        path = ctypes.util.find_library("Carbon")
        if not path:
            return False
        lib = ctypes.cdll.LoadLibrary(path)
        lib.IsSecureEventInputEnabled.restype = ctypes.c_bool
        return bool(lib.IsSecureEventInputEnabled())
    except (OSError, AttributeError):
        return False


def _focused_element_secure() -> bool:
    """Whether the AX-focused element is a secure text field (best effort).

    Chrome/Electron web password fields often do NOT enable secure event
    input, so `_secure_input_enabled` alone misses them; this reads the
    focused element's role/subrole through AX. Degrades to False when there
    is no signal (no AX grant, no focused element)."""
    try:
        import ApplicationServices as ax
    except ImportError:  # non-macOS / pyobjc missing
        return False
    err, focused = ax.AXUIElementCopyAttributeValue(
        ax.AXUIElementCreateSystemWide(), "AXFocusedUIElement", None
    )
    if err != 0 or focused is None:
        return False
    for attr in ("AXRole", "AXSubrole"):
        err, value = ax.AXUIElementCopyAttributeValue(focused, attr, None)
        if err == 0 and value == "AXSecureTextField":
            return True
    return False


# --- Target resolution & coordinate mapping --------------------------------


def _default_resolver(element: Element) -> Element:
    """MVP resolver: trust the snapshot's recorded state verbatim.

    ``observe.resolve_ref`` replaces this at integration (via `set_resolver`)
    with real anchor-based re-resolution that raises `ErrorCode.STALE_REF`.
    """
    return element


_resolver: Resolver = _default_resolver
_wait_checker: WaitChecker | None = None


def set_resolver(resolver: Resolver | None) -> None:
    """Install the live-tree re-resolver (``None`` restores the default)."""
    global _resolver
    _resolver = resolver if resolver is not None else _default_resolver


def set_wait_checker(checker: WaitChecker | None) -> None:
    """Install the `wait_for` condition checker (``None`` clears it)."""
    global _wait_checker
    _wait_checker = checker


def _resolve_point(target: Target) -> Point:
    """Turn a target into the display-qualified physical pixel to act on.

    Raises:
        ComputerUseError: `ErrorCode.STALE_REF` from the installed resolver;
            `ErrorCode.SECURE_FIELD` for secure/password elements.
    """
    if isinstance(target, Point):
        return target
    element = _resolver(target)
    if element.secure:
        raise ComputerUseError(
            ErrorCode.SECURE_FIELD,
            f"refusing to act on secure field {element.ref!r} ({element.title!r}); "
            "secure fields require human handoff",
            detail={"ref": element.ref, "role": element.role},
        )
    return element.bounds.center


def _point_to_global(point: Point) -> tuple[float, float]:
    """Map display-local physical pixels to global CGEvent point coordinates."""
    mode = Quartz.CGDisplayCopyDisplayMode(point.display_id)
    if mode is None:
        raise ValueError(f"unknown display_id {point.display_id}")
    bounds = Quartz.CGDisplayBounds(point.display_id)  # global, in points
    if not bounds.size.width:
        raise ValueError(f"display {point.display_id} has empty bounds")
    scale = Quartz.CGDisplayModeGetPixelWidth(mode) / bounds.size.width
    return (bounds.origin.x + point.x / scale, bounds.origin.y + point.y / scale)


# --- CGEvent construction & posting -----------------------------------------

if Quartz is not None:
    _CG_BUTTON = {
        MouseButton.LEFT: Quartz.kCGMouseButtonLeft,
        MouseButton.RIGHT: Quartz.kCGMouseButtonRight,
        MouseButton.MIDDLE: Quartz.kCGMouseButtonCenter,
    }
    _MOUSE_DOWN = {
        MouseButton.LEFT: Quartz.kCGEventLeftMouseDown,
        MouseButton.RIGHT: Quartz.kCGEventRightMouseDown,
        MouseButton.MIDDLE: Quartz.kCGEventOtherMouseDown,
    }
    _MOUSE_UP = {
        MouseButton.LEFT: Quartz.kCGEventLeftMouseUp,
        MouseButton.RIGHT: Quartz.kCGEventRightMouseUp,
        MouseButton.MIDDLE: Quartz.kCGEventOtherMouseUp,
    }
    _MOUSE_DRAG = {
        MouseButton.LEFT: Quartz.kCGEventLeftMouseDragged,
        MouseButton.RIGHT: Quartz.kCGEventRightMouseDragged,
        MouseButton.MIDDLE: Quartz.kCGEventOtherMouseDragged,
    }

    #: schema.MODIFIER_KEYS -> CGEventFlags. Kept in lockstep with the schema.
    _MODIFIER_FLAGS = {
        "cmd": Quartz.kCGEventFlagMaskCommand,
        "ctrl": Quartz.kCGEventFlagMaskControl,
        "alt": Quartz.kCGEventFlagMaskAlternate,
        "shift": Quartz.kCGEventFlagMaskShift,
        "fn": Quartz.kCGEventFlagMaskSecondaryFn,
    }
else:  # pragma: no cover - the CGEvent constants exist only on macOS; the
    # tables stay importable (and the chord/modifier validators usable) elsewhere.
    _CG_BUTTON = _MOUSE_DOWN = _MOUSE_UP = _MOUSE_DRAG = {}
    _MODIFIER_FLAGS = {name: 0 for name in ("cmd", "ctrl", "alt", "shift", "fn")}

# US-layout virtual keycodes (Carbon HIToolbox Events.h). MVP limitation:
# TODO(layout): resolve keycodes via UCKeyTranslate so chords work on non-US
# keyboard layouts instead of assuming ANSI-US positions.
_US_KEYCODES: dict[str, int] = {
    # letters
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5, "h": 4,
    "i": 34, "j": 38, "k": 40, "l": 37, "m": 46, "n": 45, "o": 31, "p": 35,
    "q": 12, "r": 15, "s": 1, "t": 17, "u": 32, "v": 9, "w": 13, "x": 7,
    "y": 16, "z": 6,
    # digits (top row)
    "0": 29, "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26,
    "8": 28, "9": 25,
    # function keys
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    # named keys ("enter"/"backspace"/"esc" are aliases)
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51,
    "backspace": 51, "forward_delete": 117, "escape": 53, "esc": 53,
    "left": 123, "right": 124, "down": 125, "up": 126, "home": 115,
    "end": 119, "pageup": 116, "pagedown": 121,
    # punctuation common in shortcuts
    "minus": 27, "equal": 24, "leftbracket": 33, "rightbracket": 30,
    "backslash": 42, "semicolon": 41, "quote": 39, "comma": 43,
    "period": 47, "slash": 44, "grave": 50,
}


def _modifier_flags(modifiers: tuple[str, ...]) -> int:
    """OR together the CGEventFlags for held modifiers; reject unknown names."""
    flags = 0
    for name in modifiers:
        if name not in _MODIFIER_FLAGS:
            raise ValueError(
                f"unknown modifier {name!r}; expected one of {sorted(MODIFIER_KEYS)}"
            )
        flags |= _MODIFIER_FLAGS[name]
    return flags


def _mouse_event(
    event_type: int,
    pos: tuple[float, float],
    button: MouseButton,
    *,
    click_state: int = 0,
    flags: int = 0,
) -> object:
    """Build one mouse CGEvent (not posted)."""
    event = Quartz.CGEventCreateMouseEvent(None, event_type, pos, _CG_BUTTON[button])
    if click_state:
        Quartz.CGEventSetIntegerValueField(
            event, Quartz.kCGMouseEventClickState, click_state
        )
    if flags:
        Quartz.CGEventSetFlags(event, flags)
    return event


def _key_event(keycode: int, key_down: bool, *, flags: int = 0) -> object:
    """Build one keyboard CGEvent (not posted).

    Flags are always set explicitly, even when 0: fresh CGEvents inherit the
    live hardware modifier state, so a user resting a finger on cmd while the
    agent types would otherwise turn every keystroke into a shortcut."""
    event = Quartz.CGEventCreateKeyboardEvent(None, keycode, key_down)
    Quartz.CGEventSetFlags(event, flags)
    return event


def _post(events: list[BuiltEvent], *, dry_run: bool) -> None:
    """Post events to the HID tap with small inter-event delays; no-op in
    dry_run. Callers gate on `_require_ax` *before* building/posting."""
    if dry_run:
        return
    for built in events:
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, built.event)
        time.sleep(EVENT_DELAY_S)


# --- Public actions ----------------------------------------------------------


def click(
    target: Target,
    *,
    button: MouseButton = MouseButton.LEFT,
    count: int = 1,
    modifiers: tuple[str, ...] = (),
    pre_check: PreCheck | None = None,
    dry_run: bool = False,
) -> list[BuiltEvent]:
    """Click ``target`` (element center, or the raw point).

    Posts a leading mouse-move (hover) followed by ``count`` down/up pairs;
    the i-th pair carries ``kCGMouseEventClickState = i`` (the canonical
    1 -> 2 -> 3 progression — stamping ``count`` on every pair would make
    AppKit see two completed double-clicks and fire ``doubleAction`` twice).

    Args:
        target: `Element` (preferred; re-resolved at act time) or `Point`.
        button: Mouse button to press.
        count: 1 = single, 2 = double, 3 = triple click.
        modifiers: Held modifier keys, from `schema.MODIFIER_KEYS`.
        pre_check: Safety hook; receives the `schema.Click` and may veto by
            raising.
        dry_run: Build events without posting (skips the permission gate).

    Returns:
        The built events, in post order.

    Raises:
        ComputerUseError: `ErrorCode.PERMISSION_DENIED_ACCESSIBILITY` when
            live and untrusted; `ErrorCode.STALE_REF` when an element target
            cannot be re-resolved; `ErrorCode.SECURE_FIELD` when the target
            is a secure field.
        ValueError: bad ``count`` or unknown modifier name.
    """
    if count not in (1, 2, 3):
        raise ValueError(f"count must be 1, 2 or 3, got {count}")
    flags = _modifier_flags(tuple(modifiers))
    action = Click(target=target, button=button, count=count, modifiers=tuple(modifiers))
    if pre_check is not None:
        pre_check(action)
    if not dry_run:
        _require_ax()
    pos = _point_to_global(_resolve_point(target))
    events = [BuiltEvent("mouse_move", _mouse_event(Quartz.kCGEventMouseMoved, pos, button, flags=flags))]
    for pair in range(1, count + 1):
        events.append(
            BuiltEvent("mouse_down", _mouse_event(_MOUSE_DOWN[button], pos, button, click_state=pair, flags=flags))
        )
        events.append(
            BuiltEvent("mouse_up", _mouse_event(_MOUSE_UP[button], pos, button, click_state=pair, flags=flags))
        )
    _post(events, dry_run=dry_run)
    return events


def drag(
    start: Target,
    end: Target,
    *,
    button: MouseButton = MouseButton.LEFT,
    pre_check: PreCheck | None = None,
    dry_run: bool = False,
) -> list[BuiltEvent]:
    """Press at ``start``, move to ``end`` in interpolated steps, release.

    The dragged path is linear with roughly one move per `DRAG_STEP_PT`
    global points (always >= 2 steps; the last lands exactly on ``end``).

    Returns:
        The built events: move, down, N drag moves, up.

    Raises:
        ComputerUseError / ValueError: as for `click`.
    """
    action = Drag(start=start, end=end, button=button)
    if pre_check is not None:
        pre_check(action)
    if not dry_run:
        _require_ax()
    x0, y0 = _point_to_global(_resolve_point(start))
    x1, y1 = _point_to_global(_resolve_point(end))
    events = [
        BuiltEvent("mouse_move", _mouse_event(Quartz.kCGEventMouseMoved, (x0, y0), button)),
        BuiltEvent("mouse_down", _mouse_event(_MOUSE_DOWN[button], (x0, y0), button, click_state=1)),
    ]
    steps = max(2, math.ceil(math.hypot(x1 - x0, y1 - y0) / DRAG_STEP_PT))
    for i in range(1, steps + 1):
        t = i / steps
        pos = (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
        events.append(BuiltEvent("mouse_drag", _mouse_event(_MOUSE_DRAG[button], pos, button)))
    events.append(BuiltEvent("mouse_up", _mouse_event(_MOUSE_UP[button], (x1, y1), button, click_state=1)))
    _post(events, dry_run=dry_run)
    return events


def scroll(
    target: Target,
    *,
    dx: int = 0,
    dy: int = 0,
    unit: ScrollUnit = ScrollUnit.LINES,
    pre_check: PreCheck | None = None,
    dry_run: bool = False,
) -> list[BuiltEvent]:
    """Scroll over ``target`` by the given deltas (see `schema.Scroll`).

    Posts a mouse-move to the target (scroll wheels dispatch to the window
    under the pointer) followed by one scroll-wheel event. Schema sign
    convention (positive ``dy`` = content up, positive ``dx`` = content
    left) is the negation of CG's wheel axes (positive = up/left wheel), so
    deltas are negated on the event.

    Returns:
        The built events: move, scroll.

    Raises:
        ComputerUseError / ValueError: as for `click`.
    """
    action = Scroll(target=target, dx=dx, dy=dy, unit=unit)
    if pre_check is not None:
        pre_check(action)
    if not dry_run:
        _require_ax()
    pos = _point_to_global(_resolve_point(target))
    cg_unit = (
        Quartz.kCGScrollEventUnitLine if unit is ScrollUnit.LINES else Quartz.kCGScrollEventUnitPixel
    )
    wheel = Quartz.CGEventCreateScrollWheelEvent(None, cg_unit, 2, -dy, -dx)
    Quartz.CGEventSetLocation(wheel, pos)
    events = [
        BuiltEvent("mouse_move", _mouse_event(Quartz.kCGEventMouseMoved, pos, MouseButton.LEFT)),
        BuiltEvent("scroll", wheel),
    ]
    _post(events, dry_run=dry_run)
    return events


def _utf16_chunks(text: str, max_units: int) -> list[str]:
    """Split text into chunks of <= ``max_units`` UTF-16 code units without
    ever splitting a surrogate pair (astral chars count as 2 units)."""
    chunks: list[str] = []
    current: list[str] = []
    units = 0
    for ch in text:
        n = 2 if ord(ch) > 0xFFFF else 1
        if units + n > max_units and current:
            chunks.append("".join(current))
            current, units = [], 0
        current.append(ch)
        units += n
    if current:
        chunks.append("".join(current))
    return chunks


def _type_via_unicode(text: str, *, dry_run: bool) -> list[BuiltEvent]:
    """Short-text path: keycode-less events carrying the literal characters.

    Both down and up carry the unicode payload (apps otherwise see a bare
    keycode-0 'a' key-up they never got a down for), and both carry
    explicitly cleared modifier flags (see `_key_event`)."""
    events: list[BuiltEvent] = []
    for chunk in _utf16_chunks(text, UNICODE_CHUNK_UTF16_UNITS):
        units = len(chunk.encode("utf-16-le")) // 2
        for kind, key_down in (("unicode_down", True), ("unicode_up", False)):
            event = _key_event(0, key_down)
            Quartz.CGEventKeyboardSetUnicodeString(event, units, chunk)
            events.append(BuiltEvent(kind, event))
    _post(events, dry_run=dry_run)
    return events


def _type_via_clipboard(
    text: str, *, dry_run: bool, pasteboard: Pasteboard | None
) -> list[BuiltEvent]:
    """Long-text path: save pasteboard, set text, cmd+v, restore after delay.

    The restore is skipped when the pasteboard's change count moved after our
    own write — the user (or a clipboard manager) wrote meanwhile, and the
    restore would clobber their new content. Residual risks, by design: an
    app that services the paste later than `PASTE_RESTORE_DELAY_S` (a module
    tunable) pastes the restored old content, and clipboard managers that
    ignore the `_CONCEALED_TYPE` convention still record the transient text.
    """
    pb: Pasteboard = pasteboard if pasteboard is not None else (
        _MemoryPasteboard() if dry_run else _SystemPasteboard()
    )
    saved = pb.save()
    pb.write_text(text)
    marker = pb.change_count()
    flags, keycode = parse_chord("cmd+v")
    events = [
        BuiltEvent("key_down", _key_event(keycode, True, flags=flags)),
        BuiltEvent("key_up", _key_event(keycode, False, flags=flags)),
    ]
    try:
        _post(events, dry_run=dry_run)
    finally:
        if not dry_run:
            time.sleep(PASTE_RESTORE_DELAY_S)
        if pb.change_count() == marker:  # nobody else wrote meanwhile
            pb.restore(saved)
    return events


def type_text(
    text: str,
    *,
    pre_check: PreCheck | None = None,
    dry_run: bool = False,
    pasteboard: Pasteboard | None = None,
) -> list[BuiltEvent]:
    """Type literal text into the focused element.

    Implements the three-path spec (PLAN.md §6): clipboard-paste fast path
    for text longer than `CLIPBOARD_PATH_THRESHOLD` characters, Unicode
    events in `UNICODE_CHUNK_UTF16_UNITS`-unit chunks otherwise; key chords
    go through `key_chord`. IME/dead-key composition is out of scope for
    per-char injection.

    Args:
        text: Literal text; empty text is a no-op returning ``[]``.
        pre_check: Safety hook; receives the `schema.TypeText`.
        dry_run: Build events without posting; the clipboard path then runs
            against an in-memory pasteboard, never the system one.
        pasteboard: Clipboard override for the fast path (tests inject a
            fake; default is the system pasteboard, or in-memory in dry_run).

    Returns:
        The built events, in post order.

    Raises:
        ComputerUseError: `ErrorCode.SECURE_FIELD` when a password field has
            focus — either secure event input is active
            (``IsSecureEventInputEnabled``) or the AX-focused element is an
            ``AXSecureTextField`` (Chrome/Electron web password fields rarely
            enable secure input); `ErrorCode.PERMISSION_DENIED_ACCESSIBILITY`
            when live and untrusted.
    """
    action = TypeText(text=text)
    if pre_check is not None:
        pre_check(action)
    if not text:
        return []
    secure_signal = (
        "IsSecureEventInputEnabled"
        if _secure_input_enabled()
        else ("AXFocusedUIElement" if _focused_element_secure() else None)
    )
    if secure_signal is not None:
        raise ComputerUseError(
            ErrorCode.SECURE_FIELD,
            "a password field has focus; typing requires human handoff",
            detail={"api": secure_signal},
        )
    if not dry_run:
        _require_ax()
    if len(text) > CLIPBOARD_PATH_THRESHOLD:
        return _type_via_clipboard(text, dry_run=dry_run, pasteboard=pasteboard)
    return _type_via_unicode(text, dry_run=dry_run)


def parse_chord(chord: str) -> tuple[int, int]:
    """Parse ``"cmd+shift+s"`` into ``(CGEventFlags, virtual keycode)``.

    Format (see `schema.KeyChord`): key names joined by ``"+"``, modifiers
    first, exactly one non-modifier key last. Names are case-insensitive.

    Raises:
        ValueError: empty/malformed chord, unknown modifier, missing or
            unknown non-modifier key.
    """
    parts = [p.strip().lower() for p in chord.split("+")]
    if not parts or any(not p for p in parts):
        raise ValueError(f"malformed chord {chord!r}: empty key name")
    *mods, key = parts
    flags = _modifier_flags(tuple(mods))
    if key in MODIFIER_KEYS:
        raise ValueError(
            f"chord {chord!r} has no non-modifier key: it must end with a "
            "regular key, e.g. 'cmd+shift+s'"
        )
    if key not in _US_KEYCODES:
        raise ValueError(f"unknown key {key!r} in chord {chord!r}")
    return flags, _US_KEYCODES[key]


def key_chord(
    chord: str,
    *,
    pre_check: PreCheck | None = None,
    dry_run: bool = False,
) -> list[BuiltEvent]:
    """Press a key combination, e.g. ``"cmd+shift+t"`` (see `schema.KeyChord`).

    Modifiers are applied as CGEventFlags on the key's down/up events.
    Keycodes assume a US layout for the MVP (see the UCKeyTranslate TODO on
    `_US_KEYCODES`).

    Returns:
        The built events: key_down, key_up.

    Raises:
        ValueError: malformed chord (see `parse_chord`).
        ComputerUseError: `ErrorCode.PERMISSION_DENIED_ACCESSIBILITY` when
            live and untrusted.
    """
    flags, keycode = parse_chord(chord)
    action = KeyChord(chord=chord)
    if pre_check is not None:
        pre_check(action)
    if not dry_run:
        _require_ax()
    events = [
        BuiltEvent("key_down", _key_event(keycode, True, flags=flags)),
        BuiltEvent("key_up", _key_event(keycode, False, flags=flags)),
    ]
    _post(events, dry_run=dry_run)
    return events


def wait_for(
    target: Element,
    *,
    condition: WaitCondition = WaitCondition.EXISTS,
    timeout_s: float = 10.0,
    pre_check: PreCheck | None = None,
    checker: WaitChecker | None = None,
) -> Element:
    """Block until ``target`` reaches ``condition``; the actionability primitive.

    Polls the injected checker (``checker`` argument, else the module slot
    set by `set_wait_checker` — observe supplies it at integration) every
    `WAIT_POLL_INTERVAL_S`. Posts no events, so it is not gated on the
    Accessibility grant; the checker raises its own permission errors.

    Returns:
        The re-resolved `Element` in its ready state (for GONE, the last
        observed state before disappearance), as returned by the checker.

    Raises:
        ComputerUseError: `ErrorCode.TIMEOUT` when the condition is not met
            within ``timeout_s``.
        RuntimeError: no checker is installed.
    """
    if pre_check is not None:
        pre_check(WaitFor(target=target, condition=condition, timeout_s=timeout_s))
    active = checker if checker is not None else _wait_checker
    if active is None:
        raise RuntimeError(
            "no wait checker configured; observe installs one via set_wait_checker()"
        )
    deadline = time.monotonic() + timeout_s
    while True:
        result = active(target, condition)
        if result is not None:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ComputerUseError(
                ErrorCode.TIMEOUT,
                f"wait_for {condition.value} on {target.ref!r} timed out after {timeout_s}s",
                detail={
                    "ref": target.ref,
                    "condition": condition.value,
                    "timeout_s": timeout_s,
                },
            )
        time.sleep(min(WAIT_POLL_INTERVAL_S, remaining))
