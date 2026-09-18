"""Menu bars and file panels through the accessibility tree.

Two things stay accessible even when an app draws its own content (After
Effects, Figma, games): the menu bar, and the system open/save panels. This
module drives both. The tree walk goes through a small accessor seam
(`MenuAccessor`) so the logic is testable on any OS with a fake tree; the
macOS implementation at the bottom binds it to ``AXUIElement``.

Menu paths are written the way a manual would: ``"File > Export > Add to
Render Queue"``. Titles match case-insensitively, ignore a trailing ellipsis,
and accept a unique prefix, so ``"File > Save As"`` finds ``"Save As…"``.
"""

from __future__ import annotations

import os
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from a11y_computer_use.schema import ComputerUseError, ErrorCode, FileDialogVerb

#: Characters that separate the components of a menu path.
PATH_SEPARATORS: tuple[str, ...] = (">", "→", "»")

#: Bounded breadth-first search over a panel's tree when classifying it.
_PANEL_MAX_NODES = 400
_PANEL_FANOUT = 40

#: How long the go-to-folder sheet and a panel refresh are given to settle.
DIALOG_SETTLE_S = 0.35

#: Pause after opening a menu level before pressing the next one.
MENU_OPEN_SETTLE_S = 0.12


class MenuAccessor(Protocol):
    """The four AX primitives menu and panel driving needs."""

    def attr(self, node: object, name: str) -> object | None: ...
    def children(self, node: object) -> Sequence[object]: ...
    def press(self, node: object) -> bool: ...
    def set_value(self, node: object, value: str) -> bool: ...
    def close(self, node: object) -> None:
        """Close the menu opened from ``node`` (a menu bar item or submenu item).

        Escape does not end menu tracking started through accessibility, and
        an app stuck in tracking answers every AX call at the timeout, so the
        implementation must cancel through AX: ``AXCancel`` on the open menu,
        else a second press of the item, which toggles its menu shut."""
        ...


@dataclass(frozen=True, slots=True)
class MenuItem:
    """One entry of a menu, as the planner sees it."""

    title: str
    enabled: bool
    shortcut: str | None
    submenu: bool
    checked: bool | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "enabled": self.enabled,
            "shortcut": self.shortcut,
            "submenu": self.submenu,
            "checked": self.checked,
        }


# --------------------------------------------------------------------------- #
# Paths and title matching
# --------------------------------------------------------------------------- #
def parse_path(path: str) -> tuple[str, ...]:
    """Split ``"File > Export > Add to Render Queue"`` into its components.

    Raises ``ValueError`` for an empty path or an empty component.
    """
    text = path or ""
    for sep in PATH_SEPARATORS[1:]:
        text = text.replace(sep, PATH_SEPARATORS[0])
    parts = tuple(p.strip() for p in text.split(PATH_SEPARATORS[0]))
    if not parts or any(not p for p in parts):
        raise ValueError(f"menu path must be non-empty titles separated by '>': {path!r}")
    return parts


def normalize_title(title: str) -> str:
    """Lowercase, trim, and drop a trailing ellipsis so ``Save As…`` matches
    ``save as``."""
    text = " ".join((title or "").split()).lower()
    for tail in ("…", "..."):
        if text.endswith(tail):
            text = text[: -len(tail)].rstrip()
    return text


def match_title(wanted: str, titles: Sequence[str]) -> int:
    """Index of the title matching ``wanted``: exact (normalized) first, then a
    unique prefix. Raises ``LookupError`` when nothing or several things match.
    """
    want = normalize_title(wanted)
    normalized = [normalize_title(t) for t in titles]
    exact = [i for i, t in enumerate(normalized) if t == want]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise LookupError(f"{wanted!r} matches {len(exact)} items with the same title")
    prefix = [i for i, t in enumerate(normalized) if t.startswith(want)]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        options = ", ".join(repr(titles[i]) for i in prefix)
        raise LookupError(f"{wanted!r} is ambiguous: {options}")
    available = ", ".join(repr(t) for t in titles if t)
    raise LookupError(f"no menu item matches {wanted!r}; available: {available}")


# --------------------------------------------------------------------------- #
# Menu bar walk
# --------------------------------------------------------------------------- #
#: ``AXMenuItemCmdModifiers`` bit meanings (kAXMenuItemModifier*).
_MOD_SHIFT, _MOD_OPTION, _MOD_CONTROL, _MOD_NO_COMMAND = 1, 2, 4, 8


def shortcut_text(cmd_char: object, modifiers: object) -> str | None:
    """Render ``AXMenuItemCmdChar`` + ``AXMenuItemCmdModifiers`` as a chord."""
    char = str(cmd_char or "").strip()
    if not char:
        return None
    mods = int(modifiers or 0)
    parts: list[str] = []
    if mods & _MOD_CONTROL:
        parts.append("ctrl")
    if mods & _MOD_OPTION:
        parts.append("alt")
    if mods & _MOD_SHIFT:
        parts.append("shift")
    if not mods & _MOD_NO_COMMAND:
        parts.append("cmd")
    parts.append(char.lower())
    return "+".join(parts)


def _title(accessor: MenuAccessor, node: object) -> str:
    return str(accessor.attr(node, "AXTitle") or "")


def _menu_of(accessor: MenuAccessor, item: object) -> object | None:
    """The ``AXMenu`` child of a menu bar item or a submenu item, if any."""
    for child in accessor.children(item):
        if accessor.attr(child, "AXRole") == "AXMenu":
            return child
    return None


def _entries(accessor: MenuAccessor, menu: object) -> list[object]:
    """Titled items of a menu, separators skipped."""
    return [n for n in accessor.children(menu) if _title(accessor, n)]


def menu_bar(accessor: MenuAccessor, app_el: object) -> object:
    bar = accessor.attr(app_el, "AXMenuBar")
    if bar is None:
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            "the application exposes no menu bar through accessibility",
            detail={"hint": "focus the app first; background-only apps have no menu bar"},
        )
    return bar


def walk(accessor: MenuAccessor, app_el: object, components: Sequence[str]) -> list[object]:
    """Nodes along ``components``, starting at the menu bar items."""
    nodes: list[object] = []
    container = menu_bar(accessor, app_el)
    for depth, wanted in enumerate(components):
        entries = _entries(accessor, container)
        titles = [_title(accessor, n) for n in entries]
        try:
            index = match_title(wanted, titles)
        except LookupError as exc:
            raise ComputerUseError(
                ErrorCode.APP_NOT_FOUND,
                f"menu path component {depth + 1} ({wanted!r}): {exc}",
                detail={"component": wanted, "available": titles},
            ) from None
        node = entries[index]
        nodes.append(node)
        if depth < len(components) - 1:
            submenu = _menu_of(accessor, node)
            if submenu is None:
                raise ComputerUseError(
                    ErrorCode.APP_NOT_FOUND,
                    f"{titles[index]!r} has no submenu",
                    detail={"component": wanted},
                )
            container = submenu
    return nodes


def _item(accessor: MenuAccessor, node: object) -> MenuItem:
    enabled = accessor.attr(node, "AXEnabled")
    mark = accessor.attr(node, "AXMenuItemMarkChar")
    return MenuItem(
        title=_title(accessor, node),
        enabled=True if enabled is None else bool(enabled),
        shortcut=shortcut_text(
            accessor.attr(node, "AXMenuItemCmdChar"), accessor.attr(node, "AXMenuItemCmdModifiers")
        ),
        submenu=_menu_of(accessor, node) is not None,
        checked=(bool(str(mark).strip()) if mark is not None else None),
    )


def list_items(accessor: MenuAccessor, app_el: object, path: str | None) -> list[MenuItem]:
    """Items of the menu at ``path`` (the top-level menus when ``path`` is empty)."""
    if not path or not path.strip():
        bar = menu_bar(accessor, app_el)
        return [_item(accessor, n) for n in _entries(accessor, bar)]
    nodes = walk(accessor, app_el, parse_path(path))
    menu = _menu_of(accessor, nodes[-1])
    if menu is None:
        raise ComputerUseError(
            ErrorCode.APP_NOT_FOUND,
            f"{_title(accessor, nodes[-1])!r} is an item, not a menu",
            detail={"path": path, "hint": "list its parent, or press it"},
        )
    return [_item(accessor, n) for n in _entries(accessor, menu)]


def press_path(
    accessor: MenuAccessor,
    app_el: object,
    path: str,
    *,
    settle: Callable[[float], None] = time.sleep,
) -> str:
    """Activate the item at ``path``; returns its title.

    Menus are opened level by level, the way a user does, and each level's
    items are read again after it opens: apps rebuild menu items on open and
    validate their titles and enabled state only then (``Show Fonts`` becomes
    ``Hide Fonts``), so handles captured beforehand can be dead or stale. A
    direct ``AXPress`` on a deep item is accepted by many apps yet does
    nothing while its menu is closed. If a level refuses, Escape closes what
    was opened.
    """
    components = parse_path(path)
    container = menu_bar(accessor, app_el)
    opened: list[object] = []  # menu items whose menus we opened, top level first

    def abandon() -> None:
        if opened:  # closing the top-level menu ends the whole tracking session
            accessor.close(opened[0])

    for depth, wanted in enumerate(components):
        entries = _entries(accessor, container)
        titles = [_title(accessor, n) for n in entries]
        try:
            index = match_title(wanted, titles)
        except LookupError as exc:
            abandon()
            raise ComputerUseError(
                ErrorCode.APP_NOT_FOUND,
                f"menu path component {depth + 1} ({wanted!r}): {exc}",
                detail={"component": wanted, "available": titles},
            ) from None
        node = entries[index]
        title = titles[index]
        last = depth == len(components) - 1
        if last:
            enabled = accessor.attr(node, "AXEnabled")
            if enabled is not None and not bool(enabled):
                abandon()
                raise ComputerUseError(
                    ErrorCode.UNSUPPORTED,
                    f"menu item {title!r} is disabled right now",
                    detail={"path": path, "reason": "disabled"},
                )
        if _menu_of(accessor, node) is None and not last:
            abandon()
            raise ComputerUseError(
                ErrorCode.APP_NOT_FOUND,
                f"{title!r} has no submenu",
                detail={"component": wanted},
            )
        if not accessor.press(node):
            abandon()
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                f"the app refused to press {title!r}",
                detail={"path": path, "reason": "press_failed", "level": depth},
            )
        if last:
            return title
        opened.append(node)
        settle(MENU_OPEN_SETTLE_S)
        submenu = _menu_of(accessor, node)
        if submenu is None:
            abandon()
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                f"{title!r} did not open its submenu",
                detail={"path": path, "reason": "submenu_missing", "level": depth},
            )
        container = submenu
    raise AssertionError("unreachable")  # pragma: no cover


# --------------------------------------------------------------------------- #
# Open / save panels
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Panel:
    """A detected open or save panel."""

    kind: FileDialogVerb
    node: object
    filename_field: object | None


_OPEN_BUTTONS = ("open", "choose", "select", "attach", "import", "upload")
_SAVE_BUTTONS = ("save", "export", "render")
_FILENAME_HINTS = ("save as", "name", "file name", "filename", "export as")


def _panel_candidates(accessor: MenuAccessor, app_el: object) -> list[object]:
    """Sheets first (they sit on a window), then windows, focused ones first."""
    seen: list[object] = []

    def add(node: object | None) -> None:
        if node is not None and all(node is not s for s in seen):
            seen.append(node)

    windows: list[object] = []
    for attr in ("AXFocusedWindow", "AXMainWindow"):
        w = accessor.attr(app_el, attr)
        if w is not None:
            windows.append(w)
    windows.extend(accessor.attr(app_el, "AXWindows") or ())
    for w in windows:
        for child in accessor.children(w):
            if accessor.attr(child, "AXRole") == "AXSheet":
                add(child)
    for w in windows:
        add(w)
    return seen


def _scan(accessor: MenuAccessor, root: object) -> tuple[set[str], list[object]]:
    """Bounded BFS: (lowercased button titles, text fields) under ``root``."""
    buttons: set[str] = set()
    fields: list[object] = []
    queue = deque([root])
    seen = 0
    while queue and seen < _PANEL_MAX_NODES:
        node = queue.popleft()
        seen += 1
        role = accessor.attr(node, "AXRole")
        if role == "AXButton":
            buttons.add(normalize_title(_title(accessor, node)))
        elif role in ("AXTextField", "AXComboBox"):
            fields.append(node)
        for child in tuple(accessor.children(node))[:_PANEL_FANOUT]:
            queue.append(child)
    return buttons, fields


def _filename_field(accessor: MenuAccessor, fields: Sequence[object]) -> object | None:
    for node in fields:
        label = " ".join(
            str(accessor.attr(node, a) or "")
            for a in ("AXTitle", "AXDescription", "AXPlaceholderValue")
        ).lower()
        if any(h in label for h in _FILENAME_HINTS):
            return node
    for node in fields:
        if accessor.attr(node, "AXFocused"):
            return node
    return fields[0] if fields else None


def find_panel(accessor: MenuAccessor, app_el: object) -> Panel | None:
    """The frontmost open or save panel of the app, or None."""
    for candidate in _panel_candidates(accessor, app_el):
        buttons, fields = _scan(accessor, candidate)
        if any(b in _SAVE_BUTTONS for b in buttons) and fields:
            return Panel(FileDialogVerb.SAVE, candidate, _filename_field(accessor, fields))
        if any(b in _OPEN_BUTTONS for b in buttons):
            return Panel(FileDialogVerb.OPEN, candidate, None)
    return None


def drive_panel(
    panel: Panel,
    verb: FileDialogVerb,
    path: str,
    *,
    accessor: MenuAccessor,
    key: Callable[[str], object],
    type_text: Callable[[str], object],
    settle: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Point ``panel`` at ``path`` with the go-to-folder sheet (cmd+shift+g).

    OPEN: the sheet accepts a full file path, Return navigates to it and a
    second Return presses the default button. SAVE: the sheet gets the
    directory, then the filename field is set (AX value first, typing as the
    fallback) and Return saves.
    """
    if panel.kind is not verb:
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            f"the frontmost panel is a {panel.kind.value} panel, not {verb.value}",
            detail={"panel": panel.kind.value, "requested": verb.value},
        )
    if not os.path.isabs(path):
        raise ValueError(f"file_dialog needs an absolute path, got {path!r}")
    steps: list[str] = []
    if verb is FileDialogVerb.OPEN:
        key("cmd+shift+g")
        settle(DIALOG_SETTLE_S)
        type_text(path)
        key("return")
        settle(DIALOG_SETTLE_S)
        key("return")
        steps += ["go-to-folder", f"typed {path}", "return", "return"]
        return {"action": "open", "path": path, "steps": steps}
    directory, filename = os.path.split(path)
    if not filename:
        raise ValueError(f"file_dialog save needs a file name, got {path!r}")
    key("cmd+shift+g")
    settle(DIALOG_SETTLE_S)
    type_text(directory or "/")
    key("return")
    settle(DIALOG_SETTLE_S)
    steps += ["go-to-folder", f"typed {directory or '/'}", "return"]
    named = False
    if panel.filename_field is not None and accessor.set_value(panel.filename_field, filename):
        named = True
        steps.append("set filename via AX")
    if not named:
        key("cmd+a")
        type_text(filename)
        steps.append("typed filename")
    key("return")
    steps.append("return")
    return {"action": "save", "path": path, "steps": steps}


# --------------------------------------------------------------------------- #
# macOS binding
# --------------------------------------------------------------------------- #
class AXMenuAccessor:
    """`MenuAccessor` over live ``AXUIElement`` handles (pyobjc)."""

    def __init__(self, ax) -> None:
        self._ax = ax

    def attr(self, node: object, name: str) -> object | None:
        try:
            err, value = self._ax.AXUIElementCopyAttributeValue(node, name, None)
        except Exception:
            return None
        return value if err == 0 else None

    def children(self, node: object) -> Sequence[object]:
        return tuple(self.attr(node, "AXChildren") or ())

    def press(self, node: object) -> bool:
        try:
            return self._ax.AXUIElementPerformAction(node, "AXPress") == 0
        except Exception:
            return False

    def set_value(self, node: object, value: str) -> bool:
        try:
            return self._ax.AXUIElementSetAttributeValue(node, "AXValue", value) == 0
        except Exception:
            return False

    def close(self, node: object) -> None:
        menu = None
        try:
            for child in self.children(node):
                if self.attr(child, "AXRole") == "AXMenu":
                    menu = child
                    break
            if menu is not None and self._ax.AXUIElementPerformAction(menu, "AXCancel") == 0:
                return
        except Exception:
            pass
        self.press(node)  # a second press on an open menu item toggles its menu shut


def _macos_app_element(app: str) -> tuple[object, AXMenuAccessor, str]:
    from a11y_computer_use import observe

    observe.ensure_trusted()
    ax = observe._appservices()
    pid, bundle = observe._find_app(app)
    app_el = ax.AXUIElementCreateApplication(pid)
    observe._check_responsive(ax, app_el, bundle)
    return app_el, AXMenuAccessor(ax), bundle


def macos_menu_items(app: str, path: str | None) -> list[dict[str, object]]:
    app_el, accessor, _bundle = _macos_app_element(app)
    return [item.to_dict() for item in list_items(accessor, app_el, path)]


def macos_menu_press(app: str, path: str) -> str:
    app_el, accessor, _bundle = _macos_app_element(app)
    return press_path(accessor, app_el, path)


def macos_file_dialog(verb: FileDialogVerb, path: str, app: str) -> dict[str, object]:
    from a11y_computer_use import act

    app_el, accessor, bundle = _macos_app_element(app)
    panel = find_panel(accessor, app_el)
    if panel is None:
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            f"no open or save panel is showing in {bundle}",
            detail={"app": bundle, "reason": "no_dialog",
                    "hint": "trigger the panel first (menu 'File > Open…', 'File > Save As…')"},
        )
    return drive_panel(
        panel, verb, path, accessor=accessor,
        key=lambda chord: act.key_chord(chord),
        type_text=lambda text: act.type_text(text),
    )
