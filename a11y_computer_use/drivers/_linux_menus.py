"""Linux menu bars over AT-SPI.

GTK publishes a menu-bar entry as role ``menu``. Its rows are either direct
children or the children of one popup (untitled, or the entry's only child).
The shared macOS walker looks for an ``AXMenu`` child of each item, which does
not match that tree, so the walk lives here. Title matching, ellipsis, and
prefixes still go through `menus.match_title`.

Listing reads the tree and does not press when the rows are already there. A
menu that is empty until it is shown is opened and then closed. Pressing opens
each level and activates the leaf with the item's ``click`` action. An open
menu is one whose item is ``SELECTED`` or whose popup is ``SHOWING``. Close
sends Escape and succeeds only after that menu is gone. A shortcut is the
accelerator in the AT-SPI binding, not the Alt mnemonic.

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


def _name(node: object) -> str:
    if _role(node) == "separator":
        return ""
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
    state_set = _call(node, "get_state_set")
    if state_set is None or not hasattr(state_set, "contains"):
        return False
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        enum = Atspi.StateType
    except Exception:
        return False
    for name in names:
        member = getattr(enum, name.upper(), None)
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


def _has_submenu(node: object) -> bool:
    if _role(node) in _MENU_ROLES:
        return True
    return bool(entries_of(node))


def shortcut_from_binding(raw: str | None) -> str | None:
    """Render an AT-SPI key binding (``<Control>o``, ``<Primary><Shift>S``) as ``ctrl+o``.

    GTK joins several fields with ``;``. Mousepad's New item is
    ``n;<Alt>f:n;<Primary>n``: the letter, the Alt mnemonic path (``f`` then
    ``n``, which is why a colon is in that field), and the accelerator
    ``<Primary>n``. The shortcut is that accelerator (``ctrl+n``), not the
    mnemonic. A colon marks a key sequence, so it loses to a single chord.
    A binding with no tag (the keysym half of ``s;keycode;mods``) is returned
    as that key. Digit-only segments are ignored.
    """
    if not raw or not str(raw).strip():
        return None
    parts = [part.strip() for part in str(raw).split(";") if part.strip()]
    tagged = [part for part in parts if "<" in part]
    chords = [part for part in tagged if ":" not in part]
    chosen = chords[-1] if chords else None
    if chosen is None:
        chosen = next((part for part in parts if not part.isdigit()), None)
    if not chosen:
        return None
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
    if not key:
        return None
    ordered = [name for name in _MOD_ORDER if name in found]
    return "+".join([*ordered, key])


def _key_binding(node: object) -> str:
    explicit = getattr(node, "key", None)
    if explicit:
        return str(explicit)
    action = _call(node, ("get_action_iface", "get_action"))
    if action is None:
        return ""
    return str(_call(action, ("get_key_binding", "get_keybinding"), 0, default="") or "")


def _checked(node: object) -> bool | None:
    if _role(node) not in ("check menu item", "radio menu item"):
        return None
    return _has_state(node, "CHECKED")


def _describe(node: object) -> MenuItem:
    return MenuItem(
        title=_name(node),
        enabled=_enabled(node),
        shortcut=shortcut_from_binding(_key_binding(node)),
        submenu=_has_submenu(node),
        checked=_checked(node),
    )


def _press(node: object) -> bool:
    action = _call(node, ("get_action_iface", "get_action"))
    if action is None:
        return False
    count = _call(action, ("get_n_actions", "get_nActions"), default=0) or 0
    try:
        total = int(count)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    for index in range(total):
        raw = str(_call(action, ("get_action_name", "get_name"), index, default="") or "").lower()
        if raw in _PRESS_ACTIONS:
            return bool(_call(action, ("do_action", "doAction"), index, default=False))
    return False


def find_menu_bar(root: object) -> object | None:
    """The menu bar under ``root``, preferring one inside an ACTIVE frame."""
    if root is None:
        return None
    if _role(root) == "menu bar":
        return root
    found: list[object] = []
    parent: dict[int, object] = {}
    queue = [root]
    seen: set[int] = set()
    walked = 0
    while queue and walked < 400:
        node = queue.pop(0)
        identity = id(node)
        if identity in seen:
            continue
        seen.add(identity)
        walked += 1
        if _role(node) == "menu bar":
            found.append(node)
            continue
        for child in _raw_children(node)[:80]:
            parent[id(child)] = node
            queue.append(child)
    if not found:
        return None

    def in_active(node: object) -> bool:
        current: object | None = node
        while current is not None:
            if _has_state(current, "ACTIVE"):
                return True
            current = parent.get(id(current))
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
            detail={"hint": "focus the app first; background-only apps have no menu bar"},
        )
    return bar


def _match(entries: list[object], wanted: str, depth: int) -> tuple[object, str]:
    titles = [_name(node) for node in entries]
    try:
        index = menus.match_title(wanted, titles)
    except LookupError as exc:
        raise ComputerUseError(
            ErrorCode.APP_NOT_FOUND,
            f"menu path component {depth + 1} ({wanted!r}): {exc}",
            detail={"component": wanted, "available": titles},
        ) from None
    return entries[index], titles[index]


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
        container = node
    return nodes


def _is_open(node: object) -> bool:
    """True when this menu is popped open.

    The bar entry itself is SHOWING whenever its label is on screen, so that
    state is not an open menu. SELECTED on the entry, or SHOWING/SELECTED on
    its popup child, is.
    """
    if _has_state(node, "SELECTED"):
        return True
    return any(
        _role(child) in _MENU_ROLES and (_has_state(child, "SHOWING") or _has_state(child, "SELECTED"))
        for child in _raw_children(node)
    )


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
        rows = entries_of(target)
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
        return [_describe(node).to_dict() for node in entries_of(target)]
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
            container = node
    except Exception:
        if top is not None:
            _press(top)
        raise
    assert top is not None
    return top


def menu_press(app: object, path: str, *, settle=None) -> str:
    """Activate the item at ``path``; returns its title."""
    settle = settle or _settle
    components = menus.parse_path(path)
    container: object = _require_bar(app)
    opened: list[object] = []

    def abandon() -> None:
        if opened:
            _press(opened[0])

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
        if not _press(node):
            abandon()
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                f"the app refused to press {title!r}",
                detail={"path": path, "reason": "press_failed", "level": depth},
            )
        if last:
            return title
        opened.append(node)
        settle(menus.MENU_OPEN_SETTLE_S)
        container = node
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
