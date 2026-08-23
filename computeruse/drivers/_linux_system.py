"""Linux system/windowing probes: foreground app, app-under-point, window and
app enumeration, activate/launch, and clipboard. Linux-only; used by server.py's
platform-dispatching helpers so the Runtime's gating works on Linux.

App identity on Linux is the process comm name (e.g. "gedit", "chrome") read
from ``/proc/<pid>/comm`` — the analog of a macOS bundle id / Windows exe for
permission-keying. Window/desktop facts come from EWMH properties over
python-xlib (pure Python, no build deps); clipboard shells out to
xclip/xsel/wl-clipboard.
"""

from __future__ import annotations

import os
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
    """(x, y, w, h) of ``win`` in root (screen) coordinates, or None."""
    try:
        g = win.get_geometry()
        coords = win.translate_coords(d.screen().root, 0, 0)
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


def resolve_app(identifier: str) -> str:
    """Resolve a window title / comm substring to the owning comm name (the
    permission-keying id), or the identifier itself if unmatched."""
    needle = (identifier or "").lower()
    if not needle:
        return identifier
    try:
        d = _display()
        for win in _managed_windows(d):
            comm = (_comm_for_pid(_pid_of(win, d)) or "").lower()
            title = _win_title(win, d).lower()
            if needle in comm or needle in title:
                return comm or identifier
    except Exception:
        pass
    return identifier


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


def launch_app(identifier: str) -> None:
    """Best-effort launch: run the command, else hand it to xdg-open."""
    try:
        subprocess.Popen([identifier], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    except Exception:
        pass
    opener = shutil.which("gtk-launch") or shutil.which("xdg-open")
    if opener:
        subprocess.Popen([opener, identifier], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def activate_app(identifier: str) -> str:
    """Raise+focus a window whose comm/title matches ``identifier`` (EWMH
    _NET_ACTIVE_WINDOW client message). Returns the resolved app id."""
    from Xlib import X, protocol

    needle = (identifier or "").lower()
    d = _display()
    root = d.screen().root
    resolved = identifier
    for win in _managed_windows(d):
        comm = (_comm_for_pid(_pid_of(win, d)) or "").lower()
        title = _win_title(win, d).lower()
        if needle and (needle in comm or needle in title):
            resolved = comm or identifier
            event = protocol.event.ClientMessage(
                window=win, client_type=_atom(d, "_NET_ACTIVE_WINDOW"),
                data=(32, [1, X.CurrentTime, 0, 0, 0]),
            )
            mask = X.SubstructureRedirectMask | X.SubstructureNotifyMask
            root.send_event(event, event_mask=mask)
            d.flush()
            break
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
