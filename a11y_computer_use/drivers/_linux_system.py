"""Linux system/windowing probes: foreground app, app-under-point, window and
app enumeration, activate/launch, and clipboard. Linux-only; used by server.py's
platform-dispatching helpers so the Runtime's gating works on Linux.

App identity on Linux is the process comm name (e.g. "gedit", "chrome") read
from ``/proc/<pid>/comm`` — the analog of a macOS bundle id / Windows exe for
permission-keying. A window with no pid uses its WM_CLASS instance when that
property is set. Comm is at most 15 bytes. A launcher the process drops
(``google-chrome`` runs as ``chrome``) and a name cut at that limit
(``gnome-terminal-server`` runs as ``gnome-terminal-``) resolve to the comm.
Window/desktop facts come from EWMH properties over python-xlib (pure Python,
no build deps). raise, focus, minimize, maximize, move, resize, and close are
EWMH/ICCCM client messages. The clipboard shells out to xclip/xsel/wl-clipboard
and returns text bytes unchanged.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from contextlib import contextmanager


# ---------------------------------------------------------------------------
# X / EWMH plumbing (python-xlib, lazily imported)
# ---------------------------------------------------------------------------


def _display():
    from Xlib import display as _xd

    return _xd.Display()


def _close_display(d) -> None:
    """Drop one X connection. A fake display with no ``close`` is left alone.

    Every probe opens its own connection. Leaving them open fills the
    server's client table; the server then closes sockets and the next
    flush is ``BrokenPipeError``.
    """
    close = getattr(d, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        pass


@contextmanager
def _open_display():
    d = _display()
    try:
        yield d
    finally:
        _close_display(d)


def _atom(d, name: str):
    return d.intern_atom(name)


def _prop(win, d, name: str):
    """Return the raw value list of an X property, or None."""
    try:
        p = win.get_full_property(_atom(d, name), 0)  # 0 = AnyPropertyType
    except Exception:
        return None
    return p.value if p is not None else None


def _comm_for_pid(pid: int) -> str | None:
    """Process comm name for ``pid`` (e.g. "gedit"), the permission-keying id."""
    if not pid:
        return None
    try:
        with open(f"/proc/{int(pid)}/comm", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip() or None
    except OSError:
        pass
    try:  # fall back to the exe basename
        return os.path.basename(os.readlink(f"/proc/{int(pid)}/exe")) or None
    except OSError:
        return None


def _pid_of(win, d) -> int:
    val = _prop(win, d, "_NET_WM_PID")
    return int(val[0]) if val else 0


def _wm_class_instance(win, d) -> str:
    """WM_CLASS instance (the first of the two NUL-terminated strings).

    A window with no ``_NET_WM_PID`` still has this name when the client set
    it. xmessage's instance is ``xmessage``. An unreadable property is "".
    """
    val = _prop(win, d, "WM_CLASS")
    if not val:
        return ""
    if isinstance(val, str):
        return val.split("\x00", 1)[0].strip()
    try:
        raw = bytes(val)
    except Exception:
        return ""
    return raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()


def _app_id(win, d) -> str:
    """Permission-keying app id: process comm, else the WM_CLASS instance."""
    return _comm_for_pid(_pid_of(win, d)) or _wm_class_instance(win, d)


def _atom_is(candidate, expected) -> bool:
    if candidate == expected:
        return True
    try:
        return int(candidate) == int(expected)
    except (TypeError, ValueError):
        return str(candidate) == str(expected)


def _is_hidden(win, d) -> bool:
    """True when the window is iconic (ICCCM) or ``_NET_WM_STATE_HIDDEN``.

    A property the display cannot intern (the synthetic tests hand back the
    atom name as a string) is not treated as hidden, so a normal window stays
    on screen.
    """
    state = _prop(win, d, "WM_STATE")
    if state:
        try:
            if int(state[0]) == 3:  # IconicState
                return True
        except (TypeError, ValueError, IndexError):
            pass
    atoms = _prop(win, d, "_NET_WM_STATE")
    if not atoms:
        return False
    try:
        hidden = _atom(d, "_NET_WM_STATE_HIDDEN")
    except Exception:
        return False
    try:
        return any(_atom_is(item, hidden) for item in atoms)
    except Exception:
        return False


def _win_title(win, d) -> str:
    for name in ("_NET_WM_NAME", "WM_NAME"):
        val = _prop(win, d, name)
        if val:
            return val.decode("utf-8", "replace") if isinstance(val, (bytes, bytearray)) else str(val)
    return ""


def _managed_windows(d):
    """Client windows in stacking order (bottom→top) as resource objects."""
    root = d.screen().root
    for name in ("_NET_CLIENT_LIST_STACKING", "_NET_CLIENT_LIST"):
        ids = _prop(root, d, name)
        if ids:
            return [d.create_resource_object("window", int(wid)) for wid in ids]
    return []


def _geometry_on_root(win, d):
    """(x, y, w, h) of ``win`` in root (screen) coordinates, or None.

    The window's own origin is translated INTO root coordinates:
    ``root.translate_coords(win, 0, 0)`` (XTranslateCoordinates src=win,
    dst=root). The other direction, ``win.translate_coords(root, 0, 0)``,
    returns root's origin in window coordinates, i.e. the NEGATED position.
    Under Xvfb with no window manager every window sits at (0, 0), where the
    two are equal, which is how the inverted form shipped; on a real desktop
    it made the act-time hit-test attribute every point to the full-screen
    desktop window (docs/box-testbed.md).
    """
    try:
        g = win.get_geometry()
        coords = d.screen().root.translate_coords(win, 0, 0)
        return int(coords.x), int(coords.y), int(g.width), int(g.height)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public probes (mirror _win_system's surface)
# ---------------------------------------------------------------------------


def frontmost_app_id() -> str:
    """comm name of the active window's owner (e.g. "gedit"); "" if undetectable."""
    try:
        with _open_display() as d:
            active = _prop(d.screen().root, d, "_NET_ACTIVE_WINDOW")
            if not active:
                return ""
            win = d.create_resource_object("window", int(active[0]))
            return _comm_for_pid(_pid_of(win, d)) or ""
    except Exception:
        return ""


def app_at_point_id(x: float, y: float) -> str | None:
    """comm name of the topmost window containing a screen point (act-time hit-test)."""
    try:
        with _open_display() as d:
            for win in reversed(_managed_windows(d)):  # topmost first
                geom = _geometry_on_root(win, d)
                if geom is None:
                    continue
                gx, gy, gw, gh = geom
                if gx <= x < gx + gw and gy <= y < gy + gh:
                    return _comm_for_pid(_pid_of(win, d))
    except Exception:
        return None
    return None


def _launcher_comm(identifier: str, comm: str) -> bool:
    """Whether ``identifier`` names the same process as ``comm``.

    ``/proc/<pid>/comm`` is at most 15 bytes, and a desktop launcher often
    keeps a vendor prefix the process drops: Chrome's binary is
    ``google-chrome`` and its comm is ``chrome``. A longer name that comm
    truncates (``gnome-terminal-server`` → ``gnome-terminal-``) matches the
    same way. A title is not an alias; callers that want titles use
    ``resolve_app``.
    """
    if not identifier or not comm or identifier == comm:
        return False
    if identifier.endswith("-" + comm):
        return True
    return len(comm) == 15 and len(identifier) > 15 and identifier.startswith(comm)


def resolve_app(identifier: str) -> str:
    """Resolve a window title / comm substring to the owning comm name (the
    permission-keying id), or the identifier itself if unmatched.

    A comm substring wins over a launcher alias, and both win over a window
    title: ``krita`` stays Krita even when a Chrome tab is titled
    "Donations | Krita". ``google-chrome`` and "Google Chrome" both resolve to
    the running ``chrome`` process. An unmatched name is returned unchanged so
    a grant or a launch can name an app that has no window yet.
    """
    needle = (identifier or "").lower()
    if not needle:
        return identifier
    by_alias: str | None = None
    by_title: str | None = None
    try:
        with _open_display() as d:
            for win in _managed_windows(d):
                comm = (_app_id(win, d) or "").lower()
                if needle in comm:
                    return comm  # the app itself beats any window that merely names it
                if by_alias is None and comm and _launcher_comm(needle, comm):
                    by_alias = comm
                # A title match is a fallback, never a winner over a comm match:
                # a Chromium tab "Donations | Krita" stacked above Krita's window
                # must not turn `krita` into `chrome`.
                if by_title is None and comm and needle in _win_title(win, d).lower():
                    by_title = comm
    except Exception:
        pass
    return by_alias or by_title or identifier


def pids_matching(identifier: str) -> set[int]:
    """PIDs of the managed windows whose owner comm is ``identifier``.

    A comm substring matches, and so does a launcher alias (``google-chrome``
    for comm ``chrome``). Bridges the two Linux identities: the
    permission-keying app id is the process comm ("python3"), while an AT-SPI
    application registers under its program name ("cuatestapp"), so a
    comm-based lookup must find the a11y application by PID, not by name.
    Titles are deliberately NOT matched here: Runtime already maps a title to
    its comm via `resolve_app`, and a title match at this layer would let any
    window whose title merely mentions the app id (a browser tab
    "gedit - Google Search") hand find_root a foreign application's tree."""
    needle = (identifier or "").lower()
    pids: set[int] = set()
    if not needle:
        return pids
    try:
        with _open_display() as d:
            for win in _managed_windows(d):
                pid = _pid_of(win, d)
                if not pid:
                    continue
                comm = (_comm_for_pid(pid) or "").lower()
                if needle in comm or _launcher_comm(needle, comm):
                    pids.add(pid)
    except Exception:
        pass
    return pids


def running_apps() -> list[dict]:
    """Distinct apps with managed windows: {name, pid, frontmost}."""
    out: list[dict] = []
    try:
        with _open_display() as d:
            active = _prop(d.screen().root, d, "_NET_ACTIVE_WINDOW")
            active_id = int(active[0]) if active else 0
            seen: set[str] = set()
            for win in _managed_windows(d):
                pid = _pid_of(win, d)
                comm = _app_id(win, d)
                if not comm or comm in seen:
                    continue
                seen.add(comm)
                out.append({"bundle_id": comm, "name": comm, "pid": pid,
                            "frontmost": int(win.id) == active_id})
    except Exception:
        return out
    return out


def windows() -> list[dict]:
    """Managed windows: {window_id, app, title, pid, bounds, on_screen}.

    ``app`` is the process comm, or the WM_CLASS instance when the window has
    no pid. A minimized window (ICCCM iconic or ``_NET_WM_STATE_HIDDEN``) has
    ``on_screen`` false and ``bounds`` null, so a caller does not aim at the
    rect it had before it was iconified.
    """
    rows: list[dict] = []
    try:
        with _open_display() as d:
            rows = _window_rows(d)
    except Exception:
        return rows
    return rows


def _window_rows(d) -> list[dict]:
    rows: list[dict] = []
    for win in _managed_windows(d):
        pid = _pid_of(win, d)
        hidden = _is_hidden(win, d)
        geom = None if hidden else _geometry_on_root(win, d)
        bounds = None
        if geom is not None:
            gx, gy, gw, gh = geom
            bounds = {"display_id": 0, "x": gx, "y": gy, "width": gw, "height": gh}
        rows.append({
            "window_id": int(win.id),
            "app": _app_id(win, d),
            "title": _win_title(win, d),
            "pid": pid,
            "bounds": bounds,
            "on_screen": not hidden,
        })
    return rows


def _window_by_id(d, window_id: int):
    """The managed window resource with X id ``window_id``, or None."""
    for win in _managed_windows(d):
        if int(win.id) == int(window_id):
            return win
    return None


def window_owner(window_id: int) -> str | None:
    """App id of managed window ``window_id`` (the permission-keying name).

    The process comm wins. A window with no pid uses its WM_CLASS instance.
    "" when neither can be read. None when no managed window has that id
    (or X is unreachable).
    """
    try:
        with _open_display() as d:
            win = _window_by_id(d, window_id)
            if win is None:
                return None
            return _app_id(win, d)
    except Exception:
        return None


def _client_message(d, win, atom_name: str, data: list) -> None:
    """EWMH/ICCCM client message to the root, so the window manager handles it."""
    from Xlib import X, protocol

    event = protocol.event.ClientMessage(
        window=win, client_type=_atom(d, atom_name),
        data=(32, list(data)),
    )
    mask = X.SubstructureRedirectMask | X.SubstructureNotifyMask
    d.screen().root.send_event(event, event_mask=mask)
    d.flush()


def _send_active_window(d, win) -> None:
    """EWMH: ask the window manager to activate ``win`` (raise + focus)."""
    from Xlib import X

    _client_message(d, win, "_NET_ACTIVE_WINDOW", [1, X.CurrentTime, 0, 0, 0])


@contextmanager
def _with_window(window_id: int):
    """Yield ``(display, window)``. The window is None when the id is not managed.

    The display is closed when the caller returns, including the missing-window
    path. The client message is flushed before that close, so the window
    manager already has the request and the selection of X clients stays bounded.
    """
    with _open_display() as d:
        yield d, _window_by_id(d, window_id)


def raise_window(window_id: int) -> bool:
    """Activate managed window ``window_id`` via ``_NET_ACTIVE_WINDOW``.
    Returns False when no managed window has that id."""
    with _with_window(window_id) as (d, win):
        if win is None:
            return False
        _send_active_window(d, win)
        return True


def focus_window(window_id: int) -> bool:
    """Ask the window manager to focus ``window_id`` (``_NET_ACTIVE_WINDOW``).

    Under a standard EWMH window manager this is the same client message as
    raise: activation raises and focuses, and a minimized window is restored.
    The verb is still distinct so the caller can say which one it asked for.
    Returns False when no managed window has that id.
    """
    return raise_window(window_id)


def minimize_window(window_id: int) -> bool:
    """Iconify ``window_id``: ICCCM ``WM_CHANGE_STATE`` plus ``_NET_WM_STATE_HIDDEN``.

    Returns False when no managed window has that id.
    """
    with _with_window(window_id) as (d, win):
        if win is None:
            return False
        _client_message(d, win, "WM_CHANGE_STATE", [3, 0, 0, 0, 0])  # IconicState
        hidden = _atom(d, "_NET_WM_STATE_HIDDEN")
        _client_message(d, win, "_NET_WM_STATE", [1, hidden, 0, 1, 0])  # _NET_WM_STATE_ADD
        return True


def maximize_window(window_id: int) -> bool:
    """Maximize ``window_id`` vertically and horizontally in one ``_NET_WM_STATE``."""
    with _with_window(window_id) as (d, win):
        if win is None:
            return False
        vert = _atom(d, "_NET_WM_STATE_MAXIMIZED_VERT")
        horz = _atom(d, "_NET_WM_STATE_MAXIMIZED_HORZ")
        _client_message(d, win, "_NET_WM_STATE", [1, vert, horz, 1, 0])
        return True


def move_window(window_id: int, x: int, y: int) -> bool:
    """Move ``window_id`` to root coordinates (x, y) via ``_NET_MOVERESIZE_WINDOW``.

    Gravity is NorthWest. Only the X and Y flags are set, so the window
    manager keeps the current size. Returns False when the id is not managed.
    """
    with _with_window(window_id) as (d, win):
        if win is None:
            return False
        # NorthWestGravity = 1. Flags X=1 and Y=2, shifted into the high byte.
        _client_message(d, win, "_NET_MOVERESIZE_WINDOW", [1 | (3 << 8), int(x), int(y), 0, 0])
        return True


def resize_window(window_id: int, width: int, height: int) -> bool:
    """Resize ``window_id`` via ``_NET_MOVERESIZE_WINDOW`` (width and height flags)."""
    with _with_window(window_id) as (d, win):
        if win is None:
            return False
        # Flags Width=4 and Height=8.
        _client_message(d, win, "_NET_MOVERESIZE_WINDOW", [1 | (12 << 8), 0, 0, int(width), int(height)])
        return True


def close_window(window_id: int) -> bool:
    """Ask the window manager to close ``window_id`` (``_NET_CLOSE_WINDOW``)."""
    with _with_window(window_id) as (d, win):
        if win is None:
            return False
        _client_message(d, win, "_NET_CLOSE_WINDOW", [0, 1, 0, 0, 0])
        return True


def _application_dirs() -> list[str]:
    """XDG application directories, ``XDG_DATA_HOME`` then ``XDG_DATA_DIRS``.

    An unset ``XDG_DATA_DIRS`` uses the spec default. An empty value adds no
    system directories.
    """
    home = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    raw = os.environ.get("XDG_DATA_DIRS")
    if raw is None:
        data_dirs = ["/usr/local/share", "/usr/share"]
    else:
        data_dirs = [part for part in raw.split(os.pathsep) if part]
    return [os.path.join(home, "applications"), *[os.path.join(d, "applications") for d in data_dirs]]


def _desktop_entry(identifier: str) -> tuple[str, str] | None:
    """``(desktop id, path)`` for ``identifier``, or None.

    The id is the filename ``gtk-launch`` takes, including ``.desktop``.
    Hyphens also name a subdirectory (``foo-bar.desktop`` or
    ``applications/foo/bar.desktop``). A name with a path separator is not
    a desktop id.
    """
    name = identifier.strip()
    if (
        not name
        or name != identifier
        or os.sep in name
        or (os.altsep and os.altsep in name)
        or name.startswith("-")
    ):
        return None
    entry = name if name.endswith(".desktop") else f"{name}.desktop"
    if os.path.basename(entry) != entry:
        return None
    stem = entry[: -len(".desktop")]
    parts = stem.split("-")
    relatives = [entry]
    for i in range(1, len(parts)):
        relatives.append(os.path.join(*parts[:i], "-".join(parts[i:]) + ".desktop"))
    for root in _application_dirs():
        for rel in relatives:
            path = os.path.join(root, rel)
            if os.path.isfile(path):
                return entry, path
    return None


_DESKTOP_FIELD_CODE = re.compile(r"^%[fFuUdDnNickvm]$")


def _desktop_exec(path: str) -> list[str] | None:
    """argv from a desktop file's ``Exec`` line, field codes removed.

    None when the file has no Exec, the line does not parse, or the program
    is not on PATH. This is the fallback when ``gtk-launch`` is not installed.
    """
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    in_entry = False
    exec_line = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_entry = stripped == "[Desktop Entry]"
            continue
        if in_entry and stripped.startswith("Exec="):
            exec_line = stripped[len("Exec="):].strip()
            break
    if not exec_line:
        return None
    try:
        parts = shlex.split(exec_line, posix=True)
    except ValueError:
        return None
    argv = [part for part in parts if not _DESKTOP_FIELD_CODE.match(part)]
    if not argv:
        return None
    program = shutil.which(argv[0])
    if not program:
        return None
    return [program, *argv[1:]]


def _spawn(argv: list[str], identifier: str) -> None:
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise ComputerUseError(
            ErrorCode.APP_NOT_FOUND,
            f"could not launch {identifier!r}: {exc}",
            detail={"app": identifier},
        ) from exc


def launch_app(identifier: str) -> None:
    """Launch ``identifier`` or raise `ErrorCode.APP_NOT_FOUND` immediately.

    An executable on PATH is started directly. Otherwise a matching desktop
    file is started with ``gtk-launch``, or with its ``Exec`` line when
    ``gtk-launch`` is not installed. A name that is neither is not handed to
    ``xdg-open`` and does not wait for a window.
    """
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    executable = shutil.which(identifier) if identifier else None
    if executable:
        _spawn([executable], identifier)
        return
    found = _desktop_entry(identifier) if identifier else None
    if found is not None:
        desktop_id, path = found
        opener = shutil.which("gtk-launch")
        if opener:
            _spawn([opener, desktop_id], identifier)
            return
        argv = _desktop_exec(path)
        if argv:
            _spawn(argv, identifier)
            return
    raise ComputerUseError(
        ErrorCode.APP_NOT_FOUND,
        f"could not launch {identifier!r}: not on PATH",
        detail={"app": identifier},
    )


def activate_app(identifier: str) -> str:
    """Raise+focus a window whose comm/title matches ``identifier`` (EWMH
    _NET_ACTIVE_WINDOW client message). Returns the resolved app id.

    No matching window is ``app_not_found``. The call does not report that
    it activated an app that was never launched. That answer does not build
    an X client message, so a desktop with no matching window does not need
    the X connection beyond the window list.
    """
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    needle = (identifier or "").lower()
    with _open_display() as d:
        resolved = identifier
        matched = None
        for win in _managed_windows(d):
            comm = (_app_id(win, d) or "").lower()
            title = _win_title(win, d).lower()
            if needle and (needle in comm or needle in title):
                resolved = comm or identifier
                matched = win
                break
        if matched is None:
            raise ComputerUseError(
                ErrorCode.APP_NOT_FOUND,
                f"no running application matches {identifier!r}",
                detail={"app": identifier},
            )
        _send_active_window(d, matched)
        return resolved


# ---------------------------------------------------------------------------
# Clipboard (xclip / xsel / wl-clipboard)
# ---------------------------------------------------------------------------


# Text targets, most specific first. X11 atoms and Wayland MIME types.
_TEXT_TARGETS = (
    "UTF8_STRING",
    "text/plain;charset=utf-8",
    "text/plain",
    "STRING",
    "TEXT",
)


def _clip_kind() -> str | None:
    """Which clipboard reader is installed: xclip, xsel, or wl-paste."""
    if shutil.which("xclip"):
        return "xclip"
    if shutil.which("xsel"):
        return "xsel"
    if shutil.which("wl-paste"):
        return "wl-paste"
    return None


def _clip_writer() -> list[str] | None:
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-i"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"]
    if shutil.which("wl-copy"):
        return ["wl-copy"]
    return None


def _clipboard_error(reason: str, message: str, **detail):
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    payload = {"platform": "linux", "reason": reason}
    payload.update(detail)
    return ComputerUseError(ErrorCode.UNSUPPORTED, message, detail=payload)


def _no_clipboard_tool():
    return _clipboard_error(
        "missing_clipboard_tool",
        "no clipboard tool found; install xclip, xsel, or wl-clipboard",
        hint="apt install xclip, or apt install xsel, or apt install wl-clipboard",
    )


def _targets_cmd(kind: str) -> list[str]:
    if kind == "xclip":
        return ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]
    if kind == "xsel":
        return ["xsel", "--clipboard", "--output", "--target", "TARGETS"]
    return ["wl-paste", "--list-types"]


def _read_cmd(kind: str, target: str) -> list[str]:
    if kind == "xclip":
        return ["xclip", "-selection", "clipboard", "-t", target, "-o"]
    if kind == "xsel":
        return ["xsel", "--clipboard", "--output", "--target", target]
    # --no-newline keeps a clipboard that does not end in a newline exact.
    return ["wl-paste", "--no-newline", "--type", target]


def _run_clip(cmd: list[str], payload: bytes | None = None, *, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a clipboard tool. Reads capture bytes. Writes discard them.

    ``text`` is never set, so CR and CRLF are not rewritten. A write passes
    the bytes on stdin and sends stdout and stderr to ``DEVNULL``. xclip,
    xsel, and wl-copy fork a child that keeps owning the selection; if that
    child inherits captured pipes, ``communicate`` never sees EOF and the
    call times out. ``-quiet`` is not used: it keeps xclip in the foreground,
    so the call would not return while the selection lived. With the pipes
    discarded, the parent exits, the child still owns the selection, and a
    later read returns the same bytes.
    """
    if capture:
        return subprocess.run(cmd, input=payload, capture_output=True, timeout=5, check=False)
    return subprocess.run(
        cmd,
        input=payload,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )


def _target_lines(stdout: bytes) -> list[str]:
    text = (stdout or b"").decode("utf-8", "replace")
    return [line.strip() for line in text.splitlines() if line.strip()]


def _pick_text_target(targets: list[str]) -> str | None:
    present = {name.lower(): name for name in targets}
    for name in _TEXT_TARGETS:
        found = present.get(name.lower())
        if found is not None:
            return found
    return None


def read_clipboard() -> str:
    """Clipboard text, byte for byte, decoded as UTF-8.

    CR and CRLF are preserved. A text target that exists and holds zero bytes
    returns "". Every other empty-looking case raises `ErrorCode.UNSUPPORTED`:

    * no xclip, xsel, or wl-paste on PATH (reason ``missing_clipboard_tool``);
    * targets exist and none of them are text (``clipboard_not_text``);
    * the text bytes are not valid UTF-8 (``clipboard_invalid_utf8``);
    * the clipboard has no owner, or the text target is not available
      (``clipboard_no_owner``). That last one is not the zero-byte text case.
    """
    kind = _clip_kind()
    if kind is None:
        raise _no_clipboard_tool()
    try:
        listed = _run_clip(_targets_cmd(kind))
    except Exception as exc:
        raise _clipboard_error(
            "clipboard_no_owner",
            f"clipboard targets could not be read: {exc}",
        ) from exc
    targets = _target_lines(listed.stdout)
    if listed.returncode != 0 or not targets:
        raise _clipboard_error(
            "clipboard_no_owner",
            "the clipboard has no owner, or no text target is available",
            hint="A text clipboard that holds zero bytes is a different result and returns an empty string.",
        )
    target = _pick_text_target(targets)
    if target is None:
        shown = ", ".join(targets[:8])
        raise _clipboard_error(
            "clipboard_not_text",
            f"the clipboard holds non-text data ({shown})",
            targets=targets[:16],
        )
    try:
        read = _run_clip(_read_cmd(kind, target))
    except Exception as exc:
        raise _clipboard_error(
            "clipboard_no_owner",
            f"clipboard text could not be read: {exc}",
        ) from exc
    if read.returncode != 0:
        raise _clipboard_error(
            "clipboard_no_owner",
            "the clipboard has no owner, or the text target is not available",
        )
    data = read.stdout or b""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _clipboard_error(
            "clipboard_invalid_utf8",
            "the clipboard text is not valid UTF-8",
            hint="The bytes were left unchanged; they were not replaced with U+FFFD.",
        ) from exc


def write_clipboard(text: str) -> None:
    """Write ``text`` as UTF-8 bytes. A lone surrogate is `ValueError`.

    No clipboard tool is `ErrorCode.UNSUPPORTED` (reason
    ``missing_clipboard_tool``), naming xclip, xsel, and wl-clipboard.
    """
    try:
        payload = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "clipboard text must be valid Unicode; a lone surrogate cannot be encoded as UTF-8"
        ) from exc
    cmd = _clip_writer()
    if cmd is None:
        raise _no_clipboard_tool()
    try:
        _run_clip(cmd, payload, capture=False)
    except Exception as exc:
        raise _clipboard_error("clipboard_write_failed", f"clipboard write failed: {exc}") from exc
