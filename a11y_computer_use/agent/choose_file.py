"""choose_file: type a path into an open file chooser and check the result.

This is an agent macro. It is not the desktop ``file_dialog`` tool. On Linux
that tool stays unsupported and is not called from here. The chooser is
already open. The macro presses Ctrl+L, types the absolute path once, and
presses Return. Success is only the dialog gone and the file name showing
outside that dialog. A check that cannot be made is a failure.
"""

from __future__ import annotations

import ast
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

from a11y_computer_use.schema import ComputerUseError

#: Window titles that are a file chooser, not a document named Open.
DEFINITE_TITLES = frozenset({
    "open file",
    "save file",
    "save as",
    "select file",
    "select a file",
    "select files",
    "choose a file",
    "choose file",
    "choose files",
    "choose files to upload",
    "open document",
    "save document",
    "file upload",
    "upload file",
})

#: Titles that also name ordinary windows. They count only with chooser controls.
AMBIGUOUS_TITLES = frozenset({"open", "save"})

_ACCEPT_LABELS = frozenset({"open", "save", "ok", "select"})
_CANCEL_LABELS = frozenset({"cancel"})
_WINDOW_ROLES = frozenset({"window", "dialog", "sheet", "frame", "filechooser"})
_BUTTON_ROLES = frozenset({"pushbutton", "button"})
_EMPTY_FILE_INPUT = ("no file chosen", "no file selected")
_READ_BACK = "read back "


@dataclass(frozen=True)
class ChooseResult:
    """The macro's own result, before the agent wraps it as a tool outcome."""

    ok: bool
    message: str
    reason: str = ""
    evidence: str = ""
    detail: dict = field(default_factory=dict)


@dataclass
class ChooseIO:
    """Desktop calls the macro needs. Tests pass fakes; the agent fills these."""

    windows: Callable[[], list[dict] | None]
    focus: Callable[[int], None]
    key: Callable[[str], object]
    type_text: Callable[[str], object]
    set_value: Callable[[str, str], object]
    snapshot: Callable[[str], object | None]
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic


def folded_title(title: object) -> str:
    """Case-folded window title, with the trailing ellipsis of a menu removed."""
    text = str(title or "").casefold().replace("…", "").replace("...", "")
    return " ".join(text.split()).strip(" .")


def is_definite_title(title: object) -> bool:
    return folded_title(title) in DEFINITE_TITLES


def is_ambiguous_title(title: object) -> bool:
    return folded_title(title) in AMBIGUOUS_TITLES


def same_path(value: str, path: str) -> bool:
    """True when ``value`` is ``path``, including a ``file://`` form of it."""
    return _canonical(value) == _canonical(path)


def entered_path(result: object, path: str) -> bool:
    """True when a type or set_value result read the path back exactly.

    A confirmed append, a longer string that merely contains the path, and a
    result with no read-back are not a match. Return is not pressed for those.
    """
    if getattr(result, "outcome", None) != "confirmed":
        return False
    evidence = str(getattr(result, "evidence", "") or "")
    if not evidence.startswith(_READ_BACK):
        return False
    try:
        value = ast.literal_eval(evidence[len(_READ_BACK):])
    except (SyntaxError, ValueError):
        return False
    return isinstance(value, str) and same_path(value, path)


def parse_window_list(raw: object) -> list[dict] | None:
    """Rows from a window-list tool result, or None when the text is not a list."""
    import json

    text = str(raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end < start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, list):
        return None
    return [row for row in data if isinstance(row, dict)]


def snapshot_looks_like_chooser(snap: object) -> bool:
    """True when the tree is a file chooser, not a document titled Open or Save."""
    if snap is None:
        return False
    accept = False
    cancel = False
    for element in _elements(snap):
        role = _role_key(getattr(element, "role", ""))
        title = getattr(element, "title", "")
        if role == "filechooser":
            return True
        if role in _WINDOW_ROLES and is_definite_title(title):
            return True
        if role not in _BUTTON_ROLES:
            continue
        label = _button_label(title)
        if label in _ACCEPT_LABELS:
            accept = True
        elif label in _CANCEL_LABELS:
            cancel = True
    return accept and cancel


def file_shown(snap: object | None, window_titles: list[str], path: str) -> bool:
    """True when ``path``'s name is outside a chooser frame.

    Text inside the dialog, including the file list and the location entry,
    does not count. A control that still says no file was chosen fails the
    check even when the name appears somewhere else on the page.
    """
    texts = opener_texts(snap)
    blob = "\n".join([*texts, *window_titles])
    if not blob:
        return False
    folded = blob.casefold()
    if any(phrase in folded for phrase in _EMPTY_FILE_INPUT):
        return False
    if path and path in blob:
        return True
    return _name_shown(blob, path)


def opener_texts(snap: object | None) -> list[str]:
    """Titles and values that are not inside a file-chooser frame."""
    elements = _elements(snap)
    if not elements:
        return []
    by_ref = {getattr(element, "ref", ""): element for element in elements}
    chooser_refs = {
        getattr(element, "ref", "")
        for element in elements
        if _is_chooser_frame(element)
    }
    texts: list[str] = []
    for element in elements:
        if _under_chooser(element, by_ref, chooser_refs):
            continue
        title = str(getattr(element, "title", "") or "")
        if title:
            texts.append(title)
        value = getattr(element, "value", None)
        if value is not None and str(value):
            texts.append(str(value))
    return texts


def chooser_argument_error(args: dict) -> str | None:
    """Why ``choose_file`` arguments cannot be executed, or None.

    ``open`` (the default) requires an absolute path to a file that exists.
    ``save`` requires an absolute path with a file name. The file need not
    exist yet. Nothing is typed when this returns an error.
    """
    if "path" not in args:
        return "invalid_arguments: choose_file: path is required"
    path = args.get("path")
    if not isinstance(path, str) or not path or path != path.strip():
        return "invalid_arguments: choose_file: path must be an absolute path"
    if "\n" in path or "\r" in path or "\x00" in path:
        return "invalid_arguments: choose_file: path must be an absolute path"
    if not os.path.isabs(path):
        return "invalid_arguments: choose_file: path must be an absolute path"
    verb = args.get("action", "open")
    if not isinstance(verb, str) or verb not in {"open", "save"}:
        return "invalid_arguments: choose_file: action must be open or save"
    if verb == "open":
        if not os.path.isfile(path):
            return "invalid_arguments: choose_file: open requires an existing file"
        return None
    if not os.path.basename(path) or path.endswith(("/", "\\")):
        return "invalid_arguments: choose_file: save requires a file name"
    return None


def perform_choose_file(
    io: ChooseIO,
    path: str,
    *,
    apps: list[str],
    timeout_s: float = 6.0,
) -> ChooseResult:
    """Press Ctrl+L, type ``path``, press Return, and check the dialog.

    ``apps`` are accessibility names to read after Return (the tracked app
    and the chooser's app). Return is not pressed unless the location entry
    read back ``path``. A dialog that stays open, a dialog that closes
    without the file, and a check that cannot be read are failures.
    """
    windows = _windows(io)
    if windows is None and not _any_snapshot(io, apps):
        return _fail(
            "choose_file_unverifiable",
            "the open windows could not be read, so no file chooser was driven",
        )
    chosen, problem = _select_choosers(io, windows or [], apps)
    if problem:
        return _fail("no_file_chooser", problem)
    if not chosen:
        if windows is None:
            return _fail(
                "choose_file_unverifiable",
                "the open windows could not be read, so no file chooser was driven",
            )
        return _fail(
            "no_file_chooser",
            "no GTK, Qt, or Chrome file chooser is open",
        )
    if len(chosen) > 1:
        return _fail(
            "no_file_chooser",
            "more than one file chooser is open",
            window_ids=_window_ids(chosen),
        )

    target = chosen[0]
    window_id = _window_id(target)
    if window_id is not None:
        try:
            io.focus(window_id)
        except ComputerUseError:
            pass
    try:
        io.key("ctrl+l")
    except ComputerUseError as exc:
        return _from_error(exc, default_reason="path_not_entered")

    if not _land_path(io, path, _app_names(apps, target)):
        return _fail(
            "path_not_entered",
            "the location entry did not show the path, so Return was not pressed",
            path=path,
        )
    try:
        io.key("Return")
    except ComputerUseError as exc:
        return _from_error(exc, default_reason="file_chooser_unchanged")

    ids = {window_id} if window_id is not None else set()
    titles = {folded_title(target.get("title"))} if is_definite_title(target.get("title")) else set()
    names = _app_names(apps, target)
    return _verify(io, path, ids=ids, titles=titles, apps=names, timeout_s=timeout_s)


def _land_path(io: ChooseIO, path: str, apps: list[str]) -> bool:
    """Type ``path`` once. One set_value follows only when that read-back missed."""
    try:
        typed = io.type_text(path)
    except ComputerUseError:
        typed = None
    if entered_path(typed, path):
        return True
    ref = _location_ref(io, apps)
    if ref is None:
        return False
    try:
        written = io.set_value(ref, path)
    except ComputerUseError:
        return False
    return entered_path(written, path)


def _location_ref(io: ChooseIO, apps: list[str]) -> str | None:
    """The one location entry, or None when the tree does not name exactly one."""
    for app in apps:
        snap = _snap(io, app)
        if snap is None:
            continue
        ref = _pick_entry(snap)
        if ref:
            return ref
    return None


def _pick_entry(snap: object) -> str | None:
    editables = [
        element for element in _elements(snap)
        if getattr(element, "editable", False) and str(getattr(element, "ref", "") or "")
    ]
    if not editables:
        return None
    focused = [element for element in editables if getattr(element, "focused", False)]
    pool = focused or editables

    def score(element: object) -> int:
        name = str(getattr(element, "title", "") or "").casefold()
        value = str(getattr(element, "value", "") or "")
        points = 0
        if "location" in name or name in {"name", "filename"}:
            points += 2
        if "/" in value or "\\" in value:
            points += 1
        return points

    best = max(score(element) for element in pool)
    chosen = [element for element in pool if score(element) == best]
    if len(chosen) != 1:
        return None
    return str(getattr(chosen[0], "ref"))


def _verify(
    io: ChooseIO,
    path: str,
    *,
    ids: set[int],
    titles: set[str],
    apps: list[str],
    timeout_s: float,
) -> ChooseResult:
    deadline = io.monotonic() + max(0.0, timeout_s)
    last = _fail(
        "choose_file_unverifiable",
        "the file chooser could not be checked after Return",
        path=path,
    )
    while True:
        windows = _windows(io)
        closed = _dialog_closed(windows, ids, titles, io, apps)
        if closed is None:
            last = _fail(
                "choose_file_unverifiable",
                "the file chooser could not be checked after Return",
                path=path,
            )
        elif not closed:
            last = _fail(
                "file_chooser_unchanged",
                "the file chooser stayed open after Return",
                path=path,
            )
        else:
            shown, readable = _reflected(io, windows, path, apps)
            if shown:
                base = os.path.basename(path)
                return ChooseResult(
                    True,
                    f"chose {path}",
                    evidence=f"the dialog closed and {base} is shown",
                    detail={"path": path},
                )
            if readable:
                last = _fail(
                    "file_not_chosen",
                    f"the file chooser closed without showing {os.path.basename(path)!r}",
                    path=path,
                )
            else:
                last = _fail(
                    "choose_file_unverifiable",
                    "the page could not be read after the file chooser closed",
                    path=path,
                )
        if io.monotonic() >= deadline:
            return last
        io.sleep(0.15)


def _dialog_closed(
    windows: list[dict] | None,
    ids: set[int],
    titles: set[str],
    io: ChooseIO,
    apps: list[str],
) -> bool | None:
    """False while the chooser is mapped. None when that cannot be read."""
    if windows is not None and (ids or titles):
        for row in windows:
            wid = _window_id(row)
            if wid is not None and wid in ids:
                return False
            if folded_title(row.get("title")) in titles:
                return False
        return True
    saw = False
    for app in apps:
        snap = _snap(io, app)
        if snap is None:
            continue
        saw = True
        if _a11y_chooser_open(snap):
            return False
    if saw:
        return True
    if windows is not None:
        return not any(is_definite_title(row.get("title")) for row in windows)
    return None


def _reflected(
    io: ChooseIO,
    windows: list[dict] | None,
    path: str,
    apps: list[str],
) -> tuple[bool, bool]:
    """``(shown, readable)``. ``readable`` is false when no tree and no titles were read."""
    titles = _other_titles(windows)
    readable = False
    for app in apps:
        snap = _snap(io, app)
        if snap is None:
            continue
        readable = True
        if file_shown(snap, titles, path):
            return True, True
    if not readable and titles and file_shown(None, titles, path):
        return True, True
    if windows is not None or readable:
        return False, True
    return False, False


def _select_choosers(
    io: ChooseIO,
    windows: list[dict],
    apps: list[str],
) -> tuple[list[dict], str | None]:
    definite = [row for row in windows if is_definite_title(row.get("title"))]
    if len(definite) > 1:
        return definite, "more than one file chooser is open"
    if len(definite) == 1:
        return definite, None
    ambiguous: list[dict] = []
    for row in windows:
        if not is_ambiguous_title(row.get("title")):
            continue
        app = str(row.get("app") or "")
        names = [app] if app else list(apps)
        if any(snapshot_looks_like_chooser(_snap(io, name)) for name in names if name):
            ambiguous.append(row)
    if len(ambiguous) > 1:
        return ambiguous, "more than one file chooser is open"
    if len(ambiguous) == 1:
        return ambiguous, None
    for app in apps:
        snap = _snap(io, app)
        if snap is not None and snapshot_looks_like_chooser(snap):
            return [{"title": _chooser_title(snap), "app": app}], None
    return [], None


def _chooser_title(snap: object) -> str:
    for element in _elements(snap):
        if _is_chooser_frame(element) and str(getattr(element, "title", "") or ""):
            return str(element.title)
    return "Open File"


def _a11y_chooser_open(snap: object) -> bool:
    return any(_is_chooser_frame(element) for element in _elements(snap))


def _is_chooser_frame(element: object) -> bool:
    role = _role_key(getattr(element, "role", ""))
    if role == "filechooser":
        return True
    return role in _WINDOW_ROLES and is_definite_title(getattr(element, "title", ""))


def _under_chooser(element: object, by_ref: dict, chooser_refs: set[str]) -> bool:
    seen: set[str] = set()
    current: object | None = element
    while current is not None:
        ref = str(getattr(current, "ref", "") or "")
        if not ref or ref in seen:
            return False
        if ref in chooser_refs:
            return True
        seen.add(ref)
        parent = getattr(current, "parent", None)
        current = by_ref.get(parent) if parent else None
    return False


def _other_titles(windows: list[dict] | None) -> list[str]:
    if not windows:
        return []
    titles: list[str] = []
    for row in windows:
        title = str(row.get("title") or "")
        if not title or is_definite_title(title) or is_ambiguous_title(title):
            continue
        titles.append(title)
    return titles


def _app_names(apps: list[str], chooser: dict) -> list[str]:
    names: list[str] = []
    for name in [*apps, str(chooser.get("app") or "")]:
        text = str(name or "").strip()
        if not text or text.casefold() in {"desktop", "unknown"}:
            continue
        if text not in names:
            names.append(text)
    return names


def _any_snapshot(io: ChooseIO, apps: list[str]) -> bool:
    return any(_snap(io, app) is not None for app in apps)


def _windows(io: ChooseIO) -> list[dict] | None:
    try:
        return io.windows()
    except ComputerUseError:
        return None


def _snap(io: ChooseIO, app: str) -> object | None:
    if not app:
        return None
    try:
        return io.snapshot(app)
    except ComputerUseError:
        return None


def _from_error(exc: ComputerUseError, *, default_reason: str) -> ChooseResult:
    reason = str((exc.detail or {}).get("reason") or default_reason)
    if reason in {"no_file_dialog"}:
        reason = default_reason
    detail = {key: value for key, value in (exc.detail or {}).items() if key != "outcome"}
    detail["reason"] = reason
    return ChooseResult(False, exc.message, reason=reason, detail=detail)


def _fail(reason: str, message: str, **detail: object) -> ChooseResult:
    info = {"reason": reason, **detail}
    return ChooseResult(False, message, reason=reason, detail=info)


def _window_id(row: dict) -> int | None:
    value = row.get("window_id")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0:
        return None
    return value


def _window_ids(rows: list[dict]) -> list[int]:
    found: list[int] = []
    for row in rows:
        wid = _window_id(row)
        if wid is not None:
            found.append(wid)
    return found


def _elements(snap: object | None) -> list:
    if snap is None:
        return []
    return list(getattr(snap, "elements", ()) or ())


def _role_key(role: object) -> str:
    return "".join(str(role or "").casefold().split()).removeprefix("ax")


def _button_label(name: object) -> str:
    text = str(name or "").replace("_", " ").strip().casefold()
    return text.split("(", 1)[0].strip()


def _name_shown(text: str, path: str) -> bool:
    base = os.path.basename(path)
    if not base or base in {".", ".."}:
        return False
    return re.search(rf"(?<![\w.]){re.escape(base)}(?![\w])", text) is not None


def _canonical(value: str) -> str:
    """Local path for ``value``, including a ``file:`` URI of that path.

    ``Path.as_uri()`` on Windows is ``file:///C:/...``. ``urlparse`` leaves
    the drive inside the path as ``/C:/...``, and ``url2pathname`` turns that
    back into ``C:\\...``. A URI that puts the drive in the host
    (``file://C:/...``) is joined the same way.
    """
    text = str(value or "").strip().replace("\r", "").replace("\n", "")
    if text.lower().startswith("file:"):
        from urllib.request import url2pathname

        parsed = urlparse(text)
        path = parsed.path or ""
        host = parsed.netloc
        if len(host) == 2 and host[1] == ":":
            path = "/" + host + path
        text = url2pathname(path)
    return os.path.normcase(os.path.normpath(text))
