"""Event-driven actionability waits for one Linux accessible.

A target is ready when a fresh read says it is visible, enabled, stable, and
receiving pointer events. Stable means the on-screen box matched the previous
read and those reads are at least ``STABLE_GAP_S`` apart. Receiving events
means the topmost window at the center belongs to the target's process and
the accessible under that point is the target or a descendant.

The wait wakes on AT-SPI state, children, bounds, and focus events. With no
listener it polls at ``FALLBACK_POLL_S``. Neither wait is longer than the
time still left. An event is not success: the next read has to pass every
check. A timeout is `ErrorCode.TIMEOUT` and names the first check that failed.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from a11y_computer_use.schema import ComputerUseError, Element, ErrorCode, WaitCondition

# The same ceilings LinuxDriver.wait_for already used: 0.1s of sleep when
# the listener is down, 0.4s blocked on the event condition when it is up.
FALLBACK_POLL_S = 0.1
EVENT_WAIT_CAP_S = 0.4
STABLE_GAP_S = 0.05

_CHECKS = ("visible", "enabled", "stable", "receiving_events")


@dataclass(frozen=True, slots=True)
class Observation:
    """One read of a target. ``stable`` is not a field: the waiter computes it."""

    visible: bool
    enabled: bool
    bounds: tuple[int, int, int, int] | None
    receiving_events: bool


def require_timeout(timeout_s: object) -> float:
    """``timeout_s`` as a finite nonnegative float. Bool is rejected."""
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise TypeError("timeout_s must be a finite nonnegative number")
    if not math.isfinite(timeout_s) or timeout_s < 0:
        raise ValueError("timeout_s must be a finite nonnegative number")
    return float(timeout_s)


def failing_check(obs: Observation, *, stable: bool) -> str | None:
    """The first check that failed, or None when every check passed.

    Flags have to be ``True``. A truthy stand-in is not a pass.
    """
    if not isinstance(obs, Observation):
        return "visible"
    if obs.visible is not True:
        return "visible"
    if obs.enabled is not True:
        return "enabled"
    if stable is not True or _bounds_key(obs.bounds) is None:
        return "stable"
    if obs.receiving_events is not True:
        return "receiving_events"
    return None


def _bounds_key(bounds: object) -> tuple[int, int, int, int] | None:
    if not isinstance(bounds, tuple) or len(bounds) != 4:
        return None
    if any(isinstance(part, bool) or not isinstance(part, int) for part in bounds):
        return None
    if bounds[2] <= 0 or bounds[3] <= 0:
        return None
    return bounds


def _timeout(
    target: Element,
    condition: WaitCondition,
    timeout_s: float,
    failed: str,
    obs: Observation | None,
    *,
    stable: bool,
) -> ComputerUseError:
    reading = obs if isinstance(obs, Observation) else Observation(False, False, None, False)
    if failed not in _CHECKS and failed != "actionable":
        failed = "visible"
    return ComputerUseError(
        ErrorCode.TIMEOUT,
        f"{target.ref} did not become {failed} within {timeout_s}s",
        detail={
            "ref": target.ref,
            "condition": condition.value,
            "check": failed,
            "timeout_s": timeout_s,
            "visible": reading.visible is True,
            "enabled": reading.enabled is True,
            "stable": stable is True,
            "receiving_events": reading.receiving_events is True,
        },
    )


class _Stable:
    """Bounds memory for the stable check. The first sighting of a box is not stable."""

    def __init__(self) -> None:
        self.bounds: tuple[int, int, int, int] | None = None
        self.at: float | None = None
        self.stable = False
        self.obs: Observation | None = None

    def note(self, obs: Observation, now: float) -> bool:
        key = _bounds_key(obs.bounds)
        if key is None:
            self.bounds = None
            self.at = None
            self.stable = False
            return False
        if key == self.bounds and self.at is not None and now - self.at >= STABLE_GAP_S:
            self.stable = True
            return True
        if key != self.bounds:
            self.bounds = key
            self.at = now
        self.stable = False
        return False


class _Deadline:
    """Monotonic deadline. Injected clocks let tests advance time themselves."""

    def __init__(
        self,
        timeout_s: float,
        *,
        now: Callable[[], float] | None,
        sleep: Callable[[float], None] | None,
        wait_event: Callable[[float], bool] | None,
    ) -> None:
        self._now = now or time.monotonic
        self._sleep = sleep
        self._wait_event = wait_event
        self._deadline = self._now() + timeout_s

    def now(self) -> float:
        return float(self._now())

    def remaining(self) -> float:
        return self._deadline - self.now()

    def pause(self) -> None:
        """Wait for one event, or one bounded poll, and no longer than that."""
        remaining = self.remaining()
        if remaining <= 0:
            return
        if self._wait_event is not None:
            self._wait_event(min(EVENT_WAIT_CAP_S, remaining))
            return
        pause = min(FALLBACK_POLL_S, remaining)
        if self._sleep is not None:
            self._sleep(pause)
            return
        from a11y_computer_use.drivers import _atspi_events

        if _atspi_events.listening():
            _atspi_events.wait_for_event(
                _atspi_events.current_seq(), timeout=min(EVENT_WAIT_CAP_S, remaining),
            )
            return
        time.sleep(pause)


def wait_until_actionable(
    target: Element,
    *,
    timeout_s: object,
    read: Callable[[], Observation],
    confirm: Callable[[], Element | None],
    now: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    wait_event: Callable[[float], bool] | None = None,
) -> Element:
    """Block until ``target`` is visible, enabled, stable, and receiving events.

    ``read`` is one fresh observation. ``confirm`` re-resolves the ref and
    returns the live element only when that element is actionable. Success is
    the element from ``confirm`` after a following ``read`` still passes.
    ``wait_event`` replaces the AT-SPI wake; ``sleep`` replaces the fallback
    poll. Passing neither uses the real listener or ``FALLBACK_POLL_S``.
    """
    if not isinstance(target, Element):
        raise TypeError("target must be an Element")
    if not callable(read) or not callable(confirm):
        raise TypeError("read and confirm must be callable")
    if now is not None and not callable(now):
        raise TypeError("now must be callable")
    if sleep is not None and not callable(sleep):
        raise TypeError("sleep must be callable")
    if wait_event is not None and not callable(wait_event):
        raise TypeError("wait_event must be callable")
    limit = require_timeout(timeout_s)
    clock = _Deadline(limit, now=now, sleep=sleep, wait_event=wait_event)
    state = _Stable()

    def attempt() -> Element | str:
        obs = read()
        if not isinstance(obs, Observation):
            raise TypeError("read must return an Observation")
        state.obs = obs
        failed = failing_check(obs, stable=state.note(obs, clock.now()))
        if failed is not None:
            return failed
        live = confirm()
        if not isinstance(live, Element) or live.actionable is not True:
            return "actionable"
        again = read()
        if not isinstance(again, Observation):
            raise TypeError("read must return an Observation")
        state.obs = again
        failed = failing_check(again, stable=state.note(again, clock.now()))
        if failed is not None:
            return failed
        return live

    while True:
        result = attempt()
        if isinstance(result, Element):
            return result
        if clock.remaining() <= 0:
            raise _timeout(
                target, WaitCondition.ACTIONABLE, limit, result, state.obs, stable=state.stable,
            )
        clock.pause()


def wait_until_checker(
    target: Element,
    *,
    condition: WaitCondition,
    timeout_s: object,
    checker: Callable[[Element, WaitCondition], Element | None],
    now: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    wait_event: Callable[[float], bool] | None = None,
) -> Element:
    """Block until ``checker`` returns an element. None is not success."""
    if not isinstance(target, Element):
        raise TypeError("target must be an Element")
    if not isinstance(condition, WaitCondition):
        raise TypeError("condition must be a WaitCondition")
    if condition is WaitCondition.ACTIONABLE:
        raise ValueError("actionable waits use wait_until_actionable")
    if not callable(checker):
        raise TypeError("checker must be callable")
    if now is not None and not callable(now):
        raise TypeError("now must be callable")
    if sleep is not None and not callable(sleep):
        raise TypeError("sleep must be callable")
    if wait_event is not None and not callable(wait_event):
        raise TypeError("wait_event must be callable")
    limit = require_timeout(timeout_s)
    clock = _Deadline(limit, now=now, sleep=sleep, wait_event=wait_event)
    while True:
        found = checker(target, condition)
        if found is not None:
            if not isinstance(found, Element):
                raise TypeError("checker must return an Element or None")
            return found
        if clock.remaining() <= 0:
            raise ComputerUseError(
                ErrorCode.TIMEOUT,
                f"{target.ref} did not reach {condition.value} within {limit}s",
                detail={
                    "ref": target.ref,
                    "condition": condition.value,
                    "timeout_s": limit,
                },
            )
        clock.pause()


def probe_accessible(acc: object) -> Observation:
    """Read visibility, enabled, bounds, and whether the center receives events.

    A missing accessible, a defunct one, or a read that fails is not visible
    and not enabled. A hit-test that cannot name the node under the center
    is not receiving events. This never returns a pass it could not read.
    """
    blank = Observation(False, False, None, False)
    if acc is None:
        return blank
    from a11y_computer_use.drivers import _atspi

    try:
        if _atspi.accessible_gone(acc):
            return blank
    except Exception:  # noqa: BLE001 - an unreadable node is not visible
        return blank
    try:
        hidden = _atspi.hidden_web_target(acc)
    except Exception:  # noqa: BLE001 - an unreadable document is not visible
        return blank
    if hidden:
        return blank
    try:
        showing = _atspi._state_has(acc, "SHOWING") is True
    except Exception:  # noqa: BLE001 - an unreadable state is not showing
        showing = False
    bounds = None
    visible = False
    if showing:
        try:
            pos, size = _atspi._extents(acc)
        except Exception:  # noqa: BLE001
            pos, size = None, None
        if (
            pos is not None
            and size is not None
            and _atspi._misses_primary_screen(pos, size) is False
        ):
            box = (
                int(round(float(pos[0]))),
                int(round(float(pos[1]))),
                int(round(float(size[0]))),
                int(round(float(size[1]))),
            )
            if box[2] > 0 and box[3] > 0:
                bounds = box
                visible = True
    enabled = False
    try:
        sset = _atspi._call_first(acc, ("get_state_set",))
    except Exception:  # noqa: BLE001
        sset = None
    if sset is not None:
        try:
            enabled = (
                _atspi._state_has(acc, "ENABLED") is True
                or _atspi._state_has(acc, "SENSITIVE") is True
            )
        except Exception:  # noqa: BLE001
            enabled = False
    receiving = False
    if visible and bounds is not None:
        receiving = _receives_events(acc, bounds)
    return Observation(visible, enabled, bounds, receiving)


def _receives_events(acc: object, bounds: tuple[int, int, int, int]) -> bool:
    """True when this process owns the top window and the hit is ours.

    A different pid is a cover. A hit-test that misses, or that lands on an
    accessible that is not this node or a descendant, is not this node
    receiving the event.
    """
    from a11y_computer_use.drivers import _atspi, _linux_system

    cx = bounds[0] + bounds[2] // 2
    cy = bounds[1] + bounds[3] // 2
    try:
        window_pid = _linux_system.pid_at_point(cx, cy)
    except Exception:  # noqa: BLE001 - an unread window is not proof
        return False
    try:
        node_pid = _atspi.pid_of(acc)
    except Exception:  # noqa: BLE001
        node_pid = None
    if isinstance(window_pid, bool) or isinstance(node_pid, bool):
        return False
    if not isinstance(window_pid, int) or not isinstance(node_pid, int):
        return False
    if window_pid <= 0 or node_pid <= 0 or window_pid != node_pid:
        return False
    hit = _hit_target(acc, cx, cy)
    return hit is not None and _is_self_or_descendant(acc, hit)


def _hit_target(acc: object, x: int, y: int):
    """Deepest accessible the target's top-level frame reports at ``(x, y)``."""
    from a11y_computer_use.drivers import _atspi

    frame = _toplevel(acc)
    if frame is None:
        return None
    try:
        return _atspi._deepest_at_point(frame, x, y, 1, set())
    except Exception:  # noqa: BLE001
        return None


def _toplevel(acc: object):
    from a11y_computer_use.drivers import _atspi

    current = acc
    frame = None
    seen: set[int] = set()
    for _ in range(24):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        try:
            role = _atspi._role_name(current)
        except Exception:  # noqa: BLE001
            role = ""
        if role in {"frame", "window", "dialog"}:
            frame = current
        if role == "application":
            break
        try:
            parent = _atspi._parent_of(current)
        except Exception:  # noqa: BLE001
            break
        if parent is None or parent is current:
            break
        current = parent
    return frame if frame is not None else acc


def _is_self_or_descendant(target: object, hit: object) -> bool:
    from a11y_computer_use.drivers import _atspi

    if hit is None or target is None:
        return False
    node = hit
    seen: set[int] = set()
    for _ in range(32):
        if node is None or id(node) in seen:
            return False
        seen.add(id(node))
        try:
            if _atspi._frame_cache_key(node) == _atspi._frame_cache_key(target):
                return True
        except Exception:  # noqa: BLE001
            return False
        try:
            parent = _atspi._parent_of(node)
        except Exception:  # noqa: BLE001
            return False
        if parent is None or parent is node:
            return False
        node = parent
    return False


def read_target(target: Element) -> Observation:
    """Probe the accessible the snapshot stored for ``target``."""
    from a11y_computer_use import observe

    try:
        handle = observe.ax_handle_for(target.snapshot_id, target.ref)
    except Exception:  # noqa: BLE001
        handle = None
    return probe_accessible(handle)
