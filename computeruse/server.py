"""MCP server: the v1 tool surface (PLAN.md §8).

The full MCP tool surface, one canonical schema shared with the CLI: observe
(desktop_snapshot, find, screenshot, zoom), act (click, type, key, scroll, drag,
wait_for, act, set_value, scroll_to_find), manage (app, window, clipboard), plus
console and network when the browser backend provides those feeds. EVERY tool —
observation included —
routes through `safety.check_action` and is recorded in the audit log;
structured errors (`schema.ComputerUseError`) are rendered as clear
tool-error strings that carry the doctor hint, never raised across the wire
as stack traces.

Gating policy: ref-based actions are gated against the app of the snapshot
that issued the ref; target-less actions (typing, keys, clipboard, raw
coordinates, list verbs) and screen captures are gated against the frontmost
application; snapshots against the scoped app. Grants are keyed by bundle id
(`safety.PermissionStore`). Between the permission decision and injection a
same-window recheck runs (PLAN.md §6): pointer actions hit-test the target
point, typing/keys re-read the frontmost app, and a mismatch aborts with
`ErrorCode.FOCUS_CHANGED` — toasts and focus steals can race the event post.

Ref contract: refs (``e14``) come from the LATEST ``desktop_snapshot`` and
are re-resolved against the live tree at act time (`observe.resolve_ref`);
a ``stale_ref`` error means the tree changed — re-observe.

`Runtime` is the transport-free execution core: `build_server` wraps it in
async FastMCP tools that delegate to a worker thread (a slow AX walk or a
long ``wait_for`` must not freeze the MCP event loop), and the CLI's
``run-once`` drives it directly. The ``mcp`` package is imported only inside
`build_server`, so diagnosing a broken mcp install via ``computeruse
doctor`` still works.
"""

from __future__ import annotations

import json
import math
import time
import os
import subprocess
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from functools import partial, wraps
from threading import RLock
from typing import TYPE_CHECKING, Concatenate, ParamSpec, TypeVar

if sys.platform == "darwin":
    # macOS platform helpers. The Driver seam isolates everything OS-specific,
    # so server.py (the Runtime + MCP surface) imports on Windows/Linux too;
    # get_driver() picks the backend and these names are only used on macOS.
    import Quartz
    from AppKit import (
        NSApplicationActivateIgnoringOtherApps,
        NSPasteboard,
        NSPasteboardTypeString,
        NSRunningApplication,
        NSWorkspace,
    )

from computeruse import drivers, observe, safety

#: Copied from capture.DEFAULT_MAX_LONG_EDGE so the tool defaults don't import
#: the (pyobjc-backed) capture module at build time on non-macOS.
_DEFAULT_MAX_LONG_EDGE = 1280
from computeruse.schema import (
    MODIFIER_KEYS,
    AppOp,
    AppVerb,
    Bounds,
    Click,
    ClipboardOp,
    ClipboardVerb,
    ComputerUseError,
    Drag,
    Element,
    ErrorCode,
    KeyChord,
    MouseButton,
    ObserveOp,
    ObserveVerb,
    Point,
    Scope,
    Scroll,
    ScrollUnit,
    Snapshot,
    Target,
    TypeText,
    WaitCondition,
    WaitFor,
    WindowOp,
    WindowVerb,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

_DOCTOR_HINT = "run `computeruse doctor` to see which host app needs the grant"

#: Ceiling for the ``wait_for`` timeout parameter (seconds).
MAX_WAIT_TIMEOUT_S = 60.0
MAX_BATCH_STEPS = 100
MAX_BATCH_DURATION_S = 60.0
MAX_SCROLLS = 100

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _serialized(method: Callable[Concatenate["Runtime", _P], _R]) -> Callable[Concatenate["Runtime", _P], _R]:
    """Keep an entire tool's resolve/gate/act/audit sequence on one ref epoch.

    Nested calls (including every step of a batch) are reentrant. Competing
    synchronous callers fail immediately; the MCP layer queues asynchronously
    before allocating a worker, so waiting calls never consume worker threads.
    """
    @wraps(method)
    def execute(self: "Runtime", *args: _P.args, **kwargs: _P.kwargs) -> _R:
        if not self._operation_lock.acquire(blocking=False):
            raise ComputerUseError(
                ErrorCode.BUSY,
                "this Runtime is executing another operation; retry after it completes",
                detail={"retryable": True},
            )
        try:
            if self._closed:
                raise ComputerUseError(ErrorCode.CLOSED, "this Runtime is closed")
            return method(self, *args, **kwargs)
        finally:
            self._operation_lock.release()

    return execute

#: Prefer AX activation (``AXPress``/focus — no cursor movement) over a
#: synthetic mouse click for simple left single-clicks on a ref. This is what
#: lets the agent work without hijacking the user's pointer. Set
#: ``COMPUTERUSE_AX_CLICKS=0`` to force synthetic-mouse clicks everywhere
#: (e.g. for an app whose AX press handlers misbehave).
PREFER_AX_ACTIONS = os.environ.get("COMPUTERUSE_AX_CLICKS", "1") != "0"

#: Require explicit human confirmation before a plausibly irreversible action
#: (see `safety.confirmation_prompt`). When on and the host offers no
#: confirmation channel, such actions fail-safe (blocked) rather than firing
#: unconfirmed. Set ``COMPUTERUSE_CONFIRM=0`` to disable the gate entirely.
CONFIRMATION_GATE = os.environ.get("COMPUTERUSE_CONFIRM", "1") != "0"

#: A confirmer maps a prompt to the human's yes/no. Injected per call so the
#: transport (MCP elicitation, a CLI prompt, a test double) stays out of the
#: safety core.
Confirmer = Callable[[str], bool]

#: Appended to a snapshot that exposes no actionable refs — the a11y→vision
#: handoff signal (PLAN §6 / COM-12). Custom-drawn apps (Telegram, some games,
#: Electron before AXManualAccessibility) yield a shell with nothing to click,
#: so the agent should switch to the pixel path.
_VISION_HANDOFF_HINT = (
    "note: no interactive elements were found in this app's accessibility tree — "
    "it is likely custom-drawn (e.g. Telegram, some games/Electron apps), so the "
    "a11y ref path cannot target it. Fall back to the `screenshot` tool and act "
    "by x/y coordinates; if it is Electron, the tree may populate after the app "
    "gets focus. (Try scope='app' if you used 'window'.)"
)

_PERMISSION_CODES = frozenset(
    {ErrorCode.PERMISSION_DENIED_ACCESSIBILITY, ErrorCode.PERMISSION_DENIED_SCREEN}
)


class ActionRefused(Exception):
    """A safety `Decision` blocked the action (DENY or NEEDS_PERMISSION)."""

    def __init__(self, decision: safety.Decision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


def error_text(exc: ComputerUseError) -> str:
    """Render a structured error as one clear tool-error line.

    Format: ``<code>: <message> | detail: {...} | hint: ...``. Permission
    errors always carry a doctor hint, even when the driver supplied none.
    """
    hint = exc.detail.get("hint") or exc.detail.get("doctor_hint")
    if hint is None and exc.code in _PERMISSION_CODES:
        hint = _DOCTOR_HINT
    parts = [f"{exc.code.value}: {exc.message}"]
    extra = {k: v for k, v in exc.detail.items() if k not in ("hint", "doctor_hint")}
    if extra:
        parts.append(f"detail: {json.dumps(extra, default=str)}")
    if hint:
        parts.append(f"hint: {hint}")
    return " | ".join(parts)


def refusal_text(decision: safety.Decision) -> str:
    """Render a non-ALLOW safety decision as one tool-error line."""
    return f"{decision.verdict.value}: {decision.reason}"


#: Roles that commonly own their own scroll position, most specific first. The
#: browser backend exposes an overflow ``<ul>`` as AXList and a scrolling
#: ``<div>`` as AXGroup (CDP has no scroll-area role), so scroll_to_find must
#: look past AXScrollArea or it wheels over the page and nothing moves.
_SCROLL_CONTAINER_TIERS = (
    ("AXScrollArea",),
    ("AXList", "AXTable", "AXOutline", "AXGrid", "AXMenu", "AXTabGroup"),
    ("AXGroup",),
)


def _scroll_anchor(snap):
    """The element to scroll over while searching: the largest scroll area; else
    the largest list/table/outline-like container that is not the whole window;
    else the largest AXGroup below the window; else the largest element (the
    window itself). None for an empty snapshot."""
    if not snap.elements:
        return None

    def area(el) -> int:
        return el.bounds.width * el.bounds.height

    root_area = max(area(el) for el in snap.elements)
    for roles in _SCROLL_CONTAINER_TIERS:
        pool = [el for el in snap.elements if el.role in roles]
        if roles != ("AXScrollArea",):  # a container the size of the window is the window
            pool = [el for el in pool if area(el) < 0.9 * root_area]
        if pool:
            return max(pool, key=area)
    return max(snap.elements, key=area)


# ---------------------------------------------------------------------------
# Platform helpers (thin NSWorkspace / CGWindowList / NSPasteboard adapters;
# module-level so tests can monkeypatch them)
# ---------------------------------------------------------------------------


def _system_ops():
    """The platform system-ops module (frontmost / app-at-point / resolve /
    windows / clipboard). macOS is handled inline; Windows and Linux each have a
    module exposing the same function surface."""
    if sys.platform.startswith("win"):
        from computeruse.drivers import _win_system

        return _win_system
    from computeruse.drivers import _linux_system

    return _linux_system


def _frontmost_bundle() -> str:
    """App id of the frontmost app; ``"unknown"`` when undetectable.

    macOS: bundle id. Windows: process image name (e.g. "notepad.exe").
    Linux: process comm name (e.g. "gedit")."""
    if sys.platform != "darwin":
        return _system_ops().frontmost_app_id() or "unknown"
    bundle, _pid = safety.frontmost_app()
    return bundle or "unknown"


def _list_apps() -> list[dict[str, object]]:
    """Running regular (Dock-visible) applications."""
    apps: list[dict[str, object]] = []
    for running in NSWorkspace.sharedWorkspace().runningApplications():
        if running.activationPolicy() != 0:  # NSApplicationActivationPolicyRegular
            continue
        bundle = running.bundleIdentifier()
        name = running.localizedName()
        apps.append(
            {
                "bundle_id": str(bundle) if bundle else None,
                "name": str(name) if name else None,
                "pid": int(running.processIdentifier()),
                "frontmost": bool(running.isActive()),
            }
        )
    return apps


def _running_app(identifier: str) -> tuple[object, str]:
    """Resolve an identifier to (native app handle, app id).

    macOS: (NSRunningApplication, bundle id). Windows: (None, process exe) —
    the handle is unused on the core path; app id keys the permission grant.

    Raises:
        ComputerUseError: `ErrorCode.APP_NOT_FOUND` when nothing matches (macOS).
    """
    if sys.platform != "darwin":
        return None, _system_ops().resolve_app(identifier)
    needle = identifier.lower()
    for running in NSWorkspace.sharedWorkspace().runningApplications():
        bundle = running.bundleIdentifier()
        name = running.localizedName()
        if (bundle and bundle.lower() == needle) or (name and name.lower() == needle):
            return running, str(bundle) if bundle else identifier
    raise ComputerUseError(
        ErrorCode.APP_NOT_FOUND,
        f"no running application matches {identifier!r}",
        detail={"app": identifier},
    )


def _activate(running: object) -> None:
    running.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)


def _launch_app(identifier: str) -> None:
    """Launch by bundle id (``open -b``) or display name (``open -a``).

    Dotted display names ("OBS 30.1") are indistinguishable from bundle ids,
    so ``-b`` falls back to ``-a`` before giving up.
    """
    flags = ("-b", "-a") if "." in identifier else ("-a",)
    for flag in flags:
        result = subprocess.run(
            ["/usr/bin/open", flag, identifier], capture_output=True, text=True
        )
        if result.returncode == 0:
            return
    raise ComputerUseError(
        ErrorCode.APP_NOT_FOUND,
        f"could not launch {identifier!r}: {result.stderr.strip() or 'open failed'}",
        detail={"app": identifier},
    )


def _list_windows() -> list[dict[str, object]]:
    """On-screen layer-0 windows via CGWindowList, front to back.

    Internal shape: ``bounds`` is the raw ``kCGWindowBounds`` rect in
    *global points* (the CGWindowList space) — `_window_rows` projects it
    into the schema's physical pixels for tool output, and `_app_at_point`
    hit-tests against it directly.

    Window *titles* require the Screen Recording grant; without it the API
    still lists windows but ``title`` is empty — degraded, not an error.
    """
    info = (
        Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        or ()
    )
    rows: list[dict[str, object]] = []
    for window in info:
        if window.get("kCGWindowLayer", 0) != 0:
            continue  # menu bar, Dock, overlays
        bounds = window.get("kCGWindowBounds", {})
        rows.append(
            {
                "window_id": int(window["kCGWindowNumber"]),
                "app": str(window.get("kCGWindowOwnerName", "")),
                "pid": int(window.get("kCGWindowOwnerPID", 0)),
                "title": str(window.get("kCGWindowName", "")),
                "bounds": {k: int(bounds.get(k, 0)) for k in ("X", "Y", "Width", "Height")},
            }
        )
    return rows


def _window_rows() -> list[dict[str, object]]:
    """``window list`` output rows, bounds in the schema coordinate space.

    Raw ``kCGWindowBounds`` is global *points*; every other tool takes
    display-qualified *physical pixels*, so feeding raw rows to ``click``
    would land at half the intended offset on Retina. Windows without a
    projectable rect (zero-size/fully offscreen) get ``bounds: null``.
    """
    rows = []
    for row in _list_windows():
        raw = row["bounds"]
        projected = observe.project_global_rect(
            (float(raw["X"]), float(raw["Y"])),  # type: ignore[index]
            (float(raw["Width"]), float(raw["Height"])),  # type: ignore[index]
        )
        bounds = None
        if projected is not None:
            bounds = {
                "display_id": projected.display_id,
                "x": projected.x,
                "y": projected.y,
                "width": projected.width,
                "height": projected.height,
            }
        rows.append({**row, "bounds": bounds})
    return rows


def _window_owner(window_id: int) -> tuple[int, str]:
    """(pid, owner name) of an on-screen window; APP_NOT_FOUND when absent."""
    for row in _list_windows():
        if row["window_id"] == window_id:
            return int(row["pid"]), str(row["app"])  # type: ignore[arg-type]
    raise ComputerUseError(
        ErrorCode.APP_NOT_FOUND,
        f"no on-screen window with id {window_id}",
        detail={"window_id": window_id},
    )


def _window_running(window_id: int) -> tuple[object, str]:
    """(NSRunningApplication, app id) for the process owning an on-screen
    window. The app id is the bundle id when the process has one, else the
    window owner name; it is the grant key ``window raise`` gates against.

    Raises:
        ComputerUseError: `ErrorCode.APP_NOT_FOUND` when the window is gone or
            its owning process no longer runs (a raise must never report success
            for a no-op)."""
    pid, owner = _window_owner(window_id)
    running = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if running is None:
        raise ComputerUseError(
            ErrorCode.APP_NOT_FOUND,
            f"window {window_id}'s owning process {pid} is no longer running",
            detail={"window_id": window_id, "pid": pid, "owner": owner},
        )
    bundle = (str(running.bundleIdentifier()) if running.bundleIdentifier() else None) or owner
    return running, bundle


def _read_clipboard() -> str | None:
    value = NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)
    return str(value) if value is not None else None


def _write_clipboard(text: str) -> None:
    pasteboard = NSPasteboard.generalPasteboard()
    pasteboard.clearContents()
    pasteboard.setString_forType_(text, NSPasteboardTypeString)


# ---------------------------------------------------------------------------
# Same-window recheck (between permission decision and injection, PLAN.md §6)
# ---------------------------------------------------------------------------


def _app_at_point(point: Point) -> str | None:
    """Bundle id owning the topmost layer-0 window at ``point``.

    Returns None when ownership cannot be determined (unknown display, no
    window at the point) — callers degrade to no recheck rather than
    blocking, because CGWindowList excludes the menu bar and desktop.
    """
    if sys.platform != "darwin":
        return _system_ops().app_at_point_id(point.x, point.y)

    from computeruse import act  # lazy: pyobjc-backed CGEvent, macOS-only

    try:
        gx, gy = act._point_to_global(point)
    except ValueError:
        return None
    for row in _list_windows():  # front-to-back, raw global-point bounds
        b = row["bounds"]
        if b["X"] <= gx < b["X"] + b["Width"] and b["Y"] <= gy < b["Y"] + b["Height"]:  # type: ignore[index]
            running = NSRunningApplication.runningApplicationWithProcessIdentifier_(
                row["pid"]
            )
            bundle = running.bundleIdentifier() if running is not None else None
            return str(bundle) if bundle else str(row["app"])
    return None


def _recheck_frontmost(app: str, front: str | None = None) -> None:
    """Abort typing/keys when the frontmost app is no longer the gated one.

    ``front`` is the current frontmost app id (the caller supplies it so the
    browser backend can pass its bound tab); defaults to the platform frontmost.

    Raises:
        ComputerUseError: `ErrorCode.FOCUS_CHANGED` on mismatch — the text
            would land in an app that was never granted anything.
    """
    if front is None:
        front = _frontmost_bundle()
    if front != app:
        raise ComputerUseError(
            ErrorCode.FOCUS_CHANGED,
            f"frontmost app changed from {app} to {front} between the "
            "permission decision and injection; re-observe and retry",
            detail={"gated_app": app, "frontmost_app": front},
        )


def _recheck_target_app(app: str, target: Target) -> None:
    """Abort a pointer action when the window under it changed owner.

    Hit-tests the target point immediately before injection: an overlay,
    notification, or dialog sliding over the point between the decision and
    the CGEvent post would receive a click gated for a different app.

    Raises:
        ComputerUseError: `ErrorCode.FOCUS_CHANGED` on mismatch.
    """
    point = target if isinstance(target, Point) else target.bounds.center
    owner = _app_at_point(point)
    if owner is not None and owner != app:
        raise ComputerUseError(
            ErrorCode.FOCUS_CHANGED,
            f"the app under the target point is now {owner}, not the gated "
            f"{app}; re-observe and retry",
            detail={"gated_app": app, "app_at_point": owner},
        )


# ---------------------------------------------------------------------------
# Ref plumbing
# ---------------------------------------------------------------------------


def _wait_checker(snap: Snapshot, driver) -> "act.WaitChecker":
    """Build the wait checker: re-resolve the ref through the driver (so the
    re-snapshot uses the current OS backend, not macOS's `observe.snapshot`) and
    map its outcome onto the requested condition."""

    def checker(target: Element, condition: WaitCondition) -> Element | None:
        try:
            live = driver.resolve_ref(snap, target.ref)
        except ComputerUseError as exc:
            if exc.code is ErrorCode.STALE_REF:
                return target if condition is WaitCondition.GONE else None
            raise
        if condition is WaitCondition.GONE:
            return None
        if condition is WaitCondition.ACTIONABLE and not live.actionable:
            return None
        return live

    return checker


def _describe(target: Target) -> str:
    if isinstance(target, Element):
        title = f" {target.title!r}" if target.title else ""
        return f"{target.ref} ({target.role}{title})"
    return f"({target.x}, {target.y}) on display {target.display_id}"


# ---------------------------------------------------------------------------
# Runtime: the transport-free execution core
# ---------------------------------------------------------------------------


class Runtime:
    """Executes the tool surface: resolve targets, gate, act, audit.

    Holds the latest `Snapshot` (the only epoch refs are valid against), the
    `safety.PermissionStore`, and the `safety.AuditLog`. Methods return short
    result strings (except `screenshot`/`zoom`, which return image payloads)
    and raise `ComputerUseError` or `ActionRefused` for structured failures.

    One Runtime belongs to one agent workflow and one desktop/tab. Public
    operations never overlap, and an act batch holds that ownership throughout.
    Separate agents need separate Runtimes AND isolated desktops/browser tabs:
    serializing calls does not make independently planned actions share refs.
    Direct concurrent callers receive ``busy``; MCP requests use a bounded queue.
    """

    #: Class-level default of the rendering view (see ``__init__``), so a Runtime
    #: assembled without ``__init__`` (test doubles) still renders full-mode.
    _view: str = "full"
    # Defaults support lightweight __new__ test doubles. Every initialized
    # Runtime has its own lock and lifecycle state below.
    _operation_lock = RLock()
    _closed: bool = False

    def __init__(
        self,
        *,
        store: safety.PermissionStore | None = None,
        audit: safety.AuditLog | None = None,
        driver: "drivers.Driver | None" = None,
    ) -> None:
        self._operation_lock = RLock()
        self._closed = False
        self.store = store if store is not None else safety.PermissionStore()
        self.audit = audit if audit is not None else safety.AuditLog()
        #: The OS backend. Every platform op (observe/act/capture) routes through
        #: it, so the Runtime is OS-agnostic; defaults to the current platform.
        self.driver = driver if driver is not None else drivers.get_driver()
        self._current: Snapshot | None = None
        #: The rendering view ("full" | "interactive") the agent last asked for;
        #: diff snapshots and Effect Receipts render in it so an agent that chose
        #: the cheap view keeps getting it.
        self._view: str = "full"

    def close(self) -> None:
        """Wait for the active operation, then release the driver once.

        Closing is final even if the driver's cleanup raises: a partially
        closed connection must never receive more input. Queued calls will
        fail with ``closed``. Drivers without resources need no close method.
        """
        with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            self._current = None
            close = getattr(self.driver, "close", None)
            if close is not None:
                close()

    def __enter__(self) -> "Runtime":
        if self._closed:
            raise ComputerUseError(ErrorCode.CLOSED, "this Runtime is closed")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- app identity resolution -------------------------------------------
    # The OS backends resolve app identity through the platform system-ops
    # (NSWorkspace / _win_system / _linux_system) via the module-level
    # `_frontmost_bundle`/`_running_app`/recheck functions. A non-OS backend —
    # the CDP browser, whose "apps" are tabs — sets ``resolves_apps = True`` and
    # these helpers route through the driver instead, so the WHOLE gated Runtime
    # (grants, recheck, audit, MCP tools) runs on it. OS drivers don't set the
    # flag, so their path is unchanged, byte for byte.

    def _resolves_apps(self) -> bool:
        return bool(getattr(self.driver, "resolves_apps", False))

    def _frontmost(self) -> str:
        if self._resolves_apps():
            app_id, _pid = self.driver.frontmost_app()
            return app_id or "unknown"
        return _frontmost_bundle()

    def _resolve_app(self, identifier: str) -> tuple[object, str]:
        if self._resolves_apps():
            return None, identifier  # the driver validates/binds at snapshot time
        return _running_app(identifier)

    def _recheck_frontmost_app(self, app: str) -> None:
        front = self.driver.frontmost_app()[0] if self._resolves_apps() else _frontmost_bundle()
        _recheck_frontmost(app, front or "unknown")

    def _recheck_target(self, app: str, target: Target) -> None:
        # A bound CDP tab does not slide under the pointer the way an OS window
        # can, so the frontmost check is the meaningful guard there.
        if self._resolves_apps():
            return self._recheck_frontmost_app(app)
        _recheck_target_app(app, target)

    # -- gate + audit -------------------------------------------------------

    def _require_permission(self, action, app: str, *, secure: bool = False) -> safety.Decision:
        """Read the current grant and audit a refusal before any input."""
        decision = safety.check_action(action, app, store=self.store)
        if not decision.allowed:
            self.audit.record_action(
                action, app=app, decision=decision, result=decision.verdict.value, secure=secure
            )
            raise ActionRefused(decision)
        return decision

    def _run_gated(
        self, action, app: str, execute, *, recheck=None, secure: bool = False, confirm=None
    ):
        """check_action → confirm → recheck → execute → audit, for EVERY action.

        ``recheck`` is the same-window recheck (PLAN.md §6) run between the
        permission decision and injection; it receives the gated app and
        raises `ErrorCode.FOCUS_CHANGED` on mismatch. ``confirm`` is the
        optional human-confirmation callback: when the action is plausibly
        irreversible (`safety.confirmation_prompt`) it must return True to
        proceed. The confirmation runs *before* the recheck so the frontmost
        check stays closest to injection (a slow human prompt could let focus
        drift). Refusals, declined confirmations, recheck aborts, and driver
        errors are all audited before they propagate; a SECURE_FIELD failure
        forces redaction of injectable params.
        """
        decision = self._require_permission(action, app, secure=secure)
        try:
            if CONFIRMATION_GATE:
                prompt = safety.confirmation_prompt(action, app)
                if prompt is not None and not (confirm is not None and confirm(prompt)):
                    raise ComputerUseError(
                        ErrorCode.CONFIRMATION_DECLINED,
                        prompt
                        + (
                            " — declined."
                            if confirm is not None
                            else " — no confirmation channel available; blocked. "
                            "Confirm via a host that supports elicitation, or set "
                            "COMPUTERUSE_CONFIRM=0 to disable the gate."
                        ),
                        detail={"app": app, "confirmable": confirm is not None},
                    )
                if prompt is not None:
                    # The user can revoke a grant while confirmation is open.
                    decision = self._require_permission(action, app, secure=secure)
            if recheck is not None:
                recheck(app)
            started = time.perf_counter()
            result = execute()
            duration_ms = (time.perf_counter() - started) * 1000.0
        except ComputerUseError as exc:
            self.audit.record_action(
                action,
                app=app,
                decision=decision,
                result=exc.code.value,
                secure=secure or exc.code is ErrorCode.SECURE_FIELD,
            )
            raise
        # cu-meter: per-action latency + (for text results) size/token estimate,
        # so the audit log alone yields latency p50/p95, tokens/task, and the
        # full-vs-diff snapshot savings — the numbers behind the moat.
        metrics: dict[str, object] = {"duration_ms": round(duration_ms, 1)}
        if isinstance(result, str):
            metrics["result_chars"] = len(result)
            metrics["tokens_est"] = (len(result) + 3) // 4
        self.audit.record_action(
            action, app=app, decision=decision, result="ok", secure=secure, metrics=metrics
        )
        return result

    def _effect_after(self, pre: "Snapshot | None") -> str:
        """Effect Receipt: re-snapshot ``pre``'s app after a mutating action and
        return the rendered diff (what changed), advancing the ref epoch. Returns
        the bare diff (callers format it); empty string when there's no prior
        snapshot to diff against. The re-observe is a READ of an app the caller
        already cleared a higher tier for, so it needs no separate gate."""
        if pre is None or pre.app is None:
            return ""
        try:
            snap = self.driver.snapshot(pre.scope, pre.app)
        except Exception:
            return ""
        self._current = snap
        return observe.render_diff(observe.diff_snapshots(pre, snap), mode=self._view)

    # -- target resolution ----------------------------------------------------

    def _require_snapshot(self, ref: str) -> Snapshot:
        if self._current is None:
            raise ComputerUseError(
                ErrorCode.STALE_REF,
                f"no snapshot exists yet; call desktop_snapshot before targeting ref {ref!r}",
                detail={"ref": ref},
            )
        return self._current

    def _anchor(self, ref: str) -> tuple[Snapshot, Element]:
        """Look up ``ref`` in the current snapshot; structured error when it
        was never issued by that epoch (refs are snapshot-scoped)."""
        snap = self._require_snapshot(ref)
        try:
            return snap, snap.element(ref)
        except KeyError:
            raise ComputerUseError(
                ErrorCode.STALE_REF,
                f"{ref} is not in the current snapshot {snap.snapshot_id}; "
                "refs are snapshot-scoped — call desktop_snapshot again",
                detail={"ref": ref, "snapshot_id": snap.snapshot_id},
            ) from None

    def _audit_stale(self, kind: str, ref: str, exc: ComputerUseError) -> None:
        """Record a ref that failed to resolve for tool ``kind``.

        Resolution runs before any `Action` exists (there is no live target to
        gate), so the row is written with `AuditLog.record_failure`: the ref,
        the snapshot epoch it was issued against, the failure reason, and the
        error code as ``result``. An operator reading the log sees the failed
        attempt instead of a gap."""
        snap = self._current
        params: dict[str, object] = {
            "ref": ref,
            "snapshot_id": snap.snapshot_id if snap is not None else None,
            "reason": exc.detail.get("reason", "not_found"),
        }
        app = (snap.app if snap is not None and snap.app else None) or "unknown"
        self._record_failure(kind, app=app, params=params, result=exc.code.value)

    def _record_failure(self, kind: str, *, app: str, params: dict, result: str) -> None:
        """`AuditLog.record_failure` on this Runtime's log. Tolerates a Runtime
        assembled without ``__init__`` (test doubles have no ``audit``), like the
        class-level ``_view`` default above."""
        audit = getattr(self, "audit", None)
        if audit is not None:
            audit.record_failure(kind, app=app, params=params, result=result)

    def _anchor_audited(self, ref: str, kind: str) -> tuple[Snapshot, Element]:
        """`_anchor`, with a `stale_ref` failure recorded in the audit log."""
        try:
            return self._anchor(ref)
        except ComputerUseError as exc:
            self._audit_stale(kind, ref, exc)
            raise

    def _resolve(self, ref: str, kind: str) -> tuple[Snapshot, Element]:
        """Anchor ``ref`` and re-resolve it against the live tree through the
        driver. A failure (``stale_ref`` from either step, or a driver error
        during the re-walk) is recorded in the audit log before it propagates."""
        try:
            snap, _anchor = self._anchor(ref)
            live = self.driver.resolve_ref(snap, ref)
        except ComputerUseError as exc:
            self._audit_stale(kind, ref, exc)
            raise
        return snap, live

    def _target(
        self,
        ref: str | None,
        x: int | None,
        y: int | None,
        display_id: int | None,
        *,
        kind: str = "click",
    ) -> tuple[Target, str]:
        """Resolve (ref | x,y) into an actionable target plus the gating app.

        Refs re-resolve against the live tree (`observe.resolve_ref`) and
        gate against the issuing snapshot's app; raw points gate against the
        frontmost app and default to the driver's main display (so this path is
        platform-free: Linux/Windows/browser report 0, macOS `CGMainDisplayID`).
        ``kind`` names the calling tool in the audit row a failed resolution
        leaves behind.
        """
        if ref is not None:
            snap, live = self._resolve(ref, kind)
            return live, snap.app or self._frontmost()
        if x is None or y is None:
            raise ValueError("target an element ref, or both x and y coordinates")
        if display_id is None:
            display_id = int(self.driver.main_display_id())  # through the seam, not Quartz
        return Point(display_id=display_id, x=x, y=y), self._frontmost()

    # -- secure fields (shared across drivers) ---------------------------------

    def _secure_element_under(self, point: Point) -> Element | None:
        """The secure field under ``point`` in the latest snapshot, or None.

        Picks the smallest element whose bounds contain the point on the same
        display. No snapshot, no hit, or a non-secure hit all return None. The
        lookup costs no driver call: it reads the snapshot the model just acted
        from, which is also the geometry the model's coordinates refer to."""
        snap = self._current
        if snap is None:
            return None
        best: Element | None = None
        for el in snap.elements:
            b = el.bounds
            if b.display_id != point.display_id or b.width <= 0 or b.height <= 0:
                continue
            if b.x <= point.x < b.x + b.width and b.y <= point.y < b.y + b.height:
                if best is None or b.width * b.height < best.bounds.width * best.bounds.height:
                    best = el
        return best if best is not None and best.secure else None

    def _refuse_secure(self, *targets: Target) -> None:
        """Refuse a pointer action that would land on a secure field.

        A resolved element is checked directly; a raw point is hit-tested against
        the latest snapshot (`_secure_element_under`). Called inside the gate's
        ``execute`` so the refusal is audited, with injectable params redacted,
        like every other ``secure_field`` outcome, and so it applies on every
        driver (the macOS executor also refuses on its own; the browser and Linux
        pointer paths did not until this check existed).

        Raises:
            ComputerUseError: `ErrorCode.SECURE_FIELD`.
        """
        for target in targets:
            hit = (target if target.secure else None) if isinstance(target, Element) \
                else self._secure_element_under(target)
            if hit is not None:
                raise ComputerUseError(
                    ErrorCode.SECURE_FIELD,
                    f"refusing to act on secure field {hit.ref!r} ({hit.title!r}); "
                    "secure fields require human handoff",
                    detail={"ref": hit.ref, "role": hit.role},
                )

    # -- observation tools (gated at READ + audited like everything else) ------

    @_serialized
    def desktop_snapshot(
        self,
        app: str,
        scope: str = "window",
        mode: str = "full",
        budget: int | None = None,
        include_bounds: bool = False,
    ) -> str:
        if scope not in (Scope.WINDOW.value, Scope.APP.value):
            raise ValueError("scope must be 'window' or 'app' (display/element land later)")
        if mode not in observe.SNAPSHOT_MODES:
            raise ValueError("mode must be 'full', 'interactive', or 'diff'")
        if budget is not None and budget <= 0:
            raise ValueError("budget must be a positive token count")
        # TCC before per-app gating: on an ungranted machine the actionable
        # error is the doctor hint, not a per-app permission question.
        self.driver.ensure_trusted()
        _running, bundle = self._resolve_app(app)  # grants keyed by app id

        def execute() -> str:
            prev = self._current if mode == "diff" else None
            snap = self.driver.snapshot(Scope(scope), bundle)
            self._current = snap
            if mode != "diff":
                self._view = mode  # diffs and Effect Receipts follow the view last asked for
            # diff only against a prior snapshot of the SAME app (else the agent
            # switched targets and a delta is meaningless — fall back to full).
            if prev is not None and prev.app == snap.app:
                return observe.render_diff(
                    observe.diff_snapshots(prev, snap), mode=self._view, budget=budget
                )
            text = observe.render_text(
                snap, mode=self._view, budget=budget, include_bounds=include_bounds
            )
            if observe.interactive_count(snap) == 0:  # a11y→vision handoff signal
                text = f"{text}\n\n{_VISION_HANDOFF_HINT}"
            return text

        return self._run_gated(ObserveOp(verb=ObserveVerb.SNAPSHOT, app=bundle), bundle, execute)

    @_serialized
    def find(
        self,
        app: str,
        text: str | None = None,
        role: str | None = None,
        editable: bool | None = None,
        clickable: bool | None = None,
        scope: str = "window",
    ) -> str:
        """Snapshot ``app`` and return only the elements matching the filters.

        Takes a fresh snapshot (so the returned refs are live and actionable, and
        this becomes the current ref epoch), then filters via
        `observe.find_elements`. Gated + audited at READ, exactly like
        `desktop_snapshot`."""
        if scope not in (Scope.WINDOW.value, Scope.APP.value):
            raise ValueError("scope must be 'window' or 'app'")
        if text is None and role is None and editable is None and clickable is None:
            raise ValueError("give at least one filter: text, role, editable, or clickable")
        self.driver.ensure_trusted()
        _running, bundle = self._resolve_app(app)

        def execute() -> str:
            snap = self.driver.snapshot(Scope(scope), bundle)
            self._current = snap  # refs from this call are what the agent acts on
            matches = observe.find_elements(
                snap, text=text, role=role, editable=editable, clickable=clickable
            )
            return observe.render_matches(snap, matches)

        return self._run_gated(ObserveOp(verb=ObserveVerb.SNAPSHOT, app=bundle), bundle, execute)

    @_serialized
    def screenshot(
        self, display_id: int | None = None, max_long_edge: int = _DEFAULT_MAX_LONG_EDGE,
        marks: bool = False,
    ) -> tuple[str, capture.ScaledImage]:
        def execute() -> tuple[str, "capture.ScaledImage"]:
            import dataclasses

            from computeruse import capture  # lazy: pyobjc-backed, macOS-only

            shot = self.driver.screenshot(display_id)
            scaled = capture.downscale(shot.png, max_long_edge)
            display = shot.display
            if (scaled.source_width, scaled.source_height) != (display.width, display.height):
                # The driver's PNG is not in display space (e.g. a DPR>1 capture);
                # the coordinate contract is the Display, which is what the text
                # below tells the model to multiply by and what marks/snap use.
                scaled = dataclasses.replace(
                    scaled, source_width=display.width, source_height=display.height)
            text = (
                f"display {display.display_id}: {scaled.width}x{scaled.height} px image, "
                f"downscaled from {display.width}x{display.height} physical px "
                f"(backing scale {display.scale}); multiply image coordinates by "
                f"{display.width}/{scaled.width} to get physical pixels"
            )
            if marks and self._current is not None:  # Set-of-Mark: draw refs on the image
                from computeruse import marks as _marks

                m = _marks.marks_for(self._current, scaled, display.display_id)
                if m:
                    scaled = dataclasses.replace(scaled, png=_marks.draw_marks(scaled.png, m))
                    text += (
                        f"; {len(m)} elements from the latest snapshot are marked with their "
                        "ref number — click/act on a ref you see rather than guessing pixels"
                    )
            return text, scaled

        app = self._frontmost()
        return self._run_gated(ObserveOp(verb=ObserveVerb.SCREENSHOT, app=app), app, execute)

    @_serialized
    def zoom(self, display_id: int, x: int, y: int, width: int, height: int) -> bytes:
        app = self._frontmost()
        return self._run_gated(
            ObserveOp(verb=ObserveVerb.ZOOM, app=app),
            app,
            lambda: self.driver.zoom_region(
                Bounds(display_id=display_id, x=x, y=y, width=width, height=height)
            ),
        )

    def _browser_feed(self, app: str, method: str, verb: ObserveVerb, what: str) -> str:
        """Read a browser-only observation feed (console/network) through the gate.

        Gated + audited at READ like any observation; raises UNSUPPORTED on a
        backend that has no such feed."""
        fn = getattr(self.driver, method, None)
        if fn is None:
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                f"{what} is only available on the browser backend",
                detail={"driver": self.driver.name},
            )
        _running, bundle = self._resolve_app(app)
        return self._run_gated(ObserveOp(verb=verb, app=bundle), bundle,
                               lambda: json.dumps(fn()))

    @_serialized
    def console(self, app: str) -> str:
        """Recent console output + uncaught exceptions from the browser backend."""
        return self._browser_feed(app, "console_messages", ObserveVerb.CONSOLE, "console")

    @_serialized
    def network(self, app: str) -> str:
        """Completed network outcomes (status codes + failures) from the browser."""
        return self._browser_feed(app, "network_requests", ObserveVerb.NETWORK, "network")

    # -- action tools -----------------------------------------------------------

    @_serialized
    def click(
        self,
        ref: str | None = None,
        x: int | None = None,
        y: int | None = None,
        display_id: int | None = None,
        button: str = "left",
        count: int = 1,
        modifiers: list[str] | None = None,
        confirm: Confirmer | None = None,
        verify: bool = False,
    ) -> str:
        pre = self._current if verify else None
        parsed_button = MouseButton(button)
        if count not in (1, 2, 3):
            raise ValueError(f"count must be 1, 2 or 3, got {count}")
        mods = tuple(modifiers or ())
        unknown = sorted(set(mods) - MODIFIER_KEYS)
        if unknown:  # fail fast, before the gate, so no phantom audit entry
            raise ValueError(f"unknown modifiers {unknown}; expected {sorted(MODIFIER_KEYS)}")
        target, app = self._target(ref, x, y, display_id, kind="click")
        action = Click(target=target, button=parsed_button, count=count, modifiers=mods)

        def execute() -> None:
            self._refuse_secure(target)  # audited refusal, every driver
            # AX activation (no cursor movement) is only meaningful for a plain
            # left single-click on a resolved element; anything with a button,
            # count, or modifier semantics goes through synthesized mouse events.
            if (
                PREFER_AX_ACTIONS
                and isinstance(target, Element)
                and parsed_button is MouseButton.LEFT
                and count == 1
                and not mods
                and self.driver.press_element(target)
            ):
                return  # activated via AX — the user's cursor never moved
            self.driver.click(target, button=parsed_button, count=count, modifiers=mods)

        self._run_gated(
            action, app, execute, recheck=partial(self._recheck_target, target=target), confirm=confirm
        )
        msg = f"clicked {_describe(target)}"
        effect = self._effect_after(pre)
        return f"{msg}\n\neffect: {effect}" if effect else msg

    @_serialized
    def type_text(self, text: str) -> str:
        action = TypeText(text=text)
        self._run_gated(
            action, self._frontmost(), lambda: self.driver.type_text(text), recheck=self._recheck_frontmost_app
        )
        return f"typed {len(text)} characters"

    @_serialized
    def key(self, chord: str) -> str:
        if sys.platform == "darwin":
            from computeruse import act  # lazy: US-layout keycode parse, macOS

            act.parse_chord(chord)  # validate before gating, so bad chords fail fast
        action = KeyChord(chord=chord)
        self._run_gated(
            action, self._frontmost(), lambda: self.driver.key_chord(chord), recheck=self._recheck_frontmost_app
        )
        return f"pressed {chord}"

    @_serialized
    def scroll(
        self,
        ref: str | None = None,
        x: int | None = None,
        y: int | None = None,
        display_id: int | None = None,
        dx: int = 0,
        dy: int = 0,
        unit: str = "lines",
        into_view: bool = False,
    ) -> str:
        parsed_unit = ScrollUnit(unit)
        target, app = self._target(ref, x, y, display_id, kind="scroll")
        action = Scroll(target=target, dx=dx, dy=dy, unit=parsed_unit)

        def execute() -> None:
            # into_view on a ref reveals the element via AX (no cursor
            # movement); everything else is a synthetic wheel scroll, which
            # macOS routes by moving the pointer to the scroll point.
            if (
                into_view
                and PREFER_AX_ACTIONS
                and isinstance(target, Element)
                and self.driver.scroll_into_view(target)
            ):
                return
            self._refuse_secure(target)  # the wheel path moves the pointer onto the target
            self.driver.scroll(target, dx=dx, dy=dy, unit=parsed_unit)

        self._run_gated(
            action, app, execute, recheck=partial(self._recheck_target, target=target)
        )
        if into_view:
            return f"scrolled {_describe(target)} into view"
        return f"scrolled {_describe(target)} by (dx={dx}, dy={dy}) {parsed_unit.value}"

    @_serialized
    def drag(
        self,
        start_ref: str | None = None,
        start_x: int | None = None,
        start_y: int | None = None,
        end_ref: str | None = None,
        end_x: int | None = None,
        end_y: int | None = None,
        display_id: int | None = None,
    ) -> str:
        start, start_app = self._target(start_ref, start_x, start_y, display_id, kind="drag")
        end, _ = self._target(end_ref, end_x, end_y, display_id, kind="drag")
        action = Drag(start=start, end=end)

        def execute() -> None:
            self._refuse_secure(start, end)  # neither endpoint may be a secure field
            self.driver.drag(start, end)

        self._run_gated(
            action,
            start_app,
            execute,
            recheck=partial(self._recheck_target, target=start),
        )
        return f"dragged {_describe(start)} -> {_describe(end)}"

    @_serialized
    def wait_for(self, ref: str, condition: str = "exists", timeout_s: float = 10.0) -> str:
        parsed = WaitCondition(condition)
        if not math.isfinite(timeout_s) or timeout_s < 0:
            raise ValueError("timeout_s must be finite and nonnegative")
        # Clamp: the tool runs on a worker thread, but an unbounded poll would
        # still pin that thread (and the model's patience) for minutes.
        timeout_s = min(timeout_s, MAX_WAIT_TIMEOUT_S)
        snap, anchor = self._anchor_audited(ref, "waitfor")
        action = WaitFor(target=anchor, condition=parsed, timeout_s=timeout_s)
        self._run_gated(
            action,
            snap.app or self._frontmost(),
            lambda: self.driver.wait_for(
                anchor, condition=parsed, timeout_s=timeout_s,
                checker=_wait_checker(snap, self.driver),
            ),
        )
        return f"{ref} {parsed.value}: satisfied"

    @_serialized
    def act_batch(self, steps: list[dict], *, confirm: "Confirmer | None" = None,
                  verify: bool = False) -> str:
        """Execute act steps in ONE call — the transactional path that collapses
        N observe→act round-trips into 1 (agent speed). Each step is gated +
        audited exactly like its standalone tool; the batch STOPS at the first
        failure. Returns JSON: a list of per-step {i, do, ok, result | error}.

        Step shapes (key ``do`` selects the action):
          {"do":"click","ref":"e5"}  (+ button, count, modifiers, or x/y/display_id)
          {"do":"type","text":"..."}
          {"do":"key","chord":"cmd+s"}
          {"do":"scroll","ref":"e3","dy":5}  (+ dx, unit, into_view, or x/y)
          {"do":"drag","start_ref":"e1","end_ref":"e2"}
          {"do":"wait_for","ref":"e7","condition":"actionable"}  (+ timeout_s)
        """
        if not isinstance(steps, list) or not steps:
            raise ValueError("steps must be a non-empty list of step objects")
        if len(steps) > MAX_BATCH_STEPS:
            raise ValueError(f"steps must contain at most {MAX_BATCH_STEPS} actions")
        pre = self._current if verify else None
        deadline = time.monotonic() + MAX_BATCH_DURATION_S
        out: list[dict] = []
        for i, step in enumerate(steps):
            if not isinstance(step, dict) or "do" not in step:
                out.append({"i": i, "ok": False, "error": "each step needs a 'do' field"})
                break
            do = step["do"]
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ComputerUseError(
                        ErrorCode.TIMEOUT,
                        "batch time budget exhausted; remaining steps were not executed",
                        detail={"max_duration_s": MAX_BATCH_DURATION_S, "completed_steps": i},
                    )
                out.append({"i": i, "do": do, "ok": True,
                            "result": self._dispatch_step(do, step, confirm, remaining)})
            except ActionRefused as exc:
                out.append({"i": i, "do": do, "ok": False, "error": refusal_text(exc.decision)})
                break
            except ComputerUseError as exc:
                out.append({"i": i, "do": do, "ok": False, "error": error_text(exc)})
                break
            except (KeyError, TypeError, ValueError) as exc:
                out.append({"i": i, "do": do, "ok": False, "error": str(exc)})
                break
        if verify:  # Effect Receipt: one post-batch diff of what changed
            return json.dumps({"steps": out, "effect": self._effect_after(pre)})
        return json.dumps(out)

    def _dispatch_step(self, do: str, step: dict, confirm, remaining_s: float = MAX_BATCH_DURATION_S):
        if do == "click":
            return self.click(step.get("ref"), step.get("x"), step.get("y"), step.get("display_id"),
                              step.get("button", "left"), step.get("count", 1),
                              step.get("modifiers"), confirm=confirm)
        if do == "type":
            return self.type_text(step["text"])
        if do == "key":
            return self.key(step["chord"])
        if do == "scroll":
            return self.scroll(step.get("ref"), step.get("x"), step.get("y"), step.get("display_id"),
                               step.get("dx", 0), step.get("dy", 0), step.get("unit", "lines"),
                               step.get("into_view", False))
        if do == "drag":
            return self.drag(step.get("start_ref"), step.get("start_x"), step.get("start_y"),
                             step.get("end_ref"), step.get("end_x"), step.get("end_y"),
                             step.get("display_id"))
        if do == "wait_for":
            timeout_s = step.get("timeout_s", 10.0)
            if not math.isfinite(timeout_s) or timeout_s < 0:
                raise ValueError("timeout_s must be finite and nonnegative")
            return self.wait_for(step["ref"], step.get("condition", "exists"),
                                 min(timeout_s, remaining_s))
        raise ValueError(f"unknown step '{do}' — use click/type/key/scroll/drag/wait_for")

    @_serialized
    def set_value(self, ref: str, value: str) -> str:
        """Set an editable element's value directly via the a11y API (one op),
        falling back to focus + type when the app exposes no settable value.
        Gated at the target's app, FULL tier (a text-entry path). A secure
        field is refused ahead of the tier gate (the answer is ``secure_field``
        whatever the grant: a human types secrets, not this tool) and the
        refusal is written to the audit log with the ref and role only, never
        the value."""
        snap, live = self._resolve(ref, "typetext")
        app = snap.app or self._frontmost()
        if live.secure:
            self._record_failure(
                "typetext", app=app, params={"ref": ref, "role": live.role},
                result=ErrorCode.SECURE_FIELD.value,
            )
            raise ComputerUseError(
                ErrorCode.SECURE_FIELD,
                "refusing to set a secure field; secrets are entered by the human",
                detail={"ref": ref, "role": live.role},
            )
        action = TypeText(text=value)

        def execute() -> None:
            if self.driver.set_value(live, value):
                return
            self.driver.press_element(live)  # fallback: focus then synthesize typing
            self.driver.type_text(value)

        self._run_gated(action, app, execute, recheck=partial(self._recheck_target, target=live))
        return f"set {ref} = {value!r}"

    @_serialized
    def scroll_to_find(self, app: str, text: str | None = None, role: str | None = None,
                       direction: str = "down", max_scrolls: int = 6, scope: str = "window",
                       ref: str | None = None) -> str:
        """Scroll a view until an element matching text/role enters it, then
        return its ref — for targets not in the current snapshot because they're
        scrolled out of a long/virtualized list. Re-observes each step. Gated at
        CLICK tier (it scrolls); the inner READ snapshots are covered by it.

        ``ref`` names the element to wheel over (the scrolling list itself);
        without it the largest scroll container in each snapshot is used."""
        if text is None and role is None:
            raise ValueError("give text and/or role to find")
        if direction not in ("down", "up"):
            raise ValueError("direction must be 'down' or 'up'")
        if scope not in (Scope.WINDOW.value, Scope.APP.value):
            raise ValueError("scope must be 'window' or 'app'")
        if isinstance(max_scrolls, bool) or not isinstance(max_scrolls, int) or not 0 <= max_scrolls <= MAX_SCROLLS:
            raise ValueError(f"max_scrolls must be an integer between 0 and {MAX_SCROLLS}")
        self.driver.ensure_trusted()
        _running, bundle = self._resolve_app(app)
        pinned = self._anchor_audited(ref, "scroll") if ref is not None else None
        dy = 5 if direction == "down" else -5
        self._require_permission(Scroll(target=Point(0, 0, 0), dy=dy), bundle)

        def inject_scroll(anchor: Element) -> None:
            self._refuse_secure(anchor)
            self.driver.scroll(anchor, dy=dy)

        def execute() -> str:
            for i in range(max_scrolls + 1):
                snap = self.driver.snapshot(Scope(scope), bundle)
                self._current = snap
                if pinned is not None and (pinned[0].app is None or pinned[0].app != snap.app):
                    raise ComputerUseError(
                        ErrorCode.STALE_REF,
                        "the pinned scroll ref belongs to a different app; re-observe the requested app",
                        detail={"ref": ref, "ref_app": pinned[0].app, "target_app": snap.app},
                    )
                matches = observe.find_elements(snap, text=text, role=role)
                if matches:
                    return f"found after {i} scroll(s):\n{observe.render_matches(snap, matches)}"
                if i >= max_scrolls:
                    break
                anchor = (self.driver.resolve_ref(pinned[0], ref, live=snap)
                          if pinned is not None else _scroll_anchor(snap))
                if anchor is None:
                    break
                # Scrolling is a pointer action too: the container can move,
                # disappear, become secure or be covered between iterations.
                self._run_gated(
                    Scroll(target=anchor, dy=dy), bundle, partial(inject_scroll, anchor),
                    recheck=partial(self._recheck_target, target=anchor),
                )
            return f"not found after {max_scrolls} scroll(s): no element matches text={text!r} role={role!r}"

        # Each injected scroll has its own receipt, including those that
        # complete before a later iteration fails; the outer row is observation.
        return self._run_gated(ObserveOp(verb=ObserveVerb.SNAPSHOT, app=bundle), bundle, execute)

    @_serialized
    def app(self, action: str, name: str | None = None) -> str:
        # Routed through the driver (running_apps/launch_app/activate_app), so the
        # browser backend lists/opens/focuses TABS and Windows/Linux use their own
        # backends — the macOS driver delegates to the same module helpers, so its
        # behavior is unchanged.
        verb = AppVerb(action)
        if verb is AppVerb.LIST:
            rows = self._run_gated(AppOp(verb=verb), self._frontmost(), self.driver.running_apps)
            return json.dumps(rows)
        if verb is AppVerb.QUIT:
            raise ValueError("app quit is not exposed in the MVP tool surface")
        if name is None:
            raise ValueError(f"app {verb.value} requires name")
        if verb is AppVerb.LAUNCH:
            if self._resolves_apps():
                gate_key = self._frontmost()  # browser: launch == navigate the bound tab
            else:
                try:  # gate by resolved id when possible, so grant keys stay unified
                    _, gate_key = self._resolve_app(name)
                except ComputerUseError:
                    gate_key = name  # not running yet: the identifier is the best key
            self._run_gated(AppOp(verb=verb, app=gate_key), gate_key,
                            lambda: self.driver.launch_app(name))
            return f"launched {name}"
        _running, bundle = self._resolve_app(name)  # FOCUS
        self._run_gated(AppOp(verb=verb, app=bundle), bundle,
                        lambda: self.driver.activate_app(name))
        return f"focused {bundle}"

    @_serialized
    def window(self, action: str, window_id: int | None = None) -> str:
        verb = WindowVerb(action)
        if verb is WindowVerb.LIST:
            rows = self._run_gated(WindowOp(verb=verb), self._frontmost(), self.driver.windows)
            return json.dumps(rows)
        if verb is not WindowVerb.RAISE:
            raise ValueError(
                "window supports 'list' and 'raise' in the MVP (move/resize/minimize land later)"
            )
        if window_id is None:
            raise ValueError("window raise requires window_id")
        # Through the driver seam: the owner (the grant key) is resolved first so
        # the gate checks the right app, then the raise itself runs gated. macOS
        # activates the owning app (per-window AXRaise needs the private
        # CGWindowID<->AXUIElement bridge); Linux sends _NET_ACTIVE_WINDOW to the
        # window; the browser and Windows return a structured `unsupported`.
        owner = self.driver.window_owner(window_id)
        op = WindowOp(verb=verb, window_id=window_id)
        self._run_gated(op, owner, lambda: self.driver.raise_window(window_id))
        return f"raised window {window_id} ({owner})"

    @_serialized
    def clipboard(self, action: str, text: str | None = None) -> str:
        verb = ClipboardVerb(action)
        app = self._frontmost()
        if verb is ClipboardVerb.READ:
            content = self._run_gated(ClipboardOp(verb=verb), app, self.driver.read_clipboard)
            return content if content is not None else ""
        if text is None:
            raise ValueError("clipboard write requires text")
        self._run_gated(ClipboardOp(verb=verb, text=text), app,
                        lambda: self.driver.write_clipboard(text))
        return f"wrote {len(text)} characters to the clipboard"

    # -- named dispatch (the agent loop, CLI `run-once`) ------------------------

    #: Tools ``run-once`` may call: the action verbs. Refs (and so ``wait_for``)
    #: need a live snapshot epoch, which a one-shot process never has.
    RUN_ONCE_TOOLS = frozenset(
        {"click", "type", "key", "scroll", "drag", "app", "window", "clipboard"}
    )

    @_serialized
    def call_tool(
        self, tool: str, params: dict[str, object], *, confirm: "Confirmer | None" = None
    ):
        """Execute any tool of the MCP surface by name, ``params`` being the
        tool's keyword arguments (the agent loop's entry point).

        Returns what the Runtime method returns: a string for every tool except
        ``screenshot`` (``(text, ScaledImage)``) and ``zoom`` (PNG bytes).
        ``confirm`` is the human-confirmation callback threaded into ``click``
        and ``act``; without one, plausibly irreversible actions fail safe.
        Unknown names raise ``ValueError``; the tools themselves raise
        `ComputerUseError` / `ActionRefused` exactly as under the MCP server.
        """
        methods: dict[str, Callable] = {
            "desktop_snapshot": self.desktop_snapshot,
            "find": self.find,
            "screenshot": self.screenshot,
            "zoom": self.zoom,
            "console": self.console,
            "network": self.network,
            "click": partial(self.click, confirm=confirm),
            "type": self.type_text,
            "key": self.key,
            "scroll": self.scroll,
            "drag": self.drag,
            "wait_for": self.wait_for,
            "act": partial(self.act_batch, confirm=confirm),
            "set_value": self.set_value,
            "scroll_to_find": self.scroll_to_find,
            "app": self.app,
            "window": self.window,
            "clipboard": self.clipboard,
        }
        if tool not in methods:
            raise ValueError(f"unknown tool {tool!r}; expected one of {sorted(methods)}")
        return methods[tool](**params)  # type: ignore[arg-type]

    @_serialized
    def dispatch(self, tool: str, params: dict[str, object]) -> str:
        """Execute one action tool by name (the ``run-once`` entry point).

        Only the string-returning action tools are dispatchable here;
        observation tools have their own CLI subcommands / return image
        payloads, and refs (and therefore ``wait_for``) are unavailable: each
        ``run-once`` invocation is a fresh process with no snapshot epoch, so
        click/scroll/drag take x/y coordinate targets only. `call_tool` is the
        unrestricted sibling the agent loop uses.
        """
        if tool not in self.RUN_ONCE_TOOLS:
            raise ValueError(
                f"unknown tool {tool!r}; expected one of {sorted(self.RUN_ONCE_TOOLS)}"
            )
        return self.call_tool(tool, params)


# ---------------------------------------------------------------------------
# MCP wiring
# ---------------------------------------------------------------------------

_INSTRUCTIONS = (
    "Accessibility-first computer use (macOS, Windows, Linux, and Chromium over CDP). "
    "Call desktop_snapshot first and act on "
    "element refs (click ref='e14'); refs are valid ONLY against the latest "
    "snapshot — a stale_ref error means the UI changed, re-observe. Prefer "
    "mode='interactive' (actionable elements only, same refs, far fewer tokens) "
    "and mode='diff' to re-observe after an action. Actions are "
    "gated by per-app permission tiers (read/click/full, keyed by bundle id); "
    "needs_permission/deny results must be resolved by the human user. For "
    "permission_denied_* errors, run `computeruse doctor`."
)


def build_server(
    *,
    store: safety.PermissionStore | None = None,
    audit: safety.AuditLog | None = None,
    runtime: "Runtime | None" = None,
    max_pending_calls: int = 32,
    queue_timeout_s: float = 30.0,
) -> "FastMCP":
    """Construct the MCP server with the v1 tool surface registered.

    Args:
        store: Permission grant store (default: the standard config path).
        audit: Audit log (default: the standard log directory).
        runtime: An existing Runtime to expose (default: a new one built from
            ``store``/``audit``). The agent loop passes its own so the tool
            list matches the driver it acts through. Caller-supplied Runtimes
            remain caller-owned; the server closes only a Runtime it creates.
        max_pending_calls: Maximum admitted calls, including the active call.
            Excess calls receive ``busy`` without starting a worker thread.
        queue_timeout_s: Maximum wait for the active call to finish. Expired
            calls receive ``busy`` and are never executed. Once a native call
            starts it runs to completion despite transport cancellation: Python
            cannot safely interrupt injected input or a blocking native API.

    Returns:
        The configured server; the CLI runs it over stdio
        (``computeruse mcp``).
    """
    import anyio.from_thread
    import anyio.to_thread
    from mcp.server.fastmcp import FastMCP, Image
    from mcp.server.fastmcp.exceptions import ToolError
    from pydantic import BaseModel

    if isinstance(max_pending_calls, bool) or not isinstance(max_pending_calls, int) or max_pending_calls < 1:
        raise ValueError("max_pending_calls must be a positive integer")
    if not math.isfinite(queue_timeout_s) or queue_timeout_s <= 0:
        raise ValueError("queue_timeout_s must be finite and positive")
    owns_runtime = runtime is None
    runtime = runtime if runtime is not None else Runtime(store=store, audit=audit)
    admission = anyio.CapacityLimiter(max_pending_calls)
    execution = anyio.Lock()

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield {}
        finally:
            if owns_runtime:
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(runtime.close)

    server = FastMCP("computeruse", instructions=_INSTRUCTIONS, lifespan=lifespan)

    async def run(fn, /, *args, **kwargs):
        """Run a blocking Runtime call on a worker thread and convert
        structured failures into clear tool-error strings.

        Admission and waiting happen on the event loop, before the thread hop.
        Cancellation while queued removes the call without executing it. An
        active call retains execution ownership until its worker has finished,
        even if its requester disconnects; subsequent calls cannot race it.
        """
        try:
            try:
                admission.acquire_nowait()
            except anyio.WouldBlock:
                raise ComputerUseError(
                    ErrorCode.BUSY,
                    "the Runtime request queue is full; retry later",
                    detail={"retryable": True, "max_pending_calls": max_pending_calls},
                ) from None
            try:
                try:
                    with anyio.fail_after(queue_timeout_s):
                        await execution.acquire()
                except TimeoutError:
                    raise ComputerUseError(
                        ErrorCode.BUSY,
                        "timed out waiting for the active Runtime operation; retry later",
                        detail={"retryable": True, "queue_timeout_s": queue_timeout_s},
                    ) from None
                try:
                    with anyio.CancelScope(shield=True):
                        return await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))
                finally:
                    execution.release()
            finally:
                admission.release()
        except ComputerUseError as exc:
            raise ToolError(error_text(exc)) from exc
        except ActionRefused as exc:
            raise ToolError(refusal_text(exc.decision)) from exc

    class _ConfirmResponse(BaseModel):
        """Empty elicitation schema — the human's answer is carried entirely by
        the accept/decline/cancel action, so no fields are collected."""

    async def _elicit_confirmation(ctx, prompt: str) -> bool:
        """Ask the host to confirm via MCP elicitation; True only on 'accept'.

        ``ctx`` is a ``mcp.server.fastmcp.Context`` (obtained via
        ``server.get_context()`` rather than an annotated tool param — see the
        note in the click tool)."""
        result = await ctx.elicit(message=prompt, schema=_ConfirmResponse)
        return getattr(result, "action", None) == "accept"

    def _confirmer_for(ctx) -> Confirmer | None:
        """A sync confirmer bridging a worker thread to the host's elicitation.

        Returns None when the gate is disabled. When enabled but the host has
        no elicitation channel (``ctx.elicit`` raises), the confirmer returns
        False so `_run_gated` fails the action safely rather than firing it
        unconfirmed.
        """
        if not CONFIRMATION_GATE:
            return None

        def confirm(prompt: str) -> bool:
            try:
                return bool(anyio.from_thread.run(_elicit_confirmation, ctx, prompt))
            except Exception:
                return False  # host cannot elicit -> fail-safe deny

        return confirm

    @server.tool(name="desktop_snapshot")
    async def desktop_snapshot(
        app: str,
        scope: str = "window",
        mode: str = "full",
        budget: int | None = None,
        include_bounds: bool = False,
    ) -> str:
        """Capture a pruned accessibility-tree snapshot of one app as indented
        text with element refs (e1, e2, ...). Refs are valid ONLY against this
        latest snapshot: act on them promptly and re-observe after the UI
        changes (a stale_ref error means the tree moved). scope='window'
        covers the frontmost window, 'app' all windows.

        mode='full' (default) returns the complete pruned tree. mode='interactive'
        returns the same snapshot cut down to what you can act on (buttons,
        fields, links, rows, tabs, checkable/expandable/selected items) plus the
        windows, dialogs, toolbars and titled groups that keep them apart; the
        static text under each container is folded into one 'text:' line. Same
        refs as the full view, usually a fraction of the tokens: prefer it, and
        use mode='full' or `find` when you need the text. mode='diff' returns
        ONLY what changed since your last snapshot of this app (added / removed /
        changed elements), rendered in the view you last asked for; use it to
        re-observe after an action, and read '(no change)' as 'the action had no
        visible effect'. budget=N caps the reply at about N tokens (the tail is
        replaced by a marker counting the omitted elements). include_bounds=true
        prints geometry on every line. Tier 'read' for the scoped app. Needs the
        Accessibility permission (see `computeruse doctor`)."""
        return await run(runtime.desktop_snapshot, app, scope, mode, budget, include_bounds)

    @server.tool(name="find")
    async def find(
        app: str,
        text: str | None = None,
        role: str | None = None,
        editable: bool | None = None,
        clickable: bool | None = None,
        scope: str = "window",
    ) -> str:
        """Find elements in an app without dumping its whole tree — the targeted
        alternative to desktop_snapshot when you know what you're looking for.
        text = case-insensitive substring of an element's title or value; role =
        substring of the accessibility role (e.g. 'button', 'textfield',
        'checkbox', 'link'); editable/clickable = keep only elements with that
        capability. Give at least one filter. Returns each match's ref, role,
        title, value, flags, and bounds. Takes a fresh snapshot, so the returned
        refs (e1..eN) are the current epoch — act on them promptly and re-observe
        after the UI changes. scope='window'|'app'. Tier 'read'."""
        return await run(runtime.find, app, text, role, editable, clickable, scope)

    @server.tool(name="screenshot")
    async def screenshot(
        display_id: int | None = None, max_long_edge: int = _DEFAULT_MAX_LONG_EDGE,
        marks: bool = False,
    ) -> list:
        """Capture one display (default: main) as a PNG downscaled to at most
        max_long_edge px on its long edge. The accompanying text states the
        physical resolution and how to map image coordinates back to physical
        pixels. Prefer desktop_snapshot refs; this is the vision fallback.

        marks=true draws each interactive element from your latest snapshot on
        the image, labeled with its ref (Set-of-Mark) — so you can name a ref
        ('click e7') off the picture instead of guessing pixel coordinates. Take
        a desktop_snapshot first so there are refs to mark. Tier 'read' against
        the frontmost app. Needs the Screen Recording permission."""
        text, scaled = await run(runtime.screenshot, display_id, max_long_edge, marks)
        return [text, Image(data=scaled.png, format="png")]

    @server.tool(name="zoom")
    async def zoom(display_id: int, x: int, y: int, width: int, height: int) -> list:
        """Return a native-resolution PNG crop of one display region
        (display-qualified physical pixels) for reading small text or UI the
        downscaled screenshot cannot resolve. Tier 'read' against the
        frontmost app. Needs the Screen Recording permission."""
        png = await run(runtime.zoom, display_id, x, y, width, height)
        return [f"zoom of display {display_id} at ({x}, {y}) {width}x{height}", Image(data=png, format="png")]

    @server.tool(name="click")
    async def click(
        ref: str | None = None,
        x: int | None = None,
        y: int | None = None,
        display_id: int | None = None,
        button: str = "left",
        count: int = 1,
        modifiers: list[str] | None = None,
        verify: bool = False,
    ) -> str:
        """Click an element ref from the latest desktop_snapshot (preferred;
        re-resolved against the live tree) or a raw x/y point in physical
        pixels (display_id defaults to the main display). button:
        left|right|middle; count: 1-3; modifiers: cmd|ctrl|alt|shift|fn.
        Gated at tier 'click' for the target app; a needs_permission result
        means the user must grant that app first; focus_changed means another
        app moved over the target — re-observe. A plausibly irreversible click
        (Delete, Move to Trash, ...) first asks you to confirm via elicitation;
        confirmation_declined means it was not approved. verify=true appends an
        'effect:' block — the post-click snapshot diff — so you can confirm what
        the click changed without a separate desktop_snapshot round-trip."""
        # get_context() (not an annotated param) keeps the mcp import lazy: an
        # annotated `ctx: Context` would force eval_str resolution of Context
        # against module globals, which this file's lazy import can't satisfy.
        return await run(
            runtime.click, ref, x, y, display_id, button, count, modifiers,
            confirm=_confirmer_for(server.get_context()), verify=verify,
        )

    @server.tool(name="type")
    async def type_text(text: str) -> str:
        """Type literal text into the focused element (clipboard-paste path
        for long text). Gated at tier 'full' against the frontmost app;
        refuses with secure_field when a password field has focus — secrets
        are typed by the human, never by this tool."""
        return await run(runtime.type_text, text)

    @server.tool(name="key")
    async def key(chord: str) -> str:
        """Press one key chord, e.g. 'cmd+s', 'cmd+shift+t', 'escape':
        lowercase names joined by '+', modifiers first, one regular key last.
        Gated at tier 'full' against the frontmost app."""
        return await run(runtime.key, chord)

    @server.tool(name="scroll")
    async def scroll(
        ref: str | None = None,
        x: int | None = None,
        y: int | None = None,
        display_id: int | None = None,
        dx: int = 0,
        dy: int = 0,
        unit: str = "lines",
        into_view: bool = False,
    ) -> str:
        """Scroll over an element ref (latest snapshot) or an x/y point.
        Positive dy scrolls content up, positive dx scrolls content left;
        unit is 'lines' or 'pixels'. Gated at tier 'click'. Pass
        into_view=true with a ref to reveal that element via the accessibility
        API WITHOUT moving the pointer (dx/dy ignored); a wheel scroll instead
        moves the cursor to the scroll point."""
        return await run(runtime.scroll, ref, x, y, display_id, dx, dy, unit, into_view)

    @server.tool(name="drag")
    async def drag(
        start_ref: str | None = None,
        start_x: int | None = None,
        start_y: int | None = None,
        end_ref: str | None = None,
        end_x: int | None = None,
        end_y: int | None = None,
        display_id: int | None = None,
    ) -> str:
        """Press at the start target, move, and release at the end target.
        Each target is an element ref from the latest snapshot or an x/y
        point in physical pixels. Gated at tier 'click' against the app under
        the start target."""
        return await run(runtime.drag, start_ref, start_x, start_y, end_ref, end_x, end_y, display_id)

    @server.tool(name="wait_for")
    async def wait_for(ref: str, condition: str = "exists", timeout_s: float = 10.0) -> str:
        """Block until the element (ref from the latest snapshot) reaches
        condition 'exists', 'actionable', or 'gone', polling the live tree;
        raises a structured timeout error otherwise. timeout_s is clamped to
        60s. Prefer this over fixed sleeps. Tier 'read'."""
        return await run(runtime.wait_for, ref, condition, timeout_s)

    @server.tool(name="act")
    async def act(steps: list[dict], verify: bool = False) -> str:
        """Run a SEQUENCE of actions in ONE call (batched/transactional) — the
        fast path that collapses many observe→act round-trips into one. steps is
        a list of {"do": ...} objects executed in order; the batch STOPS at the
        first failure and reports it. Supported steps:
          {"do":"click","ref":"e5"}  (or "x"/"y"; + "button","count","modifiers")
          {"do":"type","text":"..."}
          {"do":"key","chord":"cmd+s"}
          {"do":"scroll","ref":"e3","dy":5}  (+ "into_view")
          {"do":"drag","start_ref":"e1","end_ref":"e2"}
          {"do":"wait_for","ref":"e7","condition":"actionable"}
        Refs come from the latest desktop_snapshot/find. Returns JSON: a list of
        per-step {i, do, ok, result | error}. Every step is gated + audited
        exactly like its standalone tool (an irreversible click still prompts for
        confirmation). Use this to run a known multi-step interaction (fill a form,
        open a menu and pick an item) without a round-trip per action. verify=true
        returns {"steps":[...], "effect": "<post-batch snapshot diff>"} instead of
        the bare step list, so one diff confirms the net change of the whole batch."""
        return await run(runtime.act_batch, steps,
                         confirm=_confirmer_for(server.get_context()), verify=verify)

    @server.tool(name="set_value")
    async def set_value(ref: str, value: str) -> str:
        """Set an editable field's value in ONE deterministic op via the
        accessibility API — no per-character typing, no focus/click dance. ref is
        an editable element from the latest desktop_snapshot/find. Falls back to
        focus+type when the app exposes no settable value. Gated at tier 'full';
        refuses secure/password fields (secrets are entered by the human, never
        this tool). Ideal for filling forms fast."""
        return await run(runtime.set_value, ref, value)

    @server.tool(name="scroll_to_find")
    async def scroll_to_find(
        app: str, text: str | None = None, role: str | None = None,
        direction: str = "down", max_scrolls: int = 6, scope: str = "window",
        ref: str | None = None,
    ) -> str:
        """Scroll a scrollable view until an element matching text and/or role
        comes into view, then return its ref — for a target that isn't in the
        current snapshot because it's scrolled out of a long or virtualized list.
        Give text (substring of title/value) and/or role. Scrolls `direction`
        ('down'|'up') up to max_scrolls times, re-observing each step; returns the
        matching ref(s) or a not-found note. Pass ref to wheel over a specific
        scrolling element (the list itself); otherwise the largest scroll
        container in view is used. Gated at tier 'click' (it scrolls)."""
        return await run(runtime.scroll_to_find, app, text, role, direction, max_scrolls, scope, ref)

    @server.tool(name="app")
    async def app(action: str, name: str | None = None) -> str:
        """Application verbs: action='list' returns running GUI apps as JSON
        (bundle_id, name, pid, frontmost); 'launch' and 'focus' take name (a
        bundle id, preferred — permission grants are keyed by bundle id — or
        a display name). launch/focus are gated at tier 'click'."""
        return await run(runtime.app, action, name)

    @server.tool(name="window")
    async def window(action: str, window_id: int | None = None) -> str:
        """Window verbs: action='list' returns on-screen windows as JSON
        (window_id, app, pid, title, bounds; titles are empty without the
        Screen Recording grant). bounds are {display_id, x, y, width, height}
        in that display's physical pixels — the same space click/scroll/drag
        take — or null for offscreen windows. 'raise' brings window_id's app
        frontmost; raise is gated at tier 'click' against the owning app."""
        return await run(runtime.window, action, window_id)

    @server.tool(name="clipboard")
    async def clipboard(action: str, text: str | None = None) -> str:
        """Clipboard access: action='read' returns the current text (tier
        'read'); action='write' sets it from text (tier 'full' — pasting is a
        typing path). Gated against the frontmost app. The clipboard is
        cross-app: reads may return content copied from any app."""
        return await run(runtime.clipboard, action, text)

    # Browser-only: a console feed is meaningful only where the backend has one,
    # so the tool appears on the surface exactly when the driver can serve it —
    # the agent gets the verbs its surface actually supports.
    if hasattr(runtime.driver, "console_messages"):
        @server.tool(name="console")
        async def console(app: str) -> str:
            """Recent console output + uncaught JS exceptions from a browser tab
            (app = the tab/target id) — how the agent verifies whether an action
            actually worked, which a screenshot can't reveal. Returns JSON:
            a list of {level: log|warning|error|exception, text}. Reading clears
            the buffer (you get what's new since you last looked). Tier 'read'."""
            return await run(runtime.console, app)

    if hasattr(runtime.driver, "network_requests"):
        @server.tool(name="network")
        async def network(app: str) -> str:
            """Completed network outcomes for a browser tab (app = the tab/target
            id) — status codes and failures behind an action ("did that POST
            return 200?"), which a screenshot can't reveal. Returns JSON: a list
            of {method, url, status} for responses and {method, url, error} for
            failures. Reading clears the buffer. Tier 'read'."""
            return await run(runtime.network, app)

    return server


def _strip_titles(schema, *, in_properties: bool = False):
    """Drop pydantic's ``title`` decorations from a JSON schema (fewer tokens for
    a planner); a property that is itself named ``title`` is preserved."""
    if isinstance(schema, dict):
        out = {}
        for key, value in schema.items():
            if key == "title" and not in_properties:
                continue
            out[key] = _strip_titles(value, in_properties=(key == "properties"))
        return out
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    return schema


def tool_specs(runtime: Runtime) -> list[dict]:
    """The MCP tool surface as plain ``{name, description, input_schema}`` dicts.

    Derived from the very registrations `build_server` makes for ``runtime``
    (so browser-only tools appear exactly when the driver serves them, and the
    schemas are the tools' real signatures, never a hand-maintained copy). The
    agent loop hands these to a planner.
    """
    srv = build_server(runtime=runtime)
    manager = getattr(srv, "_tool_manager", None)
    tools = manager.list_tools() if manager is not None else []
    return [
        {
            "name": tool.name,
            "description": (tool.description or "").strip(),
            "input_schema": _strip_titles(dict(tool.parameters)),
        }
        for tool in tools
    ]
