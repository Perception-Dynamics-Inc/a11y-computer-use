"""Provider executor adapters: the drop-in path for existing pixel-loop agents.

An agent built against a provider's native computer-use tool (Anthropic's
``computer`` toolset, OpenAI's ``computer`` tool) keeps emitting the provider's
own actions. The adapter executes each one through the gated `server.Runtime`
(permission tiers, frontmost recheck, secure-field refusal, confirmation gate,
audit log) and hands back the screenshot the provider expects. Nothing about
the host's prompt or loop changes.

Snap-to-ref: when a click lands inside an interactive element of the latest
accessibility snapshot, the adapter executes it as a ref click. The Runtime
re-resolves the ref against the live tree and, for a plain left click,
activates the element through the accessibility API without moving the
pointer. Pixel clients get the accessibility path for free; when nothing
actionable is under the point, or the element is too large to trust the snap
(a text area, a canvas, the window itself), the click falls back to the raw
coordinate exactly as the provider asked.

Coordinate spaces: the model sees the screenshots this adapter returns and
speaks in that image's pixel space. `ComputerAdapter.to_physical` maps image
coordinates back to display-qualified physical pixels with the same
`capture.ScaledImage` mapping the Runtime's own ``screenshot`` tool uses.

Every provider action resolves to a `Result`, never an exception: refusals,
structured errors, and invalid inputs come back as text the model can read.
"""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from computeruse import capture, server
from computeruse.schema import ComputerUseError, Element, ErrorCode

__all__ = [
    "ComputerAdapter",
    "Result",
    "openai_keys_to_chords",
    "xdotool_to_chord",
]

#: The Runtime's screenshot text is the one place the captured display id and
#: the scaled/physical dimensions travel together; the adapter reads them back
#: from it rather than changing the Runtime's return shape.
_SCREENSHOT_TEXT = re.compile(
    r"^display (-?\d+): (\d+)x(\d+) px image, downscaled from (\d+)x(\d+) physical px"
)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Result:
    """Outcome of one provider action.

    Attributes:
        action: The provider action name that was handled.
        text: Human/model-readable outcome. Empty for a bare screenshot.
        png: PNG bytes when the action produced an image (screenshot, zoom).
        error: None on success; otherwise a `schema.ErrorCode` value, or
            ``"refused"`` (a safety tier decision) or ``"invalid"`` (bad input).
        snapped_ref: The element ref a coordinate click was snapped to, if any.
    """

    action: str
    text: str = ""
    png: bytes | None = None
    error: str | None = None
    snapped_ref: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def image_data_url(self) -> str | None:
        """``data:image/png;base64,...`` for the image, or None."""
        if self.png is None:
            return None
        return "data:image/png;base64," + base64.b64encode(self.png).decode("ascii")

    def anthropic_content(self) -> list[dict]:
        """Content blocks for an Anthropic ``tool_result``: the image when there
        is one, the text otherwise (``OK`` when there is neither)."""
        blocks: list[dict] = []
        if self.png is not None:
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(self.png).decode("ascii"),
                },
            })
        if self.text or not blocks:
            blocks.append({"type": "text", "text": self.text or "OK"})
        return blocks

    def to_anthropic_tool_result(self, tool_use_id: str, *, toolset_name: str | None = None) -> dict:
        """A complete ``tool_result`` block. Errors set ``is_error`` and carry the
        text, which is how the Anthropic computer-use docs report failures."""
        block: dict = {"type": "tool_result", "tool_use_id": tool_use_id}
        if toolset_name:
            block["toolset_name"] = toolset_name
        if self.error is not None:
            block["is_error"] = True
            block["content"] = self.text
        else:
            block["content"] = self.anthropic_content()
        return block


# ---------------------------------------------------------------------------
# Key names: xdotool (Anthropic) and uppercase tokens (OpenAI) -> our chords
# ---------------------------------------------------------------------------

#: Modifier spellings from both providers -> `schema.MODIFIER_KEYS` names.
#: ``super``/``meta``/``win`` all mean the platform command key: cmd on macOS,
#: the Windows/Super key on the other drivers (their chord tables map ``cmd``).
_MODIFIER_ALIASES: dict[str, str] = {
    "ctrl": "ctrl", "control": "ctrl", "control_l": "ctrl", "control_r": "ctrl",
    "shift": "shift", "shift_l": "shift", "shift_r": "shift",
    "alt": "alt", "alt_l": "alt", "alt_r": "alt", "option": "alt",
    "cmd": "cmd", "command": "cmd", "super": "cmd", "super_l": "cmd", "super_r": "cmd",
    "meta": "cmd", "meta_l": "cmd", "meta_r": "cmd", "win": "cmd", "windows": "cmd",
    "fn": "fn",
}

#: Non-modifier key spellings -> the canonical chord token every driver's chord
#: table understands (``act._US_KEYCODES`` on macOS, ``_NAMED_KEY`` in the
#: browser driver, the keysym/VK tables on Linux/Windows).
_KEY_ALIASES: dict[str, str] = {
    "return": "return", "enter": "return", "kp_enter": "return",
    "backspace": "backspace",
    "delete": "delete", "kp_delete": "delete", "del": "delete",
    "escape": "escape", "esc": "escape",
    "tab": "tab", "iso_left_tab": "tab",
    "space": "space", "spacebar": "space",
    "up": "up", "arrowup": "up", "kp_up": "up",
    "down": "down", "arrowdown": "down", "kp_down": "down",
    "left": "left", "arrowleft": "left", "kp_left": "left",
    "right": "right", "arrowright": "right", "kp_right": "right",
    "home": "home", "kp_home": "home",
    "end": "end", "kp_end": "end",
    "page_up": "pageup", "pageup": "pageup", "prior": "pageup", "kp_page_up": "pageup",
    "page_down": "pagedown", "pagedown": "pagedown", "next": "pagedown",
    "kp_page_down": "pagedown",
}

#: Punctuation: xdotool keysym names and the literal characters. The macOS
#: chord parser wants the names; the other drivers take the character.
_PUNCT_NAMES: dict[str, tuple[str, str]] = {
    "minus": ("minus", "-"), "-": ("minus", "-"),
    "equal": ("equal", "="), "=": ("equal", "="),
    "bracketleft": ("leftbracket", "["), "[": ("leftbracket", "["),
    "bracketright": ("rightbracket", "]"), "]": ("rightbracket", "]"),
    "backslash": ("backslash", "\\"), "\\": ("backslash", "\\"),
    "semicolon": ("semicolon", ";"), ";": ("semicolon", ";"),
    "apostrophe": ("quote", "'"), "quote": ("quote", "'"), "'": ("quote", "'"),
    "comma": ("comma", ","), ",": ("comma", ","),
    "period": ("period", "."), ".": ("period", "."),
    "slash": ("slash", "/"), "/": ("slash", "/"),
    "grave": ("grave", "`"), "`": ("grave", "`"),
}

_FKEY = re.compile(r"f([1-9]|1[0-2])")


def _normalize_key(name: str, driver: str) -> str:
    """One non-modifier key spelling -> the chord token for ``driver``."""
    lowered = name.strip().lower()
    if not lowered:
        raise ValueError("empty key name")
    if lowered in _KEY_ALIASES:
        canon = _KEY_ALIASES[lowered]
        # macOS keycodes call forward-delete "forward_delete" and "delete" is the
        # backspace key; every other driver uses the CDP/VK meaning of "delete".
        if canon == "delete" and driver == "macos":
            return "forward_delete"
        return canon
    if lowered in _PUNCT_NAMES:
        named, char = _PUNCT_NAMES[lowered]
        return named if driver == "macos" else char
    if _FKEY.fullmatch(lowered):
        return lowered
    if len(lowered) == 1 and (lowered.isalnum()):
        return lowered
    raise ValueError(f"unknown key name {name!r}")


def _dedupe(items: Sequence[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def xdotool_to_chord(text: str, driver: str = "macos") -> str:
    """Convert an xdotool-style key string (``"ctrl+s"``, ``"Return"``,
    ``"alt+Tab"``, ``"super+shift+4"``) into a `schema.KeyChord` string.

    Raises:
        ValueError: empty input, an unknown modifier or key name, or a chord
            with no non-modifier key (``"ctrl"`` alone).
    """
    parts = [p.strip() for p in text.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty key string {text!r}")
    *mod_parts, key = parts
    mods: list[str] = []
    for m in mod_parts:
        canon = _MODIFIER_ALIASES.get(m.lower())
        if canon is None:
            raise ValueError(f"unknown modifier {m!r} in {text!r}")
        mods.append(canon)
    if key.lower() in _MODIFIER_ALIASES:
        raise ValueError(f"{text!r} has no non-modifier key; a chord ends with a regular key")
    return "+".join([*_dedupe(mods), _normalize_key(key, driver)])


def openai_keys_to_chords(keys: Sequence[str], driver: str = "macos") -> list[str]:
    """Convert an OpenAI ``keypress`` key list (``["CTRL", "A"]``, ``["ENTER"]``)
    into one chord per non-modifier key, each holding every modifier listed.

    OpenAI sends uppercase names (``CTRL``, ``META``, ``ENTER``, ``ARROWUP``);
    the same aliases handle them lowercased.
    """
    if not keys:
        raise ValueError("empty key list")
    mods: list[str] = []
    regular: list[str] = []
    for k in keys:
        canon = _MODIFIER_ALIASES.get(str(k).strip().lower())
        if canon is not None:
            mods.append(canon)
        else:
            regular.append(_normalize_key(str(k), driver))
    if not regular:
        raise ValueError(f"key list {list(keys)!r} has no non-modifier key")
    mods = _dedupe(mods)
    return ["+".join([*mods, key]) for key in regular]


def modifiers_from_text(text: str | None) -> tuple[str, ...]:
    """Anthropic's optional ``text`` on click/scroll actions: ``+``-joined
    modifiers held during the action (``"ctrl+shift"``)."""
    if not text:
        return ()
    mods: list[str] = []
    for part in text.split("+"):
        part = part.strip()
        if not part:
            continue
        canon = _MODIFIER_ALIASES.get(part.lower())
        if canon is None:
            raise ValueError(f"unknown modifier {part!r}; expected shift, ctrl, alt, super")
        mods.append(canon)
    return tuple(_dedupe(mods))


# ---------------------------------------------------------------------------
# The adapter core
# ---------------------------------------------------------------------------


class ComputerAdapter:
    """Provider-neutral execution: image-space actions in, `Result` out.

    Args:
        runtime: The gated `server.Runtime` every action goes through.
        app: App identifier (bundle id / process name / browser tab id) used for
            the accessibility snapshots behind snap-to-ref and Set-of-Mark. None
            means the frontmost app (the bound tab on the browser backend).
        display_id: Display to capture and act on. None means the main display;
            the id the Runtime reports for the first screenshot is remembered.
        max_long_edge: Long-edge budget for screenshots sent to the model.
        marks: Draw Set-of-Mark ref labels on every screenshot.
        snap_to_refs: Execute coordinate clicks as ref clicks when the point is
            inside an actionable element of a fresh snapshot.
        max_snap_fraction: Elements covering more than this fraction of the
            display never snap (a text area or canvas click keeps its exact
            coordinate).
        confirm: Optional human-confirmation callback for plausibly irreversible
            clicks (the Runtime's confirmation gate). None blocks such clicks
            with a ``confirmation_declined`` result, never fires them.
        max_wait_s: Ceiling for provider ``wait`` actions.
    """

    def __init__(
        self,
        runtime: server.Runtime,
        *,
        app: str | None = None,
        display_id: int | None = None,
        max_long_edge: int = capture.DEFAULT_MAX_LONG_EDGE,
        marks: bool = False,
        snap_to_refs: bool = True,
        max_snap_fraction: float = 0.25,
        confirm: Callable[[str], bool] | None = None,
        max_wait_s: float = 30.0,
    ) -> None:
        self.runtime = runtime
        self.app = app
        self.max_long_edge = max_long_edge
        self.marks = marks
        self.snap_to_refs = snap_to_refs
        self.max_snap_fraction = max_snap_fraction
        self.confirm = confirm
        self.max_wait_s = max_wait_s
        self._display_id = display_id
        self._screen: capture.ScaledImage | None = None
        #: Last pointer position the model expressed, in image space.
        self._cursor: tuple[int, int] | None = None
        self._mouse_down: tuple[int, int] | None = None

    # -- state ---------------------------------------------------------------

    @property
    def driver_name(self) -> str:
        return str(getattr(self.runtime.driver, "name", "unknown"))

    @property
    def screen(self) -> capture.ScaledImage | None:
        """The last screenshot's scaling record (image <-> physical mapping)."""
        return self._screen

    @property
    def display_id(self) -> int | None:
        return self._display_id

    @property
    def cursor(self) -> tuple[int, int] | None:
        return self._cursor

    def gate_app(self) -> str:
        """The app whose accessibility tree backs snaps and marks."""
        return self.app or self.runtime._frontmost()

    def refresh_snapshot(self) -> bool:
        """Take a fresh snapshot of `gate_app` through the gate. Best effort:
        a missing READ grant or an app without a tree just disables snapping."""
        try:
            self.runtime.desktop_snapshot(self.gate_app())
        except Exception:
            return False
        return True

    # -- guard -----------------------------------------------------------------

    def _guard(self, action: str, fn: Callable[[], Result]) -> Result:
        """Run ``fn``; every structured failure becomes a readable `Result`."""
        try:
            return fn()
        except ComputerUseError as exc:
            return Result(action, server.error_text(exc), error=exc.code.value)
        except server.ActionRefused as exc:
            return Result(action, server.refusal_text(exc.decision), error="refused")
        except (ValueError, KeyError, TypeError) as exc:
            return Result(action, f"invalid {action}: {exc}", error="invalid")

    # -- coordinates -------------------------------------------------------------

    def _ensure_screen(self) -> Result | None:
        """Make sure an image<->physical mapping exists; returns the error
        `Result` when the probe screenshot fails."""
        if self._screen is not None:
            return None
        shot = self.screenshot(action="screenshot")
        return None if shot.ok else shot

    def to_physical(self, x: int, y: int) -> tuple[int, int, int | None]:
        """Image-space point -> (physical x, physical y, display id).

        Coordinates outside the image are clamped to its edge, as the Runtime's
        own mapping does. Requires a prior screenshot (see `_ensure_screen`).
        """
        if self._screen is None:
            raise ValueError("no screenshot has been taken yet; coordinates have no reference image")
        px, py = self._screen.to_source(int(round(x)), int(round(y)))
        return px, py, self._display_id

    def from_physical(self, px: int, py: int) -> tuple[int, int]:
        if self._screen is None:
            raise ValueError("no screenshot has been taken yet")
        return self._screen.from_source(px, py)

    def snap_to_ref(self, px: int, py: int, display_id: int | None) -> Element | None:
        """The smallest actionable element of the latest snapshot containing the
        physical point, or None. Elements larger than ``max_snap_fraction`` of
        the display are skipped so clicks inside big surfaces keep their exact
        coordinate."""
        snap = self.runtime._current
        if snap is None:
            return None
        display_area: int | None = None
        for d in snap.displays:
            if display_id is None or d.display_id == display_id:
                display_area = d.width * d.height
                break
        if display_area is None and self._screen is not None:
            display_area = self._screen.source_width * self._screen.source_height
        best: Element | None = None
        best_area = 0
        for el in snap.elements:
            if not el.actionable:
                continue
            b = el.bounds
            if display_id is not None and b.display_id != display_id:
                continue
            if not (b.x <= px < b.x + b.width and b.y <= py < b.y + b.height):
                continue
            area = max(1, b.width * b.height)
            if display_area and area > self.max_snap_fraction * display_area:
                continue
            if best is None or area < best_area:
                best, best_area = el, area
        return best

    # -- primitives (each returns a Result) ------------------------------------

    def screenshot(self, *, action: str = "screenshot") -> Result:
        def run() -> Result:
            if self.marks:
                self.refresh_snapshot()  # Set-of-Mark labels come from the latest tree
            text, scaled = self.runtime.screenshot(self._display_id, self.max_long_edge, self.marks)
            m = _SCREENSHOT_TEXT.match(text)
            if m is not None and self._display_id is None:
                self._display_id = int(m.group(1))
            self._screen = scaled
            return Result(action, "", scaled.png)

        return self._guard(action, run)

    def click(
        self,
        x: int,
        y: int,
        *,
        button: str = "left",
        count: int = 1,
        modifiers: Sequence[str] = (),
        action: str = "left_click",
    ) -> Result:
        def run() -> Result:
            err = self._ensure_screen()
            if err is not None:
                return err
            px, py, did = self.to_physical(x, y)
            self._cursor = (int(x), int(y))
            mods = list(modifiers)
            if self.snap_to_refs:
                self.refresh_snapshot()
                el = self.snap_to_ref(px, py, did)
                if el is not None:
                    try:
                        msg = self.runtime.click(
                            ref=el.ref, button=button, count=count, modifiers=mods, confirm=self.confirm
                        )
                    except ComputerUseError as exc:
                        if exc.code is not ErrorCode.STALE_REF:
                            raise
                    else:
                        title = f" {el.title!r}" if el.title else ""
                        return Result(
                            action,
                            f"{msg} [snapped from image point ({x}, {y}) to {el.ref} {el.role}{title}]",
                            snapped_ref=el.ref,
                        )
            msg = self.runtime.click(
                x=px, y=py, display_id=did, button=button, count=count, modifiers=mods,
                confirm=self.confirm,
            )
            return Result(action, f"{msg} [image point ({x}, {y}) -> physical ({px}, {py})]")

        return self._guard(action, run)

    def drag(self, x0: int, y0: int, x1: int, y1: int, *, action: str = "left_click_drag") -> Result:
        def run() -> Result:
            err = self._ensure_screen()
            if err is not None:
                return err
            sx, sy, did = self.to_physical(x0, y0)
            ex, ey, _ = self.to_physical(x1, y1)
            self._cursor = (int(x1), int(y1))
            msg = self.runtime.drag(start_x=sx, start_y=sy, end_x=ex, end_y=ey, display_id=did)
            return Result(action, f"{msg} [image ({x0}, {y0}) -> ({x1}, {y1})]")

        return self._guard(action, run)

    def scroll(
        self,
        x: int | None,
        y: int | None,
        *,
        dx: int = 0,
        dy: int = 0,
        unit: str = "lines",
        action: str = "scroll",
    ) -> Result:
        def run() -> Result:
            err = self._ensure_screen()
            if err is not None:
                return err
            assert self._screen is not None
            if x is None or y is None:
                cx, cy = self._cursor or (self._screen.width // 2, self._screen.height // 2)
            else:
                cx, cy = int(x), int(y)
            px, py, did = self.to_physical(cx, cy)
            self._cursor = (cx, cy)
            msg = self.runtime.scroll(x=px, y=py, display_id=did, dx=dx, dy=dy, unit=unit)
            return Result(action, f"{msg} [at image point ({cx}, {cy})]")

        return self._guard(action, run)

    def type_text(self, text: str, *, action: str = "type") -> Result:
        return self._guard(action, lambda: Result(action, self.runtime.type_text(text)))

    def key(self, chord: str, *, repeat: int = 1, action: str = "key") -> Result:
        def run() -> Result:
            n = max(1, min(int(repeat), 100))
            msg = ""
            for _ in range(n):
                msg = self.runtime.key(chord)
            return Result(action, msg if n == 1 else f"{msg} (x{n})")

        return self._guard(action, run)

    def wait(self, seconds: float, *, action: str = "wait") -> Result:
        def run() -> Result:
            s = max(0.0, min(float(seconds), self.max_wait_s))
            time.sleep(s)
            return Result(action, f"waited {s:g}s")

        return self._guard(action, run)

    def zoom(self, x0: int, y0: int, x1: int, y1: int, *, action: str = "zoom") -> Result:
        """Native-resolution crop of an image-space region, scaled back to fit
        within the screenshot dimensions (aspect preserved)."""

        def run() -> Result:
            err = self._ensure_screen()
            if err is not None:
                return err
            assert self._screen is not None
            left, top = min(x0, x1), min(y0, y1)
            right, bottom = max(x0, x1), max(y0, y1)
            if right <= left or bottom <= top:
                raise ValueError(f"empty zoom region {[x0, y0, x1, y1]}")
            px0, py0, did = self.to_physical(left, top)
            px1, py1, _ = self.to_physical(right, bottom)
            if did is None:
                raise ValueError("zoom needs a known display id; take a screenshot first")
            w, h = max(1, px1 - px0), max(1, py1 - py0)
            png = self.runtime.zoom(did, px0, py0, w, h)
            factor = min(self._screen.width / w, self._screen.height / h, 1.0)
            if factor < 1.0:
                png = capture.downscale(png, max(1, int(max(w, h) * factor))).png
            return Result(action, "", png)

        return self._guard(action, run)

    def move(self, x: int, y: int, *, action: str = "mouse_move") -> Result:
        """Record the pointer position. No driver exposes a hover primitive, so
        no event is sent; a following click or mouse-up uses this position."""
        self._cursor = (int(x), int(y))
        return Result(
            action,
            f"cursor position set to ({x}, {y}) for the next action; this backend sends no "
            "hover event",
        )

    def cursor_position(self, *, action: str = "cursor_position") -> Result:
        if self._cursor is None:
            return Result(action, "X=0, Y=0 (no pointer action has been performed yet)")
        return Result(action, f"X={self._cursor[0]}, Y={self._cursor[1]}")

    def mouse_down(self, *, action: str = "left_mouse_down") -> Result:
        """Start of a manual drag: remembered until `mouse_up` (the drivers
        expose drag as one operation, not separate button events)."""
        if self._cursor is None:
            return Result(action, "left_mouse_down needs a prior mouse_move or click", error="invalid")
        self._mouse_down = self._cursor
        return Result(action, f"left button held at ({self._cursor[0]}, {self._cursor[1]}); "
                              "released as a drag or click on left_mouse_up")

    def mouse_up(self, *, action: str = "left_mouse_up") -> Result:
        if self._mouse_down is None or self._cursor is None:
            return Result(action, "left_mouse_up without a matching left_mouse_down", error="invalid")
        start, end = self._mouse_down, self._cursor
        self._mouse_down = None
        if start == end:
            return self.click(end[0], end[1], action=action)
        return self.drag(start[0], start[1], end[0], end[1], action=action)
