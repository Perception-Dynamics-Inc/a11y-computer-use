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
import sys
import threading
import time
from contextlib import contextmanager

from a11y_computer_use.schema import ComputerUseError


# ---------------------------------------------------------------------------
# X / EWMH plumbing (python-xlib, lazily imported)
# ---------------------------------------------------------------------------


def xlib_required() -> bool:
    """True when a failed ``Xlib`` import is a missing Linux dependency.

    python-xlib is installed only on Linux. The synthetic Linux-driver
    suite also runs on macOS and Windows, where the import fails because
    the marker did not select the package. That is not a Linux session
    with the backend missing, so those probes stay empty.
    """
    return sys.platform.startswith("linux")


def missing_xlib(exc: BaseException):
    """Typed error for a process that cannot import python-xlib.

    An empty app or window list used to be ``confirmed`` in this case, and a
    LibreOffice snapshot waited out the registration budget and then blamed
    the gtk3 bridge. The hint names the package a plain install already
    depends on when the marker is honored.
    """
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    return ComputerUseError(
        ErrorCode.UNSUPPORTED,
        "python-xlib is not installed, so X11 windows cannot be listed",
        detail={
            "reason": "missing_dependency",
            "module": "Xlib",
            "hint": "pip install python-xlib",
            "error": str(exc),
        },
    )


def _display():
    try:
        from Xlib import display as _xd
    except ImportError as exc:
        if xlib_required():
            raise missing_xlib(exc) from exc
        raise
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


# ``/proc/<pid>/comm`` is at most 15 bytes (TASK_COMM_LEN includes the NUL).
_COMM_MAX = 15


def _cmdline_basename(pid: int) -> str | None:
    """argv[0] basename from ``/proc/<pid>/cmdline``, or None."""
    if not pid:
        return None
    try:
        raw = open(f"/proc/{int(pid)}/cmdline", "rb").read()
    except OSError:
        return None
    if not raw:
        return None
    field = raw.split(b"\x00", 1)[0]
    if not field:
        return None
    name = os.path.basename(field.decode("utf-8", "replace")).strip()
    return name or None


def _exe_basename(pid: int) -> str | None:
    """Basename of ``/proc/<pid>/exe``, or None when the link cannot be read."""
    if not pid:
        return None
    try:
        target = os.readlink(f"/proc/{int(pid)}/exe")
    except OSError:
        return None
    name = os.path.basename(target).strip()
    if name.endswith(" (deleted)"):
        name = name[: -len(" (deleted)")].strip()
    return name or None


def _comm_cuts(name: str, comm: str) -> bool:
    """True when ``comm`` is ``name`` or the kernel's 15-byte cut of ``name``.

    The cut is byte-exact. A different program that merely shares a prefix
    (``gnome-text-editor-nightly`` versus ``gnome-text-editor``) is not this
    name, even though both comms are ``gnome-text-edit``.
    """
    if not name or not comm:
        return False
    if name == comm:
        return True
    try:
        raw = name.encode("utf-8")
        cut = comm.encode("utf-8")
    except UnicodeError:
        return False
    return len(cut) == _COMM_MAX and len(raw) > _COMM_MAX and raw.startswith(cut)


def _identity_for_pid(pid: int, comm: str | None) -> str | None:
    """Program name grants and listings use for ``pid``.

    When ``comm`` is the 15-byte cut of the cmdline argv0 or the exe basename,
    that full name is the id. cmdline wins when both are the same extension.
    When they name two different longer programs, ``comm`` is kept: picking
    either one would grant a program the user did not name. A short comm
    (``chrome``, ``gedit``) is unchanged, and so is a pid whose cmdline and
    exe cannot be read.
    """
    if not comm or len(comm.encode("utf-8", "replace")) != _COMM_MAX:
        return comm
    if not pid:
        return comm
    cmdline = _cmdline_basename(pid)
    exe = _exe_basename(pid)

    def extends(name: str | None) -> bool:
        return bool(name) and name != comm and _comm_cuts(name, comm)

    if extends(cmdline) and extends(exe) and cmdline != exe:
        return comm
    if extends(cmdline):
        return cmdline
    if extends(exe):
        return exe
    return comm


# A client can map a window before ``_NET_WM_PID`` is visible on another
# connection. One read then reports pid 0 and the app id falls back to
# WM_CLASS. Wait once per window id, then remember that it stayed unset so
# a later list does not pay the wait again. A display with no window
# manager does not wait.
_PID_WAIT_S = 0.4
_PID_POLL_S = 0.02
_pid_absent: set[int] = set()


def _read_pid(win, d) -> int:
    val = _prop(win, d, "_NET_WM_PID")
    if not val:
        return 0
    try:
        return int(val[0]) or 0
    except (TypeError, ValueError, IndexError):
        return 0


def _pid_of(win, d) -> int:
    """``_NET_WM_PID``, or 0 when the window has none.

    Callers that only record the property monkeypatch this function as
    ``_pid_of(win, d)``. The window list waits in ``_settled_pid`` instead,
    so that patch keeps working.
    """
    return _read_pid(win, d)


def _settled_pid(win, d) -> int:
    """``_pid_of``, after a short re-read when the property is still missing."""
    pid = _pid_of(win, d)
    if pid:
        _pid_absent.discard(int(win.id))
        return pid
    wid = int(win.id)
    if wid in _pid_absent or _active_window_property(d) is None:
        return 0
    deadline = time.monotonic() + _PID_WAIT_S
    while time.monotonic() < deadline:
        time.sleep(_PID_POLL_S)
        pid = _pid_of(win, d)
        if pid:
            return pid
    _pid_absent.add(wid)
    return 0


def _wm_class_strings(win, d) -> tuple[str, str]:
    """(instance, class) from WM_CLASS. Missing or unreadable parts are ""."""
    val = _prop(win, d, "WM_CLASS")
    if not val:
        return "", ""
    if isinstance(val, str):
        parts = val.split("\x00")
    else:
        try:
            raw = bytes(val)
        except Exception:
            return "", ""
        parts = raw.split(b"\x00")
        parts = [part.decode("utf-8", "replace") for part in parts]
    instance = parts[0].strip() if parts else ""
    klass = parts[1].strip() if len(parts) > 1 else ""
    return instance, klass


def _wm_class_instance(win, d) -> str:
    """WM_CLASS instance (the first of the two NUL-terminated strings).

    A window with no ``_NET_WM_PID`` still has this name when the client set
    it. xmessage's instance is ``xmessage``. An unreadable property is "".
    """
    return _wm_class_strings(win, d)[0]


def _is_dialog_window(win, d) -> bool:
    """True for ``_NET_WM_WINDOW_TYPE_DIALOG`` or a window transient for a parent.

    A save prompt is often an AT-SPI alert and also a dialog window. Either
    signal is enough for ``app quit`` to report unsaved changes. A normal
    top-level window is neither.
    """
    types = _prop(win, d, "_NET_WM_WINDOW_TYPE")
    if types:
        try:
            dialog = _atom(d, "_NET_WM_WINDOW_TYPE_DIALOG")
        except Exception:
            dialog = "_NET_WM_WINDOW_TYPE_DIALOG"
        try:
            if any(_atom_is(item, dialog) for item in types):
                return True
        except Exception:
            pass
    transient = _prop(win, d, "WM_TRANSIENT_FOR")
    if not transient:
        return False
    try:
        return int(transient[0]) != 0
    except (TypeError, ValueError, IndexError):
        return True


_PORTAL_BACKENDS = frozenset({
    "xdg-desktop-portal-gtk", "xdg-desktop-portal-gnome", "xdg-desktop-portal-kde",
})


def _app_id(win, d, *, settle: bool = False) -> str:
    """Permission-keying app id: full program name, else comm, else WM_CLASS.

    The full name comes from ``/proc/<pid>/cmdline`` or the exe when the
    kernel comm is that name's 15-byte cut. A short comm is the id, which is
    how ``chrome`` stays ``chrome``. A recognized portal backend dialog
    uses its managed transient parent's process identity.
    """
    pid = _settled_pid(win, d) if settle else _pid_of(win, d)
    comm = _comm_for_pid(pid)
    if comm:
        identity = _identity_for_pid(pid, comm) or comm
        # Portal backends serve many apps. Only a managed transient dialog
        # inherits its explicit parent's identity; never grant the backend.
        if identity in _PORTAL_BACKENDS:
            parent = _prop(win, d, "WM_TRANSIENT_FOR")
            if parent and int(parent[0]) != int(win.id):
                owner = _window_by_id(d, int(parent[0]))
                if owner is not None:
                    owner_pid = _pid_of(owner, d)
                    owner_comm = _comm_for_pid(owner_pid)
                    owner_id = _identity_for_pid(owner_pid, owner_comm)
                    if owner_id and not owner_id.startswith("xdg-desktop-portal"):
                        return owner_id
        return identity
    return _wm_class_instance(win, d)


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
    """(x, y, w, h) of the client window in root (screen) coordinates, or None.

    This is the X client window, inside the frame. ``window list`` reports
    this origin, and ``window move`` places this origin. The window manager
    frame (title bar and borders) is not included.

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
    """App id of the active window's owner; "" if undetectable.

    The id is the full program name when ``/proc/<pid>/comm`` is the 15-byte
    cut of that name (``gnome-text-editor``, not ``gnome-text-edit``). A short
    comm stays the comm. A window with no pid is not a frontmost app here,
    even when ``active_window`` can still name it by WM_CLASS. A missing
    python-xlib is an error, not an empty frontmost app.
    """
    from a11y_computer_use.schema import ComputerUseError

    try:
        with _open_display() as d:
            active = _prop(d.screen().root, d, "_NET_ACTIVE_WINDOW")
            if not active:
                return ""
            win = d.create_resource_object("window", int(active[0]))
            pid = _pid_of(win, d)
            comm = _comm_for_pid(pid)
            if not comm:
                return ""
            return _app_id(win, d)
    except ComputerUseError:
        raise
    except Exception:
        return ""


def active_window() -> dict | None:
    """The EWMH ``_NET_ACTIVE_WINDOW``, or None when there is no active window.

    ``window_id`` is the X id. ``app`` is the process comm, or the WM_CLASS
    instance when the window has no pid. ``pid`` is 0 when ``_NET_WM_PID`` is
    missing. A window id of 0 is None: that is how a window manager reports
    that nothing is active.
    """
    try:
        with _open_display() as d:
            active = _prop(d.screen().root, d, "_NET_ACTIVE_WINDOW")
            if not active or not int(active[0]):
                return None
            win = d.create_resource_object("window", int(active[0]))
            pid = _pid_of(win, d)
            return {
                "window_id": int(win.id),
                "app": _app_id(win, d),
                "pid": pid,
                "title": _win_title(win, d),
            }
    except ComputerUseError:
        raise
    except Exception:
        return None


# Desktop shells also own harmless panels/settings. Only their identified run
# surfaces require exec; a document merely titled "Run Program" is not one.
_COMMAND_DIALOG_HOSTS = {"xfce4-panel", "gnome-panel", "mate-panel", "gnome-shell", "plasmashell"}
_COMMAND_DIALOG_IDS = {
    "run", "run-dialog", "run_dialog", "rundialog", "panel-run-dialog", "panel_run_dialog",
    "gnome-panel-run-dialog", "xfce4-run-dialog", "run-command",
    "xfce4-appfinder", "krunner",
}
_COMMAND_DIALOG_TITLES = {"run program", "run application", "run a command", "run command"}


def command_launcher_open(app: str) -> bool:
    """Recognize panel-owned run windows by owner and native class/role.

    Conservatively gate the host while its run window is mapped: popup Paste
    items can be outside that window's accessibility ancestry.
    """
    if app.casefold() not in _COMMAND_DIALOG_HOSTS:
        return False
    with _open_display() as d:
        for win in _managed_windows(d):
            if _app_id(win, d).casefold() != app.casefold() or _is_hidden(win, d):
                continue
            instance, klass = _wm_class_strings(win, d)
            role = _prop(win, d, "WM_WINDOW_ROLE") or b""
            if not isinstance(role, str):
                role = bytes(role).decode("utf-8", "replace")
            identities = {instance.casefold(), klass.casefold(), role.strip("\x00").casefold()}
            if identities & _COMMAND_DIALOG_IDS:
                return True
            if (_is_dialog_window(win, d)
                    and _win_title(win, d).strip().casefold() in _COMMAND_DIALOG_TITLES):
                return True
    return False


def _window_at_point(d, x: float, y: float):
    """Topmost on-screen managed window containing a screen point, or None.

    A minimized window keeps the rectangle it had before it was iconified.
    That rectangle is not on screen, so it is not a hit and not a cover.
    """
    for win in reversed(_managed_windows(d)):  # topmost first
        if _is_hidden(win, d):
            continue
        geom = _geometry_on_root(win, d)
        if geom is None:
            continue
        gx, gy, gw, gh = geom
        if gx <= x < gx + gw and gy <= y < gy + gh:
            return win
    return None


def _top_window_pid(x: float, y: float) -> int | None:
    """Pid of the topmost client window containing a screen point, or None."""
    try:
        with _open_display() as d:
            win = _window_at_point(d, x, y)
            if win is None:
                return None
            pid = _pid_of(win, d)
            return int(pid) if pid else None
    except ComputerUseError:
        raise
    except Exception:
        return None
    return None


def drag_overlay_at_point(x: float, y: float) -> tuple[int, str] | None:
    """Identity of a full-screen unmanaged window at a drag's start.

    Region selectors are override-redirect windows: neither EWMH's active
    window nor its managed client list identifies their owner. Read only the
    root's stacking list and native rectangles, never accessibility subtrees.
    A managed window above an overlay prevents using that covered overlay.
    """
    from Xlib import X

    with _open_display() as d:
        root = d.screen().root
        screen = root.get_geometry()
        for win in reversed(root.query_tree().children):
            try:
                attrs = win.get_attributes()
                if attrs.map_state != X.IsViewable or attrs.win_class != X.InputOutput:
                    continue
                bounds = _geometry_on_root(win, d)
                if bounds is None:
                    continue
                left, top, width, height = bounds
                if not (left <= x < left + width and top <= y < top + height):
                    continue
                if not attrs.override_redirect:
                    return None
                # Limit this ownership path to full-screen selectors. Menus,
                # tooltips and small utility windows do not establish a
                # region-selection owner (and may be input-transparent).
                if bounds != (0, 0, screen.width, screen.height):
                    continue
                # Missing ownership must not fall through to a granted app
                # underneath the overlay.
                return int(win.id), _app_id(win, d) or "unknown"
            except Exception as exc:
                # Never replace unreadable ownership with the granted app
                # underneath it.
                from a11y_computer_use.schema import ErrorCode

                raise ComputerUseError(
                    ErrorCode.FOCUS_CHANGED,
                    "the drag window owner could not be read; re-observe",
                    detail={"reason": "drag_overlay_unreadable"},
                ) from exc
    return None


def app_at_point_id(x: float, y: float) -> str | None:
    """App id of the topmost window containing a screen point.

    The process comm when the window has a pid. A Tk or Xt window has no
    ``_NET_WM_PID``; the hit-test then uses its WM_CLASS instance, the same
    key ``window focus`` gates on. None when the point hits no window, or
    the window has neither a comm nor a class.
    """
    try:
        with _open_display() as d:
            win = _window_at_point(d, x, y)
            if win is None:
                return None
            return _app_id(win, d) or None
    except ComputerUseError:
        raise
    except Exception:
        return None


def pid_at_point(x: float, y: float) -> int | None:
    """Pid of the topmost client window containing a screen point."""
    return _top_window_pid(x, y)


def window_at_point(x: float, y: float) -> dict | None:
    """Identity and title of the topmost managed window at a screen point."""
    try:
        with _open_display() as d:
            win = _window_at_point(d, x, y)
            if win is None:
                return None
            return {"window_id": int(win.id), "title": _win_title(win, d)}
    except Exception:
        return None


def window_cover_rows() -> list[dict]:
    """Read stacking once for snapshot covers, without process identity scans.

    Do not wait for PID settlement; missing ownership stays unknown.
    """
    rows = []
    try:
        with _open_display() as d:
            for win in _managed_windows(d):
                if _is_hidden(win, d):
                    continue
                geom = _geometry_on_root(win, d)
                if geom is None:
                    continue
                x, y, width, height = geom
                rows.append({
                    "window_id": int(win.id), "title": _win_title(win, d),
                    "pid": _pid_of(win, d), "on_screen": True,
                    "bounds": {"x": x, "y": y, "width": width, "height": height},
                })
    except Exception:
        return []
    return rows


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


def _identity_needles(identifier: str) -> list[str]:
    """Names a window may use for ``identifier``.

    An absolute path is also the binary's basename. ``/usr/bin/mousepad``
    matches a window whose comm or WM_CLASS is ``mousepad``, the same names
    ``launch_app`` records on the process handle.
    """
    text = (identifier or "").strip()
    if not text:
        return []
    folded = text.lower()
    names = [folded]
    if "/" in text:
        base = os.path.basename(folded)
        if base and base not in names:
            names.append(base)
    return names


def _names_program(needle: str, name: str) -> bool:
    """Whether ``name`` holds ``needle`` as a program name, not a prefix of a longer one.

    ``needle`` matches the whole name or a trailing part of it (``terminal``
    in ``xfce4-terminal``, ``chrome`` in ``google chrome``). A name that
    extends ``needle`` with more name characters is a different program:
    ``python`` is not ``python3``, ``firefox`` is not ``firefox-esr``,
    ``code`` is not ``code-insiders`` and ``gimp`` is not ``gimp-2.10``. An
    app that stopped answering or exited must not resolve to such a
    neighbour (#1191).
    """
    if not needle or not name:
        return False
    if needle == name:
        return True
    return re.search(re.escape(needle) + r"(?![\w.-])", name) is not None


def _comm_matches_identifier(identifier: str, comm: str) -> bool:
    """True when ``comm`` is the process ``identifier`` names.

    A comm that holds the name (see ``_names_program``) matches, and so
    does a launcher alias. A path matches
    by its basename as well as the full string.
    """
    folded = (comm or "").lower()
    if not folded:
        return False
    from a11y_computer_use.app_identity import shares_alias

    if shares_alias(identifier, folded):
        return True
    for needle in _identity_needles(identifier):
        if _names_program(needle, folded) or _launcher_comm(needle, folded):
            return True
    return False


def _identifier_matches_process(identifier: str, identity: str) -> bool:
    """True when ``identifier`` names the process whose id is ``identity``.

    ``identity`` is the full program name when the comm was its 15-byte cut,
    and the comm otherwise. A long name matches that exact name, an alias,
    or the 15-byte comm of that same name. It does not match a different
    program that shares the prefix, and a shorter token inside the name
    (``text`` inside ``gnome-text-editor``) is not a match. A short comm
    keeps the launcher rule and the ``_names_program`` rule, so ``python``
    never matches ``python3``.
    """
    folded = (identity or "").lower()
    if not folded:
        return False
    from a11y_computer_use.app_identity import shares_alias

    if shares_alias(identifier, folded):
        return True
    long_name = len(folded.encode("utf-8", "replace")) > _COMM_MAX
    for needle in _identity_needles(identifier):
        if needle == folded:
            return True
        if long_name:
            if _comm_cuts(folded, needle):
                return True
            if needle.endswith("-" + folded):
                return True
            continue
        if _names_program(needle, folded) or _launcher_comm(needle, folded):
            return True
    return False


def _unique_identity(identifier: str, hits: list[str]) -> str | None:
    """The one program ``hits`` name, or None when they disagree.

    One id is that id. An exact spelling of ``identifier`` wins when several
    windows matched. Two different full names that share a truncated comm are
    not collapsed into either name.
    """
    ordered: list[str] = []
    for hit in hits:
        if hit and hit not in ordered:
            ordered.append(hit)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    wanted = (identifier or "").lower()
    exact = [hit for hit in ordered if hit.lower() == wanted]
    if len(exact) == 1:
        return exact[0]
    return None


def _class_matches_identifier(identifier: str, instance: str, klass: str) -> bool:
    """True when WM_CLASS instance or class is one of ``identifier``'s names."""
    inst = (instance or "").lower()
    cls = (klass or "").lower()
    from a11y_computer_use.app_identity import shares_alias

    if shares_alias(identifier, inst) or shares_alias(identifier, cls):
        return True
    for needle in _identity_needles(identifier):
        if not needle:
            continue
        if needle == inst or needle == cls or _launcher_comm(needle, inst):
            return True
    return False


def resolve_app(identifier: str) -> str:
    """Resolve a window title or program name to the permission-keying id.

    The id is the full program name when the kernel comm is that name's
    15-byte cut, and the comm otherwise. ``gnome-text-editor`` stays
    ``gnome-text-editor``; ``google-chrome`` still resolves to ``chrome``.
    A comm match wins over a window title: ``krita`` stays Krita even when a
    Chrome tab is titled "Donations | Krita". Two different programs that
    share a truncated comm are not collapsed, so a grant for one is not a
    grant for the other. An unmatched name is returned unchanged so a grant
    or a launch can name an app that has no window yet.
    """
    needle = (identifier or "").lower()
    if not needle:
        return identifier
    hits: list[str] = []
    by_class: str | None = None
    by_title: str | None = None
    try:
        with _open_display() as d:
            for win in _managed_windows(d):
                identity = (_app_id(win, d) or "").lower()
                if identity and _identifier_matches_process(identifier, identity):
                    hits.append(identity)
                    continue
                instance, klass = _wm_class_strings(win, d)
                if by_class is None and _class_matches_identifier(identifier, instance, klass):
                    by_class = identity or instance.lower()
                # A title match is a fallback, never a winner over a program
                # match: a Chromium tab "Donations | Krita" stacked above
                # Krita's window must not turn `krita` into `chrome`. A path
                # matches its basename in the title the same way the bare
                # name does.
                if by_title is None and identity:
                    title = _win_title(win, d).lower()
                    if any(name and name in title for name in _identity_needles(identifier)):
                        by_title = identity
    except ComputerUseError:
        raise
    except Exception:
        pass
    chosen = _unique_identity(identifier, hits)
    if chosen:
        return chosen
    return by_class or by_title or identifier


def pids_matching(identifier: str) -> set[int]:
    """PIDs of the managed windows whose program ``identifier`` names.

    The match is the full program name when the comm is its 15-byte cut, and
    the comm otherwise. A launcher alias (``google-chrome`` for comm
    ``chrome``) still matches. A different longer program that shares the
    cut does not. Titles are deliberately NOT matched here: Runtime already
    maps a title to its id via `resolve_app`, and a title match at this layer
    would let any window whose title merely mentions the app id (a browser
    tab "gedit - Google Search") hand find_root a foreign application's tree.
    """
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
                comm = _comm_for_pid(pid) or ""
                identity = (_identity_for_pid(pid, comm) or comm).lower()
                if _identifier_matches_process(identifier, identity):
                    pids.add(pid)
    except ComputerUseError:
        raise
    except Exception:
        pass
    return pids


def running_apps() -> list[dict]:
    """Distinct apps with managed windows: {name, pid, frontmost}.

    A missing python-xlib raises ``missing_dependency``. It is not an empty
    list: callers were reporting that list as confirmed.
    """
    from a11y_computer_use.schema import ComputerUseError

    out: list[dict] = []
    try:
        with _open_display() as d:
            active = _prop(d.screen().root, d, "_NET_ACTIVE_WINDOW")
            active_id = int(active[0]) if active else 0
            seen: set[str] = set()
            for win in _managed_windows(d):
                pid = _settled_pid(win, d)
                comm = _app_id(win, d, settle=True)
                if not comm or comm in seen:
                    continue
                seen.add(comm)
                out.append({"bundle_id": comm, "name": comm, "pid": pid,
                            "frontmost": int(win.id) == active_id})
    except ComputerUseError:
        raise
    except Exception:
        return out
    return out


def windows() -> list[dict]:
    """Managed windows: {window_id, app, title, pid, bounds, on_screen}.

    ``app`` is the full program name (or the process comm when that name is
    not longer than the kernel's 15-byte comm), or the WM_CLASS instance when
    the window has no pid. A missing ``_NET_WM_PID`` is read again before that
    fallback, so
    a window that has just mapped does not list pid 0 while the property is
    still arriving. A minimized window (ICCCM iconic or ``_NET_WM_STATE_HIDDEN``) has
    ``on_screen`` false and ``bounds`` null, so a caller does not aim at the
    rect it had before it was iconified. ``bounds`` is the client window
    (inside the frame), the same origin ``move_window`` places.
    """
    from a11y_computer_use.schema import ComputerUseError

    rows: list[dict] = []
    try:
        with _open_display() as d:
            rows = _window_rows(d)
    except ComputerUseError:
        raise
    except Exception:
        return rows
    return rows


def window_id_for_toplevel(*, title: str, pid: int | None, extents,
                           rows: list[dict] | None = None) -> int | None:
    """X id of the on-screen window that owns an AT-SPI top-level window.

    An exact title within the same pid wins. Several windows with that title,
    or a window with no title, match the box that overlaps ``extents`` the
    most. ``extents`` is ``(x, y, width, height)`` in screen pixels. The X
    client box sits inside the frame, so the match is overlap, not equality.
    None when nothing on screen matches: the caller still runs the action.
    Discovery may supply snapshot-local rows to avoid another native scan.
    """
    rows = [row for row in (windows() if rows is None else rows)
            if row.get("on_screen") and row.get("bounds")]
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        owned = [row for row in rows if int(row.get("pid") or 0) == int(pid)]
        if owned:
            rows = owned
    titled = (title or "").strip()
    if titled:
        named = [row for row in rows if (row.get("title") or "") == titled]
        if len(named) == 1:
            return int(named[0]["window_id"])
        if len(named) > 1:
            picked = _window_overlapping(named, extents)
            if picked is not None:
                return picked
    return _window_overlapping(rows, extents)


def transient_parent_title(*, pid: int, title: str) -> str | None:
    """Resolve a managed window's exact same-process WM_TRANSIENT_FOR parent.

    Require unique titles on both sides to map back to AT-SPI without geometry
    guesses. The dialog need not be active: another document can accept input.
    This relationship establishes ownership, not modality.
    """
    if not pid or not title:
        return None
    try:
        with _open_display() as d:
            owned = [win for win in _managed_windows(d) if _pid_of(win, d) == pid]
            children = [win for win in owned if _win_title(win, d) == title]
            if len(children) != 1:
                return None
            child = children[0]
            transient = _prop(child, d, "WM_TRANSIENT_FOR")
            if transient is None or len(transient) != 1:
                return None
            parents = [win for win in owned if win.id == int(transient[0])
                       and win.id != child.id]
            if len(parents) != 1:
                return None
            name = _win_title(parents[0], d)
            if name and sum(_win_title(win, d) == name for win in owned) == 1:
                return name
    except Exception:
        pass
    return None


def active_transient_dialog(*, pid: int, title: str, parent_title: str) -> bool:
    """Match an active X11 dialog to its exact same-process transient parent.

    Titles must identify unique managed windows; never infer ownership from
    overlap. This relationship alone does not establish modality.
    """
    if not pid or not title or not parent_title:
        return False
    try:
        with _open_display() as d:
            owned = [win for win in _managed_windows(d) if _pid_of(win, d) == pid]
            children = [win for win in owned if _win_title(win, d) == title]
            parents = [win for win in owned if _win_title(win, d) == parent_title]
            if len(children) != 1 or len(parents) != 1:
                return False
            child, parent = children[0], parents[0]
            if child.id == parent.id or _active_window_id(d) != child.id:
                return False
            if child.get_attributes().map_state != 2 or parent.get_attributes().map_state != 2:
                return False
            actions = _prop(parent, d, "_NET_WM_ALLOWED_ACTIONS")
            if actions is None or not any(
                    _atom_is(a, _atom(d, "_NET_WM_ACTION_RESIZE")) for a in actions):
                return False
            types = _prop(child, d, "_NET_WM_WINDOW_TYPE")
            transient = _prop(child, d, "WM_TRANSIENT_FOR")
            return (types is not None and transient is not None
                    and any(_atom_is(t, _atom(d, "_NET_WM_WINDOW_TYPE_DIALOG")) for t in types)
                    and int(transient[0]) == int(parent.id))
    except Exception:
        return False


def transient_modal_windows(*, pid: int, parent_title: str) -> list[dict]:
    """Mapped same-process windows that declare themselves modal for one parent.

    Electron's ``dialog.showMessageBox(win)`` is a GTK dialog created outside
    Chromium's accessibility tree (#1146). X11 still reports its ownership:
    ``WM_TRANSIENT_FOR`` names the exact parent window and ``_NET_WM_STATE``
    carries ``_NET_WM_STATE_MODAL``. A transient relationship alone (a find
    bar or tool window) is ownership, not modality, so MODAL is required.
    The parent title must name exactly one managed window of ``pid``; an
    ambiguous or unreadable relationship returns nothing. One X connection,
    no geometry inference.
    """
    if not pid or not parent_title:
        return []
    try:
        with _open_display() as d:
            owned = [win for win in _managed_windows(d) if _pid_of(win, d) == pid]
            if len(owned) < 2:
                return []
            parents = [win for win in owned if _win_title(win, d) == parent_title]
            if len(parents) != 1:
                return []
            parent = parents[0]
            modal = _atom(d, "_NET_WM_STATE_MODAL")
            found = []
            for win in owned:
                if win.id == parent.id:
                    continue
                transient = _prop(win, d, "WM_TRANSIENT_FOR")
                if not transient or len(transient) != 1 or int(transient[0]) != int(parent.id):
                    continue
                states = _prop(win, d, "_NET_WM_STATE")
                if not states or not any(_atom_is(state, modal) for state in states):
                    continue
                if win.get_attributes().map_state != 2 or _is_hidden(win, d):
                    continue
                geom = _geometry_on_root(win, d)
                bounds = None
                if geom is not None:
                    gx, gy, gw, gh = geom
                    bounds = {"display_id": 0, "x": gx, "y": gy, "width": gw, "height": gh}
                found.append({"window_id": int(win.id), "title": _win_title(win, d),
                              "bounds": bounds})
            return found
    except Exception:
        return []


def _window_overlapping(rows: list[dict], extents) -> int | None:
    """Window id whose client box overlaps ``extents`` the most, or None."""
    if not extents or len(extents) != 4:
        return None
    ax, ay, aw, ah = (float(part) for part in extents)
    if aw <= 0 or ah <= 0:
        return None
    best = None
    best_area = 0.0
    for row in rows:
        bounds = row.get("bounds") or {}
        try:
            bx = float(bounds["x"])
            by = float(bounds["y"])
            bw = float(bounds["width"])
            bh = float(bounds["height"])
        except (KeyError, TypeError, ValueError):
            continue
        overlap_w = min(ax + aw, bx + bw) - max(ax, bx)
        overlap_h = min(ay + ah, by + bh) - max(ay, by)
        area = max(overlap_w, 0.0) * max(overlap_h, 0.0)
        if area > best_area:
            best, best_area = int(row["window_id"]), area
    return best


def _window_rows(d) -> list[dict]:
    rows: list[dict] = []
    for win in _managed_windows(d):
        pid = _settled_pid(win, d)
        hidden = _is_hidden(win, d)
        geom = None if hidden else _geometry_on_root(win, d)
        bounds = None
        if geom is not None:
            gx, gy, gw, gh = geom
            bounds = {"display_id": 0, "x": gx, "y": gy, "width": gw, "height": gh}
        instance, klass = _wm_class_strings(win, d)
        rows.append({
            "window_id": int(win.id),
            "app": _app_id(win, d, settle=True),
            "title": _win_title(win, d),
            "pid": pid,
            "bounds": bounds,
            "on_screen": not hidden,
            "wm_class": instance,
            "wm_class_class": klass,
            "dialog": _is_dialog_window(win, d),
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

    The process comm wins. A missing ``_NET_WM_PID`` is read again before
    the WM_CLASS fallback. "" when neither can be read. None when no managed
    window has that id (or X is unreachable).
    """
    try:
        with _open_display() as d:
            win = _window_by_id(d, window_id)
            if win is None:
                return None
            return _app_id(win, d, settle=True)
    except ComputerUseError:
        raise
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


# A second client's present() can land after one activation and leave the
# previous window in _NET_ACTIVE_WINDOW. Poll that property and send again
# until this window is what two reads in a row report. 5s is the same budget
# the live app= focus wait uses. A display with no such property does not wait.
_ACTIVATE_WAIT_S = 5.0
_ACTIVATE_POLL_S = 0.05
_ACTIVATE_SETTLE_HITS = 2
_ACTIVATE_MAX_POLLS = 120


def _active_window_property(d):
    """The root ``_NET_ACTIVE_WINDOW`` property, or None when it is absent."""
    try:
        return d.screen().root.get_full_property(_atom(d, "_NET_ACTIVE_WINDOW"), 0)
    except Exception:
        return None


def _active_window_id(d) -> int | None:
    """The window id in ``_NET_ACTIVE_WINDOW``, or None when nothing is active."""
    active = _prop(d.screen().root, d, "_NET_ACTIVE_WINDOW")
    if not active:
        return None
    try:
        value = int(active[0])
    except (TypeError, ValueError, IndexError):
        return None
    return value or None


def _activation_blocker(d, win) -> str | None:
    """Read client visibility, independently of the WM's active-window flag."""
    atoms = _prop(win, d, "_NET_WM_STATE")
    shaded = _atom(d, "_NET_WM_STATE_SHADED")
    if atoms is not None and any(_atom_is(item, shaded) for item in atoms):
        return "window_shaded"
    try:
        # X.IsViewable; keep the probe importable without python-xlib.
        if win.get_attributes().map_state != 2:
            return "window_unmapped"
    except Exception:
        return "window_visibility_unverified"
    return None


def _activation_error(win, reason: str):
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    return ComputerUseError(
        ErrorCode.UNSUPPORTED,
        f"window {win.id} is not ready for input: {reason}",
        detail={"reason": reason, "window_id": int(win.id),
                "hint": "Use window focus to restore the window, then retry."},
    )


def refuse_unviewable_input(window_id: int | None = None) -> None:
    """Refuse input to a known shaded/unmapped X client, including active ones.

    No X display leaves the existing AT-SPI/Wayland checks responsible for
    input. This probe never activates another window or sends input itself.
    """
    try:
        d = _display()
    except Exception:
        return
    try:
        target = window_id if window_id is not None else _active_window_id(d)
        if target is None:
            return
        win = d.create_resource_object("window", int(target))
        reason = _activation_blocker(d, win)
        if reason in {"window_shaded", "window_unmapped"}:
            raise _activation_error(win, reason)
    finally:
        _close_display(d)


def _request_activation(d, win, *, restore: bool) -> None:
    """Unshade, optionally uniconify, then request WM activation."""
    if restore:
        _restore_if_hidden(d, win)
    atoms = _prop(win, d, "_NET_WM_STATE")
    shaded = _atom(d, "_NET_WM_STATE_SHADED")
    if atoms is not None and any(_atom_is(item, shaded) for item in atoms):
        _client_message(d, win, "_NET_WM_STATE", [0, shaded, 0, 1, 0])
    _send_active_window(d, win)


def _activate_and_settle(d, win, *, restore: bool) -> None:
    """Activate ``win`` and wait until ``_NET_ACTIVE_WINDOW`` stays on it.

    Openbox applies ``_NET_ACTIVE_WINDOW`` in ``client_activate``. Focus
    stealing can drop that message when another client presents in the same
    moment, and the previous window stays active. A minimized window is
    uniconified first when ``restore`` is set, because that same path
    uniconifies only when the focus change is allowed. The uniconify
    messages do not depend on it.

    Two consecutive active, mapped and unshaded reads mean activation settled. A
    read of a different window sends the request again. A root that does
    not answer ``_NET_ACTIVE_WINDOW`` (no window manager, or a caller that
    only records the client message) gets the one request and does not wait.
    """
    if _active_window_property(d) is None:
        _request_activation(d, win, restore=restore)
        reason = _activation_blocker(d, win)
        if reason:
            raise _activation_error(win, reason)
        return
    target = int(win.id)
    hits = 0
    deadline = time.monotonic() + _ACTIVATE_WAIT_S
    for _ in range(_ACTIVATE_MAX_POLLS):
        if hits == 0:
            _request_activation(d, win, restore=restore)
        if _active_window_id(d) == target and _activation_blocker(d, win) is None:
            hits += 1
            if hits >= _ACTIVATE_SETTLE_HITS:
                return
        else:
            hits = 0
        if time.monotonic() >= deadline:
            break
        time.sleep(_ACTIVATE_POLL_S)
    raise _activation_error(win, _activation_blocker(d, win) or "window_activation_unverified")


# A move, resize, minimize, maximize, or close can be sent before Openbox
# has finished managing the window. The message is then ignored and the
# window list still shows the old state. Send again until the list matches,
# on the same budget as activation. A display that does not publish
# _NET_ACTIVE_WINDOW still gets one message and does not poll.
_GEOMETRY_SLOP = 8


def _effect_observable(d) -> bool:
    """True when a window manager publishes ``_NET_ACTIVE_WINDOW``."""
    return _active_window_property(d) is not None


def _retry_until(d, send, done) -> None:
    """Call ``send`` until ``done`` is true, when a window manager is listening."""
    if not _effect_observable(d):
        send()
        return
    deadline = time.monotonic() + _ACTIVATE_WAIT_S
    for _ in range(_ACTIVATE_MAX_POLLS):
        if done():
            return
        send()
        if done():
            return
        if time.monotonic() >= deadline:
            return
        time.sleep(_ACTIVATE_POLL_S)


def _geometry_matches(win, d, *, x=None, y=None, width=None, height=None) -> bool:
    """True when the client geometry is within ``_GEOMETRY_SLOP`` of the request."""
    geom = _geometry_on_root(win, d)
    if geom is None:
        return False
    gx, gy, gw, gh = geom
    if x is not None and abs(gx - int(x)) > _GEOMETRY_SLOP:
        return False
    if y is not None and abs(gy - int(y)) > _GEOMETRY_SLOP:
        return False
    if width is not None and abs(gw - int(width)) > _GEOMETRY_SLOP:
        return False
    if height is not None and abs(gh - int(height)) > _GEOMETRY_SLOP:
        return False
    return True


def _is_maximized(win, d) -> bool:
    """True when both EWMH maximized atoms are set."""
    atoms = _prop(win, d, "_NET_WM_STATE")
    if not atoms:
        return False
    try:
        vert = _atom(d, "_NET_WM_STATE_MAXIMIZED_VERT")
        horz = _atom(d, "_NET_WM_STATE_MAXIMIZED_HORZ")
    except Exception:
        return False
    try:
        has_vert = any(_atom_is(item, vert) for item in atoms)
        has_horz = any(_atom_is(item, horz) for item in atoms)
    except Exception:
        return False
    return has_vert and has_horz


@contextmanager
def _with_window(window_id: int):
    """Yield ``(display, window)``. The window is None when the id is not managed.

    The display is closed when the caller returns, including the missing-window
    path. The client message is flushed before that close, so the window
    manager already has the request and the selection of X clients stays bounded.
    """
    with _open_display() as d:
        yield d, _window_by_id(d, window_id)


def _restore_if_hidden(d, win) -> None:
    """Ask the window manager to uniconify ``win`` before a later verb.

    Openbox 3.6 handles ``_NET_ACTIVE_WINDOW`` in ``client_activate``. That
    path uniconifies only when focus-stealing prevention allows the focus
    change. When it refuses, the window stays iconic and a following move or
    resize is not visible: ``window list`` reports ``on_screen`` false and
    ``bounds`` null. ``MapRequest`` on an iconic window uses that same
    activate path. ``WM_CHANGE_STATE`` NormalState and ``_NET_WM_STATE``
    remove of ``_NET_WM_STATE_HIDDEN`` call ``client_iconify(FALSE)``
    directly, so the window is mapped even when activation is refused.
    A window that is already showing is left alone, and this sends nothing.
    """
    if not _is_hidden(win, d):
        return
    _client_message(d, win, "WM_CHANGE_STATE", [1, 0, 0, 0, 0])  # NormalState
    hidden = _atom(d, "_NET_WM_STATE_HIDDEN")
    _client_message(d, win, "_NET_WM_STATE", [0, hidden, 0, 1, 0])  # _NET_WM_STATE_REMOVE


def is_notification_window(window_id: int) -> bool:
    """Whether the managed client explicitly reports the EWMH notification type.

    Notification bubbles accept pointer input without becoming active. This
    probe is only for implicit ref-action preparation, not explicit focus.
    Missing or unreadable metadata keeps the ordinary activation path.
    """
    try:
        with _with_window(window_id) as (d, win):
            if win is None:
                return False
            types = _prop(win, d, "_NET_WM_WINDOW_TYPE")
            notification = _atom(d, "_NET_WM_WINDOW_TYPE_NOTIFICATION")
            return types is not None and any(
                _atom_is(item, notification) for item in types
            )
    except Exception:
        return False


def request_input_activation(window_id: int) -> bool:
    """Request focus once for a viewable client; the caller verifies focus.

    Unlike the window-management verb, input recovery must neither restore
    a hidden window nor start a second activation wait inside its deadline.
    """
    with _with_window(window_id) as (d, win):
        if win is None or _activation_blocker(d, win) is not None:
            return False
        _send_active_window(d, win)
        return True


def raise_window(window_id: int) -> bool:
    """Activate managed window ``window_id`` via ``_NET_ACTIVE_WINDOW``.

    A minimized window is uniconified and a shaded window is unshaded first.
    The client must stay mapped, unshaded and active, so a present() from another
    client in the same moment does not leave the previous window active.
    Returns False when no managed window has that id.
    """
    with _with_window(window_id) as (d, win):
        if win is None:
            return False
        _activate_and_settle(d, win, restore=True)
        return True


def focus_window(window_id: int) -> bool:
    """Ask the window manager to focus ``window_id`` (``_NET_ACTIVE_WINDOW``).

    Under a standard EWMH window manager activation raises and focuses.
    Openbox does not uniconify when focus-stealing prevention refuses that
    message, so a minimized window is restored before the activate message.
    The request is retried until ``_NET_ACTIVE_WINDOW`` stays on this window.
    The verb is still distinct so the caller can say which one it asked for.
    Returns False when no managed window has that id.
    """
    return raise_window(window_id)


def minimize_window(window_id: int) -> bool:
    """Iconify ``window_id``: ICCCM ``WM_CHANGE_STATE`` plus ``_NET_WM_STATE_HIDDEN``.

    The request is sent again until the window is hidden. A message sent
    before the window manager has finished managing the window is ignored,
    and the list would still show it on screen. Returns False when no
    managed window has that id.
    """
    with _with_window(window_id) as (d, win):
        if win is None:
            return False

        def send() -> None:
            _client_message(d, win, "WM_CHANGE_STATE", [3, 0, 0, 0, 0])  # IconicState
            hidden = _atom(d, "_NET_WM_STATE_HIDDEN")
            _client_message(d, win, "_NET_WM_STATE", [1, hidden, 0, 1, 0])  # _NET_WM_STATE_ADD

        _retry_until(d, send, lambda: _is_hidden(win, d))
        return True


def maximize_window(window_id: int) -> bool:
    """Maximize ``window_id`` vertically and horizontally in one ``_NET_WM_STATE``.

    The request is sent again until both maximized atoms are set.
    """
    with _with_window(window_id) as (d, win):
        if win is None:
            return False

        def send() -> None:
            vert = _atom(d, "_NET_WM_STATE_MAXIMIZED_VERT")
            horz = _atom(d, "_NET_WM_STATE_MAXIMIZED_HORZ")
            _client_message(d, win, "_NET_WM_STATE", [1, vert, horz, 1, 0])

        _retry_until(d, send, lambda: _is_maximized(win, d))
        return True


def _frame_extent_box(win, d) -> tuple[int, int, int, int]:
    """(left, right, top, bottom) from ``_NET_FRAME_EXTENTS``.

    Missing or unreadable extents are zeros: a client under Xvfb with no
    frame, and a window the manager has not decorated yet.
    """
    val = _prop(win, d, "_NET_FRAME_EXTENTS")
    if not val:
        return 0, 0, 0, 0
    try:
        items = [int(part) for part in list(val)[:4]]
    except (TypeError, ValueError):
        return 0, 0, 0, 0
    if len(items) < 4 or any(part < 0 for part in items):
        return 0, 0, 0, 0
    return items[0], items[1], items[2], items[3]


def _frame_insets(win, d) -> tuple[int, int]:
    """(left, top) of the window-manager frame around the client window."""
    left, _right, top, _bottom = _frame_extent_box(win, d)
    return left, top


def _client_rows_for_pid(d, pid: int) -> list[tuple]:
    """On-screen ``(window, x, y, width, height)`` rows owned by ``pid``."""
    rows = []
    for win in _managed_windows(d):
        if _is_hidden(win, d):
            continue
        owner = _pid_of(win, d)
        if not owner or int(owner) != int(pid):
            continue
        geom = _geometry_on_root(win, d)
        if geom is None:
            continue
        x, y, w, h = geom
        if w <= 0 or h <= 0:
            continue
        rows.append((win, int(x), int(y), int(w), int(h)))
    return rows


def _choose_client_row(rows: list[tuple], width: float | None, height: float | None):
    """The one row whose origin is known, or None when the windows disagree.

    One window, or several that share an origin, is that origin. A size
    match breaks a tie between different origins. Anything else is None.
    """
    if not rows:
        return None
    origins = {(row[1], row[2]) for row in rows}
    if len(origins) == 1:
        if width is None or height is None:
            return rows[0]
        try:
            want_w = float(width)
            want_h = float(height)
        except (TypeError, ValueError):
            return rows[0]
        sized = [
            row for row in rows
            if want_w > 0 and want_h > 0
            and abs(row[3] - want_w) <= 8 and abs(row[4] - want_h) <= 8
        ]
        return sized[0] if sized else rows[0]
    if width is None or height is None:
        return None
    try:
        want_w = float(width)
        want_h = float(height)
    except (TypeError, ValueError):
        return None
    if want_w <= 0 or want_h <= 0:
        return None
    sized = [
        row for row in rows
        if abs(row[3] - want_w) <= 8 and abs(row[4] - want_h) <= 8
    ]
    unique = {(row[1], row[2]) for row in sized}
    if len(unique) == 1:
        return sized[0]
    return None


def client_origin_for_pid(
    pid: int, width: float | None = None, height: float | None = None,
) -> tuple[int, int] | None:
    """X client origin of an on-screen window owned by ``pid``, or None.

    Hidden windows are skipped. One on-screen window is that window, and
    several windows that share an origin are that origin. Several origins
    resolve only when ``width`` and ``height`` match one window's client
    size within a few pixels. Disagreeing origins with no unique size match
    are None: picking one would place every element in the wrong window.
    """
    try:
        wanted = int(pid)
    except (TypeError, ValueError):
        return None
    if wanted <= 0:
        return None
    try:
        with _open_display() as d:
            chosen = _choose_client_row(_client_rows_for_pid(d, wanted), width, height)
    except ComputerUseError:
        raise
    except Exception:
        return None
    if chosen is None:
        return None
    return chosen[1], chosen[2]


def _input_shape_rect(d, win):
    """The window's one X input-shape rectangle, or None.

    A client-side-decorated GTK window is larger than the surface that
    receives clicks. The input shape is that surface, in window coordinates.
    A missing extension, or more than one rectangle, is None rather than a
    guessed margin.
    """
    try:
        if not d.has_extension("SHAPE"):
            return None
    except Exception:
        return None
    method = getattr(win, "shape_get_rectangles", None)
    if method is None:
        return None
    try:
        from Xlib.ext import shape

        kind = shape.SK.Input
    except Exception:
        kind = 2
    try:
        reply = method(kind)
    except Exception:
        return None
    rects = list(getattr(reply, "rectangles", None) or [])
    if len(rects) != 1:
        return None
    rect = rects[0]
    try:
        return int(rect.x), int(rect.y), int(rect.width), int(rect.height)
    except (TypeError, ValueError):
        return None


def gtk4_content_origin(
    pid: int, width: float | None = None, height: float | None = None,
) -> tuple[int, int] | None:
    """Screen origin of a GTK 4 frame inside the X window that owns ``pid``.

    The client origin plus the input-shape offset. When that shape is a few
    pixels larger than the AT-SPI frame on every side, half the difference
    is the border between the shape and the frame (a shadow or resize
    margin). A shape that cannot be read leaves the client origin as it is.
    None when no on-screen window matches.
    """
    try:
        wanted = int(pid)
    except (TypeError, ValueError):
        return None
    if wanted <= 0:
        return None
    try:
        with _open_display() as d:
            chosen = _choose_client_row(_client_rows_for_pid(d, wanted), width, height)
            if chosen is None:
                return None
            win, x, y, _w, _h = chosen
            rect = _input_shape_rect(d, win)
    except ComputerUseError:
        raise
    except Exception:
        return None
    if rect is None:
        return x, y
    ix, iy, iw, ih = rect
    dx, dy = ix, iy
    try:
        frame_w = float(width) if width is not None else 0.0
        frame_h = float(height) if height is not None else 0.0
    except (TypeError, ValueError):
        frame_w = frame_h = 0.0
    extra_w = iw - frame_w
    extra_h = ih - frame_h
    if frame_w > 0 and frame_h > 0 and 0 <= extra_w <= 48 and 0 <= extra_h <= 48:
        dx += int(round(extra_w / 2))
        dy += int(round(extra_h / 2))
    return int(x + dx), int(y + dy)


def client_origin_for_outer_frame(
    x: int, y: int, width: int, height: int, title: str = "",
) -> tuple[int, int] | None:
    """Client origin whose outer frame matches an AT-SPI frame rectangle.

    LibreOffice's frame ``SCREEN`` rectangle is the outer window, including
    the title bar. ``WINDOW`` coordinates are relative to the X client
    window, which starts below that bar. The caller adds this origin to
    each element's window position. A title match wins when two frames
    share a rectangle. None when no managed window sits on that outer frame.
    """
    want = " ".join(str(title).casefold().split())
    try:
        with _open_display() as d:
            best: tuple[int, int] | None = None
            best_key: tuple[int, int] | None = None
            for win in _managed_windows(d):
                geom = _geometry_on_root(win, d)
                if geom is None:
                    continue
                cx, cy, cw, ch = geom
                left, right, top, bottom = _frame_extent_box(win, d)
                ox, oy = cx - left, cy - top
                ow, oh = cw + left + right, ch + top + bottom
                if abs(ox - int(x)) > 4 or abs(oy - int(y)) > 4:
                    continue
                # The AT-SPI frame height can omit the title bar the X outer
                # frame includes. Origin and width still identify the window.
                if abs(ow - int(width)) > 48 or abs(oh - int(height)) > 48:
                    continue
                label = " ".join(_win_title(win, d).casefold().split())
                title_rank = 0 if want and (want in label or label in want) else 1
                key = (title_rank, abs(ox - int(x)) + abs(oy - int(y)))
                if best_key is None or key < best_key:
                    best_key = key
                    best = (int(cx), int(cy))
            return best
    except Exception:
        return None


# Signed 32-bit range of an EWMH move coordinate. The client-message field
# is CARD32 on the wire; values outside this range are not a position.
_INT32_MIN = -1 << 31
_INT32_MAX = (1 << 31) - 1


def _screen_pixels(d) -> tuple[int, int] | None:
    """(width, height) of this X screen, or None when it publishes none.

    A synthetic display has no ``width_in_pixels``. That absence is not a
    1280x800 guess, and it is not a reason to widen the range.
    """
    try:
        screen = d.screen()
        width = screen.width_in_pixels
        height = screen.height_in_pixels
    except Exception:
        return None
    if isinstance(width, bool) or isinstance(height, bool):
        return None
    try:
        width = int(width)
        height = int(height)
    except (TypeError, ValueError):
        return None
    if width < 1 or height < 1:
        return None
    return width, height


def _move_integers(x: object, y: object) -> tuple[int, int]:
    """``(x, y)`` when both are integers. A bool is not an integer.

    A float is not truncated. Truncating would place the window somewhere
    other than the coordinate the caller passed.
    """
    if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, int) or not isinstance(y, int):
        raise ValueError(f"window move x and y must be integers, got ({x!r}, {y!r})")
    return x, y


def _reject_move_point(x: object, y: object, size: tuple[int, int] | None) -> tuple[int, int]:
    """The client origin, or ValueError when it is not a pixel we can place.

    With a screen size, valid pixels are 0..width-1 and 0..height-1, the
    same range as a click. Without one, the point has to be a non-negative
    signed 32-bit coordinate. Nothing is clamped into range.
    """
    px, py = _move_integers(x, y)
    if size is None:
        if px < 0 or py < 0 or px > _INT32_MAX or py > _INT32_MAX:
            raise ValueError(
                f"window move ({px}, {py}) is outside the signed screen range "
                f"(valid x and y are 0..{_INT32_MAX})"
            )
        return px, py
    width, height = size
    if 0 <= px <= width - 1 and 0 <= py <= height - 1:
        return px, py
    raise ValueError(
        f"window move ({px}, {py}) is outside the screen "
        f"({width}x{height} physical px; "
        f"valid x is 0..{width - 1}, y is 0..{height - 1})"
    )


def _card32(value: int) -> int:
    """CARD32 wire form of a signed frame coordinate.

    python-xlib packs client-message data with an unsigned array, which
    raises ``OverflowError: can't convert negative value to unsigned int``
    for a negative. EWMH reads the field as signed. The two's-complement
    bits are that same coordinate, not a clamp to zero. A value outside
    signed 32-bit is rejected and no message is sent.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"window move coordinate must be an integer, got {value!r}")
    if value < _INT32_MIN or value > _INT32_MAX:
        raise ValueError(
            f"window move coordinate {value} is outside the signed 32-bit range"
        )
    if value < 0:
        return value + (1 << 32)
    return value


def move_window(window_id: int, x: int, y: int) -> bool:
    """Move the client window of ``window_id`` so its top-left is (x, y).

    ``window list`` reports that client origin. ``_NET_MOVERESIZE_WINDOW``
    with NorthWest gravity places the outer frame, so the request is shifted
    back by ``_NET_FRAME_EXTENTS`` (left, top). A move to (100, 80) with a
    5px border and a 29px title bar sends the frame to (95, 51), and the
    client then lists at (100, 80). A point inside the title bar makes the
    frame origin negative. That signed coordinate is sent as a CARD32; it
    is not clamped to zero, and the client origin stays the one asked for.
    Only the X and Y flags are set, so the window manager keeps the current
    size. The request is sent again, with fresh frame extents, until the
    client origin is that point.

    ``x`` and ``y`` have to be integers. When the display reports a size
    they also have to be on that screen (0..width-1 and 0..height-1). A
    float, a bool, a negative, or a point past that edge raises ValueError
    and sends nothing. Returns False when the id is not managed.
    """
    _reject_move_point(x, y, None)
    with _with_window(window_id) as (d, win):
        px, py = _reject_move_point(x, y, _screen_pixels(d))
        if win is None:
            return False

        def send() -> None:
            left, top = _frame_insets(win, d)
            # NorthWestGravity = 1. Flags X=1 and Y=2, shifted into the high byte.
            # Validate the frame origin before any client message, including
            # the uniconify a hidden window would otherwise receive.
            data = [
                1 | (3 << 8),
                _card32(px - left),
                _card32(py - top),
                0,
                0,
            ]
            _restore_if_hidden(d, win)
            _client_message(d, win, "_NET_MOVERESIZE_WINDOW", data)

        _retry_until(d, send, lambda: _geometry_matches(win, d, x=px, y=py))
        return True


def resize_window(window_id: int, width: int, height: int) -> bool:
    """Resize ``window_id`` via ``_NET_MOVERESIZE_WINDOW`` (width and height flags).

    A minimized window is uniconified first. Openbox applies the size, and
    ``window list`` only reports bounds for a window that is on screen.
    The request is sent again until the client size is the one asked for.
    """
    with _with_window(window_id) as (d, win):
        if win is None:
            return False

        def send() -> None:
            _restore_if_hidden(d, win)
            # Flags Width=4 and Height=8.
            _client_message(
                d, win, "_NET_MOVERESIZE_WINDOW",
                [1 | (12 << 8), 0, 0, int(width), int(height)],
            )

        _retry_until(
            d, send, lambda: _geometry_matches(win, d, width=width, height=height),
        )
        return True


def close_window(window_id: int) -> bool:
    """Ask the window manager to close ``window_id`` (``_NET_CLOSE_WINDOW``).

    The request is sent again until the window leaves the managed list.
    """
    with _with_window(window_id) as (d, win):
        if win is None:
            return False

        def send() -> None:
            current = _window_by_id(d, window_id)
            if current is None:
                return
            _client_message(d, current, "_NET_CLOSE_WINDOW", [0, 1, 0, 0, 0])

        _retry_until(d, send, lambda: _window_by_id(d, window_id) is None)
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
    return _desktop_entry_scan(identifier)


def _desktop_launch_id(root: str, path: str) -> str:
    """gtk-launch id for a desktop file under an applications directory.

    ``applications/foo/bar.desktop`` is ``foo-bar.desktop``.
    """
    rel = os.path.relpath(path, root)
    if rel.endswith(".desktop"):
        rel = rel[: -len(".desktop")]
    return rel.replace(os.sep, "-") + ".desktop"


def _desktop_keys(path: str) -> dict[str, str] | None:
    """Selected ``[Desktop Entry]`` keys, or None when the file cannot be read."""
    try:
        if os.path.getsize(path) > 262144:
            return None
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    fields: dict[str, str] = {}
    in_entry = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_entry = stripped == "[Desktop Entry]"
            continue
        if not in_entry or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key in {"Name", "GenericName", "Type", "Hidden", "NoDisplay"} and key not in fields:
            fields[key] = value.strip()
    return fields


def _desktop_visible(fields: dict[str, str]) -> bool:
    """False for a hidden, ``NoDisplay``, or non-application entry.

    An exact desktop id still launches those files. A display-name lookup
    does not.
    """
    if fields.get("Type") not in {None, "", "Application"}:
        return False
    if fields.get("Hidden", "").casefold() == "true":
        return False
    if fields.get("NoDisplay", "").casefold() == "true":
        return False
    return True


def _desktop_entry_scan(identifier: str) -> tuple[str, str] | None:
    """Desktop file for ``identifier`` by case-insensitive id, then display name.

    The id is compared case-insensitively after the exact path lookup misses.
    ``Name`` wins over ``GenericName``. The first installed file in XDG order
    wins within each. A hidden or ``NoDisplay`` file is skipped here. An exact
    id still launches that file.
    """
    from a11y_computer_use.app_identity import normalize

    want_id = identifier.casefold()
    if want_id.endswith(".desktop"):
        want_id = want_id[: -len(".desktop")]
    want_name = normalize(identifier)
    generic_hit: tuple[str, str] | None = None
    for root in _application_dirs():
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                if not filename.endswith(".desktop"):
                    continue
                path = os.path.join(dirpath, filename)
                launch_id = _desktop_launch_id(root, path)
                stem = launch_id[: -len(".desktop")]
                fields = _desktop_keys(path)
                if fields is None or not _desktop_visible(fields):
                    continue
                if want_id and stem.casefold() == want_id:
                    return launch_id, path
                if not want_name:
                    continue
                title = fields.get("Name") or ""
                if title and normalize(title) == want_name:
                    return launch_id, path
                generic = fields.get("GenericName") or ""
                if generic_hit is None and generic and normalize(generic) == want_name:
                    generic_hit = (launch_id, path)
    return generic_hit


def _which_program(name: str) -> str | None:
    """Absolute executable for ``name``.

    The lower-case spelling is tried when the caller's case is not on PATH,
    so ``Mousepad`` finds ``mousepad``.
    """
    if not name:
        return None
    found = shutil.which(name)
    if found:
        return found
    folded = name.casefold()
    if folded != name:
        found = shutil.which(folded)
        if found:
            return found
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
    program = _which_program(argv[0])
    if not program:
        return None
    return [program, *argv[1:]]


_LAUNCHER_BASENAMES = frozenset({"gtk-launch", "xdg-open", "gio"})


def _spawn(argv: list[str], identifier: str):
    """Start ``argv`` and reap it asynchronously while preserving its exit status.

    A recorder that returns an object with no ``pid`` still works: the handle
    simply has no pid. ``OSError`` is ``app_not_found`` and starts nothing.
    """
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    try:
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise ComputerUseError(
            ErrorCode.APP_NOT_FOUND,
            f"could not launch {identifier!r}: {exc}",
            detail={"app": identifier},
        ) from exc

    # Retain ownership even after the caller drops the launch handle. Popen's
    # waitpid lock lets startup probes poll the same process safely.
    wait = getattr(proc, "wait", None)
    if callable(wait):
        threading.Thread(target=wait, name="a11y-launch-reaper", daemon=True).start()
    return proc


def _launch_handle(proc, identifier: str, names: list[str], *, is_launcher: bool) -> dict:
    pid = getattr(proc, "pid", None)
    try:
        pid = int(pid) if pid else None
    except (TypeError, ValueError):
        pid = None
    cleaned: list[str] = []
    for name in names:
        if name and name not in cleaned:
            cleaned.append(str(name))
    return {
        "pid": pid,
        "proc": proc,
        "identifier": identifier,
        "names": cleaned,
        "is_launcher": bool(is_launcher),
    }


def _desktop_match_names(identifier: str, path: str) -> list[str]:
    """Names a window of this desktop file may be matched by.

    The desktop id, the file stem, ``StartupWMClass``, and the ``Exec``
    program's basename. ``gtk-launch`` itself is not one of them: it exits
    before the real window exists.
    """
    names = [identifier]
    stem = os.path.basename(path)
    if stem.endswith(".desktop"):
        stem = stem[: -len(".desktop")]
    names.append(stem)
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return names
    in_entry = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_entry = stripped == "[Desktop Entry]"
            continue
        if not in_entry:
            continue
        if stripped.startswith("StartupWMClass="):
            names.append(stripped.split("=", 1)[1].strip())
        elif stripped.startswith("Exec="):
            try:
                parts = shlex.split(stripped.split("=", 1)[1].strip(), posix=True)
            except ValueError:
                parts = []
            program = next((part for part in parts if part and not _DESKTOP_FIELD_CODE.match(part)), "")
            if program:
                names.append(os.path.basename(program))
    return names


_OFFICE_WINDOW_NAMES = ("soffice", "soffice.bin", "libreoffice")
_OFFICE_WRAPPERS = frozenset({"localc", "lowriter", "loimpress", "soffice", "libreoffice"})
_OFFICE_FLAGS = frozenset({"--calc", "--writer", "--impress"})


def launch_app(identifier: str, *, argv: tuple[str, ...] | None = None) -> dict:
    """Launch ``identifier`` or raise `ErrorCode.APP_NOT_FOUND` immediately.

    An executable on PATH is started directly. The lookup is case-insensitive,
    so ``Mousepad`` starts ``mousepad``. Otherwise a desktop file is started
    with ``gtk-launch``, or with its ``Exec`` line when ``gtk-launch`` is not
    installed: first by id, then by ``Name``, then by ``GenericName``
    (``Text Editor``). A name that is none of these is not handed to
    ``xdg-open`` and does not wait for a window.

    ``argv`` starts that program and its arguments instead of ``identifier``.
    A Calc, Writer, or Impress launch passes ``localc`` or ``soffice --calc``
    (and the writer and impress equivalents). The handle names include
    ``soffice.bin``, which is the process that owns the document window.

    The return value is a handle: pid, the process (so a caller can see it
    exit), the names a window of this launch may use, and whether the process
    is a launcher that exits before the real window. ``gtk-launch`` exiting 0
    is not the app exiting.
    """
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    if argv:
        program = argv[0]
        executable = shutil.which(program) if program else None
        if not executable:
            raise ComputerUseError(
                ErrorCode.APP_NOT_FOUND,
                f"could not launch {identifier!r}: not on PATH",
                detail={"app": identifier},
            )
        base = os.path.basename(executable)
        names = [identifier, base, executable]
        if base in _OFFICE_WRAPPERS or any(part in _OFFICE_FLAGS for part in argv[1:]):
            names.extend(_OFFICE_WINDOW_NAMES)
        proc = _spawn([executable, *argv[1:]], identifier)
        return _launch_handle(
            proc, identifier, names, is_launcher=base in _LAUNCHER_BASENAMES,
        )

    executable = _which_program(identifier) if identifier else None
    if executable:
        base = os.path.basename(executable)
        proc = _spawn([executable], identifier)
        return _launch_handle(
            proc, identifier, [identifier, base, executable],
            is_launcher=base in _LAUNCHER_BASENAMES,
        )
    found = _desktop_entry(identifier) if identifier else None
    if found is not None:
        desktop_id, path = found
        names = _desktop_match_names(identifier, path)
        opener = shutil.which("gtk-launch")
        if opener:
            proc = _spawn([opener, desktop_id], identifier)
            return _launch_handle(proc, identifier, names, is_launcher=True)
        argv = _desktop_exec(path)
        if argv:
            base = os.path.basename(argv[0])
            proc = _spawn(argv, identifier)
            return _launch_handle(
                proc, identifier, [*names, base, argv[0]],
                is_launcher=base in _LAUNCHER_BASENAMES,
            )
    raise ComputerUseError(
        ErrorCode.APP_NOT_FOUND,
        f"could not launch {identifier!r}: not on PATH",
        detail={"app": identifier},
    )


def _root_screen_size(d) -> tuple[int, int] | None:
    """Pixel size of the X screen, or None when the display does not say.

    Synthetic tests omit ``width_in_pixels``. A missing size is not treated
    as a zero screen.
    """
    try:
        screen = d.screen()
        width = int(getattr(screen, "width_in_pixels", 0) or 0)
        height = int(getattr(screen, "height_in_pixels", 0) or 0)
    except Exception:
        return None
    if width > 0 and height > 0:
        return width, height
    return None


def _geometry_on_screen(geom, screen: tuple[int, int] | None) -> bool:
    """Whether ``geom`` (x, y, w, h) meets the screen.

    A box that sits entirely left of or above the origin does not. When the
    screen size is known, a box that starts at or past the far edge does not
    either. When the size is missing, a positive box that reaches the origin
    side counts, so a test double without ``width_in_pixels`` still focuses
    a window at (10, 20).
    """
    if geom is None:
        return False
    try:
        x, y, width, height = (int(geom[0]), int(geom[1]), int(geom[2]), int(geom[3]))
    except (TypeError, ValueError, IndexError):
        return False
    if width <= 0 or height <= 0:
        return False
    if x + width <= 0 or y + height <= 0:
        return False
    if screen is None:
        return True
    return x < screen[0] and y < screen[1]


def activate_app(identifier: str) -> str:
    """Raise+focus the topmost on-screen window matching ``identifier``.

    Stacking is bottom to top, so the last on-screen match is the front
    window. A minimized window and a window whose box misses the screen are
    not that match. When every match is hidden or off the display, this
    raises ``window_off_screen`` and does not send ``_NET_ACTIVE_WINDOW``.
    No matching window is ``app_not_found``. The call does not report that
    it activated an app that was never launched.
    """
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    with _open_display() as d:
        screen = _root_screen_size(d)
        resolved = identifier
        matched = None
        saw = False
        for win in _managed_windows(d):
            comm = (_app_id(win, d) or "")
            instance, klass = _wm_class_strings(win, d)
            title = _win_title(win, d)
            named = _comm_matches_identifier(identifier, comm) or _class_matches_identifier(
                identifier, instance, klass
            )
            if not named and title:
                named = any(name and name in title.lower() for name in _identity_needles(identifier))
            if not named:
                continue
            saw = True
            if _is_hidden(win, d):
                continue
            if not _geometry_on_screen(_geometry_on_root(win, d), screen):
                continue
            resolved = comm or instance or identifier
            matched = win
        if matched is None:
            if saw:
                raise ComputerUseError(
                    ErrorCode.UNSUPPORTED,
                    f"{identifier!r} has no window on this screen",
                    detail={
                        "app": identifier,
                        "reason": "window_off_screen",
                        "hint": "every matching window is minimized or outside the display",
                    },
                )
            raise ComputerUseError(
                ErrorCode.APP_NOT_FOUND,
                f"no running application matches {identifier!r}",
                detail={"app": identifier},
            )
        _activate_and_settle(d, matched, restore=False)
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


def clipboard_copy_state() -> tuple | None:
    """X11 clipboard owner and advertised targets; no clipboard content read.

    Missing metadata stays unknown. A missing owner is a real baseline, so
    a subsequent owner can establish a copy into an initially empty clipboard.
    """
    if not os.environ.get("DISPLAY"):
        return None
    try:
        with _open_display() as display:
            atom = _atom(display, "CLIPBOARD")
            owner = display.get_selection_owner(atom)
            owner_id = getattr(owner, "id", owner)
            targets = None
            if owner_id and shutil.which("xclip"):
                try:
                    result = subprocess.run(
                        ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
                        capture_output=True, timeout=0.2, check=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass  # Owner evidence remains useful without target metadata.
                else:
                    if result.returncode == 0:
                        targets = tuple(sorted(set(_target_lines(result.stdout))))
            # A replacement during TARGETS conversion makes this read incoherent.
            current = display.get_selection_owner(atom)
            if getattr(current, "id", current) != owner_id:
                return None
            return (owner_id, targets)
    except Exception:
        return None
