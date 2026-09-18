"""Environment diagnostics: `a11y_computer_use doctor`.

Detects missing TCC grants (Accessibility, Screen Recording) and — because
grants attach to the *responsible process* — names the host app (Terminal,
Claude Desktop, an IDE) that actually needs the grant (PLAN.md §7). Also
sanity-checks the Python runtime, pyobjc, and the ``mcp`` package.

`run_doctor()` returns pure data; pretty console rendering lives in
``cli.py``, with `render_text()` here as the plain-text fallback.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import importlib
import importlib.metadata
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import TypedDict

#: Deep link to the Accessibility pane of System Settings.
ACCESSIBILITY_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)

#: Deep link to the Screen Recording pane of System Settings.
SCREEN_RECORDING_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
)

_MIN_PYTHON = (3, 11)


class CheckResult(TypedDict):
    """One diagnostic finding, wire-stable for MCP results and the CLI."""

    check: str  #: short check id, e.g. "accessibility_grant"
    ok: bool  #: True when the check passed
    detail: str  #: human-readable status, e.g. which host app was detected
    fix: str | None  #: actionable remediation when not ok, else None


# ---------------------------------------------------------------------------
# TCC grant probes (ctypes, so doctor works even when pyobjc is broken)
# ---------------------------------------------------------------------------


def _call_bool(library: str, symbol: str) -> bool:
    """Call a zero-argument Bool function from a system library.

    Returns False off macOS, when the library/symbol is unavailable, or when
    the call itself reports False — doctor treats all three as "not granted".
    """
    if sys.platform != "darwin":
        return False
    try:
        path = ctypes.util.find_library(library)
        if not path:
            return False
        lib = ctypes.cdll.LoadLibrary(path)
        fn = getattr(lib, symbol)
        fn.restype = ctypes.c_bool
        return bool(fn())
    except (OSError, AttributeError):
        return False


def _ax_trusted() -> bool:
    """Whether this process holds the Accessibility (AX) TCC grant."""
    return _call_bool("ApplicationServices", "AXIsProcessTrusted")


def _screen_capture_preflight() -> bool:
    """Whether this process holds the Screen Recording TCC grant."""
    return _call_bool("CoreGraphics", "CGPreflightScreenCaptureAccess")


# ---------------------------------------------------------------------------
# Responsible-process detection (PLAN.md §7: TCC attributes grants to the
# app bundle owning the process tree, not to this Python interpreter)
# ---------------------------------------------------------------------------


def _run_ps(pid: int) -> str:
    """Raw ``ps -o ppid=,comm= -p pid`` output; empty string when ps fails."""
    try:
        return subprocess.run(
            ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _parse_ps_line(raw: str) -> tuple[int, str] | None:
    """Parse one ``ps -o ppid=,comm=`` line into (ppid, command path).

    The command path keeps embedded spaces (``.../Google Chrome``); returns
    None for empty or unparseable output.
    """
    line = raw.strip()
    if not line:
        return None
    parts = line.splitlines()[0].split(None, 1)
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    return int(parts[0]), parts[1]


def parent_chain(
    pid: int,
    *,
    run_ps: Callable[[int], str] | None = None,
    max_depth: int = 32,
) -> list[str]:
    """Command paths from ``pid`` upward (self first), ending at launchd.

    Walks parent pids by parsing ``ps -o ppid=,comm=`` one hop at a time
    (psutil-free). Stops at pid <= 1, on unparseable ps output, at
    ``max_depth``, or on a ppid cycle.
    """
    lookup = run_ps if run_ps is not None else _run_ps
    chain: list[str] = []
    seen: set[int] = set()
    while pid > 1 and pid not in seen and len(chain) < max_depth:
        seen.add(pid)
        parsed = _parse_ps_line(lookup(pid))
        if parsed is None:
            break
        pid, comm = parsed
        chain.append(comm)
    return chain


def responsible_app(chain: list[str]) -> str | None:
    """Name of the app bundle that owns this process tree, or None.

    TCC attributes grants to the *responsible process* — in practice the
    nearest ancestor living inside an ``.app`` bundle (Terminal, iTerm2,
    Claude, an IDE). Walks ``chain`` self-first and returns the bundle name
    (``".../Terminal.app/Contents/MacOS/Terminal"`` → ``"Terminal"``);
    None for orphan chains (launchd, cron) with no app-bundle ancestor.
    """
    for comm in chain:
        idx = comm.find(".app/")
        if idx != -1:
            return comm[:idx].rsplit("/", 1)[-1]
    return None


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _grant_fix(permission: str, pane_url: str, app: str | None) -> str:
    """Remediation naming the host app that must receive the TCC grant."""
    target = app or "the app you launched a11y_computer_use from (could not auto-detect it)"
    return (
        f"Grant {permission} to {target} in System Settings → Privacy & Security → "
        f"{permission}, then relaunch it. Deep link: {pane_url}"
    )


def _check_responsible_app(app: str | None) -> CheckResult:
    if app is not None:
        return {
            "check": "responsible_app",
            "ok": True,
            "detail": f"TCC grants attach to {app} (the app bundle owning this process tree)",
            "fix": None,
        }
    return {
        "check": "responsible_app",
        "ok": False,
        "detail": "no app bundle found in the parent process chain",
        "fix": (
            "Run a11y_computer_use from a regular host app (Terminal, iTerm2, your IDE) "
            "so macOS can attribute TCC grants to it."
        ),
    }


def _check_accessibility(app: str | None) -> CheckResult:
    ok = _ax_trusted()
    return {
        "check": "accessibility_grant",
        "ok": ok,
        "detail": f"AXIsProcessTrusted() = {ok}",
        "fix": None if ok else _grant_fix("Accessibility", ACCESSIBILITY_SETTINGS_URL, app),
    }


def _check_screen_recording(app: str | None) -> CheckResult:
    ok = _screen_capture_preflight()
    return {
        "check": "screen_recording_grant",
        "ok": ok,
        "detail": f"CGPreflightScreenCaptureAccess() = {ok}",
        "fix": None if ok else _grant_fix("Screen Recording", SCREEN_RECORDING_SETTINGS_URL, app),
    }


def _check_python() -> CheckResult:
    ok = sys.version_info >= _MIN_PYTHON
    wanted = ".".join(str(part) for part in _MIN_PYTHON)
    return {
        "check": "python_version",
        "ok": ok,
        "detail": f"Python {platform.python_version()}",
        "fix": None if ok else f"a11y_computer_use requires Python >= {wanted}; upgrade your interpreter.",
    }


def _check_pyobjc() -> CheckResult:
    try:
        version = importlib.metadata.version("pyobjc-core")
    except importlib.metadata.PackageNotFoundError:
        return {
            "check": "pyobjc_version",
            "ok": False,
            "detail": "pyobjc-core is not installed",
            "fix": (
                "Install the pyobjc framework wheels: pip install pyobjc-framework-Cocoa "
                "pyobjc-framework-Quartz pyobjc-framework-ApplicationServices"
            ),
        }
    return {"check": "pyobjc_version", "ok": True, "detail": f"pyobjc-core {version}", "fix": None}


def _check_mcp() -> CheckResult:
    try:
        importlib.import_module("mcp")
    except Exception as exc:  # noqa: BLE001 — a broken install can raise anything; failures are data
        return {
            "check": "mcp_import",
            "ok": False,
            "detail": f"import mcp failed: {exc}",
            "fix": "Install the MCP SDK: pip install mcp",
        }
    try:
        version = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown version"
    return {"check": "mcp_import", "ok": True, "detail": f"mcp {version} imports cleanly", "fix": None}


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Linux (AT-SPI2 / X11) and Windows (UIA) checks — the same shape as the macOS
# TCC probes, so `doctor` tells the truth on every platform the driver seam
# supports instead of reporting macOS grants that do not exist there.
# ---------------------------------------------------------------------------


def _check_display_session() -> CheckResult:
    display = os.environ.get("DISPLAY")
    wayland = os.environ.get("WAYLAND_DISPLAY")
    kind = os.environ.get("XDG_SESSION_TYPE") or "unset"
    ok = bool(display or wayland)
    return {
        "check": "display_session",
        "ok": ok,
        "detail": f"XDG_SESSION_TYPE={kind} DISPLAY={display or 'unset'} WAYLAND_DISPLAY={wayland or 'unset'}",
        "fix": None if ok else (
            "Run inside the graphical session, or export DISPLAY and XAUTHORITY (X11) or "
            "WAYLAND_DISPLAY and XDG_RUNTIME_DIR from a session process; scripts/box/run-live.sh shows how."
        ),
    }


def _check_window_manager() -> CheckResult:
    if (os.environ.get("XDG_SESSION_TYPE") or "").lower() == "wayland":
        return {
            "check": "window_manager",
            "ok": True,
            "detail": "Wayland compositor session",
            "fix": None,
        }
    try:
        from Xlib import display as xdisplay

        d = xdisplay.Display()
        root = d.screen().root
        prop = root.get_full_property(d.intern_atom("_NET_SUPPORTING_WM_CHECK"), 0)
        if prop is None or not prop.value:
            raise LookupError("no _NET_SUPPORTING_WM_CHECK on the root window")
        win = d.create_resource_object("window", int(prop.value[0]))
        name = win.get_full_property(d.intern_atom("_NET_WM_NAME"), 0)
        wm = name.value.decode("utf-8", "replace") if name is not None and name.value else "unknown"
        return {"check": "window_manager", "ok": True, "detail": f"EWMH window manager: {wm}", "fix": None}
    except Exception as exc:  # noqa: BLE001 — failures are data
        return {
            "check": "window_manager",
            "ok": False,
            "detail": f"no EWMH window manager on this display ({exc})",
            "fix": (
                "Focus-dependent input (coordinate clicks, XTEST typing) misbehaves without a window "
                "manager; plain Xvfb has none. Start one (openbox, xfwm4) or use a real desktop session."
            ),
        }


def _check_atspi_bindings() -> CheckResult:
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return {
            "check": "atspi_bindings",
            "ok": False,
            "detail": f"AT-SPI2 GI bindings unavailable: {exc}",
            "fix": (
                "apt install at-spi2-core gir1.2-atspi-2.0 gir1.2-gtk-3.0 python3-gi, then create the venv "
                "with --system-site-packages (or pip install PyGObject)."
            ),
        }
    return {"check": "atspi_bindings", "ok": True, "detail": "gi + Atspi 2.0 typelib import cleanly", "fix": None}


def _check_a11y_bus() -> CheckResult:
    try:
        from a11y_computer_use.drivers.linux import LinuxDriver

        LinuxDriver().ensure_trusted()
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        count = Atspi.get_desktop(0).get_child_count()
    except Exception as exc:  # noqa: BLE001
        return {
            "check": "a11y_bus",
            "ok": False,
            "detail": f"org.a11y.Bus not reachable: {str(exc).splitlines()[0][:200]}",
            "fix": (
                "Turn accessibility on for the session (GNOME/Budgie: gsettings set "
                "org.gnome.desktop.interface toolkit-accessibility true) or start "
                "/usr/libexec/at-spi-bus-launcher --launch-immediately; GTK apps need "
                "GTK_MODULES=gail:atk-bridge NO_AT_BRIDGE=0 to publish their trees."
            ),
        }
    return {
        "check": "a11y_bus",
        "ok": True,
        "detail": f"org.a11y.Bus reachable; {count} application(s) on the desktop",
        "fix": None,
    }


def _check_coordinate_input() -> CheckResult:
    if (os.environ.get("XDG_SESSION_TYPE") or "").lower() == "wayland":
        return {
            "check": "coordinate_input",
            "ok": False,
            "detail": "Wayland: raw coordinate clicks and key chords are UNSUPPORTED (a11y actions work)",
            "fix": "Use ref-based actions (click ref, set_value, type via EditableText); libei/RemoteDesktop portal input is pending.",
        }
    try:
        from Xlib import display as xdisplay

        d = xdisplay.Display()
        if not d.query_extension("XTEST"):
            raise LookupError("XTEST extension missing on this X server")
    except Exception as exc:  # noqa: BLE001
        return {
            "check": "coordinate_input",
            "ok": False,
            "detail": f"XTEST unavailable: {exc}",
            "fix": "pip install python-xlib and run against an X server with the XTEST extension.",
        }
    return {"check": "coordinate_input", "ok": True, "detail": "XTEST available (python-xlib)", "fix": None}


def _check_clipboard_tool() -> CheckResult:
    wayland = (os.environ.get("XDG_SESSION_TYPE") or "").lower() == "wayland"
    candidates = ("wl-copy", "xclip", "xsel") if wayland else ("xclip", "xsel", "wl-copy")
    found = next((c for c in candidates if shutil.which(c)), None)
    return {
        "check": "clipboard_tool",
        "ok": found is not None,
        "detail": f"{found} on PATH" if found else "no xclip / xsel / wl-copy on PATH",
        "fix": None if found else ("apt install wl-clipboard" if wayland else "apt install xclip"),
    }


def _check_uiautomation() -> CheckResult:
    try:
        importlib.import_module("uiautomation")
    except Exception as exc:  # noqa: BLE001
        return {
            "check": "uiautomation_import",
            "ok": False,
            "detail": f"import uiautomation failed: {exc}",
            "fix": 'pip install "a11y_computer_use[windows]" (uiautomation + comtypes)',
        }
    return {"check": "uiautomation_import", "ok": True, "detail": "uiautomation imports cleanly", "fix": None}


def run_doctor() -> list[CheckResult]:
    """Run the environment checks for THIS platform and return their results.

    macOS: responsible host app, Accessibility grant, Screen Recording grant,
    Python, pyobjc, mcp. Linux: graphical session, window manager, AT-SPI2
    bindings, a11y bus, XTEST coordinate input, clipboard tool, Python, mcp.
    Windows: uiautomation, Python, mcp. Never raises for a failed check —
    failures are data, not exceptions.
    """
    if sys.platform == "darwin":
        app = responsible_app(parent_chain(os.getpid()))
        return [
            _check_responsible_app(app),
            _check_accessibility(app),
            _check_screen_recording(app),
            _check_python(),
            _check_pyobjc(),
            _check_mcp(),
        ]
    if sys.platform.startswith("linux"):
        return [
            _check_display_session(),
            _check_window_manager(),
            _check_atspi_bindings(),
            _check_a11y_bus(),
            _check_coordinate_input(),
            _check_clipboard_tool(),
            _check_python(),
            _check_mcp(),
        ]
    return [_check_uiautomation(), _check_python(), _check_mcp()]


def render_text(report: list[CheckResult]) -> str:
    """Plain-text rendering of a doctor report (one line per check + fixes)."""
    lines = []
    for result in report:
        mark = " OK " if result["ok"] else "FAIL"
        lines.append(f"[{mark}] {result['check']}: {result['detail']}")
        if not result["ok"] and result["fix"]:
            lines.append(f"       fix: {result['fix']}")
    passed = sum(result["ok"] for result in report)
    lines.append(f"{passed}/{len(report)} checks passed")
    return "\n".join(lines)
