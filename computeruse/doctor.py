"""Environment diagnostics: `computeruse doctor`.

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
    target = app or "the app you launched computeruse from (could not auto-detect it)"
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
            "Run computeruse from a regular host app (Terminal, iTerm2, your IDE) "
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
        "fix": None if ok else f"computeruse requires Python >= {wanted}; upgrade your interpreter.",
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


def run_doctor() -> list[CheckResult]:
    """Run all environment checks and return their results.

    Checks (Phase 0 set): responsible host app identification, Accessibility
    grant, Screen Recording grant, Python version, pyobjc install, mcp import.
    Never raises for a failed check — failures are data, not exceptions.
    """
    app = responsible_app(parent_chain(os.getpid()))
    return [
        _check_responsible_app(app),
        _check_accessibility(app),
        _check_screen_recording(app),
        _check_python(),
        _check_pyobjc(),
        _check_mcp(),
    ]


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
