"""Canonical action/observation schema — the contract every module imports.

This file defines the shapes shared by observe/act/capture/safety/server
(PLAN.md §6). Modules import from here and never redefine these types.

Design invariants (do not weaken without updating every consumer):

* **Display-qualified physical pixels.** Every coordinate (`Point`, `Bounds`)
  carries the ``display_id`` it lives on and is expressed in that display's
  *physical* pixel space (Retina backing scale already applied). There is no
  single global coordinate space — mixed Retina/1x setups (and, later,
  Windows negative virtual-desktop origins) make one a bug factory.
* **Refs are snapshot-scoped.** An element ref like ``"e14"`` is only
  meaningful within the `Snapshot` that produced it. macOS ``AXUIElementRef``
  objects are live and unserializable, and trees mutate between ``observe()``
  and ``act()``; ``act()`` re-resolves refs via the anchor attributes stored
  on `Element` (role, title, path, bounds proximity) and raises
  `ComputerUseError` with `ErrorCode.STALE_REF` when re-resolution fails,
  prompting the caller to re-observe.
* **Errors are structured, never stringly-typed.** Every failure mode a model
  is expected to react to is an `ErrorCode`; drivers raise `ComputerUseError`
  and the server serializes it via `ComputerUseError.to_dict()`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

# ---------------------------------------------------------------------------
# Geometry & display metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Point:
    """A display-qualified point in physical pixels.

    Attributes:
        display_id: CGDirectDisplayID of the display this point lives on.
        x: Horizontal offset in physical pixels from the display's left edge.
        y: Vertical offset in physical pixels from the display's top edge.
    """

    display_id: int
    x: int
    y: int


@dataclass(frozen=True, slots=True)
class Bounds:
    """An axis-aligned rectangle in one display's physical-pixel space.

    Attributes:
        display_id: CGDirectDisplayID of the display the rect lives on.
        x: Left edge, physical pixels.
        y: Top edge, physical pixels.
        width: Width in physical pixels (>= 0).
        height: Height in physical pixels (>= 0).
    """

    display_id: int
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> Point:
        """Center of the rect — the default click target for an element."""
        return Point(self.display_id, self.x + self.width // 2, self.y + self.height // 2)


@dataclass(frozen=True, slots=True)
class Display:
    """Metadata for one attached display, included in every `Snapshot`.

    Attributes:
        display_id: CGDirectDisplayID; the value referenced by Point/Bounds.
        width: Physical pixel width (points * scale).
        height: Physical pixel height (points * scale).
        scale: Backing scale factor (2.0 on Retina, 1.0 otherwise). Needed to
            rescale coordinates when screenshots are downscaled for a model.
        is_main: True for the display holding the menu bar.
    """

    display_id: int
    width: int
    height: int
    scale: float
    is_main: bool


# ---------------------------------------------------------------------------
# Observation: snapshots and elements
# ---------------------------------------------------------------------------


class Scope(str, Enum):
    """How much UI an ``observe()``/``screenshot()`` call covers."""

    DISPLAY = "display"  #: everything on one display
    APP = "app"  #: all windows of one application
    WINDOW = "window"  #: the frontmost (or named) window of one application
    ELEMENT = "element"  #: the subtree under a single element


@dataclass(frozen=True, slots=True)
class Element:
    """One node of a pruned accessibility tree.

    The fields double as the *re-resolution anchor*: ``act()`` uses
    (role, title, path, bounds proximity) to find the same node in the live
    tree, because ``ref`` is only valid within its snapshot epoch.

    Attributes:
        ref: Snapshot-scoped id, e.g. ``"e14"``. Unique within one snapshot.
        role: Accessibility role, e.g. ``"AXButton"``.
        title: Accessible title/label; empty string when the app exposes none.
        value: Current value for value-bearing elements (text fields,
            checkboxes, sliders); None for elements without a value. Secure
            fields always carry ``value=None`` (never the secret).
        bounds: On-screen rect in display-qualified physical pixels.
        snapshot_id: Id of the owning `Snapshot`; the staleness anchor.
        parent: Ref of the parent element; None for the snapshot root.
        path: Role path from the root to this node inclusive, e.g.
            ``("AXWindow", "AXToolbar", "AXButton")``. Re-resolution anchor.
        enabled: False for greyed-out/disabled controls.
        focused: True if the element currently has keyboard focus.
        clickable: True if the element accepts press/click actions.
        editable: True if the element accepts text input.
        secure: True for secure/password fields. Acting on a secure element
            raises `ErrorCode.SECURE_FIELD`; the safety layer converts that
            into a human-handoff gate.
    """

    ref: str
    role: str
    title: str
    value: str | None
    bounds: Bounds
    snapshot_id: str
    parent: str | None = None
    path: tuple[str, ...] = ()
    enabled: bool = True
    focused: bool = False
    clickable: bool = False
    editable: bool = False
    secure: bool = False

    @property
    def actionable(self) -> bool:
        """Whether the element can meaningfully receive an action right now."""
        return self.enabled and (self.clickable or self.editable)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A pruned, indexed accessibility tree captured at one instant.

    Refs inside ``elements`` are valid only against this snapshot's
    ``snapshot_id`` (see module docstring on the ref lifecycle).

    Attributes:
        snapshot_id: Unique epoch id, e.g. ``"snap-42"``.
        scope: What the snapshot covers.
        app: Bundle id of the scoped application (e.g.
            ``"com.apple.TextEdit"``); None for `Scope.DISPLAY`.
        pid: Unix pid of the scoped application; None for `Scope.DISPLAY`.
        created_at: ``time.time()`` at capture.
        displays: Metadata for all attached displays at capture time.
        elements: Pruned tree in pre-order (parents before children).
    """

    snapshot_id: str
    scope: Scope
    app: str | None
    pid: int | None
    created_at: float
    displays: tuple[Display, ...]
    elements: tuple[Element, ...]

    def element(self, ref: str) -> Element:
        """Look up a ref within this snapshot.

        Raises:
            KeyError: if ``ref`` does not exist in this snapshot. (Cross-epoch
                staleness is diagnosed by ``observe.resolve_ref``, which
                raises `ErrorCode.STALE_REF` instead.)
        """
        for el in self.elements:
            if el.ref == ref:
                return el
        raise KeyError(f"no element {ref!r} in snapshot {self.snapshot_id!r}")


# ---------------------------------------------------------------------------
# Structured errors
# ---------------------------------------------------------------------------


class ErrorCode(str, Enum):
    """Every failure state a model is expected to react to.

    The string values are wire-stable: they appear verbatim in MCP tool
    results and in the JSONL audit log.
    """

    STALE_REF = "stale_ref"
    """Ref could not be re-resolved against the live tree; re-observe."""

    PERMISSION_DENIED_ACCESSIBILITY = "permission_denied_accessibility"
    """Accessibility (AX) TCC grant missing for the responsible process."""

    PERMISSION_DENIED_SCREEN = "permission_denied_screen"
    """Screen Recording TCC grant missing for the responsible process."""

    SECURE_FIELD = "secure_field"
    """Target is a secure/password field; requires human handoff."""

    FOCUS_CHANGED = "focus_changed"
    """The app in front of / under the target changed between the permission
    decision and injection (same-window recheck, PLAN.md §6); re-observe."""

    APP_NOT_FOUND = "app_not_found"
    """No running (or launchable) application matches the given identifier."""

    TIMEOUT = "timeout"
    """The operation (e.g. ``wait_for``, a hung-app AX call) timed out."""

    CONFIRMATION_DECLINED = "confirmation_declined"
    """An irreversible action needed explicit human confirmation and it was
    declined, cancelled, or could not be requested (no elicitation channel)."""


class ComputerUseError(Exception):
    """A structured failure carrying an `ErrorCode` plus machine-readable detail.

    Drivers raise this; the MCP server serializes it with `to_dict()` so the
    model receives a typed error state instead of a stack trace.
    """

    def __init__(self, code: ErrorCode, message: str, detail: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail: dict[str, object] = detail or {}

    def to_dict(self) -> dict[str, object]:
        """Wire form used in MCP tool results and the audit log."""
        return {"error": self.code.value, "message": self.message, "detail": self.detail}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ComputerUseError({self.code.value!r}, {self.message!r})"


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


class MouseButton(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


class ScrollUnit(str, Enum):
    LINES = "lines"
    PIXELS = "pixels"


class WaitCondition(str, Enum):
    """What `WaitFor` waits for — the actionability primitive."""

    EXISTS = "exists"  #: element re-resolves in the live tree
    ACTIONABLE = "actionable"  #: element exists and `Element.actionable`
    GONE = "gone"  #: element no longer re-resolves


#: Where an action lands: an `Element` (ref-based, re-resolved at act time —
#: preferred) or a raw display-qualified `Point` (always available fallback).
Target: TypeAlias = Element | Point

#: Modifier key names accepted in `Click.modifiers` and `KeyChord.chord`,
#: always lowercase.
MODIFIER_KEYS: frozenset[str] = frozenset({"cmd", "ctrl", "alt", "shift", "fn"})


@dataclass(frozen=True, slots=True)
class Click:
    """Press a mouse button on a target.

    ``count`` folds single/double/triple clicks into one shape; ``button``
    folds left/right/middle. E.g. a plain right-click is
    ``Click(target, button=MouseButton.RIGHT)``.

    Attributes:
        target: Element ref (preferred) or raw point.
        button: Which mouse button.
        count: 1 = single, 2 = double, 3 = triple.
        modifiers: Held modifier keys, from `MODIFIER_KEYS`.
    """

    target: Target
    button: MouseButton = MouseButton.LEFT
    count: int = 1
    modifiers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Drag:
    """Press at ``start``, move to ``end``, release."""

    start: Target
    end: Target
    button: MouseButton = MouseButton.LEFT


@dataclass(frozen=True, slots=True)
class Scroll:
    """Element-targeted scroll with explicit deltas.

    Positive ``dy`` scrolls content up (wheel down); positive ``dx`` scrolls
    content left. Deltas are in ``unit`` — lines for list-like widgets,
    pixels for precise canvas scrolling.
    """

    target: Target
    dx: int = 0
    dy: int = 0
    unit: ScrollUnit = ScrollUnit.LINES


@dataclass(frozen=True, slots=True)
class TypeText:
    """Type literal text into the focused element.

    Implementations pick the path per the three-path spec (PLAN.md §6):
    clipboard-paste for long text, Unicode events for short text; chords go
    through `KeyChord` instead. Typing into a secure field raises
    `ErrorCode.SECURE_FIELD`.
    """

    text: str


@dataclass(frozen=True, slots=True)
class KeyChord:
    """Press a key combination, e.g. ``"cmd+shift+t"`` or ``"escape"``.

    Format: lowercase key names joined by ``"+"``; modifiers (from
    `MODIFIER_KEYS`) first, exactly one non-modifier key last.
    """

    chord: str


@dataclass(frozen=True, slots=True)
class WaitFor:
    """Block until ``target`` reaches ``condition`` or raise `ErrorCode.TIMEOUT`.

    The single biggest reliability lever (Playwright's lesson): act only when
    the UI is ready instead of sleeping fixed amounts.
    """

    target: Element
    condition: WaitCondition = WaitCondition.EXISTS
    timeout_s: float = 10.0


class WindowVerb(str, Enum):
    LIST = "list"
    RAISE = "raise"
    MOVE = "move"
    RESIZE = "resize"
    MINIMIZE = "minimize"


@dataclass(frozen=True, slots=True)
class WindowOp:
    """A window-management verb.

    Attributes:
        verb: What to do.
        window_id: CGWindowID of the target window; required for every verb
            except LIST.
        position: New top-left corner (display-qualified), MOVE only.
        size: New (width, height) in physical pixels, RESIZE only.
    """

    verb: WindowVerb
    window_id: int | None = None
    position: Point | None = None
    size: tuple[int, int] | None = None


class AppVerb(str, Enum):
    LIST = "list"
    LAUNCH = "launch"
    FOCUS = "focus"
    QUIT = "quit"


@dataclass(frozen=True, slots=True)
class AppOp:
    """An application-management verb.

    Attributes:
        verb: What to do.
        app: Bundle id (preferred, e.g. ``"com.apple.TextEdit"``) or display
            name; required for every verb except LIST. Unknown identifiers
            raise `ErrorCode.APP_NOT_FOUND`.
    """

    verb: AppVerb
    app: str | None = None


class ObserveVerb(str, Enum):
    SNAPSHOT = "snapshot"
    SCREENSHOT = "screenshot"
    ZOOM = "zoom"


@dataclass(frozen=True, slots=True)
class ObserveOp:
    """An observation verb: snapshot, screenshot, or zoom.

    Observation reads app content (text-field values, pixels), so it is
    permission-gated at the READ tier and audit-logged like every other
    action ("wraps EVERY action", PLAN.md §6).

    Attributes:
        verb: What is observed.
        app: Bundle id the observation is gated against — the scoped app for
            snapshots, the frontmost app for screen captures.
    """

    verb: ObserveVerb
    app: str | None = None


class ClipboardVerb(str, Enum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class ClipboardOp:
    """Read or write the system clipboard.

    Attributes:
        verb: READ or WRITE.
        text: Plain-text payload; required for WRITE, must be None for READ.
    """

    verb: ClipboardVerb
    text: str | None = None


#: Union of every action the safety layer gates and drivers execute. This is
#: the type ``safety.check_action`` receives and the audit log records.
Action: TypeAlias = (
    Click
    | Drag
    | Scroll
    | TypeText
    | KeyChord
    | WaitFor
    | ObserveOp
    | WindowOp
    | AppOp
    | ClipboardOp
)


def action_to_dict(action: Action) -> dict[str, object]:
    """Serialize an action for the audit log / MCP results.

    Adds a ``"kind"`` discriminator (the lowercase class name) alongside the
    dataclass fields; enums serialize to their string values.
    """
    payload: dict[str, object] = {"kind": type(action).__name__.lower()}
    for f in dataclasses.fields(action):
        value = getattr(action, f.name)
        if isinstance(value, Enum):
            value = value.value
        elif dataclasses.is_dataclass(value) and not isinstance(value, type):
            value = dataclasses.asdict(value)
        payload[f.name] = value
    return payload
