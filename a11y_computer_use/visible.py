"""Best-effort Linux action feedback, independent of native action dispatch."""
from __future__ import annotations

import logging
import math
import os
import queue
import threading
import time
from contextlib import contextmanager

_LOG = logging.getLogger(__name__)


def enabled(config=None):
    value = os.environ.get("A11Y_VISIBLE_ACTIONS")
    return bool(os.environ.get("DISPLAY")) and not (
        os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland"
    ) and (value != "0" if value is not None else config is not False)


def usable(bounds):
    return bounds is not None and all(math.isfinite(v) for v in (
        bounds.x, bounds.y, bounds.width, bounds.height
    )) and bounds.width > 0 and bounds.height > 0


class VisibleActions:
    def __init__(self):
        self.pending = queue.Queue(maxsize=1)
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.thread = None
        self.disabled = False
        self.capturing = 0
        self.mapped = False

    def prepare(self, kind, label, bounds, config=None, *, secure=False, glide=True):
        """Optional caller-thread glide, followed by asynchronous announcement.

        ``glide=False`` keeps the highlight but sends no pointer motion; the
        driver passes it where the glide would sit on the action's critical
        path (GTK 4 clicks, PR #719).
        """
        try:
            if glide and enabled(config) and not self.disabled and usable(bounds) and not secure:
                if os.environ.get("A11Y_VISIBLE_ACTIONS_POINTER", "0") == "1":
                    self._glide(bounds)
        except Exception:
            self.disabled = True
            _LOG.debug("Visible pointer unavailable; disabling feedback", exc_info=True)
        self.announce(kind, label, bounds, config)

    def announce(self, kind, label, bounds, config=None):
        """Enqueue latest feedback; never wait for rendering or expiry."""
        try:
            if not enabled(config) or self.disabled or not usable(bounds):
                _LOG.debug("visible: skipped(disabled/no_bounds)")
                return
            if os.environ.get("A11Y_VISIBLE_ACTIONS_HIGHLIGHT", "1") == "0":
                return
            item = (bounds, f"{kind}: {str(label).splitlines()[0][:24]}" if label else kind)
            with self.condition:
                if self.capturing:
                    return
                if self.thread is None:
                    self.thread = threading.Thread(target=self._consume, daemon=True,
                                                   name="a11y-visible-actions")
                    self.thread.start()
                try:
                    self.pending.get_nowait()
                except queue.Empty:
                    pass
                self.pending.put_nowait(item)
                self.condition.notify_all()
        except Exception:
            _LOG.debug("Visible actions unavailable", exc_info=True)

    def _glide(self, bounds):
        """Move the pointer toward the target centre within a hard wall-time cap.

        Total wall time is at most ``A11Y_VISIBLE_ACTIONS_GLIDE_MS`` (default 80,
        max 120). A monotonic deadline ends intermediate steps early and jumps
        to the target. Each step is flushed (a non-blocking write) so the
        server moves the pointer in real time, and the final move is synced so
        the pointer is at the target when this returns. No AT-SPI work runs.
        Pointer mode stays off by default.
        """
        from a11y_computer_use.drivers import _linux_input
        try:
            duration = float(os.environ.get("A11Y_VISIBLE_ACTIONS_GLIDE_MS", "80"))
        except ValueError:
            duration = 80
        if not math.isfinite(duration) or not 0 <= duration <= 120:
            duration = 80
        deadline = time.monotonic() + duration / 1000.0
        connection = _linux_input._disp()
        if not all(connection.has_extension(ext) for ext in ("SHAPE", "XTEST")):
            self.disabled = True
            _LOG.debug("Visible actions require XShape/XTest; disabling feedback")
            return
        screen = connection.screen()
        if not (0 <= bounds.x < screen.width_in_pixels and 0 <= bounds.y < screen.height_in_pixels
                and bounds.x + bounds.width <= screen.width_in_pixels
                and bounds.y + bounds.height <= screen.height_in_pixels):
            return
        start = screen.root.query_pointer()
        target_x = round(bounds.x + bounds.width / 2)
        target_y = round(bounds.y + bounds.height / 2)
        for step in range(1, 9):
            if time.monotonic() >= deadline:
                break
            t = step / 8
            eased = t * t * (3 - 2 * t)
            _linux_input._move(
                round(start.root_x + (target_x - start.root_x) * eased),
                round(start.root_y + (target_y - start.root_y) * eased),
            )
            connection.flush()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(duration / 8000.0, remaining))
        _linux_input._move(target_x, target_y)
        connection.sync()

    def _consume(self):
        backend = None
        try:
            from a11y_computer_use._x11_overlay import X11Overlay
            backend = X11Overlay()
            while True:
                bounds, label = self.pending.get()
                with self.condition:
                    if self.capturing:
                        continue
                    self.mapped = True
                backend.show(bounds, label)
                # Wake early for capture or newer feedback.
                with self.condition:
                    self.condition.wait_for(lambda: self.capturing or not self.pending.empty(), timeout=0.3)
                backend.hide()
                with self.condition:
                    self.mapped = False
                    self.condition.notify_all()
        except Exception:
            _LOG.debug("Visible actions X11 unavailable; disabling", exc_info=True)
            with self.condition:
                self.disabled = True
                self.mapped = False
                self.condition.notify_all()
        finally:
            if backend is not None:
                backend.close()

    @contextmanager
    def capture(self):
        """Keep overlay unmapped for the entire screen capture."""
        with self.condition:
            self.capturing += 1
            self.condition.notify_all()
            self.condition.wait_for(lambda: not self.mapped)
        try:
            yield
        finally:
            with self.condition:
                self.capturing -= 1


visible_actions = VisibleActions()
