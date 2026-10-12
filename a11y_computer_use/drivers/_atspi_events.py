"""Opt-in AT-SPI event thread (``A11Y_COMPUTER_USE_ATSPI_EVENTS=1``). Linux-only.

libatspi keeps a full client-side cache of each accessible (role, name, states,
attributes, parent, children) but only *serves reads from it* when a GLib main
loop is running — ``Atspi.event_main`` sets the internal ``atspi_main_loop`` flag
that gates ``_atspi_accessible_test_cache``. So the driver's ~7 D-Bus
round-trips per node is a *gate*, not a protocol limit: run the loop and reads
collapse to the cache (measured ~1.8x faster repeat snapshots on a 26-node tree).

This module runs that loop on ONE dedicated daemon thread that owns libatspi, and
``submit(fn)`` marshals a whole driver operation onto it via ``GLib.idle_add``
(safe to call from any thread), blocking the caller for the result. libatspi
stays single-threaded, so the driver routes every AT-SPI op through here when
enabled. The standing event registrations also give us a live change feed for
event-driven waits (a follow-up).

Default OFF: without the env flag ``enabled()`` is False and the driver runs
AT-SPI calls inline exactly as before (the CI-verified path) — zero risk to the
shipped backend.
"""

from __future__ import annotations

import os
import threading
from collections import deque

_STANDING_EVENTS = (
    "object:children-changed",
    "object:state-changed",
    "object:text-changed",
    "object:property-change:accessible-name",
    "document:load-complete",
    "window:activate",
)

_thread: threading.Thread | None = None
_ready = threading.Event()
_start_lock = threading.Lock()
_listener = None
_events: deque = deque(maxlen=4096)
_events_cv = threading.Condition()
_seq = 0


def enabled() -> bool:
    """True when the event thread is opted in via ``A11Y_COMPUTER_USE_ATSPI_EVENTS``."""
    return bool(os.environ.get("A11Y_COMPUTER_USE_ATSPI_EVENTS"))


def current_seq() -> int:
    with _events_cv:
        return _seq


def wait_for_event(since_seq: int = 0, timeout: float = 0.5):
    """Block until any AT-SPI event with seq > ``since_seq`` arrives (a UI change
    on the standing set: children/state/text-changed, document:load-complete),
    or ``timeout`` seconds pass. Returns the newest such event tuple (seq, type,
    detail1) or None on timeout. Lets a waiter wake the instant the UI changes
    instead of polling on a fixed tick."""
    with _events_cv:
        def _newest():
            if _events and _events[-1][0] > since_seq:
                return _events[-1]
            return None

        found = _newest()
        if found is not None:
            return found
        _events_cv.wait(timeout)
        return _newest()


_STRUCTURAL_MARKERS = (
    "children-changed",
    "load-complete",
    "property-change",
    "window:activate",
)


def event_is_structural(event: tuple) -> bool:
    """True for a tree, load, name, or window event.

    State and text changes are ignored. A caret blink is one of those, and
    treating it as a wake would snapshot on every blink.
    """
    kind = str(event[1] if len(event) > 1 else "")
    return any(marker in kind for marker in _STRUCTURAL_MARKERS)


def wait_for_structural(since_seq: int = 0, timeout: float = 0.5):
    """Block until a structural event with seq > ``since_seq``, or ``timeout``.

    A state or text event already in the queue does not end the wait. The
    call keeps blocking until a structural event arrives or the timeout
    passes. Returns that event, or None.
    """
    import time

    deadline = time.monotonic() + max(0.0, float(timeout))
    with _events_cv:
        while True:
            found = None
            for event in _events:
                if event[0] > since_seq and event_is_structural(event):
                    found = event
                    break
            if found is not None:
                return found
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            _events_cv.wait(remaining)


def _on_event(event) -> bool:
    global _seq
    with _events_cv:
        _seq += 1
        _events.append((_seq, getattr(event, "type", ""),
                        getattr(event, "detail1", 0)))
        _events_cv.notify_all()
    return False


def _run() -> None:
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    try:
        from a11y_computer_use.drivers import _dbus_guard

        _dbus_guard.install()
        Atspi.init()
        # Same bound as `_atspi._atspi`: 300ms per call, no 15s startup grace.
        Atspi.set_timeout(300, 0)
        global _listener
        _listener = Atspi.EventListener.new(_on_event)
        for ev in _STANDING_EVENTS:
            try:
                _listener.register(ev)
            except Exception:  # noqa: BLE001 - a missing event type must not abort the rest
                pass
    finally:
        _ready.set()
    Atspi.event_main()  # runs the GLib loop; blocks this (daemon) thread


def _ensure() -> None:
    global _thread
    if _thread is not None:
        return
    with _start_lock:
        if _thread is not None:
            return
        t = threading.Thread(target=_run, name="a11y_computer_use-atspi", daemon=True)
        t.start()
        _ready.wait(5)
        _thread = t


def submit(fn, timeout: float = 15.0):
    """Run ``fn`` on the a11y thread (where the GLib loop runs, so libatspi's
    read cache is trusted) and return its result. Marshals via ``GLib.idle_add``,
    which is safe to call from any thread. Raises ``fn``'s exception, or
    ``TimeoutError`` if it does not complete within ``timeout`` seconds."""
    _ensure()
    from gi.repository import GLib

    box: dict = {}
    done = threading.Event()

    def _wrapper() -> bool:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001 - propagate to the submitting thread
            box["error"] = exc
        finally:
            done.set()
        return False  # one-shot idle callback

    GLib.idle_add(_wrapper)
    if not done.wait(timeout):
        raise TimeoutError("AT-SPI operation timed out on the event thread")
    if "error" in box:
        raise box["error"]
    return box.get("value")
