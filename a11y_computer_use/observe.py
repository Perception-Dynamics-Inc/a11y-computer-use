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

#: Kept-children cap per node. Overflow is elided with a "… N more" marker,
#: keeping interactive/labelled children preferentially.
MAX_CHILDREN = 24

#: Tighter per-widget cap for dense, repetitive containers (grids/tables/
#: outlines — e.g. Calendar's month grid, a spreadsheet). These blow the
#: snapshot token budget with near-identical rows, so cap them harder; the
#: elision marker tells the agent to scroll/re-observe for the rest (COM-12).
DENSE_MAX_CHILDREN = 12
_DENSE_CONTAINER_ROLES = frozenset({"AXGrid", "AXTable", "AXOutline"})

#: Raw children inspected per node before giving up — virtualized lists can
#: report thousands of rows and each read costs several AX round-trips.
_MAX_WALK_CHILDREN = 200

_MAX_VALUE_CHARS = 200  #: Element.value cap (anchors never use value)
_RENDER_VALUE_CHARS = 48  #: value cap in the text rendering
_MAX_EPOCHS = 8  #: snapshot epochs kept in the render registry
_AMBIGUITY_PX = 2.0  #: distance tie window that makes re-resolution ambiguous
_WEAK_ANCHOR_DRIFT_PX = 400.0  #: max drift when only one of title/path matches

_SECURE_ROLE = "AXSecureTextField"
_DECORATIVE_ROLES = frozenset({"AXUnknown", "AXSplitter", "AXGrowArea"})
_WRAPPER_ROLES = frozenset(
    {"AXGroup", "AXGenericElement", "AXLayoutArea", "AXScrollArea", "AXSplitGroup"}
)
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
_EDITABLE_ROLES = frozenset(
    {"AXComboBox", "AXSearchField", _SECURE_ROLE, "AXTextArea", "AXTextField"}
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
#: switch panes, sliders take values. Cells are folded into their row's text.
_INTERACTIVE_ROLES = frozenset({"AXRow", "AXTab", "AXSlider", "AXIncrementor"})
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
    if root is not None:
        pruned = _prune_root(root, accessor, tuple(geometry))
        _flatten(pruned, None, (), snapshot_id, elements, elisions, handles)
    _register_epoch(snapshot_id, elisions, handles)
    return Snapshot(
        snapshot_id=snapshot_id,
        scope=scope,
        app=app,
        pid=pid,
        created_at=time.time(),
        displays=tuple(g.display for g in geometry),
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


def rematch_ref(snap: Snapshot, ref: str, live: Snapshot) -> Element:
    """Re-resolve ``ref`` (issued by ``snap``) against an already-captured
    ``live`` snapshot via the shared anchor matcher — the backend-agnostic core
    of every driver's ``resolve_ref``.

    Each `Driver` captures ``live`` through its own backend (macOS via
    `observe.snapshot`, Linux/browser via ``driver.snapshot``) and calls this, so
    the match-or-raise logic and the STALE_REF payload live in exactly one place.

    Raises:
        KeyError: if ``ref`` was never part of ``snap`` (see `Snapshot.element`).
        ComputerUseError: `ErrorCode.STALE_REF` when the element no longer exists
            or the anchors no longer match unambiguously — with near-miss
            candidates so the agent can retry a likely ref without re-observing.
    """
    anchor = snap.element(ref)
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
        return _appservices().AXUIElementSetAttributeValue(handle, "AXValue", value) == 0
    except Exception:
        return False


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
    Deterministic for a given snapshot.
    """
    if mode not in RENDER_MODES:
        raise ValueError(f"mode must be one of {RENDER_MODES}")
    if mode == "interactive":
        entries = _interactive_entries(snap, include_bounds)
    else:
        entries = _full_entries(snap, include_bounds)
    return "\n".join(_apply_budget(entries, budget))


def _children_map(snap: Snapshot) -> dict[str | None, list[Element]]:
    children: dict[str | None, list[Element]] = {}
    for el in snap.elements:
        children.setdefault(el.parent, []).append(el)
    return children


def _full_entries(snap: Snapshot, include_bounds: bool) -> list[tuple[str, bool]]:
    """(line, is_element_line) pairs for the full view."""
    elisions = _EPOCHS.get(snap.snapshot_id, {})
    children = _children_map(snap)
    bounds: bool | None = True if include_bounds else None
    entries = [(f"[{snap.snapshot_id}] {snap.app or 'display'} ({snap.scope.value})", False)]

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
        (f"[{snap.snapshot_id}] {snap.app or 'display'} ({snap.scope.value}) interactive", False)
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
                    items.append(text.replace("\n", " "))
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
    `find` tool. Pure and platform-free (operates on the canonical snapshot, so
    it works identically on every backend).

    Args:
        text: case-insensitive substring matched against an element's title or
            value (either may contain it).
        role: case-insensitive substring of the role, with an optional ``AX``
            prefix ignored, so ``"button"`` matches ``AXButton``/``AXMenuButton``
            and ``"AXTextField"`` matches exactly.
        editable / clickable: keep only elements whose flag equals the given
            boolean.

    Returns the matches in the snapshot's pre-order, so refs read top-to-bottom.
    """
    needle = text.lower() if text else None
    role_needle = role.lower().removeprefix("ax") if role else None
    out: list[Element] = []
    for el in snap.elements:
        if needle is not None and needle not in f"{el.title} {el.value or ''}".lower():
            continue
        if role_needle is not None and role_needle not in el.role.lower().removeprefix("ax"):
            continue
        if editable is not None and el.editable is not editable:
            continue
        if clickable is not None and el.clickable is not clickable:
            continue
        out.append(el)
    return tuple(out)


def render_matches(snap: Snapshot, matches: Sequence[Element]) -> str:
    """Render `find_elements` results as compact lines — one per match with its
    ref, role, title, value, flags, and bounds (unlike the tree render, bounds
    show on every match since results are a flat list, not a nested tree)."""
    if not matches:
        return f"[{snap.snapshot_id}] {snap.app or 'display'}: no elements match"
    lines = [f"[{snap.snapshot_id}] {snap.app or 'display'}: {len(matches)} match(es)"]
    for el in matches:
        line = _render_line(el)
        if el.parent is not None:  # _render_line prints bounds only for roots
            b = el.bounds
            line += f" [{b.width}x{b.height} @{b.display_id}:{b.x},{b.y}]"
        lines.append("  " + line)
    return "\n".join(lines)


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
    if (ab.x, ab.y, ab.width, ab.height) != (bb.x, bb.y, bb.width, bb.height):
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
                text_changes.append(f"{new.ref} {_clip(str(a), 24)}→{_clip(str(b), 24)}")
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
            f"{attr}: {_clip(str(a), 24)}→{_clip(str(b), 24)}" for attr, (a, b) in changes.items()
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
# Pruning engine
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _PNode:
    """One kept node of the pruned tree, pre-flattening."""

    raw: RawNode
    bounds: Bounds
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
    pruned = _prune_inner(node, raw, accessor, geometry, depth=0, kept_depth=0)
    if pruned is not None:
        return pruned
    # Roots survive even when pruning would drop them (offscreen/decorative).
    bounds = _project(raw.position, raw.size, main)
    return _PNode(raw=raw, bounds=bounds, children=[], elided=0, has_interactive=False, node=node)


def _wrapper_candidate(raw: RawNode, depth: int) -> bool:
    """Could this node collapse onto a single kept child? The child-independent
    half of the collapse test (the other half is "exactly one kept child and
    nothing elided", known only after its subtree is pruned)."""
    clickable, editable, _ = _flags(raw)
    return (
        depth > 0
        and raw.role in _WRAPPER_ROLES
        and not (clickable or editable)
        and not raw.title
        and not raw.description
        and raw.value is None
    )


def _prune_inner(
    node: object,
    raw: RawNode,
    accessor: TreeAccessor,
    geometry: tuple[DisplayGeometry, ...],
    depth: int,
    kept_depth: int,
) -> _PNode | None:
    """``depth`` is the raw tree depth; ``kept_depth`` is the exact pruned depth
    of this node. A wrapper candidate adds a level for its children only when it
    will NOT collapse, which is decided from the children's raw attributes before
    recursing, so `MAX_DEPTH` is applied during the walk (bounding its cost) and
    a node whose pruned depth is under the cap is never elided."""
    bounds = _to_bounds(raw.position, raw.size, geometry)
    if bounds is None or _is_decorative(raw):
        return None  # zero-size, fully offscreen, or decorative: drop subtree

    kept: list[_PNode] = []
    elided = 0
    raw_children = tuple(accessor.children(node))
    candidate = _wrapper_candidate(raw, depth)
    if kept_depth >= MAX_DEPTH or depth >= _MAX_RAW_DEPTH:
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
        # walk. The reads happen here anyway.
        read_children = [(child, accessor.read(child)) for child in walked]
        survivors = sum(
            1
            for _, child_raw in read_children
            if _to_bounds(child_raw.position, child_raw.size, geometry) is not None
            and not _is_decorative(child_raw)
        )
        collapses = candidate and survivors == 1 and elided == 0
        child_kept_depth = kept_depth if collapses else kept_depth + 1
        for child, child_raw in read_children:
            pruned = _prune_inner(child, child_raw, accessor, geometry, depth + 1, child_kept_depth)
            if pruned is not None:
                kept.append(pruned)
        cap = DENSE_MAX_CHILDREN if raw.role in _DENSE_CONTAINER_ROLES else MAX_CHILDREN
        if len(kept) > cap:
            kept, dropped = _cap_children(kept, cap)
            elided += dropped

    clickable, editable, _ = _flags(raw)
    interactive = clickable or editable
    if candidate and len(kept) == 1 and elided == 0:
        return kept[0]  # collapse single-child wrapper (keeps the child's own handle)
    return _PNode(
        raw=raw,
        bounds=bounds,
        children=kept,
        elided=elided,
        has_interactive=interactive or any(c.has_interactive for c in kept),
        node=node,
    )


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


def _is_decorative(raw: RawNode) -> bool:
    if raw.role in _DECORATIVE_ROLES:
        return True
    labelled = bool(raw.title or raw.description)
    if raw.role == "AXImage" and not labelled:
        return True
    return raw.role == "AXStaticText" and not labelled and raw.value in (None, "")


def _flags(raw: RawNode) -> tuple[bool, bool, bool]:
    """Derive (clickable, editable, secure) from role/subrole/actions."""
    secure = raw.role == _SECURE_ROLE or raw.subrole == _SECURE_ROLE
    clickable = bool(_PRESS_ACTIONS.intersection(raw.actions)) or raw.role in _CLICKABLE_ROLES
    editable = secure or raw.role in _EDITABLE_ROLES
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
) -> None:
    """Assign pre-order refs and emit `Element`s (parents before children)."""
    ref = f"e{len(out) + 1}"
    path = parent_path + (node.raw.role,)
    clickable, editable, secure = _flags(node.raw)
    if node.node is not None:  # retain the live handle for act-time AX actions
        handles[ref] = node.node
    value: str | None = None
    if node.raw.value is not None and not secure:  # secure fields never leak values
        value = _clip(str(node.raw.value), _MAX_VALUE_CHARS)
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
        )
    )
    if node.elided:
        elisions[ref] = node.elided
    for child in node.children:
        _flatten(child, ref, path, snapshot_id, out, elisions, handles)


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


def _render_line(el: Element, *, bounds: bool | None = None, compact: bool = False) -> str:
    """One element line. ``bounds``: None prints geometry on roots only (the
    full view's rule), True on every line, False never. ``compact`` (the
    interactive view) leaves the ``click`` flag implicit on roles that are
    pressable by definition (buttons, links, menu items, checkboxes, tabs)."""
    role = el.role[2:].lower() if el.role.startswith("AX") else el.role.lower()
    parts = [el.ref, role]
    if el.title:
        parts.append(f'"{el.title}"')
    if el.value is not None:
        parts.append(f'="{_clip(el.value.replace(chr(10), " "), _RENDER_VALUE_CHARS)}"')
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
        )
        if on
    ]
    if flags:
        parts.append(f"({','.join(flags)})")
    if bounds is True or (bounds is None and el.parent is None):
        b = el.bounds
        parts.append(f"[{b.width}x{b.height} @{b.display_id}:{b.x},{b.y}]")
    return " ".join(parts)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


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


def _register_epoch(
    snapshot_id: str, elisions: dict[str, int], handles: dict[str, object]
) -> None:
    _EPOCHS[snapshot_id] = elisions
    _HANDLES[snapshot_id] = handles
    while len(_EPOCHS) > _MAX_EPOCHS:
        _EPOCHS.popitem(last=False)
    while len(_HANDLES) > _MAX_EPOCHS:
        _HANDLES.popitem(last=False)


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

    same_role = [el for el in live.elements if el.role == anchor.role]
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


def _center_distance(a: Bounds, b: Bounds) -> float:
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
        if el.role != anchor.role:
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
        same_role = [el for el in live.elements if el.role == anchor.role]
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
