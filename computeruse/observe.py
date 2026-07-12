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

import itertools
import math
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from computeruse.schema import (
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

#: Traversal depth cap. Nodes deeper than this are elided (with a marker on
#: the parent); also bounds walk cost on pathological/cyclic trees.
MAX_DEPTH = 12

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

_DOCTOR_HINT = (
    "Grant Accessibility to the host app that launched this process in "
    "System Settings > Privacy & Security > Accessibility, then retry; "
    "`computeruse doctor` names the exact host app that needs the grant."
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
    anchor = snap.element(ref)
    if live is None:
        live = snapshot(snap.scope, app=snap.app)
    match, reason = _match_anchor(anchor, live)
    if match is None:
        raise ComputerUseError(
            ErrorCode.STALE_REF,
            f"{ref} ({anchor.role} {anchor.title!r}) no longer resolves; re-observe",
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


def render_text(snap: Snapshot) -> str:
    """Render ``snap`` as compact indented text for LLM consumption.

    One line per element: ``ref role "title" ="value" (flags)``, two-space
    indentation, geometry only on roots. Children elided by the depth or
    fan-out caps show as ``… N more`` markers when ``snap`` was produced by
    this module (foreign snapshots render without markers).
    """
    elisions = _EPOCHS.get(snap.snapshot_id, {})
    children: dict[str | None, list[Element]] = {}
    for el in snap.elements:
        children.setdefault(el.parent, []).append(el)
    lines = [f"[{snap.snapshot_id}] {snap.app or 'display'} ({snap.scope.value})"]

    def emit(el: Element, depth: int) -> None:
        lines.append("  " * depth + _render_line(el))
        for child in children.get(el.ref, ()):
            emit(child, depth + 1)
        elided = elisions.get(el.ref, 0)
        if elided:
            lines.append("  " * (depth + 1) + f"… {elided} more")

    for root in children.get(None, ()):
        emit(root, 1)
    return "\n".join(lines)


def estimate_tokens(snap: Snapshot) -> int:
    """Estimate the LLM token footprint of ``snap``'s serialized form.

    Used to enforce the per-snapshot budget (~1k tokens) and to publish the
    per-app coverage table from Phase 0. Heuristic: ``chars / 4`` over the
    `render_text` form, rounded up.
    """
    return (len(render_text(snap)) + 3) // 4


def interactive_count(snap: Snapshot) -> int:
    """How many elements the model can actually act on (clickable or editable).

    Zero is the a11y→vision handoff signal: a custom-drawn app (Telegram, many
    games, some Electron before `AXManualAccessibility`) exposes a shell with
    no actionable refs, so the caller should fall back to screenshot+coordinates.
    """
    return sum(1 for el in snap.elements if el.clickable or el.editable)


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
    pruned = _prune_inner(node, raw, accessor, geometry, depth=0)
    if pruned is not None:
        return pruned
    # Roots survive even without usable geometry (AXApplication has none):
    # project against the main display, or cover it entirely.
    main = next((g for g in geometry if g.display.is_main), geometry[0])
    if raw.position is not None and raw.size is not None:
        bounds = _project(raw.position, raw.size, main)
    else:
        bounds = Bounds(main.display.display_id, 0, 0, main.display.width, main.display.height)
    return _PNode(raw=raw, bounds=bounds, children=[], elided=0, has_interactive=False, node=node)


def _prune_inner(
    node: object,
    raw: RawNode,
    accessor: TreeAccessor,
    geometry: tuple[DisplayGeometry, ...],
    depth: int,
) -> _PNode | None:
    bounds = _to_bounds(raw.position, raw.size, geometry)
    if bounds is None or _is_decorative(raw):
        return None  # zero-size, fully offscreen, or decorative: drop subtree

    kept: list[_PNode] = []
    elided = 0
    raw_children = tuple(accessor.children(node))
    if depth >= MAX_DEPTH:
        elided = len(raw_children)
    else:
        walked = raw_children[:_MAX_WALK_CHILDREN]
        elided = len(raw_children) - len(walked)
        for child in walked:
            pruned = _prune_inner(child, accessor.read(child), accessor, geometry, depth + 1)
            if pruned is not None:
                kept.append(pruned)
        cap = DENSE_MAX_CHILDREN if raw.role in _DENSE_CONTAINER_ROLES else MAX_CHILDREN
        if len(kept) > cap:
            kept, dropped = _cap_children(kept, cap)
            elided += dropped

    clickable, editable, _ = _flags(raw)
    interactive = clickable or editable
    if (
        depth > 0
        and raw.role in _WRAPPER_ROLES
        and not interactive
        and not raw.title
        and not raw.description
        and raw.value is None
        and len(kept) == 1
        and elided == 0
    ):
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


def _render_line(el: Element) -> str:
    role = el.role[2:].lower() if el.role.startswith("AX") else el.role.lower()
    parts = [el.ref, role]
    if el.title:
        parts.append(f'"{el.title}"')
    if el.value is not None:
        parts.append(f'="{_clip(el.value.replace(chr(10), " "), _RENDER_VALUE_CHARS)}"')
    flags = [
        name
        for name, on in (
            ("click", el.clickable),
            ("edit", el.editable),
            ("secure", el.secure),
            ("focus", el.focused),
            ("disabled", not el.enabled),
        )
        if on
    ]
    if flags:
        parts.append(f"({','.join(flags)})")
    if el.parent is None:
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


def _match_anchor(anchor: Element, live: Snapshot) -> tuple[Element | None, str]:
    """Find ``anchor``'s counterpart in ``live``; (None, reason) on failure.

    Candidates must share the role and at least one strong anchor (title or
    path). Title+path matches win over partial matches; ties break by bounds
    proximity; a near-exact distance tie is ambiguous. Partial matches
    additionally must lie within `_WEAK_ANCHOR_DRIFT_PX`.
    """
    candidates: list[tuple[int, float, Element]] = []
    for el in live.elements:
        if el.role != anchor.role:
            continue
        score = (4 if el.title == anchor.title else 0) + (2 if el.path == anchor.path else 0)
        if score == 0:
            continue
        distance = _center_distance(anchor.bounds, el.bounds)
        if score < 6 and distance > _WEAK_ANCHOR_DRIFT_PX:
            continue
        candidates.append((score, distance, el))
    if not candidates:
        return None, "not_found"
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
        return RawNode(
            role=str(role) if role else "AXUnknown",
            subrole=str(subrole) if subrole else None,
            title=str(self._attr(node, "AXTitle") or ""),
            value=self._attr(node, "AXValue"),
            description=str(self._attr(node, "AXDescription") or ""),
            enabled=True if enabled is None else bool(enabled),
            focused=bool(self._attr(node, "AXFocused")),
            position=self._geometry(node, "AXPosition", self._point_type),
            size=self._geometry(node, "AXSize", self._size_type),
            actions=self._actions(node),
        )

    def children(self, node: object) -> Sequence[object]:
        return tuple(self._attr(node, "AXChildren") or ())

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


def _find_app(app: str) -> tuple[int, str]:
    """Resolve a bundle id or display name to (pid, bundle id)."""
    from AppKit import NSWorkspace

    needle = app.lower()
    for running in NSWorkspace.sharedWorkspace().runningApplications():
        bundle = running.bundleIdentifier()
        name = running.localizedName()
        if (bundle and bundle.lower() == needle) or (name and name.lower() == needle):
            return int(running.processIdentifier()), str(bundle) if bundle else app
    raise ComputerUseError(
        ErrorCode.APP_NOT_FOUND,
        f"no running application matches {app!r}",
        detail={"app": app},
    )


def _display_geometry() -> tuple[DisplayGeometry, ...]:
    """Enumerate attached displays as `DisplayGeometry` (needs no TCC grant).

    Per-display metadata (physical pixels, backing scale) comes from
    `capture.displays` so observe and capture cannot drift apart on what
    "physical pixels" means. (``CGDisplayPixelsWide`` returns *points* on
    Retina — using it here once emitted every snapshot at half scale while
    ``act`` divided clicks by the true backing scale.)
    """
    import Quartz

    from computeruse import capture

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
