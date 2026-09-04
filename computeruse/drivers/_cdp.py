"""Minimal Chrome DevTools Protocol client — the transport behind BrowserDriver.

CDP is JSON over one WebSocket per target (tab). All wire I/O sits behind the
`Transport` protocol so the driver logic stays pure and unit-testable: tests drive
BrowserDriver with a scripted fake transport and never touch a browser. The live
transport lazily imports ``websocket-client`` (the optional ``browser`` extra) and
discovers targets over the CDP HTTP endpoint with stdlib ``urllib``.

The live handshake uses ``suppress_origin`` so no ``Origin`` header is sent —
Chrome otherwise rejects a WebSocket whose origin it was not told to allow
(``--remote-allow-origins``), so this keeps the browser launchable with zero
extra flags. One connection speaks to one page target directly (the
``/devtools/page/<id>`` URL), so no ``Target.attachToTarget``/``sessionId``
plumbing is needed.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from typing import Protocol

from computeruse.schema import ComputerUseError, ErrorCode


class Transport(Protocol):
    """The wire under one CDP session: full-duplex JSON text frames."""

    def send(self, payload: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str: ...

    def close(self) -> None: ...


def discover_targets(endpoint: str) -> list[dict]:
    """``GET {endpoint}/json`` — every debuggable target (tabs, workers, ...).

    ``endpoint`` is the CDP HTTP base, e.g. ``http://127.0.0.1:9222``.
    """
    import urllib.request

    url = f"{endpoint.rstrip('/')}/json"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 (configured CDP)
            raw = resp.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("CDP target discovery exceeded 4 MiB")
        targets = json.loads(raw)
        if not isinstance(targets, list) or any(not isinstance(t, dict) for t in targets):
            raise ValueError("CDP target discovery must return a list of targets")
        return targets
    except (OSError, ValueError) as exc:
        raise ComputerUseError(
            ErrorCode.APP_NOT_FOUND,
            f"no Chrome DevTools endpoint at {endpoint}",
            detail={"hint": "start Chrome/Chromium with --remote-debugging-port=<port> "
                    "(headless=new for CI), then point BrowserDriver at that endpoint.",
                    "error": str(exc)},
        ) from exc


def page_targets(endpoint: str) -> list[dict]:
    """Just the page targets (real tabs), newest CDP order preserved."""
    return [t for t in discover_targets(endpoint) if t.get("type") == "page"]


def connect(ws_url: str, *, timeout: float = 10.0) -> Transport:
    """A live `Transport` to one target's WebSocket (needs the ``browser`` extra)."""
    try:
        import websocket  # websocket-client
    except ImportError as exc:
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            "the CDP browser backend needs websocket-client",
            detail={"hint": "pip install computeruse[browser]", "error": str(exc)},
        ) from exc

    try:
        ws = websocket.create_connection(ws_url, suppress_origin=True,
                                         timeout=timeout)
    except (OSError, websocket.WebSocketException) as exc:
        raise ComputerUseError(
            ErrorCode.APP_NOT_FOUND,
            f"could not open the CDP WebSocket at {ws_url}",
            detail={"error": str(exc)},
        ) from exc
    return _WebSocketTransport(ws)


class _WebSocketTransport:
    """`Transport` over a websocket-client connection (localhost, text frames)."""

    def __init__(self, ws: object) -> None:
        self._ws = ws

    def send(self, payload: str) -> None:
        import websocket

        try:
            self._ws.send(payload)
        except websocket.WebSocketTimeoutException as exc:
            raise TimeoutError(str(exc)) from exc
        except websocket.WebSocketException as exc:
            raise OSError(str(exc)) from exc

    def recv(self, timeout: float | None = None) -> str:
        import websocket

        try:
            self._ws.settimeout(timeout)
            return self._ws.recv()
        except websocket.WebSocketTimeoutException as exc:
            raise TimeoutError(str(exc)) from exc
        except websocket.WebSocketException as exc:
            raise OSError(str(exc)) from exc

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 — close is best-effort
            pass


class CDPSession:
    """Synchronous request/response over one `Transport`.

    CDP multiplexes command replies and unsolicited events on the same socket;
    `call` serializes commands and reads frames until the matching reply arrives, buffering
    events for `drain_events` (used by nothing latency-critical yet — observe is
    a11y-first and `wait_for` re-observes). A CDP ``error`` becomes a structured
    `ComputerUseError`.
    """

    def __init__(self, transport: Transport, *, default_timeout: float = 10.0,
                 event_buffer: int = 4000, event_bytes: int = 4 * 1024 * 1024,
                 max_message_size: int = 64 * 1024 * 1024) -> None:
        if not math.isfinite(default_timeout) or default_timeout <= 0:
            raise ValueError("default_timeout must be finite and positive")
        if event_buffer < 0 or event_bytes < 0 or max_message_size <= 0:
            raise ValueError("event limits must be nonnegative and max_message_size positive")
        self._t = transport
        self._id = 0
        self._lock = threading.Lock()
        self._closed = False
        # Bounded: high-volume domains (Network) can emit events faster than a
        # caller drains them; the oldest silently drop instead of growing without
        # limit. Console/Log are low-volume, so this only ever bites network bursts.
        self._events: deque[dict] = deque(maxlen=event_buffer)
        self._event_sizes: deque[int] = deque()
        self._event_bytes = 0
        self._max_event_bytes = event_bytes
        self._max_message_size = max_message_size
        self.dropped_events = 0
        self._default_timeout = default_timeout

    def call(self, method: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
        """Send once and wait for its reply; queueing consumes the same timeout.

        Commands are never automatically retried: a timed-out click may already
        have executed. Late replies are ignored by id on the next call.
        """
        timeout = self._default_timeout if timeout is None else timeout
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        deadline = time.monotonic() + timeout
        if not self._lock.acquire(timeout=timeout):
            raise self._timeout(method, timeout, sent=False)
        try:
            if self._closed:
                raise ComputerUseError(ErrorCode.APP_NOT_FOUND, "CDP session is closed",
                                       detail={"method": method, "sent": False})
            if time.monotonic() >= deadline:
                raise self._timeout(method, timeout, sent=False)
            return self._call(method, params, timeout, deadline)
        finally:
            self._lock.release()

    def _call(self, method: str, params: dict | None, timeout: float, deadline: float) -> dict:
        self._id += 1
        mid = self._id
        try:
            self._t.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        except TimeoutError as exc:
            # A partial send leaves the stream unusable; reconnect on next use.
            self._disconnect()
            raise self._timeout(method, timeout, sent=True) from exc
        except OSError as exc:
            raise self._connection_error(method, str(exc)) from exc
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._timeout(method, timeout, sent=True)
            try:
                raw = self._t.recv(timeout=remaining)
            except TimeoutError as exc:
                raise self._timeout(method, timeout, sent=True) from exc
            except OSError as exc:
                raise self._connection_error(method, str(exc)) from exc
            if not raw:
                raise self._connection_error(method, "the target closed the connection")
            if len(raw) > self._max_message_size:
                raise self._connection_error(method, "CDP message exceeded max_message_size")
            try:
                msg = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise self._connection_error(method, "invalid JSON from CDP") from exc
            if not isinstance(msg, dict):
                raise self._connection_error(method, "CDP message is not an object")
            if msg.get("id") == mid:
                if "error" in msg:
                    err = msg["error"]
                    if not isinstance(err, dict):
                        raise self._connection_error(method, "invalid CDP error reply")
                    raise ComputerUseError(
                        ErrorCode.UNSUPPORTED,
                        f"CDP {method} failed: {err.get('message', err)}",
                        detail={"method": method, "code": err.get("code")},
                    )
                result = msg.get("result", {})
                if not isinstance(result, dict):
                    raise self._connection_error(method, "invalid CDP result reply")
                return result
            if "method" in msg:  # an event; keep it for anyone draining
                self._buffer_event(msg, len(raw))

    def _buffer_event(self, event: dict, size: int) -> None:
        if not self._events.maxlen or size > self._max_event_bytes:
            self.dropped_events += 1
            return
        while self._events and (len(self._events) == self._events.maxlen
                                or self._event_bytes + size > self._max_event_bytes):
            self._events.popleft()
            self._event_bytes -= self._event_sizes.popleft()
            self.dropped_events += 1
        self._events.append(event)
        self._event_sizes.append(size)
        self._event_bytes += size

    @staticmethod
    def _timeout(method: str, timeout: float, *, sent: bool) -> ComputerUseError:
        return ComputerUseError(
            ErrorCode.TIMEOUT, f"CDP {method} did not reply within {timeout}s",
            detail={"method": method, "sent": sent, "outcome_unknown": sent},
        )

    def _connection_error(self, method: str, error: str) -> ComputerUseError:
        self._disconnect()
        return ComputerUseError(
            ErrorCode.APP_NOT_FOUND, f"CDP {method} lost its connection: {error}",
            detail={"method": method, "outcome_unknown": True},
        )

    @property
    def closed(self) -> bool:
        return self._closed

    def drain_events(self) -> list[dict]:
        """Return and clear the events buffered while waiting on `call` replies."""
        with self._lock:
            out = list(self._events)
            self._events.clear()
            self._event_sizes.clear()
            self._event_bytes = 0
            return out

    def close(self) -> None:
        with self._lock:
            self._disconnect()

    def _disconnect(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._t.close()
            except OSError:
                pass  # a broken transport must not mask the command's error


__all__ = ["Transport", "CDPSession", "connect", "discover_targets", "page_targets"]
