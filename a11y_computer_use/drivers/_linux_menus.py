"""Linux menu bars over AT-SPI.

GTK publishes a menu-bar entry as role ``menu``. Its rows are either direct
children or the children of one popup (untitled, or the entry's only child).
Chromium and Electron publish the bar entry as a push button with no children.
Pressing it opens a menu in another window. Qt Quick uses actionless nested
menu-bar entries that open a sibling popup through a measured pointer click.
Item names include the accelerator
(``New Note Ctrl+N``). An exact title wins; otherwise the name with that
suffix removed matches.

The shared macOS walker looks for an ``AXMenu`` child of each item, which does
not match that tree, so the walk lives here. Title matching, ellipsis, and
prefixes still go through `menus.match_title`.

Listing reads the tree and does not press when the rows are already there. A
menu that is empty until it is shown is opened and then closed. Pressing opens
each level and activates the leaf with the item's ``click`` action. A menu
whose only action has an empty name (LibreOffice menu-bar entries) is opened
with that action. An open menu is one whose item is ``SELECTED`` or whose
popup is ``SHOWING``. Java ATK leaves ``SELECTED`` on a menu-bar entry after
the popup has closed; that entry is open only when a popup or an item is
``SHOWING``. A leaf press closes a popup that is still showing, and does not
send Escape when a dialog is already showing. Close sends Escape and succeeds
only after that menu is gone. A shortcut is the accelerator in the AT-SPI
binding: a tagged chord, or
a bare key name such as F11, not the Alt mnemonic letter. A binding whose accelerator
field is empty (``a;<Alt>v:a;``, ``f;<Alt>f;``, ``_F;;``) has no shortcut;
the underlined letter is ``mnemonic``.

This module does not import ``gi``. Tests drive it with fake accessibles.
``file_dialog`` is not implemented here.
"""

from __future__ import annotations

import re
import time

from a11y_computer_use import menus
from a11y_computer_use.menus import MenuItem
from a11y_computer_use.schema import ComputerUseError, ErrorCode

_MENU_ROLES = frozenset({"menu", "popup menu"})
_ITEM_ROLES = frozenset({"menu item", "check menu item", "radio menu item"})
_PRESS_ACTIONS = frozenset({"click", "press", "activate", "do default", "open", "showmenu", "menu"})
_MOD_ORDER = ("ctrl", "alt", "shift", "super")
_MOD_TAGS = {
    "control": "ctrl",
    "ctrl": "ctrl",
    "primary": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "mod1": "alt",
    "super": "super",
    "meta": "super",
    "mod4": "super",
}
_TAG = re.compile(r"<([^>]+)>")
# Chromium appends the accelerator to the accessible name ("New Note Ctrl+N",
# "Save As Ctrl+Shift+S", "Fullscreen F11"). The suffix is not the title.
_ACCEL_SUFFIX = re.compile(
    r"\s+(?:(?:(?:ctrl|control|alt|option|shift|super|meta|cmd|command|primary)\+)+[^\s]+|f\d{1,2})$",
    re.IGNORECASE,
)
# Page content is not a menu popup. A Chromium menu opens in its own window,
# which a breadth-first walk reaches before the document's children.
_POPUP_STOP_ROLES = frozenset({"document web", "document frame", "internal frame"})


def _settle(seconds: float) -> None:
    time.sleep(seconds)


def _call(obj: object, names: str | tuple[str, ...], *args: object, default: object = None) -> object:
    if obj is None:
        return default
    if isinstance(names, str):
        names = (names,)
    for name in names:
        method = getattr(obj, name, None)
        if method is None:
            continue
        try:
            return method(*args)
        except Exception:
            return default
    return default


def _role(node: object) -> str:
    return str(_call(node, "get_role_name", default="") or "").lower()


def _drop_event_cache(node: object) -> None:
    """Drop one item's event-thread property cache before a name or state read."""
    if getattr(node, "clear_cache_single", None) is None:
        return
    from a11y_computer_use.drivers._atspi import _drop_cached_properties

    _drop_cached_properties(node)


def _name(node: object) -> str:
    if _role(node) == "separator":
        return ""
    _drop_event_cache(node)
    return str(_call(node, "get_name", default="") or "")


def _raw_children(node: object) -> list[object]:
    count = _call(node, "get_child_count", default=None)
    if count is None:
        kids = getattr(node, "children", None)
        return list(kids) if isinstance(kids, (list, tuple)) else []
    try:
        total = int(count)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return []
    out: list[object] = []
    for index in range(min(total, 200)):
        child = _call(node, "get_child_at_index", index, default=None)
        if child is not None:
            out.append(child)
    return out


def _states(node: object) -> set[str] | None:
    direct = getattr(node, "states", None)
    if isinstance(direct, (set, frozenset, list, tuple)):
        return {str(item).lower() for item in direct}
    return None


def _has_state(node: object, *names: str) -> bool:
    direct = _states(node)
    if direct is not None:
        return any(name.lower() in direct for name in names)
    _drop_event_cache(node)
    state_set = _call(node, "get_state_set")
    if state_set is None or not hasattr(state_set, "contains"):
        return False
    enum = None
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        enum = Atspi.StateType
    except Exception:
        enum = None
    # macOS and Windows CI import this module without the Atspi typelib.
    # A fake state set still answers contains(); the name is the token.
    # Linux uses the StateType member the real set expects.
    for name in names:
        member = getattr(enum, name.upper(), None) if enum is not None else name
        if member is not None and bool(_call(state_set, "contains", member, default=False)):
            return True
    return False


def _enabled(node: object) -> bool:
    direct = _states(node)
    if direct is not None:
        return "sensitive" in direct
    if _call(node, "get_state_set") is None:
        return True
    return _has_state(node, "SENSITIVE")


def _sole_popup(raw: list[object]) -> object | None:
    """A single child menu that exists only to hold this menu's rows."""
    menus_found = [child for child in raw if _role(child) in _MENU_ROLES]
    items = [child for child in raw if _role(child) in _ITEM_ROLES]
    if items or len(menus_found) != 1:
        return None
    return menus_found[0]


def entries_of(node: object, depth: int = 0) -> list[object]:
    """Titled rows inside ``node``. A menu bar is not descended into.

    One child menu and no menu items means the rows live in that popup, whether
    the popup is untitled or repeats the parent's name.
    """
    if depth > 6:
        return []
    raw = [child for child in _raw_children(node) if _role(child) != "separator"]
    if _role(node) != "menu bar":
        popup = _sole_popup(raw)
        if popup is not None:
            return entries_of(popup, depth + 1)
    return [child for child in raw if _name(child).strip()]


def _strip_accelerator_suffix(title: str) -> str:
    """Title without a trailing accelerator Chromium glues onto the name.

    ``New Note Ctrl+N`` becomes ``New Note``. A name that is only the chord
    stays as it is, so an exact match on the full string still wins.
    """
    text = " ".join((title or "").split())
    match = _ACCEL_SUFFIX.search(text)
    if match is None:
        return text
    stripped = text[: match.start()].strip()
    return stripped or text


def _has_submenu(node: object) -> bool:
    """True when activating ``node`` opens another menu.

    GTK bar entries are role ``menu``, or they already have rows. A Chromium
    menu-bar entry is a push button (a nested menu bar in Qt Quick) whose
    parent is the menu bar and whose popup does not exist until pressed. ``HAS_POPUP`` or
    ``EXPANDABLE`` is that same fact on a menu item.
    """
    if _role(node) in _MENU_ROLES:
        return True
    if entries_of(node):
        return True
    if _has_state(node, "HAS_POPUP", "EXPANDABLE"):
        return True
    if _role(node) in {"push button", "toggle button", "menu bar"}:
        parent = _call(node, ("get_parent", "getParent"))
        if _role(parent) == "menu bar":
            return True
    return False


def _render_chord(chosen: str) -> str | None:
    """One accelerator token (``<Primary>n``, ``F11``, ``Delete``) as ``ctrl+n``."""
    found: list[str] = []

    def repl(match: re.Match[str]) -> str:
        mapped = _MOD_TAGS.get(match.group(1).strip().lower())
        if mapped and mapped not in found:
            found.append(mapped)
        return ""

    key = _TAG.sub(repl, chosen).strip().lower()
    if not found and "+" in key:
        bits = [bit.strip().lower() for bit in key.split("+") if bit.strip()]
        if bits:
            key = bits[-1]
            for bit in bits[:-1]:
                mapped = _MOD_TAGS.get(bit)
                if mapped and mapped not in found:
                    found.append(mapped)
    if not key or key.isdigit():
        return None
    ordered = [name for name in _MOD_ORDER if name in found]
    return "+".join([*ordered, key])


def _gtk_mnemonic_fields(raw: str) -> list[str] | None:
    """GTK's ``mnemonic;alt-path;accelerator`` fields, keeping an empty accelerator.

    ``n;37;4`` is a key name plus keycode digits, not this form. A tag, a
    colon, or a leading underscore marks the GTK form. The accelerator is
    the third field. ``f;<Alt>f;`` and ``_F;;`` leave it empty.
    """
    if ";" not in raw:
        return None
    fields = raw.split(";")
    if not any("<" in part or ":" in part or part.strip().startswith("_") for part in fields):
        return None
    return fields


def _lone_letter(rendered: str | None) -> bool:
    return bool(rendered) and "+" not in rendered and len(rendered) == 1


def shortcut_from_binding(raw: str | None) -> str | None:
    """Render an AT-SPI key binding (``<Control>o``, ``<Primary><Shift>S``) as ``ctrl+o``.

    GTK joins three fields with ``;``: the mnemonic, the Alt mnemonic path,
    then the accelerator. Mousepad's New item is ``n;<Alt>f:n;<Primary>n``,
    so the shortcut is ``ctrl+n``, not the Alt path. Statusbar is
    ``a;<Alt>v:a;``: the accelerator field is empty, so there is no shortcut.
    ``f;<Alt>f;`` and ``_F;;`` are the same. A lone letter in that field is
    the mnemonic again, not an accelerator.

    A binding with no semicolon is the accelerator itself (``<Primary>n``,
    ``<Alt>F4``). A bare name is the accelerator when it is not the GTK
    form: Fullscreen's third field is ``F11``. ``n;37;4`` has no mnemonic
    tag, so the key name remains ``n``. Digit-only segments are ignored.
    """
    if not raw or not str(raw).strip():
        return None
    text = str(raw)
    fields = _gtk_mnemonic_fields(text)
    if fields is not None:
        accel = fields[2].strip() if len(fields) >= 3 else ""
        if not accel:
            return None
        rendered = _render_chord(accel)
        if _lone_letter(rendered):
            return None
        return rendered
    parts = [part.strip() for part in text.split(";") if part.strip()]
    tagged = [part for part in parts if "<" in part]
    chords = [part for part in tagged if ":" not in part]
    chosen = chords[-1] if chords else None
    if chosen is None:
        plain = [part for part in parts if "<" not in part and ":" not in part and not part.isdigit()]
        named = [part for part in plain if len(part) > 1]
        chosen = (named[-1] if named else plain[-1]) if plain else None
    if not chosen:
        return None
    return _render_chord(chosen)


def mnemonic_from_binding(raw: str | None) -> str | None:
    """The underlined mnemonic letter, or None when the binding has none.

    ``f;<Alt>f;`` and ``<Alt>f;<Alt>f;`` are ``f``. ``_F;;`` is the label
    form of the same letter. The Alt path is not the shortcut.
    """
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip()
    fields = _gtk_mnemonic_fields(text)
    if fields is None:
        if text.startswith("_") and len(text) >= 2 and text[1].isalpha():
            return text[1].lower()
        return None
    first = fields[0].strip()
    if len(first) == 1 and first.isalpha():
        return first.lower()
    if first.startswith("_") and len(first) >= 2 and first[1].isalpha():
        return first[1].lower()
    rendered = _render_chord(first)
    if rendered is not None and rendered.startswith("alt+") and _lone_letter(rendered[4:]):
        return rendered[-1]
    return None


def _key_binding(node: object) -> str:
    explicit = getattr(node, "key", None)
    if explicit:
        return str(explicit)
    action = _call(node, ("get_action_iface", "get_action"))
    if action is None:
        return ""
    return str(_call(action, ("get_key_binding", "get_keybinding"), 0, default="") or "")


def _checked(node: object) -> bool | None:
    """True, false, or None when the item is not a check at all.

    GTK uses the roles ``check menu item`` and ``radio menu item``. Qt keeps
    the role ``menu item``. A bridge that sets ``CHECKABLE`` reports false
    when the action is off and true when ``CHECKED`` is also set. Qt 6.4
    publishes ``CHECKED`` when the action is on and does not publish
    ``CHECKABLE``, so an on item is true and an off item stays None. A plain
    item stays None.
    """
    role = _role(node)
    checkable = role in ("check menu item", "radio menu item") or _has_state(node, "CHECKABLE")
    checked = _has_state(node, "CHECKED")
    if not checkable and not checked:
        return None
    return checked


def _describe(node: object) -> MenuItem:
    binding = _key_binding(node)
    return MenuItem(
        title=_name(node),
        enabled=_enabled(node),
        shortcut=shortcut_from_binding(binding),
        submenu=_has_submenu(node),
        checked=_checked(node),
        mnemonic=mnemonic_from_binding(binding),
    )


# A leaf DoAction that does not answer within this long was delivered and then
# swallowed. at-spi2-atk queues the method return and then runs the action;
# Gtk.Dialog.run() can hold that reply until the client gives up. A quicker
# false is the application refusing the action.
LEAF_DISPATCH_S = 0.30


def classify_leaf_press(result: object, *, failed: bool, elapsed: float) -> str:
    """``ok``, ``dispatched``, or ``refused`` for one leaf action result.

    True is the reply. A timeout, or a false that took at least
    `LEAF_DISPATCH_S`, is ``dispatched``: the action was already sent. A
    quick false or a quick error is ``refused``.
    """
    if result is True and not failed:
        return "ok"
    if elapsed >= LEAF_DISPATCH_S:
        return "dispatched"
    return "refused"


def _action_name(action: object, index: int) -> str:
    name_method = getattr(action, "get_action_name", None) or getattr(action, "get_name", None)
    if name_method is None:
        return ""
    return str(name_method(index) or "").lower()


def _do_action(action: object, index: int) -> bool:
    do = getattr(action, "do_action", None) or getattr(action, "doAction", None)
    if do is None:
        return False
    return bool(do(index))


def _invoke_press(node: object) -> bool:
    """Run the node's activating action. D-Bus errors propagate.

    ``_press`` turns those errors into false so a parent menu that cannot
    open still reports a refusal. The leaf classifier needs the exception
    and the elapsed time to tell a timeout from a real refusal.

    A named press action (``click``, ``press``, and the rest of
    `_PRESS_ACTIONS`) wins. LibreOffice menu-bar entries are role ``menu``
    and publish exactly one action whose name is the empty string.
    ``do_action(0)`` on that node opens the menu. A single unnamed action
    on any other role is not pressed, and a menu with more than one action
    is not guessed.
    """
    getter = None
    for name in ("get_action_iface", "get_action"):
        method = getattr(node, name, None)
        if method is not None:
            getter = method
            break
    if getter is None:
        return False
    action = getter()
    if action is None:
        return False
    count_method = getattr(action, "get_n_actions", None) or getattr(action, "get_nActions", None)
    if count_method is None:
        return False
    try:
        total = int(count_method() or 0)
    except (TypeError, ValueError):
        return False
    for index in range(total):
        if _action_name(action, index) not in _PRESS_ACTIONS:
            continue
        return _do_action(action, index)
    # LibreOffice: role menu, one action, name "". Items inside still use click.
    if total == 1 and _role(node) == "menu" and _action_name(action, 0) == "":
        return _do_action(action, 0)
    return False


def _quick_menu_entry(node: object) -> bool:
    """A named nested menu bar, the Qt Quick MenuBarItem shape."""
    return (
        _role(node) == "menu bar" and bool(_name(node).strip())
        and _role(_call(node, ("get_parent", "getParent"))) == "menu bar"
    )


def _quick_popup(node: object) -> object | None:
    """A showing popup in this entry's own window, outside the menu bar."""
    frame = _call(node, ("get_parent", "getParent"))
    for _ in range(12):
        if frame is None:
            return None
        if _role(frame) in {"frame", "window", "dialog"}:
            return next(
                (menu for menu in _popup_menus(frame)
                 if _has_state(menu, "SHOWING") and not _contains(menu, node)),
                None,
            )
        frame = _call(frame, ("get_parent", "getParent"))
    return None


def _press_quick_entry(node: object) -> bool:
    """Click measured native bounds and verify popup visibility changed."""
    from a11y_computer_use.drivers import _atspi, _linux_input

    if not _enabled(node) or not _atspi._x11_keys_available():
        return False
    pos, size = _atspi._extents(node)
    if pos is None or size is None or size[0] <= 0 or size[1] <= 0:
        return False
    was_open = _quick_popup(node) is not None
    _linux_input.click(int(pos[0] + size[0] / 2), int(pos[1] + size[1] / 2))
    for _ in range(3):
        _settle(menus.MENU_OPEN_SETTLE_S)
        if (_quick_popup(node) is not None) != was_open:
            return True
    return False


def _press(node: object) -> bool:
    try:
        if _quick_menu_entry(node):
            action = _call(node, ("get_action_iface", "get_action"))
            if not _call(action, ("get_n_actions", "get_nActions"), default=0):
                return _press_quick_entry(node)
        return _invoke_press(node)
    except Exception:
        return False


def _node_key(node: object) -> object:
    """Identity of one accessible across recreated Python wrappers.

    The AT-SPI bus name and object path when both are on the GObject.
    Otherwise ``id()`` of this wrapper. A collected wrapper's ``id()`` can
    be reused for a different accessible, so a walk that falls back to
    ``id()`` has to keep every wrapper alive until it finishes.
    """
    from a11y_computer_use.drivers._atspi import _atspi_wrapper_key

    stable = _atspi_wrapper_key(node)
    if stable is not None:
        return stable
    return id(node)


def find_menu_bar(root: object) -> object | None:
    """The menu bar under ``root``, preferring one inside an ACTIVE frame.

    ``get_child_at_index`` returns a new proxy. The proxy is reachable only
    from this walk, so a leaf that has already been processed can be
    collected before a later node is created. CPython then reuses the leaf's
    ``id()``. A seen-set of those ids skips the new node. LibreOffice Writer
    publishes the bar at ``application > frame > panel > panel > root pane >
    menu bar``. The root pane is created after toolbar leaves have been
    collected, the skip drops it, and the bar is never reached.

    Visited proxies stay referenced for the whole search, and the seen-set
    and parent links use `_node_key` rather than a bare ``id()``.
    """
    if root is None:
        return None
    if _role(root) == "menu bar":
        return root
    found: list[object] = []
    parent: dict[object, object] = {}
    # Keep every proxy alive. Dropping one lets its id be reused for a node
    # that has not been walked; the seen-set would then skip that node.
    pinned: list[object] = [root]
    queue = [root]
    seen: set[object] = set()
    walked = 0
    while queue and walked < 400:
        node = queue.pop(0)
        identity = _node_key(node)
        if identity in seen:
            continue
        seen.add(identity)
        walked += 1
        if _role(node) == "menu bar":
            found.append(node)
            continue
        for child in _raw_children(node)[:80]:
            pinned.append(child)
            parent[_node_key(child)] = node
            queue.append(child)
    if not found:
        return None

    def in_active(node: object) -> bool:
        current: object | None = node
        guard: set[object] = set()
        while current is not None and len(guard) < 64:
            if _has_state(current, "ACTIVE"):
                return True
            key = _node_key(current)
            if key in guard:
                return False
            guard.add(key)
            current = parent.get(key)
        return False

    for bar in found:
        if in_active(bar):
            return bar
    return found[0]


def _require_bar(app: object) -> object:
    bar = find_menu_bar(app)
    if bar is None:
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            "the application exposes no menu bar through accessibility",
            detail={"hint": (
                "Focus the app and use desktop_snapshot. Header-bar apps may expose a "
                "Menu or Main Menu button instead of a menu bar: click its snapshot ref, "
                "then take another snapshot and click the exposed button/checkbox refs. "
                "If the popover is already open, use its current snapshot refs directly; "
                "menu paths do not support these popovers. Do not use Escape to dismiss "
                "Eye of GNOME's menu: Escape can quit the app."
            )},
        )
    return bar


def _match(entries: list[object], wanted: str, depth: int) -> tuple[object, str]:
    """The entry named ``wanted``.

    An exact normalized title wins, including a name that still has its
    accelerator. Otherwise one title whose accelerator suffix was removed
    equals ``wanted``. Unique prefixes and ellipsis still go through
    `menus.match_title`.
    """
    titles = [_name(node) for node in entries]
    want = menus.normalize_title(wanted)
    normalized = [menus.normalize_title(title) for title in titles]
    exact = [index for index, title in enumerate(normalized) if title == want]
    if len(exact) == 1:
        return entries[exact[0]], titles[exact[0]]
    if len(exact) > 1:
        raise ComputerUseError(
            ErrorCode.APP_NOT_FOUND,
            f"menu path component {depth + 1} ({wanted!r}): {wanted!r} matches {len(exact)} items with the same title",
            detail={"component": wanted, "available": titles},
        )
    stripped = [menus.normalize_title(_strip_accelerator_suffix(title)) for title in titles]
    accel = [
        index
        for index, title in enumerate(stripped)
        if title == want and title != normalized[index]
    ]
    if len(accel) == 1:
        return entries[accel[0]], titles[accel[0]]
    if len(accel) > 1:
        options = ", ".join(repr(titles[index]) for index in accel)
        raise ComputerUseError(
            ErrorCode.APP_NOT_FOUND,
            f"menu path component {depth + 1} ({wanted!r}): {wanted!r} matches {len(accel)} items: {options}",
            detail={"component": wanted, "available": titles},
        )
    try:
        index = menus.match_title(wanted, titles)
    except LookupError as exc:
        raise ComputerUseError(
            ErrorCode.APP_NOT_FOUND,
            f"menu path component {depth + 1} ({wanted!r}): {exc}",
            detail={"component": wanted, "available": titles},
        ) from None
    return entries[index], titles[index]


def _contains(root: object, target: object) -> bool:
    """True when ``target`` is ``root`` or one of its descendants."""
    if root is None or target is None:
        return False
    want = _node_key(target)
    pinned: list[object] = [root]
    queue = [root]
    seen: set[object] = set()
    walked = 0
    while queue and walked < 200:
        node = queue.pop(0)
        identity = _node_key(node)
        if identity in seen:
            continue
        seen.add(identity)
        walked += 1
        if identity == want:
            return True
        for child in _raw_children(node)[:40]:
            pinned.append(child)
            queue.append(child)
    return False


def _popup_menus(app: object) -> list[object]:
    """Menus under ``app`` that already have rows. The page is not walked."""
    found: list[object] = []
    pinned: list[object] = [app]
    queue = [app]
    seen: set[object] = set()
    walked = 0
    while queue and walked < 400 and len(found) < 8:
        node = queue.pop(0)
        identity = _node_key(node)
        if identity in seen:
            continue
        seen.add(identity)
        walked += 1
        role = _role(node)
        if role in _MENU_ROLES and entries_of(node):
            found.append(node)
        if role in _POPUP_STOP_ROLES:
            continue
        for child in _raw_children(node)[:80]:
            pinned.append(child)
            queue.append(child)
    return found


def _menu_showing(menu: object) -> bool:
    if _has_state(menu, "SHOWING"):
        return True
    parent = _call(menu, ("get_parent", "getParent"))
    for _ in range(4):
        if parent is None:
            return False
        if _role(parent) in {"window", "frame", "dialog"} and _has_state(parent, "SHOWING"):
            return True
        parent = _call(parent, ("get_parent", "getParent"))
    return False


def _opened_menu(app: object, node: object) -> object:
    """The container whose rows are ``node``'s submenu.

    GTK rows are children of ``node``. A Chromium menu-bar button has none
    until it is pressed, and the popup is another window. A menu that still
    contains ``node`` is the parent popup, not the one this press opened.
    """
    if entries_of(node):
        return node
    if _quick_menu_entry(node):
        return _quick_popup(node) or node
    found = [menu for menu in _popup_menus(app) if not _contains(menu, node)]
    if not found:
        return node
    showing = [menu for menu in found if _menu_showing(menu)]
    return (showing or found)[-1]


def _walk(app: object, path: str) -> list[object]:
    """Menus and the final row along ``path``, without pressing."""
    components = menus.parse_path(path)
    container: object = _require_bar(app)
    nodes: list[object] = []
    for depth, wanted in enumerate(components):
        node, _title = _match(entries_of(container), wanted, depth)
        nodes.append(node)
        if depth < len(components) - 1 and not _has_submenu(node):
            raise ComputerUseError(
                ErrorCode.APP_NOT_FOUND,
                f"{_name(node)!r} has no submenu",
                detail={"component": wanted},
            )
        container = node if depth == len(components) - 1 else _opened_menu(app, node)
    return nodes


def _java_toolkit(node: object) -> bool:
    """True when ``node`` belongs to the Java ATK wrapper.

    The toolkit name is ``JAVA``. A fake tree with no toolkit is not Java,
    so GTK's SELECTED-means-open rule stays in place.
    """
    try:
        from a11y_computer_use.drivers._atspi import _toolkit_name
    except Exception:
        return False
    try:
        return "java" in _toolkit_name(node)
    except Exception:
        return False


_DIALOG_ROLES = frozenset({
    "dialog", "alert", "file chooser", "color chooser", "font chooser",
})


def _popup_or_item_showing(node: object, depth: int = 0) -> bool:
    """True when a popup or a menu item under ``node`` is SHOWING.

    The bar entry's own SHOWING state is the label on the menu bar, not an
    open popup. Only a child menu or a child item counts.
    """
    if depth > 5:
        return False
    for child in _raw_children(node):
        role = _role(child)
        if role in _MENU_ROLES or role in _ITEM_ROLES:
            if _has_state(child, "SHOWING"):
                return True
        if role in _MENU_ROLES and _popup_or_item_showing(child, depth + 1):
            return True
    return False


def _tree_has_showing_dialog(node: object, depth: int = 0) -> bool:
    """True when a dialog under ``node`` is SHOWING.

    Escape would go to that dialog, not to a menu on the window behind it.
    A dialog that is only in the tree and not SHOWING is not this case.
    """
    if node is None or depth > 8:
        return False
    role = _role(node)
    if (role in _DIALOG_ROLES or role.endswith(" dialog")) and _has_state(node, "SHOWING"):
        return True
    for child in _raw_children(node)[:40]:
        if _tree_has_showing_dialog(child, depth + 1):
            return True
    return False


def escape_reaches_menu(app: object) -> bool:
    """False when Escape would dismiss a showing dialog instead of the menu."""
    return not _tree_has_showing_dialog(app)


def _is_open(node: object) -> bool:
    """True when this menu is popped open.

    The bar entry itself is SHOWING whenever its label is on screen, so that
    state is not an open menu. SELECTED on the entry, or SHOWING/SELECTED on
    its popup child, is.

    Java ATK keeps SELECTED on the bar entry after the popup closes. That
    leftover bit is not an open menu: the entry counts only when a popup or
    an item is SHOWING.
    """
    if _quick_menu_entry(node):
        return _quick_popup(node) is not None
    if _java_toolkit(node):
        return _popup_or_item_showing(node)
    if _has_state(node, "SELECTED"):
        return True
    return any(
        _role(child) in _MENU_ROLES and (_has_state(child, "SHOWING") or _has_state(child, "SELECTED"))
        for child in _raw_children(node)
    )


def _mnemonic_letter(node: object) -> str:
    """The menu item's mnemonic letter, from the AT-SPI key binding.

    GTK stores ``f;<Alt>f`` or ``<Alt>f;<Alt>f;`` on a File menu. ``_F;;`` is
    the same letter from the label. A binding with no mnemonic letter is empty.
    """
    return mnemonic_from_binding(_key_binding(node)) or ""


def menu_mnemonic(app: object, letter: str) -> str | None:
    """Title of the top-level menu whose mnemonic is ``letter``, or None.

    The key binding wins. When no binding names the letter, a single top-level
    title that starts with it is used (Search for ``s``). Two titles that
    share that initial are not a guess.
    """
    wanted = (letter or "").strip().lower()
    if len(wanted) != 1 or not wanted.isalpha():
        return None
    try:
        bar = _require_bar(app)
    except ComputerUseError:
        return None
    bound: list[str] = []
    firsts: list[str] = []
    for node in entries_of(bar):
        title = _name(node).strip()
        if not title:
            continue
        if _mnemonic_letter(node) == wanted:
            bound.append(title)
        if title[0].lower() == wanted:
            firsts.append(title)
    if len(bound) == 1:
        return bound[0]
    if not bound and len(firsts) == 1:
        return firsts[0]
    return None


def menu_items(app: object, path: str | None, *, settle=None) -> list[dict[str, object]]:
    """Items of the menu at ``path`` (top-level menus when ``path`` is empty)."""
    settle = settle or _settle
    bar = _require_bar(app)
    if not path or not str(path).strip():
        return [_describe(node).to_dict() for node in entries_of(bar)]
    try:
        nodes = _walk(app, path)
    except ComputerUseError as exc:
        if exc.code is not ErrorCode.APP_NOT_FOUND or "not a menu" in exc.message:
            raise
        nodes = None
    if nodes is not None:
        target = nodes[-1]
        if not _has_submenu(target):
            raise ComputerUseError(
                ErrorCode.APP_NOT_FOUND,
                f"{_name(target)!r} is an item, not a menu",
                detail={"path": path, "hint": "list its parent, or press it"},
            )
        rows = entries_of(_opened_menu(app, target))
        if rows:
            return [_describe(node).to_dict() for node in rows]
    top = _open_branch(app, path, settle)
    try:
        target = _walk(app, path)[-1]
        if not _has_submenu(target):
            raise ComputerUseError(
                ErrorCode.APP_NOT_FOUND,
                f"{_name(target)!r} is an item, not a menu",
                detail={"path": path, "hint": "list its parent, or press it"},
            )
        return [_describe(node).to_dict() for node in entries_of(_opened_menu(app, target))]
    finally:
        _press(top)


def _open_branch(app: object, path: str, settle) -> object:
    """Press each menu in ``path`` so a GTK menu that fills on show has rows.

    A leaf is not pressed. On failure, the top menu that was opened is closed.
    """
    components = menus.parse_path(path)
    container: object = _require_bar(app)
    top: object | None = None
    try:
        for depth, wanted in enumerate(components):
            node, title = _match(entries_of(container), wanted, depth)
            if not _has_submenu(node):
                if depth == len(components) - 1:
                    raise ComputerUseError(
                        ErrorCode.APP_NOT_FOUND,
                        f"{title!r} is an item, not a menu",
                        detail={"path": path, "hint": "list its parent, or press it"},
                    )
                raise ComputerUseError(
                    ErrorCode.APP_NOT_FOUND,
                    f"{title!r} has no submenu",
                    detail={"component": wanted},
                )
            if not _press(node):
                raise ComputerUseError(
                    ErrorCode.UNSUPPORTED,
                    f"the app refused to press {title!r}",
                    detail={"path": path, "reason": "press_failed", "level": depth},
                )
            if top is None:
                top = node
            settle(menus.MENU_OPEN_SETTLE_S)
            container = _opened_menu(app, node)
    except Exception:
        if top is not None:
            _press(top)
        raise
    assert top is not None
    return top


def _menubar_has_focus(app: object) -> bool:
    """Detect keyboard tracking even when the popup has already closed."""
    bar = _require_bar(app)
    return _has_state(bar, "FOCUSED") or any(
        _has_state(item, "FOCUSED") for item in entries_of(bar)
    )


def _close_leftover_popup(app: object, path: str, settle, dismiss) -> None:
    """Close a leftover popup or menubar keyboard tracking after the leaf ran.

    A dialog the leaf opened is left alone: Escape would cancel it. A Java
    bar entry that only keeps SELECTED, with no showing item, is already
    closed and is not dismissed. When the popup is still showing after
    Escape, or menubar focus remains, the press reports a cleanup error.
    """
    if _tree_has_showing_dialog(app):
        return
    if not menu_state(app).get("open") and not _menubar_has_focus(app):
        return
    dismiss()
    after = menu_state(app)
    for _attempt in range(3):
        if not after.get("open") and not _menubar_has_focus(app):
            return
        settle(menus.MENU_OPEN_SETTLE_S)
        after = menu_state(app)
    if not after.get("open") and not _menubar_has_focus(app):
        return
    still = [str(part) for part in (after.get("path") or [])]
    raise ComputerUseError(
        ErrorCode.UNSUPPORTED,
        "the menu is still open or the menubar still has focus after the item was pressed",
        detail={
            "path": path,
            "reason": "menu_still_open",
            "open": still,
            "hint": "Escape was sent after the item ran; the popup or menubar keyboard focus remains.",
        },
    )


def menu_press(app: object, path: str, *, settle=None, dismiss=None, dispatch=None, await_dialog=None) -> str:
    """Activate the item at ``path``; returns its title.

    A refusal after a menu was opened sends Escape. Pressing the bar entry
    again leaves GTK menu tracking on, so the Edit menu stayed open after a
    disabled Undo. Escape restores the state from before the press. If the
    menu is still open, the refusal says so.

    ``dispatch`` presses the leaf when the driver has one. It returns
    ``ok``, ``dispatched``, or ``refused``. A modal dialog's D-Bus reply is
    ``dispatched``, not a refusal. ``await_dialog(app, dispatched=...)``
    then waits for that window on the connection used for reads. Parent
    menus stay on the synchronous press. A leaf that is not itself a menu
    then closes a popup that is still showing. A showing dialog is not
    given that Escape.
    """
    settle = settle or _settle
    dismiss = dismiss or _dismiss_menu
    components = menus.parse_path(path)
    container: object = _require_bar(app)
    opened: list[object] = []

    def abandon() -> None:
        if not opened:
            return
        if _tree_has_showing_dialog(app):
            return
        dismiss()
        after = menu_state(app)
        if after.get("open") or _menubar_has_focus(app):
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                "the menu stayed open or the menubar still has focus after Escape; the previous state was not restored",
                detail={
                    "path": path,
                    "reason": "menu_still_open",
                    "open": [str(part) for part in (after.get("path") or [])],
                    "hint": "Pressing the menu-bar entry again does not end GTK menu tracking.",
                },
            )

    for depth, wanted in enumerate(components):
        try:
            node, title = _match(entries_of(container), wanted, depth)
        except ComputerUseError:
            abandon()
            raise
        last = depth == len(components) - 1
        if last and not _enabled(node):
            abandon()
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                f"menu item {title!r} is disabled right now",
                detail={"path": path, "reason": "disabled"},
            )
        if not last and not _has_submenu(node):
            abandon()
            raise ComputerUseError(
                ErrorCode.APP_NOT_FOUND,
                f"{title!r} has no submenu",
                detail={"component": wanted},
            )
        if last and dispatch is not None and not _quick_menu_entry(node):
            outcome = dispatch(node)
            if outcome == "refused":
                abandon()
                raise ComputerUseError(
                    ErrorCode.UNSUPPORTED,
                    f"the app refused to press {title!r}",
                    detail={"path": path, "reason": "press_failed", "level": depth},
                )
            if await_dialog is not None:
                await_dialog(app, dispatched=(outcome == "dispatched"))
            if not _has_submenu(node):
                _close_leftover_popup(app, path, settle, dismiss)
            return title
        if not _press(node):
            abandon()
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                f"the app refused to press {title!r}",
                detail={"path": path, "reason": "press_failed", "level": depth},
            )
        if last:
            if not _has_submenu(node):
                _close_leftover_popup(app, path, settle, dismiss)
            return title
        opened.append(node)
        settle(menus.MENU_OPEN_SETTLE_S)
        container = _opened_menu(app, node)
    raise AssertionError("unreachable")  # pragma: no cover


def menu_state(app: object) -> dict[str, object]:
    try:
        bar = _require_bar(app)
    except ComputerUseError:
        return {"open": False, "path": []}
    for item in entries_of(bar):
        if not _is_open(item):
            continue
        path = [_name(item)]
        container = item
        while True:
            nxt = next((child for child in entries_of(container) if _has_submenu(child) and _is_open(child)), None)
            if nxt is None:
                break
            path.append(_name(nxt))
            container = nxt
        return {"open": True, "path": path}
    return {"open": False, "path": []}


def _dismiss_menu() -> None:
    """End GTK menu tracking with Escape.

    Pressing the open bar entry again does not leave menu-tracking mode on
    GTK (Mousepad on X11). Escape does.
    """
    import os

    if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        from a11y_computer_use.drivers.linux import _wayland_input_error

        raise _wayland_input_error("menu close")
    from a11y_computer_use.drivers import _linux_input

    try:
        _linux_input.press_chord("escape")
    except ComputerUseError:
        raise
    except Exception as exc:
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            "could not send Escape to close the menu",
            detail={
                "reason": "escape_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "hint": "Escape ends GTK menu tracking. Pressing the menu-bar entry again does not.",
            },
        ) from exc


def menu_close(app: object, *, dismiss=None, state=None, settle=None, attempts: int = 3) -> list[str]:
    """Close the open menu. Return the path that was open.

    ``dismiss`` ends tracking (Escape on a real session). Success is returned
    only after `menu_state` says the popup is gone. A menu that is still open
    raises, so the caller cannot report a closed menu.
    """
    read = state or (lambda: menu_state(app))
    dismiss = dismiss or _dismiss_menu
    settle = settle or _settle
    before = read()
    path = [str(part) for part in (before.get("path") or [])]
    if not before.get("open") or not path:
        return []
    dismiss()
    after = before
    for _ in range(attempts):
        settle(menus.MENU_OPEN_SETTLE_S)
        after = read()
        if not after.get("open"):
            return path
    still = [str(part) for part in (after.get("path") or path)]
    raise ComputerUseError(
        ErrorCode.UNSUPPORTED,
        "the menu is still open after Escape; close was not successful",
        detail={
            "path": still,
            "reason": "menu_still_open",
            "hint": "Pressing the menu-bar entry again does not end GTK menu tracking. "
                    "Escape was sent and the popup is still showing.",
        },
    )
