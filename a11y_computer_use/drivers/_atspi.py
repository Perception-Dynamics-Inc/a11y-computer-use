"""AT-SPI2 adapter: presents the Linux accessibility tree to the SHARED,
platform-free pruning engine (`observe.build_snapshot`).

Same trick as the Windows adapter (`_uia.py`): map AT-SPI role names onto the
**same AX role vocabulary** the pruning engine keys off (`AXButton`,
`AXTextField`, `AXWindow`, ...) and AT-SPI action names onto the AX action names
(`AXPress`/`AXPick`). Then a Linux tree prunes/indexes through the identical
engine as macOS and Windows, with zero engine changes.

Binding: PyGObject's `gi.repository.Atspi` (the modern AT-SPI2 client). Imported
lazily so `drivers` stays import-safe on macOS/Windows. Every attribute read is
defensive — AT-SPI is a D-Bus protocol with no batch read, so any single call
can be slow or fail cross-process, and a flaky read must degrade to a sane
default, not crash the walk. (The batch/prefetch weakness AT-SPI has vs UIA's
`CacheRequest` / AX's multi-attribute reads is why we cap the fetched children:
see `_MAX_CHILDREN_FETCH`. A subtree cache/diff is the perf follow-up.)

Linux-only; imported lazily by `drivers/linux.py`.
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence

from a11y_computer_use.observe import MAX_CHILDREN, DisplayGeometry, RawNode
from a11y_computer_use.schema import Display

# AT-SPI role name (english, from get_role_name()) -> canonical AX role.
# Keyed by the human role-name string rather than the numeric Atspi.Role enum
# because the strings are stable across atspi2 versions and bindings.
_ROLE = {
    "push button": "AXButton",
    "toggle button": "AXButton",
    "spin button": "AXTextField",
    "button": "AXButton",
    "check box": "AXCheckBox",
    "check menu item": "AXMenuItem",
    "radio button": "AXRadioButton",
    "radio menu item": "AXMenuItem",
    "link": "AXLink",
    "entry": "AXTextField",
    "password text": "AXSecureTextField",
    "text": "AXTextArea",
    "document text": "AXTextArea",
    "terminal": "AXTextArea",  # VTE (gnome-terminal, tilix): its Text iface is the screen contents
    "document frame": "AXGroup",
    "document web": "AXGroup",
    "document email": "AXGroup",
    # A cross-origin iframe (reCAPTCHA's "I'm not a robot" lives in one).
    # Explicit, not the AXGroup fallback: the pruner restarts kept depth here.
    "internal frame": "AXGroup",
    "label": "AXStaticText",
    "static": "AXStaticText",
    "heading": "AXStaticText",
    "paragraph": "AXStaticText",
    "caption": "AXStaticText",
    "image": "AXImage",
    "icon": "AXImage",
    "frame": "AXWindow",
    "window": "AXWindow",
    "dialog": "AXWindow",
    "alert": "AXWindow",
    "file chooser": "AXWindow",
    "color chooser": "AXWindow",
    "menu item": "AXMenuItem",
    "menu bar": "AXMenuBar",
    "menu": "AXMenu",
    "popup menu": "AXMenu",
    "list": "AXList",
    "list box": "AXList",
    "list item": "AXRow",
    "tree": "AXOutline",
    "tree table": "AXOutline",
    "tree item": "AXRow",
    "table": "AXTable",
    "combo box": "AXComboBox",
    "tool bar": "AXToolbar",
    "scroll bar": "AXScrollBar",
    "slider": "AXSlider",  # Gtk.Scale and friends: draggable, carries a Value iface
    "scroll pane": "AXScrollArea",
    "viewport": "AXScrollArea",
    "page tab list": "AXTabGroup",
    "page tab": "AXButton",
    "panel": "AXGroup",
    "filler": "AXGroup",
    "section": "AXGroup",
    "grouping": "AXGroup",
    "redundant object": "AXUnknown",
    "separator": "AXSplitter",
    "unknown": "AXUnknown",
}

# AT-SPI role name -> RawNode.atspi_web. Only Chromium document/frame roles.
# Anything else stays "" so the pruner's macOS/Windows path is unchanged.
_ATSPI_WEB = {
    "document web": "page",
    "document frame": "docframe",
    "internal frame": "iframe",
}


def atspi_web_kind(role_name: str) -> str:
    """``page`` / ``docframe`` / ``iframe`` for a Chromium document or frame, else ``""``."""
    return _ATSPI_WEB.get((role_name or "").lower(), "")

# AT-SPI action name (lowercased) -> AX action the pruning engine treats as
# "interactive" (`_PRESS_ACTIONS` = AXPress/AXOpen/AXConfirm/AXPick).
# ``click`` on a real control is a press. Chrome also stamps ``click``,
# ``clickAncestor``, and ``showContextMenu`` on plain divs. ``clickAncestor``
# and ``showContextMenu`` are not presses: a context menu is not activation,
# and clickAncestor would mark every container that holds a click listener.
# A zero-size web wrapper that still receives AXPress from ``click`` is not a
# target; the pruner drops that flag (`observe._plain_zero_wrapper`).
_PRESS_ACTION_NAMES = frozenset(
    {"click", "press", "activate", "do default", "jump", "open", "toggle",
     "expand", "collapse", "expand or contract", "show", "showmenu", "menu"}
)
_PICK_ACTION_NAMES = frozenset({"select", "pick"})

#: Cap on children fetched per node. AT-SPI has no batch read (one D-Bus
#: round-trip per get_child_at_index), so fetching a virtualized 10k-row list is
#: ruinous; the engine only walks the first `_MAX_WALK_CHILDREN` (200) anyway,
#: so stop just above that. Overflow is elided by the engine's fan-out caps.
_MAX_CHILDREN_FETCH = 250

_inited = False


def _atspi():
    """The `Atspi` module, initialized once. Raises ImportError if PyGObject /
    the AT-SPI2 typelib are absent (the driver turns that into a clear message)."""
    global _inited
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    if not _inited:
        _safe(Atspi.init)  # 0 = ok, 1 = already running; both fine
        # Bound per-call D-Bus wait: a hung app stalls one read ~300ms, not the
        # libatspi default (~800ms), so one bad node can't wreck a snapshot.
        _safe(lambda: Atspi.set_timeout(300, 15000))
        _inited = True
    return Atspi


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


_a11y_status_forced = False


def enable_a11y_status() -> bool:
    """Force the desktop's accessibility 'active' flags on via D-Bus so already-
    running Chromium/Electron apps (Chrome, Slack, VS Code, Discord, Electron)
    build their AT-SPI tree WITH NO RELAUNCH — the Linux equivalent of the macOS
    AXEnhancedUserInterface trick, and the counter to Grok's a11y-OFF desktop.

    Chromium enables renderer accessibility when ``org.a11y.Status.IsEnabled`` /
    ``ScreenReaderEnabled`` go true on the session bus (the at-spi-bus-launcher
    owns ``org.a11y.Bus`` at ``/org/a11y/bus``); it listens for the change live.
    Idempotent + cached per process. Opt out with A11Y_COMPUTER_USE_NO_WEB_A11Y=1.
    Returns True if the flags are (now) set."""
    global _a11y_status_forced
    if _a11y_status_forced:
        return True
    if os.environ.get("A11Y_COMPUTER_USE_NO_WEB_A11Y"):
        return False
    try:
        import gi

        gi.require_version("Atspi", "2.0")  # ensure the a11y stack is present
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        def _set(prop: str) -> None:
            bus.call_sync(
                "org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties", "Set",
                GLib.Variant("(ssv)", ("org.a11y.Status", prop, GLib.Variant("b", True))),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )

        for prop in ("IsEnabled", "ScreenReaderEnabled"):
            _safe(lambda p=prop: _set(p))
        _a11y_status_forced = True
        return True
    except Exception:
        return False


def _call_first(obj, names, *args, default=None):
    """Call the first method in ``names`` that exists on ``obj`` (binding-version
    tolerant — atspi2 renamed a few getters across releases)."""
    for name in names:
        method = getattr(obj, name, None)
        if method is not None:
            return _safe(lambda m=method: m(*args), default)
    return default


# ---------------------------------------------------------------------------
# Per-node reads (each defensive; each may be a D-Bus round-trip)
# ---------------------------------------------------------------------------


def _role_name(acc) -> str:
    return (_call_first(acc, ("get_role_name",), default="") or "").lower()


def _component(acc):
    return _call_first(acc, ("get_component_iface", "get_component"))


def _extents(acc, *, keep_zero: bool = False):
    """(position, size) in SCREEN pixels, or (None, None).

    A zero width or height is ``(None, None)`` for hit-testing and scrolling.
    The snapshot read passes ``keep_zero=True`` so a 0-height Chromium section
    still carries its size into the pruner, which keeps the painted children
    under a web document. Callers that do not opt in are unchanged.
    """
    Atspi = _atspi()
    comp = _component(acc)
    if comp is None:
        return None, None
    coord = getattr(getattr(Atspi, "CoordType", None), "SCREEN", 0)
    rect = _safe(lambda: comp.get_extents(coord))
    if rect is None:
        return None, None
    w = float(getattr(rect, "width", 0) or 0)
    h = float(getattr(rect, "height", 0) or 0)
    if w < 0 or h < 0:
        # GTK's "no allocation of my own" sentinel (-1, -1, -1, -1): a notebook
        # page tab whose label is hidden. Passed through as a negative extent so
        # the engine keeps the page's content below it instead of dropping it.
        return (-1.0, -1.0), (-1.0, -1.0)
    if w == 0 or h == 0:
        if keep_zero:
            return (float(getattr(rect, "x", 0) or 0), float(getattr(rect, "y", 0) or 0)), (w, h)
        return None, None
    return (float(getattr(rect, "x", 0) or 0), float(getattr(rect, "y", 0) or 0)), (w, h)


def _action_iface(acc):
    return _call_first(acc, ("get_action_iface", "get_action"))


def _action_names(acc) -> tuple[str, ...]:
    """AX action names for this node, mapped from its AT-SPI actions."""
    action = _action_iface(acc)
    if action is None:
        return ()
    n = _call_first(action, ("get_n_actions", "get_nActions"), default=0) or 0
    out: list[str] = []
    for i in range(int(n)):
        raw = (_call_first(action, ("get_action_name", "get_name"), i, default="") or "").lower()
        if raw in _PRESS_ACTION_NAMES:
            out.append("AXPress")
        elif raw in _PICK_ACTION_NAMES:
            out.append("AXPick")
    return tuple(dict.fromkeys(out))  # de-dup, preserve order


# Roles that never carry a text/numeric value — reading one costs two wasted
# D-Bus round-trips per node (Text + Value iface probes that always fail), and
# groups/buttons/windows are the bulk of a tree. Their label lives in the Name
# (title), not the value, so skipping the value read is a pure speedup.
_NO_VALUE_ROLES = frozenset({
    "AXButton", "AXImage", "AXGroup", "AXWindow", "AXToolbar", "AXMenuBar",
    "AXMenu", "AXScrollBar", "AXScrollArea", "AXSplitter", "AXTabGroup", "AXUnknown",
})


def _value_text(acc, role: str) -> object | None:
    """The node's current value: text contents for text roles, numeric value
    for sliders/progress. Secure fields never have their value read here (the
    engine also blanks AXSecureTextField values).

    Text is read via the explicit interface class form ``Atspi.Text.get_text(acc,
    0, -1)``. The instance form (``acc.get_text_iface().get_text(a, b)``) resolves
    to ``Atspi.Accessible.get_text`` (a 1-arg method) on this binding and raises
    TypeError — which, swallowed defensively, silently blanked every field value.
    Calling the interface method with the accessible as the first argument avoids
    the name collision. Non-Text accessibles make the call raise → None."""
    if role == "AXSecureTextField":
        return None
    Atspi = _atspi()
    count = _safe(lambda: Atspi.Text.get_character_count(acc))
    if count:
        got = _safe(lambda: Atspi.Text.get_text(acc, 0, -1))
        if got:
            return got
    cur = _safe(lambda: Atspi.Value.get_current_value(acc))
    if cur is not None:
        return cur
    return None


def _state_flags(acc):
    """(enabled, focused, checked, selected, expanded, focusable) from the state set.

    checked/expanded are None when the element is not checkable/expandable so
    the shared schema can tell "off" apart from "not a checkbox"."""
    Atspi = _atspi()
    sset = _call_first(acc, ("get_state_set",))
    if sset is None:
        return True, False, None, False, None, False
    st = getattr(Atspi, "StateType", None)

    def has(name: str) -> bool:
        member = getattr(st, name, None)
        return bool(member is not None and _safe(lambda: sset.contains(member), False))

    enabled = has("ENABLED") or has("SENSITIVE")
    focused = has("FOCUSED")
    checked = has("CHECKED") if (has("CHECKABLE") or has("CHECKED")) else None
    selected = has("SELECTED")
    expanded = has("EXPANDED") if has("EXPANDABLE") else None
    focusable = has("FOCUSABLE")
    return enabled, focused, checked, selected, expanded, focusable


# ARIA role (AT-SPI 'xml-roles' attribute, set by Chromium/GTK for web content)
# -> canonical AX role. Used to sharpen a generic AT-SPI role (a role="button"
# <div> is a 'section'/'panel' to AT-SPI but an AXButton to us). Standard HTML
# already gets a proper AT-SPI role, so this only refines generic containers.
_XML_ROLE = {
    "button": "AXButton", "link": "AXLink", "textbox": "AXTextField",
    "searchbox": "AXSearchField", "checkbox": "AXCheckBox", "radio": "AXRadioButton",
    "tab": "AXButton", "menuitem": "AXMenuItem", "menuitemcheckbox": "AXMenuItem",
    "menuitemradio": "AXMenuItem", "combobox": "AXComboBox", "switch": "AXCheckBox",
    "slider": "AXSlider", "option": "AXRow", "listbox": "AXList", "tablist": "AXTabGroup",
}


def _get_attributes(acc) -> dict:
    """The AT-SPI object attributes as a plain dict (xml-roles, id, tag, class,
    …), fetched ONCE per node so stable-id + web-role both reuse it — one D-Bus
    round-trip instead of two (AT-SPI has no batch read). {} on any failure."""
    a = _call_first(acc, ("get_attributes",))
    if a is None:
        return {}
    if isinstance(a, dict):
        return a
    if hasattr(a, "keys") and hasattr(a, "get"):  # GLib.HashTable-ish
        try:
            return {str(k): (a.get(k) and str(a.get(k))) for k in a.keys()}
        except Exception:
            return {}
    return {}


def _refine_web_role(role: str, attrs: dict) -> str:
    """Upgrade a generic container role to the ARIA role its xml-roles declares,
    so ARIA-widget <div>s become real interactive elements in the snapshot."""
    if role == "AXGroup" and attrs:
        for token in (attrs.get("xml-roles") or "").split():
            if token in _XML_ROLE:
                return _XML_ROLE[token]
    return role


def _stable_id(acc, attrs: dict | None = None) -> str | None:
    """A layout-independent id for ``acc``: the AT-SPI ``accessible-id``, else a
    web element's DOM id from the object attributes (Chromium exposes 'id').
    None when the app assigns none."""
    sid = _call_first(acc, ("get_accessible_id",))
    if sid:
        return str(sid)
    attrs = attrs if attrs is not None else _get_attributes(acc)
    for key in ("id", "html-id", "xml-id"):
        val = attrs.get(key)
        if val:
            return str(val)
    return None


class _BandRect:
    """Screen box for a snapshot-only row group. Not an AT-SPI rect."""

    def __init__(self, x: float, y: float, width: float, height: float) -> None:
        self.x = x
        self.y = y
        self.width = width
        self.height = height


class _OnScreenBand:
    """Group of on-screen rows the snapshot walk can keep. Not an AT-SPI object.

    The shared pruner keeps at most ``MAX_CHILDREN`` children of one node and
    drops the rest. A Chromium list can paint more rows than that (29 rows of
    18px in a 520px box). A band stays within the cap, so find still matches
    the last painted row. A shorter run is not wrapped.
    """

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        left = min(float(pos[0]) for _acc, pos, _size in rows)
        top = min(float(pos[1]) for _acc, pos, _size in rows)
        right = max(float(pos[0]) + float(size[0]) for _acc, pos, size in rows)
        bottom = max(float(pos[1]) + float(size[1]) for _acc, pos, size in rows)
        self._rect = _BandRect(left, top, max(1.0, right - left), max(1.0, bottom - top))

    def get_role_name(self) -> str:
        return "panel"

    def get_name(self) -> str:
        return ""

    def get_description(self) -> str:
        return ""

    def get_child_count(self) -> int:
        return len(self._rows)

    def get_child_at_index(self, index: int):
        return self._rows[int(index)][0]

    def get_component_iface(self):
        return self

    def get_extents(self, _coord):
        return self._rect


def _bands_for_snapshot(rows: list[tuple]) -> list:
    """Row accessibles, grouped when they would exceed the snapshot child cap."""
    if len(rows) <= MAX_CHILDREN:
        return [acc for acc, _pos, _size in rows]
    return [
        _OnScreenBand(rows[start:start + MAX_CHILDREN])
        for start in range(0, len(rows), MAX_CHILDREN)
    ]


def _cached_rows_for_snapshot(bounds: dict, accs: list) -> list:
    """The saved row list, grouped the same way as a live walk.

    A saved row with no box is returned flat, which is the previous list.
    """
    rows = []
    for acc in accs:
        found = bounds.get(id(acc))
        if found is None:
            return list(accs)
        pos, size = found
        rows.append((acc, pos, size))
    return _bands_for_snapshot(rows)


class ATSPIAccessor:
    """`observe.TreeAccessor` over `Atspi.Accessible` handles.

    For a Chromium list, the snapshot lists every row of the list node it
    is reading whose own top is on or below the list's top and which
    extends below the clipped top edge. A row flush with that top is on
    screen. The walk does not stop after 16 rows: that number is the
    hit-test sample count, and using it as the row set omitted painted
    rows with no elision marker. When more rows are on screen than the
    shared child cap, they are grouped so the cap does not drop the tail.
    A saved head from a different Python wrapper is not that list: a live
    walk wraps a new object each time, and the 0.4.16 retest still showed
    the pre-scroll row at y=-2. ``scroll_to_find`` searches this snapshot.
    A non-Chromium tree is read from ``get_child_at_index`` as before.
    """

    def __init__(self) -> None:
        self._visible_children: dict[int, list] = {}
        self._visible_bounds: dict[int, tuple] = {}

    def refresh_visible(self, root: object) -> None:
        """Point Chromium lists at the rows inside their boxes.

        No-op unless the tree's toolkit is Chromium. The cached child list is
        left untouched on the accessible. Rows are taken from the list node
        this walk is reading. A saved head stored on a different wrapper is
        not reused ahead of those rows.
        """
        self._visible_children.clear()
        self._visible_bounds.clear()
        if root is None or not _chromium_app(root):
            return
        for container in _list_containers(root):
            rows = _shown_or_probe(container)
            if len(_row_names(rows)) < 2:
                continue
            self._visible_children[id(container)] = [acc for acc, _pos, _size in rows]
            for acc, pos, size in rows:
                self._visible_bounds[id(acc)] = (pos, size)

    def read(self, node: object) -> RawNode:
        role_str = _role_name(node)
        role = _ROLE.get(role_str, "AXGroup")
        # GTK3 gives a single-line Gtk.Entry the same "text" role as a
        # Gtk.TextView. The entry carries SINGLE_LINE; the view carries
        # MULTI_LINE. A single-line entry is a text field, not a textarea.
        if role_str == "text" and _state_has(node, "SINGLE_LINE") and not _state_has(node, "MULTI_LINE"):
            role = "AXTextField"
        attrs = _get_attributes(node)  # one D-Bus fetch, reused for role + id
        role = _refine_web_role(role, attrs)
        override = self._visible_bounds.get(id(node))
        if override is not None:
            position, size = override
        else:
            # Keep a 0-height section's size. Hit-testing still treats it as
            # no box; only the snapshot walk needs the zero extent.
            position, size = _extents(node, keep_zero=True)
        enabled, focused, checked, selected, expanded, focusable = _state_flags(node)
        name = _call_first(node, ("get_name",), default="") or ""
        description = _call_first(node, ("get_description",), default="") or ""
        return RawNode(
            role=role,
            subrole=None,
            title=str(name),
            description=str(description),
            enabled=enabled,
            focused=focused,
            focusable=focusable,
            position=position,
            size=size,
            actions=_action_names(node),
            # skip the value probe (2 D-Bus calls) on roles that never have one
            value=None if role in _NO_VALUE_ROLES else _value_text(node, role),
            checked=checked,
            selected=selected,
            expanded=expanded,
            stable_id=_stable_id(node, attrs),
            atspi_web=atspi_web_kind(role_str),
        )

    def children(self, node: object) -> Sequence[object]:
        # The pruner's list object is not the wrapper ``refresh_visible``
        # probed. Keying the overlay by ``id()`` published the cached first
        # child, whose live top was above the viewport (y=-2, y=-18, y=-26
        # on the 0.4.16 retest). Read this node.
        if _role_name(node) in _LIST_ROLES and _chromium_app(node):
            live = _in_view_named_rows(node)
            if len(_row_names(live)) >= 2:
                for acc, pos, size in live:
                    self._visible_bounds[id(acc)] = (pos, size)
                return _bands_for_snapshot(live)
        visible = self._visible_children.get(id(node))
        if visible is not None:
            return _cached_rows_for_snapshot(self._visible_bounds, visible)
        count = _call_first(node, ("get_child_count", "get_childCount"), default=0) or 0
        count = min(int(count), _MAX_CHILDREN_FETCH)
        kids = []
        for i in range(count):
            child = _call_first(node, ("get_child_at_index", "getChildAtIndex"), i)
            if child is not None:
                kids.append(child)
        return kids


# ---------------------------------------------------------------------------
# Geometry, app resolution, act-time helpers
# ---------------------------------------------------------------------------


def primary_geometry() -> tuple[DisplayGeometry, ...]:
    """The primary X screen as one `DisplayGeometry`. AT-SPI SCREEN extents are
    physical pixels with a top-left origin, so scale=1.0 makes the engine's
    point->pixel projection an identity (same as the Windows adapter)."""
    width, height = _screen_size()
    display = Display(display_id=0, width=width, height=height, scale=1.0, is_main=True)
    return (DisplayGeometry(display=display, origin=(0.0, 0.0)),)


def _screen_size() -> tuple[int, int]:
    """Primary screen size in pixels: Xlib, then $A11Y_COMPUTER_USE_SCREEN, then 1280x800.

    A too-small guess would make the engine drop real nodes as "offscreen", so
    err large; the value only bounds the projected coordinate space."""
    try:
        from Xlib import display as _xd

        d = _xd.Display()
        try:
            screen = d.screen()
            w = int(screen.width_in_pixels)
            h = int(screen.height_in_pixels)
        finally:
            try:
                d.close()
            except Exception:
                pass
        if w > 0 and h > 0:
            return w, h
    except Exception:
        pass
    env = os.environ.get("A11Y_COMPUTER_USE_SCREEN", "")
    if "x" in env:
        try:
            w_s, h_s = env.lower().split("x", 1)
            return int(w_s), int(h_s)
        except Exception:
            pass
    return 1280, 800


def pid_of(acc) -> int | None:
    if acc is None:
        return None
    pid = _call_first(acc, ("get_process_id",))
    return int(pid) if pid else None


def find_root(app: str, scope) -> object | None:
    """The AT-SPI root for ``app`` at ``scope``.

    ``app`` matches an application accessible by name substring (case-insensitive)
    — the Linux analog of a bundle id / process exe. For `Scope.APP` the app
    accessible is returned; for `Scope.WINDOW` its active (else first) top-level
    frame. None when nothing matches (the engine yields an empty snapshot)."""
    from a11y_computer_use.schema import Scope

    Atspi = _atspi()
    needle = (app or "").lower()
    if not needle:
        return None
    desktop = _safe(lambda: Atspi.get_desktop(0))
    if desktop is None:
        return None
    count = _call_first(desktop, ("get_child_count",), default=0) or 0
    st = getattr(Atspi, "StateType", None)
    active = getattr(st, "ACTIVE", None)

    def _frames(acc):
        n = _call_first(acc, ("get_child_count",), default=0) or 0
        out = []
        for j in range(int(n)):
            frame = _call_first(acc, ("get_child_at_index",), j)
            if frame is not None:
                out.append(frame)
        return out

    def _is_active(frame) -> bool:
        sset = _call_first(frame, ("get_state_set",))
        return bool(active is not None and sset is not None and _safe(lambda: sset.contains(active), False))

    # Several applications can share a name: on Linux the permission-keying app
    # id is the process comm ("python3"), and every Python process that touched
    # AT-SPI (this one included) registers on the bus with that name and no
    # windows. Rank matches so a windowless registrant never shadows the real
    # app: active top-level frame first, then any app that owns frames, then
    # the first name match.
    # The a11y application name is the program name (GLib prgname, e.g.
    # "cuatestapp"), while the Linux app id used for permissions is the window
    # owner's comm (e.g. "python3"); match by PID as well as by name so an id
    # resolved from X11 finds the same application on the a11y bus.
    try:
        from a11y_computer_use.drivers import _linux_system

        pids = _linux_system.pids_matching(app)
    except Exception:  # noqa: BLE001 - X11 may be unavailable (Wayland/headless)
        pids = set()
    best = None
    best_rank = -1
    for i in range(int(count)):
        candidate = _call_first(desktop, ("get_child_at_index",), i)
        if candidate is None:
            continue
        name = (_call_first(candidate, ("get_name",), default="") or "").lower()
        if needle not in name and not (pids and pid_of(candidate) in pids):
            continue
        frames = _frames(candidate)
        rank = 2 if any(_is_active(f) for f in frames) else (1 if frames else 0)
        if rank > best_rank:
            best, best_rank = candidate, rank
            if rank == 2:
                break
    app_acc = best
    if app_acc is None or scope is Scope.APP:
        return app_acc
    # WINDOW scope: prefer the ACTIVE top-level frame, else the first child.
    frames = _frames(app_acc)
    for frame in frames:
        if _is_active(frame):
            return frame
    return frames[0] if frames else app_acc


def is_secure(acc) -> bool:
    """Whether ``acc`` is a password field (AT-SPI role name ``password text``,
    the role the engine maps to ``AXSecureTextField``)."""
    return _role_name(acc) == "password text"


def _focused_via_collection(root, Atspi, focused_state):
    """The FOCUSED descendant of ``root`` through org.a11y.atspi.Collection
    (one round-trip; served by the at-spi2-atk bridge for GTK3/Chromium/Firefox/
    Electron/LibreOffice). Returns (found, acc): found=False when the interface
    is absent or the call fails, so the caller falls back to the walk."""
    coll = _call_first(root, ("get_collection_iface", "get_collection"))
    if coll is None:
        return False, None
    try:
        states = Atspi.StateSet.new([focused_state])
        mt = Atspi.CollectionMatchType
        rule = Atspi.MatchRule.new(states, mt.ALL, {}, mt.NONE, [], mt.NONE, [], mt.NONE, False)
        hits = coll.get_matches(rule, Atspi.CollectionSortOrder.CANONICAL, 1, True)
    except Exception:  # noqa: BLE001 - GTK4/Qt expose no Collection; fall back to the walk
        return False, None
    return True, (hits[0] if hits else None)


def _focused_node(app: str, *, max_nodes: int = 400):
    """(focused accessible or None, truncated).

    truncated is True when the walk stopped before it could tell whether
    anything is focused. A Collection query that finds no focused node, a
    missing app, and a full walk that meets no focused node are
    ``(None, False)``.

    AT-SPI has no global "focused accessible" getter (focus arrives as
    events). The Collection interface answers in one round-trip where the
    toolkit exposes it; otherwise the active top-level frame is walked
    breadth-first, bounded by ``max_nodes``.
    """
    from a11y_computer_use.schema import Scope

    root = find_root(app, Scope.WINDOW)
    if root is None:
        return None, False
    Atspi = _atspi()
    st = getattr(Atspi, "StateType", None)
    focused_state = getattr(st, "FOCUSED", None)
    if focused_state is None:
        return None, False
    found, acc = _focused_via_collection(root, Atspi, focused_state)
    if found:
        return acc, False
    queue = [root]
    seen = 0
    truncated = False
    while queue and seen < max_nodes:
        acc = queue.pop(0)
        seen += 1
        sset = _call_first(acc, ("get_state_set",))
        if sset is not None and _safe(lambda state=sset: state.contains(focused_state), False):
            return acc, False
        n = int(_call_first(acc, ("get_child_count",), default=0) or 0)
        if n > _MAX_CHILDREN_FETCH:
            truncated = True
        for j in range(min(n, _MAX_CHILDREN_FETCH)):
            child = _call_first(acc, ("get_child_at_index",), j)
            if child is not None:
                queue.append(child)
    if queue or truncated:
        return None, True
    return None, False


def focused_secure(app: str, *, max_nodes: int = 400) -> bool | None:
    """Whether the keyboard-focused node of ``app``'s active window is a
    password field: True / False / None.

    True: the focused node is a password field. False: a focused non-secure
    node was found, or the whole frame was walked and nothing is focused, or the
    app is not on the bus (no signal, the same degradation as the macOS
    ``AXFocusedUIElement`` probe). None: the frame is larger than the walk bound
    (or a node had more than `_MAX_CHILDREN_FETCH` children) and no focused node
    was met, so focus is UNKNOWN; the caller must not type blind on None.

    This is the check `LinuxDriver.type_text` runs before the XTEST path,
    which types into whatever holds focus."""
    acc, truncated = _focused_node(app, max_nodes=max_nodes)
    if truncated:
        return None
    if acc is None:
        return False
    return is_secure(acc)


def focused_editable(app: str, *, max_nodes: int = 400):
    """The focused node when `type` can insert into it, else None.

    A password field is returned so the caller can refuse it before any
    write. None means there is no focused editable: nothing is focused, the
    focused node has no EditableText, or the walk could not find focus. The
    caller then uses keystrokes. The same node `insert_text` accepts, so a
    coordinate click and a ref click share that helper.
    """
    acc, truncated = _focused_node(app, max_nodes=max_nodes)
    if truncated or acc is None:
        return None
    if is_secure(acc):
        return acc
    if _editable_iface(acc) is None:
        return None
    return acc


def do_press(acc) -> bool:
    """Perform the first activating AT-SPI action on ``acc`` (True on success)."""
    action = _action_iface(acc)
    if action is None:
        return False
    n = _call_first(action, ("get_n_actions", "get_nActions"), default=0) or 0
    for i in range(int(n)):
        raw = (_call_first(action, ("get_action_name", "get_name"), i, default="") or "").lower()
        if raw in _PRESS_ACTION_NAMES or raw in _PICK_ACTION_NAMES:
            if _call_first(action, ("do_action", "doAction"), i, default=False):
                return True
    return False


def grab_focus(acc) -> bool:
    """Give ``acc`` keyboard focus via AT-SPI (True on success) — no cursor move."""
    comp = _component(acc)
    if comp is None:
        return False
    return bool(_call_first(comp, ("grab_focus", "grabFocus"), default=False))


def _editable_iface(acc):
    return _call_first(acc, ("get_editable_text_iface", "get_editable_text"))


def _state_has(acc, name: str) -> bool:
    """Whether the state set contains ``name``.

    A fake state set matches the name string. The GI binding matches
    ``Atspi.StateType``. Missing gi, or a node with no state set, is false.
    """
    sset = _call_first(acc, ("get_state_set",))
    if sset is None:
        return False
    names = getattr(sset, "names", None)
    if isinstance(names, (set, frozenset, list, tuple)):
        folded = {str(item) for item in names}
        return name in folded or name.lower() in {item.lower() for item in folded}
    if bool(_safe(lambda: sset.contains(name), False)):
        return True
    try:
        Atspi = _atspi()
    except Exception:
        return False
    member = getattr(getattr(Atspi, "StateType", None), name, None)
    if member is None:
        return False
    return bool(_safe(lambda: sset.contains(member), False))


def _insert_length(method, text: str) -> int:
    """The ``length`` argument EditableText.insert_text actually wants.

    libatspi and ``gi.repository.Atspi`` take a UTF-8 byte count. Passing the
    character count keeps only that many bytes, so ``Привет`` becomes ``При``
    and a cut code point inserts nothing. A Python double that slices
    characters has no ``gi.`` module and gets ``len(text)`` unless it sets
    ``_length_unit`` to ``bytes`` or ``chars``.
    """
    unit = getattr(method, "_length_unit", None)
    if unit == "bytes":
        return len(text.encode("utf-8"))
    if unit == "chars":
        return len(text)
    func = getattr(method, "__func__", method)
    module = str(getattr(method, "__module__", None) or getattr(func, "__module__", "") or "")
    if module.startswith("gi."):
        return len(text.encode("utf-8"))
    return len(text)


def _selection_bounds(selection) -> tuple[int, int] | None:
    """(start, end) character offsets from an AT-SPI selection, or None."""
    if selection is None:
        return None
    start = getattr(selection, "start_offset", None)
    end = getattr(selection, "end_offset", None)
    if isinstance(start, int) and not isinstance(start, bool) and isinstance(end, int) and not isinstance(end, bool):
        return int(start), int(end)
    if isinstance(selection, tuple):
        nums = [item for item in selection if isinstance(item, int) and not isinstance(item, bool)]
        if len(nums) >= 2:
            return nums[-2], nums[-1]
    return None


def _caret_and_selection(acc, nchars: int) -> tuple[int, int, int]:
    """(caret, selection start, selection end). An empty selection has start == end.

    Offsets are characters, which is what AT-SPI uses. A missing caret is the
    end of the field.
    """
    Atspi = _atspi()
    caret = _safe(lambda: Atspi.Text.get_caret_offset(acc))
    if not isinstance(caret, int) or isinstance(caret, bool) or caret < 0 or caret > nchars:
        caret = nchars
    start, end = caret, caret
    count = _safe(lambda: Atspi.Text.get_n_selections(acc))
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        bounds = _selection_bounds(_safe(lambda: Atspi.Text.get_selection(acc, 0)))
        if bounds is not None:
            start, end = bounds
            start = max(0, min(int(start), nchars))
            end = max(0, min(int(end), nchars))
            if end < start:
                start, end = end, start
    return int(caret), start, end


def _text_mismatch(reason: str, message: str, **detail):
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    payload = {"platform": "linux", "reason": reason}
    payload.update(detail)
    return ComputerUseError(ErrorCode.UNSUPPORTED, message, detail=payload)


def _excerpt(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) <= 80:
        return value
    return value[:80] + "…"


def insert_text(acc, text: str) -> int | None:
    """Insert ``text`` at the caret via AT-SPI EditableText.

    A selection is deleted first and the text replaces it. ``None`` means the
    element has no EditableText, so the caller may use keystrokes. The return
    value is the number of characters the read-back shows were inserted. A
    field that does not contain that text raises `ErrorCode.UNSUPPORTED`
    instead of reporting success. A CRLF is one newline.

    The insert length is the UTF-8 byte count on the GI/C binding and the
    character count on a binding that slices characters.
    """
    eti = _editable_iface(acc)
    if eti is None:
        return None
    typed = text.replace("\r\n", "\n")
    current = _full_text(acc)
    if current is None:
        raise _text_mismatch(
            "text_unreadable",
            "the field text could not be read, so type was not reported as success",
        )
    caret, start, end = _caret_and_selection(acc, len(current))
    if end > start:
        _call_first(eti, ("delete_text",), start, end, default=False)
        cleared = _full_text(acc)
        wanted = current[:start] + current[end:]
        if cleared != wanted:
            raise _text_mismatch(
                "selection_not_replaced",
                "the selection was not removed, so the text was not inserted",
                expected=_excerpt(wanted),
                actual=_excerpt(cleared),
            )
        offset = start
        expected = wanted[:start] + typed + wanted[start:]
    else:
        offset = caret
        expected = current[:caret] + typed + current[caret:]
    insert = None
    for name in ("insert_text", "insertText"):
        insert = getattr(eti, name, None)
        if insert is not None:
            break
    length = _insert_length(insert, typed) if insert is not None else len(typed)
    wrote = bool(_call_first(eti, ("insert_text", "insertText"), int(offset), typed, int(length), default=False))
    if _confirm_text(acc, expected):
        return len(typed)
    actual = _full_text(acc)
    if actual == current and not wrote:
        return None
    raise _text_mismatch(
        "text_mismatch",
        "the field text after type does not match what was inserted",
        expected=_excerpt(expected),
        actual=_excerpt(actual),
        inserted_chars=len(typed),
    )


# After a clear or a write, a web field's text can show up on a later read.
# The first read is immediate; these extra reads cover that gap. A value that
# never equals the request is still a failure.
_TEXT_CONFIRM_POLLS = 4
_TEXT_CONFIRM_PAUSE_S = 0.02


def _full_text(acc) -> str | None:
    """The text a snapshot shows: ``Text.get_text(acc, 0, -1)``.

    ``None`` means that read failed. ``""`` is an empty field when the toolkit
    returns one. A bounded ``get_text(0, character_count)`` is not used. On a
    Chromium web field that count can be the length of the string just passed
    to ``set_text_contents``, and the bounded read echoes that string while
    ``get_text(0, -1)`` still has the previous contents. The client text cache
    is dropped first so the read is not the pre-write value.
    """
    _call_first(acc, ("clear_cache", "clearCache"))
    got = _safe(lambda: _atspi().Text.get_text(acc, 0, -1))
    if got is None:
        return None
    return str(got)


def _x11_keys_available() -> bool:
    """True when XTEST can reach the session. Matches ``linux._on_wayland``."""
    return not (os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"))


def _select_range(acc, end: int) -> None:
    """Select ``[0, end)`` on the Text interface. A missing method is ignored."""
    if end <= 0:
        return
    Atspi = _atspi()
    if not _safe(lambda: Atspi.Text.set_selection(acc, 0, 0, end)):
        _safe(lambda: Atspi.Text.add_selection(acc, 0, end))


def _text_is_gone(acc) -> bool:
    """True when the snapshot read is empty or could not be read.

    Chromium returns NULL from ``get_text(0, -1)`` on an empty field, because
    the start offset is past the end. An unreadable field is treated as clear
    here so the replacement can be written; ``set_text`` still returns True
    only when a later snapshot read equals the new string.
    """
    return not _full_text(acc)


def _wait_until_gone(acc) -> bool:
    for attempt in range(_TEXT_CONFIRM_POLLS):
        if _text_is_gone(acc):
            return True
        if attempt + 1 < _TEXT_CONFIRM_POLLS:
            time.sleep(_TEXT_CONFIRM_PAUSE_S)
    return False


def _x11_select_all_and_delete(acc) -> None:
    """Focus the field and replace its selection with nothing. X11 only."""
    grab_focus(acc)
    from a11y_computer_use.drivers import _linux_input

    _linux_input.press_chord("ctrl+a")
    _linux_input.press_chord("backspace")


def _type_string(text: str) -> None:
    from a11y_computer_use.drivers import _linux_input

    _linux_input.type_string(text)


def _replace_with_keys(acc, text: str) -> bool:
    """Clear the field and type ``text``. Used when EditableText is missing.

    Chromium's ATK objects do not implement AtkEditableText, so
    ``get_editable_text`` is absent and the runtime would otherwise type the
    new string onto the old one and still report success. Returns True only
    when the snapshot read equals ``text``. An unreadable field is not typed
    into. Native Wayland has no XTEST, so this returns False there.
    """
    current = _full_text(acc)
    if current == text:
        return True
    if current is None or not _x11_keys_available():
        return False
    if current:
        _x11_select_all_and_delete(acc)
        if not _wait_until_gone(acc):
            return False
    else:
        grab_focus(acc)
    _type_string(text)
    return _confirm_text(acc, text)


def _clear_text(acc, eti, current: str) -> bool:
    """Remove ``current`` so the next write substitutes.

    The deleted range is ``len(current)`` from the snapshot read, not
    ``character_count``. ``delete_text`` is not trusted on its return value:
    the AT-SPI editable-text adaptor reports true after the call whether or
    not the text changed. When the snapshot read still has text, and this is
    an X11 session, the field is focused and ctrl+a, BackSpace is sent.
    """
    if current == "":
        return True
    _select_range(acc, len(current))
    _call_first(eti, ("delete_text",), 0, len(current), default=False)
    if _wait_until_gone(acc):
        return True
    if not _x11_keys_available():
        return False
    _x11_select_all_and_delete(acc)
    return _wait_until_gone(acc)


def _confirm_text(acc, text: str) -> bool:
    for attempt in range(_TEXT_CONFIRM_POLLS):
        if _full_text(acc) == text:
            return True
        if attempt + 1 < _TEXT_CONFIRM_POLLS:
            time.sleep(_TEXT_CONFIRM_PAUSE_S)
    return False


def set_text(acc, text: str) -> bool:
    """Replace the element's whole text via AT-SPI EditableText.

    GTK's ``set_text_contents`` replaces, and the snapshot read then equals
    ``text``, so nothing is deleted. Chromium's web fields implement that
    call as an insert and the editable-text adaptor still returns true. The
    success check is ``Text.get_text(0, -1)``, the same read a snapshot uses,
    not ``get_text(0, character_count)``. When they disagree, the snapshot
    text is selected and deleted; if it is still there, X11 sends ctrl+a and
    BackSpace. The new string is written only after that, and this returns
    True only when the snapshot read equals ``text``. A toolkit whose text
    cannot be read at all is trusted when ``set_text_contents`` returned
    true, so a replace is not refused just because ``Text.get_text`` failed.
    A field with no EditableText is cleared and typed on X11; that also
    returns True only when the snapshot read equals ``text``.
    """
    eti = _editable_iface(acc)
    if eti is None:
        return _replace_with_keys(acc, text)
    wrote = bool(_call_first(eti, ("set_text_contents",), text, default=False))
    current = _full_text(acc)
    if current == text:
        return True
    if current is None:
        return wrote
    if not _clear_text(acc, eti, current):
        return False
    if not _call_first(eti, ("set_text_contents",), text, default=False):
        insert = None
        for name in ("insert_text", "insertText"):
            insert = getattr(eti, name, None)
            if insert is not None:
                break
        length = _insert_length(insert, text) if insert is not None else len(text)
        if not _call_first(eti, ("insert_text", "insertText"), 0, text, length, default=False):
            if _text_is_gone(acc) and _x11_keys_available():
                _type_string(text)
            else:
                return False
    if _confirm_text(acc, text):
        return True
    # The AT-SPI write did not stick. Typing is only safe once the snapshot
    # read says the field is empty; otherwise it would append.
    if _text_is_gone(acc) and _x11_keys_available():
        _type_string(text)
        return _confirm_text(acc, text)
    return False


def scroll_to(acc) -> bool:
    """Reveal ``acc`` via AT-SPI (`Component.scroll_to ANYWHERE`) — no cursor move."""
    Atspi = _atspi()
    comp = _component(acc)
    if comp is None:
        return False
    stype = getattr(getattr(Atspi, "ScrollType", None), "ANYWHERE", 0)
    return bool(_call_first(comp, ("scroll_to",), stype, default=False))


def scroll_to_edge(acc, edge: str) -> bool:
    """Reveal ``acc`` on one AT-SPI edge. No wheel and no pointer move.

    ``edge`` is an ``Atspi.ScrollType`` member name. ``TOP_EDGE`` puts the
    row at the top of its scroller. False when the binding has no such
    member, the accessible has no component, or ``scroll_to`` is missing.
    ``scroll_to`` still uses ``ANYWHERE`` and is a different call.
    """
    Atspi = _atspi()
    comp = _component(acc)
    if comp is None:
        return False
    scroll_type = getattr(getattr(Atspi, "ScrollType", None), edge, None)
    if scroll_type is None:
        return False
    return bool(_call_first(comp, ("scroll_to",), scroll_type, default=False))


# Pixel scroll walks a few ancestors and their immediate children looking for
# scroll bars. A text buffer can report thousands of children; cap the walk so
# one pixel scroll cannot turn into a full-tree D-Bus crawl.
_MAX_SCROLL_ANCESTORS = 16
_MAX_SCROLL_NODES = 48
_SCROLL_BAR_ROLE = "scroll bar"


def _child_count(acc) -> int:
    try:
        return int(_call_first(acc, ("get_child_count", "get_childCount"), default=0) or 0)
    except (TypeError, ValueError):
        return 0


def _child_at(acc, index: int):
    return _call_first(acc, ("get_child_at_index", "getChildAtIndex"), index)


def _parent_of(acc):
    return _call_first(acc, ("get_parent", "getParent"))


def _is_scroll_bar(acc) -> bool:
    return _role_name(acc) == _SCROLL_BAR_ROLE


def _node_name(acc) -> str:
    return (_call_first(acc, ("get_name", "getName"), default="") or "").strip()


def _is_list_marker(name: str) -> bool:
    """True when ``name`` is a list bullet, not the row's text.

    Chromium's default ``<ul>`` exposes the marker as a static named "•"
    and the row text as its sibling. A one-character name that is not a
    letter or digit is that marker. ``ITEM-001`` is a row.
    """
    text = name.strip()
    if text in _LIST_MARKERS:
        return True
    return len(text) == 1 and not text.isalnum()


# Chromium's first GetAccessibleAtPoint answer is an approximate hit test of
# the accessibility bounds, which stay stale across a wheel. That call starts
# a layout hit test in the renderer. A later call at the same point returns
# the layout row once it is cached. These waits cover that gap. A row that
# never changes is the answer, not a timeout success.
_HIT_TRIES = 3
_HIT_PAUSE_S = 0.05
# The 0.4.12 retest still showed the pre-wheel head 1.5s after a 3-line scroll,
# and a different wrong head about 2.7s later. Polling this long is what lets
# the snapshot head leave the stale row. A head that never changes is not success.
_HEAD_POLLS = 8
_HEAD_PAUSE_S = 0.4
# Pixels inside the list. A still grab is 0. The 0.4.12 retest measured 6.677
# where the list moved and 0 where it did not. The 0.4.13 retest's own
# mean_abs was 0.0 on a scroll whose list later measured 6.13 and 5.90.
# This threshold still sits between a still grab and those moves. A single
# difference above it is not a successful scroll.
_PAGE_MOVE_MEAN = 1.0
# Uniform rows move the list and change few pixels. The 0.4.25 retest of a
# fixed-height list reported page_unchanged at mean_abs about 0.5 to 0.7
# while the painted head had moved, then sent a wheel or a track click as
# well. A grab above this floor, with a new on-screen head, is that move.
# A grab at or under it is still when the head did not move by about the
# requested lines. The 0.4.28 retest moved five rows and stayed under this
# floor, then clicked the track: five rows plus one page. A far hit-test
# name on a still grab is not that step.
_UNIFORM_ROW_MEAN = 0.4
# The grab taken in the same turn as the wheel can still be the pre-paint
# frame. 0.4.13 compared that one pair and returned page_unchanged, so the
# snapshot never advanced. Resample until the pixels change. A box that
# stays at or under the threshold for the whole window is unchanged.
_PAINT_POLLS = 12
_PAINT_PAUSE_S = 0.15
# The top of the list box can be a clipped row. On the 0.4.12 retest the
# snapshot started at ITEM-009 while the screen head was ITEM-010, and
# scroll_to_find showed a clipped row above the visible run. Hit-test
# samples start below this sliver so that edge is not the head. It is not
# an OCR read. A row whose own top is the list's top is fully on screen:
# 0.4.17 and 0.4.18 required that top to clear these 8px, and the snapshot
# then started at the next row.
_CLIP_SLIVER_PX = 8
_LIST_ROLES = frozenset({"list", "list box", "table", "tree", "tree table"})
_MAX_LIST_CONTAINERS = 4
# Hit-test points in ``_probe_visible_rows`` when the child walk has not
# already named the rows. Not a cap on the rows snapshot and find list.
# 0.4.22 through 0.4.26 also stopped the on-screen walk at this count, so a
# 520px list of 18px rows (about 29 painted) published ITEM-001 through
# ITEM-016 and left the rest out with no elision marker.
_MAX_ROW_SAMPLES = 16
# One requested line is one content row. The 0.4.26 retest stepped dy=5 by
# 15 rows on a fixed-height list and by 14 on overflow-y:auto, which is
# five times the old three-rows-per-line pace. The step still stays inside
# the rows already on screen, so the next snapshot overlaps this one.
_ROWS_PER_LINE = 1
# Chromium draws a list marker as its own static ("•") beside the row text.
# The list item's name is empty. Treating the marker as the head leaves the
# head unchanged after a real move, so the line step is rejected and a wheel
# follows. A marker is not a row.
_LIST_MARKERS = frozenset({
    "•", "●", "◦", "▪", "▫", "▸", "‣", "·", "○", "■", "□", "∙", "◉",
})
_ROW_SEQUENCE_CAP = 240
# Nodes examined while looking for the on-screen head. Rows parked above
# the viewport do not count as a reason to stop: the 0.4.16 cap of 40
# could end before the first visible row, and a wrapper whose top is above
# the line was not opened even when it held that row.
_VISIBLE_WALK_CAP = _MAX_CHILDREN_FETCH
_VISIBLE_DESCEND = 4
_SKIP_ROW_ROLES = frozenset({
    "scroll bar", "separator", "menu bar", "tool bar", "status bar",
})
# Rows last confirmed for a list box. A later hit test is not installed over
# these unless a wheel's pixels moved and the head below the sliver changed,
# or the confirmed head's own top has moved above that line.
_SHOWN: dict[tuple, list] = {}
# A one-pixel change in the reported list origin still names the same box.
# A content-height list that scrolls moves its origin by thousands of pixels
# (y=120 to about y=-4847). Those two boxes overlap, and the overlap must
# not resurrect the rows saved at the old origin.
_BOX_JITTER_PX = 4


def _chromium_app(acc) -> bool:
    """True when ``acc`` belongs to Chromium. GTK and other toolkits are not."""
    app = _call_first(acc, ("get_application", "getApplication")) or acc
    toolkit = (_call_first(app, ("get_toolkit_name", "getToolkitName"), default="") or "").lower()
    if "chrom" in toolkit:
        return True
    name = (_call_first(app, ("get_name", "getName"), default="") or "").lower()
    return "chrom" in name


def _list_containers(root, limit: int = _MAX_LIST_CONTAINERS) -> list:
    """List, table, and tree nodes under ``root``, nearest first, bounded."""
    found: list = []
    queue = [root]
    seen: set[int] = set()
    while queue and len(found) < limit and len(seen) < 80:
        node = queue.pop(0)
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        if _role_name(node) in _LIST_ROLES and _extents(node)[0] is not None:
            found.append(node)
        count = min(_child_count(node), 30)
        for index in range(count):
            queue.append(_child_at(node, index))
    return found


def _point_in_extents(acc, x: int, y: int) -> bool | None:
    """Whether ``acc``'s own screen box covers ``(x, y)``.

    None when the accessible has no box. False when it has a box and the
    point is outside it. Covering the point is not enough: the 0.4.15 retest
    still published ITEM-001 at y=-2, and a row that tall still covers a
    sample inside the list. ``_top_above_line`` is what drops that row.
    """
    pos, size = _extents(acc)
    if pos is None or size is None:
        return None
    left, top = pos
    width, height = size
    if width <= 0 or height <= 0:
        return False
    return (left - 1) <= x <= (left + width + 1) and (top - 1) <= y <= (top + height + 1)


def _clip_sliver(height: float) -> float:
    """Pixels of clipped row at the top of a list that are not a head.

    8 when the list is tall enough to have an edge distinct from its rows.
    1 when the box is only a few pixels tall.
    """
    if height > _CLIP_SLIVER_PX * 2:
        return float(_CLIP_SLIVER_PX)
    return 1.0


# Chromium's page, as opposed to the browser chrome around it. The 0.4.21
# retest's document group was 1271 by 709 on an 800px screen, so about three
# rows sit between the screen top and the page. Those rows are not painted.
_PAGE_ANCESTOR_ROLES = frozenset({"document web", "document frame", "html container"})


def _page_edges(container) -> tuple[float, float, float, float] | None:
    """Screen box of the document that contains ``container``, if it has one.

    None when no document ancestor has a box. The nearest document wins.
    """
    node = _parent_of(container)
    seen: set[int] = set()
    for _ in range(8):
        if node is None or id(node) in seen:
            return None
        seen.add(id(node))
        if _role_name(node) in _PAGE_ANCESTOR_ROLES:
            pos, size = _extents(node)
            if pos is None or size is None or size[1] < 4 or size[0] < 1:
                return None
            left = float(pos[0])
            top = float(pos[1])
            return left, top, left + float(size[0]), top + float(size[1])
        node = _parent_of(node)
    return None


def _intersect_edges(
    box: tuple[float, float, float, float],
    other: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    """The overlap of two ``(left, top, right, bottom)`` boxes, or None."""
    left = max(box[0], other[0])
    top = max(box[1], other[1])
    right = min(box[2], other[2])
    bottom = min(box[3], other[3])
    if right - left < 1 or bottom - top < 4:
        return None
    return left, top, right, bottom


# Ancestors that are not an overflow wrapper around a list. The document
# clip is separate: treating it as the wrapper would step a body-scroll
# page with the overflow bar. The frame is the window.
_SCROLLPORT_STOP_ROLES = _PAGE_ANCESTOR_ROLES | frozenset({
    "frame", "window", "application", "dialog", "alert",
})


def _overflow_ancestor_edges(container) -> tuple[float, float, float, float] | None:
    """Screen box of the overflow wrapper around ``container``, or None.

    A plain list inside ``overflow:auto`` does not scroll. The wrapper is
    shorter than the list, and the list extends outside it. Rows in that
    band are hidden by the wrapper. The nearest such ancestor wins. The
    document and the frame are not it. None when the list is its own
    scrollport, which is the fixed-height overflow list.
    """
    pos, size = _extents(container)
    if pos is None or size is None:
        return None
    list_left = float(pos[0])
    list_top = float(pos[1])
    list_width = float(size[0])
    list_height = float(size[1])
    list_right = list_left + list_width
    list_bottom = list_top + list_height
    node = _parent_of(container)
    seen: set[int] = set()
    for _ in range(8):
        if node is None or id(node) in seen:
            return None
        seen.add(id(node))
        if _role_name(node) in _SCROLLPORT_STOP_ROLES:
            return None
        ppos, psize = _extents(node)
        node = _parent_of(node)
        if ppos is None or psize is None:
            continue
        width = float(psize[0])
        height = float(psize[1])
        if width < 4 or height < 4 or height >= list_height - 8:
            continue
        left = float(ppos[0])
        top = float(ppos[1])
        right = left + width
        bottom = top + height
        if list_top >= top - 1 and list_bottom <= bottom + 1:
            continue
        overlap = min(right, list_right) - max(left, list_left)
        if overlap < min(width, list_width) * 0.5:
            continue
        return left, top, right, bottom
    return None


def overflow_ancestor_box(container) -> tuple[int, int, int, int] | None:
    """``(x, y, width, height)`` of the overflow wrapper, or None.

    See ``_overflow_ancestor_edges``. The caller photographs this box and
    steps the list it scrolls. A body-scroll document is not this box.
    """
    edges = _overflow_ancestor_edges(container)
    if edges is None:
        return None
    left, top, right, bottom = edges
    if right - left < 1 or bottom - top < 4:
        return None
    return int(left), int(top), int(right - left), int(bottom - top)


def list_with_overflow_ancestor(root):
    """A Chromium list under ``root`` that an ancestor wrapper scrolls.

    None when no list has that wrapper, so a body-scroll document keeps
    the wheel. The first list in the bounded walk wins. A fixed-height
    overflow list whose own box is the scrollport is not returned: its
    parent is the document, and that is not this wrapper.
    """
    if root is None or not _chromium_app(root):
        return None
    for container in _list_containers(root):
        if _overflow_ancestor_edges(container) is not None:
            return container
    return None


def _viewport_edges(container) -> tuple[float, float, float, float] | None:
    """Edges of the painted page inside a list: left, top, right, bottom.

    The head line is this top. An overflow list that already sits on the
    page keeps its own top, so a row flush with that top stays the head and
    a row above the list stays out. A list inside a shorter ancestor is
    clipped to that wrapper first, so a row the wrapper hides is not the
    head. A content-height list is clipped to the document, then to the
    screen. The document starts below the screen top (the browser chrome).
    Rows in that band are on the screen and are not painted; they are not
    the head. A list with no document ancestor is clipped to the screen only.
    """
    pos, size = _extents(container)
    if pos is None or size is None or size[1] < 4 or size[0] < 1:
        return None
    left = float(pos[0])
    top = float(pos[1])
    right = left + float(size[0])
    bottom = top + float(size[1])
    ancestor = _overflow_ancestor_edges(container)
    if ancestor is not None:
        clipped = _intersect_edges((left, top, right, bottom), ancestor)
        if clipped is not None:
            left, top, right, bottom = clipped
    page = _page_edges(container)
    if page is not None:
        clipped = _intersect_edges((left, top, right, bottom), page)
        if clipped is not None:
            left, top, right, bottom = clipped
    sw, sh = _screen_size()
    if sw <= 0 or sh <= 0:
        return left, top, right, bottom
    screen = _intersect_edges((left, top, right, bottom), (0.0, 0.0, float(sw), float(sh)))
    if screen is None:
        return left, top, right, bottom
    return screen


def _head_line(container) -> float | None:
    """Y of the on-screen top of the list.

    A row whose own top is above this edge is outside the viewport. When the
    list's own top is on the screen, that top is the edge: the 8px sliver is
    not added. 0.4.17 and 0.4.18 added it, and a fully visible row flush with
    the list then failed the check, so the snapshot started at the next row.
    When the list's own top is above the page, the edge is the document top,
    not the screen top. Rows between those two lines are browser chrome.
    """
    edges = _viewport_edges(container)
    if edges is None:
        return None
    return edges[1]


def list_wheel_point(container) -> tuple[int, int] | None:
    """Where a line-scroll wheel lands on a Chromium list, when a wheel is sent.

    The center of the first painted row, clamped into the painted page.
    None when ``container`` is not a Chromium list, so a document group keeps
    its own wheel point. No hit test: the row walk is enough, and a list
    with no named rows is wheeled just below its clipped top edge. The
    0.4.22 overflow list was wheeled here and did not move; a viewport list
    uses ``scroll_viewport_by_lines`` before this point.
    """
    if _role_name(container) not in _LIST_ROLES or not _chromium_app(container):
        return None
    edges = _viewport_edges(container)
    if edges is None:
        return None
    left, top, right, bottom = edges
    rows = _in_view_named_rows(container)
    if rows:
        _acc, (x, y), (width, height) = rows[0]
        cx = int(x + width / 2)
        cy = int(y + height / 2)
    else:
        cx = int((left + right) / 2)
        cy = int(top + _clip_sliver(bottom - top) + 1)
    cx = min(max(cx, int(left)), max(int(left), int(right) - 1))
    cy = min(max(cy, int(top)), max(int(top), int(bottom) - 1))
    return cx, cy


def _top_above_line(acc, line: float) -> bool:
    """True when ``acc``'s own top is above ``line``.

    False when the accessible has no box. Callers pass the list's top edge.
    A row at y=-2 is above a list at y=100, including when its height still
    covers a sample inside the list. The 0.4.15 check kept that row because
    the box covered the point. A row whose top is the list's top is not
    above the line.
    """
    pos, size = _extents(acc)
    if pos is None or size is None:
        return False
    return float(pos[1]) < line - 1


def _on_screen_row(top: float, bottom: float, box_y: float, box_b: float, sliver: float) -> bool:
    """True when a row is on screen inside the list, not the clipped edge.

    The row's own top is on or below the list's top, within 1px, and its
    bottom is below the top sliver. A fully visible row that starts on the
    list's top is included. A box that only fills that sliver is the clipped
    edge. A row whose top is above the list is the parked row (y=-2 on the
    0.4.16 retest) and is not included.
    """
    if top < box_y - 1 or top > box_b:
        return False
    return min(bottom, box_b) > box_y + sliver


def _vertical_span(acc) -> tuple[float, float] | None:
    """``(top, bottom)`` of ``acc``, or None when it has no positive box."""
    pos, size = _extents(acc)
    if pos is None or size is None:
        return None
    width = float(size[0])
    height = float(size[1])
    if width <= 0 or height <= 0:
        return None
    top = float(pos[1])
    return top, top + height


def _child_vertical_span(node, index: int) -> tuple[float, float] | None:
    """Span of child ``index``, skipping scroll bars and separators.

    None when that child is missing, has no box, or is not a row.
    """
    child = _child_at(node, index)
    if child is None or _role_name(child) in _SKIP_ROW_ROLES:
        return None
    return _vertical_span(child)


def _first_index_reaching(node, line: float, count: int) -> int:
    """First child index that is not entirely above ``line``.

    0 when ``count`` fits in ``_VISIBLE_WALK_CAP``, when the ends have no
    box or are not top-to-bottom, or when child 0 already reaches the
    line. A short list keeps the walk that starts at child 0. On the
    0.4.31 live list a 520px scroller held 2000 ``list item`` children.
    With ITEM-0201 at the top of the box the walk spent the cap on the
    rows above the viewport, so the snapshot stopped at ITEM-0224. With
    ITEM-0226 at the top it stopped at ITEM-0237. Past child 250 it
    listed nothing. The cap still bounds how many children are read
    after this index. A run that is not top-to-bottom is not skipped.
    """
    if count <= 0 or count <= _VISIBLE_WALK_CAP:
        return 0
    first = _child_vertical_span(node, 0)
    last_index = count - 1
    last = _child_vertical_span(node, last_index)
    if last is None:
        for index in range(count - 2, max(-1, count - 6), -1):
            last = _child_vertical_span(node, index)
            if last is not None:
                last_index = index
                break
    if first is None or last is None or last[0] < first[0] - 1:
        return 0
    # ``bottom < line - 1`` is the walk's "entirely above" test.
    if first[1] >= line - 1:
        return 0
    lo = 0
    hi = last_index
    while lo < hi:
        mid = (lo + hi) // 2
        span = _child_vertical_span(node, mid)
        if span is None or span[0] < first[0] - 1:
            return 0
        if span[1] < line - 1:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _child_scan_range(node, line: float, limit: int) -> range:
    """Child indexes to read, at most ``limit``, from the first that reaches ``line``.

    ``limit`` is the remaining node budget. A short child list starts at
    0, which is the previous walk.
    """
    count = _child_count(node)
    if count <= 0 or limit <= 0:
        return range(0)
    start = _first_index_reaching(node, line, count)
    return range(start, min(count, start + limit))


def _content_ends_span(container) -> float | None:
    """Distance from the first child's top to the last row's top.

    None when an end has no box or the run is not top-to-bottom. A scroll
    bar at the end is not the last row. The on-screen window is not this
    distance: a fractional bar scaled by that window steps past the rows
    in between.
    """
    count = _child_count(container)
    if count < 2:
        return None
    first = _child_vertical_span(container, 0)
    last = None
    for index in range(count - 1, max(-1, count - 6), -1):
        last = _child_vertical_span(container, index)
        if last is not None:
            break
    if first is None or last is None or last[0] < first[0] - 1:
        return None
    return last[0] - first[0]


def _settle_hit(comp, x: int, y: int, coord):
    """The accessible at ``(x, y)`` that covers that point.

    Chromium's first answer can be the pre-scroll row. A row that does not
    cover ``(x, y)`` is ignored. A row that covers the point can still start
    above the list; the probe drops that one. Two covering answers with the
    same name are the result. Python identity is not the check: a new
    wrapper for the same row is still that row.
    """
    previous_name = None
    for attempt in range(_HIT_TRIES):
        hit = _call_first(
            comp, ("get_accessible_at_point", "getAccessibleAtPoint"), x, y, coord
        )
        if hit is not None and _point_in_extents(hit, x, y) is False:
            hit = None
        if hit is not None:
            name = _node_name(hit)
            if previous_name is not None and name and name == previous_name:
                return hit
            previous_name = name
        if attempt + 1 < _HIT_TRIES:
            time.sleep(_HIT_PAUSE_S)
    return None


def _in_view_named_rows(container) -> list[tuple]:
    """Named rows under ``container`` that are on screen in the list.

    Each entry is ``(accessible, (x, y), (width, height))``. Every on-screen
    row is included. ``_MAX_ROW_SAMPLES`` is not applied here: it only
    limits hit-test points, and stopping this walk at 16 left painted rows
    out of the snapshot. The walk reads ``container`` itself. A row entirely
    above the list is skipped and the scan continues, including past the
    first forty such rows. A wrapper whose top is above the list is opened
    when its box still covers the list, which is where the on-screen rows
    sit. A node tall enough to be that wrapper is not itself a row. A row
    is on screen when its own top is on or below the list's top and it
    extends below the clipped edge. The rectangle is the node's own box
    clipped to the on-screen part of the list, so the snapshot y is not the
    pre-scroll y. A row at the content origin of a list whose top is above
    the screen is not on screen. Nodes examined are still bounded by
    ``_VISIBLE_WALK_CAP``, counted from the first child whose box reaches
    the viewport. A list longer than that cap is not read from child 0:
    the rows above the viewport would use up the cap, and the on-screen
    rows past it would be missing. A run that is not top-to-bottom is
    still read from child 0.
    """
    if _role_name(container) not in _LIST_ROLES or not _chromium_app(container):
        return []
    pos, size = _extents(container)
    edges = _viewport_edges(container)
    if pos is None or size is None or edges is None:
        return []
    box_x, box_y, box_r, box_b = edges
    full_h = float(size[1])
    sliver = _clip_sliver(box_b - box_y)
    found: list[tuple] = []
    seen = 0

    def walk(node, depth: int) -> None:
        nonlocal seen
        if node is None or node is container or seen >= _VISIBLE_WALK_CAP:
            return
        seen += 1
        npos, nsize = _extents(node)
        if npos is None or nsize is None:
            if depth > 0:
                for index in _child_scan_range(node, box_y, _VISIBLE_WALK_CAP - seen):
                    if seen >= _VISIBLE_WALK_CAP:
                        break
                    walk(_child_at(node, index), depth - 1)
            return
        top = float(npos[1])
        height = float(nsize[1])
        left = float(npos[0])
        width = float(nsize[0])
        if width <= 0 or height <= 0:
            return
        bottom = top + height
        if bottom < box_y - 1 or top > box_b or left + width < box_x or left > box_r:
            return
        role = _role_name(node)
        spans = height > full_h * 0.9
        in_view = _on_screen_row(top, bottom, box_y, box_b, sliver)
        name = _node_name(node)
        if name and _is_list_marker(name):
            if depth > 0 and top < box_b:
                for index in _child_scan_range(node, box_y, _VISIBLE_WALK_CAP - seen):
                    if seen >= _VISIBLE_WALK_CAP:
                        break
                    walk(_child_at(node, index), depth - 1)
            return
        if (
            in_view
            and name
            and not spans
            and role not in _SKIP_ROW_ROLES
            and role not in _LIST_ROLES
        ):
            row_top = max(top, box_y)
            row_left = max(left, box_x)
            row_height = max(1.0, min(height, box_b - row_top))
            row_width = max(1.0, min(width, box_r - row_left))
            found.append((node, (row_left, row_top), (row_width, row_height)))
            return
        if depth > 0 and top < box_b:
            for index in _child_scan_range(node, box_y, _VISIBLE_WALK_CAP - seen):
                if seen >= _VISIBLE_WALK_CAP:
                    break
                walk(_child_at(node, index), depth - 1)

    for index in _child_scan_range(container, box_y, _VISIBLE_WALK_CAP):
        child = _child_at(container, index)
        if child is None:
            continue
        cpos, csize = _extents(child)
        if (
            found
            and cpos is not None
            and csize is not None
            and float(cpos[1]) > box_b
        ):
            break
        walk(child, _VISIBLE_DESCEND)
    found.sort(key=lambda item: (item[1][1], item[1][0]))
    return found


def _row_top(acc) -> float:
    """Screen y of ``acc``, or 0 when it has no box."""
    pos, _size = _extents(acc)
    if pos is None:
        return 0.0
    return float(pos[1])


def _named_rows_in_order(container) -> list:
    """Named rows under ``container``, including those below the viewport.

    Each entry is the accessible, ordered by screen y. A recorded row is
    not opened. A scroll bar is skipped. A list marker is not a row; the
    walk continues into it for the row text. A node taller than the list is
    a wrapper and is opened. Each direct child is visited on its own, so a
    marker plus a label on an early row does not stop the walk before
    ITEM-100. This does not change which rows the snapshot lists; it only
    chooses a row for ``scroll_viewport_by_lines`` to reveal.
    A list longer than ``_ROW_SEQUENCE_CAP`` is not read from child 0.
    The sequence starts a few rows above the viewport, so a line step
    can still see the previous head, and it still stops at that cap.
    """
    _pos, size = _extents(container)
    if size is None:
        return []
    full_h = float(size[1])
    found: list = []

    def descend(node, depth: int, budget: list[int]) -> None:
        if depth <= 0:
            return
        count = _child_count(node)
        # A wrapper that holds the whole list is one child of the list.
        # Sharing this call's eight-node budget across those rows stops
        # before the viewport. Each row gets its own budget, and the
        # read starts near the viewport rather than at child 0.
        if count > _ROW_SEQUENCE_CAP:
            edges = _viewport_edges(container)
            line = edges[1] if edges is not None else 0.0
            start = _first_index_reaching(node, line, count)
            start = max(0, start - 40)
            for index in range(start, min(count, start + _ROW_SEQUENCE_CAP)):
                if len(found) >= _ROW_SEQUENCE_CAP:
                    return
                walk(_child_at(node, index), depth - 1, [8])
            return
        for index in range(count):
            if budget[0] <= 0 or len(found) >= _ROW_SEQUENCE_CAP:
                return
            walk(_child_at(node, index), depth - 1, budget)

    def walk(node, depth: int, budget: list[int]) -> None:
        if node is None or node is container or len(found) >= _ROW_SEQUENCE_CAP or budget[0] <= 0:
            return
        budget[0] -= 1
        role = _role_name(node)
        if role in _SKIP_ROW_ROLES or role in _LIST_ROLES:
            return
        npos, nsize = _extents(node)
        if npos is None or nsize is None or float(nsize[0]) <= 0 or float(nsize[1]) <= 0:
            descend(node, depth, budget)
            return
        if float(nsize[1]) > full_h * 0.9:
            descend(node, depth, budget)
            return
        name = _node_name(node)
        if not name or _is_list_marker(name):
            descend(node, depth, budget)
            return
        found.append(node)

    count = _child_count(container)
    if count <= _ROW_SEQUENCE_CAP:
        indexes = range(count)
    else:
        edges = _viewport_edges(container)
        line = edges[1] if edges is not None else 0.0
        start = _first_index_reaching(container, line, count)
        # Forty children above the viewport cover the previous head of
        # a line step. The cap still bounds the read.
        start = max(0, start - 40)
        indexes = range(start, min(count, start + _ROW_SEQUENCE_CAP))
    for index in indexes:
        if len(found) >= _ROW_SEQUENCE_CAP:
            break
        # Eight nodes cover a list item, its marker, and its label. The
        # budget is per row so row 100 is still a scroll_to target.
        walk(_child_at(container, index), _VISIBLE_DESCEND, [8])
    found.sort(key=_row_top)
    return found


def scroll_viewport_by_lines(container, dy: int) -> bool:
    """Reveal a row a few lines from the on-screen head. No wheel.

    False when ``container`` is not a Chromium list, when no further row
    exists, or when ``scroll_to`` is unavailable. True means ``scroll_to``
    was called. The caller still requires the list pixels to move. One
    line is one content row. The step is not larger than the rows already
    on screen, so a later snapshot still overlaps this one. Already being
    on the first or last row returns False and does not call ``scroll_to``.
    """
    if not int(dy) or _role_name(container) not in _LIST_ROLES or not _chromium_app(container):
        return False
    ordered = _named_rows_in_order(container)
    if len(ordered) < 2:
        return False
    names = _row_names(_in_view_named_rows(container))
    if not names:
        return False
    head = names[0]
    index = next((i for i, acc in enumerate(ordered) if _node_name(acc) == head), None)
    if index is None:
        return False
    step = min(abs(int(dy)) * _ROWS_PER_LINE, max(1, len(names) - 1))
    target = index + step if int(dy) > 0 else index - step
    target = max(0, min(len(ordered) - 1, target))
    if target == index:
        return False
    return scroll_to_edge(ordered[target], "TOP_EDGE")


def _child_scrollbars(node) -> list:
    """Scroll bars that are direct children of ``node``, including late ones.

    A fixed-height list can hold every row and put the bar after them.
    ``_collect_scrollbars`` only peeks at the first children of a long
    parent, which misses that bar. This walk is only the direct children,
    capped, and it does not replace ``_collect_scrollbars``.
    """
    found: list = []
    count = min(_child_count(node), _VISIBLE_WALK_CAP)
    for index in range(count):
        child = _child_at(node, index)
        if _is_scroll_bar(child):
            found.append(child)
    return found


def _bar_range(bar) -> tuple[float, float, float] | None:
    """``(current, minimum, maximum)`` for a vertical scroll bar with a range."""
    if _bar_axis(bar) != "vertical":
        return None
    current = _read_value(bar)
    if current is None:
        return None
    minimum = _value_bound(bar, ("get_minimum_value", "getMinimumValue"), current)
    maximum = _value_bound(bar, ("get_maximum_value", "getMaximumValue"), current)
    if maximum <= minimum:
        return None
    return current, minimum, maximum


def _bar_sits_on_list(bar, origin, size) -> bool:
    """True when ``bar`` overlaps the list, or sits on its right edge.

    A sibling of the list is the overflow bar only when it lines up with
    that box. A document bar elsewhere is not.
    """
    pos, bar_size = _extents(bar)
    if pos is None or bar_size is None:
        return False
    left = float(origin[0])
    top = float(origin[1])
    right = left + float(size[0])
    bottom = top + float(size[1])
    bx = float(pos[0])
    by = float(pos[1])
    bh = float(bar_size[1])
    if by + bh < top or by > bottom:
        return False
    return left - 1 <= bx <= right + 32


def _viewport_vertical_bar(container):
    """The overflow list's vertical bar, or a sibling that sits on the list.

    None when the only bars are elsewhere. Direct children win, so a bar
    inside the list is not replaced by a document bar on the parent.
    """
    for bar in _child_scrollbars(container):
        if _bar_range(bar) is not None:
            return bar
    parent = _parent_of(container)
    if parent is None:
        return None
    origin, size = _extents(container)
    if origin is None or size is None:
        return None
    for bar in _child_scrollbars(parent):
        if _bar_range(bar) is None or not _bar_sits_on_list(bar, origin, size):
            continue
        return bar
    return None


def _viewport_bar_delta(container, dy: int, span: float) -> float | None:
    """Signed bar step for one line scroll, or None when the scale is unknown.

    One line is one content row, and not more than the rows already on
    screen. The 0.4.23 overflow rows were 28px apart, so five lines is
    five rows. When the named rows extend past the viewport,
    that extent is the scroll range: a bar on that scale (pixels) is
    stepped by the pixel distance, and a smaller bar (a fraction, or one
    unit per row) is stepped by the same distance as a fraction of the
    extent. A bar that does not show rows past the viewport is stepped
    only when its range is already in pixels. Guessing a fraction there
    would jump to the end.
    """
    rows = _in_view_named_rows(container)
    if len(rows) < 2:
        return None
    pitch = abs(float(rows[1][1][1]) - float(rows[0][1][1]))
    if pitch < 1:
        pitch = max(1.0, float(rows[0][2][1]))
    step_rows = min(abs(int(dy)) * _ROWS_PER_LINE, max(1, len(rows) - 1))
    pixel_step = step_rows * pitch
    if pixel_step <= 0 or span <= 0:
        return None
    ordered = _named_rows_in_order(container)
    if len(ordered) < 2:
        return None
    tops = [_row_top(acc) for acc in ordered]
    row_span = max(tops) - min(tops)
    # A long list's ordered window is the viewport, not the content.
    # Scaling a fractional bar by that window jumps. The ends are the
    # content height. A list that fits in the sequence cap keeps the
    # span of the rows it already collected.
    if _child_count(container) > _ROW_SEQUENCE_CAP:
        extent = _content_ends_span(container)
        if extent is not None and extent > row_span:
            row_span = extent
    _origin, size = _extents(container)
    if size is None:
        return None
    viewport_h = float(size[1])
    if row_span > viewport_h:
        if span >= row_span * 0.5:
            magnitude = pixel_step * (span / row_span)
        else:
            magnitude = pixel_step / row_span * span
    elif span >= viewport_h and span >= pixel_step:
        magnitude = pixel_step
    else:
        return None
    if magnitude <= 0:
        return None
    return magnitude if int(dy) > 0 else -magnitude


def nudge_viewport_scrollbar(container, dy: int) -> tuple[str, object]:
    """Step the overflow list's vertical bar. No wheel and no ``scroll_to``.

    Returns ``("moved", undo)`` when the value changed by the requested
    step. ``undo`` writes the previous value. ``"absent"`` means this list
    has no vertical bar, so the caller may try ``scroll_to``. ``"rejected"``
    means a bar is there and this step is not a scroll: the bar is already
    at that end, the write jumped past the request, or the bar's scale is
    not one this function will guess. A jump is undone here. The caller
    still requires the list pixels and the on-screen head to change, and
    undoes a write that does not. This does not call ``_collect_scrollbars``
    or ``_nudge_scrollbar``.
    """
    if not int(dy) or _role_name(container) not in _LIST_ROLES or not _chromium_app(container):
        return "absent", None
    bar = _viewport_vertical_bar(container)
    if bar is None:
        return "absent", None
    found = _bar_range(bar)
    if found is None:
        return "rejected", None
    current, minimum, maximum = found
    span = maximum - minimum
    delta = _viewport_bar_delta(container, int(dy), span)
    if delta is None:
        return "rejected", None
    target = min(max(current + float(delta), minimum), maximum)
    expected = target - current
    floor = 1e-4 if span <= 2 else 0.5
    if abs(expected) < floor:
        return "rejected", None
    _write_value(bar, target)
    updated = _read_value(bar)
    if updated is None:
        _write_value(bar, current)
        return "rejected", None
    actual = updated - current
    tolerance = max(span * 0.02, 1e-4) if span <= 2 else max(1.0, abs(expected) * 0.05)
    jumped = abs(actual) > abs(expected) + tolerance
    wrong_way = (expected > 0 and actual < 0) or (expected < 0 and actual > 0)
    if jumped or wrong_way or abs(actual) < floor:
        _write_value(bar, current)
        return "rejected", None

    def undo() -> None:
        _write_value(bar, current)

    return "moved", undo


def viewport_track_point(container, dy: int) -> tuple[int, int] | None:
    """A point on the overflow list's vertical track. No value write.

    Downward is the lower track. Upward is the upper track. The bar's own
    box is used when that bar sits on the list. Otherwise the point is
    inside the list, in the right-hand gutter, where the track is drawn.
    The arrow buttons at the ends of a classic bar are not the point.
    None when ``container`` is not a Chromium list or has no box. The
    caller still requires the painted rows to change.
    """
    if not int(dy) or _role_name(container) not in _LIST_ROLES or not _chromium_app(container):
        return None
    box: tuple[int, int, int, int] | None = None
    bar = _viewport_vertical_bar(container)
    if bar is not None:
        pos, size = _extents(bar)
        if (
            pos is not None
            and size is not None
            and float(size[0]) >= 1
            and float(size[1]) >= 8
        ):
            box = (int(pos[0]), int(pos[1]), int(size[0]), int(size[1]))
    if box is None:
        ancestor = overflow_ancestor_box(container)
        list_box = ancestor if ancestor is not None else list_screen_box(container)
        if list_box is None:
            return None
        x, y, width, height = list_box
        if width < 16 or height < 16:
            return None
        gutter = min(14, width // 5)
        box = (x + width - gutter, y, gutter, height)
    bx, by, bw, bh = box
    margin = min(max(bh // 8, 4), max(bh // 3, 1))
    if int(dy) > 0:
        y = by + bh - margin
    else:
        y = by + margin
    y = min(max(y, by + 1), by + max(bh - 2, 1))
    x = bx + max(bw // 2, 0)
    x = min(max(x, bx), bx + max(bw - 1, 0))
    return int(x), int(y)


def _probe_visible_rows(container) -> list[tuple]:
    """Rows on screen inside ``container``.

    Each entry is ``(accessible, (x, y), (width, height))``. Children whose
    own tops are on or below the on-screen top of the list win over the hit
    test: the 0.4.15 retest kept ITEM-001 at y=-2 because that box still
    covered the sample. A hit whose top is above that edge is dropped even
    when it covers the sample. Otherwise the rectangle is the span of
    samples that hit that accessible. Samples that hit the container itself
    are skipped. Samples start below the clipped edge so that edge is not
    the head. The samples cover the on-screen part of the list, not the
    content origin: a list at y=-4847 is not sampled there.
    """
    edges = _viewport_edges(container)
    if edges is None:
        return []
    box_x, box_y, box_r, box_b = edges
    top = int(box_y)
    bottom = int(box_b)
    sliver = int(_clip_sliver(box_b - box_y))
    child_rows = _in_view_named_rows(container)
    if len(_row_names(child_rows)) >= 2:
        return child_rows
    comp = _component(container)
    if comp is None:
        return []
    coord = getattr(getattr(_atspi(), "CoordType", None), "SCREEN", 0)
    x = int((box_x + box_r) / 2)
    step = max(12, int((bottom - top) // 12) or 12)
    grouped: list[list] = []
    # Skip the clipped sliver at the top edge. A hit there is the row above
    # the on-screen head, which is what 0.4.12 published as ITEM-009.
    y = top + sliver
    samples = 0
    while y < bottom and samples < _MAX_ROW_SAMPLES:
        samples += 1
        hit = _settle_hit(comp, x, y, coord)
        if (
            hit is not None
            and hit is not container
            and not _top_above_line(hit, float(top))
        ):
            if grouped and grouped[-1][0] is hit:
                grouped[-1][2] = y + step
            else:
                grouped.append([hit, y, y + step])
        y += step
    rows = []
    for acc, y0, y1 in grouped:
        height = max(1, min(y1, bottom) - y0)
        rows.append((acc, (float(box_x), float(y0)), (float(box_r - box_x), float(height))))
    return rows


def _row_names(probed) -> tuple[str, ...]:
    names: list[str] = []
    for acc, _pos, _size in probed:
        name = _node_name(acc)
        if name and (not names or names[-1] != name):
            names.append(name)
    return tuple(names)


def _list_key(container) -> tuple | None:
    """Stable identity of a list box. Not ``id()``: a live walk wraps a new
    Python object each time, while the box itself does not move when the
    rows scroll.
    """
    pos, size = _extents(container)
    if pos is None:
        return None
    return (
        _role_name(container),
        _node_name(container),
        int(pos[0]),
        int(pos[1]),
        int(size[0]),
        int(size[1]),
    )


def reset_shown_rows() -> None:
    """Drop confirmed list rows. Tests call this so one list cannot leak
    into the next."""
    _SHOWN.clear()


def _saved_for(container) -> list | None:
    """Rows last confirmed for this list, including when its box jitters.

    The exact box is the first choice. A one-pixel change in the reported
    extents must not miss that entry and fall through to the cached
    children: after a scroll those children are the pre-scroll row parked
    above the viewport. A content-height list whose origin has moved by
    more than a few pixels is a different box: the old rows are not it.
    """
    key = _list_key(container)
    if key is None:
        return None
    found = _SHOWN.get(key)
    if found is not None:
        return found
    role, name, x, y, width, height = key
    best = None
    best_area = 0
    for saved_key, rows in _SHOWN.items():
        if len(saved_key) != 6 or saved_key[0] != role or saved_key[1] != name:
            continue
        sx, sy, sw, sh = saved_key[2], saved_key[3], saved_key[4], saved_key[5]
        if abs(sx - x) > _BOX_JITTER_PX or abs(sy - y) > _BOX_JITTER_PX:
            continue
        overlap_w = min(x + width, sx + sw) - max(x, sx)
        overlap_h = min(y + height, sy + sh) - max(y, sy)
        area = max(0, overlap_w) * max(0, overlap_h)
        if area > best_area:
            best = rows
            best_area = area
    return best


def saved_rows(container) -> list | None:
    """Rows last confirmed for ``container``, or None when nothing is saved."""
    return _saved_for(container)


def _known_head_above(container, rows) -> bool:
    """True when the first row's own top is above the list's top edge.

    False when that row has no box. A stored sample rectangle is not the
    check: the accessible's live extents are. A row flush with the list
    is not above the edge.
    """
    line = _head_line(container)
    if line is None or not rows:
        return False
    return _top_above_line(rows[0][0], line)


def commit_shown_rows(container, rows) -> None:
    """Remember ``rows`` as the on-screen contents of ``container``.

    Fewer than two named rows are not a page, and are not stored. A head
    whose own top is above the list is not stored either.
    """
    key = _list_key(container)
    if key is None or len(_row_names(rows or ())) < 2:
        return
    if _known_head_above(container, rows):
        return
    _SHOWN[key] = list(rows)


def row_head(rows) -> str | None:
    """The first named row, which is the snapshot head."""
    names = _row_names(rows or ())
    return names[0] if names else None


def shown_line_step(container, before_head: str | None, rows, dy: int) -> bool:
    """True when the head moved by at most one content row per requested line.

    The 0.4.28 retest asked for five lines. The list moved five rows, and
    the grab stayed at or under the uniform-row floor, so the driver
    treated the step as no move and clicked the track. Five rows plus that
    page is the jump of 31 on a fixed-height list and 18 on overflow:auto,
    and the next window skipped the rows in between. A move of about
    ``dy`` rows is the line step. A page is not. A hit-test name that is
    not a row within that count is not, so a still page that names a far
    row stays still.
    """
    if not int(dy) or not before_head or not rows:
        return False
    after = row_head(rows)
    if after is None or after == before_head:
        return False
    names: list[str] = []
    for acc in _named_rows_in_order(container):
        name = _node_name(acc)
        if name and (not names or names[-1] != name):
            names.append(name)
    try:
        delta = names.index(after) - names.index(before_head)
    except ValueError:
        return False
    limit = abs(int(dy)) * _ROWS_PER_LINE
    if int(dy) > 0:
        return 1 <= delta <= limit
    return -limit <= delta <= -1


def row_names(rows) -> tuple[str, ...]:
    return _row_names(rows or ())


def list_container(acc):
    """The Chromium list, table, or tree that contains ``acc``.

    ``acc`` may be that list or a row inside it. The walk uses parents.
    None when no such list is found, or when the list's application is not
    Chromium. A coordinate target has no accessible and is not checked.
    """
    if acc is None:
        return None
    node = acc
    seen: set[int] = set()
    for _ in range(8):
        if node is None or id(node) in seen:
            return None
        seen.add(id(node))
        if _role_name(node) in _LIST_ROLES and _extents(node)[0] is not None:
            return node if _chromium_app(node) else None
        node = _parent_of(node)
    return None


def list_screen_box(container) -> tuple[int, int, int, int] | None:
    """Screen box of the list widget, ``(x, y, width, height)``.

    This is the list's own extents, not a row's. None when the list has
    no box.
    """
    pos, size = _extents(container)
    if pos is None:
        return None
    return (int(pos[0]), int(pos[1]), int(size[0]), int(size[1]))


def _shown_or_probe(container) -> list:
    """Rows for ``container``.

    Rows whose own tops are on or below the list's top come first. A saved
    head is not returned ahead of them: the 0.4.16 retest kept ITEM-001 at
    y=-2 for 12 seconds after the pixels had moved. A saved window is used
    only when this list has no such rows, and only while its head is not
    above the list. A later hit test does not replace a head that is still
    inside the list. A probe whose head is above the list is not stored.
    """
    live = _in_view_named_rows(container)
    if len(_row_names(live)) >= 2 and not _known_head_above(container, live):
        key = _list_key(container)
        if key is not None:
            _SHOWN[key] = list(live)
        return live
    saved = _saved_for(container)
    if saved is not None and not _known_head_above(container, saved):
        return saved
    key = _list_key(container)
    rows = _probe_visible_rows(container)
    if _known_head_above(container, rows):
        rows = []
    if key is not None and len(_row_names(rows)) >= 2:
        _SHOWN[key] = rows
        return rows
    if saved is not None and not _known_head_above(container, saved):
        return saved
    return rows


def ensure_shown_rows(container) -> list | None:
    """Confirmed rows, probing and storing them when the list has none yet."""
    saved = saved_rows(container)
    if saved is not None and len(_row_names(saved)) >= 2:
        return saved
    commit_shown_rows(container, _probe_visible_rows(container))
    return saved_rows(container)


def wait_for_shown_rows(container, previous_head: str | None) -> list | None:
    """Rows whose head differs from ``previous_head`` on two probes in a row.

    The head is the first row whose own top is on or below the list's top
    and which extends below the clipped edge. A row above the list is not
    a head, even when its box covers the sample or it is the first cached
    child. The 0.4.16 retest moved the
    pixels and still published that child (ITEM-001 at y=-2, and ITEM-013
    at y=-26). None when no on-screen head ever leaves ``previous_head``.
    The caller does not treat that as a scroll and does not install the
    off-screen row.
    """
    last: str | None = None
    for attempt in range(_HEAD_POLLS):
        rows = _probe_visible_rows(container)
        names = _row_names(rows)
        head = names[0] if len(names) >= 2 else None
        if head is not None and head == last and head != previous_head:
            return rows
        last = head
        if attempt + 1 < _HEAD_POLLS:
            time.sleep(_HEAD_PAUSE_S)
    return None


# Descendant names collected for one line-scroll signature. Chrome's row
# label is often a nested static text, not the direct child's name. The walk
# stops at the first list of four or more names so a window full of toolbar
# buttons is not what gets compared.
_LIST_NAME_LIMIT = 48
_LIST_DEPTH = 4
_LIST_ANCESTORS = 5


def _invalidate_tree_cache(acc) -> None:
    """Drop the AT-SPI child cache on ``acc`` and its ancestors.

    libatspi answers ``get_child_at_index`` from that cache once it has been
    filled. A line scroll reads the list before the wheel, so the read after
    the wheel can still be the pre-wheel rows. A later snapshot, or a fresh
    process, fetches the children again and shows the move.
    """
    node = acc
    seen: set[int] = set()
    for _ in range(_MAX_SCROLL_ANCESTORS):
        if node is None or id(node) in seen:
            break
        seen.add(id(node))
        _call_first(node, ("clear_cache", "clearCache"))
        node = _parent_of(node)


def _descendant_names(acc) -> tuple[str, ...]:
    """Names under ``acc``, not including ``acc`` itself."""
    names: list[str] = []
    queue: list[tuple[object, int]] = []
    count = min(_child_count(acc), _LIST_NAME_LIMIT)
    for index in range(count):
        child = _child_at(acc, index)
        if child is not None:
            queue.append((child, _LIST_DEPTH))
    seen = 0
    while queue and len(names) < _LIST_NAME_LIMIT and seen < _LIST_NAME_LIMIT:
        node, remaining = queue.pop(0)
        seen += 1
        name = _node_name(node)
        if name:
            names.append(name)
        if remaining <= 1:
            continue
        child_count = min(_child_count(node), _LIST_NAME_LIMIT - seen)
        for index in range(child_count):
            child = _child_at(node, index)
            if child is not None:
                queue.append((child, remaining - 1))
    return tuple(names)


def list_signature(acc) -> tuple[str, ...] | None:
    """Names of the nearest visible list under ``acc``.

    None when fewer than two named nodes are visible. A text area with no
    such list is not judged a failure just because its own name stays put.
    The child cache is cleared first. Descendant names are included, so a
    row label that lives on a nested static text counts. A longer list
    further up replaces a short one: two scroll-bar labels must not hide
    the rows. Four or more names stops the walk.
    """
    if acc is None:
        return None
    _invalidate_tree_cache(acc)
    best: tuple[str, ...] | None = None
    node = acc
    seen: set[int] = set()
    for _ in range(_LIST_ANCESTORS):
        if node is None or id(node) in seen:
            break
        seen.add(id(node))
        names = _descendant_names(node)
        if len(names) >= 2 and (best is None or len(names) > len(best)):
            best = names
            if len(best) >= 4:
                break
        node = _parent_of(node)
    return best


def _collect_scrollbars(start) -> list:
    """Scroll bars on ``start`` and its ancestors, nearest first.

    GTK puts the bars on the scroll pane, beside the viewport that holds the
    text, so the pane is an ancestor of the content and the bars are its
    children. A busy window is only peeked at (first children), which is
    enough to see a scroll pane sitting next to a toolbar."""
    found: list = []
    seen: set[int] = set()
    visits = 0

    def consider(acc) -> None:
        nonlocal visits
        if acc is None or visits >= _MAX_SCROLL_NODES:
            return
        marker = id(acc)
        if marker in seen:
            return
        seen.add(marker)
        visits += 1
        if _is_scroll_bar(acc):
            found.append(acc)

    node = start
    for _ in range(_MAX_SCROLL_ANCESTORS):
        if node is None or visits >= _MAX_SCROLL_NODES:
            break
        consider(node)
        count = _child_count(node)
        limit = count if count <= 16 else 12
        for i in range(limit):
            if visits >= _MAX_SCROLL_NODES:
                break
            child = _child_at(node, i)
            consider(child)
            if child is None or _is_scroll_bar(child):
                continue
            nested = _child_count(child)
            if 0 < nested <= 8:
                for j in range(nested):
                    if visits >= _MAX_SCROLL_NODES:
                        break
                    consider(_child_at(child, j))
        node = _parent_of(node)
    return found


def _state_contains(acc, name: str) -> bool:
    Atspi = _atspi()
    sset = _call_first(acc, ("get_state_set",))
    if sset is None:
        return False
    member = getattr(getattr(Atspi, "StateType", None), name, None)
    if member is None:
        return False
    return bool(_safe(lambda: sset.contains(member), False))


def _bar_axis(acc) -> str | None:
    """'vertical' or 'horizontal' from AT-SPI state, else the bar's extents."""
    vertical = _state_contains(acc, "VERTICAL")
    horizontal = _state_contains(acc, "HORIZONTAL")
    if vertical and not horizontal:
        return "vertical"
    if horizontal and not vertical:
        return "horizontal"
    _position, size = _extents(acc)
    if not size:
        return None
    width, height = size
    if height > width:
        return "vertical"
    if width > height:
        return "horizontal"
    return None


def _value_call(names: tuple[str, ...], acc, *args):
    """Call the first `Atspi.Value` class method in ``names`` (binding-tolerant)."""
    value_iface = getattr(_atspi(), "Value", None)
    if value_iface is None:
        return None
    for name in names:
        method = getattr(value_iface, name, None)
        if method is not None:
            return _safe(lambda m=method: m(acc, *args))
    return None


def _read_value(acc) -> float | None:
    raw = _value_call(("get_current_value", "getCurrentValue"), acc)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _value_bound(acc, names: tuple[str, ...], fallback: float) -> float:
    raw = _value_call(names, acc)
    if raw is None:
        return fallback
    try:
        return float(raw)
    except (TypeError, ValueError):
        return fallback


def _write_value(acc, value: float) -> None:
    _value_call(("set_current_value", "setCurrentValue"), acc, float(value))


def _nudge_scrollbar(acc, delta: int, *, current: float) -> bool:
    """Set ``acc``'s value by ``delta`` and require the read-back to match.

    A zero-range bar (nothing to scroll) is not a success: the caller tries
    the next bar. A bar already at the limit we know about reports success
    without moving. A toolkit that clamps short of the reported maximum
    (GTK's maximum is often ``upper``, while the visible end is
    ``upper - page_size``) is a success only if one more pixel will not move.
    A write that jumps farther than the request (a wheel notch, a page) is
    undone and rejected.
    """
    minimum = _value_bound(acc, ("get_minimum_value", "getMinimumValue"), current)
    maximum = _value_bound(acc, ("get_maximum_value", "getMaximumValue"), current)
    if minimum > maximum:
        minimum, maximum = maximum, minimum
    target = min(max(current + float(delta), minimum), maximum)
    expected = target - current
    if abs(expected) < 0.5 and abs(float(delta)) >= 1 and (maximum - minimum) < 1.0:
        return False
    _write_value(acc, target)
    updated = _read_value(acc)
    if updated is None:
        _write_value(acc, current)
        return False
    actual = updated - current
    tolerance = max(1.0, abs(expected) * 0.05)
    if abs(actual - expected) <= tolerance:
        return True
    undershoot = abs(actual) + 0.5 < abs(expected) and (
        (delta > 0 and actual > 0.5) or (delta < 0 and actual < -0.5)
    )
    if undershoot and _stuck_at_limit(acc, updated, delta):
        return True
    if abs(actual) >= 0.5:
        _write_value(acc, current)
    return False


def _stuck_at_limit(acc, updated: float, delta: int) -> bool:
    """True when one more pixel from ``updated`` does not move the bar.

    If the probe moves, ``updated`` is written back and this returns False;
    the caller then restores the pre-nudge value. A short write stays only
    when that extra pixel does not move."""
    step = 1.0 if delta > 0 else -1.0
    _write_value(acc, updated + step)
    probed = _read_value(acc)
    if probed is None:
        _write_value(acc, updated)
        return False
    if abs(probed - updated) >= 0.5:
        _write_value(acc, updated)
        return False
    return True


def _apply_axis(bars, axis: str, delta: int) -> tuple[object, float] | None:
    """Nudge the nearest ``axis`` bar. Returns (bar, previous value) on success."""
    for bar in bars:
        if _bar_axis(bar) != axis:
            continue
        before = _read_value(bar)
        if before is None:
            continue
        if _nudge_scrollbar(bar, delta, current=before):
            return bar, before
    return None


def scroll_by_pixels(acc, *, dx: int = 0, dy: int = 0) -> bool:
    """Move ``acc``'s scroll bars by ``dx``/``dy`` via AT-SPI Value.

    Positive ``dy`` increases the vertical bar (content moves up). Positive
    ``dx`` increases the horizontal bar (content moves left). GTK scrolled
    windows expose that value in pixels, so a write of +3 that reads back as
    +3 is a 3-pixel scroll. Returns False when a requested axis has no bar,
    or when the value that reads back is not the requested delta. Does not
    send wheel events. On a partial failure the bars already written are put
    back.
    """
    dx, dy = int(dx), int(dy)
    if dx == 0 and dy == 0:
        return True
    if acc is None:
        return False
    bars = _collect_scrollbars(acc)
    applied: list[tuple[object, float]] = []
    for axis, delta in (("vertical", dy), ("horizontal", dx)):
        if not delta:
            continue
        done = _apply_axis(bars, axis, delta)
        if done is None:
            for prev, old in reversed(applied):
                _write_value(prev, old)
            return False
        applied.append(done)
    return True


def _accessible_at_point(x: int, y: int):
    """Deepest accessible under screen point ``(x, y)``, or None."""
    Atspi = _atspi()
    desktop = _safe(lambda: Atspi.get_desktop(0))
    if desktop is None:
        return None
    found = _deepest_at_point(desktop, int(x), int(y), 0, set())
    if found is None or found is desktop:
        return None
    return found


def _deepest_at_point(acc, x: int, y: int, depth: int, seen: set[int]):
    if acc is None or depth > 24:
        return None
    marker = id(acc)
    if marker in seen:
        return None if depth == 0 else acc
    seen.add(marker)
    comp = _component(acc)
    if comp is None:
        return None if depth == 0 else acc
    coord = getattr(getattr(_atspi(), "CoordType", None), "SCREEN", 0)
    child = _call_first(
        comp, ("get_accessible_at_point", "getAccessibleAtPoint"), x, y, coord
    )
    if child is None or id(child) in seen:
        return None if depth == 0 else acc
    nested = _deepest_at_point(child, x, y, depth + 1, seen)
    return nested if nested is not None else child


def scroll_at_point(x: int, y: int, *, dx: int = 0, dy: int = 0) -> bool:
    """Pixel-scroll whatever accessible is under screen point ``(x, y)``.

    Hit-tests with ``Component.get_accessible_at_point`` and then applies
    `scroll_by_pixels`. Returns False when the point hits nothing that
    exposes a scroll bar — the caller must not fall back to wheel notches.
    """
    acc = _accessible_at_point(int(x), int(y))
    if acc is None:
        return False
    return scroll_by_pixels(acc, dx=dx, dy=dy)
