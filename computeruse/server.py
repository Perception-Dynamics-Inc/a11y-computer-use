"""MCP server: the v1 tool surface (PLAN.md §8).

12 tools: desktop_snapshot, screenshot, zoom, click, type, key, scroll,
drag, wait_for, app, window, clipboard. EVERY tool — observation included —
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
import os
import subprocess
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING

import Quartz
from AppKit import (
    NSApplicationActivateIgnoringOtherApps,
    NSPasteboard,
    NSPasteboardTypeString,
    NSRunningApplication,
    NSWorkspace,
)

from computeruse import act, capture, observe, safety
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


# ---------------------------------------------------------------------------
# Platform helpers (thin NSWorkspace / CGWindowList / NSPasteboard adapters;
# module-level so tests can monkeypatch them)
# ---------------------------------------------------------------------------


def _frontmost_bundle() -> str:
    """Bundle id of the frontmost app; ``"unknown"`` when undetectable."""
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
    """Resolve a bundle id or display name to (NSRunningApplication, bundle id).

    Raises:
        ComputerUseError: `ErrorCode.APP_NOT_FOUND` when nothing matches.
    """
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


def _recheck_frontmost(app: str) -> None:
    """Abort typing/keys when the frontmost app is no longer the gated one.

    Raises:
        ComputerUseError: `ErrorCode.FOCUS_CHANGED` on mismatch — the text
            would land in an app that was never granted anything.
    """
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


def _wait_checker(snap: Snapshot) -> act.WaitChecker:
    """Build the `act.wait_for` checker: poll `observe.resolve_ref` and map
    its outcome onto the requested condition."""

    def checker(target: Element, condition: WaitCondition) -> Element | None:
        try:
            live = observe.resolve_ref(snap, target.ref)
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
    """

    def __init__(
        self,
        *,
        store: safety.PermissionStore | None = None,
        audit: safety.AuditLog | None = None,
    ) -> None:
        self.store = store if store is not None else safety.PermissionStore()
        self.audit = audit if audit is not None else safety.AuditLog()
        self._current: Snapshot | None = None

    # -- gate + audit -------------------------------------------------------

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
        decision = safety.check_action(action, app, store=self.store)
        if not decision.allowed:
            self.audit.record_action(
                action, app=app, decision=decision, result=decision.verdict.value, secure=secure
            )
            raise ActionRefused(decision)
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
            if recheck is not None:
                recheck(app)
            result = execute()
        except ComputerUseError as exc:
            self.audit.record_action(
                action,
                app=app,
                decision=decision,
                result=exc.code.value,
                secure=secure or exc.code is ErrorCode.SECURE_FIELD,
            )
            raise
        self.audit.record_action(action, app=app, decision=decision, result="ok", secure=secure)
        return result

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

    def _target(
        self, ref: str | None, x: int | None, y: int | None, display_id: int | None
    ) -> tuple[Target, str]:
        """Resolve (ref | x,y) into an actionable target plus the gating app.

        Refs re-resolve against the live tree (`observe.resolve_ref`) and
        gate against the issuing snapshot's app; raw points gate against the
        frontmost app and default to the main display.
        """
        if ref is not None:
            snap, _anchor = self._anchor(ref)
            live = observe.resolve_ref(snap, ref)
            return live, snap.app or _frontmost_bundle()
        if x is None or y is None:
            raise ValueError("target an element ref, or both x and y coordinates")
        if display_id is None:
            display_id = int(Quartz.CGMainDisplayID())
        return Point(display_id=display_id, x=x, y=y), _frontmost_bundle()

    # -- observation tools (gated at READ + audited like everything else) ------

    def desktop_snapshot(self, app: str, scope: str = "window") -> str:
        if scope not in (Scope.WINDOW.value, Scope.APP.value):
            raise ValueError("scope must be 'window' or 'app' (display/element land later)")
        # TCC before per-app gating: on an ungranted machine the actionable
        # error is the doctor hint, not a per-app permission question.
        observe.ensure_trusted()
        _running, bundle = _running_app(app)  # grants are keyed by bundle id

        def execute() -> str:
            snap = observe.snapshot(Scope(scope), app=bundle)
            self._current = snap
            text = observe.render_text(snap)
            if observe.interactive_count(snap) == 0:  # a11y→vision handoff signal
                text = f"{text}\n\n{_VISION_HANDOFF_HINT}"
            return text

        return self._run_gated(ObserveOp(verb=ObserveVerb.SNAPSHOT, app=bundle), bundle, execute)

    def screenshot(
        self, display_id: int | None = None, max_long_edge: int = capture.DEFAULT_MAX_LONG_EDGE
    ) -> tuple[str, capture.ScaledImage]:
        def execute() -> tuple[str, capture.ScaledImage]:
            shot = capture.screenshot(display_id)
            scaled = capture.downscale(shot.png, max_long_edge)
            display = shot.display
            text = (
                f"display {display.display_id}: {scaled.width}x{scaled.height} px image, "
                f"downscaled from {display.width}x{display.height} physical px "
                f"(backing scale {display.scale}); multiply image coordinates by "
                f"{display.width}/{scaled.width} to get physical pixels"
            )
            return text, scaled

        app = _frontmost_bundle()
        return self._run_gated(ObserveOp(verb=ObserveVerb.SCREENSHOT, app=app), app, execute)

    def zoom(self, display_id: int, x: int, y: int, width: int, height: int) -> bytes:
        app = _frontmost_bundle()
        return self._run_gated(
            ObserveOp(verb=ObserveVerb.ZOOM, app=app),
            app,
            lambda: capture.zoom_region(
                Bounds(display_id=display_id, x=x, y=y, width=width, height=height)
            ),
        )

    # -- action tools -----------------------------------------------------------

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
    ) -> str:
        parsed_button = MouseButton(button)
        if count not in (1, 2, 3):
            raise ValueError(f"count must be 1, 2 or 3, got {count}")
        mods = tuple(modifiers or ())
        unknown = sorted(set(mods) - MODIFIER_KEYS)
        if unknown:  # fail fast, before the gate, so no phantom audit entry
            raise ValueError(f"unknown modifiers {unknown}; expected {sorted(MODIFIER_KEYS)}")
        target, app = self._target(ref, x, y, display_id)
        action = Click(target=target, button=parsed_button, count=count, modifiers=mods)

        def execute() -> None:
            # AX activation (no cursor movement) is only meaningful for a plain
            # left single-click on a resolved element; anything with a button,
            # count, or modifier semantics goes through synthesized mouse events.
            if (
                PREFER_AX_ACTIONS
                and isinstance(target, Element)
                and parsed_button is MouseButton.LEFT
                and count == 1
                and not mods
                and observe.press_element(target)
            ):
                return  # activated via AX — the user's cursor never moved
            act.click(target, button=parsed_button, count=count, modifiers=mods)

        self._run_gated(
            action, app, execute, recheck=partial(_recheck_target_app, target=target), confirm=confirm
        )
        return f"clicked {_describe(target)}"

    def type_text(self, text: str) -> str:
        action = TypeText(text=text)
        self._run_gated(
            action, _frontmost_bundle(), lambda: act.type_text(text), recheck=_recheck_frontmost
        )
        return f"typed {len(text)} characters"

    def key(self, chord: str) -> str:
        act.parse_chord(chord)  # validate before gating, so bad chords fail fast
        action = KeyChord(chord=chord)
        self._run_gated(
            action, _frontmost_bundle(), lambda: act.key_chord(chord), recheck=_recheck_frontmost
        )
        return f"pressed {chord}"

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
        target, app = self._target(ref, x, y, display_id)
        action = Scroll(target=target, dx=dx, dy=dy, unit=parsed_unit)

        def execute() -> None:
            # into_view on a ref reveals the element via AX (no cursor
            # movement); everything else is a synthetic wheel scroll, which
            # macOS routes by moving the pointer to the scroll point.
            if (
                into_view
                and PREFER_AX_ACTIONS
                and isinstance(target, Element)
                and observe.scroll_into_view(target)
            ):
                return
            act.scroll(target, dx=dx, dy=dy, unit=parsed_unit)

        self._run_gated(
            action, app, execute, recheck=partial(_recheck_target_app, target=target)
        )
        if into_view:
            return f"scrolled {_describe(target)} into view"
        return f"scrolled {_describe(target)} by (dx={dx}, dy={dy}) {parsed_unit.value}"

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
        start, start_app = self._target(start_ref, start_x, start_y, display_id)
        end, _ = self._target(end_ref, end_x, end_y, display_id)
        action = Drag(start=start, end=end)
        self._run_gated(
            action,
            start_app,
            lambda: act.drag(start, end),
            recheck=partial(_recheck_target_app, target=start),
        )
        return f"dragged {_describe(start)} -> {_describe(end)}"

    def wait_for(self, ref: str, condition: str = "exists", timeout_s: float = 10.0) -> str:
        parsed = WaitCondition(condition)
        # Clamp: the tool runs on a worker thread, but an unbounded poll would
        # still pin that thread (and the model's patience) for minutes.
        timeout_s = min(timeout_s, MAX_WAIT_TIMEOUT_S)
        snap, anchor = self._anchor(ref)
        action = WaitFor(target=anchor, condition=parsed, timeout_s=timeout_s)
        self._run_gated(
            action,
            snap.app or _frontmost_bundle(),
            lambda: act.wait_for(
                anchor, condition=parsed, timeout_s=timeout_s, checker=_wait_checker(snap)
            ),
        )
        return f"{ref} {parsed.value}: satisfied"

    def app(self, action: str, name: str | None = None) -> str:
        verb = AppVerb(action)
        if verb is AppVerb.LIST:
            rows = self._run_gated(AppOp(verb=verb), _frontmost_bundle(), _list_apps)
            return json.dumps(rows)
        if verb is AppVerb.QUIT:
            raise ValueError("app quit is not exposed in the MVP tool surface")
        if name is None:
            raise ValueError(f"app {verb.value} requires name")
        if verb is AppVerb.LAUNCH:
            try:  # gate by bundle id when resolvable, so grant keys stay unified
                _, gate_key = _running_app(name)
            except ComputerUseError:
                gate_key = name  # not running yet: the identifier is the best key
            self._run_gated(AppOp(verb=verb, app=gate_key), gate_key, lambda: _launch_app(name))
            return f"launched {name}"
        running, bundle = _running_app(name)  # FOCUS
        self._run_gated(AppOp(verb=verb, app=bundle), bundle, lambda: _activate(running))
        return f"focused {bundle}"

    def window(self, action: str, window_id: int | None = None) -> str:
        verb = WindowVerb(action)
        if verb is WindowVerb.LIST:
            rows = self._run_gated(WindowOp(verb=verb), _frontmost_bundle(), _window_rows)
            return json.dumps(rows)
        if verb is not WindowVerb.RAISE:
            raise ValueError(
                "window supports 'list' and 'raise' in the MVP (move/resize/minimize land later)"
            )
        if window_id is None:
            raise ValueError("window raise requires window_id")
        pid, owner = _window_owner(window_id)
        running = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if running is None:  # never report success for a no-op raise
            raise ComputerUseError(
                ErrorCode.APP_NOT_FOUND,
                f"window {window_id}'s owning process {pid} is no longer running",
                detail={"window_id": window_id, "pid": pid, "owner": owner},
            )
        bundle = (str(running.bundleIdentifier()) if running.bundleIdentifier() else None) or owner
        op = WindowOp(verb=verb, window_id=window_id)
        # MVP: raising activates the owning app (per-window AXRaise needs the
        # private CGWindowID<->AXUIElement bridge; Phase 1).
        self._run_gated(op, bundle, lambda: _activate(running))
        return f"raised window {window_id} ({bundle})"

    def clipboard(self, action: str, text: str | None = None) -> str:
        verb = ClipboardVerb(action)
        app = _frontmost_bundle()
        if verb is ClipboardVerb.READ:
            content = self._run_gated(ClipboardOp(verb=verb), app, _read_clipboard)
            return content if content is not None else ""
        if text is None:
            raise ValueError("clipboard write requires text")
        self._run_gated(ClipboardOp(verb=verb, text=text), app, lambda: _write_clipboard(text))
        return f"wrote {len(text)} characters to the clipboard"

    # -- one-shot dispatch (CLI `run-once`) -----------------------------------

    def dispatch(self, tool: str, params: dict[str, object]) -> str:
        """Execute one action tool by name (the ``run-once`` entry point).

        Only the string-returning action tools are dispatchable; observation
        tools have their own CLI subcommands / return image payloads. Refs
        (and therefore ``wait_for``) are unavailable: each ``run-once``
        invocation is a fresh process with no snapshot epoch, so click/
        scroll/drag take x/y coordinate targets only.
        """
        methods = {
            "click": self.click,
            "type": self.type_text,
            "key": self.key,
            "scroll": self.scroll,
            "drag": self.drag,
            "app": self.app,
            "window": self.window,
            "clipboard": self.clipboard,
        }
        if tool not in methods:
            raise ValueError(f"unknown tool {tool!r}; expected one of {sorted(methods)}")
        return methods[tool](**params)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# MCP wiring
# ---------------------------------------------------------------------------

_INSTRUCTIONS = (
    "Accessibility-first macOS control. Call desktop_snapshot first and act on "
    "element refs (click ref='e14'); refs are valid ONLY against the latest "
    "snapshot — a stale_ref error means the UI changed, re-observe. Actions are "
    "gated by per-app permission tiers (read/click/full, keyed by bundle id); "
    "needs_permission/deny results must be resolved by the human user. For "
    "permission_denied_* errors, run `computeruse doctor`."
)


def build_server(
    *,
    store: safety.PermissionStore | None = None,
    audit: safety.AuditLog | None = None,
) -> "FastMCP":
    """Construct the MCP server with the v1 tool surface registered.

    Args:
        store: Permission grant store (default: the standard config path).
        audit: Audit log (default: the standard log directory).

    Returns:
        The configured server; the CLI runs it over stdio
        (``computeruse mcp``).
    """
    import anyio.from_thread
    import anyio.to_thread
    from mcp.server.fastmcp import FastMCP, Image
    from mcp.server.fastmcp.exceptions import ToolError
    from pydantic import BaseModel

    runtime = Runtime(store=store, audit=audit)
    server = FastMCP("computeruse", instructions=_INSTRUCTIONS)

    async def run(fn, /, *args, **kwargs):
        """Run a blocking Runtime call on a worker thread and convert
        structured failures into clear tool-error strings.

        The thread hop keeps the MCP event loop responsive (ping,
        tools/list, cancellation) while an AX walk or ``wait_for`` blocks.
        """
        try:
            return await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))
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
    async def desktop_snapshot(app: str, scope: str = "window") -> str:
        """Capture a pruned accessibility-tree snapshot of one app as indented
        text with element refs (e1, e2, ...). Refs are valid ONLY against this
        latest snapshot: act on them promptly and re-observe after the UI
        changes (a stale_ref error means the tree moved). scope='window'
        covers the frontmost window, 'app' all windows. Tier 'read' for the
        scoped app. Needs the Accessibility permission (see `computeruse
        doctor`)."""
        return await run(runtime.desktop_snapshot, app, scope)

    @server.tool(name="screenshot")
    async def screenshot(display_id: int | None = None, max_long_edge: int = capture.DEFAULT_MAX_LONG_EDGE) -> list:
        """Capture one display (default: main) as a PNG downscaled to at most
        max_long_edge px on its long edge. The accompanying text states the
        physical resolution and how to map image coordinates back to physical
        pixels. Prefer desktop_snapshot refs; this is the vision fallback.
        Tier 'read' against the frontmost app. Needs the Screen Recording
        permission."""
        text, scaled = await run(runtime.screenshot, display_id, max_long_edge)
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
    ) -> str:
        """Click an element ref from the latest desktop_snapshot (preferred;
        re-resolved against the live tree) or a raw x/y point in physical
        pixels (display_id defaults to the main display). button:
        left|right|middle; count: 1-3; modifiers: cmd|ctrl|alt|shift|fn.
        Gated at tier 'click' for the target app; a needs_permission result
        means the user must grant that app first; focus_changed means another
        app moved over the target — re-observe. A plausibly irreversible click
        (Delete, Move to Trash, ...) first asks you to confirm via elicitation;
        confirmation_declined means it was not approved."""
        # get_context() (not an annotated param) keeps the mcp import lazy: an
        # annotated `ctx: Context` would force eval_str resolution of Context
        # against module globals, which this file's lazy import can't satisfy.
        return await run(
            runtime.click, ref, x, y, display_id, button, count, modifiers,
            confirm=_confirmer_for(server.get_context()),
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

    return server
