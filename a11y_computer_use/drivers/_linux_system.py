"""Linux system/windowing probes: foreground app, app-under-point, window and
app enumeration, activate/launch, and clipboard. Linux-only; used by server.py's
platform-dispatching helpers so the Runtime's gating works on Linux.

App identity on Linux is the process comm name (e.g. "gedit", "chrome") read
from ``/proc/<pid>/comm`` — the analog of a macOS bundle id / Windows exe for
permission-keying. Comm is at most 15 bytes. A launcher the process drops
(``google-chrome`` runs as ``chrome``) and a name cut at that limit
(``gnome-terminal-server`` runs as ``gnome-terminal-``) resolve to the comm.
Window/desktop facts come from EWMH properties over python-xlib (pure Python,
no build deps); clipboard shells out to xclip/xsel/wl-clipboard.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess


# ---------------------------------------------------------------------------
# X / EWMH plumbing (python-xlib, lazily imported)
# ---------------------------------------------------------------------------


def _display():
    from Xlib import display as _xd

    return _xd.Display()


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
        d = _display()
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
        d = _display()
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
        d = _display()
        for win in _managed_windows(d):
            comm = (_comm_for_pid(_pid_of(win, d)) or "").lower()
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
        d = _display()
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
        d = _display()
        active = _prop(d.screen().root, d, "_NET_ACTIVE_WINDOW")
        active_id = int(active[0]) if active else 0
        seen: set[str] = set()
        for win in _managed_windows(d):
            pid = _pid_of(win, d)
            comm = _comm_for_pid(pid)
            if not comm or comm in seen:
                continue
            seen.add(comm)
            out.append({"bundle_id": comm, "name": comm, "pid": pid,
                        "frontmost": int(win.id) == active_id})
    except Exception:
        return out
    return out


def windows() -> list[dict]:
    """Managed windows: {window_id, app, title, pid, bounds}."""
    rows: list[dict] = []
    try:
        d = _display()
        for win in _managed_windows(d):
            pid = _pid_of(win, d)
            geom = _geometry_on_root(win, d)
            bounds = None
            if geom is not None:
                gx, gy, gw, gh = geom
                bounds = {"display_id": 0, "x": gx, "y": gy, "width": gw, "height": gh}
            rows.append({
                "window_id": int(win.id),
                "app": _comm_for_pid(pid) or "",
                "title": _win_title(win, d),
                "pid": pid,
                "bounds": bounds,
            })
    except Exception:
        return rows
    return rows


def _window_by_id(d, window_id: int):
    """The managed window resource with X id ``window_id``, or None."""
    for win in _managed_windows(d):
        if int(win.id) == int(window_id):
            return win
    return None


def window_owner(window_id: int) -> str | None:
    """comm name of the process owning managed window ``window_id`` (the
    permission-keying app id), "" when the pid is unreadable, None when no
    managed window has that id (or X is unreachable)."""
    try:
        d = _display()
        win = _window_by_id(d, window_id)
        if win is None:
            return None
        return _comm_for_pid(_pid_of(win, d)) or ""
    except Exception:
        return None


def _send_active_window(d, win) -> None:
    """EWMH: ask the window manager to activate ``win`` (raise + focus)."""
    from Xlib import X, protocol

    event = protocol.event.ClientMessage(
        window=win, client_type=_atom(d, "_NET_ACTIVE_WINDOW"),
        data=(32, [1, X.CurrentTime, 0, 0, 0]),
    )
    mask = X.SubstructureRedirectMask | X.SubstructureNotifyMask
    d.screen().root.send_event(event, event_mask=mask)
    d.flush()


def raise_window(window_id: int) -> bool:
    """Activate managed window ``window_id`` via ``_NET_ACTIVE_WINDOW``.
    Returns False when no managed window has that id."""
    d = _display()
    win = _window_by_id(d, window_id)
    if win is None:
        return False
    _send_active_window(d, win)
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
    d = _display()
    resolved = identifier
    matched = None
    for win in _managed_windows(d):
        comm = (_comm_for_pid(_pid_of(win, d)) or "").lower()
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
    from Xlib import X, protocol

    root = d.screen().root
    event = protocol.event.ClientMessage(
        window=matched, client_type=_atom(d, "_NET_ACTIVE_WINDOW"),
        data=(32, [1, X.CurrentTime, 0, 0, 0]),
    )
    mask = X.SubstructureRedirectMask | X.SubstructureNotifyMask
    root.send_event(event, event_mask=mask)
    d.flush()
    return resolved


# ---------------------------------------------------------------------------
# Clipboard (xclip / xsel / wl-clipboard)
# ---------------------------------------------------------------------------


def _clip_reader() -> list[str] | None:
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-o"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--output"]
    if shutil.which("wl-paste"):
        return ["wl-paste", "--no-newline"]
    return None


def _clip_writer() -> list[str] | None:
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-i"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"]
    if shutil.which("wl-copy"):
        return ["wl-copy"]
    return None


def read_clipboard() -> str | None:
    cmd = _clip_reader()
    if cmd is None:
        return None
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None


def write_clipboard(text: str) -> None:
    cmd = _clip_writer()
    if cmd is None:
        raise RuntimeError("no clipboard tool found; install xclip, xsel, or wl-clipboard")
    try:
        subprocess.run(cmd, input=text, text=True, timeout=5, check=False)
    except Exception as exc:  # pragma: no cover - environmental
        raise RuntimeError(f"clipboard write failed: {exc}") from exc
