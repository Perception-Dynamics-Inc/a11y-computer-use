"""AT-SPI event thread. On by default on Linux when the typelib imports.

libatspi keeps a full client-side cache of each accessible (role, name, states,
attributes, parent, children) but only *serves reads from it* when a GLib main
loop is running — ``Atspi.event_main`` sets the internal ``atspi_main_loop`` flag
that gates ``_atspi_accessible_test_cache``. So the driver's ~7 D-Bus
round-trips per node is a *gate*, not a protocol limit: run the loop and reads
collapse to the cache (measured ~1.8x faster repeat snapshots on a 26-node tree).

This module runs that loop on ONE dedicated daemon thread that owns libatspi, and
``submit(fn)`` marshals a whole driver operation onto it via ``GLib.idle_add``
(safe to call from any thread), blocking the caller for the result. libatspi
stays single-threaded. Once the loop is running, the driver routes AT-SPI calls
through here. A call that is already on the event thread runs inline, so a
snapshot submitted from the caller cannot deadlock on a nested submit.

On Linux, ``enabled()`` is true when the variable is unset and the AT-SPI
typelib imports. ``0``, ``false``, ``no``, and ``off`` force the inline path.
``1``, ``true``, ``yes``, and ``on`` turn the thread on. Any other value is
ignored, with a warning, and the unset default is used. Other platforms stay
inline when the variable is unset, so constructing a Linux driver there does
not start it.
Actionability waits call ``ensure_started`` themselves, so on a Linux display
the listener is on even when the variable forces the inline path. The standing
registrations are ``object:state-changed``, ``object:children-changed``,
``object:bounds-changed``, and ``focus:``, plus the text, name, document, and
window events the cache path already watched.
"""

from __future__ import annotations

import os
import sys
import threading
import warnings
from collections import deque

_STANDING_EVENTS = (
    "object:children-changed",
    "object:state-changed",
    "object:bounds-changed",
    "focus:",
    "object:text-changed",
    "object:property-change:accessible-name",
    "document:load-complete",
    "window:activate",
)

_thread: threading.Thread | None = None
_ready = threading.Event()
_listening = threading.Event()
_start_lock = threading.Lock()
_listener = None
_listener_failed = False
_events: deque = deque(maxlen=4096)
_events_cv = threading.Condition()
_seq = 0


_ON = frozenset({"1", "true", "yes", "on"})
_OFF = frozenset({"0", "false", "no", "off"})
_typelib_ok: bool | None = None
_warned_values: set[str] = set()


def typelib_imports() -> bool:
    """True when PyGObject can import ``Atspi`` 2.0. Does not start a main loop."""
    global _typelib_ok
    if _typelib_ok is not None:
        return _typelib_ok
    if not sys.platform.startswith("linux"):
        _typelib_ok = False
        return False
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # noqa: F401
    except Exception:
        _typelib_ok = False
        return False
    _typelib_ok = True
    return True


def _default_enabled() -> bool:
    """Unset means on for a Linux process that can import the typelib."""
    return sys.platform.startswith("linux") and typelib_imports()


def enabled() -> bool:
    """True when libatspi calls should run on the event thread.

    Unset on Linux is on when the typelib imports. ``0``, ``false``, ``no``,
    and ``off`` are off. ``1``, ``true``, ``yes``, and ``on`` are on. Any
    other value warns once and uses that unset default. Comparison ignores
    case and surrounding whitespace. Actionability waits do not use this
    flag; they call `ensure_started`.
    """
    raw = os.environ.get("A11Y_COMPUTER_USE_ATSPI_EVENTS")
    if raw is None:
        return _default_enabled()
    value = raw.strip().lower()
    if value in _ON:
        return True
    if value in _OFF:
        return False
    if value not in _warned_values:
        _warned_values.add(value)
        default_on = _default_enabled()
        warnings.warn(
            f"A11Y_COMPUTER_USE_ATSPI_EVENTS={raw!r} is not 0/false/no/off or "
            "1/true/yes/on; using the default "
            f"({'on' if default_on else 'off'})",
            RuntimeWarning,
            stacklevel=2,
        )
    return _default_enabled()


def event_names() -> tuple[str, ...]:
    """Event types the listener registers, in registration order."""
    return _STANDING_EVENTS


def listening() -> bool:
    """True once the GLib loop is up and the standing events are registered."""
    return _listening.is_set()


def on_event_thread() -> bool:
    """True when the caller is the thread that runs ``Atspi.event_main``."""
    return _thread is not None and threading.current_thread() is _thread


def run(fn):
    """Run ``fn`` on the event thread when that thread is inside ``event_main``.

    A caller already on the thread runs inline, so a nested read cannot
    deadlock the loop. A listener that was never started runs inline too:
    a test that only reports ``listening()`` must not boot the loop.
    libatspi is not safe from a second thread while the loop is up. A
    child walk on the caller then segfaults inside ``event_main``.
    """
    thread = _thread
    if (
        thread is None
        or not thread.is_alive()
        or not _listening.is_set()
        or on_event_thread()
    ):
        return fn()
    return submit(fn)



def ensure_started() -> bool:
    """Start the listener on a Linux display. False when it cannot run.

    A missing display does not mark the listener failed: a later opt-in can
    still start it. A failed init is not retried on every wait.
    """
    global _listener_failed
    if _listening.is_set():
        return True
    if _listener_failed:
        return False
    if not sys.platform.startswith("linux"):
        return False
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    _ensure()
    if not _listening.is_set():
        _listener_failed = True
        return False
    return True


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
        # Set before _ready so a waiter that observes _ready also observes
        # the listener. event_main runs only after that.
        _listening.set()
    except Exception:  # noqa: BLE001 - a dead bus must not look like a live listener
        _listening.clear()
    finally:
        _ready.set()
    if not _listening.is_set():
        return
    try:
        Atspi.event_main()  # runs the GLib loop; blocks this (daemon) thread
    finally:
        _listening.clear()


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
    ``TimeoutError`` if it does not complete within ``timeout`` seconds.

    A call that is already on the event thread runs ``fn`` inline. Waiting
    for an idle callback from inside one would deadlock the loop.
    """
    if on_event_thread():
        return fn()
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
