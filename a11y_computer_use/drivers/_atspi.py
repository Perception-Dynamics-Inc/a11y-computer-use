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
from collections.abc import Sequence

from a11y_computer_use.observe import DisplayGeometry, RawNode
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

# AT-SPI action name (lowercased) -> AX action the pruning engine treats as
# "interactive" (`_PRESS_ACTIONS` = AXPress/AXOpen/AXConfirm/AXPick).
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


def _extents(acc):
    """(position, size) in SCREEN pixels, or (None, None)."""
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
    """(enabled, focused, checked, selected, expanded) from the state set.

    checked/expanded are None when the element is not checkable/expandable so
    the shared schema can tell "off" apart from "not a checkbox"."""
    Atspi = _atspi()
    sset = _call_first(acc, ("get_state_set",))
    if sset is None:
        return True, False, None, False, None
    st = getattr(Atspi, "StateType", None)

    def has(name: str) -> bool:
        member = getattr(st, name, None)
        return bool(member is not None and _safe(lambda: sset.contains(member), False))

    enabled = has("ENABLED") or has("SENSITIVE")
    focused = has("FOCUSED")
    checked = has("CHECKED") if (has("CHECKABLE") or has("CHECKED")) else None
    selected = has("SELECTED")
    expanded = has("EXPANDED") if has("EXPANDABLE") else None
    return enabled, focused, checked, selected, expanded


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


class ATSPIAccessor:
    """`observe.TreeAccessor` over `Atspi.Accessible` handles."""

    def read(self, node: object) -> RawNode:
        role_str = _role_name(node)
        role = _ROLE.get(role_str, "AXGroup")
        attrs = _get_attributes(node)  # one D-Bus fetch, reused for role + id
        role = _refine_web_role(role, attrs)
        position, size = _extents(node)
        enabled, focused, checked, selected, expanded = _state_flags(node)
        name = _call_first(node, ("get_name",), default="") or ""
        description = _call_first(node, ("get_description",), default="") or ""
        return RawNode(
            role=role,
            subrole=None,
            title=str(name),
            description=str(description),
            enabled=enabled,
            focused=focused,
            position=position,
            size=size,
            actions=_action_names(node),
            # skip the value probe (2 D-Bus calls) on roles that never have one
            value=None if role in _NO_VALUE_ROLES else _value_text(node, role),
            checked=checked,
            selected=selected,
            expanded=expanded,
            stable_id=_stable_id(node, attrs),
        )

    def children(self, node: object) -> Sequence[object]:
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

        screen = _xd.Display().screen()
        w = int(screen.width_in_pixels)
        h = int(screen.height_in_pixels)
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


def focused_secure(app: str, *, max_nodes: int = 400) -> bool | None:
    """Whether the keyboard-focused node of ``app``'s active window is a
    password field: True / False / None.

    True: the focused node is a password field. False: a focused non-secure
    node was found, or the whole frame was walked and nothing is focused, or the
    app is not on the bus (no signal, the same degradation as the macOS
    ``AXFocusedUIElement`` probe). None: the frame is larger than the walk bound
    (or a node had more than `_MAX_CHILDREN_FETCH` children) and no focused node
    was met, so focus is UNKNOWN; the caller must not type blind on None.

    AT-SPI has no global "focused accessible" getter (focus arrives as events).
    The Collection interface answers in one round-trip where the toolkit
    exposes it; otherwise the active top-level frame is walked breadth-first,
    bounded by ``max_nodes``. This is the check `LinuxDriver.type_text` runs
    before the XTEST path, which types into whatever holds focus."""
    from a11y_computer_use.schema import Scope

    root = find_root(app, Scope.WINDOW)
    if root is None:
        return False
    Atspi = _atspi()
    st = getattr(Atspi, "StateType", None)
    focused_state = getattr(st, "FOCUSED", None)
    if focused_state is None:
        return False
    found, acc = _focused_via_collection(root, Atspi, focused_state)
    if found:
        return is_secure(acc) if acc is not None else False
    queue = [root]
    seen = 0
    truncated = False
    while queue and seen < max_nodes:
        acc = queue.pop(0)
        seen += 1
        sset = _call_first(acc, ("get_state_set",))
        if sset is not None and _safe(lambda: sset.contains(focused_state), False):
            return is_secure(acc)
        n = int(_call_first(acc, ("get_child_count",), default=0) or 0)
        if n > _MAX_CHILDREN_FETCH:
            truncated = True
        for j in range(min(n, _MAX_CHILDREN_FETCH)):
            child = _call_first(acc, ("get_child_at_index",), j)
            if child is not None:
                queue.append(child)
    if queue or truncated:
        return None  # bound exhausted: focus unknown, not "not secure"
    return False


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


def insert_text(acc, text: str) -> bool:
    """Insert ``text`` at the caret (end of the field) via AT-SPI EditableText —
    the deterministic, a11y-first text-entry path. Unlike synthetic XTEST keys it
    needs no X/widget focus (which headless AT-SPI grab_focus does not grant), so
    it lands reliably. Returns False if the element exposes no EditableText."""
    eti = _editable_iface(acc)
    if eti is None:
        return False
    offset = _safe(lambda: _atspi().Text.get_character_count(acc)) or 0
    return bool(_call_first(eti, ("insert_text",), int(offset), text, len(text), default=False))


def set_text(acc, text: str) -> bool:
    """Replace the element's whole text via AT-SPI EditableText (True on success)."""
    eti = _editable_iface(acc)
    if eti is None:
        return False
    return bool(_call_first(eti, ("set_text_contents",), text, default=False))


def scroll_to(acc) -> bool:
    """Reveal ``acc`` via AT-SPI (`Component.scroll_to ANYWHERE`) — no cursor move."""
    Atspi = _atspi()
    comp = _component(acc)
    if comp is None:
        return False
    stype = getattr(getattr(Atspi, "ScrollType", None), "ANYWHERE", 0)
    return bool(_call_first(comp, ("scroll_to",), stype, default=False))
