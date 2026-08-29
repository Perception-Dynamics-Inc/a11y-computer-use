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
import time
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
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 (localhost CDP)
            return json.loads(resp.read().decode())
    except OSError as exc:
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
        ws = websocket.create_connection(ws_url, max_size=None, suppress_origin=True,
                                         timeout=timeout)
    except OSError as exc:
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
        self._ws.send(payload)

    def recv(self, timeout: float | None = None) -> str:
        self._ws.settimeout(timeout)
        return self._ws.recv()

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 — close is best-effort
            pass


class CDPSession:
    """Synchronous request/response over one `Transport`.

    CDP multiplexes command replies and unsolicited events on the same socket;
    `call` reads frames until the reply with the matching id arrives, buffering
    events for `drain_events` (used by nothing latency-critical yet — observe is
    a11y-first and `wait_for` re-observes). A CDP ``error`` becomes a structured
    `ComputerUseError`.
    """

    def __init__(self, transport: Transport, *, default_timeout: float = 10.0) -> None:
        self._t = transport
        self._id = 0
        self._events: list[dict] = []
        self._default_timeout = default_timeout

    def call(self, method: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
        timeout = self._default_timeout if timeout is None else timeout
        self._id += 1
        mid = self._id
        self._t.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ComputerUseError(
                    ErrorCode.TIMEOUT,
                    f"CDP {method} did not reply within {timeout}s",
                    detail={"method": method},
                )
            raw = self._t.recv(timeout=remaining)
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    err = msg["error"]
                    raise ComputerUseError(
                        ErrorCode.UNSUPPORTED,
                        f"CDP {method} failed: {err.get('message', err)}",
                        detail={"method": method, "code": err.get("code")},
                    )
                return msg.get("result", {})
            if "method" in msg:  # an event; keep it for anyone draining
                self._events.append(msg)

    def drain_events(self) -> list[dict]:
        """Return and clear the events buffered while waiting on `call` replies."""
        out, self._events = self._events, []
        return out

    def close(self) -> None:
        self._t.close()


__all__ = ["Transport", "CDPSession", "connect", "discover_targets", "page_targets"]
