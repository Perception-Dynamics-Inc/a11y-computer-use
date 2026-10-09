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

import math
import os
import re
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
    "dialog": "AXDialog",
    "alert": "AXDialog",
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
    # LibreOffice Calc. The name is the address (A1). AXGroup is in
    # _NO_VALUE_ROLES, so mapping this to a group hid the cell text.
    # A GTK file chooser's body cells use the same role and stay in the
    # snapshot by name, via `_with_table_body`.
    "table cell": "AXCell",
    "combo box": "AXComboBox",
    "tool bar": "AXToolbar",
    "scroll bar": "AXScrollBar",
    "slider": "AXSlider",  # Gtk.Scale and friends: draggable, carries a Value iface
    "progress bar": "AXProgressIndicator",
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
    the AT-SPI2 typelib are absent (the driver turns that into a clear message).

    The import stays inside this function. A missing ``gi`` module, or a
    typelib ``require_version`` cannot load, is ImportError either way.
    """
    global _inited
    try:
        import gi
    except ImportError as exc:
        raise ImportError("PyGObject is not installed; the AT-SPI2 binding is unavailable") from exc
    try:
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError) as exc:
        raise ImportError("the AT-SPI2 typelib is not available") from exc

    if not _inited:
        _safe(Atspi.init)  # 0 = ok, 1 = already running; both fine
        # Per-call D-Bus wait is 300ms. The second argument is the startup
        # grace: while an app is younger than that, libatspi waits the grace
        # instead (default 15000). An unanswered read of a just-opened GTK
        # dialog then blocks type and snapshot for 15s. Grace 0 keeps the
        # 300ms bound from the first call.
        _safe(lambda: Atspi.set_timeout(300, 0))
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


# Chromium embeds each list option in the parent's text as U+FFFC (object
# replacement). That character is not the selected option.
_OBJECT_REPLACEMENT = "\ufffc"
_CHOICE_ROLES = frozenset({"AXComboBox", "AXList", "AXPopUpButton"})
_CHOICE_ROLE_NAMES = frozenset({"combo box", "list box", "list"})
_OPTION_ROLE_NAMES = frozenset({
    "menu item", "check menu item", "radio menu item", "list item", "list box item",
})
_OPTION_CONTAINER_NAMES = frozenset({
    "menu", "popup menu", "list", "list box", "panel", "filler", "scroll pane", "combo box",
})
_ROW_ROLE_NAMES = frozenset({
    "table cell", "tree item", "list item", "table row", "row", "list box item",
})
# A flat GTK tree cell's first action is expand or edit. Performing it returns
# success and does not move the selection. A Chrome list option's click does.
_SELECTING_ACTION_NAMES = frozenset({
    "click", "press", "select", "pick", "jump", "toggle", "do default",
})
_TEXT_ROLE_NAMES = frozenset({
    "entry", "text", "password text", "terminal", "document text", "paragraph",
})
# Roles whose Value interface is a real range. Qt exposes Value on labels,
# checks, rows, and empty text fields too; those numbers are 0.0 or an
# uninitialized double, not a value the widget holds.
_RANGE_ROLE_NAMES = frozenset({"slider", "spin button", "progress bar"})


def _strip_objects(text: str) -> str:
    return text.replace(_OBJECT_REPLACEMENT, "").strip()


def _is_choice_role(role: str, role_name: str) -> bool:
    return role in _CHOICE_ROLES or role_name in _CHOICE_ROLE_NAMES


def _norm_nbsp(text: str | None) -> str | None:
    """U+00A0 is a space. Chrome stores edge spaces in contenteditable as NBSP."""
    if text is None:
        return None
    return text.replace("\u00a0", " ")


def _texts_match(got: str | None, wanted: str | None) -> bool:
    if got is None or wanted is None:
        return False
    return _norm_nbsp(got) == _norm_nbsp(wanted)


def _text_is_blank(text: str | None) -> bool:
    """True when the snapshot read is empty, unreadable, or only whitespace.

    Chrome's empty contenteditable reads back as a newline (the ``<br>``) or a
    single space, not ``""``. That leftover is not the user's text.
    """
    if text is None:
        return True
    return _norm_nbsp(text).strip() == ""


def _direct_text(acc) -> str:
    got = _safe(lambda: _atspi().Text.get_text(acc, 0, -1))
    return got if isinstance(got, str) else ""


def _hypertext_objects(acc) -> list:
    """Accessibles Chromium embeds as U+FFFC, in text order.

    ``Hypertext.get_link`` is that mapping. A node with no hypertext (or a
    fake that does not implement it) returns an empty list so the caller can
    fall back to the children whose text is not already in the parent string.
    """
    try:
        Atspi = _atspi()
    except Exception:
        return []
    hyper = getattr(Atspi, "Hypertext", None)
    link_cls = getattr(Atspi, "Hyperlink", None)
    if hyper is None or link_cls is None:
        return []
    count = _safe(lambda: hyper.get_n_links(acc))
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return []
    objects = []
    for index in range(min(int(count), 64)):
        link = _safe(lambda index=index: hyper.get_link(acc, index))
        obj = None
        if link is not None:
            obj = _safe(lambda link=link: link_cls.get_object(link, 0))
        objects.append(obj)
    return objects


def _embedded_targets(acc, text: str) -> list:
    """The accessibles that stand in for each U+FFFC in ``text``, in order."""
    needed = text.count(_OBJECT_REPLACEMENT)
    objects = _hypertext_objects(acc)
    if objects:
        return objects[:needed]
    plain = text.replace(_OBJECT_REPLACEMENT, "")
    embedded = []
    for index in range(min(_child_count(acc), 64)):
        child = _child_at(acc, index)
        if child is None:
            continue
        visible = _direct_text(child).replace(_OBJECT_REPLACEMENT, "").strip()
        if visible and visible in plain:
            continue
        embedded.append(child)
    return embedded[:needed]


def _object_text(obj, depth: int, surrounding: str) -> str:
    """Text to splice in place of one embedded-object character.

    A choice control is not spliced: its options are U+FFFC too, and the
    selected option is already the control's own value. An empty text field
    contributes its name when that name is not already in the surrounding
    words, so ``<p><input aria-label="Bare para input"></p>`` is not blank.
    """
    if obj is None or depth > 6:
        return ""
    role_name = _role_name(obj)
    role = _ROLE.get(role_name, "AXGroup")
    if _is_choice_role(role, role_name):
        return ""
    raw = _direct_text(obj)
    if _OBJECT_REPLACEMENT in raw:
        expanded = _expand_embedded(obj, raw, depth + 1)
        if expanded.strip():
            return expanded
        raw = raw.replace(_OBJECT_REPLACEMENT, "")
    if raw.strip():
        return raw
    name = _node_name(obj)
    if name and name not in surrounding:
        return name
    return ""


def _div_inline_sentence(acc, role_str: str, attrs: dict) -> str | None:
    """The readable text of a ``<div>`` whose children are inline.

    AT-SPI exposes that div as a section, and a section is a group, so the
    value read is skipped. Chromium's text is the sentence with one U+FFFC
    per link, and the same words are also separate static-text children, so
    find cannot match a phrase that crosses them. Firefox exposes that same
    string and no static-text children, so the valueless group collapses
    onto the link and the words disappear. A div whose text is only an
    embedded control (a button wrapper, ``\\ufffc``) stays valueless and
    still collapses onto that control. A choice control is not spliced in.
    """
    if role_str != "section":
        return None
    if str(attrs.get("tag") or "").lower() != "div":
        return None
    raw = _direct_text(acc)
    if not raw.replace(_OBJECT_REPLACEMENT, "").strip():
        return None
    if _OBJECT_REPLACEMENT in raw:
        expanded = _expand_embedded(acc, raw)
        if expanded.strip():
            return expanded
    return raw.strip() or None


def _expand_embedded(acc, text: str, depth: int = 0) -> str:
    """Replace each U+FFFC with the embedded child's text.

    ``"The \\ufffc fox jumps over the \\ufffc dog."`` with a link "quick brown"
    and an emphasis "lazy" becomes "The quick brown fox jumps over the lazy
    dog." A leftover object character with no child is dropped, which is what
    a label around a select already did.
    """
    if not isinstance(text, str):
        return ""
    if depth > 6 or _OBJECT_REPLACEMENT not in text:
        return text.replace(_OBJECT_REPLACEMENT, "").strip()
    targets = _embedded_targets(acc, text)
    parts = text.split(_OBJECT_REPLACEMENT)
    surrounding = "".join(parts)
    out: list[str] = []
    for index, part in enumerate(parts):
        out.append(part)
        if index >= len(parts) - 1:
            break
        obj = targets[index] if index < len(targets) else None
        out.append(_object_text(obj, depth + 1, surrounding))
    return "".join(out).strip()


def _option_label(node) -> str:
    label = _node_name(node)
    if label:
        return label
    raw = _safe(lambda n=node: _atspi().Text.get_text(n, 0, -1))
    if isinstance(raw, str):
        return _strip_objects(raw)
    return ""


def _iface_selected_labels(acc) -> list[str] | None:
    """Labels from the Selection interface, or None when it names nothing.

    Every selected child is included. Index 0 alone is what a multi-select
    listbox used to show, hiding the rest. A child with no label is skipped
    so the state walk can still see the option.
    """
    iface = _selection_iface(acc)
    if iface is None:
        return None
    labels: list[str] = []
    count = _call_first(iface, ("get_n_selected_children", "getNSelectedChildren"))
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        for index in range(min(int(count), 64)):
            child = _call_first(iface, ("get_selected_child", "getSelectedChild"), index)
            if child is None:
                continue
            label = _option_label(child)
            if label and label not in labels:
                labels.append(label)
        if len(labels) > 1:
            return labels
    child = _call_first(iface, ("get_selected_child", "getSelectedChild"), 0)
    if child is None:
        return labels or None
    label = _option_label(child)
    if label and label not in labels:
        labels.append(label)
    return labels or None


def _state_selected_labels(acc, *, skip_menus: bool) -> list[str]:
    """SELECTED option labels in tree order.

    ``skip_menus`` leaves a popup menu alone. GTK marks the highlighted row
    SELECTED there without changing the combo. A listbox's own items are not
    in a menu, so a multi-select still reports every selected option.
    """
    labels: list[str] = []

    def walk(node, depth: int) -> None:
        if depth > 4:
            return
        count = min(_child_count(node), 64)
        for index in range(count):
            child = _child_at(node, index)
            if child is None:
                continue
            role = _role_name(child)
            if skip_menus and role in {"menu", "popup menu"}:
                continue
            if _state_has(child, "SELECTED"):
                label = _option_label(child)
                if label and label not in labels:
                    labels.append(label)
                continue
            if role in _OPTION_CONTAINER_NAMES:
                walk(child, depth + 1)

    walk(acc, 0)
    return labels


def _selected_option_text(acc) -> str | None:
    """The combo or list's selected option text, every one of them.

    ``Selection.get_selected_child`` is the active item (GTK's combo uses it
    for ``gtk_combo_box_get_active``). A popup menu can mark a row SELECTED
    when that row is only highlighted, which leaves the combo unchanged, so
    that menu is not merged in. A multi-select listbox has several selected
    children; all of their names are shown, in tree order, separated by
    ", ". One selected option is that option's text alone.
    """
    iface_labels = _iface_selected_labels(acc)
    if iface_labels is not None and len(iface_labels) > 1:
        return ", ".join(iface_labels)
    outside_menus = _state_selected_labels(acc, skip_menus=True)
    if outside_menus and (not iface_labels or len(outside_menus) > len(iface_labels)):
        return ", ".join(outside_menus)
    if iface_labels:
        return ", ".join(iface_labels)
    nested = _state_selected_labels(acc, skip_menus=False)
    if not nested:
        return None
    return ", ".join(nested)


def _toolkit_name(acc) -> str:
    """AT-SPI toolkit name for ``acc``'s application, cached per application.

    Qt and GTK share the GI client. The server is what interprets insert
    length and which Value numbers are real, so the split is the toolkit
    name (``Qt`` or ``gtk``), not the binding module.
    """
    app = _call_first(acc, ("get_application", "getApplication")) or acc
    cached = getattr(app, "_a11y_toolkit_name", None)
    if isinstance(cached, str):
        return cached
    name = (_call_first(app, ("get_toolkit_name", "getToolkitName"), default="") or "")
    text = str(name).lower()
    try:
        setattr(app, "_a11y_toolkit_name", text)
    except Exception:
        pass
    return text


def _qt_app(acc) -> bool:
    """True when this node belongs to Qt. GTK and a fake with no toolkit are not."""
    return "qt" in _toolkit_name(acc)


def _sane_range(low: float, high: float) -> bool:
    """True when minimum and maximum are a real widget range.

    An uninitialized Qt double is finite and tiny (about ``1e-310``), and the
    minimum, maximum, and current value are the same garbage. A range needs
    a finite minimum strictly below a finite maximum, and neither end may be
    a subnormal.
    """
    if not math.isfinite(low) or not math.isfinite(high) or not low < high:
        return False
    for number in (low, high):
        if number != 0.0 and abs(number) < 1e-200:
            return False
    return True


def _relation_type_token(value) -> str:
    parts = (
        value,
        getattr(value, "value_name", ""),
        getattr(value, "value_nick", ""),
    )
    return " ".join(str(part) for part in parts).lower().replace("-", "_")


def _relation_set(acc):
    """Relations on ``acc``. Fakes expose ``get_relation_set``. GI uses the class form."""
    if not hasattr(type(acc), "__gtype__"):
        got = _call_first(acc, ("get_relation_set", "getRelationSet"))
        if got:
            return got
    return _safe(lambda: _atspi().Accessible.get_relation_set(acc)) or []


def _gi_object(obj) -> bool:
    return hasattr(type(obj), "__gtype__")


def _relation_kind(rel) -> str:
    rtype = None
    if not _gi_object(rel):
        rtype = _call_first(rel, ("get_relation_type", "getRelationType"))
    if rtype is None:
        rtype = _safe(lambda: _atspi().Relation.get_relation_type(rel))
    if rtype is None:
        return ""
    return _relation_type_token(rtype)


def _relation_targets(acc, kind: str) -> list:
    """Targets of relations whose type token contains ``kind``."""
    wanted = kind.lower().replace("-", "_")
    targets: list = []
    for rel in _relation_set(acc):
        if wanted not in _relation_kind(rel):
            continue
        count = None
        if not _gi_object(rel):
            count = _call_first(rel, ("get_n_targets", "getNTargets"))
        if not isinstance(count, int) or isinstance(count, bool):
            count = _safe(lambda r=rel: _atspi().Relation.get_n_targets(r))
        if not isinstance(count, int) or isinstance(count, bool):
            continue
        for index in range(int(count)):
            target = None
            if not _gi_object(rel):
                target = _call_first(rel, ("get_target", "getTarget"), index)
            if target is None:
                target = _safe(lambda r=rel, i=index: _atspi().Relation.get_target(r, i))
            if target is not None:
                targets.append(target)
    return targets


def _labelled_by_name(acc) -> str:
    """Name of the first LABELLED_BY target, or ``""``."""
    for target in _relation_targets(acc, "labelled_by"):
        label = _node_name(target)
        if label:
            return label
    return ""


def _combo_display_name(acc, name: str) -> str:
    """Title for a combo box.

    On Linux, Qt puts the current item in the accessible name and says the
    label relation is the widget's name. A GTK combo already carries the name
    it was given, so this returns that name unchanged.
    """
    if not _qt_app(acc):
        return name
    label = _labelled_by_name(acc)
    if label and label != name:
        return label
    return name


def _value_text(acc, role: str, role_name: str | None = None) -> object | None:
    """The node's current value: text contents for text roles, numeric value
    for sliders/progress. Secure fields never have their value read here (the
    engine also blanks AXSecureTextField values).

    Text is read via the explicit interface class form ``Atspi.Text.get_text(acc,
    0, -1)``. The instance form (``acc.get_text_iface().get_text(a, b)``) resolves
    to ``Atspi.Accessible.get_text`` (a 1-arg method) on this binding and raises
    TypeError — which, swallowed defensively, silently blanked every field value.
    Calling the interface method with the accessible as the first argument avoids
    the name collision. Non-Text accessibles make the call raise → None.

    Chromium's select and listbox text is U+FFFC once per option. The value is
    the selected option's name instead. Anywhere else, each U+FFFC is the
    embedded child's text (a link, an emphasis, a control), not a character to
    delete: a paragraph then reads as the sentence, and a paragraph whose only
    content is a control is not an empty text leaf. An empty number field
    exposes Value 0.0 with no text; that default is not shown. A slider has no
    text interface and still reports its Value.

    Qt publishes a Value interface on labels, checks, rows, and empty text
    fields. The number is 0.0 or an uninitialized double (about ``1e-310``).
    Text, or the accessible name, is what those roles show. Value is read for
    a slider, spin button, or progress bar, and only when Qt's minimum and
    maximum are a real range. GTK's fallback for a non-range control is
    unchanged.
    """
    if role == "AXSecureTextField":
        return None
    Atspi = _atspi()
    if role_name is None:
        role_name = _role_name(acc)
    count = _safe(lambda: Atspi.Text.get_character_count(acc))
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        got = _safe(lambda: Atspi.Text.get_text(acc, 0, -1))
        if isinstance(got, str) and got:
            if _OBJECT_REPLACEMENT in got:
                if _is_choice_role(role, role_name or ""):
                    cleaned = _strip_objects(got)
                    if cleaned:
                        return cleaned
                else:
                    expanded = _expand_embedded(acc, got)
                    if expanded:
                        return expanded
            else:
                return got
        handled, choice = _choice_value(acc, role, role_name)
        if handled:
            return choice
        if role_name == "spin button" or (role_name in {"entry", "text"} and _number_input(acc)):
            return None
    elif count == 0 and (
        role_name == "spin button" or (role_name in {"entry", "text"} and _number_input(acc))
    ):
        # Text is present and empty. Value 0.0 is the number field's default,
        # not a number the user entered. A slider has no text interface
        # (count is None) and still falls through to Value.
        return None
    handled, choice = _choice_value(acc, role, role_name)
    if handled:
        return choice
    if role_name == "table cell":
        # An empty Calc cell's Value interface is a full double range whose
        # current value is 0.0. That is not the cell's text. A formula is
        # shown only when the text interface is empty; LibreOffice stores
        # the formula without a leading "=".
        formula = _sheet_formula_text(acc)
        if formula:
            return formula
        if _SHEET_ADDRESS.match(_node_name(acc)):
            return None
    if role_name in _RANGE_ROLE_NAMES:
        return _range_value(acc)
    # A Qt label, check, row, or empty line edit has a Value interface whose
    # current value is not a number the widget holds. The name is the title.
    if _qt_app(acc):
        return None
    cur = _safe(lambda: Atspi.Value.get_current_value(acc))
    if cur is not None:
        return cur
    return None


def _range_value(acc):
    """Current Value for a slider, spin button, or progress bar.

    Qt's minimum and maximum have to be a real range. A denormal or a
    minimum that is not below the maximum is not shown. Other toolkits keep
    the current value whenever the interface returns a finite number.
    """
    if _qt_app(acc):
        span = _value_range(acc)
        if span is None or not _sane_range(*span):
            return None
    cur = _safe(lambda: _atspi().Value.get_current_value(acc))
    if isinstance(cur, bool) or not isinstance(cur, (int, float)):
        return None
    number = float(cur)
    if not math.isfinite(number):
        return None
    if _qt_app(acc) and number != 0.0 and abs(number) < 1e-200:
        return None
    return cur


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
    # aria-pressed is AT-SPI PRESSED, not CHECKED. A GTK toggle already uses
    # CHECKED. A pressed Chrome button is shown the same way.
    if checked is None and has("PRESSED"):
        checked = True
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
        self._gecko: bool | None = None

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
        if role_str == "combo box":
            name = _combo_display_name(node, str(name))
        if _OBJECT_REPLACEMENT in str(name):
            name = _strip_objects(str(name))
        if checked is None and role == "AXButton":
            flag = str(attrs.get("aria-pressed") or attrs.get("pressed") or "").strip().lower()
            if flag in {"true", "false"}:
                checked = flag == "true"
        # Groups skip the value probe. A div is a section, and its text is
        # the inline sentence; reading that one string is what find matches.
        value = None if role in _NO_VALUE_ROLES else _value_text(node, role, role_str)
        if value is None:
            sentence = _div_inline_sentence(node, role_str, attrs)
            if sentence:
                value = sentence
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
            value=value,
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
        # -1 is a wedged D-Bus read. Revive once, then treat a still-negative
        # count as empty so range() does not walk nothing and hide the dialog.
        count = _child_count(node)
        if count < 0:
            count = 0
        # Calc reports 1048576×16384 and get_child_at_index is row-major, so
        # the first fetch is all of row 1. A modest child count is the
        # visible cells (LibreOffice 25.2 exposes about 65) and is walked
        # as usual. The observe cap for address-titled cells is 96.
        if count > _MAX_CHILDREN_FETCH and _spreadsheet_table(node):
            return _sheet_window_cells(node)
        count = min(count, _MAX_CHILDREN_FETCH)
        kids = []
        for i in range(count):
            child = _call_first(node, ("get_child_at_index", "getChildAtIndex"), i)
            if child is not None:
                kids.append(child)
        if self._tree_is_gecko(node):
            # Firefox keeps background tabs and the preloaded New Tab page in
            # the tree with on-screen bounds. They are not SHOWING. Drop the
            # document here so snapshot and find never offer it.
            kids = [child for child in kids if not _hidden_gecko_browser(child)]
        return _with_table_body(node, kids)

    def _tree_is_gecko(self, node: object) -> bool:
        if self._gecko is None:
            self._gecko = _gecko_app(node)
        return self._gecko


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


_DOC_URL_KEYS = ("docurl", "uri", "url")
_LINK_URL_KEYS = ("href", "link-target")


def _hyperlink_target(acc) -> str | None:
    """The Hyperlink URI of this node, not a document URL inherited from a parent."""
    from a11y_computer_use.untrusted import looks_like_url

    link = _call_first(acc, ("get_hyperlink",))
    if link is not None:
        uri = _call_first(link, ("get_uri",), 0)
        if isinstance(uri, str) and looks_like_url(uri.strip()):
            return uri.strip()
    attrs = _get_attributes(acc)
    for key, val in attrs.items():
        if str(key).lower() in _LINK_URL_KEYS and isinstance(val, str) and looks_like_url(val.strip()):
            return val.strip()
    return None


def hyperlink_uri(acc) -> str | None:
    """URI of a link node, or of an ancestor whose role is link.

    Chromium's document exposes Hyperlink as the page URL. That is the
    document URL, not a navigation target, so the walk stops at a document.
    """
    node = acc
    for _ in range(6):
        if node is None:
            return None
        role = _role_name(node)
        if "document" in role:
            return None
        if role == "link":
            return _hyperlink_target(node)
        node = _call_first(node, ("get_parent",))
    return None


def _doc_url(acc) -> str | None:
    """DocURL for a document accessible, else None."""
    from a11y_computer_use.untrusted import looks_like_url

    role = _role_name(acc)
    if "document" not in role:
        return None
    # Chromium exposes the page URL as the document's hyperlink. That is not
    # a link the agent clicked; document_url is the only reader that wants it.
    link = _call_first(acc, ("get_hyperlink",))
    if link is not None:
        uri = _call_first(link, ("get_uri",), 0)
        if isinstance(uri, str) and looks_like_url(uri.strip()):
            return uri.strip()
    for key in ("DocURL", "URI", "Url"):
        val = _call_first(acc, ("get_document_attribute_value",), key)
        if isinstance(val, str) and looks_like_url(val.strip()):
            return val.strip()
    attrs = _get_attributes(acc)
    for key, val in attrs.items():
        if str(key).lower() in _DOC_URL_KEYS and isinstance(val, str) and looks_like_url(val.strip()):
            return val.strip()
    description = _call_first(acc, ("get_description",), default="")
    if isinstance(description, str) and looks_like_url(description.strip()):
        return description.strip()
    return None


def _collected_content_document(root) -> str | None:
    """On-screen content URL from Collection, or None when it has no documents.

    Firefox's showing document is reachable from the focused node and from
    Collection, and a child walk from the window often never meets it. The
    same filter as the walk applies: a hidden tab is not the page, and an
    iframe does not replace the top document.
    """
    if root is None:
        return None
    from a11y_computer_use.untrusted import is_browser_chrome_url

    try:
        Atspi = _atspi()
    except Exception:
        return None
    coll = _call_first(root, ("get_collection_iface", "get_collection"))
    if coll is None:
        return None
    role_enum = getattr(Atspi, "Role", None)
    roles = []
    for name in ("DOCUMENT_WEB", "DOCUMENT_FRAME"):
        role = getattr(role_enum, name, None)
        if role is not None:
            roles.append(role)
    if not roles:
        return None
    try:
        states = Atspi.StateSet.new([])
        mt = Atspi.CollectionMatchType
        rule = Atspi.MatchRule.new(
            states, mt.NONE, {}, mt.NONE, roles, mt.ANY, [], mt.NONE, False,
        )
        hits = coll.get_matches(rule, Atspi.CollectionSortOrder.CANONICAL, 12, True)
    except Exception:  # noqa: BLE001 - GTK and fakes expose no Collection
        return None
    held: list[tuple[object, str]] = []
    for hit in hits or []:
        url = _doc_url(hit)
        if url and not is_browser_chrome_url(url):
            held.append((hit, url))
    return _pick_content_document(held)


def document_url_of(root) -> str | None:
    """Content-document URL under ``root``.

    Firefox keeps every tab's document in the tree. The URL is the selected
    tab that is SHOWING, not the first document a walk meets. Collection is
    asked first, because that showing document is often missing from the
    window's children. A Chromium omnibox popup is browser chrome and is not
    a page URL. An iframe's document is nested; the page URL is the document
    that is not inside another one. The walk is bounded so one policy check
    cannot become a full tree walk.
    """
    if root is None:
        return None
    collected = _collected_content_document(root)
    if collected:
        return collected
    from a11y_computer_use.untrusted import is_browser_chrome_url

    held: list[tuple[object, str]] = []
    queue = [root]
    seen = 0
    visited: set[int] = set()
    while queue and seen < 400:
        node = queue.pop(0)
        ident = id(node)
        if ident in visited:
            continue
        visited.add(ident)
        seen += 1
        url = _doc_url(node)
        if url and not is_browser_chrome_url(url):
            if not _gecko_app(node) and not _nested_in_document(node):
                return url
            if (
                _gecko_app(node)
                and _gecko_web_document_on_screen(node)
                and not _nested_in_document(node)
            ):
                return url
            held.append((node, url))
        elif url and is_browser_chrome_url(url):
            # The popup's children are browser UI. Do not spend the budget
            # walking them when the page document is a sibling window.
            continue
        count = _call_first(node, ("get_child_count",), default=0) or 0
        for index in range(min(int(count), 80)):
            child = _call_first(node, ("get_child_at_index",), index)
            if child is not None:
                queue.append(child)
    return _pick_content_document(held)


def _nested_in_document(node) -> bool:
    """True when ``node`` sits inside another content document (an iframe)."""
    from a11y_computer_use.untrusted import is_browser_chrome_url

    parent = _parent_of(node)
    seen: set[int] = set()
    for _ in range(16):
        if parent is None or id(parent) in seen:
            return False
        seen.add(id(parent))
        url = _doc_url(parent)
        if url and not is_browser_chrome_url(url):
            return True
        if _role_name(parent) in {"frame", "window", "application"}:
            return False
        parent = _parent_of(parent)
    return False


def _pick_content_document(candidates: list[tuple[object, str]]) -> str | None:
    """The on-screen top document, or None when every Firefox document is hidden."""
    if not candidates:
        return None
    visible: list[tuple[object, str]] = []
    for node, url in candidates:
        if _gecko_app(node) and not _gecko_web_document_on_screen(node):
            continue
        visible.append((node, url))
    if not visible:
        if any(_gecko_app(node) for node, _url in candidates):
            return None
        visible = list(candidates)
    top = [(node, url) for node, url in visible if not _nested_in_document(node)]
    pool = top or visible
    for node, url in pool:
        if _state_has(node, "SHOWING"):
            return url
    return pool[0][1]


def other_frame_document_url(app_root, skip) -> str | None:
    """Page URL of a top-level frame other than ``skip``.

    The active frame can be the omnibox popup. The page lives in another
    frame of the same application. Each frame is walked on its own budget.
    """
    if app_root is None:
        return None
    count = _call_first(app_root, ("get_child_count",), default=0) or 0
    for index in range(min(int(count), 8)):
        frame = _call_first(app_root, ("get_child_at_index",), index)
        if frame is None or frame is skip:
            continue
        url = document_url_of(frame)
        if url:
            return url
    return None


def document_url_for(acc) -> str | None:
    """URL of the content document that owns ``acc``.

    An iframe is its own document, so a control inside the frame reports the
    frame origin. The toolbar, the tab strip, and the omnibox are not inside
    a content document.
    """
    from a11y_computer_use.untrusted import is_browser_chrome_url

    node = acc
    seen: set[int] = set()
    for _ in range(32):
        if node is None or id(node) in seen:
            return None
        seen.add(id(node))
        url = _doc_url(node)
        if url and not is_browser_chrome_url(url):
            return url
        if _role_name(node) == "application":
            return None
        parent = _parent_of(node)
        if parent is None or parent is node:
            return None
        node = parent
    return None


def in_browser_chrome(acc) -> bool:
    """True when ``acc`` is Chromium or Firefox UI, not page content.

    GTK and Qt are not browser chrome. A node inside a page or iframe
    document is page content even when its text looks like a URL.
    """
    from a11y_computer_use.untrusted import is_browser_chrome_url

    if acc is None or not (_chromium_app(acc) or _gecko_app(acc)):
        return False
    node = acc
    seen: set[int] = set()
    for _ in range(32):
        if node is None or id(node) in seen:
            return True
        seen.add(id(node))
        url = _doc_url(node)
        if url and is_browser_chrome_url(url):
            return True
        if url:
            return False
        if _role_name(node) == "application":
            return True
        parent = _parent_of(node)
        if parent is None or parent is node:
            return True
        node = parent
    return True


_LOCATION_NAME_BITS = (
    "address and search",
    "enter address",
    "search or enter",
    "search with google",
    "location bar",
    "url bar",
)


def _node_id(acc) -> str:
    attrs = _get_attributes(acc)
    for key, val in attrs.items():
        if str(key).lower() in {"id", "html-id", "id-attribute"} and isinstance(val, str):
            return val.strip().lower()
    return ""


def _node_plain_text(acc) -> str:
    """Text of ``acc`` for the address bar. A fake may set ``text`` or ``get_text``."""
    direct = getattr(acc, "text", None)
    if isinstance(direct, str) and direct:
        return direct
    getter = getattr(acc, "get_text", None)
    if callable(getter):
        got = None
        try:
            got = getter(0, -1)
        except TypeError:
            got = _safe(getter, None)
        if isinstance(got, str) and got:
            return got
    try:
        raw = _full_text(acc)
    except Exception:
        raw = None
    return raw if isinstance(raw, str) else ""


def is_location_entry(acc) -> bool:
    """True for the address bar of Chromium or Firefox, not a page text field.

    The node is outside any content document. Its name or id is the location
    bar, or it is an entry whose text is a URL. A GTK entry is neither.
    """
    from a11y_computer_use.untrusted import looks_like_url

    if acc is None or not in_browser_chrome(acc):
        return False
    ident = _node_id(acc)
    if "urlbar" in ident:
        return True
    name = _node_name(acc).lower()
    if any(bit in name for bit in _LOCATION_NAME_BITS):
        return True
    role = _role_name(acc)
    if role in {"entry", "text", "combo box", "editable text", "combo-box"}:
        if looks_like_url(_node_plain_text(acc).strip()) or looks_like_url(_node_name(acc).strip()):
            return True
    return False


def address_bar_text_under(root) -> str | None:
    """Text of the location entry under ``root``, preferring the focused one."""
    from a11y_computer_use.untrusted import is_browser_chrome_url, looks_like_url

    if root is None:
        return None
    found = []
    queue = [root]
    seen = 0
    visited: set[int] = set()
    while queue and seen < 500 and len(found) < 4:
        node = queue.pop(0)
        if node is None or id(node) in visited:
            continue
        visited.add(id(node))
        seen += 1
        if is_location_entry(node):
            found.append(node)
        url = _doc_url(node)
        if url and not is_browser_chrome_url(url):
            continue
        count = _call_first(node, ("get_child_count",), default=0) or 0
        for index in range(min(int(count), 40)):
            child = _call_first(node, ("get_child_at_index",), index)
            if child is not None:
                queue.append(child)
    if not found:
        return None
    ordered = sorted(found, key=lambda node: not _state_has(node, "FOCUSED"))
    for node in ordered:
        text = _node_plain_text(node).strip()
        if looks_like_url(text):
            return text
        name = _node_name(node).strip()
        if looks_like_url(name):
            return name
        if text:
            return text
    return None


def location_text(node) -> str | None:
    """Visible text of a location entry. A URL wins over the accessible name."""
    from a11y_computer_use.untrusted import looks_like_url

    if node is None:
        return None
    text = _node_plain_text(node).strip()
    name = _node_name(node).strip()
    if looks_like_url(text):
        return text
    if looks_like_url(name):
        return name
    return text or None


def _location_entries(root) -> list:
    """Location entries Collection can see under ``root``.

    Chromium's address bar is the focused entry, and Collection lists it.
    A child walk from the application often never meets that entry. A fake
    has no Collection interface, so the caller also walks.
    """
    if root is None:
        return []
    try:
        Atspi = _atspi()
    except Exception:
        return []
    coll = _call_first(root, ("get_collection_iface", "get_collection"))
    if coll is None:
        return []
    role = getattr(getattr(Atspi, "Role", None), "ENTRY", None)
    if role is None:
        return []
    try:
        states = Atspi.StateSet.new([])
        mt = Atspi.CollectionMatchType
        rule = Atspi.MatchRule.new(
            states, mt.NONE, {}, mt.NONE, [role], mt.ANY, [], mt.NONE, False,
        )
        hits = coll.get_matches(rule, Atspi.CollectionSortOrder.CANONICAL, 12, True)
    except Exception:  # noqa: BLE001 - GTK and fakes expose no Collection
        return []
    return [hit for hit in (hits or []) if is_location_entry(hit)]


def address_bar_text(app: str) -> str | None:
    """The address bar under ``app``, including when the omnibox popup is focused.

    The popup window does not contain the entry that holds the typed URL.
    The focused location entry is read first. Collection is next, because
    Chromium does not put that entry where a child walk can see it. The
    walk remains for a toolkit that has no Collection interface.
    """
    from a11y_computer_use.schema import Scope

    if not app:
        return None
    try:
        acc, truncated = _focused_node(app)
    except Exception:  # noqa: BLE001 - an unreadable focus is not the bar
        acc, truncated = None, False
    if acc is not None and not truncated and is_location_entry(acc):
        text = location_text(acc)
        if text:
            return text
    root = find_root(app, Scope.APP)
    collected = _collected_address_bar(app)
    if collected:
        return collected
    return address_bar_text_under(root)


def _collected_address_bar(app: str) -> str | None:
    """The address bar as Collection reports it, ignoring keyboard focus.

    The focused wrapper can still show the pre-type URL after the keys
    landed in a newer accessible for the same entry.
    """
    from a11y_computer_use.schema import Scope

    if not app:
        return None
    return _address_bar_from_entries(_location_entries_under(find_root(app, Scope.APP)))


def _location_entries_under(root) -> list:
    """Location entries on ``root`` and on each of its top-level frames."""
    entries = _location_entries(root)
    if entries or root is None:
        return entries
    count = _call_first(root, ("get_child_count",), default=0) or 0
    for index in range(min(int(count), 6)):
        frame = _call_first(root, ("get_child_at_index",), index)
        entries.extend(_location_entries(frame))
    return entries


def _address_bar_from_entries(entries: list) -> str | None:
    from a11y_computer_use.untrusted import looks_like_url

    ordered = sorted(entries, key=lambda node: not _state_has(node, "FOCUSED"))
    for node in ordered:
        text = location_text(node)
        if text and looks_like_url(text):
            return text
    for node in ordered:
        text = location_text(node)
        if text:
            return text
    return None


def location_shows_typed(before: str | None, after: str | None, typed: str) -> bool:
    """Whether ``typed`` landed in a location entry.

    The omnibox often drops ``http://`` once the host matches the current
    site. A page field does not use this. An entry that did not change is
    not a success.
    """
    if _typed_visible(before, after, typed):
        return True
    if not after or not typed:
        return False
    bare = typed
    lowered = typed.lower()
    for prefix in ("https://", "http://"):
        if lowered.startswith(prefix):
            bare = typed[len(prefix):]
            break
    if bare == typed or not bare:
        return False
    if _typed_visible(before, after, bare):
        return True
    before_n = _norm_nbsp(before) or ""
    after_n = _norm_nbsp(after) or ""
    return bare in after_n and after_n != before_n


def _focused_accessibles(root, limit: int = 6) -> list:
    """Focused nodes Collection can see under ``root``.

    Chromium can mark the address bar and the page focused at the same time
    after ctrl+l. A query for one match may return the page. Callers that
    care about the address bar have to see the whole set.
    """
    if root is None:
        return []
    try:
        Atspi = _atspi()
    except Exception:
        return []
    st = getattr(Atspi, "StateType", None)
    focused_state = getattr(st, "FOCUSED", None)
    if focused_state is None:
        return []
    coll = _call_first(root, ("get_collection_iface", "get_collection"))
    if coll is None:
        return []
    try:
        states = Atspi.StateSet.new([focused_state])
        mt = Atspi.CollectionMatchType
        rule = Atspi.MatchRule.new(
            states, mt.ALL, {}, mt.NONE, [], mt.NONE, [], mt.NONE, False,
        )
        hits = coll.get_matches(rule, Atspi.CollectionSortOrder.CANONICAL, limit, True)
    except Exception:  # noqa: BLE001 - GTK and fakes expose no Collection
        return []
    return list(hits or [])


def focused_location_entry(app: str):
    """The focused address bar, even when the page is focused too."""
    if not app:
        return None
    from a11y_computer_use.schema import Scope

    try:
        root = find_root(app, Scope.APP)
    except Exception:  # noqa: BLE001 - no desktop is not an address bar
        root = None
    for hit in _focused_accessibles(root):
        try:
            if is_location_entry(hit):
                return hit
        except Exception:  # noqa: BLE001 - one bad node is not the bar
            continue
    try:
        acc, truncated = _focused_node(app)
    except Exception:  # noqa: BLE001 - unknown focus is not the address bar
        return None
    if acc is None or truncated:
        return None
    if is_location_entry(acc):
        return acc
    return None


def focus_in_browser_chrome(app: str) -> bool:
    """True when keyboard focus is in browser UI rather than a page document."""
    if not app:
        return False
    try:
        if focused_location_entry(app) is not None:
            return True
    except Exception:  # noqa: BLE001 - fall through to the single focused node
        pass
    try:
        acc, truncated = _focused_node(app)
    except Exception:
        acc, truncated = None, False
    if acc is not None and not truncated:
        return in_browser_chrome(acc)
    from a11y_computer_use.schema import Scope

    root = find_root(app, Scope.WINDOW)
    if root is None:
        return False
    return _window_is_only_browser_chrome(root)


def _window_is_only_browser_chrome(root) -> bool:
    """True when ``root`` has a browser-chrome document and no page document."""
    from a11y_computer_use.untrusted import is_browser_chrome_url

    chrome = False
    queue = [root]
    seen = 0
    visited: set[int] = set()
    while queue and seen < 80:
        node = queue.pop(0)
        if node is None or id(node) in visited:
            continue
        visited.add(id(node))
        seen += 1
        url = _doc_url(node)
        if url and is_browser_chrome_url(url):
            chrome = True
            continue
        if url:
            return False
        count = _call_first(node, ("get_child_count",), default=0) or 0
        for index in range(min(int(count), 20)):
            child = _call_first(node, ("get_child_at_index",), index)
            if child is not None:
                queue.append(child)
    return chrome


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
        n = _child_count(acc)
        if n < 0:
            n = 0
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
        # A dialog opened with Gtk.Dialog.run() wedges this process's
        # connection to the app: get_name comes back empty and child count
        # is -1 until the bus is replaced. Revive before treating the
        # registrant as unnamed, then read the name on the fresh connection.
        if not name:
            _child_count(candidate)
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


def _focused_contenteditable(app: str, *, max_nodes: int = 400):
    """The focused node when it is a Chrome contenteditable, else None.

    The XTEST type path uses this to decide whether a late AT-SPI read
    should be polled. An input, a textarea, and a GTK field are None.
    """
    try:
        acc, truncated = _focused_node(app, max_nodes=max_nodes)
    except Exception:
        return None
    if truncated or acc is None or not _chromium_contenteditable(acc):
        return None
    return acc


def focused_text(app: str, *, max_nodes: int = 400) -> str | None:
    """Text of the focused node, or None when that text cannot be read.

    A terminal and a canvas that expose Text are readable even when they
    have no EditableText. None means a keystroke type has nothing to compare
    against and the caller may report the count it sent. An unreachable bus
    is None, not an error.
    """
    try:
        acc, truncated = _focused_node(app, max_nodes=max_nodes)
    except Exception:
        return None
    if truncated or acc is None:
        return None
    try:
        return _readable_text(acc)
    except Exception:
        return None


def _readable_text(acc) -> str | None:
    """Text a type read-back compares.

    NBSP is a space. Each U+FFFC is the embedded child's text, so a
    contenteditable whose paragraphs are object characters still contains the
    words that were typed into them. A choice control is left as the snapshot
    read it: expanding it would list every option.
    """
    raw = _full_text(acc)
    if raw is None:
        return None
    if _OBJECT_REPLACEMENT in raw:
        role_name = _role_name(acc)
        role = _ROLE.get(role_name, "AXGroup")
        if not _is_choice_role(role, role_name):
            expanded = _expand_embedded(acc, raw)
            if expanded:
                raw = expanded
    return raw.replace("\u00a0", " ")


def _field_text_for_type(text: str | None, typed: str) -> str | None:
    """The field text a type read-back compares.

    NBSP is a space: Chrome stores the edge spaces of a contenteditable as
    U+00A0. One trailing newline is the empty paragraph's ``<br>`` and is
    not part of the value, unless the typed text itself ends in a newline.
    Interior spaces stay, so a missing space is still a mismatch.
    """
    if text is None:
        return None
    normalized = text.replace("\u00a0", " ")
    if normalized.endswith("\n") and not (typed or "").replace("\u00a0", " ").endswith("\n"):
        normalized = normalized[:-1]
    return normalized


def _type_needles(text: str, *, chrome: bool) -> tuple[str, ...]:
    """Strings that count as ``text`` having landed.

    NBSP is already a space in the caller. Chrome's contenteditable drops a
    trailing space (the edge one is not kept once the read settles). The
    needle without those trailing spaces counts. An interior space is part
    of the needle, so ``a  b`` does not match ``a b``. A string of only
    spaces is not trimmed down to empty.
    """
    needle = _norm_nbsp(text) or ""
    if not chrome or not needle:
        return (needle,)
    trimmed = needle.rstrip(" ")
    if not trimmed or trimmed == needle:
        return (needle,)
    return (needle, trimmed)


def _typed_visible(before: str | None, after: str | None, text: str, *, chrome: bool = False) -> bool:
    """Whether ``text`` showed up in the focused text.

    A readable field that still shows the pre-type text, or that shows the
    case-inverted string, does not count. A suffix or an insertion does.
    NBSP compares as a space, and one trailing contenteditable newline is
    not part of the value. ``chrome`` also accepts the typed text with its
    trailing spaces removed, which is the string Chrome keeps. A field that
    settled without the characters is not a match.
    """
    before_n = _field_text_for_type(before, text)
    after_n = _field_text_for_type(after, text)
    needles = _type_needles(text, chrome=chrome)
    if after_n is None or not any(needle and needle in after_n for needle in needles):
        return False
    if before_n is None:
        return True
    if after_n == before_n:
        return False
    for needle in needles:
        if needle and (after_n.endswith(needle) or after_n == (before_n + needle)):
            return True
    return before_n in after_n or len(after_n) > len(before_n)


# Chrome publishes a contenteditable's new characters a beat after the keys.
# Sixteen reads, 50ms apart, cover that gap. A value that changed and then
# stayed wrong is settled: more waiting would not turn it into the request.
_TYPE_SETTLE_POLLS = 16
_TYPE_SETTLE_PAUSE_S = 0.05
_TYPE_SETTLE_STABLE = 2


def _poll_typed_text(read, before: str | None, text: str, *, chrome: bool = False) -> str | None:
    """Poll ``read`` until ``text`` is visible or the field settles.

    ``read`` returns the current readable text. The first hit wins. A read
    that is still the pre-type text is not settled: the update can still be
    in flight. A read that changed to something else and stays there is
    settled, and the caller reports ``text_mismatch`` when the characters
    are absent. ``chrome`` uses Chrome's trailing-space comparison. The
    last read is what the caller shows.
    """
    last: str | None = None
    stable = 0
    seen: str | None = None
    before_n = _field_text_for_type(before, text)
    for attempt in range(_TYPE_SETTLE_POLLS):
        seen = read()
        if _typed_visible(before, seen, text, chrome=chrome):
            return seen
        norm = _field_text_for_type(seen, text)
        if norm is not None and norm == last and norm != before_n:
            stable += 1
            if stable >= _TYPE_SETTLE_STABLE:
                return seen
        else:
            stable = 0
        last = norm
        if attempt + 1 < _TYPE_SETTLE_POLLS:
            time.sleep(_TYPE_SETTLE_PAUSE_S)
    return seen


def focused_editable(app: str, *, max_nodes: int = 400):
    """The focused node when `type` can insert into it, else None.

    A password field is returned so the caller can refuse it before any
    write. None means there is no focused editable: nothing is focused, the
    focused node has no EditableText, the walk could not find focus, or
    PyGObject is not installed. The caller then uses keystrokes. The same
    node `insert_text` accepts, so a coordinate click and a ref click share
    that helper.
    """
    try:
        acc, truncated = _focused_node(app, max_nodes=max_nodes)
    except ImportError:
        return None
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


def _insert_length(method, text: str, acc=None) -> int:
    """The ``length`` argument EditableText.insert_text actually wants.

    GTK through libatspi and ``gi.repository.Atspi`` takes a UTF-8 byte count.
    Passing the character count keeps only that many bytes, so ``Привет``
    becomes ``При`` and a cut code point inserts nothing. Qt's adaptor does
    ``QString::resize(length)`` and inserts that string, so the same byte
    count reads past the text and appends uninitialized characters
    (``ünï`` becomes ``ünï`` plus a NUL and whatever followed it). Qt gets
    the character count. The position is a character offset on both.

    A Python double that slices characters has no ``gi.`` module and gets
    ``len(text)`` unless it sets ``_length_unit`` to ``bytes`` or ``chars``.
    ``_length_unit`` wins over the toolkit, so a test can force either one.
    """
    unit = getattr(method, "_length_unit", None)
    if unit == "bytes":
        return len(text.encode("utf-8"))
    if unit == "chars":
        return len(text)
    if acc is not None and _qt_app(acc):
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

    The insert length is the UTF-8 byte count on the GTK GI/C binding and the
    character count for Qt and for a binding that slices characters. The
    position is a character offset. A Qt read-back also requires the
    character count to equal that string: a D-Bus string stops at an
    embedded NUL, so the text alone can look right while the widget holds
    extra characters.
    """
    eti = _editable_iface(acc)
    if eti is None:
        return None
    typed = text.replace("\r\n", "\n")
    current = _full_text(acc)
    # Read the words, not the U+FFFC placeholders, before the insert. A
    # contenteditable's parent text can stay ``\ufffc`` while the child gains
    # the characters. NBSP in that read is a space.
    before_readable = _readable_text(acc) if current is not None else None
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
    length = _insert_length(insert, typed, acc) if insert is not None else len(typed)
    wrote = bool(_call_first(eti, ("insert_text", "insertText"), int(offset), typed, int(length), default=False))
    if _confirm_text(acc, expected):
        return len(typed)
    # The raw string can still be U+FFFC, or spaces can come back as NBSP.
    # Compare the expanded read so a successful insert is not a mismatch.
    # Qt still has to report the character count of the string that was
    # asked for: GetText stops at a NUL, so the readable text can match
    # while the widget is longer.
    after_readable = _readable_text(acc)
    if (
        not _typed_visible(before_readable, after_readable, typed)
        and _chromium_contenteditable(acc)
    ):
        # The first AT-SPI read can still be the pre-type text. Wait until
        # the field settles. A settled read that lacks the characters is
        # still a mismatch, and a Firefox field is not delayed here.
        after_readable = _poll_typed_text(
            lambda: _readable_text(acc), before_readable, typed, chrome=True
        )
    if _typed_visible(
        before_readable, after_readable, typed, chrome=_chromium_contenteditable(acc)
    ) and _qt_text_count_matches(acc, expected):
        return len(typed)
    actual = _full_text(acc)
    if actual == current and not wrote:
        return None
    # ``unchanged`` is the words, not the raw object-replacement string.
    # A Firefox contenteditable can keep ``\ufffc\ufffc`` while a child
    # paragraph gains the typed text. That is a change. A parent that still
    # reads the same sentence did not change, and the caller may send keys.
    # A field that changed to something else is still a mismatch, with no
    # key fallback.
    if before_readable is None:
        unchanged = actual == current
        shown = actual
    else:
        unchanged = _norm_nbsp(after_readable) == _norm_nbsp(before_readable)
        shown = after_readable if after_readable is not None else actual
    raise _text_mismatch(
        "text_mismatch",
        "the field text after type does not match what was inserted",
        expected=_excerpt(expected),
        actual=_excerpt(shown),
        inserted_chars=len(typed),
        unchanged=unchanged,
        before=_excerpt(before_readable if before_readable is not None else current),
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
    """True when the snapshot read is blank or could not be read.

    Chromium returns NULL from ``get_text(0, -1)`` on an empty field, because
    the start offset is past the end. An empty contenteditable reads back as
    a newline or a space (the ``<br>``), which is the same clear. An
    unreadable field is treated as clear here so the replacement can be
    written; ``set_text`` still returns True only when a later snapshot read
    equals the new string.
    """
    return _text_is_blank(_full_text(acc))


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


def _shown_matches(acc, text: str) -> bool:
    """True when the readable text is ``text``.

    NBSP is a space. One trailing newline is the empty contenteditable's
    ``<br>`` and is not part of the value. U+FFFC is expanded before the
    comparison, so a paragraph child counts.
    """
    raw = _full_text(acc)
    if _texts_match(raw, text):
        return True
    shown = _readable_text(acc)
    if shown is None:
        return False
    if _texts_match(shown, text):
        return True
    if shown.endswith("\n") and _texts_match(shown[:-1], text):
        return True
    return False


def _confirm_text_landed(acc, text: str) -> bool:
    """Like ``_confirm_text``, with a longer wait for a key event to land.

    The comparison is the readable text: NBSP is a space, and U+FFFC is the
    child text. A Firefox contenteditable updates a beat after the key.
    """
    for attempt in range(16):
        if _shown_matches(acc, text):
            return True
        if attempt + 1 < 16:
            time.sleep(0.05)
    return False


def chromium_contenteditable_type(acc, text: str) -> bool | None:
    """Type ``text`` into a Chrome contenteditable and wait for the read.

    ``None`` means this is not the path: ``acc`` is not a Chrome
    contenteditable, or XTEST cannot reach the session. ``True`` means a
    settled read contains ``text``. ``False`` means the read settled without
    those characters. That is a mismatch, not a success. NBSP is a space
    and one trailing newline is the ``<br>``. An input, a textarea, and a
    GTK field are not this path.
    """
    if not _chromium_contenteditable(acc) or not _x11_keys_available():
        return None
    grab_focus(acc)
    before = _readable_text(acc)
    _type_string(text)
    after = _poll_typed_text(lambda: _readable_text(acc), before, text, chrome=True)
    return bool(_typed_visible(before, after, text, chrome=True))


def focus_and_type_into(acc, text: str) -> bool:
    """Focus ``acc`` and insert ``text`` with key events.

    True only when a later read of this field contains ``text``. A Firefox
    web entry's EditableText insert returns true and leaves the field empty;
    the same key events ``key`` already delivers do land once the entry has
    focus. The read-back is the expanded text, so a NBSP is a space and a
    contenteditable whose parent string stays U+FFFC still shows the words
    in its children. The read is polled: Firefox applies the keys a beat
    after they are sent. No EditableText is not this path: the caller types
    into whatever is focused and uses that read-back.
    """
    if not _x11_keys_available():
        return False
    grab_focus(acc)
    before = _readable_text(acc)
    if before is None:
        return False
    _type_string(text)
    for attempt in range(16):
        after = _readable_text(acc)
        if _typed_visible(before, after, text):
            return True
        if attempt + 1 < 16:
            time.sleep(0.05)
    return False


def _focus_and_replace(acc, text: str) -> bool:
    """Focus ``acc``, replace its text with key events, and read it back.

    True only when the readable text equals ``text``. An empty field,
    including one whose snapshot read is a newline, is focused and typed.
    A field that still has other text is cleared first; if that clear does
    not stick, nothing is typed on top of it. The caller puts the original
    text back when this returns False, so a contenteditable is not left empty.
    """
    if not _x11_keys_available():
        return False
    grab_focus(acc)
    current = _full_text(acc)
    if _shown_matches(acc, text):
        return True
    if current is None:
        return False
    if not _text_is_blank(current):
        _x11_select_all_and_delete(acc)
        if not _wait_until_gone(acc):
            return False
    _type_string(text)
    return _confirm_text_landed(acc, text)


def _replace_with_keys(acc, text: str) -> bool:
    """Clear the field and type ``text``. Used when EditableText is missing.

    Chromium's ATK objects do not implement AtkEditableText, so
    ``get_editable_text`` is absent and the runtime would otherwise type the
    new string onto the old one and still report success. Returns True only
    when the snapshot read equals ``text``. An unreadable field is not typed
    into. Native Wayland has no XTEST, so this returns False there.

    A contenteditable that fails the read-back is not left empty. The clear
    is what erases "Hello world"; if the new text cannot be verified, the
    original text is typed back.
    """
    current = _full_text(acc)
    if _texts_match(current, text):
        return True
    if current is None or not _x11_keys_available():
        return False
    original = current
    if not _text_is_blank(current):
        _x11_select_all_and_delete(acc)
        if not _wait_until_gone(acc):
            _restore_text(acc, original)
            return False
    else:
        grab_focus(acc)
    _type_string(text)
    if _confirm_text(acc, text):
        return True
    _restore_text(acc, original)
    return False


def _restore_text(acc, original: str | None) -> None:
    """Put ``original`` back when a write cannot be verified.

    No-op when the field already shows that text, or when XTEST cannot reach
    the session. A blank original stays blank. A field that still holds the
    original is not cleared.
    """
    if original is None or not _x11_keys_available():
        return
    now = _full_text(acc)
    if now == original or _texts_match(now, original):
        return
    if not _text_is_blank(now):
        _x11_select_all_and_delete(acc)
        if not _wait_until_gone(acc):
            return
    else:
        grab_focus(acc)
    if not _text_is_blank(original):
        _type_string(original)


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


def _character_count(acc) -> int | None:
    """``Text.get_character_count``, or None when that read fails."""
    count = _safe(lambda: _atspi().Text.get_character_count(acc))
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        return int(count)
    return None


def _qt_text_count_matches(acc, text: str) -> bool:
    """True when a Qt field's character count equals ``text``.

    GTK is not checked here. Qt's ``InsertText`` can append a NUL and more
    characters when the length was a byte count. ``GetText`` is a D-Bus
    string and stops at the NUL, so the text compares equal while the
    character count is longer than the string that was asked for.
    """
    if not _qt_app(acc):
        return True
    return _character_count(acc) == len(text)


def _content_is_blank(acc) -> bool:
    """True when the field a person reads has no text.

    An empty Chrome contenteditable reads back as a newline (the ``<br>``),
    a space, a NBSP, or NULL from ``get_text(0, -1)``. U+FFFC is expanded
    first, so a parent whose children still hold words is not blank.
    """
    raw = _full_text(acc)
    if isinstance(raw, str) and _OBJECT_REPLACEMENT in raw:
        shown = _readable_text(acc)
        if shown is not None:
            return _text_is_blank(shown)
    return _text_is_blank(raw)


def _confirm_text(acc, text: str) -> bool:
    for attempt in range(_TEXT_CONFIRM_POLLS):
        shown = _full_text(acc)
        blank = text == "" and (_text_is_blank(shown) or _content_is_blank(acc))
        if (blank or _texts_match(shown, text)) and _qt_text_count_matches(acc, text):
            return True
        if attempt + 1 < _TEXT_CONFIRM_POLLS:
            time.sleep(_TEXT_CONFIRM_PAUSE_S)
    return False


def _number_input(acc) -> bool:
    """True when object attributes say this is ``<input type=number>``."""
    attrs = _get_attributes(acc)
    tag = str(attrs.get("tag") or "").lower()
    kind = str(
        attrs.get("text-input-type")
        or attrs.get("html-input-type")
        or attrs.get("input-type")
        or attrs.get("type")
        or ""
    ).lower()
    return tag in {"", "input"} and kind == "number" and (
        tag == "input" or "text-input-type" in attrs or "html-input-type" in attrs
    )


def _value_range(acc) -> tuple[float, float] | None:
    """(minimum, maximum) from the Value interface, or None when it has none."""
    Atspi = _atspi()
    minimum = _safe(lambda: Atspi.Value.get_minimum_value(acc))
    maximum = _safe(lambda: Atspi.Value.get_maximum_value(acc))
    if isinstance(minimum, bool) or not isinstance(minimum, (int, float)):
        return None
    if isinstance(maximum, bool) or not isinstance(maximum, (int, float)):
        return None
    low, high = float(minimum), float(maximum)
    if not math.isfinite(low) or not math.isfinite(high):
        return None
    return low, high


def _parse_number(value: str) -> float:
    text = str(value).strip()
    if text.lower() in {"", "nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}:
        raise ValueError(text)
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(text)
    return number


def _numbers_match(got: float, wanted: float) -> bool:
    return abs(float(got) - float(wanted)) <= 1e-6 * max(1.0, abs(wanted))


def _format_bound(number: float) -> str:
    return format(number, "g")


def _outside_range(number: float, low: float, high: float, *, open_upper: bool) -> bool:
    """True when ``number`` is below the minimum or above a real maximum."""
    if number < low:
        return True
    return not open_upper and number > high


def _range_message(value: str, low: float, high: float, *, open_upper: bool) -> str:
    if open_upper:
        return f"value {value!r} is below the minimum {_format_bound(low)}"
    return f"value {value!r} is outside {_format_bound(low)}..{_format_bound(high)}"

# Calc cell name. A1, B2, AA10. Not a calendar day ("15") and not row 0.
_SHEET_ADDRESS = re.compile(r"^[A-Z]{1,3}[1-9][0-9]*$")
_CELL_EDITOR_PANEL = re.compile(r"^Cell\s+([A-Z]{1,3}[1-9][0-9]*)$")
_SHEET_SCAN_ROWS = 32
_SHEET_SCAN_COLS = 16
_SHEET_KEEP = 96
_LO_COMMS = frozenset({"soffice", "soffice.bin", "oosplash"})


def _sheet_formula(acc) -> str | None:
    """The Formula attribute, or None when the cell has none.

    LibreOffice stores ``=B1*2`` as ``B1*2``. An empty attribute is not a formula.
    """
    attrs = _get_attributes(acc)
    for key, val in attrs.items():
        if str(key).lower() != "formula":
            continue
        text = str(val or "").strip()
        return text or None
    return None


def _sheet_formula_text(acc) -> str | None:
    """Formula text for a snapshot, with the leading ``=`` restored."""
    formula = _sheet_formula(acc)
    if not formula:
        return None
    if formula.startswith("="):
        return formula
    return "=" + formula


def is_sheet_cell(acc) -> bool:
    """True for a Calc cell: address title, or a non-empty Formula attribute.

    A GTK tree cell and an HTML table cell are not addresses, so they stay
    on the path they already had.
    """
    if _role_name(acc) != "table cell":
        return False
    if _SHEET_ADDRESS.match(_node_name(acc)):
        return True
    return _sheet_formula(acc) is not None


def libreoffice_app(app: str) -> bool:
    """True when ``app`` names LibreOffice. The comm is often ``soffice.bin``."""
    name = (app or "").lower()
    return "soffice" in name or "libreoffice" in name


def libreoffice_process_running() -> bool:
    """True when a LibreOffice process comm is ``soffice``, ``soffice.bin``, or ``oosplash``.

    Reads ``/proc/<pid>/comm``. A command line that merely mentions the name
    is not a match.
    """
    try:
        entries = os.listdir("/proc")
    except OSError:
        return False
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm", encoding="utf-8", errors="replace") as fh:
                comm = fh.read().strip()
            with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as fh:
                stat = fh.read()
        except OSError:
            continue
        if comm not in _LO_COMMS:
            continue
        # A zombie still has the comm until its parent reaps it. It is not
        # a running LibreOffice.
        end = stat.rfind(")")
        state = stat[end + 2:].split(None, 1)[0] if end >= 0 and end + 2 < len(stat) else ""
        if state == "Z":
            continue
        return True
    return False


def libreoffice_without_bridge(app: str) -> bool:
    """True when ``app`` is LibreOffice and a soffice process is running.

    The caller has already failed to find an AT-SPI root. The gen VCL plugin
    stays off the bus; gtk3 with libreoffice-gtk3 does not.
    """
    return libreoffice_app(app) and libreoffice_process_running()


# LibreOffice's X window shows up in app list and window list while AT-SPI is
# still registering. That gap was 3–13 s. Snapshot polls until this deadline,
# then reports the app missing (or the missing gtk3 bridge).
ATSPI_REGISTER_WAIT_S = 15.0
ATSPI_REGISTER_POLL_S = 0.25


def app_listed(identifier: str) -> bool:
    """True when app list or window list already shows ``identifier``.

    App list keys the row by process comm. Window list uses that comm, the
    WM_CLASS, and the title. Those are the same names ``resolve_app`` uses,
    so a snapshot of ``LibreOffice`` sees the ``soffice.bin`` row the list
    already returned.
    """
    if not (identifier or "").strip():
        return False
    from a11y_computer_use.drivers import _linux_system

    try:
        apps = _linux_system.running_apps()
    except Exception:
        apps = []
    for row in apps or []:
        if not isinstance(row, dict):
            continue
        comm = str(row.get("bundle_id") or row.get("name") or "")
        if _linux_system._comm_matches_identifier(identifier, comm):
            return True
    try:
        rows = _linux_system.windows()
    except Exception:
        rows = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        comm = str(row.get("app") or "")
        if comm and _linux_system._comm_matches_identifier(identifier, comm):
            return True
        if _linux_system._class_matches_identifier(
            identifier,
            str(row.get("wm_class") or ""),
            str(row.get("wm_class_class") or ""),
        ):
            return True
        title = str(row.get("title") or "").lower()
        if title and any(
            name and name in title for name in _linux_system._identity_needles(identifier)
        ):
            return True
    return False


def should_wait_for_atspi(identifier: str) -> bool:
    """True when snapshot should wait for this app to appear on the AT-SPI bus.

    The app is already in the app list or the window list, or a LibreOffice
    process is running and has not registered yet. A name that is in neither
    place, and is not that process, is ``app_not_found`` on the first look.
    """
    if app_listed(identifier):
        return True
    return libreoffice_app(identifier) and libreoffice_process_running()


def _table_dimensions(acc) -> tuple[int | None, int | None]:
    try:
        Atspi = _atspi()
    except ImportError:
        return None, None
    table = getattr(Atspi, "Table", None)
    if table is None:
        return None, None

    def _as_int(value) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return int(value)

    return (
        _as_int(_safe(lambda: table.get_n_rows(acc))),
        _as_int(_safe(lambda: table.get_n_columns(acc))),
    )


def _spreadsheet_table(node) -> bool:
    """True for a Calc grid, not a calendar or a GTK tree table."""
    if _role_name(node) != "table":
        return False
    rows, cols = _table_dimensions(node)
    if rows is not None and cols is not None and (rows >= 256 or cols >= 64):
        return True
    parent = _call_first(node, ("get_parent", "getParent"))
    if parent is not None and "spreadsheet" in _role_name(parent):
        return True
    return False


def _sheet_window_cells(acc) -> list:
    """Cells from ``Table.get_accessible_at``, on-screen ones first.

    Scans 32 rows by 16 columns and keeps up to 96 cells that have a
    positive size. That window includes B2 and E1–G1 on a new sheet. When
    every cell in the scan is 0×0, the first 96 are returned so a toolkit
    that does not report extents still lists addresses.
    """
    try:
        table = _atspi().Table
    except (ImportError, AttributeError):
        return []
    on_screen: list = []
    fallback: list = []
    for row in range(_SHEET_SCAN_ROWS):
        for col in range(_SHEET_SCAN_COLS):
            cell = _safe(lambda r=row, c=col: table.get_accessible_at(acc, r, c))
            if cell is None:
                continue
            fallback.append(cell)
            _pos, size = _extents(cell, keep_zero=True)
            width = (size or (0, 0))[0] or 0
            height = (size or (0, 0))[1] or 0
            if width > 0 and height > 0:
                on_screen.append(cell)
                if len(on_screen) >= _SHEET_KEEP:
                    return on_screen
    if on_screen:
        return on_screen
    return fallback[:_SHEET_KEEP]


def sheet_cell_matches(acc, value: str) -> bool:
    """True when the cell text is ``value``, or the formula is that request.

    The formula attribute omits a leading ``=``. The computed text (``0``
    when ``=B1*2`` and B1 is empty) does not have to equal the request when
    the formula does.
    """
    if _full_text(acc) == value:
        return True
    formula = _sheet_formula(acc)
    if not formula:
        return False
    wanted = value[1:] if value.startswith("=") else value
    return formula == value or formula == wanted or ("=" + formula) == value


def sheet_outcome_text(app: str, requested: str, cell=None) -> str | None:
    """Read-back for a Calc type or set_value, or None to keep the snapshot value.

    An open cell editor's paragraph is the in-progress type. A committed
    formula is that formula, not the number the cell displays. A snapshot
    value that already equals the request is left alone.
    """
    if not requested or not libreoffice_app(app or ""):
        return None
    # The focused node during a type is often the editor paragraph, not the
    # cell. The paragraph is what holds the characters before Return.
    editor = sheet_editor_text(app)
    if editor:
        normalized = _field_text_for_type(editor, requested)
        if normalized and _typed_visible("", editor, requested):
            return normalized
    sheet = cell is not None and is_sheet_cell(cell)
    if not sheet:
        return None
    shown = _full_text(cell)
    if shown == requested:
        return None
    if not sheet_cell_matches(cell, requested):
        return None
    if not _sheet_formula(cell):
        return None
    return requested


def _find_cell_editor(node, budget: list[int]):
    """The ``Cell A1`` group opened while a cell is being edited.

    The name is matched on any role: LibreOffice 24.2 exposes it as a
    group, and 25.2 as a panel. Menus and the sheet table are not descended.
    A Calc table's child count is huge, and the editor is a sibling of the
    table, not a cell.
    """
    if budget[0] <= 0 or node is None:
        return None
    if _CELL_EDITOR_PANEL.match(_node_name(node)):
        return node
    role = _role_name(node)
    if role in {"table", "menu", "menu bar", "popup menu"}:
        return None
    budget[0] -= 1
    count = _child_count(node)
    if count > 40:
        count = 40
    for index in range(count):
        child = _child_at(node, index)
        found = _find_cell_editor(child, budget)
        if found is not None:
            return found
    return None


def sheet_editor_text(app: str) -> str | None:
    """Paragraph text of the open cell editor, or None.

    Only LibreOffice. The panel is a sibling of the sheet, named ``Cell F1``,
    and its paragraph is the in-progress string. Absent when no edit is open.
    """
    if not libreoffice_app(app):
        return None
    try:
        from a11y_computer_use.schema import Scope

        root = find_root(app, Scope.APP)
    except Exception:
        return None
    if root is None:
        return None
    panel = _find_cell_editor(root, [160])
    if panel is None:
        return None
    count = min(_child_count(panel), 8)
    for index in range(count):
        child = _child_at(panel, index)
        if child is None:
            continue
        text = _full_text(child)
        if text:
            return text
    return None


def control_kind(acc) -> str | None:
    """``combo``, ``value``, or None when ``set_text`` is the writer.

    A combo is set through its own items or its own entry. A spin button,
    slider, or other Value-interface control is set through current value.
    A plain text role stays on ``set_text``. A scroll bar's Value drives
    pixel scroll and is not a ``set_value`` target.

    Qt publishes Value on labels and empty text as well. Those are not
    numeric targets. A Qt slider, spin button, or progress bar is a numeric
    target only when its minimum and maximum are a real range. A non-Qt
    control keeps the previous rule: a spin button, a slider, a number
    input, or any other non-text role that exposes a range.
    """
    role = _role_name(acc)
    if role == "combo box":
        return "combo"
    if role in {"list box", "list"} and _collect_options(acc):
        return "combo"
    # A missing role is a fake or a node we cannot classify. Do not probe
    # Value: that import is gi, and a plain text write must stay on set_text.
    if role == "scroll bar" or role == "":
        return None
    if _qt_app(acc):
        if role in _RANGE_ROLE_NAMES:
            span = _value_range(acc)
            if span is not None and _sane_range(*span):
                return "value"
        return None
    if role in {"spin button", "slider"} or _number_input(acc):
        return "value"
    if role in _TEXT_ROLE_NAMES or is_sheet_cell(acc):
        # A spreadsheet cell's Value interface is the numeric range of a
        # double, including 0.0 on an empty cell. The text is written by
        # typing, not by Value.set_current_value. Checked before
        # ``_value_range``, which imports gi.
        return None
    if _value_range(acc) is not None:
        return "value"
    return None


def _collect_options(acc) -> list[tuple[str, object, object, int]]:
    """(label, node, parent, index) for each choice under ``acc``."""
    found: list[tuple[str, object, object, int]] = []

    def walk(node, depth: int) -> None:
        if depth > 5:
            return
        count = min(_child_count(node), 64)
        for index in range(count):
            child = _child_at(node, index)
            if child is None:
                continue
            role = _role_name(child)
            if role in _OPTION_ROLE_NAMES:
                label = _node_name(child)
                if not label:
                    raw = _safe(lambda c=child: _atspi().Text.get_text(c, 0, -1))
                    label = _strip_objects(raw) if isinstance(raw, str) else ""
                found.append((label, child, node, index))
                continue
            if role in _TEXT_ROLE_NAMES or role in {"label", "static", "push button"}:
                continue
            if depth == 0 or role in _OPTION_CONTAINER_NAMES:
                walk(child, depth + 1)

    walk(acc, 0)
    return found


def _combo_entry(acc):
    """The editable entry that belongs to this combo, not some other focused field."""
    count = min(_child_count(acc), 12)
    for index in range(count):
        child = _child_at(acc, index)
        if child is None:
            continue
        role = _role_name(child)
        if role not in {"entry", "text"}:
            continue
        if _editable_iface(child) is not None or role in {"entry", "text"}:
            return child
    return None


def _choice_value(acc, role: str, role_name: str) -> tuple[bool, str | None]:
    """(handled, value) for a combo or list.

    An editable combo's value is its own entry's text, including when that
    text is empty. A non-editable combo uses the active option. Neither falls
    through to the Value interface.
    """
    if role not in _CHOICE_ROLES and role_name not in _CHOICE_ROLE_NAMES:
        return False, None
    entry = _combo_entry(acc)
    if entry is not None:
        text = _full_text(entry)
        if isinstance(text, str):
            return True, (_strip_objects(text) or None)
        return True, None
    # Linux Qt stores the current item in the accessible name. That name is
    # the value. The label relation, applied in the snapshot title, is the
    # widget's name. The Value interface on the combo is uninitialized.
    if _qt_app(acc):
        current = _node_name(acc)
        if current:
            return True, current
    return True, _selected_option_text(acc)


def _set_entry_contents(entry, value: str) -> bool:
    """Replace ``entry`` via EditableText. No focus change and no keystrokes.

    Keystrokes would land in whichever widget is focused, which is how a combo
    write changed a different field. ``set_text_contents`` writes this entry.
    """
    eti = _editable_iface(entry)
    if eti is None:
        return False
    if _full_text(entry) == value and _qt_text_count_matches(entry, value):
        return True
    _call_first(eti, ("set_text_contents",), value, default=False)
    if _confirm_text(entry, value):
        return True
    current = _full_text(entry)
    if current:
        _select_range(entry, len(current))
        _call_first(eti, ("delete_text",), 0, len(current), default=False)
        if _full_text(entry) not in ("", None):
            return False
    _call_first(eti, ("set_text_contents",), value, default=False)
    if _confirm_text(entry, value):
        return True
    if _full_text(entry) not in ("", None):
        return False
    length = None
    for name in ("insert_text", "insertText"):
        method = getattr(eti, name, None)
        if method is not None:
            length = _insert_length(method, value, entry)
            break
    if length is None:
        return False
    _call_first(eti, ("insert_text", "insertText"), 0, value, length, default=False)
    return _confirm_text(entry, value)


def _do_action_named(acc, names: frozenset[str]) -> bool:
    action = _action_iface(acc)
    if action is None:
        return False
    count = _call_first(action, ("get_n_actions", "get_nActions"), default=0) or 0
    for index in range(int(count)):
        raw = (_call_first(action, ("get_action_name", "get_name"), index, default="") or "").lower()
        if raw in names and _call_first(action, ("do_action", "doAction"), index, default=False):
            return True
    return False


def _chromium_control(acc) -> bool:
    """True when this node belongs to Chromium. A fake without a toolkit is not."""
    try:
        return _chromium_app(acc)
    except Exception:
        return False


def _popup_open(acc) -> bool:
    """Whether the choice popup is up.

    Chrome's select sets EXPANDED, sometimes a beat after the call that
    opened it. A child menu that is SHOWING or EXPANDED counts too, so a
    popup that does not flip the combo's own state is still closed.
    """
    if _state_has(acc, "EXPANDED"):
        return True
    # Qt keeps the combo's list SHOWING in the tree while the popup is closed.
    # EXPANDED on the combo is the popup. Treating that list as open would
    # send Escape on every set and then still see the list as showing.
    if _qt_app(acc):
        return False
    count = min(_child_count(acc), 12)
    for index in range(count):
        child = _child_at(acc, index)
        if child is None:
            continue
        if _role_name(child) not in {"menu", "popup menu", "list box", "list"}:
            continue
        if _state_has(child, "EXPANDED") or _state_has(child, "SHOWING") or _state_has(child, "VISIBLE"):
            return True
    return False


def _gecko_popup_open(acc) -> bool:
    """Whether a Firefox select popup is actually open.

    The option menu stays VISIBLE while the select is collapsed. Treating
    that as open would send Escape and undo a selection that already landed.
    EXPANDED on the combo, or SHOWING on the menu, is the open popup.
    """
    if _state_has(acc, "EXPANDED"):
        return True
    count = min(_child_count(acc), 8)
    for index in range(count):
        child = _child_at(acc, index)
        if child is None:
            continue
        if _role_name(child) not in {"menu", "popup menu"}:
            continue
        if _state_has(child, "SHOWING") or _state_has(child, "EXPANDED"):
            return True
    return False


def _close_combo_popup(acc) -> None:
    """Close a popup this call opened. A combo that is not expanded is left alone.

    The close is repeated briefly. Chrome can mark the select EXPANDED after
    ``select_child`` returns, and one Escape is not always enough. Collapse
    is tried before Escape so a toolkit that has the action does not also
    receive a key. A collapsed Firefox select is left alone: its menu stays
    VISIBLE, and Escape would undo a selection that already landed.
    """
    if _gecko_app(acc) and not _gecko_popup_open(acc):
        return
    for attempt in range(4):
        if not _popup_open(acc):
            # Chrome can set EXPANDED after select_child has already returned.
            # One beat covers that. A GTK combo that is not expanded stays put.
            if attempt > 0 or not _chromium_control(acc):
                return
            time.sleep(0.05)
            continue
        _do_action_named(acc, frozenset({"collapse", "close", "hide"}))
        if _popup_open(acc) and _x11_keys_available():
            from a11y_computer_use.drivers import _linux_input

            _linux_input.press_chord("Escape")
        if not _popup_open(acc):
            return
        time.sleep(0.04)


def _combo_active_label(acc) -> str | None:
    """The combo's active item, not a popup row that is only highlighted.

    GTK's combo Selection child is ``gtk_combo_box_get_active``. A menu's
    own Selection can mark a different row SELECTED without changing that.
    When the toolkit has no ``get_selected_child``, a SELECTED child of the
    combo itself is the active item. A SELECTED row nested in the popup is not.
    """
    iface = _selection_iface(acc)
    if iface is not None:
        child = _call_first(iface, ("get_selected_child", "getSelectedChild"), 0)
        if child is not None:
            label = _option_label(child)
            if label:
                return label
    for label, node, parent, _index in _collect_options(acc):
        if parent is acc and label and _state_has(node, "SELECTED"):
            return label
    shown = _full_text(acc)
    if isinstance(shown, str):
        cleaned = _strip_objects(shown)
        if cleaned:
            return cleaned
    return None


def _qt_combo_current(acc) -> str | None:
    """Qt's current combo item. On Linux that is the accessible name."""
    name = _node_name(acc)
    return name or None


def _qt_combo_shows(combo, label: str) -> bool:
    """Whether Qt's current item is ``label``. The name can trail the click."""
    for attempt in range(8):
        if _qt_combo_current(combo) == label:
            return True
        if attempt + 1 < 8:
            time.sleep(0.05)
    return False


def _open_qt_popup(combo) -> None:
    """Show the Qt combo popup. A combo that is already expanded is left open."""
    if _state_has(combo, "EXPANDED"):
        return
    _do_action_named(combo, frozenset({"showmenu", "press", "show", "open"}))
    for _attempt in range(8):
        if _state_has(combo, "EXPANDED"):
            return
        time.sleep(0.05)


def _activate_qt_option(combo, label: str, node) -> None:
    """Choose ``label`` on a Qt combo through its list popup.

    The combo has no Selection interface. ``Toggle`` on a list item returns
    true and leaves the current item alone. Opening the popup and clicking
    the item's center is what changes ``currentText``. The popup closes
    itself when that click lands. The caller reads the name back.
    """
    if _qt_combo_current(combo) == label:
        return
    _open_qt_popup(combo)
    target = _option_named(combo, label) or node
    _click_center(target)
    _qt_combo_shows(combo, label)


def _combo_landed(acc, entry, value: str) -> bool:
    if entry is not None:
        text = _full_text(entry)
        return text is not None and text.replace(_OBJECT_REPLACEMENT, "") == value
    if _qt_app(acc):
        if _qt_combo_current(acc) == value:
            return True
        return _selected_option_text(acc) == value
    if _chromium_control(acc):
        # The same text the snapshot shows: the SELECTED option's name. Chrome
        # keeps the combobox name as the aria-label and its text as U+FFFC.
        # The selected menu item is the value, and it can arrive a beat late.
        return _choice_shows(acc, value)
    if _gecko_app(acc):
        # Firefox's selected option is a menu item. The combo has no Selection
        # child and its own text is empty. The selected item's name is the value.
        return _gecko_choice_shows(acc, value)
    return _combo_active_label(acc) == value


def _option_named(acc, label: str):
    """The option node named ``label`` after a popup has been opened, or None."""
    for item_label, node, _parent, _index in _collect_options(acc):
        if item_label == label:
            return node
    return None


def _click_center(node) -> None:
    """One left click at the node's screen center, when it has a box."""
    if not _x11_keys_available():
        return
    pos, size = _extents(node)
    if pos is None or size is None:
        return
    width, height = size
    if width <= 0 or height <= 0:
        return
    try:
        from a11y_computer_use.drivers import _linux_input

        _linux_input.click(int(pos[0] + width / 2), int(pos[1] + height / 2))
    except Exception:
        return


_WEB_OPTION_ACTIONS = frozenset({"select", "click", "press", "activate"})


def _choice_shows(combo, label: str, *, wait: bool = True) -> bool:
    """Whether the selected-option text is ``label``.

    Chrome's option ``select`` action updates the menu item's SELECTED state
    and the DOM together. A busy renderer can publish that state a beat
    after the action returns, so a check that follows an action waits. The
    wait is bounded and stops on the first match. A check before any action
    does not wait.
    """
    attempts = 6 if wait else 1
    for attempt in range(attempts):
        if _selected_option_text(combo) == label:
            return True
        if attempt + 1 < attempts:
            time.sleep(0.05)
    return False


def _menu_selection(combo):
    """Selection interface that can choose an option, and the node that owns it.

    A Chrome ``<select>`` combobox has no Selection interface. The child menu
    does, and ``select_child`` on that menu does not change the HTML value.
    The interface is still tried after the option action, for a toolkit that
    implements it. ``(None, None)`` when neither node has one.
    """
    iface = _selection_iface(combo)
    if iface is not None:
        return combo, iface
    count = min(_child_count(combo), 6)
    for index in range(count):
        child = _child_at(combo, index)
        if child is None:
            continue
        if _role_name(child) not in {"menu", "popup menu", "list box", "list"}:
            continue
        child_iface = _selection_iface(child)
        if child_iface is not None:
            return child, child_iface
    return None, None


def _activate_web_option(combo, label: str, node, index: int) -> None:
    """Select a Chrome ``<select>`` option and leave the popup closable.

    The option's own ``select`` action is what changes the HTML value. The
    combobox has no Selection interface, and ``select_child`` on its menu
    returns false and leaves the value alone. ``click`` / ``press`` /
    ``activate`` are accepted for a tree that names the action that way.
    An option that is already selected is not touched, so the popup is not
    opened for the current value. A click at the option's center is the last
    try, after the popup has been opened so the item has a box.
    """
    if _choice_shows(combo, label, wait=False):
        return
    target = _option_named(combo, label) or node
    _do_action_named(target, _WEB_OPTION_ACTIONS)
    if _choice_shows(combo, label):
        return
    _owner, iface = _menu_selection(combo)
    if iface is not None:
        _call_first(iface, ("select_child", "selectChild"), index, default=False)
    if _choice_shows(combo, label):
        return
    if not _popup_open(combo):
        _do_action_named(combo, frozenset({"press", "show", "open"}))
    target = _option_named(combo, label) or target
    _do_action_named(target, _WEB_OPTION_ACTIONS)
    if _choice_shows(combo, label):
        return
    _click_center(target)
    _choice_shows(combo, label)


def _gecko_choice_shows(combo, label: str, *, wait: bool = True) -> bool:
    """Whether Firefox's selected option text is ``label``.

    The selected menu item can update a beat after the key. The wait is
    bounded and stops on the first match.
    """
    attempts = 6 if wait else 1
    for attempt in range(attempts):
        if _selected_option_text(combo) == label:
            return True
        if attempt + 1 < attempts:
            time.sleep(0.05)
    return False


def _activate_gecko_option(combo, label: str, node, options) -> None:
    """Choose ``label`` on a Firefox ``<select>``.

    The option's ``select`` action is tried first. On this toolkit that
    action does not change a collapsed select. Focus plus Up or Down then
    moves the selection by the option index, which is what a keyboard user
    does, and the selected option is read back. The popup is not opened:
    opening it and then sending Escape puts the previous value back.
    """
    if _selected_option_text(combo) == label:
        return
    target = _option_named(combo, label) or node
    _do_action_named(target, frozenset({"select", "click", "press", "activate"}))
    if _gecko_choice_shows(combo, label, wait=False):
        return
    if not _x11_keys_available():
        return
    grab_focus(combo)
    labels = [item[0] for item in options if item[0]]
    current = _selected_option_text(combo)
    if current in labels and label in labels and current != label:
        from a11y_computer_use.drivers import _linux_input

        delta = labels.index(label) - labels.index(current)
        chord = "Down" if delta > 0 else "Up"
        for _ in range(abs(delta)):
            _linux_input.press_chord(chord)
            if _selected_option_text(combo) == label:
                return
    if _selected_option_text(combo) == label:
        return
    _type_string(label)


def _activate_combo_option(combo, options, match) -> None:
    """Choose ``match`` on the combo, not on its popup menu.

    ``Selection.select_child`` on a GTK combo is the model index and calls
    ``gtk_combo_box_set_active``. The same call on the popup only highlights
    the row. A click on the item is the fallback that activates it. Chrome's
    select uses its own path: the snapshot's selected-option text is the
    read-back, and the popup is not opened when that text is already the value.
    """
    label, node, _parent, index = match
    if _chromium_control(combo):
        model_index = next((i for i, item in enumerate(options) if item[1] is node), index)
        _activate_web_option(combo, label, node, model_index)
        return
    if _gecko_app(combo):
        _activate_gecko_option(combo, label, node, options)
        return
    if _qt_app(combo):
        _activate_qt_option(combo, label, node)
        return
    if _combo_active_label(combo) == label:
        return
    model_index = next(i for i, item in enumerate(options) if item[1] is node)
    iface = _selection_iface(combo)
    if iface is not None:
        _call_first(iface, ("select_child", "selectChild"), model_index, default=False)
    if _combo_active_label(combo) == label:
        return
    if not _state_has(combo, "EXPANDED"):
        _do_action_named(combo, frozenset({"press", "show", "open"}))
    _do_action_named(node, frozenset({"click", "press", "activate"}))


def set_combo_value(acc, value: str) -> None:
    """Choose ``value`` on this combo or list, or write its own entry.

    An editable combo is written with ``set_text_contents`` on its own entry.
    That sends no keystrokes, so a different focused field is left untouched.
    A non-editable combo is set through its own Selection. An unknown option
    raises ValueError before any selection, and the message lists the options.
    A popup this call opened is closed. The call raises when the read-back is
    not ``value``. A highlighted popup row is not a successful read-back.
    """
    entry = _combo_entry(acc)
    options = _collect_options(acc)
    if entry is None and not options:
        _do_action_named(acc, frozenset({"press", "show", "open"}))
        options = _collect_options(acc)
        entry = _combo_entry(acc)
    try:
        if entry is not None:
            if not _set_entry_contents(entry, value):
                raise _text_mismatch(
                    "text_mismatch",
                    f"the combo entry read back does not match {value!r}",
                    expected=value,
                )
        else:
            match = next((item for item in options if item[0] == value), None)
            if match is None:
                labels = [label for label, *_rest in options if label]
                listed = ", ".join(labels)
                raise ValueError(f"value {value!r} is not one of: {listed}")
            _activate_combo_option(acc, options, match)
    finally:
        _close_combo_popup(acc)
    if _state_has(acc, "EXPANDED"):
        raise _text_mismatch(
            "popup_open",
            "the combo popup is still open",
            expected=value,
        )
    if not _combo_landed(acc, entry, value):
        raise _text_mismatch(
            "text_mismatch",
            f"the value read back does not match {value!r}",
            expected=value,
        )


def _minimum_increment(acc) -> float | None:
    """The Value interface's step, or None when it is missing or not positive."""
    Atspi = _atspi()
    got = _safe(lambda: Atspi.Value.get_minimum_increment(acc))
    if isinstance(got, bool) or not isinstance(got, (int, float)):
        return None
    step = float(got)
    if not math.isfinite(step) or step <= 0:
        return None
    return step


def _snap_spin(acc, number: float, low: float) -> float:
    """Round a GTK spin button onto its step.

    Setting 4.6 on an integer spin leaves the adjustment at 4.6 while the
    text shows 5. The value written is the nearest step, so the adjustment
    and the text agree. A Chrome number input is not a spin adjustment and
    keeps the number it was given. A spin whose step is missing and whose
    text is an integer uses a step of 1.
    """
    if _role_name(acc) != "spin button" or _number_input(acc):
        return number
    step = _minimum_increment(acc)
    if step is None:
        shown = _full_text(acc)
        if not isinstance(shown, str) or not shown.strip().lstrip("+-").isdigit():
            return number
        step = 1.0
    snapped = low + round((number - low) / step) * step
    return float(format(snapped, ".10g"))


def _number_text(acc) -> str | None:
    """The number field's text, stripped. None when the read failed.

    An empty Chrome number input is ``""``. Its Value interface still reports
    the minimum (0.0 when min is 0). That 0.0 is not a value the field holds.
    """
    raw = _full_text(acc)
    if raw is None:
        return None
    return raw.strip()


def _number_field_is_empty(acc) -> bool:
    """True for an ``<input type=number>`` whose text is empty or unreadable."""
    if not _number_input(acc):
        return False
    return _number_text(acc) in ("", None)


def _number_text_matches(acc, value: str) -> bool:
    """True when the field's text parses as ``value`` and is not empty.

    An empty field does not match ``0``, even when the Value interface reads
    0.0. The poll is bounded and stops on the first match.
    """
    wanted = _parse_number(value)
    for attempt in range(8):
        shown = _number_text(acc)
        if shown:
            try:
                if _numbers_match(_parse_number(shown), wanted):
                    return True
            except ValueError:
                if shown == str(value).strip():
                    return True
        if attempt + 1 < 8:
            time.sleep(0.05)
    return False


def _fill_empty_number(acc, value: str) -> bool:
    """Type ``value`` into an empty Chrome number input.

    ``Value.set_current_value`` does not change an empty input that has a
    minimum and a maximum: the DOM stays ``""`` and the Value interface stays
    at the minimum. A click that focuses the field, then the digits, is what
    fills it. Success is the text, not the Value interface.
    """
    grab_focus(acc)
    _click_center(acc)
    if not _x11_keys_available():
        return False
    _type_string(value)
    return _number_text_matches(acc, value)


# Chrome publishes a cleared number input after the key events, and a click
# that just selected a list row can still own the caret when the clear starts.
# Four 20ms reads (the generic text confirm) return before that DOM update.
# These tries click the field and poll the text. A field that never empties
# is still a failure.
_NUMBER_CLEAR_TRIES = 3
_NUMBER_CLEAR_WAIT_S = 0.8


def _clear_number_input(acc) -> bool:
    """Make an ``<input type=number>`` empty. True only when its text is ``""``.

    A toolkit with EditableText is cleared through ``set_text``. Chrome's
    spin button has no EditableText (probed on Chrome 154: role ``spin
    button``, ``text-input-type=number``, no editable interface, text ``"3"``).
    ``set_text`` then focuses and sends ctrl+a, BackSpace, and gives up after
    about 80ms. Those keys miss the field when a list row still has the
    caret, and a busy renderer can publish the empty value after that window.
    A click at the field's center places the caret. ctrl+a, BackSpace, and
    Delete are sent, and the text is polled until it is empty. A failed read
    is not an empty field.
    """
    if _editable_iface(acc) is not None and set_text(acc, ""):
        if _number_text(acc) == "":
            return True
    if not _x11_keys_available():
        return False
    from a11y_computer_use.drivers import _linux_input

    for _attempt in range(_NUMBER_CLEAR_TRIES):
        grab_focus(acc)
        _click_center(acc)
        _linux_input.press_chord("ctrl+a")
        _linux_input.press_chord("backspace")
        _linux_input.press_chord("delete")
        deadline = time.monotonic() + _NUMBER_CLEAR_WAIT_S
        while True:
            if _number_text(acc) == "":
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    return False


def set_numeric_value(acc, value: str) -> bool | str:
    """Set the Value interface's current value.

    A non-number or a number outside minimum..maximum raises ValueError before
    the write. The message includes the minimum and maximum. The current value
    is read back and must match. False means ``value`` is a finite number and
    this control has no Value interface, so the caller may use ``set_text``.
    An empty string on an ``<input type=number>`` clears the field. When the
    control has EditableText, ``set_text`` clears it. Chrome's number input
    has no EditableText: a click places the caret, then ctrl+a, BackSpace,
    and Delete are sent, and the text is polled until it is empty. A GTK spin
    button is snapped to its step; the return value is that step's text when
    it differs from what was asked, so the caller reports the value held.
    An empty Chrome number input is filled by typing. A read-back of 0.0 from
    the Value interface is not success while the text is still empty.
    """
    if value == "" and _number_input(acc):
        if _clear_number_input(acc):
            return True
        raise _text_mismatch(
            "text_mismatch",
            "the number field could not be cleared",
            expected="",
        )
    span = _value_range(acc)
    low = high = None
    open_upper = False
    if span is not None:
        low, high = span
        # Chrome reports maximum 0 on <input type=number min=0> with no max.
        # A maximum that is not above the minimum is not a bound.
        open_upper = high <= low
    try:
        number = _parse_number(value)
    except ValueError:
        if low is None or high is None:
            raise ValueError(f"value {value!r} is not a number") from None
        if open_upper:
            raise ValueError(
                f"value {value!r} is not a number; minimum is {_format_bound(low)}"
            ) from None
        raise ValueError(
            f"value {value!r} is not a number; valid range is {_format_bound(low)}..{_format_bound(high)}"
        ) from None
    if span is None:
        return False
    if _outside_range(number, low, high, open_upper=open_upper):
        raise ValueError(_range_message(value, low, high, open_upper=open_upper))
    number = _snap_spin(acc, number, low)
    if _outside_range(number, low, high, open_upper=open_upper):
        raise ValueError(_range_message(value, low, high, open_upper=open_upper))
    if _number_field_is_empty(acc):
        if _fill_empty_number(acc, value):
            return True
        shown = _number_text(acc)
        raise _text_mismatch(
            "text_mismatch",
            f"the value read back {shown!r} does not match {value!r}",
            expected=value,
            actual=shown if shown is not None else "",
        )
    Atspi = _atspi()
    _safe(lambda: Atspi.Value.set_current_value(acc, number), False)
    got = None
    for attempt in range(_TEXT_CONFIRM_POLLS):
        got = _safe(lambda: Atspi.Value.get_current_value(acc))
        if isinstance(got, (int, float)) and not isinstance(got, bool) and _numbers_match(float(got), number):
            # An empty number field reports the minimum. That is not the text.
            if _number_input(acc) and _number_text(acc) in ("", None):
                break
            if _numbers_match(float(got), _parse_number(value)):
                return True
            return _format_bound(float(got))
        if attempt + 1 < _TEXT_CONFIRM_POLLS:
            time.sleep(_TEXT_CONFIRM_PAUSE_S)
    raise _text_mismatch(
        "text_mismatch",
        f"the value read back {got!r} does not match {value!r}",
        expected=value,
        actual="" if _number_input(acc) and _number_text(acc) in ("", None) else got,
    )


def _selection_iface(acc):
    return _call_first(acc, ("get_selection_iface", "get_selection"))


def _child_index(parent, child) -> int | None:
    count = min(_child_count(parent), 500)
    for index in range(count):
        kid = _child_at(parent, index)
        if kid is None:
            continue
        if kid is child or kid == child:
            return index
    return None


def _node_contains(node, target, depth: int = 0) -> bool:
    if depth > 6:
        return False
    count = min(_child_count(node), 64)
    for index in range(count):
        child = _child_at(node, index)
        if child is None:
            continue
        if child is target or child == target:
            return True
        if _node_contains(child, target, depth + 1):
            return True
    return False


def _index_of_containing_child(parent, target) -> int | None:
    """Index of the direct child of ``parent`` that is or contains ``target``."""
    count = min(_child_count(parent), 500)
    for index in range(count):
        kid = _child_at(parent, index)
        if kid is None:
            continue
        if kid is target or kid == target or _node_contains(kid, target):
            return index
    return None


def _selection_parent(acc):
    """(parent, child index, row node) when ``acc`` sits in a Selection container.

    A scroll pane or filler between the row and the list still counts. The
    index is that row's index under the selection parent. A menu is not a
    selection target: menu items are activated, not selected as list rows.
    """
    origin = _role_name(acc)
    if origin not in _ROW_ROLE_NAMES and origin not in _OPTION_ROLE_NAMES:
        return None, None, None
    node = acc
    for _ in range(8):
        parent = _parent_of(node)
        if parent is None:
            return None, None, None
        parent_role = _role_name(parent)
        if parent_role in {"menu", "popup menu", "menu bar"}:
            return None, None, None
        if _selection_iface(parent) is not None and parent_role in (
            _LIST_ROLES | {"tree", "tree table", "table"}
        ):
            index = _child_index(parent, acc)
            if index is None:
                index = _index_of_containing_child(parent, acc)
            return parent, index, acc
        node = parent
    return None, None, None


def _first_press_action(acc) -> str:
    action = _action_iface(acc)
    if action is None:
        return ""
    count = _call_first(action, ("get_n_actions", "get_nActions"), default=0) or 0
    for index in range(int(count)):
        raw = (_call_first(action, ("get_action_name", "get_name"), index, default="") or "").lower()
        if raw in _PRESS_ACTION_NAMES or raw in _PICK_ACTION_NAMES:
            return raw
    return ""


def _row_selected(node) -> bool:
    return _state_has(node, "SELECTED")


def chromium_list_row(acc) -> bool:
    """True when ``acc`` is an option in a Chromium list or list box.

    The option's ``select`` action toggles, and ``Selection.select_child``
    issued while that action is still landing cancels it. A plain click at
    the row center is the path that selects the row. GTK tables are not
    Chromium lists and stay on Selection.
    """
    parent, _index, target = _selection_parent(acc)
    if parent is None or target is None:
        return False
    if _role_name(parent) not in {"list box", "list"}:
        return False
    try:
        return bool(_chromium_app(parent))
    except Exception:
        return False


def selected_option_names(acc) -> list[str]:
    """Names of the selected options in ``acc``'s list, in child order."""
    parent, _index, _target = _selection_parent(acc)
    if parent is None:
        return []
    names: list[str] = []
    count = min(_child_count(parent), 50)
    for index in range(count):
        child = _child_at(parent, index)
        if child is None or not _row_selected(child):
            continue
        label = _node_name(child)
        if label:
            names.append(label)
    return names


def restore_selection(acc, names: list[str]) -> None:
    """Select each name in ``names`` that a failed click deselected.

    ``select_child`` adds on a Chrome list box. It is not preceded by the
    option's ``select`` action, which would toggle the row back off. Names
    that are still selected are left alone. A list that has no Selection
    interface is unchanged.
    """
    wanted = [name for name in names if name]
    if not wanted:
        return
    parent, _index, _target = _selection_parent(acc)
    if parent is None:
        return
    iface = _selection_iface(parent)
    if iface is None:
        return
    for _attempt in range(6):
        pending = set(wanted)
        count = min(_child_count(parent), 50)
        for index in range(count):
            child = _child_at(parent, index)
            if child is None:
                continue
            label = _node_name(child)
            if label not in pending:
                continue
            if _row_selected(child):
                pending.discard(label)
                continue
            _call_first(iface, ("select_child", "selectChild"), index, default=False)
            if _row_selected(child):
                pending.discard(label)
        if not pending:
            return
        time.sleep(0.05)


def row_becomes_selected(acc) -> bool:
    """True when the row is selected and still selected after a short beat.

    A coordinate click's SELECTED state can trail the button release. The
    wait is a handful of reads and stops on the first stable selection.
    """
    _parent, _index, target = _selection_parent(acc)
    node = target or acc
    for attempt in range(8):
        if _row_selected(node):
            time.sleep(0.04)
            if _row_selected(node):
                return True
        if attempt + 1 < 8:
            time.sleep(0.05)
    return False


def _row_settled(node) -> bool:
    """True when ``node`` is selected and still selected after a short beat.

    Chrome's listbox click action can report success and set SELECTED for one
    read, then leave the HTML selection unchanged. A success has to outlast
    that beat. Hermetic fakes patch ``time.sleep`` so the second read is
    immediate.
    """
    if not _row_selected(node):
        return False
    time.sleep(0.04)
    return _row_selected(node)


def select_contained_row(acc) -> bool | None:
    """Select a row or cell inside a Selection container.

    None means this node is not such a row, so the caller uses ``do_press``.
    True means the row is selected and stayed selected. False means it is a
    selection row and it is still not selected: the caller clicks the
    on-screen center, then checks again. A GTK cell's expand/edit/activate
    action is not used. A Chrome option's click is, and the selection is
    checked afterwards. A click action that returns without leaving the row
    selected is not success.
    """
    parent, index, target = _selection_parent(acc)
    if parent is None or target is None:
        return None
    action = _first_press_action(acc)
    if action in _SELECTING_ACTION_NAMES:
        do_press(acc)
        if _row_settled(target):
            return True
    iface = _selection_iface(parent)
    if iface is not None and index is not None:
        _call_first(iface, ("select_child", "selectChild"), index, default=False)
        if _row_settled(target):
            return True
    return False


def row_is_selected(acc) -> bool:
    """Whether the selection row under ``acc`` is selected. False for other nodes."""
    _parent, _index, target = _selection_parent(acc)
    if target is None:
        return False
    return _row_settled(target)


# Chrome's contenteditable ignores EditableText and a short ctrl+a/BackSpace.
# A click is not required: focus plus select-all, BackSpace, and Delete, then
# a poll, is what empties it. Three tries cover a renderer that publishes
# the empty value late. A field that still has words is a failure.
_CONTENT_CLEAR_TRIES = 3
_CONTENT_CLEAR_POLLS = 12


def _chromium_contenteditable(acc) -> bool:
    """True for a Chrome contenteditable, not an input, a textarea, or GTK.

    The live node is an ``entry`` whose tag is ``div`` and whose xml-roles
    is ``textbox``. A number input stays on the number-clear path. A GTK
    entry is not a Chromium app.
    """
    if not _chromium_app(acc) or _number_input(acc):
        return False
    attrs = _get_attributes(acc)
    tag = str(attrs.get("tag") or "").lower()
    if tag in {"input", "textarea", "select"}:
        return False
    xml = set(str(attrs.get("xml-roles") or "").split())
    role = _role_name(acc)
    if "textbox" in xml and tag not in {"input", "textarea"}:
        return True
    return tag in {"div", "span", "p", "pre"} and role in {
        "entry", "text", "section", "paragraph", "panel", "document text",
    }


def _clear_contenteditable(acc) -> bool:
    """Empty a Chrome contenteditable. True only when the read is blank.

    Focus, select all, BackSpace, then Delete. The read is polled. A newline,
    a space, or NULL counts as empty. Words that are still there are a
    failure: the previous text is put back when this call changed it, and
    the caller reports ``text_mismatch``. An input, a textarea, and a GTK
    field do not use this path.
    """
    if _content_is_blank(acc):
        return True
    if not _x11_keys_available():
        return False
    from a11y_computer_use.drivers import _linux_input

    original = _full_text(acc)
    for _attempt in range(_CONTENT_CLEAR_TRIES):
        grab_focus(acc)
        _linux_input.press_chord("ctrl+a")
        _linux_input.press_chord("backspace")
        _linux_input.press_chord("delete")
        for poll in range(_CONTENT_CLEAR_POLLS):
            if _content_is_blank(acc):
                return True
            if poll + 1 < _CONTENT_CLEAR_POLLS:
                time.sleep(0.05)
    if not _content_is_blank(acc):
        _restore_text(acc, original)
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
    returns True only when the snapshot read equals ``text``. On Qt the
    character count has to equal that string too: ``GetText`` stops at an
    embedded NUL, so the text alone can match while the widget is longer.
    On Firefox, when EditableText returns success and the snapshot read is
    still not ``text``, the field is focused and the value is typed, and
    success is still that read-back. Chromium and GTK keep the previous
    tail: keys are sent only when the snapshot read is already empty,
    because focusing a Chrome number input can make an empty field read
    back as 0. When the snapshot read is left blank and the new text cannot
    be verified, the text from before the call is put back, so a
    contenteditable is not left empty. A Chromium field that still reads a
    value (an empty number input reads 0) is not focused in order to restore
    it. An empty string on a Chrome contenteditable is focus, select-all,
    BackSpace, and Delete, then a poll. A newline left by ``<br>`` is empty.
    Words that remain are ``text_mismatch``, and the previous text stays.
    An input, a textarea, and a GTK field do not use that clear.
    """
    if text == "" and _chromium_contenteditable(acc):
        return _clear_contenteditable(acc)
    eti = _editable_iface(acc)
    if eti is None:
        return _replace_with_keys(acc, text)
    original = _full_text(acc)
    wrote = bool(_call_first(eti, ("set_text_contents",), text, default=False))
    current = _full_text(acc)
    if (current == text or _texts_match(current, text)) and _qt_text_count_matches(acc, text):
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
        length = _insert_length(insert, text, acc) if insert is not None else len(text)
        if not _call_first(eti, ("insert_text", "insertText"), 0, text, length, default=False):
            if _text_is_gone(acc) and _x11_keys_available():
                _type_string(text)
            else:
                return False
    if _confirm_text(acc, text):
        return True
    # Firefox returns true from set_text_contents and insert_text and the DOM
    # stays empty. Key events land after the field is focused. Chromium is
    # not this path: focusing an empty number input can make the read-back 0.
    # A contenteditable that does not confirm is not left empty: the clear
    # already removed "Hello world", so the original is typed back.
    if _gecko_app(acc):
        if _focus_and_replace(acc, text):
            return True
        _restore_text(acc, original)
        return False
    if _text_is_gone(acc) and _x11_keys_available():
        _type_string(text)
        if _confirm_text(acc, text):
            return True
    # Only a blank read is restored. A Chromium number input that reads 0
    # after a failed clear is not blank, and focusing it to put the old
    # digits back is what makes the empty field read 0.
    if _text_is_gone(acc):
        _restore_text(acc, original)
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


# bus name -> monotonic time before which another reconnect is refused.
# A dialog flood wedges the current connection. Replacing it once recovers
# the following read. Further -1 counts in that same walk must not open a
# connection per node. A later action (Ctrl+L in the file chooser) can wedge
# the replacement too; that is a new failure and gets its own connection
# once the short same-walk block has passed. A replacement that is itself
# still failing extends the block so a dead app cannot reconnect per node.
_revive_block_until: dict[str, float] = {}
_last_revive_key: str | None = None


def _gobject_pointer(obj) -> int | None:
    """Address of a PyGObject, or None for a synthetic test double."""
    cap = getattr(obj, "__gpointer__", None)
    if cap is None:
        return None
    try:
        import ctypes

        api = ctypes.pythonapi
        api.PyCapsule_GetPointer.restype = ctypes.c_void_p
        api.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
        api.PyCapsule_GetName.restype = ctypes.c_char_p
        api.PyCapsule_GetName.argtypes = [ctypes.py_object]
        return int(api.PyCapsule_GetPointer(cap, api.PyCapsule_GetName(cap)) or 0) or None
    except Exception:
        return None


def _a11y_bus_address() -> str | None:
    """The session's accessibility bus address, or None when it cannot be read."""
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress",
            None, GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 1000, None,
        )
        address = reply.get_child_value(0).get_string()
    except Exception:
        return None
    return address or None


def _revive_application(acc) -> bool:
    """Replace a wedged per-app D-Bus connection with a fresh accessibility-bus one.

    libatspi moves each app onto a private socket. A ``DoAction`` that opens a
    ``Gtk.Dialog.run()`` nested loop (a file chooser, or any dialog the app
    runs modally) leaves that socket stuck: the click itself returns, and the
    next read on the same connection waits out the timeout. Child count comes
    back -1, so the snapshot root is the application group with no children,
    or the app disappears from name search entirely. A new connection to the
    shared accessibility bus reads the same tree at once. The old connection
    is left open: unreffing a socket that still has a watch is a use-after-free.
    Returns True when the pointer was replaced.
    """
    pointer = _gobject_pointer(acc)
    if not pointer:
        return False
    try:
        import ctypes
    except Exception:
        return False

    class _GObject(ctypes.Structure):
        _fields_ = [
            ("g_class", ctypes.c_void_p),
            ("ref_count", ctypes.c_uint),
            ("pad", ctypes.c_uint),
            ("qdata", ctypes.c_void_p),
        ]

    class _AtspiObject(ctypes.Structure):
        _fields_ = [
            ("parent", _GObject),
            ("app", ctypes.c_void_p),
            ("path", ctypes.c_char_p),
        ]

    class _AtspiApplication(ctypes.Structure):
        _fields_ = [
            ("parent", _GObject),
            ("hash", ctypes.c_void_p),
            ("bus_name", ctypes.c_char_p),
            ("bus", ctypes.c_void_p),
        ]

    class _DBusError(ctypes.Structure):
        _fields_ = [
            ("name", ctypes.c_char_p),
            ("message", ctypes.c_char_p),
            ("dummy", ctypes.c_uint),
            ("padding", ctypes.c_void_p),
        ]

    try:
        obj = _AtspiObject.from_address(pointer)
        path = obj.path or b""
        if not path.startswith(b"/org/a11y/atspi/") or not obj.app:
            return False
        app = _AtspiApplication.from_address(obj.app)
        bus_name = app.bus_name or b""
        if not bus_name.startswith(b":"):
            return False
        key = bus_name.decode("utf-8", "replace")
        now = time.monotonic()
        if now < _revive_block_until.get(key, 0.0):
            return False
        # Hold the block across clear_cache so a re-entrant -1 does not
        # open a second connection before this one has been tried.
        _revive_block_until[key] = now + 0.5
        address = _a11y_bus_address()
        if not address:
            return False
        lib = ctypes.CDLL("libdbus-1.so.3")
        lib.dbus_error_init.argtypes = [ctypes.POINTER(_DBusError)]
        lib.dbus_error_is_set.argtypes = [ctypes.POINTER(_DBusError)]
        lib.dbus_error_is_set.restype = ctypes.c_int
        lib.dbus_error_free.argtypes = [ctypes.POINTER(_DBusError)]
        lib.dbus_connection_open_private.argtypes = [ctypes.c_char_p, ctypes.POINTER(_DBusError)]
        lib.dbus_connection_open_private.restype = ctypes.c_void_p
        lib.dbus_bus_register.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DBusError)]
        lib.dbus_bus_register.restype = ctypes.c_int
        err = _DBusError()
        lib.dbus_error_init(ctypes.byref(err))
        conn = lib.dbus_connection_open_private(address.encode(), ctypes.byref(err))
        if not conn or lib.dbus_error_is_set(ctypes.byref(err)):
            if lib.dbus_error_is_set(ctypes.byref(err)):
                lib.dbus_error_free(ctypes.byref(err))
            return False
        lib.dbus_error_init(ctypes.byref(err))
        if not lib.dbus_bus_register(conn, ctypes.byref(err)):
            if lib.dbus_error_is_set(ctypes.byref(err)):
                lib.dbus_error_free(ctypes.byref(err))
            return False
        app.bus = conn
        global _last_revive_key
        _last_revive_key = key
        _call_first(acc, ("clear_cache", "clearCache"))
        return True
    except Exception:
        return False


def _raw_child_count(acc) -> int:
    try:
        return int(_call_first(acc, ("get_child_count", "get_childCount"), default=0) or 0)
    except (TypeError, ValueError):
        return 0


def _child_count(acc) -> int:
    """Child count, replacing a wedged app bus once when the read returns -1.

    -1 is libatspi's D-Bus failure, not an empty parent. ``range(-1)`` is
    empty, which is how a file chooser or a ``Dialog.run()`` dialog became a
    single disabled group.
    """
    count = _raw_child_count(acc)
    if count < 0 and _revive_application(acc):
        count = _raw_child_count(acc)
        key = _last_revive_key
        if key:
            # A working replacement may be wedged by the next action, so the
            # block stays short. A replacement that still reads -1 is a dead
            # app: keep the block long enough to cover the rest of the walk.
            _revive_block_until[key] = time.monotonic() + (2.0 if count < 0 else 0.05)
    return count


def _with_table_body(node, kids: list) -> list:
    """Append a GTK table's body cells when the children are only headers.

    A file chooser's file list is a table whose AT-SPI children are the
    column headers. ``Table.get_n_rows`` is the file rows. Cells already
    present as children are not added again. A table that already exposes
    row children is unchanged, and a table with no rows stays header-only.
    """
    if _role_name(node) not in {"table", "tree table"}:
        return kids
    rows = _call_first(node, ("get_n_rows",), default=None)
    if isinstance(rows, bool) or not isinstance(rows, int) or rows <= 0:
        return kids
    if kids and any("header" not in _role_name(child) for child in kids):
        return kids
    seen = {id(child) for child in kids}
    body = list(kids)
    for row in range(min(rows, _MAX_CHILDREN_FETCH)):
        cell = _call_first(node, ("get_accessible_at",), row, 0)
        if cell is None or id(cell) in seen:
            continue
        seen.add(id(cell))
        body.append(cell)
    return body


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


def _gecko_app(acc) -> bool:
    """True when ``acc`` belongs to Firefox. Chromium and GTK are not."""
    if acc is None:
        return False
    app = _call_first(acc, ("get_application", "getApplication")) or acc
    toolkit = (_call_first(app, ("get_toolkit_name", "getToolkitName"), default="") or "").lower()
    if "gecko" in toolkit:
        return True
    name = (_call_first(app, ("get_name", "getName"), default="") or "").lower()
    return name == "firefox" or name.startswith("firefox ")


def _frame_hierarchy_showing(node) -> bool:
    """Whether ``node`` or a document/frame ancestor has STATE_SHOWING.

    Firefox background tabs and the preloaded New Tab page are VISIBLE and
    carry the content area's bounds. They are not SHOWING. The active tab's
    document web, its internal frame, and the scroll pane around that frame
    are SHOWING.
    """
    current = node
    seen: set[int] = set()
    for _ in range(8):
        if current is None or id(current) in seen:
            return False
        seen.add(id(current))
        role = _role_name(current)
        if role in {"document web", "document frame", "internal frame", "scroll pane"}:
            if _state_has(current, "SHOWING"):
                return True
        if role in {"frame", "window", "application"}:
            break
        parent = _parent_of(current)
        if parent is None or parent is current:
            break
        current = parent
    return False


def _selected_tab_name(node) -> str | None:
    """The selected page tab in ``node``'s top-level frame, or None."""
    current = node
    frame = None
    seen: set[int] = set()
    for _ in range(16):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        role = _role_name(current)
        if role in {"frame", "window"}:
            frame = current
            break
        parent = _parent_of(current)
        if parent is None or parent is current:
            break
        current = parent
    if frame is None:
        return None
    return _selected_tab_under(frame, 0)


def _selected_tab_under(node, depth: int) -> str | None:
    if node is None or depth > 5:
        return None
    role = _role_name(node)
    if role == "page tab" and _state_has(node, "SELECTED"):
        label = _node_name(node)
        return label or None
    if depth and role in {"document web", "internal frame", "document frame"}:
        return None
    count = min(_child_count(node), 30)
    for index in range(count):
        found = _selected_tab_under(_child_at(node, index), depth + 1)
        if found:
            return found
    return None


def _names_match_tab(title: str, tab: str) -> bool:
    """Whether a document title is the selected tab's label.

    Equality is the match. A longer title that starts with a tab label of at
    least 8 characters still matches a truncated tab. ``New Tab`` does not
    match ``Form Probe``.
    """
    left = title.strip().casefold()
    right = tab.strip().casefold()
    if not left or not right:
        return True
    if left == right:
        return True
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    return len(short) >= 8 and long.startswith(short)


def _enclosing_tab_document(doc):
    """The tab's own document web, not an iframe document nested under it.

    Firefox gives a child frame the ``<title>`` of the page inside the
    frame. That name is not the selected tab. The title match belongs to
    the outermost document web under the window. An iframe's document web
    has that outer document as an ancestor.
    """
    current = doc
    outer = doc
    seen: set[int] = set()
    for _ in range(24):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        role = _role_name(current)
        if role in {"frame", "window", "application"}:
            break
        if role == "document web":
            outer = current
        parent = _parent_of(current)
        if parent is None or parent is current:
            break
        current = parent
    return outer


def _gecko_web_document_on_screen(doc) -> bool:
    """True when this document web is the one the user can see.

    The frame hierarchy has to be SHOWING. When the window has a selected
    page tab, the tab's own document title has to be that tab. A preloaded
    New Tab that is not the selected tab is not on screen, and neither is a
    background tab whose frame is only VISIBLE. A child frame's document
    keeps its own ``<title>``. That title is not compared to the tab: the
    match uses the enclosing tab document, so a titled iframe in the active
    tab stays, and one in a background tab does not.
    """
    if not _frame_hierarchy_showing(doc):
        return False
    tab = _selected_tab_name(doc)
    if not tab:
        return True
    owner = _enclosing_tab_document(doc)
    return _names_match_tab(_node_name(owner), tab)


def _has_browser_frame(node, depth: int = 0) -> bool:
    """True when ``node`` is or directly wraps a browser document frame."""
    if node is None or depth > 2:
        return False
    role = _role_name(node)
    if role in {"internal frame", "document web"}:
        return True
    count = min(_child_count(node), 6)
    for index in range(count):
        if _has_browser_frame(_child_at(node, index), depth + 1):
            return True
    return False


def _hidden_gecko_browser(node) -> bool:
    """True when ``node`` is a Firefox document the user is not looking at.

    A scroll pane that is not SHOWING and wraps an internal frame is a
    background tab or the preloaded New Tab browser. An internal frame
    without SHOWING is the same. A document web is hidden when its frame
    hierarchy is not SHOWING, or when the tab that encloses it is not the
    selected tab. An iframe document whose title differs from the tab is
    not hidden for that reason.
    """
    role = _role_name(node)
    if role == "scroll pane":
        if _state_has(node, "SHOWING"):
            return False
        return _has_browser_frame(node)
    if role == "internal frame":
        if _state_has(node, "SHOWING"):
            return False
        # A frame inside the showing page is an iframe, not a background tab.
        return not _nested_in_document(node)
    if role == "document web":
        return not _gecko_web_document_on_screen(node)
    return False


def accessible_gone(acc) -> bool:
    """True when ``acc`` is missing or Firefox has destroyed it.

    A background-tab node is still alive: it has a role and is not DEFUNCT.
    A node removed from the document is DEFUNCT, or it no longer answers
    with a role. That one is gone, not merely hidden.
    """
    if acc is None:
        return True
    try:
        if _state_has(acc, "DEFUNCT"):
            return True
    except Exception:
        return True
    try:
        role = _role_name(acc)
    except Exception:
        return True
    return not bool(role)


def hidden_named_target(root, ax_role: str, title: str) -> bool:
    """True when a live node of ``ax_role`` and ``title`` sits in a hidden document.

    The snapshot prunes that document, so a ref into it fails to rematch and
    would otherwise be reported as stale. A match that is showing is not this
    case: the element really left the tree the ref was issued against. A
    DEFUNCT node is skipped. GTK and Chromium trees are not walked.
    """
    if root is None or not ax_role or not title or not _gecko_app(root):
        return False
    hidden = False
    showing = False
    queue = [root]
    seen: set[int] = set()
    examined = 0
    while queue and examined < 400:
        node = queue.pop(0)
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        examined += 1
        if accessible_gone(node):
            continue
        mapped = _ROLE.get(_role_name(node), "")
        if mapped == ax_role and _node_name(node) == title:
            if hidden_web_target(node):
                hidden = True
            else:
                showing = True
            if hidden and showing:
                break
        count = min(_child_count(node), 40)
        for index in range(count):
            queue.append(_child_at(node, index))
    return hidden and not showing


def hidden_web_target(acc) -> bool:
    """True when ``acc`` sits in a Firefox document that is not showing.

    False for GTK, Chromium, and Firefox chrome (the tab strip, the toolbar).
    A click on a link in a background tab is this case: the link's document
    is not the selected, showing one.
    """
    if acc is None or not _gecko_app(acc):
        return False
    node = acc
    seen: set[int] = set()
    for _ in range(32):
        if node is None or id(node) in seen:
            return False
        seen.add(id(node))
        role = _role_name(node)
        if role == "document web":
            return not _gecko_web_document_on_screen(node)
        if role == "internal frame" and not _state_has(node, "SHOWING"):
            return True
        parent = _parent_of(node)
        if parent is None or parent is node:
            break
        node = parent
    return False


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
    exposes a scroll bar. The Linux driver then tries a DOM scroll or a
    wheel at the element box, and keeps the step only when the position
    changed.
    """
    acc = _accessible_at_point(int(x), int(y))
    if acc is None:
        return False
    return scroll_by_pixels(acc, dx=dx, dy=dy)
