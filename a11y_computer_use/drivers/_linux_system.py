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
import time
from contextlib import contextmanager

from a11y_computer_use.schema import ComputerUseError


# ---------------------------------------------------------------------------
# X / EWMH plumbing (python-xlib, lazily imported)
# ---------------------------------------------------------------------------


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
        raise missing_xlib(exc) from exc
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


def _app_id(win, d, *, settle: bool = False) -> str:
    """Permission-keying app id: process comm, else the WM_CLASS instance."""
    pid = _settled_pid(win, d) if settle else _pid_of(win, d)
    return _comm_for_pid(pid) or _wm_class_instance(win, d)


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
    """comm name of the active window's owner (e.g. "gedit"); "" if undetectable.

    This is the process comm only. A window with no pid is not a frontmost
    app here, even when ``active_window`` can still name it by WM_CLASS.
    A missing python-xlib is an error, not an empty frontmost app.
    """
    from a11y_computer_use.schema import ComputerUseError

    try:
        with _open_display() as d:
            active = _prop(d.screen().root, d, "_NET_ACTIVE_WINDOW")
            if not active:
                return ""
            win = d.create_resource_object("window", int(active[0]))
            return _comm_for_pid(_pid_of(win, d)) or ""
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


def _top_window_pid(x: float, y: float) -> int | None:
    """Pid of the topmost client window containing a screen point, or None."""
    try:
        with _open_display() as d:
            for win in reversed(_managed_windows(d)):  # topmost first
                geom = _geometry_on_root(win, d)
                if geom is None:
                    continue
                gx, gy, gw, gh = geom
                if gx <= x < gx + gw and gy <= y < gy + gh:
                    pid = _pid_of(win, d)
                    return int(pid) if pid else None
    except ComputerUseError:
        raise
    except Exception:
        return None
    return None


def app_at_point_id(x: float, y: float) -> str | None:
    """comm name of the topmost window containing a screen point (act-time hit-test)."""
    pid = _top_window_pid(x, y)
    if not pid:
        return None
    return _comm_for_pid(pid)


def pid_at_point(x: float, y: float) -> int | None:
    """Pid of the topmost client window containing a screen point."""
    return _top_window_pid(x, y)


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


def _comm_matches_identifier(identifier: str, comm: str) -> bool:
    """True when ``comm`` is the process ``identifier`` names.

    A comm substring matches, and so does a launcher alias. A path matches
    by its basename as well as the full string.
    """
    folded = (comm or "").lower()
    if not folded:
        return False
    from a11y_computer_use.app_identity import shares_alias

    if shares_alias(identifier, folded):
        return True
    for needle in _identity_needles(identifier):
        if needle in folded or _launcher_comm(needle, folded):
            return True
    return False


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
    by_class: str | None = None
    by_title: str | None = None
    try:
        with _open_display() as d:
            for win in _managed_windows(d):
                comm = (_app_id(win, d) or "").lower()
                if comm and _comm_matches_identifier(identifier, comm):
                    return comm  # the app itself beats any window that merely names it
                instance, klass = _wm_class_strings(win, d)
                if by_class is None and _class_matches_identifier(identifier, instance, klass):
                    by_class = comm or instance
                # A title match is a fallback, never a winner over a comm match:
                # a Chromium tab "Donations | Krita" stacked above Krita's window
                # must not turn `krita` into `chrome`. A path matches its
                # basename in the title the same way the bare name does.
                if by_title is None and comm:
                    title = _win_title(win, d).lower()
                    if any(name and name in title for name in _identity_needles(identifier)):
                        by_title = comm
    except ComputerUseError:
        raise
    except Exception:
        pass
    return by_class or by_title or identifier


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
                if _comm_matches_identifier(identifier, comm):
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

    ``app`` is the process comm, or the WM_CLASS instance when the window has
    no pid. A missing ``_NET_WM_PID`` is read again before that fallback, so
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


def _request_activation(d, win, *, restore: bool) -> None:
    """Send ``_NET_ACTIVE_WINDOW``. ``restore`` uniconifies a minimized window first."""
    if restore:
        _restore_if_hidden(d, win)
    _send_active_window(d, win)


def _activate_and_settle(d, win, *, restore: bool) -> None:
    """Activate ``win`` and wait until ``_NET_ACTIVE_WINDOW`` stays on it.

    Openbox applies ``_NET_ACTIVE_WINDOW`` in ``client_activate``. Focus
    stealing can drop that message when another client presents in the same
    moment, and the previous window stays active. A minimized window is
    uniconified first when ``restore`` is set, because that same path
    uniconifies only when the focus change is allowed. The uniconify
    messages do not depend on it.

    Two consecutive reads of this window mean the activation settled. A
    read of a different window sends the request again. A root that does
    not answer ``_NET_ACTIVE_WINDOW`` (no window manager, or a caller that
    only records the client message) gets the one request and does not wait.
    """
    if _active_window_property(d) is None:
        _request_activation(d, win, restore=restore)
        return
    target = int(win.id)
    hits = 0
    deadline = time.monotonic() + _ACTIVATE_WAIT_S
    for _ in range(_ACTIVATE_MAX_POLLS):
        if hits == 0:
            _request_activation(d, win, restore=restore)
        if _active_window_id(d) == target:
            hits += 1
            if hits >= _ACTIVATE_SETTLE_HITS:
                return
        else:
            hits = 0
        if time.monotonic() >= deadline:
            return
        time.sleep(_ACTIVATE_POLL_S)


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


def raise_window(window_id: int) -> bool:
    """Activate managed window ``window_id`` via ``_NET_ACTIVE_WINDOW``.

    A minimized window is uniconified first. The request is sent again until
    ``_NET_ACTIVE_WINDOW`` stays on this window, so a present() from another
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


def _frame_insets(win, d) -> tuple[int, int]:
    """(left, top) of the window-manager frame around the client window.

    ``_NET_FRAME_EXTENTS`` is left, right, top, bottom. Missing or unreadable
    extents are (0, 0): every client under Xvfb with no frame, and a window
    the manager has not decorated yet.
    """
    val = _prop(win, d, "_NET_FRAME_EXTENTS")
    if not val:
        return 0, 0
    try:
        items = list(val)
    except TypeError:
        return 0, 0
    if len(items) < 4:
        return 0, 0
    try:
        left, top = int(items[0]), int(items[2])
    except (TypeError, ValueError):
        return 0, 0
    if left < 0 or top < 0:
        return 0, 0
    return left, top


def move_window(window_id: int, x: int, y: int) -> bool:
    """Move the client window of ``window_id`` so its top-left is (x, y).

    ``window list`` reports that client origin. ``_NET_MOVERESIZE_WINDOW``
    with NorthWest gravity places the outer frame, so the request is shifted
    back by ``_NET_FRAME_EXTENTS`` (left, top). A move to (100, 80) with a
    5px border and a 29px title bar sends the frame to (95, 51), and the
    client then lists at (100, 80). Only the X and Y flags are set, so the
    window manager keeps the current size. The request is sent again, with
    fresh frame extents, until the client origin is that point. Returns
    False when the id is not managed.
    """
    with _with_window(window_id) as (d, win):
        if win is None:
            return False

        def send() -> None:
            _restore_if_hidden(d, win)
            left, top = _frame_insets(win, d)
            # NorthWestGravity = 1. Flags X=1 and Y=2, shifted into the high byte.
            _client_message(
                d, win, "_NET_MOVERESIZE_WINDOW",
                [1 | (3 << 8), int(x) - left, int(y) - top, 0, 0],
            )

        _retry_until(d, send, lambda: _geometry_matches(win, d, x=x, y=y))
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


_LAUNCHER_BASENAMES = frozenset({"gtk-launch", "xdg-open", "gio"})


def _spawn(argv: list[str], identifier: str):
    """Start ``argv`` and return the process. The caller reaps it with ``poll``.

    A recorder that returns an object with no ``pid`` still works: the handle
    simply has no pid. ``OSError`` is ``app_not_found`` and starts nothing.
    """
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    try:
        return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise ComputerUseError(
            ErrorCode.APP_NOT_FOUND,
            f"could not launch {identifier!r}: {exc}",
            detail={"app": identifier},
        ) from exc


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


def launch_app(identifier: str) -> dict:
    """Launch ``identifier`` or raise `ErrorCode.APP_NOT_FOUND` immediately.

    An executable on PATH is started directly. Otherwise a matching desktop
    file is started with ``gtk-launch``, or with its ``Exec`` line when
    ``gtk-launch`` is not installed. A name that is neither is not handed to
    ``xdg-open`` and does not wait for a window.

    The return value is a handle: pid, the process (so a caller can see it
    exit), the names a window of this launch may use, and whether the process
    is a launcher that exits before the real window. ``gtk-launch`` exiting 0
    is not the app exiting.
    """
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    executable = shutil.which(identifier) if identifier else None
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


def activate_app(identifier: str) -> str:
    """Raise+focus a window whose comm/title matches ``identifier`` (EWMH
    _NET_ACTIVE_WINDOW client message). Returns the resolved app id.

    The request is retried until ``_NET_ACTIVE_WINDOW`` stays on that window.
    No matching window is ``app_not_found``. The call does not report that
    it activated an app that was never launched. That answer does not build
    an X client message, so a desktop with no matching window does not need
    the X connection beyond the window list.
    """
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    with _open_display() as d:
        resolved = identifier
        matched = None
        for win in _managed_windows(d):
            comm = (_app_id(win, d) or "")
            instance, klass = _wm_class_strings(win, d)
            title = _win_title(win, d)
            named = _comm_matches_identifier(identifier, comm) or _class_matches_identifier(
                identifier, instance, klass
            )
            if not named and title:
                named = any(name and name in title.lower() for name in _identity_needles(identifier))
            if named:
                resolved = comm or instance or identifier
                matched = win
                break
        if matched is None:
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
