"""HTTP API for one agent process.

``a11y-agent serve`` binds ``127.0.0.1`` unless ``--host`` says otherwise.
A bearer token is optional on loopback and required for any other host.
The implementation is the stdlib HTTP server so the ``[agent]`` extra stays
``httpx`` for model clients only.
"""

from __future__ import annotations

import ipaddress
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from a11y_computer_use.agent.service import DisplayBusy, RunStore, _unknown_field_message

_MAX_BODY = 1_000_000


def loopback_host(host: str) -> bool:
    """True for localhost names and loopback IP addresses."""
    text = host.strip().strip("[]")
    if text.lower() in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def bind_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
    store: RunStore | None = None,
    approval_timeout_s: float = 60.0,
) -> ThreadingHTTPServer:
    """Bind the agent HTTP server. Does not serve until ``serve_forever``."""
    if not loopback_host(host) and not token:
        raise ValueError("a bearer token is required when binding beyond localhost")
    if token is not None and not str(token):
        raise ValueError("token must be non-empty when set")
    shared = store if store is not None else RunStore(approval_timeout_s=approval_timeout_s)
    handler = _handler_class(shared, None if token is None else str(token))
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
    approval_timeout_s: float = 60.0,
    store: RunStore | None = None,
) -> None:
    """Bind and serve until the process is interrupted."""
    httpd = bind_server(
        host=host,
        port=port,
        token=token,
        store=store,
        approval_timeout_s=approval_timeout_s,
    )
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def _handler_class(shared_store: RunStore, shared_token: str | None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        store = shared_store
        token = shared_token

        def log_message(self, fmt: str, *args: object) -> None:
            del fmt, args

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            parts = _parts(self.path)
            if len(parts) == 2 and parts[0] == "runs":
                self._get_run(parts[1])
                return
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "events":
                self._events(parts[1])
                return
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "trace":
                self._trace(parts[1])
                return
            if len(parts) == 4 and parts[0] == "runs" and parts[2] == "trace":
                self._trace_file(parts[1], parts[3])
                return
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            parts = _parts(self.path)
            try:
                body = self._json()
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            if parts == ["runs"]:
                self._start(body)
                return
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "cancel":
                self._cancel(parts[1], body)
                return
            if len(parts) == 4 and parts[0] == "runs" and parts[2] == "approvals":
                self._approve(parts[1], parts[3], body)
                return
            self._send(404, {"error": "not found"})

        def _authorized(self) -> bool:
            expected = type(self).token
            if expected is None:
                return True
            header = self.headers.get("Authorization", "")
            presented = header[7:] if header.startswith("Bearer ") else ""
            if len(presented) != len(expected) or not _same(presented, expected):
                self._send(401, {"error": "bearer token required"})
                return False
            return True

        def _json(self) -> dict:
            length = self.headers.get("Content-Length")
            if length is None:
                return {}
            try:
                size = int(length)
            except ValueError as exc:
                raise ValueError("Content-Length must be an integer") from exc
            if size < 0 or size > _MAX_BODY:
                raise ValueError("body is too large")
            raw = self.rfile.read(size)
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("body must be JSON") from exc
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            return data

        def _start(self, body: dict) -> None:
            try:
                record = type(self).store.start(body)
            except DisplayBusy as exc:
                self._send(409, {
                    "error": "a run is already active on this display",
                    "display": exc.display,
                    "run_id": exc.run_id,
                })
                return
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            self._send(202, {"id": record.id})

        def _get_run(self, run_id: str) -> None:
            record = type(self).store.get(run_id)
            if record is None:
                self._send(404, {"error": "run not found"})
                return
            self._send(200, type(self).store.view(record))

        def _cancel(self, run_id: str, body: dict) -> None:
            message = _unknown_field_message(body, set())
            if message:
                self._send(400, {"error": message})
                return
            record = type(self).store.cancel(run_id)
            if record is None:
                self._send(404, {"error": "run not found"})
                return
            self._send(202, {"id": record.id, "cancel": True})

        def _approve(self, run_id: str, approval_id: str, body: dict) -> None:
            message = _unknown_field_message(body, {"approve"})
            if message:
                self._send(400, {"error": message})
                return
            if "approve" not in body or not isinstance(body.get("approve"), bool):
                self._send(400, {"error": "approve must be a boolean"})
                return
            outcome = type(self).store.resolve_approval(run_id, approval_id, body["approve"])
            if outcome == "missing":
                self._send(404, {"error": "approval not found"})
                return
            if outcome == "answered":
                self._send(409, {"error": "approval already answered"})
                return
            self._send(200, {"approval_id": approval_id, "approve": body["approve"]})

        def _events(self, run_id: str) -> None:
            record = type(self).store.get(run_id)
            if record is None:
                self._send(404, {"error": "run not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            cursor = 0
            try:
                while True:
                    with record.cond:
                        while cursor >= len(record.events) and not record.done:
                            record.cond.wait(timeout=15)
                        batch = list(record.events[cursor:])
                        cursor = len(record.events)
                        finished = record.done and cursor >= len(record.events)
                    for event in batch:
                        blob = json.dumps(event, ensure_ascii=False)
                        chunk = f"id: {event['seq']}\nevent: {event['type']}\ndata: {blob}\n\n"
                        self.wfile.write(chunk.encode("utf-8"))
                    if not batch and not finished:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    if finished:
                        break
            except (BrokenPipeError, ConnectionResetError):
                return

        def _trace(self, run_id: str) -> None:
            record = type(self).store.get(run_id)
            if record is None:
                self._send(404, {"error": "run not found"})
                return
            self._send(200, {
                "trajectory": type(self).store.trajectory(record),
                "files": type(self).store.trace_files(record),
            })

        def _trace_file(self, run_id: str, name: str) -> None:
            record = type(self).store.get(run_id)
            if record is None:
                self._send(404, {"error": "run not found"})
                return
            path = type(self).store.trace_file(record, name)
            if path is None:
                self._send(404, {"error": "trace file not found"})
                return
            data = path.read_bytes()
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send(self, status: int, body: dict) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def _parts(path: str) -> list[str]:
    parsed = urlparse(path).path
    text = unquote(parsed).strip("/")
    if not text:
        return []
    return text.split("/")


def _same(presented: str, expected: str) -> bool:
    import secrets

    return secrets.compare_digest(presented, expected)


__all__ = ["bind_server", "loopback_host", "serve"]
