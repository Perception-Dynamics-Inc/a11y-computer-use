"""Observation: pruned accessibility-tree snapshots and ref resolution.

Wraps the macOS AX APIs (``AXUIElement``) and the tree pruning engine
(PLAN.md §6). Budget target: a filtered snapshot serializes to <= ~1k tokens
on the Phase-0 test apps.

Layering: `snapshot` owns everything platform-specific — the TCC check, pid
lookup, AX handles with `AXUIElementSetMessagingTimeout`, display topology —
and hands a root node plus a `TreeAccessor` to `build_snapshot`, the pure
pruning/indexing core. Tests (and future non-AX backends) drive
`build_snapshot` with synthetic accessors; only this module's ``_AX*``
helpers import pyobjc, and only lazily.

Ref lifecycle: `build_snapshot` assigns refs ``e1..eN`` in pre-order and
registers each snapshot epoch in a small module registry (which also carries
the "…N more" elision counts that `render_text` displays). Refs are only
meaningful against their own epoch; `resolve_ref` re-resolves an element in a
*live* tree via its anchors (role, title, path, bounds proximity) and raises
`ErrorCode.STALE_REF` when the element is gone or ambiguous.
"""

from __future__ import annotations

import dataclasses
import re

import itertools
import difflib
import math
import os
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from a11y_computer_use.schema import (
    Bounds,
    ComputerUseError,
    Display,
    Element,
    ErrorCode,
    Scope,
    Snapshot,
)

#: Seconds a hung app may stall a single AX call before it errors out
#: (`AXUIElementSetMessagingTimeout`, PLAN.md §6 "supervised workers").
AX_MESSAGING_TIMEOUT_S = 2.0

#: Depth cap, counted over KEPT ancestors: nodes whose pruned depth reaches this
#: are elided (with a marker on the parent). Single-child wrappers that collapse
#: (`_WRAPPER_ROLES`) do not consume a level, so a link's text node under a
#: dozen generic web wrappers is still shown. The kept depth is exact during
#: the walk (a wrapper adds a level unless it will collapse), so this cap also
#: bounds the walk cost on cyclic trees with fan-out, as it always did.
MAX_DEPTH = 12

#: Backstop on RAW recursion depth for the one shape `MAX_DEPTH` cannot bound:
#: a fan-out-1 cycle of collapsing wrappers (a group whose only child is itself),
#: which costs O(_MAX_RAW_DEPTH) reads.
_MAX_RAW_DEPTH = 64

#: Nested Chromium iframes may each restart the kept-depth budget this many
#: times per snapshot (an ``internal frame``, or a ``document web`` that is
#: already inside a page). Each restart walks at most `MAX_DEPTH` further kept
#: levels. The raw-depth backstop still applies.
_MAX_WEB_DEPTH_RESTARTS = 4

#: Extra node reads, per snapshot, spent looking for one of those frames after
#: a web subtree is already past `MAX_DEPTH`. The scan keeps only the frame it
#: opens; ordinary deep nodes stay elided, so a page with no nested frame
#: still drops the same children.
_MAX_WEB_FRAME_SCAN = 64

#: Kept-children cap per node. Overflow is elided with a "… N more" marker,
#: keeping interactive/labelled children preferentially.
MAX_CHILDREN = 24

#: Tighter per-widget cap for dense, repetitive containers (grids/tables/
#: outlines — e.g. Calendar's month grid). These blow the snapshot token
#: budget with near-identical rows, so cap them harder; the elision marker
#: tells the agent to scroll/re-observe for the rest (COM-12). A spreadsheet
#: whose cells are titled with addresses uses `SHEET_MAX_CHILDREN` instead.
DENSE_MAX_CHILDREN = 12
_DENSE_CONTAINER_ROLES = frozenset({"AXGrid", "AXTable", "AXOutline"})

#: A Calc sheet is an AXTable whose children are titled with cell addresses.
#: The dense cap of 12 keeps A1–L1 and elides B2. Calendars stay at 12.
#: One band of that grid is at most this many cells. The Linux walker splits
#: the on-screen window (more than the first six rows) into bands of this
#: size so the walk cap does not drop the lower rows. A table or outline
#: with at most MAX_CHILDREN children is not that grid: a Qt 4×3 table plus
#: its headers is 19 nodes and keeps every cell.
_SHEET_ADDRESS = re.compile(r"^[A-Z]{1,3}[1-9][0-9]*$")
SHEET_MAX_CHILDREN = 96

#: Raw children inspected per node before giving up — virtualized lists can
#: report thousands of rows and each read costs several AX round-trips.
_MAX_WALK_CHILDREN = 200

_MAX_VALUE_CHARS = 200  #: Element.value cap the model reads (anchors never use value)
_RENDER_VALUE_CHARS = 48  #: value cap in the text rendering
#: A VTE screen is kept whole up to this many characters. Past it, the
#: snapshot shows the tail and a leading ellipsis marks the cut. ``find``
#: still searches the whole buffer.
_TERMINAL_VALUE_CHARS = 4000
_MAX_EPOCHS = 8  #: snapshot epochs kept in the render registry
_AMBIGUITY_PX = 2.0  #: distance tie window that makes re-resolution ambiguous
_WEAK_ANCHOR_DRIFT_PX = 400.0  #: max drift when only one of title/path matches

_SECURE_ROLE = "AXSecureTextField"
_DECORATIVE_ROLES = frozenset({"AXUnknown", "AXSplitter", "AXGrowArea"})
_WRAPPER_ROLES = frozenset(
    {"AXGroup", "AXGenericElement", "AXLayoutArea", "AXScrollArea", "AXSplitGroup"}
)
#: Painted pixels the accessibility tree does not describe. A canvas, a
#: drawing area, and an image with no usable name. The snapshot role is this
#: string, not an ``AX*`` name, so the rendered line says ``opaque_region``.
OPAQUE_ROLE = "opaque_region"
_OPAQUE_SURFACE_ROLES = frozenset({"AXImage", "AXCanvas", OPAQUE_ROLE})
#: Childless containers that currently survive as untitled groups. A group
#: that still has children is layout, not a pixel surface.
_DEAD_REGION_ROLES = frozenset({"AXGroup", "AXGenericElement", "AXLayoutArea"})
_GENERIC_REGION_NAMES = frozenset({
    "",
    "image",
    "img",
    "graphic",
    "icon",
    "photo",
    "picture",
    "canvas",
    "webgl",
    "drawing area",
    "unnamed",
    "unknown",
    "group",
})
#: Chrome's name for an ``<img>`` that has no alt. It is a placeholder, not
#: a description of the pixels, so the image is still an opaque region.
_CHROME_IMAGE_PLACEHOLDER = "to get missing image descriptions"
_PRESS_ACTIONS = frozenset({"AXPress", "AXOpen", "AXConfirm", "AXPick"})
_CLICKABLE_ROLES = frozenset(
    {
        "AXButton",
        "AXCheckBox",
        "AXComboBox",
        "AXDisclosureTriangle",
        "AXLink",
        "AXMenuBarItem",
        "AXMenuButton",
        "AXMenuItem",
        "AXPopUpButton",
        "AXRadioButton",
    }
)
# A combo or select is not an editable entry. set_value writes it through the
# combo path. AXComboBox used to set the edit flag, which made a Firefox
# <select> match find(editable=true).
_EDITABLE_ROLES = frozenset(
    {"AXSearchField", _SECURE_ROLE, "AXTextArea", "AXTextField"}
)
#: Controls kept when a Chromium page is already at ``MAX_DEPTH``. A named
#: group is not one of these: it is walked through and not kept. Static text
#: is not one either, so a buried label stays elided.
_BURIED_CONTROL_ROLES = frozenset(
    {
        "AXButton",
        "AXCheckBox",
        "AXComboBox",
        "AXLink",
        "AXPopUpButton",
        "AXRadioButton",
        "AXSearchField",
        _SECURE_ROLE,
        "AXSlider",
        "AXSpinButton",
        "AXTextArea",
        "AXTextField",
    }
)

#: Rendering views of a snapshot. ``full`` is the whole pruned tree. ``interactive``
#: is the same snapshot (same elements, same refs) rendered down to what an agent
#: can act on: actionable/stateful elements, the containers needed to tell them
#: apart, and one folded ``text:`` line per container for the static text. Both
#: views share refs, so an agent can switch views mid-task without a stale ref.
RENDER_MODES = ("full", "interactive")
#: What ``desktop_snapshot(mode=...)`` accepts: a view, or ``diff`` (delta since
#: the previous snapshot of the same app, rendered in the view last requested).
SNAPSHOT_MODES = ("full", "interactive", "diff")

#: Roles kept by the interactive view even when the platform exposes no press
#: action on them: rows are what an agent selects in lists/tables/trees, tabs
#: switch panes, sliders and ARIA spin buttons take values. Cells are folded
#: into their row's text.
_INTERACTIVE_ROLES = frozenset({"AXRow", "AXTab", "AXSlider", "AXSpinButton", "AXIncrementor"})
#: Containers the interactive view keeps as structure (even untitled) so refs
#: inside them stay disambiguated: windows/dialogs, menus, toolbars, tab groups.
_STRUCTURAL_ROLES = frozenset(
    {"AXWindow", "AXSheet", "AXDialog", "AXDrawer", "AXPopover", "AXMenu",
     "AXMenuBar", "AXToolbar", "AXTabGroup", "AXWebArea"}
)
_COLLAPSED_TEXT_CHARS = 96  #: cap of the folded ``text:`` line per container
_COLLAPSED_TEXT_ITEMS = 12  #: static-text items folded before "(+N)" takes over

_DOCTOR_HINT = (
    "Grant Accessibility to the host app that launched this process in "
    "System Settings > Privacy & Security > Accessibility, then retry; "
    "`a11y_computer_use doctor` names the exact host app that needs the grant."
)


# ---------------------------------------------------------------------------
# Accessor seam: how the pruning core reads any (live or synthetic) tree
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawNode:
    """Unpruned attributes of one a11y node, as reported by a `TreeAccessor`.

    ``position``/``size`` are in the platform's *global point* space (AX
    screen coordinates, top-left origin); `build_snapshot` projects them into
    display-qualified physical pixels via `DisplayGeometry`. Either being
    None means the node has no on-screen geometry.
    """

    role: str
    subrole: str | None = None
    title: str = ""
    value: object | None = None
    description: str = ""
    enabled: bool = True
    focused: bool = False
    position: tuple[float, float] | None = None
    size: tuple[float, float] | None = None
    actions: tuple[str, ...] = ()
    checked: bool | None = None
    selected: bool = False
    expanded: bool | None = None
    placeholder: str = ""
    stable_id: str | None = None
    #: Linux AT-SPI only. Empty on macOS, Windows, and the browser, so those
    #: trees keep today's walk and token budget. ``"page"`` is a Chromium
    #: ``document web``, ``"docframe"`` a ``document frame``, ``"iframe"`` an
    #: ``internal frame``. A zero-size non-interactive wrapper under any of
    #: these is hollow. A nested iframe, or a document web already inside a
    #: page, restarts the kept-depth budget.
    atspi_web: str = ""
    #: AT-SPI ``FOCUSABLE``. Default false, so macOS, Windows, and the browser
    #: do not grow a new signal. A zero-size web wrapper that can take focus
    #: stays a control; a plain div that cannot does not.
    focusable: bool = False
    #: Platform text-entry signal independent of role. AT-SPI sets this only
    #: when the node itself is an editable entry: an entry, text, or password
    #: role that is not read-only, or a contenteditable section (a div or span
    #: with ``STATE_EDITABLE``; Chromium may omit EditableText, Firefox must
    #: expose it). A paragraph, panel, document, or combo is not an entry, even
    #: when Firefox reports ``STATE_EDITABLE`` or EditableText on it. An
    #: Impress or Draw paragraph is an entry when it has ``STATE_EDITABLE``
    #: and EditableText. Default false, so macOS, Windows, and the browser
    #: stay editable only when the role is a text field.
    editable: bool = False
    #: Process identity of the app that owns this node. Linux sets
    #: ``pid:<pid>|start:<ticks>`` and appends ``|bus:<name>`` when the
    #: application exposes an AT-SPI bus name. None on backends that do not
    #: record one, so those refs still re-resolve by role and title.
    instance_id: str | None = None
    #: Window or document this node belongs to. Set on a frame, window, or
    #: dialog; descendants inherit it while the tree is flattened. None when
    #: the backend has no document identity for the node.
    document_id: str | None = None
    #: Chromium or Firefox UI outside a content document. Default false, so
    #: macOS, Windows, GTK, and page content are unchanged. A childless group
    #: with this flag is the browser frame, not a page region.
    browser_chrome: bool = False
    #: VTE only. The on-screen slice of ``value``, with a leading ellipsis
    #: when characters sit above the widget. None on every other node, and
    #: when the driver could not separate the screen from the buffer. The
    #: snapshot shows this text; ``find`` still searches ``value``.
    screen_text: str | None = None
    #: A control the toolkit did not allocate, such as a notebook page tab
    #: while the tab bar is hidden. The snapshot keeps the name and does not
    #: invent a box. Default false, so every other node is unchanged.
    unallocated: bool = False
    #: Linux only. A title read from somewhere else in the tree (the image
    #: label, or the frame title) for a surface that has no name of its own.
    #: It is not the node's name: the dead-region check ignores it, so a
    #: childless panel still becomes an opaque region. The pruner copies it
    #: onto that region's title. Empty on macOS, Windows, and the browser.
    surface_name: str = ""
    #: Linux only. A sentence such as ``canvas content isn't accessible``
    #: when a drawing area has no text and no children. Empty when the
    #: toolkit exposed that content. The pruner copies it onto the element.
    #: It does not invent a box for a shape inside the canvas.
    content_note: str = ""
    #: Tooltip sentence split off a multiline accessible name. Empty when
    #: the name was a single line. The snapshot prints it as ``hint=``, not
    #: inside the name.
    hint: str = ""
    #: Key chord split off the end of that tooltip, such as ``T`` or
    #: ``Ctrl+S``. Empty when the name had no parenthetical chord.
    shortcut: str = ""
    #: Linux only. ``activates on double-click`` when a single click on this
    #: GTK outline row only selects it. Empty everywhere else. The click is
    #: not turned into that double-click.
    note: str = ""


class TreeAccessor(Protocol):
    """Read-only view over one a11y tree.

    The live implementation wraps ``AXUIElement`` handles; tests substitute
    dict-backed fixtures. ``node`` is an opaque handle owned by the accessor.
    """

    def read(self, node: object) -> RawNode:
        """Return the attributes of ``node``."""
        ...

    def children(self, node: object) -> Sequence[object]:
        """Return the child handles of ``node`` in document order."""
        ...


@dataclass(frozen=True, slots=True)
class DisplayGeometry:
    """One display plus its origin in the global point space.

    Attributes:
        display: Schema metadata (physical pixels + scale).
        origin: Top-left corner in global points; extent in points derives
            from ``display.width / display.scale`` (resp. height).
    """

    display: Display
    origin: tuple[float, float]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def snapshot(scope: Scope = Scope.WINDOW, *, app: str | None = None) -> Snapshot:
    """Capture a pruned, indexed a11y tree for the given scope.

    Args:
        scope: How much UI to cover. Phase 0 implements `Scope.APP` and
            `Scope.WINDOW` (both require ``app``); DISPLAY/ELEMENT land with
            the multi-app walker.
        app: Bundle id (preferred) or display name of the target application.

    Returns:
        A `Snapshot` with a fresh ``snapshot_id``; all refs inside it are
        scoped to that id. An app with no windows yields an empty snapshot
        under `Scope.WINDOW`.

    Raises:
        ComputerUseError: `ErrorCode.PERMISSION_DENIED_ACCESSIBILITY` when the
            AX TCC grant is missing; `ErrorCode.APP_NOT_FOUND` when ``app``
            matches no running application; `ErrorCode.TIMEOUT` when the
            target app's AX server hangs past the messaging timeout.
    """
    ensure_trusted()
    if scope not in (Scope.APP, Scope.WINDOW):
        raise NotImplementedError(
            f"scope {scope.value!r} lands with the multi-app walker; use APP or WINDOW"
        )
    if app is None:
        raise ValueError("app is required for APP/WINDOW scope")

    ax = _appservices()
    pid, bundle = _find_app(app)
    app_el = ax.AXUIElementCreateApplication(pid)
    # The timeout must be set on the *system-wide* element: per the AX
    # headers, setting it on an ordinary element covers only that element —
    # not the child handles AXChildren returns during the walk, which would
    # each fall back to the ~6s global default on a hung app.
    ax.AXUIElementSetMessagingTimeout(
        ax.AXUIElementCreateSystemWide(), AX_MESSAGING_TIMEOUT_S
    )
    _check_responsive(ax, app_el, bundle)
    accessor = _AXAccessor(ax)
    _maybe_enable_web_a11y(ax, app_el, accessor, pid)
    root = app_el if scope is Scope.APP else _front_window(accessor, app_el)
    return build_snapshot(
        root, accessor, scope=scope, app=bundle, pid=pid, geometry=_display_geometry()
    )


def build_snapshot(
    root: object | None,
    accessor: TreeAccessor,
    *,
    scope: Scope,
    app: str | None,
    pid: int | None,
    geometry: Sequence[DisplayGeometry],
) -> Snapshot:
    """Prune and index one tree into a `Snapshot` (the platform-free core).

    Walks ``root`` through ``accessor``, applies the pruning rules (drop
    zero-size/offscreen/decorative nodes, collapse single-child wrappers, cap
    depth and children-per-node), projects point-space geometry into
    display-qualified physical pixels, and assigns pre-order refs ``e1..eN``.

    Args:
        root: Accessor-owned handle of the subtree root; None produces an
            empty snapshot (e.g. an app with no windows).
        accessor: Tree reader (live AX or a synthetic stand-in).
        scope: Recorded on the snapshot.
        app: Bundle id recorded on the snapshot (anchor for `resolve_ref`).
        pid: Pid recorded on the snapshot.
        geometry: All attached displays; must be non-empty.
    """
    if not geometry:
        raise ValueError("geometry must contain at least one display")
    snapshot_id = f"snap-{next(_EPOCH_COUNTER)}"
    elements: list[Element] = []
    elisions: dict[str, int] = {}
    handles: dict[str, object] = {}
    full_values: dict[str, str] = {}
    if root is not None:
        pruned = _prune_root(root, accessor, tuple(geometry))
        _flatten(pruned, None, (), snapshot_id, elements, elisions, handles, full_values)
    _register_epoch(snapshot_id, elisions, handles, full_values)
    return Snapshot(
        snapshot_id=snapshot_id,
        scope=scope,
        app=app,
        pid=pid,
        created_at=time.time(),
        displays=tuple(g.display for g in geometry),
        elements=tuple(elements),
    )


def merge_snapshots(snaps: Sequence[Snapshot]) -> Snapshot:
    """One display snapshot containing every element of ``snaps``.

    Refs are renumbered ``e1..eN`` in the order given, and the live handles
    move with them so a ref from the merged tree still activates. Each input
    keeps its own epoch. ``app`` and ``pid`` are None, which is the display
    scope contract.
    """
    if not snaps:
        raise ValueError("merge_snapshots needs at least one snapshot")
    snapshot_id = f"snap-{next(_EPOCH_COUNTER)}"
    elements: list[Element] = []
    handles: dict[str, object] = {}
    elisions: dict[str, int] = {}
    full_values: dict[str, str] = {}
    for snap in snaps:
        mapping: dict[str, str] = {}
        old_handles = _HANDLES.get(snap.snapshot_id, {})
        old_values = _FULL_VALUES.get(snap.snapshot_id, {})
        old_elisions = _EPOCHS.get(snap.snapshot_id, {})
        for el in snap.elements:
            new_ref = f"e{len(elements) + 1}"
            mapping[el.ref] = new_ref
            parent = mapping[el.parent] if el.parent and el.parent in mapping else None
            elements.append(
                dataclasses.replace(el, ref=new_ref, snapshot_id=snapshot_id, parent=parent)
            )
            if el.ref in old_handles:
                handles[new_ref] = old_handles[el.ref]
            if el.ref in old_values:
                full_values[new_ref] = old_values[el.ref]
        for parent_ref, count in old_elisions.items():
            mapped = mapping.get(parent_ref)
            if mapped is not None:
                elisions[mapped] = count
    _register_epoch(snapshot_id, elisions, handles, full_values)
    return Snapshot(
        snapshot_id=snapshot_id,
        scope=Scope.DISPLAY,
        app=None,
        pid=None,
        created_at=time.time(),
        displays=snaps[0].displays,
        elements=tuple(elements),
    )


def merge_window_snapshots(
    snaps: Sequence[Snapshot],
    *,
    scope: Scope,
    app: str | None,
    pid: int | None,
) -> Snapshot:
    """One app snapshot whose roots are ``snaps``, in the order given.

    The first snapshot is the active window. Refs are renumbered and the
    live handles move with them, so a ref in a window that is not active
    still activates. ``scope``, ``app``, and ``pid`` stay the caller's.
    Two or more windows mark the first root ``(active)`` when rendered.
    One snapshot is returned unchanged, with no active mark.
    """
    if not snaps:
        raise ValueError("merge_window_snapshots needs at least one snapshot")
    if len(snaps) == 1:
        return snaps[0]
    snapshot_id = f"snap-{next(_EPOCH_COUNTER)}"
    elements: list[Element] = []
    handles: dict[str, object] = {}
    elisions: dict[str, int] = {}
    full_values: dict[str, str] = {}
    for snap in snaps:
        mapping: dict[str, str] = {}
        old_handles = _HANDLES.get(snap.snapshot_id, {})
        old_values = _FULL_VALUES.get(snap.snapshot_id, {})
        old_elisions = _EPOCHS.get(snap.snapshot_id, {})
        for el in snap.elements:
            new_ref = f"e{len(elements) + 1}"
            mapping[el.ref] = new_ref
            parent = mapping[el.parent] if el.parent and el.parent in mapping else None
            elements.append(
                dataclasses.replace(el, ref=new_ref, snapshot_id=snapshot_id, parent=parent)
            )
            if el.ref in old_handles:
                handles[new_ref] = old_handles[el.ref]
            if el.ref in old_values:
                full_values[new_ref] = old_values[el.ref]
        for parent_ref, count in old_elisions.items():
            mapped = mapping.get(parent_ref)
            if mapped is not None:
                elisions[mapped] = count
    _register_epoch(snapshot_id, elisions, handles, full_values)
    if elements:
        _note_active_root(snapshot_id, elements[0].ref)
    return Snapshot(
        snapshot_id=snapshot_id,
        scope=scope,
        app=app,
        pid=pid,
        created_at=time.time(),
        displays=snaps[0].displays,
        elements=tuple(elements),
    )


def resolve_ref(snap: Snapshot, ref: str, *, live: Snapshot | None = None) -> Element:
    """Re-resolve a snapshot-scoped ref against the *live* tree.

    Uses the anchor attributes on `Element` (role, title, path, bounds
    proximity) because AX refs are not stable across tree mutations.

    Args:
        snap: The snapshot (epoch) that issued ``ref``.
        ref: The element ref to re-resolve, e.g. ``"e14"``.
        live: Current-state snapshot to resolve against; defaults to a fresh
            `snapshot` of the same scope/app. Injectable for tests and for
            callers that already re-observed.

    Returns:
        The re-resolved `Element` with current bounds/state (carrying the
        *live* snapshot's id).

    Raises:
        KeyError: if ``ref`` was never part of ``snap`` (see
            `Snapshot.element`).
        ComputerUseError: `ErrorCode.STALE_REF` when the element no longer
            exists or the anchors no longer match unambiguously — the caller
            should re-observe.
    """
    if live is None:
        live = snapshot(snap.scope, app=snap.app)
    return rematch_ref(snap, ref, live)


def normalize_document_title(title: str) -> str:
    """Window title with one leading modified marker removed.

    GTK editors prefix ``*`` when the buffer is dirty
    (``*/tmp/a.txt - Mousepad``). The path is the document. The marker is not,
    so a later action on the same file still resolves after the first edit.
    """
    text = " ".join(str(title or "").split())
    if text.startswith("*"):
        text = text[1:].lstrip()
    return text


def _file_document(document_id: str | None) -> str | None:
    """The file path carried in a document id, or None.

    GTK editors put the path in the window title (``/tmp/a.txt - Mousepad``).
    A browser title (``Form Probe - Google Chrome``) has no path. Comparing
    those titles treats a tab switch, or a title that flickers, as a different
    document and hides the not-showing result the background tab should get.
    """
    if not document_id or not document_id.startswith("title:"):
        return None
    text = document_id[len("title:"):]
    if "/" not in text and "\\" not in text:
        return None
    return text


def _bound_to_same_target(anchor: Element, el: Element) -> bool:
    """Whether ``el`` is the same process instance and file as ``anchor``.

    A side that did not record an id does not fail the check. A different
    process instance does. A different file path does. Two browser window
    titles do not: the title follows the active tab.
    """
    if not _same_instance(anchor, el):
        return False
    anchor_file = _file_document(anchor.document_id)
    live_file = _file_document(el.document_id)
    if anchor_file and live_file and anchor_file != live_file:
        return False
    return True


def _identity_mismatch(anchor: Element, live: Snapshot) -> str | None:
    """``app_restarted`` or ``document_changed`` when no live node shares the binding.

    None when the anchor recorded no identity, or the live tree did not, so
    older snapshots and non-Linux backends keep the role/title matcher.
    """
    if anchor.instance_id:
        seen = {el.instance_id for el in live.elements if el.instance_id}
        if seen and anchor.instance_id not in seen:
            return "app_restarted"
    anchor_file = _file_document(anchor.document_id)
    if anchor_file:
        files = {
            _file_document(el.document_id)
            for el in live.elements
            if _same_instance(anchor, el) and _file_document(el.document_id)
        }
        if files and anchor_file not in files:
            return "document_changed"
    return None


def _same_instance(anchor: Element, el: Element) -> bool:
    """Whether ``el`` is the same process instance as ``anchor``.

    A side that did not record an instance id does not fail the check.
    """
    if anchor.instance_id and el.instance_id and anchor.instance_id != el.instance_id:
        return False
    return True


def _raise_identity_stale(
    snap: Snapshot, ref: str, anchor: Element, live: Snapshot, reason: str,
) -> None:
    """``stale_ref`` for a process or document that is not the one the ref names.

    No candidates. The elements in ``live`` belong to the replacement, and
    offering them would point the next action at the wrong document.
    """
    if reason == "app_restarted":
        message = (
            f"{ref} ({anchor.role} {anchor.title!r}) was issued by an application "
            "instance that is no longer running. The process was quit or relaunched; "
            "re-snapshot before acting. Nothing was changed."
        )
    else:
        message = (
            f"{ref} ({anchor.role} {anchor.title!r}) belongs to a different window "
            "or document than the one now showing. Re-snapshot before acting. "
            "Nothing was changed."
        )
    raise ComputerUseError(
        ErrorCode.STALE_REF,
        message,
        detail={
            "ref": ref,
            "snapshot_id": snap.snapshot_id,
            "live_snapshot_id": live.snapshot_id,
            "reason": reason,
            "anchor": {
                "role": anchor.role,
                "title": anchor.title,
                "path": list(anchor.path),
            },
            "candidates": [],
            "next": ["ref"],
            "instance_id": anchor.instance_id,
            "document_id": anchor.document_id,
        },
    )


def rematch_ref(snap: Snapshot, ref: str, live: Snapshot) -> Element:
    """Re-resolve ``ref`` (issued by ``snap``) against an already-captured
    ``live`` snapshot via the shared anchor matcher — the backend-agnostic core
    of every driver's ``resolve_ref``.

    Each `Driver` captures ``live`` through its own backend (macOS via
    `observe.snapshot`, Linux/browser via ``driver.snapshot``) and calls this, so
    the match-or-raise logic and the STALE_REF payload live in exactly one place.

    Raises:
        KeyError: if ``ref`` was never part of ``snap`` (see `Snapshot.element`).
        ComputerUseError: `ErrorCode.STALE_REF` when the element no longer exists,
            the anchors no longer match unambiguously, or the live tree is a
            different process instance or document. An instance or document
            mismatch carries no candidates: those elements are the replacement,
            and the action must not use them.
    """
    anchor = snap.element(ref)
    mismatch = _identity_mismatch(anchor, live)
    if mismatch is not None:
        _raise_identity_stale(snap, ref, anchor, live, mismatch)
    match, reason = _match_anchor(anchor, live)
    if match is None:
        message = f"{ref} ({anchor.role} {anchor.title!r}) no longer resolves; re-observe"
        candidates = stale_ref_candidates(anchor, live)
        if reason == "title_changed":
            occupant = next((c for c in candidates if c.get("at_old_position")), None)
            where = (f"; the {anchor.role} at that position is now {occupant['title']!r}"
                     if occupant else "")
            message = (f"{ref} ({anchor.role} {anchor.title!r}) is no longer in the tree under "
                       f"that title{where}. The list may have reordered: use find(text=...) or "
                       "scroll_to_find to locate it again rather than clicking the slot")
        raise ComputerUseError(
            ErrorCode.STALE_REF,
            message,
            detail={
                "ref": ref,
                "snapshot_id": snap.snapshot_id,
                "live_snapshot_id": live.snapshot_id,
                "reason": reason,
                "anchor": {
                    "role": anchor.role,
                    "title": anchor.title,
                    "path": list(anchor.path),
                },
                # near-misses so the agent can retry a likely ref, no re-snapshot
                "candidates": candidates,
            },
        )
    return match


#: AX actions that activate an element, in preference order. `AXPress` covers
#: buttons/links/checkboxes; `AXConfirm`/`AXOpen`/`AXPick` cover the stragglers
#: (default buttons, Finder items, pop-ups).
_ACTIVATE_ACTIONS = ("AXPress", "AXConfirm", "AXOpen", "AXPick")


def press_element(element: Element) -> bool:
    """Activate ``element`` through the accessibility API — no cursor movement.

    This is the payoff of being accessibility-first: because the element is a
    real AX object (not a guessed coordinate), it can be pressed or focused
    directly, so the agent never warps the user's physical mouse or steals the
    pointer mid-task. The click tool tries this first and falls back to a
    synthetic mouse click only when it returns False.

    ``element`` must come from a live snapshot (e.g. `resolve_ref`'s result) so
    its handle is still registered.

    Returns:
        True when the element was pressed (or, for a plain editable field,
        focused) via AX. False — meaning *fall back to a synthetic click* —
        when the element is secure, no live handle is registered for its epoch,
        it exposes no activate action, or the AX call reported an error.
    """
    if element.secure:
        return False  # secure fields require human handoff; let the click path raise
    handle = ax_handle_for(element.snapshot_id, element.ref)
    if handle is None:
        return False
    names = set(_copy_action_names(handle))
    for action in _ACTIVATE_ACTIONS:
        if action in names:
            return _perform_action(handle, action)
    if element.editable:  # a text field with no press action: focus it without a click
        return _set_focused(handle)
    return False


def scroll_into_view(element: Element) -> bool:
    """Scroll ``element`` into view through the AX API — no cursor movement.

    A synthetic scroll-wheel event repositions the physical pointer to the
    scroll location (macOS routes wheel events by where the cursor lands), so
    delta scrolling is inherently intrusive. ``AXScrollToVisible`` is the one
    cursor-free scroll: it asks the element's own scroll ancestors to reveal
    it. It's a *reveal* operation, not a by-N-lines delta — the click/scroll
    tool exposes it as an opt-in (``into_view``) alongside the wheel path.

    ``element`` must come from a live snapshot so its handle is registered.

    Returns:
        True if the AX scroll-to-visible succeeded; False when no handle is
        registered or the element doesn't support the action — the caller
        falls back to a synthetic wheel scroll.
    """
    handle = ax_handle_for(element.snapshot_id, element.ref)
    if handle is None:
        return False
    return _perform_action(handle, "AXScrollToVisible")


def element_pid(element: Element) -> int | None:
    """The process owning ``element``'s live handle, or None (no handle, off macOS)."""
    handle = ax_handle_for(element.snapshot_id, element.ref)
    if handle is None:
        return None
    try:
        err, pid = _appservices().AXUIElementGetPid(handle, None)
    except Exception:
        return None
    return int(pid) if err == 0 and pid else None


def set_value(element: Element, value: str) -> bool:
    """Set ``element``'s AXValue directly (no synthesized typing) — the macOS
    intent-verb path. False when the element is secure, no live handle is
    registered, or the AX call errors (caller falls back to focus + type)."""
    if element.secure:
        return False
    handle = ax_handle_for(element.snapshot_id, element.ref)
    if handle is None:
        return False
    try:
        ok = _appservices().AXUIElementSetAttributeValue(handle, "AXValue", value) == 0
    except Exception:
        return False
    if ok:
        try:  # a following key chord (Return in an address bar) should land here (#12)
            _appservices().AXUIElementSetAttributeValue(handle, "AXFocused", True)
        except Exception:
            pass
    return ok


def _copy_action_names(handle: object) -> tuple[str, ...]:
    """The AX action names ``handle`` supports (empty tuple on any AX error).

    Broad except: pyobjc raises assorted bridging errors and a live app's AX
    server can fail arbitrarily; the safe answer is "no known action", which
    makes `press_element` fall back to a synthetic click rather than crash.
    """
    try:
        err, names = _appservices().AXUIElementCopyActionNames(handle, None)
    except Exception:
        return ()
    if err != 0 or not names:
        return ()
    return tuple(str(n) for n in names)


def _perform_action(handle: object, action: str) -> bool:
    """Perform one AX action; True iff the AX API reports success (err 0)."""
    try:
        return _appservices().AXUIElementPerformAction(handle, action) == 0
    except Exception:
        return False


def _set_focused(handle: object) -> bool:
    """Give ``handle`` keyboard focus via AX; True iff the AX API reports success."""
    try:
        return _appservices().AXUIElementSetAttributeValue(handle, "AXFocused", True) == 0
    except Exception:
        return False


def focus_for_type(element: Element) -> bool:
    """Focus ``element`` and return True only when AXFocused reads back true.

    Does not type. False means the caller must not send keystrokes: the
    set failed, the handle is gone, or the read-back does not show focus.
    """
    if element.secure:
        return False
    handle = ax_handle_for(element.snapshot_id, element.ref)
    if handle is None:
        return False
    if not _set_focused(handle):
        return False
    try:
        err, value = _appservices().AXUIElementCopyAttributeValue(handle, "AXFocused", None)
    except Exception:
        return False
    return err == 0 and bool(value)


def render_text(
    snap: Snapshot,
    *,
    mode: str = "full",
    budget: int | None = None,
    include_bounds: bool = False,
) -> str:
    """Render ``snap`` as compact indented text for LLM consumption.

    ``mode="full"`` (default): one line per element: ``ref role "title"
    ="value" (flags)``, two-space indentation, geometry only on roots. Children
    elided by the depth or fan-out caps show as ``… N more`` markers when
    ``snap`` was produced by this module (foreign snapshots render without
    markers).

    ``mode="interactive"``: the same snapshot rendered down to the elements an
    agent can act on (see `is_interactive`) plus the containers that keep them
    apart (see `interactive_view`); each kept container gets one folded
    ``text:`` line holding the static text beneath it. The refs are the
    snapshot's own refs, so they re-resolve exactly like full-mode refs and an
    agent can switch views mid-task. Geometry is omitted unless
    ``include_bounds`` is set (which then prints bounds on every kept line).

    ``budget`` is an approximate token cap (4 chars per token, the cu-meter
    basis): the header always survives, lines are kept in order until the next
    one would overflow, and a final marker states how many element lines were
    omitted so the agent knows to use ``find``/``scroll_to_find`` or raise it.
    Two or more top-level windows share that budget. Each window is offered
    an equal part of what remains, and a window that does not fit ends with
    ``… N more`` instead of the single-window tail marker. Deterministic for
    a given snapshot.
    """
    if mode not in RENDER_MODES:
        raise ValueError(f"mode must be one of {RENDER_MODES}")
    if mode == "interactive":
        entries = _interactive_entries(snap, include_bounds)
    else:
        entries = _full_entries(snap, include_bounds)
    if budget is not None and _several_windows(entries):
        return "\n".join(_apply_window_budget(entries, budget))
    return "\n".join(_apply_budget(entries, budget))


def _children_map(snap: Snapshot) -> dict[str | None, list[Element]]:
    children: dict[str | None, list[Element]] = {}
    for el in snap.elements:
        children.setdefault(el.parent, []).append(el)
    return children


def _canvas_note_lines(snap: Snapshot) -> list[tuple[str, bool]]:
    """One line per distinct canvas note, so a miss is not silent.

    The same sentence is also on the canvas element. A snapshot with no
    such canvas contributes nothing.
    """
    seen: list[str] = []
    for el in snap.elements:
        note = el.content_note
        if note and note not in seen:
            seen.append(note)
    return [(note, False) for note in seen]


def _full_entries(snap: Snapshot, include_bounds: bool) -> list[tuple[str, bool]]:
    """(line, is_element_line) pairs for the full view."""
    elisions = _EPOCHS.get(snap.snapshot_id, {})
    children = _children_map(snap)
    bounds: bool | None = True if include_bounds else None
    entries = [
        (f"[{snap.snapshot_id}] {snap.app or 'display'} ({snap.scope.value})", False),
        *_canvas_note_lines(snap),
    ]

    def emit(el: Element, depth: int) -> None:
        entries.append(("  " * depth + _render_line(el, bounds=bounds), True))
        for child in children.get(el.ref, ()):
            emit(child, depth + 1)
        elided = elisions.get(el.ref, 0)
        if elided:
            entries.append(("  " * (depth + 1) + f"… {elided} more", False))

    for root in children.get(None, ()):
        emit(root, 1)
    return entries


def is_interactive(el: Element) -> bool:
    """Whether the interactive view keeps ``el`` on its own line.

    True for elements that take input (clickable or editable, disabled ones
    included so the agent sees what is greyed out), carry actionable state
    (checked / expanded / selected / focused), or have a role an agent selects
    even when the platform exposes no press action on it (rows, tabs, sliders).
    `interactive_view` refines the role-only case: a row that merely contains
    links or buttons is layout, not a target, and is folded instead.
    """
    return _takes_input(el) or el.role in _INTERACTIVE_ROLES


def _takes_input(el: Element) -> bool:
    return (
        el.clickable
        or el.editable
        or el.checked is not None
        or el.expanded is not None
        or el.selected
        or el.focused
    )


#: Grid geometry: a titled row/cell is table layout, not a semantic group, so it
#: never earns a structural line of its own in the interactive view.
_GRID_ROLES = frozenset({"AXRow", "AXCell", "AXColumn"})


def _is_structural(el: Element) -> bool:
    if el.parent is None or el.role in _STRUCTURAL_ROLES:
        return True
    return bool(el.title) and el.role not in _GRID_ROLES


def interactive_view(snap: Snapshot) -> tuple[Element, ...]:
    """The elements the interactive view shows, in the snapshot's pre-order.

    Every `is_interactive` element, plus the ancestors needed to keep them
    apart: roots, structural containers (windows, dialogs, menus, toolbars, tab
    groups) and any titled container. Untitled wrapper groups/lists/scroll
    areas between them are skipped; their static text folds into the nearest
    kept ancestor's ``text:`` line. Rows/tabs/sliders kept for their role alone
    are dropped when an interactive element lives inside them (a layout-table
    row holding links is not itself a target). Pure and platform-free.
    """
    by_ref = {el.ref: el for el in snap.elements}
    # Roles kept only for what they are (rows, tabs, sliders) count as targets
    # only when nothing inside them takes input; propagate that fact upward
    # over the reversed pre-order so parents see their descendants first.
    has_input_below: set[str] = set()
    for el in reversed(snap.elements):
        if el.parent is not None and (_takes_input(el) or el.ref in has_input_below):
            has_input_below.add(el.parent)
    keep: set[str] = {el.ref for el in snap.elements if el.parent is None}
    for el in snap.elements:
        if not (_takes_input(el) or (el.role in _INTERACTIVE_ROLES and el.ref not in has_input_below)):
            continue
        keep.add(el.ref)
        parent = el.parent
        while parent is not None and parent not in keep:
            node = by_ref[parent]
            if _is_structural(node):
                keep.add(parent)
            parent = node.parent
    return tuple(el for el in snap.elements if el.ref in keep)


def _interactive_entries(snap: Snapshot, include_bounds: bool) -> list[tuple[str, bool]]:
    """(line, is_element_line) pairs for the interactive view."""
    elisions = _EPOCHS.get(snap.snapshot_id, {})
    children = _children_map(snap)
    by_ref = {el.ref: el for el in snap.elements}
    kept = interactive_view(snap)
    kept_refs = {el.ref for el in kept}
    entries = [
        (f"[{snap.snapshot_id}] {snap.app or 'display'} ({snap.scope.value}) interactive", False),
        *_canvas_note_lines(snap),
    ]

    def folded_text(el: Element) -> tuple[list[str], int, int]:
        """(text items, total text items, elided children) of the dropped
        descendants of ``el``, stopping at kept elements. The elision counts of
        skipped wrappers roll up so the agent still learns that a list holds
        more rows than the snapshot walked."""
        items: list[str] = []
        total = 0
        elided = 0
        stack = list(reversed(children.get(el.ref, ())))
        while stack:
            node = stack.pop()
            if node.ref in kept_refs:
                continue
            text = _folded_label(node)
            # A folded container's elided children are hidden rows the agent may
            # need to scroll to; a folded text node's are its own text fragments.
            if not text:
                elided += elisions.get(node.ref, 0)
            # blank text, the container's own title, and a run of duplicates (a
            # cell repeating its text node) carry nothing worth a token
            if text and text != el.title and (not items or items[-1] != text):
                total += 1
                if len(items) < _COLLAPSED_TEXT_ITEMS:
                    items.append(_compact_value(text))
            stack.extend(reversed(children.get(node.ref, ())))
        return items, total, elided

    depth_of: dict[str, int] = {}
    for el in kept:
        parent = el.parent
        while parent is not None and parent not in kept_refs:
            parent = by_ref[parent].parent
        depth = depth_of[parent] + 1 if parent is not None else 1
        depth_of[el.ref] = depth
        entries.append(("  " * depth + _render_line(el, bounds=include_bounds, compact=True), True))
        items, total, folded_elided = folded_text(el)
        if items:
            joined = _clip(" | ".join(items), _COLLAPSED_TEXT_CHARS)
            extra = total - len(items)
            suffix = f" (+{extra})" if extra > 0 else ""
            entries.append(("  " * (depth + 1) + f'text: "{joined}"{suffix}', False))
        # Elision markers stay on containers; an input-taking element's elided
        # children are the text it already shows as its title or value.
        elided = 0 if _takes_input(el) else elisions.get(el.ref, 0) + folded_elided
        if elided:
            entries.append(("  " * (depth + 1) + f"… {elided} more", False))
    hidden = len(snap.elements) - len(kept)
    if hidden:
        entries.append((f"… {hidden} static elements folded; mode='full' or find lists them", False))
    return entries


def _folded_label(el: Element) -> str:
    """The text a folded (non-interactive) element contributes to its container's
    ``text:`` line: a labelled value reads ``label: value``, otherwise whichever of
    title/value the platform filled in. Secure fields carry no value by
    construction, so nothing secret can fold in."""
    title = el.title.strip()
    value = "" if el.value is None else str(el.value).strip()
    if title and value and title != value:
        return f"{title}: {value}"
    return title or value


def _apply_budget(entries: Sequence[tuple[str, bool]], budget: int | None) -> list[str]:
    """Cut ``entries`` to roughly ``budget`` tokens (4 chars each); the header
    always survives and a trailing marker reports the omitted element lines."""
    if budget is None:
        return [line for line, _ in entries]
    if budget <= 0:
        raise ValueError("budget must be a positive token count")
    limit = budget * 4
    out = [entries[0][0]]
    used = len(out[0])
    omitted = 0
    cut = False
    for line, is_element in entries[1:]:
        if not cut and used + 1 + len(line) <= limit:
            out.append(line)
            used += 1 + len(line)
        else:
            cut = True
            omitted += int(is_element)
    if cut:
        out.append(
            f"… truncated at ~{budget} tokens: {omitted} element lines omitted; "
            "use find / scroll_to_find for the rest, or raise budget"
        )
    return out


def _several_windows(entries: Sequence[tuple[str, bool]]) -> bool:
    """True when ``entries`` has two or more depth-1 roots."""
    roots = 0
    for line, _is_element in entries[1:]:
        if len(line) - len(line.lstrip(" ")) == 2:
            roots += 1
            if roots >= 2:
                return True
    return False


def _split_window_entries(
    entries: Sequence[tuple[str, bool]],
) -> tuple[tuple[str, bool], list[list[tuple[str, bool]]], list[tuple[str, bool]]]:
    """Header, one group per top-level window, and unindented footer lines."""
    header = entries[0]
    groups: list[list[tuple[str, bool]]] = []
    footer: list[tuple[str, bool]] = []
    current: list[tuple[str, bool]] | None = None
    for item in entries[1:]:
        indent = len(item[0]) - len(item[0].lstrip(" "))
        if indent == 0:
            footer.append(item)
            continue
        if indent == 2:
            if current:
                groups.append(current)
            current = [item]
            continue
        if current is None:
            current = [item]
        else:
            current.append(item)
    if current:
        groups.append(current)
    return header, groups, footer


def _apply_window_budget(entries: Sequence[tuple[str, bool]], budget: int) -> list[str]:
    """Split ``budget`` across top-level windows and fold each overflow.

    Each window is offered ``remaining / windows_left`` characters, with the
    same newline accounting as `_apply_budget`. Characters a window does not
    use stay in the pool for the windows after it. A window that does not
    fit ends with ``… N more``, counting the element lines of that window
    that were left out. One window, or no budget, stays on `_apply_budget`.
    """
    if budget <= 0:
        raise ValueError("budget must be a positive token count")
    header, groups, footer = _split_window_entries(entries)
    if len(groups) < 2:
        return _apply_budget(entries, budget)
    limit = budget * 4
    out = [header[0]]
    used = len(out[0])
    remaining = len(groups)
    for group in groups:
        share = max(0, limit - used) // remaining if remaining else 0
        cap = used + share
        omitted = 0
        kept_any = False
        for line, is_element in group:
            if used + 1 + len(line) <= cap:
                out.append(line)
                used += 1 + len(line)
                kept_any = True
            else:
                omitted += int(is_element)
        if omitted:
            marker = ("    " if kept_any else "  ") + f"… {omitted} more"
            if used + 1 + len(marker) <= limit:
                out.append(marker)
                used += 1 + len(marker)
        remaining -= 1
    for line, _is_element in footer:
        if used + 1 + len(line) <= limit:
            out.append(line)
            used += 1 + len(line)
    return out


def expand_start_index(names: Sequence[str], shown: set[str], cursor: int = 0) -> int:
    """Child index where the next expand page starts.

    A positive ``cursor`` is that index exactly: the next page resumes where
    the previous page ended, even when the snapshot also shows a later name.
    With no cursor, the start is the first child whose name is not already
    listed. A later listed name does not skip the hole in front of it, and
    an empty name ends the prefix (it is not proof the child was shown).
    """
    if cursor > 0:
        return int(cursor)
    shown_folded = {item.casefold() for item in shown if item}
    for index, name in enumerate(names):
        text = (name or "").strip()
        if text and text.casefold() in shown_folded:
            continue
        return index
    return len(names)


def render_subtree(snap: Snapshot, ref: str) -> str:
    """``ref`` and the descendants this snapshot kept, plus ``… N more``.

    The lines use the same shape as ``render_text``. The header names the
    ref so a caller can tell this page from a whole-window listing.
    """
    children = _children_map(snap)
    root = next((el for el in snap.elements if el.ref == ref), None)
    if root is None:
        return f"[{snap.snapshot_id}] {ref} is not in this snapshot"
    elisions = _EPOCHS.get(snap.snapshot_id, {})
    lines = [f"[{snap.snapshot_id}] {snap.app or 'display'}: {ref}"]

    def emit(el: Element, depth: int) -> None:
        lines.append("  " * depth + _render_line(el))
        for child in children.get(el.ref, ()):
            emit(child, depth + 1)
        hidden = elisions.get(el.ref, 0)
        if hidden:
            lines.append("  " * (depth + 1) + f"… {hidden} more")

    emit(root, 0)
    return "\n".join(lines)


def folded_children(snap: Snapshot) -> tuple[tuple[str, int], ...]:
    """``(ref, count)`` for nodes whose children ``render_text`` hides as ``… N more``.

    The count is the elision recorded for this snapshot epoch. An empty tuple
    means nothing was folded, or the epoch has already left the registry.
    """
    counts = _EPOCHS.get(snap.snapshot_id, {})
    if not counts:
        return ()
    return tuple(
        (element.ref, counts[element.ref])
        for element in snap.elements
        if counts.get(element.ref, 0)
    )


def estimate_tokens(snap: Snapshot, *, mode: str = "full") -> int:
    """Estimate the LLM token footprint of ``snap``'s serialized form.

    Used to enforce the per-snapshot budget (~1k tokens) and to publish the
    per-app coverage table from Phase 0. Heuristic: ``chars / 4`` over the
    `render_text` form (in ``mode``), rounded up.
    """
    return (len(render_text(snap, mode=mode)) + 3) // 4


def interactive_count(snap: Snapshot) -> int:
    """How many elements the model can actually act on (clickable or editable).

    Zero is the a11y→vision handoff signal: a custom-drawn app (Telegram, many
    games, some Electron before `AXManualAccessibility`) exposes a shell with
    no actionable refs, so the caller should fall back to screenshot+coordinates.
    """
    return sum(1 for el in snap.elements if el.clickable or el.editable)


def find_elements(
    snap: Snapshot,
    *,
    text: str | None = None,
    role: str | None = None,
    editable: bool | None = None,
    clickable: bool | None = None,
) -> tuple[Element, ...]:
    """Filter ``snap``'s elements by text/role/capability — the query behind the
    `find` tool. Platform-free (operates on the canonical snapshot, so it works
    identically on every backend).

    Args:
        text: case-insensitive substring matched against an element's title or
            its full value (either may contain it). ``Element.value`` is capped
            at `_MAX_VALUE_CHARS` for the text the model reads; a snapshot built
            here is still searched past that cap. A VTE terminal's snapshot
            value is the on-screen text, or the tail when that text is longer,
            with a leading ellipsis when older output was omitted. Find still
            searches the whole buffer, including text that has scrolled above
            the view.
        role: case-insensitive substring of the role, with an optional ``AX``
            prefix ignored, so ``"button"`` matches ``AXButton``/``AXMenuButton``
            and ``"AXTextField"`` matches exactly.
        editable / clickable: keep only elements whose flag equals the given
            boolean.

    Returns the matches in the snapshot's pre-order, so refs read top-to-bottom.
    """
    needle = text.lower() if text else None
    role_needle = role.lower().removeprefix("ax") if role else None
    full_values = _FULL_VALUES.get(snap.snapshot_id, {})
    out: list[Element] = []
    for el in snap.elements:
        if needle is not None:
            # The stored value is clipped for the model; the index keeps the
            # field's real text so a token past the cap still matches.
            value = full_values.get(el.ref, el.value or "")
            # NBSP is a space. The container value already has each U+FFFC
            # replaced by the embedded child's text, so the sentence matches.
            haystack = f"{el.title} {value} {el.hint} {el.shortcut}".replace("\u00a0", " ").lower()
            if needle.replace("\u00a0", " ") not in haystack:
                continue
        if role_needle is not None and role_needle not in el.role.lower().removeprefix("ax"):
            continue
        if editable is not None and el.editable is not editable:
            continue
        if clickable is not None and el.clickable is not clickable:
            continue
        out.append(el)
    return tuple(out)


def render_matches(
    snap: Snapshot, matches: Sequence[Element], *, text: str | None = None,
) -> str:
    """Render `find_elements` results as compact lines — one per match with its
    ref, role, title, value, flags, and bounds (unlike the tree render, bounds
    show on every match since results are a flat list, not a nested tree).

    ``text`` is the substring ``find`` searched for. A VTE match that sits only
    in the buffer, not in the on-screen value, is marked above the view or
    off-screen. A snapshot whose canvas has no accessible content says so
    when nothing matched, instead of a bare miss.
    """
    if not matches:
        note = next((el.content_note for el in snap.elements if el.content_note), "")
        suffix = f" ({note})" if note else ""
        return f"[{snap.snapshot_id}] {snap.app or 'display'}: no elements match{suffix}"
    lines = [f"[{snap.snapshot_id}] {snap.app or 'display'}: {len(matches)} match(es)"]
    for el in matches:
        line = _render_line(el)
        b = el.bounds
        if b is not None and el.parent is not None:  # _render_line prints bounds only for roots
            line += f" [{b.width}x{b.height} @{b.display_id}:{b.x},{b.y}]"
        note = _terminal_match_note(snap, el, text)
        if note:
            line += f" ({note})"
        elif el.bounds is None:
            # The toolkit exposed the node and did not give it an on-screen
            # box. The line does not invent one.
            line += " (off-screen)"
        lines.append("  " + line)
    return "\n".join(lines)


def _terminal_match_note(snap: Snapshot, el: Element, text: str | None) -> str | None:
    """Where a terminal match sits when it is not in the snapshot value.

    ``above the view`` is scrollback (or the omitted head of a long buffer).
    ``off-screen`` is a match the snapshot value does not show and that is
    not above that value. None when the match is on screen, or when ``text``
    was not the query.
    """
    if not text or el.subrole != "terminal":
        return None

    def fold(value: str) -> str:
        return value.replace("\u00a0", " ").lower()

    needle = fold(text)
    if not needle:
        return None
    if needle in fold(el.title or ""):
        return None
    shown = el.value or ""
    if needle in fold(shown):
        return None
    full = _FULL_VALUES.get(snap.snapshot_id, {}).get(el.ref, shown)
    folded_full = fold(str(full))
    at = folded_full.find(needle)
    if at < 0:
        return None
    body = shown[1:] if shown.startswith("…") else shown
    folded_body = fold(body)
    where = folded_full.rfind(folded_body) if folded_body else len(folded_full)
    if where >= 0 and at + len(needle) <= where:
        return "above the view"
    return "off-screen"


# ---------------------------------------------------------------------------
# Snapshot diffing — the non-accumulation token win (send deltas, not the tree)
# ---------------------------------------------------------------------------


def _identity(el: Element) -> tuple:
    """A cross-epoch identity key for an element. A developer-assigned stable_id
    is authoritative; otherwise fall back to (role, title, path) — the same
    signal `_match_anchor` trusts, minus the live bounds that a diff expects to
    move."""
    if el.stable_id:
        return ("id", el.stable_id, el.role)
    return ("rtp", el.role, el.title, el.path)


_DIFF_ATTRS = ("value", "title", "enabled", "focused", "checked", "selected", "expanded")


def _state_changes(a: Element, b: Element) -> dict[str, tuple]:
    """The per-attribute changes between two elements sharing an identity."""
    changes: dict[str, tuple] = {}
    for attr in _DIFF_ATTRS:
        av, bv = getattr(a, attr), getattr(b, attr)
        if av != bv:
            changes[attr] = (av, bv)
    ab, bb = a.bounds, b.bounds
    if ab is None or bb is None:
        if (ab is None) != (bb is None):
            changes["bounds"] = (
                None if ab is None else (ab.x, ab.y),
                None if bb is None else (bb.x, bb.y),
            )
    elif (ab.x, ab.y, ab.width, ab.height) != (bb.x, bb.y, bb.width, bb.height):
        changes["bounds"] = ((ab.x, ab.y), (bb.x, bb.y))
    return changes


@dataclass(frozen=True, slots=True)
class SnapshotDiff:
    """What changed between two snapshots of the same app. Refs on ``added`` /
    ``changed`` are the NEW snapshot's (act on them); ``removed`` carries the old
    element only to report what's gone."""

    old_id: str
    new_id: str
    added: tuple[Element, ...]
    removed: tuple[Element, ...]
    changed: tuple[tuple[Element, Element, dict], ...]  # (old, new, changes)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


def diff_snapshots(old: Snapshot, new: Snapshot) -> SnapshotDiff:
    """Structured delta between two snapshots (pure; platform-free). Elements are
    matched by `_identity` (stable_id, else role/title/path); an element only in
    ``new`` is *added*, only in ``old`` is *removed*, in both with a differing
    state signature is *changed*. Unchanged elements are omitted — that omission
    is the whole point: after an action the agent re-observes deltas, not the
    entire (largely identical) tree, so context stops accumulating."""
    old_by: dict[tuple, Element] = {}
    for el in old.elements:
        old_by.setdefault(_identity(el), el)
    new_by: dict[tuple, Element] = {}
    for el in new.elements:
        new_by.setdefault(_identity(el), el)
    added = tuple(el for k, el in new_by.items() if k not in old_by)
    removed = tuple(el for k, el in old_by.items() if k not in new_by)
    changed = tuple(
        (old_by[k], nel, ch)
        for k, nel in new_by.items()
        if k in old_by and (ch := _state_changes(old_by[k], nel))
    )
    return SnapshotDiff(old.snapshot_id, new.snapshot_id, added, removed, changed)


def render_diff(diff: SnapshotDiff, *, mode: str = "full", budget: int | None = None) -> str:
    """Render a `SnapshotDiff` as compact text: a header line then +added,
    -removed, ~changed. Empty diff says so explicitly (a real, useful signal:
    'the action produced no observable tree change').

    ``mode="interactive"`` keeps the +/-/~ lines of `is_interactive` elements
    only; static-text changes fold into one ``~ text:`` line (a status label
    flipping to "Saved" still shows) and added/removed static elements are
    reported as counts. The header counts always describe the whole diff.
    ``budget`` truncates like `render_text`.
    """
    if mode not in RENDER_MODES:
        raise ValueError(f"mode must be one of {RENDER_MODES}")
    head = f"[{diff.new_id} ← {diff.old_id}] +{len(diff.added)} -{len(diff.removed)} ~{len(diff.changed)}"
    if diff.empty:
        return head + "\n  (no change)"
    added, removed, changed = diff.added, diff.removed, diff.changed
    text_changes: list[str] = []
    hidden_added = hidden_removed = 0
    if mode == "interactive":
        added = tuple(el for el in diff.added if is_interactive(el))
        removed = tuple(el for el in diff.removed if is_interactive(el))
        kept_changes = []
        for old, new, ch in diff.changed:
            if is_interactive(old) or is_interactive(new):
                kept_changes.append((old, new, ch))
            elif "value" in ch or "title" in ch:
                a, b = ch.get("value") or ch["title"]
                text_changes.append(f"{new.ref} {_compact_clip(a)}→{_compact_clip(b)}")
        changed = tuple(kept_changes)
        hidden_added = len(diff.added) - len(added)
        hidden_removed = len(diff.removed) - len(removed)
    entries: list[tuple[str, bool]] = [(head, False)]
    for el in added:
        entries.append(("  + " + _render_line(el), True))
    for el in removed:
        role = el.role[2:].lower() if el.role.startswith("AX") else el.role.lower()
        title = f' "{el.title}"' if el.title else ""
        entries.append((f"  - {el.ref} {role}{title} (gone)", True))
    for _old, new, changes in changed:
        parts = ", ".join(
            f"{attr}: {_compact_clip(a)}→{_compact_clip(b)}" for attr, (a, b) in changes.items()
        )
        role = new.role[2:].lower() if new.role.startswith("AX") else new.role.lower()
        entries.append((f"  ~ {new.ref} {role} [{parts}]", True))
    if text_changes:
        shown = text_changes[:_COLLAPSED_TEXT_ITEMS]
        extra = len(text_changes) - len(shown)
        suffix = f" (+{extra})" if extra > 0 else ""
        entries.append((f"  ~ text: {_clip(' | '.join(shown), _COLLAPSED_TEXT_CHARS)}{suffix}", True))
    if hidden_added or hidden_removed:
        entries.append(
            (f"  ({hidden_added} added, {hidden_removed} removed static elements folded)", False)
        )
    return "\n".join(_apply_budget(entries, budget))


# ---------------------------------------------------------------------------
# Model observations: full tree once, then a diff with stable refs
# ---------------------------------------------------------------------------


#: A bounds change smaller than this is not reported. Accessibility walks
#: jitter by a pixel or two; the ref still resolves. A real move or resize
#: is included.
_BOUNDS_MOVE_PX = 8

_WINDOW_ROLES = frozenset({"AXWindow", "AXWebArea", "AXDialog", "AXSheet"})

_ELEMENT_LINE = re.compile(r"^\s*e\d+\s")


def window_identity(snap: Snapshot) -> tuple:
    """Identity of the front window.

    A different value means the next observation sent to the model is a full
    snapshot. A developer-assigned stable id, or a file path carried in the
    document id, wins over the title. Otherwise the title is used, with one
    leading modified-marker removed, so ``*name`` and ``name`` are the same
    window. A different title, app, pid, or instance is a different window.
    """
    scope = snap.scope.value if isinstance(snap.scope, Scope) else str(snap.scope)
    window = next((el for el in snap.elements if el.role in _WINDOW_ROLES), None)
    if window is None:
        window = next((el for el in snap.elements if el.parent is None), None)
    if window is None:
        return (snap.app, snap.pid, scope, None)
    file_id = _file_document(window.document_id)
    instance = window.instance_id
    if window.stable_id:
        kind: tuple = ("id", window.stable_id, instance, file_id)
    elif file_id:
        kind = ("file", file_id, instance)
    else:
        kind = ("title", normalize_document_title(window.title), instance)
    return (snap.app, snap.pid, scope, window.role, kind)


def observation_side_lines(text: str) -> tuple[str, ...]:
    """Non-element lines a full snapshot adds: open menu, page tools, notes.

    Element lines, the ``[snap]`` header, and ``… N more`` markers are not
    included. Those markers travel with the element they belong to.
    """
    from a11y_computer_use.untrusted import unwrap

    body = unwrap(text)
    plain = body if body is not None else text
    kept: list[str] = []
    for line in plain.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("[") or stripped.startswith("…") or stripped.startswith("..."):
            continue
        if _ELEMENT_LINE.match(line) or stripped.startswith("text:"):
            continue
        kept.append(stripped)
    return tuple(kept)


def _bounds_matter(old: Bounds, new: Bounds) -> bool:
    if old.display_id != new.display_id:
        return True
    return any(
        abs(getattr(old, name) - getattr(new, name)) > _BOUNDS_MOVE_PX
        for name in ("x", "y", "width", "height")
    )


def _model_changes(
    old: Element, new: Element, old_snap: Snapshot, new_snap: Snapshot,
) -> dict[str, tuple]:
    """State the model must see. Sub-pixel bounds jitter is not a change."""
    changes = dict(_state_changes(old, new))
    # `_state_changes` ignores display_id. A move to another display is still
    # a change the model has to see. Jitter within `_BOUNDS_MOVE_PX` is not.
    moved = "bounds" in changes or old.bounds.display_id != new.bounds.display_id
    changes.pop("bounds", None)
    if moved and _bounds_matter(old.bounds, new.bounds):
        ab, bb = old.bounds, new.bounds
        changes["bounds"] = (
            f"{ab.width}x{ab.height}@{ab.display_id}:{ab.x},{ab.y}",
            f"{bb.width}x{bb.height}@{bb.display_id}:{bb.x},{bb.y}",
        )
    old_more = _EPOCHS.get(old_snap.snapshot_id, {}).get(old.ref, 0)
    new_more = _EPOCHS.get(new_snap.snapshot_id, {}).get(new.ref, 0)
    if old_more != new_more:
        changes["more"] = (old_more, new_more)
    return changes


def _stable_id_keeps(old: Element, new: Element) -> bool:
    """A slot id on a row or link does not survive a new label.

    Buttons and fields may relabel (Submit → Sending) under a developer
    assigned id. Rows, cells, links, and static text do not: the label is
    the identity, matching `rematch_ref`.
    """
    if new.role not in _ITEM_ROLES:
        return True
    if _labels_match(old.title, new.title):
        return True
    if not old.title and not new.title:
        return _labels_match(str(old.value or ""), str(new.value or ""))
    return False


def _claim(mapping: dict[str, str], claimed: set[str], new: Element, old: Element) -> None:
    mapping[new.ref] = old.ref
    claimed.add(old.ref)


def _match_refs(old: Snapshot, new: Snapshot) -> dict[str, str]:
    """Map ``new`` ref → ``old`` ref for the same element.

    Order: stable id (except a relabeled row or link), then role + title +
    path, then a unique role + title whose path changed, then a unique
    role + path whose title changed, then the same ref string when that
    element still has the same role, title, and path. The last pass is how
    two controls with the same name keep the refs a fresh walk assigned
    them again. Item roles do not match on path alone, because every row
    in a list shares one path.
    """
    claimed: set[str] = set()
    mapping: dict[str, str] = {}
    old_by_sid: dict[tuple, list[Element]] = {}
    for el in old.elements:
        if el.stable_id:
            old_by_sid.setdefault((el.stable_id, el.role), []).append(el)
    rest: list[Element] = []
    for el in new.elements:
        cands: list[Element] = []
        if el.stable_id:
            cands = [c for c in old_by_sid.get((el.stable_id, el.role), []) if c.ref not in claimed]
        if len(cands) == 1 and _stable_id_keeps(cands[0], el):
            _claim(mapping, claimed, el, cands[0])
        else:
            rest.append(el)

    old_by_rtp: dict[tuple, list[Element]] = {}
    for el in old.elements:
        if el.ref not in claimed:
            old_by_rtp.setdefault((el.role, el.title, el.path), []).append(el)
    still: list[Element] = []
    for el in rest:
        cands = [c for c in old_by_rtp.get((el.role, el.title, el.path), []) if c.ref not in claimed]
        if len(cands) == 1:
            _claim(mapping, claimed, el, cands[0])
        else:
            still.append(el)

    old_by_rt: dict[tuple, list[Element]] = {}
    for el in old.elements:
        if el.ref not in claimed and el.title:
            old_by_rt.setdefault((el.role, el.title), []).append(el)
    titled = [el for el in still if el.ref not in mapping and el.title]
    new_by_rt: dict[tuple, list[Element]] = {}
    for el in titled:
        new_by_rt.setdefault((el.role, el.title), []).append(el)
    for key, group in new_by_rt.items():
        cands = [c for c in old_by_rt.get(key, []) if c.ref not in claimed]
        if len(group) == 1 and len(cands) == 1:
            _claim(mapping, claimed, group[0], cands[0])

    old_by_rp: dict[tuple, list[Element]] = {}
    for el in old.elements:
        if el.ref not in claimed:
            old_by_rp.setdefault((el.role, el.path), []).append(el)
    new_by_rp: dict[tuple, list[Element]] = {}
    for el in new.elements:
        if el.ref not in mapping:
            new_by_rp.setdefault((el.role, el.path), []).append(el)
    for key, group in new_by_rp.items():
        if key[0] in _ITEM_ROLES:
            continue
        cands = [c for c in old_by_rp.get(key, []) if c.ref not in claimed]
        if len(group) == 1 and len(cands) == 1:
            _claim(mapping, claimed, group[0], cands[0])

    # A walk that did not insert or remove nodes assigns the same pre-order
    # refs. Duplicates (two Save buttons) are not unique by title, so the
    # passes above leave them unmatched. Keeping the ref that both snapshots
    # already used leaves those refs valid. A shifted walk gives the ref to
    # a different node, and a different title or path does not take it.
    old_by_ref = {el.ref: el for el in old.elements}
    for el in new.elements:
        if el.ref in mapping:
            continue
        prev = old_by_ref.get(el.ref)
        if prev is None or prev.ref in claimed:
            continue
        if prev.role != el.role or prev.path != el.path:
            continue
        if not _labels_match(prev.title, el.title):
            continue
        _claim(mapping, claimed, el, prev)
    return mapping


def _alloc_ref(used: set[str]) -> str:
    number = 1
    while True:
        ref = f"e{number}"
        number += 1
        if ref not in used:
            used.add(ref)
            return ref


def align_refs(basis: Snapshot, current: Snapshot) -> Snapshot:
    """Copy ``current`` so elements that still exist keep ``basis`` refs.

    Added elements get refs that are not in ``basis``, so a removed ref is
    not handed to a new element in the same step. Handles, elision counts,
    and unclipped values move with the refs. ``basis`` is not modified.
    """
    touch_epoch(basis.snapshot_id)
    touch_epoch(current.snapshot_id)
    mapping = _match_refs(basis, current)
    used = {el.ref for el in basis.elements}
    assigned: dict[str, str] = {}
    for el in current.elements:
        kept = mapping.get(el.ref)
        if kept is not None and kept not in assigned.values():
            assigned[el.ref] = kept
        else:
            assigned[el.ref] = _alloc_ref(used)
    snapshot_id = f"snap-{next(_EPOCH_COUNTER)}"
    elements: list[Element] = []
    for el in current.elements:
        parent = assigned.get(el.parent) if el.parent else None
        elements.append(dataclasses.replace(
            el, ref=assigned[el.ref], snapshot_id=snapshot_id, parent=parent,
        ))

    def move(store: dict) -> dict:
        return {assigned[key]: value for key, value in store.items() if key in assigned}

    _register_epoch(
        snapshot_id,
        move(_EPOCHS.get(current.snapshot_id, {})),
        move(_HANDLES.get(current.snapshot_id, {})),
        move(_FULL_VALUES.get(current.snapshot_id, {})),
    )
    return Snapshot(
        snapshot_id=snapshot_id,
        scope=current.scope,
        app=current.app,
        pid=current.pid,
        created_at=current.created_at,
        displays=current.displays,
        elements=tuple(elements),
    )


def _element_depth(el: Element, by_ref: dict[str, Element]) -> int:
    depth = 0
    parent = el.parent
    seen: set[str] = set()
    while parent is not None and parent not in seen and parent in by_ref:
        seen.add(parent)
        depth += 1
        parent = by_ref[parent].parent
    return depth


def _role_name(el: Element) -> str:
    return el.role[2:].lower() if el.role.startswith("AX") else el.role.lower()


def render_model_diff(basis: Snapshot, aligned: Snapshot) -> str:
    """Added, removed, and changed elements, with the refs on ``aligned``.

    A changed line is the element's current rendering plus the old->new
    values. The markers are ASCII so a UTF-8 trace still decodes with the
    Windows locale encoding. The focused element is included when it did
    not itself change, so a diff still says where the keyboard is. Bounds
    jitter below `_BOUNDS_MOVE_PX` is omitted. Nothing else the snapshot
    kept is omitted from an element that changed: the line is the same
    shape as `render_text`.
    """
    old = {el.ref: el for el in basis.elements}
    new = {el.ref: el for el in aligned.elements}
    added = [el for el in aligned.elements if el.ref not in old]
    removed = [el for el in basis.elements if el.ref not in new]
    changed: list[tuple[Element, Element, dict[str, tuple]]] = []
    for el in aligned.elements:
        prev = old.get(el.ref)
        if prev is None:
            continue
        delta = _model_changes(prev, el, basis, aligned)
        if delta:
            changed.append((prev, el, delta))
    changed_refs = {el.ref for _prev, el, _delta in changed}
    added_refs = {el.ref for el in added}
    head = (
        f"[{aligned.snapshot_id} <- {basis.snapshot_id}] "
        f"+{len(added)} -{len(removed)} ~{len(changed)}"
    )
    lines = [head]
    if not added and not removed and not changed:
        lines.append("  (no change)")
    by_ref = {el.ref: el for el in aligned.elements}
    for el in added:
        parent = f" parent={el.parent}" if el.parent else ""
        indent = "  " * _element_depth(el, by_ref)
        lines.append(f"{indent}+ {_render_line(el)}{parent}")
    for el in removed:
        title = f' "{el.title}"' if el.title else ""
        lines.append(f"  - {el.ref} {_role_name(el)}{title} (gone)")
    for _prev, el, delta in changed:
        shown = ", ".join(
            f"{attr}: {_clip(str(before), _RENDER_VALUE_CHARS)}->{_clip(str(after), _RENDER_VALUE_CHARS)}"
            for attr, (before, after) in delta.items()
        )
        lines.append(f"  ~ {_render_line(el)} [{shown}]")
    for el in aligned.elements:
        if el.focused and el.ref not in changed_refs and el.ref not in added_refs:
            lines.append(f"  focus {_render_line(el)}")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ModelObservation:
    """The text to send, the snapshot its refs name, and whether it is full.

    ``window`` and ``side`` are what the next diff compares against. ``side``
    is the open-menu, page-tool, and snapshot-note lines of the runtime text.
    """

    text: str
    snap: Snapshot
    full: bool
    window: tuple
    side: tuple[str, ...]


def project_model_observation(
    basis: Snapshot | None,
    snap: Snapshot,
    full_text: str,
    *,
    basis_window: tuple | None = None,
    basis_side: tuple[str, ...] = (),
    force_full: bool = False,
) -> ModelObservation:
    """Choose a full snapshot or a diff for the model.

    The first observation, a window change, an empty tree, and ``force_full``
    send every element. A later observation of the same window sends the
    diff from `render_model_diff`. Refs of elements that still exist are the
    basis refs, including when ``force_full`` redraws the whole tree, so a
    request for a full snapshot does not invalidate them. ``full_text`` is
    the runtime's own rendering and is what a window change returns, so a
    menu header or page-tool block the runtime added is kept.
    """
    window = window_identity(snap)
    side = observation_side_lines(full_text)
    if basis is not None and basis_window is None:
        basis_window = window_identity(basis)
    same = (
        basis is not None
        and bool(basis.elements)
        and bool(snap.elements)
        and basis_window == window
    )
    if not same:
        return ModelObservation(full_text, snap, True, window, side)
    aligned = align_refs(basis, snap)
    if force_full:
        text = render_text(aligned)
        if side:
            text = text + "\n" + "\n".join(side)
        return ModelObservation(text, aligned, True, window, side)
    text = render_model_diff(basis, aligned)
    if side != basis_side:
        if side:
            text = text + "\n" + "\n".join(side)
        else:
            text = text + "\n(snapshot note gone)"
    return ModelObservation(text, aligned, False, window, side)


# ---------------------------------------------------------------------------
# Pruning engine
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _PNode:
    """One kept node of the pruned tree, pre-flattening."""

    raw: RawNode
    bounds: Bounds | None
    children: list[_PNode]
    elided: int  #: direct children hidden by the depth/fan-out caps
    has_interactive: bool  #: self or any kept descendant is interactive
    node: object | None = None  #: accessor handle (live AXUIElementRef), for act-time AX actions


def _prune_root(
    node: object,
    accessor: TreeAccessor,
    geometry: tuple[DisplayGeometry, ...],
) -> _PNode:
    """Prune the tree at ``node``, force-keeping the root itself."""
    raw = accessor.read(node)
    main = next((g for g in geometry if g.display.is_main), geometry[0])
    if raw.position is None or raw.size is None:
        # An AXApplication root often has no geometry at all (every Electron
        # app, some native ones). Without it `_prune_inner` would drop the
        # whole subtree and the snapshot would show an empty app while the
        # windows sit right there under it. Give the root the main display's
        # rect in points so the walk proceeds; the root's own bounds are
        # cosmetic.
        raw = dataclasses.replace(
            raw,
            position=main.origin,
            size=(main.display.width / main.display.scale, main.display.height / main.display.scale),
        )
    pruned = _prune_inner(
        node, raw, accessor, geometry, depth=0, kept_depth=0, web=_WebWalk()
    )
    if pruned is not None:
        return pruned
    # Roots survive even when pruning would drop them (offscreen/decorative).
    bounds = _project(raw.position, raw.size, main)
    return _PNode(raw=raw, bounds=bounds, children=[], elided=0, has_interactive=False, node=node)


def _plain_zero_wrapper(raw: RawNode) -> bool:
    """Unnamed, valueless, non-focusable wrapper whose box has a zero side.

    Chrome adds ``click`` (mapped to AXPress), ``clickAncestor``, and
    ``showContextMenu`` to plain divs. On a zero-size web wrapper those
    actions are not a control: the node is hollow, and it is not a clickable
    target. A focusable node, a named node, a node with a value or
    description, or a real interactive role (push button, link, check box,
    entry) is not this wrapper. ``clickAncestor`` and ``showContextMenu`` are
    not press actions, so they never mark a node clickable on their own.
    """
    if raw.role not in _WRAPPER_ROLES:
        return False
    if raw.title or raw.description or raw.value is not None:
        return False
    if raw.focused or raw.focusable:
        return False
    return _zero_extent(raw.size)


def _target_flags(raw: RawNode) -> tuple[bool, bool, bool]:
    """(clickable, editable, secure) for a kept node.

    A plain zero-size wrapper is not clickable, even when Chrome gave it a
    press action. An opaque region is clickable: the ref addresses the box,
    and a later coordinate click uses those bounds. Every other node uses
    `_flags`.
    """
    if raw.role == OPAQUE_ROLE:
        return True, False, False
    clickable, editable, secure = _flags(raw)
    if _plain_zero_wrapper(raw) or raw.unallocated:
        clickable = False
    return clickable, editable, secure


def _noninteractive_wrapper(raw: RawNode) -> bool:
    """Untitled wrapper role that is not itself a control."""
    clickable, editable, _ = _flags(raw)
    return (
        raw.role in _WRAPPER_ROLES
        and not (clickable or editable)
        and not raw.title
        and not raw.description
        and raw.value is None
    )


def _wrapper_candidate(raw: RawNode, depth: int) -> bool:
    """Could this node collapse onto a single kept child? The child-independent
    half of the collapse test (the other half is "exactly one kept child and
    nothing elided", known only after its subtree is pruned)."""
    return depth > 0 and _noninteractive_wrapper(raw)


@dataclass
class _WebWalk:
    """Per-snapshot bounds on the Chromium iframe exception.

    macOS, Windows, and browser nodes leave ``RawNode.atspi_web`` empty, so
    this counter is never spent and the walk is the one those trees had.
    """

    restarts: int = _MAX_WEB_DEPTH_RESTARTS
    scan_reads: int = _MAX_WEB_FRAME_SCAN


def _prune_inner(
    node: object,
    raw: RawNode,
    accessor: TreeAccessor,
    geometry: tuple[DisplayGeometry, ...],
    depth: int,
    kept_depth: int,
    *,
    in_page: bool = False,
    in_web: bool = False,
    web_depth: int = 0,
    web: _WebWalk | None = None,
    parent_bounds: Bounds | None = None,
) -> _PNode | None:
    """``depth`` is the raw tree depth; ``kept_depth`` is the exact pruned depth
    of this node. A wrapper candidate adds a level for its children only when it
    will NOT collapse, which is decided from the children's raw attributes before
    recursing, so `MAX_DEPTH` is applied during the walk (bounding its cost) and
    a node whose pruned depth is under the cap is never elided.

    Linux AT-SPI web content is the exception. ``in_web`` means this node sits
    under a Chromium document web, document frame, or internal frame. A
    zero-size non-interactive wrapper there is hollow, the same as GTK's
    negative extent. A nested iframe restarts ``kept_depth``. Nodes with an
    empty ``atspi_web`` (macOS, Windows, the browser) never set ``in_web``.
    """
    kind = raw.atspi_web
    # An internal frame always restarts. A document web that is already inside
    # a page restarts only once it is deeper than the first level, so the
    # document directly under a frame that just restarted does not spend a
    # second restart. One iframe then costs one restart, and four restarts
    # cover four frames.
    restart = kind == "iframe" or (kind == "page" and in_page and kept_depth > 1)
    if web is not None and web.restarts > 0 and depth < _MAX_RAW_DEPTH and restart:
        web.restarts -= 1
        kept_depth = 0
    child_in_page = in_page or kind in ("page", "iframe")
    child_in_web = in_web or kind in ("page", "docframe", "iframe")
    if in_web:
        child_web_depth = web_depth + 1
    elif child_in_web:
        child_web_depth = 1
    else:
        child_web_depth = 0

    bounds = _to_bounds(raw.position, raw.size, geometry)
    if raw.unallocated:
        # A hidden notebook tab has a name and no box. Do not adopt the
        # page as its bounds, and do not drop the name. The page is hoisted
        # onto the tab list, so this node keeps no children.
        return _PNode(
            raw=raw, bounds=None, children=[], elided=0, has_interactive=False, node=node,
        )
    # An empty label or paragraph is decorative static text. Returning here
    # used to drop the checkbox, button, or field inside it (#124, #136).
    # Walk those children. A childless empty text node is still dropped below.
    text_container = _empty_text_container(raw)
    if _is_decorative(raw) and not text_container:
        return None
    # A node with no rect of its own drops its subtree when it is zero-size or
    # fully offscreen (scrolled-away rows, hidden GTK widgets parked at -2^31),
    # but not when its extent is NEGATIVE: GTK reports (-1, -1, -1, -1) for a
    # notebook page tab whose label is hidden while the page's content (the
    # gedit document, a VTE terminal) has real bounds below it. Such a hollow
    # container is kept only if descendants survive, with their union as rect.
    # Chrome does the same with a 0-height section around a painted form, but
    # only under a document web/frame (or an internal frame). The live Figma
    # login wrapper is ``section#react-page`` at 1271 by 0 with actions
    # ``click`` and ``showContextMenu``. ``click`` maps to AXPress, so the
    # wrapper looks clickable; it is still hollow when it is unnamed,
    # valueless, and not focusable. Zero-size outside that web content still
    # drops, so macOS trees and their token budgets stay unchanged. A real
    # rect that merely lies off every display still drops, except a
    # viewport-sized Chromium content root shifted above the screen by
    # find-in-page: that root is hollow so the on-screen descendants stay.
    # A focusable, named, or genuinely interactive zero-size node still drops.
    hollow_zero = in_web and _plain_zero_wrapper(raw)
    # Find-in-page scrolls the document and Chrome leaves the content root
    # at a negative Y with the viewport's height, so that box misses the
    # screen while the text now on screen sits underneath it. Walk that
    # root. A smaller node above the viewport, and everything below the
    # fold, still drops without a descendant walk.
    scrolled = (
        in_web
        and 1 <= web_depth <= 2
        and _scrolled_off_web_container(raw, geometry)
    )
    hollow = bounds is None and (_degenerate_size(raw.size) or hollow_zero or scrolled)
    # An expand page lists the next children even when they sit below the
    # viewport. A normal snapshot still drops those nodes. A marked node
    # with a zero side is hollow so the cell under it is still walked;
    # a positive off-screen rect keeps its own coordinates.
    forced = _forced_offscreen(accessor, node, raw)
    # The empty label may have no box of its own. Its checkbox still does.
    if bounds is None and not hollow and not text_container and not forced:
        return None  # zero-size (not web) or fully offscreen: drop subtree
    if bounds is None and forced and not hollow:
        if _zero_extent(raw.size):
            hollow = True
        else:
            bounds = _project_offscreen(raw.position, raw.size, geometry)
            if bounds is None:
                return None

    kept: list[_PNode] = []
    elided = 0
    raw_children = tuple(accessor.children(node))
    candidate = _wrapper_candidate(raw, depth)
    past_cap = kept_depth >= MAX_DEPTH or depth >= _MAX_RAW_DEPTH
    # A web subtree already at the cap still opens a nested iframe, bounded by
    # ``web.scan_reads``. Anything else at the cap is elided without a read,
    # which is what keeps the macOS/Windows walk count unchanged.
    if past_cap and in_web and depth < _MAX_RAW_DEPTH and web is not None and web.scan_reads > 0:
        kept, elided = _open_nested_frames(
            raw_children, accessor, geometry, depth, child_in_page, child_in_web, web,
            parent_bounds=bounds,
        )
    elif past_cap:
        elided = len(raw_children)
    else:
        walked = raw_children[:_MAX_WALK_CHILDREN]
        elided = len(raw_children) - len(walked)
        # Read the children first: a child is kept iff it has bounds and is not
        # decorative (the only None return below), so the collapse decision --
        # exactly one survivor, nothing elided -- is known before recursing and
        # the kept depth passed down is exact, not a lower bound. This is what
        # bounds walk cost on deep wrapper soup and on cyclic trees: a wrapper
        # that keeps >1 child consumes a level, so MAX_DEPTH fires during the
        # walk. The reads happen here anyway. A hollow web wrapper is not a
        # survivor here; its own child, which has the real rect, is.
        read_children = [(child, accessor.read(child)) for child in walked]
        survivors = sum(
            1
            for _, child_raw in read_children
            if _to_bounds(child_raw.position, child_raw.size, geometry) is not None
            and not _is_decorative(child_raw)
        )
        # An empty label is not a survivor on its own, and it may still keep
        # a checkbox. Do not spend the collapse depth until that walk is done.
        textish = sum(1 for _, child_raw in read_children if _empty_text_container(child_raw))
        collapses = candidate and survivors == 1 and elided == 0 and textish == 0
        child_kept_depth = kept_depth if collapses else kept_depth + 1
        for child, child_raw in read_children:
            pruned = _prune_inner(
                child,
                child_raw,
                accessor,
                geometry,
                depth + 1,
                child_kept_depth,
                in_page=child_in_page,
                in_web=child_in_web,
                web_depth=child_web_depth,
                web=web,
                parent_bounds=bounds,
            )
            if pruned is not None:
                kept.append(pruned)
        if _sheet_table(raw, kept):
            cap = SHEET_MAX_CHILDREN
        elif raw.role in _DENSE_CONTAINER_ROLES and len(kept) > MAX_CHILDREN:
            cap = DENSE_MAX_CHILDREN
        else:
            cap = MAX_CHILDREN
        if len(kept) > cap:
            kept, dropped = _cap_children(kept, cap)
            elided += dropped

    # A table can publish only the on-screen band. The rest are still its
    # children; the count is what ``… N more`` and expand(ref) page from.
    take_unlisted = getattr(accessor, "take_unlisted", None)
    if take_unlisted is not None:
        try:
            extra = int(take_unlisted(node) or 0)
        except (TypeError, ValueError):
            extra = 0
        if extra > 0:
            elided += extra

    if hollow:
        if not kept:
            return None  # zero-size and nothing visible below it
        rects = [c.bounds for c in kept if c.bounds is not None]
        if not rects:
            return None
        bounds = _union_bounds(rects)
    if text_container:
        if not kept:
            return None  # empty static text with nothing under it stays decorative
        if bounds is None:
            rects = [c.bounds for c in kept if c.bounds is not None]
            bounds = _union_bounds(rects) if rects else None
        # One control replaces the empty label, the same way a wrapper
        # collapses. Several controls stay parented so none of them is dropped.
        if len(kept) == 1 and elided == 0:
            return kept[0]
    if candidate and len(kept) == 1 and elided == 0:
        return kept[0]  # collapse single-child wrapper (keeps the child's own handle)
    if bounds is not None:
        raw = _as_opaque_region(raw, kept, elided, bounds=bounds, parent_bounds=parent_bounds)
    clickable, editable, _ = _target_flags(raw)
    return _PNode(
        raw=raw,
        bounds=bounds,
        children=kept,
        elided=elided,
        has_interactive=clickable or editable or any(c.has_interactive for c in kept),
        node=node,
    )


def _web_scan_identity(node: object) -> object:
    """Identity of one node in the buried web scan.

    ``get_child_at_index`` returns a new proxy. After a leaf is handled,
    nothing else referenced it, so it was collected and CPython reused its
    ``id()`` for a later control. A seen-set of those ids then skipped the
    control (a settings button under a named group never reached
    ``_prune_inner``). The AT-SPI bus name and object path stay on the C
    object and do not call D-Bus. A synthetic node has no GObject pointer
    and uses ``id()``. The scan also retains every proxy, so that fallback
    cannot collide with a collected leaf.
    """
    if getattr(node, "__gpointer__", None) is not None:
        try:
            from a11y_computer_use.drivers._atspi import _atspi_wrapper_key

            stable = _atspi_wrapper_key(node)
        except Exception:
            stable = None
        if stable is not None:
            return stable
    return id(node)


def _open_nested_frames(
    raw_children: Sequence[object],
    accessor: TreeAccessor,
    geometry: tuple[DisplayGeometry, ...],
    depth: int,
    in_page: bool,
    in_web: bool,
    web: _WebWalk,
    *,
    parent_bounds: Bounds | None = None,
) -> tuple[list[_PNode], int]:
    """Past ``MAX_DEPTH`` inside web content, open a nested frame or a control.

    Descend through wrappers, including a named group, spending
    ``web.scan_reads``. A found ``internal frame`` (or a ``document web``
    already inside a page) is pruned with a restarted kept-depth budget. A
    button, field, link, or other control is kept at the cap, so a phone
    entry or a settings control under a named group is not dropped. The
    group itself is not kept. Static text stays elided. ``MAX_DEPTH`` and
    ``web.scan_reads`` are not raised. Proxies stay referenced for the
    whole scan, and the seen-set uses `_web_scan_identity` rather than a
    bare ``id()``.
    """
    kept: list[_PNode] = []
    elided = 0
    pending: list[tuple[object, int, bool, bool]] = [
        (child, depth + 1, in_page, in_web) for child in raw_children[:_MAX_WALK_CHILDREN]
    ]
    elided += max(0, len(raw_children) - len(pending))
    # Keep every proxy alive. Dropping a leaf lets its id be reused for a
    # control that has not been walked; the seen-set would then skip it.
    pinned: list[object] = []
    seen: set[object] = set()
    while pending and web.scan_reads > 0 and len(kept) < MAX_CHILDREN:
        child, child_depth, page, web_here = pending.pop(0)
        pinned.append(child)
        if child_depth >= _MAX_RAW_DEPTH:
            elided += 1
            continue
        marker = _web_scan_identity(child)
        if marker in seen:
            continue
        seen.add(marker)
        web.scan_reads -= 1
        child_raw = accessor.read(child)
        kind = child_raw.atspi_web
        nested = kind == "iframe" or (kind == "page" and page)
        if nested and web.restarts > 0:
            pruned = _prune_inner(
                child,
                child_raw,
                accessor,
                geometry,
                child_depth,
                MAX_DEPTH,
                in_page=page,
                in_web=web_here,
                web=web,
                parent_bounds=parent_bounds,
            )
            if pruned is not None:
                kept.append(pruned)
            continue
        if child_raw.role in _BURIED_CONTROL_ROLES:
            # kept_depth stays at the cap, so this control's own children
            # are still scanned under the same ``web.scan_reads`` budget.
            pruned = _prune_inner(
                child,
                child_raw,
                accessor,
                geometry,
                child_depth,
                MAX_DEPTH,
                in_page=page,
                in_web=web_here,
                web=web,
                parent_bounds=parent_bounds,
            )
            if pruned is not None:
                kept.append(pruned)
            else:
                elided += 1
            continue
        if (
            _noninteractive_wrapper(child_raw)
            or _plain_zero_wrapper(child_raw)
            or kind in ("docframe", "page", "iframe")
            or child_raw.role in _WRAPPER_ROLES
        ):
            kids = tuple(accessor.children(child))
            room = kids[:_MAX_WALK_CHILDREN]
            elided += max(0, len(kids) - len(room))
            next_page = page or kind in ("page", "iframe")
            next_web = web_here or kind in ("page", "docframe", "iframe")
            for kid in room:
                pinned.append(kid)
                pending.append((kid, child_depth + 1, next_page, next_web))
            continue
        elided += 1
    elided += len(pending)
    return kept, elided


def _scrolled_off_web_container(
    raw: RawNode, geometry: tuple[DisplayGeometry, ...]
) -> bool:
    """Chrome content root parked above the viewport after find-in-page.

    The section keeps a viewport-sized box and a negative Y, so the rect
    misses the screen. Descendants that are actually showing still have
    screen coordinates. A one-line control, a node below the fold, and a
    section taller than the screen are not this root.
    """
    if raw.role not in _WRAPPER_ROLES:
        return False
    if raw.position is None or raw.size is None or not geometry:
        return False
    width, height = raw.size
    if width <= 0 or height <= 0 or raw.position[1] >= 0:
        return False
    if _to_bounds(raw.position, raw.size, geometry) is not None:
        return False
    screen_w = max(geom.display.width / geom.display.scale for geom in geometry)
    screen_h = max(geom.display.height / geom.display.scale for geom in geometry)
    if width < min(400.0, screen_w * 0.5) or height < min(400.0, screen_h * 0.5):
        return False
    return height <= screen_h * 1.25


def _degenerate_size(size: tuple[float, float] | None) -> bool:
    """A rect the toolkit marked invalid with a negative extent (GTK's -1
    sentinel for a widget that has no allocation of its own), as opposed to a
    zero-size rect or a real rect that merely lies off every display."""
    return size is not None and (size[0] < 0 or size[1] < 0)


def _zero_extent(size: tuple[float, float] | None) -> bool:
    """A rect with a zero width or height and no negative component.

    Chrome reports this for an empty wrapper section (1279 by 0) whose
    children still have a painted box. Distinct from GTK's negative sentinel
    and from a positive rect that lies off every display.
    """
    return (
        size is not None
        and size[0] >= 0
        and size[1] >= 0
        and (size[0] == 0 or size[1] == 0)
    )


def _union_bounds(rects: list[Bounds]) -> Bounds:
    """The smallest rect covering ``rects`` (all on the first rect's display)."""
    first = rects[0]
    same = [r for r in rects if r.display_id == first.display_id] or [first]
    x1 = min(r.x for r in same)
    y1 = min(r.y for r in same)
    x2 = max(r.x + r.width for r in same)
    y2 = max(r.y + r.height for r in same)
    return Bounds(display_id=first.display_id, x=x1, y=y1, width=x2 - x1, height=y2 - y1)


def _sheet_table(raw: RawNode, kept: list[_PNode]) -> bool:
    """True when this table's kept children are spreadsheet addresses.

    Two address titles are enough. A calendar cell is titled ``15``, not
    ``B2``, and stays on the dense cap.
    """
    if raw.role != "AXTable":
        return False
    hits = 0
    for node in kept:
        if _SHEET_ADDRESS.match(node.raw.title or ""):
            hits += 1
            if hits >= 2:
                return True
    return False


def _cap_children(kept: list[_PNode], cap: int) -> tuple[list[_PNode], int]:
    """Keep the ``cap`` highest-priority children, in document order."""
    ranked = sorted(range(len(kept)), key=lambda i: (-_keep_priority(kept[i]), i))
    winners = sorted(ranked[:cap])
    return [kept[i] for i in winners], len(kept) - cap


def _keep_priority(node: _PNode) -> int:
    priority = 2 if node.has_interactive else 0
    if node.raw.title or node.raw.value is not None:
        priority += 1
    return priority


def _generic_region_name(text: str) -> bool:
    """True for an empty name or one that does not describe the pixels.

    Chrome names an ``<img>`` without alt ``To get missing image descriptions,
    open the context menu.`` That sentence is the same kind of placeholder as
    ``image``.
    """
    folded = text.strip().casefold()
    if folded in _GENERIC_REGION_NAMES:
        return True
    return folded.startswith(_CHROME_IMAGE_PLACEHOLDER)


def _no_accessible_content(raw: RawNode) -> bool:
    """No name, description, value, or placeholder an agent can read.

    A generic name such as ``image`` or ``canvas`` is not content. A described
    image (``Sync status``) is.
    """
    if not _generic_region_name(raw.title) or not _generic_region_name(raw.description):
        return False
    if raw.value not in (None, ""):
        return False
    return not raw.placeholder


def _positive_extent(raw: RawNode) -> bool:
    size = raw.size
    return size is not None and size[0] > 0 and size[1] > 0


def _bounds_duplicate(bounds: Bounds | None, parent: Bounds | None) -> bool:
    """True when ``bounds`` is the same box as ``parent``, within one pixel.

    A childless group with its parent's box is not a separate region. Chrome's
    nested wrappers copy the page box; the shorter content-area view is a
    browser-frame group instead. A canvas or an image keeps its own role
    even when the boxes match.
    """
    if bounds is None or parent is None or bounds.display_id != parent.display_id:
        return False
    return (
        abs(bounds.x - parent.x) <= 1
        and abs(bounds.y - parent.y) <= 1
        and abs(bounds.width - parent.width) <= 1
        and abs(bounds.height - parent.height) <= 1
    )


def _as_opaque_region(
    raw: RawNode,
    kept: list[_PNode],
    elided: int,
    *,
    bounds: Bounds | None = None,
    parent_bounds: Bounds | None = None,
) -> RawNode:
    """Rename a pixel surface or a childless dead group to ``opaque_region``.

    A canvas stays opaque even when it has a real name: the name does not
    describe the pixels. An image needs an empty or generic name. A group is
    opaque only when it has a real box, nothing to read, and no kept or
    elided children. The box has to be at least 32 by 32. A smaller childless
    panel is a layout spacer; marking it clickable would crowd the child cap
    and hide a titled document that sits beside it. A zero-size wrapper keeps
    its role so a hollow section around a form is not reported as a clickable
    surface. An editable node stays an entry.

    A browser-frame view stays a group. So does a childless group whose box
    only duplicates its parent: that view is the frame again, not page
    content. A canvas, a WebGL surface, an unnamed image, and an empty page
    region that has its own box stay opaque.
    """
    if raw.editable or not _positive_extent(raw):
        return raw
    size = raw.size or (0.0, 0.0)
    surface = raw.role in _OPAQUE_SURFACE_ROLES
    dead = (
        raw.role in _DEAD_REGION_ROLES
        and not kept
        and elided == 0
        and _no_accessible_content(raw)
        and size[0] >= 32
        and size[1] >= 32
    )
    if surface and raw.role == "AXImage" and not _no_accessible_content(raw):
        return raw
    if not surface and not dead:
        return raw
    if dead and (raw.browser_chrome or _bounds_duplicate(bounds, parent_bounds)):
        return raw
    title = "" if _generic_region_name(raw.title) else raw.title
    description = "" if _generic_region_name(raw.description) else raw.description
    # ``surface_name`` is not the node's own name, so it did not keep this
    # region from being demoted. Once it is an opaque region, that string is
    # the honest title. A generic placeholder is not used.
    if not title and not _generic_region_name(raw.surface_name):
        title = raw.surface_name
    return dataclasses.replace(raw, role=OPAQUE_ROLE, title=title, description=description)


def _is_decorative(raw: RawNode) -> bool:
    if raw.role in _DECORATIVE_ROLES:
        return True
    labelled = bool(raw.title or raw.description) and not (
        _generic_region_name(raw.title) and _generic_region_name(raw.description)
    )
    if raw.role == "AXImage" and not labelled:
        # A painted image with a real box is an opaque region. A zero-size
        # or missing box is still decoration and drops with its subtree.
        if _positive_extent(raw):
            return False
        return True
    return raw.role == "AXStaticText" and not labelled and raw.value in (None, "")


def _empty_text_container(raw: RawNode) -> bool:
    """A label, paragraph, or other text container with no text of its own.

    AT-SPI maps ``<label>`` and ``<p>`` to static text. An empty one is
    decorative, and dropping it used to drop the checkbox or button inside.
    ``_prune_inner`` walks these and keeps whatever descendants survive.
    """
    return raw.role in _TEXT_LIKE_ROLES and _is_decorative(raw)


def _flags(raw: RawNode) -> tuple[bool, bool, bool]:
    """Derive (clickable, editable, secure) from role/subrole/actions.

    ``raw.editable`` is the platform signal that the node itself is an
    editable entry. A contenteditable section can carry it. A combo does not
    become editable from its role.
    """
    secure = raw.role == _SECURE_ROLE or raw.subrole == _SECURE_ROLE
    clickable = bool(_PRESS_ACTIONS.intersection(raw.actions)) or raw.role in _CLICKABLE_ROLES
    editable = secure or raw.editable or raw.role in _EDITABLE_ROLES
    return clickable, editable, secure


_CHECKABLE_ROLES = frozenset({"AXCheckBox", "AXRadioButton"})


def _checked_state(role: str, subrole: str, value: object) -> bool | None:
    """Toggle state for a checkable control (checkbox / radio / toggle button)
    from its AXValue (0=off, 1=on, 2=mixed); None when the element isn't
    checkable. Tri-state 'mixed' reads as True (it is not 'off')."""
    if role not in _CHECKABLE_ROLES and subrole != "AXToggle":
        return None
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return None


def _flatten(
    node: _PNode,
    parent_ref: str | None,
    parent_path: tuple[str, ...],
    snapshot_id: str,
    out: list[Element],
    elisions: dict[str, int],
    handles: dict[str, object],
    full_values: dict[str, str],
    instance_id: str | None = None,
    document_id: str | None = None,
) -> None:
    """Assign pre-order refs and emit `Element`s (parents before children)."""
    instance_id = node.raw.instance_id or instance_id
    document_id = node.raw.document_id or document_id
    ref = f"e{len(out) + 1}"
    path = parent_path + (node.raw.role,)
    clickable, editable, secure = _target_flags(node.raw)
    if node.node is not None:  # retain the live handle for act-time AX actions
        handles[ref] = node.node
    value: str | None = None
    terminal = node.raw.subrole == "terminal"
    if node.raw.value is not None and not secure:  # secure fields never leak values
        raw_value = str(node.raw.value)
        if terminal:
            # The snapshot shows the on-screen slice, or the tail of a
            # longer screen. The full buffer stays searchable.
            source = node.raw.screen_text if node.raw.screen_text else raw_value
            value = _terminal_value(source)
            if raw_value != value:
                full_values[ref] = raw_value
        else:
            value = _clip(raw_value, _MAX_VALUE_CHARS)
            if raw_value != value:
                full_values[ref] = raw_value
    out.append(
        Element(
            ref=ref,
            role=node.raw.role,
            title=str(node.raw.title or node.raw.description),
            value=value,
            bounds=node.bounds,
            snapshot_id=snapshot_id,
            parent=parent_ref,
            path=path,
            enabled=node.raw.enabled,
            focused=node.raw.focused,
            clickable=clickable,
            editable=editable,
            secure=secure,
            checked=node.raw.checked,
            selected=node.raw.selected,
            expanded=node.raw.expanded,
            placeholder=node.raw.placeholder,
            stable_id=node.raw.stable_id or None,
            instance_id=instance_id,
            document_id=document_id,
            subrole="terminal" if terminal else None,
            content_note=node.raw.content_note,
            hint=node.raw.hint,
            shortcut=node.raw.shortcut,
            note=node.raw.note or None,
        )
    )
    if node.elided:
        elisions[ref] = node.elided
    for child in node.children:
        _flatten(
            child, ref, path, snapshot_id, out, elisions, handles, full_values,
            instance_id, document_id,
        )


def _forced_offscreen(accessor: object, node: object, raw: RawNode) -> bool:
    """True when an expand page asked to list ``node`` even though it is off screen.

    A normal snapshot has no ``keep_offscreen`` and still drops the node.
    GTK's off-screen sentinel stays dropped so a parked widget is not given
    a box. A zero-size marked node is still forced: the caller treats it as
    hollow and keeps the children that hold the text.
    """
    fn = getattr(accessor, "keep_offscreen", None)
    if not callable(fn):
        return False
    try:
        if not fn(node):
            return False
    except Exception:
        return False
    pos, size = raw.position, raw.size
    if pos is None or size is None:
        return False
    try:
        x, y = float(pos[0]), float(pos[1])
    except (TypeError, ValueError):
        return False
    if x <= -2_000_000_000 or y <= -2_000_000_000:
        return False
    return True


def _project_offscreen(
    position: tuple[float, float] | None,
    size: tuple[float, float] | None,
    geometry: tuple[DisplayGeometry, ...],
) -> Bounds | None:
    """Project a real off-screen rect onto the main display without moving it.

    The box stays where the toolkit put it, including past the display edge,
    so a click is not aimed at a different control that happens to be visible.
    """
    if position is None or size is None or not geometry:
        return None
    try:
        if float(size[0]) <= 0 or float(size[1]) <= 0:
            return None
    except (TypeError, ValueError):
        return None
    geom = next((item for item in geometry if item.display.is_main), geometry[0])
    return _project(position, size, geom)


def _to_bounds(
    position: tuple[float, float] | None,
    size: tuple[float, float] | None,
    geometry: tuple[DisplayGeometry, ...],
) -> Bounds | None:
    """Project a global-point rect onto its display; None when invisible.

    Picks the display with the largest overlap; zero overlap everywhere means
    the rect is zero-size or fully offscreen.
    """
    if position is None or size is None:
        return None
    x, y = position
    w, h = size
    best: DisplayGeometry | None = None
    best_area = 0.0
    for geom in geometry:
        ox, oy = geom.origin
        extent_w = geom.display.width / geom.display.scale
        extent_h = geom.display.height / geom.display.scale
        overlap_w = min(x + w, ox + extent_w) - max(x, ox)
        overlap_h = min(y + h, oy + extent_h) - max(y, oy)
        area = max(overlap_w, 0.0) * max(overlap_h, 0.0)
        if area > best_area:
            best, best_area = geom, area
    if best is None:
        return None
    return _project(position, size, best)


def _project(
    position: tuple[float, float], size: tuple[float, float], geom: DisplayGeometry
) -> Bounds:
    scale = geom.display.scale
    ox, oy = geom.origin
    return Bounds(
        display_id=geom.display.display_id,
        x=round((position[0] - ox) * scale),
        y=round((position[1] - oy) * scale),
        width=round(size[0] * scale),
        height=round(size[1] * scale),
    )


#: Roles whose lines the interactive view prints without the ``click`` flag: the
#: role already says the element is pressable, so the flag is pure token cost.
_IMPLICIT_CLICK_ROLES = frozenset(
    {"AXButton", "AXLink", "AXMenuItem", "AXMenuBarItem", "AXMenuButton", "AXPopUpButton",
     "AXCheckBox", "AXRadioButton", "AXTab", "AXDisclosureTriangle"}
)


def _compact_value(text: str) -> str:
    """Escape line breaks so one snapshot field stays on one line.

    ``Element.value`` keeps the real breaks. Compact output writes ``\\n``
    and ``\\r`` so a multi-line document is not a space-separated line.
    A value with no break is returned unchanged, including a backslash.
    """
    if "\n" not in text and "\r" not in text:
        return text
    return (
        text.replace("\\", "\\\\")
        .replace("\r\n", "\\r\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _compact_clip(value: object, limit: int = 24) -> str:
    """Clip a diff field after escaping its line breaks."""
    return _clip(_compact_value(str(value)), limit)


def _render_line(el: Element, *, bounds: bool | None = None, compact: bool = False) -> str:
    """One element line. ``bounds``: None prints geometry on roots only (the
    full view's rule), True on every line, False never. ``compact`` (the
    interactive view) leaves the ``click`` flag implicit on roles that are
    pressable by definition (buttons, links, menu items, checkboxes, tabs)."""
    role = el.role[2:].lower() if el.role.startswith("AX") else el.role.lower()
    parts = [el.ref, role]
    if el.title:
        parts.append(f'"{el.title}"')
    if el.hint:
        parts.append("hint=" + _field_quote(el.hint))
    if el.shortcut:
        parts.append("shortcut=" + _field_quote(el.shortcut))
    if el.value is not None:
        # A terminal value is already the on-screen text, or its tail.
        # Clipping that head would hide the output the snapshot is showing.
        # Line breaks stay in ``el.value``. The field escapes them so this
        # line does not become several elements.
        shown = el.value if el.subrole == "terminal" else _clip(el.value, _RENDER_VALUE_CHARS)
        parts.append(f'="{_compact_value(shown)}"')
    elif el.placeholder and not el.title:  # blank field: show its prompt for identity
        parts.append(f'~"{_clip(el.placeholder, _RENDER_VALUE_CHARS)}"')
    show_click = el.clickable and not (compact and el.role in _IMPLICIT_CLICK_ROLES)
    flags = [
        name
        for name, on in (
            ("click", show_click),
            ("edit", el.editable),
            ("secure", el.secure),
            ("checked", el.checked is True),
            ("unchecked", el.checked is False),
            ("selected", el.selected),
            ("expanded", el.expanded is True),
            ("collapsed", el.expanded is False),
            ("focus", el.focused),
            ("disabled", not el.enabled),
            ("hidden", el.bounds is None),
        )
        if on
    ]
    if _ACTIVE_ROOTS.get(el.snapshot_id) == el.ref:
        flags.append("active")
    if flags:
        parts.append(f"({','.join(flags)})")
    if el.note:
        parts.append(f"({el.note})")
    # An opaque region is addressed by its box. Always print the bounds, in
    # both views, so a coordinate click does not have to guess the size.
    # A hidden control has no box. Printing one would invent a click target.
    b = el.bounds
    if b is not None and (bounds is True or el.role == OPAQUE_ROLE or (bounds is None and el.parent is None)):
        parts.append(f"[{b.width}x{b.height} @{b.display_id}:{b.x},{b.y}]")
    if el.content_note:
        parts.append(f"({el.content_note})")
    return " ".join(parts)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _field_quote(text: str) -> str:
    """One quoted field. Newlines and embedded quotes stay on one line."""
    shown = " ".join(str(text).replace('"', "'").split())
    return '"' + _clip(shown, 160) + '"'


def _terminal_value(text: str) -> str:
    """On-screen terminal text, or the tail of a longer buffer.

    Trailing blank rows are the unused part of the screen. When the text
    is longer than ``_TERMINAL_VALUE_CHARS``, the head is omitted and a
    leading ellipsis marks the cut. ``find`` still searches the full buffer.
    """
    body = str(text).rstrip("\n")
    limit = _TERMINAL_VALUE_CHARS
    if len(body) <= limit:
        return body
    room = max(1, limit - 1)
    tail = body[-room:]
    # Drop a partial first line when the cut landed just after a newline.
    # A newline near the end of the tail is a real row, not the cut.
    newline = tail.find("\n")
    if 0 <= newline <= 120 and newline < len(tail) - 1:
        tail = tail[newline + 1 :]
    if tail.startswith("…"):
        return tail
    return "…" + tail


# ---------------------------------------------------------------------------
# Epoch registry (snapshot-scoped render metadata)
# ---------------------------------------------------------------------------

_EPOCH_COUNTER = itertools.count(1)
#: snapshot_id -> {parent ref -> elided child count}, for render markers.
#: Bounded FIFO: refs are snapshot-scoped, so old epochs are worthless.
_EPOCHS: OrderedDict[str, dict[str, int]] = OrderedDict()
#: snapshot_id -> {ref -> live accessor handle (AXUIElementRef)}. Lets
#: `press_element` activate an element through the AX API without moving the
#: cursor. Same bounded FIFO as `_EPOCHS`; the handles keep the walk's AX
#: objects alive only for the recent epochs an in-flight act can still target.
_HANDLES: OrderedDict[str, dict[str, object]] = OrderedDict()
#: snapshot_id -> {ref -> unclipped value}. ``Element.value`` stays capped so
#: the model and the audit anchor never see a whole document; `find_elements`
#: reads this so a substring past the cap still matches. Same bounded FIFO.
_FULL_VALUES: OrderedDict[str, dict[str, str]] = OrderedDict()
#: snapshot_id -> ref of the active top-level window, only when that snapshot
#: merged two or more windows. Render adds ``(active)`` on that line.
_ACTIVE_ROOTS: OrderedDict[str, str] = OrderedDict()


def _note_active_root(snapshot_id: str, ref: str) -> None:
    _ACTIVE_ROOTS[snapshot_id] = ref
    _ACTIVE_ROOTS.move_to_end(snapshot_id)
    while len(_ACTIVE_ROOTS) > _MAX_EPOCHS:
        _ACTIVE_ROOTS.popitem(last=False)


def discard_epoch(snapshot_id: str) -> None:
    """Forget one snapshot's render metadata.

    A merged window snapshot copies handles out of the per-window epochs.
    Those epochs then only consume the FIFO, and the next few snapshots
    evict the merge the caller is still acting on.
    """
    _EPOCHS.pop(snapshot_id, None)
    _HANDLES.pop(snapshot_id, None)
    _FULL_VALUES.pop(snapshot_id, None)
    _ACTIVE_ROOTS.pop(snapshot_id, None)


def _register_epoch(
    snapshot_id: str,
    elisions: dict[str, int],
    handles: dict[str, object],
    full_values: dict[str, str] | None = None,
) -> None:
    _EPOCHS[snapshot_id] = elisions
    _HANDLES[snapshot_id] = handles
    _FULL_VALUES[snapshot_id] = full_values or {}
    while len(_EPOCHS) > _MAX_EPOCHS:
        _EPOCHS.popitem(last=False)
    while len(_HANDLES) > _MAX_EPOCHS:
        _HANDLES.popitem(last=False)
    while len(_FULL_VALUES) > _MAX_EPOCHS:
        _FULL_VALUES.popitem(last=False)


def touch_epoch(snapshot_id: str) -> None:
    """Keep ``snapshot_id`` among the newest epochs.

    A probe snapshot taken to judge an action must not evict the handles the
    action's own ref still uses.
    """
    for store in (_EPOCHS, _HANDLES, _FULL_VALUES, _ACTIVE_ROOTS):
        if snapshot_id in store:
            store.move_to_end(snapshot_id)


def attach_offscreen_matches(
    snap: Snapshot,
    found: Sequence[tuple[object, RawNode, object | None]],
) -> Snapshot:
    """Append sought nodes that have no on-screen box.

    ``found`` is ``(handle, raw, parent_handle)``. Bounds stay None. The
    live handle is registered so a later read can still name the node. A
    click is not given a rectangle. An empty list leaves ``snap`` as it was.
    """
    if not found:
        return snap
    handles = _HANDLES.get(snap.snapshot_id)
    if handles is None:
        return snap
    by_handle = {id(handle): ref for ref, handle in handles.items()}
    elements = list(snap.elements)
    added = False
    for cell, raw, table in found:
        if cell is None or id(cell) in by_handle:
            continue
        parent_ref = by_handle.get(id(table)) if table is not None else None
        parent = next((el for el in elements if el.ref == parent_ref), None) if parent_ref else None
        ref = f"e{len(elements) + 1}"
        clickable, editable, secure = _target_flags(raw)
        value = None
        if raw.value is not None and not secure:
            value = _clip(str(raw.value), _MAX_VALUE_CHARS)
        elements.append(
            Element(
                ref=ref,
                role=raw.role,
                title=str(raw.title or raw.description or ""),
                value=value,
                bounds=None,
                snapshot_id=snap.snapshot_id,
                parent=parent_ref,
                path=(parent.path if parent is not None else ()) + (raw.role,),
                enabled=raw.enabled,
                focused=raw.focused,
                clickable=clickable,
                editable=editable,
                secure=secure,
                checked=raw.checked,
                selected=raw.selected,
                expanded=raw.expanded,
                stable_id=raw.stable_id or None,
                instance_id=(parent.instance_id if parent is not None else raw.instance_id),
                document_id=(parent.document_id if parent is not None else raw.document_id),
                note=raw.note or None,
            )
        )
        handles[ref] = cell
        by_handle[id(cell)] = ref
        added = True
    if not added:
        return snap
    return dataclasses.replace(snap, elements=tuple(elements))


def ax_handle_for(snapshot_id: str, ref: str) -> object | None:
    """Return the live accessor handle for ``ref`` in ``snapshot_id``, or None.

    None means the epoch was evicted (older than the last `_MAX_EPOCHS`
    snapshots) or the ref carried no handle (e.g. a synthetic root) — the
    caller falls back to coordinate-based input.
    """
    return _HANDLES.get(snapshot_id, {}).get(ref)


# ---------------------------------------------------------------------------
# Ref re-resolution
# ---------------------------------------------------------------------------


#: Roles whose ``value`` is the text a person reads (no separate title), so a
#: value-bearing anchor of one of these must keep its value to re-resolve.
_TEXT_LIKE_ROLES = frozenset({"AXStaticText", "AXHeading"})
#: Roles whose identity IS their text: rows, cells, items, links. A stable id on
#: one of these is often slot-based (``row-3`` keeps its id while the content
#: scrolls or reorders under it), so the label stays binding even when the id
#: matches. Buttons and fields may legitimately relabel ("Submit" -> "Sending")
#: under a developer-assigned id, so for them a matching id still wins when no
#: live element carries the old label.
_ITEM_ROLES = frozenset({"AXRow", "AXCell", "AXListItem", "AXOutlineRow", "AXMenuItem",
                         "AXStaticText", "AXHeading", "AXLink"})
_ELLIPSES = ("\u2026", "...")


def _norm_label(text: str) -> str:
    text = " ".join(text.split()).lower()
    for mark in _ELLIPSES:
        if text.endswith(mark):
            text = text[: -len(mark)].rstrip()
    return text


def _labels_match(anchor_text: str, live_text: str | None) -> bool:
    """Case- and whitespace-insensitive equality, plus one tolerance: a title
    truncated with an ellipsis ("email-router prod…") matches a live title that
    starts with the same stem (at least three characters), and the reverse."""
    if live_text is None:
        return False
    a, b = _norm_label(anchor_text), _norm_label(live_text)
    if a == b:
        return True
    a_cut = any(anchor_text.rstrip().endswith(m) for m in _ELLIPSES)
    b_cut = any(live_text.rstrip().endswith(m) for m in _ELLIPSES)
    if a_cut and len(a) >= 3 and b.startswith(a):
        return True
    if b_cut and len(b) >= 3 and a.startswith(b):
        return True
    return False


def _anchor_label(anchor: Element) -> tuple[str, str] | None:
    """The text a ref was issued for: ("title", ...) when the element has a
    title, ("value", ...) for text-like roles that only carry a value, else None
    (an untitled anchor is matched by path and position alone)."""
    if anchor.title:
        return "title", anchor.title
    if anchor.role in _TEXT_LIKE_ROLES and anchor.value:
        return "value", str(anchor.value)
    return None


def _label_of(el: Element, kind: str) -> str | None:
    if kind == "title":
        return el.title
    return None if el.value is None else str(el.value)


def _label_similarity(a: str, b: str | None) -> float:
    if not b:
        return 0.0
    na, nb = _norm_label(a), _norm_label(b)
    if not na or not nb:
        return 0.0
    ta, tb = set(na.split()), set(nb.split())
    overlap = len(ta & tb) / max(1, len(ta | tb))
    return max(overlap, difflib.SequenceMatcher(None, na, nb).ratio())


def _match_anchor(anchor: Element, live: Snapshot) -> tuple[Element | None, str]:
    """Find ``anchor``'s counterpart in ``live``; (None, reason) on failure.

    The text a ref was issued for is binding. A titled anchor (or a text-like
    element with a value) only ever re-resolves onto a live element with the
    same title, wherever it moved; it never resolves onto whatever element now
    occupies its old slot. That case, a live list reordering under a ref, was
    the single largest failure in the incident-gauntlet benchmark: the click
    landed on the row that had slid into the position. Untitled anchors keep
    the positional ladder (path, then bounds proximity within
    `_WEAK_ANCHOR_DRIFT_PX`).

    A developer-assigned `stable_id` (AXIdentifier / AutomationId / accessible-id
    / backend DOM node id) is layout-independent, so an exact (stable_id, role)
    match is authoritative when the label still matches, or when the anchor had
    no label; a recreated node that reuses an id under a different title falls
    through to the label ladder. Duplicate ids disambiguate by proximity; a
    near-exact distance tie is ambiguous.

    Reasons: ``not_found`` (no same-role element at all, or nothing matching an
    untitled anchor), ``title_changed`` (same-role elements exist but none
    carries the anchor's text), ``ambiguous`` (a tie).
    """
    label = _anchor_label(anchor)

    def label_ok(el: Element) -> bool:
        return label is None or _labels_match(label[1], _label_of(el, label[0]))

    if anchor.stable_id:
        exact = [
            el for el in live.elements
            if el.stable_id == anchor.stable_id and el.role == anchor.role
            and _bound_to_same_target(anchor, el)
        ]
        if label is not None:
            labelled = [el for el in exact if label_ok(el)]
            if labelled:
                exact = labelled
            elif anchor.role in _ITEM_ROLES or any(
                label_ok(el) for el in live.elements if el.role == anchor.role
            ):
                # The id kept its slot but the text moved (or the row is an item
                # whose text is its identity): the label ladder decides.
                exact = []
        if len(exact) == 1:
            return exact[0], ""
        if len(exact) > 1:  # duplicate ids: disambiguate by bounds proximity
            ranked = sorted(exact, key=lambda el: _center_distance(anchor.bounds, el.bounds))
            d0 = _center_distance(anchor.bounds, ranked[0].bounds)
            d1 = _center_distance(anchor.bounds, ranked[1].bounds)
            return (None, "ambiguous") if d1 - d0 <= _AMBIGUITY_PX else (ranked[0], "")

    same_role = [
        el for el in live.elements
        if el.role == anchor.role and _bound_to_same_target(anchor, el)
    ]
    candidates: list[tuple[int, float, Element]] = []
    for el in same_role:
        if label is not None:
            if not label_ok(el):
                continue
            # The text matched: it may have moved anywhere (a reordered or
            # scrolled list), so no drift cap; path agreement still ranks first.
            score = 4 + (2 if el.path == anchor.path else 0)
            candidates.append((score, _center_distance(anchor.bounds, el.bounds), el))
            continue
        score = 2 if el.path == anchor.path else 0
        if score == 0:
            continue
        distance = _center_distance(anchor.bounds, el.bounds)
        if distance > _WEAK_ANCHOR_DRIFT_PX:
            continue
        candidates.append((score, distance, el))
    if not candidates:
        return None, ("title_changed" if label is not None and same_role else "not_found")
    best_score = max(score for score, _, _ in candidates)
    pool = sorted(
        ((d, el) for score, d, el in candidates if score == best_score),
        key=lambda pair: pair[0],
    )
    if len(pool) > 1 and pool[1][0] - pool[0][0] <= _AMBIGUITY_PX:
        return None, "ambiguous"
    return pool[0][1], ""


def _center_distance(a: Bounds | None, b: Bounds | None) -> float:
    if a is None or b is None:
        return 0.0 if a is None and b is None else math.inf
    ca, cb = a.center, b.center
    if ca.display_id != cb.display_id:
        return math.inf
    return math.hypot(ca.x - cb.x, ca.y - cb.y)


def stale_ref_candidates(anchor: Element, live: Snapshot, limit: int = 3) -> list[dict]:
    """The best near-misses for a ref that failed to re-resolve, attached to the
    STALE_REF error so the agent can self-correct without re-snapshotting.

    Same-role elements ranked by anchor strength (exact text 4, path 2, partial
    text overlap or closest text by similarity 1..3), then proximity. For a
    labelled anchor the element now occupying the old position is always
    included and flagged ``at_old_position``, so the planner reads "the row at
    that slot is now X" instead of clicking it blind.
    """
    label = _anchor_label(anchor)
    scored: list[tuple[float, float, Element]] = []
    for el in live.elements:
        if el.role != anchor.role or not _bound_to_same_target(anchor, el):
            continue
        score: float = 0.0
        if label is not None:
            live_text = _label_of(el, label[0])
            if _labels_match(label[1], live_text):
                score = 4.0
            else:
                sim = _label_similarity(label[1], live_text)
                if sim >= 0.3:
                    score = 1.0 + 2.0 * sim  # 1.6 .. 3.0: closest texts first
        elif el.title == anchor.title:
            score = 4.0
        if el.path == anchor.path:
            score += 2.0
        if score == 0.0:
            continue
        scored.append((score, _center_distance(anchor.bounds, el.bounds), el))
    scored.sort(key=lambda t: (-t[0], t[1]))
    out = [
        {"ref": el.ref, "role": el.role, "title": el.title, "score": int(round(score))}
        for score, _dist, el in scored[:limit]
    ]
    if label is not None:
        same_role = [
            el for el in live.elements
            if el.role == anchor.role and _bound_to_same_target(anchor, el)
        ]
        if same_role:
            occupant = min(same_role, key=lambda el: _center_distance(anchor.bounds, el.bounds))
            if math.isfinite(_center_distance(anchor.bounds, occupant.bounds)):
                for row in out:
                    if row["ref"] == occupant.ref:
                        row["at_old_position"] = True
                        break
                else:
                    out.append({"ref": occupant.ref, "role": occupant.role,
                                "title": occupant.title, "score": 0, "at_old_position": True})
    return out


# ---------------------------------------------------------------------------
# Platform layer: live AX access (pyobjc, lazily imported)
# ---------------------------------------------------------------------------


def _appservices():  # pragma: no cover - trivial import shim
    import ApplicationServices

    return ApplicationServices


def _is_trusted() -> bool:
    """Whether this process holds the Accessibility TCC grant."""
    return bool(_appservices().AXIsProcessTrusted())


def ensure_trusted() -> None:
    """Raise the structured Accessibility error when the TCC grant is missing.

    Raises:
        ComputerUseError: `ErrorCode.PERMISSION_DENIED_ACCESSIBILITY`, with
            the doctor hint attached.
    """
    if not _is_trusted():
        raise ComputerUseError(
            ErrorCode.PERMISSION_DENIED_ACCESSIBILITY,
            "Accessibility permission is missing for this process",
            detail={"hint": _DOCTOR_HINT},
        )


class _AXAccessor:
    """`TreeAccessor` over live ``AXUIElement`` handles.

    Per-attribute AX errors (unsupported attribute, no value, a node that
    stops answering mid-walk) read as missing data rather than raising: a
    degraded snapshot beats an aborted one. The fully-hung-app case is caught
    up front by `_check_responsive`.
    """

    def __init__(self, ax) -> None:
        self._ax = ax
        self._point_type = getattr(ax, "kAXValueCGPointType", 1)
        self._size_type = getattr(ax, "kAXValueCGSizeType", 2)

    def read(self, node: object) -> RawNode:
        role = self._attr(node, "AXRole")
        subrole = self._attr(node, "AXSubrole")
        enabled = self._attr(node, "AXEnabled")
        value = self._attr(node, "AXValue")
        selected = self._attr(node, "AXSelected")
        expanded = self._attr(node, "AXExpanded")
        return RawNode(
            role=str(role) if role else "AXUnknown",
            subrole=str(subrole) if subrole else None,
            title=str(self._attr(node, "AXTitle") or ""),
            value=value,
            description=str(self._attr(node, "AXDescription") or ""),
            enabled=True if enabled is None else bool(enabled),
            focused=bool(self._attr(node, "AXFocused")),
            position=self._geometry(node, "AXPosition", self._point_type),
            size=self._geometry(node, "AXSize", self._size_type),
            actions=self._actions(node),
            checked=_checked_state(str(role) if role else "", str(subrole) if subrole else "", value),
            selected=bool(selected) if selected is not None else False,
            expanded=bool(expanded) if expanded is not None else None,
            placeholder=str(self._attr(node, "AXPlaceholderValue") or ""),
            stable_id=str(self._attr(node, "AXIdentifier") or "") or None,
        )

    def children(self, node: object) -> Sequence[object]:
        kids = tuple(self._attr(node, "AXChildren") or ())
        if self._attr(node, "AXRole") == "AXApplication":
            # Chromium/Electron (Figma, Slack, VS Code, ...) list only menu bars
            # under the application's AXChildren; the windows are reachable
            # solely through AXWindows. Native apps list both, so dedupe.
            # AXWindows itself is flaky on Electron (sometimes an empty array
            # while AXMainWindow/AXFocusedWindow still answer), so union all three.
            windows = list(self._attr(node, "AXWindows") or ())
            for attr in ("AXMainWindow", "AXFocusedWindow"):
                single = self._attr(node, attr)
                if single is not None:
                    windows.append(single)
            for w in windows:
                if not any(_ax_same(w, k) for k in kids):
                    kids = kids + (w,)
        return kids

    def _attr(self, node: object, name: str) -> object | None:
        err, value = self._ax.AXUIElementCopyAttributeValue(node, name, None)
        return value if err == 0 else None

    def _geometry(self, node: object, name: str, ax_type: int) -> tuple[float, float] | None:
        boxed = self._attr(node, name)
        if boxed is None:
            return None
        ok, value = self._ax.AXValueGetValue(boxed, ax_type, None)
        if not ok:
            return None
        if ax_type == self._point_type:
            return float(value.x), float(value.y)
        return float(value.width), float(value.height)

    def _actions(self, node: object) -> tuple[str, ...]:
        err, names = self._ax.AXUIElementCopyActionNames(node, None)
        return tuple(str(n) for n in names) if err == 0 and names else ()


def _check_responsive(ax, app_el: object, app: str) -> None:
    """Fail fast with a structured TIMEOUT when the app's AX server hangs."""
    cannot_complete = getattr(ax, "kAXErrorCannotComplete", -25204)
    err, _ = ax.AXUIElementCopyAttributeValue(app_el, "AXRole", None)
    if err == cannot_complete:
        raise ComputerUseError(
            ErrorCode.TIMEOUT,
            f"{app} did not answer AX queries within {AX_MESSAGING_TIMEOUT_S}s",
            detail={"app": app, "timeout_s": AX_MESSAGING_TIMEOUT_S},
        )


def _front_window(accessor: _AXAccessor, app_el: object) -> object | None:
    """The focused window, else the main one, else the first; None if none."""
    for attr in ("AXFocusedWindow", "AXMainWindow"):
        window = accessor._attr(app_el, attr)
        if window is not None:
            return window
    windows = accessor._attr(app_el, "AXWindows")
    return windows[0] if windows else None


# Chromium/Electron apps (Chrome, Slack, VS Code, Discord, Spotify, Teams, …)
# expose only an empty AXWebArea shell until a screen-reader-like client sets
# these attributes; then they build the real render tree. We detect such an app
# by the presence of a web area and flip the switch once per process — turning
# "0 interactive refs, fall back to blind vision" into a full a11y tree. Opt out
# with A11Y_COMPUTER_USE_NO_WEB_A11Y=1.
_WEB_ROLES = frozenset({"AXWebArea", "AXWebView"})
_WEB_A11Y_SETTLE_S = 0.4  #: give Chromium a moment to build the tree after enabling
_WEB_A11Y_ENABLED: set[int] = set()  #: pids already handled this session (probe once)
_WEB_PROBE_MAX_NODES = 240  #: bound the shallow BFS for a web area
_WEB_PROBE_FANOUT = 20  #: children inspected per node while probing


def _ax_same(a: object, b: object) -> bool:
    """Identity of two AXUIElement handles (CFEqual semantics; pyobjc maps == to it)."""
    try:
        return bool(a == b)
    except Exception:
        return a is b


def _has_web_area(accessor: "_AXAccessor", app_el: object) -> bool:
    """Shallow, bounded BFS for an AXWebArea/AXWebView under the app — the tell
    that this is a Chromium/Electron app (the shell exists even before the tree
    is populated). An application element that advertises the Chromium-only
    ``AXManualAccessibility`` attribute counts as well, so the switch is flipped
    even while the render tree is still an empty shell."""
    from collections import deque

    try:
        err, names = accessor._ax.AXUIElementCopyAttributeNames(app_el, None)
        if err == 0 and names and "AXManualAccessibility" in tuple(str(n) for n in names):
            return True
    except Exception:
        pass
    queue = deque([app_el])
    seen = 0
    while queue and seen < _WEB_PROBE_MAX_NODES:
        node = queue.popleft()
        seen += 1
        role = accessor._attr(node, "AXRole")
        if role in _WEB_ROLES:
            return True
        children = accessor._attr(node, "AXChildren") or ()
        for child in tuple(children)[:_WEB_PROBE_FANOUT]:
            queue.append(child)
    return False


def _maybe_enable_web_a11y(ax, app_el: object, accessor: "_AXAccessor", pid: int) -> None:
    """Force a Chromium/Electron app to expose its accessibility tree.

    Sets ``AXManualAccessibility`` and ``AXEnhancedUserInterface`` on the app
    element (both — ``AXManualAccessibility`` is unsupported on Electron 22+, and
    the reverse on older Chrome), then lets the tree build. Idempotent and cached
    per pid, so the set+settle cost is paid once per app per session; native apps
    (no web area) are marked handled after one cheap probe and never re-probed."""
    if pid in _WEB_A11Y_ENABLED or os.environ.get("A11Y_COMPUTER_USE_NO_WEB_A11Y"):
        return
    _WEB_A11Y_ENABLED.add(pid)  # mark up front: probe/enable at most once per pid
    if not _has_web_area(accessor, app_el):
        return
    enabled = False
    for attr in ("AXManualAccessibility", "AXEnhancedUserInterface"):
        try:
            if ax.AXUIElementSetAttributeValue(app_el, attr, True) == 0:
                enabled = True
        except Exception:
            pass
    if enabled:
        time.sleep(_WEB_A11Y_SETTLE_S)  # Chromium builds the render tree asynchronously


def _find_app(app: str) -> tuple[int, str]:
    """Resolve a bundle id or display name to (pid, bundle id)."""
    from AppKit import NSWorkspace

    from a11y_computer_use.server import _match_running_app

    match = _match_running_app(NSWorkspace.sharedWorkspace().runningApplications(), app)
    if match is None:  # maybe launched since the list was last refreshed
        from a11y_computer_use.safety import refresh_workspace

        refresh_workspace()
        match = _match_running_app(NSWorkspace.sharedWorkspace().runningApplications(), app)
    if match is None:
        raise ComputerUseError(
            ErrorCode.APP_NOT_FOUND,
            f"no running application matches {app!r}",
            detail={"app": app},
        )
    running, bundle = match
    return int(running.processIdentifier()), bundle or app


def _display_geometry() -> tuple[DisplayGeometry, ...]:
    """Enumerate attached displays as `DisplayGeometry` (needs no TCC grant).

    Per-display metadata (physical pixels, backing scale) comes from
    `capture.displays` so observe and capture cannot drift apart on what
    "physical pixels" means. (``CGDisplayPixelsWide`` returns *points* on
    Retina — using it here once emitted every snapshot at half scale while
    ``act`` divided clicks by the true backing scale.)
    """
    import Quartz

    from a11y_computer_use import capture

    geometry = []
    for display in capture.displays():
        rect = Quartz.CGDisplayBounds(display.display_id)
        geometry.append(
            DisplayGeometry(
                display=display,
                origin=(float(rect.origin.x), float(rect.origin.y)),
            )
        )
    return tuple(geometry)


def project_global_rect(
    position: tuple[float, float], size: tuple[float, float]
) -> Bounds | None:
    """Project a global-point rect (AX / CGWindowList space) onto its display.

    Returns the display-qualified physical-pixel `Bounds` the schema
    mandates, or None when the rect is zero-size or fully offscreen. For
    callers outside the snapshot pipeline (e.g. ``window list`` rows).
    """
    return _to_bounds(position, size, _display_geometry())
